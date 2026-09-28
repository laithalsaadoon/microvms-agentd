# SPDX-License-Identifier: Apache-2.0
"""Tests for `scripts/check-guards-fire.py`: when a seeded fault counts as fired (#274).

Each case builds a throwaway git repository with a registry and a fake test runner on PATH
named `cargo`, `pytest` or `node`. The fake reads `state.txt` in the tree it runs in and prints
the line the real runner would, so every verdict branch is driven through the real script,
the scratch worktree included. The census cases run the real ast-grep, so run this through
`mise run guards:list`.
"""

import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "check-guards-fire.py"
MARKER = "**" + "Falsification" + "**"

# The pointers a git hook exports; see check-guards-fire.py's `GIT_ENV_LEAKS`. The fixture
# repos below are written with `git add` and `git commit`, which an inherited GIT_DIR would
# point at the real repository.
GIT_ENV_LEAKS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_CEILING_DIRECTORIES",
    "GIT_PREFIX",
)

# One runner for all three names. It reads every `state*.txt` in the tree it runs in as
# `name=value` words: `build=E0599` breaks the build with that code, `<test>=fail` fails that
# test, `color=yes` wraps each line in ANSI codes the way CI's `CARGO_TERM_COLOR=always` does,
# `slow=N` sleeps N tenths of a second, and `hang=yes` sleeps past any timeout (with a child
# that sleeps too, and both pids written under $FAKE_PIDS when it's set). With
# CARGO_TARGET_DIR set, the runner stands in for a build there: `built.txt` records the state
# it last ran on, `runs` counts runs, and `flaky=N` fails the Nth run. $FAKE_LOG, when set,
# gets a line per run: the target, the venv, the tree, whether the tree was seeded, and the
# arguments.
FAKE_RUNNER = """\
#!{python}
import os
import pathlib
import subprocess
import sys
import time

texts = [p.read_text() for p in sorted(pathlib.Path(".").glob("state*.txt"))]
state = dict(w.split("=", 1) for w in " ".join(texts).split())
tool = pathlib.Path(sys.argv[0]).name


def say(line):
    print(f"\\x1b[1m{{line}}\\x1b[0m" if state.get("color") == "yes" else line)


if os.environ.get("FAKE_LOG"):
    seeded = any(
        v not in ("ok", "no") for k, v in state.items() if k not in ("slow", "flaky")
    )
    with open(os.environ["FAKE_LOG"], "a") as log:
        log.write("\\t".join([
            os.environ.get("CARGO_TARGET_DIR", ""),
            os.environ.get("VIRTUAL_ENV", ""),
            os.getcwd(),
            "seeded" if seeded else "clean",
            " ".join(sys.argv[1:]),
        ]) + "\\n")
if os.environ.get("CARGO_TARGET_DIR"):
    target = pathlib.Path(os.environ["CARGO_TARGET_DIR"])
    target.mkdir(parents=True, exist_ok=True)
    runs = target / "runs"
    count = int(runs.read_text()) + 1 if runs.exists() else 1
    runs.write_text(str(count))
    (target / "built.txt").write_text("".join(texts))
    if state.get("flaky") == str(count):
        say(f"run {{count}} went red")
        sys.exit(1)
time.sleep(int(state.get("slow", "0")) / 10)
if state.get("hang") == "yes":
    child = subprocess.Popen(["sleep", "30"])
    if os.environ.get("FAKE_PIDS"):
        for pid in (os.getpid(), child.pid):
            pathlib.Path(os.environ["FAKE_PIDS"], str(pid)).write_text("")
    time.sleep(30)
if state.get("build", "ok") != "ok":
    say(f"error[{{state['build']}}]: the build broke")
    say("error: could not compile `fixture`")
    sys.exit(101)
names = [a for a in sys.argv[1:] if not a.startswith("-") and a != "test"]
failed = False
for number, name in enumerate(names, 1):
    ok = state.get(name, "ok") == "ok"
    failed |= not ok
    if tool == "cargo":
        say(f"test {{name}} ... {{'ok' if ok else 'FAILED'}}")
    elif tool == "pytest":
        say(f"{{'PASSED' if ok else 'FAILED'}} {{name}}" + ("" if ok else " - assert"))
    else:
        say(f"{{'ok' if ok else 'not ok'}} {{number}} - {{name}}")
sys.exit(1 if failed else 0)
"""

CARGO = ["cargo", "test", "--", "--exact", "the_guard"]


def clean_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in GIT_ENV_LEAKS}
    env.update(extra)
    return env


def git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env=env or clean_env(),
    ).stdout


def toml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def toml_argv(argv: list[str]) -> str:
    return "[" + ", ".join(toml_str(a) for a in argv) + "]"


def entry(
    fid: str = "one",
    guard: str = "the_guard",
    run: list[str] | None = None,
    expect: str = "test-failed",
    fault: str = 'transform = { file = "state.txt", replace = "the_guard=ok", with = "the_guard=fail" }',
    suite: str = "rust",
    **extra: str,
) -> str:
    lines = [
        "[[fault]]",
        f"id = {toml_str(fid)}",
        f"guard = {toml_str(guard)}",
        f"run = {toml_argv(run or CARGO)}",
        f"expect = {toml_str(expect)}",
        f"suite = {toml_str(suite)}",
        fault,
        *(f"{k} = {toml_str(v)}" for k, v in extra.items()),
    ]
    return "\n".join(lines) + "\n"


class Repo:
    """A throwaway committed repository with a fake runner on PATH."""

    def __init__(self, test: unittest.TestCase, files: dict[str, str]):
        directory = tempfile.TemporaryDirectory()
        test.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "repo"
        self.bin = Path(directory.name) / "bin"
        self.bin.mkdir()
        for tool in ("cargo", "pytest", "node"):
            path = self.bin / tool
            path.write_text(FAKE_RUNNER.format(python=sys.executable))
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        files = {"guards/unregistered.txt": "", **files}
        for path, text in files.items():
            self.write(path, text)
        git(self.root.parent, "init", "-q", "-b", "main", str(self.root))
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "fixture")
        # `list` compares guards/unregistered.txt with the merge base of HEAD and origin/main.
        git(self.root, "update-ref", "refs/remotes/origin/main", "HEAD")
        self.target = Path(directory.name) / "target"
        # The script's scratch trees go here, so one a seeded fault leaves behind goes with
        # the fixture rather than into the caller's temp directory.
        self.tmp = Path(directory.name) / "tmp"
        self.tmp.mkdir()

    def commit(self, message: str = "change") -> None:
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", message)

    def write(self, path: str, text: str) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(text))

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        path = os.pathsep.join([str(self.bin), os.environ.get("PATH", "")])
        # The fake runner writes where cargo would build; never into a real target.
        env.setdefault("CARGO_TARGET_DIR", str(self.target))
        env.setdefault("TMPDIR", str(self.tmp))
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--root", str(self.root), *args],
            capture_output=True,
            text=True,
            env=clean_env(PATH=path, **env),
        )

    def worktrees(self) -> int:
        return git(self.root, "worktree", "list").count("\n")


def fire_repo(
    test: unittest.TestCase, *entries: str, state: str = "the_guard=ok\n"
) -> Repo:
    return Repo(test, {"guards/faults.toml": "".join(entries), "state.txt": state})


