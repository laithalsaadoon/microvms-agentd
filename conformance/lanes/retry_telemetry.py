# SPDX-License-Identifier: Apache-2.0
"""Actual-AWS retry/launch observations (#674, #677), with a bounded request burst.

AWS need not throttle at any assumed quota. The report explicitly says whether a typed
throttle was observed; a quiet run proves launch telemetry but cannot prove live throttling.
One stable token and identical payload per binding limit this section to two intended VMs.
The shared image's baseline is excluded from independent, eventually consistent cleanup.
"""

from __future__ import annotations

import json
import math
import os
import secrets
import subprocess
import time
from collections.abc import Callable
from typing import Any

from botocore.config import Config
from harness.cli import Cli
from harness.constants import REPO, SERVICE
from harness.envelope import Envelope
from harness.results import Results

from lanes.bindings import BINDINGS, BUILD_TIMEOUT_SEC, BindingBuild

POLICIES = ({"maxAttempts": 1, "count": 8}, {"maxAttempts": 3, "count": 4})
BUDGET_SECONDS = 10
DRIVER_SECONDS = 15 * 60
CLEANUP_SECONDS = 180
VISIBILITY_SECONDS = 60
POLL_SECONDS = 2


def bounded_plane(aws: Any) -> Any:
    # The cleanup deadline is checked between iterations: bound each SDK call too,
    # so one unresponsive connection cannot keep that final iteration alive forever.
    return aws.client(
        SERVICE,
        config=Config(connect_timeout=5, read_timeout=5, retries={"max_attempts": 0}),
    )


