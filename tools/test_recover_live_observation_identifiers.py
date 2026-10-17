# SPDX-License-Identifier: Apache-2.0
"""Missing List rows still require exact, source-scoped image reads."""

import copy
import json

import pytest
from test_recover_live_observation import (
    ARN,
    NAME,
    REGION,
    recovery,
    report,
)
from test_recover_live_observation import (
    census as census,
)

assert recovery.__name__ == "tools.recover-live"

CAPABILITY = "synthetic-capability"
EXPLICIT = "synthetic-explicit"


def arn(name):
    return f"arn:aws:lambda:{REGION}:000000000000:microvm-image:{name}"


def not_found():
    error = RuntimeError("synthetic-private-provider-diagnostic")
    error.response = {"Error": {"Code": "ResourceNotFoundException"}}
    return error


@pytest.fixture
def unlisted(census):
    census.plane.list_microvm_images.return_value = {"items": []}
    for proof, name in (
        ("capability-default-image", CAPABILITY),
        ("explicit-incident-image", EXPLICIT),
    ):
        key = recovery.identity("image", REGION, name)
        census.manifest["resources"].append({"kind": "image", "sha256": key})
        census.owned[key] = (proof, name, None)

    def exact_read(imageIdentifier):
        assert imageIdentifier in {ARN, arn(CAPABILITY), arn(EXPLICIT)}
        raise not_found()

    census.plane.get_microvm_image.side_effect = exact_read
    return census


def test_unlisted_name_only_images_use_exact_source_namespace_reads(unlisted):
    before = copy.deepcopy(unlisted.owned)
    value = report()
    recovery.observe(unlisted.manifest, unlisted.owned, unlisted.api, value)
    assert value["status"] == "completed"
    assert value["errors"] == []
    assert {
        call.kwargs["imageIdentifier"]
        for call in unlisted.plane.get_microvm_image.call_args_list
    } == {ARN, arn(CAPABILITY), arn(EXPLICIT)}
    assert unlisted.plane.get_microvm_image.call_count == 3
    assert unlisted.owned == before
    for row in value["images"]:
        assert row["approved"] is True
        assert row["getOutcome"] == "not_found"
        assert row["identityMatches"] is None
        has_source = row["imageSha256"] == recovery.identity("image", REGION, NAME)
        assert ("requestedArnSha256" in row) is has_source
        assert ("sourceIdentityMatches" in row) is has_source
    assert value["versions"] == value["versionCoverage"] == []
    assert value["recoveryAccepted"] is value["mutationsPerformed"] is False
    unlisted.plane.delete_microvm_image.assert_not_called()
    unlisted.logs.delete_log_group.assert_not_called()
    assert recovery.validate_observation(value) is value
    assert all(
        secret not in json.dumps(value) for secret in (ARN, NAME, CAPABILITY, EXPLICIT)
    )


@pytest.mark.parametrize(
    "source_arn",
    [
        None,
        ARN.replace("arn:aws:", "arn:aws-cn:"),
        ARN.replace(":lambda:", ":s3:"),
        ARN.replace(REGION, "us-west-2"),
        ARN.replace("000000000000", "00000000000"),
        ARN.replace("000000000000", "０" * 12),
        ARN.replace(":microvm-image:", ":microvm-image/"),
        ARN.replace(":microvm-image:", ":microvm:"),
        ARN + "-different",
    ],
)
def test_source_namespace_must_be_exact_canonical_image_arn(source_arn, census):
    key = recovery.identity("image", REGION, NAME)
    census.owned[key] = ("shared-image", NAME, source_arn)
    with pytest.raises(recovery.Refused, match="^observation_identity$"):
        recovery.observation_image_arn(CAPABILITY, REGION, census.owned)


@pytest.mark.parametrize("problem", ["missing", "different-proof", "ambiguous"])
def test_only_one_authenticated_shared_image_supplies_namespace(census, problem):
    key = recovery.identity("image", REGION, NAME)
    if problem == "missing":
        census.owned.clear()
    elif problem == "different-proof":
        census.owned[key] = ("explicit-incident-image", NAME, ARN)
    else:
        census.owned["different"] = ("shared-image", EXPLICIT, arn(EXPLICIT))
    with pytest.raises(recovery.Refused, match="^observation_identity$"):
        recovery.observation_image_arn(CAPABILITY, REGION, census.owned)


@pytest.mark.parametrize("name", [None, "", "a/b", "a:b", "a.b", "a" * 65, "name\n"])
def test_derived_image_name_is_one_exact_service_name(census, name):
    with pytest.raises(recovery.Refused, match="^observation_identity$"):
        recovery.observation_image_arn(name, REGION, census.owned)


@pytest.mark.parametrize(
    "source_name,target_name", [(NAME, "Mixed_Target9"), ("Mixed_Source9", CAPABILITY)]
)
def test_valid_uppercase_names_are_preserved_exactly(census, source_name, target_name):
    owned = {
        recovery.identity("image", REGION, source_name): (
            "shared-image",
            source_name,
            arn(source_name),
        )
    }
    assert recovery.observation_image_arn(target_name, REGION, owned) == arn(
        target_name
    )


