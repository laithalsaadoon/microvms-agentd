# SPDX-License-Identifier: Apache-2.0
"""Tests for `scripts/check-guards-fire.py`: when a seeded fault counts as fired (#274).

Each case builds a throwaway git repository with a registry and a fake test runner on PATH
named `cargo`, `pytest` or `node`. The fake reads `state.txt` in the tree it runs in and prints
the line the real runner would, so every verdict branch is driven through the real script,
the scratch worktree included. The census cases run the real ast-grep, so run this through
`mise run guards:list`.
"""

import json
import os
import re
import runpy
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import tomllib
import types
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

# One runner for all three names. `cargo metadata` prints the tree's `metadata.json` (exit 101
# without one). It reads every `state*.txt` in the tree it runs in as
# `name=value` words: `build=E0599` breaks the build with that code, `<test>=fail` fails that
# test, `color=yes` wraps each line in ANSI codes the way CI's `CARGO_TERM_COLOR=always` does,
# `slow=N` sleeps N tenths of a second, and `hang=yes` sleeps past any timeout (with a child
# that sleeps too, and both pids written under $FAKE_PIDS when it's set). `write=F` writes
# the file F into the tree, and `absent=F` fails the run when F is there. With
# CARGO_TARGET_DIR set, the runner stands in for a build there: `built.txt` records the state
# it last ran on, `runs` counts runs, and `flaky=N` fails the Nth run; $FAKE_DEPS, when set,
# gets the target and the names in its `debug/deps` before each run. $FAKE_LOG, when set,
# gets a line per run: the target, the venv, the tree, whether the tree was seeded, and the
# arguments. $FAKE_SPANS, when set, gets a `start` and an `end` line per run, each with the
# time, the tree, whether it was seeded, and the arguments.
#
# As `cargo clippy`, it also reads the tree's `.rs` files for what rustc would report: a line
# holding `LINT(text)` gets an error `text` there (a clippy ban's), `LINT@N(text)` the same
# error at line N instead, `WARN(text)` a warning, and `BROKEN(E0425)` a compile error with
# that code. Any error exits 101. With `--message-format=json` each is a cargo
# `compiler-message` record on one line, and `GARBLED()` prints a line that isn't JSON.
FAKE_RUNNER = """\
#!{python}
import atexit
import json
import os
import pathlib
import re
import subprocess
import sys
import time

texts = [p.read_text() for p in sorted(pathlib.Path(".").glob("state*.txt"))]
state = dict(w.split("=", 1) for w in " ".join(texts).split())
tool = pathlib.Path(sys.argv[0]).name
marks = []
if tool == "cargo" and "clippy" in sys.argv[1:]:
    for path in sorted(pathlib.Path(".").rglob("*.rs")):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            for kind, at, text in re.findall(r"(LINT|WARN|BROKEN|GARBLED)(?:@(\\d+))?\\(([^)]*)\\)", line):
                marks.append((kind, path.as_posix(), int(at or number), text))
seeded = bool(marks) or any(
    v not in ("ok", "no") for k, v in state.items() if k not in ("slow", "flaky", "absent")
)
if os.environ.get("FAKE_SPANS"):
    def span(edge):
        with open(os.environ["FAKE_SPANS"], "a") as log:
            log.write("\\t".join([
                edge, repr(time.time()), os.getcwd(), "seeded" if seeded else "clean",
                " ".join(sys.argv[1:]),
            ]) + "\\n")
    span("start")
    atexit.register(span, "end")
if tool == "cargo" and sys.argv[1:2] == ["metadata"]:
    meta = pathlib.Path("metadata.json")
    if not meta.is_file():
        sys.exit(101)
    print(meta.read_text())
    sys.exit(0)
if os.environ.get("FAKE_DEPS") and os.environ.get("CARGO_TARGET_DIR"):
    deps = pathlib.Path(os.environ["CARGO_TARGET_DIR"], "debug", "deps")
    names = sorted(p.name for p in deps.iterdir()) if deps.is_dir() else []
    with open(os.environ["FAKE_DEPS"], "a") as log:
        log.write(os.environ["CARGO_TARGET_DIR"] + "\\t" + " ".join(names) + "\\n")


def say(line):
    print(f"\\x1b[1m{{line}}\\x1b[0m" if state.get("color") == "yes" else line)


if os.environ.get("FAKE_LOG"):
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
if state.get("absent") and pathlib.Path(state["absent"]).exists():
    say(f"found {{state['absent']}}, which an earlier run left")
    sys.exit(1)
if state.get("write"):
    pathlib.Path(state["write"]).write_text("left behind\\n")
as_json = "--message-format=json" in sys.argv
for kind, path, number, text in marks:
    if kind == "GARBLED":
        print("{{not json")
        continue
    level = "warning" if kind == "WARN" else "error"
    code = text if kind == "BROKEN" else "clippy::disallowed_methods"
    head = f"error[{{text}}]: the build broke" if kind == "BROKEN" else f"{{level}}: {{text}}"
    rendered = f"{{head}}\\n --> {{path}}:{{number}}:1\\n"
    if as_json:
        where = {{"file_name": path, "line_start": number, "line_end": number, "is_primary": True}}
        message = {{"rendered": rendered, "level": level, "message": text, "code": {{"code": code}}, "spans": [where]}}
        print(json.dumps({{"reason": "compiler-message", "message": message}}))
    else:
        say(rendered.rstrip())
if any(kind in ("LINT", "BROKEN") for kind, _, _, _ in marks):
    if as_json:
        print(json.dumps({{"reason": "build-finished", "success": False}}))
    sys.exit(101)
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
    def listed(
        self, registry: str, files: dict[str, str] | None
    ) -> subprocess.CompletedProcess[str]:
        repo = notes_repo(
            self,
            {"src/lib.rs": RUST_NOTE, **(files or {})},
            faults=registry,
            unregistered="src/lib.rs::the_guard\n",
        )
        return repo.run("list")

    def check(
        self, registry: str, message: str, files: dict[str, str] | None = None
    ) -> None:
        out = self.listed(registry, files)
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn(message, out.stderr)

    def passes(self, registry: str, files: dict[str, str] | None = None) -> None:
        out = self.listed(registry, files)
        self.assertEqual(out.returncode, 0, out.stderr)

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

    def test_a_sha_in_a_message_fails(self):
        # The step name ci:parity prints carries the action's SHA, which Dependabot bumps.
        sha = "bec219d24cd3e171d82865faccec33120bb574f4"
        self.check(
            entry(
                expect="exit-nonzero",
                run=["./gate"],
                message=f"step `astral-sh/setup-uv@{sha}` has no `version` input",
            ),
            f"`message` carries {sha}, a SHA or digest",
        )

    def test_a_tree_version_in_a_message_fails(self):
        # The fault writes `the_guard=fail`; 0.15.22 is a pin the tree owns.
        self.check(
            entry(
                expect="exit-nonzero",
                run=["./gate"],
                message="the_guard=fail, and mise.toml pins 0.15.22",
            ),
            "`message` carries 0.15.22, a version its fault doesn't write",
        )

    def test_a_version_the_anchor_carries_into_the_fault_is_the_trees(self):
        # `with` repeats the anchor's version: the fault adds a key, not the version.
        kept = (
            'transform = { file = "ci.txt", replace = "run: uvx ruff@0.15.22 format", '
            'with = "run: uvx ruff@0.15.22 format continue-on-error" }'
        )
        self.check(
            entry(
                expect="exit-nonzero",
                run=["./gate"],
                fault=kept,
                message="step `uvx ruff@0.15.22 format` sets `continue-on-error`",
            ),
            "`message` carries 0.15.22, a version its fault doesn't write",
            files={"ci.txt": "run: uvx ruff@0.15.22 format\n"},
        )

    def test_a_version_the_fault_seeds_passes(self):
        # One entry for each place a fault writes a version: a transform's `with`, a patch's
        # added lines, and `argv_fault`.
        patch = textwrap.dedent(
            """\
            --- a/uv.txt
            +++ b/uv.txt
            @@ -1 +1 @@
            -uv=0.12.13
            +uv=0.12.14
            """
        )
        registry = (
            entry(
                fid="by-transform",
                expect="exit-nonzero",
                run=["./gate"],
                fault='transform = { file = "ruff.txt", replace = "ruff=0.15.22", with = "ruff=0.15.21" }',
                message="runs 0.15.21, and mise.toml pins",
            )
            + entry(
                fid="by-patch",
                expect="exit-nonzero",
                run=["./gate"],
                fault='patch = "guards/faults/by-patch.patch"',
                message="and mise.toml pins 0.12.14",
            )
            + entry(
                fid="by-argv",
                expect="exit-nonzero",
                run=["./gate"],
                fault='argv_fault = ["--pin", "2.0.1"]',
                message="pins 2.0.1, which nothing installs",
            )
        )
        self.passes(
            registry,
            {
                "ruff.txt": "ruff=0.15.22\n",
                "uv.txt": "uv=0.12.13\n",
                "guards/faults/by-patch.patch": patch,
            },
        )


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

    def test_lint_batches_report_the_serial_verdicts_at_every_job_count(self):
        # A batch that proves some entries and leaves others to run alone (one that doesn't
        # fire, one whose error is off its lines, one whose anchor is gone), a second batch on
        # another command, and an entry of another kind between them.
        other = [*CLIPPY[:3], "other", *CLIPPY[4:]]
        repo = lint_repo(
            self,
            lint("a", "use of `a`", "    // LINT(use of `a`)\n"),
            lint("b", "use of `a`", "    let _ = 1;\n", anchor=TWO),
            lint("c", "use of `c`", "    // LINT@1(use of `c`)\n", anchor=TWO),
            keyed("t", "g1", "g1"),
            lint(
                "gone",
                "use of `g`",
                "    // LINT(use of `g`)\n",
                anchor="    gone();\n",
            ),
            lint("d", "use of `d`", "    // LINT(use of `d`)\n", run=other),
            lint("e", "use of `e`", "    // LINT(use of `e`)\n", anchor=TWO, run=other),
            state="g1=ok\n",
        )
        serial = repo.run("fire")
        self.assertEqual(serial.returncode, 1, serial.stdout + serial.stderr)
        for jobs in ("2", "3"):
            with self.subTest(jobs=jobs):
                parallel = repo.run("fire", "--jobs", jobs)
                self.assertEqual(parallel.returncode, serial.returncode)
                self.assertEqual(verdicts(parallel.stdout), verdicts(serial.stdout))
        lines = verdicts(serial.stdout)
        for fid in ("a", "c", "t", "d", "e"):
            self.assertIn(f"fired: {fid} (t)", lines)
        self.assertIn(
            "guards: b ran alone: no error carrying its message is on its own lines",
            lines,
        )
        self.assertIn(
            "guards: gone ran alone: src/lib.rs: the anchor '    gone();' matches 0 times, not once",
            lines,
        )
        self.assertIn("guards: 5 of 7 fired (t)", lines)


# ── lint batches ─────────────────────────────────────────────────────────────

CLIPPY = ["cargo", "clippy", "-p", "fixture", "--all-targets", "--", "-D", "warnings"]
ONE = "    anchor_one();\n"
TWO = "    anchor_two();\n"
LIB = "fn one() {\n" + ONE + "}\n\nfn two() {\n" + TWO + "}\n"


def lint(
    fid: str,
    message: str,
    wrote: str,
    anchor: str = ONE,
    run: list[str] | None = None,
    instead: bool = False,
) -> str:
    """A lint entry on a clippy command whose fault writes `wrote` ahead of `anchor` in
    `src/lib.rs`, or in its place with `instead`."""
    lines = wrote if instead else wrote + anchor
    # A JSON string is a TOML basic string, newlines escaped.
    fault = (
        f'transform = {{ file = "src/lib.rs", replace = {json.dumps(anchor)}, '
        f"with = {json.dumps(lines)} }}"
    )
    return entry(
        fid=fid,
        guard="fixture clippy.toml: a ban",
        run=run or CLIPPY,
        expect="lint-error",
        message=message,
        fault=fault,
    )


def lint_repo(test: unittest.TestCase, *entries: str, state: str = "") -> Repo:
    return Repo(
        test,
        {"guards/faults.toml": "".join(entries), "src/lib.rs": LIB, "state.txt": state},
    )


class FireLintBatches(unittest.TestCase):
    """The lint entries on one clippy command are seeded together and fire in one run, and
    each counts only by an error on the lines its own fault wrote. Whatever the batch can't
    attribute runs alone, so every verdict is the one the entry's own run gives."""

    def fire(
        self, repo: Repo, *args: str
    ) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        """The run, and each seeded run it made in order: `batch` for the one with
        `--message-format=json`, `alone` for an entry's own."""
        log = repo.tmp.parent / "runs.log"
        out = repo.run("fire", *args, FAKE_LOG=str(log))
        rows = [line.split("\t") for line in log.read_text().splitlines()]
        return out, [
            "batch" if "--message-format=json" in row[4] else "alone"
            for row in rows
            if row[3] == "seeded"
        ]

    def test_one_run_proves_each_entry_by_the_error_on_its_own_lines(self):
        repo = lint_repo(
            self,
            lint("a", "use of `a`", "    // LINT(use of `a`)\n"),
            lint("b", "use of `b`", "    // LINT(use of `b`)\n"),
            lint("c", "use of `c`", "    // LINT(use of `c`)\n", anchor=TWO),
        )
        logs = repo.tmp.parent / "logs"
        out, seeded = self.fire(repo, "--logs", str(logs))
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        for fid in "abc":
            self.assertIn(f"fired: {fid} (", out.stdout)
        self.assertEqual(seeded, ["batch"], out.stdout)
        self.assertIn(
            "guards: 3 of 3 lint entries on `cargo clippy -p fixture --all-targets -- -D "
            "warnings` fired in one run (",
            out.stdout,
        )
        # Each entry keeps its own log, the batch's run as cargo prints it without JSON.
        text = (logs / "b.fault.log").read_text()
        self.assertIn(
            "its error is at src/lib.rs:3, on lines its own fault wrote", text
        )
        self.assertIn("error: use of `b`\n --> src/lib.rs:3:1", text)
        self.assertNotIn('"reason"', text)

    def test_an_error_off_the_entrys_own_lines_leaves_it_to_run_alone(self):
        # b's error is on its own line and on line 1 too, d's on line 1 only: no entry wrote
        # line 1. Each fires in its own run, which prints its message wherever it lands.
        repo = lint_repo(
            self,
            lint("a", "use of `a`", "    // LINT(use of `a`)\n"),
            lint("b", "use of `b`", "    // LINT(use of `b`) LINT@1(use of `b`)\n"),
            lint("d", "use of `d`", "    // LINT@1(use of `d`)\n"),
        )
        out, seeded = self.fire(repo)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        for fid in "abd":
            self.assertIn(f"fired: {fid} (", out.stdout)
        self.assertEqual(seeded, ["batch", "alone", "alone"], out.stdout)
        for fid in "bd":
            self.assertIn(
                f"guards: {fid} ran alone: an error carrying its message is on no entry's "
                "lines (src/lib.rs:1)",
                out.stdout,
            )

    def test_two_entries_with_one_message_are_each_proven_on_their_own_lines(self):
        # a and c each write the error; b writes a line that draws only a warning of
        # another kind, under the same message. Proven by the message alone, b would fire on
        # a's or c's error.
        shared = "use of `shared`"
        repo = lint_repo(
            self,
            lint("a", shared, "    // LINT(use of `shared`)\n"),
            lint("b", shared, "    // WARN(unused)\n", anchor=TWO),
            lint("c", shared, "    // LINT(use of `shared`)\n", anchor=TWO),
        )
        out, seeded = self.fire(repo)
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertIn("fired: a (", out.stdout)
        self.assertIn("fired: c (", out.stdout)
        self.assertIn(
            "DID NOT FIRE: b: the command passed with the fault seeded", out.stdout
        )
        self.assertEqual(seeded, ["batch", "alone"], out.stdout)
        self.assertIn(
            "guards: b ran alone: no error carrying its message is on its own lines",
            out.stdout,
        )

    def test_a_warning_on_the_entrys_own_lines_proves_nothing(self):
        # b's fault draws a warning, which doesn't fail the command: a's error does, in the
        # batch, and b's own run passes.
        repo = lint_repo(
            self,
            lint("a", "use of `a`", "    // LINT(use of `a`)\n"),
            lint("b", "use of `b`", "    // WARN(use of `b`)\n", anchor=TWO),
        )
        out, seeded = self.fire(repo)
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertIn("fired: a (", out.stdout)
        self.assertIn(
            "DID NOT FIRE: b: the command passed with the fault seeded", out.stdout
        )
        self.assertEqual(seeded, ["batch", "alone"], out.stdout)

    def test_a_batch_that_does_not_compile_runs_every_entry_alone(self):
        repo = lint_repo(
            self,
            lint("a", "use of `a`", "    // LINT(use of `a`)\n"),
            lint("b", "use of `b`", "    // BROKEN(E0425)\n"),
            lint("c", "use of `c`", "    // LINT(use of `c`)\n", anchor=TWO),
        )
        out, seeded = self.fire(repo)
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertIn("fired: a (", out.stdout)
        self.assertIn("fired: c (", out.stdout)
        self.assertIn(
            "DID NOT FIRE: b: the command failed (101) but its output never says 'use of `b`'",
            out.stdout,
        )
        self.assertEqual(seeded, ["batch", "alone", "alone", "alone"], out.stdout)
        self.assertIn(
            "guards: a ran alone: the batch doesn't compile: error[E0425] at src/lib.rs:3",
            out.stdout,
        )

    def test_entries_that_change_the_same_lines_run_apart(self):
        repo = lint_repo(
            self,
            lint(
                "a",
                "use of `a`",
                "    anchor_one(); // LINT(use of `a`)\n",
                instead=True,
            ),
            lint(
                "b",
                "use of `b`",
                "    anchor_one(); // LINT(use of `b`)\n",
                instead=True,
            ),
            lint("c", "use of `c`", "    // LINT(use of `c`)\n", anchor=TWO),
        )
        out, seeded = self.fire(repo)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        for fid in "abc":
            self.assertIn(f"fired: {fid} (", out.stdout)
        self.assertEqual(seeded, ["batch", "alone"], out.stdout)
        self.assertIn("guards: b ran alone: it changes lines a changes too", out.stdout)

    def attribute(self, said: str, code: int = 101) -> tuple[dict, dict]:
        """`attribute` over two entries, `use of `a`` on lines 2-3 and `use of `b`` on 7."""
        regions = {0: [("src/lib.rs", 2, 3)], 1: [("src/lib.rs", 7, 7)]}
        messages = ["use of `a`", "use of `b`"]
        return fire_module()["attribute"](said, code, regions, messages, Path("/t"))

    @staticmethod
    def error(
        text: str, line: int, level: str = "error", code: str | None = "clippy::x"
    ) -> str:
        where = {
            "file_name": "src/lib.rs",
            "line_start": line,
            "line_end": line,
            "is_primary": True,
        }
        message = {
            "rendered": f"{level}: {text}",
            "level": level,
            "message": text,
            "code": {"code": code} if code else None,
            "spans": [where],
        }
        return json.dumps({"reason": "compiler-message", "message": message})

    def test_attribution_reads_nothing_it_cannot_parse_and_nothing_from_a_run_that_passed(
        self,
    ):
        both = self.error("use of `a`", 2) + "\n" + self.error("use of `b`", 7) + "\n"
        proven, _ = self.attribute(both)
        self.assertEqual(sorted(proven), [0, 1])
        proven, why = self.attribute("{not json\n" + both)
        self.assertEqual(proven, {})
        self.assertIn("isn't JSON", why[0])
        proven, why = self.attribute(both, code=0)
        self.assertEqual(proven, {})
        self.assertEqual(why[1], "the batch's run passed")
        # An error with no code, as rustc gives a syntax error, is a compile error too.
        proven, why = self.attribute(both + self.error("expected `;`", 7, code=None))
        self.assertEqual(proven, {})
        self.assertIn("the batch doesn't compile", why[0])


