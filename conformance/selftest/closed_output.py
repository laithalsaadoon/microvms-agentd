# SPDX-License-Identifier: Apache-2.0
"""The closed-output section's reader and its JUnit reader, offline."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from harness.results import Results
from lanes.closed_output import (
    BDD_LIVE_SCENARIO,
    bdd_scenario_outcome,
    launched_in_window,
    run_history_vms,
    run_with_closing_reader,
)


def check_closing_reader_helper(results: Results) -> None:
    """The live CLI-9 section is only as good as its reader: prove the close causes EPIPE.

    `yes` writes forever and, being a C program under the default SIGPIPE disposition that
    `subprocess` restores for its children, dies by that signal the moment its reader leaves.
    A helper that failed to close the read end would see it run into the timeout instead.
    """
    if os.name != "posix":
        results.skip("the closing reader delivers EPIPE", "needs a POSIX `yes`")
        return
    run = run_with_closing_reader(["yes"], read_first_chunk=True, timeout=10.0)
    results.check(
        "the closing reader delivers EPIPE to a writer after its first chunk",
        not run.timed_out and run.returncode == -13 and bool(run.head),
        f"rc={run.returncode} timedOut={run.timed_out} head={len(run.head)} bytes",
    )
    run = run_with_closing_reader(["yes"], read_first_chunk=False, timeout=10.0)
    results.check(
        "the closing reader delivers EPIPE before the first byte",
        not run.timed_out and run.returncode == -13 and not run.head,
        f"rc={run.returncode} timedOut={run.timed_out}",
    )


def check_bdd_outcome(results: "Results") -> None:
    """The JUnit reader tells a passed scenario from a failed, skipped, or absent one."""
    name = BDD_LIVE_SCENARIO

    def report(body: str) -> str:
        return f'<testsuites><testsuite name="closed_output">{body}</testsuite></testsuites>'

    other = '<testcase name="Scenario: help with stdout closed"/>'
    cases = {
        "passed": report(f'{other}<testcase name="Scenario: {name}"/>'),
        "failed": report(f'<testcase name="Scenario: {name}"><failure/></testcase>'),
        "skipped": report(f'<testcase name="Scenario: {name}"><skipped/></testcase>'),
        "missing": report(other),
    }
    seen = {want: bdd_scenario_outcome(xml, name) for want, xml in cases.items()}
    results.check(
        "the Gherkin JUnit reader tells passed from failed, skipped, and absent",
        all(want == got for want, got in seen.items())
        and bdd_scenario_outcome("not xml", name) == "missing",
        repr(seen),
    )


def check_cli8_attribution(results: Results) -> None:
    """CLI-8's live count names the run's VM once, and still sees a second launch.

    The listing is wave 4's shape (2026-10-01): the run's VM on two pages of one paginated
    `ListMicrovms`, which a count of entries read as two launches. Its twins: a second VM
    from the image inside the window is still counted, so a real double launch fails the
    check, and the shared VM, another image, and a VM outside the window are not.
    """
    image = "microvm-cli-conformance-5a050110-8b93164d60ae"
    arn = f"arn:aws:lambda:us-east-1:123456789012:microvm-image:{image}"
    start = datetime(2026, 10, 1, 8, 34, 20, tzinfo=timezone.utc)
    end = start + timedelta(seconds=30)

    def vm(vm_id: str, at: datetime, image_arn: str = arn) -> dict:
        return {"microvmId": vm_id, "imageArn": image_arn, "startedAt": at}

    run = vm("microvm-run", start + timedelta(seconds=7))
    others = [
        vm("microvm-shared", start + timedelta(seconds=8)),
        vm("microvm-other-image", start + timedelta(seconds=9), arn + "x"),
        vm("microvm-before", start - timedelta(seconds=40)),
        vm("microvm-after", end + timedelta(seconds=40)),
    ]
    listed_twice = launched_in_window(
        [run, *others, run], image, "microvm-shared", start, end
    )
    second = launched_in_window(
        [run, vm("microvm-second", start + timedelta(seconds=12)), *others],
        image,
        "microvm-shared",
        start,
        end,
    )
    results.check(
        "CLI-8's window names a VM listed on two pages once",
        listed_twice == {"microvm-run"},
        repr(sorted(listed_twice)),
    )
    results.check(
        "CLI-8's window still holds a second VM the run launched",
        second == {"microvm-run", "microvm-second"},
        repr(sorted(second)),
    )

    with tempfile.TemporaryDirectory() as state:
        empty = run_history_vms(Path(state))
        history = Path(state) / "history"
        history.mkdir()
        (history / "microvm-run.jsonl").write_text(
            json.dumps({"seq": 0, "event": "launched"})
            + "\n"
            + json.dumps({"seq": 1, "event": "terminated", "terminateAccepted": True})
            + "\n",
            encoding="utf-8",
        )
        read = run_history_vms(Path(state))
    results.check(
        "CLI-8 reads the run's VM from its history, and no history as none",
        empty == {}
        and list(read) == ["microvm-run"]
        and [event["event"] for event in read["microvm-run"]]
        == ["launched", "terminated"],
        f"empty={empty!r} read={read!r}",
    )
