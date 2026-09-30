# SPDX-License-Identifier: Apache-2.0
"""`microvm quickstart` on a fresh state directory: the daemon fetched from this CLI's own
release, verified in-process with `gh` logged out, cached, and booted."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from harness.cli import Cli
from harness.results import Results

#: The variables `gh` reads a login from. Unset, with an empty config directory, `gh` is
#: logged out.
GH_TOKEN_VARIABLES = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
)

#: A `gh` that records its argv and refuses the way a logged-out `gh` does.
GH_SHIM = """#!/bin/sh
printf '%s\\n' "$*" >> "$(dirname "$0")/gh.ran"
echo 'To get started with GitHub CLI, please run:  gh auth login' >&2
exit 4
"""


def gh_logged_out(root: Path, base: dict[str, str]) -> tuple[dict[str, str], Path]:
    """`base` as a machine where `gh` is logged out, and the file that records each run.

    Three things, so the proof doesn't rest on any one: every variable `gh` reads a token
    from is unset (so is the `GITHUB_TOKEN` the fetch itself would send), `GH_CONFIG_DIR`
    names an empty directory, and a shim `gh` first on `PATH` appends its argv to the
    returned file and exits 4 with the logged-out message. A real `gh` later on `PATH` is
    logged out by the first two; the third says whether anything tried it.
    """
    shim = root / "gh-shim"
    config = root / "gh-config"
    shim.mkdir(parents=True, exist_ok=True)
    config.mkdir(parents=True, exist_ok=True)
    gh = shim / "gh"
    gh.write_text(GH_SHIM)
    gh.chmod(0o755)
    env = {
        name: value for name, value in base.items() if name not in GH_TOKEN_VARIABLES
    }
    env["GH_CONFIG_DIR"] = str(config)
    env["PATH"] = os.pathsep.join([str(shim), base.get("PATH", "")])
    return env, shim / "gh.ran"


def digest_record_agrees(
    record: dict[str, Any], verified: Any, body: bytes, version: str
) -> bool:
    """Whether BIND-19's digest record names these bytes, this proof and this version.

    One named check with `eq`'s rule inside it: a proof that's absent from both the record
    and the envelope reads as None on each side and would otherwise agree. Kept apart from
    the live driver so the self-test can make it fail, which a real run never does.
    """
    return (
        record.get("sha256") == hashlib.sha256(body).hexdigest()
        and record.get("verification") is not None
        and record.get("verification") == verified
        and record.get("version") == version
    )


def drive_provisioned_quickstart(
    cli: Cli, state: Path, logs: Any, results: Results
) -> None:
    """`microvm quickstart` on a fresh state directory: the self-provisioning surface.

    One invocation covers both new surfaces at once — the provisioning chain (a run
    with no binary fetches this CLI's own version's release asset, verifies it, caches
    it under the state directory) and `quickstart` itself (issue #75). Live rather
    than only scripted for the id-prefix lesson's reason: the local guards script the
    fetch, so nothing local ever proves the real release carries the asset, that its
    attestation verifies in-process, or that the fetched bytes boot a real VM. This is
    the one place the whole chain runs against the things it actually talks to.

    It runs with `gh` logged out (`gh_logged_out`), which is the machine #284 is for:
    the fetch used to need a `gh` login for an attestation check, and a machine without
    one got the checksum. So the proof asserted here is `attestation`, not either proof,
    and a `gh` shim first on `PATH` records whether anything tried the tool.

    The fetch targets `v{CLI version}`. On a release tag, `live-conformance.yml` sets
    `$MICROVM_RELEASE_DIR` to the tag's draft release, so the fetch reads the draft's
    assets and proves them by the same attestation, and the release's live gate can pass
    before anything is public. On main, between a version bump landing and that version's
    release publishing, this section fails with the fetch error naming the missing tag.
    That failure is the calendar, not the code: run the suite again once the release
    exists.

    Its own build (~3 minutes): provisioning fires only when building, so no launch
    from the suite's image can carry it. Teardown is quickstart's default, and the
    teardown-left-nothing check is asserted off the same envelope.
    """
    print("\n== quickstart (self-provisioned daemon, own build, gh logged out) ==")
    env, gh_ran = gh_logged_out(state.parent / "gh-logged-out", dict(os.environ))
    envelope = cli.call(
        "quickstart",
        "--state-dir",
        str(state),
        "--region",
        cli.region,
        timeout=50 * 60,
        env=env,
    )
    results.eq(
        "quickstart emitted the run envelope shape", envelope.type, "microvm.run"
    )

    agentd = envelope.data.get("agentd") or {}
    results.check(
        "BIND-18 a run with no binary provisioned a verified daemon from the release",
        agentd.get("source") == "fetched"
        and agentd.get("verified") in ("attestation", "checksum"),
        f"source={agentd.get('source')!r} verified={agentd.get('verified')!r}",
    )
    results.check(
        "BIND-18 with gh logged out, the provisioned daemon was verified by its attestation",
        agentd.get("verified") == "attestation",
        f"verified={agentd.get('verified')!r}",
    )
    results.check(
        "BIND-18 provisioning the daemon ran no gh",
        not gh_ran.exists(),
        gh_ran.read_text() if gh_ran.exists() else "",
    )

    # The cached install, read off this machine rather than trusted from the envelope:
    # twenty bytes of header is the same gate core ran, asserted independently.
    cached = Path(str(agentd.get("path") or state / "missing"))
    machine = None
    body = cached.read_bytes() if cached.exists() else b""
    header = body[:20]
    if header[:4] == b"\x7fELF" and len(header) >= 20:
        order = "little" if header[5] == 1 else "big"
        machine = int.from_bytes(header[18:20], order)
    results.check(
        "BIND-20 the provisioned daemon is cached as an aarch64 ELF",
        machine == 0xB7,
        f"{cached} e_machine={machine!r}",
    )
    # The release the CLI asked for is its own version's: the cache directory is the tag,
    # and the tag is `v` plus the version `microvm --version` prints.
    version = cli.version()
    results.check(
        "BIND-17 the provisioned daemon is the release for the CLI's own version",
        cached.parent.name == f"v{version}",
        f"{cached} for CLI {version}",
    )
    # The digest record core writes after verification, read back and recomputed here:
    # the record is what lets the next run trust the cache, so it must name exactly the
    # bytes that were verified and the proof the envelope reported.
    record_path = cached.parent / "agentd.verified.json"
    try:
        record = json.loads(record_path.read_text())
    except (OSError, ValueError) as exc:
        record = {"error": str(exc)}
    results.check(
        "BIND-19 the cached daemon's digest record matches its bytes and proof",
        digest_record_agrees(record, agentd.get("verified"), body, version),
        f"{record_path}: {record!r}",
    )

    stdout = envelope.data.get("stdout") or ""
    results.check(
        "the hello-world executed inside the provisioned VM",
        envelope.data.get("execExitCode") == 0 and "hello from a microvm" in stdout,
        f"exit={envelope.data.get('execExitCode')!r} stdout={stdout[:80]!r}",
    )
    # The teardown claim, with the one documented exception stated rather than absorbed:
    # the service creates the build log group and teardown cannot delete it on this path
    # (docs/PLATFORM.md, "The build log group survives Terraform"), so a real run's
    # `leaked` carries exactly that group — measured on the first live run of this
    # surface (2026-08-30, us-east-1), which is why this is not `leaked == []`.
    leaked = envelope.data.get("leaked") or []
    log_groups = [
        entry for entry in leaked if str(entry).startswith("/aws/lambda-microvms/")
    ]
    results.check(
        "quickstart tore down everything but the service-created log group",
        envelope.data.get("kept") is False and leaked == log_groups,
        f"kept={envelope.data.get('kept')!r} leaked={leaked!r}",
    )
    # Deleted here so the suite leaves the account as clean as it found it — the same
    # discipline drive_teardown applies to the suite's own groups.
    for group in log_groups:
        try:
            logs.delete_log_group(logGroupName=str(group))
        except Exception as exc:  # noqa: BLE001 - a cleanup failure is a report, not a crash
            print(f"  (could not delete {group}: {exc})")
