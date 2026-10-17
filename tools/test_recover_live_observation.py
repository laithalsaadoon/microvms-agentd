# SPDX-License-Identifier: Apache-2.0
"""Read-only evidence preserves uncertainty without granting cleanup authority."""

import contextlib
import copy
import importlib.util
import io
import json
import runpy
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

SPEC = importlib.util.spec_from_file_location(
    "tools.recover-live", Path(__file__).with_name("recover-live.py")
)
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)
# Load once so each fixture avoids recompiling the wrapper's validation namespaces.
CREDENTIALS = runpy.run_path(str(Path(__file__).with_name("recovery-credentials.py")))
REGION = "us-east-1"
NAME = "synthetic-private-image"
ARN = f"arn:aws:lambda:{REGION}:000000000000:microvm-image:{NAME}"
VERSION = "synthetic-private-version"
GROUP = "/aws/lambda-microvms/synthetic-private-group"
CREATED = datetime(2026, 10, 17, 16, tzinfo=timezone.utc)


def report():
    return {
        "kind": "live-recovery-observation",
        "schemaVersion": 1,
        "conformance_verified": False,
        "recoveryAccepted": False,
        "mutationsPerformed": False,
        "status": "incomplete",
        "sourceRun": 9,
        "sourceSha": "a" * 40,
        "region": REGION,
        "manifestSha256": "b" * 64,
        "parserVersion": recovery.PARSER_VERSION,
        "sourceLogRepresentation": recovery.LOG_REPRESENTATION,
        "sections": {
            name: "not_run"
            for name in (
                "independentVerifier",
                "images",
                "microvms",
                "logGroups",
                "versions",
            )
        },
        "images": [],
        "microvms": [],
        "logGroups": [],
        "versions": [],
        "versionCoverage": [],
        "errors": [],
    }


@pytest.fixture
def census(monkeypatch):
    image = {"name": NAME, "imageArn": ARN, "state": "CREATED", "createdAt": CREATED}
    version = {
        "imageArn": ARN,
        "imageVersion": VERSION,
        "state": "SUCCESSFUL",
        "status": "ACTIVE",
        "createdAt": CREATED,
        "logging": {"cloudWatch": {"logGroup": GROUP}},
    }
    plane = SimpleNamespace(
        list_microvm_images=Mock(return_value={"items": [image]}),
        get_microvm_image=Mock(return_value=copy.deepcopy(image)),
        list_microvms=Mock(return_value={"items": []}),
        list_microvm_image_versions=Mock(return_value={"items": [version]}),
        get_microvm_image_version=Mock(return_value=copy.deepcopy(version)),
        delete_microvm_image=Mock(side_effect=AssertionError("write attempted")),
    )
    logs = SimpleNamespace(
        describe_log_groups=Mock(return_value={"logGroups": [{"logGroupName": GROUP}]}),
        delete_log_group=Mock(side_effect=AssertionError("write attempted")),
    )
    events = []

    def verifier(command, **kwargs):
        assert command == [
            recovery.sys.executable,
            str(recovery.ROOT / "tools/verify-clean.py"),
        ]
        assert kwargs["capture_output"] is True and kwargs["text"] is True
        assert "--delete" not in command
        events.append("verifier")
        return subprocess.CompletedProcess(
            command,
            0,
            f"account {REGION}: clean — no conformance resources survive\n",
            "",
        )

    monkeypatch.setattr(recovery.subprocess, "run", verifier)
    api = recovery.ReadOnlyApi(plane, logs, clock=lambda: 0, sleep=lambda _: None)
    original_call = api.call

    def read(client, operation, **kwargs):
        if operation in recovery.OBSERVATION_OPERATIONS:
            assert events and events[0] == "verifier", (
                "census preceded independent verifier"
            )
            events.append(operation)
        return original_call(client, operation, **kwargs)

    monkeypatch.setattr(api, "call", read)
    manifest = {
        "region": REGION,
        "resources": [
            {"kind": "image", "sha256": recovery.identity("image", REGION, NAME)}
        ],
    }
    owned = {manifest["resources"][0]["sha256"]: ("shared-image", NAME, ARN)}
    return SimpleNamespace(
        plane=plane,
        logs=logs,
        api=api,
        manifest=manifest,
        owned=owned,
        image=image,
        version=version,
        events=events,
    )


@pytest.mark.parametrize(
    "logging,expected",
    [
        ({}, {"kind": "missing"}),
        ({"logging": None}, {"kind": "null"}),
        ({"logging": {"disabled": {}}}, {"kind": "disabled"}),
        (
            {"logging": {"cloudWatch": {"logGroup": GROUP}}},
            {"kind": "cloudWatch", "logGroupSha256": None},
        ),
        ({"logging": []}, {"kind": "malformed"}),
        ({"logging": {"disabled": True}}, {"kind": "malformed"}),
        ({"logging": {"cloudWatch": {}}}, {"kind": "malformed"}),
        (
            {"logging": {"disabled": {}, "cloudWatch": {"logGroup": GROUP}}},
            {"kind": "malformed"},
        ),
        (
            {"logging": {"SDK_UNKNOWN_MEMBER": {"name": "synthetic-secret-member"}}},
            {"kind": "unknownMember"},
        ),
    ],
)
def test_logging_observation_retains_exact_uncertainty_without_raw_values(
    logging, expected
):
    expected = copy.deepcopy(expected)
    if "logGroupSha256" in expected:
        expected["logGroupSha256"] = recovery.identity("log-group", REGION, GROUP)
    snapshot = recovery.observation_snapshot(logging, REGION)
    assert snapshot["logging"] == expected
    serialized = json.dumps(snapshot)
    assert GROUP not in serialized and "synthetic-secret-member" not in serialized


