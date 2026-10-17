#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3>=1.40"]
# ///
# SPDX-License-Identifier: Apache-2.0
"""Recover exact reviewed incident images and source-run-owned log groups.

The source log stays in memory. It must match the reviewed digest and independently
produce the source-owned identities and resolve any explicitly authorized image
identity before AWS clients are constructed. No VM or
infrastructure mutation is available here. Recovery never establishes conformance.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
MANIFESTS = ROOT / "conformance/recovery"
WORKFLOW = ".github/workflows/live-conformance.yml"
SERVICE_PREFIX = "/aws/lambda-microvms/"
SCHEME = "sha256-kind-nul-region-nul-name-v1"
PARSER_VERSION = 2
LOG_REPRESENTATION = "gh-run-view-job-log-python-text-utf8-v1"
IMAGE_STATES = {
    "CREATING",
    "CREATED",
    "CREATE_FAILED",
    "UPDATING",
    "UPDATED",
    "UPDATE_FAILED",
    "DELETING",
    "DELETE_FAILED",
    "DELETED",
}
VM_STATES = {
    "PENDING",
    "RUNNING",
    "SUSPENDING",
    "SUSPENDED",
    "TERMINATING",
    "TERMINATED",
}
VERSION_STATES = {
    "PENDING",
    "IN_PROGRESS",
    "SUCCESSFUL",
    "FAILED",
    "DELETING",
    "DELETED",
    "DELETE_FAILED",
}
RETRY_CODES = {
    "ThrottlingException",
    "TooManyRequestsException",
    "ServiceUnavailableException",
    "RequestTimeout",
}
ERROR_CODES = RETRY_CODES | {
    "AccessDeniedException",
    "ConflictException",
    "ValidationException",
    "ResourceInUseException",
    "InvalidStateException",
    "InternalServerException",
    "ServiceException",
    "ResourceNotFoundException",
}
OPERATIONS = {
    "list_microvm_images",
    "list_microvms",
    "describe_log_groups",
    "list_microvm_image_versions",
    "get_microvm_image_version",
    "get_microvm_image",
    "delete_microvm_image",
    "delete_log_group",
    "get_role",
}
TOTAL_SECONDS = 600
VISIBILITY_SECONDS = 10


class Refused(Exception):
    """A fixed, public reason code; never a provider's exception message."""


def require(ok: Any, code: str) -> None:
    if not ok:
        raise Refused(code)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def identity(kind: str, region: str, name: str) -> str:
    return digest(f"{kind}\0{region}\0{name}".encode())


def text(value: Any) -> str:
    require(isinstance(value, str) and bool(value), "missing_text")
    return value


def stamp(value: Any) -> float:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise Refused("invalid_timestamp") from None
    require(
        isinstance(value, datetime) and value.tzinfo is not None, "invalid_timestamp"
    )
    number = value.timestamp()
    require(math.isfinite(number), "invalid_timestamp")
    return number


def load_manifest(run: str, expected: str) -> dict:
    require(re.fullmatch(r"[1-9][0-9]*", run), "invalid_source_run")
    require(re.fullmatch(r"[a-f0-9]{64}", expected), "invalid_manifest_digest")
    path = MANIFESTS / f"{run}.json"
    require(not path.is_symlink(), "manifest_symlink")
    raw = path.read_bytes()
    require(digest(raw) == expected, "manifest_digest")
    manifest = json.loads(raw)
    require(
        manifest["version"] == 1 and manifest["hash_scheme"] == SCHEME,
        "manifest_schema",
    )
    require(manifest["source_run_id"] == int(run), "manifest_run")
    require(
        manifest["parser_version"] == PARSER_VERSION
        and manifest["source_log_representation"] == LOG_REPRESENTATION,
        "manifest_parser",
    )
    require(manifest["workflow_path"] == WORKFLOW, "manifest_workflow")
    require(manifest["region"] in {"us-east-1", "us-west-2"}, "manifest_region")
    resources = manifest["resources"]
    require(isinstance(resources, list) and resources, "empty_resources")
    required = {
        "shared-image",
        "shared-log",
        "configured-log",
        "capability-default-image",
    }
    proofs = {r["proof"] for r in resources}
    require(
        proofs in (required, required | {"explicit-incident-image"})
        and len(resources) == len(proofs),
        "resource_proofs",
    )
    hashes = [r["sha256"] for r in resources]
    require(
        len(set(hashes)) == len(hashes)
        and all(re.fullmatch(r"[a-f0-9]{64}", h) for h in hashes),
        "resource_hashes",
    )
    require(not set(hashes) & set(manifest["excluded"]), "excluded_resource")
    for resource in resources:
        require(set(resource) == {"kind", "sha256", "proof"}, "resource_schema")
        require(
            resource["kind"]
            == ("image" if resource["proof"].endswith("image") else "log-group"),
            "resource_kind",
        )
    return manifest


def source_leaked_images(log: str) -> set[str]:
    """Resolve exact names from the pinned post-destroy inventory, without ownership."""
    names = set()
    for line in log.splitlines():
        columns = line.split("\t", 2)
        if len(columns) != 3 or columns[1] != "verify again after destroy":
            continue
        value = re.sub(r"^\d{4}-\d\d-\d\dT\S+\s*", "", columns[2]).strip()
        value = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", value)
        match = re.fullmatch(r"LEAK image ([A-Za-z0-9_.-]+) in [A-Z_]+", value)
        if match:
            names.add(match[1])
    return names