@pytest.mark.parametrize("name", [None, "", "a/b", "a:b", "a.b", "a" * 65, "name\n"])
def test_source_image_suffix_is_one_exact_service_name(census, name):
    key = recovery.identity("image", REGION, NAME)
    census.owned[key] = ("shared-image", name, arn(name))
    with pytest.raises(recovery.Refused, match="^observation_identity$"):
        recovery.observation_image_arn(CAPABILITY, REGION, census.owned)


def test_unresolvable_namespace_is_incomplete_without_name_fallback(unlisted):
    key = recovery.identity("image", REGION, NAME)
    unlisted.owned[key] = ("shared-image", NAME, ARN.replace(REGION, "us-west-2"))
    value = report()
    recovery.observe(unlisted.manifest, unlisted.owned, unlisted.api, value)
    assert value["status"] == "incomplete"
    assert value["sections"]["images"] == "incomplete"
    calls = unlisted.plane.get_microvm_image.call_args_list
    assert len(calls) == 1
    assert calls[0].kwargs["imageIdentifier"] == unlisted.owned[key][2]
    assert [
        row for row in value["errors"] if row["code"] == "observation_identity"
    ] == [
        {
            "section": "images",
            "code": "observation_identity",
            "imageSha256": recovery.identity("image", REGION, name),
        }
        for name in (CAPABILITY, EXPLICIT)
    ]
    assert recovery.validate_observation(value) is value


@pytest.mark.parametrize("code", ["AccessDeniedException", "ValidationException"])
def test_derived_get_failure_never_becomes_absence(unlisted, code):
    def denied(imageIdentifier):
        if imageIdentifier == arn(CAPABILITY):
            error = not_found()
            error.response["Error"]["Code"] = code
            raise error
        raise not_found()

    unlisted.plane.get_microvm_image.side_effect = denied
    value = report()
    recovery.observe(unlisted.manifest, unlisted.owned, unlisted.api, value)
    row = next(
        row
        for row in value["images"]
        if row["imageSha256"] == recovery.identity("image", REGION, CAPABILITY)
    )
    assert row["getOutcome"] == "api_error"
    assert value["status"] == value["sections"]["images"] == "incomplete"
    assert value["errors"] == [
        {
            "section": "images",
            "code": "api_failed",
            "imageSha256": row["imageSha256"],
            "operation": "get_microvm_image",
        }
    ]
    assert "synthetic-private-provider-diagnostic" not in json.dumps(value)
    assert recovery.validate_observation(value) is value


@pytest.mark.parametrize("change", [None, "name", "imageArn"])
@pytest.mark.parametrize("direct_source", [False, True])
def test_get_observed_after_missing_list_keeps_version_census_incomplete(
    unlisted, change, direct_source
):
    target = NAME if direct_source else CAPABILITY

    def observed(imageIdentifier):
        if imageIdentifier != arn(target):
            raise not_found()
        row = {"name": target, "imageArn": arn(target), "state": "CREATED"}
        if change:
            row[change] += "-different"
        return row

    unlisted.plane.get_microvm_image.side_effect = observed
    value = report()
    recovery.observe(unlisted.manifest, unlisted.owned, unlisted.api, value)
    row = next(
        row
        for row in value["images"]
        if row["imageSha256"] == recovery.identity("image", REGION, target)
    )
    assert row["getOutcome"] == "observed"
    if not direct_source:
        assert row["identityMatches"] is (change is None)
        assert "sourceIdentityMatches" not in row
    else:
        assert row["sourceIdentityMatches"] is (change is None)
    assert value["status"] == "incomplete"
    assert value["sections"]["images"] == value["sections"]["versions"] == "incomplete"
    assert value["versionCoverage"] == []
    assert value["errors"] == [
        {
            "section": "images",
            "code": "observation_identity",
            "imageSha256": row["imageSha256"],
        }
    ]
    assert recovery.validate_observation(value) is value


def test_cancellation_during_derived_read_preserves_only_prior_evidence(unlisted):
    def interrupted(imageIdentifier):
        assert imageIdentifier == arn(CAPABILITY)
        raise KeyboardInterrupt

    unlisted.plane.get_microvm_image.side_effect = interrupted
    value = report()
    with pytest.raises(KeyboardInterrupt):
        recovery.observe(unlisted.manifest, unlisted.owned, unlisted.api, value)
    assert value["status"] == "incomplete"
    assert len(value["images"]) == 1
    assert value["images"][0]["imageSha256"] == recovery.identity(
        "image", REGION, CAPABILITY
    )
    assert value["images"][0]["getOutcome"] == "api_error"
    unlisted.plane.delete_microvm_image.assert_not_called()
    unlisted.logs.delete_log_group.assert_not_called()
    assert recovery.validate_observation(value) is value


def test_unlisted_get_after_prior_image_error_still_lacks_version_coverage(unlisted):
    def current(imageIdentifier):
        if imageIdentifier == arn(CAPABILITY):
            raise RuntimeError("synthetic-private-provider-diagnostic")
        if imageIdentifier == arn(EXPLICIT):
            return {"name": EXPLICIT, "imageArn": arn(EXPLICIT), "state": "CREATED"}
        raise not_found()

    unlisted.plane.get_microvm_image.side_effect = current
    value = report()
    recovery.observe(unlisted.manifest, unlisted.owned, unlisted.api, value)
    assert value["status"] == "incomplete"
    assert value["sections"]["images"] == value["sections"]["versions"] == "incomplete"
    assert value["versionCoverage"] == []
    assert recovery.validate_observation(value) is value
