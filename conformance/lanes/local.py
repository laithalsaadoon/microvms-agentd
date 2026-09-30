# SPDX-License-Identifier: Apache-2.0
"""The sections that launch nothing: the local commands, core's `preflight`, and
`doctor --region`. They run before the suite's build, so a CLI-4 violation or a region
mix-up is found before any money is spent."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from harness.cli import Cli
from harness.constants import BASELINE_MEMORY_MIB, REPO
from harness.redact import command_for_log
from harness.results import Results


def drive_local_commands(cli: Cli, results: Results) -> None:
    """The commands that reach no account. Free, and they check the CLI's own contract.

    Run first and deliberately: every one of them is a CLI-4 assertion (`Cli.call`
    parses stdout whole), and finding a stray `println!` before spending fifteen
    minutes on a build is worth the two seconds.
    """
    print("\n-- local commands (no account) --")
    results.ok("ls reports the local ledger", lambda: cli.call("ls"))
    results.ok(
        "cost reports a labelled estimate",
        lambda: cli.call("cost", "--running-sec", "3600"),
    )

    # The size object's static headroom (issue #99). The peak and the headroom are
    # properties of the class, read from the sizing table — the peak is provisioned
    # from the start and headroom is peak minus baseline, so both are asserted
    # against the documented row for the class this suite launches with, not
    # against `baseline * 4` arithmetic (TRAP-13's discipline, at the envelope).
    estimate = cli.call(
        "cost",
        "--estimate",
        "--running-sec",
        "3600",
        "--memory",
        str(BASELINE_MEMORY_MIB),
    )
    size = estimate.data["report"]["size"]
    # The documented row for 1024 MiB: peak 4096. Written as a literal pair rather
    # than derived, for the same reason the core's table is data.
    results.check(
        "the cost size object reports its static headroom from the table",
        size.get("baselineMib") == BASELINE_MEMORY_MIB
        and size.get("peakMib") == 4096
        and size.get("headroomMib") == size["peakMib"] - size["baselineMib"],
        f"baselineMib={size.get('baselineMib')!r} peakMib={size.get('peakMib')!r} "
        f"headroomMib={size.get('headroomMib')!r}",
    )

    manifest = cli.call("manifest")
    commands = [entry["name"] for entry in manifest.data["commands"]]
    results.check(
        "manifest lists every command this suite drives",
        {"run", "exec", "suspend", "resume", "terminate"} <= set(commands),
        f"{len(commands)} commands",
    )

    # `logs` succeeds since #79 (PR #115), and the distinction the old refusal carried
    # now rides the envelope: `data.lines` is explicitly `null`, never `[]`, because an
    # empty array is the wire shape for "the group exists and has no events", which is
    # exactly what a wrong build-role prefix produces, and this client did not read the
    # group (no CloudWatch reader; both thinness guards pin that). The runnable read
    # command is the success payload, so it is asserted beside the null. Asserted live
    # because the first full-suite run after #115 (2026-09-03) failed here: the check
    # still expected the pre-#79 `ERR_PRECONDITION`.
    logs = cli.call("logs", "agentd-conformance")
    results.check(
        "logs names the group and refuses to imply it is empty",
        "agentd-conformance" in str(logs.data.get("logGroup"))
        and "lines" in logs.data
        and logs.data.get("lines") is None
        and "aws logs tail" in str(logs.data.get("tailCommand")),
        f"logGroup={logs.data.get('logGroup')!r} lines={logs.data.get('lines')!r} "
        f"tailCommand={logs.data.get('tailCommand')!r}",
    )


def preflight_test(
    cli: Cli, name: str, env: dict[str, str]
) -> subprocess.CompletedProcess[str] | None:
    """One `live_preflight` test, or `None` when it did not finish."""
    command = [
        "cargo",
        "test",
        "-p",
        "microvms-core",
        "--test",
        "live_preflight",
        name,
        "--",
        "--ignored",
        "--exact",
        "--nocapture",
    ]
    cli.log.append(command_for_log(command))
    try:
        return subprocess.run(
            command,
            cwd=REPO,
            env=env,
            text=True,
            capture_output=True,
            timeout=10 * 60,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None


def preflight_lines(stderr: str) -> dict[str, str]:
    """`PREFLIGHT <run> <check>=<ok|fail> ...` lines, as {"<run> <check>": "ok"|"fail"}."""
    lines: dict[str, str] = {}
    for line in stderr.splitlines():
        if line.startswith("PREFLIGHT "):
            run, _, rest = line.removeprefix("PREFLIGHT ").partition(" ")
            key, _, value = rest.partition("=")
            if key and " " not in key:
                lines[f"{run} {key}"] = value.split(" ", 1)[0]
    return lines


def drive_preflight(cli: Cli, results: Results) -> None:
    """BIND-15 and BIND-16 (#223): `preflight`, what both bindings call, against AWS.

    Three runs of core's `preflight`, none of which launches or builds anything. In the suite's
    region every check passes and the account's VM and image listings are the same after as
    before. In ca-central-1, a region without MicroVMs, the region line is advisory and the
    service check fails. With every credential source removed from the environment, the
    report stops at the credentials line and never sends the listing.
    """
    print("\n-- preflight (region, credentials, one free listing) --")
    env = os.environ.copy()
    env["AWS_REGION"] = cli.region
    suite = preflight_test(
        cli, "preflight_passes_in_the_suites_region_and_changes_nothing", env
    )
    seen = preflight_lines(suite.stderr if suite else "")
    results.check(
        "BIND-15 preflight passes in the suite's region",
        bool(suite) and suite.returncode == 0 and seen.get("suite ok") == "true",
        f"exit={suite.returncode if suite else 'timeout'} lines={seen!r}",
    )
    results.check(
        "BIND-16 preflight left the account's VMs and images as they were",
        bool(suite) and "before=" in suite.stderr and suite.returncode == 0,
        next(
            (
                line
                for line in (suite.stderr if suite else "").splitlines()
                if "before=" in line
            ),
            "no count line",
        ),
    )

    elsewhere = preflight_test(
        cli, "preflight_in_a_region_without_microvms_fails_its_service_check", env
    )
    seen = preflight_lines(elsewhere.stderr if elsewhere else "")
    results.check(
        "BIND-15 preflight in a region without MicroVMs fails its service check",
        bool(elsewhere)
        and elsewhere.returncode == 0
        and seen.get("elsewhere service") == "fail"
        and seen.get("elsewhere ok") == "false",
        f"exit={elsewhere.returncode if elsewhere else 'timeout'} lines={seen!r}",
    )

    with tempfile.TemporaryDirectory() as empty_home:
        bare = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("AWS_") and key != "HOME"
        }
        bare["HOME"] = empty_home
        bare["AWS_EC2_METADATA_DISABLED"] = "true"
        bare["AWS_CONFIG_FILE"] = str(Path(empty_home) / "config")
        bare["AWS_SHARED_CREDENTIALS_FILE"] = str(Path(empty_home) / "credentials")
        # Cargo and rustup live under the real home; point them there explicitly.
        for key in ("CARGO_HOME", "RUSTUP_HOME"):
            bare[key] = os.environ.get(
                key, str(Path.home() / (".cargo" if key == "CARGO_HOME" else ".rustup"))
            )
        nocreds = preflight_test(
            cli, "preflight_without_credentials_makes_no_call", bare
        )
    seen = preflight_lines(nocreds.stderr if nocreds else "")
    results.check(
        "BIND-16 preflight without credentials stops before any AWS call",
        bool(nocreds)
        and nocreds.returncode == 0
        and seen.get("nocreds credentials") == "fail"
        and seen.get("nocreds ok") == "false",
        f"exit={nocreds.returncode if nocreds else 'timeout'} lines={seen!r}",
    )


def drive_doctor_region(cli: Cli, results: Results) -> None:
    """#250: `doctor --region` reports on the flag's region on every line, against AWS.

    One `doctor` call with `--region` set to the suite's region and `AWS_REGION` set to
    another, so the flag and the environment disagree. The credentials line and the two
    managed-base reads must follow the flag. Live because the scripted seam in `guards.rs`
    only records which region was asked for; this is where the listing is signed for and
    sent to that region, and where AWS answers it. The flag is the suite's region because
    the account is known to answer there, so the check needs no access anywhere else. It
    launches nothing and reads two free listings.
    """
    print("\n-- doctor --region (the credentials and managed-base lines) --")
    other = "us-west-2" if cli.region != "us-west-2" else "us-east-1"
    env = {**os.environ, "AWS_REGION": other, "AWS_DEFAULT_REGION": other}
    report = cli.call("doctor", "--region", cli.region, "--no-config", env=env)
    ok, detail = doctor_region_lines(report.data.get("checks", []), cli.region, other)
    results.check(
        "doctor --region names the flag's region on every line, whatever AWS_REGION says",
        ok,
        detail,
    )


#: The `doctor` lines whose detail can name a region: the ones #250's check reads.
DOCTOR_REGION_LINES = ("region", "credentials", "managed-bases", "base-image-versions")


def doctor_region_lines(
    checks: list[dict[str, Any]], region: str, other: str
) -> tuple[bool, str]:
    """Whether a `doctor --region <region>` report is about that region, and not `other`.

    #250: the credentials and managed-base checks resolved the region again from
    `AWS_REGION` alone, so the lines below the region line named the environment's region.
    The `credentials` line has to name `region`, the `managed-bases` line has to name it as
    the region it listed (its "could not list" branch names none, so it fails here), and no
    region-bearing line may name `other`. The bucket and role lines are left out, since a
    bucket name can carry a region of its own. Kept apart from the live driver so the
    self-test can make it fail, which a real run on a fixed binary never does.
    """
    details = {str(row.get("name")): str(row.get("detail", "")) for row in checks}
    credentials = details.get("credentials", "")
    bases = details.get("managed-bases", "")
    ok = (
        credentials.endswith(f"for {region}")
        and f" in {region}" in bases
        and not any(other in details.get(name, "") for name in DOCTOR_REGION_LINES)
    )
    return ok, f"credentials={credentials!r} managed-bases={bases!r}"