@pytest.mark.parametrize(
    "value,kind",
    [
        (None, "null"),
        (True, "malformed"),
        ("not-a-date", "malformed"),
        (CREATED, "valid"),
    ],
)
def test_observed_timestamp_is_typed_and_never_copied_raw(value, kind):
    snapshot = recovery.observation_snapshot({"createdAt": value}, REGION)
    assert snapshot["createdAtKind"] == kind
    assert ("createdAt" in snapshot) == (kind == "valid")
    if kind == "valid":
        assert snapshot["createdAt"] == CREATED.timestamp()


@pytest.mark.parametrize(
    "error,code",
    [
        (RuntimeError("synthetic-private-diagnostic"), "observation_failed"),
        (recovery.Refused("synthetic-private-diagnostic"), "observation_failed"),
        (recovery.Refused("pagination_token"), "pagination_token"),
        (subprocess.TimeoutExpired(["synthetic-private-command"], 1), "deadline"),
    ],
)
def test_observation_error_retains_only_fixed_categories(error, code):
    value = report()
    recovery.observation_error(value, "versions", error)
    assert value["sections"]["versions"] == "incomplete"
    assert value["errors"] == [{"section": "versions", "code": code}]
    assert "synthetic-private" not in json.dumps(value)
    assert recovery.validate_observation(value) is value


@pytest.mark.parametrize("existing", [999, 1000])
def test_observation_error_stays_within_report_error_bound(existing):
    value = report()
    value["errors"] = [{"section": "images", "code": "api_failed"}] * existing
    recovery.observation_error(value, "versions", RuntimeError("private-error"))
    assert value["sections"]["versions"] == "incomplete"
    assert len(value["errors"]) == 1000
    assert value["errors"][-1]["section"] == (
        "versions" if existing == 999 else "images"
    )
    assert recovery.validate_observation(value) is value


def test_missing_and_unknown_state_and_availability_are_not_inferred():
    assert recovery.observation_snapshot({}, REGION) == {
        "state": "missing",
        "availability": "missing",
        "createdAtKind": "missing",
        "logging": {"kind": "missing"},
    }
    for field, target in (("state", "state"), ("status", "availability")):
        assert recovery.observation_snapshot({field: None}, REGION)[target] == "null"
        assert (
            recovery.observation_snapshot({field: "synthetic-secret-state"}, REGION)[
                target
            ]
            == "malformed"
        )
    snapshot = recovery.observation_snapshot(
        {"state": "SUCCESSFUL", "status": "INACTIVE"}, REGION
    )
    assert snapshot["state"] == "SUCCESSFUL" and snapshot["availability"] == "INACTIVE"


@pytest.mark.parametrize(
    "operation",
    [
        "delete_microvm_image",
        "delete_log_group",
        "run_microvm",
        "update_microvm_image_version",
        "get_role",
        "unknown",
    ],
)
def test_read_only_api_rejects_non_observation_operations_before_provider_call(
    census, operation
):
    client = Mock()
    with pytest.raises(recovery.Refused):
        census.api.call(client, operation, secret="synthetic-private-argument")
    getattr(client, operation).assert_not_called()


@pytest.mark.parametrize(
    "operation",
    [
        "list_microvm_images",
        "get_microvm_image",
        "list_microvms",
        "describe_log_groups",
        "list_microvm_image_versions",
        "get_microvm_image_version",
    ],
)
def test_read_only_api_allows_each_required_observation_operation(operation):
    client = Mock()
    getattr(client, operation).return_value = {"items": []}
    api = recovery.ReadOnlyApi(client, client, clock=lambda: 0)
    assert api.call(client, operation, imageIdentifier="synthetic-id") == {"items": []}
    getattr(client, operation).assert_called_once_with(imageIdentifier="synthetic-id")


def test_observation_reads_both_version_responses_and_never_cleanup_paths(
    census, monkeypatch
):
    forbidden = Mock(side_effect=AssertionError("cleanup path reached"))
    for name in ("recover", "selected", "settled_selection", "independent_absence"):
        monkeypatch.setattr(recovery, name, forbidden)
    monkeypatch.setattr(recovery.Api, "configured_dependencies", forbidden)
    monkeypatch.setattr(recovery.Api, "observe_version", forbidden)
    value = report()
    recovery.observe(census.manifest, census.owned, census.api, value)
    assert value["sections"] == {
        "independentVerifier": "completed",
        "images": "completed",
        "microvms": "completed",
        "logGroups": "completed",
        "versions": "completed",
    }
    assert census.events[0:2] == ["verifier", "list_microvm_images"]
    assert value["status"] == "completed"
    assert len(value["images"]) == len(value["versions"]) == 1
    assert value["images"][0]["approved"] is True
    row = value["versions"][0]
    assert row["imageSha256"] == recovery.identity("image", REGION, NAME)
    assert row["versionSha256"] == recovery.digest(VERSION.encode())
    assert row["getOutcome"] == "observed"
    assert row["listed"] == row["observed"]
    assert all(
        row[name] is True
        for name in (
            "identityMatches",
            "stateMatches",
            "createdAtMatches",
            "loggingMatches",
            "availabilityMatches",
        )
    )
    census.plane.get_microvm_image_version.assert_called_once_with(
        imageIdentifier=ARN, imageVersion=VERSION
    )
    forbidden.assert_not_called()
    census.plane.delete_microvm_image.assert_not_called()
    census.logs.delete_log_group.assert_not_called()
    serialized = json.dumps(value)
    assert all(
        secret not in serialized
        for secret in (NAME, ARN, VERSION, GROUP, "000000000000")
    )


