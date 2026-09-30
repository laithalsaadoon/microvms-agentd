# SPDX-License-Identifier: Apache-2.0
"""The ensure-image checks against complete and gapped reports, and the section's cleanup
against a fake account, offline."""

from __future__ import annotations

from typing import Any

from harness.results import Results
from lanes.ensure_image import (
    ENSURE_GROUP_GONE,
    ENSURE_IMAGE_GONE,
    ENSURE_OBJECTS_GONE,
    ENSURE_VM_TERMINATED,
    SERVICE_LOG_PREFIX,
    ensure_image_checks,
    ensure_image_cleanup,
)


class ResourceNotFoundException(Exception):
    """The fake plane's answer for a deleted image, named as boto3 names the real one."""


class FakeEnsureImageAws:
    """The MicroVM plane, S3 and CloudWatch Logs in one dict each, for the cleanup's self-test.

    Each flag is one way an account can mislead the cleanup: `blind` makes the image
    listing return no items, `listing_raises` makes it fail, `undeletable` makes every image
    delete a no-op, `stuck_vms` makes every terminate a no-op, and `stuck_groups` makes every
    log group delete a no-op. `refusals` is how many deletes an image refuses before it
    takes one, as an image still `CREATING` does.
    """

    def __init__(
        self,
        *,
        blind: bool = False,
        listing_raises: bool = False,
        undeletable: bool = False,
        stuck_vms: bool = False,
        stuck_groups: bool = False,
        refusals: int = 0,
    ) -> None:
        self.blind = blind
        self.listing_raises = listing_raises
        self.undeletable = undeletable
        self.stuck_vms = stuck_vms
        self.stuck_groups = stuck_groups
        self.refusals = refusals
        self.images: dict[str, dict[str, Any]] = {}
        self.vms: dict[str, dict[str, Any]] = {}
        self.objects: set[str] = set()
        self.groups: set[str] = set()

    def client(self, _service: str) -> FakeEnsureImageAws:
        return self

    def get_paginator(self, operation: str) -> FakeEnsureImagePages:
        return FakeEnsureImagePages(self, operation)

    # the MicroVM plane
    def get_microvm_image(self, *, imageIdentifier: str) -> dict[str, Any]:
        if imageIdentifier not in self.images:
            raise ResourceNotFoundException(imageIdentifier)
        return dict(self.images[imageIdentifier])

    def delete_microvm_image(self, *, imageIdentifier: str) -> None:
        if self.refusals:
            self.refusals -= 1
            raise RuntimeError("ConflictException: the image is still CREATING")
        if not self.undeletable:
            self.images.pop(imageIdentifier, None)

    def get_microvm(self, *, microvmIdentifier: str) -> dict[str, Any]:
        return dict(self.vms[microvmIdentifier])

    def terminate_microvm(self, *, microvmIdentifier: str) -> None:
        if not self.stuck_vms:
            self.vms[microvmIdentifier]["state"] = "TERMINATED"

    # S3
    def list_objects_v2(self, *, Bucket: str, Prefix: str) -> dict[str, Any]:
        keys = [key for key in self.objects if key.startswith(Prefix)]
        return {"Contents": [{"Key": key} for key in keys], "KeyCount": len(keys)}

    def delete_objects(self, *, Bucket: str, Delete: dict[str, Any]) -> None:
        for item in Delete["Objects"]:
            self.objects.discard(item["Key"])

    # CloudWatch Logs
    def delete_log_group(self, *, logGroupName: str) -> None:
        if not self.stuck_groups:
            self.groups.discard(logGroupName)

    def describe_log_groups(self, *, logGroupNamePrefix: str) -> dict[str, Any]:
        groups = [
            group for group in self.groups if group.startswith(logGroupNamePrefix)
        ]
        return {"logGroups": [{"logGroupName": group} for group in groups]}

    def leftovers(self, prefix: str) -> list[str]:
        """What of the run the account still holds, a VM only while it isn't TERMINATED."""
        return sorted(
            [f"image {arn}" for arn in self.images if f":{prefix}-" in arn]
            + [
                f"vm {vm}"
                for vm, it in self.vms.items()
                if f":{prefix}-" in it["imageArn"] and it["state"] != "TERMINATED"
            ]
            + [f"object {key}" for key in self.objects]
            + [f"group {group}" for group in self.groups if f"/{prefix}-" in group]
        )


class FakeEnsureImagePages:
    """One paginator of `FakeEnsureImageAws`, serving a single page of its operation."""

    def __init__(self, aws: FakeEnsureImageAws, operation: str) -> None:
        self.aws = aws
        self.operation = operation

    def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
        if self.operation == "list_microvm_images":
            if self.aws.listing_raises:
                raise RuntimeError("scripted listing failure")
            images = [] if self.aws.blind else list(self.aws.images.values())
            return [{"items": [dict(image) for image in images]}]
        if self.operation == "list_microvms":
            return [{"items": [dict(vm) for vm in self.aws.vms.values()]}]
        if self.operation == "describe_log_groups":
            return [self.aws.describe_log_groups(**kwargs)]
        raise AssertionError(f"the fake account has no paginator for {self.operation}")


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
            "longestStallMs": 14,
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
        len(probe.passed) == 10 and not probe.failed,
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
        "the ensure race never held": {
            **complete,
            "race": {**complete["race"], "longestStallMs": 1100},
        },
        "the ensure race never held the caller's runtime": {
            **complete,
            "race": {
                key: value
                for key, value in complete["race"].items()
                if key != "longestStallMs"
            },
        },
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
    check_ensure_image_cleanup(results)


