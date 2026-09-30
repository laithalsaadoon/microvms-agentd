# SPDX-License-Identifier: Apache-2.0
"""Handing a VM off with `detach()`, refused before any AWS call when there is nothing to hand off.

Credentials come from the environment, which the default chain reads without a network
call. The hand-off itself (fields, silence on drop, refused transitions) is covered in the
core; the detach -> adopt round trip against a real VM is recorded in the PR.
"""

from __future__ import annotations

import pytest

import microvms


@pytest.fixture(autouse=True)
def offline_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.delenv("AWS_PROFILE", raising=False)


def region() -> microvms.Region:
    return microvms.Region.us_east_1()


def test_a_sandbox_with_nothing_launched_has_nothing_to_detach() -> None:
    sandbox = microvms.Sandbox(region())
    with pytest.raises(microvms.PreconditionError, match="detach"):
        sandbox.detach()
    assert sandbox.is_detached is False


def test_an_agent_vm_with_nothing_launched_has_nothing_to_detach() -> None:
    with pytest.raises(microvms.PreconditionError, match="detach"):
        microvms.AgentVm(region()).detach()


def test_the_hand_off_record_is_built_by_the_binding_only() -> None:
    with pytest.raises(TypeError):
        microvms.Detached()  # type: ignore[call-arg]
    for field in ("microvm_id", "endpoint", "region", "port", "agent_token"):
        assert hasattr(microvms.Detached, field)
    assert hasattr(microvms.Detached, "to_dict")
