# SPDX-License-Identifier: Apache-2.0
"""The changelog check fails a branch that changes shipped code with no fragment, and each
malformed fragment, and passes what it should.

Each case runs the real script, through uv so towncrier is the pinned one, in a throwaway git
repo holding this repo's towncrier.toml and template, a CHANGELOG.md with one release, and a
file under each path the script calls shipped. `origin/main` is the fixture's first commit.
"""

import os
import runpy
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).with_name("changelog.py")
REPO = SCRIPT.parent.parent
# The script's own constants (its `main` doesn't run under runpy), so the fixture follows the
# shipped set rather than a copy of it.
CONSTANTS = runpy.run_path(str(SCRIPT))
SHIPPED = CONSTANTS["SHIPPED"]
DEPENDABOT = CONSTANTS["DEPENDABOT"]

CHANGELOG = """# Changelog

The fixture's header.

<!-- towncrier release notes start -->

## [1.0.0] - 2026-01-01

### Added

- **The first release (#1).** It had one entry.
"""
ENTRY = "- **A fixed thing (#7).** It works now.\n  Its second line.\n"

# The pointers a git hook exports (the copy check-live-wiring.py and test_license_headers.py
# keep). `mise run check` runs from lefthook's pre-push, where git exports `GIT_DIR` in a
# linked worktree, and inherited it would aim the fixture's git at the real repo's index.
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
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env=clean_env(),
    ).stdout


class ChangelogTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "test@example.com")
        git(self.repo, "config", "user.name", "test")
        git(self.repo, "config", "commit.gpgsign", "false")
        shutil.copy(REPO / "towncrier.toml", self.repo / "towncrier.toml")
        (self.repo / "changelog.d").mkdir()
        shutil.copy(
            REPO / "changelog.d/template.md", self.repo / "changelog.d/template.md"
        )
        self.write("CHANGELOG.md", CHANGELOG)
        for path in SHIPPED:
            self.write(f"{path}lib.rs" if path.endswith("/") else path, "// shipped\n")
        self.write("scripts/tool.py", "# machinery\n")
        self.write("Cargo.toml", "[workspace]\n")
        self.commit("base")
        git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, relative: str, text: str) -> None:
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def commit(self, message: str) -> None:
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", message)

    def run_script(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["uv", "run", "--quiet", "--script", str(SCRIPT), *args],
            cwd=self.repo,
            capture_output=True,
            text=True,
            env=clean_env(),
        )

    def assert_check(self, code: int, *args: str) -> str:
        result = self.run_script("check", *args)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, code, output)
        return output

    def change_shipped(self) -> None:
        self.write("microvms-core/src/lib.rs", "// shipped, changed\n")

    # ── the branch rule ─────────────────────────────────────────────────────

    def test_a_branch_with_no_change_passes(self):
        output = self.assert_check(0)
        self.assertIn("changes no shipped code", output)

    def test_a_shipped_change_with_no_fragment_fails(self):
        self.change_shipped()
        output = self.assert_check(1)
        self.assertIn(
            "changes shipped code against origin/main and adds no fragment", output
        )
        self.assertIn("  microvms-core/src/lib.rs", output)

    def test_a_committed_shipped_change_with_no_fragment_fails(self):
        git(self.repo, "switch", "-q", "-c", "feature")
        self.change_shipped()
        self.commit("change")
        output = self.assert_check(1)
        self.assertIn("  microvms-core/src/lib.rs", output)

    def test_each_shipped_path_needs_a_fragment(self):
        for path in SHIPPED:
            relative = f"{path}lib.rs" if path.endswith("/") else path
            with self.subTest(relative):
                git(self.repo, "checkout", "-q", "--", ".")
                self.write(relative, "// changed\n")
                output = self.assert_check(1)
                self.assertIn(f"  {relative}", output)

    def test_a_fragment_satisfies_the_rule(self):
        self.change_shipped()
        self.write("changelog.d/7.fixed.md", ENTRY)
        output = self.assert_check(0)
        self.assertIn("with changelog.d/7.fixed.md", output)

    def test_a_changed_fragment_satisfies_the_rule(self):
        self.write("changelog.d/7.fixed.md", ENTRY)
        self.commit("an entry")
        git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        self.change_shipped()
        self.write("changelog.d/7.fixed.md", ENTRY + "- **Another (#7).** Also.\n")
        self.assert_check(0)

    def test_an_internal_fragment_satisfies_the_rule(self):
        self.change_shipped()
        self.write(
            "changelog.d/7.internal.md", "Tests only: a unit test beside the code.\n"
        )
        self.assert_check(0)

    def test_a_change_outside_shipped_code_needs_no_fragment(self):
        self.write("scripts/tool.py", "# machinery, changed\n")
        self.write("Cargo.toml", "[workspace]\nmembers = []\n")
        self.write("microvms-core/tests/new.rs", "// a test\n")
        output = self.assert_check(0)
        self.assertIn("changes no shipped code", output)

    def test_a_test_only_module_needs_no_fragment(self):
        self.write("microvms-domain/src/lib.rs", "#[cfg(test)]\nmod sizing_fuzz;\n")
        self.write("microvms-domain/src/sizing_fuzz.rs", "// fuzz\n")
        self.commit("a fuzz module")
        git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        self.write("microvms-domain/src/sizing_fuzz.rs", "// fuzz, changed\n")
        output = self.assert_check(0)
        self.assertIn("changes no shipped code", output)

    def test_an_exclusion_over_code_that_ships_fails(self):
        self.write("microvms-domain/src/lib.rs", "mod sizing_fuzz;\n")
        self.write("microvms-domain/src/sizing_fuzz.rs", "// compiled into the crate\n")
        output = self.assert_check(1)
        self.assertIn("NOT_SHIPPED covers microvms-domain/src/sizing_fuzz.rs", output)

    def test_a_hand_edit_of_the_changelog_is_not_a_fragment(self):
        self.change_shipped()
        self.write("CHANGELOG.md", CHANGELOG + "- **An entry written by hand (#7).**\n")
        output = self.assert_check(1)
        self.assertIn("a hand-written entry there is what fragments replace", output)

    def test_dependabot_takes_no_fragment(self):
        self.change_shipped()
        output = self.assert_check(0, "--author", DEPENDABOT)
        self.assertIn("take no fragment", output)

    def test_the_dependabot_skip_is_for_dependabot_only(self):
        self.change_shipped()
        for author in (
            "bonk-ai[bot]",
            "Dependabot[bot]",
            "dependabot",
            "renovate[bot]",
        ):
            with self.subTest(author):
                output = self.assert_check(1, "--author", author)
                self.assertIn("adds no fragment", output)

    def test_dependabot_still_gets_the_fragment_rules(self):
        self.write("changelog.d/7.adedd.md", ENTRY)
        self.assert_check(1, "--author", DEPENDABOT)

    def test_a_base_that_names_no_commit_fails(self):
        output = self.assert_check(1, "--base", "origin/nowhere")
        self.assertIn("--base origin/nowhere doesn't name a commit", output)

    # ── the fragment rules ──────────────────────────────────────────────────

    def test_a_fragment_of_an_unknown_type_fails(self):
        self.write("changelog.d/7.adedd.md", ENTRY)
        output = self.assert_check(1)
        self.assertIn("changelog.d/7.adedd.md: `adedd` isn't a type", output)

    def test_a_malformed_fragment_name_fails(self):
        cases = {
            "7.added": "not a fragment name",
            "notes.md": "not a fragment name",
            "7.added.md.txt": "not a fragment name",
            "7.added.1.2.md": "not a fragment name",
            "07.added.md": "isn't an issue number",
            "gh-7.added.md": "isn't an issue number",
            "+7.added.md": "isn't an issue number",
            "7.added.0.md": "isn't a counter",
            "7.added.x.md": "isn't a counter",
        }
        for name, why in cases.items():
            with self.subTest(name):
                self.write(f"changelog.d/{name}", ENTRY)
                output = self.assert_check(1)
                self.assertIn(f"changelog.d/{name}: ", output)
                self.assertIn(why, output)
                (self.repo / "changelog.d" / name).unlink()

    def test_a_directory_in_the_fragments_fails(self):
        self.write("changelog.d/added/7.md", ENTRY)
        output = self.assert_check(1)
        self.assertIn("changelog.d/added: not a file", output)

    def test_a_fragment_that_isnt_bold_lead_list_items_fails(self):
        cases = {
            "A fixed thing.\n": "line 1 doesn't open an entry",
            "- A fixed thing (#7).\n": "line 1 doesn't open an entry",
            "- **A (#7).** One.\nnot indented\n": "line 2 neither opens",
            "- **A (#7).** One.\n\n- **B (#7).** Two.\n": "line 2 is blank",
            "\n": "empty",
        }
        for text, why in cases.items():
            with self.subTest(text):
                self.write("changelog.d/7.fixed.md", text)
                output = self.assert_check(1)
                self.assertIn(why, output)

    def test_a_counter_and_several_entries_pass(self):
        self.write("changelog.d/7.fixed.md", ENTRY + "- **B (#7).** Two.\n")
        self.write("changelog.d/7.fixed.1.md", ENTRY)
        self.write("changelog.d/.gitkeep", "")
        output = self.assert_check(0)
        self.assertIn("2 fragments in changelog.d/, and they build", output)

    def test_a_shipped_path_with_no_file_fails(self):
        git(self.repo, "rm", "-q", "-r", "protocol/src")
        output = self.assert_check(1)
        self.assertIn(
            "SHIPPED names protocol/src/, which matches no file in the tree", output
        )

    # ── the draft and the release build ─────────────────────────────────────

    def test_the_draft_renders_each_entry_as_written(self):
        self.write("changelog.d/7.fixed.md", ENTRY)
        self.write("changelog.d/3.added.md", "- **An added thing (#3).** New.\n")
        self.write("changelog.d/9.internal.md", "Tests only.\n")
        result = self.run_script("draft")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(result.stdout.startswith("## [Unreleased] - "), result.stdout)
        added = result.stdout.index("### Added\n\n- **An added thing (#3).** New.\n")
        self.assertGreater(result.stdout.index(f"### Fixed\n\n{ENTRY}"), added)
        self.assertNotIn("Tests only", result.stdout)
        self.assertNotIn("Internal", result.stdout)

    def test_a_release_build_writes_the_section_and_passes_the_check(self):
        self.write("changelog.d/7.fixed.md", ENTRY)
        self.write("changelog.d/9.internal.md", "Tests only.\n")
        self.commit("entries")
        git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        result = self.run_script("build", "v1.1.0")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        text = (self.repo / "CHANGELOG.md").read_text()
        header, _, releases = text.partition("<!-- towncrier release notes start -->\n")
        self.assertEqual(header, CHANGELOG.partition("<!--")[0])
        self.assertRegex(
            releases, r"^\n## \[1\.1\.0\] - \d{4}-\d{2}-\d{2}\n\n### Fixed\n\n"
        )
        self.assertIn(f"### Fixed\n\n{ENTRY}\n## [1.0.0] - 2026-01-01\n", releases)
        self.assertNotIn("Tests only", text)
        self.assertEqual(
            sorted(p.name for p in (self.repo / "changelog.d").iterdir()),
            ["template.md"],
        )
        staged = git(self.repo, "diff", "--cached", "--name-status").split("\n")
        self.assertIn("M\tCHANGELOG.md", staged)
        self.assertIn("D\tchangelog.d/7.fixed.md", staged)
        # The release's version bump reaches shipped code, and the build is its fragment.
        self.write("microvms-py/microvms.pyi", "# version 1.1.0\n")
        output = self.assert_check(0)
        self.assertIn(
            "in a release build, which writes 2 fragments into CHANGELOG.md", output
        )

    def test_a_build_with_nothing_to_release_is_refused(self):
        self.write("changelog.d/9.internal.md", "Tests only.\n")
        result = self.run_script("build", "1.1.0")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("has no fragment that renders", result.stdout)
        self.assertEqual((self.repo / "CHANGELOG.md").read_text(), CHANGELOG)

    def test_a_build_refuses_a_version_that_isnt_semver(self):
        self.write("changelog.d/7.fixed.md", ENTRY)
        result = self.run_script("build", "1.1")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("`1.1` isn't a version", result.stdout)
        self.assertTrue((self.repo / "changelog.d/7.fixed.md").exists())

    def test_an_inherited_git_dir_leaves_that_repo_alone(self):
        # The hook case: `GIT_DIR` names another repo while a case runs. The decoy's index
        # must stay empty, and the case must still fail on its own throwaway repo.
        with tempfile.TemporaryDirectory() as decoy:
            git(Path(decoy), "init", "-q")
            with mock.patch.dict(os.environ, {"GIT_DIR": str(Path(decoy) / ".git")}):
                self.test_a_shipped_change_with_no_fragment_fails()
            self.assertEqual(git(Path(decoy), "ls-files"), "")


if __name__ == "__main__":
    unittest.main()
