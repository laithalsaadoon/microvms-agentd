# SPDX-License-Identifier: Apache-2.0
"""`build --project`: the lockfile-keyed environment layer, its content-hash name and reuse,
and a VM importing the locked dependency with no egress."""

from __future__ import annotations

import secrets
import time
from pathlib import Path
from typing import Any

from harness.cli import Cli
from harness.constants import BASELINE_MEMORY_MIB
from harness.envelope import Envelope
from harness.results import Results

#: The one-dependency uv project the `--project` section bakes. Locked 2026-09-02 with
#: uv 0.12.1 and embedded verbatim rather than re-locked at run time, because the whole
#: point of the check is that **these bytes** are the image identity: a lock produced
#: fresh on each run would resolve to whatever attrs is current and the image name would
#: move under the check. `uv sync --locked` in the derived Dockerfile refuses a lock that
#: disagrees with the manifest, so a stale pair fails the build loudly rather than
#: quietly re-resolving.
PROJECT_PYPROJECT = """\
[project]
name = "w4-probe"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["attrs"]
"""

PROJECT_UV_LOCK = """\
version = 1
revision = 3
requires-python = ">=3.12"

[[package]]
name = "attrs"
version = "26.1.0"
source = { registry = "https://pypi.org/simple" }
sdist = { url = "https://files.pythonhosted.org/packages/9a/8e/82a0fe20a541c03148528be8cac2408564a6c9a0cc7e9171802bc1d26985/attrs-26.1.0.tar.gz", hash = "sha256:d03ceb89cb322a8fd706d4fb91940737b6642aa36998fe130a9bc96c985eff32", size = 952055, upload-time = "2026-03-19T14:22:25.026Z" }
wheels = [
    { url = "https://files.pythonhosted.org/packages/64/b4/17d4b0b2a2dc85a6df63d1157e028ed19f90d4cd97c36717afef2bc2f395/attrs-26.1.0-py3-none-any.whl", hash = "sha256:c647aa4a12dfbad9333ca4e71fe62ddc36f4e63b2d260a37a8b83d2f043ac309", size = 67548, upload-time = "2026-03-19T14:22:23.645Z" },
]

[[package]]
name = "w4-probe"
version = "0.1.0"
source = { virtual = "." }
dependencies = [
    { name = "attrs" },
]

[package.metadata]
requires-dist = [{ name = "attrs" }]
"""

#: What the baked layer is asked to prove: the venv `uv sync --locked` wrote at build
#: time answers an import, from the image WORKDIR, with nothing installed at launch. The
#: version string is the lock's, so a guest that had somehow re-resolved would fail here.
PROJECT_PROBE = (
    "pwd; /project/.venv/bin/python -c "
    '\'import attrs,sys;print("attrs", attrs.__version__, "python", '
    "sys.version.split()[0])'"
)
PROJECT_LOCKED_ATTRS = "attrs 26.1.0"


