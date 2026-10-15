# SPDX-License-Identifier: Apache-2.0
"""Offline positive and negative controls for the actual opt-in power-off lane."""

from __future__ import annotations

import copy
import io
import json
import os
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness.results import Results
from lanes.poweroff import (
    BILLING_UNVERIFIED,
    CHILD_FALLBACK_SECONDS,
    LEADER_SECONDS,
    REQUEST_SECONDS,
    SHUTDOWN_SECONDS,
    check_poweroff,
    drive_poweroff,
)

ACCEPTED = "AGENTD-25 the opt-in detached exec is accepted"
LEADER = "AGENTD-25 the direct child exits before guest shutdown"
HELD = "AGENTD-25 the guest stays running while the same group has a child"
TERMINAL = "AGENTD-25 the released group powers off the guest within the bound"


def successful_report() -> dict:
    return {
        "before": "RUNNING",
        "started": {"execId": "poweroff-1", "phase": "running"},
        "leader": {
            "execId": "poweroff-1",
            "phase": "exited",
            "exitCode": 0,
            "stdout": "poweroff-child:42\n",
        },
        "group": {
            "execId": "poweroff-1",
            "pgid": 41,
            "childExited": True,
            "pids": [42],
        },
        "heldState": "RUNNING",
        "states": [
            {"state": "TERMINATING", "seconds": 1},
            {"state": "TERMINATED", "seconds": 2},
        ],
        "shutdownSeconds": 2,
        "totalSeconds": 4,
        "billingStop": BILLING_UNVERIFIED,
    }


def check_poweroff_lane(results: Results) -> None:
    good = successful_report()
    positive = Results(probe=True)
    check_poweroff(good, positive)
    results.check(
        "power-off lane accepts complete ordered evidence", not positive.failed
    )
    floor = Results(probe=True)
    check_poweroff({}, floor)
    results.check(
        "power-off lane rejects an empty observation",
        {name for name, _ in floor.failed}
        == {
            ACCEPTED,
            LEADER,
            HELD,
            TERMINAL,
            "AGENTD-25 terminal state does not claim a billing stop",
        },
    )
    changes = [
        (("started",), None, ACCEPTED),
        (("started", "execId"), "", ACCEPTED),
        (("started", "phase"), "exited", ACCEPTED),
        (("leader",), None, LEADER),
        (("leader", "exitCode"), False, LEADER),
        (("leader", "exitCode"), 1, LEADER),
        (("leader", "phase"), "running", LEADER),
        (("leader", "stdout"), "", LEADER),
        (("group",), None, HELD),
        (("group", "childExited"), False, HELD),
        (("group", "pgid"), True, HELD),
        (("group", "pids"), [], HELD),
        (("group", "pids"), [True], HELD),
        (("group", "pids"), [43], HELD),
        (("heldState",), "TERMINATED", HELD),
        (("before",), None, TERMINAL),
        (("states",), [], TERMINAL),
        (("states",), [None], TERMINAL),
        (("states",), [{"state": "RUNNING", "seconds": 1}], TERMINAL),
        (("states",), [{"state": None, "seconds": 1}], TERMINAL),
        (("states",), [{"state": {}, "seconds": 1}], TERMINAL),
        (("states",), [{"state": "TERMINATED", "seconds": True}], TERMINAL),
        (("states",), [{"state": "TERMINATED", "seconds": float("nan")}], TERMINAL),
        (("states",), [{"state": "TERMINATED", "seconds": -1}], TERMINAL),
        (
            ("states",),
            [{"state": "RUNNING", "seconds": 2}, {"state": "TERMINATED", "seconds": 1}],
            TERMINAL,
        ),
        (("shutdownSeconds",), None, TERMINAL),
        (("shutdownSeconds",), False, TERMINAL),
        (("shutdownSeconds",), SHUTDOWN_SECONDS + 1, TERMINAL),
        (("totalSeconds",), CHILD_FALLBACK_SECONDS, TERMINAL),
        (
            ("billingStop",),
            "verified",
            "AGENTD-25 terminal state does not claim a billing stop",
        ),
    ]
    for path, value, guard in changes:
        broken = copy.deepcopy(good)
        node = broken
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
        negative = Results(probe=True)
        check_poweroff(broken, negative)
        results.check(
            f"power-off lane rejects invalid {'.'.join(path)}",
            any(name == guard for name, _ in negative.failed),
        )
    for key in good:
        absent = copy.deepcopy(good)
        del absent[key]
        negative = Results(probe=True)
        check_poweroff(absent, negative)
        results.check(f"power-off lane rejects missing {key}", bool(negative.failed))
    exercise_driver(results)
    check_selector(results)


