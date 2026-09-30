# SPDX-License-Identifier: Apache-2.0
"""A build refuses a daemon that isn't an aarch64 ELF before any AWS call (#257, BIND-20).

Credentials come from the environment, which the default chain reads without a network call,
so a request core let through would reach AWS with them and fail as something other than a
precondition. `build_artifact` refuses where the bytes enter an artifact, which is the upload a
caller makes before `build_image`; `build_image` and `ensure_image` refuse in core's preflight.
"""

from __future__ import annotations

import pytest

import microvms

ROLE = "arn:aws:iam::123456789012:role/build"
URI = "s3://agentd-conformance-bucket/task.zip"


def elf(machine: int) -> bytes:
    """A little-endian ELF header for `machine` (0xB7 is aarch64, 0x3E is x86_64)."""
    header = bytearray(20)
    header[:4] = b"\x7fELF"
    header[5] = 1
    header[18:20] = machine.to_bytes(2, "little")
    return bytes(header)


WRONG = [
    pytest.param(elf(0x3E), "ELF machine 0x3e, not aarch64", id="x86_64"),
    pytest.param(b"#!/bin/sh\nexec agentd\n", "not an ELF binary at all", id="script"),
]
EMPTY = pytest.param(b"", "not an ELF binary at all", id="empty")


@pytest.fixture(autouse=True)
def offline_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.delenv("AWS_PROFILE", raising=False)


def sandbox() -> microvms.Sandbox:
    return microvms.Sandbox(microvms.Region.us_east_1())


@pytest.mark.parametrize(("binary", "why"), [*WRONG, EMPTY])
def test_bind20_build_artifact_refuses_a_daemon_that_is_not_aarch64(
    binary: bytes, why: str
) -> None:
    with pytest.raises(microvms.PreconditionError, match=why):
        sandbox().build_artifact(
            name="task", binary=binary, code_artifact_uri=URI, build_role_arn=ROLE
        )


@pytest.mark.parametrize(("binary", "why"), WRONG)
def test_bind20_build_image_refuses_a_daemon_that_is_not_aarch64(
    binary: bytes, why: str
) -> None:
    with pytest.raises(microvms.PreconditionError, match=why):
        sandbox().build_image(
            name="task", binary=binary, code_artifact_uri=URI, build_role_arn=ROLE
        )


@pytest.mark.parametrize(("binary", "why"), [*WRONG, EMPTY])
def test_bind20_ensure_image_refuses_a_daemon_that_is_not_aarch64(
    binary: bytes, why: str
) -> None:
    with pytest.raises(microvms.PreconditionError, match=why):
        sandbox().ensure_image(
            name_prefix="task",
            binary=binary,
            dockerfile=microvms.wrap_dockerfile("FROM python:3.12-slim\n"),
            s3_bucket="agentd-conformance-bucket",
            build_role_arn=ROLE,
        )


def test_an_aarch64_daemon_builds_an_artifact() -> None:
    """The positive control: the same call with an aarch64 header zips."""
    artifact = sandbox().build_artifact(
        name="task", binary=elf(0xB7), code_artifact_uri=URI, build_role_arn=ROLE
    )
    assert artifact[:2] == b"PK"
