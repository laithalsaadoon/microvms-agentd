# SPDX-License-Identifier: Apache-2.0
"""Named VMs: register at launch, address and adopt by name, collide, list against the
account, and release, with every `--name` read taking the region from the VM's record."""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

from harness.cli import Cli
from harness.constants import BASELINE_MEMORY_MIB
from harness.envelope import Envelope, KindError
from harness.results import Results


def terminate_by_name_from_elsewhere(
    cli: "Cli",
    results: "Results",
    vm_name: str,
    microvm_id: str,
    state_dir: Path,
    elsewhere: str,
    elsewhere_env: dict[str, str],
) -> Envelope:
    """`drive_named_vm`'s terminate by name from another region's environment (#251), and
    the terminate by id that stops a miss from leaking the VM.

    A terminate that misses the VM doesn't raise. A failed `TerminateMicrovm` puts the id in
    `leaked` on a success envelope (exit Platform), so the fallback reads the envelope as
    well as the exception. Reading only the exception would skip it on the very regression
    the check is for, and leave the VM billing until its maximum duration with its name
    still registered. The check also needs the `--wait` state `TERMINATED`: nobody has
    measured what the service answers to a `TerminateMicrovm` for an id another region
    holds, and if it's a no-op success, a terminate sent to the wrong region reports the id
    with nothing leaked. Its `--wait` then reads `GetMicrovm` in that region, misses, and
    leaves `TERMINATING`, so the state tells the miss apart whatever the delete answered.
    Returns the envelope the checks after this one read: the named terminate's when it
    reached the VM, the fallback's otherwise.
    """
    by_name = "terminate by name from another region's environment reached the VM"
    gone: Envelope | None = None
    try:
        gone = cli.call(
            "terminate",
            vm_name,
            "--wait",
            "--state-dir",
            str(state_dir),
            env=elsewhere_env,
        )
        detail = (
            f"environment={elsewhere} microvm={gone.data.get('microvmId')} "
            f"state={gone.data.get('state')} leaked={gone.data.get('leaked')}"
        )
    except KindError as exc:
        detail = (
            f"environment={elsewhere} kind={exc.kind} code={exc.code} "
            f"error={exc.envelope.error!r}"
        )
    reached = (
        gone is not None
        and gone.data.get("microvmId") == microvm_id
        and gone.data.get("state") == "TERMINATED"
        and not gone.data.get("leaked")
    )
    results.check(by_name, reached, detail)
    if reached and gone is not None:
        return gone
    # Terminate it by id in its own region, which releases the name too, so the checks
    # after this one still read an envelope.
    return cli.call(
        "terminate",
        microvm_id,
        "--wait",
        "--state-dir",
        str(state_dir),
        "--region",
        cli.region,
    )


