# SPDX-License-Identifier: Apache-2.0
"""Observation rows retain strict types, bounds, and cross-section coverage."""

import copy
import json
import runpy
from pathlib import Path
from unittest.mock import Mock

import pytest

CORE = runpy.run_path(
    str(Path(__file__).with_name("recover-live.py")), run_name="tools.recover-live"
)
validate = CORE["validate_observation"]
Refused = CORE["Refused"]


def snapshot(state="CREATED"):
    return {
        "state": state,
        "availability": "ACTIVE",
        "createdAtKind": "valid",
        "createdAt": 0,
        "logging": {"kind": "disabled"},
    }


def rows():
    return {
        "images": {
            "imageSha256": "a" * 64,
            "imageArnSha256": "b" * 64,
            "approved": True,
            "getOutcome": "observed",
            "identityMatches": True,
            "listed": snapshot(),
            "observed": snapshot(),
        },
        "microvms": {"sha256": "c" * 64, "state": "RUNNING"},
        "logGroups": {"sha256": "d" * 64},
        "versions": {
            "imageSha256": "a" * 64,
            "imageArnSha256": "b" * 64,
            "versionSha256": "e" * 64,
            "getOutcome": "observed",
            "identityMatches": True,
            "stateMatches": True,
            "availabilityMatches": True,
            "createdAtMatches": True,
            "loggingMatches": True,
            "listed": snapshot("SUCCESSFUL"),
            "observed": snapshot("SUCCESSFUL"),
        },
        "versionCoverage": {
            "imageSha256": "a" * 64,
            "imageArnSha256": "b" * 64,
            "listedCount": 1,
            "getCount": 1,
            "observedCount": 1,
            "notFoundCount": 0,
            "apiErrorCount": 0,
            "paginationComplete": True,
        },
        "errors": {"section": "images", "code": "api_failed"},
    }


def report(*, completed=False):
    return {
        "kind": "live-recovery-observation",
        "schemaVersion": 1,
        "conformance_verified": False,
        "recoveryAccepted": False,
        "mutationsPerformed": False,
        "status": "completed" if completed else "incomplete",
        "sourceRun": 9,
        "sourceSha": "a" * 40,
        "region": "us-east-1",
        "manifestSha256": "b" * 64,
        "parserVersion": CORE["PARSER_VERSION"],
        "sourceLogRepresentation": CORE["LOG_REPRESENTATION"],
        "sections": {key: "completed" for key in CORE["OBSERVATION_SECTIONS"]},
        "independentVerifier": {
            "resources": [],
            "summary_present": True,
            "leakCount": 0,
            "exitCode": 0,
        },
        **{key: [row] for key, row in rows().items() if key != "errors"},
        "errors": [],
    }


def rejected(value):
    with pytest.raises(Refused, match="^receipt_schema$"):
        validate(value)


@pytest.mark.parametrize("count", [999, 1000, 1001])
def test_readonly_iterator_bounds_the_accumulated_rows_across_pages(count):
    api = CORE["ReadOnlyApi"](None, None)
    expected = [{"index": index} for index in range(count)]
    api.call = Mock(
        side_effect=[
            {"items": expected[:500], "nextToken": "second-page"},
            {"items": expected[500:]},
        ]
    )
    iterator = api.iter_rows(None, "list_microvms", "items")
    if count <= 1000:
        assert list(iterator) == expected
    else:
        with pytest.raises(Refused, match="^observation_limit$"):
            list(iterator)


@pytest.mark.parametrize(
    "pages,reason",
    [
        ([{}], "missing_page_items"),
        ([{"items": [None]}], "invalid_page_item"),
        ([{"items": [], "nextToken": ""}], "pagination_token"),
        ([{"items": [], "nextToken": 3}], "pagination_token"),
        (
            [
                {"items": [], "nextToken": "duplicate"},
                {"items": [], "nextToken": "duplicate"},
            ],
            "pagination_token",
        ),
    ],
)
def test_readonly_iterator_preserves_the_fixed_refusal_reason(pages, reason):
    api = CORE["ReadOnlyApi"](None, None)
    api.call = Mock(side_effect=pages)
    with pytest.raises(Refused, match=f"^{reason}$"):
        list(api.iter_rows(None, "list_microvms", "items"))


