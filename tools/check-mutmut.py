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
     `setup.cfg` names them and their suites. Each run mutates only the changed functions
     (`mutmut run <module>.<function>__mutmut_*`), so a change pays for what it touched.
  6. Reads each function's verdicts from mutmut's results (`mutants/<script>.meta`) and counts
     the mutants that survived, timed out or ended some way other than a failing test. One no
     test reached in-process (`no tests`) is listed, not counted: a suite that runs the script
     as a subprocess tests it, and mutmut can't see that. Fails when a changed function has more
     counted mutants than on the base, with each one's diff (`mutmut show`). A function the
     base's suites didn't measure has no base count, and isn't held to one.

It fails rather than read a count from nothing: mutmut exiting non-zero while a changed function
has mutants, a results file that's missing or names no mutant of its script, a mutant left
without a verdict, and a function hash that isn't the one mutmut recorded, which would mean the
functions read as changed aren't the ones mutmut sees.

`--detect` stops after step 1 and prints `python=true` or `python=false`, for CI's first step to
append to `$GITHUB_OUTPUT` before it installs anything. It needs nothing but git, so the runner's
`python3` runs it.

mutmut runs from this repository's locked `dev` group (`uv run --locked --project <repo>
mutmut`), the environment the unit suites run in. `--mutmut PATH` names another executable (the
unit tests hand it a fake), `--jobs N` is mutmut's `--max-children`, and `--keep` leaves the
scratch directory for `mutmut browse` (its worktrees go with `git worktree remove`).
"""

from __future__ import annotations

import argparse
import ast
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

# mutmut 3.8.0's names for its exit codes (`status_by_exit_code` in `mutmut/stats.py`). A code
# missing here is its "suspicious", and None is a mutant it never ran.
STATUS = {
    0: "survived",
    1: "killed",
    2: "interrupted",
    3: "killed",
    5: "no tests",
    33: "no tests",
    34: "skipped",
    35: "suspicious",
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
# cache written into the checkout, and no diffing of a scratch directory that isn't a repo.
CONFIG = """[mutmut]
source_paths ={sources}
pytest_add_cli_args_test_selection ={suites}
pytest_add_cli_args =
    -p
    no:cacheprovider
use_git_change_detection = false
"""
# The end of mutmut's log a failure prints, and the scratch directory's name.
TAIL = 40
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


@dataclass
class Tally:
    """One function's mutants on one side: how many, the counted ones with their status, and
    how many no in-process test reached."""

    total: int = 0
    counted: list[tuple[str, str]] = field(default_factory=list)
    unreached: int = 0


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


def prepare(
    root: Path, commit: str, side: Path, paths: list[str], selected: list[str]
) -> None:
    """`side/mutants` holds `commit` checked out, and `side` the scripts mutmut reads and the
    config naming them. mutmut writes each mutated script into `mutants/` where the checkout's
    copy was, and runs the suites there."""
    git("worktree", "add", str(side / "mutants"), commit, cwd=root)
    for path in paths:
        (side / path).parent.mkdir(exist_ok=True)
        # mutmut mutates the copy in `mutants/` when it's missing or no newer than the source:
        # moved, not copied, so the mtime stays.
        (side / "mutants" / path).rename(side / path)
    sources = "".join(f"\n    {p}" for p in paths)
    (side / "setup.cfg").write_text(
        CONFIG.format(sources=sources, suites="".join(f"\n    {s}" for s in selected))
    )


def run_mutmut(mutmut: list[str], side: Path, globs: list[str], jobs: list[str]) -> int:
    """mutmut over `globs` in `side`, its output in `side/mutmut.log`; its exit code."""
    with (side / "mutmut.log").open("w") as log:
        proc = subprocess.Popen(
            [*mutmut, "run", *jobs, *globs],
            cwd=side,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=clean_env(),
            start_new_session=True,
        )
        try:
            return proc.wait()
        finally:
            # A signal or an error here ends mutmut's session, its workers with it.
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait()


def results(side: Path, path: str) -> dict:
    """mutmut's results for `path` on one side. Missing, unreadable, or naming no mutant of the
    script, fails."""
    meta = side / "mutants" / f"{path}.meta"
    try:
        data = json.loads(meta.read_bytes())
        verdicts = data["exit_code_by_key"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise Failure(
            f"mutmut wrote no results for {path}: {meta}: {error!r}"
        ) from None
    prefix = f"{module_name(path)}.x"
    if not verdicts or not all(
        k.startswith(prefix) and "__mutmut_" in k for k in verdicts
    ):
        raise Failure(
            f"{meta} names no mutant of {path} as `{prefix}<function>__mutmut_<n>`"
        )
    return data


def tally(data: dict, path: str, key: str) -> Tally:
    """Count one function's mutants from its script's results. One without a verdict fails."""
    out = Tally()
    for name, code in data["exit_code_by_key"].items():
        if not name.startswith(f"{module_name(path)}.{key}__mutmut_"):
            continue
        if code in UNREAD:
            raise Failure(
                f"{name} has no verdict, so no count for {shown(key)} in {path} can be read"
            )
        out.total += 1
        status = STATUS.get(code, "suspicious")
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
    """The end of mutmut's log, each spinner redraw a line of its own."""
    lines = (side / "mutmut.log").read_bytes().decode(errors="replace").splitlines()
    return "\n".join([line for line in lines if line.strip()][-TAIL:])


def measure(
    mutmut: list[str],
    root: Path,
    commit: str,
    side: Path,
    wanted: dict[str, list[str]],
    selected: list[str],
    jobs: list[str],
) -> dict[tuple[str, str], Tally]:
    """Mutate the `wanted` functions of each script at `commit`, and tally each one."""
    prepare(root, commit, side, list(wanted), selected)
    globs = [
        f"{module_name(p)}.{k}__mutmut_*" for p, keys in wanted.items() for k in keys
    ]
    code = run_mutmut(mutmut, side, globs, jobs)
    failed = Failure(
        f"mutmut exited {code} in {side}; the end of its log:\n{log_tail(side)}"
    )
    # A run mutmut failed leaves results it didn't finish, so its own log is the reason given.
    try:
        data = {path: results(side, path) for path in wanted}
        out = {(p, k): tally(data[p], p, k) for p, keys in wanted.items() for k in keys}
    except Failure:
        if code:
            raise failed from None
        raise
    # mutmut refuses names that match no mutant, which is the right answer when no changed
    # function has anything to mutate. Any other failure stands.
    if code and any(t.total for t in out.values()):
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
    grew = []
    for script in measured:
        print(f"  {script.change.path}, measured by {', '.join(script.head_suites)}:")
        for key in script.functions:
            head = heads[(script.change.path, key)]
            base = bases.get((script.change.base, key))
            before = len(base.counted) if base else 0
            if key not in script.on_base:
                origin = "new"
            elif base:
                origin = f"{before} on the base"
            else:
                origin, before = "not measured on the base", len(head.counted)
            line = f"    {shown(key)}: {len(head.counted)} of {head.total} survive ({origin})"
            if head.unreached:
                line += (
                    f", and {head.unreached} no in-process test reached aren't counted"
                )
            if len(head.counted) > before:
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
    if not grew:
        print(
            "check-mutmut: no changed function has more surviving mutants than on the base"
        )
    return 1 if grew else 0


def gate(
    root: Path, args: argparse.Namespace, merge_base: str, found: list[Change]
) -> int:
    scripts, problems = plan(root, merge_base, found)
    if problems:
        raise Failure("\n  ".join(["a no-mutate pragma needs its reason:", *problems]))
    measured = []
    for script in scripts:
        path, module = script.change.path, module_name(script.change.path)
        if not script.functions:
            print(f"check-mutmut: {path}: no function changed, so nothing to mutate")
        elif script.head_suites:
            measured.append(script)
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
    if not measured:
        return 0
    mutmut = [args.mutmut] if args.mutmut else MUTMUT
    jobs = ["--max-children", args.jobs] if args.jobs else []
    # The base run mutates the changed functions the base has and its suites measured.
    on_base = {
        s.change.base: s.on_base for s in measured if s.on_base and s.base_suites
    }
    base_suites = sorted(
        {x for s in measured if s.change.base in on_base for x in s.base_suites}
    )
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
        heads = measure(
            mutmut, root, snapshot(root, work), work / "head", head, head_suites, jobs
        )
        bases = on_base and measure(
            mutmut, root, merge_base, work / "base", on_base, base_suites, jobs
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--root")
    parser.add_argument("--detect", action="store_true")
    parser.add_argument("--mutmut")
    parser.add_argument("--jobs")
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
            print(f"python={str(bool(found)).lower()}")
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
