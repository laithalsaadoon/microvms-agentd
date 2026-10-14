# SPDX-License-Identifier: Apache-2.0
"""Positive and negative controls for the STATE-12 live verdicts."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import patch

from harness.envelope import Envelope, KindError
from harness.results import Results
from lanes.state12 import check_ended_resume, drive_state12


def check_state12(results: Results) -> None:
    observed = {
        "state": "TERMINATED",
        "stateReason": "MicroVM exceeded maximum lifetime.",
    }
    refusal = Envelope(
        status="error",
        api_version="1",
        type="",
        data={},
        code="ERR_WINDOW_CLOSED",
        exit_code=8,
        error="GetMicrovm reports the VM TERMINATED (MicroVM exceeded maximum lifetime.).",
    )
    positive = Results(probe=True)
    check_ended_resume(observed, refusal, positive)
    results.eq(
        "STATE-12 matching service refusal passes every check", len(positive.failed), 0
    )
    results.eq(
        "STATE-12 positive control exercises every check", len(positive.passed), 5
    )
    for label, row, reply in [
        ("missing state", {"stateReason": observed["stateReason"]}, refusal),
        (
            "live VM",
            {**observed, "state": "SUSPENDED"},
            replace(refusal, error=refusal.error.replace("TERMINATED", "SUSPENDED")),
        ),
        ("missing reason", {"state": "TERMINATED"}, refusal),
        ("successful resume", observed, None),
        ("wrong error", observed, replace(refusal, code="ERR_PLATFORM")),
        ("wrong exit", observed, replace(refusal, exit_code=9)),
        ("wrong state text", observed, replace(refusal, error=observed["stateReason"])),
        ("wrong reason text", observed, replace(refusal, error="TERMINATED")),
    ]:
        probe = Results(probe=True)
        check_ended_resume(row, reply, probe)
        results.check(
            f"STATE-12 rejects {label}",
            bool(probe.failed),
            f"failed={len(probe.failed)}",
        )


class FakeState12:
    """One scripted account and CLI, advancing only when the driver sleeps."""

    region = "us-east-1"

    def __init__(self) -> None:
        self.now = 0.0
        self.states: dict[str, str] = {}
        self.started: dict[str, float] = {}
        self.resumed_after: float | None = None

    def client(self, _service: str) -> FakeState12:
        return self

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def get_microvm(self, *, microvmIdentifier: str) -> dict[str, str]:
        state = self.states[microvmIdentifier]
        if microvmIdentifier == "idle" and state != "TERMINATED":
            age = self.now - self.started["idle"]
            state = (
                "TERMINATED" if age >= 120 else "SUSPENDED" if age >= 60 else "RUNNING"
            )
            self.states["idle"] = state
        return {"state": state, "stateReason": "MicroVM exceeded maximum lifetime."}

    def call(self, *args: str, **_kwargs: object) -> Envelope:
        operation = args[0]
        if operation == "run":
            name = args[args.index("--name") + 1]
            vm_id = "explicit" if "-explicit-" in name else "idle"
            self.states[vm_id] = "RUNNING"
            self.started[vm_id] = self.now
            data = {"microvmId": vm_id}
        elif operation == "suspend":
            self.states[args[1]] = "SUSPENDED"
            self.started[args[1]] = self.now
            data = {"state": "SUSPENDED"}
        elif operation == "resume":
            if args[1] == "idle":
                raise KindError(
                    Envelope(
                        status="error",
                        api_version="1",
                        type="",
                        data={},
                        code="ERR_WINDOW_CLOSED",
                        exit_code=8,
                        error="TERMINATED: MicroVM exceeded maximum lifetime.",
                    )
                )
            self.resumed_after = self.now - self.started["explicit"]
            self.states[args[1]] = "RUNNING"
            data = {"state": "RUNNING"}
        elif operation == "terminate":
            self.states[args[1]] = "TERMINATED"
            data = {"leaked": []}
        else:
            raise AssertionError(f"unexpected command {operation}")
        return Envelope(status="ok", api_version="1", type="", data=data)


def check_state12_driver(results: Results) -> None:
    fake = FakeState12()
    probe = Results(probe=True)
    launched = Envelope(
        status="ok", api_version="1", type="", data={"imageIdentifier": "arn:image"}
    )
    with (
        patch("lanes.state12.time.monotonic", lambda: fake.now),
        patch("lanes.state12.time.sleep", fake.sleep),
    ):
        drive_state12(fake, launched, fake, probe)
    results.eq("STATE-12 driver passes its scripted lifecycle", len(probe.failed), 0)
    results.check(
        "STATE-12 driver checks the ended resume",
        "STATE-12 ended resume reports ERR_WINDOW_CLOSED" in probe.passed,
        repr(probe.passed),
    )
    results.check(
        "STATE-12 driver waits beyond the explicit window",
        fake.resumed_after is not None and fake.resumed_after > 60,
        repr(fake.resumed_after),
    )
    results.eq(
        "STATE-12 driver cleans both VMs",
        fake.states,
        {"explicit": "TERMINATED", "idle": "TERMINATED"},
    )
