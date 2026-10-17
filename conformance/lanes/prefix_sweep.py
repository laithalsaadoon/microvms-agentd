# SPDX-License-Identifier: Apache-2.0
"""Prefix cleanup against dedicated images, preserving a kept image and the suite VM."""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from harness.constants import SERVICE
from harness.envelope import Envelope
from harness.image import conformance_dockerfile, resolve_base_ref
from harness.results import Results

from lanes.ensure_image import READY_IMAGE_STATES, image_state
from lanes.isolated_image import isolated_image, launch_image

DRY_RUN = "prefix sweep dry-run selects only unkept resources (#675)"
CLEANED = "prefix sweep confirms scoped VM termination and image deletion (#675)"
PRESERVED = "prefix sweep preserves kept and outside-prefix resources (#675)"
TIMESTAMP = "VM list summaries preserve service start timestamps (#617)"


def check_reports(
    dry: dict[str, Any],
    cleaned: dict[str, Any],
    deleted_image: str,
    kept_image: str,
    deleted_vm: str,
    results: Results,
) -> None:
    """Compare inventories and confirmed outcomes; absent report values fail closed."""
    results.check(
        DRY_RUN,
        dry.get("dryRun") is True
        and set(dry.get("imagesMatched") or []) == {deleted_image, kept_image}
        and dry.get("imagesKept") == [kept_image]
        and dry.get("microvmsMatched") == [deleted_vm]
        and dry.get("imagesDeleted") == []
        and dry.get("microvmsTerminated") == [],
        "dry-run scope and empty mutation outcomes",
    )
    results.check(
        CLEANED,
        cleaned.get("dryRun") is False
        and cleaned.get("imagesDeleted") == [deleted_image]
        and cleaned.get("microvmsTerminated") == [deleted_vm]
        and cleaned.get("imagesInUse") == []
        and cleaned.get("failures") == [],
        "confirmed deletion and termination, without refusals",
    )


def drive_prefix_sweep(
    cli: Any, binary: Path, aws: Any, outside: Envelope, results: Results
) -> None:
    """Build two images owned by this lane, launch each, and sweep only the unkept one.

    Independent context cleanup finds resources even if the command fails before returning
    its report. The suite VM supplies the outside-prefix control without another build.
    """
    label = f"sweep-{secrets.token_hex(4)}"
    prefix = f"microvm-cli-{label}-"
    recipe = conformance_dockerfile(resolve_base_ref())
    with (
        isolated_image(cli, binary, aws, results, f"{label}-delete", recipe) as (
            deleted,
            delete_cleanup,
        ),
        isolated_image(cli, binary, aws, results, f"{label}-keep", recipe) as (
            kept,
            keep_cleanup,
        ),
    ):
        victim = launch_image(cli, deleted, delete_cleanup)
        survivor = launch_image(cli, kept, keep_cleanup)
        deleted_image = str(deleted.data["imageIdentifier"])
        kept_image = str(kept.data["imageIdentifier"])
        deleted_vm = str(victim.data["microvmId"])
        kept_vm = str(survivor.data["microvmId"])
        remote = cli.call("ls", "--remote", "--region", cli.region).data["remote"]
        listed = {row["microvmId"]: row for row in remote["microvms"]}
        plane = aws.client(SERVICE)
        actual = plane.get_microvm(microvmIdentifier=deleted_vm).get("startedAt")
        timestamp = actual.timestamp() if hasattr(actual, "timestamp") else actual
        results.eq(TIMESTAMP, listed.get(deleted_vm, {}).get("startedAt"), timestamp)
        args = ("gc", "--prefix", prefix, "--keep", kept_image, "--region", cli.region)
        dry = cli.call(*args).data
        # Check the dry run against independent service reads before permitting cleanup.
        alive_after_dry = plane.get_microvm(microvmIdentifier=deleted_vm).get("state")
        results.check(
            "prefix sweep dry-run leaves its candidate alive (#675)",
            alive_after_dry == "RUNNING"
            and image_state(plane, deleted_image) in READY_IMAGE_STATES,
            "candidate VM and image still exist",
        )
        cleaned = cli.call(*args, "--yes", timeout=15 * 60).data
        check_reports(dry, cleaned, deleted_image, kept_image, deleted_vm, results)
        outside_vm = str(outside.data["microvmId"])
        outside_image = str(outside.data["imageIdentifier"])
        results.check(
            PRESERVED,
            plane.get_microvm(microvmIdentifier=kept_vm).get("state") == "RUNNING"
            and plane.get_microvm(microvmIdentifier=outside_vm).get("state")
            == "RUNNING"
            and image_state(plane, kept_image) in READY_IMAGE_STATES
            and image_state(plane, outside_image) in READY_IMAGE_STATES,
            "kept and outside-prefix VMs and images remain usable",
        )
        results.check(
            "prefix sweep deletion agrees with independent service reads (#675)",
            plane.get_microvm(microvmIdentifier=deleted_vm).get("state") == "TERMINATED"
            and image_state(plane, deleted_image) in {None, "DELETED"},
            "deleted VM and image states",
        )
