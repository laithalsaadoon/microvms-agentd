# SPDX-License-Identifier: Apache-2.0
"""`Sandbox::ensure_image` against AWS through its ignored Rust live test, and this
section's own cleanup, read back through boto3. `--only ensure_image` runs it alone."""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from harness.constants import REPO, SERVICE
from harness.results import Results

#: What `drive_ensure_image` names the image it ensures: a fresh nonce per run, so the
#: first call of the run builds rather than reusing an image an earlier run left behind.
ENSURE_PREFIX = "conformance-ensure"

#: The ready spellings `Image.is_ready` accepts.
READY_IMAGE_STATES = ("CREATED", "UPDATED", "ACTIVE", "AVAILABLE")

#: The cleanup's four checks, named once so the self-test's scenarios can say which fail.
ENSURE_VM_TERMINATED = "the ensure-image VM is terminated"
ENSURE_IMAGE_GONE = "the ensured image is deleted"
ENSURE_OBJECTS_GONE = "the ensure-image artifacts are deleted from S3"
ENSURE_GROUP_GONE = "the ensure-image build log group is deleted"

#: Where the service writes an image's build log: `<prefix><image name>`.
SERVICE_LOG_PREFIX = "/aws/lambda-microvms/"

#: How long cleanup keeps at the run's VMs, and then at its images, before it reports what's
#: left: an image still `CREATING` refuses deletion, and a VM takes a while to terminate.
CLEANUP_SECONDS = 10 * 60
CLEANUP_POLL_SECONDS = 15

#: The name in an image ARN, written `microvm-image:<name>` or `microvm-image/<name>`, before
#: any version suffix.
IMAGE_ARN_NAME = re.compile(r"microvm-image[:/]([^:/]+)")

#: The race's runtime-stall check (#309). The live test runs on one worker, and a probe task
#: beside the two ensures records the longest time between two of its 10 ms wakes: an ensure
#: that hashed and zipped inline held the worker for about a second, and the instance-metadata
#: credential provider gives a fetch one second. Half that leaves room for the S3 signature's
#: inline payload hash and a loaded host.
ENSURE_RACE_UNSTALLED = (
    "the ensure race never held the caller's runtime for half a second (#309)"
)
STALL_LIMIT_MS = 500


def ensure_image_checks(
    report: dict[str, Any], prefix: str, bucket: str, key_prefix: str, results: Results
) -> None:
    """The named IMAGE checks, read off the live test's report.

    Separate from `drive_ensure_image` so the self-test can feed it a report and prove each
    check fails when its evidence is missing, rather than trusting that it would.
    """
    name = str(report.get("name") or "")
    race = report.get("race") or {}
    reuse = report.get("reuse") or {}
    guest = report.get("guest") or {}
    forced = report.get("forced") or {}
    arn = report.get("arn")
    error = report.get("error")
    results.check(
        "IMAGE-6 the ensured image is named by its prefix and twelve hex characters",
        re.fullmatch(rf"{re.escape(prefix)}-[0-9a-f]{{12}}", name) is not None,
        f"name={name!r} error={error!r}",
    )
    identifiers = race.get("identifiers") or []
    states = race.get("states") or []
    results.check(
        "IMAGE-11 two concurrent ensures return one ready image, one built and one joined",
        len(identifiers) == 2
        and identifiers[0] == identifiers[1] == arn
        and len(set(race.get("versions") or [])) == 1
        and all(state in READY_IMAGE_STATES for state in states)
        and sorted(race.get("reused") or []) == [False, True],
        f"reused={race.get('reused')!r} states={states!r} "
        f"versions={race.get('versions')!r} seconds={race.get('seconds')!r} "
        f"error={error!r}",
    )
    stall = race.get("longestStallMs")
    results.check(
        ENSURE_RACE_UNSTALLED,
        type(stall) is int and stall < STALL_LIMIT_MS,
        f"longestStallMs={stall!r} limit={STALL_LIMIT_MS} error={error!r}",
    )
    results.check(
        "IMAGE-4 a task image on a non-managed FROM built under the derived base",
        bool(states) and all(state in READY_IMAGE_STATES for state in states),
        f"states={states!r} error={error!r}",
    )
    results.check(
        "IMAGE-9 a later ensure reuses the image with no upload and no create",
        reuse.get("reused") is True
        and reuse.get("uploaded") is False
        and reuse.get("identifier") == arn,
        f"reuse={reuse!r}",
    )
    uri = f"s3://{bucket}/{key_prefix}/{name}/artifact.zip"
    puts = report.get("puts") or []
    results.check(
        "IMAGE-8 each sandbox resolved its account once and the artifact is at its "
        "content-addressed key",
        report.get("accountCalls") == [1, 1]
        and report.get("artifactUri") == uri
        and bool(puts)
        and all(put == uri for put in puts),
        f"accountCalls={report.get('accountCalls')!r} artifactUri="
        f"{report.get('artifactUri')!r} puts={puts!r}",
    )
    results.check(
        "IMAGE-7 the guest has the context's script and neither the ignored file nor the "
        "symlink",
        guest.get("context") == "context-ok" and guest.get("excluded") == "excluded",
        f"guest={guest!r}",
    )
    results.check(
        "IMAGE-7 the skipped symlink is named in the warnings",
        any("link.sh" in str(warning) for warning in report.get("warnings") or []),
        f"warnings={report.get('warnings')!r}",
    )
    results.check(
        "IMAGE-2 the wrapped image runs the daemon as root after the task's USER",
        guest.get("uid") == "0",
        f"uid={guest.get('uid')!r}",
    )
    results.check(
        "IMAGE-10 a forced ensure deletes the ready image and rebuilds it under its name",
        forced.get("reused") is False
        and forced.get("uploaded") is True
        and forced.get("identifier") == arn
        and forced.get("state") in READY_IMAGE_STATES,
        f"forced={forced!r}",
    )