def test_observation_does_not_drop_deleted_or_unknown_state_versions(census):
    records = [
        {
            **census.version,
            "imageVersion": "deleted",
            "state": "DELETED",
            "logging": None,
        },
        {
            **census.version,
            "imageVersion": "unknown",
            "state": "synthetic-new-state",
            "logging": {"SDK_UNKNOWN_MEMBER": {"name": "private-union"}},
        },
    ]
    census.plane.list_microvm_image_versions.return_value = {"items": records}
    census.plane.get_microvm_image_version.side_effect = lambda **kwargs: next(
        copy.deepcopy(row)
        for row in records
        if row["imageVersion"] == kwargs["imageVersion"]
    )
    value = report()
    recovery.observe(census.manifest, census.owned, census.api, value)
    assert len(value["versions"]) == 2
    assert {row["listed"]["state"] for row in value["versions"]} == {
        "DELETED",
        "malformed",
    }
    assert {row["listed"]["logging"]["kind"] for row in value["versions"]} == {
        "null",
        "unknownMember",
    }
    assert census.plane.get_microvm_image_version.call_count == 2


@pytest.mark.parametrize(
    "failure", ["missing_page", "repeated_token", "duplicate", "provider_error"]
)
def test_incomplete_version_scan_preserves_other_sections_and_marks_failure(
    census, failure
):
    if failure == "missing_page":
        census.plane.list_microvm_image_versions.return_value = {}
    elif failure == "repeated_token":
        census.plane.list_microvm_image_versions.return_value = {
            "items": [],
            "nextToken": "cycle",
        }
    elif failure == "duplicate":
        census.plane.list_microvm_image_versions.return_value = {
            "items": [census.version, census.version]
        }
    else:
        census.plane.list_microvm_image_versions.side_effect = RuntimeError(
            "synthetic-provider-secret"
        )
    value = report()
    recovery.observe(census.manifest, census.owned, census.api, value)
    assert value["sections"]["versions"] == "incomplete"
    assert value["sections"]["images"] == value["sections"]["logGroups"] == "completed"
    assert value["images"] and value["logGroups"]
    assert value["errors"] and all(
        row["section"] == "versions" for row in value["errors"]
    )
    assert "synthetic-provider-secret" not in json.dumps(value)
    census.logs.delete_log_group.assert_not_called()


def test_complete_version_pagination_reads_every_exact_pair(census):
    second = {**census.version, "imageVersion": "later-version"}
    census.plane.list_microvm_image_versions.side_effect = [
        {"items": [census.version], "nextToken": "later"},
        {"items": [second]},
    ]
    census.plane.get_microvm_image_version.side_effect = [census.version, second]
    value = report()
    recovery.observe(census.manifest, census.owned, census.api, value)
    assert census.plane.list_microvm_image_versions.call_args_list[-1].kwargs == {
        "imageIdentifier": ARN,
        "nextToken": "later",
    }
    assert {row["versionSha256"] for row in value["versions"]} == {
        recovery.digest(VERSION.encode()),
        recovery.digest(b"later-version"),
    }
    assert [
        call.kwargs["imageVersion"]
        for call in census.plane.get_microvm_image_version.call_args_list
    ] == [VERSION, "later-version"]


@pytest.mark.parametrize(
    "section,operation,field",
    [
        ("images", "list_microvm_images", "items"),
        ("microvms", "list_microvms", "items"),
        ("logGroups", "describe_log_groups", "logGroups"),
    ],
)
@pytest.mark.parametrize("failure", ["missing_page", "duplicate", "provider_error"])
def test_each_regional_census_section_fails_closed_independently(
    census, section, operation, field, failure
):
    method = getattr(census.logs if section == "logGroups" else census.plane, operation)
    row = (
        census.image
        if section == "images"
        else {"microvmId": "private-vm", "state": "RUNNING"}
        if section == "microvms"
        else {"logGroupName": GROUP}
    )
    if failure == "missing_page":
        method.return_value = {}
    elif failure == "duplicate":
        method.return_value = {field: [row, row]}
    else:
        method.side_effect = RuntimeError("synthetic-private-provider-error")
    value = report()
    recovery.observe(census.manifest, census.owned, census.api, value)
    assert value["status"] == "incomplete"
    assert value["sections"][section] == "incomplete"
    assert value["sections"]["independentVerifier"] == "completed"
    assert any(error["section"] == section for error in value["errors"])
    assert "synthetic-private-provider-error" not in json.dumps(value)


def test_later_version_page_error_retains_the_previous_exact_pair(census):
    census.plane.list_microvm_image_versions.side_effect = [
        {"items": [census.version], "nextToken": "later"},
        RuntimeError("private-late-error"),
    ]
    value = report()
    recovery.observe(census.manifest, census.owned, census.api, value)
    assert value["status"] == "incomplete"
    assert value["sections"]["versions"] == "incomplete"
    assert len(value["versions"]) == 1
    assert value["versions"][0]["getOutcome"] == "observed"
    assert value["versions"][0]["versionSha256"] == recovery.digest(VERSION.encode())
    assert value["independentVerifier"]["exitCode"] == 0


