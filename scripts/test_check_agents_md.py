# SPDX-License-Identifier: Apache-2.0
"""The AGENTS.md reference check fails on a name that points at nothing, and on reading nothing.

Each case runs the real script in a throwaway git repo, because the docs and the paths come
from `git ls-files` in the directory it's pointed at. A healthy fixture names one reference of
every kind; each failing case breaks one thing in it and requires the output to name it.
"""

import importlib.util
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).with_name("check-agents-md.py")


def load_script():
    """The script as a module, for its YAML reader and its exception list."""
    spec = importlib.util.spec_from_file_location("check_agents_md", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


CHECK = load_script()


def decision(number: int) -> str:
    """A decision id spelled so this file doesn't cite it: the real census reads this file."""
    return f"D{number}"


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

[tasks."agents:check"]
run = "./scripts/check-agents-md.py"
"""

CI = """\
name: ci
on: [push]
jobs:
  security:
    name: semgrep, secret history, licenses, workflow lint
    runs-on: ubuntu-latest
    steps:
      - run: ./scripts/check-agents-md.py

# A blank line and a column-0 comment inside `jobs:` don't end the block.
  # The guards job runs as shards (D35).
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

RESULTS = """\
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

- Every guard ships with a seeded fault in `guards/faults/fs.toml`. The CI `guards` job runs
  `mise run guards:fire`, and `guards:list` in `check` fails on a note with no entry.
- The CI `mutants` job fails on a surviving mutant.
- `Results.eq` in `conformance/run_rs.py` fails on an absent value, and `Results.absent`
  is the one way to assert absence. `parity:check` holds the table.
"""

# The fixture's registry file, one owner's, which check-guards-fire.py's loader reads.
REGISTRY = "guards/faults/fs.toml"
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

LEFTHOOK = """\
pre-commit:
  jobs:
    - name: workflow lint
      run: mise exec -- actionlint
      glob: ".github/workflows/*.yml"
    - name: headers
      root: crate/
      run: ../scripts/check-agents-md.py src/lib.rs
      glob: "{*.rs,guards/faults/*.toml}"
"""

# A second workflow: a path filter, a job's default working directory, and a step that names
# every word the script's exception list excuses, so none of its entries reads as stale.
FUZZ = f"""\
name: fuzz
on:
  pull_request:
    paths:
      - 'crate/src/**'
      - 'guards/faults/*.toml'
jobs:
  fuzz:
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: crate
    steps:
      - run: cargo fuzz run --fuzz-dir src/lib.rs
      - name: the words CENSUS_NOT_PATHS excuses
        run: echo {" ".join(CHECK.CENSUS_NOT_PATHS)}
"""

DEPENDABOT = """\
version: 2
updates:
  - package-ecosystem: cargo
    directory: /
  - package-ecosystem: cargo
    directory: /agentd/fuzz
"""

DECISIONS = """\
[D35]
decision = "The guards job runs as a matrix of shards."
source = "#346."
"""

# The script the sentinels name, standing in for this one, with the constants the census reads.
GATE = """\
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGISTRY_DIR = "guards/faults"
CRATE = ROOT / "crate" / "Cargo.toml"
"""

HEALTHY = {
    "mise.toml": MISE,
    ".github/workflows/ci.yml": CI,
    ".github/PULL_REQUEST_TEMPLATE.md": "## Guards\n\nName the `guards/faults/fs.toml` entry.\n",
    "conformance/run_rs.py": "from lanes.suite import run_suite\n",
    "conformance/harness/results.py": RESULTS,
    REGISTRY: FAULTS,
    "AGENTS.md": AGENTS,
    "CONTRIBUTING.md": CONTRIBUTING,
    "crate/Cargo.toml": "",
    "crate/src/lib.rs": "fn fs_pop_is_refused() {}\n",
    "lefthook.yml": LEFTHOOK,
    ".github/workflows/fuzz.yml": FUZZ,
    ".github/dependabot.yml": DEPENDABOT,
    "agentd/fuzz/Cargo.toml": "",
    "docs/decisions.toml": DECISIONS,
    "scripts/check-agents-md.py": GATE,
    # `src/lib.rs` is the crate's own, resolved from the doc's directory; the second path is
    # the repo's, resolved from the root.
    "crate/AGENTS.md": (
        "The crate docs in `src/lib.rs`. The registry is `guards/faults/fs.toml`.\n"
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
                    "`guards/faults/fs.toml`", "`guards/faults/f.toml`"
                )
            }
        )
        self.assert_fails_with(
            "AGENTS.md:10:", "`guards/faults/f.toml`", "no such path"
        )

    def test_a_dangling_crate_relative_path_fails(self):
        self.healthy(**{"crate/AGENTS.md": "The crate docs in `src/main.rs`.\n"})
        self.assert_fails_with("crate/AGENTS.md:1:", "`src/main.rs`")

    def test_a_path_deleted_from_the_tree_but_still_in_the_index_fails(self):
        self.healthy()
        (self.repo / REGISTRY).unlink()
        self.assert_fails_with(f"`{REGISTRY}`")

    def test_a_new_untracked_file_resolves(self):
        # An uncommitted worktree: the doc and the file it names are both new.
        self.healthy()
        (self.repo / "scripts").mkdir(exist_ok=True)
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
        without = RESULTS.replace("def absent(", "def missing(")
        self.healthy(**{"conformance/harness/results.py": without})
        self.assert_fails_with(
            "AGENTS.md:13:", "`Results.absent`", "conformance/harness/results.py"
        )

    def test_a_missing_results_eq_fails(self):
        without = RESULTS.replace("def eq(", "def equal(")
        self.healthy(**{"conformance/harness/results.py": without})
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
        self.healthy(**{REGISTRY: FAULTS.replace('"fs-pop"', '"fs-pop-2"')})
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
                REGISTRY: FAULTS.replace("fs_pop_is_refused", "fs_pop_refused"),
            }
        )
        self.assert_fails_with("CONTRIBUTING.md:2:", "`fs_pop_is_refused`")

    def test_a_name_spelled_only_in_prose_about_the_code_fails(self):
        # A docstring example, a comment and a seeded transform aren't the name existing.
        prose = {
            "crate/src/lib.rs": "// fs_pop_is_refused\nfn other() {}\n",
            "scripts/tool.py": '"""An example: fs_pop_is_refused."""\n# fs_pop_is_refused\n',
            REGISTRY: FAULTS.replace(
                'with = "fn fs_pop_seeded_name"', 'with = "fs_pop_is_refused"'
            ).replace("fs::tests::fs_pop_is_refused", "fs::tests::other"),
            "notes.md": "fs_pop_is_refused\n",
        }
        self.healthy(**prose)
        self.assert_fails_with("`fs_pop_is_refused` isn't spelled in any code")

    def test_a_name_in_a_python_string_or_a_registry_guard_resolves(self):
        cases = {
            "scripts/tool.py": 'TEST = "fs::tests::fs_pop_is_refused"\n',
            REGISTRY: FAULTS,
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
            "path": AGENTS.replace("`guards/faults/fs.toml`", "the registry").replace(
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
        self.healthy(**{REGISTRY: ""})
        self.assert_fails_with("found no fault ids in guards/faults/*.toml")

    def test_fault_ids_come_from_every_registry_file_through_the_shared_loader(self):
        # The id the docs name moves to another owner's file and still resolves; left in the
        # single file the registry used to be, which the loader doesn't read, it doesn't.
        other = FAULTS.replace('"fs-pop"', '"fs-other"')
        self.healthy(**{REGISTRY: other, "guards/faults/another.toml": FAULTS})
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        git(self.repo, "rm", "-q", "-f", "guards/faults/another.toml")
        self.write("guards/faults.toml", FAULTS)
        self.assert_fails_with("CONTRIBUTING.md:1:", "`fs-pop` is no fault id")

    def test_a_seeded_transform_in_any_registry_file_is_not_the_name_existing(self):
        # check-guards-fire.py's loader says which files are the registry, so a `transform` in
        # a second owner's file is prose here as it is in the first.
        seeded = (
            FAULTS.replace('"fs-pop"', '"fs-other"')
            .replace('with = "fn fs_pop_seeded_name"', 'with = "fs_pop_is_refused"')
            .replace("fs::tests::fs_pop_is_refused", "fs::tests::other")
        )
        self.healthy(
            **{
                "crate/src/lib.rs": "fn other() {}\n",
                REGISTRY: FAULTS.replace(
                    "fs::tests::fs_pop_is_refused", "fs::tests::other"
                ),
                "guards/faults/another.toml": seeded,
            }
        )
        self.assert_fails_with("`fs_pop_is_refused` isn't spelled in any code")

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
        self.healthy(**{"conformance/harness/results.py": "def main():\n    pass\n"})
        self.assert_fails_with("no class Results in conformance/harness/results.py")

    # ── the path census: hooks, tasks, workflows, dependabot, script constants ─

    def assert_passes(self) -> None:
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("every cited decision id is defined", result.stdout)

    def test_a_stale_lefthook_glob_alternative_fails_naming_its_line(self):
        # The move this guards: a hook's glob keeps a path that left, and the hook stops
        # running on it without a word.
        text = LEFTHOOK.replace("guards/faults/*.toml}", "verify/faults/*.toml}")
        self.healthy(**{"lefthook.yml": text})
        self.assert_fails_with(
            "lefthook.yml:9:", "`verify/faults/*.toml` matches no tracked file"
        )

    def test_a_lefthook_glob_is_matched_the_way_lefthook_matches_it(self):
        # lefthook's default matcher: `*` crosses `/`, and `**/` needs a directory, so
        # `crate/src/**/*.rs` misses `crate/src/lib.rs` (measured with lefthook 2.1.10).
        for glob, passes in (
            ("*.rs", True),
            ("crate/*.rs", True),
            ("CRATE/SRC/*.RS", True),
            ("crate/**/*.rs", True),
            ("crate/src/**/*.rs", False),
        ):
            with self.subTest(glob=glob):
                self.healthy(**{"lefthook.yml": LEFTHOOK.replace("*.rs,", f"{glob},")})
                if passes:
                    self.assert_passes()
                else:
                    self.assert_fails_with(f"`{glob}` matches no tracked file")

    def test_the_opt_in_doublestar_matcher_is_honored(self):
        # Under `glob_matcher: doublestar`, `*` stays in its directory and `**/` may be none.
        text = "glob_matcher: doublestar\n" + LEFTHOOK
        self.healthy(**{"lefthook.yml": text.replace("*.rs,", "crate/src/**/*.rs,")})
        self.assert_passes()
        self.healthy(**{"lefthook.yml": text.replace("*.rs,", "crate/*.rs,")})
        self.assert_fails_with("`crate/*.rs` matches no tracked file")

    def test_a_stale_path_in_a_lefthook_run_or_root_fails(self):
        cases = {
            "run": (
                LEFTHOOK.replace("src/lib.rs", "src/main.rs"),
                "lefthook.yml:8: `src/main.rs` (`crate/src/main.rs`) is no such path",
            ),
            "root": (
                LEFTHOOK.replace("root: crate/", "root: crates/crate/"),
                "lefthook.yml:7: `crates/crate/` (`crates/crate`) is no directory",
            ),
        }
        for key, (text, needle) in cases.items():
            with self.subTest(key=key):
                self.healthy(**{"lefthook.yml": text})
                self.assert_fails_with(needle)

    def remove(self, relative: str) -> None:
        git(self.repo, "rm", "-q", "--cached", "-f", "--ignore-unmatch", relative)
        (self.repo / relative).unlink(missing_ok=True)

    def test_a_lefthook_file_that_names_no_path_fails_naming_the_floor(self):
        self.healthy(**{"lefthook.yml": "pre-commit:\n  parallel: true\n"})
        self.assert_fails_with("the census read no paths from lefthook.yml")
        self.remove("lefthook.yml")
        self.assert_fails_with("the census read no paths from lefthook.yml")

    def test_a_lefthook_file_without_the_sentinel_fails_naming_it(self):
        text = LEFTHOOK.replace('".github/workflows/*.yml"', '".github/*/*.yml"')
        self.healthy(**{"lefthook.yml": text})
        self.assert_fails_with(
            "the census sentinel `.github/workflows/*.yml` isn't among the paths lefthook.yml"
        )

    def test_a_stale_script_in_a_mise_task_fails_naming_its_line(self):
        text = MISE.replace(
            "./scripts/check-agents-md.py", "./tools/check-agents-md.py"
        )
        self.healthy(**{"mise.toml": text})
        self.assert_fails_with(
            "mise.toml:17: `tools/check-agents-md.py` is no such path in this tree"
        )

    def test_a_mise_task_is_read_relative_to_its_dir(self):
        task = (
            '\n[tasks.fuzz]\ndir = "crate"\nsources = ["src/**/*.rs"]\n'
            'outputs = ["target/fuzz"]\nrun = [\n  "cargo build",\n  """\n'
            "set -e\n# src/gone.rs is only a comment\n"
            'cp src/lib.rs "$out/lib.rs"\n"""\n]\n'
        )
        self.healthy(**{"mise.toml": MISE + task, ".gitignore": "target/\n"})
        self.assert_passes()
        cases = {
            "run": (
                task.replace("cp src/lib.rs", "cp lib.rs src/main.rs"),
                "mise.toml:28: `src/main.rs` (`crate/src/main.rs`) is no such path",
            ),
            "dir": (
                task.replace('dir = "crate"', 'dir = "crates/crate"'),
                "mise.toml:20: `crates/crate` is no directory",
            ),
            "sources": (
                task.replace('"src/**/*.rs"', '"lib/**/*.rs"'),
                "`lib/**/*.rs` (`crate/lib/**/*.rs`) matches no tracked file",
            ),
        }
        for key, (text, needle) in cases.items():
            with self.subTest(key=key):
                self.healthy(**{"mise.toml": MISE + text})
                self.assert_fails_with(needle)

    def test_a_gitignored_output_passes_and_an_expansion_is_read_up_to_its_directory(
        self,
    ):
        task = (
            '\n[tasks.sbom]\nrun = "syft . -o spdx-json=sbom/tree.spdx.json'
            " --exclude './target/**' target/$AGENTD_TARGET/agentd {{arg(name=x)}}\"\n"
        )
        self.healthy(**{"mise.toml": MISE + task, ".gitignore": "target/\nsbom/\n"})
        self.assert_passes()
        self.healthy(**{"mise.toml": MISE + task, ".gitignore": "target/\n"})
        self.assert_fails_with("`sbom/tree.spdx.json` is no such path in this tree")

    def test_an_included_task_file_is_read_and_a_directory_include_is_refused(self):
        included = '[fuzz]\nrun = "cargo fuzz run crate/src/lib.rs"\n'
        config = '[task_config]\nincludes = ["tasks/fuzz.toml"]\n\n'
        self.healthy(**{"mise.toml": config + MISE, "tasks/fuzz.toml": included})
        with (self.repo / "CONTRIBUTING.md").open("a", encoding="utf-8") as doc:
            doc.write("Run `mise run fuzz` for the fuzzer.\n")
        self.assert_passes()
        stale = included.replace("crate/src/lib.rs", "crate/src/fz.rs")
        self.healthy(**{"mise.toml": config + MISE, "tasks/fuzz.toml": stale})
        self.assert_fails_with("tasks/fuzz.toml:2: `crate/src/fz.rs` is no such path")
        refused = {
            "a directory": ('includes = ["tasks"]', "is a directory of file tasks"),
            "a missing file": (
                'includes = ["tasks/gone.toml"]',
                "`tasks/gone.toml` is no such path",
            ),
            "a remote": (
                'includes = ["git::https://x/y.git//t"]',
                "isn't a file in this tree",
            ),
        }
        for case, (line, needle) in refused.items():
            with self.subTest(case=case):
                text = config.replace('includes = ["tasks/fuzz.toml"]', line) + MISE
                self.healthy(**{"mise.toml": text})
                self.assert_fails_with(needle)

    def test_a_default_file_task_directory_is_refused(self):
        self.healthy(**{".mise/tasks/fuzz": "#!/bin/sh\n"})
        self.assert_fails_with("mise reads file tasks from `.mise/tasks/`")

    def test_a_mise_toml_whose_tasks_name_no_path_fails_naming_the_floor(self):
        text = MISE.replace('run = "./scripts/check-agents-md.py"', 'run = "true"')
        self.healthy(**{"mise.toml": text})
        self.assert_fails_with("the census read no paths from mise.toml")

    def test_a_stale_workflow_paths_filter_fails_under_githubs_syntax(self):
        # A filter's `*` stays inside one directory, where a hook's crosses them.
        cases = {
            "'crate/*.rs'": False,
            "'crate/**'": True,
            "'crate/*/lib.rs'": True,
            "'!crate/src/*.rs'": True,
            "'!crate/lib/**'": False,
        }
        for glob, passes in cases.items():
            with self.subTest(glob=glob):
                text = FUZZ.replace("'crate/src/**'", glob)
                self.healthy(**{".github/workflows/fuzz.yml": text})
                if passes:
                    self.assert_passes()
                else:
                    self.assert_fails_with(
                        ".github/workflows/fuzz.yml:5:", "matches no tracked file"
                    )

    def test_a_stale_working_directory_fails_and_a_run_resolves_against_a_good_one(
        self,
    ):
        self.healthy(
            **{
                ".github/workflows/fuzz.yml": FUZZ.replace(
                    "working-directory: crate", "working-directory: crates/crate"
                )
            }
        )
        self.assert_fails_with(
            ".github/workflows/fuzz.yml:12: `crates/crate` is no directory in this tree",
            "`src/lib.rs` (`crates/crate/src/lib.rs`) is no such path",
        )
        text = FUZZ.replace(
            "--fuzz-dir src/lib.rs", "--fuzz-dir lib.rs ../scripts/x.py"
        )
        self.healthy(**{".github/workflows/fuzz.yml": text})
        self.assert_fails_with("`../scripts/x.py` (`scripts/x.py`) is no such path")

    def test_a_stale_script_in_a_workflow_step_or_input_fails(self):
        cases = {
            "run": (
                CI.replace(
                    "./scripts/check-agents-md.py", "./tools/check-agents-md.py"
                ),
                "ci.yml:8: `tools/check-agents-md.py`",
            ),
            "with": (
                CI + "  upload:\n    runs-on: ubuntu-latest\n    steps:\n"
                "      - uses: a/b@v1\n        with:\n"
                "          files: crate/src/lib.rs,crate/lib.rs\n",
                "ci.yml:27: `crate/lib.rs` is no such path",
            ),
        }
        for key, (text, needle) in cases.items():
            with self.subTest(key=key):
                self.healthy(**{".github/workflows/ci.yml": text})
                self.assert_fails_with(needle)

    def test_workflows_that_name_no_path_or_not_the_sentinel_fail_naming_it(self):
        self.healthy(
            **{
                ".github/workflows/ci.yml": CI.replace(
                    "./scripts/check-agents-md.py", "true"
                )
            }
        )
        self.assert_fails_with(
            "the census sentinel `scripts/check-agents-md.py` isn't among the paths"
            " .github/workflows/*.yml names"
        )
        self.remove(".github/workflows/fuzz.yml")
        self.assert_fails_with("the census read no paths from .github/workflows/*.yml")

    def test_a_stale_dependabot_directory_fails_and_the_root_passes(self):
        text = DEPENDABOT.replace("/agentd/fuzz", "/crates/agentd/fuzz")
        self.healthy(**{".github/dependabot.yml": text})
        self.assert_fails_with(
            ".github/dependabot.yml:6: `/crates/agentd/fuzz` (`crates/agentd/fuzz`) is no"
            " directory"
        )

    def test_a_dependabot_file_with_no_directory_fails_naming_the_floor(self):
        self.healthy(**{".github/dependabot.yml": "version: 2\nupdates: []\n"})
        self.assert_fails_with("the census read no paths from .github/dependabot.yml")

    def test_a_stale_path_constant_fails_naming_its_line(self):
        cases = {
            "a string": (
                GATE.replace('"guards/faults"', '"verify/faults"'),
                "scripts/check-agents-md.py:4: `verify/faults` is no such path",
            ),
            "a join": (
                GATE.replace('ROOT / "crate"', 'ROOT / "crates"'),
                "scripts/check-agents-md.py:5: `crates/Cargo.toml` is no such path",
            ),
            "a file name": (
                GATE + 'LOCK = "Cargo.lok"\n',
                "scripts/check-agents-md.py:6: `Cargo.lok` is no file's name",
            ),
            "inside a call": (
                GATE + 'SUITE = run(str(Path(__file__).with_name("gone.py")))\n',
                "`scripts/gone.py` is no such path",
            ),
            "in a tuple": (
                GATE + 'PAIRS = (("crate/tests", "*.rs"),)\n',
                "`crate/tests` is no such path",
            ),
        }
        for case, (text, needle) in cases.items():
            with self.subTest(case=case):
                self.healthy(**{"scripts/check-agents-md.py": text})
                self.assert_fails_with(needle)

    def test_what_the_constant_rule_leaves_alone_passes(self):
        # A test module's fixture paths, a dict's keys, a lower-case name, a pattern.
        self.healthy(
            **{
                "scripts/test_gate.py": 'CASE = "crate/tests/case.rs"\n',
                "scripts/check-agents-md.py": GATE
                + 'RELEASES = {"owner/repo": "v1"}\nhelper = "crate/gone.rs"\n'
                'SHARD = re.compile("([0-9]+)/([0-9]+)")\nURL = "https://x.io/a/b"\n',
            }
        )
        self.assert_passes()

    def test_scripts_with_no_path_constant_fail_naming_the_floor(self):
        self.healthy(**{"scripts/check-agents-md.py": "X = 1\n"})
        self.assert_fails_with("the census read no paths from scripts/*.py")

    def test_a_census_exception_no_source_names_fails(self):
        first = next(iter(CHECK.CENSUS_NOT_PATHS))
        text = FUZZ.replace(f" {first} ", " ").replace(f"echo {first} ", "echo ")
        self.healthy(**{".github/workflows/fuzz.yml": text})
        self.assert_fails_with(f"CENSUS_NOT_PATHS lists `{first}`")

    def test_a_yaml_construct_the_reader_doesnt_take_fails_naming_its_line(self):
        self.healthy(
            **{"lefthook.yml": LEFTHOOK.replace("pre-commit:", "pre-commit: &pc")}
        )
        self.assert_fails_with("lefthook.yml:1: a value that starts with `&`")

    def test_an_undefined_decision_id_fails_naming_its_line(self):
        cited = CONTRIBUTING + f"CI shards the guards job ({decision(7)}).\n"
        self.healthy(**{"CONTRIBUTING.md": cited})
        self.assert_fails_with(
            f"CONTRIBUTING.md:4: `{decision(7)}` is a decision id docs/decisions.toml doesn't"
        )
        defined = DECISIONS + f'\n[{decision(7)}]\ndecision = "x"\nsource = "#1"\n'
        self.healthy(**{"docs/decisions.toml": defined})
        self.assert_passes()

    def test_a_hex_color_a_path_or_a_lockfile_isnt_a_decision_citation(self):
        self.healthy(
            **{
                "README.md": f"![badge](https://x.io/badge/a-{decision(97757)}?logo=y)\n",
                "notes/{0}.txt".format(decision(4)): "",
                "site/pnpm-lock.yaml": f"integrity: sha512-a/{decision(12)}+b==\n",
            }
        )
        self.assert_passes()

    def test_a_malformed_or_empty_decision_register_fails(self):
        cases = {
            "": "docs/decisions.toml defines no decision",
            DECISIONS.replace('source = "#346."\n', ""): "`D35` has no source",
            DECISIONS
            + '\n[shards]\ndecision = "x"\nsource = "y"\n': "`shards` isn't a decision table",
        }
        for text, needle in cases.items():
            with self.subTest(needle=needle):
                self.healthy(**{"docs/decisions.toml": text})
                self.assert_fails_with(needle)

    def test_a_tree_that_cites_no_decision_or_not_the_sentinel_fails(self):
        self.healthy(**{".github/workflows/ci.yml": CI.replace(" (D35)", "")})
        self.assert_fails_with("found no decision id cited in the tree")
        cited = CI.replace(" (D35)", f" ({decision(7)})")
        defined = DECISIONS + f'\n[{decision(7)}]\ndecision = "x"\nsource = "#1"\n'
        self.healthy(
            **{".github/workflows/ci.yml": cited, "docs/decisions.toml": defined}
        )
        self.assert_fails_with("the sentinel decision `D35` isn't cited anywhere")

    def test_an_inherited_git_dir_leaves_that_repo_alone(self):
        # The hook case: `GIT_DIR` names another repo while a case runs. The decoy's index
        # must stay empty, and the case must still pass on its own throwaway repo.
        with tempfile.TemporaryDirectory() as decoy:
            git(Path(decoy), "init", "-q")
            gitdir = str(Path(decoy) / ".git")
            with mock.patch.dict(os.environ, {"GIT_DIR": gitdir}):
                self.test_a_tree_whose_references_all_resolve_passes()
            self.assertEqual(git(Path(decoy), "ls-files"), "")


class YamlReaderTests(unittest.TestCase):
    """The census's YAML reader gives what a YAML parser gives for the constructs it takes.

    Checked against PyYAML 6.0.3 over every tracked YAML file when it was written; these
    cases pin each construct, since the suite runs without PyYAML.
    """

    def load(self, text: str) -> object:
        return CHECK.load_yaml(textwrap.dedent(text), "t.yml")

    def test_mappings_sequences_and_compact_nesting(self):
        text = """\
            on:
              push:
                branches: [main, 'release/*']
            jobs:
              a:
                steps:
                  - uses: x/y@v1 # a comment
                    with: { path: 'a b', n: 1 }
                  -
                    run: echo hi
            list:
            - one
            - - two
              - three
            """
        self.assertEqual(
            self.load(text),
            {
                "on": {"push": {"branches": ["main", "release/*"]}},
                "jobs": {
                    "a": {
                        "steps": [
                            {"uses": "x/y@v1", "with": {"path": "a b", "n": "1"}},
                            {"run": "echo hi"},
                        ]
                    }
                },
                "list": ["one", ["two", "three"]],
            },
        )

    def test_scalars_quoted_block_and_multi_line(self):
        text = """\
            single: 'it''s # not a comment'
            double: "a\\tb \\"c\\""
            plain: one
              two  # comment
            literal: |
              line one
                indented
            
              after a blank
            folded: >-
              a
              b
            keep: |+
              x

            last: end
            """
        loaded = self.load(text)
        self.assertEqual(loaded["single"], "it's # not a comment")
        self.assertEqual(loaded["double"], 'a\tb "c"')
        self.assertEqual(loaded["plain"], "one two")
        self.assertEqual(loaded["literal"], "line one\n  indented\n\nafter a blank\n")
        self.assertEqual(loaded["folded"], "a b")
        self.assertEqual(loaded["keep"], "x\n\n")
        self.assertEqual(loaded["last"], "end")

    def test_a_scalar_keeps_its_line(self):
        loaded = self.load("a:\n  b: x\n  run: |\n    first\n")
        self.assertEqual((loaded["a"]["b"].line, loaded["a"]["run"].line), (2, 4))

    def test_constructs_it_doesnt_take_raise_naming_the_line(self):
        cases = {
            "a: &x 1\n": "t.yml:1: a value that starts with `&`",
            "a: 1\nb: *x\n": "t.yml:2: a value that starts with `*`",
            "a: !!str 1\n": "t.yml:1: a value that starts with `!`",
            "a:\n\tb: 1\n": "t.yml:2: a tab in the indentation",
            "a: 1\na: 2\n": "t.yml:2: a second `a`",
            "a: 'open\n": "t.yml:1: a quoted scalar that never closes",
            "a:\n    b: 1\n  c: 2\n": "a line indented past",
        }
        for text, needle in cases.items():
            with self.subTest(text=text):
                with self.assertRaises(CHECK.YamlSubsetError) as caught:
                    CHECK.load_yaml(text, "t.yml")
                self.assertIn(needle, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
