# SPDX-License-Identifier: Apache-2.0
"""Tests for `ci-local.py` (#315), over a throwaway repo with its own workflows and tasks.

Each job's `mise run` step runs a task, through the mise on PATH, that writes what it saw into
`facts.txt` in its clone, and the tests read it back: the checkout's depth and refs, the
environment a step gets, and which steps ran. The last ones check that a run leaves the source
repo's index, refs and tree as they were.
"""

import contextlib
import io
import json
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

RUNNER = runpy.run_path(str(Path(__file__).with_name("ci-local.py")))

# A git hook exports these; inherited, the fixture's git would write into the real repo.
GIT_LEAKS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_PREFIX",
)

CI = """\
name: ci
on: pull_request
env:
  WORKFLOW_LEVEL: workflow
jobs:
  shallow:
    runs-on: ubuntu-latest
    env:
      JOB_LEVEL: job
    steps:
      - uses: actions/checkout@v7
      - name: runner only
        run: echo "ran=runner-only" >> facts.txt
      - name: probe
        env:
          STEP_LEVEL: ${{ github.base_ref }}
        run: mise run probe
      - name: a push's step
        if: github.event_name != 'pull_request'
        run: mise run pushed
      - name: default shell has no pipefail
        run: false | true && mise run noted
  full:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
        with:
          fetch-depth: 0
      - run: mise run probe-full
  failing:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - name: bash has pipefail
        shell: bash
        run: mise run crash | true
      - run: mise run noted
  matrix:
    strategy:
      matrix:
        os: [ubuntu-latest, windows-latest]
        include:
          - os: ubuntu-latest
            task: noted
          - os: windows-latest
            task: pushed
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v7
      - run: mise run ${{ matrix.task }}
  slow:
    runs-on: ubuntu-latest
    timeout-minutes: 0.03
    steps:
      - uses: actions/checkout@v7
      - run: mise run sleeps
  cleanup:
    runs-on: ubuntu-latest
    timeout-minutes: 0.03
    steps:
      - uses: actions/checkout@v7
      - run: mise run cleans-up
  runner-only:
    runs-on: ubuntu-latest
    steps:
      - run: echo nothing to run here
"""

FUZZ = """\
name: fuzz
on: pull_request
jobs:
  fuzzing:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - run: mise run noted
"""

MISE = """\
[env]
MISE_LEVEL = "mise"

[tasks.probe]
run = '''
{
  echo "shallow=$(git rev-parse --is-shallow-repository)"
  echo "remotes=$(git remote | wc -l)"
  if git rev-parse -q --verify refs/remotes/origin/main >/dev/null; then echo "origin_main=yes"; else echo "origin_main=no"; fi
  echo "commits=$(git rev-list --count HEAD)"
  echo "workflow=$WORKFLOW_LEVEL"
  echo "job=$JOB_LEVEL"
  echo "step=$STEP_LEVEL"
  echo "mise=$MISE_LEVEL"
  echo "venv=${VIRTUAL_ENV-unset}"
  echo "target=${CARGO_TARGET_DIR-unset}"
  echo "caller_mise=${MISE_JOBS-unset}"
  echo "path=$PATH"
  echo "untracked=$(cat untracked.txt)"
  echo "edited=$(cat tracked.txt)"
  if [ -e ignored.log ]; then echo "ignored=yes"; else echo "ignored=no"; fi
} >> facts.txt
'''

[tasks.probe-full]
run = '''
echo "shallow=$(git rev-parse --is-shallow-repository)" >> facts.txt
echo "origin_main=$(git rev-parse refs/remotes/origin/main)" >> facts.txt
echo "commits=$(git rev-list --count HEAD)" >> facts.txt
'''

[tasks.pushed]
run = 'echo "ran=pushed" >> facts.txt'

[tasks.noted]
run = 'echo "ran=noted" >> facts.txt'

[tasks.crash]
run = 'echo "the input that crashed" > crash-1; exit 1'

[tasks.sleeps]
run = "sleep 30"

[tasks.cleans-up]
run = "{python} cleanup.py"
"""