@pytest.mark.parametrize(
    "code,outcome",
    [
        ("ResourceNotFoundException", "not_found"),
        ("AccessDeniedException", "api_error"),
    ],
)
def test_exact_version_read_failure_retains_listed_snapshot_and_uncertain_matches(
    census, code, outcome
):
    error = RuntimeError("synthetic-provider-diagnostic")
    error.response = {"Error": {"Code": code}}
    census.plane.get_microvm_image_version.side_effect = error
    value = report()
    recovery.observe(census.manifest, census.owned, census.api, value)
    assert value["status"] == "incomplete"
    row = value["versions"][0]
    assert row["getOutcome"] == outcome
    assert "observed" not in row
    assert row["listed"]["logging"]["kind"] == "cloudWatch"
    assert all(
        row[key] is None
        for key in (
            "identityMatches",
            "stateMatches",
            "createdAtMatches",
            "loggingMatches",
            "availabilityMatches",
        )
    )
    assert "synthetic-provider-diagnostic" not in json.dumps(value)


def test_failed_independent_verifier_still_collects_census_without_claiming_completion(
    census, monkeypatch
):
    def broken(*args, **kwargs):
        census.events.append("verifier")
        return subprocess.CompletedProcess(
            args[0], 0, "private-unrecognized-output", "private-error"
        )

    monkeypatch.setattr(recovery.subprocess, "run", broken)
    value = report()
    recovery.observe(census.manifest, census.owned, census.api, value)
    assert value["status"] == "incomplete"
    assert value["sections"]["independentVerifier"] == "incomplete"
    assert value["sections"]["images"] == value["sections"]["versions"] == "completed"
    assert "independentVerifier" not in value
    assert "private-unrecognized-output" not in json.dumps(value)
    assert "private-error" not in json.dumps(value)


@pytest.mark.parametrize("error", [InterruptedError, KeyboardInterrupt])
@pytest.mark.parametrize("phase", ["verifier", "version_read"])
def test_observation_interruption_stops_reads_and_retains_prior_evidence(
    census, monkeypatch, error, phase
):
    if phase == "verifier":

        def interrupted(*args, **kwargs):
            census.events.append("verifier")
            raise error("synthetic-interruption")

        monkeypatch.setattr(recovery.subprocess, "run", interrupted)
    else:
        census.plane.list_microvm_image_versions.return_value = {
            "items": [census.version, {**census.version, "imageVersion": "later"}]
        }
        census.plane.get_microvm_image_version.side_effect = error(
            "synthetic-interruption"
        )
    value = report()
    with pytest.raises(error):
        recovery.observe(census.manifest, census.owned, census.api, value)
    assert value["status"] == "incomplete"
    if phase == "verifier":
        assert census.events == ["verifier"]
        assert value["sections"]["independentVerifier"] == "incomplete"
        assert value["images"] == value["versions"] == []
        census.plane.list_microvm_images.assert_not_called()
        census.plane.list_microvms.assert_not_called()
        census.logs.describe_log_groups.assert_not_called()
    else:
        assert value["sections"]["independentVerifier"] == "completed"
        assert value["sections"]["versions"] == "incomplete"
        assert len(value["images"]) == len(value["versions"]) == 1
        assert value["images"][0]["getOutcome"] == "observed"
        census.plane.get_microvm_image_version.assert_called_once_with(
            imageIdentifier=ARN, imageVersion=VERSION
        )
        assert census.events[-1] == "get_microvm_image_version"
    assert "synthetic-interruption" not in json.dumps(value)
    assert recovery.validate_observation(value) is value


@pytest.mark.parametrize("count", [1001, 9000])
def test_oversize_independent_verifier_retains_bounded_census_and_incomplete_receipt(
    census, monkeypatch, tmp_path, count
):
    stdout = "\n".join(
        "UNCLASSIFIED (not deleted, not provably ours): log group "
        f"/aws/lambda-microvms/synthetic-unclassified-{index}"
        for index in range(count)
    )
    stdout += (
        f"\naccount {REGION}: clean of everything this project can name; {count} "
        "log group(s) under /aws/lambda-microvms/ belong to no known prefix and no "
        "ledger record and were left alone (listed above; --strict fails on them)\n"
    )
    parsed = recovery.captured_verifier(
        subprocess.CompletedProcess([], 0, stdout, ""), REGION
    )
    assert len(parsed["resources"]) == count
    assert len(parsed["resources"]) > recovery.OBSERVATION_MAX_ROWS
    if count == 9000:
        assert len(json.dumps(parsed).encode()) > recovery.OBSERVATION_MAX_BYTES

    def verifier(command, **kwargs):
        census.events.append("verifier")
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(recovery.subprocess, "run", verifier)
    value = report()
    recovery.observe(census.manifest, census.owned, census.api, value)
    assert value["status"] == "incomplete"
    assert value["sections"]["independentVerifier"] == "incomplete"
    assert "independentVerifier" not in value
    assert value["errors"] == [
        {"section": "independentVerifier", "code": "observation_limit"}
    ]
    assert all(
        value["sections"][section] == "completed"
        for section in ("images", "microvms", "logGroups", "versions")
    )
    assert (
        len(value["images"]) == len(value["versions"]) == len(value["logGroups"]) == 1
    )
    assert len(json.dumps(value).encode()) < recovery.OBSERVATION_MAX_BYTES
    assert recovery.validate_observation(value) is value
    path = tmp_path / "incomplete-observation.json"
    path.write_text(json.dumps(value))
    assert CREDENTIALS["read_receipt"](path, observe_only=True) == value
    assert CREDENTIALS["failure_receipt"](path, observe_only=True) == value


def test_observation_receipt_cannot_be_accepted_as_cleanup(census):
    value = report()
    recovery.observe(census.manifest, census.owned, census.api, value)
    assert recovery.validate_observation(value) is value
    with pytest.raises(recovery.Refused):
        recovery.validate_receipt(value)
    legacy = {
        "kind": "live-recovery",
        "status": "passed",
        "conformance_verified": False,
    }
    assert recovery.validate_receipt(legacy) is legacy
    with pytest.raises(recovery.Refused):
        recovery.validate_observation(legacy)


