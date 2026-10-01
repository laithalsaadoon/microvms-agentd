#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
# SPDX-License-Identifier: Apache-2.0
"""Mutation-test the functions a change makes in `tools/*.py`, and fail when one has more
surviving mutants than it had on the base.

The Python twin of `tools/check-mutants.py`. A gate script's unit test can run the code it names
and assert nothing about what it did. mutmut asks the direct question: it changes one thing in a
function (an operator, a constant, an argument), runs the tests that call the function, and
reports the change as survived when they still pass. A first run over check-ci-parity.py left a
fifth of its mutants alive, real gaps among them, so the rule is a ratchet rather than zero: a
function the change touches can't have more survivors than the base's version of it had, and a
new function starts from none. The survivors in the functions nobody touches stay where they are
until a change meets them.

What it does:

  1. Lists the scripts the working tree changes against the merge base of `--base` and HEAD:
     `tools/*.py`, untracked ones included, not the `test_*.py` suites. A rename is compared
     with its old path. A base git can't resolve fails, since a shallow checkout must not read
     as no change.
  2. In each, the changed functions: a function or method whose code differs from the base's,
     or that the base doesn't have. Code is compared the way mutmut hashes it (`ast.dump` of the
     definition, so a move or a comment isn't a change), and the functions are the ones mutmut
     mutates: each top-level function and each method of a top-level class. Code outside a
     function is never mutated, so a change there has nothing to measure.
  3. Refuses a no-mutate pragma in a changed script that gives no reason in parentheses after
     it, `# pragma: no mutate (a message's wording isn't a verdict)`, except the `end` that
     closes a `start`. The pragma is how an equivalent mutant is marked, one no test could tell
     from the original, and the reason is what review reads. mutmut honors the comment on any
     line; a line that says it in prose is refused too, since mutmut reads it the same way.
  4. For each script, the suites that measure it: the `tools/test_*.py` modules that load it
     under mutmut's name for it, `tools.<stem>` (`runpy.run_path(..., run_name="tools.<stem>")`).
     mutmut credits a test with a function only when the function's module has that name, so a
     suite that loads the script under another name, or only runs it as a subprocess, credits
     nothing. A script's own suite names it. A suite that loads another script for a constant or
     a helper leaves runpy's default name, which keeps it out of that script's runs, where it
     would cost its whole run and kill nothing. So does a suite that runs its script as a
     subprocess from another directory: mutmut's stats pass reaches a mutated copy there, which
     reads mutmut's config from the working directory, doesn't find it, and fails the pass. A
     script no suite names is reported and not mutated, and one whose suite named it on the
     base fails, since the change stopped measuring it.
  5. Runs mutmut over a snapshot of the working tree (untracked files included) and, when a
     changed function is on the base, over the merge base, each in a scratch directory whose
     `mutants/` is a git worktree of that tree. mutmut runs the suites in `mutants/`, so they read
     a whole checkout. The scripts beside it are the sources it mutates into `mutants/`, and
     `setup.cfg` names them and their suites. Each run mutates only the changed functions: the
     copy mutmut reads marks every other function with a block pragma, so mutmut writes no
     mutants for them into the script the suites load, and `mutmut run <module>.<function>*`
     names the ones it runs. The glob matches the function's own name as well as its mutants,
     which is how mutmut finds the tests to run clean: with `__mutmut_*` in it, it found none and
     ran every suite clean. A change pays for what it touched. The mutants are generated before
     mutmut runs and the mutated script saved compiled (`GENERATE`), so no load in a suite
     compiles them again. The base run measures only the functions some mutant survives in on
     the head, since one none survives in can't have more than its base had. While mutmut runs,
     each function's count of mutants run and survived prints as it moves.
  6. Reads each function's verdicts from mutmut's results (`mutants/<script>.meta`) and counts
     the mutants whose tests passed or ran out of time. A test run a signal ended, other than the
     one mutmut's timeout sends, is caught: its tests didn't pass. One no test reached in-process
     (`no tests`) is listed, not counted: a suite that runs the script as a subprocess tests it,
     and mutmut can't see that. Fails when a changed function has more
     counted mutants than on the base, with each one's diff (`mutmut show`). A function the
     base's suites didn't measure, or that no in-process test reached on the base, has no base
     count, and isn't held to one.

`--shard k/N` measures shard k's share of the changed functions, heaviest first (by syntax
nodes, which track mutants) onto the lightest shard, each with its base, so a shard's verdict
needs no other's. `--budget SECONDS` bounds a run: the head side's mutmut gets `HEAD_SHARE` of
it, the base side what's left, and each is stopped when its share runs out. A mutant a stopped
run didn't reach has no verdict, and a function with one is decided by what its counts already
settle: survivors only grow with the mutants left, and so does a base's count, so a head count
above a whole base count fails, a new function with a survivor fails, and a head count no
higher than a partial base count passes. Any other function is undecided: it passes, and the
run names it.

It fails rather than read a count from nothing: mutmut exiting non-zero while a changed function
has mutants, a results file that's missing or names a mutant not of its script, a mutant left
without a verdict or with an exit code mutmut doesn't name, and a function hash that isn't the
one mutmut recorded, which would mean the functions read as changed aren't the ones mutmut sees.

`--detect` stops after step 1 and prints `python=true` or `python=false`, for CI's first step to
append to `$GITHUB_OUTPUT` before it installs anything: with `--shard k/N`, shard 0 answers true
whenever a script changed, since it reports what no shard measures, and another when it has a
changed function. It needs nothing but git, so the runner's `python3` runs it.

mutmut runs from this repository's locked `dev` group (`uv run --locked --project <repo>
mutmut`), the environment the unit suites run in. `--mutmut PATH` names another executable (the
unit tests hand it a fake) and `--python PATH` another interpreter for `GENERATE`; `--jobs N` is
mutmut's `--max-children`, and `--keep` leaves the scratch directory for `mutmut browse` (its
worktrees go with `git worktree remove`).
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import hashlib
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tokenize
from dataclasses import dataclass, field
from pathlib import Path

# The repository whose `dev` group holds mutmut: this script's, whichever tree it's run over.
HOME = Path(__file__).resolve().parent.parent
# What's measured: the gate scripts and the modules they share, not their suites. `--` ahead of
# the pathspec, since a path is never an option.
SCRIPTS = ":(glob)tools/*.py"
PATHSPEC = ("--", SCRIPTS)
SUITE = re.compile(r"^tools/test_[^/]*\.py$")
MUTMUT = ["uv", "run", "--locked", "--project", str(HOME), "mutmut"]
# The interpreter of the environment mutmut runs in, which runs `GENERATE`.
PYTHON = ["uv", "run", "--locked", "--project", str(HOME), "python"]

# mutmut 3.8.0's names for its exit codes (`status_by_exit_code` in `mutmut/stats.py`), less
# its 35, "suspicious", which nothing in 3.8.0 assigns. mutmut calls any code its table lacks
# "suspicious" too, which is how a test run SIGTERM ended reads there (-15): measured on
# 2026-09-30, a suite whose test signals itself under a mutant recorded -15, and #452's CI once
# counted such a mutant as a survivor, depending on which of its tests mutmut ran first. None is
# a mutant it never ran.
STATUS = {
    0: "survived",
    1: "killed",
    2: "interrupted",
    3: "killed",
    5: "no tests",
    33: "no tests",
    34: "skipped",
    36: "timeout",
    37: "caught by type check",
    24: "timeout",
    -24: "timeout",
    152: "timeout",
    255: "timeout",
    -11: "segfault",
    -9: "segfault",
}
# A test failed with the mutant in, or the mutant couldn't run: caught.
CAUGHT = {"killed", "caught by type check", "segfault", "skipped"}
# No test called the function in-process, so there's no verdict to count either way.
UNREACHED = "no tests"
# The run ended before the mutant's tests did, so the count can't be read.
UNREAD = {None, 2}
# A test run a signal ended exits with the signal's number, negated.
SIGNALS = frozenset(s.value for s in signal.Signals)

# The pointers a git hook exports, and the `uv run --script` environment this runs in, which
# isn't the one mutmut runs in. Inherited, the first would aim git at the caller's index.
LEAKS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_CEILING_DIRECTORIES",
    "GIT_PREFIX",
    "VIRTUAL_ENV",
)
# The snapshot's scratch index, and its commit's author, since a CI checkout has no identity
# configured. No ref names the commit.
INDEX = "index"
IDENTITY = {
    "GIT_AUTHOR_NAME": "check-mutmut",
    "GIT_AUTHOR_EMAIL": "check-mutmut@localhost",
    "GIT_COMMITTER_NAME": "check-mutmut",
    "GIT_COMMITTER_EMAIL": "check-mutmut@localhost",
}
SNAPSHOT = ("-m", "check-mutmut snapshot")
# How mutmut 3.8.0 dumps a function to hash it (`compute_function_hashes`).
DUMP = {"annotate_fields": False}

# How mutmut finds a no-mutate pragma (a comment holding both `pragma:` after a hash and
# `no mutate`, which this comment avoids), and the form required here: `block` or `start` if
# any, then a reason in parentheses. `end` closes a `start` and carries none. mutmut reads a
# reason written after a colon as its first word, so one that starts with `end` closes a block.
PRAGMA = re.compile(r"# pragma:.*no mutate")
REASONED = re.compile(
    r"# pragma: no mutate(?: block| start)? \(\S.*\)|# pragma: no mutate end"
)
# The config mutmut reads from setup.cfg: the scripts it mutates, the suites it runs, no pytest
# cache written into the checkout, and no diffing of a scratch directory that isn't a repo. And
# the fork server: by default mutmut runs pytest again and again in one process, the stats pass,
# then the clean run, then a fork per mutant, so a suite's module and class state outlives the
# run that made it. test_check_guards_fire.py's FireSharded caches its fixture repos on the
# class, and the clean run read the repos the stats pass's class cleanup had deleted. The fork
# server runs each pass in a fresh child and each mutant in a fresh fork of a process that has
# only collected the suites.
CONFIG = """[mutmut]
source_paths ={sources}
pytest_add_cli_args_test_selection ={suites}
pytest_add_cli_args =
    -p
    no:cacheprovider