def drive_ensure_image(binary: Path, aws: Any, results: Results) -> None:
    """`Sandbox::ensure_image` against AWS (#221), through the ignored Rust live test.

    The test (`crates/microvms-core/tests/live_ensure_image.rs`) builds a task image from a task
    directory of its own — a Dockerfile on a non-managed `FROM` ending on `USER nobody`,
    wrapped by `wrap_dockerfile`, with a `.dockerignore`, an ignored file, and a symlink —
    by two sandboxes at once, reuses it from a third call, launches a VM from it, and then
    forces a rebuild under the same name. Its report becomes the named IMAGE checks in
    `ensure_image_checks`.

    Cleanup is this function's and is verified independently of the test's own: every VM
    launched from the run's images is TERMINATED, every image the run's prefix names is
    absent, the S3 objects under the run's key prefix are deleted, and the service-created
    log groups are deleted, each read back through boto3 (`ensure_image_cleanup`).
    """
    nonce = secrets.token_hex(4)
    prefix = f"{ENSURE_PREFIX}-{nonce}"
    key_prefix = f"{ENSURE_PREFIX}/{nonce}"
    bucket = os.environ["MICROVM_BUCKET"]
    with tempfile.TemporaryDirectory() as tmp:
        report_path = Path(tmp) / "report.json"
        env = os.environ.copy()
        env.update(
            {
                "MICROVM_AGENTD_BINARY": str(binary),
                "MICROVM_ENSURE_PREFIX": prefix,
                "MICROVM_ENSURE_KEY_PREFIX": key_prefix,
                "MICROVM_ENSURE_REPORT": str(report_path),
                "AWS_REGION": aws.region_name,
            }
        )
        command = [
            "cargo",
            "test",
            "-p",
            "microvms-core",
            "--test",
            "live_ensure_image",
            "ensure_image_builds_once_reuses_and_rebuilds_under_force",
            "--",
            "--ignored",
            "--exact",
            "--nocapture",
        ]
        started = time.monotonic()
        try:
            run = subprocess.run(
                command,
                cwd=REPO,
                env=env,
                text=True,
                capture_output=True,
                timeout=75 * 60,
                check=False,
            )
            exit_code: int | str = run.returncode
            tail = (run.stderr or "")[-2000:]
        except subprocess.TimeoutExpired:
            exit_code, tail = "timeout after 75 minutes", ""
        seconds = time.monotonic() - started
        try:
            report = json.loads(report_path.read_text())
        except (OSError, ValueError) as exc:
            report = {
                "error": f"no report ({exc}); exit={exit_code} stderr tail: {tail}"
            }
        print(f"ensure_image live test: exit={exit_code} in {seconds:.0f}s", flush=True)
        if exit_code != 0:
            print(tail, file=sys.stderr)
        ensure_image_checks(report, prefix, bucket, key_prefix, results)
        ensure_image_cleanup(report, prefix, bucket, key_prefix, aws, results)


