# SPDX-License-Identifier: Apache-2.0
"""Offline negative twins for the bounded retry lane and its independent cleanup."""

from __future__ import annotations

import copy
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from botocore.exceptions import ClientError
from harness.constants import REPO
from harness.observation import retry_observation
from harness.results import Results
from lanes.retry_telemetry import (
    POLICIES,
    bounded_plane,
    cleanup,
    drive_retry_telemetry,
    inventory,
    run_driver,
    verdicts,
)

# The service filters a listing by an image's ARN and answers a name with no VMs, so the
# fakes' images are ARN-shaped and their listings honor that filter the same way.
IMAGE = "arn:aws:lambda:us-east-1:000000000000:microvm-image:image"
OTHER_IMAGE = "arn:aws:lambda:us-east-1:000000000000:microvm-image:other"
PYTHON_BINDING = """
import time
class MicrovmError(Exception):
    code = 'ERR_RETRYABLE'
    retryable = True
    throttled = True
    def __init__(self, attempts):
        self.attempt_count = attempts
        self.backoff_seconds = 0 if attempts == 1 else 0.3
class Region:
    @staticmethod
    def parse(value):
        return value
class Session:
    def __init__(self, attempts, ready=False):
        self.launch_metrics = dict(attempt_count=attempts, backoff_seconds=0,
                                   running_at=time.time() if ready else None,
                                   ready_at=time.time() if ready else None)
class Sandbox:
    microvm_id = 'fake-one'
    created = 0
    @staticmethod
    async def create_async(region, *, retry=None):
        assert region == 'us-east-1' and retry in ((1, 10), (3, 10))
        obj = Sandbox()
        obj.attempts = retry[0]
        obj.denied = Sandbox.created % 2 == 1
        Sandbox.created += 1
        return obj
    async def run_async(self, **kwargs):
        assert kwargs == dict(image_identifier='image', execution_role_arn='role',
            agent_token='canary-agent', client_token='canary-client', identity=False,
            wait=False, max_idle_sec=900, suspended_sec=900, max_duration_sec=900)
        if self.denied:
            raise MicrovmError(self.attempts)
        return Session(self.attempts)
    async def wait_until_running_async(self, *, timeout):
        assert timeout == 180
        return Session(self.attempts, True)
"""

NODE_BINDING = """
const assert = require('node:assert/strict');
// The driver samples before launch and after readiness, in that order per phase.
let clockReads = 0;
Date.now = () => ++clockReads % 2 === 1 ? 99000 : 101000;
class Session {
  constructor(attempts, ready = false) { this.attempts = attempts; this.ready = ready; }
  async launchMetrics() { return {attemptCount: this.attempts, backoffSeconds: 0,
    ...(this.ready ? {runningAt: 100, readyAt: 101.0005} : {})}; }
}
class Sandbox {
  static created = 0;
  static async create(region, retry) {
    assert.equal(region, 'us-east-1');
    assert.ok([1, 3].includes(retry.maxAttempts));
    assert.equal(retry.maxElapsedSeconds, 10);
    const obj = new Sandbox(); obj.attempts = retry.maxAttempts;
    obj.denied = Sandbox.created++ % 2 === 1; return obj;
  }
  async microvmId() { return 'fake-one'; }
  async run(opts) {
    assert.deepEqual(opts, {imageIdentifier: 'image', executionRoleArn: 'role',
      agentToken: 'canary-agent', clientToken: 'canary-client', identity: false,
      wait: false, maxIdleSec: 900, suspendedSec: 900, maxDurationSec: 900});
    if (this.denied) {
      const error = new Error('private error', {cause: new Error('ERR_RETRYABLE')});
      error.details = {attemptCount: this.attempts,
        backoffSeconds: this.attempts === 1 ? 0 : 0.3, throttled: true};
      throw error;
    }
    return new Session(this.attempts);
  }
  async waitUntilRunning(timeout) {
    assert.equal(timeout, 180); return new Session(this.attempts, true);
  }
}
module.exports = {Sandbox, Region: {parse: (value) => value},
  retryDetails: (error) => error.details, isRetryable: () => true};
"""