use_git_change_detection = false
process_isolation = forkserver
timeout_multiplier = 4
"""
# What marks a function mutmut isn't to touch in the copy of a script it reads: every function
# the run doesn't mutate gets it on its header, so mutmut neither writes that function's mutants
# into the mutated script nor puts it behind a trampoline.
SKIP = "# pragma: no mutate block (check-mutmut mutates the changed functions only)"
# The brackets a function's header can nest a colon in: an annotation, a default.
OPEN, CLOSE = {"(", "[", "{"}, {")", "]", "}"}
# The `sitecustomize` every Python a mutmut run starts imports first, from the directory the run
# puts ahead on PYTHONPATH. mutmut's stats pass marks the environment, and a test that runs its
# script as a subprocess from another directory (test_check_guards_fire.py's cases run it from a
# throwaway repo) then ran a mutated copy that records its hits through mutmut's config, which
# mutmut reads from the working directory, didn't find there, and failed the pass. The hits would
# be lost with the child anyway, so a new interpreter drops the mark. A mutant's run marks the
# environment with the mutant's name, which a child keeps, so it runs the mutant.
SITE = """import os

if os.environ.get("MUTANT_UNDER_TEST") == "stats":
    del os.environ["MUTANT_UNDER_TEST"]
"""
# What mutmut 3.8.0 prints when its stats pass found no test that reached any mutant, before it
# stops with exit 1 and every mutant unrun: the verdict "no tests" for each. Only the changed
# functions are trampolined, so a change whose functions no in-process test calls ends this way.
NONE_REACHED = "could not find any test case for any mutant"
# Writes each script's mutants the way `mutmut run` does (its own `create_mutants_for_file`),
# then saves the mutated script as compiled code under its own name, newer than its source, so
# `mutmut run` takes it as already generated. Python runs a file that starts with the bytecode
# magic number as compiled code, through `runpy.run_path` and as `python <script>` alike, so no
# load compiles it again. check-guards-fire.py with the twelve functions #495 changes has 1,306
# mutants, and its mutated source took 6 to 8 s to compile, once for every load in every test:
# the stats pass took 986 s of test time against 244 s for the plain suite, and each mutant's
# tests paid it again. The mutated source stays beside it, which `restore` puts back for `mutmut
# show`. A suite that reads its script as text reads bytecode in a run, and fails mutmut's stats
# pass: it copies the script with `read_bytes`, as test_check_mutmut.py's CI case does.
GENERATE = """
import importlib.util, marshal, os, sys
from pathlib import Path
import mutmut.__main__ as mutmut

