# SPDX-License-Identifier: Apache-2.0
"""The caller-artifact checks and their driver, against a fake account."""

from __future__ import annotations

import json
import os
import subprocess
import unittest.mock
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from harness.cli import Cli
from harness.constants import REGION
from harness.envelope import Envelope
from harness.results import Results
from lanes.caller_artifact import (
    CALLER_ARTIFACT_BUCKET_SET,
    CALLER_ARTIFACT_BUILT_FROM,
    CALLER_ARTIFACT_GROUP_GONE,
    CALLER_ARTIFACT_IMAGE_GONE,
    CALLER_ARTIFACT_OBJECT_GONE,
    CALLER_ARTIFACT_UNTOUCHED,
    caller_artifact_checks,
    drive_caller_artifact,
)


class ResourceNotFoundException(Exception):
    """The fake plane's answer for a deleted image, named as boto3 names the real one."""


class FakeCallerArtifactAws:
    """S3, the MicroVM plane and CloudWatch Logs in one dict each, for the driver's self-test.

    `cli` says which CLI the fake `build` plays: `fixed` builds from the caller's URI, says
    the bucket went unused when `MICROVM_BUCKET` is in its environment, and uploads nothing;
    `unfixed` is main before #249, which overwrote the object and said nothing. `blind` makes
    the listing return no items, `undeletable` makes every image delete a no-op, and
    `listing_raises` makes the paginator fail, which are the three ways cleanup can be misled.
    """

    def __init__(
        self,
        *,
        cli: str = "fixed",
        blind: bool = False,
        undeletable: bool = False,
        listing_raises: bool = False,
    ) -> None:
        self.cli = cli
        self.blind = blind
        self.undeletable = undeletable
        self.listing_raises = listing_raises
        self.objects: dict[str, dict[str, Any]] = {}
        self.images: dict[str, dict[str, Any]] = {}
        self.groups: set[str] = set()
        self.stamp = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
        self.exceptions = type(
            "Exceptions", (), {"ResourceNotFoundException": ResourceNotFoundException}
        )

    def client(self, _service: str) -> FakeCallerArtifactAws:
        return self

    # S3
    def copy_object(self, **kwargs: Any) -> None:
        self.objects[kwargs["Key"]] = {
            "ETag": '"copy"',
            "LastModified": self.stamp,
            "Metadata": dict(kwargs.get("Metadata") or {}),
        }

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        return dict(self.objects[Key])

    def list_objects_v2(self, *, Bucket: str, Prefix: str) -> dict[str, Any]:
        keys = [key for key in self.objects if key.startswith(Prefix)]
        return {"Contents": [{"Key": key} for key in keys], "KeyCount": len(keys)}

    def delete_objects(self, *, Bucket: str, Delete: dict[str, Any]) -> None:
        for item in Delete["Objects"]:
            self.objects.pop(item["Key"], None)

    # the MicroVM plane
    def get_paginator(self, _operation: str) -> FakeCallerArtifactAws:
        return self

    def paginate(self) -> list[dict[str, Any]]:
        if self.listing_raises:
            raise RuntimeError("scripted listing failure")
        items = [] if self.blind else [dict(image) for image in self.images.values()]
        return [{"items": items}]

    def get_microvm_image(self, *, imageIdentifier: str) -> dict[str, Any]:
        if imageIdentifier not in self.images:
            raise ResourceNotFoundException(imageIdentifier)
        return {**self.images[imageIdentifier], "latestActiveImageVersion": "1"}

    def get_microvm_image_version(self, **kwargs: Any) -> dict[str, Any]:
        return {"codeArtifact": {"uri": self.images[kwargs["imageIdentifier"]]["uri"]}}

    def delete_microvm_image(self, *, imageIdentifier: str) -> None:
        if not self.undeletable:
            self.images.pop(imageIdentifier, None)

    # CloudWatch Logs
    def delete_log_group(self, *, logGroupName: str) -> None:
        self.groups.discard(logGroupName)

    def describe_log_groups(self, *, logGroupNamePrefix: str) -> dict[str, Any]:
        return {
            "logGroups": [
                {"logGroupName": group}
                for group in self.groups
                if group.startswith(logGroupNamePrefix)
            ]
        }

    # the `microvm` binary
    def build(
        self, argv: list[str], env: dict[str, str] | None
    ) -> subprocess.CompletedProcess[str]:
        effective = os.environ if env is None else env
        uri = argv[argv.index("--artifact-uri") + 1]
        name = argv[argv.index("--name") + 1]
        key = uri.split("/", 3)[3]
        stderr = f"building image {name} (2 GB)\n"
        if self.cli == "fixed":
            bucket = effective.get("MICROVM_BUCKET")
            if bucket:
                stderr += (
                    "--artifact-uri names the artifact, so nothing is uploaded and the "
                    f"bucket {bucket} is unused for this build\n"
                )
        else:
            self.objects[key] = {
                "ETag": '"cli"',
                "LastModified": self.stamp + timedelta(seconds=5),
                "Metadata": {},
            }
        arn = f"arn:aws:lambda:us-east-1:123456789012:microvm-image:{name}"
        self.images[arn] = {
            "imageArn": arn,
            "name": name,
            "state": "CREATED",
            "uri": uri,
        }
        self.groups.add(f"/aws/lambda-microvms/{name}")
        envelope = {
            "status": "ok",
            "apiVersion": "1",
            "type": "microvm.build",
            "data": {"imageIdentifier": arn, "imageName": name},
        }
        return subprocess.CompletedProcess(argv, 0, json.dumps(envelope), stderr)


