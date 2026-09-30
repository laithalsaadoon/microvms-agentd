# SPDX-License-Identifier: Apache-2.0
"""The `live_preflight` reader and the `doctor --region` check, offline."""

from __future__ import annotations

from typing import Any

from harness.results import Results
from lanes.local import doctor_region_lines, preflight_lines


def check_preflight_lines(results: "Results") -> None:
    """The `live_preflight` reader keys each run's check and skips the count line."""
    stderr = (
        "PREFLIGHT suite region=ok ran=true fatal=true detail=us-east-1 is known\n"
        "PREFLIGHT suite service=fail ran=true fatal=true detail=denied\n"
        "PREFLIGHT suite ok=false\n"
        "PREFLIGHT suite vms+images before=(1, 2) after=(1, 2)\n"
        "test result: ok\n"
    )
    results.eq(
        "the preflight-line reader keys each run's check",
        preflight_lines(stderr),
        {"suite region": "ok", "suite service": "fail", "suite ok": "false"},
    )


def check_doctor_region_lines(results: "Results") -> None:
    """`doctor_region_lines` passes the fixed report and refuses main's, an empty one, a
    versions line on the environment's base ARN, a credentials line on a third region, and
    a listing that didn't read."""

    def report(region: str, bases: str) -> list[dict[str, Any]]:
        return [
            {"name": "region", "detail": "us-east-1 is a known MicroVMs region"},
            {
                "name": "credentials",
                "detail": f"the default chain resolved credentials for {region}",
            },
            {"name": "bucket", "detail": "conformance-bucket-us-west-2"},
            {"name": "managed-bases", "detail": bases},
            {"name": "base-image-versions", "detail": "al2023: 1 - pass one"},
        ]

    def only(region: str) -> str:
        return (
            f"arn:aws:lambda:{region}:aws:microvm-image:al2023-1 is the only base AWS "
            f"publishes in {region}"
        )

    fixed = report("us-east-1", only("us-east-1"))
    ok, detail = doctor_region_lines(fixed, "us-east-1", "us-west-2")
    results.check(
        "doctor_region_lines passes a report that names the flag's region on every line",
        ok,
        detail,
    )
    # What main printed before #250: every line below the region line on AWS_REGION's.
    unfixed = report("us-west-2", only("us-west-2"))
    ok, detail = doctor_region_lines(unfixed, "us-east-1", "us-west-2")
    results.check(
        "doctor_region_lines refuses a report that names the environment's region",
        not ok,
        detail,
    )
    ok, detail = doctor_region_lines([], "us-east-1", "us-west-2")
    results.check("doctor_region_lines refuses an empty report", not ok, detail)
    # The versions read on AWS_REGION's base ARN, with the two lines above it right.
    versions = [
        row
        if row["name"] != "base-image-versions"
        else {
            "name": "base-image-versions",
            "detail": "arn:aws:lambda:us-west-2:aws:microvm-image:al2023-1 reports no "
            "versions at all",
        }
        for row in fixed
    ]
    ok, detail = doctor_region_lines(versions, "us-east-1", "us-west-2")
    results.check(
        "doctor_region_lines refuses a versions line on the environment's base ARN",
        not ok,
        detail,
    )
    # A credentials line on a third region, with the listing right: the `other` clause
    # can't see it, so only the credentials clause refuses it.
    third = report("eu-central-1", only("us-east-1"))
    ok, detail = doctor_region_lines(third, "us-east-1", "us-west-2")
    results.check(
        "doctor_region_lines refuses a credentials line on a third region",
        not ok,
        detail,
    )
    unread = report("us-east-1", "could not list the managed bases: denied")
    ok, detail = doctor_region_lines(unread, "us-east-1", "us-west-2")
    results.check(
        "doctor_region_lines refuses a managed-bases line that listed nothing",
        not ok,
        detail,
    )
