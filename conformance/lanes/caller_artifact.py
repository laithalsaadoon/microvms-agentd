# SPDX-License-Identifier: Apache-2.0
"""`build --artifact-uri` with the suite's bucket set (#249): the caller's object keeps its
bytes, and the image, the object and the build log group are each deleted afterward."""

from __future__ import annotations

import contextlib
import os
import secrets
import subprocess
import time
from collections.abc import Callable
from typing import Any

from harness.cli import Cli
from harness.constants import BASELINE_MEMORY_MIB, SERVICE
from harness.envelope import Envelope, EnvelopeError, KindError
from harness.redact import command_for_log
from harness.results import Results, section_failure_detail

#: What `drive_caller_artifact` names its image and its S3 key prefix, with a fresh nonce per
#: run. Under `microvm-cli`, one of `scripts/verify-clean.py`'s prefixes, so a leak this
#: section's own cleanup misses still shows in `live:verify-clean`.
CALLER_ARTIFACT_PREFIX = "microvm-cli-caller-artifact"
CALLER_ARTIFACT_UNTOUCHED = (
    "build --artifact-uri with a bucket set leaves the caller's S3 object untouched "
    "(issue #249)"
)
CALLER_ARTIFACT_BUILT_FROM = (
    "the caller-artifact image was built from the caller's URI (issue #249)"
)


CALLER_ARTIFACT_BUCKET_SET = (
    "the caller-artifact build ran with the suite's bucket set and said it went unused "
    "(issue #249)"
)
CALLER_ARTIFACT_IMAGE_GONE = "the caller-artifact image is deleted"
CALLER_ARTIFACT_OBJECT_GONE = "the caller-artifact S3 object is deleted"
CALLER_ARTIFACT_GROUP_GONE = "the caller-artifact build log group is deleted"


def caller_artifact_checks(report: dict[str, Any], results: Results) -> None:
    """The three #249 checks, read off `drive_caller_artifact`'s report.

    Separate from the driver so the self-test can feed it reports and show each check fails
    on its gap. Every read is defensive: a malformed report, or one a seeded fault leaves
    half-filled, gets a wrong answer and a FAIL line rather than a `KeyError` that ends the
    self-test before it prints anything.

    The sentinel is metadata the section wrote with its copy. A re-upload of identical bytes
    keeps the ETag, but `aws s3 cp` never carries custom metadata over, so a missing sentinel
    is an overwrite even when the bytes match. `present` is what stops an empty report from
    passing: without it every `.get` reads `None` on both sides and the comparisons agree.

    `saidUnused` is what makes the section about #249 at all. Without a bucket in effect the
    unfixed CLI never uploaded either, so an untouched object proves nothing unless the build
    itself said it had a bucket and left it unused.
    """
    before = report.get("before") or {}
    after = report.get("after") or {}
    sentinel = report.get("sentinel")
    present = bool(before) and bool(after) and bool(sentinel)
    marked = (after.get("Metadata") or {}).get("conformance-sentinel") == sentinel
    untouched = (
        present
        and marked
        and before.get("ETag") == after.get("ETag")
        and before.get("LastModified") == after.get("LastModified")
    )
    results.check(
        CALLER_ARTIFACT_UNTOUCHED,
        untouched,
        f"ETag {before.get('ETag')!r} -> {after.get('ETag')!r}, LastModified "
        f"{before.get('LastModified')!r} -> {after.get('LastModified')!r}, sentinel "
        f"{sentinel!r} read back as "
        f"{(after.get('Metadata') or {}).get('conformance-sentinel')!r}"
        + (
            f", build error: {report['buildError']}" if report.get("buildError") else ""
        ),
    )
    results.eq(CALLER_ARTIFACT_BUILT_FROM, report.get("versionUri"), report.get("uri"))
    results.check(
        CALLER_ARTIFACT_BUCKET_SET,
        report.get("saidUnused") is True,
        f"bucket {report.get('bucket')!r}, unused-bucket line on stderr: "
        f"{report.get('saidUnused')!r}",
    )