def check_drivers(results: Results) -> None:
    request = {
        "region": "us-east-1",
        "image": "image",
        "role": "role",
        "agentToken": "canary-agent",
        "clientToken": "canary-client",
        "policies": POLICIES,
        "budgetSeconds": 10,
        "lifetimeSeconds": 900,
        "runningTimeoutSeconds": 180,
    }
    driver = REPO / "conformance" / "drivers"
    python = (
        "import sys, types, runpy; stub=types.ModuleType('microvms'); "
        f"exec({PYTHON_BINDING!r}, stub.__dict__); sys.modules['microvms']=stub; "
        f"runpy.run_path({str(driver / 'retry.py')!r}, run_name='__main__')"
    )
    with tempfile.TemporaryDirectory() as tmp:
        stub = Path(tmp) / "binding.cjs"
        stub.write_text(NODE_BINDING)
        commands = {
            "python": [sys.executable, "-I", "-c", python],
            "node": ["node", str(driver / "retry.mjs"), str(stub)],
        }
        for binding, command in commands.items():
            data, known, failure = run_driver(command, request)
            results.check(
                f"retry {binding} driver forwards policy and stable launch fields",
                not failure
                and known == {"fake-one"}
                and all(ok for _, ok in verdicts(data, binding)),
            )
            if binding == "node":
                results.check(
                    "retry node driver reports raw millisecond observations",
                    bool(data.get("phases"))
                    and all(
                        type(phase.get("observedAtMilliseconds")) is int
                        and phase["observedAtMilliseconds"] == 101000
                        for phase in data["phases"]
                    ),
                )


def report(*, throttled: bool = True) -> dict:
    phases = []
    for policy in POLICIES:
        metrics = {
            "attemptCount": 1,
            "backoffSeconds": 0,
            "runningAt": None,
            "readyAt": None,
        }
        success = {"accepted": True, "metrics": metrics}
        error = {
            "accepted": False,
            "code": "ERR_RETRYABLE",
            "retryable": True,
            "details": {
                "attemptCount": policy["maxAttempts"],
                "backoffSeconds": 0 if policy["maxAttempts"] == 1 else 0.3,
                "throttled": True,
            },
        }
        phases.append(
            {
                "outcomes": [success]
                + [
                    copy.deepcopy(error if throttled else success)
                    for _ in range(policy["count"] - 1)
                ],
                "ready": {**metrics, "runningAt": 100, "readyAt": 101},
                "startedAt": 99,
                "observedAt": 102,
            }
        )
    return {"phases": phases}


class Plane:
    def __init__(self, *, stuck: bool = False) -> None:
        self.states = {
            "baseline": "RUNNING",
            "hidden": "RUNNING",
            "delayed": "RUNNING",
            "other-image": "RUNNING",
        }
        self.listings = 0
        self.params: list[dict[str, Any]] = []
        self.terminated: set[str] = set()
        self.stuck = stuck

    def get_paginator(self, operation: str) -> SimpleNamespace:
        assert operation == "list_microvms"

        def paginate(**params: Any) -> list[dict]:
            self.params.append(params)
            self.listings += 1
            ids = ["baseline", "other-image"] + (
                ["delayed"] if self.listings > 1 else []
            )
            rows = [
                {
                    "microvmId": key,
                    "state": self.states[key],
                    "imageArn": OTHER_IMAGE if key == "other-image" else IMAGE,
                }
                for key in ids
            ]
            if "imageIdentifier" in params:
                wanted = params["imageIdentifier"]
                rows = [
                    row
                    for row in rows
                    if wanted.startswith("arn:") and row["imageArn"] == wanted
                ]
            return [{"items": rows[:1]}, {"items": rows[1:]}]

        return SimpleNamespace(paginate=paginate)

    def get_microvm(self, *, microvmIdentifier: str) -> dict:
        return {"state": self.states[microvmIdentifier]}

    def terminate_microvm(self, *, microvmIdentifier: str) -> None:
        self.terminated.add(microvmIdentifier)
        if not self.stuck:
            self.states[microvmIdentifier] = "TERMINATED"


