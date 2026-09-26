# SPDX-License-Identifier: Apache-2.0
"""The header check refuses to pass on a file set it didn't actually read.

Each case runs the real script in a throwaway git repo, because the enumerator
is `git ls-files` in the working directory: an empty answer there is exactly the
state that used to print "all 0 tracked source files" and exit 0.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).with_name("check-license-headers.py")
SPDX = "SPDX-License-Identifier: Apache-2.0"
SENTINELS = ("scripts/check-license-headers.py", "microvms-core/src/lib.rs")

# One licensed file per extension the script checks, beside the two sentinels, so a
# healthy fixture meets the per-extension floor. Kept in step with the script's
# COMMENT by hand: scripts aren't importable modules here, and a fixture that fell
# behind fails the pass case loudly rather than hiding anything.
HEALTHY = {
    SENTINELS[0]: f"# {SPDX}\n",
    SENTINELS[1]: f"// {SPDX}\n",
    "python/stub.pyi": f"# {SPDX}\n",
    "js/test.mjs": f"// {SPDX}\n",
    "site/page.ts": f"// {SPDX}\n",
    "site/Card.astro": f"---\n// {SPDX}\n---\n",
    "site/theme.css": f"/* {SPDX} */\n",
    "examples/run.sh": f"#!/bin/sh\n# {SPDX}\n",
    "infra/main.tf": f"# {SPDX}\n",
}

# The pointers a git hook exports, copied from check-live-wiring.py's
# `_GIT_ENV_LEAKS` (scripts aren't importable). `mise run check` runs this file
# from lefthook's pre-push, and from a linked worktree git exports `GIT_DIR`
# there. Inherited, it turns `git init` and `git add` below into writes to the
# real repo's index: measured in review, a push staged a one-line
# `microvms-core/src/lib.rs` over the real one.
GIT_ENV_LEAKS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_CEILING_DIRECTORIES",
)


def clean_env() -> dict[str, str]:
    """`os.environ` without the inherited git pointers, read at call time."""
    return {k: v for k, v in os.environ.items() if k not in GIT_ENV_LEAKS}


def git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env=clean_env(),
    )
    return out.stdout


def run_check(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=repo,
        capture_output=True,
        text=True,
        env=clean_env(),
    )


def track(repo: Path, relative: str, text: str) -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    git(repo, "add", relative)


class LicenseHeaderFloorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        git(self.repo, "init", "-q")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_an_empty_repo_fails_and_names_the_enumerator(self):
        result = run_check(self.repo)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("git ls-files", result.stdout)
        self.assertIn("no tracked source files", result.stdout)

    def test_licensed_files_without_the_sentinels_fail_naming_them(self):
        # A set that isn't empty but lacks the files that must always be there
        # means the enumerator reached somewhere other than this repo.
        track(self.repo, "src/lib.rs", f"// {SPDX}\n")
        result = run_check(self.repo)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        for sentinel in SENTINELS:
            self.assertIn(sentinel, result.stdout)

    def test_a_licensed_file_of_every_kind_passes(self):
        for relative, text in HEALTHY.items():
            track(self.repo, relative, text)
        result = run_check(self.repo)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"all {len(HEALTHY)} tracked source files", result.stdout)

    def test_an_extension_that_yields_no_file_fails_naming_it(self):
        # The sentinels are a `.py` and a `.rs`, so they can't see a pathspec
        # that stopped matching `*.mjs`; the per-extension floor can.
        for relative, text in HEALTHY.items():
            if not relative.endswith(".mjs"):
                track(self.repo, relative, text)
        result = run_check(self.repo)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("none ending in .mjs", result.stdout)

    def test_an_unlicensed_sentinel_is_still_reported_as_missing(self):
        for relative, text in HEALTHY.items():
            track(self.repo, relative, text)
        track(self.repo, SENTINELS[1], "fn main() {}\n")
        result = run_check(self.repo)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(f"  {SENTINELS[1]}", result.stdout)

    def test_an_inherited_git_dir_leaves_that_repo_alone(self):
        # The hook case: `GIT_DIR` names another repo while a case runs. The
        # decoy's index must stay empty, and the case must still pass on its
        # own throwaway repo.
        with tempfile.TemporaryDirectory() as decoy:
            git(Path(decoy), "init", "-q")
            gitdir = str(Path(decoy) / ".git")
            with mock.patch.dict(os.environ, {"GIT_DIR": gitdir}):
                self.test_a_licensed_file_of_every_kind_passes()
            self.assertEqual(git(Path(decoy), "ls-files"), "")


if __name__ == "__main__":
    unittest.main()