def test_append_row_limit_preserves_the_fixed_refusal_reason_and_prior_rows():
    value = report()
    value.update({key: [] for key in rows()})
    value["logGroups"] = [{"sha256": f"{index:064x}"} for index in range(1000)]
    before = copy.deepcopy(value)
    with pytest.raises(Refused, match="^observation_limit$"):
        CORE["observation_append"](value, "images", rows()["images"])
    assert value == before


@pytest.mark.parametrize("section", tuple(rows()))
@pytest.mark.parametrize("invalid", [None, {}, "", 0, True])
def test_observation_collections_require_lists(section, invalid):
    value = report()
    value[section] = invalid
    rejected(value)


@pytest.mark.parametrize("section", tuple(rows()))
@pytest.mark.parametrize("invalid", [None, [], "", 0, True])
def test_observation_collections_require_object_rows(section, invalid):
    value = report()
    value[section] = [invalid]
    rejected(value)


REQUIRED_FIELDS = [
    (section, key)
    for section, row in rows().items()
    for key in row
    if key not in {"observed", "listed", "imageArnSha256"}
    or section in {"versions", "versionCoverage"}
]


@pytest.mark.parametrize("section,key", REQUIRED_FIELDS)
def test_observation_row_required_fields_cannot_be_omitted(section, key):
    value = report()
    value["errors"] = [rows()["errors"]]
    del value[section][0][key]
    rejected(value)


@pytest.mark.parametrize("section", tuple(rows()))
def test_observation_rows_reject_unknown_fields(section):
    value = report()
    value[section] = [rows()[section]]
    value[section][0]["privateIdentifier"] = "private-value"
    rejected(value)


@pytest.mark.parametrize(
    "section,key",
    [
        (section, key)
        for section, row in rows().items()
        for key in row
        if key.endswith("Sha256") or key == "sha256"
    ]
    + [("errors", "imageSha256"), ("errors", "versionSha256")],
)
@pytest.mark.parametrize("invalid", [None, 0, "a" * 63, "A" * 64, "g" * 64])
def test_observation_hashes_reject_raw_or_malformed_identities(section, key, invalid):
    value = report()
    value[section] = [rows()[section]]
    value[section][0][key] = invalid
    rejected(value)


@pytest.mark.parametrize("section", ["images", "versions"])
@pytest.mark.parametrize("outcome", ["not_found", "api_error"])
def test_incomplete_observation_accepts_unavailable_get_without_observed_snapshot(
    section, outcome
):
    value = report()
    row = value[section][0]
    row["getOutcome"] = outcome
    del row["observed"]
    for key in row:
        if key.endswith("Matches"):
            row[key] = None
    assert validate(value) is value


@pytest.mark.parametrize("section", ["images", "versions"])
@pytest.mark.parametrize(
    "corruption", ["unknown_outcome", "missing_observed", "extra_observed"]
)
def test_get_outcomes_require_exact_snapshot_presence(section, corruption):
    value = report()
    row = value[section][0]
    if corruption == "unknown_outcome":
        row["getOutcome"] = "private-error"
    elif corruption == "missing_observed":
        del row["observed"]
    else:
        row["getOutcome"] = "not_found"
    rejected(value)


@pytest.mark.parametrize(
    "section,key",
    [
        (section, key)
        for section, row in rows().items()
        for key in row
        if key.endswith("Matches")
    ],
)
@pytest.mark.parametrize("valid", [None, False, True])
def test_observation_match_fields_accept_only_boolean_or_unknown(section, key, valid):
    value = report()
    value[section][0][key] = valid
    assert validate(value) is value