class FireVerdicts(unittest.TestCase):
    def test_a_fault_the_guard_catches_is_fired(self):
        repo = fire_repo(self, entry())
        out = repo.run("fire")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("fired: one", out.stdout)

    def test_a_guard_that_passes_with_its_fault_did_not_fire(self):
        fault = 'transform = { file = "state.txt", replace = "other=ok", with = "other=fail" }'
        repo = fire_repo(self, entry(fault=fault), state="the_guard=ok other=ok\n")
        out = repo.run("fire")
        self.assertEqual(out.returncode, 1)
        self.assertIn("DID NOT FIRE: one: the command passed", out.stdout)

    def test_a_fault_that_only_breaks_the_build_did_not_fire(self):
        fault = 'transform = { file = "state.txt", replace = "build=ok", with = "build=E0599" }'
        repo = fire_repo(self, entry(fault=fault), state="build=ok the_guard=ok\n")
        out = repo.run("fire")
        self.assertEqual(out.returncode, 1)
        self.assertIn("DID NOT FIRE: one: the build broke (error[E0599])", out.stdout)
        self.assertNotIn("fired: one", out.stdout)

    def test_a_compile_error_with_another_code_did_not_fire(self):
        fault = 'transform = { file = "state.txt", replace = "build=ok", with = "build=E0599" }'
        repo = fire_repo(
            self,
            entry(expect="compile-error", fault=fault, code="E0432"),
            state="build=ok\n",
        )
        out = repo.run("fire")
        self.assertEqual(out.returncode, 1)
        self.assertIn(
            "DID NOT FIRE: one: expected error[E0432], got ['E0599']", out.stdout
        )

    def test_a_compile_error_with_its_code_is_fired(self):
        fault = 'transform = { file = "state.txt", replace = "build=ok", with = "build=E0432" }'
        repo = fire_repo(
            self,
            entry(expect="compile-error", fault=fault, code="E0432"),
            state="build=ok\n",
        )
        out = repo.run("fire")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("fired: one", out.stdout)

    def test_an_anchor_that_matches_twice_is_a_stale_anchor(self):
        fault = 'transform = { file = "state.txt", replace = "=ok", with = "=fail" }'
        repo = fire_repo(self, entry(fault=fault), state="the_guard=ok other=ok\n")
        out = repo.run("fire")
        self.assertEqual(out.returncode, 1)
        self.assertIn(
            "stale anchor: one: state.txt: the anchor '=ok' matches 2 times", out.stdout
        )
        listed = repo.run("list")
        self.assertEqual(listed.returncode, 1)
        self.assertIn("stale anchor: one", listed.stderr)

    def test_an_anchor_that_matches_nothing_is_a_stale_anchor(self):
        fault = 'transform = { file = "state.txt", replace = "gone=ok", with = "gone=fail" }'
        out = fire_repo(self, entry(fault=fault)).run("fire")
        self.assertEqual(out.returncode, 1)
        self.assertIn("matches 0 times, not once", out.stdout)

    def test_a_clean_tree_whose_guard_is_already_red_stops_before_any_fault(self):
        second = entry(
            fid="two", guard="other", run=["cargo", "test", "--", "--exact", "other"]
        )
        second = second.replace("the_guard=ok", "other=ok").replace(
            "the_guard=fail", "other=fail"
        )
        repo = fire_repo(self, entry(), second, state="the_guard=fail other=ok\n")
        out = repo.run("fire")
        self.assertEqual(out.returncode, 1)
        self.assertIn(
            "already red: one: the command exits 1 with no fault seeded (clean run)",
            out.stdout,
        )
        self.assertNotIn("fired:", out.stdout)
        self.assertNotIn("DID NOT FIRE", out.stdout)

    def test_a_guard_the_clean_run_never_reports_is_not_found(self):
        repo = fire_repo(self, entry(guard="the_gaurd"))
        out = repo.run("fire")
        self.assertEqual(out.returncode, 1)
        self.assertIn("guard not found: one", out.stdout)

    def test_another_test_failing_is_not_the_guard_firing(self):
        run = ["cargo", "test", "--", "--exact", "the_guard", "neighbor"]
        fault = 'transform = { file = "state.txt", replace = "neighbor=ok", with = "neighbor=fail" }'
        repo = fire_repo(
            self, entry(run=run, fault=fault), state="the_guard=ok neighbor=ok\n"
        )
        out = repo.run("fire")
        self.assertEqual(out.returncode, 1)
        self.assertIn("but not with the_guard reported failed", out.stdout)

    def test_pytest_reports_the_named_test(self):
        guard = "tests/test_x.py::test_guard"
        run = ["pytest", "-rA", guard]
        fault = f'transform = {{ file = "state.txt", replace = "{guard}=ok", with = "{guard}=fail" }}'
        repo = fire_repo(
            self, entry(guard=guard, run=run, fault=fault), state=f"{guard}=ok\n"
        )
        out = repo.run("fire")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("fired: one", out.stdout)

    def test_pytest_failing_another_test_did_not_fire(self):
        guard = "tests/test_x.py::test_guard"
        other = "tests/test_x.py::test_other"
        run = ["pytest", "-rA", guard, other]
        fault = f'transform = {{ file = "state.txt", replace = "{other}=ok", with = "{other}=fail" }}'
        repo = fire_repo(
            self,
            entry(guard=guard, run=run, fault=fault),
            state=f"{guard}=ok {other}=ok\n",
        )
        out = repo.run("fire")
        self.assertEqual(out.returncode, 1)
        self.assertIn(f"but not with {guard} reported failed", out.stdout)

    def test_node_tap_reports_the_named_test(self):
        run = ["node", "--test", "--test-reporter=tap", "stream"]
        fault = 'transform = { file = "state.txt", replace = "stream=ok", with = "stream=fail" }'
        repo = fire_repo(
            self, entry(guard="stream", run=run, fault=fault), state="stream=ok\n"
        )
        out = repo.run("fire")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("fired: one", out.stdout)

    def test_node_not_ok_for_another_test_did_not_fire(self):
        run = ["node", "--test", "--test-reporter=tap", "stream", "other"]
        fault = 'transform = { file = "state.txt", replace = "other=ok", with = "other=fail" }'
        repo = fire_repo(
            self,
            entry(guard="stream", run=run, fault=fault),
            state="stream=ok other=ok\n",
        )
        out = repo.run("fire")
        self.assertEqual(out.returncode, 1)
        self.assertIn("but not with stream reported failed", out.stdout)

    def test_exit_nonzero_needs_its_message(self):
        gate = [
            sys.executable,
            "-c",
            "import sys; bad = open('state.txt').read().strip() == 'x=bad'; "
            "bad and print('the wrong reason'); sys.exit(1 if bad else 0)",
        ]
        fault = 'transform = { file = "state.txt", replace = "x=ok", with = "x=bad" }'
        repo = fire_repo(
            self,
            entry(
                expect="exit-nonzero", run=gate, fault=fault, message="the named reason"
            ),
            state="x=ok\n",
        )
        out = repo.run("fire")
        self.assertEqual(out.returncode, 1)
        self.assertIn("never says 'the named reason'", out.stdout)
        repo.write(
            "guards/faults.toml",
            entry(
                expect="exit-nonzero", run=gate, fault=fault, message="the wrong reason"
            ),
        )
        out = repo.run("fire")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    def test_a_message_the_clean_run_already_prints_is_refused(self):
        gate = [
            sys.executable,
            "-c",
            "import sys; print('FAIL  a named case'); "
            "sys.exit(1 if open('state.txt').read().strip() == 'x=bad' else 0)",
        ]
        fault = 'transform = { file = "state.txt", replace = "x=ok", with = "x=bad" }'
        repo = fire_repo(
            self,
            entry(
                expect="exit-nonzero",
                run=gate,
                fault=fault,
                message="FAIL  a named case",
            ),
            state="x=ok\n",
        )
        out = repo.run("fire")
        self.assertEqual(out.returncode, 1)
        self.assertIn("weak message: one: the clean run already prints", out.stdout)
        self.assertNotIn("fired: one", out.stdout)

    def test_a_message_in_the_command_line_is_not_the_output(self):
        # The log echoes each argv; a gate whose own argv carries the message must still
        # print it to fire.
        gate = [
            sys.executable,
            "-c",
            "import sys; sys.exit(1 if open('state.txt').read().strip() == 'x=bad' else 0)",
            "the named reason",
        ]
        fault = 'transform = { file = "state.txt", replace = "x=ok", with = "x=bad" }'
        repo = fire_repo(
            self,
            entry(
                expect="exit-nonzero", run=gate, fault=fault, message="the named reason"
            ),
            state="x=ok\n",
        )
        out = repo.run("fire")
        self.assertEqual(out.returncode, 1)
        self.assertIn("never says 'the named reason'", out.stdout)

    def test_colored_runner_output_reads_as_plain(self):
        fault = 'transform = { file = "state.txt", replace = "build=ok", with = "build=E0599" }'
        repo = fire_repo(
            self, entry(fault=fault), state="build=ok color=yes the_guard=ok\n"
        )
        out = repo.run("fire")
        self.assertEqual(out.returncode, 1)
        self.assertIn("DID NOT FIRE: one: the build broke (error[E0599])", out.stdout)
        repo = fire_repo(self, entry(), state="color=yes the_guard=ok\n")
        out = repo.run("fire")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    def test_fire_builds_in_its_own_target_and_ends_on_a_clean_build(self):
        # cargo would trust the scratch tree's builds for the caller's unchanged tree, so
        # they go under the caller's target, not into it, and the run ends on a clean one.
        repo = fire_repo(self, entry())
        out = repo.run("fire", CARGO_TARGET_DIR=str(repo.target))
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("guards: restored, every command passes again", out.stdout)
        own = repo.target / "guards-fire"
        self.assertEqual((own / "built.txt").read_text(), "the_guard=ok\n")
        self.assertEqual((own / "runs").read_text(), "3")
        self.assertFalse((repo.target / "built.txt").exists())
        self.assertFalse((repo.target / "runs").exists())

    def test_target_dir_names_where_cargo_builds(self):
        repo = fire_repo(self, entry())
        out = repo.run("fire", "--target-dir", str(repo.target / "ci"))
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(
            (repo.target / "ci" / "built.txt").read_text(), "the_guard=ok\n"
        )

    def test_a_command_that_is_red_after_the_faults_fails_the_run(self):
        repo = fire_repo(self, entry(), state="the_guard=ok flaky=3\n")
        out = repo.run("fire", CARGO_TARGET_DIR=str(repo.target))
        self.assertEqual(out.returncode, 1)
        self.assertIn("fired: one", out.stdout)
        self.assertIn(
            "already red: one: the command exits 1 with no fault seeded (restored run)",
            out.stdout,
        )
        self.assertIn("the tree didn't come back clean after the faults", out.stdout)
        self.assertIn("stopped with a fault's build in", out.stderr)

    def test_a_command_that_hangs_times_out_and_leaves_no_worktree(self):
        fault = (
            'transform = { file = "state.txt", replace = "hang=no", with = "hang=yes" }'
        )
        repo = fire_repo(self, entry(fault=fault), state="hang=no the_guard=ok\n")
        before = repo.worktrees()
        out = repo.run("fire", "--timeout", "1")
        self.assertEqual(out.returncode, 1)
        self.assertIn("DID NOT FIRE: one: the command failed (124)", out.stdout)
        self.assertIn("guards: timed out after 1 s", out.stdout)
        self.assertEqual(repo.worktrees(), before)

    def test_an_argv_fault_hands_the_gate_an_empty_directory(self):
        gate = [
            sys.executable,
            "-c",
            "import os, sys; d = sys.argv[-1]; "
            "sys.exit(0) if d == 'here' else (print('read nothing in', d), sys.exit(1 if not os.listdir(d) else 0))",
            "here",
        ]
        repo = fire_repo(
            self,
            entry(
                expect="exit-nonzero",
                run=gate,
                fault='argv_fault = ["{empty_dir}"]',
                message="read nothing in",
            ),
        )
        out = repo.run("fire")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("fired: one", out.stdout)

    def test_a_patch_fault_fires_and_a_patch_that_no_longer_applies_is_stale(self):
        patch = textwrap.dedent(
            """\
            --- a/state.txt
            +++ b/state.txt
            @@ -1 +1 @@
            -the_guard=ok
            +the_guard=fail
            """
        )
        repo = Repo(
            self,
            {
                "guards/faults.toml": entry(fault='patch = "guards/faults/one.patch"'),
                "guards/faults/one.patch": patch,
                "state.txt": "the_guard=ok\n",
            },
        )
        out = repo.run("fire")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("fired: one", out.stdout)
        repo.write("state.txt", "moved=ok the_guard=ok\n")
        out = repo.run("fire")
        self.assertEqual(out.returncode, 1)
        self.assertIn(
            "stale anchor: one: guards/faults/one.patch doesn't apply", out.stdout
        )

    def test_the_tree_is_reset_between_faults(self):
        # Both faults anchor on the same text. If the first one's edit survived into the
        # second's run, the second anchor would match nothing.
        second = entry(fid="two")
        repo = fire_repo(self, entry(), second)
        out = repo.run("fire")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("fired: one", out.stdout)
        self.assertIn("fired: two", out.stdout)

    def test_the_callers_uncommitted_changes_are_the_tree_under_test(self):
        repo = fire_repo(self, entry())
        repo.write("state.txt", "the_guard=fail\n")
        out = repo.run("fire")
        self.assertEqual(out.returncode, 1)
        self.assertIn("already red: one", out.stdout)
        # And an untracked entry is read from the working tree.
        repo.write("state.txt", "the_guard=ok\n")
        repo.write("guards/faults.toml", entry(fid="new-one"))
        out = repo.run("fire", "--only", "new-one")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("fired: new-one", out.stdout)

    def test_the_scratch_worktree_is_removed_after_a_run_that_fails(self):
        repo = fire_repo(self, entry(guard="the_gaurd"))
        before = repo.worktrees()
        repo.run("fire")
        self.assertEqual(repo.worktrees(), before)

    def test_an_inherited_git_dir_leaves_that_repo_alone(self):
        decoy = Repo(self, {"decoy.txt": "decoy\n"})
        repo = fire_repo(self, entry())
        out = repo.run("fire", GIT_DIR=str(decoy.root / ".git"))
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(decoy.worktrees(), 1)
        self.assertEqual(git(decoy.root, "status", "--porcelain"), "")

    def test_only_and_suite_select_entries(self):
        repo = fire_repo(self, entry(), entry(fid="two", suite="script"))
        out = repo.run("fire", "--suite", "script")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("fired: two", out.stdout)
        self.assertNotIn("fired: one", out.stdout)
        out = repo.run("fire", "--only", "missing")
        self.assertEqual(out.returncode, 1)
        self.assertIn("no entry has the id missing", out.stderr)

    def test_a_bindings_entry_asked_for_without_a_venv_is_refused(self):
        repo = fire_repo(self, entry(suite="bindings"))
        out = repo.run("fire", "--suite", "bindings")
        self.assertEqual(out.returncode, 1)
        self.assertIn("pass --venv DIR", out.stderr)


