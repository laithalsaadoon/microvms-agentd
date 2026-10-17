# SPDX-License-Identifier: Apache-2.0
"""Bounded launch probes through Python's public binding, JSON lines on stdin/stdout.

Tokens arrive only on stdin. Accepted IDs are emitted immediately for independent cleanup;
errors expose typed telemetry only, never exception messages or request payloads.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Any

import microvms


def emit(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, **fields}), flush=True)


def metrics(session: Any) -> dict | None:
    value = session.launch_metrics
    if value is None:
        return None
    return {
        "attemptCount": value.get("attempt_count"),
        "backoffSeconds": value.get("backoff_seconds"),
        "runningAt": value.get("running_at"),
        "readyAt": value.get("ready_at"),
    }


async def run(request: dict) -> None:
    phases = []
    for policy in request["policies"]:
        # Construct before the burst, so credential resolution is not its pacing source.
        sandboxes = await asyncio.gather(
            *(
                microvms.Sandbox.create_async(
                    microvms.Region.parse(request["region"]),
                    retry=(policy["maxAttempts"], request["budgetSeconds"]),
                )
                for _ in range(policy["count"])
            )
        )

        async def launch(sandbox: Any) -> dict:
            try:
                session = await sandbox.run_async(
                    image_identifier=request["image"],
                    execution_role_arn=request["role"],
                    agent_token=request["agentToken"],
                    client_token=request["clientToken"],
                    identity=False,
                    wait=False,
                    max_idle_sec=request["lifetimeSeconds"],
                    suspended_sec=request["lifetimeSeconds"],
                    max_duration_sec=request["lifetimeSeconds"],
                )
                emit("accepted", vmId=sandbox.microvm_id)
                return {"accepted": True, "metrics": metrics(session)}
            except microvms.MicrovmError as error:
                return {
                    "accepted": False,
                    "code": error.code,
                    "retryable": error.retryable,
                    "details": {
                        "attemptCount": error.attempt_count,
                        "backoffSeconds": error.backoff_seconds,
                        "throttled": error.throttled,
                    },
                }

        started = time.time()
        outcomes = await asyncio.gather(*(launch(sandbox) for sandbox in sandboxes))
        ready = None
        for sandbox, outcome in zip(sandboxes, outcomes, strict=True):
            if outcome["accepted"]:
                session = await sandbox.wait_until_running_async(
                    timeout=request["runningTimeoutSeconds"]
                )
                ready = metrics(session)
                break
        phases.append(
            {
                "outcomes": outcomes,
                "ready": ready,
                "startedAt": started,
                "observedAt": time.time(),
            }
        )
    emit("report", phases=phases)


def main() -> int:
    try:
        asyncio.run(run(json.loads(sys.stdin.readline())))
    except Exception as error:  # noqa: BLE001 - no exception text crosses this boundary
        emit("failed", errorType=type(error).__name__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
