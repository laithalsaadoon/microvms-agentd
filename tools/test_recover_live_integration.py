# SPDX-License-Identifier: Apache-2.0
"""Image progress and positive version logging compose without crossing phases."""

import copy
import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "tools.recover-live", Path(__file__).with_name("recover-live.py")
)
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)
FIXTURES = importlib.util.spec_from_file_location(
    "integration_fixtures", Path(__file__).with_name("test_recover_live.py")
)
fixtures = importlib.util.module_from_spec(FIXTURES)
FIXTURES.loader.exec_module(fixtures)
fixtures.recovery = recovery
MISSING = object()


@pytest.fixture
def incident(monkeypatch):
    case = fixtures.RecoveryTests()
    case.setUp()
    approved = [fixtures.arn(fixtures.SHARED), fixtures.arn(fixtures.CAPABILITY)]
    events, confirmed = [], set()
    original_delete = case.plane.delete_microvm_image
    original_get = case.plane.get_microvm_image
    original_log_delete = case.logs.delete_log_group

    def delete_image(imageIdentifier):
        events.append(("delete_image", imageIdentifier))
        return original_delete(imageIdentifier)

    def get_image(imageIdentifier):
        try:
            return original_get(imageIdentifier)
        except fixtures.Missing:
            confirmed.add(imageIdentifier)
            events.append(("absent_image", imageIdentifier))
            raise

    def delete_log(logGroupName):
        events.append(("delete_log", logGroupName))
        return original_log_delete(logGroupName)

    monkeypatch.setattr(case.plane, "delete_microvm_image", delete_image)
    monkeypatch.setattr(case.plane, "get_microvm_image", get_image)
    monkeypatch.setattr(case.logs, "delete_log_group", delete_log)

    def configure(get_logging=MISSING, list_logging=MISSING):
        record = {
            "imageArn": fixtures.arn(fixtures.UNKNOWN),
            "imageVersion": "1",
            "state": "SUCCESSFUL",
            "createdAt": fixtures.CREATED,
        }
        listed = copy.deepcopy(record)
        if get_logging is not MISSING:
            record["logging"] = get_logging
        if list_logging is not MISSING:
            listed["logging"] = list_logging

        def list_versions(imageIdentifier, **kwargs):
            # The first version read must follow accepted deletion AND exact Get
            # absence for every approved image, not just disappearance from a list.
            assert confirmed == set(approved)
            assert imageIdentifier == fixtures.arn(fixtures.UNKNOWN)
            events.append(("list_version", imageIdentifier))
            return {"items": [copy.deepcopy(listed)]}

        def get_version(imageIdentifier, imageVersion):
            assert imageIdentifier == record["imageArn"] and imageVersion == "1"
            events.append(("get_version", imageIdentifier))
            return copy.deepcopy(record)

        monkeypatch.setattr(case.plane, "list_microvm_image_versions", list_versions)
        monkeypatch.setattr(case.plane, "get_microvm_image_version", get_version)

    case.configure = configure
    case.events = events
    case.expected_image_events = [
        event
        for arn in approved
        for event in (("delete_image", arn), ("absent_image", arn))
    ]
    return case


@pytest.mark.parametrize(
    "logging", [{"disabled": {}}, {"cloudWatch": {"logGroup": "other-group"}}]
)
def test_positive_list_logging_is_consulted_only_after_image_absence(incident, logging):
    incident.configure(list_logging=logging)
    report = {}
    recovery.recover(incident.manifest, incident.owned, incident.api, report)
    assert incident.events[:4] == incident.expected_image_events
    assert incident.events[4][0] == "list_version"
    assert [kind for kind, _ in incident.events].count("delete_log") == 2
    assert all(
        row["status"] == "absent" and row["deletionRequested"]
        for row in report["resources"]
    )
    assert set(incident.plane.images) == {fixtures.arn(fixtures.UNKNOWN)}
    assert set(incident.logs.groups) == {recovery.SERVICE_PREFIX + fixtures.UNKNOWN}
    assert "independentVerifier" not in report


@pytest.mark.parametrize(
    "get_logging,list_logging,reason",
    [
        (MISSING, MISSING, "version_logging_missing"),
        (None, {"disabled": {}}, "version_logging_missing"),
        (MISSING, {}, "version_logging_ambiguous"),
        (
            {"disabled": {}},
            {"cloudWatch": {"logGroup": "other-group"}},
            "version_logging_ambiguous",
        ),
        (
            MISSING,
            {"cloudWatch": {"logGroup": fixtures.CONFIGURED}},
            "configured_group_in_use",
        ),
    ],
)
def test_unproven_list_logging_preserves_image_progress_without_log_deletion(
    incident, get_logging, list_logging, reason
):
    incident.configure(get_logging, list_logging)
    report = {}
    with pytest.raises(recovery.Refused, match=f"^{reason}$"):
        recovery.recover(incident.manifest, incident.owned, incident.api, report)
    assert incident.events[:4] == incident.expected_image_events
    assert incident.events[4][0] == "list_version"
    assert not any(kind == "delete_log" for kind, _ in incident.events)
    for row in report["resources"]:
        assert row["deletionRequested"] == (row["kind"] == "image")
        assert row["status"] == ("absent" if row["kind"] == "image" else "present")
    assert "independentVerifier" not in report