for path in map(Path, sys.argv[1:]):
    out = Path("mutants") / path
    made = mutmut.create_mutants_for_file(path, out)
    if made.error:
        raise made.error
    source = out.read_bytes()
    out.with_name(out.name + ".source").write_bytes(source)
    code = compile(source, str(out.resolve()), "exec")
    out.write_bytes(importlib.util.MAGIC_NUMBER + bytes(12) + marshal.dumps(code))
    newer = path.stat().st_mtime_ns + 10**9
    os.utime(out, ns=(newer, newer))
"""
# How often a run's progress is read and printed, and the share of `--budget` the head side gets
# before it's stopped; the base side has the rest.
POLL = 60
HEAD_SHARE = 0.6
# The end of mutmut's log a failure prints, and the scratch directory's name.
TAIL = 40
SPINNER = re.compile("[\u2800-\u28ff] ")
PREFIX = "check-mutmut-"


class Failure(Exception):
    """The gate can't read a verdict, or the verdict is a failure."""


def clean_env() -> dict[str, str]:
    """The caller's environment without `LEAKS`."""
    return {k: v for k, v in os.environ.items() if k not in LEAKS}


def git(*args: str, cwd: Path | str | None, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, env=env or clean_env()
    )
    if result.returncode:
        raise Failure(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


@dataclass(frozen=True)
class Change:
    """A script the change touches: its path here, and on the base (None when it's new)."""

    path: str
    base: str | None


def changes(root: Path, base: str) -> tuple[str, list[Change]]:
    """The merge base with `base`, and the scripts the working tree changes against it."""
    merge_base = git("merge-base", base, "HEAD", cwd=root).strip()
    found = {}
    status = git("diff", "--name-status", "-M", merge_base, *PATHSPEC, cwd=root)
    for line in status.splitlines():
        kind, *paths = line.split("\t")
        if kind != "D":
            found[paths[-1]] = Change(paths[-1], None if kind == "A" else paths[0])
    untracked = git("ls-files", "--others", "--exclude-standard", *PATHSPEC, cwd=root)
    for path in untracked.splitlines():
        found[path] = Change(path, None)
    return merge_base, [c for p, c in sorted(found.items()) if not SUITE.match(p)]


def module_name(path: str) -> str:
    """mutmut's name for a script's module: its path without `.py`, dotted."""
    return path.removesuffix(".py").replace("/", ".")


def function_hashes(source: str | bytes) -> dict[str, str]:
    """Each function mutmut mutates, by mutmut's key for it (`x_name`, `xǁClassǁname`), with
    mutmut's hash of it: the first twelve hex digits of the sha256 of `ast.dump` of the
    definition. `compute_function_hashes` in mutmut 3.8.0's `mutation/file_mutation.py`,
    restated so `--detect` needs no mutmut; each run holds it to the hashes mutmut records."""
    hashes = {}

    def visit(body: list[ast.stmt], cls: str | None = None) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                key = f"xǁ{cls}ǁ{node.name}" if cls else f"x_{node.name}"
                digest = hashlib.sha256(ast.dump(node, **DUMP).encode())
                hashes[key] = digest.hexdigest()[:12]
            elif isinstance(node, ast.ClassDef):
                visit(node.body, f"{cls}.{node.name}" if cls else node.name)

    visit(ast.parse(source).body)
    return hashes


def shown(key: str) -> str:
    """A function's name as its source spells it: `check_pins`, `Command.where`."""
    return key.removeprefix("x_").removeprefix("xǁ").replace("ǁ", ".")


def pragma_problems(path: str, source: bytes) -> list[str]:
    return [
        f"{path}:{token.start[0]}: `{token.string}` gives no reason in parentheses"
        for token in tokenize.tokenize(io.BytesIO(source).readline)
        if token.type == tokenize.COMMENT
        and PRAGMA.search(token.string)
        and not REASONED.search(token.string)
    ]


def suites(root: Path, commit: str | None, module: str) -> list[str]:
    """The suites that load `module` under its own name, in the working tree (`commit` None)
    or at `commit`: the ones whose source holds the name as a string."""
    if commit:
        listed = git("ls-tree", "--name-only", commit, "tools/", cwd=root).splitlines()
    else:
        listed = [str(p.relative_to(root)) for p in root.glob("tools/test_*.py")]
    found = []
    for name in sorted(n for n in listed if SUITE.match(n)):
        text = (
            git("show", f"{commit}:{name}", cwd=root)
            if commit
            else (root / name).read_bytes()
        )
        try:
            names = {
                n.value
                for n in ast.walk(ast.parse(text))
                if isinstance(n, ast.Constant)
            }
        except SyntaxError:
            continue
        if module in names:
            found.append(name)
    return found


@dataclass
class Script:
    """One changed script: its changed functions, the ones the base has, and its suites."""

    change: Change
    functions: list[str]
    on_base: list[str]
    head_suites: list[str] = field(default_factory=list)
    base_suites: list[str] = field(default_factory=list)
    # Each function's weight, which `shard` balances.
    weights: dict[str, int] = field(default_factory=dict)


@dataclass
class Tally:
    """One function's mutants on one side: how many, the counted ones with their status, and
    how many no in-process test reached."""

    total: int = 0
    counted: list[tuple[str, str]] = field(default_factory=list)
    unreached: int = 0
    # Mutants the run's budget stopped it before, which have no verdict.
    unrun: int = 0


def plan(
    root: Path, merge_base: str, found: list[Change]
) -> tuple[list[Script], list[str]]:
    """Each changed script with its changed functions and suites, and the problems its source
    has."""
    scripts, problems = [], []
    for change in found:
        head = (root / change.path).read_bytes()
        problems += pragma_problems(change.path, head)
        head_hashes = function_hashes(head)
        base_hashes = (
            function_hashes(git("show", f"{merge_base}:{change.base}", cwd=root))
            if change.base
            else {}
        )
        functions = [k for k, h in head_hashes.items() if base_hashes.get(k) != h]
        script = Script(change, functions, [k for k in functions if k in base_hashes])
        script.weights = weights(head)
        if functions:
            script.head_suites = suites(root, None, module_name(change.path))
            if change.base:
                script.base_suites = suites(root, merge_base, module_name(change.base))
        scripts.append(script)
    return scripts, problems


def snapshot(root: Path, work: Path) -> str:
    """A commit of the working tree, untracked files included, through a scratch index. No
    ref names it, and the caller's index isn't touched."""
    env = clean_env() | {"GIT_INDEX_FILE": str(work / INDEX)}
    git("add", "-A", cwd=root, env=env)
    tree = git("write-tree", cwd=root, env=env).strip()
    commit = ("commit-tree", tree, *SNAPSHOT)
    return git(*commit, cwd=root, env=clean_env() | IDENTITY).strip()


def only(source: str, keep: list[str]) -> str:
    """`source` with `SKIP` on the header of each function mutmut mutates that `keep` doesn't
    name: at the end of the line its header's colon is on, or ahead of a comment already there.
    mutmut writes every function's mutants into the mutated script otherwise, and every suite
    that loads the script compiles them: check-guards-fire.py's came to 40 MB and took minutes
    to generate, for a run that tested one function's."""
    starts = set()

    def visit(body: list[ast.stmt], cls: str | None = None) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if (f"xǁ{cls}ǁ{node.name}" if cls else f"x_{node.name}") not in keep:
                    starts.add(node.lineno)
            elif isinstance(node, ast.ClassDef):
                visit(node.body, f"{cls}.{node.name}" if cls else node.name)

    visit(ast.parse(source).body)
    tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    comments = {t.start[0]: t.start[1] for t in tokens if t.type == tokenize.COMMENT}
    lines = source.splitlines(keepends=True)
    for i, token in enumerate(tokens):
        if token.string != "def" or token.start[0] not in starts:
            continue
        depth = 0
        for after in tokens[i:]:
            depth += (after.string in OPEN) - (after.string in CLOSE)
            if after.string == ":" and depth <= 0:
                break
        row = after.start[0]
        text = lines[row - 1]
        at = comments.get(row, len(text.rstrip("\r\n")))
        lines[row - 1] = f"{text[:at]}  {SKIP}  {text[at:]}"
    return "".join(lines)


def prepare(
    root: Path,
    commit: str,
    side: Path,
    wanted: dict[str, list[str]],
    selected: list[str],
) -> None:
    """`side/mutants` holds `commit` checked out, and `side` the scripts mutmut reads, each
    with the functions this run doesn't mutate marked (`only`), and the config naming them.
    mutmut writes each mutated script into `mutants/` where the checkout's copy was, and runs
    the suites there."""
    git("worktree", "add", str(side / "mutants"), commit, cwd=root)
    for path, keys in wanted.items():
        (side / path).parent.mkdir(exist_ok=True)
        # mutmut mutates the copy in `mutants/` when it's missing, which it then copies from
        # the source with the source's mtime.
        source = (side / "mutants" / path).read_bytes().decode()
        (side / path).write_bytes(only(source, keys).encode())
        (side / "mutants" / path).unlink()
    sources = "".join(f"\n    {p}" for p in wanted)
    (side / "setup.cfg").write_text(
        CONFIG.format(sources=sources, suites="".join(f"\n    {s}" for s in selected))
    )


def progress(side: Path, wanted: dict[str, list[str]]) -> list[str]:
    """One line for each function of `wanted` with mutants: how many have a verdict, of how
    many, and how many survived. Empty while mutmut hasn't written its results."""
    lines = []
    for path, keys in wanted.items():
        try:
            verdicts = json.loads((side / "mutants" / f"{path}.meta").read_bytes())
            verdicts = verdicts["exit_code_by_key"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        for key in keys:
            mine = [
                code
                for name, code in verdicts.items()
                if name.startswith(f"{module_name(path)}.{key}__mutmut_")
            ]
            if mine:
                done = sum(code is not None for code in mine)
                lines.append(
                    f"{shown(key)} {done} of {len(mine)} run, {mine.count(0)} survived"
                )
    return lines


def run_mutmut(
    mutmut: list[str],
    side: Path,
    wanted: dict[str, list[str]],
    jobs: list[str],
    seconds: float | None,
) -> tuple[int, bool]:
    """mutmut over `wanted` in `side`, its output in `side/mutmut.log`: its exit code, and
    whether it was stopped after `seconds`. Every Python it starts imports `SITE` first. Each
    function's progress prints as it moves, a line at most every `POLL` seconds."""
    (side / "site").mkdir()
    (side / "site" / "sitecustomize.py").write_text(SITE)
    env = clean_env()
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(side / "site"), env.get("PYTHONPATH")])
    )
    globs = [f"{module_name(p)}.{k}*" for p, keys in wanted.items() for k in keys]
    printed: list[str] = []
    deadline = None if seconds is None else time.monotonic() + seconds
    with (side / "mutmut.log").open("w") as log:
        proc = subprocess.Popen(
            [*mutmut, "run", *jobs, *globs],
            cwd=side,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
        try:
            while True:
                left = None if deadline is None else deadline - time.monotonic()
                try:
                    return proc.wait(
                        timeout=POLL if left is None else max(0, min(POLL, left))
                    ), False
                except subprocess.TimeoutExpired:
                    pass
                lines = progress(side, wanted)
                if lines != printed:
                    printed = lines
                    print(
                        f"check-mutmut: {side.name}: {'; '.join(lines)}",
                        file=sys.stderr,
                    )
                if deadline is None:
                    continue
                # `>` instead reads the same: a deadline met exactly and one just passed
                # can't be told apart.
                late = time.monotonic() >= deadline  # pragma: no mutate (ties)
                if late:
                    os.killpg(proc.pid, signal.SIGTERM)
                    return proc.wait(), True
        finally:
            # A signal or an error here ends mutmut's session, its workers with it.
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait()


def results(side: Path, path: str) -> dict:
    """mutmut's results for `path` on one side. Missing, unreadable, or naming a mutant that
    isn't the script's, fails. Naming none is mutmut's answer that the functions this run left
    unmarked have nothing to mutate: it writes the file when it generates the mutants, and
    exits 1 on names that match none."""
    meta = side / "mutants" / f"{path}.meta"
    try:
        data = json.loads(meta.read_bytes())
        verdicts = data["exit_code_by_key"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise Failure(
            f"mutmut wrote no results for {path}: {meta}: {error!r}"
        ) from None
    prefix = f"{module_name(path)}.x"
    strays = [k for k in verdicts if not k.startswith(prefix) or "__mutmut_" not in k]
    if strays:
        raise Failure(
            f"{meta} names {strays[0]}, not a mutant of {path} as"
            f" `{prefix}<function>__mutmut_<n>`"
        )
    return data


def tally(data: dict, path: str, key: str, stopped: bool = False) -> Tally:
    """Count one function's mutants from its script's results. One without a verdict fails,
    unless the run was `stopped` at its budget, which leaves it unrun."""
    out = Tally()
    for name, code in data["exit_code_by_key"].items():
        if not name.startswith(f"{module_name(path)}.{key}__mutmut_"):
            continue
        if code is None and stopped:
            out.unrun += 1
            continue
        if code in UNREAD:
            raise Failure(
                f"{name} has no verdict, so no count for {shown(key)} in {path} can be read"
            )
        if code not in STATUS and -code not in SIGNALS:
            raise Failure(
                f"{name} ended with exit {code}, which is neither a status mutmut 3.8.0 names nor"
                f" a signal, so no count for {shown(key)} in {path} can be read"
            )
        out.total += 1
        # A signal the table doesn't name ended the test run, so its tests didn't pass.
        status = STATUS.get(code, "killed")
        if status == UNREACHED:
            out.unreached += 1
        elif status not in CAUGHT:
            out.counted.append((name, status))
    return out


def check_hashes(data: dict, path: str, source: bytes) -> None:
    """Fail unless every hash mutmut recorded for `path` is the one `function_hashes` gives it."""
    mine = function_hashes(source)
    wrong = [
        k for k, h in data.get("hash_by_function_name", {}).items() if mine.get(k) != h
    ]
    if wrong:
        raise Failure(
            f"mutmut hashes {', '.join(map(shown, wrong))} in {path} differently than this script"
            " does, so what it reads as changed isn't what mutmut sees: restate `function_hashes`"
            " from the pinned mutmut's `compute_function_hashes`"
        )


def log_tail(side: Path) -> str:
    """The end of mutmut's log, each redraw a line of its own, without its spinner's frames: a
    braille dot pattern and the step it's on, which filled the whole tail of a run that failed in
    its stats pass."""
    lines = (side / "mutmut.log").read_bytes().decode(errors="replace").splitlines()
    kept = [line for line in lines if line.strip() and not SPINNER.match(line)]
    return "\n".join(kept[-TAIL:])


def generate(python: list[str], side: Path, wanted: dict[str, list[str]]) -> None:
    """Write each script's mutants, saved compiled (`GENERATE`)."""
    done = subprocess.run(
        [*python, "-c", GENERATE, *wanted],
        cwd=side,
        capture_output=True,
        text=True,
    )
    if done.returncode:
        raise Failure(
            f"generating the mutants in {side} exited {done.returncode}:\n"
            f"{done.stdout}{done.stderr}".rstrip()
        )


def restore(side: Path, wanted: dict[str, list[str]]) -> None:
    """Put each mutated script's source back where its compiled code was, for `mutmut show`."""
    for path in wanted:
        source = side / "mutants" / f"{path}.source"
        if source.exists():
            source.replace(side / "mutants" / path)


def measure(
    mutmut: list[str],
    python: list[str],
    root: Path,
    commit: str,
    side: Path,
    wanted: dict[str, list[str]],
    selected: list[str],
    jobs: list[str],
    seconds: float | None = None,
) -> dict[tuple[str, str], Tally]:
    """Mutate the `wanted` functions of each script at `commit`, and tally each one."""
    prepare(root, commit, side, wanted, selected)
    generate(python, side, wanted)
    try:
        code, budget = run_mutmut(mutmut, side, wanted, jobs, seconds)
    finally:
        restore(side, wanted)
    failed = Failure(
        f"mutmut exited {code} in {side}; the end of its log:\n{log_tail(side)}"
    )
    stopped = NONE_REACHED.encode() in (side / "mutmut.log").read_bytes()
    # A run mutmut failed leaves results it didn't finish, so its own log is the reason given.
    try:
        data = {path: results(side, path) for path in wanted}
        if stopped:
            for found in data.values():
                found["exit_code_by_key"] = dict.fromkeys(found["exit_code_by_key"], 33)
        out = {
            (p, k): tally(data[p], p, k, budget)
            for p, keys in wanted.items()
            for k in keys
        }
    except Failure:
        if code and not budget:
            raise failed from None
        raise
    # mutmut refuses names that match no mutant, which is the right answer when no changed
    # function has anything to mutate. Any other failure stands, but a run its budget stopped
    # ends with whatever code the signal leaves.
    if code and not budget and not stopped and any(t.total for t in out.values()):
        raise failed
    for path in wanted:
        check_hashes(data[path], path, (side / path).read_bytes())
    return out


def show(mutmut: list[str], side: Path, name: str) -> str:
    done = subprocess.run(
        [*mutmut, "show", name],
        cwd=side,
        capture_output=True,
        text=True,
        env=clean_env(),
    )
    return (done.stdout + done.stderr).strip()


def verdict(
    mutmut: list[str],
    side: Path,
    measured: list[Script],
    heads: dict[tuple[str, str], Tally],
    bases: dict[tuple[str, str], Tally],
) -> int:
    """Print each changed function's count against its base count; 1 when one grew."""
    grew, undecided = [], []
    for script in measured:
        print(f"  {script.change.path}, measured by {', '.join(script.head_suites)}:")
        for key in script.functions:
            head = heads[(script.change.path, key)]
            base = bases.get((script.change.base, key))
            before = len(base.counted) if base else 0
            held = True
            if key not in script.on_base:
                origin = "new"
            elif base and base.total and base.unreached == base.total:
                # Adding the first test that reaches a function in-process can't fail a change
                # for what that test leaves alive: the base had no count to hold it to.
                origin, held = "no in-process test reached it on the base", False
            elif base and base.unrun:
                origin = f"at least {before} on the base, {base.unrun} not run within the budget"
            elif base:
                origin = f"{before} on the base"
            elif script.base_suites and not head.counted:
                origin = "none survive, so the base isn't measured"
            else:
                origin, held = "not measured on the base", False
            line = f"    {shown(key)}: {len(head.counted)} of {head.total} survive ({origin})"
            if head.unreached:
                line += (
                    f", and {head.unreached} no in-process test reached aren't counted"
                )
            if head.unrun:
                line += f", and {head.unrun} weren't run within the budget"
            more = held and len(head.counted) > before
            # A run the budget stopped decides what its counts already settle: survivors only
            # grow with the mutants left, and so does the base's count, so more than a whole
            # base count fails, and no more than a partial one passes. The rest is undecided.
            if (more and base and base.unrun) or (held and not more and head.unrun):
                line += ": NOT DECIDED within the budget"
                undecided.append(f"{shown(key)} in {script.change.path}")
            elif more:
                line += ": MORE"
                grew.append((script.change.path, key, head.counted, before))
            print(line)
    for path, key, counted, before in grew:
        print(
            f"\ncheck-mutmut: {shown(key)} in {path} has {len(counted)} surviving mutants,"
            f" {before} on the base. Strengthen the test that should catch them, or mark one no"
            " test can tell from the original with a no-mutate pragma and its reason:",
            file=sys.stderr,
        )
        for name, status in counted:
            print(f"\n  {name}: {status}\n{show(mutmut, side, name)}", file=sys.stderr)
    if undecided:
        print(
            f"check-mutmut: the budget ran out before {len(undecided)} changed functions were"
            f" decided, and they pass undecided: {', '.join(undecided)}"
        )
    if not grew:
        print(
            "check-mutmut: no changed function has more surviving mutants than on the base"
        )
    return 1 if grew else 0


def weights(source: str | bytes) -> dict[str, int]:
    """Each function mutmut mutates, by its key, with its count of syntax nodes: the measure of
    how many mutants it has that `shard` balances on, since counting them takes mutmut."""
    out = {}

    def visit(body: list[ast.stmt], cls: str | None = None) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                key = f"xǁ{cls}ǁ{node.name}" if cls else f"x_{node.name}"
                out[key] = sum(1 for _ in ast.walk(node))
            elif isinstance(node, ast.ClassDef):
                visit(node.body, f"{cls}.{node.name}" if cls else node.name)

    visit(ast.parse(source).body)
    return out


def shard(measured: list[Script], k: int, n: int) -> list[Script]:
    """The scripts of shard `k` of `n`, each with its share of the changed functions: heaviest
    first onto the lightest shard, a tie in weight to the function first in path and key order
    and a tie in load to the lower shard. Each function's head and base runs in its shard, so a
    shard's verdict needs no other's."""
    if n == 1:
        return measured
    items = sorted(
        (
            (-s.weights[key], s.change.path, key)
            for s in measured
            for key in s.functions
        ),
    )
    mine = set()
    # Every shard starts from one load, so which load it is doesn't change the split.
    load = [0] * n  # pragma: no mutate (any one starting load is the same split)
    for weight, path, key in items:
        lightest = load.index(min(load))
        load[lightest] -= weight
        if lightest == k:
            mine.add((path, key))
    out = []
    for script in measured:
        functions = [
            key for key in script.functions if (script.change.path, key) in mine
        ]
        if functions:
            on_base = [key for key in script.on_base if key in functions]
            out.append(
                dataclasses.replace(script, functions=functions, on_base=on_base)
            )
    print(
        f"check-mutmut: shard {k} of {n} measures {sum(len(s.functions) for s in out)} of"
        f" {sum(len(s.functions) for s in measured)} changed functions",
        file=sys.stderr,
    )
    return out


def measurable(scripts: list[Script]) -> list[Script]:
    """The scripts with a changed function and a suite that names them."""
    return [s for s in scripts if s.functions and s.head_suites]


def gate(
    root: Path, args: argparse.Namespace, merge_base: str, found: list[Change]
) -> int:
    scripts, problems = plan(root, merge_base, found)
    if problems:
        raise Failure("\n  ".join(["a no-mutate pragma needs its reason:", *problems]))
    for script in scripts:
        path, module = script.change.path, module_name(script.change.path)
        if not script.functions:
            print(f"check-mutmut: {path}: no function changed, so nothing to mutate")
        elif script.head_suites:
            pass
        elif script.base_suites:
            raise Failure(
                f"{', '.join(script.base_suites)} loaded {script.change.base} under its module name"
                f" on the base, and no suite loads {path} as `{module}`, so the change stopped"
                " measuring it"
            )
        else:
            print(
                f"check-mutmut: {path}: no suite loads it as `{module}`, so its"
                f" {len(script.functions)} changed functions aren't mutated"
            )
    measured = shard(measurable(scripts), *args.shard)
    if not measured:
        return 0
    mutmut = [args.mutmut] if args.mutmut else MUTMUT
    python = [args.python] if args.python else PYTHON
    jobs = ["--max-children", args.jobs] if args.jobs else []
    # Each side's share of the budget counts from when its mutmut starts, and the base gets
    # what the head left of it.
    head_seconds = None if args.budget is None else args.budget * HEAD_SHARE
    work = Path(tempfile.mkdtemp(prefix=PREFIX))
    # CI cancelling the job sends SIGTERM, and unwinding through `finally` removes the worktrees.
    previous = signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    try:
        print(
            f"check-mutmut: mutating {sum(len(s.functions) for s in measured)} changed functions"
            f" against {args.base} (merge base {merge_base[:12]}) in {work}",
            file=sys.stderr,
        )
        head = {s.change.path: s.functions for s in measured}
        head_suites = sorted({x for s in measured for x in s.head_suites})
        begun = time.monotonic()
        heads = measure(
            mutmut,
            python,
            root,
            snapshot(root, work),
            work / "head",
            head,
            head_suites,
            jobs,
            head_seconds,
        )
        left = (
            None
            if args.budget is None
            else max(0, args.budget - (time.monotonic() - begun))
        )
        # The base run mutates the changed functions the base has and its suites measured, and
        # of those only the ones some mutant survives in here: a function none survives in has
        # no more survivors than any base count, so measuring its base decides nothing.
        on_base = {
            s.change.base: survived
            for s in measured
            if s.base_suites
            and (
                survived := [k for k in s.on_base if heads[(s.change.path, k)].counted]
            )
        }
        base_suites = sorted(
            {x for s in measured if s.change.base in on_base for x in s.base_suites}
        )
        bases = on_base and measure(
            mutmut,
            python,
            root,
            merge_base,
            work / "base",
            on_base,
            base_suites,
            jobs,
            left,
        )
        return verdict(mutmut, work / "head", measured, heads, bases or {})
    finally:
        signal.signal(signal.SIGTERM, previous)
        if args.keep:
            print(f"check-mutmut: the scratch directory is {work}")
        else:
            for side in (work / "head", work / "base"):
                if (side / "mutants").exists():
                    git(
                        "worktree", "remove", "--force", str(side / "mutants"), cwd=root
                    )
            shutil.rmtree(work)


def shard_spec(text: str) -> tuple[int, int]:
    """`k/N`, with 0 <= k < N."""
    parts = text.split("/")
    if not (
        len(parts) == 2
        and all(part.isascii() and part.isdigit() for part in parts)
        and int(parts[0]) < int(parts[1])
    ):
        raise argparse.ArgumentTypeError(
            f"--shard takes k/N with 0 <= k < N; got {text}"
        )
    return int(parts[0]), int(parts[1])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--root")
    parser.add_argument("--detect", action="store_true")
    parser.add_argument("--mutmut")
    parser.add_argument("--python")
    parser.add_argument("--jobs")
    parser.add_argument("--shard", type=shard_spec, default=(0, 1))
    parser.add_argument("--budget", type=float)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args(argv)
    try:
        # The tree the caller is in (or `--root`), not this script's, so the tests can run it
        # over a throwaway repo.
        root = Path(git("rev-parse", "--show-toplevel", cwd=args.root).strip())
        merge_base, found = changes(root, args.base)
        if args.detect:
            print(
                "\n".join(c.path for c in found)
                or f"no script changes against {args.base}",
                file=sys.stderr,
            )
            # Shard 0 runs whenever a script changed, since it reports what no shard measures;
            # another runs when it has a changed function to measure.
            k, n = args.shard
            runs = bool(found) and (
                k == 0
                or bool(shard(measurable(plan(root, merge_base, found)[0]), k, n))
            )
            print(f"python={str(runs).lower()}")
            return 0
        if not found:
            print(f"check-mutmut: no script in tools/ changed against {args.base}")
            return 0
        return gate(root, args, merge_base, found)
    except Failure as error:
        print(f"check-mutmut: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
