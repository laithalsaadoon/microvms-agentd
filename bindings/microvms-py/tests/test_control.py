# SPDX-License-Identifier: Apache-2.0
"""Lifecycle by ID, launch options, and per-VM logging, refused before any AWS call.

Credentials come from the environment, which the default chain reads without a network
call, so every object here is the real one. Each case is a refusal the core makes before
the wire; the live half is `conformance/run_rs.py`.
"""

from __future__ import annotations

import inspect

import pytest

import microvms


@pytest.fixture(autouse=True)
def offline_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.delenv("AWS_PROFILE", raising=False)


def region() -> microvms.Region:
    return microvms.Region.us_east_1()


def test_the_control_plane_checks_identifiers_before_the_wire() -> None:
    plane = microvms.ControlPlane(region())
    assert "us-east-1" in repr(plane)
    for call in (plane.get, plane.suspend, plane.resume, plane.terminate):
        with pytest.raises(microvms.InvalidArgError):
            call("")
    with pytest.raises(microvms.InvalidArgError):
        plane.list(image_identifier="")
    with pytest.raises(microvms.InvalidArgError):
        plane.wait_for_state("", ["RUNNING"])
    with pytest.raises(microvms.InvalidArgError):
        plane.wait_for_state("mvm-1", ["RUNNING"], timeout=-1)


def test_service_shapes_come_only_from_the_service() -> None:
    for cls in (microvms.Microvm, microvms.MicrovmSummary, microvms.IdlePolicy):
        with pytest.raises(TypeError):
            cls()  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"log_stream": "s"}, "pass log_group"),
        ({"log_group": "/g", "disable_logging": True}, "cannot be combined"),
        ({"log_group": "bad group!"}, "log"),
    ],
)
def test_per_vm_logging_is_refused_locally(kwargs: dict, message: str) -> None:
    with pytest.raises(microvms.InvalidArgError, match=message):
        microvms.Sandbox(region()).run(image_identifier="arn:image", **kwargs)


def test_wait_until_running_needs_a_launch() -> None:
    with pytest.raises(microvms.PreconditionError):
        microvms.Sandbox(region()).wait_until_running()


def test_an_agent_vm_takes_vpc_connectors_and_checks_them_locally() -> None:
    vm = microvms.AgentVm(region(), [microvms.AgentSpec.claude_code()])
    with pytest.raises(microvms.InvalidArgError):
        vm.launch(
            image_identifier="arn:image",
            image_version="1.0",
            egress_network_connectors=["not-a-connector-arn"],
        )
    with pytest.raises(microvms.InvalidArgError, match="pass log_group"):
        vm.launch(image_identifier="arn:image", log_stream="s")


def test_terminate_waits_for_a_bool_or_a_number_of_seconds() -> None:
    """`wait_for_terminated` takes `True`, for the core's lifecycle default, or seconds (#267).

    A sandbox that launched nothing has no VM to wait for, so each report comes back at once:
    what's asserted is that both shapes reach the core's teardown options, and that a number no
    duration can be is refused before the teardown runs.

    **Falsification**: map a number to the core's default wait in `Sandbox.terminate` (drop the
    seconds on their way to `TeardownOpts`) and the negative wait is no longer refused.
    """
    for wait in (False, True, 0.5, 120):
        report = microvms.Sandbox(region()).terminate(wait_for_terminated=wait)
        assert report.terminate_accepted is False and not report.leaked
    with pytest.raises(microvms.InvalidArgError):
        microvms.Sandbox(region()).terminate(wait_for_terminated=-1.0)
    with pytest.raises(TypeError):
        microvms.Sandbox(region()).terminate(wait_for_terminated="soon")  # type: ignore[arg-type]


def test_the_lifecycle_wait_defaults_the_signature_states_are_cores() -> None:
    """`wait_for_state` writes its defaults out, and they're core's (#266).

    Written out so the stub shows them (a default named for a core constant prints as `...`),
    which is what makes them a second copy; `session_constants()` publishes core's figures, and
    this holds the copy to them. The other waits name core's constant, so they can't drift.
    """
    constants = microvms.session_constants()
    parameters = inspect.signature(microvms.ControlPlane.wait_for_state).parameters
    assert (
        parameters["timeout"].default == constants["defaultLifecycleTimeoutSeconds"]
    ), parameters["timeout"]
    assert (
        parameters["poll_interval"].default == constants["lifecyclePollIntervalSeconds"]
    ), parameters["poll_interval"]