# ── the restored pass by build ───────────────────────────────────────────────


def spans(log: Path) -> list[dict]:
    """$FAKE_SPANS's runs, each with its start, end, tree, kind and arguments."""
    rows = [line.split("\t") for line in log.read_text().splitlines()]
    runs: list[dict] = []
    for edge, when, tree, kind, argv in rows:
        if edge == "start":
            runs.append(
                {
                    "start": float(when),
                    "end": None,
                    "tree": tree,
                    "kind": kind,
                    "argv": argv,
                }
            )
        else:
            run = next(
                r
                for r in runs
                if r["end"] is None and (r["tree"], r["argv"]) == (tree, argv)
            )
            run["end"] = float(when)
    return runs


class FireRestoredByBuild(unittest.TestCase):
    """The restored pass runs a build at a time: a worker restores a build it ran once no
    fault that builds it is queued on any worker or running on any, while other builds'
    faults still run, and a tree's reset removes what a run left there."""

    def test_a_build_is_restored_after_its_last_fault_and_before_the_others_end(self):
        # Build one (`-p one`) has two quick faults, build two (`-p two`) two slow ones and a
        # quick one. Two workers: worker 1 builds two clean, worker 2 builds one, finishes
        # one's faults, restores one, and takes two's quick fault from worker 1's queue.
        log = Path(tempfile.mkdtemp()) / "spans.log"
        self.addCleanup(shutil.rmtree, log.parent, True)

        def fault(fid: str, build: str, guard: str, extra: str = "") -> str:
            return entry(
                fid=fid,
                guard=guard,
                run=["cargo", "test", "-p", build, "--", "--exact", guard],
                fault=f'transform = {{ file = "state.txt", replace = "w-{fid}=ok", with = "w-{fid}=ok {guard}=fail{extra}" }}',
            )

        repo = fire_repo(
            self,
            fault("b1", "two", "g2", " slow=12"),
            fault("b2", "two", "g2", " slow=12"),
            fault("b3", "two", "g2"),
            fault("a1", "one", "g1"),
            fault("a2", "one", "g1"),
            state="g1=ok g2=ok w-b1=ok w-b2=ok w-b3=ok w-a1=ok w-a2=ok\n",
        )
        out = repo.run("fire", "--jobs", "2", FAKE_SPANS=str(log))
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        runs = spans(log)
        # A tree's clean runs after its first seeded one are its restored runs.
        restored = []
        for tree in {r["tree"] for r in runs}:
            mine = [r for r in runs if r["tree"] == tree]
            first = next(i for i, r in enumerate(mine) if r["kind"] == "seeded")
            restored += [r for r in mine[first:] if r["kind"] == "clean"]
            # Each tree's last run of each command it ran is clean.
            for argv in {r["argv"] for r in mine}:
                self.assertEqual(
                    [r for r in mine if r["argv"] == argv][-1]["kind"],
                    "clean",
                    (tree, argv),
                )
        for build in ("one", "two"):
            faults = [
                r for r in runs if r["kind"] == "seeded" and f"-p {build} " in r["argv"]
            ]
            checks = [r for r in restored if f"-p {build} " in r["argv"]]
            self.assertTrue(faults and checks, runs)
            for check in checks:
                self.assertGreater(
                    check["start"], max(f["end"] for f in faults), (build, runs)
                )
        # And there's no barrier: build one comes back while two's faults still run.
        one = min(r["start"] for r in restored if "-p one " in r["argv"])
        two = max(
            r["end"] for r in runs if r["kind"] == "seeded" and "-p two " in r["argv"]
        )
        self.assertLess(one, two, runs)

    def test_a_build_is_ready_only_when_none_of_its_faults_is_queued_or_running(self):
        m = fire_module()
        board_of, task_of, wait = m["Board"], m["Task"], m["WAIT"]
        build = ("rust", (("cargo", "test", "--no-run"),))
        restore = ("restore", build)

        def key(task: object) -> object:
            return getattr(task, "key", task)

        # Worker 2 ran the build clean. Its one fault waits in worker 1's queue, pinned there.
        board = board_of([[task_of(0, True, build)], []], True, [{}, {build: None}])
        self.assertIs(board.next(1), wait)
        fault = board.next(0)
        self.assertEqual(key(fault), 0)
        self.assertIs(board.next(1), wait)
        board.finish(0, fault, [(0, "fired")], [])
        self.assertEqual(key(board.next(1)), restore)
        # Worker 1 took a fault in the build, so it owes the build a restored run too.
        self.assertEqual(key(board.next(0)), restore)
        self.assertIsNone(board.next(0))
        self.assertIsNone(board.next(1))
        # A batch's fallbacks are queued in the step that ends it, so its build isn't ready.
        board = board_of(
            [[task_of(("batch", 0), False, build)], []], True, [{}, {build: None}]
        )
        batch = board.next(0)
        board.finish(0, batch, [], [task_of(5, True, build)])
        self.assertIs(board.next(1), wait)

    def test_a_result_no_worker_is_left_to_put_fails_the_run(self):
        # Every worker has stopped and the result was never put: waiting would hang the run.
        board = fire_module()["Board"]([[], []], True)
        board.leave()
        board.leave()
        raised: list[BaseException] = []

        def wait() -> None:
            try:
                board.get("unput")
            except RuntimeError as error:
                raised.append(error)

        waiter = threading.Thread(target=wait, daemon=True)
        waiter.start()
        waiter.join(timeout=10)
        self.assertFalse(
            waiter.is_alive(), "get() waited for a result no worker can put"
        )
        self.assertIn("every worker stopped with 0 of 1 results", str(raised[0]))

    def test_an_ignored_file_a_fault_writes_is_gone_before_the_next_run(self):
        fault = 'transform = { file = "state.txt", replace = "the_guard=ok", with = "the_guard=fail write=junk.log" }'
        repo = Repo(
            self,
            {
                ".gitignore": "junk.log\n",
                "guards/faults.toml": entry(fault=fault),
                "state.txt": "the_guard=ok absent=junk.log\n",
            },
        )
        out = repo.run("fire")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("fired: one", out.stdout)
        self.assertIn("guards: restored, every command passes again", out.stdout)


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
OWED = "the full fire runs on every push to main, or here without --affected"