def source_ownership(log: str, region: str) -> dict[str, tuple[str, str, str | None]]:
    """Reproduce only the incident's positively observed ownership, never name families."""
    shared = configured = prefix = None
    leaked_images = source_leaked_images(log)
    phase = None  # pragma: no mutate (only exact active-step equality matters before a group)
    for line in log.splitlines():
        columns = line.split("\t", 2)
        if len(columns) != 3:
            continue
        step = columns[1]
        value = re.sub(r"^\d{4}-\d\d-\d\dT\S+\s*", "", columns[2]).strip()
        value = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", value)
        if value.startswith("##[group]Run "):
            phase = "conformance suite" if "conformance/run_rs.py" in value else None
        if step == "UNKNOWN STEP":
            step = phase
        if step != "conformance suite":
            continue
        match = re.fullmatch(r"(PASS|FAIL)\s+(.+?) — (.*)", value)
        if not match:
            continue
        verdict, name, detail = match.groups()
        if (
            verdict == "PASS"
            and name == "keep reported the identifiers the caller now owns"
        ):
            found = re.search(r"\bimage=(arn:[^\s]+)", detail)
            if found:
                arn = found[1]
                parsed = re.fullmatch(
                    r"arn:[^:]+:lambda:"
                    + re.escape(region)
                    + r":[0-9]+:microvm-image[:/]([A-Za-z0-9_.-]+)",
                    arn,
                )
                require(parsed is not None, "source_image_identity")
                require(shared in (None, (parsed[1], arn)), "source_image_conflict")
                shared = (parsed[1], arn)
        if (
            verdict == "PASS"
            and name == "the configured build log group exists and is readable"
        ):
            found = re.search(
                re.escape(SERVICE_PREFIX) + r"conformance-configured-[0-9a-f]{8}\b",
                detail,
            )
            require(found is not None, "source_configured_identity")
            require(configured in (None, found[0]), "source_configured_conflict")
            configured = found[0]
        if (
            verdict == "FAIL"
            and name == "capability-policy-default: the ensured image is deleted"
        ):
            found = re.search(
                r"\bprefix='(microvm-cli-capability-policy-default-[0-9a-f]{8})'",
                detail,
            )
            require(found is not None, "source_capability_identity")
            require(prefix in (None, found[1]), "source_capability_conflict")
            prefix = found[1]
    require(shared and configured and prefix, "source_ownership_incomplete")
    candidates = [
        name
        for name in leaked_images
        if name == prefix or name.startswith(prefix + "-")
    ]
    require(len(candidates) == 1, "source_capability_ambiguous")
    rows = [
        ("image", shared[0], "shared-image", shared[1]),
        ("log-group", SERVICE_PREFIX + shared[0], "shared-log", None),
        ("log-group", configured, "configured-log", None),
        ("image", candidates[0], "capability-default-image", None),
    ]
    return {
        identity(kind, region, name): (proof, name, arn)
        for kind, name, proof, arn in rows
    }


def explicit_image_selection(
    log: str, region: str, resources: list[dict], proven: dict
) -> dict:
    """A reviewed manifest supplies authority; cleanup rows only resolve exact identity."""
    selected = [row for row in resources if row["proof"] == "explicit-incident-image"]
    require(len(selected) <= 1, "explicit_image_conflict")
    if not selected:
        return {}
    row = selected[0]
    require(row["kind"] == "image", "explicit_image_kind")
    names = [
        name
        for name in source_leaked_images(log)
        if identity("image", region, name) == row["sha256"]
    ]
    require(len(names) == 1, "explicit_image_unresolved")
    require(row["sha256"] not in proven, "explicit_image_conflict")
    return {row["sha256"]: ("explicit-incident-image", names[0], None)}


def source_infrastructure(log: str, region: str) -> dict:
    """Correlate observations with exact source inputs, without granting ownership."""
    buckets, suite_buckets, roles = set(), set(), set()
    for line in log.splitlines():
        columns = line.split("\t", 2)
        if len(columns) != 3:
            continue
        value = re.sub(r"^\d{4}-\d\d-\d\dT\S+\s*", "", columns[2]).strip()
        value = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", value)
        bucket = re.fullmatch(r's3_bucket = "([a-z0-9][a-z0-9.-]+)"', value)
        suite = re.fullmatch(r"infra: bucket=([a-z0-9][a-z0-9.-]+)", value)
        role = re.fullmatch(
            r'build_role_arn = "(arn:aws(?:-[a-z]+)?:iam::\d{12}:role/[A-Za-z0-9_+=,.@/-]+)"',
            value,
        )
        if bucket:
            buckets.add(bucket[1])
        if suite:
            suite_buckets.add(suite[1])
        if role:
            roles.add(role[1])
    require(
        len(buckets) == len(roles) == 1 and buckets == suite_buckets,
        "source_infrastructure_ambiguous",
    )
    return {
        "bucket_sha256": identity("bucket", region, next(iter(buckets))),
        "build_role_arn_sha256": digest(next(iter(roles)).encode()),
    }