@pytest.mark.parametrize(
    "field,value",
    [
        ("kind", "live-recovery"),
        ("schemaVersion", True),
        ("status", "passed"),
        ("recoveryAccepted", True),
        ("mutationsPerformed", True),
        ("conformance_verified", True),
        ("private-field", "raw-resource"),
    ],
)
def test_observation_schema_rejects_wrong_kind_success_claims_and_unknown_fields(
    census, field, value
):
    original = report()
    recovery.observe(census.manifest, census.owned, census.api, original)
    original[field] = value
    with pytest.raises(recovery.Refused, match="^receipt_schema$"):
        recovery.validate_observation(original)


@pytest.mark.parametrize("invalid", ["not-a-hash", "a" * 63, "g" * 64, None, True])
def test_observation_schema_rejects_malformed_identity_hash(census, invalid):
    value = report()
    recovery.observe(census.manifest, census.owned, census.api, value)
    value["images"][0]["imageSha256"] = invalid
    with pytest.raises(recovery.Refused, match="^receipt_schema$"):
        recovery.validate_observation(value)


@pytest.mark.parametrize("target", ["images", "versions"])
@pytest.mark.parametrize("field", ["listed", "observed"])
@pytest.mark.parametrize(
    "state,availability,timestamp,logging",
    [
        ("missing", "missing", "missing", {"kind": "missing"}),
        ("null", "null", "null", {"kind": "null"}),
        ("malformed", "malformed", "malformed", {"kind": "malformed"}),
        ("known", "ACTIVE", "valid", {"kind": "disabled"}),
        (
            "known",
            "INACTIVE",
            "valid",
            {"kind": "cloudWatch", "logGroupSha256": "d" * 64},
        ),
        ("known", "ACTIVE", "valid", {"kind": "unknownMember"}),
    ],
)
def test_observation_schema_accepts_exact_snapshot_classifications(
    census, target, field, state, availability, timestamp, logging
):
    value = report()
    recovery.observe(census.manifest, census.owned, census.api, value)
    if state == "known":
        state = "CREATED" if target == "images" else "SUCCESSFUL"
    snapshot = {
        "state": state,
        "availability": availability,
        "createdAtKind": timestamp,
        "logging": logging,
    }
    if timestamp == "valid":
        snapshot["createdAt"] = 0
    value[target][0][field] = snapshot
    assert recovery.validate_observation(value) is value


@pytest.mark.parametrize(
    "invalid", [-1, -0.1, float("nan"), float("inf"), float("-inf"), "1", True, None]
)
def test_observation_schema_refuses_invalid_snapshot_timestamp(census, invalid):
    value = report()
    recovery.observe(census.manifest, census.owned, census.api, value)
    value["versions"][0]["observed"]["createdAt"] = invalid
    with pytest.raises(recovery.Refused, match="^receipt_schema$"):
        recovery.validate_observation(value)


@pytest.mark.parametrize(
    "invalid",
    [
        None,
        [],
        {},
        {"kind": "cloudWatch"},
        {"kind": "cloudWatch", "logGroupSha256": "invalid"},
        {"kind": "cloudWatch", "logGroupSha256": None},
        {"kind": "disabled", "logGroupSha256": "d" * 64},
        {"kind": "unknown"},
    ],
)
def test_observation_schema_refuses_malformed_logging(census, invalid):
    value = report()
    recovery.observe(census.manifest, census.owned, census.api, value)
    value["versions"][0]["observed"]["logging"] = invalid
    with pytest.raises(recovery.Refused, match="^receipt_schema$"):
        recovery.validate_observation(value)


def test_observation_schema_accepts_required_only_incomplete_report():
    value = {
        "kind": "live-recovery-observation",
        "schemaVersion": 1,
        "conformance_verified": False,
        "recoveryAccepted": False,
        "mutationsPerformed": False,
        "status": "incomplete",
    }
    assert recovery.validate_observation(value) is value


def test_observation_schema_accepts_every_optional_top_level_field(census):
    value = report()
    recovery.observe(census.manifest, census.owned, census.api, value)
    value.update(status="incomplete", reason="interrupted_or_invalid_input")
    assert recovery.validate_observation(value) is value


@pytest.mark.parametrize(
    "field,invalid",
    [
        ("sourceRun", False),
        ("sourceRun", 0),
        ("sourceSha", "a" * 39),
        ("region", "invalid"),
        ("manifestSha256", "g" * 64),
        ("parserVersion", True),
        ("sourceLogRepresentation", "unrecognized"),
    ],
)
def test_observation_schema_validates_shared_bindings(census, field, invalid):
    value = report()
    recovery.observe(census.manifest, census.owned, census.api, value)
    value[field] = invalid
    with pytest.raises(recovery.Refused, match="^receipt_schema$"):
        recovery.validate_observation(value)


@pytest.mark.parametrize(
    "field,invalid",
    [
        ("summary_present", 1),
        ("summary_present", False),
        ("leakCount", True),
        ("leakCount", -1),
        ("leakCount", 1),
        ("exitCode", None),
        ("resources", None),
        (
            "resources",
            [{"kind": "log-group", "sha256": "bad", "category": "unclassified"}],
        ),
        (
            "resources",
            [{"kind": "invalid", "sha256": "d" * 64, "category": "unclassified"}],
        ),
        (
            "resources",
            [{"kind": "log-group", "sha256": "d" * 64, "category": "invalid"}],
        ),
    ],
)
def test_observation_schema_validates_independent_verifier(census, field, invalid):
    value = report()
    recovery.observe(census.manifest, census.owned, census.api, value)
    value["independentVerifier"][field] = invalid
    with pytest.raises(recovery.Refused, match="^receipt_schema$"):
        recovery.validate_observation(value)


