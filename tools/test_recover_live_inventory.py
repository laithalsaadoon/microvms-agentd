# SPDX-License-Identifier: Apache-2.0
"""Offline inventory, dependency and convergence verdicts for reviewed recovery."""

from __future__ import annotations

import copy
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

SPEC = importlib.util.spec_from_file_location(
    "tools.recover-live", Path(__file__).with_name("recover-live.py")
)
assert SPEC and SPEC.loader
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)
REGION = "us-east-1"
CREATED = datetime(2026, 10, 17, 16, tzinfo=timezone.utc)
ARN = "arn:aws:lambda:us-east-1:000000000000:microvm-image:reviewed"
GROUP = "/aws/lambda-microvms/reviewed"


class Clock:
    def __init__(self):
        self.now = 0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        assert self.now <= 100, "recovery never converged"


@pytest.fixture
def setup():
    clock = Clock()
    plane = SimpleNamespace()
    logs = SimpleNamespace()
    api = recovery.Api(plane, logs, clock=clock, sleep=clock.sleep)
    image = {
        "imageArn": ARN,
        "name": "reviewed",
        "state": "CREATED",
        "createdAt": CREATED,
    }
    group = {"logGroupName": GROUP, "creationTime": CREATED.timestamp() * 1000}
    manifest = {
        "region": REGION,
        "source_job_started_at": (CREATED - timedelta(seconds=1)).isoformat(),
        "source_job_completed_at": (CREATED + timedelta(seconds=1)).isoformat(),
        "source_infrastructure": {"bucket_sha256": "a" * 64},
        "resources": [
            {"kind": "image", "sha256": "image-key", "proof": "shared-image"},
            {"kind": "log-group", "sha256": "group-key", "proof": "shared-log"},
        ],
    }
    ownership = {
        "image-key": ("shared-image", "reviewed", ARN),
        "group-key": ("shared-log", GROUP, None),
    }
    plane.list_microvm_images = Mock(return_value={"items": [image]})
    plane.list_microvms = Mock(return_value={"items": []})
    plane.get_microvm_image = Mock(return_value=image)
    plane.list_microvm_image_versions = Mock(return_value={"items": []})
    logs.describe_log_groups = Mock(return_value={"logGroups": [group]})
    return SimpleNamespace(
        api=api,
        clock=clock,
        plane=plane,
        logs=logs,
        image=image,
        group=group,
        manifest=manifest,
        ownership=ownership,
    )


@pytest.mark.parametrize(
    "page,reason",
    [
        ({}, "missing_page_items"),
        ({"items": None}, "missing_page_items"),
        ({"items": {}}, "missing_page_items"),
        ({"items": [None]}, "invalid_page_item"),
        ({"items": [1]}, "invalid_page_item"),
        ({"items": [], "nextToken": ""}, "pagination_token"),
        ({"items": [], "nextToken": 1}, "pagination_token"),
        ({"items": [], "nextToken": False}, "pagination_token"),
    ],
)
def test_malformed_page_has_exact_failure_verdict(setup, page, reason):
    setup.plane.list_microvms = Mock(side_effect=[page, page])
    with pytest.raises(recovery.Refused, match=f"^{reason}$"):
        setup.api.pages(setup.plane, "list_microvms", "items")


def test_pagination_keeps_arguments_and_every_page_and_rejects_cycle(setup):
    setup.logs.describe_log_groups.side_effect = [
        {"logGroups": [{"id": "first"}], "nextToken": "next"},
        {"logGroups": [{"id": "last"}]},
    ]
    assert setup.api.pages(
        setup.logs, "describe_log_groups", "logGroups", logGroupNamePrefix=GROUP
    ) == [
        {"id": "first"},
        {"id": "last"},
    ]
    assert setup.logs.describe_log_groups.call_args_list == [
        ((), {"logGroupNamePrefix": GROUP}),
        ((), {"logGroupNamePrefix": GROUP, "nextToken": "next"}),
    ]
    # A broken cycle check must fail with a wrong verdict, not loop indefinitely.
    setup.plane.list_microvms = Mock(
        side_effect=[{"items": [], "nextToken": "cycle"}] * 2
    )
    with pytest.raises(recovery.Refused, match="^pagination_token$"):
        setup.api.pages(setup.plane, "list_microvms", "items")
    assert setup.plane.list_microvms.call_count == 2


@pytest.mark.parametrize(
    "rows,reason",
    [
        ([{}], "missing_text"),
        ([{"id": ""}], "missing_text"),
        ([{"id": 7}], "missing_text"),
        ([{"id": "same"}, {"id": "same"}], "duplicate_identity"),
    ],
)
def test_unique_refuses_unknown_or_ambiguous_identity(rows, reason):
    with pytest.raises(recovery.Refused, match=f"^{reason}$"):
        recovery.Api.unique(rows, "id")