HASH = "0123456789abcdef"


def fire_module() -> dict:
    return runpy.run_path(str(SCRIPT), run_name="guards_fire")


def fake_target(root: Path, names: list[str]) -> Path:
    """A target whose dev profile holds `names` (each `kind/entry`, a trailing `/` for a
    directory), every file an hour old."""
    for name in names:
        path = root / "debug" / name.rstrip("/")
        if name.endswith("/"):
            path.mkdir(parents=True, exist_ok=True)
            (path / "inside").write_text(name)
            os.utime(path / "inside", (time.time() - 3600,) * 2)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(name)
            os.utime(path, (time.time() - 3600,) * 2)
    return root


def units(target: Path) -> set[str]:
    return {
        f"{kind}/{p.name}"
        for kind in ("deps", "build", ".fingerprint", "incremental")
        if (target / "debug" / kind).is_dir()
        for p in (target / "debug" / kind).iterdir()
    }


# A dependency's units, and a workspace member's (`microvms-cli`, its `microvm` bin, and a
# crate called `libc`-like `liblocal` to cover both readings of the `lib` prefix).
DEPENDENCY = [
    f"deps/libserde-{HASH}.rlib",
    f"deps/libserde-{HASH}.rmeta",
    f"deps/serde-{HASH}.d",
    f"deps/liblibc-{HASH}.rlib",
    f"deps/libc-{HASH}.d",
    f".fingerprint/serde-{HASH}/",
    f"build/aws-lc-sys-{HASH}/",
    f".fingerprint/aws-lc-sys-{HASH}/",
]
WORKSPACE = [
    f"deps/libmicrovms_cli-{HASH}.rlib",
    f"deps/microvms_cli-{HASH}.d",
    f"deps/microvm-{HASH}",
    f"deps/liblocal-{HASH}.rlib",
    f"deps/local-{HASH}.d",
    f".fingerprint/microvms-cli-{HASH}/",
    f"build/microvms-js-{HASH}/",
    f"incremental/microvms_cli-{HASH}/",
    "deps/libmicrovms.rlib",
]
LOCAL_NAMES = {
    "microvms-cli",
    "microvms_cli",
    "microvm",
    "microvms-js",
    "microvms_js",
    "local",
}


