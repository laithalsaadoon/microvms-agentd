# SPDX-License-Identifier: Apache-2.0
"""The measurement lane refuses absent observations without assuming AWS outcomes."""

from __future__ import annotations

import copy
import io
import json
import os
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from drivers import platform_probe
from harness.results import Results
from lanes.platform_measurements import (
    DROPPED,
    SNAPSHOT_MARKER,
    STATUS_FIELDS,
    check_measurement,
    drive_platform_measurements,
    measurement_dockerfile,
)


def check_platform_measurements(results: Results) -> None:
    status = dict.fromkeys(STATUS_FIELDS, "0")
    report = {
        "daemon": status,
        "execBefore": status,
        "execAfter": status,
        "build": {"before": status, "after": status, "dropErrnos": {}},
        "dropErrnos": {str(cap): 1 for cap in DROPPED},
        "memOpenErrnos": {"0": 0, "4242": 13},
        "ptraceScope": "1",
        "blackholeExit": 0,
        "metadataConnectBefore": 0,
        "metadataConnectAfter": 101,
    }
    positive = Results(probe=True)
    check_measurement(report, "test", positive)
    results.check(
        "measurement accepts complete observations, including refusals",
        not positive.failed,
    )
    gaps = [{}]
    for key in report:
        if key != "metadataConnectAfter":
            missing = copy.deepcopy(report)
            del missing[key]
            gaps.append(missing)
    missing = copy.deepcopy(report)
    del missing["daemon"]["CapEff"]
    gaps.append(missing)
    missing = copy.deepcopy(report)
    del missing["metadataConnectAfter"]
    gaps.append(missing)
    for path in (
        ("daemon", "CapEff"),
        ("build", "before", "CapEff"),
        ("build", "after", "CapEff"),
        ("dropErrnos", "21"),
        ("memOpenErrnos", "4242"),
        ("ptraceScope",),
        ("blackholeExit",),
        ("metadataConnectBefore",),
        ("metadataConnectAfter",),
        ("build", "dropErrnos"),
    ):
        for invalid in (None, "", True, "garbage", "-1"):
            missing = copy.deepcopy(report)
            node = missing
            for key in path[:-1]:
                node = node[key]
            node[path[-1]] = invalid
            gaps.append(missing)
    for missing in gaps:
        negative = Results(probe=True)
        check_measurement(missing, "test", negative)
        results.check(
            "measurement rejects every absent observation", bool(negative.failed)
        )
    no_route = copy.deepcopy(report)
    no_route["blackholeExit"] = 2
    no_route["metadataConnectAfter"] = None
    refused = Results(probe=True)
    check_measurement(no_route, "test", refused)
    results.check(
        "measurement records a refused blackhole without inventing connectivity",
        not refused.failed,
    )
    recipe = measurement_dockerfile(True)
    results.check(
        "snapshot measurement drops before execing the daemon",
        '"bootstrap", "--drop"' in recipe and 'CMD ["/agentd"]' not in recipe,
    )

    snapshot = copy.deepcopy(report)
    for name in ("daemon", "execBefore", "execAfter"):
        snapshot[name] = dict(status)
    snapshot["build"] = {
        "before": {**status, "CapBnd": f"{1 << SNAPSHOT_MARKER:x}"},
        "after": dict(status),
        "dropErrnos": {str(cap): 0 for cap in (*DROPPED, SNAPSHOT_MARKER)},
    }
    marker_result = Results(probe=True)
    check_measurement(snapshot, "snapshot-drop", marker_result)
    results.check(
        "snapshot marker accepts an observed change", not marker_result.failed
    )
    for path, value in (
        (("build", "before", "CapBnd"), "0"),
        (("build", "dropErrnos", str(SNAPSHOT_MARKER)), 1),
        (("build", "dropErrnos", str(SNAPSHOT_MARKER)), None),
        (("build", "after", "CapBnd"), f"{1 << SNAPSHOT_MARKER:x}"),
        (("daemon", "CapBnd"), f"{1 << SNAPSHOT_MARKER:x}"),
        (("execBefore", "CapBnd"), f"{1 << SNAPSHOT_MARKER:x}"),
        (("execAfter", "CapBnd"), f"{1 << SNAPSHOT_MARKER:x}"),
    ):
        broken = copy.deepcopy(snapshot)
        node = broken
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
        negative = Results(probe=True)
        check_measurement(broken, "snapshot-drop", negative)
        results.check(
            "snapshot marker rejects an absent or retained change",
            any(
                name == "snapshot marker changed and persisted"
                for name, _ in negative.failed
            ),
        )

    with (
        patch.object(platform_probe.sys, "argv", ["probe", "bootstrap", "--drop"]),
        patch.object(platform_probe, "status", return_value=status),
        patch.object(platform_probe, "drop", return_value={}) as dropped,
        patch.object(platform_probe.BUILD_REPORT.__class__, "write_text"),
        patch.object(platform_probe.os, "execv"),
    ):
        platform_probe.main()
    results.check(
        "snapshot bootstrap requests the marker drop",
        dropped.call_args.args == ((*DROPPED, SNAPSHOT_MARKER),),
    )
    check_measurement_selector(results, report, snapshot)


