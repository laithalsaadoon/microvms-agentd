# SPDX-License-Identifier: Apache-2.0
"""Adopting a VM another process launched, refused before any AWS call when the record is bad.

Credentials come from the environment, which the default chain reads without a network
call. The lifecycle mapping and every guard are covered in the core; the live half is
`conformance/run_rs.py`'s `drive_adopt_by_id`.
"""

from __future__ import annotations

import pytest

import microvms

CANARY = "adopt-canary-token-5e2d"
ENDPOINT = "https://mvm-1.example.invalid"


@pytest.fixture(autouse=True)
def offline_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.delenv("AWS_PROFILE", raising=False)


def region() -> microvms.Region:
    return microvms.Region.us_east_1()


def test_adopt_needs_the_launch_token_before_any_call() -> None:
    with pytest.raises(microvms.InvalidArgError, match="agent token"):
        microvms.Sandbox.adopt(region(), "mvm-1", ENDPOINT, "")
    with pytest.raises(microvms.InvalidArgError, match="agent token"):
        microvms.AgentVm.adopt(region(), "mvm-1", ENDPOINT, "")


def test_a_bad_identifier_is_refused_without_printing_the_token() -> None:
    for adopt in (microvms.Sandbox.adopt, microvms.AgentVm.adopt):
        with pytest.raises(microvms.InvalidArgError) as caught:
            adopt(region(), "", ENDPOINT, CANARY)
        assert CANARY not in str(caught.value)


def test_an_agent_vm_checks_its_specs_before_any_call() -> None:
    with pytest.raises(microvms.InvalidArgError):
        microvms.AgentVm.adopt(region(), "mvm-1", ENDPOINT, CANARY, [])
    spec = microvms.AgentSpec.claude_code()
    with pytest.raises(microvms.InvalidArgError):
        microvms.AgentVm.adopt(region(), "mvm-1", ENDPOINT, CANARY, [spec, spec])


def test_a_sandbox_this_process_made_is_not_adopted() -> None:
    assert microvms.Sandbox(region()).adopted is False
