# SPDX-License-Identifier: Apache-2.0
"""Observation retries survive transient throttling without broadening mutations."""

import importlib.util
from pathlib import Path
from unittest.mock import Mock, call

import pytest

SPEC = importlib.util.spec_from_file_location(
    "tools.recover-live", Path(__file__).with_name("recover-live.py")
)
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)
FIXTURES = importlib.util.spec_from_file_location(
    "retry_fixtures", Path(__file__).with_name("test_recover_live.py")
)
fixtures = importlib.util.module_from_spec(FIXTURES)
FIXTURES.loader.exec_module(fixtures)

READS = [
    "list_microvm_images",
    "list_microvms",
    "describe_log_groups",
    "list_microvm_image_versions",
    "get_microvm_image_version",
    "get_microvm_image",
    "get_role",
]
RETRY_CODES = [
    "ThrottlingException",
    "TooManyRequestsException",
    "ServiceUnavailableException",
    "RequestTimeout",
]


def error(code):
    value = RuntimeError("private-provider-diagnostic")
    value.response = {"Error": {"Code": code, "Message": "private-resource"}}
    return value


def api(*, budget=100, edge="high"):
    clock = fixtures.Clock()
    waits = []

    def sleep(seconds):
        waits.append(seconds)
        clock.sleep(seconds)

    jitter = Mock(side_effect=lambda low, high: low if edge == "low" else high)
    value = recovery.Api(
        None, None, clock=clock, sleep=sleep, deadline=budget, jitter=jitter
    )
    return value, clock, waits, jitter


def test_default_jitter_uses_the_random_uniform_source():
    value = recovery.Api(None, None)
    assert value.jitter is recovery.random.uniform


@pytest.mark.parametrize("operation", READS)
def test_fourth_observation_attempt_can_succeed_after_throttling(operation):
    value, clock, waits, jitter = api()
    client = Mock()
    method = getattr(client, operation)
    method.side_effect = [error("ThrottlingException")] * 3 + [{"observed": True}]
    assert value.call(client, operation, selector="exact") == {"observed": True}
    assert method.call_args_list == [call(selector="exact")] * 4
    assert jitter.call_args_list == [call(1, 2), call(2, 4), call(4, 8)]
    assert waits == [2, 4, 8]
    assert clock.now == 14
    assert value.failures == []


@pytest.mark.parametrize("operation", READS)
@pytest.mark.parametrize("code", RETRY_CODES)
@pytest.mark.parametrize(
    "edge,expected_waits", [("low", [1, 2, 4, 8, 8]), ("high", [2, 4, 8, 16, 16])]
)
def test_observation_retries_exhaust_at_six_attempts_with_bounded_equal_jitter(
    operation, code, edge, expected_waits
):
    value, clock, waits, jitter = api(edge=edge)
    client = Mock()
    method = getattr(client, operation)
    method.side_effect = error(code)
    with pytest.raises(recovery.Refused, match="^api_failed$"):
        value.call(client, operation)
    assert method.call_count == 6
    assert waits == expected_waits
    assert clock.now == sum(expected_waits)
    assert jitter.call_args_list == [
        call(1, 2),
        call(2, 4),
        call(4, 8),
        call(8, 16),
        call(8, 16),
    ]
    assert value.failures == [{"operation": operation, "code": code}]


@pytest.mark.parametrize(
    "operation", ["delete_microvm_image", "delete_log_group", "unrecognized_operation"]
)
@pytest.mark.parametrize("code", RETRY_CODES)
def test_mutating_and_unrecognized_operations_keep_the_existing_retry_policy(
    operation, code
):
    value, clock, waits, jitter = api()
    client = Mock()
    method = getattr(client, operation)
    method.side_effect = error(code)
    with pytest.raises(recovery.Refused, match="^api_failed$"):
        value.call(client, operation)
    assert method.call_count == 3
    assert waits == [2, 2]
    assert clock.now == 4
    jitter.assert_not_called()
    assert value.failures == [
        {
            "operation": operation
            if operation != "unrecognized_operation"
            else "unknown",
            "code": code,
        }
    ]