def check_ensure_image_cleanup(results: "Results") -> None:
    """The cleanup against accounts that hold what a run can leave, whatever its report says.

    The report can be empty: the test writes what it got before it failed, and a timeout
    leaves no report at all. Each scenario runs `ensure_image_cleanup` whole and names the
    checks it must fail. A scenario that fails nothing must also leave nothing of the run in
    the account, so a cleanup that stops deleting what it finds can't pass by reading back
    only what it deleted.
    """
    prefix, bucket, key_prefix = "conformance-ensure-abcd1234", "bucket", "p/abcd1234"
    name = f"{prefix}-0123456789ab"
    arn = f"arn:aws:lambda:us-east-1:123456789012:microvm-image:{name}"
    group = f"{SERVICE_LOG_PREFIX}{name}"
    other = "conformance-ensure-abcd12345-0123456789ab"
    other_arn = f"arn:aws:lambda:us-east-1:123456789012:microvm-image:{other}"
    every = [
        ENSURE_VM_TERMINATED,
        ENSURE_IMAGE_GONE,
        ENSURE_OBJECTS_GONE,
        ENSURE_GROUP_GONE,
    ]
    complete = {"name": name, "arn": arn, "vm": {"id": "vm-1"}}

    def account(*, cleaned: bool = False, **flags: Any) -> FakeEnsureImageAws:
        """An account holding everything one run creates, the VM still running, or, when
        the test `cleaned` up after itself, the objects and the log group it leaves."""
        aws = FakeEnsureImageAws(**flags)
        if not cleaned:
            aws.images[arn] = {"imageArn": arn, "name": name, "state": "CREATED"}
        aws.vms["vm-1"] = {
            "microvmId": "vm-1",
            "imageArn": arn,
            "state": "TERMINATED" if cleaned else "RUNNING",
        }
        aws.objects.add(f"{key_prefix}/{name}/artifact.zip")
        aws.groups.add(group)
        # Another run's image, its VM and its group, which cleanup must leave alone: the
        # name starts with this run's prefix, but not with the prefix and a hyphen.
        aws.images[other_arn] = {
            "imageArn": other_arn,
            "name": other,
            "state": "CREATED",
        }
        aws.vms["vm-2"] = {
            "microvmId": "vm-2",
            "imageArn": other_arn,
            "state": "RUNNING",
        }
        aws.groups.add(f"{SERVICE_LOG_PREFIX}{other}")
        return aws

    scenarios: dict[str, tuple[dict[str, Any], FakeEnsureImageAws, list[str]]] = {
        "a complete report after the test's own cleanup": (
            complete,
            account(cleaned=True),
            [],
        ),
        "a complete report and everything left": (complete, account(), []),
        "an empty report": ({}, account(), []),
        "an empty report and an image that won't delete": (
            {},
            account(undeletable=True),
            [ENSURE_IMAGE_GONE],
        ),
        "an empty report and a hashed log group that won't delete": (
            {},
            account(stuck_groups=True),
            [ENSURE_GROUP_GONE],
        ),
        "an empty report and a VM that won't terminate": (
            {},
            account(stuck_vms=True),
            [ENSURE_VM_TERMINATED],
        ),
        "an image still CREATING that deletes on a later try": (
            {},
            account(refusals=2),
            [],
        ),
        "a listing that reads nothing": (complete, account(blind=True), []),
        "a hidden image that won't delete": (
            complete,
            account(blind=True, undeletable=True),
            [ENSURE_IMAGE_GONE],
        ),
        "a listing that raises": (
            complete,
            account(listing_raises=True),
            [ENSURE_IMAGE_GONE],
        ),
    }
    wrong: list[str] = []
    for scenario, (report, aws, expected) in scenarios.items():
        probe = Results(probe=True)
        # Each sleep moves this clock instead of the process's, so a scenario whose image
        # never goes runs the ten-minute poll to its deadline in no time.
        elapsed = [0.0]
        try:
            ensure_image_cleanup(
                report,
                prefix,
                bucket,
                key_prefix,
                aws,
                probe,
                sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
                clock=lambda: elapsed[0],
            )
        except Exception as exc:  # noqa: BLE001 - a raise is the finding
            wrong.append(f"{scenario}: raised {type(exc).__name__}: {exc}")
            continue
        failed = sorted(check for check, _ in probe.failed)
        ran = sorted({*probe.passed, *failed})
        left = aws.leftovers(prefix) if not expected else []
        kept = (
            other_arn in aws.images
            and aws.vms["vm-2"]["state"] == "RUNNING"
            and f"{SERVICE_LOG_PREFIX}{other}" in aws.groups
        )
        if failed != sorted(expected) or ran != sorted(every) or left or not kept:
            wrong.append(
                f"{scenario}: failed {failed!r}, ran {ran!r}, left {left!r}, "
                f"another run's image, VM and group kept: {kept}"
            )
    results.check(
        "the ensure-image cleanup fails exactly where each account misleads it",
        not wrong,
        "; ".join(wrong),
    )