def finite(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def metric_valid(value: Any, cap: int) -> bool:
    if not isinstance(value, dict):
        return False
    attempts, backoff = value.get("attemptCount"), value.get("backoffSeconds")
    return (
        type(attempts) is int
        and 1 <= attempts <= cap
        and finite(backoff)
        and backoff >= 0
        and (attempts != 1 or backoff == 0)
    )


def observed_after_ready(phase: dict, ready_at: float, binding: str) -> bool:
    if binding == "node":
        observed_ms = phase.get("observedAtMilliseconds")
        # Date.now() identifies [sample, sample + 1 ms), not an exact instant.
        # The caller selects the known driver; a report cannot widen this bound.
        return (
            type(observed_ms) is int
            and observed_ms > 0
            and finite(observed_ms)
            and ready_at < (observed_ms + 1) / 1000
        )
    observed = phase.get("observedAt")
    return binding == "python" and finite(observed) and ready_at <= observed


def verdicts(report: dict, binding: str = "python") -> list[tuple[str, bool]]:
    phases = report.get("phases") or []
    checks = [("reports both configured retry policies", len(phases) == len(POLICIES))]
    for index, policy in enumerate(POLICIES):
        phase = phases[index] if index < len(phases) else {}
        rows = phase.get("outcomes") or []
        accepted = [row for row in rows if row.get("accepted") is True]
        errors = [row for row in rows if row.get("accepted") is False]
        prefix = f"retry maxAttempts={policy['maxAttempts']}"
        # Ordinary transport/5xx failures retain five retries. The final throttle bit
        # cannot reveal those earlier failures, so do not infer an all-throttle sequence.
        cap = policy["maxAttempts"] + 5
        checks += [
            (
                f"{prefix} returns every bounded launch outcome",
                len(rows) == policy["count"]
                and len(accepted) + len(errors) == len(rows),
            ),
            (f"{prefix} accepts a launch", bool(accepted)),
            (
                f"{prefix} reports finite attempt and backoff telemetry",
                bool(accepted)
                and all(metric_valid(row.get("metrics"), cap) for row in accepted),
            ),
            (
                f"{prefix} leaves readiness timestamps absent before waiting",
                bool(accepted)
                and all(
                    isinstance(row.get("metrics"), dict)
                    and {"runningAt", "readyAt"} <= row["metrics"].keys()
                    and row["metrics"].get("runningAt") is None
                    and row["metrics"].get("readyAt") is None
                    for row in accepted
                ),
            ),
            (
                f"{prefix} exposes typed retryable throttles for rejected launches",
                all(
                    row.get("code") == "ERR_RETRYABLE"
                    and row.get("retryable") is True
                    and isinstance(row.get("details"), dict)
                    and row["details"].get("throttled") is True
                    and metric_valid(row["details"], cap)
                    for row in errors
                ),
            ),
        ]
        ready = phase.get("ready") or {}
        running_at, ready_at = ready.get("runningAt"), ready.get("readyAt")
        started = phase.get("startedAt")
        checks.append(
            (
                f"{prefix} observes RUNNING before daemon readiness",
                metric_valid(ready, cap)
                and finite(running_at)
                and finite(ready_at)
                and finite(started)
                and 0 < started <= running_at <= ready_at
                and observed_after_ready(phase, ready_at, binding),
            )
        )
        checks.append(
            (
                f"{prefix} preserves launch attempts across readiness",
                bool(accepted)
                and all(
                    accepted[0].get("metrics", {}).get(key) == ready.get(key)
                    for key in ("attemptCount", "backoffSeconds")
                ),
            )
        )
    return checks


def run_driver(command: list[str], request: dict) -> tuple[dict, set[str], str]:
    """Retain partial accepted events on timeout; never log raw stdout/stderr."""
    failure = ""
    try:
        done = subprocess.run(
            command,
            input=json.dumps(request) + "\n",
            text=True,
            capture_output=True,
            timeout=DRIVER_SECONDS,
            cwd=REPO,
            check=False,
        )
        output = done.stdout
        if done.returncode:
            failure = "driver exited unsuccessfully"
    except subprocess.TimeoutExpired as error:
        output = error.stdout or ""
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        failure = "driver exceeded bounded runtime"
    except OSError:
        return {}, set(), "driver could not start"
    report: dict = {}
    known: set[str] = set()
    for line in output.splitlines():
        try:
            event = json.loads(line)
            if event.get("event") == "accepted" and isinstance(event.get("vmId"), str):
                known.add(event["vmId"])
            elif event.get("event") == "report":
                report = event
            else:
                failure = failure or "driver reported failure"
        except (ValueError, AttributeError):
            failure = "driver returned invalid protocol"
    return report, known, failure


def inventory(plane: Any, image: str) -> dict[str, str]:
    found = {}
    for page in plane.get_paginator("list_microvms").paginate():
        for item in page["items"]:
            if item.get("imageArn") == image:
                found[item["microvmId"]] = item["state"]
    return found


def cleanup(
    plane: Any,
    image: str,
    baseline: set[str],
    known: set[str],
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    started = clock()
    owned = known - baseline
    while True:
        read_ok = True
        try:
            owned.update(set(inventory(plane, image)) - baseline)
        except Exception:  # noqa: BLE001 - continue cleaning known IDs after a listing error
            read_ok = False
        terminal = set()
        for vm in owned:
            try:
                state = plane.get_microvm(microvmIdentifier=vm).get("state")
                if state == "TERMINATED":
                    terminal.add(vm)
                elif state != "TERMINATING":
                    plane.terminate_microvm(microvmIdentifier=vm)
            except Exception:  # noqa: BLE001 - bounded retries also cover transient failures
                read_ok = False
        elapsed = clock() - started
        if read_ok and terminal == owned and elapsed >= VISIBILITY_SECONDS:
            return len(owned) <= len(BINDINGS)
        if elapsed >= CLEANUP_SECONDS:
            return False
        sleep(POLL_SECONDS)


def drive_retry_telemetry(
    cli: Cli, launched: Envelope, build: BindingBuild, aws: Any, results: Results
) -> None:
    print("\n-- retry telemetry (#674, #677: bounded AWS launch probes) --")
    image = str(launched.data["imageIdentifier"])
    plane = bounded_plane(aws)
    baseline = set(inventory(plane, image))
    known: set[str] = set()
    built = build.wait(BUILD_TIMEOUT_SEC)
    try:
        for binding in BINDINGS:
            report: dict = {}
            command = built.commands.get(binding)
            failure = "binding was not built"
            if command:
                command = list(command)
                script_index = 2 if binding == "python" else 1
                command[script_index] = str(
                    REPO
                    / "conformance"
                    / "drivers"
                    / ("retry.py" if binding == "python" else "retry.mjs")
                )
                report, accepted, failure = run_driver(
                    command,
                    {
                        "region": cli.region,
                        "image": image,
                        "role": os.environ["MICROVM_EXECUTION_ROLE_ARN"],
                        "agentToken": secrets.token_hex(32),
                        "clientToken": secrets.token_hex(32),
                        "policies": POLICIES,
                        "budgetSeconds": BUDGET_SECONDS,
                        "lifetimeSeconds": DRIVER_SECONDS,
                        "runningTimeoutSeconds": 180,
                    },
                )
                known.update(accepted)
                results.check(
                    f"{binding} stable launch token resolves one VM", len(accepted) == 1
                )
            results.check(f"{binding} retry driver completes", not failure, failure)
            prefix = "BIND-3" if binding == "python" else "BIND-4"
            for name, ok in verdicts(report, binding):
                results.check(f"{prefix} {binding} {name}", ok)
            rows = [
                row
                for phase in report.get("phases", [])
                for row in phase.get("outcomes", [])
            ]
            details = [row.get("metrics") or row.get("details") or {} for row in rows]
            observed = any(detail.get("throttled") is True for detail in details)
            print(
                f"  {binding}: throttleObserved={str(observed).lower()} "
                f"attemptCounts={[detail.get('attemptCount') for detail in details]} "
                f"backoffSeconds={[detail.get('backoffSeconds') for detail in details]}"
            )
    finally:
        results.check(
            "retry probe independently terminates only new exact-image VMs",
            cleanup(plane, image, baseline, known),
        )