def test_inventory_preserves_all_collections_and_scopes_group_read(setup):
    vm = {"microvmId": "vm", "imageArn": ARN, "state": "TERMINATED"}
    setup.plane.list_microvms.return_value = {"items": [vm]}
    assert setup.api.inventory() == (
        {ARN: setup.image},
        {"vm": vm},
        {GROUP: setup.group},
    )
    setup.logs.describe_log_groups.assert_called_once_with(
        logGroupNamePrefix="/aws/lambda-microvms/"
    )
    setup.plane.list_microvms.assert_called_once_with()
    setup.plane.list_microvm_images.assert_called_once_with()


@pytest.mark.parametrize(
    "collection,field,value,reason",
    [
        ("image", "name", None, "missing_text"),
        ("image", "state", None, "image_state"),
        ("image", "state", "UNKNOWN", "image_state"),
        ("vm", "imageArn", "", "missing_text"),
        ("vm", "state", None, "vm_state"),
        ("vm", "state", "UNKNOWN", "vm_state"),
    ],
)
def test_inventory_uncertainty_has_exact_failure_verdict(
    setup, collection, field, value, reason
):
    if collection == "image":
        setup.image[field] = value
    else:
        vm = {"microvmId": "vm", "imageArn": ARN, "state": "RUNNING", field: value}
        setup.plane.list_microvms.return_value = {"items": [vm]}
    with pytest.raises(recovery.Refused, match=f"^{reason}$"):
        setup.api.inventory()


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("imageArn", "wrong", "version_identity"),
        ("imageVersion", "wrong", "version_identity"),
        ("state", None, "version_state"),
        ("state", "UNKNOWN", "version_state"),
        ("logging", None, "version_logging_missing"),
        ("logging", {}, "version_logging_ambiguous"),
        ("logging", {"disabled": True}, "version_logging_ambiguous"),
        ("logging", {"disabled": []}, "version_logging_ambiguous"),
        ("logging", {"disabled": {"extra": 1}}, "version_logging_ambiguous"),
        (
            "logging",
            {"disabled": {}, "cloudWatch": {}},
            "version_logging_ambiguous",
        ),
        ("logging", {"disabled": True, "cloudWatch": {}}, "version_logging_ambiguous"),
        ("logging", {"disabled": False}, "version_logging_ambiguous"),
        ("logging", {"cloudWatch": None}, "version_logging_ambiguous"),
        ("logging", {"cloudWatch": {}, "extra": 1}, "version_logging_ambiguous"),
        ("logging", {"cloudWatch": {}}, "missing_text"),
        ("logging", {"cloudWatch": {"logGroup": GROUP}}, "configured_group_in_use"),
    ],
)
def test_dependency_metadata_uncertainty_refuses_group_deletion(
    setup, field, value, reason
):
    record = {
        "imageArn": ARN,
        "imageVersion": "1",
        "state": "SUCCESSFUL",
        "createdAt": CREATED,
        "logging": {"disabled": {}},
        field: value,
    }
    setup.plane.list_microvm_image_versions.return_value = {
        "items": [{"imageVersion": "1"}]
    }
    setup.plane.get_microvm_image_version = Mock(return_value=record)
    with pytest.raises(recovery.Refused, match=f"^{reason}$"):
        setup.api.configured_dependencies(GROUP, {ARN: setup.image}, set())
    setup.plane.list_microvm_image_versions.assert_called_once_with(imageIdentifier=ARN)
    setup.plane.get_microvm_image_version.assert_called_once_with(
        imageIdentifier=ARN, imageVersion="1"
    )


@pytest.mark.parametrize("first", ["DELETED", "disabled"])
def test_skippable_version_does_not_hide_later_dependency(setup, first):
    setup.plane.list_microvm_image_versions.return_value = {
        "items": [{"imageVersion": "1"}, {"imageVersion": "2"}]
    }

    def version(*, imageIdentifier, imageVersion):
        assert imageIdentifier == ARN
        row = {
            "imageArn": ARN,
            "imageVersion": imageVersion,
            "state": "SUCCESSFUL",
            "createdAt": CREATED,
            "logging": {"cloudWatch": {"logGroup": GROUP}},
        }
        if imageVersion == "1":
            if first == "DELETED":
                row.update(state="DELETED", logging=None)
            else:
                row["logging"] = {"disabled": {}}
        return row

    setup.plane.get_microvm_image_version = Mock(side_effect=version)
    with pytest.raises(recovery.Refused, match="^configured_group_in_use$"):
        setup.api.configured_dependencies(GROUP, {ARN: setup.image}, set())
    assert setup.plane.get_microvm_image_version.call_count == 2


@pytest.mark.parametrize("destination,deleting", [("unrelated", set()), (GROUP, {ARN})])
def test_dependencies_allow_unrelated_destination_or_reviewed_image_delete(
    setup, destination, deleting
):
    setup.api.region = REGION
    setup.plane.list_microvm_image_versions.return_value = {
        "items": [{"imageVersion": "v"}]
    }
    record = {
        "imageArn": ARN,
        "imageVersion": "v",
        "state": "SUCCESSFUL",
        "createdAt": CREATED,
        "logging": {"cloudWatch": {"logGroup": destination}},
    }
    setup.plane.get_microvm_image_version = Mock(return_value=record)
    setup.api.configured_dependencies(GROUP, {ARN: setup.image}, deleting)
    assert setup.api.version_evidence == [
        {
            "imageSha256": recovery.identity("image", REGION, "reviewed"),
            "versionSha256": recovery.digest(b"v"),
            "createdAt": CREATED.timestamp(),
            "logGroupSha256": recovery.identity("log-group", REGION, destination),
        }
    ]