@pytest.mark.parametrize("count", [999, 1000, 1001])
def test_observation_schema_enforces_independent_verifier_row_boundary(census, count):
    value = report()
    recovery.observe(census.manifest, census.owned, census.api, value)
    value["independentVerifier"]["resources"] = [
        {"kind": "log-group", "sha256": f"{index:064x}", "category": "unclassified"}
        for index in range(count)
    ]
    if count <= 1000:
        assert recovery.validate_observation(value) is value
    else:
        with pytest.raises(recovery.Refused, match="^receipt_schema$"):
            recovery.validate_observation(value)


@pytest.mark.parametrize(
    "status,reason", [("completed", "api_failed"), ("incomplete", "unrecognized")]
)
def test_observation_schema_refuses_invalid_reason_status_pairs(census, status, reason):
    value = report()
    recovery.observe(census.manifest, census.owned, census.api, value)
    value.update(status=status, reason=reason)
    with pytest.raises(recovery.Refused, match="^receipt_schema$"):
        recovery.validate_observation(value)


@pytest.mark.parametrize(
    "corruption", ["missing", "extra", "value", "verifier", "unfinished"]
)
def test_observation_schema_refuses_inconsistent_sections(census, corruption):
    value = report()
    recovery.observe(census.manifest, census.owned, census.api, value)
    if corruption == "missing":
        del value["sections"]["versions"]
    elif corruption == "extra":
        value["sections"]["unknown"] = "completed"
    elif corruption == "value":
        value["sections"]["versions"] = "invalid"
    elif corruption == "verifier":
        value["status"] = "incomplete"
        value["sections"]["independentVerifier"] = "not_run"
    else:
        value["sections"]["versions"] = "incomplete"
    with pytest.raises(recovery.Refused, match="^receipt_schema$"):
        recovery.validate_observation(value)


@pytest.mark.parametrize(
    "target,key,value",
    [
        ("image", "imageArn", ARN),
        ("version", "imageVersion", VERSION),
        ("snapshot", "buildRoleArn", "arn:aws:iam::000000000000:role/private-role"),
        ("logging", "logGroupName", GROUP),
    ],
)
def test_observation_schema_rejects_raw_identifier_escape(census, target, key, value):
    original = report()
    recovery.observe(census.manifest, census.owned, census.api, original)
    node = original["images"][0] if target == "image" else original["versions"][0]
    if target in {"snapshot", "logging"}:
        node = node["observed"]
    if target == "logging":
        node = node["logging"]
    node[key] = value
    with pytest.raises(recovery.Refused):
        recovery.validate_observation(original)


def test_empty_version_lists_still_prove_coverage_for_every_regional_image(census):
    other = {**census.image, "imageArn": ARN + "-other", "name": NAME + "-other"}
    census.plane.list_microvm_images.return_value = {"items": [census.image, other]}
    census.plane.get_microvm_image.side_effect = [census.image, other]
    census.plane.list_microvm_image_versions.return_value = {"items": []}
    value = report()
    recovery.observe(census.manifest, census.owned, census.api, value)
    assert {
        call.kwargs["imageIdentifier"]
        for call in census.plane.list_microvm_image_versions.call_args_list
    } == {ARN, ARN + "-other"}
    assert value["status"] == "completed"
    assert value["versions"] == []
    assert value["versionCoverage"] == [
        {
            "imageSha256": recovery.identity("image", REGION, row["name"]),
            "imageArnSha256": recovery.digest(row["imageArn"].encode()),
            "listedCount": 0,
            "getCount": 0,
            "observedCount": 0,
            "notFoundCount": 0,
            "apiErrorCount": 0,
            "paginationComplete": True,
        }
        for row in (census.image, other)
    ]
    assert recovery.validate_observation(value) is value


@pytest.mark.parametrize("failure", [False, True])
def test_version_coverage_counts_every_exact_get_and_refuses_false_completion(
    census, failure
):
    if failure:
        error = RuntimeError("private-provider-error")
        error.response = {"Error": {"Code": "ResourceNotFoundException"}}
        census.plane.get_microvm_image_version.side_effect = error
    value = report()
    recovery.observe(census.manifest, census.owned, census.api, value)
    coverage = value["versionCoverage"]
    assert coverage == [
        {
            "imageSha256": recovery.identity("image", REGION, NAME),
            "imageArnSha256": recovery.digest(ARN.encode()),
            "listedCount": 1,
            "getCount": 1,
            "observedCount": int(not failure),
            "notFoundCount": int(failure),
            "apiErrorCount": 0,
            "paginationComplete": True,
        }
    ]
    assert recovery.validate_observation(value) is value
    forged = copy.deepcopy(value)
    forged["versionCoverage"][0]["getCount"] = 0
    with pytest.raises(recovery.Refused):
        recovery.validate_observation(forged)


