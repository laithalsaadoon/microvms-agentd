# SPDX-License-Identifier: Apache-2.0
"""Historical terminal VMs cannot hide current read-only recovery evidence."""

import copy
import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

SPEC = importlib.util.spec_from_file_location(
    "tools.recover-live", Path(__file__).with_name("recover-live.py")
)
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)
REGION = "us-east-1"
IMAGE = "synthetic-image"
IMAGE_ID = "synthetic-image-identifier"
VERSION = "synthetic-version"
GROUP = "/aws/lambda-microvms/synthetic-history"
DOMAIN = b"live-recovery-terminated-microvms-v1\0"
EMPTY_DIGEST = "44dbdb622d2121964fca53ed796f8109945ce0829ec0c16bcb9687637b4c0387"


def identity(kind, name):
    return hashlib.sha256(f"{kind}\0{REGION}\0{name}".encode()).hexdigest()


def terminal_digest(records):
    hashes = sorted(
        bytes.fromhex(identity("microvm", row["microvmId"])) for row in records
    )
    return hashlib.sha256(DOMAIN + b"".join(hashes)).hexdigest()


def pages(records):
    chunks = [records[i : i + 50] for i in range(0, len(records), 50)] or [[]]
    return [
        {
            "items": chunk,
            **({"nextToken": f"page-{i + 1}"} if i + 1 < len(chunks) else {}),
        }
        for i, chunk in enumerate(chunks)
    ]


def initial_report():
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
    }


def minimal_report(version=2):
    result = {
        key: value
        for key, value in initial_report().items()
        if key
        in {
            "kind",
            "schemaVersion",
            "conformance_verified",
            "recoveryAccepted",
            "mutationsPerformed",
            "status",
        }
    }
    result["schemaVersion"] = version
    return result


@pytest.fixture
def history(monkeypatch):
    image = {"name": IMAGE, "imageArn": IMAGE_ID, "state": "CREATED"}
    version = {
        "imageArn": IMAGE_ID,
        "imageVersion": VERSION,
        "state": "SUCCESSFUL",
        "status": "ACTIVE",
        "logging": {"cloudWatch": {"logGroup": GROUP}},
    }
    calls = []

    def endpoint(operation, response):
        def record(**kwargs):
            calls.append(operation)
            return copy.deepcopy(response)

        return Mock(side_effect=record)

    plane = SimpleNamespace(
        list_microvm_images=endpoint("images", {"items": [image]}),
        get_microvm_image=endpoint("image", image),
        list_microvm_image_versions=endpoint("versions", {"items": [version]}),
        get_microvm_image_version=endpoint("version", version),
        list_microvms=Mock(return_value={"items": []}),
    )
    logs = SimpleNamespace(
        describe_log_groups=endpoint("logs", {"logGroups": [{"logGroupName": GROUP}]}),
    )
    monkeypatch.setattr(
        recovery.subprocess,
        "run",
        Mock(
            return_value=subprocess.CompletedProcess(
                [],
                0,
                f"account {REGION}: clean — no conformance resources survive\n",
                "",
            )
        ),
    )
    api = recovery.ReadOnlyApi(plane, logs, clock=lambda: 0, sleep=lambda _: None)
    original_call = api.call

    def read(client, operation, **kwargs):
        if operation == "list_microvms":
            calls.append("microvms")
        return original_call(client, operation, **kwargs)

    monkeypatch.setattr(api, "call", read)
    manifest = {
        "region": REGION,
        "resources": [{"kind": "image", "sha256": identity("image", IMAGE)}],
    }
    ownership = {identity("image", IMAGE): ("shared-image", IMAGE, IMAGE_ID)}

    def observe(records=None, responses=None):
        if records is not None:
            plane.list_microvms.side_effect = pages(records)
        if responses is not None:
            plane.list_microvms.side_effect = responses
        result = initial_report()
        recovery.observe(manifest, ownership, api, result)
        return result

    return SimpleNamespace(
        observe=observe, api=api, plane=plane, logs=logs, calls=calls, version=version
    )


