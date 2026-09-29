#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
# SPDX-License-Identifier: Apache-2.0
"""Seed each registered fault and require the guard it names to catch it (#274).

The repo asks every new guard to be shown able to fail. Until this script, the proof was a
sentence in a PR or a Falsification note in a doc comment, and nothing ran it again: a later
change could leave a test that no longer reaches the code it names, or a scanner whose input
quietly emptied, and every gate would stay green. `guards/faults.toml` records each proof as a
fault this script can seed, and `guards/unregistered.txt` lists the notes that don't have one
yet.

Two subcommands:

  `list`  (in `mise run check`; no builds) reads the registry and holds it to the tree:
          - every entry is well formed (the schema below);
          - every transform anchor matches its file exactly once, and every patch passes
            `git apply --check`, so an entry whose target moved fails here instead of
            seeding nothing;
          - every test whose doc carries a Falsification note has an entry naming it in
            `note`, or its key is in `guards/unregistered.txt`. An entry's `note` has to be
            about its own guard: the note sits on the guard's test, or its text names that
            test (the agentd-model notes sit on the spec function the property test checks).
          - `guards/unregistered.txt` only shrinks. A key there that now has an entry fails,
            a key that no longer names a note fails, a new note in neither file fails, and a
            key the base's copy doesn't have fails (`--base REF`, the merge base with
            origin/main by default, as `ratchet.py` does). A key that replaces a base key
            whose note is gone, sharing its path or its name, is a move and passes. A base
            with no copy of the file is the bootstrap, and that rule is skipped.

  `fire`  (CI's `guards` job and the bindings job; `mise run guards:fire`) makes a detached
          temporary git worktree at HEAD, copies the caller's uncommitted changes into it,
          and runs every selected entry's command once on that clean tree. A command that's
          already red there proves nothing when it goes red again, so the run stops before
          any fault, and so does an entry whose `message` the clean run already prints (it
          can't tell the fault's failure from a pass). Then, for each fault: reset the tree,
          seed the fault, run the command, and print one line: `fired`, `DID NOT FIRE`, or
          `stale anchor`. Last, reset the tree and run every command clean again, which
          must pass as the first time did. Anything else exits 1.

          `--jobs N` runs the same passes in N scratch worktrees at once. Each command's
          clean run goes to one worker, each fault starts on the worker that built its
          command clean, and a worker with nothing left takes a fault from the end of
          another's queue. Bindings entries all run on the first worker, since they share
          `--venv`. Every worker then runs each command it ran again, clean, so each tree is
          shown to come back. The lines print in registry order whatever order they finish
          in, so the output and the summary are the serial run's. Only the first worker
          builds in `--target-dir`; the others build in a target beside their worktree, and
          both are removed when the run ends, a signal included, with every command still
          running (each runs in its own process group, so its rustc and test processes go
          too). SIGKILL can't be caught: a killed run leaves its worktrees (`git worktree
          prune` forgets them) and the extra targets beside them in the temp directory.

          `--affected` selects the entries whose own files changed: the registry text of the
          entry changed or is new, or a file it names changed between the merge base of HEAD
          and `--base` (origin/main by default) and the working tree, untracked files and both
          sides of a rename included. The files are the ones it seeds (transform files, the
          patch and what it touches) and its guard's (the `note`'s path, a path in `guard` or
          in an argv, the script a unit suite tests, the sibling scripts a named script loads
          or imports, the `-p` crate's file that defines a cargo test, the crate's clippy.toml
          for a lint). A change to this script, or to a build input every command reads
          (any Cargo.toml, Cargo.lock, rust-toolchain.toml, .cargo/config.toml, the root
          clippy.toml, mise.toml, mise.lock), selects every entry, and so does a change to
          ci.yml's `guards` job or its top-level `env` (CI's side of mise.toml: that job's
          steps install the toolchain every command runs under). It's a rule, not a trace:
          a change to code a guard reaches without naming it (the module that defines a type
          a clippy ban names, a helper a test calls) selects nothing, so a green run here
          isn't the full fire. It prints why each entry is in, the ids it skipped, and where
          the full fire runs when it skipped any. CI's `guards` job runs it on a pull request
          against the pull request's base, and fires every entry on each push to main (#323).

          `--shard k/N` fires shard k of N (numbered from 0, as cargo-mutants numbers its
          shards) of what the other flags select: it's cut after `--only`, `--suite`,
          `--affected` and the bindings drop, and the restored pass runs the shard's own
          commands. CI's `guards` job runs one shard a leg (#345). The N shards partition the
          selection, and shard k of N of the same tree and base is always the same entries. A
          shard holds whole commands, so a command's clean and restored runs happen once across
          the matrix. Each command weighs the sum of its entries' rough CI seconds
          (`entry_cost`: 16 for an entry that builds the CLI, 14 for another Rust entry, 6 for
          a script one), and commands go heaviest first onto the lightest shard, a tie in
          weight to the command that comes first in the registry and a tie in load to the lower
          shard; a shard's entries keep registry order. A cost, not a count, because a Rust
          entry costs about twice a script entry's time and the CLI's entries sit together:
          split by entry count, one of three shards held most of the CLI's entries and took
          440 s locally against 295 s and 314 s for the other two, and split by cost the three
          took 302 s, 359 s and 304 s (2026-09-29, this change's tree). Modeled on main's push
          at 6a868e9 with that run's per-command and per-fault seconds, the slowest of three
          legs is about 12 minutes by cost against about 17 by count, and about 15 either way
          with #340's entries added. Commands stay whole because splitting one repeats its
          clean and restored runs in each shard that holds part of it. It prints which shard
          it is and how much of the selection it keeps, and a shard with nothing in its slice
          exits 0.
          `mise run guards:fire -- --affected --shard 1/3` runs one pull request leg's share
          here.

          Cargo builds into `--target-dir`, by default `guards-fire` under the caller's
          target (`$CARGO_TARGET_DIR`, or `<repo>/target`). It persists, so a fault costs an
          incremental build from the second run on. It isn't the caller's own target because
          cargo keys a workspace crate's artifact by its workspace-relative path and trusts
          it while the sources are older: sharing one, the caller's next `cargo test -p
          <crate>` ran the scratch tree's last faulted build, and a test that reads its
          sources through `env!("CARGO_MANIFEST_DIR")` read the deleted scratch tree and
          found nothing. CI passes `--target-dir target`, since `fire` is its job's last
          step. The last clean pass reinstalls the clean extension into `--venv`, which
          no target dir separates. A run stopped before it (a signal, a timeout) says so
          on stderr.

What counts as fired, by `expect`:

  test-failed    the runner reports the named test (`guard`) as failed: `test <guard> ...
                 FAILED` from cargo, `FAILED <guard>` from `pytest -rA`, `not ok N - <guard>`
                 from `node --test --test-reporter=tap`. The clean run must report the same
                 test as passing, so a misspelled or deleted guard fails before any fault. A
                 build that breaks never counts, because any fault that breaks the build
                 would otherwise "fire" every guard.
  exit-nonzero   the command exits non-zero and its output contains `message`.
  compile-error  the build fails with `error[<code>]` (and `message`, when given). An entry
                 that expects a compile error names the code it expects: an unrelated one
                 (the E0599 a stale fault produces) isn't the guard firing.
  lint-error     the command exits non-zero and its output contains `message`, the lint's
                 own line (`use of a disallowed type ...`). For clippy bans. Not the lint's
                 name: the crate's `#![deny(...)]` note prints every name it lists, whichever
                 one fired.

An entry, in `guards/faults.toml`:

  [[fault]]
  id = "agentd-fs-pop"          # stable, [a-z0-9-]; `fire --only <id>`
  guard = "fs::tests::normalize_rejects_escapes_and_absorbs_benign_traversal"
  run = ["cargo", "test", ...]  # argv, no shell; or a list of argvs run in order
  expect = "test-failed"        # test-failed | exit-nonzero | compile-error | lint-error
  message = "..."               # a substring the failing output must contain
  code = "E0432"                # compile-error only
  suite = "rust"                # rust | script | bindings
  note = "<path>::<item>"       # the Falsification note this entry registers, if any
  # and exactly one fault:
  transform = { file = "...", replace = "...", with = "..." }   # or a list of them
  patch = "guards/faults/<id>.patch"
  argv_fault = ["--root", "{empty_dir}"]   # appended to the last command; for a gate whose
                                           # proof is the input it's handed, not the tree

Every cargo test run passes `--exact` (cargo's filter is a substring match). A bindings entry
rebuilds the extension first (`maturin develop`, `napi build`), or the test loads the stale
artifact; `fire` refuses to run one without `--venv`, because `maturin develop` installs into
whatever environment is active. One fault that several guards catch is one entry per guard.

A `message` is matched after ANSI color codes are stripped, since CI sets
`CARGO_TERM_COLOR=always`.

The note census reads tracked `.rs`, `.py`, `.mjs`, `.js`, `.cjs` and `.ts` files (the site's
vitest suite is TypeScript). Rust notes are doc comments on a function, found with ast-grep
(pinned in `mise.toml`, installed by checksum in CI); Python notes are a function's
docstring, read with stdlib `ast`; JavaScript and TypeScript notes are a comment inside a
`test()` or `it()` call, found with ast-grep. A note's key is `<path>::<function>`
(`<path>::<Class>::<method>` in Python, `<path>::<title>` in a JS or TS test). Every line
carrying the marker must land on one of those, so a note the parsers can't place, or a parser
that returns nothing, fails `list` by name rather than shrinking the census. Near-miss
spellings (a colon inside the bold, a lowercase or single-star marker, a `# Falsification`
heading) fail too, because the census can't see them. The colon rule stays case-sensitive:
prose like "the falsification: replace ..." is a sentence, not a note. `**Guard proof.**`
notes are out of scope: they record reasoning, not a fault to seed.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

REGISTRY = "guards/faults.toml"
# What every build or command reads: a change to one can move any entry's verdict.
BUILD_INPUTS = {
    "Cargo.lock",
    "rust-toolchain.toml",
    ".cargo/config.toml",
    "clippy.toml",
    "mise.toml",
    "mise.lock",
}
# CI's side of mise.toml: on the runner, the `guards` job's own steps install the toolchain and
# tools every command runs under (the Rust components, uv, Node, the ast-grep and cargo-mutants
# pins), and the workflow's top-level `env` reaches every step. A change to either selects every
# entry, as a mise.toml change does.
CI_WORKFLOW = ".github/workflows/ci.yml"
CI_JOB = "guards"
UNREGISTERED = "guards/unregistered.txt"

# Built, not written, so this file doesn't carry the marker it counts.
MARKER = "**" + "Falsification" + "**"
# This script and its tests spell the marker in strings and fixtures; neither is a note.
CENSUS_SKIPS = ("scripts/check-guards-fire.py", "scripts/test_check_guards_fire.py")
NEAR_MISS = (
    re.compile(
        r"[*_]{1,2}falsification[*_]{1,2}|\*\*falsification[^*]|#+\s*falsification\b",
        re.IGNORECASE,
    ),
    re.compile(r"Falsification\s*:"),
)
# The census's file types. ast-grep picks each file's language by its extension.
CENSUS_GLOBS = ("*.rs", "*.py", "*.mjs", "*.js", "*.cjs", "*.ts")

EXPECTS = ("test-failed", "exit-nonzero", "compile-error", "lint-error")
SUITES = ("rust", "script", "bindings")
ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
CODE = re.compile(r"^E\d{4}$")
KEYS = {
    "id",
    "guard",
    "run",
    "expect",
    "message",
    "code",
    "suite",
    "note",
    "transform",
    "patch",
    "argv_fault",
}

CARGO_OK = re.compile(r"^test (\S+) \.\.\. ok$", re.MULTILINE)
CARGO_FAILED = re.compile(r"^test (\S+) \.\.\. FAILED$", re.MULTILINE)
PYTEST_OK = re.compile(r"^PASSED (\S+)", re.MULTILINE)
PYTEST_FAILED = re.compile(r"^FAILED (\S+?)(?: - .*)?$", re.MULTILINE)
NODE_OK = re.compile(r"^\s*ok \d+ - (.+?)(?: # .*)?$", re.MULTILINE)
NODE_FAILED = re.compile(r"^\s*not ok \d+ - (.+?)(?: # .*)?$", re.MULTILINE)
BUILD_BROKE = re.compile(r"^error(?:\[(E\d{4})\])?: ", re.MULTILINE)
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

# The pointers a git hook exports (lefthook's pre-push runs `check`, and from a linked
# worktree git exports GIT_DIR there). Inherited, they'd point this script's `git worktree`
# and `git checkout` at the caller's index instead of the scratch tree's.
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


def clean_env() -> dict[str, str]:
    """The caller's environment without git pointers or the uv environment this runs in.

    `uv run --script` sets VIRTUAL_ENV to its own throwaway environment and puts it first on
    PATH. A guard's command should see the caller's tools, not this script's interpreter.
    """
    env = {k: v for k, v in os.environ.items() if k not in GIT_ENV_LEAKS}
    if sys.prefix != sys.base_prefix:
        own = str(Path(sys.prefix) / "bin")
        env["PATH"] = os.pathsep.join(
            p for p in env.get("PATH", "").split(os.pathsep) if p != own
        )
        if env.get("VIRTUAL_ENV") == sys.prefix:
            del env["VIRTUAL_ENV"]
    return env


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=clean_env(),
        check=check,
    )


@dataclass
class Fault:
    id: str
    guard: str
    run: list[list[str]]
    expect: str
    suite: str
    message: str | None = None
    code: str | None = None
    note: str | None = None
    transforms: list[dict] = field(default_factory=list)
    patch: str | None = None
    argv_fault: list[str] | None = None


def runner(argv: list[str]) -> str | None:
    """Which test runner an argv drives, for `test-failed`."""
    if "pytest" in argv:
        return "pytest"
    if argv and Path(argv[0]).name == "node" and "--test" in argv:
        return "node"
    if argv and Path(argv[0]).name == "cargo" and "test" in argv:
        return "cargo"
    return None


def load(root: Path) -> tuple[list[Fault], list[str]]:
    """The registry's entries, and what's wrong with its shape."""
    path = root / REGISTRY
    if not path.is_file():
        return [], [f"{REGISTRY} doesn't exist"]
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        return [], [f"{REGISTRY} doesn't parse: {error}"]
    raw = data.get("fault")
    if not isinstance(raw, list) or not raw:
        return [], [f"{REGISTRY} has no [[fault]] entry"]
    faults: list[Fault] = []
    problems: list[str] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        where = f"{REGISTRY} entry {index + 1}"
        if not isinstance(entry, dict):
            problems.append(f"{where} isn't a table")
            continue
        fid = entry.get("id")
        if isinstance(fid, str):
            where = f"{REGISTRY} entry {fid!r}"
        bad = [f"{where}: {m}" for m in shape(entry)]
        if not bad and fid in seen:
            bad.append(f"{where}: the id is used twice")
        if bad:
            problems += bad
            continue
        seen.add(fid)
        run = entry["run"]
        transform = entry.get("transform")
        faults.append(
            Fault(
                id=fid,
                guard=entry["guard"],
                run=[run] if isinstance(run[0], str) else run,
                expect=entry["expect"],
                suite=entry["suite"],
                message=entry.get("message"),
                code=entry.get("code"),
                note=entry.get("note"),
                transforms=(
                    []
                    if transform is None
                    else [transform]
                    if isinstance(transform, dict)
                    else transform
                ),
                patch=entry.get("patch"),
                argv_fault=entry.get("argv_fault"),
            )
        )
    return faults, problems


def _argv(value: object) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(a, str) and a for a in value)
    )


def shape(entry: dict) -> list[str]:
    """Every way one entry breaks the schema in the module docstring."""
    out = [f"unknown key {k!r}" for k in sorted(set(entry) - KEYS)]
    fid = entry.get("id")
    if not isinstance(fid, str) or not ID.match(fid):
        out.append("`id` must be a lowercase [a-z0-9-] string")
    for key in ("guard", "expect", "suite"):
        if not isinstance(entry.get(key), str) or not entry.get(key):
            out.append(f"`{key}` must be a non-empty string")
    for key in ("message", "code", "note", "patch"):
        if key in entry and (not isinstance(entry[key], str) or not entry[key]):
            out.append(f"`{key}` must be a non-empty string")
    run = entry.get("run")
    commands = [run] if _argv(run) else run
    if not (
        isinstance(commands, list) and commands and all(_argv(c) for c in commands)
    ):
        out.append("`run` must be an argv list, or a list of argv lists")
        commands = []
    expect = entry.get("expect")
    if isinstance(expect, str) and expect not in EXPECTS:
        out.append(f"`expect` must be one of {', '.join(EXPECTS)}")
    suite = entry.get("suite")
    if isinstance(suite, str) and suite not in SUITES:
        out.append(f"`suite` must be one of {', '.join(SUITES)}")
    if expect in ("exit-nonzero", "lint-error") and "message" not in entry:
        out.append(f"`expect = {expect!r}` needs a `message` the output must contain")
    if expect == "compile-error":
        if not isinstance(entry.get("code"), str) or not CODE.match(entry["code"]):
            out.append("`expect = 'compile-error'` needs the rustc `code`, like E0432")
    elif "code" in entry:
        out.append("`code` is for `expect = 'compile-error'` only")
    if expect == "test-failed" and commands:
        kind = runner(commands[-1])
        last = commands[-1]
        if kind is None:
            out.append(
                "`expect = 'test-failed'` needs the last command to be a cargo test, "
                "pytest or node --test run"
            )
        elif kind == "cargo" and "--exact" not in last:
            out.append(
                "a cargo test run needs `--exact`: cargo's filter is a substring"
            )
        elif kind == "pytest" and "-rA" not in last:
            out.append("a pytest run needs `-rA`, which reports each test by node id")
        elif kind == "node" and "--test-reporter=tap" not in last:
            out.append(
                "a node run needs `--test-reporter=tap`: Node 24 prints spec output "
                "otherwise, and CI runs Node 22"
            )
    faults = [k for k in ("transform", "patch", "argv_fault") if k in entry]
    if len(faults) != 1:
        out.append("needs exactly one of `transform`, `patch` or `argv_fault`")
    transform = entry.get("transform")
    if transform is not None:
        items = [transform] if isinstance(transform, dict) else transform
        if not isinstance(items, list) or not items:
            out.append("`transform` must be a table or a list of tables")
            items = []
        for item in items:
            if (
                not isinstance(item, dict)
                or set(item) != {"file", "replace", "with"}
                or not all(isinstance(v, str) for v in item.values())
                or not item["file"]
                or not item["replace"]
                or item["replace"] == item["with"]
            ):
                out.append(
                    "each transform needs exactly `file`, `replace` and `with` strings, "
                    "and `with` must differ from `replace`"
                )
    if "argv_fault" in entry and not _argv(entry["argv_fault"]):
        out.append("`argv_fault` must be a non-empty argv list")
    return out


def seed(tree: Path, fault: Fault, dry: bool) -> str | None:
    """Seed `fault` into `tree`, or only check that it would apply. The reason if not."""
    if fault.patch:
        patch = tree / fault.patch
        if not patch.is_file():
            return f"the patch {fault.patch} doesn't exist"
        checked = git(tree, "apply", "--check", str(patch), check=False)
        if checked.returncode != 0:
            return f"{fault.patch} doesn't apply: {checked.stderr.strip()}"
        if not dry:
            git(tree, "apply", str(patch))
        return None
    edited: dict[Path, str] = {}
    for item in fault.transforms:
        path = tree / item["file"]
        if path not in edited:
            try:
                edited[path] = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                return f"{item['file']} doesn't exist"
        count = edited[path].count(item["replace"])
        if count != 1:
            first = item["replace"].splitlines()[0] if item["replace"] else ""
            return (
                f"{item['file']}: the anchor {first!r} matches {count} times, not once"
            )
        edited[path] = edited[path].replace(item["replace"], item["with"])
    if not dry:
        for path, text in edited.items():
            path.write_text(text, encoding="utf-8")
    return None


# ── the census of Falsification notes ────────────────────────────────────────

_RUN_ENDS = {
    "not": {
        "any": [
            {"kind": "attribute_item"},
            {"kind": "line_comment"},
            {"kind": "block_comment"},
        ]
    }
}
_MARKED = re.escape(MARKER)
RULES = [
    {
        # A Rust note is in the run of doc comments and attributes that ends at a function.
        "id": "rust-note",
        "language": "Rust",
        "rule": {
            "any": [{"kind": "line_comment"}, {"kind": "block_comment"}],
            "regex": _MARKED,
            "precedes": {
                "kind": "function_item",
                "has": {"field": "name", "pattern": "$NAME"},
                "stopBy": _RUN_ENDS,
            },
        },
    },
    {
        # A Node note is a comment inside the test it's about; tests carry no doc comment.
        "id": "js-note",
        "language": "JavaScript",
        "rule": {
            "kind": "comment",
            "regex": _MARKED,
            "inside": {
                "kind": "call_expression",
                "stopBy": "end",
                "all": [
                    {
                        "has": {
                            "field": "function",
                            "regex": r"^(?:test|it)(?:\.only)?$",
                        }
                    },
                    {
                        "has": {
                            "field": "arguments",
                            "has": {
                                "nthChild": 1,
                                "any": [
                                    {"kind": "string"},
                                    {"kind": "template_string"},
                                ],
                                "pattern": "$TITLE",
                            },
                        }
                    },
                ],
            },
        },
    },
    {
        # The same, for the site's vitest suite.
        "id": "ts-note",
        "language": "TypeScript",
        "rule": {
            "kind": "comment",
            "regex": _MARKED,
            "inside": {
                "kind": "call_expression",
                "stopBy": "end",
                "all": [
                    {
                        "has": {
                            "field": "function",
                            "regex": r"^(?:test|it)(?:\.only)?$",
                        }
                    },
                    {
                        "has": {
                            "field": "arguments",
                            "has": {
                                "nthChild": 1,
                                "any": [
                                    {"kind": "string"},
                                    {"kind": "template_string"},
                                ],
                                "pattern": "$TITLE",
                            },
                        }
                    },
                ],
            },
        },
    },
]


def tracked(root: Path) -> list[str]:
    out = git(root, "ls-files", "-z", "--", *CENSUS_GLOBS).stdout
    return [p for p in out.split("\0") if p and p not in CENSUS_SKIPS]


def ast_grep(root: Path, files: list[str]) -> list[dict]:
    if not files:
        return []
    inline = "\n---\n".join(json.dumps(rule) for rule in RULES)
    try:
        out = subprocess.run(
            ["ast-grep", "scan", "--inline-rules", inline, "--json=stream", *files],
            cwd=root,
            capture_output=True,
            text=True,
            env=clean_env(),
        )
    except FileNotFoundError:
        raise SystemExit(
            "guards: ast-grep isn't on PATH; run this through `mise run guards:list`"
        ) from None
    if out.returncode != 0:
        raise SystemExit(f"guards: ast-grep failed:\n{out.stderr}")
    return [json.loads(line) for line in out.stdout.splitlines() if line.strip()]


def python_notes(path: str, text: str) -> tuple[dict[int, tuple[str, str]], list[str]]:
    """Docstring lines, keyed `<path>::<Class>::<function>`, with the docstring's text."""
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError as error:
        return {}, [f"{path} doesn't parse: {error}"]
    lines: dict[int, tuple[str, str]] = {}

    def walk(node: ast.AST, scope: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                walk(child, [*scope, child.name])
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                doc = child.body[0] if child.body else None
                if (
                    isinstance(doc, ast.Expr)
                    and isinstance(doc.value, ast.Constant)
                    and isinstance(doc.value.value, str)
                ):
                    key = "::".join([path, *scope, child.name])
                    for number in range(doc.lineno, (doc.end_lineno or doc.lineno) + 1):
                        lines[number] = (key, doc.value.value)
                walk(child, [*scope, child.name])

    walk(tree, [])
    return lines, []


@dataclass
class Census:
    notes: dict[str, str] = field(default_factory=dict)  # key -> "path:line"
    texts: dict[str, str] = field(default_factory=dict)  # key -> the note's own text
    problems: list[str] = field(default_factory=list)


def census(root: Path) -> Census:
    """Every Falsification note in the tracked tests, keyed by the item it's on."""
    result = Census()
    files = tracked(root)
    if not files:
        result.problems.append(
            f"`git ls-files` returned no {', '.join(CENSUS_GLOBS)} file; run this from "
            "the repo root"
        )
        return result
    marked: dict[tuple[str, int], str] = {}
    sources: dict[str, list[str]] = {}
    for path in files:
        try:
            text = (root / path).read_text(encoding="utf-8")
        except (FileNotFoundError, UnicodeDecodeError):
            continue
        sources[path] = text.splitlines()
        for number, line in enumerate(sources[path], 1):
            if MARKER in line:
                marked[(path, number)] = line.strip()
            elif any(rule.search(line) for rule in NEAR_MISS):
                result.problems.append(
                    f"{path}:{number} spells a note the census can't see; write it "
                    f"`{MARKER}`: {line.strip()}"
                )
    if not marked:
        result.problems.append(
            f"no {MARKER} note in {len(files)} tracked files; the census read nothing"
        )
        return result
    # Each placed line's key, and the note's text: a Rust note runs from its marker to the
    # function it's on, and a JS or TS note is its comment.
    placed: dict[tuple[str, int], tuple[str, str]] = {}
    parsed = sorted({p for p, _ in marked if not p.endswith(".py")})
    for match in ast_grep(root, parsed):
        single = match.get("metaVariables", {}).get("single", {})
        start = match["range"]["start"]["line"] + 1
        end = match["range"]["end"]["line"] + 1
        if match["ruleId"] == "rust-note":
            item = single["NAME"]["text"]
            upto = single["NAME"]["range"]["start"]["line"]
            note = "\n".join(sources[match["file"]][start - 1 : upto])
        else:
            item = single["TITLE"]["text"][1:-1]
            note = match["text"]
        for number in range(start, end + 1):
            placed[(match["file"], number)] = (f"{match['file']}::{item}", note)
    for path in sorted({p for p, _ in marked if p.endswith(".py")}):
        lines, problems = python_notes(path, "\n".join(sources[path]))
        result.problems += problems
        placed.update({(path, number): value for number, value in lines.items()})
    for (path, number), line in sorted(marked.items()):
        value = placed.get((path, number))
        if value is None:
            result.problems.append(
                f"{path}:{number} has a note that isn't on a test the census can key "
                f"(a Rust doc comment on a function, a Python docstring, or a comment "
                f"inside a JS or TS test): {line}"
            )
        else:
            key, note = value
            result.notes.setdefault(key, f"{path}:{number}")
            result.texts[key] = "\n".join(filter(None, [result.texts.get(key), note]))
    return result


def unregistered(root: Path) -> tuple[dict[str, str], list[str]]:
    """`guards/unregistered.txt`: one key per line, an optional `  # reason` after it."""
    path = root / UNREGISTERED
    if not path.is_file():
        return {}, [f"{UNREGISTERED} doesn't exist"]
    return parse_unregistered(path.read_text(encoding="utf-8"), UNREGISTERED)


def parse_unregistered(text: str, label: str) -> tuple[dict[str, str], list[str]]:
    keys: dict[str, str] = {}
    problems: list[str] = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.startswith("#"):
            continue
        key, _, reason = line.partition("  # ")
        key = key.strip()
        if key in keys:
            problems.append(f"{label}:{number} lists {key} twice")
        keys[key] = reason.strip()
    return keys, problems


def default_base(root: Path) -> str:
    # ratchet.py's rule: locally the ratchet is only as good as `origin/main`, and the run
    # that decides a merge is CI's, which passes `--base` explicitly.
    out = git(root, "merge-base", "HEAD", "origin/main", check=False)
    if out.returncode != 0:
        raise SystemExit(
            f"guards: no merge base of HEAD and origin/main, so {UNREGISTERED} has "
            "nothing to shrink from. Fetch origin, or pass --base <ref>."
        )
    return out.stdout.strip()


def base_unregistered(root: Path, ref: str) -> dict[str, str] | None:
    """The list at `ref`, or None when `ref` predates it (the bootstrap)."""
    commit = git(
        root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False
    )
    if commit.returncode != 0:
        raise SystemExit(f"guards: --base {ref} doesn't name a commit")
    spec = f"{commit.stdout.strip()}:{UNREGISTERED}"
    if git(root, "cat-file", "-e", spec, check=False).returncode != 0:
        return None
    keys, _ = parse_unregistered(git(root, "show", spec).stdout, spec)
    return keys


def grown(
    listed: dict[str, str], base: dict[str, str], notes: dict[str, str]
) -> list[str]:
    """Keys the base's list doesn't have and that no move accounts for.

    A test that's renamed or moved re-keys its note. A new key is that move when it takes
    the place of a base key whose note is gone from the tree, and the two share their path
    (a rename) or their name (a move). Each base key takes one replacement, so a rename and
    a move in one change take two PRs, as in ratchet.py. The note has to be gone: a PR that
    registers one listed note and lists a new one in the same file isn't a move.
    """
    vacated = [k for k in base if k not in listed and k not in notes]
    out: list[str] = []
    for key in (k for k in listed if k not in base):
        path, _, item = key.partition("::")
        pair = next(
            (
                old
                for old in vacated
                if old.partition("::")[0] == path or old.partition("::")[2] == item
            ),
            None,
        )
        if pair is None:
            out.append(key)
        else:
            vacated.remove(pair)
    return out


def names_its_guard(fault: Fault, key: str, text: str) -> bool:
    """Whether the note `key` is about `fault`'s guard: on its test, or naming it."""
    item = key.partition("::")[2]
    if fault.guard in (key, item) or fault.guard.endswith("::" + item):
        return True
    test = fault.guard.rpartition("::")[2]
    return re.search(rf"(?<!\w){re.escape(test)}(?!\w)", text) is not None


def cmd_list(root: Path, base_ref: str | None) -> int:
    faults, problems = load(root)
    for fault in faults:
        why = seed(root, fault, dry=True)
        if why:
            problems.append(f"stale anchor: {fault.id}: {why}")
    found = census(root)
    problems += found.problems
    listed, bad = unregistered(root)
    problems += bad
    registered: dict[str, str] = {}
    for fault in faults:
        if fault.note is None:
            continue
        if fault.note not in found.notes:
            problems.append(
                f"{fault.id}: `note = {fault.note!r}` names no {MARKER} note; "
                "was the test renamed or moved?"
            )
        elif not names_its_guard(fault, fault.note, found.texts.get(fault.note, "")):
            problems.append(
                f"{fault.id}: `note = {fault.note!r}` is on another test than its guard "
                f"{fault.guard!r}, and its text doesn't name that test either. An entry "
                "registers the note about the guard it seeds"
            )
        registered.setdefault(fault.note, fault.id)
    for key in listed:
        if key in registered:
            problems.append(
                f"{key} has an entry now ({registered[key]}); delete it from {UNREGISTERED}"
            )
        elif key not in found.notes:
            problems.append(
                f"{key} in {UNREGISTERED} isn't a {MARKER} note any more; delete the line"
            )
    for key, where in sorted(found.notes.items()):
        if key not in registered and key not in listed:
            problems.append(
                f"{key} ({where}) carries a {MARKER} note with no entry in {REGISTRY}; "
                "register its fault there. New notes can't join the unregistered list"
            )
    ref = base_ref or default_base(root)
    label = base_ref or ref[:12]
    base = base_unregistered(root, ref)
    if base is not None:
        for key in grown(listed, base, found.notes):
            problems.append(
                f"{key} is in {UNREGISTERED} but not in {label}'s copy, and it doesn't "
                f"replace a key there whose note is gone. The list only shrinks: register "
                f"the note's fault in {REGISTRY} instead"
            )
    for problem in problems:
        print(f"guards: {problem}", file=sys.stderr)
    if problems:
        return 1
    compared = (
        f"{label}, which has no {UNREGISTERED}: the bootstrap, so it can't have grown"
        if base is None
        else label
    )
    print(
        f"guards: {len(faults)} faults registered; {len(found.notes)} notes, "
        f"{len(registered)} with an entry and {len(listed)} in {UNREGISTERED} "
        f"(compared with {compared})"
    )
    return 0


# ── fire ─────────────────────────────────────────────────────────────────────


def reported(output: str, kind: str, passed: bool) -> set[str]:
    pattern = {
        ("cargo", True): CARGO_OK,
        ("cargo", False): CARGO_FAILED,
        ("pytest", True): PYTEST_OK,
        ("pytest", False): PYTEST_FAILED,
        ("node", True): NODE_OK,
        ("node", False): NODE_FAILED,
    }[(kind, passed)]
    return {m.group(1).strip() for m in pattern.finditer(output)}


def verdict(fault: Fault, code: int, output: str) -> str | None:
    """None when the fault fired; otherwise why it didn't."""
    if code == 0:
        return "the command passed with the fault seeded"
    if fault.message and fault.message not in output:
        return (
            f"the command failed ({code}) but its output never says {fault.message!r}"
        )
    broke = BUILD_BROKE.findall(output)
    if fault.expect == "test-failed":
        kind = runner(fault.run[-1]) or ""
        if fault.guard in reported(output, kind, passed=False):
            return None
        if broke:
            codes = sorted({c for c in broke if c})
            return (
                "the build broke"
                + (
                    f" ({', '.join('error[' + c + ']' for c in codes)})"
                    if codes
                    else ""
                )
                + "; a compile error isn't this guard failing"
            )
        return (
            f"the command failed ({code}), but not with {fault.guard} reported failed"
        )
    if fault.expect == "compile-error":
        if f"error[{fault.code}]" in output:
            return None
        codes = sorted({c for c in broke if c})
        return f"expected error[{fault.code}], got {codes or 'no rustc error code'}"
    return None


@dataclass
class Tree:
    """A scratch worktree, reset to the caller's tree between faults."""

    root: Path
    path: Path
    scratch: Path

    @classmethod
    def make(cls, root: Path) -> Tree:
        scratch = Path(tempfile.mkdtemp(prefix="guards-fire-"))
        path = scratch / "tree"
        git(root, "worktree", "add", "--detach", "--quiet", str(path), "HEAD")
        tree = cls(root, path, scratch)
        try:
            tree.overlay()
        except BaseException:
            tree.remove()
            raise
        return tree

    def overlay(self) -> int:
        """Copy the caller's uncommitted changes in, and stage them in the scratch index.

        `git checkout -- .` restores from the index, so staging them here is what makes the
        reset between faults return to the caller's tree rather than to HEAD. The scratch
        worktree has its own index; nothing here writes the caller's.
        """
        changed = git(self.root, "diff", "--name-only", "-z", "HEAD").stdout.split("\0")
        untracked = git(
            self.root, "ls-files", "--others", "--exclude-standard", "-z"
        ).stdout.split("\0")
        paths = sorted({p for p in changed + untracked if p})
        for rel in paths:
            source, target = self.root / rel, self.path / rel
            if source.exists() or source.is_symlink():
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.is_symlink() or target.exists():
                    target.unlink()
                shutil.copy2(source, target, follow_symlinks=False)
            elif target.exists() or target.is_symlink():
                target.unlink()
        git(self.path, "add", "-A")
        return len(paths)

    def reset(self) -> None:
        git(self.path, "checkout", "--quiet", "--", ".")
        git(self.path, "clean", "-fdq")

    def remove(self) -> None:
        git(self.root, "worktree", "remove", "--force", str(self.path), check=False)
        shutil.rmtree(self.scratch, ignore_errors=True)


class Stopped(Exception):
    """The run is ending (a signal, or another worker's error), so nothing new starts."""


class Procs:
    """Every command the workers have running, so a signal can end them all.

    Each command starts its own session, and a kill goes to its process group: killing the
    child alone leaves cargo's rustc and test processes running, holding the output pipe open
    and the scratch tree in use.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.live: set[subprocess.Popen] = set()
        self.stopping = False

    def run(
        self, argv: list[str], cwd: Path, env: dict[str, str], timeout: int
    ) -> tuple[int | None, str]:
        """The exit code (None when it timed out) and the combined output."""
        with self.lock:
            if self.stopping:
                raise Stopped
            proc = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                start_new_session=True,
            )
            self.live.add(proc)
        try:
            try:
                out, _ = proc.communicate(timeout=timeout)
                code: int | None = proc.returncode
            except subprocess.TimeoutExpired:
                self.kill(proc)
                out, _ = proc.communicate()
                code = None
        finally:
            with self.lock:
                self.live.discard(proc)
        if self.stopping:
            # A command killed on the way out failed for that reason, not the fault's.
            raise Stopped
        return code, out

    @staticmethod
    def kill(proc: subprocess.Popen) -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def stop(self) -> None:
        with self.lock:
            self.stopping = True
            live = list(self.live)
        for proc in live:
            self.kill(proc)


def run_commands(
    fault: Fault,
    tree: Path,
    env: dict[str, str],
    timeout: int,
    seeded: bool,
    procs: Procs,
) -> tuple[int, str, str]:
    """The exit code, the log (each argv echoed before its output), and the output alone.

    Verdicts read the output alone: an echoed argv that happens to contain an entry's
    `message` would otherwise match on every run.
    """
    output: list[str] = []
    said: list[str] = []
    code = 0
    with tempfile.TemporaryDirectory(prefix="guards-empty-") as empty:
        for index, argv in enumerate(fault.run):
            if seeded and fault.argv_fault and index == len(fault.run) - 1:
                argv = argv + [
                    a.replace("{empty_dir}", empty) for a in fault.argv_fault
                ]
            output.append(f"$ {' '.join(argv)}\n")
            try:
                done, out = procs.run(argv, tree, env, timeout)
            except FileNotFoundError:
                output.append(f"guards: {argv[0]} isn't on PATH\n")
                return 127, "".join(output), "".join(said)
            said.append(ANSI.sub("", out))
            output.append(said[-1])
            if done is None:
                output.append(f"guards: timed out after {timeout} s\n")
                return 124, "".join(output), "".join(said)
            code = done
            if code != 0:
                break
    return code, "".join(output), "".join(said)


def tail(text: str, lines: int = 25) -> str:
    return "\n".join("    " + line for line in text.rstrip().splitlines()[-lines:])


def command_key(fault: Fault) -> tuple:
    return (fault.suite, tuple(map(tuple, fault.run)))


@dataclass
class Worker:
    """One scratch tree and the target it builds into. Worker 1 is the serial run's."""

    number: int
    tree: Tree
    env: dict[str, str]
    binding_env: dict[str, str]
    # The commands this worker ran, clean or seeded: its restored pass runs each again.
    touched: dict[tuple, None] = field(default_factory=dict)


@dataclass
class Task:
    key: object
    pinned: bool  # bindings share one venv, so worker 1 runs them all


class Board:
    """One phase's work: a queue per worker, and the results the main thread prints in order.

    A worker takes from the front of its own queue, then from the back of the longest other
    queue, so a worker whose commands build fast doesn't sit idle. A worker's queue starts with
    the commands it already built, and a pinned task stays where it is.
    """

    def __init__(self, queues: list[list[Task]], steal: bool) -> None:
        self.queues = [list(q) for q in queues]
        self.steal = steal
        self.cond = threading.Condition()
        self.done: dict[object, list] = {}
        self.error: BaseException | None = None

    def take(self, number: int) -> Task | None:
        with self.cond:
            own = self.queues[number]
            if own:
                return own.pop(0)
            if not self.steal:
                return None
            for other in sorted(self.queues, key=len, reverse=True):
                for index in range(len(other) - 1, -1, -1):
                    if not other[index].pinned:
                        return other.pop(index)
            return None

    def put(self, key: object, value: object) -> None:
        with self.cond:
            self.done.setdefault(key, []).append(value)
            self.cond.notify_all()

    def fail(self, error: BaseException) -> None:
        with self.cond:
            if self.error is None:
                self.error = error
            self.cond.notify_all()

    def get(self, key: object, count: int = 1) -> list:
        """The results for `key` once `count` workers have put one. Wakes each second, so a
        signal reaches the main thread while it waits."""
        with self.cond:
            while len(self.done.get(key, [])) < count:
                if self.error is not None:
                    raise self.error
                self.cond.wait(timeout=1)
            return self.done[key]


def start(workers: list[Worker], board: Board, do) -> list[threading.Thread]:
    def loop(worker: Worker) -> None:
        try:
            while (task := board.take(worker.number - 1)) is not None:
                board.put(task.key, do(worker, task))
        except Stopped:
            board.fail(Stopped())
        # A worker's crash ends the run, not just its thread: the main thread raises it.
        except BaseException as error:  # noqa: BLE001 - handed on, not swallowed
            board.fail(error)

    threads = [
        threading.Thread(target=loop, args=(w,), daemon=True, name=f"guards-{w.number}")
        for w in workers
    ]
    for thread in threads:
        thread.start()
    return threads


def command_weights(
    faults: list[Fault], cost: Callable[[Fault], int] = lambda fault: 1
) -> dict[tuple, int]:
    """Each command's weight in a split, in registry order: the sum of its entries' `cost`,
    by default the number of entries it runs (the split over workers)."""
    weight: dict[tuple, int] = {}
    for fault in faults:
        key = command_key(fault)
        weight[key] = weight.get(key, 0) + cost(fault)
    return weight


def entry_cost(fault: Fault) -> int:
    """An entry's seconds on CI's runner, roughly, for the split over shards: an entry that
    builds the CLI (its argv names microvms-cli) about 16, another Rust entry (a cargo build)
    about 14, and a script entry about 6. Measured per fault on main's push at 6a868e9 (cargo
    13.7 s, the rest 5.8 s) and on #340's second round (entries naming microvms-cli 17.8 s).
    The module docstring's `--shard` says why a cost and not a count."""
    if any("microvms-cli" in arg for argv in fault.run for arg in argv):
        return 16
    return 14 if fault.suite == "rust" else 6


def spread(
    weight: dict[tuple, int], bins: int, load: list[int] | None = None
) -> list[list[tuple]]:
    """`weight`'s keys in `bins` bins, heaviest first onto the lightest bin, from `load`.

    A tie in weight goes to the key that comes first in `weight` (registry order) and a tie in
    load to the lower bin, so the same keys always land in the same bins. No hash and no set
    order: `hash()` of a string changes with PYTHONHASHSEED from one process to the next.
    """
    order = {key: index for index, key in enumerate(weight)}
    load = list(load or [0] * bins)
    out: list[list[tuple]] = [[] for _ in range(bins)]
    for key in sorted(weight, key=lambda k: (-weight[k], order[k])):
        lightest = min(range(bins), key=lambda n: (load[n], n))
        out[lightest].append(key)
        load[lightest] += weight[key]
    return out


def assign(selected: list[Fault], jobs: int) -> list[list[tuple]]:
    """Each worker's commands for the clean pass, heaviest first onto the lightest worker.

    Bindings commands go to worker 1, which holds `--venv`. Within a worker, commands keep
    the registry's order, so the main thread's in-order printing waits as little as it can.
    """
    weight = command_weights(selected)
    order = {key: index for index, key in enumerate(weight)}
    pinned = [k for k in weight if k[0] == "bindings"]
    load = [sum(weight[k] for k in pinned)] + [0] * (jobs - 1)
    queues = spread({k: w for k, w in weight.items() if k[0] != "bindings"}, jobs, load)
    queues[0] = pinned + queues[0]
    return [sorted(q, key=order.__getitem__) for q in queues]


# ASCII digits only: `\d` would take any script's digits, which `int` reads too.
SHARD = re.compile(r"([0-9]+)/([0-9]+)")


def parse_shard(text: str) -> tuple[int, int] | None:
    """`k/N` as (k, N) when 0 <= k < N; None otherwise."""
    match = SHARD.fullmatch(text)
    if match is None:
        return None
    k, n = map(int, match.groups())
    return (k, n) if k < n else None


def shard(selected: list[Fault], k: int, n: int) -> list[Fault]:
    """Shard `k` of `n` (from 0) of `selected`, in registry order: whole commands, weighted by
    `entry_cost` and spread by `spread` over `n` bins. The module docstring's `--shard` says
    why."""
    weights = command_weights(selected, entry_cost)
    keep = set(spread(weights, n)[k])
    return [fault for fault in selected if command_key(fault) in keep]


def check_pass(
    selected: list[Fault], board: Board, counts: dict[tuple, int], log, label: str
) -> bool:
    """Print each command's result in registry order. False if any entry can't prove a thing.

    A red command, a guard the run never reports passing, or a `message` the passing run
    already prints each make an entry's later verdict meaningless. A command that more than one
    worker ran must pass in every one of them.
    """
    printed: set[tuple] = set()
    ok = True
    for fault in selected:
        key = command_key(fault)
        runs = board.get(key, counts[key])
        if key not in printed:
            printed.add(key)
            print(
                f"guards: {label} run for {fault.id} ({max(r[3] for r in runs):.1f} s)"
            )
            log(
                f"{fault.id}.{label}.log",
                runs[0][1]
                if len(runs) == 1
                else "".join(f"## worker {r[4]}\n{r[1]}" for r in runs),
            )
        for code, output, said, _, _ in runs:
            if code != 0:
                print(
                    f"already red: {fault.id}: the command exits {code} with no fault "
                    f"seeded ({label} run)\n{tail(output)}"
                )
                ok = False
                break
            if fault.expect == "test-failed" and fault.guard not in reported(
                said, runner(fault.run[-1]) or "", passed=True
            ):
                print(
                    f"guard not found: {fault.id}: the {label} run never reports "
                    f"{fault.guard} passing\n{tail(output)}"
                )
                ok = False
                break
            if fault.message and fault.message in said:
                print(
                    f"weak message: {fault.id}: the {label} run already prints "
                    f"{fault.message!r}, so finding it with the fault seeded proves "
                    "nothing; use a line only the failure prints"
                )
                ok = False
                break
    return ok


# ── --affected ───────────────────────────────────────────────────────────────


def changed_since(root: Path, ref: str) -> tuple[set[str], str]:
    """Paths that differ between the merge base with `ref` and the working tree, both sides
    of a rename, untracked files included; and the merge base."""
    base = git(root, "merge-base", "HEAD", ref, check=False)
    if base.returncode != 0:
        raise SystemExit(
            f"guards: no merge base of HEAD and {ref}, so --affected has nothing to diff "
            "against. Fetch it, or pass another --base."
        )
    commit = base.stdout.strip()
    diff = git(root, "diff", "--name-only", "--no-renames", "-z", commit).stdout
    untracked = git(root, "ls-files", "--others", "--exclude-standard", "-z").stdout
    return {p for p in (diff + "\0" + untracked).split("\0") if p}, commit


def base_entries(root: Path, commit: str) -> dict[str, dict]:
    """The registry's entries at `commit`, by id. Empty when it has no registry."""
    spec = f"{commit}:{REGISTRY}"
    if git(root, "cat-file", "-e", spec, check=False).returncode != 0:
        return {}
    try:
        data = tomllib.loads(git(root, "show", spec).stdout)
    except tomllib.TOMLDecodeError:
        return {}
    return {
        e["id"]: e
        for e in data.get("fault", [])
        if isinstance(e, dict) and isinstance(e.get("id"), str)
    }


def workflow_inputs(text: str) -> list[str]:
    """The workflow's top-level `env` block and its `guards` job, blank and comment lines
    dropped. A line scan, not a YAML parse: this script has no dependencies, and ci.yml starts
    each top-level key at column 0 and each job's key at two spaces."""
    kept: list[str] = []
    top = job = None
    for line in text.splitlines():
        body = line.strip()
        if not body or body.startswith("#"):
            continue
        if not line[0].isspace():
            top, job = line.split(":", 1)[0], None
        elif top == "jobs" and re.match(r"  \S", line):
            job = line.split(":", 1)[0].strip()
        if top == "env" or (top == "jobs" and job == CI_JOB):
            kept.append(line)
    return kept


def workflow_inputs_changed(root: Path, commit: str) -> bool:
    """Whether ci.yml's `guards` job or its top-level `env` differs between `commit` and the
    working tree. A deleted ci.yml differs from any base that had them."""
    try:
        head = (root / CI_WORKFLOW).read_text(encoding="utf-8")
    except FileNotFoundError:
        head = ""
    # Empty when the base has no ci.yml.
    base = git(root, "show", f"{commit}:{CI_WORKFLOW}", check=False).stdout
    return workflow_inputs(base) != workflow_inputs(head)


def crate_dirs(root: Path, files: set[str]) -> dict[str, str]:
    """Each workspace package's name and directory, from the tracked Cargo.toml files."""
    out: dict[str, str] = {}
    for path in sorted(f for f in files if f.endswith("Cargo.toml")):
        try:
            package = tomllib.loads((root / path).read_text(encoding="utf-8")).get(
                "package", {}
            )
        except (OSError, tomllib.TOMLDecodeError):
            continue
        if isinstance(package.get("name"), str):
            out[package["name"]] = str(Path(path).parent)
    return out


def sibling_scripts(root: Path, scripts: set[str], files: set[str]) -> set[str]:
    """The scripts beside each of `scripts` that it loads by file name (`runpy.run_path`
    on `Path(__file__).with_name("x.py")`) or imports, and theirs in turn. Read with `ast`:
    ci-local.py runs from check-ci-parity.py's `plan()`, so a change there moves ci-local's
    suite too."""
    seen: set[str] = set()
    todo = sorted(scripts)
    while todo:
        path = todo.pop()
        if path in seen:
            continue
        seen.add(path)
        try:
            tree = ast.parse((root / path).read_text(encoding="utf-8"))
        except (OSError, SyntaxError, ValueError):
            continue
        here = Path(path).parent
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value.endswith(".py") and "/" not in node.value:
                    names.add(node.value)
            elif isinstance(node, ast.Import):
                names |= {f"{a.name}.py" for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names.add(f"{node.module}.py")
        for name in names:
            sibling = str(here / name) if str(here) != "." else name
            if sibling in files and sibling not in seen:
                todo.append(sibling)
    return seen - scripts


def fault_files(
    root: Path, fault: Fault, files: set[str], crates: dict[str, str]
) -> set[str]:
    """The files an entry's verdict rests on: what it seeds, and the guard and gate it runs.

    The guard's file is found where the entry says it: its `note`, a path in `guard` or in
    an argv (a pytest node id, a script, `-s DIR -p FILE` for unittest and the script that
    suite tests), and for a cargo test the file in the `-p` crate that defines the test
    function, or the crate's clippy.toml for a lint.
    """
    out = {t["file"] for t in fault.transforms}
    if fault.patch:
        out.add(fault.patch)
        numstat = git(root, "apply", "--numstat", fault.patch, check=False).stdout
        out |= {line.split("\t")[-1] for line in numstat.splitlines() if "\t" in line}
    if fault.note:
        out.add(fault.note.partition("::")[0])
    words = fault.guard.split()
    for argv in fault.run:
        words += [w for a in argv for w in a.split()]
        if "-s" in argv and "-p" in argv:
            where, suite = argv[argv.index("-s") + 1], argv[argv.index("-p") + 1]
            out |= {f for f in files if fnmatch.fnmatchcase(f, f"{where}/{suite}")}
            # And the script the suite tests, whose change can move its verdict as much as
            # the suite's own: test_ratchet.py is ratchet.py's, test_model_drift.py is
            # check-model-drift.py's.
            name = suite.removeprefix("test_").removesuffix(".py").replace("_", "-")
            out |= {f"{where}/{n}.py" for n in (name, f"check-{name}")} & files
    for word in words:
        for part in word.split("::"):
            path = part.removeprefix("./").rstrip(":,")
            if path in files:
                out.add(path)
    out |= sibling_scripts(root, {f for f in out if f.endswith(".py")}, files)
    last = fault.run[-1]
    package = next(
        (
            last[i + 1]
            for i, a in enumerate(last[:-1])
            if a in ("-p", "--package") and last[i + 1] in crates
        ),
        None,
    )
    if package is not None:
        where = crates[package]
        if fault.expect == "lint-error":
            clippy = f"{where}/clippy.toml"
            if clippy in files:
                out.add(clippy)
        elif fault.expect == "test-failed" and runner(last) == "cargo":
            name = fault.guard.rpartition("::")[2]
            found = git(
                root,
                "grep",
                "--untracked",
                "-l",
                "-E",
                rf"fn {re.escape(name)}\b",
                "--",
                where,
                check=False,
            ).stdout
            out |= set(found.split())
    return out


def affected(
    root: Path, faults: list[Fault], ref: str
) -> tuple[dict[str, str], str, int]:
    """Why each affected entry is selected (id -> reason), the base, and the changed count."""
    changed, commit = changed_since(root, ref)
    before = base_entries(root, commit)
    files = set(git(root, "ls-files", "-z").stdout.split("\0")) | changed
    files.discard("")
    crates = crate_dirs(root, files)
    current = tomllib.loads((root / REGISTRY).read_text(encoding="utf-8"))["fault"]
    raw = {e["id"]: e for e in current}
    reasons: dict[str, str] = {}
    this = "scripts/check-guards-fire.py"
    inputs = sorted(
        p for p in changed if p in BUILD_INPUTS or Path(p).name == "Cargo.toml"
    )
    workflow = CI_WORKFLOW in changed and workflow_inputs_changed(root, commit)
    for fault in faults:
        if this in changed:
            reasons[fault.id] = f"{this} changed, and it decides every verdict"
        elif inputs:
            reasons[fault.id] = (
                f"{inputs[0]} changed, and every build or command reads it"
            )
        elif workflow:
            reasons[fault.id] = (
                f"the `{CI_JOB}` job or the top-level `env` in {CI_WORKFLOW} changed, "
                "and CI runs every command under them"
            )
        elif fault.id not in before:
            reasons[fault.id] = f"the entry is new in {REGISTRY}"
        elif before[fault.id] != raw[fault.id]:
            reasons[fault.id] = f"the entry changed in {REGISTRY}"
        else:
            hit = sorted(fault_files(root, fault, files, crates) & changed)
            if hit:
                reasons[fault.id] = f"{hit[0]} changed"
    return reasons, commit, len(changed)


def cmd_fire(root: Path, args: argparse.Namespace) -> int:
    faults, problems = load(root)
    if problems:
        for problem in problems:
            print(f"guards: {problem}", file=sys.stderr)
        return 1
    if args.jobs < 1:
        print("guards: --jobs needs a count of 1 or more", file=sys.stderr)
        return 1
    spec = None
    if args.shard is not None:
        spec = parse_shard(args.shard)
        if spec is None:
            print(
                f"guards: --shard takes k/N with 0 <= k < N; got {args.shard}",
                file=sys.stderr,
            )
            return 1
    unknown = sorted(set(args.only) - {f.id for f in faults})
    if unknown:
        print(f"guards: no entry has the id {', '.join(unknown)}", file=sys.stderr)
        return 1
    selected = [
        f
        for f in faults
        if (not args.only or f.id in args.only)
        and (not args.suite or f.suite in args.suite)
    ]
    if args.affected:
        ref = args.base or "origin/main"
        reasons, commit, count = affected(root, selected, ref)
        skipped = [f.id for f in selected if f.id not in reasons]
        selected = [f for f in selected if f.id in reasons]
        for fault in selected:
            print(f"affected: {fault.id}: {reasons[fault.id]}")
        print(
            f"guards: --affected against {ref} ({commit[:12]}, {count} paths changed) "
            f"selects {len(selected)} and skips {len(skipped)} entries"
            + (f": {', '.join(skipped)}" if skipped else "")
        )
        if skipped:
            print(
                "guards: --affected selects by the files each entry names, so a change "
                "that reaches a skipped guard some other way isn't seen; the full fire "
                "runs on every push to main, or here without --affected"
            )
        if not selected:
            print("guards: no selected entry names a changed file, so nothing to fire")
            return 0
    elif args.base:
        print("guards: --base is for --affected", file=sys.stderr)
        return 1
    venv = Path(args.venv).resolve() if args.venv else None
    if venv is None:
        bindings = [f for f in selected if f.suite == "bindings"]
        if bindings and (args.only or args.suite):
            print(
                "guards: bindings entries rebuild the extension into the active "
                "environment; pass --venv DIR",
                file=sys.stderr,
            )
            return 1
        if bindings:
            print(
                f"guards: skipping {len(bindings)} bindings entries; they run with "
                "`--suite bindings --venv DIR` (CI's bindings job)"
            )
        selected = [f for f in selected if f.suite != "bindings"]
    if not selected and args.affected:
        # Affected, then filtered to nothing by the missing --venv: the same answer as
        # nothing affected, since the bindings line above says what didn't run.
        print("guards: no affected entry runs without --venv, so nothing to fire")
        return 0
    if not selected:
        print("guards: no entry selected", file=sys.stderr)
        return 1
    if spec is not None:
        k, n = spec
        whole, commands = len(selected), len(command_weights(selected))
        selected = shard(selected, k, n)
        print(
            f"guards: shard {k} of {n} keeps {len(selected)} of {whole} selected entries "
            f"({len(command_weights(selected))} of {commands} commands)"
        )
        if not selected:
            print("guards: this shard's slice is empty, so nothing to fire")
            return 0
    env = clean_env()
    env["CARGO_TARGET_DIR"] = str(
        Path(args.target_dir).resolve()
        if args.target_dir
        else Path(env.get("CARGO_TARGET_DIR") or root / "target").resolve()
        / "guards-fire"
    )
    env.pop("VIRTUAL_ENV", None)
    logs = Path(args.logs).resolve() if args.logs else None
    if logs:
        logs.mkdir(parents=True, exist_ok=True)

    def log(name: str, text: str) -> None:
        if logs:
            (logs / name).write_text(text, encoding="utf-8")

    # A signal ends the run through `finally`, so no scratch worktree, extra target or
    # command outlives it.
    def stop(signum: int, _frame: object) -> None:
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, stop)
    # `ci-local.py` sends SIGINT first on a job's timeout, as the runner does.
    signal.signal(signal.SIGINT, stop)
    head = git(root, "rev-parse", "--short", "HEAD").stdout.strip()
    state = {"seeded": False}
    procs = Procs()
    workers: list[Worker] = []
    threads: list[threading.Thread] = []
    jobs = min(args.jobs, len(selected))
    try:
        for number in range(1, jobs + 1):
            tree = Tree.make(root)
            wenv = dict(env)
            if number > 1:
                # Its own target, removed with its tree: cargo's build lock would otherwise
                # queue every worker behind one build.
                wenv["CARGO_TARGET_DIR"] = str(tree.scratch / "target")
            benv = dict(wenv)
            if venv is not None:
                benv["VIRTUAL_ENV"] = str(venv)
                benv["PATH"] = os.pathsep.join(
                    [str(venv / "bin"), wenv.get("PATH", "")]
                )
            workers.append(Worker(number, tree, wenv, benv))
        first = workers[0].tree
        changed = git(first.path, "diff", "--cached", "--name-only", "HEAD").stdout
        print(
            f"guards: tree {head} plus {len(changed.split())} uncommitted paths, "
            f"CARGO_TARGET_DIR={env['CARGO_TARGET_DIR']}"
        )
        if jobs > 1:
            print(
                f"guards: {jobs} workers, each in its own scratch worktree; worker 1 builds "
                "in the directory above, the others in a target beside their worktree, "
                "removed when the run ends"
            )

        def run_clean(label: str):
            def do(worker: Worker, task: Task):
                fault = by_key[task.key]
                started = time.monotonic()
                fenv = worker.binding_env if fault.suite == "bindings" else worker.env
                worker.touched.setdefault(task.key)
                code, output, said = run_commands(
                    fault, worker.tree.path, fenv, args.timeout, False, procs
                )
                return code, output, said, time.monotonic() - started, worker.number

            return do

        def phase(queues: list[list[Task]], steal: bool, do) -> Board:
            board = Board(queues, steal)
            threads.extend(start(workers, board, do))
            return board

        def join() -> None:
            while threads:
                threads.pop().join()

        by_key: dict[tuple, Fault] = {}
        for fault in selected:
            by_key.setdefault(command_key(fault), fault)
        clean_queues = [
            [Task(k, k[0] == "bindings") for k in q] for q in assign(selected, jobs)
        ]
        counts = dict.fromkeys(by_key, 1)
        board = phase(clean_queues, True, run_clean("clean"))
        ok = check_pass(selected, board, counts, log, "clean")
        join()
        if not ok:
            return 1
        # Each fault starts on the worker that built its command clean.
        owner = {k: n for n, w in enumerate(workers) for k in w.touched}
        fault_queues: list[list[Task]] = [[] for _ in workers]
        for index, fault in enumerate(selected):
            fault_queues[owner[command_key(fault)]].append(
                Task(index, fault.suite == "bindings")
            )

        def run_fault(worker: Worker, task: Task) -> tuple[str, bool]:
            fault = selected[task.key]
            worker.touched.setdefault(command_key(fault))
            worker.tree.reset()
            why = seed(worker.tree.path, fault, dry=False)
            if why:
                return f"stale anchor: {fault.id}: {why}", True
            started = time.monotonic()
            fenv = worker.binding_env if fault.suite == "bindings" else worker.env
            code, output, said = run_commands(
                fault, worker.tree.path, fenv, args.timeout, True, procs
            )
            elapsed = time.monotonic() - started
            log(f"{fault.id}.fault.log", output)
            why = verdict(fault, code, said)
            if why is None:
                return f"fired: {fault.id} ({elapsed:.1f} s)", False
            return (
                f"DID NOT FIRE: {fault.id}: {why} ({elapsed:.1f} s)\n{tail(output)}",
                True,
            )

        state["seeded"] = True
        failures = 0
        total = time.monotonic()
        board = phase(fault_queues, True, run_fault)
        for index in range(len(selected)):
            [(line, failed)] = board.get(index)
            print(line)
            failures += failed
        join()
        print(
            f"guards: {len(selected) - failures} of {len(selected)} fired "
            f"({time.monotonic() - total:.1f} s of faults)"
        )
        # The restored leg, and what keeps the caller's target and venv clean: see the
        # module docstring. Every worker runs again each command it ran, so each scratch tree
        # is shown to come back clean, and worker 1's target ends on clean builds.
        for worker in workers:
            worker.tree.reset()
        started = time.monotonic()
        restored_queues = [[Task(k, True) for k in w.touched] for w in workers]
        counts = {k: sum(k in w.touched for w in workers) for k in by_key}
        board = phase(restored_queues, False, run_clean("restored"))
        ok = check_pass(selected, board, counts, log, "restored")
        join()
        if not ok:
            print("guards: the tree didn't come back clean after the faults")
            return 1
        state["seeded"] = False
        print(
            f"guards: restored, every command passes again "
            f"({time.monotonic() - started:.1f} s)"
        )
        return 1 if failures else 0
    finally:
        procs.stop()
        for thread in threads:
            thread.join(timeout=60)
        for worker in workers:
            worker.tree.remove()
        if state["seeded"]:
            print(
                f"guards: stopped with a fault's build in {env['CARGO_TARGET_DIR']}"
                + (f" and {venv}" if venv else "")
                + ". Anything that reads them can run the faulted code; run `fire` "
                "again to the end",
                file=sys.stderr,
            )


def main(argv: list[str] | None = None) -> int:
    # A CI log is a pipe, and a fault run is minutes long: print each verdict as it lands.
    sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root", default=None, help="the repository (default: this script's)"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    listing = sub.add_parser(
        "list", help="check the registry against the tree; no builds"
    )
    listing.add_argument(
        "--base",
        help=f"the ref {UNREGISTERED} may only shrink from (default: the merge base with "
        "origin/main)",
    )
    fire = sub.add_parser("fire", help="seed each fault and require its guard to fail")
    fire.add_argument("--only", action="append", default=[], metavar="ID")
    fire.add_argument("--suite", action="append", default=[], choices=SUITES)
    fire.add_argument(
        "--venv", help="the environment bindings entries build into and test from"
    )
    fire.add_argument("--logs", help="write each command's output here")
    fire.add_argument(
        "--target-dir",
        help="where cargo builds (default: guards-fire under the caller's target; the "
        "module docstring says why it isn't the caller's own)",
    )
    fire.add_argument(
        "--timeout", type=int, default=1800, help="seconds per command (1800)"
    )
    fire.add_argument(
        "--jobs",
        type=int,
        default=1,
        metavar="N",
        help="run faults in N scratch worktrees at once, each with its own target (1)",
    )
    fire.add_argument(
        "--affected",
        action="store_true",
        help="select only the entries whose files or registry text changed against --base",
    )
    fire.add_argument(
        "--base",
        help="the ref --affected diffs against, through its merge base with HEAD "
        "(default: origin/main)",
    )
    fire.add_argument(
        "--shard",
        metavar="K/N",
        help="fire only shard K of N (from 0) of the selection, as one leg of CI's matrix",
    )
    args = parser.parse_args(argv)
    root = (
        Path(args.root).resolve() if args.root else Path(__file__).resolve().parents[1]
    )
    if args.command == "list":
        return cmd_list(root, args.base)
    return cmd_fire(root, args)


if __name__ == "__main__":
    sys.exit(main())
