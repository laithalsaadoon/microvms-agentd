# SPDX-License-Identifier: Apache-2.0
"""The logged-out `gh` environment the quickstart section runs in, offline."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from harness.results import Results
from lanes.quickstart import GH_TOKEN_VARIABLES, gh_logged_out


def check_gh_logged_out(results: "Results") -> None:
    """`gh_logged_out` strips every token, empties the config, and puts a recording shim
    first; the run file stays absent until something runs `gh`, then names what it ran."""
    with tempfile.TemporaryDirectory() as tmp:
        base = {name: "a-token" for name in GH_TOKEN_VARIABLES}
        base["PATH"] = "/usr/bin:/bin"
        base["HOME"] = tmp
        env, ran = gh_logged_out(Path(tmp), base)
        config = Path(env["GH_CONFIG_DIR"])
        results.check(
            "the gh-logged-out environment carries no token and an empty gh config",
            not any(name in env for name in GH_TOKEN_VARIABLES)
            and config.is_dir()
            and not any(config.iterdir())
            and env["HOME"] == tmp,
            f"{sorted(env)} config={sorted(config.iterdir())}",
        )
        results.check(
            "the gh shim is first on PATH and nothing has run it yet",
            env["PATH"].split(os.pathsep)[0] == str(ran.parent)
            and env["PATH"].endswith("/usr/bin:/bin")
            and not ran.exists(),
            env["PATH"],
        )
        try:
            proc = subprocess.run(
                ["gh", "release", "download", "v0.10.0"],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            exit_code, stderr = proc.returncode, proc.stderr
        except OSError as exc:
            exit_code, stderr = None, str(exc)
        results.check(
            "a gh run under it refuses as logged out and is recorded",
            exit_code == 4
            and "gh auth login" in stderr
            and ran.exists()
            and ran.read_text() == "release download v0.10.0\n",
            f"exit={exit_code} stderr={stderr!r} "
            f"ran={ran.read_text() if ran.exists() else None!r}",
        )