def assert_other_evidence(result):
    assert result["sections"]["images"] == "completed"
    assert result["sections"]["versions"] == "completed"
    assert result["sections"]["logGroups"] == "completed"
    assert result["sections"]["independentVerifier"] == "completed"
    assert result["images"][0]["imageSha256"] == identity("image", IMAGE)
    assert result["images"][0]["getOutcome"] == "observed"
    assert (
        result["versions"][0]["versionSha256"]
        == hashlib.sha256(VERSION.encode()).hexdigest()
    )
    assert result["versions"][0]["getOutcome"] == "observed"
    assert result["logGroups"] == [{"sha256": identity("log-group", GROUP)}]


def test_terminal_history_cannot_starve_live_vms_or_other_resource_evidence(history):
    terminal = [
        {"microvmId": f"synthetic-history-{i}", "state": "TERMINATED"}
        for i in range(1051)
    ]
    retained = [
        {"microvmId": "synthetic-running", "state": "RUNNING"},
        {"microvmId": "synthetic-missing"},
        {"microvmId": "synthetic-null", "state": None},
        {"microvmId": "synthetic-unknown", "state": "FUTURE_TERMINAL"},
    ]
    result = history.observe(terminal + retained)
    # Assert the preexisting verdict and evidence before inspecting the new schema.
    assert result["status"] == "completed"
    assert_other_evidence(result)
    assert result["microvms"] == [
        {"sha256": identity("microvm", row["microvmId"]), "state": state}
        for row, state in zip(retained, ["RUNNING", "missing", "null", "malformed"])
    ]
    assert result["schemaVersion"] == 2
    assert history.calls.index("microvms") > max(
        history.calls.index(operation)
        for operation in ("images", "image", "versions", "version", "logs")
    )
    assert result["microvmCoverage"] == {
        "processedCount": len(terminal) + len(retained),
        "terminatedCount": len(terminal),
        "retainedCount": len(retained),
        "pagesScanned": 22,
        "terminatedIdentitySha256": terminal_digest(terminal),
        "paginationComplete": True,
    }
    calls = history.plane.list_microvms.call_args_list
    assert calls[0].kwargs == {"maxResults": 50}
    assert calls[-1].kwargs == {"maxResults": 50, "nextToken": "page-21"}
    assert recovery.validate_observation(result) is result
    serialized = json.dumps(result)
    assert all(
        value not in serialized for value in ("synthetic-history-", IMAGE_ID, GROUP)
    )


@pytest.mark.parametrize(
    "state,expected",
    [
        ("PENDING", "PENDING"),
        ("RUNNING", "RUNNING"),
        ("SUSPENDING", "SUSPENDING"),
        ("SUSPENDED", "SUSPENDED"),
        ("TERMINATING", "TERMINATING"),
        ("terminated", "malformed"),
        ("TERMINATED ", "malformed"),
        ("FUTURE_TERMINAL", "malformed"),
        (None, "null"),
        (False, "malformed"),
        ({"TERMINATED": True}, "malformed"),
    ],
)
def test_only_exact_terminal_state_is_compacted(history, state, expected):
    result = history.observe([{"microvmId": "synthetic-vm", "state": state}])
    assert result["microvms"] == [
        {"sha256": identity("microvm", "synthetic-vm"), "state": expected}
    ]
    assert result["microvmCoverage"]["terminatedCount"] == 0
    assert result["microvmCoverage"]["terminatedIdentitySha256"] == terminal_digest([])
    assert result["microvmCoverage"]["retainedCount"] == 1
    assert recovery.validate_observation(result) is result


@pytest.mark.parametrize("reverse", [False, True])
def test_terminal_digest_is_sorted_and_bound_to_exact_identities(history, reverse):
    terminal = [
        {"microvmId": "synthetic-terminal-z", "state": "TERMINATED"},
        {"microvmId": "synthetic-terminal-a", "state": "TERMINATED"},
    ]
    result = history.observe(terminal[::-1] if reverse else terminal)
    assert result["microvms"] == []
    assert result["microvmCoverage"]["terminatedIdentitySha256"] == terminal_digest(
        terminal
    )
    assert result["microvmCoverage"]["terminatedCount"] == 2
    assert result["status"] == "completed"


def test_empty_history_has_the_fixed_domain_digest(history):
    result = history.observe([])
    assert result["microvmCoverage"] == {
        "pagesScanned": 1,
        "processedCount": 0,
        "terminatedCount": 0,
        "retainedCount": 0,
        "terminatedIdentitySha256": EMPTY_DIGEST,
        "paginationComplete": True,
    }
    assert result["status"] == "completed"