class Github:
    def __init__(self, repository: str, *, deadline=None, clock=time.monotonic):
        require(
            re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository),
            "repository_name",
        )
        self.repository = repository
        self.clock = clock
        self.deadline = deadline if deadline is not None else clock() + TOTAL_SECONDS

    def command(self, args: list[str]) -> str:
        remaining = self.deadline - self.clock()
        require(remaining > 0, "deadline")
        done = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=min(120, remaining),
            check=False,
        )
        require(self.clock() < self.deadline, "deadline")
        require(done.returncode == 0, "source_fetch_failed")
        return done.stdout

    def api(self, suffix: str) -> dict:
        return json.loads(self.command(["api", f"repos/{self.repository}/{suffix}"]))

    def log(self, manifest: dict) -> str:
        return self.command(
            [
                "run",
                "view",
                str(manifest["source_run_id"]),
                "--repo",
                self.repository,
                "--job",
                str(manifest["source_job_id"]),
                "--log",
            ]
        )


def provenance(manifest: dict, github: Github, environ: dict[str, str]) -> dict:
    require(
        environ.get("GITHUB_ACTIONS") == "true"
        and environ.get("GITHUB_REF") == "refs/heads/main",
        "hosted_main_required",
    )
    require(
        environ.get("GITHUB_REPOSITORY_ID") == str(manifest["repository_id"]),
        "repository_id",
    )
    run_id = environ.get("GITHUB_RUN_ID")
    require(
        isinstance(run_id, str) and re.fullmatch(r"[1-9][0-9]*", run_id),
        "current_run_id",
    )
    require(
        environ.get("GITHUB_RUN_ID") != str(manifest["source_run_id"]),
        "source_is_current_run",
    )
    require(environ.get("AWS_REGION") == manifest["region"], "selected_region")
    repo = github.api("")
    owner = text(repo["owner"]["login"])
    require(repo["id"] == manifest["repository_id"], "repository_id")
    require(
        environ.get("GITHUB_ACTOR") == owner
        and environ.get("GITHUB_TRIGGERING_ACTOR") == owner,
        "recovery_owner",
    )
    run = github.api(
        f"actions/runs/{manifest['source_run_id']}/attempts/{manifest['source_attempt']}"
    )
    for key, expected in {
        "id": manifest["source_run_id"],
        "run_attempt": manifest["source_attempt"],
        "workflow_id": manifest["workflow_id"],
        "path": WORKFLOW,
        "head_sha": manifest["source_sha"],
        "event": "workflow_dispatch",
        "status": "completed",
        "conclusion": manifest["source_conclusion"],
    }.items():
        require(run.get(key) == expected, "origin_" + key)
    require(
        run["repository"]["id"] == manifest["repository_id"]
        and run["head_repository"]["id"] == manifest["repository_id"],
        "origin_repository",
    )
    require(
        run["actor"]["login"] == owner and run["triggering_actor"]["login"] == owner,
        "origin_owner",
    )
    latest = github.api(f"actions/runs/{manifest['source_run_id']}")
    require(
        latest.get("run_attempt") == manifest["source_attempt"]
        and latest.get("status") == "completed",
        "origin_attempt_changed",
    )
    job = github.api(f"actions/jobs/{manifest['source_job_id']}")
    for key, expected in {
        "run_id": manifest["source_run_id"],
        "run_attempt": manifest["source_attempt"],
        "name": manifest["source_job_name"],
        "status": "completed",
        "conclusion": manifest["source_job_conclusion"],
        "started_at": manifest["source_job_started_at"],
        "completed_at": manifest["source_job_completed_at"],
    }.items():
        require(job.get(key) == expected, "origin_job_" + key)
    source = github.api(f"contents/{WORKFLOW}?ref={manifest['source_sha']}")
    require(source.get("encoding") == "base64", "source_encoding")
    require(
        digest(base64.b64decode(source["content"]))
        == manifest["source_workflow_sha256"],
        "source_workflow_digest",
    )
    log = github.log(manifest)
    require(digest(log.encode()) == manifest["source_log_sha256"], "source_log_digest")
    require(
        source_infrastructure(log, manifest["region"])
        == manifest["source_infrastructure"],
        "source_infrastructure_digest",
    )
    ownership = source_ownership(log, manifest["region"])
    positive = [
        row
        for row in manifest["resources"]
        if row["proof"] != "explicit-incident-image"
    ]
    for row in positive:
        require(
            row["sha256"] in ownership and ownership[row["sha256"]][0] == row["proof"],
            "unproven_resource",
        )
    require(set(ownership) == {r["sha256"] for r in positive}, "ownership_set")
    ownership.update(
        explicit_image_selection(
            log, manifest["region"], manifest["resources"], ownership
        )
    )
    return ownership


def error_code(error: Exception) -> str:
    response = getattr(error, "response", None)
    return (
        response.get("Error", {}).get("Code", "unknown")
        if isinstance(response, dict)
        else "unknown"
    )