@pytest.mark.parametrize(
    "operation", READS + ["delete_microvm_image", "delete_log_group"]
)
@pytest.mark.parametrize(
    "code",
    ["AccessDenied", "AccessDeniedException", "ValidationException", "private-code"],
)
def test_terminal_errors_do_not_retry_or_leak_provider_data(operation, code):
    value, _, waits, jitter = api()
    client = Mock()
    getattr(client, operation).side_effect = error(code)
    with pytest.raises(recovery.Refused, match="^api_failed$"):
        value.call(client, operation)
    assert getattr(client, operation).call_count == 1
    assert waits == []
    jitter.assert_not_called()
    assert value.failures == [
        {"operation": operation, "code": "unknown" if code == "private-code" else code}
    ]


@pytest.mark.parametrize(
    "budget,calls,expected_waits",
    [(0, 0, []), (1, 1, []), (2, 1, []), (3, 2, [2]), (6, 2, [2]), (6.5, 3, [2, 4])],
)
def test_observation_wait_must_fit_strictly_within_the_remaining_deadline(
    budget, calls, expected_waits
):
    value, clock, waits, _ = api(budget=budget)
    client = Mock()
    client.list_microvms.side_effect = error("ThrottlingException")
    with pytest.raises(recovery.Refused, match="^deadline$"):
        value.call(client, "list_microvms")
    assert client.list_microvms.call_count == calls
    assert waits == expected_waits
    assert clock.now < budget if budget else clock.now == 0


@pytest.mark.parametrize("failure", [InterruptedError, KeyboardInterrupt])
@pytest.mark.parametrize("stage", ["call", "jitter", "sleep"])
def test_observation_retry_propagates_cancellation_without_another_call(failure, stage):
    value, _, _, jitter = api()
    client = Mock()
    client.list_microvms.side_effect = (
        failure if stage == "call" else error("ThrottlingException")
    )
    if stage == "jitter":
        jitter.side_effect = failure
    elif stage == "sleep":
        value.sleep = Mock(side_effect=failure)
    with pytest.raises(failure):
        value.call(client, "list_microvms")
    assert client.list_microvms.call_count == 1
    assert value.failures == []


def test_exhausted_initial_inventory_leaves_the_deletion_loop_unreached():
    clock = fixtures.Clock()
    plane = fixtures.Plane()
    logs = fixtures.Logs(plane.calls)
    plane.list_microvms = Mock(side_effect=error("ThrottlingException"))
    value = recovery.Api(
        plane, logs, clock=clock, sleep=clock.sleep, jitter=lambda low, high: high
    )
    report = {}
    with pytest.raises(recovery.Refused, match="^api_failed$"):
        recovery.recover(
            fixtures.manifest(),
            recovery.source_ownership(fixtures.source_log(), fixtures.REGION),
            value,
            report,
        )
    assert plane.list_microvms.call_count == 6
    assert report == {}
    assert not [event for event in plane.calls if event[0].startswith("delete")]
    assert value.failures == [
        {"operation": "list_microvms", "code": "ThrottlingException"}
    ]


def test_iam_access_denied_is_optional_fixed_and_nonretrying():
    value, _, waits, jitter = api()
    iam = Mock()
    iam.get_role.side_effect = error("AccessDenied")
    assert recovery.observe_duration(
        iam, "arn:aws:iam::000000000000:role/read-target", value
    ) == {"status": "unavailable"}
    iam.get_role.assert_called_once_with(RoleName="read-target")
    assert value.failures == [{"operation": "get_role", "code": "AccessDenied"}]
    assert waits == []
    jitter.assert_not_called()
    client = Mock()
    client.list_microvms.return_value = {"items": []}
    assert value.call(client, "list_microvms") == {"items": []}