@pytest.mark.parametrize(
    "second,code",
    [
        ({}, "missing_page_items"),
        ({"items": [None]}, "invalid_page_item"),
        ({"items": [{"state": "TERMINATED"}]}, "missing_text"),
        ({"items": [], "nextToken": "cycle"}, "pagination_token"),
        ({"items": [], "nextToken": ""}, "pagination_token"),
        ({"items": [], "nextToken": 7}, "pagination_token"),
        ({"items": [], "nextToken": "é" * 4097}, "pagination_token"),
        (
            {"items": [{"microvmId": "synthetic-terminal", "state": "TERMINATED"}]},
            "duplicate_identity",
        ),
        (RuntimeError("synthetic-provider-diagnostic"), "api_failed"),
    ],
)
def test_failed_scan_retains_prior_counts_digest_and_other_evidence(
    history, second, code
):
    terminal = {"microvmId": "synthetic-terminal", "state": "TERMINATED"}
    retained = {"microvmId": "synthetic-live", "state": "RUNNING"}
    result = history.observe(
        responses=[{"items": [terminal, retained], "nextToken": "cycle"}, second]
    )
    assert result["status"] == "incomplete"
    assert result["sections"]["microvms"] == "incomplete"
    assert_other_evidence(result)
    assert result["microvms"] == [
        {"sha256": identity("microvm", retained["microvmId"]), "state": "RUNNING"}
    ]
    coverage = result["microvmCoverage"]
    assert coverage["processedCount"] == 2
    assert coverage["terminatedCount"] == coverage["retainedCount"] == 1
    assert coverage["terminatedIdentitySha256"] == terminal_digest([terminal])
    assert coverage["paginationComplete"] is False
    assert coverage["pagesScanned"] == (1 if isinstance(second, Exception) else 2)
    assert result["errors"][-1] == {
        "section": "microvms",
        "code": code,
        "operation": "list_microvms",
    }
    assert "synthetic-provider-diagnostic" not in json.dumps(result)
    assert recovery.validate_observation(result) is result


@pytest.mark.parametrize("exception", [InterruptedError, KeyboardInterrupt])
def test_cancelled_scan_preserves_prior_counts_and_digest(history, exception):
    terminal = {"microvmId": "synthetic-terminal", "state": "TERMINATED"}
    history.plane.list_microvms.side_effect = [
        {"items": [terminal], "nextToken": "next"},
        exception("synthetic-interruption"),
    ]
    result = initial_report()
    with pytest.raises(exception):
        recovery.observe({"region": REGION, "resources": []}, {}, history.api, result)
    assert result["microvmCoverage"]["processedCount"] == 1
    assert result["microvmCoverage"]["terminatedCount"] == 1
    assert result["microvmCoverage"]["terminatedIdentitySha256"] == terminal_digest(
        [terminal]
    )
    assert result["microvmCoverage"]["paginationComplete"] is False
    assert result["sections"]["microvms"] == "incomplete"
    assert_other_evidence(result)
    assert recovery.validate_observation(result) is result


@pytest.mark.parametrize("exception", [InterruptedError, KeyboardInterrupt])
def test_verifier_cancellation_preserves_empty_unscanned_history(
    history, monkeypatch, exception
):
    monkeypatch.setattr(
        recovery.subprocess,
        "run",
        Mock(side_effect=exception("synthetic-cancellation")),
    )
    result = initial_report()
    with pytest.raises(exception):
        recovery.observe({"region": REGION, "resources": []}, {}, history.api, result)
    assert result["microvmCoverage"] == {
        "pagesScanned": 0,
        "processedCount": 0,
        "terminatedCount": 0,
        "retainedCount": 0,
        "terminatedIdentitySha256": EMPTY_DIGEST,
        "paginationComplete": False,
    }
    assert result["sections"]["independentVerifier"] == "incomplete"
    assert all(
        result["sections"][section] == "not_run"
        for section in ("images", "versions", "logGroups", "microvms")
    )
    assert history.calls == []
    for method in vars(history.plane).values():
        method.assert_not_called()
    history.logs.describe_log_groups.assert_not_called()
    assert recovery.validate_observation(result) is result