def notes_repo(
    test: unittest.TestCase,
    files: dict[str, str],
    faults: str = "",
    unregistered: str = "",
) -> Repo:
    base = {"state.txt": "the_guard=ok\n"}
    registry = faults or entry()
    return Repo(
        test,
        {
            **base,
            **files,
            "guards/faults.toml": registry,
            "guards/unregistered.txt": unregistered,
        },
    )


RUST_NOTE = f"""\
// SPDX-License-Identifier: Apache-2.0
#[cfg(test)]
mod tests {{
    /// Says what it guards.
    ///
    /// {MARKER}: drop the check and this fails.
    #[test]
    fn the_guard() {{}}

    #[test]
    fn unmarked() {{}}
}}
"""


class ListCensus(unittest.TestCase):
    def test_every_note_registered_or_listed_passes(self):
        repo = notes_repo(
            self,
            {"src/lib.rs": RUST_NOTE},
            faults=entry(note="src/lib.rs::the_guard"),
        )
        out = repo.run("list")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("1 notes, 1 with an entry and 0 in", out.stdout)

    def test_a_new_note_with_no_entry_fails(self):
        repo = notes_repo(self, {"src/lib.rs": RUST_NOTE})
        out = repo.run("list")
        self.assertEqual(out.returncode, 1)
        self.assertIn("src/lib.rs::the_guard (src/lib.rs:6) carries a", out.stderr)

    def test_a_listed_note_that_has_an_entry_now_fails(self):
        repo = notes_repo(
            self,
            {"src/lib.rs": RUST_NOTE},
            faults=entry(note="src/lib.rs::the_guard"),
            unregistered="src/lib.rs::the_guard\n",
        )
        out = repo.run("list")
        self.assertEqual(out.returncode, 1)
        self.assertIn("src/lib.rs::the_guard has an entry now (one)", out.stderr)

    def test_a_listed_key_that_is_no_note_fails(self):
        repo = notes_repo(
            self,
            {"src/lib.rs": RUST_NOTE},
            unregistered="src/lib.rs::the_guard  # live: needs AWS\nsrc/lib.rs::gone\n",
        )
        out = repo.run("list")
        self.assertEqual(out.returncode, 1)
        self.assertIn("src/lib.rs::gone in guards/unregistered.txt isn't a", out.stderr)
        self.assertNotIn("the_guard", out.stderr)

    def test_an_entry_naming_no_note_fails(self):
        repo = notes_repo(
            self,
            {"src/lib.rs": RUST_NOTE},
            faults=entry(note="src/lib.rs::renamed"),
            unregistered="src/lib.rs::the_guard\n",
        )
        out = repo.run("list")
        self.assertEqual(out.returncode, 1)
        self.assertIn("one: `note = 'src/lib.rs::renamed'` names no", out.stderr)

    def test_a_near_miss_spelling_fails(self):
        for spelling in (
            "/// **Falsification:** x",
            "/// # Falsification",
            "// Falsification: x",
            "/// **falsification**: x",
            "/// *Falsification*. x",
            "/// __Falsification__: x",
        ):
            with self.subTest(spelling=spelling):
                text = RUST_NOTE.replace("/// Says what it guards.", spelling)
                repo = notes_repo(
                    self, {"src/lib.rs": text}, unregistered="src/lib.rs::the_guard\n"
                )
                out = repo.run("list")
                self.assertEqual(out.returncode, 1)
                self.assertIn(
                    "src/lib.rs:4 spells a note the census can't see", out.stderr
                )

    def test_prose_naming_a_falsification_is_not_a_near_miss(self):
        text = RUST_NOTE.replace(
            "/// Says what it guards.", "/// The falsification: replace the body."
        )
        repo = notes_repo(
            self, {"src/lib.rs": text}, unregistered="src/lib.rs::the_guard\n"
        )
        out = repo.run("list")
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_a_note_on_no_function_fails(self):
        text = RUST_NOTE.replace(
            "    #[test]\n    fn the_guard() {}", "    struct Guard;"
        )
        repo = notes_repo(self, {"src/lib.rs": text})
        out = repo.run("list")
        self.assertEqual(out.returncode, 1)
        self.assertIn("src/lib.rs:6 has a note that isn't on a test", out.stderr)

    def test_a_note_in_a_plain_comment_inside_a_body_fails(self):
        text = RUST_NOTE.replace(
            "fn unmarked() {}", f"fn unmarked() {{\n        // {MARKER}: x\n    }}"
        )
        repo = notes_repo(
            self, {"src/lib.rs": text}, unregistered="src/lib.rs::the_guard\n"
        )
        out = repo.run("list")
        self.assertEqual(out.returncode, 1)
        self.assertIn("src/lib.rs:12 has a note that isn't on a test", out.stderr)

    def test_python_and_node_notes_are_keyed_by_their_test(self):
        python = f'''\
            class TestThing:
                def test_method(self):
                    """{MARKER}: x."""


            def test_function():
                """Guards it.

                {MARKER}: y.
                """
            '''
        node = f"""\
            test('a titled test', () => {{
              // {MARKER}: z.
            }});
            """
        typescript = f"""\
            it("a typed test", (): void => {{
              // {MARKER}: w.
              const n: number = 1;
            }});
            """
        repo = notes_repo(
            self,
            {
                "tests/test_x.py": python,
                "__test__/x.mjs": node,
                "site/tests/x.test.ts": typescript,
            },
            unregistered=(
                "tests/test_x.py::TestThing::test_method\n"
                "tests/test_x.py::test_function\n"
                "__test__/x.mjs::a titled test\n"
                "site/tests/x.test.ts::a typed test\n"
            ),
        )
        out = repo.run("list")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("4 notes, 0 with an entry and 4 in", out.stdout)

    def test_a_typescript_note_outside_a_test_fails(self):
        repo = notes_repo(
            self,
            {"site/src/x.ts": f"// {MARKER}: nothing here.\nexport const x = 1;\n"},
        )
        out = repo.run("list")
        self.assertEqual(out.returncode, 1)
        self.assertIn("site/src/x.ts:1 has a note that isn't on a test", out.stderr)

    def test_a_python_note_in_a_comment_fails(self):
        repo = notes_repo(
            self, {"tests/test_x.py": f"def test_x():\n    # {MARKER}: x\n    pass\n"}
        )
        out = repo.run("list")
        self.assertEqual(out.returncode, 1)
        self.assertIn("tests/test_x.py:2 has a note that isn't on a test", out.stderr)

    def test_an_empty_file_set_fails(self):
        repo = notes_repo(self, {})
        out = repo.run("list")
        self.assertEqual(out.returncode, 1)
        self.assertIn("`git ls-files` returned no *.rs, *.py, *.mjs", out.stderr)

    def test_files_with_no_note_fail(self):
        repo = notes_repo(
            self, {"src/lib.rs": "// SPDX-License-Identifier: Apache-2.0\n"}
        )
        out = repo.run("list")
        self.assertEqual(out.returncode, 1)
        self.assertIn("the census read nothing", out.stderr)

    def test_a_parser_that_returns_nothing_fails(self):
        repo = notes_repo(
            self, {"src/lib.rs": RUST_NOTE}, unregistered="src/lib.rs::the_guard\n"
        )
        stub = repo.bin / "ast-grep"
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
        out = repo.run("list")
        self.assertEqual(out.returncode, 1)
        self.assertIn("src/lib.rs:6 has a note that isn't on a test", out.stderr)

    def test_a_parser_that_fails_fails(self):
        repo = notes_repo(
            self, {"src/lib.rs": RUST_NOTE}, unregistered="src/lib.rs::the_guard\n"
        )
        stub = repo.bin / "ast-grep"
        stub.write_text("#!/bin/sh\necho broken >&2\nexit 2\n")
        stub.chmod(0o755)
        out = repo.run("list")
        self.assertEqual(out.returncode, 1)
        self.assertIn("ast-grep failed", out.stderr)