def exercise_driver(results: Results) -> None:
    launched = SimpleNamespace(
        data={
            "microvmId": "vm-test",
            "endpoint": "https://example.invalid",
            "agentToken": "not-a-token",
        }
    )
    for timeout in (None, "leader", "shutdown"):
        calls = []
        ownership = []
        closed = []
        now = [0.0]
        states = iter(["RUNNING", "RUNNING", "TERMINATING", "TERMINATED"])

        class Plane:
            def get_microvm(self, **_):
                return {"state": "RUNNING" if timeout == "shutdown" else next(states)}

        class Aws:
            def client(self, service, *, config):
                ownership.append(
                    (
                        service,
                        config.connect_timeout,
                        config.read_timeout,
                        config.retries,
                    )
                )
                return Plane()

        class Cli:
            region = "us-east-1"

            def call(self, *args, **kwargs):
                calls.append((args, kwargs))
                if args[0] == "ps":
                    return SimpleNamespace(
                        data={"procs": [successful_report()["group"]]}
                    )
                if "--poll" in args:
                    data = successful_report()[
                        "started" if timeout == "leader" else "leader"
                    ]
                else:
                    data = successful_report()["started"]
                return SimpleNamespace(data=data)

        @contextmanager
        def image(*args, **kwargs):
            ownership.append(kwargs.get("repair"))
            try:
                yield SimpleNamespace(), {}
            finally:
                closed.append(True)

        lane_results = Results(probe=True)
        raised = None
        with (
            patch("lanes.poweroff.isolated_image", image),
            patch("lanes.poweroff.launch_image", return_value=launched),
            patch("lanes.poweroff.conformance_dockerfile", return_value="recipe"),
            patch("lanes.poweroff.resolve_base_ref", return_value="base"),
            patch("lanes.poweroff.time.monotonic", side_effect=lambda: now[0]),
            patch(
                "lanes.poweroff.time.sleep",
                side_effect=lambda delay: now.__setitem__(0, now[0] + delay),
            ),
            redirect_stdout(io.StringIO()),
        ):
            try:
                drive_poweroff(Cli(), Path(__file__), Aws(), lane_results)
            except TimeoutError as error:
                raised = str(error)
        if timeout is None:
            results.check(
                "power-off lane dispatches the public detached opt-in",
                "--detach" in calls[0][0]
                and "--poweroff-on-exit" in calls[0][0]
                and not lane_results.failed,
            )
            results.check(
                "power-off lane builds a repair image",
                True in ownership and ownership.count(True) == 1,
            )
            results.check(
                "power-off lane bounds every CLI and control-plane read",
                all(
                    0 < kwargs.get("timeout", 0) <= REQUEST_SECONDS
                    for _, kwargs in calls
                )
                and ownership[0][1:3] == (5, REQUEST_SECONDS)
                and ownership[0][3].get("total_max_attempts") == 1,
            )
        else:
            bound = LEADER_SECONDS if timeout == "leader" else SHUTDOWN_SECONDS
            results.check(
                f"power-off lane bounds a missing {timeout} observation",
                raised is not None and now[0] == bound,
            )
        results.check(
            f"power-off lane cleans its image after {timeout or 'success'}",
            closed == [True],
        )


def check_selector(results: Results) -> None:
    from lanes.suite import run_suite
    from run_rs import parse_args

    selected = parse_args(
        ["--only", "poweroff", "--binary", __file__, "--microvm-binary", __file__]
    )
    called = []
    outputs = {
        key: {"value": "test"}
        for key in ("s3_bucket", "build_role_arn", "execution_role_arn")
    }
    with (
        patch("lanes.suite.sh", return_value=json.dumps(outputs)),
        patch("lanes.suite.boto3.Session", return_value=None),
        patch(
            "lanes.suite.drive_poweroff", side_effect=lambda *args: called.append(args)
        ),
        patch.dict(os.environ),
        redirect_stdout(io.StringIO()),
    ):
        code = run_suite(selected)
    results.check(
        "power-off selector reaches the actual opt-in lane",
        code == 0 and len(called) == 1,
    )