@pytest.mark.parametrize(
    "record",
    [
        {"imageArn": "wrong", "state": "CREATED"},
        {"imageArn": ARN, "state": "UNKNOWN"},
        {},
    ],
)
def test_image_absence_needs_not_found_not_malformed_success(setup, record):
    setup.plane.get_microvm_image.return_value = record
    with pytest.raises(recovery.Refused, match="^image_readback$"):
        setup.api.image_absent(ARN)


@pytest.mark.parametrize("state", ["CREATED", "DELETING", "DELETED"])
def test_even_terminal_image_records_are_present_until_not_found(setup, state):
    setup.image["state"] = state
    assert setup.api.image_absent(ARN) is False
    setup.plane.get_microvm_image.assert_called_once_with(imageIdentifier=ARN)


@pytest.mark.parametrize("reason", ["not_found", "api_failed", "deadline"])
def test_image_absence_accepts_only_exact_not_found(setup, monkeypatch, reason):
    call = Mock(side_effect=recovery.Refused(reason))
    monkeypatch.setattr(setup.api, "call", call)
    if reason == "not_found":
        assert setup.api.image_absent(ARN) is True
    else:
        with pytest.raises(recovery.Refused, match=f"^{reason}$"):
            setup.api.image_absent(ARN)
    call.assert_called_once_with(setup.plane, "get_microvm_image", imageIdentifier=ARN)


def test_wait_for_image_absence_polls_until_the_exact_read_is_gone(setup, monkeypatch):
    absent = Mock(side_effect=[False, False, True])
    monkeypatch.setattr(setup.api, "image_absent", absent)
    setup.api.wait_image_absent(ARN)
    assert absent.call_args_list == [((ARN,), {})] * 3
    assert setup.clock.sleeps == [2, 2]
    assert setup.clock.now == 4


@pytest.mark.parametrize("kind", ["image", "log-group"])
@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_fingerprint_accepts_lifetime_endpoints_and_preserves_identity(
    setup, kind, offset
):
    created = CREATED + timedelta(seconds=offset)
    row = copy.deepcopy(setup.image if kind == "image" else setup.group)
    row["createdAt" if kind == "image" else "creationTime"] = (
        created if kind == "image" else created.timestamp() * 1000
    )
    expected = (
        ("reviewed", ARN, created.timestamp())
        if kind == "image"
        else (GROUP, created.timestamp())
    )
    assert recovery.fingerprint(kind, row, setup.manifest) == expected


@pytest.mark.parametrize(
    "value", [None, True, False, "1700000000000", float("nan"), float("inf")]
)
def test_group_timestamp_uncertainty_has_fixed_refusal(setup, value):
    setup.group["creationTime"] = value
    with pytest.raises(recovery.Refused, match="^group_creation_time$"):
        recovery.fingerprint("log-group", setup.group, setup.manifest)


@pytest.mark.parametrize("kind", ["image", "log-group"])
@pytest.mark.parametrize("offset", [-2, 2])
def test_resource_outside_source_lifetime_is_not_owned(setup, kind, offset):
    created = CREATED + timedelta(seconds=offset)
    row = setup.image if kind == "image" else setup.group
    row["createdAt" if kind == "image" else "creationTime"] = (
        created if kind == "image" else created.timestamp() * 1000
    )
    with pytest.raises(recovery.Refused, match="^outside_source_lifetime$"):
        recovery.fingerprint(kind, row, setup.manifest)


def test_selection_contains_only_reviewed_objects_and_sets_evidence_context(setup):
    plan, state = recovery.selected(setup.manifest, setup.ownership, setup.api)
    assert plan == {
        "image-key": (
            setup.manifest["resources"][0],
            setup.image,
            ("reviewed", ARN, CREATED.timestamp()),
        ),
        "group-key": (
            setup.manifest["resources"][1],
            setup.group,
            (GROUP, CREATED.timestamp()),
        ),
    }
    assert state == ({ARN: setup.image}, {}, {GROUP: setup.group})
    assert setup.api.region == REGION
    assert setup.api.source_infrastructure == {"bucket_sha256": "a" * 64}
    setup.plane.get_microvm_image.assert_called_once_with(imageIdentifier=ARN)


@pytest.mark.parametrize(
    "state", ["PENDING", "RUNNING", "SUSPENDING", "SUSPENDED", "TERMINATING"]
)
@pytest.mark.parametrize("image_phase", [False, True])
def test_every_live_vm_state_blocks_image_selection(setup, state, image_phase):
    setup.plane.list_microvms.return_value = {
        "items": [{"microvmId": "vm", "imageArn": ARN, "state": state}]
    }
    with pytest.raises(recovery.Refused, match="^image_in_use$"):
        recovery.selected(
            setup.manifest, setup.ownership, setup.api, image_phase=image_phase
        )


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("state", "UNKNOWN", "image_readback_state"),
        ("createdAt", CREATED + timedelta(microseconds=1), "image_changed"),
        ("imageArn", "other", "image_changed"),
    ],
)
@pytest.mark.parametrize("image_phase", [False, True])
def test_changed_readback_refuses_image_selection(
    setup, field, value, reason, image_phase
):
    setup.plane.get_microvm_image.return_value = {**setup.image, field: value}
    with pytest.raises(recovery.Refused, match=f"^{reason}$"):
        recovery.selected(
            setup.manifest, setup.ownership, setup.api, image_phase=image_phase
        )