TWO_NOTES = RUST_NOTE.replace(
    "    #[test]\n    fn unmarked() {}",
    f"    /// {MARKER}: a second one.\n    #[test]\n    fn second() {{}}",
)


class NoteBelongsToItsGuard(unittest.TestCase):
    def test_a_note_on_another_test_fails(self):
        repo = notes_repo(
            self,
            {"src/lib.rs": TWO_NOTES},
            faults=entry(note="src/lib.rs::second"),
            unregistered="src/lib.rs::the_guard\n",
        )
        out = repo.run("list")
        self.assertEqual(out.returncode, 1)
        self.assertIn(
            "one: `note = 'src/lib.rs::second'` is on another test than its guard",
            out.stderr,
        )

    def test_a_note_whose_text_names_the_guard_passes(self):
        # The agentd-model shape: the note is on the spec function, and names the test.
        text = TWO_NOTES.replace("a second one.", "breaking it fails `the_guard`.")
        repo = notes_repo(
            self,
            {"src/lib.rs": text},
            faults=entry(note="src/lib.rs::second"),
            unregistered="src/lib.rs::the_guard\n",
        )
        out = repo.run("list")
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_a_note_on_the_guards_own_test_passes(self):
        repo = notes_repo(
            self,
            {"src/lib.rs": TWO_NOTES},
            faults=entry(guard="tests::the_guard", note="src/lib.rs::the_guard"),
            unregistered="src/lib.rs::second\n",
        )
        out = repo.run("list")
        self.assertEqual(out.returncode, 0, out.stderr)