class WarmWorkers(unittest.TestCase):
    """`fire --jobs N`'s extra workers start from the first target's dependency units and
    never from a workspace member's: a copied member's unit is fresh against an
    older tree and reads the tree it was built in through `env!("CARGO_MANIFEST_DIR")`."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp = Path(directory.name)
        self.m = fire_module()

    def test_a_seeded_target_holds_no_workspace_unit(self):
        first = fake_target(self.tmp / "first", DEPENDENCY + WORKSPACE)
        other = self.tmp / "other"
        line = self.m["seed_targets"](first, [other], LOCAL_NAMES)
        self.assertEqual(units(other), {u.rstrip("/") for u in DEPENDENCY})
        self.assertIn("start from the first target's", line)
        # The copies keep their mtimes: cargo compares a unit's with its dep-info's.
        source = first / "debug" / "deps" / f"libserde-{HASH}.rlib"
        copy = other / "debug" / "deps" / f"libserde-{HASH}.rlib"
        self.assertEqual(copy.stat().st_mtime, source.stat().st_mtime)

    def test_cargo_not_answering_copies_nothing(self):
        first = fake_target(self.tmp / "first", DEPENDENCY)
        other = self.tmp / "other"
        line = self.m["seed_targets"](first, [other], None)
        self.assertFalse(other.exists())
        self.assertIn("cargo metadata didn't answer", line)

    def test_an_unparsed_name_is_not_shared(self):
        self.assertFalse(self.m["dependency_unit"]("libmicrovms.so", set()))
        self.assertFalse(self.m["dependency_unit"](f"serde-{HASH[:8]}.d", set()))
        self.assertTrue(self.m["dependency_unit"](f"serde-{HASH}.d", set()))

    def test_an_overlaid_file_gets_a_fresh_mtime(self):
        # The caller's uncommitted file, a day old: its copy in the scratch tree is new, so
        # an artifact built from other text at that path in an earlier run is stale to cargo.
        repo = fire_repo(self, keyed("a", "g1", "g1"), state="g1=ok\n")
        repo.write("new.txt", "uncommitted\n")
        day_old = time.time() - 86400
        os.utime(repo.root / "new.txt", (day_old, day_old))
        tree = self.m["Tree"].make(repo.root)
        self.addCleanup(tree.remove)
        copied = tree.path / "new.txt"
        self.assertEqual(copied.read_text(), "uncommitted\n")
        self.assertGreater(copied.stat().st_mtime, day_old + 3600)

    def test_local_packages_names_members_their_targets_and_path_dependencies(self):
        root = self.tmp / "ws"
        files = {
            "Cargo.toml": '[workspace]\nmembers = ["a-crate"]\nresolver = "3"\n',
            "a-crate/Cargo.toml": (
                '[package]\nname = "a-crate"\nversion = "0.1.0"\nedition = "2024"\n'
                '[dependencies]\nnear = { path = "../near" }\n'
                '[[bin]]\nname = "a-tool"\npath = "src/main.rs"\n'
            ),
            "a-crate/src/lib.rs": "",
            "a-crate/src/main.rs": "fn main() {}\n",
            "a-crate/tests/t_one.rs": "",
            "near/Cargo.toml": '[package]\nname = "near"\nversion = "0.1.0"\nedition = "2024"\n',
            "near/src/lib.rs": "",
        }
        for path, text in files.items():
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_text(text)
        names = self.m["local_packages"](root)
        self.assertIsNotNone(names)
        for name in ("a-crate", "a_crate", "a-tool", "a_tool", "t_one", "near"):
            self.assertIn(name, names)

    def test_each_extra_worker_starts_from_the_first_workers_dependencies(self):
        # End to end: before any worker's first command, each extra target holds the first
        # target's dependency unit and not the workspace member's.
        seen = self.tmp / "deps.log"
        repo = fire_repo(
            self,
            keyed("a", "g1", "g1"),
            keyed("b", "g2", "g2"),
            keyed("c", "g3", "g3"),
            state="g1=ok g2=ok g3=ok\n",
        )
        repo.write(
            "metadata.json",
            json.dumps(
                {
                    "packages": [
                        {
                            "name": "fixture",
                            "targets": [{"name": "fixture"}],
                            "dependencies": [],
                        }
                    ]
                }
            ),
        )
        repo.commit("metadata")
        first = repo.target / "guards-fire"
        fake_target(
            first, [f"deps/libthird-{HASH}.rlib", f"deps/libfixture-{HASH}.rlib"]
        )
        tmp = self.tmp / "scratch"
        tmp.mkdir()
        out = repo.run("fire", "--jobs", "3", TMPDIR=str(tmp), FAKE_DEPS=str(seen))
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn(
            "guards: the other 2 workers start from the first target's 1 dependency units",
            out.stdout,
        )
        runs = [line.split("\t") for line in seen.read_text().splitlines()]
        extra = [(t, deps.split()) for t, deps in runs if t != str(first)]
        self.assertTrue(extra, runs)
        self.assertEqual(len({t for t, _ in extra}), 2, runs)
        for target, deps in extra:
            self.assertIn(f"libthird-{HASH}.rlib", deps, target)
            self.assertNotIn(f"libfixture-{HASH}.rlib", deps, target)


class BuildCommand(unittest.TestCase):
    """`build` compiles what the selected entries' cargo commands compile, once each, and runs
    nothing, so CI's `guards-cache` job saves every build the `guards` legs restore."""

    def test_each_cargo_command_builds_once_without_running(self):
        log = Path(tempfile.mkdtemp()) / "runs.log"
        self.addCleanup(shutil.rmtree, log.parent, True)
        repo = fire_repo(
            self,
            entry(
                fid="t1", run=["cargo", "test", "-p", "x", "--", "--exact", "the_guard"]
            ),
            entry(
                fid="t2",
                guard="other",
                run=["cargo", "test", "-p", "x", "--", "--exact", "other"],
            ),
            entry(
                fid="lint",
                expect="lint-error",
                message="m",
                run=[
                    "cargo",
                    "clippy",
                    "-p",
                    "x",
                    "--all-targets",
                    "--",
                    "-D",
                    "warnings",
                ],
            ),
            entry(
                fid="doc",
                guard="x::doctest",
                run=[
                    "cargo",
                    "test",
                    "-p",
                    "x",
                    "--doc",
                    "--",
                    "--exact",
                    "x::doctest",
                ],
            ),
            entry(
                fid="sc",
                guard="g",
                run=[sys.executable, "g.py"],
                expect="exit-nonzero",
                message="m",
                suite="script",
            ),
        )
        out = repo.run(
            "build", "--suite", "rust", "--suite", "script", FAKE_LOG=str(log)
        )
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        args = [line.split("\t")[4] for line in log.read_text().splitlines()]
        self.assertEqual(
            args, ["test -p x --no-run", "clippy -p x --all-targets"], out.stdout
        )
        self.assertIn("guards: 2 builds", out.stdout)

    def test_a_build_that_fails_fails_the_command(self):
        repo = fire_repo(self, entry(), state="the_guard=ok build=E0599\n")
        out = repo.run("build")
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertIn("exits 101", out.stderr)

    def test_no_cargo_command_selected_fails(self):
        repo = fire_repo(self, entry())
        out = repo.run("build", "--suite", "script")
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertIn("nothing to build", out.stderr)


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

    # CI's side of mise.toml (#323): the `guards` job's own steps and the workflow's `env`.

    def workflow_repo(self) -> Repo:
        repo = affected_repo(self)
        repo.write(WORKFLOW, FIXTURE_WORKFLOW)
        repo.commit("the workflow")
        git(repo.root, "update-ref", "refs/remotes/origin/main", "HEAD")
        return repo

    def test_a_change_to_the_guards_jobs_steps_or_the_workflow_env_selects_every_entry(
        self,
    ):
        # A pull request that drops clippy from the job would otherwise skip every clippy
        # entry, pass, and turn main red.
        for old, new in (
            ("components: clippy, rustfmt", "components: rustfmt"),
            ("CARGO_TERM_COLOR: always", "CARGO_TERM_COLOR: never"),
        ):
            with self.subTest(change=new):
                repo = self.workflow_repo()
                repo.write(WORKFLOW, FIXTURE_WORKFLOW.replace(old, new))
                self.check(repo, ALL, f"affected: a: {WORKFLOW_REASON}")

    def test_a_deleted_or_emptied_workflow_selects_every_entry(self):
        for emptied in (False, True):
            with self.subTest(emptied=emptied):
                repo = self.workflow_repo()
                if emptied:
                    repo.write(WORKFLOW, "")
                else:
                    (repo.root / WORKFLOW).unlink()
                self.check(repo, ALL, f"affected: a: {WORKFLOW_REASON}")

    def test_another_job_or_a_comment_in_the_workflow_selects_nothing(self):
        repo = self.workflow_repo()
        repo.write(
            WORKFLOW,
            FIXTURE_WORKFLOW.replace("echo other", "echo changed").replace(
                "# The toolchain", "# Its toolchain"
            ),
        )
        self.check(repo, set())