@pytest.mark.parametrize("exception", [InterruptedError, KeyboardInterrupt])
def test_cancellation_after_retained_append_keeps_matching_counts(
    history, monkeypatch, exception
):
    original_append = recovery.observation_append

    def append_then_cancel(report, section, row):
        original_append(report, section, row)
        if section == "microvms":
            raise exception("synthetic-post-append-cancellation")

    monkeypatch.setattr(recovery, "observation_append", append_then_cancel)
    terminal = {"microvmId": "synthetic-terminal", "state": "TERMINATED"}
    live = {"microvmId": "synthetic-live", "state": "RUNNING"}
    history.plane.list_microvms.side_effect = [{"items": [terminal, live]}]
    result = initial_report()
    with pytest.raises(exception):
        recovery.observe({"region": REGION, "resources": []}, {}, history.api, result)
    assert_other_evidence(result)
    assert result["microvms"] == [
        {"sha256": identity("microvm", live["microvmId"]), "state": "RUNNING"}
    ]
    assert result["microvmCoverage"] == {
        "pagesScanned": 1,
        "processedCount": 2,
        "terminatedCount": 1,
        "retainedCount": 1,
        "terminatedIdentitySha256": terminal_digest([terminal]),
        "paginationComplete": False,
    }
    assert result["sections"]["microvms"] == "incomplete"
    assert recovery.validate_observation(result) is result


def test_oversize_vm_page_fails_before_accepting_its_rows(history):
    records = [
        {"microvmId": f"synthetic-terminal-{i}", "state": "TERMINATED"}
        for i in range(51)
    ]
    result = history.observe(responses=[{"items": records}])
    assert result["status"] == "incomplete"
    assert result["microvmCoverage"]["processedCount"] == 0
    assert result["microvmCoverage"]["pagesScanned"] == 1
    assert result["microvmCoverage"]["terminatedIdentitySha256"] == terminal_digest([])
    assert result["microvmCoverage"]["paginationComplete"] is False
    assert result["errors"][-1] == {
        "section": "microvms",
        "code": "observation_limit",
        "operation": "list_microvms",
    }
    assert_other_evidence(result)


@pytest.mark.parametrize(
    "first_state,second_state", [("TERMINATED", "RUNNING"), ("RUNNING", "TERMINATED")]
)
def test_duplicate_identity_across_compacted_and_retained_rows_fails(
    history, first_state, second_state
):
    result = history.observe(
        responses=[
            {
                "items": [{"microvmId": "synthetic-duplicate", "state": first_state}],
                "nextToken": "next",
            },
            {"items": [{"microvmId": "synthetic-duplicate", "state": second_state}]},
        ]
    )
    coverage = result["microvmCoverage"]
    assert result["status"] == "incomplete"
    assert coverage["processedCount"] == 1
    assert coverage["terminatedCount"] == int(first_state == "TERMINATED")
    assert coverage["retainedCount"] == int(first_state == "RUNNING")
    assert result["errors"][-1]["code"] == "duplicate_identity"
    assert recovery.validate_observation(result) is result


def test_retained_vm_budget_cannot_erase_prior_resource_evidence(history):
    terminal = [{"microvmId": "synthetic-terminal", "state": "TERMINATED"}]
    retained = [
        {"microvmId": f"synthetic-live-{i}", "state": "RUNNING"} for i in range(1000)
    ]
    result = history.observe(terminal + retained)
    assert result["status"] == "incomplete"
    assert_other_evidence(result)
    total = sum(
        len(result[field])
        for field in ("images", "versions", "versionCoverage", "logGroups", "microvms")
    )
    assert total == 1000
    coverage = result["microvmCoverage"]
    assert coverage["retainedCount"] == len(result["microvms"])
    assert coverage["processedCount"] == coverage["retainedCount"] + 1
    assert coverage["terminatedCount"] == 1
    assert coverage["terminatedIdentitySha256"] == terminal_digest(terminal)
    assert coverage["paginationComplete"] is False
    assert result["errors"][-1]["code"] == "observation_limit"
    assert recovery.validate_observation(result) is result


