# SPDX-License-Identifier: Apache-2.0
"""Tests for `scripts/check-guards-fire.py`: when a seeded fault counts as fired (#274).

Each case builds a throwaway git repository with a registry and a fake test runner on PATH
named `cargo`, `pytest` or `node`. The fake reads `state.txt` in the tree it runs in and prints
the line the real runner would, so every verdict branch is driven through the real script,
the scratch worktree included. The census cases run the real ast-grep, so run this through
`mise run guards:list`.
"""

import os
import stat
import subprocess
import sys
import tempfile
import textwrap
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

# One runner for all three names. `state.txt` holds `name=value` words: `build=E0599` breaks
# the build with that code, `<test>=fail` fails that test, `color=yes` wraps each line in ANSI
# codes the way CI's `CARGO_TERM_COLOR=always` does, and `hang=yes` sleeps past any timeout.
# With CARGO_TARGET_DIR set, the runner stands in for a build there: `built.txt` records the
# state.txt it last ran on, `runs` counts runs, and `flaky=N` fails the Nth run.
FAKE_RUNNER = """\
#!{python}
import os
import pathlib
import sys
import time

state = dict(w.split("=", 1) for w in pathlib.Path("state.txt").read_text().split())
tool = pathlib.Path(sys.argv[0]).name


def say(line):
    print(f"\\x1b[1m{{line}}\\x1b[0m" if state.get("color") == "yes" else line)


if os.environ.get("CARGO_TARGET_DIR"):
    target = pathlib.Path(os.environ["CARGO_TARGET_DIR"])
    target.mkdir(parents=True, exist_ok=True)
    runs = target / "runs"
    count = int(runs.read_text()) + 1 if runs.exists() else 1
    runs.write_text(str(count))
    (target / "built.txt").write_text(pathlib.Path("state.txt").read_text())
    if state.get("flaky") == str(count):
        say(f"run {{count}} went red")
        sys.exit(1)
if state.get("hang") == "yes":
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


if __name__ == "__main__":
    unittest.main()
