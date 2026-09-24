# SPDX-License-Identifier: Apache-2.0
"""`wrap_dockerfile` and `BaseImage.from_dockerfile` (IMAGE-5): thin, and core's refusals intact.

Pure functions over Dockerfile text; no credentials and no AWS call. The stanza, the guards,
and their messages are core's (IMAGE-1 through IMAGE-4); this file checks that the binding
passes the arguments through and raises core's refusals as `InvalidArgError`.
"""

from __future__ import annotations

import pytest

import microvms

MANAGED = microvms.BaseImage.al2023()


def test_a_bare_from_wraps_to_the_default_stanza() -> None:
    wrapped = microvms.wrap_dockerfile("FROM x\n")
    assert wrapped.startswith("FROM x\nCOPY agentd /agentd\nRUN chmod 0755 /agentd\n")
    assert wrapped.endswith(
        "ENV AGENTD_PORT=9000\nENV AGENTD_LOG=info\nEXPOSE 9000\n"
        'ENTRYPOINT []\nCMD ["/agentd"]\n'
    )
    assert "USER" not in wrapped


def test_the_options_reach_the_stanza() -> None:
    wrapped = microvms.wrap_dockerfile(
        "FROM python:3.12-slim\nUSER app", port=8080, workdir="/srv/task"
    )
    task, _, added = wrapped.partition("USER app\n")
    assert task == "FROM python:3.12-slim\n"
    assert added.startswith("USER root\nCOPY agentd /agentd\n")
    assert "RUN mkdir -p /srv/task\nWORKDIR /srv/task\n" in added
    assert "ENV AGENTD_PORT=8080\n" in added
    assert "EXPOSE 8080\n" in added


@pytest.mark.parametrize(
    ("task", "kwargs", "cause"),
    [
        ("RUN echo hello\n", {}, "no FROM"),
        ("FROM x\nRUN make \\\n", {}, "line continuation"),
        ("FROM x\nRUN <<EOF\necho open\n", {}, "heredoc"),
        ("FROM x\nENV AGENTD_SSE_KEEPALIVE_SECS=90\n", {}, "AGENTD_SSE_KEEPALIVE_SECS"),
        ("FROM x\n", {"workdir": "relative"}, "absolute path"),
        ("FROM x\n", {"port": 0}, "port"),
        ("FROM x\n", {"inherit_workdir": True}, "nothing to inherit"),
    ],
)
def test_core_refusals_raise_invalid_arg(
    task: str, kwargs: dict[str, object], cause: str
) -> None:
    with pytest.raises(microvms.InvalidArgError, match=cause):
        microvms.wrap_dockerfile(task, **kwargs)  # type: ignore[arg-type]


def test_a_derived_base_keeps_the_managed_name_and_takes_the_from() -> None:
    digest = "c439fb4994ea7ca529233d6256446d3f8b7b4efb58956073e015303a170011de"
    wrapped = microvms.wrap_dockerfile(f"FROM python:3.12-slim@sha256:{digest}\n")
    base = microvms.BaseImage.from_dockerfile(wrapped)
    assert base.name == MANAGED.name
    assert base.docker_ref == f"python:3.12-slim@sha256:{digest}"
    assert base.working_dir == ""


def test_a_dockerfile_with_no_from_has_no_base() -> None:
    with pytest.raises(microvms.InvalidArgError, match="no FROM"):
        microvms.BaseImage.from_dockerfile("RUN echo hello\n")
