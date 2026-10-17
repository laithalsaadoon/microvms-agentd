# SPDX-License-Identifier: Apache-2.0
"""Exact incident log authority stays bound to its source and fresh dependencies."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

SPEC = importlib.util.spec_from_file_location(
    "tools.recover-live", Path(__file__).with_name("recover-live.py")
)
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)
FIXTURES = importlib.util.spec_from_file_location(
    "incident_log_fixtures", Path(__file__).with_name("test_recover_live.py")
)
fixtures = importlib.util.module_from_spec(FIXTURES)
FIXTURES.loader.exec_module(fixtures)
fixtures.recovery = recovery

REGION = fixtures.REGION
CREATED = fixtures.CREATED
EXPLICIT = "microvm-cli-slow-ready-01234567-abcdef012345"
EXPLICIT_GROUP = recovery.SERVICE_PREFIX + EXPLICIT
IMAGE_NAMES = (fixtures.SHARED, fixtures.CAPABILITY, EXPLICIT)
MISSING = object()


def arn(name):
    return f"arn:aws:lambda:{REGION}:000000000000:microvm-image:{name}"


# The older shared fixtures permit a shortened account component. This suite
# exercises exact derived addresses, so every source and response uses 12 digits.
fixtures.arn = arn


def key(kind, name):
    return recovery.identity(kind, REGION, name)


def source_log():
    return (
        fixtures.source_log()
        + "real AWS\tverify again after destroy\t2026-10-17T16:00:00Z "
        + f"LEAK image {EXPLICIT} in CREATE_FAILED\n"
    )


def manifest():
    value = fixtures.manifest()
    value["parser_version"] = 4
    fixtures.set_source_manifest(value, source_log())
    value["resources"].extend(
        [
            {
                "kind": "image",
                "proof": "explicit-incident-image",
                "sha256": key("image", EXPLICIT),
            },
            {
                "kind": "log-group",
                "proof": "incident-default-log",
                "sha256": key("log-group", EXPLICIT_GROUP),
            },
        ]
    )
    value["incident_recovery"] = {
        "policy": "incident-owned-logs-v1",
        "sourceRun": value["source_run_id"],
        "sourceSha": value["source_sha"],
        "region": REGION,
        "imageSha256": sorted(key("image", name) for name in IMAGE_NAMES),
        "historicalRecovery": {
            "runId": 12,
            "attempt": 1,
            "producerSha": "b" * 40,
            "manifestSha256": "c" * 64,
            "receiptSha256": "d" * 64,
        },
    }
    return value


def owned(value):
    github = fixtures.FakeGithub()
    github.log_value, steps, _ = fixtures.source_representation(source_log())
    github.values["actions/jobs/10"]["steps"] = steps
    return recovery.provenance(value, github, fixtures.environment())


def version(number, logging=MISSING):
    row = {
        "imageArn": arn(fixtures.UNKNOWN),
        "imageVersion": str(number),
        "state": "SUCCESSFUL",
        "createdAt": CREATED,
    }
    if logging is not MISSING:
        row["logging"] = copy.deepcopy(logging)
    return row


@pytest.fixture
def incident(monkeypatch):
    value = manifest()
    ownership = recovery.source_ownership(source_log(), REGION)
    ownership.update(
        {
            key("image", EXPLICIT): ("explicit-incident-image", EXPLICIT, None),
            key("log-group", EXPLICIT_GROUP): (
                "incident-default-log",
                EXPLICIT_GROUP,
                None,
            ),
        }
    )
    plane = fixtures.Plane()
    plane.images = {arn(fixtures.UNKNOWN): plane.images[arn(fixtures.UNKNOWN)]}
    logs = fixtures.Logs(plane.calls)
    logs.groups[EXPLICIT_GROUP] = {
        "logGroupName": EXPLICIT_GROUP,
        "creationTime": int(CREATED.timestamp() * 1000),
    }
    clock = fixtures.Clock()
    api = recovery.Api(plane, logs, clock=clock, sleep=clock.sleep)
    case = SimpleNamespace(
        manifest=value,
        ownership=ownership,
        plane=plane,
        logs=logs,
        api=api,
        clock=clock,
        events=[],
        records={"1": version(1), "2": version(2, {"disabled": {}})},
        listed=None,
        split_versions=False,
        split_vms=False,
        late_vms=[],
    )
    original_get = plane.get_microvm_image
    original_delete = logs.delete_log_group

    def get_image(imageIdentifier):
        try:
            row = original_get(imageIdentifier)
        except fixtures.Missing:
            case.events.append(("absent_image", imageIdentifier))
            raise
        case.events.append(("present_image", imageIdentifier))
        return row

    def list_versions(imageIdentifier, **kwargs):
        assert imageIdentifier == arn(fixtures.UNKNOWN)
        case.events.append(("list_versions", kwargs.get("nextToken")))
        rows = list((case.listed or case.records).values())
        if case.split_versions:
            if "nextToken" not in kwargs:
                return {"items": copy.deepcopy(rows[:1]), "nextToken": "later"}
            assert kwargs["nextToken"] == "later"
            return {"items": copy.deepcopy(rows[1:])}
        return {"items": copy.deepcopy(rows)}

    def get_version(imageIdentifier, imageVersion):
        assert imageIdentifier == arn(fixtures.UNKNOWN)
        case.events.append(("get_version", imageVersion))
        return copy.deepcopy(case.records[imageVersion])

    def list_vms(**kwargs):
        case.events.append(("list_vms", kwargs.get("nextToken")))
        if case.split_vms:
            if "nextToken" not in kwargs:
                return {"items": copy.deepcopy(plane.vms), "nextToken": "later"}
            assert kwargs["nextToken"] == "later"
            return {"items": copy.deepcopy(case.late_vms)}
        return {"items": copy.deepcopy(plane.vms)}

    def delete_log(logGroupName):
        case.events.append(("delete_log", logGroupName))
        return original_delete(logGroupName)

    monkeypatch.setattr(plane, "get_microvm_image", Mock(side_effect=get_image))
    monkeypatch.setattr(
        plane, "list_microvm_image_versions", Mock(side_effect=list_versions)
    )
    monkeypatch.setattr(
        plane, "get_microvm_image_version", Mock(side_effect=get_version)
    )
    monkeypatch.setattr(plane, "list_microvms", Mock(side_effect=list_vms))
    monkeypatch.setattr(logs, "delete_log_group", Mock(side_effect=delete_log))
    return case


def recover(case):
    report = {}
    recovery.recover(case.manifest, case.ownership, case.api, report)
    return report


def no_log_deletes(case):
    case.logs.delete_log_group.assert_not_called()


def test_parser_four_accepts_the_complete_reviewed_scope(tmp_path, monkeypatch):
    value = manifest()
    raw = json.dumps(value).encode()
    (tmp_path / "9.json").write_bytes(raw)
    monkeypatch.setattr(recovery, "MANIFESTS", tmp_path)
    assert recovery.load_manifest("9", recovery.digest(raw)) == value
    assert recovery.incident_policy(value) == value["incident_recovery"]


@pytest.mark.parametrize(
    "path,bad",
    [
        (("policy",), "other-policy"),
        (("sourceRun",), 10),
        (("sourceRun",), True),
        (("sourceSha",), "b" * 40),
        (("sourceSha",), "A" * 40),
        (("region",), "us-west-2"),
        (("imageSha256",), []),
        (("imageSha256",), ["e" * 64] * 3),
        (("imageSha256",), sorted(key("image", name) for name in IMAGE_NAMES[:2])),
        (("historicalRecovery", "runId"), 0),
        (("historicalRecovery", "runId"), True),
        (("historicalRecovery", "runId"), "12"),
        (("historicalRecovery", "attempt"), 0),
        (("historicalRecovery", "attempt"), True),
        (("historicalRecovery", "producerSha"), "b" * 39),
        (("historicalRecovery", "producerSha"), "B" * 40),
        (("historicalRecovery", "manifestSha256"), "c" * 63),
        (("historicalRecovery", "receiptSha256"), "D" * 64),
        (("historicalRecovery",), None),
    ],
)
def test_incident_policy_rejects_mismatched_or_malformed_scope(path, bad):
    value = manifest()
    parent = value["incident_recovery"]
    for name in path[:-1]:
        parent = parent[name]
    parent[path[-1]] = bad
    with pytest.raises(recovery.Refused, match="^incident_policy$"):
        recovery.incident_policy(value)


@pytest.mark.parametrize("part", ["scope", "history"])
def test_incident_policy_rejects_unreviewed_fields(part):
    value = manifest()
    parent = value["incident_recovery"]
    if part == "history":
        parent = parent["historicalRecovery"]
    parent["unreviewed"] = True
    with pytest.raises(recovery.Refused, match="^incident_policy$"):
        recovery.incident_policy(value)


def test_incident_policy_requires_the_current_parser_version():
    value = manifest()
    value["parser_version"] = 3
    with pytest.raises(recovery.Refused, match="^incident_policy$"):
        recovery.incident_policy(value)


@pytest.mark.parametrize(
    "field",
    ["policy", "sourceRun", "sourceSha", "region", "imageSha256", "historicalRecovery"],
)
def test_incident_policy_requires_every_scope_binding(field):
    value = manifest()
    del value["incident_recovery"][field]
    with pytest.raises(recovery.Refused, match="^incident_policy$"):
        recovery.incident_policy(value)


@pytest.mark.parametrize(
    "field", ["runId", "attempt", "producerSha", "manifestSha256", "receiptSha256"]
)
def test_incident_policy_requires_every_historical_anchor(field):
    value = manifest()
    del value["incident_recovery"]["historicalRecovery"][field]
    with pytest.raises(recovery.Refused, match="^incident_policy$"):
        recovery.incident_policy(value)


def test_incident_default_log_is_derived_only_from_its_explicit_image():
    value = manifest()
    original = copy.deepcopy(value)
    ownership = owned(value)
    assert ownership[key("log-group", EXPLICIT_GROUP)] == (
        "incident-default-log",
        EXPLICIT_GROUP,
        None,
    )
    assert ownership[key("image", EXPLICIT)] == (
        "explicit-incident-image",
        EXPLICIT,
        None,
    )
    assert value == original


@pytest.mark.parametrize("change", ["unmatched_log", "missing_image", "missing_scope"])
def test_incident_default_log_cannot_borrow_another_resource_authority(change):
    value = manifest()
    if change == "unmatched_log":
        value["resources"][-1]["sha256"] = key("log-group", EXPLICIT_GROUP + "-other")
    elif change == "missing_image":
        value["resources"] = [
            r for r in value["resources"] if r["proof"] != "explicit-incident-image"
        ]
    else:
        del value["incident_recovery"]
    with pytest.raises(recovery.Refused):
        owned(value)


def test_historical_anchor_does_not_request_a_runtime_artifact():
    value = manifest()
    github = fixtures.FakeGithub()
    github.log_value, steps, _ = fixtures.source_representation(source_log())
    github.values["actions/jobs/10"]["steps"] = steps
    github.api = Mock(side_effect=github.api)
    recovery.provenance(value, github, fixtures.environment())
    assert [call.args[0] for call in github.api.call_args_list] == [
        "",
        "actions/runs/9/attempts/1",
        "actions/runs/9",
        "actions/jobs/10",
        f"contents/{recovery.WORKFLOW}?ref={'a' * 40}",
    ]


def test_scoped_unknown_logging_keeps_scanning_and_emits_exact_evidence(incident):
    incident.split_versions = True
    report = recover(incident)
    expected_groups = {
        fixtures.CONFIGURED: "configured-log",
        recovery.SERVICE_PREFIX + fixtures.SHARED: "shared-log",
        EXPLICIT_GROUP: "incident-default-log",
    }
    assert {
        call.kwargs["logGroupName"]
        for call in incident.logs.delete_log_group.call_args_list
    } == set(expected_groups)
    evidence = {row["sha256"]: row for row in report["incidentLogEvidence"]}
    assert set(evidence) == {key("log-group", group) for group in expected_groups}
    for group, proof in expected_groups.items():
        expected = {
            "sha256": key("log-group", group),
            "ownershipProof": proof,
            "outcome": "deleted",
            "initialCreationTime": int(CREATED.timestamp() * 1000),
            "preDeleteCreationTime": int(CREATED.timestamp() * 1000),
            "inventoryComplete": True,
            "imagesAbsent": sorted(key("image", name) for name in IMAGE_NAMES),
            "nonTerminatedVmCount": 0,
            "knownReferenceCount": 0,
            "unknownLoggingVersionCount": 1,
        }
        if proof != "configured-log":
            image = fixtures.SHARED if proof == "shared-log" else EXPLICIT
            expected["pairedImageSha256"] = key("image", image)
        assert evidence[key("log-group", group)] == expected
    assert {
        row["versionSha256"]: row["loggingStatus"]
        for row in incident.api.version_evidence
    } == {
        recovery.digest(b"1"): "unknown",
        recovery.digest(b"2"): "disabled",
    }
    assert ("get_version", "2") in incident.events
    assert all("logGroupSha256" not in row for row in incident.api.version_evidence)
    serialized = json.dumps(report)
    for name in (*IMAGE_NAMES, *expected_groups, arn(fixtures.UNKNOWN)):
        assert name not in serialized


def test_each_log_delete_rechecks_all_exact_addresses_and_complete_censuses(incident):
    incident.split_vms = incident.split_versions = True
    incident.late_vms = [
        {
            "microvmId": "finished",
            "imageArn": arn(fixtures.UNKNOWN),
            "state": "TERMINATED",
        }
    ]
    recover(incident)
    previous = 0
    for index, event in enumerate(incident.events):
        if event[0] != "delete_log":
            continue
        preceding = incident.events[previous:index]
        assert {value for kind, value in preceding if kind == "absent_image"} == {
            arn(name) for name in IMAGE_NAMES
        }
        assert ("list_vms", "later") in preceding
        assert ("list_versions", "later") in preceding
        assert ("get_version", "2") in preceding
        previous = index + 1
    assert sum(kind == "delete_log" for kind, _ in incident.events) == 3
    assert all(
        call.kwargs["imageIdentifier"].startswith("arn:aws:lambda:")
        for call in incident.plane.get_microvm_image.call_args_list
    )


def test_unscoped_unknown_logging_retains_the_existing_deletion_barrier(incident):
    del incident.manifest["incident_recovery"]
    incident.manifest["resources"] = [
        r
        for r in incident.manifest["resources"]
        if r["proof"] != "incident-default-log"
    ]
    with pytest.raises(recovery.Refused, match="^version_logging_missing$"):
        recover(incident)
    no_log_deletes(incident)


@pytest.mark.parametrize("source", ["get", "list", "both"])
def test_late_page_known_reference_vetoes_after_an_unknown_version(incident, source):
    incident.split_versions = True
    incident.records["2"] = version(2)
    incident.listed = copy.deepcopy(incident.records)
    destination = {"cloudWatch": {"logGroup": fixtures.CONFIGURED}}
    if source in {"get", "both"}:
        incident.records["2"]["logging"] = destination
    if source in {"list", "both"}:
        incident.listed["2"]["logging"] = destination
    with pytest.raises(recovery.Refused, match="^configured_group_in_use$"):
        recover(incident)
    assert ("get_version", "1") in incident.events
    assert ("get_version", "2") in incident.events
    no_log_deletes(incident)


@pytest.mark.parametrize(
    "get,listed,reason",
    [
        (None, MISSING, "version_logging_missing"),
        (MISSING, None, "version_logging_missing"),
        ({}, MISSING, "version_logging_ambiguous"),
        (MISSING, {}, "version_logging_ambiguous"),
        ({"disabled": True}, MISSING, "version_logging_ambiguous"),
        (MISSING, {"cloudWatch": {}}, "missing_text"),
        (
            {"disabled": {}},
            {"cloudWatch": {"logGroup": "other-group"}},
            "version_logging_ambiguous",
        ),
    ],
)
def test_scoped_unknown_exception_never_accepts_present_invalid_logging(
    incident, get, listed, reason
):
    incident.records = {"1": version(1, get)}
    incident.listed = {"1": version(1, listed)}
    with pytest.raises(recovery.Refused, match=f"^{reason}$"):
        recover(incident)
    no_log_deletes(incident)


@pytest.mark.parametrize(
    "field,bad",
    [
        ("imageArn", arn("another-image")),
        ("imageVersion", "different-version"),
        ("state", "FAILED"),
        ("createdAt", CREATED.replace(year=2025)),
        ("createdAt", None),
    ],
)
def test_unknown_logging_requires_exact_list_and_get_version_identity(
    incident, field, bad
):
    incident.records = {"1": version(1)}
    incident.listed = {"1": {**version(1), field: bad}}
    if field == "imageVersion":
        # The Get reply still names version 1 when List requests the changed id.
        incident.records[bad] = incident.records.pop("1")
    with pytest.raises(
        recovery.Refused,
        match="^version_logging_ambiguous$|^version_identity$|^invalid_timestamp$",
    ):
        recover(incident)
    no_log_deletes(incident)


@pytest.mark.parametrize("name", IMAGE_NAMES)
@pytest.mark.parametrize("result", ["present", "error"])
@pytest.mark.parametrize("logs_absent", [False, True])
def test_missing_list_image_requires_fresh_exact_get_absence(
    incident, name, result, logs_absent
):
    if logs_absent:
        incident.logs.groups = {}
    incident.records = {"1": version(1, {"disabled": {}})}
    original = incident.plane.get_microvm_image.side_effect

    class Denied(Exception):
        response = {"Error": {"Code": "AccessDeniedException"}}

    def get_image(imageIdentifier):
        if imageIdentifier == arn(name):
            if result == "error":
                raise Denied()
            return {
                "imageArn": arn(name),
                "name": name,
                "state": "CREATED",
                "createdAt": CREATED,
            }
        return original(imageIdentifier)

    incident.plane.get_microvm_image.side_effect = get_image
    with pytest.raises(
        recovery.Refused,
        match="^image_not_absent$" if result == "present" else "^api_failed$",
    ):
        recover(incident)
    assert any(
        call.kwargs["imageIdentifier"] == arn(name)
        for call in incident.plane.get_microvm_image.call_args_list
    )
    no_log_deletes(incident)


@pytest.mark.parametrize("value", [None, True, "1", float("nan"), float("inf"), -1, 1])
def test_incident_log_requires_finite_creation_time_inside_source_window(
    incident, value
):
    incident.logs.groups[EXPLICIT_GROUP]["creationTime"] = value
    with pytest.raises(
        recovery.Refused, match="^group_creation_time$|^outside_source_lifetime$"
    ):
        recover(incident)
    no_log_deletes(incident)


def test_changed_log_creation_time_vetoes_before_delete(incident):
    original = incident.logs.describe_log_groups
    calls = 0

    def groups(**kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            for group in incident.logs.groups.values():
                group["creationTime"] += 1
        return original(**kwargs)

    incident.logs.describe_log_groups = groups
    with pytest.raises(recovery.Refused, match="^incarnation_changed$"):
        recover(incident)
    no_log_deletes(incident)


@pytest.mark.parametrize(
    "state", ["PENDING", "RUNNING", "SUSPENDING", "SUSPENDED", "TERMINATING"]
)
@pytest.mark.parametrize("logs_absent", [False, True])
def test_nonterminal_vm_on_late_page_vetoes_log_deletion(incident, state, logs_absent):
    if logs_absent:
        incident.logs.groups = {}
    incident.split_vms = True
    incident.late_vms = [
        {"microvmId": "unrelated", "imageArn": arn(fixtures.UNKNOWN), "state": state}
    ]
    with pytest.raises(recovery.Refused, match="^log_vm_dependencies_unobservable$"):
        recover(incident)
    assert ("list_vms", "later") in incident.events
    no_log_deletes(incident)


@pytest.mark.parametrize("state", ["PENDING", "IN_PROGRESS"])
@pytest.mark.parametrize("logs_absent", [False, True])
def test_unknown_logging_for_an_active_build_vetoes_log_deletion(
    incident, state, logs_absent
):
    if logs_absent:
        incident.logs.groups = {}
    incident.records["1"]["state"] = state
    with pytest.raises(recovery.Refused, match="^log_build_in_progress$"):
        recover(incident)
    no_log_deletes(incident)


@pytest.mark.parametrize("state", ["CREATING", "UPDATING"])
def test_absent_logs_do_not_hide_an_active_image_writer(incident, state):
    incident.logs.groups = {}
    incident.plane.images[arn(fixtures.UNKNOWN)]["state"] = state
    with pytest.raises(recovery.Refused, match="^log_build_in_progress$"):
        recover(incident)
    no_log_deletes(incident)


@pytest.mark.parametrize(
    "group",
    [fixtures.CONFIGURED, recovery.SERVICE_PREFIX + fixtures.SHARED, EXPLICIT_GROUP],
)
@pytest.mark.parametrize("source", ["get", "list", "both"])
def test_absent_logs_still_refuse_a_late_explicit_version_reference(
    incident, group, source
):
    incident.logs.groups = {}
    incident.split_versions = True
    incident.records["2"] = version(2)
    incident.listed = copy.deepcopy(incident.records)
    destination = {"cloudWatch": {"logGroup": group}}
    if source in {"get", "both"}:
        incident.records["2"]["logging"] = destination
    if source in {"list", "both"}:
        incident.listed["2"]["logging"] = destination
    with pytest.raises(recovery.Refused, match="^configured_group_in_use$"):
        recover(incident)
    assert ("get_version", "1") in incident.events
    assert ("get_version", "2") in incident.events
    no_log_deletes(incident)


def test_already_absent_logs_have_only_non_deletion_evidence(incident):
    incident.logs.groups = {}
    report = recover(incident)
    expected = []
    for row in incident.manifest["resources"]:
        if row["kind"] != "log-group":
            continue
        evidence = {
            "sha256": row["sha256"],
            "ownershipProof": row["proof"],
            "outcome": "already_absent",
        }
        if row["proof"] != "configured-log":
            evidence["pairedImageSha256"] = key(
                "image", fixtures.SHARED if row["proof"] == "shared-log" else EXPLICIT
            )
        expected.append(evidence)
    assert report["incidentLogEvidence"] == expected
    no_log_deletes(incident)


@pytest.mark.parametrize("source", ["get", "list"])
def test_logging_status_preserves_get_only_destination_digest(incident, source):
    destination = {"cloudWatch": {"logGroup": "unrelated-destination"}}
    incident.records = {"1": version(1, destination if source == "get" else MISSING)}
    incident.listed = {"1": version(1, destination if source == "list" else MISSING)}
    recover(incident)
    assert incident.api.version_evidence[0]["loggingStatus"] == "configured"
    if source == "get":
        assert incident.api.version_evidence[0]["logGroupSha256"] == key(
            "log-group", "unrelated-destination"
        )
    else:
        assert "logGroupSha256" not in incident.api.version_evidence[0]


@pytest.mark.parametrize(
    "before,after", [("configured", "unknown"), ("unknown", "configured")]
)
def test_each_scan_refreshes_status_and_get_destination_digest(incident, before, after):
    destination = {"cloudWatch": {"logGroup": "unrelated-destination"}}
    incident.api.region = REGION

    def scan(status):
        incident.records = {
            "1": version(1, destination if status == "configured" else MISSING)
        }
        incident.api.configured_dependencies(
            {EXPLICIT_GROUP}, incident.plane.images, set(), allow_unknown=True
        )
        assert len(incident.api.version_evidence) == 1
        row = incident.api.version_evidence[0]
        assert row["loggingStatus"] == status
        if status == "configured":
            assert row["logGroupSha256"] == key("log-group", "unrelated-destination")
        else:
            assert "logGroupSha256" not in row

    scan(before)
    scan(after)
    no_log_deletes(incident)


@pytest.mark.parametrize(
    "field,bad",
    [("imageArn", arn("different-image")), ("createdAt", CREATED.replace(year=2025))],
)
def test_scoped_identity_checks_get_only_configuration(incident, field, bad):
    incident.records = {
        "1": version(1, {"cloudWatch": {"logGroup": "unrelated-destination"}})
    }
    incident.listed = {"1": {**version(1), field: bad}}
    with pytest.raises(recovery.Refused, match="^version_logging_ambiguous$"):
        recover(incident)
    no_log_deletes(incident)


def test_scoped_identity_checks_precede_deleted_version_skip(incident):
    incident.records = {"1": {**version(1), "state": "DELETED"}}
    incident.listed = {"1": version(1, {"cloudWatch": {"logGroup": EXPLICIT_GROUP}})}
    with pytest.raises(recovery.Refused, match="^version_logging_ambiguous$"):
        recover(incident)
    no_log_deletes(incident)


@pytest.mark.parametrize("source", ["get", "list"])
@pytest.mark.parametrize(
    "group",
    [
        " ",
        "\t",
        "\n",
        "group\n",
        "group\r",
        "group\0",
        " leading",
        "trailing ",
        "colon:group",
        "unicode-\u00e9",
        "a" * 513,
    ],
)
def test_present_log_destination_must_be_a_valid_name(incident, source, group):
    destination = {"cloudWatch": {"logGroup": group}}
    incident.records = {"1": version(1, destination if source == "get" else MISSING)}
    incident.listed = {"1": version(1, destination if source == "list" else MISSING)}
    with pytest.raises(recovery.Refused, match="^version_logging_ambiguous$"):
        recover(incident)
    no_log_deletes(incident)


@pytest.mark.parametrize("source", ["get", "list"])
@pytest.mark.parametrize("group", ["A", "aA09_/.#-", "A" * 512])
def test_log_destination_accepts_documented_punctuation_and_length(
    incident, source, group
):
    destination = {"cloudWatch": {"logGroup": group}}
    incident.records = {"1": version(1, destination if source == "get" else MISSING)}
    incident.listed = {"1": version(1, destination if source == "list" else MISSING)}
    report = recover(incident)
    assert all(row["outcome"] == "deleted" for row in report["incidentLogEvidence"])
    assert incident.api.version_evidence[0]["loggingStatus"] == "configured"


def test_failed_second_delete_leaves_first_evidence_at_requested(incident):
    report = {}
    original = incident.logs.delete_log_group.side_effect
    calls = 0

    class Denied(Exception):
        response = {"Error": {"Code": "AccessDeniedException"}}

    def delete_log(logGroupName):
        nonlocal calls
        calls += 1
        if calls == 2:
            assert len(report["incidentLogEvidence"]) == 1
            assert report["incidentLogEvidence"][0]["outcome"] == "deletion_requested"
            raise Denied()
        return original(logGroupName)

    incident.logs.delete_log_group.side_effect = delete_log
    with pytest.raises(recovery.Refused, match="^api_failed$"):
        recovery.recover(incident.manifest, incident.ownership, incident.api, report)
    assert calls == 2
    assert len(report["incidentLogEvidence"]) == 1
    row = report["incidentLogEvidence"][0]
    assert row["outcome"] == "deletion_requested"
    assert row["initialCreationTime"] == row["preDeleteCreationTime"]
    assert row["inventoryComplete"] is True
    assert row["imagesAbsent"] == sorted(key("image", name) for name in IMAGE_NAMES)
    report.update(
        kind="live-recovery",
        conformance_verified=False,
        status="failed",
        reason="api_failed",
    )
    assert recovery.validate_receipt(report) is report


@pytest.mark.parametrize("failure", ["verification_error", "deadline"])
def test_failed_visibility_check_does_not_claim_deletion_complete(incident, failure):
    report = {}
    original = incident.logs.describe_log_groups
    absent_reads = 0

    class Denied(Exception):
        response = {"Error": {"Code": "AccessDeniedException"}}

    def groups(**kwargs):
        nonlocal absent_reads
        if incident.logs.delete_log_group.call_count == 3:
            absent_reads += 1
            if failure == "verification_error" and absent_reads == 2:
                raise Denied()
        return original(**kwargs)

    incident.logs.describe_log_groups = groups
    if failure == "deadline":
        incident.api.deadline = 1
    reason = "api_failed" if failure == "verification_error" else "deadline"
    with pytest.raises(recovery.Refused, match=f"^{reason}$"):
        recovery.recover(incident.manifest, incident.ownership, incident.api, report)
    assert absent_reads >= 1
    assert incident.logs.delete_log_group.call_count == 3
    assert len(report["incidentLogEvidence"]) == 3
    assert all(
        row["outcome"] == "deletion_requested" for row in report["incidentLogEvidence"]
    )
    assert all(
        row["status"] == "deletion_requested"
        for row in report["resources"]
        if row["kind"] == "log-group"
    )
    report.update(
        kind="live-recovery",
        conformance_verified=False,
        status="failed",
        reason=reason,
    )
    assert recovery.validate_receipt(report) is report


def test_incident_evidence_is_an_optional_validated_receipt_extension(incident):
    report = recover(incident)
    report.update(
        kind="live-recovery",
        conformance_verified=False,
        status="passed",
        parserVersion=4,
    )
    report["versionEvidence"] = incident.api.version_evidence
    assert recovery.validate_receipt(report) is report


@pytest.mark.parametrize(
    "field,bad",
    [
        ("ownershipProof", "explicit-incident-image"),
        ("outcome", "present"),
        ("initialCreationTime", True),
        ("preDeleteCreationTime", -1),
        ("inventoryComplete", False),
        ("imagesAbsent", []),
        ("imagesAbsent", ["a" * 64] * 3),
        ("nonTerminatedVmCount", 1),
        ("knownReferenceCount", 1),
        ("unknownLoggingVersionCount", -1),
        ("unknownLoggingVersionCount", True),
        ("pairedImageSha256", "bad"),
        ("unreviewed", True),
    ],
)
def test_receipt_rejects_forged_incident_deletion_evidence(incident, field, bad):
    report = recover(incident)
    report.update(kind="live-recovery", conformance_verified=False, status="passed")
    row = next(
        row
        for row in report["incidentLogEvidence"]
        if row["ownershipProof"] == "incident-default-log"
    )
    row[field] = bad
    with pytest.raises(recovery.Refused, match="^receipt_schema$"):
        recovery.validate_receipt(report)


@pytest.mark.parametrize("field", ["loggingStatus", "ownershipProof", "outcome"])
@pytest.mark.parametrize("bad", [{}, {"untrusted": True}, [], ["configured"]])
def test_nested_receipt_values_have_a_schema_refusal(incident, field, bad):
    report = recover(incident)
    report.update(kind="live-recovery", conformance_verified=False, status="passed")
    if field == "loggingStatus":
        report["versionEvidence"] = copy.deepcopy(incident.api.version_evidence)
        row = report["versionEvidence"][0]
    else:
        row = report["incidentLogEvidence"][0]
    row[field] = bad
    with pytest.raises(recovery.Refused, match="^receipt_schema$"):
        recovery.validate_receipt(report)
