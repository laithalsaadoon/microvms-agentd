# SPDX-License-Identifier: Apache-2.0
"""Positive and negative twins for the prefix sweep report checks."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

from harness.results import Results
from lanes.prefix_sweep import (
    CLEANED,
    DRY_RUN,
    check_reports,
    image_vm_ids,
    outside_control,
)


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

    for failure in ("body", "launch"):
        cli = Mock(region="us-east-1")
        aws = Mock()
        aws.client.return_value.get_microvm.return_value = {"state": "TERMINATED"}
        control = SimpleNamespace(data={"microvmId": "fresh-control"})
        source = SimpleNamespace(data={"imageIdentifier": "suite-image"})
        cleanup = Results()
        launch = (
            {"return_value": control}
            if failure == "body"
            else {"side_effect": ValueError("readiness failed")}
        )
        with (
            patch("lanes.prefix_sweep.launch_image", **launch),
            patch(
                "lanes.prefix_sweep.image_vm_ids",
                side_effect=[{"suite-vm"}, {"suite-vm", "fresh-control"}],
            ),
        ):
            try:
                with outside_control(cli, source, aws, cleanup) as launched:
                    assert launched is control
                    raise ValueError("sweep failed")
            except ValueError:
                pass
        results.check(
            f"prefix sweep owns and cleans its fresh outside control after {failure} failure",
            cli.call.call_count == 1
            and cli.call.call_args.args
            == ("terminate", "fresh-control", "--wait", "--region", "us-east-1")
            and not cleanup.failed,
        )

    check_image_vm_listing(results)


def check_image_vm_listing(results: Results) -> None:
    """The sweep lists VMs by its image's ARN and still drops a foreign image's VM."""
    image = "arn:aws:lambda:us-east-1:000000000000:microvm-image:suite-image"
    listed: list[dict] = []

    def pages(**params):
        listed.append(params)
        # The service answers a name given as the filter with no VMs. Past an ARN, this
        # fake ignores the filter, so the lane's own per-item check is what drops the
        # foreign VM.
        if params.get("imageIdentifier", "").startswith("arn:"):
            yield {
                "items": [
                    {"microvmId": "suite-vm", "imageArn": image},
                    {"microvmId": "foreign-vm", "imageArn": image + "-foreign"},
                ]
            }

    plane = SimpleNamespace(
        get_paginator=lambda _operation: SimpleNamespace(paginate=pages)
    )
    found = image_vm_ids(plane, image)
    results.check(
        "prefix sweep lists only its image's VMs by ARN, 50 to a page",
        found == {"suite-vm"}
        and listed
        == [{"imageIdentifier": image, "PaginationConfig": {"PageSize": 50}}],
        repr((found, listed)),
    )