def drive_project_build(
    cli: Cli, binary: Path, project: Path, logs: Any, results: Results
) -> None:
    """`build --project --reuse` twice, then a launch proving the layer (issue #74).

    Its own build (~3 minutes), for the quickstart section's reason: the environment
    layer is baked at build time, so no launch from the suite's image can carry one. Live
    rather than only scripted because every local guard sees the derived Dockerfile as
    text — the toolchain bootstrap it runs (`dnf install python3.12`, `pip install uv`,
    `uv sync --locked` on al2023-minimal, under the platform's builder) is a claim about
    the real base image and the real network, and the first arm64 build of it is the
    first time anything checks it.

    Three properties, in the order the issue states them. The **name** is the content
    hash: `<prefix>-<12 hex>`, derived from the daemon bytes, the Dockerfile, and the
    manifest+lockfile pair. The **reuse**: a second build with identical files reports
    `reused: true` and the same name, costing a listing rather than a build. The
    **layer**: a VM launched from the image — with no `--egress`, so nothing could be
    installed at launch — imports the locked dependency from the venv the build wrote.
    The lockfile-edit half (a changed lock is a new name and a fresh build) is the pure
    function `the_content_hash_keys_on_the_lockfile` pins in core and was run live once
    by hand (2026-09-02, `docs/PLATFORM.md`); it is not repeated here because it is a
    second three-minute build for a property the unit test states exactly.

    `--keep` and an explicit `terminate --delete-image`, so the teardown is asserted
    rather than trusted. The service-created log group is deleted here, under the same
    discipline `drive_teardown` applies to the suite's own.
    """
    print("\n== build --project (lockfile-keyed environment layer, own build) ==")
    project.mkdir(parents=True, exist_ok=True)
    (project / "pyproject.toml").write_text(PROJECT_PYPROJECT)
    (project / "uv.lock").write_text(PROJECT_UV_LOCK)
    prefix = f"microvm-cli-project-{secrets.token_hex(4)}"
    build_args = (
        "build",
        str(binary),
        "--project",
        str(project),
        "--reuse",
        "--name",
        prefix,
        "--memory",
        str(BASELINE_MEMORY_MIB),
        "--region",
        cli.region,
    )

    started = time.monotonic()
    built = cli.call(*build_args, timeout=50 * 60)
    build_seconds = time.monotonic() - started
    results.eq(
        "build --project emitted the image envelope", built.type, "microvm.image"
    )
    name = str(built.data.get("imageName") or "")
    suffix = name.removeprefix(f"{prefix}-")
    results.check(
        "the project image is named by the content hash",
        name != suffix
        and len(suffix) == 12
        and all(c in "0123456789abcdef" for c in suffix),
        f"{name!r}",
    )
    results.check(
        "the first build of the pair built rather than reused",
        built.data.get("reused") is False,
        f"reused={built.data.get('reused')!r} in {build_seconds:.0f}s",
    )

    started = time.monotonic()
    again = cli.call(*build_args, timeout=5 * 60)
    reuse_seconds = time.monotonic() - started
    results.check(
        "identical dependency files reuse the image without a build",
        again.data.get("reused") is True and again.data.get("imageName") == name,
        f"reused={again.data.get('reused')!r} name={again.data.get('imageName')!r} "
        f"in {reuse_seconds:.1f}s",
    )

    image = str(built.data["imageIdentifier"])
    launched: Envelope | None = None
    try:
        started = time.monotonic()
        launched = cli.call(
            "run",
            "--image",
            image,
            "--keep",
            "--exec",
            PROJECT_PROBE,
            "--memory",
            str(BASELINE_MEMORY_MIB),
            "--region",
            cli.region,
            "--max-idle-sec",
            "600",
            "--suspended-sec",
            "600",
            "--max-duration-sec",
            "3600",
            timeout=15 * 60,
        )
        launch_seconds = time.monotonic() - started
        stdout = launched.data.get("stdout") or ""
        results.check(
            "the baked layer imports the locked dependency with no egress and no install",
            launched.data.get("execExitCode") == 0
            and PROJECT_LOCKED_ATTRS in stdout
            and "/project" in stdout,
            f"exit={launched.data.get('execExitCode')!r} stdout={stdout.strip()!r} "
            f"launch+exec {launch_seconds:.1f}s",
        )
    finally:
        if launched is not None:
            torn = cli.call(
                "terminate",
                str(launched.data["microvmId"]),
                "--image-identifier",
                image,
                "--image-name",
                name,
                "--delete-image",
                "--wait",
                "--region",
                cli.region,
                timeout=15 * 60,
            )
            results.check(
                "the project VM and image were deleted",
                torn.type == "microvm.teardown" and not torn.data.get("leaked"),
                f"leaked={torn.data.get('leaked')!r}",
            )
            # The service-created group, named by terminate and deleted by the party that
            # caused it to exist — `drive_teardown`'s docstring carries the argument. An
            # already-absent group is the desired end state, not a failure.
            groups = [str(group) for group in torn.data.get("undeletedLogGroups") or []]
            failures: list[str] = []
            for group in groups:
                try:
                    logs.delete_log_group(logGroupName=group)
                except Exception as exc:  # noqa: BLE001 - the reason is the finding
                    if type(exc).__name__ != "ResourceNotFoundException":
                        failures.append(f"{group}: {type(exc).__name__}: {exc}")
            results.check(
                "the suite deleted the project build's log group",
                bool(groups) and not failures,
                f"groups={groups!r} failures={failures!r}",
            )
