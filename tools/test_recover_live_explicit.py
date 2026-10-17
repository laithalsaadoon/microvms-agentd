# SPDX-License-Identifier: Apache-2.0
"""An exact maintainer selection extends image authority without extending log scope."""

import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "tools.recover-live", Path(__file__).with_name("recover-live.py")
)
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)
FIXTURES = importlib.util.spec_from_file_location(
    "explicit_fixtures", Path(__file__).with_name("test_recover_live.py")
)
fixtures = importlib.util.module_from_spec(FIXTURES)
FIXTURES.loader.exec_module(fixtures)
NAME = "microvm-cli-slow-ready-01234567-abcdef012345"
GROUP = recovery.SERVICE_PREFIX + NAME
KEY = recovery.identity("image", fixtures.REGION, NAME)


def selected_source():
    return (
        fixtures.source_log()
        + f"real AWS\tverify again after destroy\t2026-10-17T16:00:00Z LEAK image {NAME} in CREATE_FAILED\n"
    )


def manifest():
    value = fixtures.manifest()
    fixtures.set_source_manifest(value, selected_source())
    value["resources"].append(
        {"kind": "image", "proof": "explicit-incident-image", "sha256": KEY}
    )
    value["excluded"].append(recovery.identity("log-group", fixtures.REGION, GROUP))
    return value


def owned(value):
    github = fixtures.FakeGithub()
    github.log_value, steps, _ = fixtures.source_representation(selected_source())
    github.values["actions/jobs/10"]["steps"] = steps
    return recovery.provenance(value, github, fixtures.environment())


def state():
    plane = fixtures.Plane()
    plane.images[fixtures.arn(NAME)] = {
        "imageArn": fixtures.arn(NAME),
        "name": NAME,
        "state": "CREATE_FAILED",
        "createdAt": fixtures.CREATED,
    }
    plane.version_logging[fixtures.arn(NAME)] = {"disabled": {}}
    logs = fixtures.Logs(plane.calls)
    logs.groups[GROUP] = {
        "logGroupName": GROUP,
        "creationTime": int(fixtures.CREATED.timestamp() * 1000),
    }
    clock = fixtures.Clock()
    return plane, logs, recovery.Api(plane, logs, clock=clock, sleep=clock.sleep)


def test_explicit_authority_is_separate_from_the_four_positive_source_proofs():
    positive = recovery.source_ownership(selected_source(), fixtures.REGION)
    assert len(positive) == 4
    assert KEY not in positive
    value = manifest()
    resolved = owned(value)
    assert resolved[KEY] == ("explicit-incident-image", NAME, None)
    assert set(resolved) == {row["sha256"] for row in value["resources"]}
    value["resources"].pop()
    assert owned(value) == positive


def test_explicit_image_deletes_without_authorizing_its_log_group():
    value = manifest()
    plane, logs, api = state()
    report = {}
    recovery.recover(value, owned(value), api, report)
    assert [row for row in plane.calls if row[0] == "delete_image"] == [
        ("delete_image", fixtures.arn(fixtures.SHARED)),
        ("delete_image", fixtures.arn(fixtures.CAPABILITY)),
        ("delete_image", fixtures.arn(NAME)),
    ]
    assert set(plane.images) == {fixtures.arn(fixtures.UNKNOWN)}
    assert GROUP in logs.groups
    assert ("delete_group", GROUP) not in plane.calls
    assert len(report["resources"]) == 5
    assert all(
        row["status"] == "absent" and row["deletionRequested"] is True
        for row in report["resources"]
    )
    plane.calls.clear()
    recovery.recover(value, owned(value), api, report)
    assert not [row for row in plane.calls if row[0].startswith("delete")]
    assert all(
        row["status"] == "absent" and row["deletionRequested"] is False
        for row in report["resources"]
    )


@pytest.mark.parametrize("problem", ["vm", "timestamp"])
def test_explicit_authority_retains_fingerprint_and_dependency_vetoes(problem):
    value = manifest()
    plane, logs, api = state()
    if problem == "vm":
        plane.vms = [
            {
                "microvmId": "attached",
                "imageArn": fixtures.arn(NAME),
                "state": "RUNNING",
            }
        ]
        reason = "image_in_use"
    else:
        plane.images[fixtures.arn(NAME)]["createdAt"] = fixtures.CREATED.replace(
            year=2020
        )
        reason = "outside_source_lifetime"
    with pytest.raises(recovery.Refused, match=f"^{reason}$"):
        recovery.recover(value, owned(value), api, {})
    assert not [row for row in plane.calls if row[0].startswith("delete")]


def test_manifest_hash_and_explicit_observation_must_both_agree():
    value = manifest()
    value["resources"][-1]["sha256"] = "0" * 64
    with pytest.raises(recovery.Refused, match="^explicit_image_unresolved$"):
        owned(value)
    value = manifest()
    github = fixtures.FakeGithub()
    changed = selected_source().replace("verify again after destroy", "different step")
    github.log_value, steps = fixtures.set_source_manifest(value, changed)
    github.values["actions/jobs/10"]["steps"] = steps
    with pytest.raises(
        recovery.Refused,
        match="^source_ownership_incomplete$|^source_capability_ambiguous$",
    ):
        recovery.provenance(value, github, fixtures.environment())


def test_explicit_image_cannot_be_reported_by_independent_verifier_after_cleanup():
    value = manifest()
    with pytest.raises(recovery.Refused, match="^independent_survivor$"):
        recovery.independent_absence(
            value,
            f"LEAK image {NAME} in CREATE_FAILED\naccount {fixtures.REGION}: 1 leaked resource(s)\n",
        )
    assert (
        recovery.independent_absence(
            value,
            f"LEAK log group {GROUP} (ours by prefix)\naccount {fixtures.REGION}: 1 leaked resource(s)\n",
        )["leakCount"]
        == 1
    )


@pytest.mark.parametrize("escape", ["\x1b[m", "\x1b[31m", "\x1b[K"])
def test_cleanup_name_resolution_accepts_lowercase_and_uppercase_ansi(escape):
    line = (
        "real AWS\tverify again after destroy\t"
        f"2026-10-17T16:00:00Z {escape}LEAK image {NAME} in CREATE_FAILED\n"
    )
    assert recovery.source_leaked_images(line) == {NAME}


def test_receipt_validation_retains_legacy_parser_one_and_resource_shape():
    receipt = {
        "kind": "live-recovery",
        "conformance_verified": False,
        "status": "passed",
        "sourceRun": 1,
        "parserVersion": 1,
        "resources": [{"kind": "image", "sha256": KEY, "status": "absent"}],
    }
    assert recovery.validate_receipt(receipt) is receipt