def clean(plane: Plane) -> bool:
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    return cleanup(
        plane, IMAGE, {"baseline"}, {"hidden"}, clock=lambda: now[0], sleep=sleep
    )


def check_retry_telemetry(results: Results) -> None:
    check_baseline_retries(results)
    check_drivers(results)
    check_observation_resolution(results)
    calls = []
    bounded_plane(
        SimpleNamespace(client=lambda *args, **kwargs: calls.append((args, kwargs)))
    )
    config = calls[0][1]["config"]
    results.eq(
        "retry cleanup bounds each SDK call without automatic retries",
        (config.connect_timeout, config.read_timeout, config.retries),
        (5, 5, {"max_attempts": 0}),
    )
    for throttled in (False, True):
        results.check(
            f"retry telemetry accepts valid observations with throttle={throttled}",
            all(ok for _, ok in verdicts(report(throttled=throttled))),
        )

    bad = report()
    bad["phases"] = []
    results.check(
        "retry telemetry rejects an empty report",
        verdicts(bad)[0][1] is False,
    )
    mutations = {
        "missing launch outcome": lambda p: p["outcomes"].pop(),
        "unknown outcome": lambda p: p["outcomes"][0].pop("accepted"),
        "missing launch metrics": lambda p: p["outcomes"][0].pop("metrics"),
        "excess attempts": lambda p: p["outcomes"][0]["metrics"].update(
            attemptCount=99
        ),
        "negative backoff": lambda p: p["outcomes"][0]["metrics"].update(
            attemptCount=2, backoffSeconds=-1
        ),
        "nonfinite backoff": lambda p: p["outcomes"][0]["metrics"].update(
            backoffSeconds=float("nan")
        ),
        "invented single-attempt backoff": lambda p: p["outcomes"][0]["metrics"].update(
            backoffSeconds=1
        ),
        "missing pre-wait timestamp": lambda p: p["outcomes"][0]["metrics"].pop(
            "runningAt"
        ),
        "premature readiness": lambda p: p["outcomes"][0]["metrics"].update(
            readyAt=100
        ),
        "untyped throttle": lambda p: p["outcomes"][1]["details"].update(
            throttled=False
        ),
        "wrong throttle code": lambda p: p["outcomes"][1].update(code="ERR_PLATFORM"),
        "nonretryable throttle": lambda p: p["outcomes"][1].update(retryable=False),
        "missing throttle telemetry": lambda p: p["outcomes"][1].pop("details"),
        "missing ready timestamp": lambda p: p["ready"].pop("readyAt"),
        "reversed phase timestamps": lambda p: p["ready"].update(readyAt=99),
        "stale RUNNING timestamp": lambda p: p["ready"].update(runningAt=98),
        "future readiness timestamp": lambda p: p["ready"].update(readyAt=103),
        "changed launch attempts": lambda p: p["ready"].update(attemptCount=2),
        "changed launch backoff": lambda p: p["ready"].update(backoffSeconds=1),
        "missing successful launch": lambda p: p["outcomes"].__setitem__(
            0, p["outcomes"][1]
        ),
    }
    targets = {
        "missing launch outcome": "returns every bounded launch outcome",
        "unknown outcome": "returns every bounded launch outcome",
        "missing launch metrics": "reports finite attempt and backoff telemetry",
        "excess attempts": "reports finite attempt and backoff telemetry",
        "negative backoff": "reports finite attempt and backoff telemetry",
        "nonfinite backoff": "reports finite attempt and backoff telemetry",
        "invented single-attempt backoff": "reports finite attempt and backoff telemetry",
        "missing pre-wait timestamp": "leaves readiness timestamps absent before waiting",
        "premature readiness": "leaves readiness timestamps absent before waiting",
        "untyped throttle": "exposes typed retryable throttles for rejected launches",
        "wrong throttle code": "exposes typed retryable throttles for rejected launches",
        "nonretryable throttle": "exposes typed retryable throttles for rejected launches",
        "missing throttle telemetry": "exposes typed retryable throttles for rejected launches",
        "missing ready timestamp": "observes RUNNING before daemon readiness",
        "reversed phase timestamps": "observes RUNNING before daemon readiness",
        "stale RUNNING timestamp": "observes RUNNING before daemon readiness",
        "future readiness timestamp": "observes RUNNING before daemon readiness",
        "changed launch attempts": "preserves launch attempts across readiness",
        "changed launch backoff": "preserves launch attempts across readiness",
        "missing successful launch": "accepts a launch",
    }
    for name, mutate in mutations.items():
        bad = report()
        mutate(bad["phases"][0])
        results.check(
            f"retry telemetry rejects {name}",
            dict(verdicts(bad)).get(f"retry maxAttempts=1 {targets[name]}") is False,
        )

    plane = Plane()
    results.check(
        "retry inventory uses the exact image and every page",
        inventory(plane, IMAGE) == {"baseline": "RUNNING"},
    )
    results.check(
        "retry cleanup finds delayed and hidden VMs without touching the baseline",
        clean(plane)
        and plane.terminated == {"hidden", "delayed"}
        and plane.states["baseline"] == plane.states["other-image"] == "RUNNING",
    )
    results.check(
        "retry cleanup rejects a VM that never terminates", not clean(Plane(stuck=True))
    )

    # Real subprocess protocol: keep accepted IDs even when the final report is lost.
    source = (
        "import sys; sys.stdin.readline(); print("
        + repr(json.dumps({"event": "accepted", "vmId": "partial"}))
        + ", flush=True); sys.exit(3)"
    )
    data, known, failure = run_driver(
        [sys.executable, "-I", "-c", source], {"agentToken": "selftest-canary"}
    )
    results.check(
        "retry driver preserves accepted IDs after a failed process",
        data == {}
        and known == {"partial"}
        and bool(failure)
        and "selftest-canary" not in failure,
    )
    source = "import sys; sys.stdin.readline(); print('not-json')"
    _, _, failure = run_driver([sys.executable, "-I", "-c", source], {})
    results.eq(
        "retry driver rejects malformed protocol without logging raw output",
        failure,
        "driver returned invalid protocol",
    )
    source = (
        "import sys,time; sys.stdin.readline(); print("
        + repr(json.dumps({"event": "accepted", "vmId": "partial"}))
        + ", flush=True); time.sleep(5)"
    )
    with patch("lanes.retry_telemetry.DRIVER_SECONDS", 0.2):
        data, known, failure = run_driver([sys.executable, "-I", "-c", source], {})
    results.check(
        "retry driver preserves accepted IDs after its deadline",
        data == {}
        and known == {"partial"}
        and failure == "driver exceeded bounded runtime",
    )


