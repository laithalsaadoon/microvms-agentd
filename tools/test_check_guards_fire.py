# SPDX-License-Identifier: Apache-2.0
"""Tests for `tools/check-guards-fire.py`: when a seeded fault counts as fired (#274).

Each case builds a throwaway git repository with a registry and a fake test runner on PATH
named `cargo`, `pytest` or `node`. The fake reads `state.txt` in the tree it runs in and prints
the line the real runner would, so every verdict branch is driven through the real script,
the scratch worktree included. The census cases run the real ast-grep, so run this through
`mise run guards:list`.
"""

import dataclasses
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
import types
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "check-guards-fire.py"
MARKER = "**" + "Falsification" + "**"
# The fixture repos' registry: one owner's file.
REGISTRY = "verify/guards/faults/fixture.toml"
# An id the real registry always has: the daemon's first entry, and the schema example in the
# script's docstring.
SENTINEL = "agentd-fs-pop"

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

# One runner for every name. `uv venv P` makes P/bin, `uv pip install --python P/bin/python ...`
# does nothing, and `npx` builds nothing and passes; each writes a line to $FAKE_TOOLS when it's
# set (the tool, the directory it ran in, VIRTUAL_ENV, CARGO_TARGET_DIR, the arguments), and
# `uv` exits $FAKE_UV_EXIT (0 by default). None of them touches the target. `cargo metadata`
# prints the tree's `metadata.json` (exit 101 without one). It reads every `state*.txt` in the
# tree it runs in as `name=value` words: `build=E0599` breaks the build with that code,
# `<test>=fail` fails that test, `color=yes` wraps each line in ANSI codes the way CI's
# `CARGO_TERM_COLOR=always` does, `slow=N` sleeps N tenths of a second, and `hang=yes` sleeps
# past any timeout (with a child that sleeps too, and both pids written under $FAKE_PIDS when
# it's set). `write=F` writes
# the file F into the tree, and `absent=F` fails the run when F is there. `mark=N` creates
# $FAKE_SYNC/N as the run starts, and `await=N` waits for $FAKE_SYNC/N before it goes on (failing
# after 30 seconds), so a test can order runs in two workers without a clock. With
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
import dataclasses
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time

texts = [p.read_text() for p in sorted(pathlib.Path(".").glob("state*.txt"))]
state = dict(w.split("=", 1) for w in " ".join(texts).split())
tool = pathlib.Path(sys.argv[0]).name
if tool in ("uv", "npx"):
    if os.environ.get("FAKE_TOOLS"):
        with open(os.environ["FAKE_TOOLS"], "a") as log:
            log.write("\\t".join([
                tool,
                os.getcwd(),
                os.environ.get("VIRTUAL_ENV", ""),
                os.environ.get("CARGO_TARGET_DIR", ""),
                " ".join(sys.argv[1:]),
            ]) + "\\n")
    if tool == "uv":
        if int(os.environ.get("FAKE_UV_EXIT", "0")):
            print("uv broke")
            sys.exit(int(os.environ["FAKE_UV_EXIT"]))
        if sys.argv[1:2] == ["venv"]:
            pathlib.Path(sys.argv[-1], "bin").mkdir(parents=True)
    sys.exit(0)
marks = []
if tool == "cargo" and "clippy" in sys.argv[1:]:
    for path in sorted(pathlib.Path(".").rglob("*.rs")):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            for kind, at, text in re.findall(r"(LINT|WARN|BROKEN|GARBLED)(?:@(\\d+))?\\(([^)]*)\\)", line):
                marks.append((kind, path.as_posix(), int(at or number), text))
seeded = bool(marks) or any(
    v not in ("ok", "no")
    for k, v in state.items()
    if k not in ("slow", "flaky", "absent", "mark", "await") and "." not in k
)
if os.environ.get("FAKE_TRACE"):
    here = os.getcwd()
    wanted = {{a for a in sys.argv[1:] if not a.startswith("-")}}
    lines = []
    for path in sorted(pathlib.Path(".").glob("state*.txt")):
        keys = {{w.split("=", 1)[0].split(".", 1)[0] for w in path.read_text().split()}}
        if path.name == "state.txt" or keys & wanted:
            lines.append(f'openat(AT_FDCWD<{{here}}>, "{{path}}", O_RDONLY|O_CLOEXEC) = 3<{{here}}/{{path}}>')
    child = os.getpid() + 1000000
    for name in sorted(wanted):
        for kind in ("reads", "lists", "stats", "probes", "git", "runs"):
            for value in filter(None, state.get(f"{{name}}.{{kind}}", "").split(":")):
                there = pathlib.Path(value).exists()
                gone = "-1 ENOENT (No such file or directory)"
                if kind == "reads":
                    lines.append(f'openat(AT_FDCWD<{{here}}>, "{{value}}", O_RDONLY) = ' + (f"3<{{here}}/{{value}}>" if there else gone))
                elif kind == "lists":
                    lines.append(f'openat(AT_FDCWD<{{here}}>, "{{value}}", O_RDONLY|O_CLOEXEC|O_DIRECTORY) = 3<{{here}}/{{value}}>')
                elif kind in ("stats", "probes"):
                    lines.append(f'newfstatat(AT_FDCWD<{{here}}>, "{{value}}", 0x7ffc, 0) = ' + ("0" if there else gone))
                else:
                    child += 1
                    lines.append(f"clone3({{{{flags=CLONE_VM|CLONE_VFORK}}}}, 88) = {{child}}")
                    words = ["git", value] if kind == "git" else value.split("+")
                    program = shutil.which(words[0]) or words[0]
                    listed = ", ".join(json.dumps(w) for w in words)
                    lines.append((child, f'execve("{{program}}", [{{listed}}], 0x7ffd /* 3 vars */) = 0'))
                    if kind == "git":
                        lines.append((child, f'openat(AT_FDCWD<{{here}}>, ".git/HEAD", O_RDONLY) = 3<{{here}}/.git/HEAD>'))
    with open(os.environ["FAKE_TRACE"], "a") as log:
        for line in lines:
            pid, call = line if isinstance(line, tuple) else (os.getpid(), line)
            log.write(f"{{pid}} {{call}}\\n")
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
if state.get("mark"):
    pathlib.Path(os.environ["FAKE_SYNC"], state["mark"]).write_text("")
if state.get("await"):
    awaited = pathlib.Path(os.environ["FAKE_SYNC"], state["await"])
    deadline = time.monotonic() + 30
    while not awaited.exists():
        if time.monotonic() > deadline:
            say(f"{{awaited.name}} never started")
            sys.exit(1)
        time.sleep(0.02)
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