class Api:
    def __init__(
        self,
        plane: Any,
        logs: Any,
        *,
        clock=time.monotonic,
        sleep=time.sleep,
        deadline=None,
    ):
        self.plane, self.logs = plane, logs
        self.clock, self.sleep = clock, sleep
        self.deadline = deadline if deadline is not None else clock() + TOTAL_SECONDS
        self.version_evidence: list[dict] = []
        self.failures: list[dict] = []
        self._observed_versions: set[tuple[str, str]] = set()
        self.region = ""
        self.source_infrastructure: dict = {}

    def check_time(self) -> None:
        require(self.clock() < self.deadline, "deadline")

    def call(self, client: Any, operation: str, **kwargs) -> dict:
        attempt = 0
        while True:
            self.check_time()
            try:
                result = getattr(client, operation)(**kwargs)
                require(isinstance(result, dict), "invalid_response")
                return result
            except (Refused, InterruptedError):
                raise
            except Exception as exc:  # noqa: BLE001 - provider messages may contain private identifiers
                code = error_code(exc)
                if code == "ResourceNotFoundException":
                    raise Refused("not_found") from None
                if code in RETRY_CODES and attempt < 2:
                    attempt += 1
                    self.sleep(2)
                    continue
                self.failures.append(
                    {
                        "operation": operation
                        if operation in OPERATIONS
                        else "unknown",
                        "code": code if code in ERROR_CODES else "unknown",
                    }
                )
                raise Refused("api_failed") from None

    def pages(self, client: Any, operation: str, field: str, **kwargs) -> list[dict]:
        rows, seen = [], set()
        while True:
            page = self.call(client, operation, **kwargs)
            require(isinstance(page.get(field), list), "missing_page_items")
            require(
                all(isinstance(row, dict) for row in page[field]), "invalid_page_item"
            )
            rows.extend(page[field])
            token = page.get("nextToken")
            if token is None:
                return rows
            require(
                isinstance(token, str) and token and token not in seen,
                "pagination_token",
            )
            seen.add(token)
            kwargs["nextToken"] = token

    @staticmethod
    def unique(rows: list[dict], field: str) -> dict[str, dict]:
        result = {}
        for row in rows:
            key = text(row.get(field))
            require(key not in result, "duplicate_identity")
            result[key] = row
        return result

    def inventory(self) -> tuple[dict, dict, dict]:
        images = self.unique(
            self.pages(self.plane, "list_microvm_images", "items"), "imageArn"
        )
        vms = self.unique(self.pages(self.plane, "list_microvms", "items"), "microvmId")
        groups = self.unique(
            self.pages(
                self.logs,
                "describe_log_groups",
                "logGroups",
                logGroupNamePrefix=SERVICE_PREFIX,
            ),
            "logGroupName",
        )
        for image in images.values():
            text(image.get("name"))
            require(image.get("state") in IMAGE_STATES, "image_state")
        for vm in vms.values():
            text(vm.get("imageArn"))
            require(vm.get("state") in VM_STATES, "vm_state")
        return images, vms, groups

    def configured_dependencies(
        self, group: str | None, images: dict, deleting: set[str]
    ) -> None:
        for arn in images:
            versions = self.unique(
                self.pages(
                    self.plane,
                    "list_microvm_image_versions",
                    "items",
                    imageIdentifier=arn,
                ),
                "imageVersion",
            )
            for version in versions:
                record = self.call(
                    self.plane,
                    "get_microvm_image_version",
                    imageIdentifier=arn,
                    imageVersion=version,
                )
                require(
                    record.get("imageArn") == arn
                    and record.get("imageVersion") == version,
                    "version_identity",
                )
                require(record.get("state") in VERSION_STATES, "version_state")
                if record["state"] == "DELETED":
                    continue
                self.observe_version(images[arn]["name"], version, record)
                if group is None:
                    continue
                logging = record.get("logging")
                require(isinstance(logging, dict), "version_logging_missing")
                if logging.get("disabled") == {}:
                    require(set(logging) == {"disabled"}, "version_logging_ambiguous")
                    continue
                cloud = logging.get("cloudWatch")
                require(
                    set(logging) == {"cloudWatch"} and isinstance(cloud, dict),
                    "version_logging_ambiguous",
                )
                destination = text(cloud.get("logGroup"))
                require(
                    destination != group or arn in deleting, "configured_group_in_use"
                )

    def observe_version(self, name: str, version: str, record: dict) -> None:
        # Version metadata can inform a later reviewed incident amendment. It
        # never adds a resource to this run's deletion allowlist.
        if (name, version) in self._observed_versions:
            return
        self._observed_versions.add((name, version))
        row = {
            "imageSha256": identity("image", self.region, name),
            "versionSha256": digest(version.encode()),
            "createdAt": stamp(record.get("createdAt")),
        }
        role = record.get("buildRoleArn")
        if isinstance(role, str):
            row["buildRoleArnSha256"] = digest(role.encode())
        artifact = record.get("codeArtifact")
        if isinstance(artifact, dict) and isinstance(artifact.get("uri"), str):
            parsed = urlsplit(artifact["uri"])
            if parsed.scheme == "s3" and parsed.netloc:
                row["artifactBucketSha256"] = identity(
                    "bucket", self.region, parsed.netloc
                )
                row["artifactKeySha256"] = digest(parsed.path.lstrip("/").encode())
        logging = record.get("logging")
        cloud = logging.get("cloudWatch") if isinstance(logging, dict) else None
        if isinstance(cloud, dict) and isinstance(cloud.get("logGroup"), str):
            row["logGroupSha256"] = identity(
                "log-group", self.region, cloud["logGroup"]
            )
        if self.source_infrastructure:
            row["sourceBucketMatches"] = row.get(
                "artifactBucketSha256"
            ) == self.source_infrastructure.get("bucket_sha256")
            row["sourceBuildRoleMatches"] = row.get(
                "buildRoleArnSha256"
            ) == self.source_infrastructure.get("build_role_arn_sha256")
        self.version_evidence.append(row)

    def image_absent(self, arn: str) -> bool:
        try:
            record = self.call(self.plane, "get_microvm_image", imageIdentifier=arn)
        except Refused as exc:
            if str(exc) == "not_found":
                return True
            raise
        require(
            record.get("imageArn") == arn and record.get("state") in IMAGE_STATES,
            "image_readback",
        )
        return False

    def wait_image_absent(self, arn: str) -> None:
        while not self.image_absent(arn):
            self.check_time()
            self.sleep(2)