def check_baseline_retries(results: Results) -> None:
    """A partial or failed baseline never becomes permission to clean existing VMs."""
    target = "ThrottlingException"
    for code in (target, "AccessDeniedException", "persistent throttle"):
        calls: list[str] = []
        elapsed = [0.0]

        def snapshot() -> dict[str, str]:
            calls.append("snapshot")
            if len(calls) == 1 or code != target:
                raise ClientError(
                    {
                        "Error": {
                            "Code": target if code == "persistent throttle" else code
                        }
                    },
                    "ListMicrovms",
                )
            return {"existing": "RUNNING"}

        failure = None
        value = None
        try:
            value = retry_observation(
                snapshot,
                deadline=3,
                clock=lambda: elapsed[0],
                sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
            )
        except ClientError as error:
            failure = error.response["Error"]["Code"]
        results.check(
            f"observer read handles {code} without inventing a snapshot",
            (value == {"existing": "RUNNING"} and failure is None and len(calls) == 2)
            if code == target
            else value is None
            and failure == (target if code == "persistent throttle" else code)
            and len(calls) == (2 if code == "persistent throttle" else 1)
            and elapsed[0] <= 3,
        )

    for failure_code in (None, target, "AccessDeniedException"):
        calls = []
        listed: list[dict] = []
        elapsed = [0.0]

        class SnapshotPlane:
            def get_paginator(self, operation: str) -> SimpleNamespace:
                assert operation == "list_microvms"

                def pages(**params):
                    calls.append("listing")
                    listed.append(params)
                    if not params.get("imageIdentifier", "").startswith("arn:"):
                        return
                    yield {
                        "items": [
                            {
                                "microvmId": "existing",
                                "imageArn": IMAGE,
                                "state": "RUNNING",
                            }
                        ]
                    }
                    if failure_code or calls.count("listing") == 1:
                        raise ClientError(
                            {"Error": {"Code": failure_code or target}}, "ListMicrovms"
                        )
                    yield {
                        "items": [
                            {
                                "microvmId": "second",
                                "imageArn": IMAGE,
                                "state": "RUNNING",
                            }
                        ]
                    }

                return SimpleNamespace(paginate=pages)

        def wait(_timeout):
            calls.append("build")
            return SimpleNamespace(
                commands={
                    "python": ["python", "-I", "old"],
                    "node": ["node", "old", "addon"],
                }
            )

        def driver(_command, _request):
            calls.append("driver")
            data = report()
            for phase in data["phases"]:
                phase["observedAtMilliseconds"] = 102000
            return data, {"new"}, ""

        def clean(_plane, _image, baseline, known):
            calls.append("cleanup")
            return baseline == {"existing", "second"} and known == {"new"}

        probe = Results(probe=True)
        caught = False
        with (
            patch("lanes.retry_telemetry.BASELINE_SECONDS", 3),
            patch("lanes.retry_telemetry.bounded_plane", return_value=SnapshotPlane()),
            patch("lanes.retry_telemetry.run_driver", side_effect=driver),
            patch("lanes.retry_telemetry.cleanup", side_effect=clean),
            patch.dict("os.environ", {"MICROVM_EXECUTION_ROLE_ARN": "role"}),
        ):
            try:
                drive_retry_telemetry(
                    SimpleNamespace(region="us-east-1"),
                    SimpleNamespace(data={"imageIdentifier": IMAGE}),
                    SimpleNamespace(wait=wait),
                    None,
                    probe,
                    clock=lambda: elapsed[0],
                    sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
                )
            except ClientError:
                caught = True
        results.check(
            "retry probe stops before drivers on an exhausted baseline"
            if failure_code == target
            else "retry probe stops before drivers on a nonretryable baseline"
            if failure_code
            else "retry probe rebuilds a throttled baseline before drivers and cleanup",
            caught
            and calls == ["listing"] * (2 if failure_code == target else 1)
            and not probe.passed
            if failure_code
            else not caught
            and calls == ["listing", "listing", "build", "driver", "driver", "cleanup"]
            and not probe.failed,
        )
        if failure_code is None:
            results.check(
                "retry probe lists only its image's VMs, 50 to a page",
                listed
                == [{"imageIdentifier": IMAGE, "PaginationConfig": {"PageSize": 50}}]
                * 2,
                repr(listed),
            )