@pytest.mark.parametrize("image_phase", [False, True])
def test_source_arn_cannot_be_replaced_even_when_name_matches(setup, image_phase):
    setup.ownership["image-key"] = ("shared-image", "reviewed", "other")
    with pytest.raises(recovery.Refused, match="^source_arn_changed$"):
        recovery.selected(
            setup.manifest, setup.ownership, setup.api, image_phase=image_phase
        )


def test_absent_resources_remain_absent_without_invented_plan(setup):
    setup.plane.list_microvm_images.return_value = {"items": []}
    setup.logs.describe_log_groups.return_value = {"logGroups": []}
    plan, state = recovery.selected(setup.manifest, setup.ownership, setup.api)
    assert plan == {}
    assert state == ({}, {}, {})
    setup.plane.get_microvm_image.assert_not_called()


@pytest.mark.parametrize("image_phase", [False, True])
def test_settled_selection_retries_only_exact_not_found(
    setup, monkeypatch, image_phase
):
    expected = ({}, ({}, {}, {}))
    choose = Mock(side_effect=[recovery.Refused("not_found"), expected])
    monkeypatch.setattr(recovery, "selected", choose)
    assert (
        recovery.settled_selection(
            setup.manifest, setup.ownership, setup.api, image_phase=image_phase
        )
        == expected
    )
    assert setup.clock.sleeps == [2]
    assert (
        choose.call_args_list
        == [
            ((setup.manifest, setup.ownership, setup.api), {"image_phase": image_phase})
        ]
        * 2
    )
    choose.side_effect = recovery.Refused("api_failed")
    with pytest.raises(recovery.Refused, match="^api_failed$"):
        recovery.settled_selection(
            setup.manifest, setup.ownership, setup.api, image_phase=image_phase
        )
    assert setup.clock.sleeps == [2]


@pytest.mark.parametrize(
    "vm",
    [
        {"microvmId": "terminated", "imageArn": ARN, "state": "TERMINATED"},
        {"microvmId": "unrelated", "imageArn": "other", "state": "RUNNING"},
    ],
)
def test_unrelated_vm_without_logs_or_terminated_vm_does_not_veto_selection(setup, vm):
    setup.plane.list_microvms.return_value = {"items": [vm]}
    if vm["state"] != "TERMINATED":
        setup.logs.describe_log_groups.return_value = {"logGroups": []}
    expected = {"image-key"}
    if vm["state"] == "TERMINATED":
        expected.add("group-key")
    assert (
        set(recovery.selected(setup.manifest, setup.ownership, setup.api)[0])
        == expected
    )


@pytest.mark.parametrize(
    "state", ["PENDING", "RUNNING", "SUSPENDING", "SUSPENDED", "TERMINATING"]
)
@pytest.mark.parametrize("later_page", [False, True])
def test_present_log_refuses_unobservable_vm_destination_on_every_page(
    setup, state, later_page
):
    page = {"items": [{"microvmId": "unrelated", "imageArn": "other", "state": state}]}
    setup.plane.list_microvms.side_effect = (
        [{"items": [], "nextToken": "second"}, page] if later_page else [page]
    )
    with pytest.raises(recovery.Refused, match="^log_vm_dependencies_unobservable$"):
        recovery.selected(setup.manifest, setup.ownership, setup.api)
    assert setup.plane.list_microvms.call_count == (2 if later_page else 1)


@pytest.mark.parametrize("proof", ["shared-log", "configured-log"])
def test_present_log_refuses_unrelated_vm_before_any_deletion(setup, proof):
    setup.manifest["resources"] = [
        {"kind": "log-group", "sha256": "group-key", "proof": proof}
    ]
    setup.ownership = {"group-key": (proof, GROUP, None)}
    setup.plane.list_microvm_images.return_value = {"items": []}
    setup.plane.list_microvms.return_value = {
        "items": [{"microvmId": "unrelated", "imageArn": "other", "state": "RUNNING"}]
    }
    setup.plane.delete_microvm_image = Mock()
    setup.logs.delete_log_group = Mock(
        side_effect=lambda **_: setattr(
            setup.logs.describe_log_groups, "return_value", {"logGroups": []}
        )
    )
    report = {}
    with pytest.raises(recovery.Refused, match="^log_vm_dependencies_unobservable$"):
        recovery.recover(setup.manifest, setup.ownership, setup.api, report)
    setup.plane.delete_microvm_image.assert_not_called()
    setup.logs.delete_log_group.assert_not_called()
    assert report == {
        "resources": [
            {
                "kind": "log-group",
                "sha256": "group-key",
                "status": "present",
                "deletionRequested": False,
            }
        ]
    }


