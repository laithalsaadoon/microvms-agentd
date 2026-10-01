# SPDX-License-Identifier: Apache-2.0
"""A stdout reader that leaves early, through a real VM, and the Gherkin scenario the
cucumber runner drives against the suite's kept VM."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import boto3
from harness.cli import Cli, attach_args
from harness.constants import BASELINE_MEMORY_MIB, REPO, SERVICE
from harness.envelope import Envelope
from harness.redact import command_for_log
from harness.results import Results


@dataclass
class ClosedReaderRun:
    """One invocation whose stdout reader left early, as observed from outside."""

    returncode: int
    head: bytes
    stderr: str
    seconds: float
    timed_out: bool


def run_with_closing_reader(
    argv: list[str], *, read_first_chunk: bool, timeout: float
) -> ClosedReaderRun:
    """Starts `argv` with stdout on a pipe and closes the pipe's read end early.

    `read_first_chunk` reads one chunk (whatever the first `read` returns) before closing, so
    the process has already written; otherwise the read end is closed before anything is read.
    Closing the read end is what makes the next write fail with EPIPE. `>&-` would not: a
    closed descriptor is EBADF, which Rust's stdout treats as success (#216).
    """
    started = time.monotonic()
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdout is not None
    head = os.read(proc.stdout.fileno(), 65536) if read_first_chunk else b""
    proc.stdout.close()
    proc.stdout = None
    timed_out = False
    try:
        _, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        _, err = proc.communicate()
    return ClosedReaderRun(
        returncode=proc.returncode,
        head=head,
        stderr=(err or b"").decode(errors="replace"),
        seconds=time.monotonic() - started,
        timed_out=timed_out,
    )


def run_history_vms(state_dir: Path) -> dict[str, list[dict[str, Any]]]:
    """The VMs a `run` recorded in its own history, each with its events, in id order.

    `run` writes `<state-dir>/history/<vm-id>.jsonl` for the VM it launched whatever its
    stdout did, and keeps the file after a clean teardown, when the ledger record is deleted.
    So this is the run's own account of which VM was its, which no listing of the account
    gives. A state directory with no history reads as no VM, which the caller fails on.
    """
    history = Path(state_dir) / "history"
    if not history.is_dir():
        return {}
    return {
        path.stem: [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        for path in sorted(history.glob("*.jsonl"))
    }


def launched_in_window(
    items: list[dict[str, Any]],
    image_name: str,
    shared: str,
    earliest: datetime,
    latest: datetime,
) -> set[str]:
    """The distinct VMs from `image_name`, other than `shared`, that started in the window.

    A set of ids rather than a count of listing entries: measured 2026-10-01, us-east-1, API
    2025-09-09, one paginated `ListMicrovms` taken while other VMs were changing state
    returned the same VM on two of its pages (CloudTrail shows the check's two `GetMicrovm`
    calls for that one id), and counting entries read one launch as two. The image is the
    ARN's last colon segment, compared whole, so an image whose name only starts with the
    suite's isn't counted. The window is
    bounded at both ends: the suite runs one section at a time, so a VM started between the
    run's start and its exit is the run's or a second launch it made.
    """
    return {
        item["microvmId"]
        for item in items
        if item.get("microvmId") != shared
        and (item.get("imageArn") or "").rsplit(":", 1)[-1] == image_name
        and item.get("startedAt") is not None
        and earliest <= item["startedAt"] <= latest
    }


def drive_closed_output(cli: Cli, launched: Envelope, results: Results) -> None:
    """CLI-8 and CLI-9 (#216) through a real VM: a stdout reader that leaves early.

    CLI-9: an `exec --stream` of a long ticker whose reader closes after the first chunk stops
    within one further event, exits ERR_INTERRUPTED, names the exec on stderr with a reattach
    hint, and leaves the exec running on the daemon. Checked in both the NDJSON and the human
    format, on the suite's shared VM, and each ticker is killed afterwards.

    CLI-8: a `run` that launches and tears down, with stdout closed before its only write,
    still tears the VM down and exits with its own outcome — success — rather than a failure
    that would invite a retry and a second launch. Its VM is the one its own history under
    `--state-dir` names, since its stdout is gone; the account's listing must show that VM and
    no other from the suite image started while the run ran, which is what catches a second
    launch. Each one still alive afterwards is terminated here.

    No run may panic or die by signal (CLI-7). About one launch of cost on top of the shared VM.
    """
    print("\n-- closed output (#216: CLI-7, CLI-8, CLI-9) --")
    attach = attach_args(cli, launched)
    ticker = 'i=0; while [ "$i" -lt 600 ]; do echo "tick-$i"; i=$((i+1)); sleep 1; done'

    for label, prefix in (
        ("ndjson", [str(cli.binary), "--json", "--quiet"]),
        ("human", [str(cli.binary), "--quiet"]),
    ):
        exec_id = f"closed-output-{label}-{secrets.token_hex(3)}"
        argv = [*prefix, "exec", ticker, "--stream", "--exec-id", exec_id, *attach]
        cli.log.append(
            command_for_log(argv) + "  # stdout reader closes after one chunk"
        )
        run = run_with_closing_reader(argv, read_first_chunk=True, timeout=60.0)
        try:
            results.check(
                f"CLI-7 {label} stream with a closed reader neither panics nor dies by signal",
                not run.timed_out
                and run.returncode >= 0
                and "panicked" not in run.stderr,
                f"rc={run.returncode} timedOut={run.timed_out}",
            )
            results.check(
                f"CLI-9 {label} stream stops promptly and exits ERR_INTERRUPTED",
                run.returncode == 11 and run.seconds < 30.0,
                f"rc={run.returncode} after {run.seconds:.1f}s, first chunk {len(run.head)} bytes",
            )
            results.check(
                f"CLI-9 {label} stream names the exec and how to reattach on stderr",
                exec_id in run.stderr and f"--exec-id {exec_id}" in run.stderr,
                repr(run.stderr[-300:]),
            )
            polled = cli.call("exec", "--poll", exec_id, *attach)
            results.eq(
                f"CLI-9 {label} stream left the exec running on the daemon",
                polled.data.get("phase"),
                "running",
            )
        finally:
            try:
                cli.call("kill", exec_id, *attach)
            except Exception as exc:  # noqa: BLE001 - a failed kill is a finding
                results.check(f"the {label} ticker was killed", False, repr(exc))

    image = str(launched.data["imageIdentifier"])
    with tempfile.TemporaryDirectory() as state_dir:
        argv = [
            str(cli.binary),
            "--json",
            "--quiet",
            "run",
            "--image",
            image,
            "--memory",
            str(BASELINE_MEMORY_MIB),
            "--exec",
            "echo closed-output",
            "--state-dir",
            state_dir,
            "--region",
            cli.region,
        ]
        cli.log.append(
            command_for_log(argv) + "  # stdout closed before the first byte"
        )
        launched_after = datetime.now(timezone.utc)
        run = run_with_closing_reader(argv, read_first_chunk=False, timeout=15 * 60.0)
        results.check(
            "CLI-7 a run with a closed stdout neither panics nor dies by signal",
            not run.timed_out and run.returncode >= 0 and "panicked" not in run.stderr,
            f"rc={run.returncode} timedOut={run.timed_out}",
        )
        results.eq(
            "CLI-8 a run with a closed stdout exits with its own outcome",
            run.returncode,
            0,
        )
        launched_until = datetime.now(timezone.utc)
        plane = boto3.Session(region_name=cli.region).client(SERVICE)
        history = run_history_vms(Path(state_dir))
        own = next(iter(history)) if len(history) == 1 else None
        recorded = own is not None and any(
            event.get("event") == "launched" for event in history[own]
        )
        window = launched_in_window(
            [
                item
                for page in plane.get_paginator("list_microvms").paginate()
                for item in page.get("items", [])
            ],
            image.rsplit(":", 1)[-1],
            str(launched.data.get("microvmId") or ""),
            launched_after - timedelta(seconds=5),
            launched_until + timedelta(seconds=5),
        )
        states = {
            vm: plane.get_microvm(microvmIdentifier=vm)["state"]
            for vm in sorted(window | ({own} if own else set()))
        }
        results.check(
            "CLI-8 a run with a closed stdout still tore its VM down",
            recorded
            and window == {own}
            and states.get(own) in {"TERMINATING", "TERMINATED"},
            f"history names {sorted(history)}, the run's window holds {sorted(window)}, "
            f"states={states}",
        )
        for vm, state in states.items():
            if state not in {"TERMINATING", "TERMINATED"}:
                plane.terminate_microvm(microvmIdentifier=vm)


#: The `@live` scenario in `crates/microvms-cli/tests/features/closed_output.feature` this suite runs.
BDD_LIVE_SCENARIO = "a streamed exec stops when its stdout reader closes"


def bdd_scenario_outcome(junit_xml: str, scenario: str) -> str:
    """`passed`, `failed`, `skipped`, or `missing` for one scenario in cucumber's JUnit report.

    `missing` is its own answer rather than a pass: a runner that filtered the scenario out
    (for example because `MICROVM_BDD_ATTACH` never reached it) exits 0 with the scenario
    absent, and reading that as green is how a live check goes vacuous.
    """
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(junit_xml)
    except ET.ParseError:
        return "missing"
    for case in root.iter("testcase"):
        if scenario in (case.get("name") or ""):
            if case.find("failure") is not None or case.find("error") is not None:
                return "failed"
            if case.find("skipped") is not None:
                return "skipped"
            return "passed"
    return "missing"


def drive_closed_output_bdd(cli: Cli, launched: Envelope, results: Results) -> None:
    """CLI-9's Gherkin scenario, run by the cucumber runner against the suite's kept VM.

    `cargo test` leaves the `@live` scenario out; here `MICROVM_BDD_ATTACH` carries this VM's
    attach flags so it runs for real: a streamed ticker whose stdout reader leaves after the
    first chunk exits ERR_INTERRUPTED promptly, names the exec, and leaves it running. The
    scenario kills its exec whatever the outcome. The JUnit report, not the exit code alone,
    decides the check, so a scenario that never ran is a FAIL.
    """
    print("\n-- closed output, Gherkin (#216: the CLI-9 @live scenario) --")
    repo = REPO
    with tempfile.TemporaryDirectory() as tmp:
        junit = Path(tmp) / "bdd-junit.xml"
        env = {
            **os.environ,
            "MICROVM_BDD_ATTACH": json.dumps(attach_args(cli, launched)),
            "CUCUMBER_JUNIT": str(junit),
        }
        argv = ["cargo", "test", "-q", "-p", "microvms-cli", "--test", "bdd"]
        cli.log.append(command_for_log(argv) + "  # MICROVM_BDD_ATTACH=<attach flags>")
        run = subprocess.run(
            argv, cwd=repo, env=env, capture_output=True, text=True, timeout=1800
        )
        outcome = bdd_scenario_outcome(
            junit.read_text() if junit.exists() else "", BDD_LIVE_SCENARIO
        )
    results.check(
        "CLI-9 the Gherkin scenario streams, stops, and detaches against the live VM",
        run.returncode == 0 and outcome == "passed",
        f"rc={run.returncode} scenario={outcome} tail={run.stdout[-300:]!r}",
    )