class UnregisteredOnlyShrinks(unittest.TestCase):
    def repo(self) -> Repo:
        return notes_repo(
            self, {"src/lib.rs": RUST_NOTE}, unregistered="src/lib.rs::the_guard\n"
        )

    def test_a_new_note_appended_to_the_list_fails(self):
        repo = self.repo()
        repo.write("src/lib.rs", TWO_NOTES)
        repo.write(
            "guards/unregistered.txt", "src/lib.rs::the_guard\nsrc/lib.rs::second\n"
        )
        out = repo.run("list")
        self.assertEqual(out.returncode, 1)
        self.assertIn(
            "src/lib.rs::second is in guards/unregistered.txt but not in", out.stderr
        )
        # Committed on the branch, it still fails: the base is the merge base.
        repo.commit()
        out = repo.run("list")
        self.assertEqual(out.returncode, 1)
        self.assertIn("src/lib.rs::second is in guards/unregistered.txt", out.stderr)
        # And an explicit base that already lists it passes.
        out = repo.run("list", "--base", "HEAD")
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_a_renamed_test_keeps_its_place(self):
        repo = self.repo()
        repo.write("src/lib.rs", RUST_NOTE.replace("fn the_guard()", "fn renamed()"))
        repo.write("guards/unregistered.txt", "src/lib.rs::renamed\n")
        out = repo.run("list")
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_a_moved_test_keeps_its_place(self):
        repo = self.repo()
        git(repo.root, "mv", "src/lib.rs", "src/moved.rs")
        repo.write("guards/unregistered.txt", "src/moved.rs::the_guard\n")
        out = repo.run("list")
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_registering_one_note_does_not_make_room_for_another(self):
        repo = self.repo()
        repo.write("src/lib.rs", TWO_NOTES)
        repo.write("guards/faults.toml", entry(note="src/lib.rs::the_guard"))
        repo.write("guards/unregistered.txt", "src/lib.rs::second\n")
        out = repo.run("list")
        self.assertEqual(out.returncode, 1)
        self.assertIn("src/lib.rs::second is in guards/unregistered.txt", out.stderr)

    def test_a_base_with_no_list_is_the_bootstrap(self):
        repo = self.repo()
        (repo.root / "guards/unregistered.txt").unlink()
        repo.commit("no list yet")
        git(repo.root, "update-ref", "refs/remotes/origin/main", "HEAD")
        repo.write("guards/unregistered.txt", "src/lib.rs::the_guard\n")
        out = repo.run("list")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("the bootstrap", out.stdout)

    def test_no_base_to_compare_with_fails(self):
        repo = self.repo()
        git(repo.root, "update-ref", "-d", "refs/remotes/origin/main")
        out = repo.run("list")
        self.assertEqual(out.returncode, 1)
        self.assertIn("no merge base of HEAD and origin/main", out.stderr)
        out = repo.run("list", "--base", "nowhere")
        self.assertEqual(out.returncode, 1)
        self.assertIn("--base nowhere doesn't name a commit", out.stderr)


class RegistryShape(unittest.TestCase):
    def check(self, registry: str, message: str) -> None:
        repo = notes_repo(
            self,
            {"src/lib.rs": RUST_NOTE},
            faults=registry,
            unregistered="src/lib.rs::the_guard\n",
        )
        out = repo.run("list")
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn(message, out.stderr)

    def test_an_empty_registry_fails(self):
        self.check("# nothing\n", "has no [[fault]] entry")

    def test_exit_nonzero_needs_a_message(self):
        self.check(entry(expect="exit-nonzero", run=["./gate"]), "needs a `message`")

    def test_lint_error_needs_a_message(self):
        self.check(
            entry(expect="lint-error", run=["cargo", "clippy"]), "needs a `message`"
        )

    def test_compile_error_needs_a_code(self):
        self.check(entry(expect="compile-error"), "needs the rustc `code`")

    def test_a_cargo_test_run_needs_exact(self):
        self.check(entry(run=["cargo", "test", "the_guard"]), "needs `--exact`")

    def test_pytest_and_node_runs_need_their_report_flags(self):
        self.check(entry(run=["pytest", "tests"]), "needs `-rA`")
        self.check(
            entry(run=["node", "--test", "x.mjs"]), "needs `--test-reporter=tap`"
        )

    def test_test_failed_needs_a_test_runner(self):
        self.check(entry(run=["./gate"]), "needs the last command to be a cargo test")

    def test_one_fault_per_entry(self):
        self.check(entry() + 'patch = "x.patch"\n', "exactly one of")
        self.check(entry(fault=""), "exactly one of")

    def test_an_id_used_twice_fails(self):
        self.check(entry() + entry(), "the id is used twice")

    def test_an_unknown_key_fails(self):
        self.check(entry(guards="x"), "unknown key 'guards'")


TIMING = re.compile(r"\(\d+\.\d s(?: of faults)?\)")


def verdicts(stdout: str) -> list[str]:
    """The run's lines without timings or the parallel run's own worker line."""
    return [
        TIMING.sub("(t)", line)
        for line in stdout.splitlines()
        if not re.match(r"guards: \d+ workers, ", line)
    ]


def keyed(fid: str, guard: str, anchor: str, **extra: str) -> str:
    """An entry with its own command and its own state word, so it's its own clean run."""
    fault = f'transform = {{ file = "state.txt", replace = "{anchor}=ok", with = "{anchor}=fail" }}'
    return entry(
        fid=fid,
        guard=guard,
        run=["cargo", "test", "--", "--exact", guard],
        fault=fault,
        **extra,
    )