def fingerprint(kind: str, row: dict, manifest: dict) -> tuple:
    start, end = (
        stamp(manifest["source_job_started_at"]),
        stamp(manifest["source_job_completed_at"]),
    )
    if kind == "image":
        created = stamp(row.get("createdAt"))
        value = (text(row.get("name")), text(row.get("imageArn")), created)
    else:
        milliseconds = row.get("creationTime")
        require(
            type(milliseconds) in (int, float) and math.isfinite(milliseconds),
            "group_creation_time",
        )
        created = milliseconds / 1000
        value = (text(row.get("logGroupName")), created)
    require(start <= created <= end, "outside_source_lifetime")
    return value


def selected(manifest: dict, ownership: dict, api: Api) -> tuple[dict, tuple]:
    api.region = manifest["region"]
    api.source_infrastructure = manifest.get("source_infrastructure", {})
    images, vms, groups = api.inventory()
    by_name = api.unique(list(images.values()), "name")
    plan = {}
    for resource in manifest["resources"]:
        key, kind = resource["sha256"], resource["kind"]
        _, name, source_arn = ownership[key]
        row = (by_name if kind == "image" else groups).get(name)
        if row is None:
            continue
        current = fingerprint(kind, row, manifest)
        if kind == "image":
            arn = row["imageArn"]
            require(source_arn is None or arn == source_arn, "source_arn_changed")
            record = api.call(api.plane, "get_microvm_image", imageIdentifier=arn)
            require(record.get("state") in IMAGE_STATES, "image_readback_state")
            require(fingerprint(kind, record, manifest) == current, "image_changed")
            require(
                not any(
                    vm["imageArn"] == arn and vm["state"] != "TERMINATED"
                    for vm in vms.values()
                ),
                "image_in_use",
            )
        plan[key] = (resource, row, current)
    deleting = {
        row["imageArn"]
        for resource, row, _ in plan.values()
        if resource["kind"] == "image"
    }
    for resource, row, _ in plan.values():
        if resource["kind"] == "log-group":
            api.configured_dependencies(row["logGroupName"], images, deleting)
    if not any(resource["kind"] == "log-group" for resource, _, _ in plan.values()):
        # Read-only version evidence remains useful after an earlier run removed
        # every approved log group; it never extends the deletion allowlist.
        api.configured_dependencies(None, images, deleting)
    return plan, (images, vms, groups)


def settled_selection(manifest: dict, ownership: dict, api: Api) -> tuple[dict, tuple]:
    # A just-deleted object can remain in a list after its exact Get says gone.
    # Wait for the list to converge; never turn a different API error into absence.
    while True:
        try:
            return selected(manifest, ownership, api)
        except Refused as exc:
            if str(exc) != "not_found":
                raise
            api.check_time()
            api.sleep(2)


def recover(manifest: dict, ownership: dict, api: Api, report: dict) -> None:
    initial, _ = settled_selection(manifest, ownership, api)
    report["resources"] = [
        {
            "kind": r["kind"],
            "sha256": r["sha256"],
            "status": "present" if r["sha256"] in initial else "absent",
            "deletionRequested": False,
        }
        for r in manifest["resources"]
    ]
    statuses = {r["sha256"]: r for r in report["resources"]}
    # No mutation occurs before all initial ownership/incarnation/dependency checks pass.
    for key, (resource, row, old) in initial.items():
        if resource["kind"] != "image":
            continue
        current, _ = settled_selection(manifest, ownership, api)
        require(key in current and current[key][2] == old, "incarnation_changed")
        if row["state"] not in {"DELETING", "DELETED"}:
            api.call(api.plane, "delete_microvm_image", imageIdentifier=row["imageArn"])
            statuses[key]["deletionRequested"] = True
        statuses[key]["status"] = "deletion_requested"
        api.wait_image_absent(row["imageArn"])
        statuses[key]["status"] = "absent"
    for key, (resource, row, old) in initial.items():
        if resource["kind"] != "log-group":
            continue
        current, state = settled_selection(manifest, ownership, api)
        require(
            not any(r["kind"] == "image" for r, _, _ in current.values()),
            "image_not_absent",
        )
        require(key in current and current[key][2] == old, "incarnation_changed")
        api.configured_dependencies(row["logGroupName"], state[0], set())
        api.call(api.logs, "delete_log_group", logGroupName=row["logGroupName"])
        statuses[key]["deletionRequested"] = True
        statuses[key]["status"] = "deletion_requested"
    # Full inventories, including later pages, must remain clear through a visibility window.
    clear_since = None
    while True:
        current, _ = settled_selection(manifest, ownership, api)
        if current:
            clear_since = None
        elif clear_since is None:
            clear_since = api.clock()
        elif api.clock() - clear_since >= VISIBILITY_SECONDS:
            break
        api.check_time()
        api.sleep(2)
    for row in report["resources"]:
        row["status"] = "absent"