@pytest.mark.parametrize(
    "corruption",
    [
        "pagination",
        "missing_coverage",
        "missing_version",
        "version_arn",
        "orphan_version",
    ],
)
def test_completed_observation_requires_full_reconciled_version_coverage(
    census, corruption
):
    if corruption == "missing_coverage":
        census.plane.list_microvm_image_versions.return_value = {"items": []}
    value = report()
    recovery.observe(census.manifest, census.owned, census.api, value)
    assert recovery.validate_observation(value) is value
    if corruption == "pagination":
        value["versionCoverage"][0]["paginationComplete"] = False
    elif corruption == "missing_coverage":
        value["versionCoverage"] = []
    elif corruption == "missing_version":
        value["versions"] = []
    elif corruption == "version_arn":
        value["versions"][0]["imageArnSha256"] = "c" * 64
    else:
        value["versions"][0]["imageSha256"] = "c" * 64
    with pytest.raises(recovery.Refused):
        recovery.validate_observation(value)


@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize("corruption", ["missing", "mismatch"])
def test_completed_coverage_is_bound_to_listed_image_arn(census, empty, corruption):
    if empty:
        census.plane.list_microvm_image_versions.return_value = {"items": []}
    value = report()
    recovery.observe(census.manifest, census.owned, census.api, value)
    assert recovery.validate_observation(value) is value
    if corruption == "missing":
        del value["images"][0]["imageArnSha256"]
    else:
        value["images"][0]["imageArnSha256"] = "c" * 64
    with pytest.raises(recovery.Refused):
        recovery.validate_observation(value)


def test_incomplete_listed_image_requires_arn_hash(census):
    value = report()
    recovery.observe(census.manifest, census.owned, census.api, value)
    value["status"] = "incomplete"
    del value["images"][0]["imageArnSha256"]
    with pytest.raises(recovery.Refused):
        recovery.validate_observation(value)


@pytest.mark.parametrize("snapshot", ["listed", "observed"])
def test_version_snapshot_rejects_vm_state(census, snapshot):
    value = report()
    recovery.observe(census.manifest, census.owned, census.api, value)
    value["versions"][0][snapshot]["state"] = "RUNNING"
    with pytest.raises(recovery.Refused):
        recovery.validate_observation(value)


@pytest.mark.parametrize(
    "corruption", ["missing_request", "missing_match", "unapproved"]
)
def test_source_identity_requires_paired_fields_and_approved_image(census, corruption):
    value = report()
    recovery.observe(census.manifest, census.owned, census.api, value)
    row = value["images"][0]
    if corruption == "missing_request":
        del row["requestedArnSha256"]
    elif corruption == "missing_match":
        del row["sourceIdentityMatches"]
    else:
        row["approved"] = False
    with pytest.raises(recovery.Refused):
        recovery.validate_observation(value)


def test_observed_disagreement_is_evidence_not_a_recovery_default(census):
    census.plane.get_microvm_image_version.return_value = {
        **census.version,
        "state": "FAILED",
        "status": "INACTIVE",
        "logging": {"disabled": {}},
    }
    value = report()
    recovery.observe(census.manifest, census.owned, census.api, value)
    row = value["versions"][0]
    assert value["status"] == "completed"
    assert (
        row["stateMatches"]
        is row["availabilityMatches"]
        is row["loggingMatches"]
        is False
    )
    assert row["listed"]["logging"]["kind"] == "cloudWatch"
    assert row["observed"]["logging"]["kind"] == "disabled"
    assert value["recoveryAccepted"] is value["mutationsPerformed"] is False


def test_core_observe_mode_runs_verifier_and_read_only_api_without_cleanup(
    census, monkeypatch, tmp_path
):
    manifest = {**census.manifest, "source_run_id": 9, "source_sha": "a" * 40}
    monkeypatch.setattr(recovery, "load_manifest", Mock(return_value=manifest))
    monkeypatch.setattr(recovery, "provenance", Mock(return_value=census.owned))
    real_runner = recovery.subprocess.run

    def runner(command, **kwargs):
        if command == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(command, 0, "d" * 40 + "\n", "")
        return real_runner(command, **kwargs)

    monkeypatch.setattr(recovery.subprocess, "run", runner)
    readonly = Mock(return_value=census.api)
    monkeypatch.setattr(recovery, "ReadOnlyApi", readonly)
    forbidden = Mock(side_effect=AssertionError("cleanup authority reached"))
    for name in (
        "Api",
        "recover",
        "selected",
        "settled_selection",
        "configured_dependencies",
        "independent_absence",
        "observe_duration",
    ):
        if hasattr(recovery, name):
            monkeypatch.setattr(recovery, name, forbidden)
    session = Mock()
    session.client.side_effect = lambda name, **kwargs: {
        "lambda-microvms": census.plane,
        "logs": census.logs,
    }[name]
    monkeypatch.setitem(
        sys.modules, "boto3", SimpleNamespace(Session=Mock(return_value=session))
    )
    monkeypatch.setitem(sys.modules, "botocore", SimpleNamespace())
    monkeypatch.setitem(
        sys.modules,
        "botocore.config",
        SimpleNamespace(Config=Mock(return_value="configuration")),
    )
    monkeypatch.setattr(recovery, "time", SimpleNamespace(monotonic=lambda: 0))
    monkeypatch.setenv("GITHUB_SHA", "d" * 40)
    monkeypatch.setenv("GITHUB_REPOSITORY", "synthetic/repository")
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.delenv("RECOVERY_DEADLINE_MONOTONIC", raising=False)
    path = tmp_path / "observation.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "recover-live.py",
            "observe",
            "--source-run",
            "9",
            "--manifest-sha256",
            "b" * 64,
            "--report",
            str(path),
        ],
    )
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        assert recovery.main() == 0
    value = json.loads(path.read_text())
    assert json.loads(stdout.getvalue()) == value
    assert recovery.validate_observation(value) is value
    assert (
        value["status"] == "completed" and value["kind"] == "live-recovery-observation"
    )
    assert value["recoveryAccepted"] is value["mutationsPerformed"] is False
    assert [call.args[0] for call in session.client.call_args_list] == [
        "lambda-microvms",
        "logs",
    ]
    readonly.assert_called_once_with(
        census.plane, census.logs, deadline=recovery.TOTAL_SECONDS
    )
    forbidden.assert_not_called()
    assert census.events[0:2] == ["verifier", "list_microvm_images"]