def ensure_image_cleanup(
    report: dict[str, Any],
    prefix: str,
    bucket: str,
    key_prefix: str,
    aws: Any,
    results: Results,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Deletes what the ensure-image run created and reads each deletion back.

    Everything is found by the run's prefix, which carries its nonce, as well as by what the
    report names: a test that fails before it records its image or its VM still leaves them
    (#310), and a report with nothing in it must not read as an account with nothing in it.
    VMs go first, so no VM still runs from an image the next step deletes.
    """
    plane = aws.client(SERVICE)
    ensure_image_vm_cleanup(report, prefix, plane, results, sleep, clock)
    ensure_image_image_cleanup(report, prefix, plane, results, sleep, clock)

    s3 = aws.client("s3")
    try:
        listed = s3.list_objects_v2(Bucket=bucket, Prefix=f"{key_prefix}/")
        keys = [item["Key"] for item in listed.get("Contents") or []]
        if keys:
            s3.delete_objects(
                Bucket=bucket, Delete={"Objects": [{"Key": key} for key in keys]}
            )
        remaining = s3.list_objects_v2(Bucket=bucket, Prefix=f"{key_prefix}/").get(
            "KeyCount", 0
        )
        results.check(
            ENSURE_OBJECTS_GONE,
            remaining == 0,
            f"deleted={keys!r} remaining={remaining}",
        )
    except Exception as exc:  # noqa: BLE001 - the error class is the finding
        results.check(ENSURE_OBJECTS_GONE, False, type(exc).__name__)

    logs = aws.client("logs")
    try:
        found = ensure_image_log_groups(logs, prefix)
        for group in found:
            with contextlib.suppress(Exception):
                logs.delete_log_group(logGroupName=group)
        left = ensure_image_log_groups(logs, prefix)
        results.check(
            ENSURE_GROUP_GONE,
            not left,
            f"prefix={SERVICE_LOG_PREFIX}{prefix} deleted={found!r} remaining={left!r}",
        )
    except Exception as exc:  # noqa: BLE001 - the error class is the finding
        results.check(ENSURE_GROUP_GONE, False, type(exc).__name__)


def run_owns(prefix: str, name: str | None) -> bool:
    """Whether an image name is this run's: the prefix alone, or the prefix and its hash."""
    return bool(name) and (name == prefix or str(name).startswith(f"{prefix}-"))


def image_name_of(arn: str | None) -> str | None:
    """The image name in an image ARN, whichever separator it's written with."""
    match = IMAGE_ARN_NAME.search(arn or "")
    return match.group(1) if match else None


def ensure_image_images(plane: Any, prefix: str) -> list[dict[str, Any]]:
    """Every image this run's prefix names, across every page of the listing."""
    return [
        item
        for page in plane.get_paginator("list_microvm_images").paginate()
        for item in page.get("items", [])
        if run_owns(prefix, item.get("name"))
    ]


def ensure_image_vms(plane: Any, prefix: str) -> dict[str, str]:
    """Every VM launched from one of this run's images, by id, with its listed state."""
    return {
        str(item.get("microvmId")): str(item.get("state"))
        for page in plane.get_paginator("list_microvms").paginate()
        for item in page.get("items", [])
        if run_owns(prefix, image_name_of(item.get("imageArn")))
    }


def ensure_image_log_groups(logs: Any, prefix: str) -> list[str]:
    """Every build log group of this run's images, across every page of the listing."""
    names = [
        str(group.get("logGroupName"))
        for page in logs.get_paginator("describe_log_groups").paginate(
            logGroupNamePrefix=f"{SERVICE_LOG_PREFIX}{prefix}"
        )
        for group in page.get("logGroups", [])
    ]
    return [
        name
        for name in names
        if run_owns(prefix, name.removeprefix(SERVICE_LOG_PREFIX))
    ]


def image_state(plane: Any, arn: str | None) -> str | None:
    """The state `GetMicrovmImage` reports for `arn`, or None once it's gone (or never was).

    Only `ResourceNotFoundException` reads as gone. Any other error is returned as its class
    name, so a call that can't answer never passes for one that said the image is deleted.
    """
    if not arn:
        return None
    try:
        return str(plane.get_microvm_image(imageIdentifier=arn).get("state"))
    except Exception as exc:  # noqa: BLE001 - the error class is the finding
        if type(exc).__name__ == "ResourceNotFoundException":
            return None
        return type(exc).__name__


def ensure_image_vm_cleanup(
    report: dict[str, Any],
    prefix: str,
    plane: Any,
    results: Results,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> None:
    """Terminates every VM of the run that isn't yet, and waits for each to read TERMINATED.

    The VMs are the listing's, matched by the image each was launched from, plus the one
    the report names, read by its id. An error from either read ends the wait as a failure.
    The detail keeps the first read, which is what the test's own teardown left.
    """
    vm_id = (report.get("vm") or {}).get("id")
    states: dict[str, str] = {}
    first: dict[str, str] | None = None
    error = None
    deadline = clock() + CLEANUP_SECONDS
    while True:
        try:
            states = ensure_image_vms(plane, prefix)
            if vm_id:
                states[vm_id] = str(
                    plane.get_microvm(microvmIdentifier=vm_id).get("state")
                )
        except Exception as exc:  # noqa: BLE001 - the error class is the finding
            error = type(exc).__name__
            break
        if first is None:
            first = dict(states)
        live = [vm for vm, state in states.items() if state != "TERMINATED"]
        for vm in live:
            if states[vm] != "TERMINATING":
                with contextlib.suppress(Exception):
                    plane.terminate_microvm(microvmIdentifier=vm)
        if not live or clock() > deadline:
            break
        sleep(CLEANUP_POLL_SECONDS)
    results.check(
        ENSURE_VM_TERMINATED,
        error is None and all(state == "TERMINATED" for state in states.values()),
        f"report={vm_id!r} found={first!r} last={states!r}"
        + (f" error={error}" if error else ""),
    )


def ensure_image_image_cleanup(
    report: dict[str, Any],
    prefix: str,
    plane: Any,
    results: Results,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> None:
    """Deletes every image of the run, retrying until the listing and the report's ARN agree
    it's gone.

    The delete is retried because an image still `CREATING` (a build the test abandoned)
    refuses it, so one attempt could report a leak as cleaned. The ARN is read as well as
    the listing, so a listing that reads nothing (a changed page shape) can't pass for a
    deleted image when the report says one exists.
    """
    arn = report.get("arn")
    seen: list[str] = [arn] if arn else []
    last: dict[str, str] = {}
    remaining: list[dict[str, Any]] | None = None
    by_arn: str | None = None
    deadline = clock() + CLEANUP_SECONDS
    while True:
        try:
            remaining = ensure_image_images(plane, prefix)
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
        by_arn = image_state(plane, arn)
        if by_arn is not None and arn:
            last[f"{arn} (by ARN)"] = by_arn
            if by_arn != "DELETING":
                with contextlib.suppress(Exception):
                    plane.delete_microvm_image(imageIdentifier=arn)
            by_arn = image_state(plane, arn)
        if (not remaining and by_arn is None) or clock() > deadline:
            break
        sleep(CLEANUP_POLL_SECONDS)
    results.check(
        ENSURE_IMAGE_GONE,
        remaining == [] and by_arn is None,
        f"prefix={prefix!r} found={seen!r} last={last!r} "
        f"the test's own delete={report.get('cleanup')!r}",
    )