def test_vm_appearing_after_image_deletion_vetoes_exact_log_deletion(setup):
    class Missing(Exception):
        response = {"Error": {"Code": "ResourceNotFoundException"}}

    def delete_image(**_):
        setup.plane.list_microvm_images.return_value = {"items": []}
        return {}

    def get_image(**_):
        if setup.plane.delete_microvm_image.called:
            raise Missing
        return setup.image

    setup.plane.delete_microvm_image = Mock(side_effect=delete_image)
    setup.plane.get_microvm_image.side_effect = get_image
    setup.plane.list_microvms.side_effect = lambda **_: {
        "items": [{"microvmId": "late", "imageArn": "other", "state": "SUSPENDED"}]
        if setup.plane.delete_microvm_image.called
        else []
    }
    setup.logs.delete_log_group = Mock(
        side_effect=lambda **_: setattr(
            setup.logs.describe_log_groups, "return_value", {"logGroups": []}
        )
    )
    report = {}
    with pytest.raises(recovery.Refused, match="^log_vm_dependencies_unobservable$"):
        recovery.recover(setup.manifest, setup.ownership, setup.api, report)
    setup.plane.delete_microvm_image.assert_called_once_with(imageIdentifier=ARN)
    setup.logs.delete_log_group.assert_not_called()
    assert report["resources"] == [
        {
            "kind": "image",
            "sha256": "image-key",
            "status": "absent",
            "deletionRequested": True,
        },
        {
            "kind": "log-group",
            "sha256": "group-key",
            "status": "present",
            "deletionRequested": False,
        },
    ]


def test_selection_checks_every_log_destination_with_exact_deleting_set(
    setup, monkeypatch
):
    dependencies = Mock()
    monkeypatch.setattr(setup.api, "configured_dependencies", dependencies)
    recovery.selected(setup.manifest, setup.ownership, setup.api)
    dependencies.assert_called_once_with(GROUP, {ARN: setup.image}, {ARN})
    del setup.manifest["source_infrastructure"]
    recovery.selected(setup.manifest, setup.ownership, setup.api)
    assert setup.api.source_infrastructure == {}


def plan_for(setup, kind):
    resource = setup.manifest["resources"][kind == "log-group"]
    row = setup.image if kind == "image" else setup.group
    identity = (
        ("reviewed", ARN, CREATED.timestamp())
        if kind == "image"
        else (GROUP, CREATED.timestamp())
    )
    return {resource["sha256"]: (resource, row, identity)}


@pytest.mark.parametrize("kind", ["image", "log-group"])
@pytest.mark.parametrize("change", ["missing", "replaced"])
def test_every_delete_rechecks_the_same_incarnation(setup, monkeypatch, kind, change):
    initial = plan_for(setup, kind)
    current = (
        {}
        if change == "missing"
        else {
            key: (resource, row, (*identity, "different"))
            for key, (resource, row, identity) in initial.items()
        }
    )
    choose = Mock(side_effect=[(initial, ({}, {}, {})), (current, ({}, {}, {}))])
    monkeypatch.setattr(recovery, "settled_selection", choose)
    mutation = Mock()
    monkeypatch.setattr(setup.api, "call", mutation)
    with pytest.raises(recovery.Refused, match="^incarnation_changed$"):
        recovery.recover(setup.manifest, setup.ownership, setup.api, {})
    mutation.assert_not_called()


def test_log_delete_refuses_a_reappearing_approved_image(setup, monkeypatch):
    groups = plan_for(setup, "log-group")
    current = {**groups, **plan_for(setup, "image")}
    choose = Mock(side_effect=[(groups, ({}, {}, {})), (current, ({}, {}, {}))])
    monkeypatch.setattr(recovery, "settled_selection", choose)
    mutation = Mock()
    monkeypatch.setattr(setup.api, "call", mutation)
    with pytest.raises(recovery.Refused, match="^image_not_absent$"):
        recovery.recover(setup.manifest, setup.ownership, setup.api, {})
    mutation.assert_not_called()


@pytest.mark.parametrize("state", ["DELETING", "DELETED"])
def test_existing_image_deletion_is_observed_without_reissuing_mutation(
    setup, monkeypatch, state
):
    setup.image["state"] = state
    initial = plan_for(setup, "image")
    choices = [initial, initial]
    monkeypatch.setattr(
        recovery,
        "settled_selection",
        lambda *_, **__: (choices.pop(0) if choices else {}, ({}, {}, {})),
    )
    mutation, wait = Mock(), Mock()
    monkeypatch.setattr(setup.api, "call", mutation)
    monkeypatch.setattr(setup.api, "wait_image_absent", wait)
    report = {}
    recovery.recover(setup.manifest, setup.ownership, setup.api, report)
    mutation.assert_not_called()
    wait.assert_called_once_with(ARN)
    assert report == {
        "resources": [
            {
                "kind": "image",
                "sha256": "image-key",
                "status": "absent",
                "deletionRequested": False,
            },
            {
                "kind": "log-group",
                "sha256": "group-key",
                "status": "absent",
                "deletionRequested": False,
            },
        ]
    }
    assert setup.clock.sleeps == [2] * 5


