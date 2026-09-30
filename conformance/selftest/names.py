# SPDX-License-Identifier: Apache-2.0
"""The named terminate's fallback, offline."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.envelope import Envelope, KindError
from harness.results import Results
from lanes.names import terminate_by_name_from_elsewhere


def check_terminate_fallback(results: "Results") -> None:
    """The named terminate falls back to a terminate by id in the VM's region whenever it
    misses: on a success envelope that leaked the VM, on one that never reached
    `TERMINATED`, and on an error envelope. It runs nothing more when the terminate reached
    the VM."""

    class _ScriptedCli:
        region = "us-east-1"

        def __init__(self, first: Envelope | KindError) -> None:
            self.answers: list[Envelope | KindError] = [first, ok({})]
            self.calls: list[tuple[str, ...]] = []

        def call(self, *args: str, env: dict[str, str] | None = None) -> Envelope:
            self.calls.append(args)
            answer = self.answers.pop(0)
            if isinstance(answer, KindError):
                raise answer
            return answer

    def ok(data: dict[str, Any]) -> Envelope:
        return Envelope("ok", "1", "microvm.teardown", data)

    state = Path("sd")
    by_id = (
        "terminate",
        "mvm-1",
        "--wait",
        "--state-dir",
        "sd",
        "--region",
        "us-east-1",
    )
    refused = KindError(
        Envelope(
            "error",
            "1",
            "",
            {"kind": "NotFound"},
            code="ERR_PROTOCOL",
            exit_code=5,
            error="no such MicroVM",
        )
    )
    cases = [
        (
            "a named terminate that leaked the VM falls back to a terminate by id in its region",
            # TERMINATED so this case holds the `leaked` clause on its own; the CLI skips the
            # wait after a failed terminate, so a real leak also reads TERMINATING.
            ok({"microvmId": "mvm-1", "state": "TERMINATED", "leaked": ["mvm-1"]}),
            [by_id],
        ),
        (
            "a named terminate that never reached TERMINATED falls back the same way",
            ok({"microvmId": "mvm-1", "state": "TERMINATING", "leaked": []}),
            [by_id],
        ),
        (
            "a named terminate refused with an error envelope falls back the same way",
            refused,
            [by_id],
        ),
        (
            "a named terminate that reached the VM runs no fallback",
            ok({"microvmId": "mvm-1", "state": "TERMINATED", "leaked": []}),
            [],
        ),
    ]
    for name, first, fallback in cases:
        probe = Results(probe=True)
        fake = _ScriptedCli(first)
        try:
            terminate_by_name_from_elsewhere(
                fake,
                probe,
                "x",
                "mvm-1",
                state,
                "us-west-2",
                {},  # type: ignore[arg-type]
            )
            raised = ""
        except KindError as exc:
            raised = repr(exc)
        results.check(
            name,
            not raised
            and fake.calls[:1] == [("terminate", "x", "--wait", "--state-dir", "sd")]
            and fake.calls[1:] == fallback
            and bool(probe.failed) == bool(fallback),
            f"raised={raised!r} calls={fake.calls!r} failed={probe.failed!r}",
        )
