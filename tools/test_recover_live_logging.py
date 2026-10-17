# SPDX-License-Identifier: Apache-2.0
"""Positive exact-version evidence never turns missing logging into a default."""

import copy
import importlib.util
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest

RECOVERY_SPEC = importlib.util.spec_from_file_location(
    "tools.recover-live", Path(__file__).with_name("recover-live.py")
)
recovery = importlib.util.module_from_spec(RECOVERY_SPEC)
RECOVERY_SPEC.loader.exec_module(recovery)
SPEC = importlib.util.spec_from_file_location(
    "logging_fixtures", Path(__file__).with_name("test_recover_live_inventory.py")
)
fixtures = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixtures)
# The shared fixture must use this same loaded module, including its Refused type.
fixtures.recovery = recovery
setup = fixtures.setup
ARN, GROUP, CREATED = fixtures.ARN, fixtures.GROUP, fixtures.CREATED
MISSING = object()


def versions(setup, get=MISSING, listed=MISSING):
    record = {
        "imageArn": ARN,
        "imageVersion": "1",
        "state": "SUCCESSFUL",
        "createdAt": CREATED,
    }
    row = copy.deepcopy(record)
    if get is not MISSING:
        record["logging"] = get
    if listed is not MISSING:
        row["logging"] = listed
    setup.plane.list_microvm_image_versions.return_value = {"items": [row]}
    setup.plane.get_microvm_image_version = Mock(return_value=record)
    setup.plane.delete_microvm_image = Mock()
    setup.logs.delete_log_group = Mock()
    return record, row


def scan(setup, deleting=None):
    setup.api.configured_dependencies(
        GROUP, {ARN: setup.image}, set() if deleting is None else deleting
    )


def no_deletes(setup):
    setup.plane.delete_microvm_image.assert_not_called()
    setup.logs.delete_log_group.assert_not_called()


@pytest.mark.parametrize(
    "logging,deleting",
    [
        ({"disabled": {}}, set()),
        ({"cloudWatch": {"logGroup": "another-group"}}, set()),
        ({"cloudWatch": {"logGroup": GROUP}}, {ARN}),
    ],
)
def test_missing_get_uses_only_explicit_matching_list_configuration(
    setup, logging, deleting
):
    record, row = versions(setup, listed=logging)
    scan(setup, deleting)
    assert "logging" not in record
    assert row["logging"] == logging
    # This receipt field still describes Get; do not disguise the List source.
    assert "logGroupSha256" not in setup.api.version_evidence[0]
    no_deletes(setup)


def test_fallback_retains_configured_group_dependency_veto(setup):
    versions(setup, listed={"cloudWatch": {"logGroup": GROUP}})
    with pytest.raises(recovery.Refused, match="^configured_group_in_use$"):
        scan(setup)
    no_deletes(setup)


@pytest.mark.parametrize(
    "get,listed,reason",
    [
        (MISSING, MISSING, "version_logging_missing"),
        (None, {"disabled": {}}, "version_logging_missing"),
        ({}, {"disabled": {}}, "version_logging_ambiguous"),
        ({"disabled": True}, {"disabled": {}}, "version_logging_ambiguous"),
        ({"cloudWatch": {}}, {"disabled": {}}, "missing_text"),
        (MISSING, None, "version_logging_missing"),
        (MISSING, {}, "version_logging_ambiguous"),
        (MISSING, {"disabled": True}, "version_logging_ambiguous"),
        (MISSING, {"disabled": {"extra": True}}, "version_logging_ambiguous"),
        (MISSING, {"disabled": {}, "cloudWatch": {}}, "version_logging_ambiguous"),
        (MISSING, {"cloudWatch": None}, "version_logging_ambiguous"),
        (MISSING, {"cloudWatch": {}}, "missing_text"),
        ({"disabled": {}}, None, "version_logging_missing"),
    ],
)
def test_missing_null_and_malformed_evidence_never_select_a_safe_default(
    setup, get, listed, reason
):
    versions(setup, get=get, listed=listed)
    with pytest.raises(recovery.Refused, match=f"^{reason}$"):
        scan(setup)
    no_deletes(setup)


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("imageArn", "another-image", "version_logging_ambiguous"),
        ("createdAt", CREATED + timedelta(microseconds=1), "version_logging_ambiguous"),
        ("createdAt", None, "invalid_timestamp"),
        ("createdAt", 0, "invalid_timestamp"),
        ("state", "FAILED", "version_logging_ambiguous"),
        ("state", "DELETED", "version_logging_ambiguous"),
        ("state", "UNKNOWN", "version_logging_ambiguous"),
        ("state", None, "version_logging_ambiguous"),
    ],
)
def test_list_configuration_is_bound_to_the_exact_get_incarnation(
    setup, field, value, reason
):
    _, row = versions(setup, listed={"disabled": {}})
    row[field] = value
    with pytest.raises(recovery.Refused, match=f"^{reason}$"):
        scan(setup)
    no_deletes(setup)