WORKFLOW = ".github/workflows/ci.yml"
WORKFLOW_REASON = (
    f"the `guards` job or the top-level `env` in {WORKFLOW} changed, and CI runs every "
    "command under them"
)
FIXTURE_WORKFLOW = """\
name: ci
on:
  pull_request:
env:
  CARGO_TERM_COLOR: always
jobs:
  guards:
    runs-on: ubuntu-latest
    steps:
      # The toolchain every command runs under.
      - uses: dtolnay/rust-toolchain@stable
        with:
          components: clippy, rustfmt
      - run: ./scripts/check-guards-fire.py fire --affected
  other:
    runs-on: ubuntu-latest
    steps:
      - run: echo other
"""


# ── --shard (#345) ───────────────────────────────────────────────────────────


def command(guard: str, fids: list[str], noted: frozenset[str] = frozenset()) -> str:
    """Entries that share one command, `cargo test -- --exact <guard>`. Each seeds
    `<guard>=fail` after its own word `w-<fid>=ok`, which the fake runner reads after the
    guard's `ok`, so each entry fires on its own. An id in `noted` gets a `note`, which
    changes its registry text and nothing else."""
    return "".join(
        entry(
            fid=fid,
            guard=guard,
            run=["cargo", "test", "--", "--exact", guard],
            fault=f'transform = {{ file = "state.txt", replace = "w-{fid}=ok", with = "w-{fid}=ok {guard}=fail" }}',
            **({"note": "changed"} if fid in noted else {}),
        )
        for fid in fids
    )


def commands_registry(
    spec: list[tuple[str, list[str]]], noted: frozenset[str] = frozenset()
) -> str:
    return "".join(command(guard, fids, noted) for guard, fids in spec)


def commands_repo(
    test: object, spec: list[tuple[str, list[str]]], extra: str = "", **files: str
) -> Repo:
    state = [f"{guard}=ok" for guard, _ in spec]
    state += [f"w-{fid}=ok" for _, fids in spec for fid in fids]
    return Repo(
        test,
        {
            "guards/faults.toml": commands_registry(spec) + extra,
            "state.txt": " ".join(state) + "\n",
            **files,
        },
    )


# The shared fixture, in registry order: a one-entry command ahead of a two-entry one, so the
# order the split visits commands in (heaviest first) isn't the registry's, then five one-entry
# commands. Guard names run backward through the alphabet, so a tie broken by the sorted key is
# the reverse of one broken by registry position. A bindings entry comes last, and `fire` drops
# it without `--venv`.
SHARED = [
    ("gh", ["h"]),
    ("gg", ["g1", "g2"]),
    ("gf", ["f"]),
    ("ge", ["e"]),
    ("gd", ["d"]),
    ("gc", ["c"]),
    ("gb", ["b"]),
]
SHARED_IDS = [fid for _, fids in SHARED for fid in fids]
SHARED_BINDING = entry(
    fid="bi",
    guard="t",
    run=["node", "--test", "--test-reporter=tap", "t.mjs"],
    suite="bindings",
    fault='transform = { file = "state-js.txt", replace = "t=ok", with = "t=fail" }',
)
# The shared fixture's slices under the documented rule: the heaviest command first, a tie by
# first registry position, each onto the lightest shard, a tie to the lower shard; within a
# shard, registry order.
GOLDEN = {
    2: [["g1", "g2", "e", "c"], ["h", "f", "d", "b"]],
    3: [["g1", "g2", "c"], ["h", "e", "b"], ["f", "d"]],
}
BANNER = re.compile(
    r"^guards: shard (\d+) of (\d+) keeps (\d+) of (\d+) selected entries "
    r"\((\d+) of (\d+) commands\)$",
    re.MULTILINE,
)
# A child that loads the script and prints `shard`'s slices of the shared fixture, so the
# tie-break is checked under one hash seed per process.
SLICES = """\
import json, runpy, sys
from pathlib import Path
script = runpy.run_path(sys.argv[1])
faults, _ = script["load"](Path(sys.argv[2]))
rust = [f for f in faults if f.suite != "bindings"]
print(json.dumps({n: [[f.id for f in script["shard"](rust, k, n)] for k in range(n)] for n in (2, 3)}))
"""


def fired_ids(stdout: str) -> list[str]:
    return re.findall(r"^fired: ([a-z0-9-]+) ", stdout, re.MULTILINE)


