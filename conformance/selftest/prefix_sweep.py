# SPDX-License-Identifier: Apache-2.0
"""Positive and negative twins for the prefix sweep report checks."""

from __future__ import annotations

from harness.results import Results
from lanes.prefix_sweep import CLEANED, DRY_RUN, check_reports


def check_prefix_sweep(results: Results) -> None:
    dry = {
        "dryRun": True,
        "imagesMatched": ["image-delete", "image-keep"],
        "imagesKept": ["image-keep"],
        "microvmsMatched": ["vm-delete"],
        "imagesDeleted": [],
        "microvmsTerminated": [],
    }
    cleaned = {
        "dryRun": False,
        "imagesDeleted": ["image-delete"],
        "microvmsTerminated": ["vm-delete"],
        "imagesInUse": [],
        "failures": [],
    }
    good = Results()
    check_reports(dry, cleaned, "image-delete", "image-keep", "vm-delete", good)
    results.check("prefix sweep report checks accept confirmed scope", not good.failed)
    for field in dry:
        bad = Results()
        check_reports(
            {k: v for k, v in dry.items() if k != field},
            cleaned,
            "image-delete",
            "image-keep",
            "vm-delete",
            bad,
        )
        results.check(
            f"prefix sweep dry-run rejects missing {field}",
            any(name == DRY_RUN for name, _ in bad.failed),
        )
    for field in cleaned:
        bad = Results()
        check_reports(
            dry,
            {k: v for k, v in cleaned.items() if k != field},
            "image-delete",
            "image-keep",
            "vm-delete",
            bad,
        )
        results.check(
            f"prefix sweep cleanup rejects missing {field}",
            any(name == CLEANED for name, _ in bad.failed),
        )
