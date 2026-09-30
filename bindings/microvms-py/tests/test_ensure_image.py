# SPDX-License-Identifier: Apache-2.0
"""`Sandbox.ensure_image` (IMAGE-12): thin, with core's local refusals before any AWS call.

Credentials come from the environment, which the default chain reads without a network call.
Every case here is refused by core's local half, so nothing reaches STS, S3, or the control
plane. The decisions, the race, and the upload are core's (IMAGE-6 through IMAGE-11) and run
live in `conformance/run_rs.py`'s ensure-image section.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import microvms

BUCKET = "agentd-conformance-bucket"
ROLE = "arn:aws:iam::123456789012:role/build"


@pytest.fixture(autouse=True)
def offline_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.delenv("AWS_PROFILE", raising=False)


def sandbox() -> microvms.Sandbox:
    return microvms.Sandbox(microvms.Region.us_east_1())


def ensure(**overrides: object) -> microvms.EnsuredImage:
    arguments: dict[str, object] = {
        "name_prefix": "task",
        # An aarch64 ELF header, so each case below is refused for its own cause.
        "binary": b"\x7fELF\x02\x01" + bytes(12) + b"\xb7\x00",
        "dockerfile": microvms.wrap_dockerfile("FROM python:3.12-slim\n"),
        "s3_bucket": BUCKET,
        "build_role_arn": ROLE,
    }
    arguments.update(overrides)
    return sandbox().ensure_image(**arguments)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("overrides", "cause"),
    [
        ({"s3_bucket": "Not_A_Bucket"}, "bucket"),
        ({"name_prefix": "///"}, "prefix"),
        ({"build_role_arn": "not-an-arn"}, "buildRoleArn"),
        ({"s3_key_prefix": "a\nb"}, "key prefix"),
        (
            {"dockerfile": 'FROM x\nENTRYPOINT ["/bin/sh"]\nCMD ["/agentd"]\n'},
            "ENTRYPOINT",
        ),
        ({"wait_timeout": -1.0}, "not a duration"),
    ],
)
def test_core_refuses_locally_before_any_call(
    overrides: dict[str, object], cause: str
) -> None:
    """IMAGE-12: core's local refusals raise `InvalidArgError` before any AWS call."""
    with pytest.raises(microvms.InvalidArgError, match=cause):
        ensure(**overrides)


def test_a_context_directory_is_read_by_core(tmp_path: Path) -> None:
    """IMAGE-12: the context directory reaches core, which reads it and refuses it."""
    with pytest.raises(microvms.InvalidArgError, match="not a directory"):
        ensure(context_dir=tmp_path / "missing")
    (tmp_path / "agentd").write_bytes(b"not the daemon")
    with pytest.raises(microvms.InvalidArgError, match="agentd"):
        ensure(context_dir=tmp_path)


def test_the_result_class_is_built_by_the_binding_only() -> None:
    """IMAGE-12: the result has core's fields, the reused flag and warnings among them."""
    with pytest.raises(TypeError):
        microvms.EnsuredImage()  # type: ignore[call-arg]
    for field in ("image", "reused", "artifact_uri", "uploaded", "warnings"):
        assert hasattr(microvms.EnsuredImage, field)


@pytest.mark.parametrize(
    ("overrides", "cause"),
    [
        ({"s3_bucket": "Not_A_Bucket"}, "bucket"),
        ({"build_role_arn": "not-an-arn"}, "buildRoleArn"),
        ({"s3_key_prefix": "a\nb"}, "key prefix"),
    ],
)
def test_an_agent_vm_ensures_its_image_through_cores_local_refusals(
    overrides: dict[str, object], cause: str
) -> None:
    """#258: `AgentVm.ensure_image` is `Sandbox.ensure_image` over the agents' request, so
    core refuses the same inputs before any call."""
    vm = microvms.AgentVm(microvms.Region.us_east_1(), [microvms.AgentSpec.codex()])
    arguments: dict[str, object] = {
        "binary": b"\x7fELF\x02\x01" + bytes(12) + b"\xb7\x00",
        "build_role_arn": ROLE,
        "s3_bucket": BUCKET,
    }
    arguments.update(overrides)
    with pytest.raises(microvms.InvalidArgError, match=cause):
        vm.ensure_image(**arguments)  # type: ignore[arg-type]