class FireSharded(unittest.TestCase):
    """`fire --shard k/N` keeps shard k of the selection and fires only that (#345). CI's
    `guards` job runs one shard a leg, so the slices have to cover the selection, never
    overlap, keep each command whole, and come out the same on every run.

    Each registry entry for this class runs one test, so its fixture runs are cached on the
    class: a test that shares the sweep pays for it once, and so does the whole class."""

    cache: dict[str, object] = {}

    @classmethod
    def once(cls, name: str, make):
        if name not in cls.cache:
            cls.cache[name] = make(
                types.SimpleNamespace(addCleanup=cls.addClassCleanup)
            )
        return cls.cache[name]

    @classmethod
    def shared(cls) -> Repo:
        return cls.once(
            "shared",
            lambda holder: commands_repo(
                holder, SHARED, SHARED_BINDING, **{"state-js.txt": "t=ok\n"}
            ),
        )

    @classmethod
    def sweep(cls) -> dict[object, subprocess.CompletedProcess[str]]:
        """Every shard of the shared fixture at N = 1 to 3, and the unsharded fire, under one
        hash seed."""

        def run(_holder: object) -> dict[object, subprocess.CompletedProcess[str]]:
            repo = cls.shared()
            runs: dict[object, subprocess.CompletedProcess[str]] = {
                None: repo.run("fire", PYTHONHASHSEED="0")
            }
            for n in (1, 2, 3):
                for k in range(n):
                    runs[(k, n)] = repo.run(
                        "fire", "--shard", f"{k}/{n}", PYTHONHASHSEED="0"
                    )
            return runs

        runs = cls.once("sweep", run)
        for key, out in runs.items():
            if out.returncode != 0:
                raise AssertionError(
                    f"fire {key}: exit {out.returncode}\n{out.stdout}{out.stderr}"
                )
        return runs

    def shards(self, n: int) -> list[list[str]]:
        return [fired_ids(self.sweep()[(k, n)].stdout) for k in range(n)]

    def test_no_entry_is_dropped(self):
        for n in (1, 2, 3):
            with self.subTest(n=n):
                self.assertEqual(
                    set().union(*self.shards(n)),
                    set(SHARED_IDS),
                    f"the {n} shards together don't fire every selected entry",
                )

    def test_no_entry_fires_in_two_shards(self):
        for n in (2, 3):
            with self.subTest(n=n):
                shards = self.shards(n)
                self.assertEqual(
                    sum(map(len, shards)),
                    len(set().union(*shards)),
                    f"an entry fires in more than one of {n} shards: {shards}",
                )

    def test_a_command_stays_in_one_shard(self):
        # g1 and g2 share a command, so splitting them runs its clean and restored passes
        # twice across the matrix.
        for n in (2, 3):
            with self.subTest(n=n):
                self.assertEqual(
                    [("g1" in s, "g2" in s) for s in self.shards(n)].count(
                        (True, True)
                    ),
                    1,
                    f"g1 and g2 share a command but not a shard: {self.shards(n)}",
                )

    def test_shard_0_of_1_is_the_unsharded_fire(self):
        # A one-entry command comes before the two-entry one, so a slice in the order the
        # split visits commands (heaviest first) prints g1 before h.
        runs = self.sweep()
        whole = [v for v in verdicts(runs[(0, 1)].stdout) if not BANNER.match(v)]
        self.assertEqual(
            whole,
            verdicts(runs[None].stdout),
            "shard 0 of 1 doesn't print the unsharded fire's lines in its order",
        )
        self.assertEqual(fired_ids(runs[None].stdout), SHARED_IDS)

    def test_each_shard_keeps_its_documented_slice(self):
        for n, want in GOLDEN.items():
            with self.subTest(n=n, via="fire"):
                self.assertEqual(
                    self.shards(n), want, "a shard's slice isn't the documented one"
                )
        repo = self.shared()
        for seed in range(8):
            with self.subTest(seed=seed):
                out = subprocess.run(
                    [sys.executable, "-c", SLICES, str(SCRIPT), str(repo.root)],
                    capture_output=True,
                    text=True,
                    env=clean_env(PYTHONHASHSEED=str(seed)),
                )
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertEqual(
                    json.loads(out.stdout),
                    {str(n): want for n, want in GOLDEN.items()},
                    f"PYTHONHASHSEED={seed}: a shard's slice isn't the documented one",
                )

    def test_a_shard_says_which_it_is_and_what_it_keeps(self):
        runs = self.sweep()
        for n in (1, 2, 3):
            kept = commands = 0
            for k in range(n):
                with self.subTest(k=k, n=n):
                    out = runs[(k, n)].stdout
                    found = BANNER.findall(out)
                    self.assertEqual(
                        len(found), 1, f"shard {k}/{n} prints no banner\n{out}"
                    )
                    shard, count, keeps, whole, has, total = map(int, found[0])
                    self.assertEqual((shard, count), (k, n))
                    self.assertEqual(keeps, len(fired_ids(out)))
                    self.assertEqual((whole, total), (len(SHARED_IDS), len(SHARED)))
                    kept += keeps
                    commands += has
            self.assertEqual((kept, commands), (len(SHARED_IDS), len(SHARED)))

    def test_the_bindings_drop_comes_before_the_slice(self):
        # Without --venv, `fire` drops bindings entries; the slice is of what's left, so the
        # banner counts the rust entries only.
        runs = self.sweep()
        for k in (0, 1):
            with self.subTest(k=k):
                out = runs[(k, 2)].stdout
                self.assertIn("guards: skipping 1 bindings entries", out)
                self.assertEqual(
                    [int(m[3]) for m in BANNER.findall(out)],
                    [len(SHARED_IDS)],
                    "the slice isn't of the selection the bindings drop leaves",
                )
                self.assertNotIn("bi", fired_ids(out))

    def test_a_shard_runs_only_its_own_commands_clean_and_restored(self):
        runs = self.sweep()
        for n in (2, 3):
            for k in range(n):
                with self.subTest(k=k, n=n):
                    out = runs[(k, n)].stdout
                    mine = set(fired_ids(out))
                    for label in ("clean", "restored"):
                        named = set(
                            re.findall(
                                rf"^guards: {label} run for ([a-z0-9-]+) ",
                                out,
                                re.MULTILINE,
                            )
                        )
                        self.assertTrue(named, f"shard {k}/{n} has no {label} run")
                        self.assertLessEqual(
                            named,
                            mine,
                            f"shard {k}/{n} runs another shard's command {label}",
                        )

    def test_whole_commands_spread_heaviest_first(self):
        # Three three-entry commands at registry positions 0, 1 and 3 among nine: a
        # contiguous split puts the first two in shard 0, and so does round robin to the
        # first and third.
        spec = [
            ("gp", ["p1", "p2", "p3"]),
            ("gq", ["q1", "q2", "q3"]),
            ("gr", ["r"]),
            ("gs", ["s1", "s2", "s3"]),
            ("gt", ["t"]),
            ("gu", ["u"]),
            ("gv", ["v"]),
            ("gw", ["w"]),
            ("gx", ["x"]),
        ]
        repo = commands_repo(self, spec)
        for k in range(3):
            with self.subTest(k=k):
                out = repo.run("fire", "--shard", f"{k}/3")
                self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
                big = {fid[0] for fid in fired_ids(out.stdout) if fid[0] in "pqs"}
                self.assertEqual(
                    len(big), 1, f"shard {k} of 3 doesn't hold one heavy command: {big}"
                )

    @classmethod
    def affected_runs(cls) -> dict[int, subprocess.CompletedProcess[str]]:
        """A (three entries), B (two), C and D (one each); the branch changes A's and D's
        registry entries, so `--affected` selects A and D."""

        def run(holder: object) -> dict[int, subprocess.CompletedProcess[str]]:
            spec = [
                ("ga", ["a1", "a2", "a3"]),
                ("gb", ["b1", "b2"]),
                ("gc", ["c"]),
                ("gd", ["d"]),
            ]
            repo = commands_repo(holder, spec)
            noted = frozenset(["a1", "a2", "a3", "d"])
            repo.write("guards/faults.toml", commands_registry(spec, noted))
            repo.commit()
            return {
                k: repo.run("fire", "--affected", "--shard", f"{k}/2") for k in (0, 1)
            }

        return cls.once("affected", run)

    def test_the_banner_counts_the_affected_selection(self):
        # Sliced before `--affected`, the banner would count the whole registry's seven.
        for k, out in self.affected_runs().items():
            with self.subTest(k=k):
                self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
                self.assertEqual(
                    [(int(m[3]), int(m[5])) for m in BANNER.findall(out.stdout)],
                    [(4, 2)],
                    f"shard {k} of 2 doesn't count the affected selection\n{out.stdout}",
                )

    def test_each_shard_fires_its_share_of_the_affected_entries(self):
        # The registry's own slices would put A and D both in shard 0 and leave shard 1 idle.
        runs = self.affected_runs()
        self.assertEqual(
            [fired_ids(runs[k].stdout) for k in (0, 1)],
            [["a1", "a2", "a3"], ["d"]],
            "the shards don't split the affected entries",
        )

    def test_an_empty_slice_passes_and_says_so(self):
        repo = self.shared()
        out = repo.run("fire", "--only", "h", "--shard", "1/2")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertRegex(out.stdout, r"keeps 0 of 1 selected entries")
        self.assertIn("so nothing to fire", out.stdout)
        self.assertEqual(fired_ids(out.stdout), [])
        self.assertEqual(repo.worktrees(), 1)
        # An empty selection is still an error, shard or not, so an empty slice can't hide an
        # empty registry.
        out = repo.run("fire", "--suite", "script", "--shard", "0/2")
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertIn("guards: no entry selected", out.stderr)

    def test_a_shard_outside_its_count_is_refused(self):
        repo = self.shared()
        # "١/٢" is 1/2 in Arabic-Indic digits, which `int` reads and CI never writes.
        for spec in ("3/3", "2/1", "0/0", "-1/2", "1", "a/b", "1/2/3", " 1/2", "١/٢"):
            with self.subTest(spec=spec):
                out = repo.run("fire", f"--shard={spec}")
                self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
                self.assertIn(
                    f"guards: --shard takes k/N with 0 <= k < N; got {spec}", out.stderr
                )
                self.assertEqual(fired_ids(out.stdout), [])

    def test_a_shard_weighs_an_entry_by_what_it_builds(self):
        # X: three script entries (6 each), Y: two that build the CLI (16 each), Z: two other
        # Rust entries (14 each). By cost Y goes first to shard 0 and Z and X share shard 1. A
        # count, a CLI entry priced as another Rust one, or a script entry priced as a Rust
        # one each gives other slices.
        spec = [
            ("x", "script", ["python3", "x.py"]),
            (
                "y",
                "rust",
                ["cargo", "test", "-p", "microvms-cli", "--", "--exact", "y"],
            ),
            ("z", "rust", ["cargo", "test", "--", "--exact", "z"]),
        ]
        sizes = {"x": 3, "y": 2, "z": 2}
        registry = "".join(
            entry(
                fid=f"{name}{i}",
                guard=name,
                run=run,
                expect="exit-nonzero",
                suite=suite,
                message="no",
                fault=f'transform = {{ file = "state.txt", replace = "{name}{i}=ok", with = "{name}{i}=bad" }}',
            )
            for name, suite, run in spec
            for i in range(sizes[name])
        )
        state = " ".join(f"{n}{i}=ok" for n, size in sizes.items() for i in range(size))
        repo = Repo(self, {"guards/faults.toml": registry, "state.txt": state + "\n"})
        script = runpy.run_path(str(SCRIPT))
        faults, problems = script["load"](repo.root)
        self.assertEqual(problems, [])
        self.assertEqual(
            [[f.id for f in script["shard"](faults, k, 2)] for k in (0, 1)],
            [["y0", "y1"], ["x0", "x1", "x2", "z0", "z1"]],
            "the shards aren't split by the entries' cost",
        )

    def test_the_registrys_own_shards_partition_it(self):
        # The split CI makes, on the registry it makes it of: the rust and script entries in
        # CI's three shards.
        script = runpy.run_path(str(SCRIPT))
        faults, problems = script["load"](HERE.parent)
        self.assertEqual(problems, [])
        selected = [f for f in faults if f.suite in ("rust", "script")]
        self.assertTrue(selected, "the registry has no rust or script entry to split")
        key = script["command_key"]
        shards = [script["shard"](selected, k, 3) for k in range(3)]
        ids = [f.id for s in shards for f in s]
        self.assertEqual(sorted(ids), sorted(f.id for f in selected))
        self.assertEqual(len(ids), len(set(ids)))
        owners: dict[tuple, set[int]] = {}
        for number, part in enumerate(shards):
            self.assertTrue(part, f"shard {number} of 3 is empty")
            for fault in part:
                owners.setdefault(key(fault), set()).add(number)
        self.assertEqual([k for k, o in owners.items() if len(o) > 1], [])