def test_deadline_after_first_vm_page_preserves_partial_history(history):
    terminal = {"microvmId": "synthetic-terminal", "state": "TERMINATED"}

    def first_page(**kwargs):
        history.api.clock = lambda: history.api.deadline + 1
        return {"items": [terminal], "nextToken": "next"}

    history.plane.list_microvms.side_effect = first_page
    result = history.observe()
    assert result["status"] == "incomplete"
    assert_other_evidence(result)
    assert history.plane.list_microvms.call_count == 1
    coverage = result["microvmCoverage"]
    assert coverage["processedCount"] == coverage["terminatedCount"] == 1
    assert coverage["pagesScanned"] == 1
    assert coverage["terminatedIdentitySha256"] == terminal_digest([terminal])
    assert coverage["paginationComplete"] is False
    assert result["errors"][-1]["code"] == "deadline"
    assert recovery.validate_observation(result) is result


def test_maximum_utf8_page_token_is_accepted_without_retaining_it(history):
    token = "é" * 4096
    result = history.observe(
        responses=[{"items": [], "nextToken": token}, {"items": []}]
    )
    assert result["status"] == "completed"
    assert result["microvmCoverage"]["pagesScanned"] == 2
    assert history.plane.list_microvms.call_args_list[-1].kwargs == {
        "maxResults": 50,
        "nextToken": token,
    }
    assert token not in json.dumps(result)
    assert recovery.validate_observation(result) is result


@pytest.mark.parametrize("has_more", [False, True])
def test_terminal_history_page_ceiling_requires_natural_end(history, has_more):
    records = [
        {"microvmId": f"synthetic-terminal-{i}", "state": "TERMINATED"}
        for i in range(50000)
    ]
    responses = pages(records)
    if has_more:
        responses[-1]["nextToken"] = "over-ceiling"
    result = history.observe(responses=responses)
    assert result["status"] == ("incomplete" if has_more else "completed")
    assert_other_evidence(result)
    assert history.plane.list_microvms.call_count == 1000
    assert result["microvmCoverage"] == {
        "processedCount": 50000,
        "terminatedCount": 50000,
        "retainedCount": 0,
        "pagesScanned": 1000,
        "terminatedIdentitySha256": terminal_digest(records),
        "paginationComplete": not has_more,
    }
    if has_more:
        assert result["errors"][-1] == {
            "section": "microvms",
            "code": "observation_limit",
            "operation": "list_microvms",
        }
    assert recovery.validate_observation(result) is result


@pytest.mark.parametrize(
    "field", ["processedCount", "terminatedCount", "retainedCount", "pagesScanned"]
)
@pytest.mark.parametrize("invalid", [-1, True, 0.5, "1", None])
def test_history_coverage_requires_nonnegative_integer_counts(history, field, invalid):
    result = history.observe([])
    result["microvmCoverage"][field] = invalid
    with pytest.raises(recovery.Refused, match="^receipt_schema$"):
        recovery.validate_observation(result)


@pytest.mark.parametrize(
    "change",
    [
        {"processedCount": 1},
        {"terminatedCount": 1},
        {"retainedCount": 1},
        {"processedCount": 1, "retainedCount": 1},
        {"pagesScanned": 0},
        {"pagesScanned": 1001},
        {"processedCount": 51, "terminatedCount": 51},
        {"processedCount": 50001, "terminatedCount": 50001, "pagesScanned": 1000},
        {"terminatedIdentitySha256": "a" * 64},
        {"terminatedIdentitySha256": "not-a-hash"},
        {"paginationComplete": False},
        {"paginationComplete": 1},
        {"extra": 1},
    ],
)
def test_forged_history_coverage_cannot_claim_completion(history, change):
    result = history.observe([])
    result["microvmCoverage"].update(change)
    with pytest.raises(recovery.Refused, match="^receipt_schema$"):
        recovery.validate_observation(result)


def test_positive_terminal_count_cannot_use_empty_identity_digest(history):
    result = history.observe(
        [{"microvmId": "synthetic-terminal", "state": "TERMINATED"}]
    )
    result["microvmCoverage"]["terminatedIdentitySha256"] = terminal_digest([])
    with pytest.raises(recovery.Refused, match="^receipt_schema$"):
        recovery.validate_observation(result)