def safe_verifier(stdout: str, region: str) -> dict:
    """Only the independent verifier's fixed grammar leaves this process."""
    rows, summaries, seen = [], [], set()
    patterns = [
        ("pending", "image", r"pending: image ([A-Za-z0-9_./#:-]+) still DELETING"),
        ("leak", "image", r"LEAK image ([A-Za-z0-9_./#:-]+) in [A-Z_]+"),
        ("leak", "microvm", r"LEAK microvm ([A-Za-z0-9_./#:-]+) in [A-Z_]+"),
        (
            "leak",
            "log-group",
            r"LEAK log group ([A-Za-z0-9_./#:-]+) \((?:ours by prefix|named in .+|per-VM, named with --vm-log-group)\)",
        ),
        (
            "unclassified",
            "log-group",
            r"UNCLASSIFIED \(not deleted, not provably ours\): log group ([A-Za-z0-9_./#:-]+)",
        ),
        (
            "standing",
            "bucket",
            r"standing \(terraform-owned\): bucket ([a-z0-9_./#:-]+)",
        ),
        (
            "standing",
            "iam-role",
            r"standing \(terraform-owned\): iam role ([A-Za-z0-9_./#:-]+)",
        ),
        (
            "standing",
            "log-group",
            r"standing \(terraform-owned\): per-VM log group ([A-Za-z0-9_./#:-]+) \(named in .+\)",
        ),
    ]
    for line in stdout.splitlines():
        value = line.strip()
        if not value or value == "re-run with --delete to remove them":
            continue
        prefix = "account " + re.escape(region) + ": "
        leaked = re.fullmatch(prefix + r"([0-9]+) leaked resource\(s\)", value)
        unknown = re.fullmatch(
            prefix
            + r"clean of everything this project can name; ([0-9]+) log group\(s\) under /aws/lambda-microvms/ belong to no known prefix and no ledger record and were left alone \(listed above; --strict fails on them\)",
            value,
        )
        clean = value == f"account {region}: clean — no conformance resources survive"
        if leaked or unknown or clean:
            summaries.append(
                (int(leaked[1]), None)
                if leaked
                else (0, int(unknown[1]) if unknown else 0)
            )
            continue
        for category, kind, pattern in patterns:
            matched = re.fullmatch(pattern, value)
            if matched:
                key = (kind, identity(kind, region, matched[1]))
                require(key not in seen, "verifier_duplicate")
                seen.add(key)
                rows.append({"kind": kind, "sha256": key[1], "category": category})
                break
        else:
            raise Refused("verifier_output_unrecognized")
    require(len(summaries) == 1, "verifier_summary")
    leaks, unknown = summaries[0]
    require(
        leaks == sum(row["category"] == "leak" for row in rows)
        and (
            unknown is None
            or unknown == sum(row["category"] == "unclassified" for row in rows)
        ),
        "verifier_count_mismatch",
    )
    return {"resources": rows, "summary_present": True, "leakCount": leaks}


def captured_verifier(done: subprocess.CompletedProcess, region: str) -> dict:
    require(not done.stderr and done.returncode in (0, 1), "verifier_execution")
    result = safe_verifier(done.stdout, region)
    require(done.returncode == int(result["leakCount"] > 0), "verifier_exit_mismatch")
    result["exitCode"] = done.returncode
    return result


def independent_absence(manifest: dict, stdout: str) -> dict:
    result = safe_verifier(stdout, manifest["region"])
    approved = {(row["kind"], row["sha256"]) for row in manifest["resources"]}
    require(
        not any(
            (row["kind"], row["sha256"]) in approved for row in result["resources"]
        ),
        "independent_survivor",
    )
    return result


def observe_duration(iam: Any, role_arn: str | None, api: Api) -> dict:
    """An optional numeric observation; IAM read failures never expand recovery scope."""
    try:
        if not isinstance(role_arn, str) or not re.fullmatch(
            r"arn:[^:]+:iam::[0-9]+:role/.+", role_arn
        ):
            return {"status": "unavailable"}
        record = api.call(iam, "get_role", RoleName=role_arn.rsplit("/", 1)[1])["Role"]
        seconds = record.get("MaxSessionDuration")
        if not (record.get("Arn") == role_arn and type(seconds) is int and seconds > 0):
            return {"status": "unavailable"}
        return {"status": "observed", "maxSessionDurationSeconds": seconds}
    except InterruptedError:
        raise
    except Exception:  # noqa: BLE001 - optional observation must not expose provider errors
        return {"status": "unavailable"}