# A stand-in for strace, for `fire --record`: it writes the traced command's own `execve` line
# to the file after `-o`, then becomes the command with FAKE_TRACE naming that file, where the
# fake runner above adds a line for each file it reads. FAKE_STRACE=silent writes nothing, which
# is what a trace that lost its command looks like.
FAKE_STRACE = """\
#!{python}
import dataclasses
import json
import os
import shutil
import sys

args = sys.argv[1:]
out = None
while args and args[0].startswith("-"):
    flag = args.pop(0)
    if flag in ("-s", "-e", "-o"):
        value = args.pop(0)
        if flag == "-o":
            out = value
program = shutil.which(args[0]) or args[0]
if out and os.environ.get("FAKE_STRACE") != "silent":
    with open(out, "a") as log:
        listed = ", ".join(json.dumps(a) for a in args)
        log.write(f'{{os.getpid()}} execve("{{os.path.abspath(program)}}", [{{listed}}], 0x7ffd /* 3 vars */) = 0\\n')
    os.environ["FAKE_TRACE"] = out
os.execv(program, args)
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


def toml_run(run: list) -> str:
    """One argv, or a list of them."""
    if run and isinstance(run[0], list):
        return "[" + ", ".join(toml_argv(a) for a in run) + "]"
    return toml_argv(run)


def entry(
    fid: str = "one",
    guard: str = "the_guard",
    run: list | None = None,
    expect: str = "test-failed",
    fault: str = 'transform = { file = "state.txt", replace = "the_guard=ok", with = "the_guard=fail" }',
    suite: str = "rust",
    **extra: str,
) -> str:
    lines = [
        "[[fault]]",
        f"id = {toml_str(fid)}",
        f"guard = {toml_str(guard)}",
        f"run = {toml_run(run or CARGO)}",
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
        for tool in ("cargo", "pytest", "node", "uv", "npx", "strace"):
            path = self.bin / tool
            fake = FAKE_STRACE if tool == "strace" else FAKE_RUNNER
            path.write_text(fake.format(python=sys.executable))
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        files = {"verify/guards/unregistered.txt": "", **files}
        for path, text in files.items():
            self.write(path, text)
        git(self.root.parent, "init", "-q", "-b", "main", str(self.root))
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "fixture")
        # `list` compares verify/guards/unregistered.txt with the merge base of HEAD and origin/main.
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
        path = env.pop("PATH", None) or os.pathsep.join(
            [str(self.bin), os.environ.get("PATH", "")]
        )
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
    return Repo(test, {REGISTRY: "".join(entries), "state.txt": state})


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
            REGISTRY,
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
                REGISTRY: entry(fault='patch = "verify/guards/faults/one.patch"'),
                "verify/guards/faults/one.patch": patch,
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
            "stale anchor: one: verify/guards/faults/one.patch doesn't apply",
            out.stdout,
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
        repo.write(REGISTRY, entry(fid="new-one"))
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
        self.assertIn("pass --venv-per-worker, or --venv DIR", out.stderr)

    def test_one_venv_and_a_venv_per_worker_are_refused_together(self):
        repo = fire_repo(self, entry(suite="bindings"))
        out = repo.run("fire", "--venv", str(repo.tmp / "v"), "--venv-per-worker")
        self.assertEqual(out.returncode, 2, out.stdout + out.stderr)
        self.assertIn("not allowed with argument", out.stderr)


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
            REGISTRY: registry,
            "verify/guards/unregistered.txt": unregistered,
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
        self.assertIn(
            "src/lib.rs::gone in verify/guards/unregistered.txt isn't a", out.stderr
        )
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
            "verify/guards/unregistered.txt",
            "src/lib.rs::the_guard\nsrc/lib.rs::second\n",
        )
        out = repo.run("list")
        self.assertEqual(out.returncode, 1)
        self.assertIn(
            "src/lib.rs::second is in verify/guards/unregistered.txt but not in",
            out.stderr,
        )
        # Committed on the branch, it still fails: the base is the merge base.
        repo.commit()
        out = repo.run("list")
        self.assertEqual(out.returncode, 1)
        self.assertIn(
            "src/lib.rs::second is in verify/guards/unregistered.txt", out.stderr
        )
        # And an explicit base that already lists it passes.
        out = repo.run("list", "--base", "HEAD")
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_an_explicit_base_is_read_at_its_merge_base_with_head(self):
        # CI passes `--base origin/main`, and main can move while the job runs. Main here drops
        # the listed note after the branch forked; the branch still lists it, which is right
        # for the commit it forked from, so it isn't a key the branch added.
        repo = self.repo()
        git(repo.root, "checkout", "-q", "-b", "pr")
        repo.write("README", "the pull request's own change\n")
        repo.commit("pr")
        git(repo.root, "checkout", "-q", "main")
        repo.write("src/lib.rs", "pub fn gone() {}\n")
        repo.write("verify/guards/unregistered.txt", "")
        repo.commit("main drops the note")
        git(repo.root, "update-ref", "refs/remotes/origin/main", "HEAD")
        git(repo.root, "checkout", "-q", "pr")
        out = repo.run("list", "--base", "origin/main")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("the merge base with origin/main", out.stdout)

    def test_a_renamed_test_keeps_its_place(self):
        repo = self.repo()
        repo.write("src/lib.rs", RUST_NOTE.replace("fn the_guard()", "fn renamed()"))
        repo.write("verify/guards/unregistered.txt", "src/lib.rs::renamed\n")
        out = repo.run("list")
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_a_moved_test_keeps_its_place(self):
        repo = self.repo()
        git(repo.root, "mv", "src/lib.rs", "src/moved.rs")
        repo.write("verify/guards/unregistered.txt", "src/moved.rs::the_guard\n")
        out = repo.run("list")
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_registering_one_note_does_not_make_room_for_another(self):
        repo = self.repo()
        repo.write("src/lib.rs", TWO_NOTES)
        repo.write(REGISTRY, entry(note="src/lib.rs::the_guard"))
        repo.write("verify/guards/unregistered.txt", "src/lib.rs::second\n")
        out = repo.run("list")
        self.assertEqual(out.returncode, 1)
        self.assertIn(
            "src/lib.rs::second is in verify/guards/unregistered.txt", out.stderr
        )

    def test_a_base_with_no_list_is_the_bootstrap(self):
        repo = self.repo()
        (repo.root / "verify/guards/unregistered.txt").unlink()
        repo.commit("no list yet")
        git(repo.root, "update-ref", "refs/remotes/origin/main", "HEAD")
        repo.write("verify/guards/unregistered.txt", "src/lib.rs::the_guard\n")
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
        # A step name a gate prints can carry an action's SHA, which Dependabot bumps.
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
                fault='patch = "verify/guards/faults/by-patch.patch"',
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
                "verify/guards/faults/by-patch.patch": patch,
            },
        )


class RegistryFiles(unittest.TestCase):
    """The loader: which files are the registry, in what order it reads them, and what it
    refuses. `-k RegistryFiles` runs this class alone for the registry's own entries."""

    def repo(self, files: dict[str, str]) -> Repo:
        return Repo(self, {"state.txt": "the_guard=ok\n", **files})

    def fails(self, files: dict[str, str], message: str) -> None:
        repo = self.repo(files)
        for command in ("list", "fire"):
            out = repo.run(command)
            self.assertEqual(out.returncode, 1, f"{command}: {out.stdout}")
            self.assertIn(message, out.stderr, command)

    def test_no_registry_file_is_the_floor(self):
        # The patches beside the files aren't registry files, so a directory of only them
        # is empty too.
        for files in ({}, {"verify/guards/faults/one.patch": "--- a/x\n+++ b/x\n"}):
            with self.subTest(files=sorted(files)):
                self.fails(files, "no file matches verify/guards/faults/*.toml")

    def test_every_file_loads_in_sorted_order_and_nothing_else_is_read(self):
        # A patch and a file in a subdirectory aren't registry files; the verdicts follow the
        # files' sorted order, then each file's own.
        repo = self.repo(
            {
                "verify/guards/faults/b.toml": entry(fid="b-one") + entry(fid="b-two"),
                "verify/guards/faults/a.toml": entry(fid="a-one"),
                "verify/guards/faults/c.patch": "not toml",
                "verify/guards/faults/sub/d.toml": entry(fid="in-a-subdirectory"),
            }
        )
        faults, problems = fire_module()["load"](repo.root)
        self.assertEqual(problems, [])
        self.assertEqual(
            [(f.id, f.file) for f in faults],
            [
                ("a-one", "verify/guards/faults/a.toml"),
                ("b-one", "verify/guards/faults/b.toml"),
                ("b-two", "verify/guards/faults/b.toml"),
            ],
        )
        out = repo.run("fire")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(fired_ids(out.stdout), ["a-one", "b-one", "b-two"])

    def test_an_id_in_two_files_fails_naming_both(self):
        self.fails(
            {
                "verify/guards/faults/a.toml": entry(fid="twice"),
                "verify/guards/faults/b.toml": entry(fid="twice"),
            },
            "verify/guards/faults/b.toml entry 'twice': the id is used twice, here and in "
            "verify/guards/faults/a.toml",
        )

    def test_the_former_single_file_fails_and_says_where_its_entries_go(self):
        files = {
            REGISTRY: entry(),
            "verify/guards/faults.toml": entry(fid="left-behind"),
        }
        self.fails(
            files,
            "verify/guards/faults.toml is the single file the registry was before it was split by "
            "owner, and nothing reads it: move its entries into their owners' files in "
            "verify/guards/faults/ and delete it",
        )
        faults, _ = fire_module()["load"](self.repo(files).root)
        self.assertEqual([f.id for f in faults], ["one"])

    def test_a_file_with_no_entry_fails_by_name_and_the_others_still_load(self):
        files = {
            REGISTRY: entry(),
            "verify/guards/faults/empty.toml": "# an owner, no entry\n",
        }
        self.fails(files, "verify/guards/faults/empty.toml has no [[fault]] entry")
        faults, _ = fire_module()["load"](self.repo(files).root)
        self.assertEqual([f.id for f in faults], ["one"])

    def test_a_file_that_does_not_parse_fails_by_name(self):
        self.fails(
            {REGISTRY: entry(), "verify/guards/faults/broken.toml": "[[fault]\n"},
            "verify/guards/faults/broken.toml doesn't parse",
        )

    def test_the_registry_loads_its_sentinel(self):
        # The real registry through the real loader. One that reads some of the files and
        # not the rest passes the floor, and a known id is what it loses.
        loaded, problems = fire_module()["load"](HERE.parent)
        self.assertEqual(problems, [])
        self.assertIn(
            SENTINEL,
            {f.id for f in loaded},
            f"the sentinel {SENTINEL} didn't load from the registry",
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

    def bindings_repo(self, count: int, **extra: str) -> Repo:
        """`count` bindings entries, each its own pytest command, and one rust entry."""
        entries = [
            entry(
                fid=f"py{n}",
                guard=f"tests/t.py::t{n}",
                run=["pytest", "-rA", f"tests/t.py::t{n}"],
                suite="bindings",
                fault=f'transform = {{ file = "state.txt", replace = "tests/t.py::t{n}=ok", with = "tests/t.py::t{n}=fail" }}',
            )
            for n in range(count)
        ]
        state = " ".join(f"tests/t.py::t{n}=ok" for n in range(count))
        return fire_repo(
            self, *entries, keyed("r", "g1", "g1"), state=f"{state} g1=ok\n", **extra
        )

    def test_with_one_venv_bindings_entries_all_run_in_the_first_workers_target_and_venv(
        self,
    ):
        # `--venv DIR` is one environment, so the entries that install into it can't run at
        # once: all of them stay on worker 1, which builds in the first target.
        tmp = self.scratch_tmp()
        log = tmp.parent / (tmp.name + ".log")
        self.addCleanup(lambda: log.unlink(missing_ok=True))
        repo = self.bindings_repo(3)
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

    def test_with_a_venv_per_worker_bindings_entries_spread_each_in_its_own_venv(self):
        # Each worker makes its own environment beside its worktree, so the bindings
        # entries go to any worker; no two workers share one, each holds the pinned
        # packages, and each worker's restored pass reinstalls into its own.
        tmp = self.scratch_tmp()
        log = tmp.parent / (tmp.name + ".log")
        tools = tmp.parent / (tmp.name + ".tools")
        self.addCleanup(lambda: log.unlink(missing_ok=True))
        self.addCleanup(lambda: tools.unlink(missing_ok=True))
        repo = self.bindings_repo(4)
        out = repo.run(
            "fire",
            "--jobs",
            "4",
            "--venv-per-worker",
            TMPDIR=str(tmp),
            FAKE_LOG=str(log),
            FAKE_TOOLS=str(tools),
        )
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn(
            "guards: each worker's bindings entries build into and test from its own "
            "environment beside its worktree, holding pytest==",
            out.stdout,
        )
        rows = [line.split("\t") for line in log.read_text().splitlines()]
        bindings = [r for r in rows if r[4].startswith("-rA ")]
        trees = {tree for _, _, tree, _, _ in bindings}
        self.assertGreater(
            len(trees), 1, f"every bindings entry ran in one tree: {rows}"
        )
        venvs = {tree: {v for _, v, t, _, _ in bindings if t == tree} for tree in trees}
        for tree, found in venvs.items():
            (venv,) = found
            # Beside its own worktree, where the reset between faults doesn't reach.
            self.assertEqual(Path(venv), Path(tree).parent / "venv", rows)
        self.assertEqual(len({v for (v,) in venvs.values()}), len(trees), venvs)
        made = [line.split("\t") for line in tools.read_text().splitlines()]
        for (venv,) in venvs.values():
            self.assertIn(
                ["uv", "venv", "--quiet", venv], [[m[0], *m[4].split()] for m in made]
            )
            installs = [
                m[4].split()
                for m in made
                if m[0] == "uv" and f"--python {venv}/bin/python" in m[4]
            ]
            self.assertEqual(len(installs), 1, made)
            pins = installs[0][installs[0].index(f"{venv}/bin/python") + 1 :]
            self.assertTrue(pins and all("==" in p for p in pins), pins)
        # A rust entry never runs in an environment.
        self.assertEqual(
            {v for _, v, _, _, a in rows if not a.startswith("-rA ")}, {""}
        )
        # Each tree's last run of each command it ran is clean, so each environment ends on
        # the clean extension; and every environment went with its worktree.
        for tree in trees:
            for command in {r[4] for r in rows if r[2] == tree}:
                last = [r[3] for r in rows if r[2] == tree and r[4] == command][-1]
                self.assertEqual(last, "clean", (tree, command, rows))
        self.assertEqual(list(tmp.iterdir()), [])
        self.assertEqual(repo.worktrees(), 1)

    def test_npx_packages_install_once_before_any_worker_runs_one(self):
        # npm's extraction into a cold cache collides when several npx calls install one
        # package at once (#347), so each package goes in once before the workers start.
        # The `--package` after the command it runs is that command's, not npx's.
        tmp = self.scratch_tmp()
        tools = tmp.parent / (tmp.name + ".tools")
        self.addCleanup(lambda: tools.unlink(missing_ok=True))
        build = [
            "npx",
            "-y",
            "-p",
            "@x/cli@3",
            "x",
            "build",
            "--package",
            "local-crate",
        ]
        entries = [
            entry(
                fid=f"js{n}",
                guard=f"t{n}",
                run=[build, ["node", "--test", "--test-reporter=tap", f"t{n}"]],
                suite="bindings",
                fault=f'transform = {{ file = "state.txt", replace = "t{n}=ok", with = "t{n}=fail" }}',
            )
            for n in range(3)
        ]
        repo = fire_repo(self, *entries, state="t0=ok t1=ok t2=ok\n")
        out = repo.run(
            "fire",
            "--jobs",
            "3",
            "--venv-per-worker",
            TMPDIR=str(tmp),
            FAKE_TOOLS=str(tools),
        )
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        calls = [line.split("\t")[4] for line in tools.read_text().splitlines()]
        npx = [c for c in calls if c.startswith(("-y ", "--yes "))]
        fetch = "--yes --package @x/cli@3 -- node -e 0"
        self.assertEqual(npx[0], fetch, calls)
        self.assertEqual(npx.count(fetch), 1, calls)
        self.assertFalse([c for c in npx if "local-crate --" in c], calls)
        self.assertIn(
            "guards: installed @x/cli@3 for npx once, before the workers", out.stdout
        )

    def test_a_uv_that_fails_ends_the_run_and_leaves_no_tree(self):
        tmp = self.scratch_tmp()
        repo = self.bindings_repo(2)
        out = repo.run(
            "fire",
            "--jobs",
            "2",
            "--venv-per-worker",
            TMPDIR=str(tmp),
            FAKE_UV_EXIT="3",
        )
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertIn("guards: `uv venv --quiet ", out.stderr)
        self.assertIn("uv broke", out.stderr)
        self.assertNotIn("clean run for", out.stdout)
        self.assertEqual(list(tmp.iterdir()), [])
        self.assertEqual(repo.worktrees(), 1)

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

    def stealing(self, state: str) -> tuple[Repo, dict[str, str]]:
        """Three faults on one command and one on another, over two workers: worker 2 finishes
        its own and takes exactly one of worker 1's, the last, a3.

        The order is held by the runs themselves, not by their durations, because timings
        stretch on a loaded host and a second take changes every run count after it. a1 waits
        until a3 has started, so worker 2 goes idle with a2 and a3 still queued and takes the
        back one. a3 waits until a2 has started, so worker 1 has taken a2 before worker 2 could.
        """
        tmp = self.scratch_tmp()
        log = tmp.parent / (tmp.name + ".log")
        self.addCleanup(lambda: log.unlink(missing_ok=True))
        sync = self.scratch_tmp()
        knobs = {1: "await=a3", 2: "mark=a2", 3: "mark=a3 await=a2"}
        faults = [
            entry(
                fid=f"a{n}",
                guard="g1",
                run=["cargo", "test", "--", "--exact", "g1"],
                fault=f'transform = {{ file = "state.txt", replace = "s{n}=ok", with = "s{n}=ok g1=fail {knobs[n]}" }}',
            )
            for n in (1, 2, 3)
        ]
        repo = fire_repo(self, *faults, keyed("b", "g2", "g2"), state=state)
        return repo, {"FAKE_LOG": str(log), "FAKE_SYNC": str(sync)}

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
        repo, env = self.stealing("g1=ok g2=ok s1=ok s2=ok s3=ok\n")
        out = repo.run("fire", "--jobs", "2", **env)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        rows = self.runs_of(repo, Path(env["FAKE_LOG"]))
        # Each tree's last run of each command it ran is clean, the taken one included.
        for tree in {r[2] for r in rows}:
            for command in {r[4] for r in rows if r[2] == tree}:
                last = [r[3] for r in rows if r[2] == tree and r[4] == command][-1]
                self.assertEqual(last, "clean", (tree, command, rows))

    def test_a_restored_run_red_in_one_worker_only_fails_the_run(self):
        # `flaky=5` fails the fifth run in a target, which only worker 2's reaches: its clean
        # g2, its fault, the fault it took, its restored g2, then its restored g1.
        repo, env = self.stealing("g1=ok g2=ok s1=ok s2=ok s3=ok flaky=5\n")
        out = repo.run("fire", "--jobs", "2", **env)
        self.runs_of(repo, Path(env["FAKE_LOG"]))
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
        {REGISTRY: "".join(entries), "src/lib.rs": LIB, "state.txt": state},
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
                REGISTRY: entry(fault=fault),
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


def kinds_repo(test: unittest.TestCase, extra: str = "") -> Repo:
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
                fault='patch = "verify/guards/faults/p.patch"',
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
            REGISTRY: registry,
            "verify/guards/faults/p.patch": patch,
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

    def test_a_target_triples_dependency_units_are_seeded_too(self):
        # `napi build` passes `--target`, so its whole graph sits under `<triple>/debug`; an
        # extra worker without it builds that graph again. Another target nested in this one
        # (`guards-fire`, the fire's own default under the caller's) isn't a triple.
        triple = "x86_64-unknown-linux-gnu"
        first = fake_target(self.tmp / "first", DEPENDENCY[:1])
        fake_target(
            first / triple,
            [
                f"deps/libnapi-{HASH}.rlib",
                f".fingerprint/napi-{HASH}/",
                f"build/napi-sys-{HASH}/",
                f"deps/libmicrovms_js-{HASH}.so",
                f".fingerprint/microvms-js-{HASH}/",
            ],
        )
        fake_target(first / "guards-fire", [f"deps/libserde-{HASH}.rlib"])
        other = self.tmp / "other"
        self.m["seed_targets"](first, [other], LOCAL_NAMES)
        self.assertEqual(
            units(other / triple),
            {
                f"deps/libnapi-{HASH}.rlib",
                f".fingerprint/napi-{HASH}",
                f"build/napi-sys-{HASH}",
            },
        )
        self.assertEqual(units(other), {DEPENDENCY[0]})
        self.assertFalse((other / "guards-fire").exists())

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

    def test_a_staged_move_leaves_nothing_at_its_old_path(self):
        # The caller's `git mv`, staged: the scratch tree has the file at its new path only.
        repo = fire_repo(self, keyed("a", "g1", "g1"), state="g1=ok\n")
        repo.write("old.txt", "moved\n")
        git(repo.root, "add", "old.txt")
        git(repo.root, "commit", "-q", "-m", "old")
        git(repo.root, "mv", "old.txt", "new.txt")
        tree = self.m["Tree"].make(repo.root)
        self.addCleanup(tree.remove)
        self.assertEqual((tree.path / "new.txt").read_text(), "moved\n")
        self.assertFalse((tree.path / "old.txt").exists())

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

    def test_each_extension_build_runs_once_in_a_scratch_tree_with_its_own_venv(self):
        # A bindings entry's commands before its test run are its extension build (`napi
        # build`, `maturin develop`): each runs once, into the target, in a scratch worktree
        # (napi writes into the tree it builds in) with an environment made for it. The test
        # runs don't, and neither does a one-command entry, whose build is inside its check.
        tools = Path(tempfile.mkdtemp()) / "tools.log"
        self.addCleanup(shutil.rmtree, tools.parent, True)
        napi = ["npx", "-y", "-p", "@x/cli@3", "x", "build"]
        repo = fire_repo(
            self,
            *(
                entry(
                    fid=f"js{n}",
                    guard=f"t{n}",
                    run=[napi, ["node", "--test", "--test-reporter=tap", f"t{n}"]],
                    suite="bindings",
                )
                for n in range(2)
            ),
            entry(
                fid="stub",
                guard="check.py",
                run=["npx", "--yes", "-p", "@x/check@1", "check"],
                expect="exit-nonzero",
                message="m",
                suite="bindings",
            ),
        )
        target = repo.tmp / "cache"
        ran = tools.parent / "runs.log"
        out = repo.run(
            "build",
            "--suite",
            "bindings",
            "--target-dir",
            str(target),
            FAKE_TOOLS=str(tools),
            FAKE_LOG=str(ran),
        )
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        rows = [line.split("\t") for line in tools.read_text().splitlines()]
        builds = [r for r in rows if r[0] == "npx"]
        self.assertEqual([r[4] for r in builds], [" ".join(napi[1:])], rows)
        (tool, cwd, venv, built_in, _) = builds[0]
        self.assertNotEqual(Path(cwd), repo.root)
        self.assertEqual(Path(venv), Path(cwd).parent / "venv")
        self.assertIn(
            ["uv", "venv", "--quiet", venv], [[r[0], *r[4].split()] for r in rows]
        )
        self.assertEqual(built_in, str(target))
        self.assertIn("guards: 1 builds", out.stdout)
        self.assertFalse(ran.exists(), "a test run ran")
        # The scratch tree and its environment are gone.
        self.assertEqual(repo.worktrees(), 1)
        self.assertEqual([p for p in repo.tmp.iterdir() if p != target], [])


# ── the verdict cache: --record and --reuse ─────────────────────────────────


def reused(stdout: str) -> list[str]:
    return re.findall(
        r"^reused: ([a-z0-9-]+) \(fired at [0-9a-f]{12}\)$", stdout, re.MULTILINE
    )


def firing(stdout: str) -> dict[str, str]:
    return dict(re.findall(r"^fires: ([a-z0-9-]+): (.+)$", stdout, re.MULTILINE))


# Three commands, each reading its own state file under the fake strace: `a` (two entries), `b`
# and `c`, and `p`, whose patch no command reads. `extra` goes into each state file (a trace
# key such as `gb.lists=docs`), so the record is made with it.
def cache_repo(
    test: unittest.TestCase, a: str = "", b: str = "", c: str = "", **files: str
) -> Repo:
    patch = textwrap.dedent(
        """\
        --- a/state-p.txt
        +++ b/state-p.txt
        @@ -1 +1 @@
        -gp=ok
        +gp=fail
        """
    )
    registry = "".join(
        [
            entry(
                fid="a1",
                guard="ga",
                run=["cargo", "test", "--", "--exact", "ga"],
                fault='transform = { file = "state-a.txt", replace = "w1=ok", with = "w1=ok ga=fail" }',
            ),
            entry(
                fid="a2",
                guard="ga",
                run=["cargo", "test", "--", "--exact", "ga"],
                fault='transform = { file = "state-a.txt", replace = "w2=ok", with = "w2=ok ga=fail" }',
            ),
            entry(
                fid="b",
                guard="gb",
                run=["cargo", "test", "--", "--exact", "gb"],
                fault='transform = { file = "state-b.txt", replace = "gb=ok", with = "gb=fail" }',
            ),
            entry(
                fid="c",
                guard="gc",
                run=["pytest", "-rA", "gc"],
                fault='transform = { file = "state-c.txt", replace = "gc=ok", with = "gc=fail" }',
            ),
            entry(
                fid="p",
                guard="gp",
                run=["cargo", "test", "--", "--exact", "gp"],
                fault='patch = "verify/guards/faults/p.patch"',
            ),
        ]
    )
    return Repo(
        test,
        {
            REGISTRY: registry,
            "verify/guards/faults/p.patch": patch,
            "state-a.txt": f"ga=ok w1=ok w2=ok {a}\n",
            "state-b.txt": f"gb=ok {b}\n",
            "state-c.txt": f"gc=ok {c}\n",
            "state-p.txt": "gp=ok\n",
            "docs/one.md": "one\n",
            # This script's path in the fixture: a change there moves every verdict.
            "tools/check-guards-fire.py": "# the fire script\n",
            **files,
        },
    )


CACHE = ["a1", "a2", "b", "c", "p"]


class VerdictCache(unittest.TestCase):
    """`fire --record` traces each run and writes what each entry's verdict read; `fire --reuse`
    keeps a recorded `fired` verdict only while nothing it read differs from the record's commit,
    and fires every entry it can't say that of. Each invalidation reason is one case here."""

    def record(self, repo: Repo, *extra: str, **env: str) -> Path:
        where = repo.tmp.parent / "records"
        out = repo.run("fire", "--record", str(where), *extra, **env)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertRegex(
            out.stdout, r"guards: recorded \d+ verdicts of \d+ commands in "
        )
        return where

    def reuse(
        self, repo: Repo, where: Path, *extra: str, **env: str
    ) -> subprocess.CompletedProcess[str]:
        out = repo.run("fire", "--reuse", str(where), *extra, **env)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        return out

    def edit(self, where: Path, change) -> None:
        """Rewrite the record in `where` through `change`, which edits it in place."""
        (path,) = where.glob("*.json")
        data = json.loads(path.read_text())
        change(data)
        path.write_text(json.dumps(data))

    def fires(
        self, out: subprocess.CompletedProcess[str], want: dict[str, str]
    ) -> None:
        """Exactly the entries in `want` fire, each for a reason that contains its value, and
        every other one keeps its verdict."""
        got = firing(out.stdout)
        self.assertEqual(sorted(got), sorted(want), out.stdout)
        for fid, reason in want.items():
            self.assertIn(reason, got[fid], out.stdout)
        self.assertEqual(
            sorted(reused(out.stdout)), sorted(set(CACHE) - set(want)), out.stdout
        )
        self.assertEqual(sorted(fired_ids(out.stdout)), sorted(want), out.stdout)

    def test_a_record_keeps_every_fired_verdict_while_nothing_it_read_changed(self):
        repo = cache_repo(self)
        where = self.record(repo)
        out = self.reuse(repo, where)
        self.fires(out, {})
        self.assertIn(
            f"keeps {len(CACHE)} of {len(CACHE)} entries' fired verdicts", out.stdout
        )
        self.assertIn(
            "every selected entry keeps its recorded verdict, so nothing to fire",
            out.stdout,
        )
        self.assertEqual(repo.worktrees(), 1)

    def test_a_changed_file_its_command_read_fires_its_entries_alone(self):
        repo = cache_repo(self)
        where = self.record(repo)
        repo.write("state-b.txt", "gb=ok extra=ok\n")
        repo.commit()
        self.fires(self.reuse(repo, where), {"b": "state-b.txt changed since"})

    def test_an_uncommitted_change_counts_as_the_tree(self):
        repo = cache_repo(self)
        where = self.record(repo)
        repo.write("state-c.txt", "gc=ok extra=ok\n")
        self.fires(self.reuse(repo, where), {"c": "state-c.txt changed since"})

    def test_a_changed_patch_fires_its_entry_though_no_command_reads_it(self):
        repo = cache_repo(self)
        where = self.record(repo)
        repo.write(
            "verify/guards/faults/p.patch",
            (repo.root / "verify/guards/faults/p.patch").read_text() + "\n",
        )
        repo.commit()
        self.fires(
            self.reuse(repo, where), {"p": "verify/guards/faults/p.patch changed since"}
        )

    def test_a_changed_registry_entry_fires(self):
        repo = cache_repo(self)
        where = self.record(repo)
        text = (repo.root / REGISTRY).read_text()
        repo.write(
            REGISTRY,
            text.replace('guard = "gb"', 'guard = "gb"\nmessage = "FAILED"', 1),
        )
        repo.commit()
        self.fires(
            self.reuse(repo, where),
            {"b": "its entry in verify/guards/faults/fixture.toml changed"},
        )

    def test_a_changed_tool_fires_the_commands_that_ran_it(self):
        repo = cache_repo(self)
        where = self.record(repo)
        pytest = repo.bin / "pytest"
        pytest.write_text(pytest.read_text() + "# another build of the tool\n")
        self.fires(self.reuse(repo, where), {"c": "differs from the one"})

    def test_a_new_entry_in_a_listed_directory_fires_and_a_changed_one_does_not(self):
        repo = cache_repo(self, b="gb.lists=docs")
        where = self.record(repo)
        repo.write("docs/one.md", "changed\n")
        repo.commit()
        self.fires(self.reuse(repo, where), {})
        repo.write("docs/two.md", "two\n")
        repo.commit()
        self.fires(
            self.reuse(repo, where), {"b": "docs/ gained or lost an entry since"}
        )

    def test_a_new_file_where_a_command_looked_and_found_none_fires(self):
        repo = cache_repo(self, c="gc.probes=extra.toml")
        where = self.record(repo)
        repo.write("extra.toml", "x = 1\n")
        repo.commit()
        self.fires(self.reuse(repo, where), {"c": "extra.toml is new since"})

    def test_a_path_a_command_found_there_that_is_gone_fires(self):
        repo = cache_repo(self, a="ga.stats=docs")
        where = self.record(repo)
        (repo.root / "docs/one.md").unlink()
        repo.commit()
        self.fires(
            self.reuse(repo, where),
            {"a1": "docs is gone since", "a2": "docs is gone since"},
        )

    def test_a_command_that_reads_the_git_history_always_fires(self):
        repo = cache_repo(self, b="gb.git=log")
        where = self.record(repo)
        self.fires(self.reuse(repo, where), {"b": "its command reads the git history"})

    def test_a_command_that_lists_the_tracked_files_fires_when_a_path_is_added(self):
        repo = cache_repo(self, b="gb.git=ls-files")
        where = self.record(repo)
        repo.write("docs/one.md", "changed\n")
        repo.commit()
        self.fires(self.reuse(repo, where), {})
        repo.write("elsewhere.txt", "new\n")
        repo.commit()
        self.fires(
            self.reuse(repo, where), {"b": "its command lists the tracked files"}
        )

    def test_a_command_that_downloads_by_a_range_always_fires(self):
        # uv resolves `boto3>=1.40` to whatever is newest when it runs; an exact pin is the
        # same download on every run.
        repo = cache_repo(
            self,
            b="gb.runs=uv+run+--with+pyyaml==6.0.3",
            c="gc.runs=uv+run+--with+boto3>=1.40",
        )
        where = self.record(repo)
        self.fires(self.reuse(repo, where), {"c": "its command downloads boto3>=1.40"})

    def test_a_cargo_lock_change_fires_a_build_that_compiles_a_changed_package_only(
        self,
    ):
        lock = '[[package]]\nname = "dep"\nversion = "1.0.0"\n\n[[package]]\nname = "other"\nversion = "2.0.0"\n'
        repo = cache_repo(self, **{"Cargo.lock": lock})
        where = self.record(repo)

        def compiles_dep(data: dict) -> None:
            for command in data["commands"]:
                if command["run"] == [["cargo", "test", "--", "--exact", "gb"]]:
                    command["closure"]["locked"] = ["dep 1.0.0"]

        self.edit(where, compiles_dep)
        repo.write("Cargo.lock", lock.replace("2.0.0", "2.0.1"))
        repo.commit()
        self.fires(self.reuse(repo, where), {})
        repo.write("Cargo.lock", lock.replace("1.0.0", "1.0.1"))
        repo.commit()
        self.fires(self.reuse(repo, where), {"b": "Cargo.lock changed dep 1.0.0 since"})

    def test_a_change_to_the_fire_script_fires_every_entry(self):
        repo = cache_repo(self)
        where = self.record(repo)
        repo.write("tools/check-guards-fire.py", "# the fire script, changed\n")
        repo.commit()
        reason = "tools/check-guards-fire.py changed since"
        self.fires(self.reuse(repo, where), dict.fromkeys(CACHE, reason))

    def test_a_different_environment_or_timeout_fires_every_entry(self):
        repo = cache_repo(self)
        where = self.record(repo, CARGO_FIXTURE="1")
        out = self.reuse(repo, where, CARGO_FIXTURE="2")
        self.fires(out, dict.fromkeys(CACHE, "the environment differs from"))
        self.assertIn("in CARGO_FIXTURE", out.stdout)
        out = self.reuse(repo, where, "--timeout", "60", CARGO_FIXTURE="1")
        self.fires(out, dict.fromkeys(CACHE, "in --timeout"))
        # A credential is never written down, so it moves nothing.
        self.fires(
            self.reuse(repo, where, CARGO_FIXTURE="1", CARGO_REGISTRY_TOKEN="x"), {}
        )
        self.assertNotIn("CARGO_REGISTRY_TOKEN", (where / "all.json").read_text())

    def test_no_record_or_no_trace_or_no_verdict_fires(self):
        repo = cache_repo(self)
        empty = repo.tmp.parent / "none"
        empty.mkdir()
        self.fires(
            self.reuse(repo, empty), dict.fromkeys(CACHE, "no record has its command")
        )
        where = self.record(repo, FAKE_STRACE="silent")
        self.fires(
            self.reuse(repo, where), dict.fromkeys(CACHE, "has no trace of its command")
        )

    def test_a_record_without_the_entry_or_from_another_clone_fires(self):
        repo = cache_repo(self)
        where = self.record(repo)

        def forget_c(data: dict) -> None:
            for command in data["commands"]:
                command["entries"].pop("c", None)

        self.edit(where, forget_c)
        self.fires(self.reuse(repo, where), {"c": "has no verdict for it"})
        self.edit(where, lambda data: data.update(commit="0" * 40))
        gone = dict.fromkeys(CACHE, "isn't in this clone")
        self.fires(self.reuse(repo, where), {**gone, "c": "has no verdict for it"})

    def test_an_entry_the_record_says_did_not_fire_fires_again(self):
        repo = cache_repo(self)
        text = (repo.root / REGISTRY).read_text()
        repo.write(REGISTRY, text.replace('with = "gb=fail"', 'with = "gb=ok more=ok"'))
        repo.commit()
        where = repo.tmp.parent / "records"
        out = repo.run("fire", "--record", str(where))
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertIn("DID NOT FIRE: b", out.stdout)
        out = repo.run("fire", "--reuse", str(where))
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        got = firing(out.stdout)
        self.assertEqual(sorted(got), ["b"], out.stdout)
        self.assertIn("says did not fire", got["b"])

    def test_each_legs_record_answers_for_its_own_commands(self):
        # CI's legs each record their shard; a pull request reads them from one directory.
        repo = cache_repo(self)
        where = self.record(repo, "--shard", "0/2")
        self.record(repo, "--shard", "1/2")
        self.assertEqual(
            sorted(p.name for p in where.glob("*.json")),
            ["shard-0-of-2.json", "shard-1-of-2.json"],
        )
        self.fires(self.reuse(repo, where), {})

    def test_a_command_one_leg_recorded_keeps_its_verdict_on_another(self):
        # A new entry on b's command makes it as heavy as a's, and the split moves c from leg 1
        # to leg 0 and p from leg 0 to leg 1. Each leg reads both legs' records and keeps the
        # verdicts of the commands the other leg recorded; one that had only its own leg's
        # record would fire them.
        repo = cache_repo(self)
        where = self.record(repo, "--shard", "0/2")
        self.record(repo, "--shard", "1/2")
        script = fire_module()

        def legs() -> dict[str, int]:
            faults, _ = script["load"](repo.root)
            return {f.id: k for k in (0, 1) for f in script["shard"](faults, k, 2)}

        before = legs()
        text = (repo.root / REGISTRY).read_text()
        repo.write(
            REGISTRY,
            text
            + entry(
                fid="b2",
                guard="gb",
                run=["cargo", "test", "--", "--exact", "gb"],
                fault='transform = { file = "state-b.txt", replace = "gb=ok", with = "gb=ok gb=fail" }',
            ),
        )
        repo.commit()
        after = legs()
        self.assertEqual((before["c"], after["c"]), (1, 0), "the premise: c moves")
        self.assertEqual((before["p"], after["p"]), (0, 1), "the premise: p moves")
        kept = {
            k: reused(self.reuse(repo, where, "--shard", f"{k}/2").stdout)
            for k in (0, 1)
        }
        self.assertIn("c", kept[0], "leg 0 fires c, which leg 1 recorded")
        self.assertIn("p", kept[1], "leg 1 fires p, which leg 0 recorded")
        self.assertEqual(sorted(kept[0] + kept[1]), sorted(set(CACHE)))
        own = repo.tmp.parent / "own"
        own.mkdir()
        shutil.copy(where / "shard-0-of-2.json", own)
        out = self.reuse(repo, own, "--shard", "0/2")
        self.assertEqual(firing(out.stdout).get("c"), "no record has its command")

    def test_a_shard_keeps_its_slice_whatever_record_it_reads(self):
        # The slice is cut before --reuse, so a leg that restored another record, or none,
        # still fires within its own slice and never another's.
        repo = cache_repo(self)
        where = self.record(repo)
        empty = repo.tmp.parent / "none"
        empty.mkdir()
        for k in (0, 1):
            with self.subTest(k=k):
                kept = self.reuse(repo, where, "--shard", f"{k}/2")
                cold = self.reuse(repo, empty, "--shard", f"{k}/2")
                slice_ = BANNER.findall(kept.stdout)
                self.assertEqual(slice_, BANNER.findall(cold.stdout))
                self.assertEqual(
                    sorted(reused(kept.stdout)), sorted(fired_ids(cold.stdout))
                )

    def test_record_needs_a_committed_tree_and_strace(self):
        repo = cache_repo(self)
        repo.write("state-a.txt", "ga=ok w1=ok w2=ok changed=ok\n")
        out = repo.run("fire", "--record", str(repo.tmp.parent / "r"))
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertIn("--record needs a committed tree", out.stderr)
        self.assertEqual(repo.worktrees(), 1)
        repo.commit()
        (repo.bin / "strace").unlink()
        # git and nothing else from the host, which has a strace of its own.
        host = repo.tmp.parent / "host"
        host.mkdir()
        (host / "git").symlink_to(shutil.which("git"))
        out = repo.run(
            "fire", "--record", str(repo.tmp.parent / "r"), PATH=f"{repo.bin}:{host}"
        )
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertIn("strace, which isn't on PATH", out.stderr)
        out = repo.run("fire", "--record", "x", "--reuse", "y")
        self.assertEqual(out.returncode, 2, out.stdout + out.stderr)


# Lines as strace 6.x writes them under `STRACE`'s flags, from a run of check-agents-md.py and a
# cargo test, with the tree's path as {tree} and the git directory's as {git}.
TRACE = """\
100 execve("./tools/gate.py", ["./tools/gate.py"], 0x7ffd /* 60 vars */) = 0
100 execve("/usr/bin/env-stand-in", ["uv", "run"], 0x7ffd /* 60 vars */ <unfinished ...>
101 openat(AT_FDCWD</elsewhere>, "/etc/ld.so.cache", O_RDONLY|O_CLOEXEC) = 3</etc/ld.so.cache>
100 <... execve resumed>)           = 0
100 openat(AT_FDCWD<{tree}>, "docs/a.md", O_RDONLY|O_CLOEXEC) = 3<{tree}/docs/a.md>
100 newfstatat(AT_FDCWD<{tree}>, "tools/yaml.py", 0x7ffc, 0) = -1 ENOENT (No such file or directory)
100 openat(AT_FDCWD<{tree}>, "docs", O_RDONLY|O_NONBLOCK|O_CLOEXEC|O_DIRECTORY) = 4<{tree}/docs>
100 stat("{tree}/crates", {{st_mode=S_IFDIR|0755, st_size=4096, ...}}) = 0
100 clone3({{flags=CLONE_VM|CLONE_VFORK|CLONE_CLEAR_SIGHAND, exit_signal=SIGCHLD, stack=0x7f, stack_size=0x9000}}, 88 <unfinished ...>
102 chdir("docs") = 0
102 openat(AT_FDCWD, "b.md", O_RDONLY) = 3<{tree}/docs/b.md>
102 execve("/usr/bin/git", ["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", "*.md"], 0x7ffd /* 60 vars */) = 0
102 openat(AT_FDCWD<{tree}/docs>, "{git}/index", O_RDONLY) = 3<{git}/index>
102 openat(AT_FDCWD<{tree}/docs>, "{git}/info/exclude", O_RDONLY) = 3<{git}/info/exclude>
100 <... clone3 resumed>)           = 102
103 fchdir(3<{tree}/docs>) = 0
103 stat("c.md", 0x7ffc) = -1 ENOENT (No such file or directory)
100 openat(5, "d.md", O_RDONLY) = 6
"""


def traced() -> bool:
    """Whether a tracer is attached to this process (`TracerPid` in /proc)."""
    try:
        status = Path("/proc/self/status").read_text()
    except OSError:
        return False
    match = re.search(r"^TracerPid:\s+(\d+)$", status, re.MULTILINE)
    return bool(match and match.group(1) != "0")


class TraceReading(unittest.TestCase):
    """`read_trace` over real strace lines: which paths are the tree's, what each call says about
    one, and what makes a trace say nothing."""

    def setUp(self):
        self.module = fire_module()
        self.tree = "/work/tree"
        self.git = "/work/repo/.git/worktrees/tree"
        files = frozenset(
            [
                "docs/a.md",
                "docs/b.md",
                "tools/gate.py",
                "crates/x/src/lib.rs",
                "crates/x/Cargo.toml",
                "crates/y/src/lib.rs",
                "crates/y/Cargo.toml",
                "Cargo.lock",
            ]
        )
        self.view = self.module["View"](
            trees=(self.tree,),
            root=("/work/repo",),
            git_dirs=(self.git,),
            skip=("/work/target",),
            files=files,
            dirs=self.module["ancestors"](files),
            graph=None,
        )

    def read(self, text: str, view=None) -> object:
        path = Path(tempfile.mkdtemp()) / "trace.0"
        self.addCleanup(shutil.rmtree, path.parent)
        path.write_text(text.format(tree=self.tree, git=self.git))
        return self.module["read_trace"]([path], view or self.view)

    def test_each_call_places_its_path_and_says_what_it_read(self):
        out = self.read(TRACE.replace('100 openat(5, "d.md", O_RDONLY) = 6\n', ""))
        self.assertTrue(out.traced)
        # Read through AT_FDCWD's path, after a chdir, and a script the command ran.
        self.assertEqual(out.content, {"docs/a.md", "docs/b.md", "tools/gate.py"})
        # Looked for and not found, one through a relative stat after fchdir.
        self.assertEqual(out.absent, {"tools/yaml.py", "docs/c.md"})
        self.assertEqual(out.listed, {"docs"})
        self.assertEqual(out.present, {"crates"})
        # git ls-files read the index, which is the tree's list of paths; info/exclude is the
        # clone's setting.
        self.assertEqual(out.git, "tree")
        self.assertEqual(out.tools, {"/usr/bin/env-stand-in", "/usr/bin/git"})

    def test_a_path_against_a_directory_strace_couldnt_name_says_nothing(self):
        self.assertFalse(self.read(TRACE).traced)

    def test_a_trace_that_never_shows_its_command_starting_says_nothing(self):
        self.assertFalse(
            self.read(
                TRACE.split("\n", 1)[1].replace(
                    "100 <... execve resumed>)           = 0\n", ""
                )
            ).traced
        )
        self.assertFalse(self.module["read_trace"]([], self.view).traced)

    def test_a_git_command_that_reads_the_history_or_a_non_git_reader_is_history(self):
        head = '100 execve("/usr/bin/git", ["git", "show", "HEAD:x"], 0x7ffd /* 1 vars */) = 0\n'
        self.assertEqual(
            self.read(
                head + '100 openat(AT_FDCWD<{tree}>, "{git}/HEAD", O_RDONLY) = 3\n'
            ).git,
            "history",
        )
        tool = '100 execve("/usr/bin/python3", ["python3"], 0x7ffd /* 1 vars */) = 0\n'
        self.assertEqual(
            self.read(
                tool + '100 openat(AT_FDCWD<{tree}>, ".git/HEAD", O_RDONLY) = 3\n'
            ).git,
            "history",
        )
        # cargo lists a package's files through libgit2; only `package` and `publish` read the
        # commit.
        for command, want in (("doc", "tree"), ("package", "history")):
            cargo = f'100 execve("/usr/bin/cargo", ["cargo", "{command}", "-p", "x"], 0x7ffd /* 1 vars */) = 0\n'
            head = '100 openat(AT_FDCWD<{tree}>, "{git}/HEAD", O_RDONLY) = 3\n'
            self.assertEqual(self.read(cargo + head).git, want, command)
        # Looking at `.git` is finding the repo, not reading it, and the ignore rules a clone
        # keeps there are the same in every clone.
        self.assertIsNone(
            self.read(
                tool + '100 newfstatat(AT_FDCWD<{tree}>, ".git", 0x7ffc, 0) = 0\n'
            ).git
        )
        self.assertIsNone(
            self.read(
                tool
                + '100 openat(AT_FDCWD<{tree}>, "{git}/info/exclude", O_RDONLY) = 3\n'
            ).git
        )
        self.assertIsNone(
            self.read(tool + '100 openat(AT_FDCWD<{tree}>, ".git", O_RDONLY) = 3\n').git
        )
        git_reads = self.module["git_reads"]
        for argv, want in (
            (["git", "ls-files", "-z"], "tree"),
            (["git", "ls-files", "-s"], "history"),
            (["git", "-C", "x", "ls-files", "--cached", "--", "a"], "tree"),
            (["git", "grep", "--untracked", "-n", "pattern", "--", "."], "tree"),
            (["git", "grep", "-e", "a", "-e", "b", "--", "."], "tree"),
            (["git", "grep", "pattern", "HEAD"], "history"),
            (["git", "grep", "-e", "a", "HEAD", "--", "."], "history"),
            (["git", "grep", "--cached", "pattern"], "history"),
            (["git", "grep", "--no-such-flag", "pattern"], "history"),
            (["git", "rev-parse", "--show-toplevel"], "tree"),
            (["git", "rev-parse", "HEAD"], "history"),
            (["git", "diff", "HEAD"], "history"),
            (["git"], "history"),
        ):
            with self.subTest(argv=argv):
                self.assertEqual(git_reads(argv), want)

    def test_a_download_by_a_range_is_floating_and_one_by_an_exact_version_is_not(self):
        downloads, floating = self.module["downloads"], self.module["floating"]
        with tempfile.TemporaryDirectory() as tree:
            Path(tree, "gate.py").write_text(
                '# /// script\n# dependencies = ["boto3>=1.40", "pyyaml==6.0.3"]\n# ///\n'
            )
            for argv, want in (
                (["uv", "run", "--with", "pyyaml==6.0.3", "python", "-m", "x"], []),
                (["uv", "run", "--with=boto3>=1.40", "python"], ["boto3>=1.40"]),
                (["uv", "run", "--script", "gate.py"], ["boto3>=1.40"]),
                (["uvx", "maturin@1.14.1", "develop"], []),
                (["uvx", "ruff", "check"], ["ruff"]),
                (
                    ["npx", "-y", "-p", "@napi-rs/cli@3", "napi", "build"],
                    ["@napi-rs/cli@3"],
                ),
                (["npx", "--package", "typedoc@0.28.14", "--", "typedoc"], []),
                (["uv", "run", "-p", "3.12", "python"], []),
                (["python3", "--with", "boto3>=1.40"], []),
                (
                    [
                        "uv",
                        "pip",
                        "install",
                        "-q",
                        "--python",
                        "v/bin/python",
                        "out/w.whl",
                    ],
                    [],
                ),
                (["uv", "pip", "install", "pytest==9.1.1", "mypy"], ["mypy"]),
                (
                    ["uv", "pip", "install", "-r", "req.txt"],
                    ["the requirements in req.txt"],
                ),
            ):
                with self.subTest(argv=argv):
                    got = [d for d in downloads(argv, tree, tree) if floating(d)]
                    self.assertEqual(got, want)

    def test_a_cargo_process_reads_a_member_outside_its_build_for_its_manifest_alone(
        self,
    ):
        graph = self.module["Graph"](
            members={"x": "crates/x", "y": "crates/y"},
            names={"x": "x", "y": "y"},
            deps={
                "x": [("dep", False), ("devdep", True)],
                "y": [],
                "dep": [("devdep2", True)],
            },
            locked={
                "x": "x 0.1.0",
                "y": "y 0.1.0",
                "dep": "dep 1.0.0",
                "devdep": "devdep 1.0.0",
                "devdep2": "devdep2 1.0.0",
            },
        )
        view = dataclasses.replace(self.view, graph=graph)
        cargo = '100 execve("/usr/bin/cargo", ["cargo", "test", "-p", "x"], 0x7ffd /* 1 vars */) = 0\n'
        calls = "".join(
            f"100 {call}\n"
            for call in (
                'openat(AT_FDCWD<{tree}>, "crates/y/Cargo.toml", O_RDONLY) = 3',
                'statx(AT_FDCWD<{tree}>, "crates/y/src/lib.rs", AT_STATX_SYNC_AS_STAT, STATX_ALL, 0x7ffc) = 0',
                'statx(AT_FDCWD<{tree}>, "crates/y/tests", AT_STATX_SYNC_AS_STAT, STATX_ALL, 0x7ffc) = -1 ENOENT (No such file or directory)',
                'statx(AT_FDCWD<{tree}>, "crates/x/src/lib.rs", AT_STATX_SYNC_AS_STAT, STATX_ALL, 0x7ffc) = 0',
                'statx(AT_FDCWD<{tree}>, "crates/x/tests", AT_STATX_SYNC_AS_STAT, STATX_ALL, 0x7ffc) = -1 ENOENT (No such file or directory)',
                'openat(AT_FDCWD<{tree}>, "Cargo.lock", O_RDONLY) = 3',
            )
        )
        out = self.read(cargo + calls, view)
        self.assertEqual(out.content, {"crates/y/Cargo.toml", "crates/x/src/lib.rs"})
        self.assertEqual(out.present, {"crates/y/src/lib.rs"})
        self.assertEqual(out.absent, {"crates/x/tests"})
        # A dev-dependency counts for the package the command selects, not for one it reaches.
        self.assertEqual(out.locked, {"x 0.1.0", "dep 1.0.0", "devdep 1.0.0"})
        self.assertIn("rustc -vV", out.tools)
        # A build the graph can't name counts every file cargo touched, the lockfile's text too.
        unknown = self.read(cargo.replace('"-p", "x"', '"-p", "z"') + calls, view)
        self.assertLessEqual({"crates/y/src/lib.rs", "Cargo.lock"}, unknown.content)
        self.assertEqual(unknown.locked, set())

    def test_a_program_under_the_temp_directory_is_the_commands_own_and_a_script_brings_its_interpreter(
        self,
    ):
        scratch = Path(tempfile.gettempdir()) / "a-fake-tool"
        body = f'100 execve("{scratch}", ["t"], 0x7ffd /* 1 vars */) = 0\n'
        self.assertEqual(self.read(body).tools, set())
        fake = Path(tempfile.gettempdir()) / "bin" / "npx"
        body = f'100 execve("{fake}", ["npx", "-p", "@x/cli@3", "x"], 0x7ffd /* 1 vars */) = 0\n'
        self.assertEqual(self.read(body).floating, set())
        with tempfile.TemporaryDirectory() as directory:
            tree = Path(directory, "tree")
            (tree / "tools").mkdir(parents=True)
            (tree / "tools" / "gate.py").write_text("#!/usr/bin/env python3\n")
            view = dataclasses.replace(self.view, trees=(str(tree),))
            path = Path(directory, "trace.0")
            path.write_text(
                f'100 execve("{tree}/tools/gate.py", ["gate"], 0x7ffd /* 1 vars */) = 0\n'
            )
            out = self.module["read_trace"]([path], view)
        self.assertEqual(out.content, {"tools/gate.py"})
        self.assertEqual(out.tools, {os.path.realpath("/usr/bin/env")})
        # A tool uvx or npx fetched into its cache is a download: the pin answers for it.
        cache = "/home/runner/.cache/uv"
        view = dataclasses.replace(self.view, downloads=(cache,))
        fetched = f'100 execve("{cache}/archive-v0/x/bin/maturin", ["maturin"], 0x7ffd /* 1 vars */) = 0\n'
        self.assertEqual(self.read(fetched, view).tools, set())
        self.assertEqual(
            self.read(fetched).tools, {f"{cache}/archive-v0/x/bin/maturin"}
        )

    @unittest.skipUnless(shutil.which("strace"), "needs strace")
    @unittest.skipIf(traced(), "already traced, as every run under `fire --record` is")
    def test_the_real_strace_names_what_a_command_read(self):
        # The flags `--record` runs strace with, over a command that reads one file, lists one
        # directory and looks for one it doesn't find. A traced process can't trace another,
        # so this case runs where the suite runs plain: a pull request's legs, `check`.
        with tempfile.TemporaryDirectory() as directory:
            tree = Path(directory, "tree")
            (tree / "docs").mkdir(parents=True)
            (tree / "docs" / "a.md").write_text("a\n")
            code = "import os; open('docs/a.md').read(); os.listdir('docs'); os.path.exists('gone.txt')"
            trace = Path(directory, "trace")
            done = subprocess.run(
                [*self.module["STRACE"], f"{trace}.0", sys.executable, "-c", code],
                cwd=tree,
            )
            self.assertEqual(done.returncode, 0)
            files = frozenset(["docs/a.md"])
            view = dataclasses.replace(
                self.view,
                trees=(str(tree), os.path.realpath(tree)),
                files=files,
                dirs=self.module["ancestors"](files),
            )
            out = self.module["read_trace"]([Path(f"{trace}.0")], view)
        self.assertTrue(out.traced)
        self.assertEqual(out.content, {"docs/a.md"})
        # Python lists the directory it imports from, the tree's root here, as well.
        self.assertIn("docs", out.listed)
        self.assertIn("gone.txt", out.absent)
        self.assertIn(os.path.realpath(sys.executable), out.tools)


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
            REGISTRY: commands_registry(spec) + extra,
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
        # One command of each kind, in registry order: a CLI command with two entries (24 each,
        # 108 for the command), a Rust one (14, 83), a script one with two (17 each, 47), a
        # `napi build` one (119, 280), a `maturin develop` one (53, 280) and a clippy command
        # with four lint entries, which fire in one batch (2 each, 63). Over three shards the
        # napi command is one shard, the maturin and clippy commands a second, and the rest the
        # third. A count, an entry priced without its command's clean and restored runs, or a
        # CLI, script, bindings, napi or lint entry priced as another kind each gives other
        # slices.
        napi = ["npx", "-y", "-p", "@napi-rs/cli@3", "napi", "build"]
        maturin = ["uvx", "maturin@1.14.1", "develop", "-q"]
        clippy = ["cargo", "clippy", "-p", "x", "--all-targets", "--", "-D", "warnings"]
        spec = [
            (
                "c",
                "rust",
                ["cargo", "test", "-p", "microvms-cli", "--", "--exact", "c"],
                "exit-nonzero",
                2,
            ),
            ("r", "rust", ["cargo", "test", "--", "--exact", "r"], "exit-nonzero", 1),
            ("s", "script", ["python3", "s.py"], "exit-nonzero", 2),
            (
                "n",
                "bindings",
                [napi, ["node", "--test", "--test-reporter=tap", "n"]],
                "exit-nonzero",
                1,
            ),
            (
                "o",
                "bindings",
                [maturin, ["pytest", "-rA", "t.py::o"]],
                "exit-nonzero",
                1,
            ),
            ("l", "rust", clippy, "lint-error", 4),
        ]
        registry = "".join(
            entry(
                fid=f"{name}{i}",
                guard=name,
                run=run,
                expect=expect,
                suite=suite,
                message="no",
                fault=f'transform = {{ file = "state.txt", replace = "{name}{i}=ok", with = "{name}{i}=bad" }}',
            )
            for name, suite, run, expect, size in spec
            for i in range(size)
        )
        state = " ".join(f"{n}{i}=ok" for n, _, _, _, size in spec for i in range(size))
        repo = Repo(self, {REGISTRY: registry, "state.txt": state + "\n"})
        script = runpy.run_path(str(SCRIPT))
        faults, problems = script["load"](repo.root)
        self.assertEqual(problems, [])
        self.assertEqual(
            [[f.id for f in script["shard"](faults, k, 3)] for k in (0, 1, 2)],
            [["n0"], ["o0", "l0", "l1", "l2", "l3"], ["c0", "c1", "r0", "s0", "s1"]],
            "the shards aren't split by the commands' cost",
        )

    def test_the_registrys_own_shards_partition_it(self):
        # The split CI makes, on the registry it makes it of: every suite's entries in CI's
        # six shards.
        script = runpy.run_path(str(SCRIPT))
        faults, problems = script["load"](HERE.parent)
        self.assertEqual(problems, [])
        selected = [f for f in faults if f.suite in ("rust", "script", "bindings")]
        self.assertTrue(selected, "the registry has no entry of CI's suites to split")
        key = script["command_key"]
        shards = [script["shard"](selected, k, 6) for k in range(6)]
        ids = [f.id for s in shards for f in s]
        self.assertEqual(sorted(ids), sorted(f.id for f in selected))
        self.assertEqual(len(ids), len(set(ids)))
        owners: dict[tuple, set[int]] = {}
        for number, part in enumerate(shards):
            self.assertTrue(part, f"shard {number} of 6 is empty")
            for fault in part:
                owners.setdefault(key(fault), set()).add(number)
        self.assertEqual([k for k, o in owners.items() if len(o) > 1], [])


# ── the `guards` job's steps, as ci.yml writes them (#323) ────────────────────

CI = HERE.parent / ".github/workflows/ci.yml"
# The two conditions the job's fire steps may carry; any other fails the case by name.
ON_PULL_REQUEST = "github.event_name == 'pull_request'"
ON_PUSH = "github.event_name != 'pull_request'"
# What a pull request gives the steps' `${{ }}`s beyond the leg's own.
EXPRESSIONS: dict[str, str] = {}
# Where the fire steps keep the record, which the cache steps save and restore.
VERDICTS = "$RUNNER_TEMP/guards-verdicts"
VERDICTS_PATH = "${{ runner.temp }}/guards-verdicts"
VERDICTS_KEY = "guards-verdicts-${{ matrix.shard }}-of-${{ strategy.job-total }}-"
EXPRESSION = re.compile(r"\$\{\{\s*(.+?)\s*\}\}")
MISE_RUN = re.compile(r"(?<![\w./-])mise\s+run\s+(\S+)([^\n]*)")
# The action each job installs mise and its tools through.
MISE_ACTION = "./.github/actions/mise"


def workflow_jobs() -> dict:
    # Imported here, not at the top, so the module loads without pyyaml: every class but
    # this one reads no workflow.
    import yaml

    workflow = yaml.safe_load(CI.read_text()) or {}
    return workflow.get("jobs") or {}


def guards_job() -> dict:
    return workflow_jobs().get("guards") or {}


def parity() -> dict:
    """check-ci-parity.py, whose loader reads mise.toml's tasks and its includes. Loaded here,
    not at the top, since it imports pyyaml."""
    return runpy.run_path(str(HERE / "check-ci-parity.py"))


def task_command(step: dict) -> tuple[str, str, dict[str, str]]:
    """The task a `mise run <task> <arguments>` step runs, the command it runs, and the `env`
    the task sets. mise puts a step's arguments after the task's last line, and the task's
    `env` wins over the environment it inherits; test_check_ci_parity.py's `CiCommands` holds
    real mise to both."""
    match = MISE_RUN.search(step.get("run", ""))
    if match is None:
        raise AssertionError(f"`{step.get('name')}` runs no `mise run`")
    name, args = match.groups()
    task = parity()["load_tasks"](HERE.parent).get(name)
    if task is None:
        raise AssertionError(
            f"`{step.get('name')}` runs `mise run {name}`, which mise.toml lacks"
        )
    run = task.get("run")
    if not isinstance(run, str):
        raise AssertionError(
            f"`{name}` isn't one command, which these cases read it as"
        )
    env = {str(k): str(v) for k, v in (task.get("env") or {}).items()}
    return name, run + args, env


def commands_of(step: dict) -> list[str]:
    """Every command a step's `mise run` reaches, through the tasks its task calls."""
    match = MISE_RUN.search(step.get("run", ""))
    if match is None:
        return []
    script = parity()
    tasks = script["load_tasks"](HERE.parent)
    out = []
    for name in script["reached"](tasks, [match.group(1)]):
        run = tasks[name].get("run")
        out += [
            e for e in (run if isinstance(run, list) else [run]) if isinstance(e, str)
        ]
    return out


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
    """Every push to main fires every entry and records what each verdict read, and a pull
    request fires the entries whose recorded verdicts it can't keep (D31), each as a matrix of
    shards whose combined result is the required check (D35, #345). The steps run as ci.yml
    writes them, once per shard, against a fixture repository."""

    def legs(self) -> list:
        legs = ((guards_job().get("strategy") or {}).get("matrix") or {}).get("shard")
        self.assertTrue(legs, "the guards job has no `shard` matrix")
        return list(legs)

    def fire_steps(self, event: str) -> list[dict]:
        steps = [
            s
            for s in guards_job().get("steps") or []
            if any("check-guards-fire.py fire" in c for c in commands_of(s))
        ]
        self.assertTrue(
            steps,
            "ci.yml's guards job has no step whose task runs `check-guards-fire.py fire`",
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
        self,
        repo: Repo,
        step: dict,
        shard: int = 0,
        total: int = 1,
        temp: Path | None = None,
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

        _, command, task_env = task_command(step)
        env = {
            k: EXPRESSION.sub(value, str(v)) for k, v in (step.get("env") or {}).items()
        } | task_env
        # The step runs the checkout's own script; this one stands in, pointed at the fixture.
        run = command.replace(
            "./tools/check-guards-fire.py",
            f"{shlex.quote(sys.executable)} {shlex.quote(str(SCRIPT))} --root {shlex.quote(str(repo.root))}",
        )
        path = os.pathsep.join([str(repo.bin), os.environ.get("PATH", "")])
        temp = temp or repo.tmp.parent / "runner-temp"
        temp.mkdir(parents=True, exist_ok=True)
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", run],
            cwd=repo.root,
            capture_output=True,
            text=True,
            env=clean_env(
                PATH=path, TMPDIR=str(repo.tmp), RUNNER_TEMP=str(temp), **env
            ),
        )

    def test_a_push_records_every_entry_and_a_pull_request_fires_what_it_cant_keep(
        self,
    ):
        # One entry of each other suite beside the fixture's rust ones: this job fires all
        # three, each worker's bindings entries in an environment of its own.
        script = entry(
            fid="sc",
            guard="tests/test_s.py::test_s",
            run=["pytest", "-rA", "tests/test_s.py::test_s"],
            suite="script",
            fault='transform = { file = "state-s.txt", replace = "tests/test_s.py::test_s=ok", with = "tests/test_s.py::test_s=fail" }',
        )
        binding = entry(
            fid="bi",
            guard="t",
            run=["node", "--test", "--test-reporter=tap", "t"],
            suite="bindings",
            fault='transform = { file = "state-js.txt", replace = "t=ok", with = "t=fail" }',
        )
        repo = kinds_repo(self, extra=script + binding)
        # The step's `--target-dir target` is inside the checkout, as on the runner.
        repo.write(".gitignore", "target/\n")
        repo.write("state-s.txt", "tests/test_s.py::test_s=ok\n")
        repo.write("state-js.txt", "t=ok\n")
        repo.commit("main")
        legs = self.legs()

        def each_leg(event: str, step: dict, temp) -> list[str]:
            ids: list[str] = []
            for shard in legs:
                out = self.run_step(repo, step, shard, len(legs), temp(shard))
                self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
                ids += fired_ids(out.stdout)
            self.assertEqual(
                len(ids), len(set(ids)), f"{event}: an entry fires in two shards: {ids}"
            )
            return ids

        # Each leg on a runner of its own: its temp directory, and the cache ci.yml's steps save
        # into and restore from, by their own keys, paths and prefixes.
        cache: dict[str, Path] = {}
        (push,) = self.fire_steps("push")
        fired = []
        for shard in legs:
            temp = repo.tmp.parent / f"main-temp-{shard}"
            out = self.run_step(repo, push, shard, len(legs), temp)
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            fired += fired_ids(out.stdout)
            self.assertEqual(
                sorted(p.name for p in (temp / "guards-verdicts").glob("*.json")),
                [f"shard-{shard}-of-{len(legs)}.json"],
                "push: each leg writes its own record",
            )
            for step in self.cache_steps("actions/cache/save", "push"):
                given = step["with"]
                key = self.render(given["key"], shard, len(legs), temp)
                cache[key] = Path(self.render(given["path"], shard, len(legs), temp))
        self.assertEqual(
            len(fired), len(set(fired)), f"push: an entry fires twice: {fired}"
        )
        self.assertEqual(set(fired), {*ORDER, "sc", "bi"}, "push: the shards together")
        # HEAD plays the pull request's merge commit: one rust, one script and one bindings
        # entry's files change, and a new entry on `crate`'s command makes it heavier, which
        # moves most commands to another leg, as a pull request that adds a fault does (#416).
        script_module = fire_module()
        before = self.slices(script_module, repo, len(legs))
        for path in ("state-a.txt", "state-s.txt", "state-js.txt"):
            repo.write(path, (repo.root / path).read_text() + "extra=ok\n")
        repo.write(
            REGISTRY,
            (repo.root / REGISTRY).read_text()
            + entry(
                fid="crate2",
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
                fault='transform = { file = "state-d.txt", replace = "tests::the_d_guard=ok", with = "tests::the_d_guard=ok tests::the_d_guard=fail" }',
            ),
        )
        repo.commit()
        after = self.slices(script_module, repo, len(legs))
        # The premise: an entry nothing changed for, whose command another leg recorded.
        self.assertNotEqual(
            before["lint"], after["lint"], "lint's command stayed on its leg"
        )

        def restored(shard: int) -> Path:
            temp = repo.tmp.parent / f"pr-temp-{shard}"
            for step in self.cache_steps("actions/cache/restore", "pull_request"):
                given = step["with"]
                key = self.render(given["key"], shard, len(legs), temp)
                # One prefix a line, as the action reads them.
                prefixes = [
                    line.strip()
                    for line in self.render(
                        given.get("restore-keys") or "", shard, len(legs), temp
                    ).splitlines()
                    if line.strip()
                ]
                hit = key if key in cache else None
                hit = hit or next(
                    (
                        k
                        for k in sorted(cache)
                        if any(k.startswith(p) for p in prefixes)
                    ),
                    None,
                )
                if hit is not None:
                    into = Path(self.render(given["path"], shard, len(legs), temp))
                    into.mkdir(parents=True, exist_ok=True)
                    for record in cache[hit].iterdir():
                        shutil.copy(record, into)
            return temp

        (pr,) = self.fire_steps("pull_request")
        fired = each_leg("pull_request", pr, restored)
        # Only what the change can move fires: the three entries whose files changed and the
        # new one, on whichever leg their commands landed and whichever leg recorded them.
        self.assertEqual(
            set(fired), {"a", "sc", "bi", "crate2"}, "pull_request: the shards together"
        )

    def slices(self, script: dict, repo: Repo, total: int) -> dict[str, int]:
        """Each entry's leg under the script's split of the fixture's registry."""
        faults, problems = script["load"](repo.root)
        self.assertEqual(problems, [])
        return {
            fault.id: k
            for k in range(total)
            for fault in script["shard"](faults, k, total)
        }

    def render(self, text: object, shard: int, total: int, temp: Path) -> str:
        """A cache step's `with` value as the runner of leg `shard` renders it."""
        answers = {
            **EXPRESSIONS,
            "matrix.shard": str(shard),
            "strategy.job-total": str(total),
            "runner.temp": str(temp),
            "github.sha": "the-push",
            "github.event.pull_request.base.sha": "the-base",
        }

        def value(match: re.Match) -> str:
            self.assertIn(
                match.group(1), answers, f"unmodeled `${{{{ {match.group(1)} }}}}`"
            )
            return answers[match.group(1)]

        return EXPRESSION.sub(value, str(text))

    def cache_steps(self, action: str, event: str) -> list[dict]:
        """The guards job's `action` steps that run on `event`."""
        chosen = []
        for step in guards_job().get("steps") or []:
            if not str(step.get("uses", "")).startswith(action + "@"):
                continue
            condition = step.get("if")
            if condition is None or (condition == ON_PULL_REQUEST) == (
                event == "pull_request"
            ):
                chosen.append(step)
        self.assertTrue(chosen, f"the guards job has no {action} step on {event}")
        return chosen

    def test_the_pull_request_step_is_the_push_step_reusing_what_it_records(self):
        # So the two legs can't drift apart in a flag (a lost suite, or bindings entries with
        # no environment to build into, which `fire` skips), and neither can leave the worker
        # count and per-command timeout the budgets were measured with: four workers on the
        # four-vCPU runner, and a hung fault stopped well inside the job's time.
        (pr,) = self.fire_steps("pull_request")
        (push,) = self.fire_steps("push")
        pr_argv, push_argv = (
            shlex.split(task_command(pr)[1]),
            shlex.split(task_command(push)[1]),
        )
        suites = [b for a, b in zip(push_argv, push_argv[1:]) if a == "--suite"]
        self.assertEqual(suites, ["rust", "script", "bindings"], push["run"])
        self.assertIn("--venv-per-worker", push_argv, "the push step's environments")
        options = dict(zip(push_argv, push_argv[1:]))
        # `--target-dir target` is the directory rust-cache restores.
        for flag, want in (
            ("--jobs", "4"),
            ("--timeout", "900"),
            ("--target-dir", "target"),
        ):
            self.assertEqual(options.get(flag), want, f"the push step's {flag}")
        self.assertEqual(push_argv[-2:], ["--record", VERDICTS])
        self.assertEqual(pr_argv, [*push_argv[:-2], "--reuse", VERDICTS])

    def test_each_leg_reads_every_legs_record_and_saves_its_own(self):
        # A pull request's leg restores every leg's record, each under the key that leg saves on
        # main and by its prefix, into the directory both fire steps name, after strace is
        # there: a leg's slice moves when the registry's weights do, so a leg that read only its
        # own leg's record found most of its commands in none (#416). Main never restores one,
        # a pull request never saves one, and nothing else in the workflow caches it.
        steps = guards_job().get("steps") or []
        restores = self.cache_steps("actions/cache/restore", "pull_request")
        (save,) = self.cache_steps("actions/cache/save", "push")
        self.assertEqual(
            [s for s in steps if str(s.get("uses", "")).startswith("actions/cache")],
            [*restores, save],
            "a cache step runs on both legs",
        )
        for step in restores:
            self.assertEqual(step.get("if"), ON_PULL_REQUEST)
        self.assertEqual(save.get("if"), ON_PUSH)
        for step in (*restores, save):
            self.assertEqual((step.get("with") or {}).get("path"), VERDICTS_PATH)
        self.assertEqual(save["with"].get("key"), VERDICTS_KEY + "${{ github.sha }}")
        legs = self.legs()
        read = []
        for step in restores:
            prefix = str(step["with"].get("restore-keys"))
            match = re.fullmatch(
                r"guards-verdicts-(\d+)-of-\$\{\{ strategy\.job-total \}\}-", prefix
            )
            self.assertIsNotNone(match, f"a restore of no one leg's record: {prefix}")
            read.append(int(match.group(1)))
            self.assertEqual(
                step["with"].get("key"),
                prefix + "${{ github.event.pull_request.base.sha }}",
            )
        self.assertEqual(
            sorted(read), legs, "the legs whose records a pull request reads"
        )
        (strace,) = [
            s for s in steps if "apt-get install -y strace" in s.get("run", "")
        ]
        self.assertNotIn("if", strace, "strace is installed on both legs")
        (pr,) = self.fire_steps("pull_request")
        (push,) = self.fire_steps("push")
        at = steps.index
        for step in restores:
            self.assertLess(at(strace), at(step))
            self.assertLess(at(step), at(pr))
        self.assertLess(at(push), at(save))
        caches = [
            name
            for name, job in workflow_jobs().items()
            for s in job.get("steps") or []
            if str(s.get("uses", "")).startswith("actions/cache")
            and "guards-verdicts" in json.dumps(s.get("with") or {})
        ]
        self.assertEqual(caches, ["guards"] * (len(restores) + 1), caches)

    def test_ci_local_answers_the_guards_job_as_a_pull_request(self):
        # ci:local runs one leg, the pull request's, as one run of its whole selection. Its
        # answers have to agree with each other and with ci.yml: swapped event answers would run
        # main's full fire there, and a shard count above one would fire a third of it.
        runner = runpy.run_path(str(HERE / "ci-local.py"))
        answers = runner["ON_PULL_REQUEST"]
        self.assertIs(answers.get(ON_PULL_REQUEST), True, ON_PULL_REQUEST)
        self.assertIs(answers.get(ON_PUSH), False, ON_PUSH)
        expressions = runner["EXPRESSIONS"]
        # ci-local.py clones origin/main as the base, so the base branch is main.
        self.assertEqual(expressions.get("github.base_ref"), "main")
        self.assertEqual(
            (expressions.get("matrix.shard"), expressions.get("strategy.job-total")),
            ("0", "1"),
            "ci:local runs one shard of the selection, not all of it",
        )
        jobs, _ = runner["plan"](HERE.parent)
        (guards,) = [job for job in jobs if job.name == "guards"]
        fires = {
            step.label: step.skipped for step in guards.steps if "ci:guards" in step.run
        }
        (pr,) = self.fire_steps("pull_request")
        (push,) = self.fire_steps("push")
        self.assertIsNone(
            fires.get(pr["name"], "missing"), "ci:local skips the pull request's fire"
        )
        self.assertIsNotNone(fires.get(push["name"]), "ci:local runs main's full fire")
        (name,) = [
            j for j, body in workflow_jobs().items() if body.get("name") == REQUIRED
        ]
        self.assertNotIn(
            name, {job.name for job in jobs}, f"ci:local runs the `{name}` job"
        )

    def test_the_fire_steps_build_incrementally(self):
        # dtolnay/rust-toolchain writes CARGO_INCREMENTAL=0 into the job's
        # environment, and only a step's own `env` wins over it. Each fault is an edit and a
        # rebuild of one crate, which is what incremental builds are for.
        steps = [*self.fire_steps("pull_request"), *self.fire_steps("push")]
        self.assertEqual(len(steps), 2, "the pull request's fire step and the push's")
        for step in steps:
            # The task's `env` wins over the step's and the job's.
            _, _, env = task_command(step)
            self.assertEqual(env.get("CARGO_INCREMENTAL"), "1", step.get("name"))

    def test_the_guards_legs_are_the_one_place_faults_fire(self):
        # The bindings job fired the bindings suite on one worker for ten of its thirteen
        # minutes (run 36622110872); the legs fire it now, so a second fire anywhere repeats
        # every build.
        firing = [
            (name, s.get("name"))
            for name, job in workflow_jobs().items()
            for s in job.get("steps") or []
            if any("check-guards-fire.py fire" in c for c in commands_of(s))
        ]
        self.assertTrue(firing, "no step in ci.yml fires a seeded fault")
        self.assertEqual({name for name, _ in firing}, {"guards"}, firing)

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
            if any("check-guards-fire.py build" in c for c in commands_of(s))
        ]
        argv = shlex.split(task_command(build)[1])
        (push,) = self.fire_steps("push")
        fire = shlex.split(task_command(push)[1])
        suites = [b for a, b in zip(fire, fire[1:]) if a == "--suite"]
        self.assertIn("rust", suites)
        self.assertIn("bindings", suites)
        # Every suite the legs fire that builds anything: a script entry runs no cargo
        # command and has no extension to build.
        self.assertEqual(
            [b for a, b in zip(argv, argv[1:]) if a == "--suite"],
            [s for s in suites if s != "script"],
        )
        # `napi build` runs through npx, on the Node the legs run it on: both jobs take theirs,
        # with every other tool, from mise.lock through the one action, and neither installs a
        # Node of its own.
        for job in ("guards", "guards-cache"):
            uses = [s.get("uses", "") for s in jobs[job].get("steps") or []]
            self.assertIn(MISE_ACTION, uses, job)
            self.assertFalse(
                [u for u in uses if u.startswith("actions/setup-node@")],
                f"{job} installs a Node",
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

    def test_the_tools_cache_is_saved_by_one_job_on_main(self):
        # Every job restores the tools cache, and only the `guards-cache` job saves it, on a
        # push to main, after installing every task's own tools: a save from a pull request
        # pushes the repository's caches past their cap, and one from any job but this holds
        # none of the tasks' tools, so each `guards` leg would build cargo-mutants first.
        import yaml

        action = yaml.safe_load(
            (HERE.parent / ".github/actions/mise/action.yml").read_text()
        )
        steps = (action.get("runs") or {}).get("steps") or []
        uses = [str(s.get("uses", "")).split("@")[0] for s in steps]
        self.assertIn(
            "actions/cache/restore", uses, "the action restores no tools cache"
        )
        self.assertNotIn(
            "actions/cache", uses, "the action's cache saves when its job ends"
        )
        (mise,) = [
            s for s in steps if str(s.get("uses", "")).startswith("jdx/mise-action@")
        ]
        self.assertIs(
            (mise.get("with") or {}).get("cache"),
            False,
            "mise-action saves its own cache",
        )
        workflows = [CI, HERE.parent / ".github/workflows/fuzz.yml"]
        for path in workflows:
            for name, job in (
                yaml.safe_load(path.read_text()).get("jobs") or {}
            ).items():
                for step in job.get("steps") or []:
                    with self.subTest(
                        job=name, step=step.get("name") or step.get("uses")
                    ):
                        given = step.get("with") or {}
                        if step.get("uses") == MISE_ACTION and (path, name) != (
                            CI,
                            "guards-cache",
                        ):
                            self.assertNotEqual(given.get("tools-cache"), "exact", name)
                        # The guards legs' record of each verdict is main's too, and
                        # test_each_leg_reads_the_record_its_leg_saves_on_main holds its key.
                        record = str(given.get("key", "")).startswith(VERDICTS_KEY)
                        if (
                            str(step.get("uses", "")).startswith("actions/cache/save@")
                            and not record
                        ):
                            self.assertEqual(
                                (path, name), (CI, "guards-cache"), "another job saves"
                            )
        saver = workflow_jobs()["guards-cache"]
        self.assertEqual(saver.get("if"), "github.event_name == 'push'")
        (mise_step,) = [s for s in saver["steps"] if s.get("uses") == MISE_ACTION]
        self.assertEqual((mise_step.get("with") or {}).get("tools-cache"), "exact")
        missed = f"steps.{mise_step.get('id')}.outputs.tools-cache-hit != 'true'"
        (tools,) = [
            s
            for s in saver["steps"]
            if MISE_RUN.search(s.get("run", "")) and "ci:tools" in s["run"]
        ]
        self.assertEqual(tools.get("if"), missed)
        (save,) = [
            s
            for s in saver["steps"]
            if str(s.get("uses", "")).startswith("actions/cache/save@")
        ]
        self.assertGreater(saver["steps"].index(save), saver["steps"].index(tools))
        self.assertEqual(save.get("if"), f"{missed} && github.ref == 'refs/heads/main'")
        self.assertEqual(
            (save.get("with") or {}).get("key"),
            f"${{{{ steps.{mise_step.get('id')}.outputs.tools-cache-key }}}}",
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
            # `ci:guards` reads the step's SHARD, and fires the whole selection without one.
            argv = shlex.split(task_command(step)[1])
            self.assertIn(
                ("--shard", "${SHARD:-0/1}"),
                list(zip(argv, argv[1:])),
                f"{event}: the fire step's task doesn't pass --shard its SHARD",
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

    def test_the_checkout_has_the_history_the_records_commit_needs(self):
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