@pytest.mark.parametrize(
    "field",
    [
        "processedCount",
        "terminatedCount",
        "retainedCount",
        "pagesScanned",
        "terminatedIdentitySha256",
        "paginationComplete",
    ],
)
def test_history_coverage_requires_every_fixed_field(history, field):
    result = history.observe([])
    del result["microvmCoverage"][field]
    with pytest.raises(recovery.Refused, match="^receipt_schema$"):
        recovery.validate_observation(result)


@pytest.mark.parametrize("invalid", [None, [], "coverage", False])
def test_history_coverage_requires_object(history, invalid):
    result = history.observe([])
    result["microvmCoverage"] = invalid
    with pytest.raises(recovery.Refused, match="^receipt_schema$"):
        recovery.validate_observation(result)


def test_v2_complete_receipt_requires_history_coverage(history):
    result = history.observe([])
    del result["microvmCoverage"]
    with pytest.raises(recovery.Refused, match="^receipt_schema$"):
        recovery.validate_observation(result)


def test_v2_never_serializes_terminal_vms_as_retained_rows(history):
    result = history.observe([{"microvmId": "synthetic-live", "state": "RUNNING"}])
    result["microvms"][0]["state"] = "TERMINATED"
    with pytest.raises(recovery.Refused, match="^receipt_schema$"):
        recovery.validate_observation(result)


@pytest.mark.parametrize("terminal", [False, True])
def test_v1_receipts_preserve_legacy_terminal_rows_and_acceptance(history, terminal):
    result = history.observe([{"microvmId": "synthetic-live", "state": "RUNNING"}])
    result["schemaVersion"] = 1
    result.pop("microvmCoverage", None)
    if terminal:
        result["microvms"][0]["state"] = "TERMINATED"
    assert recovery.validate_observation(result) is result


def test_v1_schema_does_not_accept_v2_coverage(history):
    result = history.observe([])
    result["schemaVersion"] = 1
    with pytest.raises(recovery.Refused, match="^receipt_schema$"):
        recovery.validate_observation(result)


@pytest.mark.parametrize("with_binding", [False, True])
def test_v1_incomplete_preflight_receipt_remains_valid(with_binding):
    result = initial_report()
    if not with_binding:
        for key in (
            "sourceRun",
            "sourceSha",
            "region",
            "manifestSha256",
            "parserVersion",
            "sourceLogRepresentation",
        ):
            del result[key]
    assert recovery.validate_observation(result) is result


def test_v2_minimal_incomplete_receipt_remains_valid():
    result = minimal_report()
    assert recovery.validate_observation(result) is result


@pytest.mark.parametrize("version", [True, False, 0, -1, 3, 1.0, 2.0])
def test_minimal_receipt_requires_known_integer_schema_version(version):
    result = minimal_report(version)
    with pytest.raises(recovery.Refused, match="^receipt_schema$"):
        recovery.validate_observation(result)


@pytest.mark.parametrize("version", [1, 2])
def test_minimal_receipt_cannot_carry_verifier_without_sections(version):
    result = minimal_report(version)
    result["independentVerifier"] = {
        "resources": [],
        "summary_present": True,
        "leakCount": 0,
        "exitCode": 0,
    }
    with pytest.raises(recovery.Refused, match="^receipt_schema$"):
        recovery.validate_observation(result)


@pytest.mark.parametrize("section", ["sections", "microvms"])
def test_partial_collection_metadata_requires_history_coverage(section):
    result = minimal_report()
    result[section] = (
        {key: "not_run" for key in recovery.OBSERVATION_SECTIONS}
        if section == "sections"
        else []
    )
    with pytest.raises(recovery.Refused, match="^receipt_schema$"):
        recovery.validate_observation(result)


@pytest.mark.parametrize("missing", ["sections", "microvms"])
def test_history_coverage_requires_both_collection_fields(
    history, monkeypatch, missing
):
    monkeypatch.setattr(
        recovery.subprocess, "run", Mock(side_effect=RuntimeError("synthetic-failure"))
    )
    result = history.observe([])
    assert result["status"] == "incomplete"
    assert "independentVerifier" not in result
    del result[missing]
    with pytest.raises(recovery.Refused, match="^receipt_schema$"):
        recovery.validate_observation(result)
