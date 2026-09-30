# SPDX-License-Identifier: Apache-2.0
"""`Sandbox.preflight` and `Sandbox.managed_base_versions` (#264): core's local refusals.

`preflight` is `build_image`'s local guards alone, with zero calls, so both its refusals and
its pass are offline. `managed_base_versions` refuses a bare base name before its call.
Credentials come from the environment, which the default chain reads without a network call.
"""

from __future__ import annotations

import pytest

import microvms

ROLE = "arn:aws:iam::123456789012:role/build"
URI = "s3://agentd-conformance-bucket/task.zip"
# An aarch64 ELF header: the daemon a build takes.
DAEMON = b"\x7fELF\x02\x01" + bytes(12) + b"\xb7\x00"


@pytest.fixture(autouse=True)
def offline_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.delenv("AWS_PROFILE", raising=False)


def sandbox() -> microvms.Sandbox:
    return microvms.Sandbox(microvms.Region.us_east_1())


def preflight(**overrides: object) -> None:
    arguments: dict[str, object] = {
        "name": "task",
        "binary": DAEMON,
        "code_artifact_uri": URI,
        "build_role_arn": ROLE,
    }
    arguments.update(overrides)
    sandbox().preflight(**arguments)  # type: ignore[arg-type]


def test_a_request_build_image_would_send_passes() -> None:
    assert preflight() is None
    assert preflight(tags={"team": "x"}, log_group="/g", log_stream="s") is None


@pytest.mark.parametrize(
    ("overrides", "cause"),
    [
        pytest.param({"name": "my.image"}, "name", id="name"),
        pytest.param({"build_role_arn": "not-an-arn"}, "buildRoleArn", id="role"),
        pytest.param({"code_artifact_uri": ""}, "codeArtifact.uri", id="uri"),
        pytest.param({"log_stream": "s"}, "logGroup", id="stream"),
        pytest.param(
            {
                "dockerfile": "FROM public.ecr.aws/amazonlinux/amazonlinux:2023-minimal\n"
            },
            "CMD",
            id="dockerfile",
        ),
        pytest.param({"inherit_workdir": True}, "WORKDIR", id="workdir"),
    ],
)
def test_preflight_raises_what_build_image_would(
    overrides: dict[str, object], cause: str
) -> None:
    """The refusal core's `ControlPlane::preflight` makes, before any call."""
    with pytest.raises(microvms.InvalidArgError, match=cause):
        preflight(**overrides)


def test_managed_base_versions_needs_the_bases_full_arn() -> None:
    with pytest.raises(microvms.PreconditionError, match="full ARN"):
        sandbox().managed_base_versions("al2023-1")


def test_a_managed_base_version_comes_only_from_the_service() -> None:
    with pytest.raises(TypeError):
        microvms.ManagedBaseVersion()  # type: ignore[call-arg]
