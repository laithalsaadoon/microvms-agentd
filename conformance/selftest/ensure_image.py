# SPDX-License-Identifier: Apache-2.0
"""The ensure-image checks against complete and gapped reports, offline."""

from __future__ import annotations

from typing import Any

from harness.results import Results
from lanes.ensure_image import ensure_image_checks


def check_ensure_image_section(results: "Results") -> None:
    """The ensure-image checks pass on a complete report and each fails on its gap."""
    prefix, bucket, key_prefix = "conformance-ensure-abcd1234", "bucket", "p/abcd1234"
    name = f"{prefix}-0123456789ab"
    arn = f"arn:aws:lambda:us-east-1:123456789012:microvm-image:{name}"
    uri = f"s3://{bucket}/{key_prefix}/{name}/artifact.zip"
    complete: dict[str, Any] = {
        "name": name,
        "arn": arn,
        "artifactUri": uri,
        "warnings": ["skipped link.sh: it is a symlink"],
        "race": {
            "reused": [False, True],
            "uploaded": [True, True],
            "identifiers": [arn, arn],
            "versions": ["1", "1"],
            "states": ["CREATED", "CREATED"],
        },
        "reuse": {"reused": True, "uploaded": False, "identifier": arn},
        "accountCalls": [1, 1],
        "puts": [uri, uri],
        "guest": {"uid": "0", "context": "context-ok", "excluded": "excluded"},
        "forced": {
            "reused": False,
            "uploaded": True,
            "identifier": arn,
            "state": "CREATED",
        },
    }
    probe = Results(probe=True)
    ensure_image_checks(complete, prefix, bucket, key_prefix, probe)
    results.check(
        "the ensure-image checks all pass on a complete report",
        len(probe.passed) == 9 and not probe.failed,
        f"passed={len(probe.passed)} failed={probe.failed!r}",
    )
    gaps = {
        "IMAGE-11": {
            **complete,
            "race": {**complete["race"], "reused": [False, False]},
        },
        "IMAGE-9": {**complete, "reuse": {**complete["reuse"], "uploaded": True}},
        "IMAGE-8": {**complete, "accountCalls": [2, 1]},
        "IMAGE-2": {**complete, "guest": {**complete["guest"], "uid": "65534"}},
        "IMAGE-10": {**complete, "forced": {"error": "refused"}},
    }
    missed = []
    for key, report in gaps.items():
        probe = Results(probe=True)
        ensure_image_checks(report, prefix, bucket, key_prefix, probe)
        failed = [name for name, _ in probe.failed]
        if len(failed) != 1 or not failed[0].startswith(key):
            missed.append(f"{key}: {failed!r}")
    results.check(
        "each ensure-image check fails on the one gap it is about",
        not missed,
        "; ".join(missed),
    )