def check_measurement_selector(results: Results, report: dict, snapshot: dict) -> None:
    # Import lazily: the entry point imports this module while assembling its self-test.
    from lanes.suite import run_suite
    from run_rs import parse_args

    selected = parse_args(
        [
            "--only",
            "platform_measurements",
            "--measurement-variant",
            "snapshot-drop",
            "--binary",
            __file__,
            "--microvm-binary",
            __file__,
        ]
    )
    for argv in (
        ["--measurement-variant", "snapshot-drop"],
        ["--only", "default_user", "--measurement-variant", "snapshot-drop"],
        ["--only", "platform_measurements", "--measurement-variant", "unknown"],
    ):
        with redirect_stderr(io.StringIO()):
            try:
                parse_args(argv)
            except SystemExit as error:
                rejected = error.code == 2
            else:
                rejected = False
        results.check("measurement selector rejects invalid combinations", rejected)
    called = []
    outputs = {
        "s3_bucket": {"value": "test-bucket"},
        "build_role_arn": {"value": "test-build"},
        "execution_role_arn": {"value": "test-execution"},
    }
    with (
        patch("lanes.suite.sh", return_value=json.dumps(outputs)),
        patch("lanes.suite.boto3.Session", return_value=None),
        patch(
            "lanes.suite.drive_platform_measurements",
            side_effect=lambda *args: called.append(args[-1]),
        ),
        patch.dict(os.environ),
        redirect_stdout(io.StringIO()),
    ):
        result = run_suite(selected)
    results.check(
        "measurement selector reaches the selected lane",
        result == 0 and called == ["snapshot-drop"],
    )

    for variant, expected in (
        (None, ["default", "repair", "snapshot-drop"]),
        ("snapshot-drop", ["snapshot-drop"]),
    ):
        labels = []

        @contextmanager
        def image(*args, **kwargs):
            labels.append(args[4].removeprefix("measure-"))
            yield SimpleNamespace(), {}

        class Cli:
            def call(self, *args):
                observed = snapshot if labels[-1] == "snapshot-drop" else report
                return SimpleNamespace(
                    data={"exitCode": 0, "stdout": json.dumps(observed)}
                )

        lane_results = Results(probe=True)
        with (
            patch("lanes.platform_measurements.isolated_image", image),
            patch(
                "lanes.platform_measurements.measurement_dockerfile", return_value=""
            ),
            patch("lanes.platform_measurements.launch_image", return_value=None),
            patch("lanes.platform_measurements.attach_args", return_value=[]),
            redirect_stdout(io.StringIO()),
        ):
            drive_platform_measurements(
                Cli(), Path(__file__), None, lane_results, variant
            )
        results.check(
            "measurement selector preserves the requested variant set",
            labels == expected and not lane_results.failed,
        )
