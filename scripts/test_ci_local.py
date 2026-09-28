# SPDX-License-Identifier: Apache-2.0
"""Tests for `ci-local.py` (#315), over a throwaway repo with its own workflow and plan.

Each job's steps write what they saw into `facts.txt` in their clone, and the tests read it back:
the checkout's depth and refs, the environment a step gets, the runner files, and which steps
ran. The last ones check that a run leaves the source repo's index, refs and tree as they were.
"""

import contextlib
import io
import json
import os
import runpy
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

WORKFLOW = """\
name: ci
on: pull_request
env:
  CARGO_TERM_COLOR: always
jobs:
  shallow:
    runs-on: ubuntu-latest
    env:
      JOB_LEVEL: job
    steps:
      - uses: actions/checkout@v5
      - name: probe
        env:
          STEP_LEVEL: ${{ matrix.os }}
        run: |
          {
            echo "shallow=$(git rev-parse --is-shallow-repository)"
            echo "remotes=$(git remote | wc -l)"
            if git rev-parse -q --verify refs/remotes/origin/main >/dev/null; then echo "origin_main=yes"; else echo "origin_main=no"; fi
            echo "commits=$(git rev-list --count HEAD)"
            echo "color=$CARGO_TERM_COLOR"
            echo "job=$JOB_LEVEL"
            echo "step=$STEP_LEVEL"
            echo "local_only=${LOCAL_ONLY-unset}"
            echo "venv=${VIRTUAL_ENV-unset}"
            echo "target=$CARGO_TARGET_DIR"
            echo "untracked=$(cat untracked.txt)"
            echo "edited=$(cat tracked.txt)"
            if [ -e ignored.log ]; then echo "ignored=yes"; else echo "ignored=no"; fi
          } > facts.txt
      - name: add a path
        run: |
          mkdir -p "$RUNNER_TEMP/bin"
          printf '#!/bin/sh\\necho from-path\\n' > "$RUNNER_TEMP/bin/hello"
          chmod +x "$RUNNER_TEMP/bin/hello"
          echo "$RUNNER_TEMP/bin" >> "$GITHUB_PATH"
          echo "FROM_ENV=carried" >> "$GITHUB_ENV"
          echo "## summary line" >> "$GITHUB_STEP_SUMMARY"
      - name: use the path
        run: echo "hello=$(hello) env=$FROM_ENV" >> facts.txt
      - name: never on this leg
        if: matrix.os == 'windows-latest'
        run: echo "ran=windows" >> facts.txt
      - name: skipped here
        run: echo "ran=skipped" >> facts.txt
      - name: default shell has no pipefail
        run: false | true
      - uses: some/action@v1
  full:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
        with:
          fetch-depth: 0
      - name: probe
        run: |
          echo "shallow=$(git rev-parse --is-shallow-repository)" >> facts.txt
          echo "origin_main=$(git rev-parse refs/remotes/origin/main)" >> facts.txt
          echo "commits=$(git rev-list --count HEAD)" >> facts.txt
  failing:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - name: bash has pipefail
        shell: bash
        run: |
          echo "the input that crashed" > crash-1
          false | true
      - name: after
        run: echo "ran=after" >> facts.txt
  next:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - name: passes
        run: echo "ran=next" >> facts.txt
  slow:
    runs-on: ubuntu-latest
    timeout-minutes: 0.03
    steps:
      - uses: actions/checkout@v5
      - name: sleeps
        run: sleep 30
  cleanup:
    runs-on: ubuntu-latest
    timeout-minutes: 0.03
    steps:
      - uses: actions/checkout@v5
      - name: cleans up on a signal
        run: {python} cleanup.py
"""

# A step that starts a child in a session of its own, the way check-guards-fire.py starts
# cargo, and ends it on SIGINT or SIGTERM. The child holds the step's output pipe open, so only
# a signal the step can catch lets the job end on time.
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

PLAN = """\
workflows = ["ci.yml"]

[expressions]
"matrix.os" = "ubuntu-latest"
"matrix.os == 'windows-latest'" = "false"

[job."ci.yml".shallow]
task = "one"
steps = [
  "probe",
  "add a path",
  "use the path",
  "never on this leg",
  "skipped here",
  "default shell has no pipefail",
  "some/action",
]

[job."ci.yml".shallow.local."skipped here"]
skip = "a reason to skip"

[job."ci.yml".shallow.local."some/action"]
run = 'echo "action=local" >> facts.txt'
reason = "stands in for the action"

[job."ci.yml".full]
task = "two"
steps = ["probe"]

[job."ci.yml".failing]
task = "three"
steps = ["bash has pipefail", "after"]

[job."ci.yml".next]
task = "three"
steps = ["passes"]

[job."ci.yml".slow]
task = "four"
steps = ["sleeps"]

[job."ci.yml".cleanup]
task = "five"
steps = ["cleans up on a signal"]

[actions]
"actions/checkout" = "the runner clones the snapshot itself"
"""