# ── the `guards` job's steps, as ci.yml writes them (#323) ────────────────────

CI = HERE.parent / ".github/workflows/ci.yml"
# The two conditions the job's fire steps may carry; any other fails the case by name.
ON_PULL_REQUEST = "github.event_name == 'pull_request'"
ON_PUSH = "github.event_name != 'pull_request'"
# What a pull request gives the steps' `${{ }}`s. The fixture's base is `release`, not main, and
# it has no origin/main, so a step that ignores `github.base_ref` (a hard-coded origin/main, or
# a `$BASE` that's unset and falls back to it) finds no merge base and fails.
EXPRESSIONS = {"github.base_ref": "release"}
LOCAL = HERE.parent / "ci/local.toml"
EXPRESSION = re.compile(r"\$\{\{\s*(.+?)\s*\}\}")


def workflow_jobs() -> dict:
    # Imported here, not at the top: the registry's `-k` entries run this module without
    # pyyaml, and none of them reaches this class.
    import yaml

    workflow = yaml.safe_load(CI.read_text()) or {}
    return workflow.get("jobs") or {}


def guards_job() -> dict:
    return workflow_jobs().get("guards") or {}


# The check ruleset 21934766 requires. The shards report under their own names, and one job
# reports their combined result under this one (#345).
REQUIRED = "seeded faults fire"


def fired(stdout: str) -> list[str]:
    return [
        v
        for v in verdicts(stdout)
        if re.match(r"(fired|DID NOT FIRE|stale anchor): ", v)
    ]


