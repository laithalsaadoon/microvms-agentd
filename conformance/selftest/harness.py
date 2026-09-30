# SPDX-License-Identifier: Apache-2.0
"""The section runner's twins: a raise becomes one named FAIL, and the run goes on."""

from __future__ import annotations

from harness.envelope import Envelope, KindError
from harness.results import Results, run_section


def check_run_section(results: "Results") -> None:
    """A section's raise becomes one named FAIL with the envelope's message, and the run
    goes on; a section that returns hands its value back unchanged."""
    probe = Results(probe=True)
    envelope = Envelope(
        "error",
        "1",
        "",
        {},
        code="ERR_PRECONDITION",
        exit_code=12,
        error="could not read codex's installed version",
    )

    def raises() -> None:
        raise KindError(envelope)

    returned = run_section(probe, "agent_vm", raises)
    after = run_section(probe, "auto_resume", lambda: "ran")
    names = [name for name, _ in probe.failed]
    detail = probe.failed[0][1] if probe.failed else ""
    results.check(
        "a raising section is recorded as a named FAIL and the next section still runs",
        returned is None
        and after == "ran"
        and names == ["section agent_vm ran to completion"]
        and "ERR_PRECONDITION" in detail
        and "installed version" in detail,
        f"failed={probe.failed!r} after={after!r}",
    )
