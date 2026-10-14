# SPDX-License-Identifier: Apache-2.0
"""The measurement lane refuses absent observations without assuming AWS outcomes."""

from __future__ import annotations

import copy

from harness.results import Results
from lanes.platform_measurements import (
    DROPPED,
    STATUS_FIELDS,
    check_measurement,
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
