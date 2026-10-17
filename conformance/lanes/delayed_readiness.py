# SPDX-License-Identifier: Apache-2.0
"""Measure the image-ready hook past five minutes with a larger request timeout."""

from __future__ import annotations

import base64
import json
import os
import secrets
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness.cli import Cli
from harness.constants import BASELINE_MEMORY_MIB, REPO, SERVICE
from harness.envelope import KindError
from harness.image import conformance_dockerfile, resolve_base_ref
from harness.results import Results

from lanes.ensure_image import (
    READY_IMAGE_STATES,
    ensure_image_cleanup,
    ensure_image_images,
    ensure_image_log_groups,
)
from lanes.isolated_image import CleanupResults

HOOK_TIMEOUT_SECONDS = 600
MEASUREMENT = "ready hook beyond five minutes has a conclusive service outcome (#629)"


def delayed_recipe() -> str:
    """Keep agentd as PID 1; the relay only inserts the measured hook delay."""
    source = (REPO / "conformance/fixtures/delayed_ready.py").read_bytes()
    encoded = base64.b64encode(source).decode("ascii")
    return (
        conformance_dockerfile(resolve_base_ref())
        + "RUN dnf install -y python3 && dnf clean all\n"
        + f"RUN printf '%s' '{encoded}' | base64 -d > /delayed_ready.py\n"
        # Keep the declared hook port and bootstrap command compatible with core
        # preflight. Only the daemon process moves behind the relay; exec keeps
        # the supplied daemon as PID 1 after the launcher starts the relay.
        + "RUN mv /agentd /real-agentd && "
        + "printf '#!/bin/sh\\npython3 /delayed_ready.py &\\n"
        + "export AGENTD_PORT=9001\\nexec /real-agentd\\n' "
        + "> /agentd && chmod 0755 /agentd\n"
    )


def timing_records(messages: list[str]) -> list[dict[str, Any]]:
    records = []
    for message in messages:
        for line in message.splitlines():
            if line.startswith("READY_MEASUREMENT "):
                try:
                    record = json.loads(line.removeprefix("READY_MEASUREMENT "))
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    records.append(record)
    return records


def measurement_outcome(report: dict[str, Any]) -> str | None:
    """A failed build alone says nothing: require the timing and timeout evidence."""
    records = report.get("records") or []
    starts = sum(record.get("event") == "begin" for record in records)
    ends = [record.get("elapsed") for record in records if record.get("event") == "end"]
    requested = report.get("requestedTimeout", 0)
    if (
        requested != HOOK_TIMEOUT_SECONDS
        or report.get("echoedTimeouts") != [HOOK_TIMEOUT_SECONDS]
        or starts == 0
    ):
        return None
    # Even a cap diagnostic cannot make contradictory timing evidence valid.
    # No end is expected if the container stopped; any observed end must prove
    # that this fixture actually delayed the hook beyond five minutes.
    if not all(
        type(value) in (int, float) and 310 <= value < requested for value in ends
    ):
        return None
    if report.get("state") in READY_IMAGE_STATES and len(ends) == starts:
        return "completed beyond five minutes"
    if report.get("state") in {"FAILED", "CREATE_FAILED"} and any(
        "Ready hook invocation timed out after PT5M" in reason
        for reason in report.get("reasons") or []
    ):
        # A timed-out caller does not terminate the relay. Its sleep can finish
        # and emit an end marker after the service has reported the PT5M cap.
        return "service capped the ready hook at five minutes"
    return None


def read_timing_logs(logs: Any, prefix: str) -> list[dict[str, Any]]:
    records = []
    for group in ensure_image_log_groups(logs, prefix):
        for page in logs.get_paginator("filter_log_events").paginate(
            logGroupName=group, filterPattern='"READY_MEASUREMENT"'
        ):
            for event in page.get("events", []):
                for record in timing_records([event["message"]]):
                    records.append({**record, "loggedAtMillis": event.get("timestamp")})
    return records