def test_recovery_orders_deletions_reports_each_stage_and_restarts_visibility(
    setup, monkeypatch
):
    setup.clock.now = 7
    initial = {**plan_for(setup, "log-group"), **plan_for(setup, "image")}
    groups = plan_for(setup, "log-group")
    setup.manifest["resources"].append(
        {"kind": "image", "sha256": "already-absent", "proof": "shared-image"}
    )
    state = ({"foreign": {"name": "foreign"}}, {"other-vm": {}}, {GROUP: setup.group})
    choices = [initial, initial, groups, {}, groups, {}]
    report, events, observed = {}, [], []

    def statuses():
        return copy.deepcopy(report["resources"])

    def choose(*args, image_phase=False):
        assert args == (setup.manifest, setup.ownership, setup.api)
        assert image_phase == (len(choices) >= 5)
        if report:
            observed.append(statuses())
        return (choices.pop(0) if choices else {}), state

    def mutate(client, operation, **kwargs):
        events.append((operation, kwargs, statuses()))
        assert client is (
            setup.plane if operation == "delete_microvm_image" else setup.logs
        )
        return {}

    def wait(arn):
        assert arn == ARN
        events.append(("wait", {}, statuses()))

    def dependencies(group, images, deleting):
        assert group == GROUP
        assert images == state[0]
        assert deleting == set()
        events.append(("dependencies", {}, statuses()))

    monkeypatch.setattr(recovery, "settled_selection", choose)
    monkeypatch.setattr(setup.api, "call", mutate)
    monkeypatch.setattr(setup.api, "wait_image_absent", wait)
    monkeypatch.setattr(setup.api, "configured_dependencies", dependencies)
    recovery.recover(setup.manifest, setup.ownership, setup.api, report)
    assert [event[0] for event in events] == [
        "delete_microvm_image",
        "wait",
        "dependencies",
        "delete_log_group",
    ]
    assert events[0][1] == {"imageIdentifier": ARN}
    assert events[-1][1] == {"logGroupName": GROUP}
    expected = [
        {
            "kind": "image",
            "sha256": "image-key",
            "status": "present",
            "deletionRequested": False,
        },
        {
            "kind": "log-group",
            "sha256": "group-key",
            "status": "present",
            "deletionRequested": False,
        },
        {
            "kind": "image",
            "sha256": "already-absent",
            "status": "absent",
            "deletionRequested": False,
        },
    ]
    assert events[0][2] == expected
    expected[0]["status"] = "deletion_requested"
    expected[0]["deletionRequested"] = True
    assert events[1][2] == expected
    expected[0]["status"] = "absent"
    assert events[2][2] == events[3][2] == expected
    expected[1]["status"] = "deletion_requested"
    expected[1]["deletionRequested"] = True
    assert observed[-1] == expected
    for row in expected:
        row["status"] = "absent"
    assert report == {"resources": expected}
    assert setup.clock.now == 21
    assert setup.clock.sleeps == [2] * 7


@pytest.mark.parametrize(
    "logging", [None, {"disabled": True}, {"unexpected": "metadata"}]
)
def test_absent_approved_groups_still_observe_excluded_image_without_deleting_it(
    setup, logging
):
    foreign_arn = "arn:aws:lambda:us-east-1:000000000000:microvm-image:excluded"
    foreign = {**setup.image, "name": "excluded", "imageArn": foreign_arn}
    setup.plane.list_microvm_images.return_value = {"items": [foreign]}
    setup.logs.describe_log_groups.return_value = {"logGroups": []}
    setup.plane.list_microvm_image_versions.return_value = {
        "items": [{"imageVersion": "v"}]
    }
    setup.plane.get_microvm_image_version = Mock(
        return_value={
            "imageArn": foreign_arn,
            "imageVersion": "v",
            "state": "SUCCESSFUL",
            "createdAt": CREATED,
            "logging": logging,
        }
    )
    setup.plane.delete_microvm_image = Mock()
    setup.logs.delete_log_group = Mock()
    report = {}
    recovery.recover(setup.manifest, setup.ownership, setup.api, report)
    assert report == {
        "resources": [
            {
                "kind": "image",
                "sha256": "image-key",
                "status": "absent",
                "deletionRequested": False,
            },
            {
                "kind": "log-group",
                "sha256": "group-key",
                "status": "absent",
                "deletionRequested": False,
            },
        ]
    }
    assert len(setup.api.version_evidence) == 1
    row = setup.api.version_evidence[0]
    assert row["imageSha256"] == recovery.identity("image", REGION, "excluded")
    assert row["versionSha256"] == recovery.digest(b"v")
    assert row["createdAt"] == CREATED.timestamp()
    assert all(
        call.kwargs == {"imageIdentifier": foreign_arn}
        for call in setup.plane.list_microvm_image_versions.call_args_list
    )
    assert all(
        call.kwargs == {"imageIdentifier": foreign_arn, "imageVersion": "v"}
        for call in setup.plane.get_microvm_image_version.call_args_list
    )
    setup.plane.get_microvm_image.assert_not_called()
    setup.plane.delete_microvm_image.assert_not_called()
    setup.logs.delete_log_group.assert_not_called()


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("imageArn", "wrong", "version_identity"),
        ("imageVersion", "wrong", "version_identity"),
        ("state", "UNKNOWN", "version_state"),
    ],
)
def test_read_only_version_observation_still_refuses_invalid_identity_or_state(
    setup, field, value, reason
):
    setup.plane.list_microvm_image_versions.return_value = {
        "items": [{"imageVersion": "v"}]
    }
    setup.plane.get_microvm_image_version = Mock(
        return_value={
            "imageArn": ARN,
            "imageVersion": "v",
            "state": "SUCCESSFUL",
            "createdAt": CREATED,
            "logging": None,
            field: value,
        }
    )
    with pytest.raises(recovery.Refused, match=f"^{reason}$"):
        setup.api.configured_dependencies(None, {ARN: setup.image}, set())
    assert setup.api.version_evidence == []


