# SPDX-License-Identifier: Apache-2.0
"""The `live_posture` reader, offline."""

from __future__ import annotations

from harness.results import Results
from lanes.posture import posture_lines


def check_posture_lines(results: "Results") -> None:
    """The `live_posture` reader keeps each launch's label and ignores every other line."""
    stderr = (
        "running 1 test\n"
        "POSTURE default=unsealed microvmId=mvm-1\n"
        "POSTURE egress=open microvmId=mvm-2\n"
        "POSTURE egress-adopted=unsealed microvmId=mvm-2\n"
        "cleanup microvmId=mvm-1 state=TERMINATED\n"
    )
    results.eq(
        "the posture-line reader maps each launch to its label",
        posture_lines(stderr),
        {"default": "unsealed", "egress": "open", "egress-adopted": "unsealed"},
    )