def caller_artifact_images(plane: Any, name: str) -> list[dict[str, Any]]:
    """Every image named exactly `name`, across every page of the listing."""
    return [
        item
        for page in plane.get_paginator("list_microvm_images").paginate()
        for item in page.get("items", [])
        if item.get("name") == name
    ]


def drive_caller_artifact(
    cli: Cli,
    launched: Envelope,
    aws: Any,
    results: Results,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """`build --artifact-uri` with a bucket set leaves the caller's object alone (#249).

    The suite exports `MICROVM_BUCKET` for every call, which is the shell the issue is about:
    the bucket is set without any `--bucket` flag. The caller's object is a copy of the
    artifact the suite's own `run` uploaded, written under a key of this section's with a
    sentinel in its metadata. The build passes no `--dockerfile`, so the CLI's own artifact
    would differ from the copy (the suite's run used the conformance Dockerfile), and an
    overwrite changes the ETag as well as dropping the sentinel. It passes no binary either,
    since the caller's object holds the daemon and the CLI provisions none beside it.

    The build runs without `--quiet`, the one call in the suite that does, because its
    stderr is the evidence the bucket was in effect: the fixed CLI prints one line naming the
    bucket it left unused. Only that fact is kept, never the stream.

    A build that fails is still read: the upload this issue is about happens before the
    create call, so what S3 holds afterward answers the question either way. Cleanup takes
    the ARN when the build returned one, and the name and nonce chosen here otherwise, so a
    build that created its image and then failed still has its image and log group deleted.
    """
    print("\n== build --artifact-uri with a bucket set (issue #249) ==")
    s3 = aws.client("s3")
    plane = aws.client(SERVICE)
    bucket = os.environ["MICROVM_BUCKET"]
    nonce = secrets.token_hex(4)
    name = f"{CALLER_ARTIFACT_PREFIX}-{nonce}"
    key = f"{CALLER_ARTIFACT_PREFIX}/{nonce}/artifact.zip"
    uri = f"s3://{bucket}/{key}"
    report: dict[str, Any] = {"uri": uri, "sentinel": nonce, "bucket": bucket}
    arn = None
    try:
        s3.copy_object(
            Bucket=bucket,
            Key=key,
            CopySource={"Bucket": bucket, "Key": f"{launched.data['imageName']}.zip"},
            Metadata={"conformance-sentinel": nonce},
            MetadataDirective="REPLACE",
        )
        report["before"] = s3.head_object(Bucket=bucket, Key=key)
        argv = [
            str(cli.binary),
            "--json",
            "build",
            "--artifact-uri",
            uri,
            "--name",
            name,
            "--memory",
            str(BASELINE_MEMORY_MIB),
            "--region",
            cli.region,
        ]
        cli.log.append(command_for_log(argv))
        try:
            proc = cli.run_process(argv, 50 * 60)
            report["saidUnused"] = (
                f"the bucket {bucket} is unused for this build" in proc.stderr
            )
            built = cli.parse_stdout(proc.stdout, argv)
            if built.status == "error":
                raise KindError(built)
            arn = built.data.get("imageIdentifier")
        except (KindError, EnvelopeError, subprocess.TimeoutExpired) as exc:
            report["buildError"] = section_failure_detail(exc)
        with contextlib.suppress(Exception):
            report["after"] = s3.head_object(Bucket=bucket, Key=key)
        if not arn:
            with contextlib.suppress(Exception):
                arn = next(iter(caller_artifact_images(plane, name)), {}).get(
                    "imageArn"
                )
        if arn:
            # `GetMicrovmImage` carries no artifact; the version does.
            with contextlib.suppress(Exception):
                version = plane.get_microvm_image(imageIdentifier=arn).get(
                    "latestActiveImageVersion"
                )
                if version:
                    described = plane.get_microvm_image_version(
                        imageIdentifier=arn, imageVersion=version
                    )
                    report["versionUri"] = (described.get("codeArtifact") or {}).get(
                        "uri"
                    )
        caller_artifact_checks(report, results)
    finally:
        caller_artifact_cleanup(name, arn, bucket, nonce, aws, results, sleep, clock)


def caller_artifact_image_state(plane: Any, arn: str | None) -> str | None:
    """The state `GetMicrovmImage` reports for `arn`, or None once it's gone (or never was).

    Only `ResourceNotFoundException` reads as gone, as in `ensure_image_cleanup`. Any other
    error is returned as its class name, so a call that can't answer never passes for one
    that said the image is deleted.
    """
    if not arn:
        return None
    try:
        return str(plane.get_microvm_image(imageIdentifier=arn).get("state"))
    except Exception as exc:  # noqa: BLE001 - the error class is the finding
        if type(exc).__name__ == "ResourceNotFoundException":
            return None
        return type(exc).__name__


def caller_artifact_cleanup(
    name: str,
    arn: str | None,
    bucket: str,
    nonce: str,
    aws: Any,
    results: Results,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Deletes what `drive_caller_artifact` created, found by its ARN and by its name.

    The image is listed by its exact name as well as read by the ARN the build returned, and
    the delete is retried until both say it's gone: an image still `CREATING` (a timed-out
    build leaves one) refuses deletion, so a single attempt could report a leak as cleaned.
    The name covers a build that failed before it returned an ARN. The ARN is what stops a
    listing that reads nothing (a changed page shape, a name that no longer matches) from
    passing for a deleted image when the build said one exists.
    """
    plane = aws.client(SERVICE)
    s3 = aws.client("s3")
    logs = aws.client("logs")

    seen: list[str] = [arn] if arn else []
    last: dict[str, str] = {}
    remaining: list[dict[str, Any]] | None = None
    by_arn: str | None = None
    deadline = clock() + 10 * 60
    while True:
        try:
            remaining = caller_artifact_images(plane, name)
        except Exception as exc:  # noqa: BLE001 - the error class is the finding
            last["listing"] = type(exc).__name__
            remaining = None
            break
        for image in remaining:
            listed = str(image.get("imageArn"))
            if listed not in seen:
                seen.append(listed)
            last[listed] = str(image.get("state"))
            if image.get("state") != "DELETING":
                with contextlib.suppress(Exception):
                    plane.delete_microvm_image(imageIdentifier=listed)
        by_arn = caller_artifact_image_state(plane, arn)
        if by_arn is not None and arn:
            last[f"{arn} (by ARN)"] = by_arn
            if by_arn != "DELETING":
                with contextlib.suppress(Exception):
                    plane.delete_microvm_image(imageIdentifier=arn)
            by_arn = caller_artifact_image_state(plane, arn)
        if (not remaining and by_arn is None) or clock() > deadline:
            break
        sleep(15)
    results.check(
        CALLER_ARTIFACT_IMAGE_GONE,
        remaining == [] and by_arn is None,
        f"name={name!r} found={seen!r} last={last!r}",
    )

    prefix = f"{CALLER_ARTIFACT_PREFIX}/{nonce}/"
    try:
        listed_keys = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
        keys = [item["Key"] for item in listed_keys.get("Contents") or []]
        if keys:
            s3.delete_objects(
                Bucket=bucket, Delete={"Objects": [{"Key": key} for key in keys]}
            )
        left = s3.list_objects_v2(Bucket=bucket, Prefix=prefix).get("KeyCount", 0)
        results.check(
            CALLER_ARTIFACT_OBJECT_GONE,
            left == 0,
            f"deleted={keys!r} remaining={left}",
        )
    except Exception as exc:  # noqa: BLE001 - the error class is the finding
        results.check(CALLER_ARTIFACT_OBJECT_GONE, False, type(exc).__name__)

    group = f"/aws/lambda-microvms/{name}"
    try:
        with contextlib.suppress(Exception):
            logs.delete_log_group(logGroupName=group)
        groups = (
            logs.describe_log_groups(logGroupNamePrefix=group).get("logGroups") or []
        )
        results.check(
            CALLER_ARTIFACT_GROUP_GONE,
            not [g for g in groups if g.get("logGroupName") == group],
            f"group={group!r} remaining={[g.get('logGroupName') for g in groups]!r}",
        )
    except Exception as exc:  # noqa: BLE001 - the error class is the finding
        results.check(CALLER_ARTIFACT_GROUP_GONE, False, type(exc).__name__)
