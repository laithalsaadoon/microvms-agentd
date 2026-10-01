# SPDX-License-Identifier: Apache-2.0
"""`base_image_version` and `project_dir` on the image calls (#264).

`build_image`, `preflight`, `build_artifact` and `ensure_image` take both, and each value reaches
core's request: an illegal pin, and a directory without exactly one manifest+lockfile pair, are
core's refusals before any call, and a pair the Dockerfile never installs from is refused by
core's install check, which only sees the files the directory gave. HTTPS goes to a proxy on a
loopback port nothing listens on, so a refusal that regresses fails on a connection error
instead of reaching AWS.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

import microvms

ROLE = "arn:aws:iam::123456789012:role/build"
URI = "s3://agentd-conformance-bucket/task.zip"
# An aarch64 ELF header: the daemon a build takes.
DAEMON = b"\x7fELF\x02\x01" + bytes(12) + b"\xb7\x00"
PYPROJECT = b'[project]\nname = "probe"\nversion = "0.1.0"\n'
UV_LOCK = b"version = 1\n"


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")


@pytest.fixture
def pair(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_bytes(PYPROJECT)
    (tmp_path / "uv.lock").write_bytes(UV_LOCK)
    (tmp_path / ".env").write_bytes(b"SECRET=1\n")
    return tmp_path


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


def test_a_pin_and_a_project_pair_pass_preflight(pair: Path) -> None:
    assert preflight(base_image_version="1", project_dir=pair) is None
    assert preflight(project_dir=str(pair)) is None


def test_a_bad_pin_is_refused_before_any_call() -> None:
    with pytest.raises(microvms.InvalidArgError, match="baseImageVersion"):
        preflight(base_image_version="1 0")


def test_a_directory_without_a_pair_is_refused_before_any_call(tmp_path: Path) -> None:
    with pytest.raises(microvms.PreconditionError, match=r"pyproject\.toml\+uv\.lock"):
        preflight(project_dir=tmp_path)
    (tmp_path / "pyproject.toml").write_bytes(PYPROJECT)
    with pytest.raises(microvms.PreconditionError, match="uv lock"):
        preflight(project_dir=tmp_path)


def test_a_pair_the_dockerfile_never_installs_from_is_refused(pair: Path) -> None:
    """Core's install check reads the pair the directory gave, so the files reached it."""
    dockerfile = microvms.wrap_dockerfile(
        f"FROM {microvms.BaseImage.al2023().docker_ref}\n"
    )
    with pytest.raises(microvms.InvalidArgError, match="never mentions uv.lock"):
        preflight(project_dir=pair, dockerfile=dockerfile)


def test_the_artifact_carries_the_pair_and_nothing_else_from_the_directory(
    pair: Path,
) -> None:
    artifact = sandbox().build_artifact(
        name="task",
        binary=DAEMON,
        code_artifact_uri=URI,
        build_role_arn=ROLE,
        project_dir=pair,
    )
    with zipfile.ZipFile(io.BytesIO(artifact)) as archive:
        names = archive.namelist()
        assert archive.read("pyproject.toml") == PYPROJECT
        assert archive.read("uv.lock") == UV_LOCK
    assert ".env" not in names, names


@pytest.mark.parametrize(
    ("overrides", "error", "cause"),
    [
        pytest.param(
            {"base_image_version": "1 0"},
            microvms.InvalidArgError,
            "baseImageVersion",
            id="pin",
        ),
        pytest.param(
            {"project_dir": "EMPTY"},
            microvms.PreconditionError,
            r"pyproject\.toml\+uv\.lock",
            id="project",
        ),
    ],
)
def test_ensure_image_refuses_them_before_any_call(
    tmp_path: Path,
    overrides: dict[str, object],
    error: type[Exception],
    cause: str,
) -> None:
    if overrides.get("project_dir") == "EMPTY":
        overrides = {"project_dir": tmp_path}
    dockerfile = microvms.wrap_dockerfile(
        f"FROM {microvms.BaseImage.al2023().docker_ref}\n"
    )
    with pytest.raises(error, match=cause):
        sandbox().ensure_image(  # type: ignore[arg-type]
            name_prefix="task",
            binary=DAEMON,
            dockerfile=dockerfile,
            s3_bucket="agentd-conformance-bucket",
            build_role_arn=ROLE,
            **overrides,
        )