REASONS = [
    "api_failed",
    "aws_region",
    "configured_group_in_use",
    "current_run_id",
    "deadline",
    "duplicate_identity",
    "empty_resources",
    "excluded_resource",
    "explicit_image_conflict",
    "explicit_image_kind",
    "explicit_image_unresolved",
    "exclusive_names_required",
    "group_creation_time",
    "hosted_main_required",
    "image_changed",
    "image_in_use",
    "image_not_absent",
    "image_readback",
    "image_readback_state",
    "image_state",
    "incarnation_changed",
    "independent_survivor",
    "interrupted_or_invalid_input",
    "invalid_manifest_digest",
    "invalid_page_item",
    "invalid_response",
    "invalid_source_run",
    "invalid_timestamp",
    "invalid_deadline",
    "manifest_digest",
    "manifest_parser",
    "manifest_region",
    "manifest_run",
    "manifest_schema",
    "manifest_symlink",
    "manifest_workflow",
    "missing_page_items",
    "missing_text",
    "not_found",
    "origin_attempt_changed",
    "origin_conclusion",
    "origin_event",
    "origin_head_sha",
    "origin_id",
    "origin_job_completed_at",
    "origin_job_conclusion",
    "origin_job_name",
    "origin_job_run_attempt",
    "origin_job_run_id",
    "origin_job_started_at",
    "origin_job_status",
    "origin_owner",
    "origin_path",
    "origin_repository",
    "origin_run_attempt",
    "origin_status",
    "origin_workflow_id",
    "outside_source_lifetime",
    "ownership_set",
    "pagination_token",
    "receipt_schema",
    "recovery_checkout",
    "recovery_owner",
    "repository_id",
    "repository_name",
    "resource_hashes",
    "resource_kind",
    "resource_proofs",
    "resource_schema",
    "role_observation",
    "role_reference",
    "selected_region",
    "source_arn_changed",
    "source_capability_ambiguous",
    "source_capability_conflict",
    "source_capability_identity",
    "source_configured_conflict",
    "source_configured_identity",
    "source_encoding",
    "source_fetch_failed",
    "source_image_conflict",
    "source_image_identity",
    "source_infrastructure_ambiguous",
    "source_infrastructure_digest",
    "source_is_current_run",
    "source_log_digest",
    "source_ownership_incomplete",
    "source_workflow_digest",
    "unproven_resource",
    "verifier_count_mismatch",
    "verifier_duplicate",
    "verifier_execution",
    "verifier_exit_mismatch",
    "verifier_output_unrecognized",
    "verifier_summary",
    "version_identity",
    "version_logging_ambiguous",
    "version_logging_missing",
    "version_state",
    "vm_state",
]


