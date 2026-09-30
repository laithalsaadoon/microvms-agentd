# SPDX-License-Identifier: Apache-2.0
"""Tunnel identity: a VM launched `--identity` proving itself through the real endpoint
proxy, and a tampered pin failing closed."""

from __future__ import annotations

import json
import secrets
import subprocess
import time
from pathlib import Path

import httpx
from harness.cli import Cli
from harness.constants import AGENT_PORT, BASELINE_MEMORY_MIB
from harness.envelope import Envelope, KindError
from harness.redact import command_for_log
from harness.results import Results


def _tunnel_fetch(
    cli: Cli,
    vm_name: str,
    state_dir: Path,
    local_port: int,
    guest_port: int,
    verify: bool,
) -> str | None:
    """One HTTP GET through `microvm tunnel`, or `None` when nothing was served.

    The tunnel is a foreground process serving until Ctrl-C, so it runs as a Popen with
    `--max-connections 1`: the fetch is the one connection, and the process then exits on
    its own. The GET goes through raw sockets via httpx against localhost — the tunnel is
    the thing under test, so the client on this side of it should be anything but the code
    under test.
    """
    argv = cli.argv(
        "tunnel",
        f"{local_port}:{guest_port}",
        "--name",
        vm_name,
        "--state-dir",
        str(state_dir),
        "--region",
        cli.region,
        "--max-connections",
        "1",
        *(["--verify-identity"] if verify else []),
    )
    cli.log.append(command_for_log(argv))
    with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
        try:
            body: str | None = None
            # The listener needs a moment to bind; the retry is the wait.
            for _ in range(20):
                time.sleep(0.5)
                try:
                    answer = httpx.get(
                        f"http://127.0.0.1:{local_port}/v1/schema", timeout=10.0
                    )
                    if answer.status_code == 200:
                        body = answer.text
                    break
                except httpx.TransportError:
                    continue
            proc.wait(timeout=30)
            return body
        finally:
            if proc.poll() is None:
                proc.kill()