def test_read_only_dependency_scan_observes_every_version(setup):
    setup.api.region = REGION
    setup.plane.list_microvm_image_versions.return_value = {
        "items": [{"imageVersion": "first"}, {"imageVersion": "last"}]
    }

    def version(*, imageIdentifier, imageVersion):
        assert imageIdentifier == ARN
        return {
            "imageArn": ARN,
            "imageVersion": imageVersion,
            "state": "SUCCESSFUL",
            "createdAt": CREATED,
            "logging": None,
        }

    setup.plane.get_microvm_image_version = Mock(side_effect=version)
    setup.api.configured_dependencies(None, {ARN: setup.image}, set())
    assert [row["versionSha256"] for row in setup.api.version_evidence] == [
        recovery.digest(b"first"),
        recovery.digest(b"last"),
    ]
    assert setup.plane.get_microvm_image_version.call_args_list == [
        ((), {"imageIdentifier": ARN, "imageVersion": "first"}),
        ((), {"imageIdentifier": ARN, "imageVersion": "last"}),
    ]


@pytest.mark.parametrize("image_present", [False, True])
def test_no_selected_log_group_requests_read_only_versions_with_exact_context(
    setup, monkeypatch, image_present
):
    setup.logs.describe_log_groups.return_value = {"logGroups": []}
    if not image_present:
        setup.plane.list_microvm_images.return_value = {"items": []}
    observe = Mock()
    monkeypatch.setattr(setup.api, "configured_dependencies", observe)
    plan, state = recovery.selected(setup.manifest, setup.ownership, setup.api)
    assert set(plan) == ({"image-key"} if image_present else set())
    assert state[2] == {}
    observe.assert_called_once_with(
        None,
        {ARN: setup.image} if image_present else {},
        {ARN} if image_present else set(),
    )


@pytest.mark.parametrize(
    "kind,failure_stage,expected_status,requested",
    [
        ("image", "delete", "present", False),
        ("image", "wait", "deletion_requested", True),
        ("log-group", "delete", "present", False),
    ],
)
def test_deletion_requested_records_an_accepted_call_even_if_later_work_fails(
    setup, monkeypatch, kind, failure_stage, expected_status, requested
):
    initial = plan_for(setup, kind)
    monkeypatch.setattr(
        recovery, "settled_selection", lambda *_, **__: (initial, ({}, {}, {}))
    )
    monkeypatch.setattr(setup.api, "configured_dependencies", Mock())
    delete = Mock(
        side_effect=recovery.Refused("api_failed")
        if failure_stage == "delete"
        else None
    )
    monkeypatch.setattr(setup.api, "call", delete)
    monkeypatch.setattr(
        setup.api, "wait_image_absent", Mock(side_effect=recovery.Refused("deadline"))
    )
    report = {}
    with pytest.raises(recovery.Refused, match="^(api_failed|deadline)$"):
        recovery.recover(setup.manifest, setup.ownership, setup.api, report)
    key = "image-key" if kind == "image" else "group-key"
    target = next(row for row in report["resources"] if row["sha256"] == key)
    assert target == {
        "kind": kind,
        "sha256": key,
        "status": expected_status,
        "deletionRequested": requested,
    }
    assert all(
        row["deletionRequested"] is False
        for row in report["resources"]
        if row["sha256"] != key
    )
    assert delete.call_count == 1