def validate_receipt(value: Any) -> dict:
    """Accept only the core's bounded, identifier-free recovery receipt schema."""

    def valid(ok):
        require(ok, "receipt_schema")

    def hashed(item):
        return isinstance(item, str) and re.fullmatch(r"[a-f0-9]{64}", item) is not None

    def number(item):
        return type(item) in (int, float) and math.isfinite(item) and item >= 0

    def rows(items):
        valid(isinstance(items, list) and len(items) <= 10000)
        return items

    valid(isinstance(value, dict))
    valid(
        set(value)
        <= {
            "kind",
            "conformance_verified",
            "status",
            "reason",
            "sourceRun",
            "sourceSha",
            "region",
            "manifestSha256",
            "parserVersion",
            "sourceLogRepresentation",
            "resources",
            "versionEvidence",
            "roleDuration",
            "independentVerifier",
            "apiFailures",
        }
    )
    valid(
        value.get("kind") == "live-recovery"
        and value.get("conformance_verified") is False
    )
    valid(value.get("status") in {"passed", "failed"})
    if "reason" in value:
        valid(value["status"] == "failed" and value["reason"] in REASONS)
    for name in ("sourceRun", "parserVersion"):
        if name in value:
            valid(type(value[name]) is int and value[name] > 0)
    if "sourceSha" in value:
        valid(
            isinstance(value["sourceSha"], str)
            and re.fullmatch(r"[a-f0-9]{40}", value["sourceSha"])
        )
    if "region" in value:
        valid(value["region"] in {"us-east-1", "us-west-2"})
    if "manifestSha256" in value:
        valid(hashed(value["manifestSha256"]))
    if "sourceLogRepresentation" in value:
        valid(value["sourceLogRepresentation"] == LOG_REPRESENTATION)
    for row in rows(value.get("resources", [])):
        valid(
            isinstance(row, dict)
            and {"kind", "sha256", "status"} <= set(row)
            and set(row) <= {"kind", "sha256", "status", "deletionRequested"}
        )
        valid(row["kind"] in {"image", "log-group"} and hashed(row["sha256"]))
        valid(row["status"] in {"present", "absent", "deletion_requested"})
        if "deletionRequested" in row:
            valid(type(row["deletionRequested"]) is bool)
    for row in rows(value.get("apiFailures", [])):
        valid(isinstance(row, dict) and set(row) == {"operation", "code"})
        valid(row["operation"] in OPERATIONS | {"unknown"})
        valid(row["code"] in ERROR_CODES | {"unknown"})
    for row in rows(value.get("versionEvidence", [])):
        valid(
            isinstance(row, dict)
            and {"imageSha256", "versionSha256", "createdAt"} <= set(row)
        )
        valid(
            set(row)
            <= {
                "imageSha256",
                "versionSha256",
                "createdAt",
                "buildRoleArnSha256",
                "artifactBucketSha256",
                "artifactKeySha256",
                "logGroupSha256",
                "sourceBucketMatches",
                "sourceBuildRoleMatches",
            }
        )
        for key, item in row.items():
            valid(
                number(item)
                if key == "createdAt"
                else type(item) is bool
                if key.endswith("Matches")
                else hashed(item)
            )
    if "roleDuration" in value:
        role = value["roleDuration"]
        valid(isinstance(role, dict))
        valid(
            role == {"status": "unavailable"}
            or (
                set(role) == {"status", "maxSessionDurationSeconds"}
                and role["status"] == "observed"
                and type(role["maxSessionDurationSeconds"]) is int
                and role["maxSessionDurationSeconds"] > 0
            )
        )
    if "independentVerifier" in value:
        result = value["independentVerifier"]
        valid(
            isinstance(result, dict)
            and set(result) == {"resources", "summary_present", "leakCount", "exitCode"}
        )
        valid(
            result["summary_present"] is True
            and type(result["leakCount"]) is int
            and result["leakCount"] >= 0
        )
        valid(
            type(result["exitCode"]) is int
            and result["exitCode"] == int(result["leakCount"] > 0)
        )
        for row in rows(result["resources"]):
            valid(isinstance(row, dict) and set(row) == {"kind", "sha256", "category"})
            valid(
                row["kind"] in {"image", "log-group", "microvm", "bucket", "iam-role"}
                and hashed(row["sha256"])
            )
            valid(row["category"] in {"leak", "pending", "standing", "unclassified"})
        valid(
            result["leakCount"]
            == sum(row["category"] == "leak" for row in result["resources"])
        )
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preflight", "apply"))
    parser.add_argument("--source-run", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--exclusive-names", action="store_true")
    args = parser.parse_args()
    deadline = time.monotonic() + TOTAL_SECONDS
    report: dict = {
        "kind": "live-recovery",
        "conformance_verified": False,
        "status": "failed",
    }

    def interrupted(signum, frame):
        # Cancellation must pass through optional observers and SDK exception handling.
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        inherited = os.environ.get("RECOVERY_DEADLINE_MONOTONIC")
        if inherited is not None:
            inherited = float(inherited)
            require(math.isfinite(inherited), "invalid_deadline")
            deadline = min(deadline, inherited)
        require(time.monotonic() < deadline, "deadline")
        manifest = load_manifest(args.source_run, args.manifest_sha256)
        current = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        head_sha = os.environ.get("GITHUB_SHA")
        require(
            current.returncode == 0
            and isinstance(head_sha, str)
            and re.fullmatch(r"[a-f0-9]{40}", head_sha)
            and current.stdout.strip() == head_sha,
            "recovery_checkout",
        )
        ownership = provenance(
            manifest,
            Github(text(os.environ.get("GITHUB_REPOSITORY")), deadline=deadline),
            dict(os.environ),
        )
        report.update(
            sourceRun=manifest["source_run_id"],
            sourceSha=manifest["source_sha"],
            region=manifest["region"],
            manifestSha256=args.manifest_sha256,
            parserVersion=PARSER_VERSION,
            sourceLogRepresentation=LOG_REPRESENTATION,
        )
        if args.mode == "apply":
            require(args.exclusive_names, "exclusive_names_required")
            require(os.environ.get("AWS_REGION") == manifest["region"], "aws_region")
            require(time.monotonic() < deadline, "deadline")
            import boto3
            from botocore.config import Config

            config = Config(
                connect_timeout=5, read_timeout=5, retries={"total_max_attempts": 1}
            )
            session = boto3.Session(region_name=manifest["region"])
            api = Api(
                session.client("lambda-microvms", config=config),
                session.client("logs", config=config),
                deadline=deadline,
            )
            report["versionEvidence"] = api.version_evidence
            report["apiFailures"] = api.failures
            report["roleDuration"] = observe_duration(
                session.client("iam", config=config),
                os.environ.get("RECOVERY_ROLE_ARN"),
                api,
            )
            recover(manifest, ownership, api, report)
            remaining = deadline - time.monotonic()
            require(remaining > 0, "deadline")
            done = subprocess.run(
                [sys.executable, str(ROOT / "tools/verify-clean.py")],
                capture_output=True,
                text=True,
                timeout=min(120, remaining),
                env={
                    k: v
                    for k, v in os.environ.items()
                    if k not in {"GH_TOKEN", "GITHUB_TOKEN", "RECOVERY_ROLE_ARN"}
                },
            )
            report["independentVerifier"] = captured_verifier(done, manifest["region"])
            independent_absence(manifest, done.stdout)
        report["status"] = "passed"
        return 0
    except Refused as exc:
        report["reason"] = str(exc)
        return 1
    except (Exception, KeyboardInterrupt):  # noqa: BLE001 - retain a sanitized failure receipt
        report["reason"] = "interrupted_or_invalid_input"
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)
        args.report.write_text(json.dumps(report) + "\n")
        print(json.dumps(report))


if __name__ == "__main__":
    raise SystemExit(main())
