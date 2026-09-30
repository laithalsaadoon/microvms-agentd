# SPDX-License-Identifier: Apache-2.0
"""The closed-output section's reader and its JUnit reader, offline."""

from __future__ import annotations

import os

from harness.results import Results
from lanes.closed_output import (
    BDD_LIVE_SCENARIO,
    bdd_scenario_outcome,
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