@pytest.mark.parametrize("failure", ["refused", "unexpected", "interrupted"])
def test_core_observation_preflight_failure_retains_valid_incomplete_receipt(
    monkeypatch, tmp_path, failure
):
    error = {
        "refused": recovery.Refused("invalid_manifest_digest"),
        "unexpected": RuntimeError("synthetic-private-diagnostic"),
        "interrupted": KeyboardInterrupt("synthetic-private-diagnostic"),
    }[failure]
    monkeypatch.setattr(recovery, "load_manifest", Mock(side_effect=error))
    network = Mock(side_effect=AssertionError("preflight failure reached provider"))
    monkeypatch.setattr(recovery.subprocess, "run", network)
    monkeypatch.delenv("RECOVERY_DEADLINE_MONOTONIC", raising=False)
    path = tmp_path / "observation.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "recover-live.py",
            "observe",
            "--source-run",
            "9",
            "--manifest-sha256",
            "b" * 64,
            "--report",
            str(path),
        ],
    )
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        assert recovery.main() == 1
    value = json.loads(path.read_text())
    assert json.loads(stdout.getvalue()) == value
    assert value["status"] == "incomplete"
    assert value["kind"] == "live-recovery-observation"
    assert value["recoveryAccepted"] is value["mutationsPerformed"] is False
    assert recovery.validate_observation(value) is value
    assert "synthetic-private-diagnostic" not in stdout.getvalue()
    network.assert_not_called()


def test_full_list_inventory_and_source_image_identity_are_kept_distinct(census):
    replacement = {**census.image, "imageArn": ARN + "-replacement"}
    listed_version = {**census.version, "imageArn": replacement["imageArn"]}
    census.plane.list_microvm_images.return_value = {"items": [replacement]}
    census.plane.list_microvm_image_versions.return_value = {"items": [listed_version]}
    census.plane.get_microvm_image_version.return_value = listed_version
    value = report()
    recovery.observe(census.manifest, census.owned, census.api, value)
    census.plane.get_microvm_image.assert_called_once_with(imageIdentifier=ARN)
    census.plane.list_microvm_image_versions.assert_called_once_with(
        imageIdentifier=replacement["imageArn"]
    )
    row = value["images"][0]
    assert row["requestedArnSha256"] == recovery.digest(ARN.encode())
    assert row["imageArnSha256"] == recovery.digest(replacement["imageArn"].encode())
    assert row["sourceIdentityMatches"] is True
    assert row["identityMatches"] is False
    assert value["versionCoverage"][0]["imageArnSha256"] == recovery.digest(
        replacement["imageArn"].encode()
    )


def test_conflicting_source_arn_keeps_listed_image_and_versions_in_incomplete_census(
    census,
):
    listed = {**census.image, "name": NAME + "-other"}
    census.plane.list_microvm_images.return_value = {"items": [listed]}
    census.plane.get_microvm_image.return_value = listed
    value = report()
    recovery.observe(census.manifest, census.owned, census.api, value)
    assert value["status"] == "incomplete"
    assert value["sections"]["images"] == "incomplete"
    assert value["sections"]["versions"] == "incomplete"
    assert value["sections"]["independentVerifier"] == "completed"
    assert value["errors"] == [
        {
            "section": "images",
            "code": "observation_identity",
            "imageSha256": recovery.identity("image", REGION, NAME),
        }
    ]
    assert len(value["images"]) == len(value["versionCoverage"]) == 1
    assert value["images"][0]["imageSha256"] == recovery.identity(
        "image", REGION, listed["name"]
    )
    assert value["images"][0]["listed"] == recovery.observation_snapshot(
        listed, REGION, recovery.IMAGE_STATES
    )
    assert value["images"][0]["approved"] is False
    assert (
        value["versionCoverage"][0]["imageSha256"] == value["images"][0]["imageSha256"]
    )
    assert len(value["versions"]) == 1
    assert value["versions"][0]["versionSha256"] == recovery.digest(VERSION.encode())
    census.plane.list_microvm_image_versions.assert_called_once_with(
        imageIdentifier=ARN
    )
    census.plane.get_microvm_image_version.assert_called_once_with(
        imageIdentifier=ARN, imageVersion=VERSION
    )
    assert recovery.validate_observation(value) is value


def test_private_environment_values_never_reach_independent_verifier(
    census, monkeypatch
):
    captured = []
    original = recovery.subprocess.run

    def runner(command, **kwargs):
        captured.append(kwargs["env"])
        return original(command, **kwargs)

    monkeypatch.setattr(recovery.subprocess, "run", runner)
    monkeypatch.setenv("GH_TOKEN", "private-github-token")
    monkeypatch.setenv("GITHUB_TOKEN", "private-second-token")
    monkeypatch.setenv("RECOVERY_ROLE_ARN", "private-role-value")
    monkeypatch.setenv("UNRELATED_SETTING", "retained")
    recovery.observe(census.manifest, census.owned, census.api, report())
    assert len(captured) == 1
    assert not {"GH_TOKEN", "GITHUB_TOKEN", "RECOVERY_ROLE_ARN"} & captured[0].keys()
    assert captured[0]["UNRELATED_SETTING"] == "retained"