MISE = """\
[env]
LOCAL_ONLY = "1"
CARGO_TERM_COLOR = "always"
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
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "repo"
        self.work = Path(tmp.name) / "work"
        self.root.mkdir()
        files = {
            ".github/workflows/ci.yml": WORKFLOW.replace("{python}", sys.executable),
            "cleanup.py": CLEANUP,
            "ci/local.toml": PLAN,
            "mise.toml": MISE,
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

    def run_task(self, task, *extra):
        out = io.StringIO()
        env = {
            "LOCAL_ONLY": "1",
            "VIRTUAL_ENV": "/nowhere/venv",
            "CARGO_TARGET_DIR": "/nowhere/target",
        }
        with (
            mock.patch.dict(os.environ, env),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(out),
        ):
            code = RUNNER["main"](
                [task, "--root", str(self.root), "--work-dir", str(self.work), *extra]
            )
        return code, out.getvalue()

    def facts(self, task):
        text = (self.work / task / "src/facts.txt").read_text()
        return dict(line.split("=", 1) for line in text.splitlines())

    def result(self, task):
        return json.loads((self.work / task / "result.json").read_text())

    # ── the checkout ─────────────────────────────────────────────────────────

    def test_a_shallow_job_gets_one_commit_and_no_remote(self):
        code, out = self.run_task("one")
        self.assertEqual(code, 0, out)
        facts = self.facts("one")
        self.assertEqual(facts["shallow"], "true")
        self.assertEqual(facts["remotes"], "0")
        self.assertEqual(facts["origin_main"], "no")
        self.assertEqual(facts["commits"], "1")

    def test_a_full_job_gets_the_history_and_origin_main(self):
        code, out = self.run_task("two")
        self.assertEqual(code, 0, out)
        facts = self.facts("two")
        self.assertEqual(facts["shallow"], "false")
        self.assertEqual(facts["origin_main"], self.first)
        self.assertEqual(facts["commits"], "3")  # two commits and the snapshot on top

    def test_the_snapshot_carries_uncommitted_and_untracked_files_but_not_ignored_ones(
        self,
    ):
        code, out = self.run_task("one")
        self.assertEqual(code, 0, out)
        facts = self.facts("one")
        self.assertEqual(facts["edited"], "edited")
        self.assertEqual(facts["untracked"], "new")
        self.assertEqual(facts["ignored"], "no")

    def test_a_run_leaves_the_source_repo_as_it_was(self):
        code, out = self.run_task("two")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.repo_state(), self.before)
        snapshot = self.result("two")["commit"]
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
        code, out = self.run_task("one", "--apply", str(patch))
        self.assertEqual(code, 0, out)
        self.assertTrue((self.work / "one/src/applied.txt").exists())
        self.assertFalse((self.root / "applied.txt").exists())
        self.assertEqual(self.repo_state(), self.before)

    def test_the_target_survives_a_fresh_clone_and_nothing_else_does(self):
        self.run_task("one")
        (self.work / "one/src/target").mkdir(exist_ok=True)
        (self.work / "one/src/target/keep").write_text("built\n")
        (self.work / "one/src/stale").write_text("left over\n")
        code, out = self.run_task("one")
        self.assertEqual(code, 0, out)
        self.assertTrue((self.work / "one/src/target/keep").exists())
        self.assertFalse((self.work / "one/src/stale").exists())

    # ── the environment ──────────────────────────────────────────────────────

    def test_a_step_gets_the_workflow_job_and_step_env_and_not_the_callers(self):
        code, out = self.run_task("one")
        self.assertEqual(code, 0, out)
        facts = self.facts("one")
        self.assertEqual(facts["color"], "always")
        self.assertEqual(facts["job"], "job")
        self.assertEqual(facts["step"], "ubuntu-latest")
        self.assertEqual(facts["local_only"], "unset")  # a mise-only key
        self.assertEqual(facts["venv"], "unset")
        self.assertEqual(facts["target"], str(self.work / "one/src/target"))

    def test_github_path_env_and_summary_reach_the_later_steps(self):
        code, out = self.run_task("one")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.facts("one")["hello"], "from-path env=carried")
        self.assertIn("## summary line", (self.work / "one/summary.md").read_text())

    # ── which steps run ──────────────────────────────────────────────────────

    def test_skips_false_ifs_and_local_commands_follow_the_plan(self):
        code, out = self.run_task("one")
        self.assertEqual(code, 0, out)
        facts = self.facts("one")
        self.assertNotIn("ran", facts)
        self.assertEqual(facts["action"], "local")
        rows = {r["step"]: r for r in self.result("one")["jobs"][0]["steps"]}
        self.assertEqual(rows["skipped here"]["status"], "skipped")
        self.assertEqual(rows["skipped here"]["note"], "a reason to skip")
        self.assertEqual(rows["never on this leg"]["status"], "skipped")
        self.assertIn("skipped (a reason to skip)", out)

    def test_the_default_shell_has_no_pipefail_and_bash_does(self):
        code, out = self.run_task("one")
        self.assertEqual(code, 0, out)  # `false | true` passes under `bash -e`
        code, out = self.run_task("three")
        self.assertEqual(code, 1, out)
        self.assertIn("bash has pipefail ... FAILED (exit 1", out)

    def test_a_failing_step_ends_the_job(self):
        code, out = self.run_task("three")
        self.assertEqual(code, 1, out)
        self.assertFalse((self.work / "three/failed-failing/facts.txt").exists())
        record = self.result("three")
        self.assertEqual(record["status"], "failed")
        self.assertEqual(
            [r["step"] for r in record["jobs"][0]["steps"]], ["bash has pipefail"]
        )

    def test_a_failed_jobs_tree_outlives_the_next_jobs_clone(self):
        # ci:fuzz runs several jobs in one work dir. A crash's reproducer is the failed job's
        # file CI uploads; the next job's fresh clone would delete it.
        (self.work / "three/src/target").mkdir(parents=True)
        (self.work / "three/src/target/keep").write_text("built\n")
        code, out = self.run_task("three")
        self.assertEqual(code, 1, out)
        kept = self.work / "three/failed-failing"
        self.assertEqual((kept / "crash-1").read_text(), "the input that crashed\n")
        self.assertIn(f"its tree is kept in {kept}", out)
        self.assertEqual(self.result("three")["jobs"][0]["kept"], str(kept))
        # The next job ran in a fresh clone that kept the target.
        self.assertEqual(self.facts("three"), {"ran": "next"})
        self.assertFalse((self.work / "three/src/crash-1").exists())
        self.assertTrue((self.work / "three/src/target/keep").exists())
        self.assertFalse((kept / "target").exists())

    def test_a_passing_run_of_a_job_removes_its_kept_tree(self):
        stale = self.work / "one/failed-shallow"
        stale.mkdir(parents=True)
        (stale / "crash-1").write_text("old\n")
        code, out = self.run_task("one")
        self.assertEqual(code, 0, out)
        self.assertFalse(stale.exists())

    def test_the_jobs_timeout_applies(self):
        start = time.monotonic()
        code, out = self.run_task("four")
        self.assertEqual(code, 1, out)
        self.assertLess(time.monotonic() - start, 20)
        self.assertIn("timeout-minutes (0.03) ran out", out)

    def test_a_timed_out_step_gets_a_signal_it_can_clean_up_on(self):
        # SIGKILL first would kill the step before its handler ran, and its own-session child
        # would hold the output pipe for the whole 45 s.
        start = time.monotonic()
        code, out = self.run_task("five")
        elapsed = time.monotonic() - start
        self.assertEqual(code, 1, out)
        self.assertIn("timeout-minutes (0.03) ran out", out)
        self.assertLess(elapsed, 15, out)
        pid = int((self.work / "five/runner/temp/child.pid").read_text())
        deadline = time.monotonic() + 5
        while alive(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertFalse(alive(pid), f"the step's child {pid} outlived the job")

    # ── refusals ─────────────────────────────────────────────────────────────

    def test_an_uncovered_step_refuses_to_run(self):
        ci = self.root / ".github/workflows/ci.yml"
        ci.write_text(
            ci.read_text().replace(
                "run: sleep 30", "run: sleep 30\n      - run: echo new"
            )
        )
        code, out = self.run_task("one")
        self.assertEqual(code, 1, out)
        self.assertIn("step `echo new` has no entry in ci/local.toml", out)
        self.assertFalse((self.work / "one").exists())

    def test_an_unknown_task_fails(self):
        code, out = self.run_task("nope")
        self.assertEqual(code, 1, out)
        self.assertIn(
            "no job runs as `nope`; the tasks are five, four, one, three, two", out
        )

    def test_a_full_job_without_origin_main_fails(self):
        git(self.root, "update-ref", "-d", "refs/remotes/origin/main")
        code, out = self.run_task("two")
        self.assertEqual(code, 1, out)
        self.assertIn("this repo has no origin/main", out)

    # ── summary ──────────────────────────────────────────────────────────────

    def test_summary_reports_each_task_and_what_stays_ci_only(self):
        self.run_task("one")
        code, out = self.run_task("summary")
        self.assertEqual(code, 1, out)  # two, three and four haven't run
        self.assertIn("ci:one: passed", out)
        self.assertIn("ci:two: no result", out)
        self.assertIn("step `skipped here`: a reason to skip", out)
        self.assertIn(
            "`actions/checkout` steps: the runner clones the snapshot itself", out
        )


if __name__ == "__main__":
    unittest.main()