def drive_tunnel_identity(
    cli: Cli, launched: Envelope, state_dir: Path, results: Results
) -> None:
    """Tunnel identity (issue #70 layer 3): prove the VM, fail closed. Eight checks,
    two of them `attach --verify-identity` (issue #66) over the same pin.

    Its own VM, launched `--identity` from the image the suite already built: the seed is
    delivered only at launch, so the suite's VM — launched without one — cannot carry it.
    That VM is exactly what the no-downgrade check needs, so both launches earn their keep.

    Live rather than only scripted for the named-VMs class of reason: the Noise handshake
    rides binary WebSocket frames through the real endpoint proxy, and every local test of
    it speaks to a stand-in. The proxy's binary fidelity is measured for relay chunks;
    "measured for a cryptographic handshake whose failure mode is a silent 1006" is a
    different claim until one live run makes them the same.

    The tampered-pin check flips one base64 character mid-string, so length and padding
    stay legal and the refusal has to come from the cryptography rather than a parser.
    That is the replayed-record case from the issue's acceptance list, expressed as the
    smallest possible record edit.
    """
    print("\n-- tunnel identity (#70 layer 3: prove the VM, fail closed) --")
    vm_name = f"conformance-ident-{secrets.token_hex(4)}"
    named = cli.call(
        "run",
        "--image",
        str(launched.data["imageIdentifier"]),
        "--name",
        f"microvm-cli-conformance-ident-{secrets.token_hex(4)}",
        "--memory",
        str(BASELINE_MEMORY_MIB),
        "--keep",
        "--identity",
        "--vm-name",
        vm_name,
        "--state-dir",
        str(state_dir),
        "--region",
        cli.region,
        "--max-idle-sec",
        "600",
        "--suspended-sec",
        "600",
        "--max-duration-sec",
        "1800",
        timeout=15 * 60,
    )
    microvm_id = str(named.data["microvmId"])
    try:
        host_seed = named.data.get("identityHostSeed")
        vm_pin = named.data.get("identityVmPublicKey")
        results.check(
            "run --identity reported the identity pair on the envelope",
            isinstance(host_seed, str)
            and isinstance(vm_pin, str)
            and len(host_seed) == 44
            and len(vm_pin) == 44
            and host_seed != vm_pin,
            f"hostSeed={type(host_seed).__name__} pin={type(vm_pin).__name__}",
        )

        record_path = state_dir / "names" / f"{vm_name}.json"
        record = json.loads(record_path.read_text())
        results.check(
            "the registry record carries the host seed and the public pin",
            record.get("identityHostSeed") == host_seed
            and record.get("identityVmPublicKey") == vm_pin,
            f"record keys: {sorted(record.keys())}",
        )

        # The guest server is the daemon itself: al2023-minimal ships no python3, and the
        # daemon's unauthenticated `GET /v1/schema` on 127.0.0.1:9000 is already an HTTP
        # server whose body could only come from plaintext inside the guest. Tunnelling to
        # the daemon's own port also exercises the exact loopback dial the relay performs
        # for any other guest service.
        verified = _tunnel_fetch(
            cli, vm_name, state_dir, 18443, AGENT_PORT, verify=True
        )
        results.check(
            "a verified tunnel served a request through the real proxy",
            verified is not None and '"protocol_version"' in verified,
            f"body: {verified[:80] if verified else verified!r}",
        )

        # `attach --verify-identity` (issue #66) runs the tunnel's handshake with no relay
        # after it, into a second state directory. Live because the handshake's proxy-side
        # failure mode is a silent 1006, the same reason the tunnel checks are live.
        adopted_dir = state_dir.parent / f"{state_dir.name}-adopted"
        adopted_name = f"adopted-ident-{secrets.token_hex(4)}"
        verified_attach = cli.call(
            "attach",
            "--from",
            str(record_path),
            "--name",
            adopted_name,
            "--verify-identity",
            "--state-dir",
            str(adopted_dir),
        )
        results.check(
            "attach --verify-identity proved the VM against the pinned key",
            verified_attach.data.get("verifiedIdentity") is True
            and verified_attach.data.get("microvmId") == microvm_id
            and (adopted_dir / "names" / f"{adopted_name}.json").exists(),
            f"verifiedIdentity={verified_attach.data.get('verifiedIdentity')!r}",
        )

        # The replayed-record case: one flipped pin character mid-base64 (length and
        # padding stay legal), handshake must fail, nothing served.
        pin = record["identityVmPublicKey"]
        flipped = pin[:20] + ("A" if pin[20] != "A" else "B") + pin[21:]
        record_path.write_text(json.dumps({**record, "identityVmPublicKey": flipped}))
        results.absent(
            "AGENTD-18 a tampered pin fails closed with nothing served",
            _tunnel_fetch(cli, vm_name, state_dir, 18444, AGENT_PORT, verify=True),
        )
        # And the same tampered record cannot be adopted under the flag: the handshake
        # reply fails to verify against the flipped pin, and no record is written.
        tampered_name = f"tampered-{secrets.token_hex(4)}"
        try:
            cli.call(
                "attach",
                "--from",
                str(record_path),
                "--name",
                tampered_name,
                "--verify-identity",
                "--state-dir",
                str(adopted_dir),
            )
            results.check(
                "BIND-21 attach --verify-identity with a tampered pin is refused and writes nothing",
                False,
                "no refusal",
            )
        except KindError as exc:
            results.check(
                "BIND-21 attach --verify-identity with a tampered pin is refused and writes nothing",
                exc.code == "ERR_PRECONDITION"
                and not (adopted_dir / "names" / f"{tampered_name}.json").exists(),
                f"code={exc.code} written={(adopted_dir / 'names' / f'{tampered_name}.json').exists()}",
            )
        record_path.write_text(json.dumps(record))

        # And the same record still works untampered — so the refusal above was the flip,
        # not something the tampering round trip broke.
        restored = _tunnel_fetch(
            cli, vm_name, state_dir, 18445, AGENT_PORT, verify=True
        )
        results.check(
            "the restored record verifies again",
            restored is not None and '"protocol_version"' in restored,
            f"body: {restored[:80] if restored else restored!r}",
        )
    finally:
        gone = cli.call(
            "terminate",
            vm_name,
            "--wait",
            "--state-dir",
            str(state_dir),
            "--region",
            cli.region,
        )
        results.check(
            "the identity VM tore down clean",
            gone.data.get("microvmId") == microvm_id and not gone.data.get("leaked"),
            f"microvm={gone.data.get('microvmId')} leaked={gone.data.get('leaked')}",
        )
