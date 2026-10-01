# SPDX-License-Identifier: Apache-2.0
"""The live suite's COST checks, offline: each passes a report that keeps its rule and fails one
that breaks it. The reports are the shape `CostReport::to_json` writes."""

from __future__ import annotations

import copy
from typing import Any

from harness.results import Results
from lanes.lifecycle import check_run_cost
from lanes.local import check_estimate_cost


def item(phase: str, amount: dict[str, Any], provenance: str | None) -> dict[str, Any]:
    return {
        "phase": phase,
        "amount": amount,
        "duration": None
        if provenance is None
        else {"seconds": 60.0, "provenance": provenance},
    }


PRICED = {"kind": "estimated-usd", "usd": "0.0996998400"}
UNPRICED = {
    "kind": "unpriced",
    "reason": "AWS does not publish whether the build is billed",
}

#: A run that built its image, as `run`'s envelope reports one: the build line unpriced, the rest
#: priced, the timed phases measured and the storage line's retention floor projected.
RUN = {
    "label": "run microvm-cli-conformance",
    "fullyMeasured": False,
    "items": [
        item("image-build", UNPRICED, "measured"),
        item("image-storage", PRICED, "projected"),
        item("launch", PRICED, None),
        item("running", PRICED, "measured"),
    ],
    "total": {
        "isLowerBound": True,
        "priced": "0.0996998400",
        "render": "at least ~$0.099700 (estimated), plus 1 unpriced (image-build)",
    },
}

#: `cost --estimate`: every duration projected.
ESTIMATE = {
    "label": "estimate",
    "fullyMeasured": False,
    "items": [item("launch", PRICED, None), item("running", PRICED, "projected")],
    "total": {"isLowerBound": False, "priced": "0.0996998400", "render": "~$0.099700"},
}


def failures(check: Any, report: dict[str, Any]) -> list[str]:
    """The names of the checks `check` fails on `report`, run against a probe."""
    probe = Results(probe=True)
    check(report, probe)
    return [name for name, _ in probe.failed]


def check_cost_checks(results: Results) -> None:
    results.eq(
        "the run cost checks pass a report that keeps COST-1, COST-3 and COST-4",
        failures(check_run_cost, RUN),
        [],
    )
    results.eq(
        "the estimate cost check passes an estimate that keeps COST-10",
        failures(check_estimate_cost, ESTIMATE),
        [],
    )

    def broken(report: dict[str, Any], edit: Any) -> dict[str, Any]:
        copied = copy.deepcopy(report)
        edit(copied)
        return copied

    def timed_projected(report: dict[str, Any]) -> None:
        report["items"][3]["duration"]["provenance"] = "projected"

    def unlabelled(report: dict[str, Any]) -> None:
        del report["items"][3]["duration"]["provenance"]

    def retention_measured(report: dict[str, Any]) -> None:
        report["items"][1]["duration"]["provenance"] = "measured"

    def zero_build(report: dict[str, Any]) -> None:
        report["items"][0]["amount"] = {"kind": "estimated-usd", "usd": "0.00"}

    def no_build(report: dict[str, Any]) -> None:
        del report["items"][0]

    def plain_sum(report: dict[str, Any]) -> None:
        report["total"]["isLowerBound"] = False

    def no_durations(report: dict[str, Any]) -> None:
        for line in report["items"]:
            line["duration"] = None

    for name, edit, key in (
        ("a timed run duration labelled projected", timed_projected, "COST-1"),
        ("a run duration with no provenance", unlabelled, "COST-1"),
        ("a retention floor labelled measured", retention_measured, "COST-1"),
        ("a run report with no duration at all", no_durations, "COST-1"),
        ("an image build priced at zero dollars", zero_build, "COST-3"),
        ("a report with no image build line", no_build, "COST-3"),
        ("a total that isn't a lower bound", plain_sum, "COST-4"),
    ):
        failed = failures(check_run_cost, broken(RUN, edit))
        results.check(
            f"the run cost checks refuse {name}",
            any(check.startswith(key) for check in failed),
            f"failed={failed!r}",
        )

    def measured(report: dict[str, Any]) -> None:
        report["items"][1]["duration"]["provenance"] = "measured"

    def relabelled(report: dict[str, Any]) -> None:
        report["label"] = "run"

    for name, edit in (
        ("an estimate with a measured duration", measured),
        ("an estimate labelled as a run", relabelled),
    ):
        failed = failures(check_estimate_cost, broken(ESTIMATE, edit))
        results.check(
            f"the estimate cost check refuses {name}",
            any(check.startswith("COST-10") for check in failed),
            f"failed={failed!r}",
        )