class GuardsJob(unittest.TestCase):
    """Pull requests fire the entries their diff affects, and every push to main fires every
    entry (D31), each as a matrix of shards whose combined result is the required check (D35,
    #345). The steps run as ci.yml writes them, once per shard, against a fixture repository."""

    def legs(self) -> list:
        legs = ((guards_job().get("strategy") or {}).get("matrix") or {}).get("shard")
        self.assertTrue(legs, "the guards job has no `shard` matrix")
        return list(legs)

    def fire_steps(self, event: str) -> list[dict]:
        steps = [
            s
            for s in guards_job().get("steps") or []
            if "check-guards-fire.py fire" in s.get("run", "")
        ]
        self.assertTrue(
            steps,
            "ci.yml's guards job has no step that runs `check-guards-fire.py fire`",
        )
        chosen = []
        for step in steps:
            condition = step.get("if")
            if condition is not None:
                self.assertIn(
                    condition,
                    (ON_PULL_REQUEST, ON_PUSH),
                    f"unmodeled `if: {condition}`",
                )
            if condition is None or (condition == ON_PULL_REQUEST) == (
                event == "pull_request"
            ):
                chosen.append(step)
        self.assertEqual(
            len(chosen), 1, f"{event}: {len(chosen)} fire steps run, not one"
        )
        return chosen

    def run_step(
        self, repo: Repo, step: dict, shard: int = 0, total: int = 1
    ) -> subprocess.CompletedProcess[str]:
        answers = {
            **EXPRESSIONS,
            "matrix.shard": str(shard),
            "strategy.job-total": str(total),
        }

        def value(match: re.Match) -> str:
            self.assertIn(
                match.group(1), answers, f"unmodeled `${{{{ {match.group(1)} }}}}`"
            )
            return answers[match.group(1)]

        env = {
            k: EXPRESSION.sub(value, str(v)) for k, v in (step.get("env") or {}).items()
        }
        # The step runs the checkout's own script; this one stands in, pointed at the fixture.
        run = step["run"].replace(
            "./scripts/check-guards-fire.py",
            f"{shlex.quote(sys.executable)} {shlex.quote(str(SCRIPT))} --root {shlex.quote(str(repo.root))}",
        )
        path = os.pathsep.join([str(repo.bin), os.environ.get("PATH", "")])
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", run],
            cwd=repo.root,
            capture_output=True,
            text=True,
            env=clean_env(PATH=path, TMPDIR=str(repo.tmp), **env),
        )

    def test_a_pull_request_fires_the_entries_it_affects_and_a_push_fires_every_entry(
        self,
    ):
        # One entry of each other suite beside the fixture's rust ones: this job fires rust
        # and script, and the bindings job fires bindings.
        script = entry(
            fid="sc",
            guard="sgate.py",
            run=[sys.executable, "sgate.py"],
            expect="exit-nonzero",
            suite="script",
            message="sgate says no",
            fault='transform = { file = "state-s.txt", replace = "s=ok", with = "s=bad" }',
        )
        binding = entry(
            fid="bi",
            guard="t",
            run=["node", "--test", "--test-reporter=tap", "t.mjs"],
            suite="bindings",
            fault='transform = { file = "state-js.txt", replace = "t=ok", with = "t=fail" }',
        )
        repo = affected_repo(self, extra=script + binding)
        # The step's `--target-dir target` is inside the checkout, as on the runner.
        repo.write(".gitignore", "target/\n")
        repo.write("state-s.txt", "s=ok\n")
        repo.write(
            "sgate.py",
            "import sys\nbad = 's=bad' in open('state-s.txt').read()\n"
            "bad and print('sgate says no')\nsys.exit(1 if bad else 0)\n",
        )
        repo.write("state-js.txt", "t=ok\n")
        repo.commit("the fixture's base")
        git(repo.root, "update-ref", "refs/remotes/origin/release", "HEAD")
        git(repo.root, "update-ref", "-d", "refs/remotes/origin/main")
        # HEAD plays the pull request's merge commit: one rust, one script and one bindings
        # entry's files change.
        for path in ("state-a.txt", "state-s.txt", "state-js.txt"):
            repo.write(path, (repo.root / path).read_text() + "extra=ok\n")
        repo.commit()

        legs = self.legs()

        def each_shard(
            event: str, step: dict, selected: set[str], want: set[str]
        ) -> None:
            ids: list[str] = []
            for shard in legs:
                out = self.run_step(repo, step, shard, len(legs))
                self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
                self.assertEqual(selection(out.stdout), selected, out.stdout)
                ids += fired_ids(out.stdout)
            self.assertEqual(
                len(ids), len(set(ids)), f"{event}: an entry fires in two shards: {ids}"
            )
            self.assertEqual(set(ids), want, f"{event}: the shards together")

        (pr,) = self.fire_steps("pull_request")
        each_shard("pull_request", pr, {"a", "sc"}, {"a", "sc"})

        (push,) = self.fire_steps("push")
        self.assertNotIn("--affected", push["run"])
        self.assertNotIn("--only", push["run"])
        each_shard("push", push, set(), {*ORDER, "sc"})

    def test_the_pull_request_step_is_the_push_step_plus_the_selection(self):
        # So the two legs can't drift apart in a flag (a lost suite, or a bindings suite this
        # job has no toolchain for), and neither can leave the worker count and per-command
        # timeout the budgets were measured with: four workers on the four-vCPU runner, and
        # a hung fault stopped well inside the job's time.
        (pr,) = self.fire_steps("pull_request")
        (push,) = self.fire_steps("push")
        pr_argv, push_argv = shlex.split(pr["run"]), shlex.split(push["run"])
        suites = [b for a, b in zip(push_argv, push_argv[1:]) if a == "--suite"]
        self.assertEqual(suites, ["rust", "script"], push["run"])
        options = dict(zip(push_argv, push_argv[1:]))
        for flag, want in (("--jobs", "4"), ("--timeout", "900")):
            self.assertEqual(options.get(flag), want, f"the push step's {flag}")
        self.assertEqual(pr_argv, [*push_argv, "--affected", "--base", "$BASE"])

    def test_ci_local_answers_the_guards_job_as_a_pull_request(self):
        # ci:local runs one leg, the pull request's, as one run of its whole selection. Its
        # answers have to agree with each other and with ci.yml: swapped event answers would run
        # main's full fire there, and a shard count above one would fire a third of it.
        local = tomllib.loads(LOCAL.read_text())
        answers = local["expressions"]
        self.assertEqual(answers.get(ON_PULL_REQUEST), "true", ON_PULL_REQUEST)
        self.assertEqual(answers.get(ON_PUSH), "false", ON_PUSH)
        # ci-local.py clones origin/main as the base, so the base branch is main.
        self.assertEqual(answers.get("github.base_ref"), "main")
        self.assertEqual(
            (answers.get("matrix.shard"), answers.get("strategy.job-total")),
            ("0", "1"),
            "ci:local runs one shard of the selection, not all of it",
        )
        jobs = local["job"]["ci.yml"]
        (name,) = [
            j for j, body in workflow_jobs().items() if body.get("name") == REQUIRED
        ]
        self.assertIn(
            "skip", jobs.get(name, {}), f"ci/local.toml runs the `{name}` job"
        )

    def test_the_fire_steps_build_incrementally(self):
        # dtolnay/rust-toolchain writes CARGO_INCREMENTAL=0 into the job's
        # environment, and only a step's own `env` wins over it. Each fault is an edit and a
        # rebuild of one crate, which is what incremental builds are for.
        steps = [*self.fire_steps("pull_request"), *self.fire_steps("push")]
        steps += [
            s
            for s in workflow_jobs()["bindings"].get("steps") or []
            if "check-guards-fire.py fire" in s.get("run", "")
        ]
        self.assertEqual(len(steps), 3, "two guards fire steps and the bindings one")
        for step in steps:
            self.assertEqual(
                (step.get("env") or {}).get("CARGO_INCREMENTAL"), "1", step.get("name")
            )

    def test_the_legs_restore_the_one_cache_a_push_to_main_saves(self):
        # One job saves the guards' dependency cache, from every rust entry's
        # builds, and only on main; the legs and the mutants shards restore it and never save.
        jobs = workflow_jobs()

        def cache(job: str) -> dict:
            (step,) = [
                s
                for s in jobs[job].get("steps") or []
                if s.get("uses", "").startswith("Swatinem/rust-cache@")
            ]
            return step

        on_main = "${{ github.ref == 'refs/heads/main' }}"
        key = (cache("guards").get("with") or {}).get("shared-key")
        self.assertTrue(key, "the guards legs' cache has no shared-key")
        for job in ("guards", "mutants"):
            given = cache(job).get("with") or {}
            self.assertEqual(given.get("shared-key"), key, job)
            self.assertIs(given.get("save-if"), False, f"{job} saves its cache")
        saver = jobs["guards-cache"]
        self.assertEqual(saver.get("if"), "github.event_name == 'push'")
        step = cache("guards-cache")
        self.assertEqual(step.get("with"), {"shared-key": key, "save-if": on_main})
        (build,) = [
            s
            for s in saver.get("steps") or []
            if "check-guards-fire.py build" in s.get("run", "")
        ]
        argv = shlex.split(build["run"])
        (push,) = self.fire_steps("push")
        fire = shlex.split(push["run"])
        suites = [b for a, b in zip(fire, fire[1:]) if a == "--suite"]
        self.assertIn("rust", suites)
        self.assertEqual(
            [b for a, b in zip(argv, argv[1:]) if a == "--suite"], ["rust"]
        )
        self.assertEqual(dict(zip(argv, argv[1:])).get("--target-dir"), "target")
        self.assertEqual(
            build.get("if"), f"steps.{step.get('id')}.outputs.cache-hit != 'true'"
        )
        # No rust-cache step anywhere in the workflow saves from a pull request.
        for name, job in jobs.items():
            for s in job.get("steps") or []:
                if s.get("uses", "").startswith("Swatinem/rust-cache@"):
                    self.assertIn(
                        (s.get("with") or {}).get("save-if"), (False, on_main), name
                    )

    def test_every_shard_has_sixty_minutes_on_both_legs(self):
        # D35: one budget for a pull request's shards and main's push's, as a number, so a
        # shard's time is the same question on both.
        self.assertEqual(
            guards_job().get("timeout-minutes"),
            60,
            "each shard's budget, on a pull request and on main's push",
        )

    def test_the_matrix_runs_every_shard_and_lets_each_finish(self):
        strategy = guards_job().get("strategy") or {}
        matrix = strategy.get("matrix") or {}
        # A second axis would run each shard once per value, firing its slice twice.
        self.assertEqual(list(matrix), ["shard"], "the guards job's matrix axes")
        legs = self.legs()
        self.assertGreater(len(legs), 1, "one shard is the unsharded job")
        self.assertEqual(legs, list(range(len(legs))), "the shards aren't 0 to N-1")
        # A shard that fails would cancel the others, and the aggregator would show only the
        # first failure.
        self.assertIs(
            strategy.get("fail-fast"), False, "a failed shard cancels the others"
        )
        for event in ("pull_request", "push"):
            (step,) = self.fire_steps(event)
            self.assertEqual(
                (step.get("env") or {}).get("SHARD"),
                "${{ matrix.shard }}/${{ strategy.job-total }}",
                f"{event}: the fire step's SHARD isn't its leg of the matrix",
            )
            argv = shlex.split(step["run"])
            self.assertIn(
                ("--shard", "$SHARD"),
                list(zip(argv, argv[1:])),
                f'{event}: the fire step doesn\'t pass --shard "$SHARD"',
            )

    def aggregator(self) -> dict:
        jobs = workflow_jobs()
        named = [j for j, body in jobs.items() if body.get("name") == REQUIRED]
        self.assertEqual(
            len(named),
            1,
            f"{len(named)} jobs carry the required check's name `{REQUIRED}`, not one",
        )
        self.assertNotEqual(named[0], "guards", "the required check is one shard")
        return jobs[named[0]]

    def test_the_required_check_is_the_aggregator(self):
        job = self.aggregator()
        needs = job.get("needs")
        self.assertIn(
            "guards",
            [needs] if isinstance(needs, str) else list(needs or []),
            "the aggregator doesn't wait for the shards",
        )
        # Without `always()` it's skipped when a shard fails, and a skipped required check
        # counts as passing.
        self.assertIn(
            str(job.get("if")),
            ("always()", "${{ always() }}"),
            "the aggregator doesn't run when a shard fails",
        )

    def test_the_aggregator_passes_only_when_every_shard_passed(self):
        # A timed-out shard reports `cancelled`, not `failure` (#340's two rounds), so only
        # `success` passes.
        steps = [s for s in self.aggregator().get("steps") or [] if "run" in s]
        self.assertEqual(len(steps), 1, "the aggregator runs one step")
        (step,) = steps
        for result in ("success", "failure", "cancelled", "skipped"):
            with self.subTest(result=result):

                def value(match: re.Match) -> str:
                    self.assertEqual(
                        match.group(1),
                        "needs.guards.result",
                        f"unmodeled `${{{{ {match.group(1)} }}}}`",
                    )
                    return result

                env = {
                    k: EXPRESSION.sub(value, str(v))
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
                    env=clean_env(**env),
                )
                self.assertEqual(
                    out.returncode == 0,
                    result == "success",
                    f"the aggregator's exit {out.returncode} with the shards {result}",
                )
                if result != "success":
                    # A red required check with an empty log doesn't say where to look.
                    self.assertIn(
                        f"::error::a guards shard ended {result}",
                        out.stdout,
                        "the aggregator fails without saying why",
                    )

    def test_the_aggregator_has_no_way_to_pass_over_a_red_shard(self):
        # A step `if` skips its one step and `continue-on-error` swallows its exit, and either
        # leaves the job green with a shard red. ci:parity checks a job's keys against what
        # ci:local models, but not a job ci/local.toml skips, as this one is, so this holds
        # the aggregator to keys that can't make it pass.
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

    def test_the_checkout_has_the_base_branch_the_merge_base_needs(self):
        checkout = [
            s
            for s in guards_job().get("steps") or []
            if str(s.get("uses", "")).startswith("actions/checkout@")
        ]
        self.assertEqual(
            len(checkout),
            1,
            "the guards job doesn't have exactly one actions/checkout step",
        )
        self.assertEqual(str((checkout[0].get("with") or {}).get("fetch-depth")), "0")


if __name__ == "__main__":
    unittest.main()