def drive_named_vm(
    cli: Cli, launched: Envelope, state_dir: Path, results: Results
) -> None:
    """Named VMs (issue #67): register at launch, address by name, collide, release —
    and adopt across state directories (issue #66).

    Checks against a VM this section launches and terminates itself, from the image
    the suite already built (`run --image`, no second build). Its own VM rather than the
    suite's because registration happens only at launch — the suite's VM was launched
    before any name existed to give it.

    The first live run of this feature is the reason this section exists: the client-side
    passthrough was keyed on the fixtures' `mvm-` id prefix, every scripted test passed,
    and the real service's `microvm-` prefix sent a raw id into the name registry and
    refused a legal suspend. A prefix is exactly the kind of fact only the service can
    state, so the resolution path stays under live coverage permanently.

    `--state-dir` is this run's own temp dir, so the registry under test is this suite's
    and a developer's `~/.microvm/runs` is never touched.

    Issue #251: a name's record carries its region, and every `--name` read takes it from
    there. `keepalive --name` and the terminate by name run from an environment whose
    `AWS_REGION` names another region, with no region flag, and must still reach the VM.
    Before the fix the keepalive read the idle window in the environment's region, missed,
    and refused its interval against the 60-second fallback; the terminate went to the
    environment's region, where no such VM exists.
    """
    print("\n-- named VMs (register / resolve / collide / release) --")
    vm_name = f"conformance-named-{secrets.token_hex(4)}"
    # Any region that isn't the VM's: the environment the #251 checks run from.
    elsewhere = "us-west-2" if cli.region != "us-west-2" else "us-east-1"
    elsewhere_env = {
        **os.environ,
        "AWS_REGION": elsewhere,
        "AWS_DEFAULT_REGION": elsewhere,
    }
    named = cli.call(
        "run",
        "--image",
        str(launched.data["imageIdentifier"]),
        "--name",
        f"microvm-cli-conformance-named-{secrets.token_hex(4)}",
        "--memory",
        str(BASELINE_MEMORY_MIB),
        "--keep",
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
        results.eq(
            "run --keep --vm-name registered the name it reported",
            named.data.get("vmName"),
            vm_name,
        )

        # `ls` (issue #159): the plain form says what it is — a local ledger — and asks
        # the account nothing; `--remote` asks, through the same control plane, and marks
        # the kept run `live` because the service lists its VM. Both against this
        # section's own state directory, which holds exactly the kept run's record.
        plain = cli.call("ls", "--state-dir", str(state_dir))
        results.check(
            "plain ls names its source as the local ledger, remote null, and lists the kept run",
            plain.data.get("source") == "local-ledger"
            and plain.data.get("remote") is None
            and plain.data.get("pruned") == []
            and any(
                run.get("microvmId") == microvm_id for run in plain.data.get("runs", [])
            ),
            f"source={plain.data.get('source')!r} remote={plain.data.get('remote')!r} "
            f"runs={[run.get('microvmId') for run in plain.data.get('runs', [])]}",
        )
        listed = cli.call(
            "ls", "--remote", "--state-dir", str(state_dir), "--region", cli.region
        )
        remote = listed.data.get("remote") or {}
        mine = [
            entry
            for entry in remote.get("entries", [])
            if entry.get("microvmId") == microvm_id
        ]
        results.check(
            "ls --remote marks the kept run live while the account lists its VM",
            listed.data.get("source") == "local-ledger"
            and remote.get("region") == cli.region
            and len(mine) == 1
            and mine[0].get("status") == "live"
            and mine[0].get("microvmState") in {"PENDING", "RUNNING"}
            and microvm_id in {vm.get("microvmId") for vm in remote.get("microvms", [])}
            and microvm_id
            not in (remote.get("unknownToLedger") or {}).get("microvms", [])
            and listed.data.get("pruned") == [],
            f"region={remote.get('region')!r} entries={mine} "
            f"pruned={listed.data.get('pruned')!r}",
        )

        first = cli.call(
            "exec", "echo named", "--name", vm_name, "--state-dir", str(state_dir)
        )
        results.check(
            "exec --name attached with the registered record",
            first.data.get("exitCode") == 0
            and "named" in (first.data.get("stdout") or ""),
            f"exit={first.data.get('exitCode')} stdout={first.data.get('stdout')!r}",
        )

        # 120 s is legal only against the VM's own 600 s window: the 60 s fallback a
        # read in the wrong region leaves would refuse it before any poll.
        check = "keepalive --name from another region's environment read the VM's own idle window"
        try:
            held = cli.call(
                "keepalive",
                "--name",
                vm_name,
                "--state-dir",
                str(state_dir),
                "--interval",
                "120",
                "--for",
                "5",
                env=elsewhere_env,
            )
            results.check(
                check,
                held.data.get("idleWindowSec") == 600.0
                and held.data.get("end") == "elapsed",
                f"environment={elsewhere} idleWindowSec={held.data.get('idleWindowSec')!r} "
                f"end={held.data.get('end')!r}",
            )
        except KindError as exc:
            results.check(
                check,
                False,
                f"environment={elsewhere} kind={exc.kind} code={exc.code} "
                f"error={exc.envelope.error!r}",
            )

        # Cross-machine adoption (issue #66), with a second state directory standing in
        # for the second machine: the file this launch wrote is the export format, and a
        # registry that never launched the VM can act on it after one probe. Then the
        # negative: the same record with its token altered must be refused by the real
        # daemon's bearer check — the whole reason the probe is an authenticated read
        # and not `/v1/health` — and must leave the second registry untouched.
        adopted_dir = state_dir.parent / f"{state_dir.name}-adopted"
        record_path = state_dir / "names" / f"{vm_name}.json"
        adopted_name = f"adopted-{secrets.token_hex(4)}"
        adopted = cli.call(
            "attach",
            "--from",
            str(record_path),
            "--name",
            adopted_name,
            "--state-dir",
            str(adopted_dir),
        )
        results.check(
            "attach --from adopted the record into a state dir that never launched the VM",
            adopted.type == "microvm.attach"
            and adopted.data.get("name") == adopted_name
            and adopted.data.get("microvmId") == microvm_id
            and adopted.data.get("verifiedIdentity") is False
            and adopted.data.get("replaced") is False
            and "agentToken" not in adopted.data
            and (adopted_dir / "names" / f"{adopted_name}.json").exists(),
            f"type={adopted.type} keys={sorted(adopted.data)} microvm={adopted.data.get('microvmId')}",
        )
        through = cli.call(
            "exec",
            "echo adopted",
            "--name",
            adopted_name,
            "--state-dir",
            str(adopted_dir),
        )
        results.check(
            "exec --name through the adopted record reached the VM",
            through.data.get("exitCode") == 0
            and "adopted" in (through.data.get("stdout") or ""),
            f"exit={through.data.get('exitCode')} stdout={through.data.get('stdout')!r}",
        )
        record = json.loads(record_path.read_text())
        wrong_token = json.dumps({**record, "agentToken": record["agentToken"] + "x"})
        wrong_name = f"wrong-{secrets.token_hex(4)}"
        wrong_path = adopted_dir / "wrong-token.json"
        wrong_path.write_text(wrong_token)
        try:
            cli.call(
                "attach",
                "--from",
                str(wrong_path),
                "--name",
                wrong_name,
                "--state-dir",
                str(adopted_dir),
            )
            results.check(
                "attach with a wrong token is refused by the daemon and writes nothing",
                False,
                "no refusal",
            )
        except KindError as exc:
            results.check(
                "attach with a wrong token is refused by the daemon and writes nothing",
                exc.kind == "Unauthorized"
                and exc.code == "ERR_CREDENTIALS"
                and not (adopted_dir / "names" / f"{wrong_name}.json").exists(),
                f"kind={exc.kind} code={exc.code} written={(adopted_dir / 'names' / f'{wrong_name}.json').exists()}",
            )

        # The collision: a second launch under the live name must be refused locally,
        # with the appended row, before anything is billed. The refusal arriving at all
        # is the check; the zero-AWS-calls half is the behavioral guard's claim
        # (`guards.rs`, RefusingSeam) because no live run can see an absent request.
        try:
            cli.call(
                "run",
                "--image",
                str(launched.data["imageIdentifier"]),
                "--keep",
                "--vm-name",
                vm_name,
                "--state-dir",
                str(state_dir),
                "--region",
                cli.region,
            )
            results.check(
                "reusing a live name is refused with ERR_NAME_TAKEN",
                False,
                "no refusal",
            )
        except KindError as exc:
            results.check(
                "reusing a live name is refused with ERR_NAME_TAKEN",
                exc.code == "ERR_NAME_TAKEN" and exc.exit_code == 14,
                f"code={exc.code} exit={exc.exit_code}",
            )
    finally:
        # Terminate **by the name**, which is itself the lifecycle-positional resolution
        # under test — and never `--delete-image`, because the image is the suite's own.
        # No region flag and another region's environment (#251): the record's region is
        # the only thing that can send it to the VM.
        gone = terminate_by_name_from_elsewhere(
            cli, results, vm_name, microvm_id, state_dir, elsewhere, elsewhere_env
        )
        results.check(
            "terminate accepted the name and reported the id it resolved to",
            gone.data.get("microvmId") == microvm_id and not gone.data.get("leaked"),
            f"microvm={gone.data.get('microvmId')} leaked={gone.data.get('leaked')}",
        )
        results.check(
            "an accepted terminate released the name for reuse",
            not (state_dir / "names" / f"{vm_name}.json").exists(),
            f"registry entry survives: {sorted(p.name for p in (state_dir / 'names').glob('*.json')) if (state_dir / 'names').exists() else []}",
        )

        # `ls --remote` after the terminate (issue #159). A record naming only the VM is
        # what a process that died after its terminate was accepted leaves behind, and
        # the listing — not the ledger — is what says it is gone. Written by hand so the
        # check owns a record whose shape it knows; the kept run's own record is judged
        # beside it, whatever a sibling change made terminate do with it. (First live
        # run, 2026-09-12: a `run --image` records no image, so that record named only
        # the terminated VM and was rightly `gone` too — the prune check below asserts
        # against the verdicts, not against a guess about which records exist.)
        gone_run_id = f"conformance-gone-{secrets.token_hex(4)}"
        (state_dir / f"{gone_run_id}.json").write_text(
            json.dumps(
                {
                    "runId": gone_run_id,
                    "region": cli.region,
                    "imageIdentifier": None,
                    "imageName": None,
                    "microvmId": microvm_id,
                    "leaked": [microvm_id],
                }
            )
        )
        before = sorted(p.name for p in state_dir.glob("*.json"))
        judged = cli.call(
            "ls", "--remote", "--state-dir", str(state_dir), "--region", cli.region
        )
        verdicts = (judged.data.get("remote") or {}).get("entries", [])
        entry = next((e for e in verdicts if e.get("runId") == gone_run_id), None)
        results.check(
            "ls --remote marks a record naming only the terminated VM gone and prunes nothing unasked",
            entry is not None
            and entry.get("status") == "gone"
            and entry.get("microvmState") in (None, "TERMINATED")
            and judged.data.get("pruned") == []
            and sorted(p.name for p in state_dir.glob("*.json")) == before,
            f"entry={entry} pruned={judged.data.get('pruned')!r}",
        )
        pruned = cli.call(
            "ls",
            "--remote",
            "--prune",
            "--state-dir",
            str(state_dir),
            "--region",
            cli.region,
        )
        after = sorted(p.name for p in state_dir.glob("*.json"))
        # Exactly the records the listing judged `gone` — the hand-written one among them —
        # and nothing else: a `live` or `unjudged` record must still be on disk.
        expected_gone = sorted(
            str(e.get("runId")) for e in verdicts if e.get("status") == "gone"
        )
        results.check(
            "ls --remote --prune removed exactly the records judged gone",
            gone_run_id in expected_gone
            and sorted(pruned.data.get("pruned") or []) == expected_gone
            and after == sorted(set(before) - {f"{r}.json" for r in expected_gone}),
            f"pruned={pruned.data.get('pruned')!r} gone={expected_gone} "
            f"before={before} after={after}",
        )