@pytest.mark.parametrize(
    "section,key",
    [
        (section, key)
        for section, row in rows().items()
        for key in row
        if key.endswith("Matches")
    ],
)
@pytest.mark.parametrize("invalid", [0, 1, "true", [], {}])
def test_observation_match_fields_refuse_boolean_lookalikes(section, key, invalid):
    value = report()
    value[section][0][key] = invalid
    rejected(value)


@pytest.mark.parametrize("invalid", [0, 1, None, "true"])
def test_image_approval_requires_an_explicit_boolean(invalid):
    value = report()
    value["images"][0]["approved"] = invalid
    rejected(value)


@pytest.mark.parametrize("state", ["missing", "null", "malformed", "RUNNING"])
def test_vm_state_preserves_known_and_unknown_values(state):
    value = report()
    value["microvms"][0]["state"] = state
    assert validate(value) is value


def test_vm_state_rejects_arbitrary_provider_text():
    value = report()
    value["microvms"][0]["state"] = "private-state"
    rejected(value)


@pytest.mark.parametrize(
    "key",
    ["listedCount", "getCount", "observedCount", "notFoundCount", "apiErrorCount"],
)
@pytest.mark.parametrize("invalid", [-1, 1.0, True, None, "1"])
def test_version_coverage_counts_are_nonnegative_integers(key, invalid):
    value = report()
    value["versionCoverage"][0][key] = invalid
    rejected(value)


@pytest.mark.parametrize("invalid", [0, 1, None, "true"])
def test_version_coverage_pagination_requires_boolean(invalid):
    value = report()
    value["versionCoverage"][0]["paginationComplete"] = invalid
    rejected(value)


@pytest.mark.parametrize(
    "counts", [(0, 1, 1, 0, 0), (2, 1, 0, 0, 0), (2, 1, 1, 1, 0), (2, 1, 1, 0, 1)]
)
def test_version_coverage_rejects_inconsistent_read_counts(counts):
    value = report()
    value["versionCoverage"][0].update(
        zip(
            (
                "listedCount",
                "getCount",
                "observedCount",
                "notFoundCount",
                "apiErrorCount",
            ),
            counts,
            strict=True,
        )
    )
    rejected(value)


@pytest.mark.parametrize(
    "section", ["images", "microvms", "logGroups", "versions", "versionCoverage"]
)
def test_observation_rejects_duplicate_section_identities(section):
    value = report()
    value[section].append(copy.deepcopy(value[section][0]))
    rejected(value)


@pytest.mark.parametrize("section", tuple(rows()))
@pytest.mark.parametrize("count", [1000, 1001])
def test_observation_row_limits_apply_to_each_collection(section, count):
    value = report()
    for name in rows():
        value[name] = []
    for index in range(count):
        row = rows()[section]
        if section == "versions":
            row["versionSha256"] = f"{index:064x}"
        elif section in {"images", "versionCoverage"}:
            row["imageSha256"] = f"{index:064x}"
        elif section != "errors":
            row["sha256"] = f"{index:064x}"
        value[section].append(row)
    if count == 1000:
        assert validate(value) is value
    else:
        rejected(value)


def test_observation_limits_accumulated_metadata_but_counts_errors_separately():
    value = report()
    for key in rows():
        value[key] = []
    value["logGroups"] = [{"sha256": f"{index:064x}"} for index in range(1000)]
    value["errors"] = [rows()["errors"] for _ in range(1000)]
    assert validate(value) is value
    value["microvms"] = [rows()["microvms"]]
    rejected(value)


def sized_report(target, count):
    value = report()
    value.update({key: [] for key in rows()})
    for index in range(count):
        row = rows()["versions"]
        row["versionSha256"] = f"{index:064x}"
        for field in ("listed", "observed"):
            row[field]["createdAt"] = 10**308
        value["versions"].append(row)
    excess = len(json.dumps(value).encode()) - target
    assert excess >= 0
    # Finite numeric timestamps vary the size without adding an unrecognized field.
    for row in value["versions"]:
        for field in ("listed", "observed"):
            reduction = min(excess, 308)
            row[field]["createdAt"] = 10 ** (308 - reduction)
            excess -= reduction
    assert excess == 0
    assert len(json.dumps(value).encode()) == target
    return value