def check_observation_resolution(results: Results) -> None:
    data = report()
    for phase in data["phases"]:
        phase["ready"]["readyAt"] = 101.0005
        phase["observedAt"] = 101.0
        phase["observedAtMilliseconds"] = 101000
    target = "retry maxAttempts=1 observes RUNNING before daemon readiness"
    results.check(
        "retry telemetry accepts readiness within the Node observation millisecond",
        all(ok for _, ok in verdicts(data, "node")),
    )
    results.check(
        "retry telemetry keeps Python observation timestamps strict",
        dict(verdicts(data, "python"))[target] is False,
    )
    data["phases"][0]["ready"]["readyAt"] = 101.001
    results.check(
        "retry telemetry rejects readiness at the next Node millisecond",
        dict(verdicts(data, "node"))[target] is False,
    )
    data["phases"][0]["ready"]["readyAt"] = 101.0005
    for observed in (None, True, -1, 0, 101000.0, float("nan"), float("inf")):
        data["phases"][0]["observedAtMilliseconds"] = observed
        results.check(
            f"retry telemetry rejects malformed Node observation {observed!r}",
            dict(verdicts(data, "node"))[target] is False,
        )
    data["phases"][0].pop("observedAtMilliseconds")
    results.check(
        "retry telemetry rejects a missing Node observation",
        dict(verdicts(data, "node"))[target] is False,
    )