def test_list_version_must_equal_the_exact_get_version(setup):
    _, row = versions(setup, listed={"disabled": {}})
    row["imageVersion"] = "2"
    with pytest.raises(recovery.Refused, match="^version_identity$"):
        scan(setup)
    no_deletes(setup)


@pytest.mark.parametrize(
    "created", [CREATED.isoformat(), CREATED.isoformat().replace("+00:00", "Z")]
)
def test_equivalent_timestamp_representations_bind_the_same_incarnation(setup, created):
    _, row = versions(setup, listed={"disabled": {}})
    row["createdAt"] = created
    scan(setup)


@pytest.mark.parametrize(
    "get,listed",
    [
        ({"disabled": {}}, {"cloudWatch": {"logGroup": "other"}}),
        ({"cloudWatch": {"logGroup": GROUP}}, {"disabled": {}}),
        ({"cloudWatch": {"logGroup": "first"}}, {"cloudWatch": {"logGroup": "second"}}),
    ],
)
def test_conflicting_explicit_reads_refuse_even_when_one_would_allow_deletion(
    setup, get, listed
):
    versions(setup, get=get, listed=listed)
    with pytest.raises(recovery.Refused, match="^version_logging_ambiguous$"):
        scan(setup)
    no_deletes(setup)


@pytest.mark.parametrize(
    "logging", [{"disabled": {}}, {"cloudWatch": {"logGroup": "other"}}]
)
def test_equal_explicit_reads_and_get_only_keep_the_existing_verdict(setup, logging):
    versions(setup, get=logging, listed=copy.deepcopy(logging))
    scan(setup)
    versions(setup, get=logging)
    scan(setup)


def test_each_scan_reads_all_pages_again_before_accepting_list_evidence(setup):
    record, row = versions(setup, listed={"disabled": {}})
    setup.plane.list_microvm_image_versions.side_effect = [
        {"items": [], "nextToken": "later"},
        {"items": [copy.deepcopy(row)]},
        {"items": [], "nextToken": "later"},
        {"items": [{**row, "logging": {"cloudWatch": {"logGroup": GROUP}}}]},
    ]
    scan(setup)
    with pytest.raises(recovery.Refused, match="^configured_group_in_use$"):
        scan(setup)
    assert "logging" not in record
    assert setup.plane.list_microvm_image_versions.call_count == 4
    assert setup.plane.get_microvm_image_version.call_count == 2
    no_deletes(setup)


def test_duplicate_list_versions_refuse_before_using_the_matching_row(setup):
    _, row = versions(setup, listed={"disabled": {}})
    setup.plane.list_microvm_image_versions.return_value = {"items": [row, row]}
    with pytest.raises(recovery.Refused, match="^duplicate_identity$"):
        scan(setup)
    setup.plane.get_microvm_image_version.assert_not_called()
    no_deletes(setup)
