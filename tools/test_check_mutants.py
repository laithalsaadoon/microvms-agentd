# SPDX-License-Identifier: Apache-2.0
"""The mutants wrapper passes only a clean run and a change with no Rust in it (#275).

Each case runs the real script in a throwaway git repo with a base commit and a change on top,
and hands it a fake `cargo` that answers `cargo mutants` with one of cargo-mutants' exit codes
and the `mutants.out` files a real run writes for it, and `cargo metadata` with a workspace's
members. The fake records its argv, so a case can also say what the wrapper asked for, or that
it never ran cargo-mutants at all. A diff handed in with `--diff` fails unless it names a Rust
file. The CI job's own steps run over the same repo, read from ci.yml.

The last cases read this tree: every `mutants::skip` is a listed site, and every exclusion in
`.cargo/mutants.toml` drops only a named function's return-value mutants.
"""

import json
import os
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from collections import Counter
from pathlib import Path

import yaml

SCRIPT = Path(__file__).with_name("check-mutants.py")
ROOT = SCRIPT.parent.parent

# The workspace's members, as `cargo metadata` names them: the wrapper's `PACKAGES` and the
# ones it leaves out.
MEMBERS = [
    "agentd",
    "agentd-model",
    "microvms-app",
    "microvms-cli",
    "microvms-core",
    "microvms-domain",
    "microvms-edges",
    "microvms-js",
    "microvms-protocol",
    "microvms-py",
    "model-conformance",
]

# Every `mutants::skip` in the tree, by file. The attribute drops every mutant under it with no
# run and no trace in the job's output, so a new one is a reviewed change here, the way
# test_ratchet.py's LINT_EXCEPTIONS holds the `#[expect]` sites.
SKIPS = {
    # The app's in-module test doubles, each a `mod testing` behind
    # `cfg(any(test, feature = "test-support"))`. A mutant in a fake says nothing about
    # shipping code; .cargo/mutants.toml says why these aren't `exclude_re` entries.
    "crates/microvms-app/src/adapters.rs": 1,
    "crates/microvms-app/src/clock.rs": 1,
    "crates/microvms-app/src/entropy.rs": 1,
    "crates/microvms-app/src/session/mod.rs": 1,
    "crates/microvms-app/src/session/proxy.rs": 1,
    # The `cfg(not(unix))` twins of `mode_of` and `unix_signal`, which the Linux job never
    # compiles. A name regex would drop the tested Unix twin's mutants too.
    "crates/microvms-edges/src/control/context.rs": 1,
    "crates/agentd/src/exec.rs": 1,
}

# The one shape an `exclude_re` entry may take: anchored to a file, any line and column, and
# only the `replace <function> -> ` mutants, which swap the whole return value. An operator
# mutant is named `... in <function>`, so logic added to an excluded function is still mutated.
# The name is literal apart from one trailing group of alternatives.
EXCLUSION = re.compile(
    r"\^(?P<path>(?:[\w/-]|\\\.)+):\\d\+:\\d\+: replace [\w<>: ]+(?:\([\w|]+\))? -> "
)

# The pointers a git hook exports, copied from test_license_headers.py (scripts aren't
# importable). Inherited from lefthook, they'd turn the throwaway repo's `git init` and
# `git commit` into writes to the real one.
GIT_ENV_LEAKS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_CEILING_DIRECTORIES",
)

MISSED = "crates/microvms-app/src/lib.rs:3:5: replace answer -> u32 with 0"
TIMED_OUT = "crates/microvms-app/src/lib.rs:3:5: replace answer -> u32 with 1"

# What cargo-mutants 27.1.0 writes under `mutants.out/` and prints last for each exit code it
# has (src/exit_code.rs). Only the files the wrapper reads are here.
FIXTURES: dict[int, tuple[dict[str, str], str]] = {
    0: ({"caught.txt": MISSED + "\n", "missed.txt": ""}, "2 mutants tested: 2 caught"),
    2: (
        {"caught.txt": "", "missed.txt": MISSED + "\n"},
        "2 mutants tested: 1 missed, 1 caught",
    ),
    3: (
        {"missed.txt": "", "timeout.txt": TIMED_OUT + "\n"},
        "2 mutants tested: 1 timeouts",
    ),
    4: ({}, "FAILED   Unmutated baseline in 3s build + 1s test"),
    5: (
        {},
        "Error: Diff content doesn't match source file: crates/microvms-app/src/lib.rs",
    ),
    6: ({}, "Error: Failed to parse diff: expected a hunk header"),
}

