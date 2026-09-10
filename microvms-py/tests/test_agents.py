# SPDX-License-Identifier: Apache-2.0
"""The L3 surface, offline: every refusal is the core's and the token has one door.

Nothing here talks to AWS. `AgentVm(...)` resolves a credential chain and `mint_bedrock_token`
signs with one, so both stay out of a unit run; what is asserted is the shape a caller sees
before any call: spec defaults and refusals, the headless command, the constants, and that a
`BearerToken` cannot be built or read except through `expose()`.
"""

from __future__ import annotations

import pytest

import microvms


def test_a_spec_resolves_the_profile_default_model() -> None:
    claude = microvms.AgentSpec("claude-code")
    assert claude.agent == "claude-code"
    assert claude.model == "global.anthropic.claude-opus-5"
    assert claude.cli_version is None
    codex = microvms.AgentSpec.codex()
    assert codex.model == "global.openai.gpt-5.6-sol"


def test_overrides_are_keyword_only_and_win() -> None:
    spec = microvms.AgentSpec.codex(
        model="us.openai.gpt-5.6-sol", cli_version="0.154.0"
    )
    assert spec.model == "us.openai.gpt-5.6-sol"
    assert spec.cli_version == "0.154.0"
    with pytest.raises(TypeError):
        microvms.AgentSpec("codex", "a-model")  # type: ignore[misc]


def test_an_unknown_agent_is_the_cores_refusal_with_the_list() -> None:
    with pytest.raises(microvms.InvalidArgError) as raised:
        microvms.AgentSpec("cursor")
    assert raised.value.code == "ERR_INVALID_ARG"
    assert "claude-code" in str(raised.value) and "codex" in str(raised.value)


def test_the_headless_command_is_the_demoted_line_with_the_task_hole() -> None:
    claude = microvms.AgentSpec.claude_code().headless_command
    assert claude.startswith(". /workspace/.agent-env && claude -p '<TASK>'")
    assert "--allowedTools" in claude
    codex = microvms.AgentSpec.codex().headless_command
    assert "codex exec --skip-git-repo-check -s workspace-write '<TASK>'" in codex


def test_specs_compare_by_value_and_repr_names_the_resolved_model() -> None:
    assert microvms.AgentSpec("codex") == microvms.AgentSpec.codex()
    assert microvms.AgentSpec("codex") != microvms.AgentSpec.codex(model="x")
    assert "global.openai.gpt-5.6-sol" in repr(microvms.AgentSpec("codex"))


def test_a_bearer_token_has_no_constructor_so_it_only_comes_from_minting() -> None:
    with pytest.raises(TypeError):
        microvms.BearerToken()  # type: ignore[call-arg]
    assert not hasattr(microvms.BearerToken, "__str__") or (
        microvms.BearerToken.__str__ is object.__str__
    )


def test_the_constants_name_the_guest_contract() -> None:
    constants = microvms.agent_constants()
    assert constants["uid"] == 1000 and constants["gid"] == 1000
    assert constants["workdir"] == "/workspace"
    assert constants["env_file"] == "/workspace/.agent-env"
    assert constants["marker_file"] == "/workspace/.agent-vm.json"
    assert constants["default_prompt_timeout_sec"] == 900.0
    assert constants["max_token_lifetime_sec"] == 43200.0
    assert set(constants["profiles"]) == {"claude-code", "codex"}
    assert constants["profiles"]["codex"]["verified"].startswith("2026-")


def test_an_agent_vm_takes_a_region_object_not_a_string() -> None:
    with pytest.raises(TypeError):
        microvms.AgentVm("us-east-1")  # type: ignore[arg-type]


def test_a_prompt_over_a_direct_session_refuses_a_blank_task_before_any_call() -> None:
    session = microvms.Session.direct("http://127.0.0.1:9", "token")
    with pytest.raises(microvms.InvalidArgError):
        microvms.prompt_agent(session, microvms.AgentSpec.codex(), "   ")


def test_every_agent_function_is_exported() -> None:
    for name in (
        "AgentSpec",
        "AgentVm",
        "BearerToken",
        "agent_constants",
        "install_agent_access",
        "installed_agents",
        "mint_bedrock_token",
        "prompt_agent",
    ):
        assert hasattr(microvms, name), name