def test_explicit_additional_image_does_not_authorize_its_log_group(setup, monkeypatch):
    additional_arn = ARN.replace(":reviewed", ":additional")
    related_group = GROUP.replace("reviewed", "additional")
    image = {**setup.image, "name": "additional", "imageArn": additional_arn}
    group = {**setup.group, "logGroupName": related_group}
    images = {additional_arn: image}
    setup.manifest["resources"] = [
        {
            "kind": "image",
            "sha256": "additional-key",
            "proof": "explicit-incident-image",
        }
    ]
    setup.ownership = {
        "additional-key": ("explicit-incident-image", "additional", None)
    }
    setup.plane.list_microvm_images = Mock(
        side_effect=lambda: {"items": list(images.values())}
    )
    setup.logs.describe_log_groups.return_value = {"logGroups": [group]}

    class Missing(Exception):
        response = {"Error": {"Code": "ResourceNotFoundException"}}

    def read(*, imageIdentifier):
        if imageIdentifier not in images:
            raise Missing
        return images[imageIdentifier]

    setup.plane.get_microvm_image = Mock(side_effect=read)
    setup.plane.delete_microvm_image = Mock(
        side_effect=lambda *, imageIdentifier: images.pop(imageIdentifier)
    )
    setup.plane.list_microvm_image_versions.return_value = {
        "items": [{"imageVersion": "1"}]
    }
    setup.plane.get_microvm_image_version = Mock(
        return_value={
            "imageArn": additional_arn,
            "imageVersion": "1",
            "state": "SUCCESSFUL",
            "createdAt": CREATED,
            "logging": {"cloudWatch": {"logGroup": related_group}},
        }
    )
    setup.logs.delete_log_group = Mock()
    dependencies = Mock(wraps=setup.api.configured_dependencies)
    monkeypatch.setattr(setup.api, "configured_dependencies", dependencies)
    report = {}
    recovery.recover(setup.manifest, setup.ownership, setup.api, report)
    assert report == {
        "resources": [
            {
                "kind": "image",
                "sha256": "additional-key",
                "status": "absent",
                "deletionRequested": True,
            }
        ]
    }
    setup.plane.delete_microvm_image.assert_called_once_with(
        imageIdentifier=additional_arn
    )
    setup.logs.delete_log_group.assert_not_called()
    assert setup.logs.describe_log_groups.return_value == {"logGroups": [group]}
    assert dependencies.call_args_list
    assert all(call.args[0] is None for call in dependencies.call_args_list)
    assert setup.api.version_evidence == []
    setup.plane.get_microvm_image_version.assert_not_called()


def test_image_phase_preserves_full_inventories_without_observing_versions(setup):
    foreign = {**setup.image, "imageArn": "foreign", "name": "foreign"}
    vm = {"microvmId": "foreign-vm", "imageArn": "foreign", "state": "RUNNING"}
    group = {**setup.group, "logGroupName": GROUP + "-foreign"}
    setup.plane.list_microvm_images.return_value = {"items": [setup.image, foreign]}
    setup.plane.list_microvms.return_value = {"items": [vm]}
    setup.logs.describe_log_groups.return_value = {"logGroups": [setup.group, group]}
    setup.api.configured_dependencies = Mock(side_effect=AssertionError("log phase"))
    plan, state = recovery.selected(
        setup.manifest, setup.ownership, setup.api, image_phase=True
    )
    assert set(plan) == {"image-key", "group-key"}
    assert state == (
        {ARN: setup.image, "foreign": foreign},
        {"foreign-vm": vm},
        {GROUP: setup.group, GROUP + "-foreign": group},
    )
    setup.api.configured_dependencies.assert_not_called()
    setup.plane.get_microvm_image.assert_called_once_with(imageIdentifier=ARN)


@pytest.mark.parametrize("logging", [None, {}, {"cloudWatch": None}])
def test_unknown_logging_stops_logs_after_confirmed_image_absence(setup, logging):
    foreign = {**setup.image, "imageArn": "foreign", "name": "foreign"}
    images = {ARN: setup.image, "foreign": foreign}
    setup.plane.list_microvm_images.side_effect = lambda: {
        "items": list(images.values())
    }

    class Missing(Exception):
        response = {"Error": {"Code": "ResourceNotFoundException"}}

    def read(*, imageIdentifier):
        if imageIdentifier not in images:
            raise Missing
        return images[imageIdentifier]

    def delete(*, imageIdentifier):
        assert imageIdentifier == ARN
        images.pop(imageIdentifier)
        return {}

    setup.plane.get_microvm_image.side_effect = read
    setup.plane.delete_microvm_image = Mock(side_effect=delete)
    setup.plane.list_microvm_image_versions.return_value = {
        "items": [{"imageVersion": "1"}]
    }
    setup.plane.get_microvm_image_version = Mock(
        return_value={
            "imageArn": "foreign",
            "imageVersion": "1",
            "state": "SUCCESSFUL",
            "createdAt": CREATED,
            "logging": logging,
        }
    )
    setup.logs.delete_log_group = Mock()
    report = {}
    reason = (
        "version_logging_missing" if logging is None else "version_logging_ambiguous"
    )
    with pytest.raises(recovery.Refused, match=f"^{reason}$"):
        recovery.recover(setup.manifest, setup.ownership, setup.api, report)
    assert images == {"foreign": foreign}
    setup.plane.delete_microvm_image.assert_called_once_with(imageIdentifier=ARN)
    setup.logs.delete_log_group.assert_not_called()
    assert setup.plane.get_microvm_image.call_args_list[-1].kwargs == {
        "imageIdentifier": ARN
    }
    assert report == {
        "resources": [
            {
                "kind": "image",
                "sha256": "image-key",
                "status": "absent",
                "deletionRequested": True,
            },
            {
                "kind": "log-group",
                "sha256": "group-key",
                "status": "present",
                "deletionRequested": False,
            },
        ]
    }
    assert setup.api.version_evidence[0]["imageSha256"] == recovery.identity(
        "image", REGION, "foreign"
    )