@dataclass
class FakeCallerArtifactCli:
    """`Cli`'s surface the driver reads, with `run_process` answered by the fake AWS."""

    aws: FakeCallerArtifactAws
    binary: Path = Path("microvm")
    region: str = REGION
    log: list[str] = field(default_factory=list)
    parse_stdout = staticmethod(Cli.parse_stdout)

    def run_process(
        self, argv: list[str], timeout: float, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return self.aws.build(argv, env)


def check_caller_artifact_section(results: "Results") -> None:
    """The caller-artifact checks pass on a complete report and each fails on its gap."""
    uri = "s3://bucket/microvm-cli-caller-artifact/abcd1234/artifact.zip"
    stamp = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    head = {
        "ETag": '"0123abcd"',
        "LastModified": stamp,
        "Metadata": {"conformance-sentinel": "abcd1234"},
    }
    complete: dict[str, Any] = {
        "uri": uri,
        "sentinel": "abcd1234",
        "bucket": "bucket",
        "before": head,
        "after": dict(head),
        "versionUri": uri,
        "saidUnused": True,
    }

    def failures(report: dict[str, Any]) -> list[str]:
        # A raise counts as every check failing, named by its type, so a crash inside the
        # checks ends in this section's FAIL line and never in a traceback.
        probe = Results(probe=True)
        try:
            caller_artifact_checks(report, probe)
        except Exception as exc:  # noqa: BLE001 - a raise is the finding
            return [f"raised {type(exc).__name__}"]
        return [name for name, _ in probe.failed]

    clean = failures(complete)
    results.check(
        "the caller-artifact checks pass on a complete report", not clean, repr(clean)
    )
    untouched, built_from = CALLER_ARTIFACT_UNTOUCHED, CALLER_ARTIFACT_BUILT_FROM
    bucket_set = CALLER_ARTIFACT_BUCKET_SET
    gaps: dict[str, tuple[dict[str, Any], str]] = {
        "ETag changed": ({**complete, "after": {**head, "ETag": '"ffff"'}}, untouched),
        "LastModified changed": (
            {
                **complete,
                "after": {**head, "LastModified": stamp + timedelta(seconds=1)},
            },
            untouched,
        ),
        "sentinel missing": (
            {**complete, "after": {**head, "Metadata": {}}},
            untouched,
        ),
        "no Metadata key": (
            {**complete, "after": {"ETag": head["ETag"], "LastModified": stamp}},
            untouched,
        ),
        "versionUri different": (
            {**complete, "versionUri": "s3://other/x.zip"},
            built_from,
        ),
        "versionUri absent": (
            {key: value for key, value in complete.items() if key != "versionUri"},
            built_from,
        ),
        "no unused-bucket line": ({**complete, "saidUnused": False}, bucket_set),
        "stderr never read": (
            {key: value for key, value in complete.items() if key != "saidUnused"},
            bucket_set,
        ),
    }
    missed = [
        f"{gap}: {failed!r}"
        for gap, (report, expected) in gaps.items()
        if (failed := failures(report)) != [expected]
    ]
    results.check(
        "each caller-artifact check fails on the one gap it is about",
        not missed,
        "; ".join(missed),
    )
    empties: list[dict[str, Any]] = [{}, {"before": None, "after": None}, {"after": {}}]
    unfailed = [
        f"{report!r}: {failed!r}"
        for report in empties
        if sorted(failed := failures(report))
        != sorted([untouched, built_from, bucket_set])
    ]
    results.check(
        "the caller-artifact checks fail on an empty report",
        not unfailed,
        "; ".join(unfailed),
    )
    check_caller_artifact_driver(results)


def check_caller_artifact_driver(results: "Results") -> None:
    """The driver and its cleanup, end to end against `FakeCallerArtifactAws`.

    The report checks above can't see the driver: a build run with `MICROVM_BUCKET` dropped
    from its environment, or a cleanup that trusts an empty listing, would still leave them
    green. Each scenario here runs `drive_caller_artifact` whole and names the checks it must
    fail, none for the fixed CLI on a well-behaved account.
    """
    launched = Envelope(
        status="ok", api_version="1", type="microvm.run", data={"imageName": "suite"}
    )
    every = [
        CALLER_ARTIFACT_UNTOUCHED,
        CALLER_ARTIFACT_BUILT_FROM,
        CALLER_ARTIFACT_BUCKET_SET,
        CALLER_ARTIFACT_IMAGE_GONE,
        CALLER_ARTIFACT_OBJECT_GONE,
        CALLER_ARTIFACT_GROUP_GONE,
    ]
    scenarios: dict[str, tuple[FakeCallerArtifactAws, list[str]]] = {
        "the fixed CLI": (FakeCallerArtifactAws(), []),
        "the CLI before #249": (
            FakeCallerArtifactAws(cli="unfixed"),
            [CALLER_ARTIFACT_UNTOUCHED, CALLER_ARTIFACT_BUCKET_SET],
        ),
        "a listing that reads nothing": (FakeCallerArtifactAws(blind=True), []),
        "a hidden image that won't delete": (
            FakeCallerArtifactAws(blind=True, undeletable=True),
            [CALLER_ARTIFACT_IMAGE_GONE],
        ),
        "a listing that raises": (
            FakeCallerArtifactAws(listing_raises=True),
            [CALLER_ARTIFACT_IMAGE_GONE],
        ),
    }
    wrong: list[str] = []
    with unittest.mock.patch.dict(os.environ, {"MICROVM_BUCKET": "bucket"}):
        for scenario, (aws, expected) in scenarios.items():
            probe = Results(probe=True)
            # Each sleep moves this clock instead of the process's, so the scenario whose
            # image never goes runs the ten-minute poll to its deadline in no time.
            elapsed = [0.0]
            try:
                drive_caller_artifact(
                    FakeCallerArtifactCli(aws),
                    launched,
                    aws,
                    probe,
                    sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
                    clock=lambda: elapsed[0],
                )
            except Exception as exc:  # noqa: BLE001 - a raise is the finding
                wrong.append(f"{scenario}: raised {type(exc).__name__}: {exc}")
                continue
            failed = sorted(name for name, _ in probe.failed)
            ran = sorted({*probe.passed, *failed})
            if failed != sorted(expected) or ran != sorted(every):
                wrong.append(f"{scenario}: failed {failed!r}, ran {ran!r}")
    results.check(
        "the caller-artifact driver and cleanup fail exactly where each account misleads them",
        not wrong,
        "; ".join(wrong),
    )