def drive_delayed_readiness(cli: Cli, binary: Path, aws: Any, results: Results) -> None:
    """The snapshot build calls a delayed real ready hook; independently clean its image."""
    prefix = f"microvm-cli-slow-ready-{secrets.token_hex(4)}"
    cleanup: dict[str, Any] = {}
    plane = aws.client(SERVICE)
    logs = aws.client("logs")
    report: dict[str, Any] = {
        "requestedTimeout": HOOK_TIMEOUT_SECONDS,
        "region": cli.region,
        "apiVersion": plane.meta.service_model.api_version,
        "date": datetime.now(timezone.utc).isoformat(),
    }
    with tempfile.TemporaryDirectory() as temp:
        recipe = Path(temp) / "Dockerfile"
        recipe.write_text(delayed_recipe())
        started = time.monotonic()
        try:
            try:
                reply = cli.call(
                    "build",
                    str(binary),
                    "--dockerfile",
                    str(recipe),
                    "--name",
                    prefix,
                    "--reuse",
                    "--s3-key-prefix",
                    prefix,
                    "--memory",
                    str(BASELINE_MEMORY_MIB),
                    "--build-hook-timeout-sec",
                    str(HOOK_TIMEOUT_SECONDS),
                    "--region",
                    cli.region,
                    timeout=50 * 60,
                )
                cleanup["arn"] = reply.data["imageIdentifier"]
            except KindError as error:
                # A documented service cap is an experimental result, while any
                # unrelated build error must remain a failed measurement.
                report["errorCode"] = error.code
            report["wallSeconds"] = time.monotonic() - started
            images = ensure_image_images(plane, prefix)
            if len(images) == 1:
                cleanup["arn"] = images[0]["imageArn"]
                arn = cleanup["arn"]
                report["state"] = plane.get_microvm_image(imageIdentifier=arn).get(
                    "state"
                )
                reasons = []
                timeouts = set()
                for page in plane.get_paginator("list_microvm_image_versions").paginate(
                    imageIdentifier=arn
                ):
                    for version in page.get("items", []):
                        configured = plane.get_microvm_image_version(
                            imageIdentifier=arn, imageVersion=version["imageVersion"]
                        )
                        timeouts.add(
                            (configured.get("hooks") or {})
                            .get("microvmImageHooks", {})
                            .get("readyTimeoutInSeconds")
                        )
                        for builds in plane.get_paginator(
                            "list_microvm_image_builds"
                        ).paginate(
                            imageIdentifier=arn, imageVersion=version["imageVersion"]
                        ):
                            for build in builds.get("items", []):
                                reason = str(build.get("stateReason") or "")
                                # Service diagnostics can contain resource identifiers;
                                # retain only the fixed string this experiment tests.
                                if (
                                    "Ready hook invocation timed out after PT5M"
                                    in reason
                                ):
                                    reasons.append(
                                        "Ready hook invocation timed out after PT5M"
                                    )
                                elif reason:
                                    reasons.append("other build failure")
                report["reasons"] = reasons
                report["echoedTimeouts"] = sorted(timeouts, key=str)
            # CloudWatch delivery can trail the terminal build status. Wait for
            # this run's timing markers, never read logs from another image.
            deadline = time.monotonic() + 120
            while True:
                report["records"] = read_timing_logs(logs, prefix)
                outcome = measurement_outcome(report)
                if outcome is not None or time.monotonic() >= deadline:
                    break
                time.sleep(5)
            report["outcome"] = outcome
            print("READY_MEASUREMENT_RESULT " + json.dumps(report, sort_keys=True))
            results.check(
                MEASUREMENT, outcome is not None, json.dumps(report, sort_keys=True)
            )
        finally:
            ensure_image_cleanup(
                cleanup,
                prefix,
                os.environ["MICROVM_BUCKET"],
                prefix,
                aws,
                CleanupResults(results, "delayed-readiness"),
            )