# A stand-in for `cargo`: `cargo metadata` prints a workspace whose members are $FAKE_MEMBERS,
# and `cargo mutants ...` writes the fixture for $FAKE_EXIT into ./mutants.out, prints its
# summary line, records argv in $FAKE_ARGV and exits $FAKE_EXIT.
FAKE_CARGO = """\
import json, os, sys
from pathlib import Path
if sys.argv[1:2] == ["metadata"]:
    members = json.loads(os.environ.get("FAKE_MEMBERS", "null"))
    if members is None:  # a metadata with neither key
        print("{}")
        sys.exit(0)
    ids = [f"path+file:///w/{name}#0.1.0" for name in members]
    print(json.dumps({
        "packages": [{"name": n, "id": i} for n, i in zip(members, ids)],
        "workspace_members": ids,
    }))
    sys.exit(0)
fixtures = json.loads(os.environ["FAKE_FIXTURES"])
code = int(os.environ["FAKE_EXIT"])
Path(os.environ["FAKE_ARGV"]).write_text(json.dumps(sys.argv[1:]))
Path(os.environ["FAKE_ARGV"] + ".target").write_text(
    json.dumps(os.environ.get("CARGO_TARGET_DIR"))
)
if "--in-diff" in sys.argv:
    diff = sys.argv[sys.argv.index("--in-diff") + 1]
    Path(os.environ["FAKE_ARGV"] + ".diff").write_text(Path(diff).read_text())
files, summary = fixtures.get(str(code), ({}, "cargo-mutants failed"))
out = Path("mutants.out")
out.mkdir(exist_ok=True)
for name, text in files.items():
    (out / name).write_text(text)
print(summary)
sys.exit(code)
"""


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


class WrapperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="check-mutants-"))
        self.addCleanup(
            lambda: subprocess.run(["rm", "-rf", str(self.tmp)], check=False)
        )
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "test")
        git(self.repo, "config", "commit.gpgsign", "false")
        (self.repo / "crates/microvms-app/src").mkdir(parents=True)
        (self.repo / "crates/microvms-app/src/lib.rs").write_text(
            "pub fn answer() -> u32 {\n    41\n}\n"
        )
        (self.repo / "README.md").write_text("# fixture\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        git(self.repo, "branch", "base")
        self.cargo = self.tmp / "cargo"
        self.cargo.write_text(f"#!{sys.executable}\n{FAKE_CARGO}")
        self.cargo.chmod(0o755)
        self.argv_file = self.tmp / "argv.json"

    def change_rust(self) -> None:
        (self.repo / "crates/microvms-app/src/lib.rs").write_text(
            "pub fn answer() -> u32 {\n    42\n}\n"
        )
        (self.repo / "README.md").write_text("# fixture, changed\n")
        git(self.repo, "commit", "-qam", "change the answer")

    def change_docs_only(self) -> None:
        (self.repo / "README.md").write_text("# fixture, changed\n")
        git(self.repo, "commit", "-qam", "docs only")

    def run_wrapper(
        self,
        fake_exit: int,
        *extra: str,
        source: tuple[str, ...] = ("--base", "base"),
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = (
            clean_env()
            | {
                "FAKE_EXIT": str(fake_exit),
                "FAKE_ARGV": str(self.argv_file),
                "FAKE_FIXTURES": json.dumps({str(k): v for k, v in FIXTURES.items()}),
                "FAKE_MEMBERS": json.dumps(MEMBERS),
            }
            | (env or {})
        )
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                *source,
                "--cargo",
                str(self.cargo),
                *extra,
            ],
            cwd=self.repo,
            capture_output=True,
            text=True,
            env=env,
        )

    def cargo_mutants_argv(self) -> list[str]:
        return json.loads(self.argv_file.read_text())

    def assert_fails(
        self, result: subprocess.CompletedProcess[str], *said: str
    ) -> None:
        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, output)
        for text in said:
            self.assertIn(text, output)

    # ── the two ways to pass ────────────────────────────────────────────────────────────

    def test_a_clean_run_passes(self) -> None:
        self.change_rust()
        result = self.run_wrapper(0)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("2 mutants tested: 2 caught", result.stdout)

    def test_a_change_with_no_rust_passes_without_running_cargo_mutants(self) -> None:
        self.change_docs_only()
        result = self.run_wrapper(2)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("no Rust changes", result.stdout)
        self.assertFalse(
            self.argv_file.exists(), "cargo-mutants ran on a docs-only change"
        )

    # ── every other exit code fails ──────────────────────────────────────────────────────

    def test_a_missed_mutant_fails_and_names_it(self) -> None:
        self.change_rust()
        self.assert_fails(self.run_wrapper(2), "MISSED", MISSED)

    def test_a_timeout_fails_and_names_the_mutant(self) -> None:
        self.change_rust()
        self.assert_fails(self.run_wrapper(3), "TIMEOUT", TIMED_OUT)

    def test_a_failed_baseline_fails(self) -> None:
        self.change_rust()
        self.assert_fails(self.run_wrapper(4), "baseline")

    def test_a_diff_that_does_not_match_the_tree_fails_and_says_to_rebase(self) -> None:
        self.change_rust()
        self.assert_fails(self.run_wrapper(5), "rebase")

    def test_a_diff_that_does_not_parse_fails(self) -> None:
        self.change_rust()
        self.assert_fails(self.run_wrapper(6), "doesn't parse")

    def test_an_exit_code_it_has_no_name_for_fails(self) -> None:
        self.change_rust()
        for code in (1, 70):
            with self.subTest(code=code):
                self.assert_fails(self.run_wrapper(code), f"exit {code}")

    # ── what it hands cargo-mutants ──────────────────────────────────────────────────────

    def test_it_hands_over_the_rust_diff_the_packages_and_the_extra_arguments(
        self,
    ) -> None:
        self.change_rust()
        result = self.run_wrapper(0, "--", "--shard", "1/4")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        argv = self.cargo_mutants_argv()
        self.assertEqual(argv[:2], ["mutants", "--in-diff"])
        self.assertIn("--no-shuffle", argv)
        self.assertEqual(argv[-2:], ["--shard", "1/4"])
        packages = [argv[i + 1] for i, arg in enumerate(argv) if arg == "-p"]
        self.assertEqual(
            sorted(packages),
            sorted(
                [
                    "agentd",
                    "microvms-app",
                    "microvms-cli",
                    "microvms-core",
                    "microvms-domain",
                    "microvms-edges",
                    "microvms-protocol",
                ]
            ),
        )
        diff = Path(str(self.argv_file) + ".diff").read_text()
        self.assertIn("+    42", diff)
        self.assertNotIn("README.md", diff)

    def cargo_target_dir(self) -> str | None:
        return json.loads(Path(str(self.argv_file) + ".target").read_text())

    def test_cargo_mutants_builds_in_its_own_targets_unless_it_mutates_in_place(
        self,
    ) -> None:
        # Each copy cargo-mutants builds shares the caller's workspace-relative paths, so in
        # a shared target a later build of the caller's tree can trust an artifact built from
        # a mutated copy. In place there's one tree, and its own target is the one to reuse.
        self.change_rust()
        target = str(self.tmp / "shared-target")
        result = self.run_wrapper(0, env={"CARGO_TARGET_DIR": target})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIsNone(self.cargo_target_dir())
        self.assertIn("CARGO_TARGET_DIR", result.stdout)
        result = self.run_wrapper(
            0, "--", "--in-place", env={"CARGO_TARGET_DIR": target}
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.cargo_target_dir(), target)

    # ── a diff handed in ─────────────────────────────────────────────────────────────────

    def test_a_given_diff_is_handed_over_as_it_is(self) -> None:
        diff = self.tmp / "given.diff"
        diff.write_text(
            "--- a/crates/microvms-app/src/lib.rs\n+++ b/crates/microvms-app/src/lib.rs\n"
            "@@ -1,0 +2,1 @@\n+    41\n"
        )
        result = self.run_wrapper(0, source=("--diff", str(diff)))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.cargo_mutants_argv()[:2], ["mutants", "--in-diff"])
        self.assertEqual(
            Path(str(self.argv_file) + ".diff").read_text(), diff.read_text()
        )

    def test_a_given_diff_with_no_rust_in_it_fails_without_running_cargo_mutants(
        self,
    ) -> None:
        # Asked to mutate a diff, cargo-mutants passes one that names no Rust file, so an
        # empty file or a generator that found nothing would read as a clean run.
        cases = {
            "empty": "",
            "docs only": "--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-# a\n+# b\n",
        }
        for name, text in cases.items():
            with self.subTest(name):
                diff = self.tmp / f"{name}.diff"
                diff.write_text(text)
                result = self.run_wrapper(0, source=("--diff", str(diff)))
                self.assert_fails(result, "names no Rust file")
                self.assertFalse(self.argv_file.exists())
        result = self.run_wrapper(0, source=("--diff", str(self.tmp / "missing.diff")))
        self.assert_fails(result, "missing.diff")
        self.assertFalse(self.argv_file.exists())

    def test_a_base_git_cannot_resolve_fails_rather_than_reading_as_no_changes(
        self,
    ) -> None:
        # A shallow checkout with no base ref is the case: `git diff` failing must not look
        # like an empty diff, which would pass every PR without mutating anything.
        self.change_rust()
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--base",
                "no-such-ref",
                "--cargo",
                str(self.cargo),
            ],
            cwd=self.repo,
            capture_output=True,
            text=True,
            env=clean_env() | {"FAKE_EXIT": "0", "FAKE_ARGV": str(self.argv_file)},
        )
        self.assert_fails(result, "no-such-ref")
        self.assertFalse(self.argv_file.exists())

    # ── a user's git settings ────────────────────────────────────────────────────────────

    def test_the_diff_keeps_its_a_and_b_prefixes_whatever_git_is_set_to(self) -> None:
        # cargo-mutants reads a file's path off `+++ b/<path>`. With `diff.mnemonicPrefix`
        # the header is `+++ w/<path>`, which names no file in the tree, and a real run then
        # reports `No mutants to filter` and passes.
        self.change_rust()
        settings = {
            "mnemonicPrefix": ("diff.mnemonicPrefix", "true"),
            "noprefix": ("diff.noprefix", "true"),
            "color": ("color.diff", "always"),
        }
        for name, (key, value) in settings.items():
            with self.subTest(name):
                result = self.run_wrapper(
                    0,
                    env={
                        "GIT_CONFIG_COUNT": "1",
                        "GIT_CONFIG_KEY_0": key,
                        "GIT_CONFIG_VALUE_0": value,
                    },
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                diff = Path(str(self.argv_file) + ".diff").read_text()
                self.assertIn("--- a/crates/microvms-app/src/lib.rs\n", diff)
                self.assertIn("+++ b/crates/microvms-app/src/lib.rs\n", diff)
                self.assertNotIn("\x1b[", diff)

    # ── the workspace against the package list ───────────────────────────────────────────

    def test_a_workspace_member_in_neither_list_fails_naming_it(self) -> None:
        # A new crate isn't mutated until someone says whether its tests can catch a mutant.
        self.change_rust()
        result = self.run_wrapper(
            0, env={"FAKE_MEMBERS": json.dumps([*MEMBERS, "microvms-extra"])}
        )
        self.assert_fails(result, "microvms-extra")
        self.assertFalse(self.argv_file.exists(), "cargo-mutants ran")

    def test_a_listed_package_that_is_no_member_fails_naming_it(self) -> None:
        # A renamed crate: `-p <old name>` only warns, and the crate drops out of the job.
        self.change_rust()
        for gone in ("microvms-cli", "agentd-model"):
            with self.subTest(gone):
                members = [name for name in MEMBERS if name != gone]
                result = self.run_wrapper(0, env={"FAKE_MEMBERS": json.dumps(members)})
                self.assert_fails(result, gone)
                self.assertFalse(self.argv_file.exists(), "cargo-mutants ran")

    def test_a_workspace_with_no_members_fails(self) -> None:
        # `cargo metadata` answering nothing isn't a workspace the list matches.
        self.change_rust()
        for members in ([], None):
            with self.subTest(members=members):
                result = self.run_wrapper(0, env={"FAKE_MEMBERS": json.dumps(members)})
                self.assert_fails(result, "workspace")
                self.assertFalse(self.argv_file.exists(), "cargo-mutants ran")

    # ── `--detect`, and the CI job's steps as ci.yml writes them ─────────────────────────

    def test_detect_answers_whether_there_is_rust_without_running_cargo(self) -> None:
        self.change_docs_only()
        result = self.run_wrapper(0, "--detect")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "rust=false\n")
        self.change_rust()
        result = self.run_wrapper(0, "--detect")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "rust=true\n")
        self.assertIn("crates/microvms-app/src/lib.rs", result.stderr)
        self.assertFalse(self.argv_file.exists(), "--detect ran cargo")
        result = self.run_wrapper(0, "--detect", source=("--base", "no-such-ref"))
        self.assert_fails(result, "no-such-ref")

    def run_step(
        self, step: dict, env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        """A step's `run:` as the runner runs it, in the fixture repo."""
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
            cwd=self.repo,
            capture_output=True,
            text=True,
            env=clean_env() | env,
        )

    def test_the_ci_jobs_steps_run_the_wrapper_as_these_cases_do(self) -> None:
        # The job's first step decides whether any other step runs, so a step that always
        # answers `false` turns every shard into a green run that mutated nothing.
        steps = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"][
            "mutants"
        ]["steps"]
        ids = [step.get("id") for step in steps]
        self.assertIn("diff", ids, "the mutants job has no step with id `diff`")
        detect = steps[ids.index("diff")]
        gated = steps[ids.index("diff") + 1 :]
        for step in gated:
            with self.subTest(step=step.get("name") or step.get("uses")):
                self.assertIn("steps.diff.outputs.rust == 'true'", step.get("if", ""))
        (mutate,) = [s for s in gated if "mise run ci:mutants" in s.get("run", "")]
        # The step runs `mise run ci:mutants <arguments>`: the task's command with the step's
        # arguments after it, as mise runs it (test_check_ci_parity.py holds mise to that).
        tasks = runpy.run_path(str(ROOT / "tools/check-ci-parity.py"))["load_tasks"](
            ROOT
        )
        task = tasks["ci:mutants"]
        self.assertIsInstance(task.get("run"), str, "`ci:mutants` isn't one command")
        args = mutate["run"].split("mise run ci:mutants", 1)[1]
        mutate = {"run": task["run"] + args}

        # The steps run the checkout's own scripts; a copy stands in, untracked, so it
        # isn't in the diff. `python3` and `cargo` are this interpreter and the fake.
        (self.repo / "tools").mkdir()
        shutil.copy(SCRIPT, self.repo / "tools" / SCRIPT.name)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "python3").symlink_to(sys.executable)
        (bin_dir / "cargo").symlink_to(self.cargo)
        output = self.tmp / "github-output"
        env = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "BASE": "base",
            "SHARD": "1/4",
            "GITHUB_OUTPUT": str(output),
            "FAKE_EXIT": "0",
            "FAKE_ARGV": str(self.argv_file),
            "FAKE_FIXTURES": json.dumps({str(k): v for k, v in FIXTURES.items()}),
            "FAKE_MEMBERS": json.dumps(MEMBERS),
        }
        for change, answer in (
            (self.change_docs_only, "false"),
            (self.change_rust, "true"),
        ):
            with self.subTest(rust=answer):
                change()
                output.write_text("")
                result = self.run_step(detect, env)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(output.read_text(), f"rust={answer}\n")
        self.assertFalse(self.argv_file.exists(), "the detect step ran cargo")

        result = self.run_step(mutate, env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        argv = self.cargo_mutants_argv()
        self.assertEqual(argv[:2], ["mutants", "--in-diff"])
        self.assertEqual(argv[-3:], ["--shard", "1/4", "--in-place"])
        self.assertIn("+    42", Path(str(self.argv_file) + ".diff").read_text())


# The check main's ruleset requires of the `mutants` job by one name: the shards report under
# their own names, and one job reports their combined result under this one (#279).
REQUIRED = "mutation testing"


class ResultJobTests(unittest.TestCase):
    """The `mutants` shards' combined result is one job, `guards-result`'s twin: it waits for
    every shard, runs wherever they run even when one fails, and passes only on `success`."""

    def jobs(self) -> dict:
        return yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]

    def aggregator(self) -> dict:
        jobs = self.jobs()
        named = [j for j, body in jobs.items() if body.get("name") == REQUIRED]
        self.assertEqual(
            len(named),
            1,
            f"{len(named)} jobs carry the required check's name `{REQUIRED}`, not one",
        )
        self.assertNotEqual(named[0], "mutants", "the required check is one shard")
        return jobs[named[0]]

    def test_the_required_check_is_the_aggregator(self):
        job = self.aggregator()
        needs = job.get("needs")
        self.assertIn(
            "mutants",
            [needs] if isinstance(needs, str) else list(needs or []),
            "the aggregator doesn't wait for the shards",
        )
        # Without `always()` it's skipped when a shard fails, and a skipped required check
        # counts as passing. The rest of its condition is the shards' own: run where they don't
        # (a push to main) and it reads them `skipped` and fails there.
        condition = str(job.get("if")).removeprefix("${{").removesuffix("}}").strip()
        head, _, rest = condition.partition(" && ")
        self.assertEqual(
            head, "always()", "the aggregator doesn't run when a shard fails"
        )
        self.assertEqual(
            rest,
            str(self.jobs()["mutants"].get("if") or ""),
            "the aggregator runs where the shards don't",
        )

    def test_the_aggregator_passes_only_when_every_shard_passed(self):
        # A timed-out shard reports `cancelled`, not `failure`, so only `success` passes.
        steps = [s for s in self.aggregator().get("steps") or [] if "run" in s]
        self.assertEqual(len(steps), 1, "the aggregator runs one step")
        (step,) = steps
        expression = re.compile(r"\$\{\{\s*(.+?)\s*\}\}")
        for result in ("success", "failure", "cancelled", "skipped"):
            with self.subTest(result=result):

                def value(match: re.Match) -> str:
                    self.assertEqual(
                        match.group(1),
                        "needs.mutants.result",
                        f"unmodeled `${{{{ {match.group(1)} }}}}`",
                    )
                    return result

                env = {
                    k: expression.sub(value, str(v))
                    for k, v in (step.get("env") or {}).items()
                }
                out = subprocess.run(
                    [
                        "bash",
                        "--noprofile",
                        "--norc",
                        "-eo",
                        "pipefail",
                        "-c",
                        step["run"],
                    ],
                    capture_output=True,
                    text=True,
                    env={"PATH": os.environ["PATH"], **env},
                )
                self.assertEqual(
                    out.returncode == 0,
                    result == "success",
                    f"the aggregator's exit {out.returncode} with the shards {result}",
                )
                if result != "success":
                    # A red required check with an empty log doesn't say where to look.
                    self.assertIn(
                        f"::error::a mutants shard ended {result}",
                        out.stdout,
                        "the aggregator fails without saying why",
                    )

    def test_the_aggregator_has_no_way_to_pass_over_a_red_shard(self):
        # A step `if` skips its one step and `continue-on-error` swallows its exit, and either
        # leaves the job green with a shard red. The aggregator runs no task, so
        # test_check_ci_parity.py's `CiCommands` never runs it; this holds it to keys that can't
        # make it pass.
        job = self.aggregator()
        extra = set(job) - {
            "name",
            "needs",
            "if",
            "runs-on",
            "timeout-minutes",
            "steps",
        }
        self.assertEqual(
            extra, set(), "the aggregator job sets a key that can hide a red shard"
        )
        for step in job.get("steps") or []:
            with self.subTest(step=step.get("name")):
                extra = set(step) - {"name", "env", "run"}
                self.assertEqual(
                    extra,
                    set(),
                    "an aggregator step sets a key that can hide a red shard",
                )


