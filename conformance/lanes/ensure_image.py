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
from pathlib import Path
from typing import Any

from harness.constants import REPO, SERVICE
from harness.results import Results

#: What `drive_ensure_image` names the image it ensures: a fresh nonce per run, so the
#: first call of the run builds rather than reusing an image an earlier run left behind.
ENSURE_PREFIX = "conformance-ensure"

#: The ready spellings `Image.is_ready` accepts.
READY_IMAGE_STATES = ("CREATED", "UPDATED", "ACTIVE", "AVAILABLE")


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

    Cleanup is this function's and is verified independently of the test's own: the image
    is absent, the VM is TERMINATED, the S3 objects under the run's key prefix are deleted,
    and the service-created log group is deleted, each read back through boto3.
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
) -> None:
    """Deletes what the ensure-image run created and reads each deletion back."""
    plane = aws.client(SERVICE)
    s3 = aws.client("s3")
    logs = aws.client("logs")
    name = str(report.get("name") or "")
    arn = report.get("arn")

    image_gone = True
    detail = "no image was created"
    if arn:
        try:
            state = plane.get_microvm_image(imageIdentifier=arn).get("state")
            image_gone = False
            detail = f"still present as {state}"
            with contextlib.suppress(Exception):
                plane.delete_microvm_image(imageIdentifier=arn)
        except Exception as exc:  # noqa: BLE001 - the error class is the finding
            image_gone = type(exc).__name__ == "ResourceNotFoundException"
            detail = type(exc).__name__
    results.check("the ensured image is deleted", image_gone, detail)

    vm_id = (report.get("vm") or {}).get("id")
    vm_state = "no VM was launched"
    vm_ok = True
    if vm_id:
        try:
            vm_state = str(plane.get_microvm(microvmIdentifier=vm_id).get("state"))
            vm_ok = vm_state == "TERMINATED"
        except Exception as exc:  # noqa: BLE001
            vm_state, vm_ok = type(exc).__name__, False
    results.check("the ensure-image VM is terminated", vm_ok, f"{vm_id}: {vm_state}")

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
        "the ensure-image artifacts are deleted from S3",
        remaining == 0,
        f"deleted={keys!r} remaining={remaining}",
    )

    group = f"/aws/lambda-microvms/{name or prefix}"
    with contextlib.suppress(Exception):
        logs.delete_log_group(logGroupName=group)
    left = logs.describe_log_groups(logGroupNamePrefix=group).get("logGroups") or []
    results.check(
        "the ensure-image build log group is deleted",
        not [g for g in left if g.get("logGroupName") == group],
        f"group={group!r} remaining={[g.get('logGroupName') for g in left]!r}",
    )