# A task that starts a child in a session of its own, the way check-guards-fire.py starts
# cargo, and ends it on SIGINT or SIGTERM. The child holds the step's output pipe open, so only
# a signal the task can catch lets the job end on time.
CLEANUP = """\
import os, pathlib, signal, subprocess, sys, time

child = subprocess.Popen(["sleep", "45"], start_new_session=True)
pathlib.Path(os.environ["RUNNER_TEMP"], "child.pid").write_text(str(child.pid))


def stop(signum, frame):
    os.killpg(child.pid, signal.SIGKILL)
    sys.exit(1)


signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)
time.sleep(45)
"""


def clean_env():
    env = {k: v for k, v in os.environ.items() if k not in GIT_LEAKS}
    env.update(
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.com",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.com",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
    )
    return env


def git(cwd, *args):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=clean_env(),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def alive(pid):
    """Whether `pid` runs. A zombie counts as gone; so does a pid `kill -0` can't reach."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        status = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return not Path("/proc/self/stat").exists()
    return status.rpartition(")")[2].split()[0] != "Z"


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(
            shutil.which("mise"), "mise isn't on PATH, and the jobs run it"
        )
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "repo"
        self.work = Path(tmp.name) / "work"
        self.root.mkdir()
        files = {
            ".github/workflows/ci.yml": CI,
            ".github/workflows/fuzz.yml": FUZZ,
            "mise.toml": MISE.replace("{python}", sys.executable),
            "cleanup.py": CLEANUP,
            ".gitignore": "*.log\n",
            "tracked.txt": "committed\n",
        }
        for name, text in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        git(self.root, "-c", "init.defaultBranch=main", "init", "-q")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "one")
        self.first = git(self.root, "rev-parse", "HEAD")
        git(self.root, "update-ref", "refs/remotes/origin/main", self.first)
        git(self.root, "commit", "-q", "--allow-empty", "-m", "two")
        # The worktree's own changes: one edit, one new file, one ignored file.
        (self.root / "tracked.txt").write_text("edited\n")
        (self.root / "untracked.txt").write_text("new\n")
        (self.root / "ignored.log").write_text("ignored\n")
        self.before = self.repo_state()

    def repo_state(self):
        index = (self.root / ".git/index").read_bytes()
        return (
            git(self.root, "status", "--porcelain"),
            git(self.root, "for-each-ref"),
            index,
        )

    def run_jobs(self, *args):
        out = io.StringIO()
        # The caller's mise keeps its tools here; a clone's run gets neither its installs nor
        # its shims on PATH.
        data = self.root.parent / "mise-data"
        env = {
            "VIRTUAL_ENV": "/nowhere/venv",
            "CARGO_TARGET_DIR": "/nowhere/target",
            "MISE_JOBS": "1",
            "MISE_DATA_DIR": str(data),
            "PATH": os.pathsep.join(
                [
                    str(data / "shims"),
                    str(data / "installs/tool/1/bin"),
                    os.environ["PATH"],
                ]
            ),
        }
        with (
            mock.patch.dict(os.environ, env),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(out),
        ):
            code = RUNNER["main"](
                [*args, "--root", str(self.root), "--work-dir", str(self.work)]
            )
        return code, out.getvalue()

    def facts(self, job):
        text = (self.work / job / "src/facts.txt").read_text()
        return dict(line.split("=", 1) for line in text.splitlines())

    def result(self, job):
        return json.loads((self.work / job / "result.json").read_text())

    # ── the checkout ─────────────────────────────────────────────────────────

    def test_a_shallow_job_gets_one_commit_and_no_remote(self):
        code, out = self.run_jobs("shallow")
        self.assertEqual(code, 0, out)
        facts = self.facts("shallow")
        self.assertEqual(facts["shallow"], "true")
        self.assertEqual(facts["remotes"], "0")
        self.assertEqual(facts["origin_main"], "no")
        self.assertEqual(facts["commits"], "1")

    def test_a_full_job_gets_the_history_and_origin_main(self):
        code, out = self.run_jobs("full")
        self.assertEqual(code, 0, out)
        facts = self.facts("full")
        self.assertEqual(facts["shallow"], "false")
        self.assertEqual(facts["origin_main"], self.first)
        self.assertEqual(facts["commits"], "3")  # two commits and the snapshot on top

    def test_the_snapshot_carries_uncommitted_and_untracked_files_but_not_ignored_ones(
        self,
    ):
        code, out = self.run_jobs("shallow")
        self.assertEqual(code, 0, out)
        facts = self.facts("shallow")
        self.assertEqual(facts["edited"], "edited")
        self.assertEqual(facts["untracked"], "new")
        self.assertEqual(facts["ignored"], "no")

    def test_a_run_leaves_the_source_repo_as_it_was(self):
        code, out = self.run_jobs("full")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.repo_state(), self.before)
        snapshot = self.result("full")["commit"]
        self.assertEqual(
            git(self.root, "rev-parse", f"{snapshot}^"),
            git(self.root, "rev-parse", "HEAD"),
        )
        self.assertNotIn(
            snapshot, git(self.root, "for-each-ref", "--format=%(objectname)")
        )

    def test_apply_patches_the_snapshot_and_not_the_worktree(self):
        patch = self.root.parent / "fault.patch"
        patch.write_text(
            "diff --git a/applied.txt b/applied.txt\nnew file mode 100644\n"
            "--- /dev/null\n+++ b/applied.txt\n@@ -0,0 +1 @@\n+applied\n"
        )
        code, out = self.run_jobs("shallow", "--apply", str(patch))
        self.assertEqual(code, 0, out)
        self.assertTrue((self.work / "shallow/src/applied.txt").exists())
        self.assertFalse((self.root / "applied.txt").exists())
        self.assertEqual(self.repo_state(), self.before)

    def test_the_target_survives_a_fresh_clone_and_nothing_else_does(self):
        self.run_jobs("shallow")
        (self.work / "shallow/src/target").mkdir(exist_ok=True)
        (self.work / "shallow/src/target/keep").write_text("built\n")
        (self.work / "shallow/src/stale").write_text("left over\n")
        code, out = self.run_jobs("shallow")
        self.assertEqual(code, 0, out)
        self.assertTrue((self.work / "shallow/src/target/keep").exists())
        self.assertFalse((self.work / "shallow/src/stale").exists())

    # ── the environment ──────────────────────────────────────────────────────

    def test_a_step_gets_the_workflow_job_step_and_mise_env_and_not_the_callers(self):
        code, out = self.run_jobs("shallow")
        self.assertEqual(code, 0, out)
        facts = self.facts("shallow")
        self.assertEqual(facts["workflow"], "workflow")
        self.assertEqual(facts["job"], "job")
        self.assertEqual(facts["step"], "main")  # `github.base_ref` on a pull request
        self.assertEqual(facts["mise"], "mise")  # the clone's own `[env]`
        self.assertEqual(facts["venv"], "unset")
        self.assertEqual(facts["target"], "unset")
        self.assertEqual(facts["caller_mise"], "unset")
        self.assertNotIn("mise-data", facts["path"])

    # ── which steps run ──────────────────────────────────────────────────────

    def test_only_the_pull_requests_mise_steps_run(self):
        code, out = self.run_jobs("shallow")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.facts("shallow")["ran"], "noted")
        rows = {r["step"]: r for r in self.result("shallow")["steps"]}
        self.assertEqual(rows["a push's step"]["status"], "skipped")
        self.assertNotIn("runner only", rows)

    def test_a_matrix_job_runs_its_ubuntu_leg(self):
        code, out = self.run_jobs("matrix")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.facts("matrix"), {"ran": "noted"})

    def test_the_default_shell_has_no_pipefail_and_bash_does(self):
        code, out = self.run_jobs("shallow")
        self.assertEqual(code, 0, out)  # `false | true` passes under `bash -e`
        code, out = self.run_jobs("failing")
        self.assertEqual(code, 1, out)
        self.assertIn("bash has pipefail ... FAILED (exit 1", out)

    def test_a_failing_step_ends_the_job_and_its_tree_is_kept(self):
        (self.work / "failing/src/target").mkdir(parents=True)
        (self.work / "failing/src/target/keep").write_text("built\n")
        code, out = self.run_jobs("failing")
        self.assertEqual(code, 1, out)
        record = self.result("failing")
        self.assertEqual(record["status"], "failed")
        self.assertEqual([r["step"] for r in record["steps"]], ["bash has pipefail"])
        kept = self.work / "failing/failed"
        self.assertEqual((kept / "crash-1").read_text(), "the input that crashed\n")
        self.assertIn(f"its tree is kept in {kept}", out)
        self.assertEqual(record["kept"], str(kept))
        # The target went back for the next clone, and the next run starts clean.
        self.assertTrue((self.work / "failing/src/target/keep").exists())
        self.assertFalse((kept / "target").exists())

    def test_a_passing_run_of_a_job_removes_its_kept_tree(self):
        stale = self.work / "shallow/failed"
        stale.mkdir(parents=True)
        (stale / "crash-1").write_text("old\n")
        code, out = self.run_jobs("shallow")
        self.assertEqual(code, 0, out)
        self.assertFalse(stale.exists())

    def test_the_jobs_timeout_applies(self):
        start = time.monotonic()
        code, out = self.run_jobs("slow")
        self.assertEqual(code, 1, out)
        self.assertLess(time.monotonic() - start, 20)
        self.assertIn("timeout-minutes (0.03) ran out", out)

    def test_a_timed_out_step_gets_a_signal_it_can_clean_up_on(self):
        # SIGKILL first would kill the step before its handler ran, and its own-session child
        # would hold the output pipe for the whole 45 s.
        start = time.monotonic()
        code, out = self.run_jobs("cleanup")
        elapsed = time.monotonic() - start
        self.assertEqual(code, 1, out)
        self.assertIn("timeout-minutes (0.03) ran out", out)
        self.assertLess(elapsed, 15, out)
        pid = int((self.work / "cleanup/runner/temp/child.pid").read_text())
        deadline = time.monotonic() + 5
        while alive(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertFalse(alive(pid), f"the step's child {pid} outlived the job")

    def test_every_job_runs_at_once_and_the_summary_reports_each(self):
        (self.root / ".github/workflows/ci.yml").write_text(
            CI.split("  slow:\n")[0] + "  runner-only:\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - run: echo nothing to run here\n"
        )
        code, out = self.run_jobs()
        self.assertEqual(code, 1, out)  # `failing` fails
        for job in ("shallow", "full", "matrix", "fuzzing"):
            self.assertIn(f"  {job}: passed", out)
        self.assertIn("  failing: failed", out)
        self.assertIn(
            "ci.yml job `matrix` on windows-latest: this runs its ubuntu leg", out
        )
        self.assertIn("ci.yml job `runner-only`: it runs no task", out)
        self.assertIn("ci.yml job `shallow`: its step `runner only` runs no task", out)

    # ── refusals ─────────────────────────────────────────────────────────────

    def test_an_expression_it_cant_answer_refuses_the_run(self):
        ci = self.root / ".github/workflows/ci.yml"
        ci.write_text(
            ci.read_text().replace("${{ github.base_ref }}", "${{ github.head_ref }}")
        )
        code, out = self.run_jobs("shallow")
        self.assertEqual(code, 1, out)
        self.assertIn(
            "uses `${{ github.head_ref }}`, which tools/ci-local.py's EXPRESSIONS", out
        )
        self.assertFalse((self.work / "shallow").exists())

    def test_a_condition_it_cant_answer_refuses_the_run(self):
        ci = self.root / ".github/workflows/ci.yml"
        ci.write_text(
            ci.read_text().replace(
                "if: github.event_name != 'pull_request'",
                "if: github.actor == 'someone'",
            )
        )
        code, out = self.run_jobs("shallow")
        self.assertEqual(code, 1, out)
        self.assertIn("runs `if: github.actor == 'someone'`", out)

    def test_a_timeout_it_cant_hold_a_job_to_refuses_the_run(self):
        ci = self.root / ".github/workflows/ci.yml"
        for budget in ("${{ github.event_name == 'push' && 60 || 30 }}", ".inf", "0"):
            with self.subTest(budget=budget):
                ci.write_text(
                    CI.replace("timeout-minutes: 0.03", f"timeout-minutes: {budget}", 1)
                )
                code, out = self.run_jobs("shallow")
                self.assertEqual(code, 1, out)
                self.assertIn("job `slow` sets timeout-minutes to", out)

    def test_an_unknown_job_fails(self):
        code, out = self.run_jobs("nope")
        self.assertEqual(code, 1, out)
        self.assertIn("no job here runs as `nope`", out)

    def test_a_full_job_without_origin_main_fails(self):
        git(self.root, "update-ref", "-d", "refs/remotes/origin/main")
        code, out = self.run_jobs("full")
        self.assertEqual(code, 1, out)
        self.assertIn("this repo has no origin/main", out)


if __name__ == "__main__":
    unittest.main()