def tracked_rust() -> list[str]:
    """Every `.rs` file git sees in this tree, untracked ones included, ignored ones not."""
    return subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", "*.rs"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        env=clean_env(),
    ).stdout.split()


class TreeTests(unittest.TestCase):
    """What this tree hands cargo-mutants: the skip sites and the exclusions."""

    def test_every_mutants_skip_is_a_listed_site(self) -> None:
        found = Counter()
        for rel in tracked_rust():
            count = (ROOT / rel).read_text(encoding="utf-8").count("mutants::skip")
            if count:
                found[rel] = count
        self.assertEqual(
            dict(found),
            SKIPS,
            "a `mutants::skip` drops every mutant under it. Put it only on a test double or on "
            "code the Linux job never compiles, and list the site in SKIPS with why.",
        )

    def test_each_exclusion_drops_only_a_named_functions_return_value(self) -> None:
        config = tomllib.loads(
            (ROOT / ".cargo/mutants.toml").read_text(encoding="utf-8")
        )
        entries = config.get("exclude_re", [])
        self.assertTrue(entries, ".cargo/mutants.toml has no exclude_re entries")
        for entry in entries:
            with self.subTest(entry=entry):
                shape = EXCLUSION.fullmatch(entry)
                self.assertIsNotNone(
                    shape,
                    "an exclusion names one file and the `replace <function> -> ` mutants, "
                    "so logic added to the function is still mutated",
                )
                path = shape["path"].replace("\\.", ".")
                self.assertTrue((ROOT / path).is_file(), f"{path} doesn't exist")
        globs = config.get("exclude_globs", [])
        self.assertTrue(globs, ".cargo/mutants.toml has no exclude_globs entries")
        for glob in globs:
            with self.subTest(glob=glob):
                self.assertTrue((ROOT / glob).is_file(), f"{glob} doesn't exist")


if __name__ == "__main__":
    unittest.main()