@pytest.mark.parametrize("difference", [-1, 0, 1])
def test_observation_enforces_serialized_byte_boundary(difference):
    target = CORE["OBSERVATION_MAX_BYTES"] + difference
    value = sized_report(target, 1000)
    if difference <= 0:
        assert validate(value) is value
    else:
        rejected(value)


@pytest.mark.parametrize("difference", [-1, 0, 1])
def test_append_reserves_half_the_artifact_budget_for_later_errors(difference):
    row = rows()["images"]
    target = CORE["OBSERVATION_MAX_BYTES"] // 2 + difference
    value = sized_report(target - len(json.dumps(row)), 500)
    before = copy.deepcopy(value)
    if difference < 0:
        CORE["observation_append"](value, "images", row)
        assert value["images"] == [row]
        assert len(value["versions"]) == 500
    else:
        with pytest.raises(Refused, match="^observation_limit$"):
            CORE["observation_append"](value, "images", row)
        assert value == before


@pytest.mark.parametrize(
    "key,invalid",
    [
        ("section", "private-section"),
        ("code", "private-provider-error"),
        ("operation", "delete_microvm_image"),
    ],
)
def test_observation_errors_reject_unrecognized_diagnostics(key, invalid):
    value = report()
    value["errors"] = [{**rows()["errors"], key: invalid}]
    rejected(value)


@pytest.mark.parametrize("operation", sorted(CORE["OBSERVATION_OPERATIONS"]))
def test_observation_errors_allow_only_the_read_operation_census(operation):
    value = report()
    value["errors"] = [
        {
            **rows()["errors"],
            "operation": operation,
            "imageSha256": "a" * 64,
            "versionSha256": "b" * 64,
        }
    ]
    assert validate(value) is value


def test_complete_observation_accepts_reconciled_nonempty_collections():
    value = report(completed=True)
    assert validate(value) is value


@pytest.mark.parametrize(
    "section",
    [
        "sourceRun",
        "sourceSha",
        "region",
        "manifestSha256",
        "parserVersion",
        "sourceLogRepresentation",
        "images",
        "microvms",
        "logGroups",
        "versions",
        "versionCoverage",
        "sections",
        "independentVerifier",
    ],
)
def test_complete_observation_requires_all_evidence_sections_and_source_binding(
    section,
):
    value = report(completed=True)
    del value[section]
    rejected(value)


@pytest.mark.parametrize(
    "corruption",
    [
        "error",
        "image_api_error",
        "version_not_found",
        "version_api_error",
        "missing_listed_image",
        "extra_coverage",
        "positive_not_found",
        "positive_api_error",
        "partial_gets",
    ],
)
def test_complete_observation_rejects_incomplete_coverage(corruption):
    value = report(completed=True)
    coverage = value["versionCoverage"][0]
    if corruption == "error":
        value["errors"] = [rows()["errors"]]
    elif corruption == "image_api_error":
        value["images"][0]["getOutcome"] = "api_error"
        del value["images"][0]["observed"]
    elif corruption in {"version_not_found", "version_api_error"}:
        value["versions"][0]["getOutcome"] = corruption.removeprefix("version_")
        del value["versions"][0]["observed"]
    elif corruption == "missing_listed_image":
        del value["images"][0]["listed"]
        del value["images"][0]["imageArnSha256"]
    elif corruption == "extra_coverage":
        value["versionCoverage"].append({**coverage, "imageSha256": "f" * 64})
    elif corruption in {"positive_not_found", "positive_api_error"}:
        coverage["observedCount"] = 0
        coverage[
            "notFoundCount" if corruption == "positive_not_found" else "apiErrorCount"
        ] = 1
    else:
        coverage["listedCount"] = 2
    rejected(value)