class FireInParallel(unittest.TestCase):
    def scratch_tmp(self) -> Path:
        # Each worker's tree and extra target live under the temp directory, so a test
        # that owns it can see what's left there after a run.
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return Path(directory.name)

    def mixed(self) -> Repo:
        # Every verdict, in one registry: fired (the first one slowest, so a parallel run
        # finishes it last), did not fire, stale anchor, build broke, compile error.
        slow = entry(
            fid="slow",
            guard="g1",
            run=["cargo", "test", "--", "--exact", "g1"],
            fault='transform = { file = "state.txt", replace = "g1=ok", with = "g1=fail slow=8" }',
        )
        return fire_repo(
            self,
            slow,
            keyed("quick", "g2", "g2"),
            keyed("misses", "g3", "other"),
            keyed("stale", "g4", "gone"),
            entry(
                fid="breaks",
                guard="g5",
                run=["cargo", "test", "--", "--exact", "g5"],
                fault='transform = { file = "state.txt", replace = "build=ok", with = "build=E0599" }',
            ),
            entry(
                fid="compiles",
                expect="compile-error",
                code="E0599",
                run=["cargo", "test", "--no-run"],
                fault='transform = { file = "state.txt", replace = "build=ok", with = "build=E0599" }',
            ),
            state="g1=ok g2=ok g3=ok g4=ok g5=ok other=ok build=ok\n",
        )

    def test_jobs_report_the_serial_verdicts_in_the_serial_order(self):
        repo = self.mixed()
        serial = repo.run("fire")
        self.assertEqual(serial.returncode, 1, serial.stdout + serial.stderr)
        for jobs in ("2", "4"):
            with self.subTest(jobs=jobs):
                parallel = repo.run("fire", "--jobs", jobs)
                self.assertEqual(parallel.returncode, serial.returncode)
                self.assertEqual(verdicts(parallel.stdout), verdicts(serial.stdout))
                self.assertIn(f"guards: {jobs} workers, ", parallel.stdout)
        lines = verdicts(serial.stdout)
        self.assertIn("fired: slow (t)", lines)
        self.assertIn("fired: compiles (t)", lines)
        self.assertIn("guards: 3 of 6 fired (t)", lines)

    def test_a_fault_that_does_not_fire_is_still_did_not_fire(self):
        repo = fire_repo(
            self,
            keyed("a", "g1", "g1"),
            keyed("b", "g2", "other"),
            keyed("c", "g3", "g3"),
            state="g1=ok g2=ok g3=ok other=ok\n",
        )
        out = repo.run("fire", "--jobs", "3")
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertIn(
            "DID NOT FIRE: b: the command passed with the fault seeded", out.stdout
        )
        self.assertNotIn("fired: b", out.stdout)
        self.assertIn("guards: 2 of 3 fired", out.stdout)

    def test_each_worker_builds_in_its_own_target_and_only_the_first_one_stays(self):
        tmp = self.scratch_tmp()
        log = tmp.parent / (tmp.name + ".log")
        self.addCleanup(lambda: log.unlink(missing_ok=True))
        repo = fire_repo(
            self,
            keyed("a", "g1", "g1"),
            keyed("b", "g2", "g2"),
            keyed("c", "g3", "g3"),
            state="g1=ok g2=ok g3=ok\n",
        )
        out = repo.run(
            "fire",
            "--jobs",
            "3",
            CARGO_TARGET_DIR=str(repo.target),
            TMPDIR=str(tmp),
            FAKE_LOG=str(log),
        )
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        runs = [line.split("\t")[:4] for line in log.read_text().splitlines()]
        targets = {target for target, _, _, _ in runs}
        trees = {tree for _, _, tree, _ in runs}
        self.assertEqual(len(targets), 3, runs)
        self.assertEqual(len(trees), 3, runs)
        own = str(repo.target / "guards-fire")
        self.assertIn(own, targets)
        # Each worker's tree has one target: none builds into another's.
        self.assertEqual(len({(t, w) for t, _, w, _ in runs}), 3, runs)
        # The extra targets and every tree are gone, and the first target ends on a clean
        # build, as a serial run's does.
        self.assertEqual(list(tmp.iterdir()), [])
        self.assertEqual(repo.worktrees(), 1)
        self.assertEqual((Path(own) / "built.txt").read_text(), "g1=ok g2=ok g3=ok\n")
        # Every tree ran each of its commands again after its faults.
        for tree in trees:
            kinds = [k for _, _, w, k in runs if w == tree]
            self.assertEqual(kinds[-1], "clean", runs)

    def test_bindings_entries_all_run_in_the_first_workers_target_and_venv(self):
        tmp = self.scratch_tmp()
        log = tmp.parent / (tmp.name + ".log")
        self.addCleanup(lambda: log.unlink(missing_ok=True))
        entries = [
            entry(
                fid=f"py{n}",
                guard=f"tests/t.py::t{n}",
                run=["pytest", "-rA", f"tests/t.py::t{n}"],
                suite="bindings",
                fault=f'transform = {{ file = "state.txt", replace = "tests/t.py::t{n}=ok", with = "tests/t.py::t{n}=fail" }}',
            )
            for n in range(3)
        ]
        repo = fire_repo(
            self,
            *entries,
            keyed("r", "g1", "g1"),
            state="tests/t.py::t0=ok tests/t.py::t1=ok tests/t.py::t2=ok g1=ok\n",
        )
        venv = tmp / "venv"
        out = repo.run(
            "fire",
            "--jobs",
            "4",
            "--venv",
            str(venv),
            CARGO_TARGET_DIR=str(repo.target),
            FAKE_LOG=str(log),
        )
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        runs = [line.split("\t")[:4] for line in log.read_text().splitlines()]
        bound = {(t, w) for t, v, w, _ in runs if v == str(venv)}
        self.assertEqual(len(bound), 1, runs)
        self.assertEqual(next(iter(bound))[0], str(repo.target / "guards-fire"))

    def test_a_red_clean_run_stops_every_worker_before_any_fault(self):
        repo = fire_repo(
            self,
            keyed("a", "g1", "g1"),
            keyed("b", "g2", "g2"),
            keyed("c", "g3", "g3"),
            state="g1=ok g2=fail g3=ok\n",
        )
        out = repo.run("fire", "--jobs", "3")
        self.assertEqual(out.returncode, 1)
        self.assertIn(
            "already red: b: the command exits 1 with no fault seeded", out.stdout
        )
        self.assertNotIn("fired:", out.stdout)
        self.assertNotIn("DID NOT FIRE", out.stdout)
        self.assertEqual(repo.worktrees(), 1)

    def test_a_signal_ends_every_command_and_removes_every_tree_and_target(self):
        tmp = self.scratch_tmp()
        pids = tmp.parent / (tmp.name + ".pids")
        pids.mkdir()
        self.addCleanup(shutil.rmtree, pids, True)
        hang = (
            'transform = { file = "state.txt", replace = "hang=no", with = "hang=yes" }'
        )
        repo = fire_repo(
            self,
            entry(fid="hangs", fault=hang),
            keyed("b", "g2", "g2"),
            state="hang=no the_guard=ok g2=ok\n",
        )
        path = os.pathsep.join([str(repo.bin), os.environ.get("PATH", "")])
        proc = subprocess.Popen(
            [
                sys.executable,
                str(SCRIPT),
                "--root",
                str(repo.root),
                "fire",
                "--jobs",
                "2",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=clean_env(
                PATH=path,
                CARGO_TARGET_DIR=str(repo.target),
                TMPDIR=str(tmp),
                FAKE_PIDS=str(pids),
            ),
        )
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        deadline = time.monotonic() + 30
        while len(list(pids.iterdir())) < 2 and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertEqual(len(list(pids.iterdir())), 2, "the fault never started")
        self.assertEqual(repo.worktrees(), 3)
        proc.send_signal(signal.SIGTERM)
        _, err = proc.communicate(timeout=20)
        self.assertEqual(proc.returncode, 128 + signal.SIGTERM, err)
        self.assertIn("stopped with a fault's build in", err)
        self.assertEqual(repo.worktrees(), 1)
        self.assertEqual(list(tmp.iterdir()), [])
        # The hung runner and the child it started are both gone.
        for pid in (int(p.name) for p in pids.iterdir()):
            deadline = time.monotonic() + 5
            while alive(pid) and time.monotonic() < deadline:
                time.sleep(0.1)
            self.assertFalse(alive(pid), f"pid {pid} outlived the run")

    def stealing(self, state: str) -> tuple[Repo, Path]:
        """Three slow faults on one command and one quick fault on another, over two workers:
        worker 2 finishes its own and takes the last slow one from worker 1's queue."""
        tmp = self.scratch_tmp()
        log = tmp.parent / (tmp.name + ".log")
        self.addCleanup(lambda: log.unlink(missing_ok=True))
        slow = [
            entry(
                fid=f"a{n}",
                guard="g1",
                run=["cargo", "test", "--", "--exact", "g1"],
                fault=f'transform = {{ file = "state.txt", replace = "s{n}=ok", with = "s{n}=ok g1=fail slow=10" }}',
            )
            for n in (1, 2, 3)
        ]
        repo = fire_repo(self, *slow, keyed("b", "g2", "g2"), state=state)
        return repo, log

    def runs_of(self, repo: Repo, log: Path) -> list[list[str]]:
        rows = [line.split("\t") for line in log.read_text().splitlines()]
        own = str(repo.target / "guards-fire")
        # The premise: worker 2 took one of worker 1's slow faults.
        stolen = [
            r for r in rows if r[0] != own and r[3] == "seeded" and r[4].endswith(" g1")
        ]
        self.assertEqual(len(stolen), 1, rows)
        return rows

    def test_a_worker_that_takes_a_fault_runs_its_command_clean_again(self):
        repo, log = self.stealing("g1=ok g2=ok s1=ok s2=ok s3=ok\n")
        out = repo.run("fire", "--jobs", "2", FAKE_LOG=str(log))
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        rows = self.runs_of(repo, log)
        # Each tree's last run of each command it ran is clean, the taken one included.
        for tree in {r[2] for r in rows}:
            for command in {r[4] for r in rows if r[2] == tree}:
                last = [r[3] for r in rows if r[2] == tree and r[4] == command][-1]
                self.assertEqual(last, "clean", (tree, command, rows))

    def test_a_restored_run_red_in_one_worker_only_fails_the_run(self):
        # `flaky=5` fails the fifth run in a target, which only worker 2's reaches: its clean
        # g2, its fault, the fault it took, its restored g2, then its restored g1.
        repo, log = self.stealing("g1=ok g2=ok s1=ok s2=ok s3=ok flaky=5\n")
        out = repo.run("fire", "--jobs", "2", FAKE_LOG=str(log))
        self.runs_of(repo, log)
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertIn(
            "already red: a1: the command exits 1 with no fault seeded (restored run)",
            out.stdout,
        )
        self.assertIn("the tree didn't come back clean after the faults", out.stdout)

    def test_jobs_below_one_are_refused(self):
        out = fire_repo(self, entry()).run("fire", "--jobs", "0")
        self.assertEqual(out.returncode, 1)
        self.assertIn("--jobs needs a count of 1 or more", out.stderr)


def alive(pid: int) -> bool:
    """Whether `pid` runs. `kill -0` answers everywhere; where /proc exists, a zombie waiting
    for init to reap it counts as gone. Without /proc (macOS) a zombie reads as alive, so the
    check can fail there too rather than pass because nothing could be read."""
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


CRATE_TOML = '[package]\nname = "fixture"\nversion = "0.0.0"\n'


def affected_repo(test: unittest.TestCase, extra: str = "") -> Repo:
    """Entries each reached through one kind of file: a transform file, a patch, a gate
    script in the argv, a pytest node id, a cargo test in a `-p` crate, a lint whose guard is
    the crate's clippy.toml."""
    patch = textwrap.dedent(
        """\
        --- a/state-p.txt
        +++ b/state-p.txt
        @@ -1,2 +1,2 @@
        -pp=ok
        +pp=fail
         keep=ok
        """
    )
    gate = [sys.executable, "gate.py"]
    registry = "".join(
        [
            entry(
                fid="a",
                guard="ga",
                run=["cargo", "test", "--", "--exact", "ga"],
                fault='transform = { file = "state-a.txt", replace = "ga=ok", with = "ga=fail" }',
            ),
            entry(
                fid="p",
                guard="pp",
                run=["cargo", "test", "--", "--exact", "pp"],
                fault='patch = "guards/faults/p.patch"',
            ),
            entry(
                fid="gate",
                guard="gate.py",
                run=gate,
                expect="exit-nonzero",
                message="gate says no",
                fault='transform = { file = "state-g.txt", replace = "g=ok", with = "g=bad" }',
            ),
            entry(
                fid="py",
                guard="tests/test_e.py::test_e",
                run=["pytest", "-rA", "tests/test_e.py::test_e"],
                fault='transform = { file = "state-e.txt", replace = "tests/test_e.py::test_e=ok", with = "tests/test_e.py::test_e=fail" }',
            ),
            entry(
                fid="crate",
                guard="tests::the_d_guard",
                run=[
                    "cargo",
                    "test",
                    "-p",
                    "fixture",
                    "--",
                    "--exact",
                    "tests::the_d_guard",
                ],
                fault='transform = { file = "state-d.txt", replace = "tests::the_d_guard=ok", with = "tests::the_d_guard=fail" }',
            ),
            # A lint entry's guard is a ban in the crate's clippy.toml, which its `guard`
            # words name only in prose, as the real ones do.
            entry(
                fid="lint",
                guard="fixture clippy.toml: the ban on the_lint",
                run=["cargo", "clippy", "-p", "fixture", "--", "the_lint"],
                expect="lint-error",
                message="test the_lint ... FAILED",
                fault='transform = { file = "state-l.txt", replace = "the_lint=ok", with = "the_lint=fail" }',
            ),
            extra,
        ]
    )
    repo = Repo(
        test,
        {
            "guards/faults.toml": registry,
            "guards/faults/p.patch": patch,
            "state-a.txt": "ga=ok\n",
            "state-p.txt": "pp=ok\nkeep=ok\n",
            "state-g.txt": "g=ok\n",
            "state-e.txt": "tests/test_e.py::test_e=ok\n",
            "state-d.txt": "tests::the_d_guard=ok\n",
            "gate.py": "import sys\nbad = 'g=bad' in open('state-g.txt').read()\n"
            "bad and print('gate says no')\nsys.exit(1 if bad else 0)\n",
            "tests/test_e.py": "def test_e():\n    pass\n",
            "crates/fixture/Cargo.toml": CRATE_TOML,
            "crates/fixture/src/lib.rs": "mod tests {\n    fn the_d_guard() {}\n}\n",
            "crates/fixture/src/other.rs": "fn unrelated() {}\n",
            "crates/fixture/clippy.toml": 'disallowed-methods = ["the_lint"]\n',
            "state-l.txt": "the_lint=ok\n",
        },
    )
    return repo


ORDER = ["a", "p", "gate", "py", "crate", "lint"]
ALL = set(ORDER)
OWED = "the full fire (CI's guards job) is still owed before a push"


def selection(stdout: str) -> set[str]:
    return set(re.findall(r"^affected: ([a-z0-9-]+): ", stdout, re.MULTILINE))


class FireAffected(unittest.TestCase):
    def check(self, repo: Repo, want: set[str], *reasons: str) -> str:
        out = repo.run("fire", "--affected")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(selection(out.stdout), want, out.stdout)
        skipped = sorted(ALL - want, key=ORDER.index)
        self.assertIn(
            f"selects {len(want)} and skips {len(skipped)} entries"
            + (f": {', '.join(skipped)}" if skipped else ""),
            out.stdout,
        )
        # A run that skipped anything says it isn't the full fire; one that skipped nothing
        # has nothing to add.
        (self.assertIn if skipped else self.assertNotIn)(OWED, out.stdout)
        for reason in reasons:
            self.assertIn(reason, out.stdout)
        return out.stdout

    def test_a_changed_seeded_file_selects_only_its_entries(self):
        repo = affected_repo(self)
        repo.write("state-a.txt", "ga=ok extra=ok\n")
        repo.write("state-p.txt", "pp=ok\nkeep=ok\nextra=ok\n")
        self.check(repo, {"a", "p"}, "affected: a: state-a.txt changed")

    def test_a_changed_guard_file_selects_its_entry(self):
        repo = affected_repo(self)
        repo.write("gate.py", (repo.root / "gate.py").read_text() + "# a comment\n")
        repo.write("tests/test_e.py", "def test_e():\n    assert True\n")
        repo.write(
            "crates/fixture/src/lib.rs", "mod tests {\n    fn the_d_guard() { () }\n}\n"
        )
        self.check(
            repo,
            {"gate", "py", "crate"},
            "affected: gate: gate.py changed",
            "affected: crate: crates/fixture/src/lib.rs changed",
        )

    def test_another_file_in_the_crate_selects_nothing(self):
        repo = affected_repo(self)
        repo.write("crates/fixture/src/other.rs", "fn unrelated() { () }\n")
        out = self.check(repo, set())
        self.assertIn("nothing to fire", out)

    def test_a_changed_or_new_entry_is_affected(self):
        repo = affected_repo(self)
        text = (repo.root / "guards/faults.toml").read_text()
        text = text.replace('guard = "ga"', 'guard = "ga"\nmessage = "FAILED"', 1)
        text += entry(
            fid="new",
            guard="nn",
            run=["cargo", "test", "--", "--exact", "nn"],
            fault='transform = { file = "state-a.txt", replace = "ga=ok", with = "ga=ok nn=fail" }',
        )
        repo.write("guards/faults.toml", text)
        out = repo.run("fire", "--affected")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(selection(out.stdout), {"a", "new"}, out.stdout)
        self.assertIn(
            "affected: a: the entry changed in guards/faults.toml", out.stdout
        )
        self.assertIn(
            "affected: new: the entry is new in guards/faults.toml", out.stdout
        )

    def test_a_change_committed_on_the_branch_still_counts(self):
        repo = affected_repo(self)
        repo.write("state-a.txt", "ga=ok extra=ok\n")
        repo.commit()
        self.check(repo, {"a"})

    def test_a_change_to_this_script_selects_every_entry(self):
        repo = affected_repo(self)
        repo.write("scripts/check-guards-fire.py", "# stands in for this script\n")
        repo.commit("base with the script")
        git(repo.root, "update-ref", "refs/remotes/origin/main", "HEAD")
        repo.write("scripts/check-guards-fire.py", "# changed\n")
        self.check(repo, ALL, "scripts/check-guards-fire.py changed")

    def test_affected_reports_the_serial_verdicts_for_what_it_selects(self):
        # And a fault that doesn't fire is still reported so: `misses` seeds a word the
        # guard never reads.
        misses = entry(
            fid="misses",
            guard="ga",
            run=["cargo", "test", "--", "--exact", "ga"],
            fault='transform = { file = "state-a.txt", replace = "ga=ok", with = "ga=ok other=fail" }',
        )
        repo = affected_repo(self, extra=misses)
        repo.write("state-a.txt", "ga=ok extra=ok\n")
        chosen = repo.run("fire", "--affected", "--jobs", "2")
        self.assertEqual(chosen.returncode, 1, chosen.stdout + chosen.stderr)
        self.assertEqual(selection(chosen.stdout), {"a", "misses"})
        self.assertIn("DID NOT FIRE: misses: the command passed", chosen.stdout)
        serial = repo.run("fire", "--only", "a", "--only", "misses")
        self.assertEqual(serial.returncode, 1)
        picked = [
            line
            for line in verdicts(chosen.stdout)
            if not line.startswith(("affected: ", "guards: --affected "))
        ]
        self.assertEqual(picked, verdicts(serial.stdout))

    def test_a_changed_script_selects_the_unit_suite_that_tests_it(self):
        suite = textwrap.dedent(
            """\
            import unittest


            class T(unittest.TestCase):
                def test_y(self):
                    self.assertNotIn("y=bad", open("state-y.txt").read())
            """
        )
        run = [sys.executable, "-m", "unittest", "discover", "-s", "scripts"]
        repo = Repo(
            self,
            {
                "guards/faults.toml": entry(
                    fid="suite",
                    guard="test_gate_y.py",
                    run=[*run, "-p", "test_gate_y.py"],
                    expect="exit-nonzero",
                    message="FAIL: test_y",
                    fault='transform = { file = "state-y.txt", replace = "y=ok", with = "y=bad" }',
                ),
                "scripts/test_gate_y.py": suite,
                "scripts/check-gate-y.py": "# the script the suite tests\n",
                "scripts/other.py": "# tested by no suite\n",
                "state-y.txt": "y=ok\n",
            },
        )
        repo.write("scripts/other.py", "# changed\n")
        out = repo.run("fire", "--affected")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(selection(out.stdout), set(), out.stdout)
        repo.write("scripts/check-gate-y.py", "# changed\n")
        out = repo.run("fire", "--affected")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("affected: suite: scripts/check-gate-y.py changed", out.stdout)
        self.assertIn("fired: suite", out.stdout)

    def test_a_changed_clippy_toml_selects_its_lint_entries(self):
        repo = affected_repo(self)
        repo.write(
            "crates/fixture/clippy.toml", 'disallowed-methods = ["the_lint", "more"]\n'
        )
        self.check(repo, {"lint"}, "affected: lint: crates/fixture/clippy.toml changed")

    def test_an_untracked_file_counts(self):
        # Agents here never commit, so every new file is untracked while --affected runs.
        # The guard moves to a new file: only that file names it now.
        repo = affected_repo(self)
        repo.write("crates/fixture/src/lib.rs", "mod tests;\n")
        repo.write("crates/fixture/src/tests.rs", "fn the_d_guard() {}\n")
        self.check(
            repo, {"crate"}, "affected: crate: crates/fixture/src/tests.rs changed"
        )

    def test_a_changed_patch_selects_its_entry(self):
        # The patch seeds something else now; neither the registry text nor the file it
        # touches changed.
        repo = affected_repo(self)
        text = (repo.root / "guards/faults/p.patch").read_text()
        repo.write("guards/faults/p.patch", text.replace("+pp=fail", "+pp=fail x=1"))
        self.check(repo, {"p"}, "affected: p: guards/faults/p.patch changed")

    def test_a_renamed_file_selects_by_its_old_name(self):
        # An entry that seeds the old name is a stale anchor now, and has to be selected
        # to say so.
        repo = affected_repo(self)
        git(repo.root, "mv", "state-a.txt", "state-a2.txt")
        repo.commit("rename")
        out = repo.run("fire", "--affected")
        self.assertEqual(selection(out.stdout), {"a"}, out.stdout)
        self.assertIn("affected: a: state-a.txt changed", out.stdout)
        self.assertIn("stale anchor: a:", out.stdout)

    def test_a_build_input_selects_every_entry(self):
        for path, text in (
            ("Cargo.lock", "version = 4\n"),
            ("crates/fixture/Cargo.toml", CRATE_TOML + 'edition = "2024"\n'),
            ("mise.toml", '[env]\nX = "1"\n'),
        ):
            with self.subTest(path=path):
                repo = affected_repo(self)
                repo.write(path, text)
                self.check(
                    repo, ALL, f"{path} changed, and every build or command reads it"
                )

    def test_an_affected_selection_the_missing_venv_empties_exits_like_nothing_affected(
        self,
    ):
        binding = entry(
            fid="js",
            guard="t",
            run=["node", "--test", "--test-reporter=tap", "t.mjs"],
            suite="bindings",
            fault='transform = { file = "state-js.txt", replace = "t=ok", with = "t=fail" }',
        )
        repo = affected_repo(self, extra=binding)
        repo.write("state-js.txt", "t=ok x=ok\n")
        out = repo.run("fire", "--affected")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(selection(out.stdout), {"js"}, out.stdout)
        self.assertIn(
            "no affected entry runs without --venv, so nothing to fire", out.stdout
        )

    def test_a_script_a_tested_script_loads_selects_the_suite(self):
        # ci-local.py runs from check-ci-parity.py's plan(), so test_ci_local.py's verdict
        # moves when check-ci-parity.py changes.
        suite = textwrap.dedent(
            """\
            import unittest


            class T(unittest.TestCase):
                def test_y(self):
                    self.assertNotIn("y=bad", open("state-y.txt").read())
            """
        )
        run = [sys.executable, "-m", "unittest", "discover", "-s", "scripts"]
        repo = Repo(
            self,
            {
                "guards/faults.toml": entry(
                    fid="suite",
                    guard="test_gate_y.py",
                    run=[*run, "-p", "test_gate_y.py"],
                    expect="exit-nonzero",
                    message="FAIL: test_y",
                    fault='transform = { file = "state-y.txt", replace = "y=ok", with = "y=bad" }',
                ),
                "scripts/test_gate_y.py": suite,
                "scripts/gate-y.py": "import runpy\nfrom pathlib import Path\n"
                'PLAN = runpy.run_path(str(Path(__file__).with_name("plan-y.py")))\n',
                "scripts/plan-y.py": "import helper_y\n",
                "scripts/helper_y.py": "# imported by plan-y.py\n",
                "scripts/other.py": "# loaded by nothing\n",
                "state-y.txt": "y=ok\n",
            },
        )
        repo.write("scripts/other.py", "# changed\n")
        out = repo.run("fire", "--affected")
        self.assertEqual(selection(out.stdout), set(), out.stdout)
        for path in ("scripts/plan-y.py", "scripts/helper_y.py"):
            with self.subTest(path=path):
                git(repo.root, "checkout", "--", ".")
                repo.write(path, "# changed\n")
                out = repo.run("fire", "--affected")
                self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
                self.assertIn(f"affected: suite: {path} changed", out.stdout)

    def test_no_merge_base_fails_and_base_needs_affected(self):
        repo = affected_repo(self)
        git(repo.root, "update-ref", "-d", "refs/remotes/origin/main")
        out = repo.run("fire", "--affected")
        self.assertEqual(out.returncode, 1)
        self.assertIn("no merge base of HEAD and origin/main", out.stderr)
        out = repo.run("fire", "--affected", "--base", "HEAD")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        out = repo.run("fire", "--base", "HEAD")
        self.assertEqual(out.returncode, 1)
        self.assertIn("--base is for --affected", out.stderr)


if __name__ == "__main__":
    unittest.main()
