# SPDX-License-Identifier: Apache-2.0
"""The AGENTS.md reference check fails on a name that points at nothing, and on reading nothing.

Each case runs the real script in a throwaway git repo, because the docs and the paths come
from `git ls-files` in the directory it's pointed at. A healthy fixture names one reference of
every kind; each failing case breaks one thing in it and requires the output to name it.
"""

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).with_name("check-agents-md.py")

# The pointers a git hook exports, copied from test_license_headers.py (scripts aren't
# importable). Inherited from lefthook's pre-push, they'd turn `git init` and `git add` below
# into writes to the real repo's index.
GIT_ENV_LEAKS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_CEILING_DIRECTORIES",
)

# `check` reaches `guards:list` through `lint`, so "in `check`" is held transitively.
MISE = """\
[tasks.check]
depends = ["lint", "parity:check"]

[tasks.lint]
depends = ["guards:list"]

[tasks."guards:fire"]
run = "true"

[tasks."guards:list"]
run = "true"

[tasks."parity:check"]
run = "true"
"""

CI = """\
name: ci
on: [push]
jobs:
  security:
    name: semgrep, secret history, licenses, workflow lint
    runs-on: ubuntu-latest
    steps:
      - run: "true"

# A blank line and a column-0 comment inside `jobs:` don't end the block.
  guards:
    name: seeded faults fire
    runs-on: ubuntu-latest
    steps:
      - run: "true"
  mutants:
    name: mutation testing (shard ${{ matrix.shard }} of 4)
    runs-on: ubuntu-latest
    steps:
      - run: "true"
"""

RUN_RS = """\
class Results:
    def eq(self, name, actual, expected):
        return actual is not None and actual == expected

    def absent(self, name, value):
        return value is None
"""

AGENTS = """\
# Guide

```bash
mise run check         # the local gate
mise run guards:fire   # every seeded fault
```

## Checks that can fail

- Every guard ships with a seeded fault in `guards/faults.toml`. The CI `guards` job runs
  `mise run guards:fire`, and `guards:list` in `check` fails on a note with no entry.
- The CI `mutants` job fails on a surviving mutant.
- `Results.eq` in `conformance/run_rs.py` fails on an absent value, and `Results.absent`
  is the one way to assert absence. `parity:check` holds the table.
"""

FAULTS = """\
[[fault]]
id = "fs-pop"
guard = "fs::tests::fs_pop_is_refused"
transform = { file = "crate/src/lib.rs", replace = "fn", with = "fn fs_pop_seeded_name" }
"""

CONTRIBUTING = """\
Run `mise run guards:list` before a push, and `mise run guards:fire -- --only fs-pop` for one
fault. It printed `fired: fs-pop`, and `fs_pop_is_refused` failed. Each crate has a
`Cargo.toml`, and the crate docs are under `crate/src/`.
"""

HEALTHY = {
    "mise.toml": MISE,
    ".github/workflows/ci.yml": CI,
    ".github/PULL_REQUEST_TEMPLATE.md": "## Guards\n\nName the `guards/faults.toml` entry.\n",
    "conformance/run_rs.py": RUN_RS,
    "guards/faults.toml": FAULTS,
    "AGENTS.md": AGENTS,
    "CONTRIBUTING.md": CONTRIBUTING,
    "crate/Cargo.toml": "",
    "crate/src/lib.rs": "fn fs_pop_is_refused() {}\n",
    # `src/lib.rs` is the crate's own, resolved from the doc's directory; the second path is
    # the repo's, resolved from the root.
    "crate/AGENTS.md": (
        "The crate docs in `src/lib.rs`. The registry is `guards/faults.toml`.\n"
        "CodeQL's `rust/hard-coded-cryptographic-value` isn't a path. cargo-mutants writes\n"
        "`missed.txt` and `timeout.txt` into `mutants.out`.\n"
    ),
}


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


class AgentsMdTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        git(self.repo, "init", "-q")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, relative: str, text: str) -> None:
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text), encoding="utf-8")
        git(self.repo, "add", relative)

    def healthy(self, **overrides: str | None) -> None:
        """The healthy fixture, with a file replaced (a string) or left out (None)."""
        for relative, text in {**HEALTHY, **overrides}.items():
            if text is not None:
                self.write(relative, text)

    def run_check(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=self.repo,
            capture_output=True,
            text=True,
            env=clean_env(),
        )

    def assert_fails_with(self, *needles: str) -> None:
        result = self.run_check()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        for needle in needles:
            self.assertIn(needle, result.stdout)

    # ── the healthy tree ──────────────────────────────────────────────────────

    def test_a_tree_whose_references_all_resolve_passes(self):
        self.healthy()
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("agents:check: every reference", result.stdout)

    def test_the_root_option_reads_another_tree(self):
        self.healthy()
        with tempfile.TemporaryDirectory() as elsewhere:
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--root", str(self.repo)],
                cwd=elsewhere,
                capture_output=True,
                text=True,
                env=clean_env(),
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    # ── each kind of dangling reference ───────────────────────────────────────

    def test_a_dangling_mise_run_task_fails_naming_it_and_its_line(self):
        self.healthy(**{"CONTRIBUTING.md": "Run `mise run guards:fyre -- --only x`.\n"})
        self.assert_fails_with(
            "CONTRIBUTING.md:1:", "`mise run guards:fyre`", "mise.toml"
        )

    def test_a_task_renamed_in_mise_toml_fails_the_doc_that_names_it(self):
        # The issue's deliberate break: the docs stay put and the task moves.
        self.healthy(**{"mise.toml": MISE.replace('"guards:fire"', '"guards:seed"')})
        self.assert_fails_with("AGENTS.md:5:", "`mise run guards:fire`")

    def test_a_dangling_task_in_a_fence_fails(self):
        self.healthy(
            **{"AGENTS.md": AGENTS.replace("mise run check ", "mise run chek ")}
        )
        self.assert_fails_with("AGENTS.md:4:", "`mise run chek`")

    def test_a_dangling_bare_task_name_fails(self):
        self.healthy(**{"AGENTS.md": AGENTS.replace("`parity:check`", "`parity:chek`")})
        self.assert_fails_with("AGENTS.md:14:", "`parity:chek`")

    def test_a_placeholder_task_is_not_a_reference(self):
        text = AGENTS + "\n`mise run ci:<job>` runs one job.\n"
        self.healthy(**{"AGENTS.md": text})
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_dangling_repo_path_fails(self):
        self.healthy(
            **{
                "AGENTS.md": AGENTS.replace(
                    "`guards/faults.toml`", "`guards/fault.toml`"
                )
            }
        )
        self.assert_fails_with("AGENTS.md:10:", "`guards/fault.toml`", "no such path")

    def test_a_dangling_crate_relative_path_fails(self):
        self.healthy(**{"crate/AGENTS.md": "The crate docs in `src/main.rs`.\n"})
        self.assert_fails_with("crate/AGENTS.md:1:", "`src/main.rs`")

    def test_a_path_deleted_from_the_tree_but_still_in_the_index_fails(self):
        self.healthy()
        (self.repo / "guards/faults.toml").unlink()
        self.assert_fails_with("`guards/faults.toml`")

    def test_a_new_untracked_file_resolves(self):
        # An uncommitted worktree: the doc and the file it names are both new.
        self.healthy()
        (self.repo / "scripts").mkdir()
        (self.repo / "scripts/new-check.py").write_text("", encoding="utf-8")
        with (self.repo / "CONTRIBUTING.md").open("a", encoding="utf-8") as doc:
            doc.write("And `scripts/new-check.py`.\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_gitignored_local_path_resolves(self):
        # The live tier's Terraform state is local on purpose, and the docs name it.
        self.healthy(**{".gitignore": "conformance/infra/terraform.tfstate*\n"})
        with (self.repo / "CONTRIBUTING.md").open("a", encoding="utf-8") as doc:
            doc.write("Copy `conformance/infra/terraform.tfstate` in first.\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_slash_name_the_script_lists_as_no_path_fails_once_no_doc_names_it(self):
        self.healthy(**{"crate/AGENTS.md": "The crate docs in `src/lib.rs`.\n"})
        self.assert_fails_with("NOT_PATHS lists `rust/hard-coded-cryptographic-value`")

    def test_a_missing_guards_job_fails(self):
        self.healthy(
            **{".github/workflows/ci.yml": CI.replace("  guards:\n", "  guard:\n")}
        )
        self.assert_fails_with("AGENTS.md:10:", "the `guards` job", "ci.yml")

    def test_a_missing_mutants_job_fails(self):
        self.healthy(
            **{".github/workflows/ci.yml": CI.replace("  mutants:\n", "  mutate:\n")}
        )
        self.assert_fails_with("AGENTS.md:12:", "the `mutants` job")

    def test_a_job_named_by_its_display_name_resolves(self):
        with_display = AGENTS + "\nCI's `seeded faults fire` job seeds them.\n"
        self.healthy(**{"AGENTS.md": with_display})
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_missing_results_absent_fails(self):
        without = RUN_RS.replace("def absent(", "def missing(")
        self.healthy(**{"conformance/run_rs.py": without})
        self.assert_fails_with(
            "AGENTS.md:13:", "`Results.absent`", "conformance/run_rs.py"
        )

    def test_a_missing_results_eq_fails(self):
        without = RUN_RS.replace("def eq(", "def equal(")
        self.healthy(**{"conformance/run_rs.py": without})
        self.assert_fails_with("AGENTS.md:13:", "`Results.eq`")

    def test_an_instance_spelling_of_a_symbol_is_checked_too(self):
        # conformance/AGENTS.md writes `results.eq`, the instance a check calls.
        self.healthy(**{"crate/AGENTS.md": "Use `results.absnt` for nothing.\n"})
        self.assert_fails_with("crate/AGENTS.md:1:", "`results.absnt`")

    # ── the kinds added in the first review round ─────────────────────────────

    def test_a_task_said_to_be_in_check_that_check_doesnt_reach_fails(self):
        self.healthy(
            **{"mise.toml": MISE.replace('depends = ["guards:list"]', 'run = "true"')}
        )
        self.assert_fails_with("AGENTS.md:11:", "`guards:list` isn't in `check`")

    def test_a_mise_run_task_said_to_be_in_mise_run_check_is_held_too(self):
        text = CONTRIBUTING + "`mise run guards:fire` in `mise run check` seeds them.\n"
        self.healthy(**{"CONTRIBUTING.md": text})
        self.assert_fails_with("CONTRIBUTING.md:4:", "`guards:fire` isn't in `check`")

    def test_a_dangling_fault_id_fails(self):
        self.healthy(**{"guards/faults.toml": FAULTS.replace('"fs-pop"', '"fs-pop-2"')})
        self.assert_fails_with("CONTRIBUTING.md:1:", "`fs-pop` is no fault id")

    def test_a_placeholder_fault_id_is_not_a_reference(self):
        text = (
            CONTRIBUTING + "Show `mise run guards:fire -- --only <id>` printing it.\n"
        )
        self.healthy(**{"CONTRIBUTING.md": text})
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_renamed_test_the_docs_name_fails(self):
        # The issue's own case: the template named a test that didn't exist. The rename
        # reaches the registry's `guard` too, because `guards:list` makes the author carry it.
        self.healthy(
            **{
                "crate/src/lib.rs": "fn fs_pop_refused() {}\n",
                "guards/faults.toml": FAULTS.replace(
                    "fs_pop_is_refused", "fs_pop_refused"
                ),
            }
        )
        self.assert_fails_with("CONTRIBUTING.md:2:", "`fs_pop_is_refused`")

    def test_a_name_spelled_only_in_prose_about_the_code_fails(self):
        # A docstring example, a comment and a seeded transform aren't the name existing.
        prose = {
            "crate/src/lib.rs": "// fs_pop_is_refused\nfn other() {}\n",
            "scripts/tool.py": '"""An example: fs_pop_is_refused."""\n# fs_pop_is_refused\n',
            "guards/faults.toml": FAULTS.replace(
                'with = "fn fs_pop_seeded_name"', 'with = "fs_pop_is_refused"'
            ).replace("fs::tests::fs_pop_is_refused", "fs::tests::other"),
            "notes.md": "fs_pop_is_refused\n",
        }
        self.healthy(**prose)
        self.assert_fails_with("`fs_pop_is_refused` isn't spelled in any code")

    def test_a_name_in_a_python_string_or_a_registry_guard_resolves(self):
        cases = {
            "scripts/tool.py": 'TEST = "fs::tests::fs_pop_is_refused"\n',
            "guards/faults.toml": FAULTS,
        }
        for relative, text in cases.items():
            with self.subTest(file=relative):
                self.healthy(**{"crate/src/lib.rs": "fn other() {}\n", relative: text})
                result = self.run_check()
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                git(self.repo, "rm", "-q", "--cached", "-f", relative)
                (self.repo / relative).unlink()

    def test_a_dangling_bare_file_name_fails(self):
        self.healthy(
            **{"CONTRIBUTING.md": CONTRIBUTING.replace("`Cargo.toml`", "`Cargo.tom`")}
        )
        self.assert_fails_with("CONTRIBUTING.md:3:", "`Cargo.tom` is no file's name")

    def test_a_glob_that_matches_nothing_fails_and_one_that_matches_resolves(self):
        self.healthy(**{"CONTRIBUTING.md": CONTRIBUTING + "And `crate/src/*.rs`.\n"})
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.healthy(**{"CONTRIBUTING.md": CONTRIBUTING + "And `crate/tests/*.rs`.\n"})
        self.assert_fails_with(
            "CONTRIBUTING.md:4:", "`crate/tests/*.rs` matches no file"
        )

    def test_a_path_with_a_line_suffix_is_checked_without_it(self):
        self.healthy(
            **{"CONTRIBUTING.md": CONTRIBUTING + "See `crate/src/main.rs:12:5:`.\n"}
        )
        self.assert_fails_with(
            "CONTRIBUTING.md:4:", "`crate/src/main.rs` is no such path"
        )

    def test_a_directory_reference_resolves(self):
        # CONTRIBUTING.md names `crate/src/`, a directory with no file of that name.
        self.healthy()
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("crate/src/", result.stdout)
        self.healthy(
            **{"CONTRIBUTING.md": CONTRIBUTING.replace("`crate/src/`", "`crate/lib/`")}
        )
        self.assert_fails_with("`crate/lib/` is no such path")

    def test_a_tool_key_or_module_specifier_is_not_a_task(self):
        text = (
            CONTRIBUTING
            + "Tests use `node:test`; mise installs `cargo:cargo-mutants`.\n"
        )
        self.healthy(**{"CONTRIBUTING.md": text})
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_symbol_written_as_a_call_is_checked(self):
        self.healthy(
            **{"AGENTS.md": AGENTS.replace("`Results.absent`", "`Results.missing()`")}
        )
        self.assert_fails_with("AGENTS.md:13:", "`Results.missing` isn't a method")

    def test_an_unclosed_fence_fails_naming_its_line(self):
        text = AGENTS.replace(
            "## Checks that can fail\n", "## Checks that can fail\n```\n"
        )
        self.healthy(**{"AGENTS.md": text})
        self.assert_fails_with("AGENTS.md:9: this fence is never closed")

    def test_a_shorter_fence_inside_a_longer_one_doesnt_close_it(self):
        # A four-backtick fence around a three-backtick line: what follows the real close is
        # still prose, and its references are still read.
        text = AGENTS + "\n````md\n```\n````\n\nAnd `guards/fault.toml`.\n"
        self.healthy(**{"AGENTS.md": text})
        self.assert_fails_with("`guards/fault.toml` is no such path")

    # ── the floor, the sentinels and parsers that return nothing ──────────────

    def test_an_empty_doc_set_fails_naming_the_floor(self):
        self.healthy(
            **{
                "AGENTS.md": None,
                "CONTRIBUTING.md": None,
                "crate/AGENTS.md": None,
                ".github/PULL_REQUEST_TEMPLATE.md": None,
            }
        )
        self.assert_fails_with("found no docs to read")

    def test_a_doc_set_without_the_root_guide_fails_naming_it(self):
        self.healthy(**{"AGENTS.md": None})
        self.assert_fails_with("AGENTS.md, which this repo always has")

    def test_docs_without_the_sentinel_fail_naming_it(self):
        text = AGENTS.replace("mise run check ", "mise run guards:list ")
        self.healthy(**{"AGENTS.md": text})
        self.assert_fails_with("sentinel", "`mise run check`")

    def test_docs_with_no_reference_of_a_kind_fail_naming_the_kind(self):
        # A kind only CONTRIBUTING.md names here: an extractor that stopped matching it
        # reads as that kind's floor.
        cases = {
            "fault": CONTRIBUTING.replace("-- --only fs-pop", "").replace(
                "`fired: fs-pop`", "that"
            ),
            "file": CONTRIBUTING.replace("`Cargo.toml`", "manifest"),
            "identifier": CONTRIBUTING.replace("`fs_pop_is_refused`", "its test"),
        }
        for kind, text in cases.items():
            with self.subTest(kind=kind):
                self.healthy(**{"CONTRIBUTING.md": text})
                self.assert_fails_with(f"found no {kind} references")

    def test_a_root_guide_missing_a_kind_its_rules_use_fails_naming_it(self):
        # The other docs still name every kind, so only the root's own floor sees this.
        cases = {
            "task": AGENTS.replace("mise run ", "run ")
            .replace("`parity:check`", "it")
            .replace("`guards:list`", "the list"),
            "member": AGENTS.replace(" in `check`", ""),
            "path": AGENTS.replace("`guards/faults.toml`", "the registry").replace(
                "`conformance/run_rs.py`", "the suite"
            ),
            "job": AGENTS.replace("`guards` job", "guards job").replace(
                "`mutants` job", "mutants job"
            ),
            "symbol": AGENTS.replace("`Results.eq`", "eq").replace(
                "`Results.absent`", "absent"
            ),
        }
        for kind, text in cases.items():
            with self.subTest(kind=kind):
                self.healthy(**{"AGENTS.md": text})
                self.assert_fails_with(f"AGENTS.md yields no {kind} references")

    def test_an_empty_root_guide_fails_even_when_the_others_name_every_kind(self):
        self.healthy(**{"AGENTS.md": ""})
        self.assert_fails_with("AGENTS.md yields no references")

    def test_a_fault_registry_with_no_ids_fails_naming_the_parser(self):
        self.healthy(**{"guards/faults.toml": ""})
        self.assert_fails_with("found no fault ids in guards/faults.toml")

    def test_a_check_task_with_no_dependencies_fails_naming_it(self):
        self.healthy(
            **{
                "mise.toml": MISE.replace(
                    'depends = ["lint", "parity:check"]', 'run = "true"'
                )
            }
        )
        self.assert_fails_with("`check` depends on no task in mise.toml")

    def test_a_workflow_with_no_jobs_fails_naming_the_parser(self):
        self.healthy(**{".github/workflows/ci.yml": "name: ci\non: [push]\n"})
        self.assert_fails_with("found no jobs in .github/workflows/ci.yml")

    def test_a_mise_toml_with_no_tasks_fails_naming_the_parser(self):
        self.healthy(**{"mise.toml": "[tools]\n"})
        self.assert_fails_with("found no tasks in mise.toml")

    def test_a_suite_with_no_results_class_fails_naming_it(self):
        self.healthy(**{"conformance/run_rs.py": "def main():\n    pass\n"})
        self.assert_fails_with("no class Results in conformance/run_rs.py")

    def test_an_inherited_git_dir_leaves_that_repo_alone(self):
        # The hook case: `GIT_DIR` names another repo while a case runs. The decoy's index
        # must stay empty, and the case must still pass on its own throwaway repo.
        with tempfile.TemporaryDirectory() as decoy:
            git(Path(decoy), "init", "-q")
            gitdir = str(Path(decoy) / ".git")
            with mock.patch.dict(os.environ, {"GIT_DIR": gitdir}):
                self.test_a_tree_whose_references_all_resolve_passes()
            self.assertEqual(git(Path(decoy), "ls-files"), "")


if __name__ == "__main__":
    unittest.main()
