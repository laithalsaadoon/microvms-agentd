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
quietly emptied, and every gate would stay green. The registry, the `*.toml` files in
`guards/faults/`, records each proof as a fault this script can seed, and
`guards/unregistered.txt` lists the notes that don't have one yet.

Three subcommands:

  `list`  (in `mise run check`; no builds) reads the registry and holds it to the tree:
          - the registry's files load (the loader below): every entry is well formed (the
            schema below), and no id is in two entries;
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

  `fire`  (CI's `guards` job; `mise run guards:fire`) makes a detached
          temporary git worktree at HEAD, copies the caller's uncommitted changes into it,
          and runs every selected entry's command once on that clean tree. A command that's
          already red there proves nothing when it goes red again, so the run stops before
          any fault, and so does an entry whose `message` the clean run already prints (it
          can't tell the fault's failure from a pass). Then, for each fault: reset the tree,
          seed the fault, run the command, and print one line: `fired`, `DID NOT FIRE`, or
          `stale anchor`. After the last fault that builds what a command builds, reset the
          tree and run the command clean again, which must pass as the first time did (the
          restored pass, below). Anything else exits 1.

          `--jobs N` runs the same passes in N scratch worktrees at once. Each command's
          clean run goes to one worker, each fault starts on the worker that built its
          command clean, and a worker with nothing left takes a fault from the end of
          another's queue. With `--venv DIR` the bindings entries all run on the first worker,
          since they share that environment; with `--venv-per-worker` they spread like the
          others. Every worker runs each command it ran again, clean, so each tree is
          shown to come back. The lines print in registry order whatever order they finish
          in, so the output and the summary are the serial run's. Only the first worker
          builds in `--target-dir`; the others build in a target beside their worktree, and
          both are removed when the run ends, a signal included, with every command still
          running (each runs in its own process group, so its rustc and test processes go
          too). SIGKILL can't be caught: a killed run leaves its worktrees (`git worktree
          prune` forgets them) and the extra targets beside them in the temp directory.
          Each extra target starts, before any worker builds, as a copy of the first
          target's dependency units, so the workers don't each build every dependency: a
          dependency's artifact is the same in any tree. A local package's never is (it has
          its tree's path compiled in), so none is copied, however it got there. The copy
          takes the dev profile's units (`debug`) and a target triple's (`<triple>/debug`),
          where `napi build` builds, since it passes `--target`. Nothing is copied back: a
          unit another worker built was compiled against that worker's builds of its
          dependencies, and rustc refuses to mix it with the first target's (`can't find
          crate`), so the first target stays one worker's builds.

          The `lint-error` entries on one `cargo clippy` command that seed by transforms fire
          in one run of it. Each entry's transforms make the edits `seed` makes, on paper,
          and what the entry changes is the lines that differ. The entries whose lines don't
          overlap are seeded together (two that write lines at one place both go in, in
          registry order), and the command runs once with `--message-format=json`, which
          changes what cargo prints and not what it checks. An entry fired when an error in
          that run carries its `message` and every primary span of the error lies on the
          lines the entry's own fault wrote. That's the proof the entry's own run gives:
          clippy reports a banned call or path at that call or path, so the error is the
          entry's own text drawing the lint its `message` names, and an error is what makes
          the command exit non-zero (a warning proves nothing). The one way a batch can
          differ from the entry's own run is a lint that appears only because another fault
          in the batch is there: another entry's text supplies a name or an item the entry's
          text resolves through, so in the batch its text draws the lint and alone it
          wouldn't, or wouldn't compile. The span rule bounds that to the entry's own lines
          and its own `message`: another fault can change how the entry's text resolves, but
          it can't supply the error. The registry's clippy entries write fully qualified
          paths, so none resolves through another's. Whatever the batch can't read runs
          alone, for the entries it touches: an entry whose anchor doesn't match once, whose
          lines overlap an entry already in, or whose fault writes no line; every entry when
          the batch doesn't compile (an error whose code is rustc's, or that has none: rustc
          stops before the lints) or its output isn't JSON; and an entry whose `message` an
          error carries on no entry's lines, since that error may be its fault's lint landing
          where the batch can't say whose it is. An entry the batch doesn't prove runs alone
          and gets its own run's verdict, so the verdict lines, their order and each entry's
          `--logs` file are a run without batches' (a batched entry's log is the batch's run,
          printed as cargo prints it without JSON, under a line naming the batch). A line per
          batch after the verdicts says how many entries it proved and why each other one ran
          alone. Measured 2026-09-30 at 18645c2 with this script, on a loaded 16-core host
          with `--jobs 4`: the registry's lint entries took 120.8 s of faults one by one and
          3.4 s batched, with the same verdicts.

          The restored pass runs a build at a time, not after the last fault. A command's
          build is what `build_of` says it compiles or installs, which every command that
          builds the same shares (the CLI's unit tests are one build under many `--exact`
          filters). A worker restores a build, running each command of it that it ran again,
          clean, once no task that seeds a fault in that build is queued on any worker or
          running on any, and it takes a ready restore before its next fault. A fault moves
          between workers only while it's queued, and a batch's fallbacks are queued in the
          step that ends the batch, so once a build is ready no fault in it can start again:
          each worker that ran the build restores it once, after the last fault anywhere
          that builds it. That keeps what running every command after the last fault held.
          Every fault a worker runs is followed on that worker by its own command's clean
          run, which rebuilds from clean sources every unit the fault rebuilt, so worker 1's
          target ends on clean builds even where two builds share a unit. Each command runs
          clean after the last fault of its build, so no later fault rebuilds what it
          checked. And a reset removes ignored files too (`git clean -x`), so every run
          starts from the tree the worker started with, and a fault of another build can't
          leave anything behind for a command already restored. The restored line's seconds
          are what the restored pass adds after the last fault's verdict.

          `--affected` selects the entries whose own files changed: the entry changed or is
          new against the base's registry, or a file it names changed between the merge base of
          HEAD and `--base` (origin/main by default) and the working tree, untracked files and
          both sides of a rename included. The base's registry is read in either layout, its
          files in `guards/faults/` or the single `guards/faults.toml` it was before the split,
          and an entry is compared by its table, not by where it sits: one moved to another
          file unchanged isn't selected. The files are the ones it seeds (transform files, the
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
          the matrix. Each command weighs the sum of its entries' rough cost in a CI shard
          (`entry_cost`, in units that give a Rust entry 14: 24 for an entry that builds the
          CLI, 17 for a script one, 119 for a bindings entry that builds the Node addon and 53
          for another bindings one), and commands go heaviest first onto the lightest shard, a
          tie in weight to the command that comes first in the registry and a tie in load to
          the lower shard; a shard's entries keep registry order. A cost, not a count, because
          an entry's cost spans an order of magnitude and a command's entries sit together:
          one `napi build` entry costs about eight Rust ones. Modeled on the per-command seconds
          of run 36663996459, which fired every entry in three legs, these weights split it
          into about 12.8, 12.3 and 11.7 minutes of work a worker, where that run's own weights
          (the bindings entries at their one-worker cost, and the Rust and script ones from
          before #351's incremental builds) had given 16.3, 13.6 and 7.0. Commands stay whole
          because splitting one repeats its clean and restored runs in each shard that holds
          part of it. It prints which shard
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
          step.

          A bindings entry also builds into the environment it runs in (`maturin develop`
          installs into the active one), which no target dir separates. `--venv DIR` is one
          environment every bindings entry shares, the caller's, and the restored pass
          reinstalls the clean extension into it; a run stopped before that (a signal, a
          timeout) says so on stderr. `--venv-per-worker` gives each worker its own instead,
          made with `uv venv` beside its worktree and holding `VENV_PACKAGES` (pytest, pinned
          exactly), and removed with the worktree; each worker's restored pass reinstalls the
          clean extension into its own. It installs each package the entries' `npx -p`
          commands name once, before any worker starts, since npm's extraction into a cold
          cache collides when several npx calls install one package at once (#347), and the
          workers' first `napi build`s start together. CI's `guards` job passes it. With
          neither, `fire` skips the bindings entries.

  `build` (CI's `guards-cache` job, on a push to main) compiles what the selected entries'
          cargo commands compile (each command before `--`, with `--no-run` for a test run)
          into `--target-dir` and runs nothing, so the dependency cache the `guards` job
          restores holds every build its commands need rather than one leg's first worker's.
          A bindings entry's extension builds (each command before its last: `maturin
          develop`, `napi build`) run as written, once each, in a scratch worktree with an
          environment of its own, because they compile what no cargo command here does:
          microvms-py's dependencies under its own features, and microvms-js's whole graph
          under `napi build`'s `--target`. Without them a worker's first `napi build`
          compiled 235 crates, 90.8 s against 45.3 s with them, and its first `maturin
          develop` 35 crates, 22.1 s against 17.8 s (four pinned cores of a shared devbox,
          18645c2 plus this change, 2026-09-30).

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
                 one fired. The entries on one clippy command fire in a batch (`fire`).

The registry is every `guards/faults/*.toml`, one owner's entries in each (a gate's, a crate's,
or one issue's guards), and each file starts with a `# ── owner ──` line that says whose.
The loader reads the files in sorted order and each file's entries in its own order, which is
the registry order the output and the shards follow. It fails the registry when no file matches
(the floor), when a file doesn't parse or holds no `[[fault]]`, when two entries share an id,
in one file or in two (the message names both), and when `guards/faults.toml` exists: that's
the single file the registry was before it was split by owner, which nothing reads, so an entry
left there would stop firing without a word. check-agents-md.py and check-ci-parity.py read
the registry through the same loader (`registry_tables`). A new guard gets its entry in the
PR that adds it, in its owner's file (a new owner gets a new file), and `fire --only <id>`
must print `fired` for it. An entry goes when its guard is deleted on purpose, in the same PR.

An entry, in `guards/faults/<owner>.toml`:

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
artifact; `fire` refuses to run one without `--venv` or `--venv-per-worker`, because `maturin
develop` installs into whatever environment is active. One fault that several guards catch is
one entry per guard.

A `message` is matched after ANSI color codes are stripped, since CI sets
`CARGO_TERM_COLOR=always`.

A `message` names no pin the tree owns. A version or SHA bump, Dependabot's included, changes
what the command prints, and the entry then stops firing where only a full `fire` sees it while
`list` stays green: the action SHA in a step name that ci:parity prints is one such pin. So the
shape check refuses a `message` carrying a run of 40 or more hex digits (a commit SHA or a
digest), or an x.y.z version its fault doesn't write. A version is the fault's own when a
transform's `with` has it and that transform's `replace` doesn't, when a patch's added lines
have it and its removed lines don't, or when `argv_fault` has it; a version the anchor carries
into `with` unchanged is the tree's. Match the text around a pin, or the version the fault
seeds.

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
import dataclasses
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

# The registry: each owner's entries in a `*.toml` file of their own here, and the patches they
# seed beside them. A PR that adds a guard edits its owner's file, so it doesn't collide with
# every other open PR that adds one.
REGISTRY_DIR = "guards/faults"
REGISTRY = f"{REGISTRY_DIR}/*.toml"
# The single file the registry was before it was split by owner. Nothing reads it, so an entry
# a branch from before the split leaves there would stop firing silently: the loader refuses it.
FORMER_REGISTRY = f"{REGISTRY_DIR}.toml"
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
    # The registry file the entry is in.
    file: str | None = None


@dataclass(frozen=True)
class Table:
    """One `[[fault]]` table as its registry file holds it, before its shape is checked."""

    file: str
    number: int
    data: object


def runner(argv: list[str]) -> str | None:
    """Which test runner an argv drives, for `test-failed`."""
    if "pytest" in argv:
        return "pytest"
    if argv and Path(argv[0]).name == "node" and "--test" in argv:
        return "node"
    if argv and Path(argv[0]).name == "cargo" and "test" in argv:
        return "cargo"
    return None


def registry_file(path: str) -> bool:
    """Whether a repo-relative path is one of the registry's files."""
    return path.rpartition("/")[0] == REGISTRY_DIR and path.endswith(".toml")


def parse_registry(texts: dict[str, str]) -> tuple[list[Table], list[str]]:
    """The `[[fault]]` tables of the registry files in `texts` (path to text), in sorted file
    order, and what's wrong with the files: none at all (the floor), one that doesn't parse,
    and one that holds no entry."""
    if not texts:
        return [], [f"no file matches {REGISTRY}, so the registry has no entry"]
    tables: list[Table] = []
    problems: list[str] = []
    for name in sorted(texts):
        try:
            data = tomllib.loads(texts[name])
        except tomllib.TOMLDecodeError as error:
            problems.append(f"{name} doesn't parse: {error}")
            continue
        raw = data.get("fault")
        if not isinstance(raw, list) or not raw:
            problems.append(f"{name} has no [[fault]] entry")
            continue
        tables += [Table(name, number, entry) for number, entry in enumerate(raw, 1)]
    return tables, problems


def registry_tables(root: Path) -> tuple[list[Table], list[str]]:
    """The registry's tables in the tree at `root`, and what's wrong with its files, the
    former single file among them. check-agents-md.py and check-ci-parity.py read the
    registry through this, so no reader sees other entries than `list` and `fire` do."""
    texts = {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(root.glob(REGISTRY))
        if path.is_file()
    }
    tables, problems = parse_registry(texts)
    if (root / FORMER_REGISTRY).exists():
        problems.insert(
            0,
            f"{FORMER_REGISTRY} is the single file the registry was before it was split by "
            f"owner, and nothing reads it: move its entries into their owners' files in "
            f"{REGISTRY_DIR}/ and delete it",
        )
    return tables, problems


def load(root: Path) -> tuple[list[Fault], list[str]]:
    """The registry's entries, and what's wrong with its files and its entries' shapes."""
    tables, problems = registry_tables(root)
    faults: list[Fault] = []
    # Each id's file: an id is unique across the files, not only within one.
    seen: dict[str, str] = {}
    for table in tables:
        entry = table.data
        where = f"{table.file} entry {table.number}"
        if not isinstance(entry, dict):
            problems.append(f"{where} isn't a table")
            continue
        fid = entry.get("id")
        if isinstance(fid, str):
            where = f"{table.file} entry {fid!r}"
        bad = [f"{where}: {m}" for m in shape(entry, root)]
        if not bad and fid in seen:
            bad.append(f"{where}: the id is used twice, here and in {seen[fid]}")
        if bad:
            problems += bad
            continue
        seen[fid] = table.file
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
                file=table.file,
            )
        )
    return faults, problems


def _argv(value: object) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(a, str) and a for a in value)
    )


# What a pin bump changes in a command's output: a commit SHA or a digest, and an x.y.z version.
# A version is three parts exactly, so an address like 169.254.169.254 isn't one.
PINNED_HEX = re.compile(r"[0-9a-fA-F]{40,}")
VERSION = re.compile(r"(?<![\d.])\d+\.\d+\.\d+(?!\.?\d)")


def _versions(texts: list[str]) -> set[str]:
    return {v for text in texts for v in VERSION.findall(text)}


def seeded_versions(entry: dict, root: Path) -> set[str]:
    """The x.y.z versions an entry's fault writes: in a transform's `with` and not its
    `replace`, on a patch's added lines and not its removed ones, or in `argv_fault`.

    A version the anchor carries into `with` unchanged is still the tree's, and a bump moves
    it.
    """
    out: set[str] = set()
    transform = entry.get("transform")
    items = [transform] if isinstance(transform, dict) else transform
    for item in items if isinstance(items, list) else []:
        if (
            isinstance(item, dict)
            and isinstance(item.get("with"), str)
            and isinstance(item.get("replace"), str)
        ):
            out |= _versions([item["with"]]) - _versions([item["replace"]])
    argv = entry.get("argv_fault")
    if isinstance(argv, list):
        out |= _versions([a for a in argv if isinstance(a, str)])
    patch = entry.get("patch")
    if isinstance(patch, str) and patch:
        try:
            lines = (root / patch).read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            # `list` names a patch it can't read when it checks that the patch applies.
            lines = []
        added = [s[1:] for s in lines if s.startswith("+") and not s.startswith("+++")]
        removed = [
            s[1:] for s in lines if s.startswith("-") and not s.startswith("---")
        ]
        out |= _versions(added) - _versions(removed)
    return out


def shape(entry: dict, root: Path) -> list[str]:
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
    message = entry.get("message")
    if isinstance(message, str):
        for pinned in PINNED_HEX.findall(message):
            out.append(
                f"`message` carries {pinned}, a SHA or digest a pin bump changes: "
                "match the text around it"
            )
        tree = sorted(set(VERSION.findall(message)) - seeded_versions(entry, root))
        if tree:
            out.append(
                f"`message` carries {', '.join(tree)}, a version its fault doesn't write, "
                "and a bump changes it: match the text around it or the version the fault "
                "seeds"
            )
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
                "register its fault in its owner's file there. New notes can't join the "
                "unregistered list"
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
                # `copy`, not `copy2`: the copy's mtime is now. A kept old mtime can be older
                # than an artifact a previous run built from other text at the same relative
                # path, and cargo would take that artifact as fresh.
                shutil.copy(source, target, follow_symlinks=False)
            elif target.exists() or target.is_symlink():
                target.unlink()
        git(self.path, "add", "-A")
        return len(paths)

    def reset(self) -> None:
        git(self.path, "checkout", "--quiet", "--", ".")
        # `-x` too: runs write ignored files into the tree (napi's addon and loader,
        # `__pycache__`), faulted runs among them, and the restored pass checks a build while
        # other builds' faults still run on the same tree. Removed every time, nothing a run
        # left can reach a command already restored. The tree starts with none.
        git(self.path, "clean", "-fdqx")

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


def build_of(key: tuple) -> tuple:
    """What a command builds, which it shares with every command that builds the same: in the
    command's suite, for each cargo argv what `build_argv` compiles (the argv before `--` when
    it can't say), and each other argv but the last, the step a test run follows (`maturin
    develop`, `napi build`). A command with neither builds for itself alone. The restored
    pass runs by it: see the module docstring."""
    suite, run = key
    parts: list[tuple[str, ...]] = []
    for index, argv in enumerate(run):
        if Path(argv[0]).name == "cargo":
            head = argv[: argv.index("--")] if "--" in argv else argv
            parts.append(tuple(build_argv(list(argv)) or head))
        elif index < len(run) - 1:
            parts.append(tuple(argv))
    return (suite, tuple(parts)) if parts else key


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
    pinned: bool  # bindings sharing `--venv` stay on worker 1, which holds it
    # What a task that seeds faults builds (`build_of`); None for one that seeds none.
    build: tuple | None = None


# What `Board.next` answers when a worker has nothing to take yet but may have soon.
WAIT = object()


class Board:
    """One phase's work: a queue per worker, and the results the main thread prints in order.

    A worker takes from the front of its own queue, then from the back of the longest other
    queue, so a worker whose commands build fast doesn't sit idle. A worker's queue starts with
    the commands it already built, and a pinned task stays where it is.

    In the fault phase, `restores` holds the builds each worker owes a restored run, and a
    worker takes a ready one before anything else: a build it has run, with no task that seeds
    a fault in that build queued on any worker or running on any. Taking such a task adds its
    build to what the worker owes, and a batch's fallbacks are queued in the same step that
    ends the batch, so once a build is ready no fault in it can start again and each worker
    restores it once, after the last one. A worker with nothing ready and nothing to take waits
    while it owes a build or any fault is running, since a running batch can queue more.
    """

    def __init__(
        self,
        queues: list[list[Task]],
        steal: bool,
        restores: list[dict[tuple, None]] | None = None,
    ) -> None:
        self.queues = [list(q) for q in queues]
        self.steal = steal
        self.cond = threading.Condition()
        self.done: dict[object, list] = {}
        self.error: BaseException | None = None
        self.restores = restores
        self.running: dict[tuple, int] = {}
        # The workers still taking tasks. With none left, a result not yet put never will be.
        self.left = len(self.queues)

    def ready(self, number: int) -> tuple | None:
        """A build worker `number` can restore now, if any."""
        for build in self.restores[number] if self.restores is not None else ():
            queued = any(task.build == build for queue in self.queues for task in queue)
            if not queued and not self.running.get(build):
                return build
        return None

    def next(self, number: int) -> Task | object | None:
        """The task worker `number` takes now, `WAIT`, or None when nothing is left for it.
        Called with the lock held."""
        build = self.ready(number)
        if build is not None:
            del self.restores[number][build]
            return Task(("restore", build), True)
        task = self.pop(number)
        if task is not None:
            if task.build is not None:
                self.running[task.build] = self.running.get(task.build, 0) + 1
                if self.restores is not None:
                    self.restores[number].setdefault(task.build)
            return task
        owed = self.restores is not None and bool(self.restores[number])
        return WAIT if owed or any(self.running.values()) else None

    def pop(self, number: int) -> Task | None:
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

    def take(self, number: int) -> Task | None:
        """The next task for worker `number`, waiting while `next` says to. None when there's
        nothing left for it, or the run is ending. Wakes each second, as `get` does."""
        with self.cond:
            while self.error is None:
                task = self.next(number)
                if task is not WAIT:
                    return task
                self.cond.wait(timeout=1)
            return None

    def finish(
        self,
        number: int,
        task: Task,
        results: list[tuple[object, object]],
        follow: list[Task],
    ) -> None:
        """Put `task`'s results, queue the tasks it leaves at the front of worker `number`'s
        queue, and count it as no longer running, all in one step, so no worker sees its build
        as done in between."""
        with self.cond:
            for key, value in results:
                self.done.setdefault(key, []).append(value)
            self.queues[number][:0] = follow
            if task.build is not None:
                self.running[task.build] -= 1
            self.cond.notify_all()

    def fail(self, error: BaseException) -> None:
        with self.cond:
            if self.error is None:
                self.error = error
            self.cond.notify_all()

    def leave(self) -> None:
        with self.cond:
            self.left -= 1
            self.cond.notify_all()

    def get(self, key: object, count: int = 1) -> list:
        """The results for `key` once `count` workers have put one. Wakes each second, so a
        signal reaches the main thread while it waits. Raises when every worker has stopped
        short of `count`, a bug in this script, so the run fails rather than hangs."""
        with self.cond:
            while len(self.done.get(key, [])) < count:
                if self.error is not None:
                    raise self.error
                if self.left == 0:
                    have = len(self.done.get(key, []))
                    raise RuntimeError(
                        f"guards: every worker stopped with {have} of {count} results "
                        f"for {key!r}"
                    )
                self.cond.wait(timeout=1)
            return self.done[key]


def start(workers: list[Worker], board: Board, do) -> list[threading.Thread]:
    """Run `do` over the board's tasks, one thread a worker. `do` answers the results to put,
    each a key and a value, and the tasks the worker queues next (a batch's fallbacks)."""

    def loop(worker: Worker) -> None:
        try:
            while (task := board.take(worker.number - 1)) is not None:
                results, follow = do(worker, task)
                board.finish(worker.number - 1, task, results, follow)
        except Stopped:
            board.fail(Stopped())
        # A worker's crash ends the run, not just its thread: the main thread raises it.
        except BaseException as error:  # noqa: BLE001 - handed on, not swallowed
            board.fail(error)
        finally:
            board.leave()

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
    """An entry's cost in a CI shard, roughly, for the split over shards, in units that give a
    Rust entry (a cargo build) 14: an entry that builds the CLI (its argv names microvms-cli)
    24, a script entry 17, a bindings entry that builds the Node addon (`napi build`) 119, and
    another bindings entry 53. They're the mean seconds a fault took in shards 1 and 2 of run
    36663996459, four workers to a runner (shard 0 held the costliest napi command, and its
    contention slowed every kind there): Rust 3.39 s, CLI 5.74 s, script 4.18 s, `napi build`
    28.87 s, and `maturin develop` or the stub check 12.8 s, scaled so a Rust entry keeps its
    14. Main's push at be99c5d, with no bindings entries in its shards, put a CLI entry at 1.9
    Rust ones and a script entry at 1.5. The module docstring's `--shard` says why a cost and
    not a count."""
    if fault.suite == "bindings":
        return 119 if any("napi" in argv for argv in fault.run) else 53
    if any("microvms-cli" in arg for argv in fault.run for arg in argv):
        return 24
    return 14 if fault.suite == "rust" else 17


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


def assign(selected: list[Fault], jobs: int, pin: bool) -> list[list[tuple]]:
    """Each worker's commands for the clean pass, heaviest first onto the lightest worker.

    With `pin`, bindings commands go to worker 1, which holds the one `--venv`; each worker
    with an environment of its own takes them like any other. Within a worker, commands keep
    the registry's order, so the main thread's in-order printing waits as little as it can.
    """
    weight = command_weights(selected)
    order = {key: index for index, key in enumerate(weight)}
    pinned = [k for k in weight if pin and k[0] == "bindings"]
    load = [sum(weight[k] for k in pinned)] + [0] * (jobs - 1)
    queues = spread({k: w for k, w in weight.items() if k not in pinned}, jobs, load)
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


# ── lint batches ─────────────────────────────────────────────────────────────


def batchable(fault: Fault) -> bool:
    """Whether a lint entry can share a run with the others on its command: it's seeded by
    transforms, and its one argv is a `cargo clippy` that sets no message format of its own."""
    if fault.expect != "lint-error" or not fault.transforms or len(fault.run) != 1:
        return False
    argv = fault.run[0]
    head = argv[: argv.index("--")] if "--" in argv else argv
    sub = next((a for a in head[1:] if not a.startswith(("-", "+"))), None)
    return (
        Path(argv[0]).name == "cargo"
        and sub == "clippy"
        and not any(a.startswith("--message-format") for a in head)
    )


def lint_batches(selected: list[Fault]) -> dict[tuple, list[int]]:
    """Each command's batchable entries, by position in `selected`, where there are two or
    more."""
    groups: dict[tuple, list[int]] = {}
    for index, fault in enumerate(selected):
        if batchable(fault):
            groups.setdefault(command_key(fault), []).append(index)
    return {key: members for key, members in groups.items() if len(members) > 1}


def json_argv(argv: list[str]) -> list[str]:
    """`argv` with cargo's `--message-format=json`, ahead of the `--` that starts clippy's own
    flags. It changes what cargo prints, not what it checks or whether a crate is fresh."""
    at = argv.index("--") if "--" in argv else len(argv)
    return [*argv[:at], "--message-format=json", *argv[at:]]


def changed_lines(before: str, after: str) -> tuple[int, int, list[str]]:
    """The lines `[start, end)` of `before`, from 0, that `after` replaces, and the lines it
    puts there: what's left once the lines the two share at each end are set aside."""
    old = before.splitlines(keepends=True)
    new = after.splitlines(keepends=True)
    head = 0
    while head < min(len(old), len(new)) and old[head] == new[head]:
        head += 1
    tail = 0
    while tail < min(len(old), len(new)) - head and old[-1 - tail] == new[-1 - tail]:
        tail += 1
    return head, len(old) - tail, new[head : len(new) - tail]


@dataclass
class Plan:
    """A batch's seeding: each file's text with every batched entry's lines in, the lines each
    batched entry wrote (its region: file, first and last line, from 1), and why each entry
    that isn't batched runs alone. Both keyed by position in the batch."""

    texts: dict[str, str]
    regions: dict[int, list[tuple[str, int, int]]]
    alone: dict[int, str]

    def write(self, tree: Path) -> None:
        for name, text in self.texts.items():
            (tree / name).write_text(text, encoding="utf-8")


def plan_batch(tree: Path, faults: list[Fault]) -> Plan:
    """Seed `faults` together, on paper. Each entry's transforms make the edits `seed` makes, and
    what the entry changes in a file is the lines that differ (`changed_lines`). Entries join in
    order while no two change overlapping lines; two that write lines at one place both join,
    in registry order. An entry runs alone when its anchor doesn't match once, when its lines
    overlap an entry that joined, or when it writes no line for a lint to land on."""
    originals: dict[str, str] = {}
    taken: list[tuple[int, str, int, int, list[str]]] = []
    alone: dict[int, str] = {}
    for position, fault in enumerate(faults):
        why = seed(tree, fault, dry=True)
        if why:
            alone[position] = why
            continue
        edited: dict[str, str] = {}
        for item in fault.transforms:
            name = os.path.normpath(item["file"])
            if name not in originals:
                originals[name] = (tree / name).read_text(encoding="utf-8")
            text = edited.get(name, originals[name])
            edited[name] = text.replace(item["replace"], item["with"])
        hunks = [
            (name, *changed_lines(originals[name], text))
            for name, text in edited.items()
        ]
        if not any(lines for _, _, _, lines in hunks):
            alone[position] = "its fault writes no line for its lint to land on"
            continue
        clash = next(
            (
                faults[other].id
                for other, file, start, end, _ in taken
                for name, first, last, _ in hunks
                if file == name and first < end and start < last
            ),
            None,
        )
        if clash is not None:
            alone[position] = f"it changes lines {clash} changes too"
            continue
        taken += [(position, *hunk) for hunk in hunks]
    texts: dict[str, str] = {}
    regions: dict[int, list[tuple[str, int, int]]] = {}
    for name in dict.fromkeys(file for _, file, _, _, _ in taken):
        lines = originals[name].splitlines(keepends=True)
        out: list[str] = []
        at = 0
        for position, _, start, end, new in sorted(
            (hunk for hunk in taken if hunk[1] == name),
            key=lambda hunk: (hunk[2], hunk[3], hunk[0]),
        ):
            out += lines[at:start]
            if new:
                regions.setdefault(position, []).append(
                    (name, len(out) + 1, len(out) + len(new))
                )
            out += new
            at = end
        texts[name] = "".join(out + lines[at:])
    return Plan(texts, regions, alone)


def span_at(span: dict, tree: Path) -> tuple[str, int, int]:
    """A diagnostic span's file, relative to the tree when it's inside, and its lines."""
    name = str(span.get("file_name") or "")
    if os.path.isabs(name):
        try:
            name = str(Path(os.path.realpath(name)).relative_to(os.path.realpath(tree)))
        except ValueError:
            pass
    return (
        os.path.normpath(name),
        int(span.get("line_start") or 0),
        int(span.get("line_end") or 0),
    )


def attribute(
    said: str,
    code: int,
    regions: dict[int, list[tuple[str, int, int]]],
    messages: list[str | None],
    tree: Path,
) -> tuple[dict[int, str], dict[int, str]]:
    """Which entries of a batch its run proves fired, each with where its error is, and why
    each other entry in `regions` runs alone.

    An entry is proven by an error (a diagnostic at level `error`, what makes the command exit
    non-zero) whose rendered text carries the entry's `message` and whose every primary span
    lies in that entry's region. An error carrying an entry's `message` that lies in no one
    entry's region leaves that entry to run alone even when another error proves it, since the
    batch can't say whose fault that one is. An error in another entry's region proves only
    that entry. A run that passed, a line of output that isn't JSON, and a compile error (an
    error whose code is a rustc code, or none) prove nothing: rustc stops before the lints."""
    positions = sorted(regions)
    if code == 0:
        return {}, dict.fromkeys(positions, "the batch's run passed")
    proven: dict[int, str] = {}
    tainted: dict[int, str] = {}
    for line in said.splitlines():
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            return {}, dict.fromkeys(
                positions, f"a line of the batch's output isn't JSON: {line[:80]}"
            )
        if not isinstance(record, dict) or record.get("reason") != "compiler-message":
            continue
        message = record.get("message") or {}
        level = str(message.get("level") or "")
        lint = (message.get("code") or {}).get("code")
        spans = [
            span_at(s, tree) for s in message.get("spans") or [] if s.get("is_primary")
        ]
        at = ", ".join(f"{file}:{first}" for file, first, _ in spans) or "no line"
        if level.startswith("error") and (lint is None or CODE.match(str(lint))):
            why = "the batch doesn't compile: error" + (f"[{lint}]" if lint else "")
            return {}, dict.fromkeys(positions, f"{why} at {at}")
        if level != "error":
            continue
        rendered = ANSI.sub("", str(message.get("rendered") or message.get("message")))
        owner = next(
            (
                position
                for position in positions
                if spans
                and all(
                    any(
                        file == name and start <= first and last <= end
                        for name, start, end in regions[position]
                    )
                    for file, first, last in spans
                )
            ),
            None,
        )
        for position in positions:
            if not messages[position] or messages[position] not in rendered:
                continue
            if owner == position:
                proven.setdefault(position, at)
            elif owner is None:
                tainted.setdefault(position, at)
    unproven = {
        position: (
            f"an error carrying its message is on no entry's lines ({tainted[position]})"
            if position in tainted
            else "no error carrying its message is on its own lines"
        )
        for position in positions
        if position not in proven or position in tainted
    }
    return {p: at for p, at in proven.items() if p not in tainted}, unproven


def rendered_log(log: str) -> str:
    """A batch's log as cargo prints it without `--message-format=json`: each diagnostic's
    rendered text in place of its record, and the build records dropped."""
    out: list[str] = []
    for line in log.splitlines(keepends=True):
        if line.startswith("{"):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                record = None
            if isinstance(record, dict):
                if record.get("reason") == "compiler-message":
                    out.append(str((record.get("message") or {}).get("rendered") or ""))
                continue
        out.append(line)
    return "".join(out)


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
    """The registry's entries at `commit`, by id, in either layout: its files in REGISTRY_DIR,
    or FORMER_REGISTRY, the single file it was before the split. A base on either side of the
    split compares the same way, so the first PR after it doesn't read every entry as new.
    Empty when `commit` has no registry; a file that doesn't parse gives up no entry."""
    listed = git(root, "ls-tree", "-z", "--name-only", commit, "--", f"{REGISTRY_DIR}/")
    names = [n for n in listed.stdout.split("\0") if registry_file(n)]
    former = git(root, "cat-file", "-e", f"{commit}:{FORMER_REGISTRY}", check=False)
    if former.returncode == 0:
        names.append(FORMER_REGISTRY)
    texts = {n: git(root, "show", f"{commit}:{n}").stdout for n in names}
    tables, _ = parse_registry(texts)
    return {
        t.data["id"]: t.data
        for t in tables
        if isinstance(t.data, dict) and isinstance(t.data.get("id"), str)
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
    tables, _ = registry_tables(root)
    raw = {t.data["id"]: t.data for t in tables if isinstance(t.data, dict)}
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
            reasons[fault.id] = f"the entry is new in {fault.file}"
        elif before[fault.id] != raw[fault.id]:
            reasons[fault.id] = f"the entry changed in {fault.file}"
        else:
            hit = sorted(fault_files(root, fault, files, crates) & changed)
            if hit:
                reasons[fault.id] = f"{hit[0]} changed"
    return reasons, commit, len(changed)


# ── warm workers ─────────────────────────────────────────────────────────────

# What a worker's target shares with another worktree's: the dev profile's unit directories.
# The rest of a target is a workspace member's (the outputs cargo copies up beside `deps/`,
# `incremental/`) or isn't something a fault's command reads (`doc/`, other profiles).
UNIT_DIRS = ("deps", "build", ".fingerprint")
# A unit's file or directory name: `<name>-<16 hex>`, plus an extension in `deps/`.
UNIT_NAME = re.compile(r"^(.+)-[0-9a-f]{16}(?:\.[^/]*)?$")
# A target triple (`x86_64-unknown-linux-gnu`): a build that passes `--target`, as `napi build`
# does, keeps its dev profile in `<triple>/debug` rather than `debug`. Three parts or more, so
# a directory like `guards-fire` (another target, nested in the caller's) isn't read as one.
TRIPLE = re.compile(r"^[a-z0-9_]+(?:-[a-z0-9_.]+){2,}$")


def local_packages(tree: Path) -> set[str] | None:
    """Every name a local package's units can carry in a target: each workspace member's
    package name and target names, and each path dependency's, dashes and underscores both.
    None when `cargo metadata` can't say, and then no worker starts from a copy."""
    try:
        out = subprocess.run(
            ["cargo", "metadata", "--no-deps", "--format-version", "1"],
            cwd=tree,
            capture_output=True,
            text=True,
            env=clean_env(),
            timeout=300,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    try:
        packages = json.loads(out.stdout).get("packages")
    except (json.JSONDecodeError, AttributeError):
        return None
    if not isinstance(packages, list) or not packages:
        return None
    names: set[str] = set()
    for package in packages:
        names.add(package["name"])
        names.update(t["name"] for t in package.get("targets", []))
        for dep in package.get("dependencies", []):
            if dep.get("path"):
                names.add(dep["name"])
                if dep.get("rename"):
                    names.add(dep["rename"])
    return (
        names
        | {n.replace("-", "_") for n in names}
        | {n.replace("_", "-") for n in names}
    )


def dependency_unit(name: str, local: set[str]) -> bool:
    """Whether a unit directory's entry is a dependency's, so safe to share between trees.

    A local package's unit is never shared: cargo keys it by its workspace-relative path and
    trusts it while its sources are older, and it has the absolute path of the tree it was
    built in compiled in (`env!("CARGO_MANIFEST_DIR")`, which guards read their sources
    through). Copied into a tree whose files are older, it stays fresh and reads the other
    tree. An entry whose name doesn't parse isn't shared either. `lib` may or may not be the
    crate's own prefix (`liblibc-*.rlib` and `libc-*.d` are one crate), so both readings are
    checked, and a match on either keeps the entry out.
    """
    match = UNIT_NAME.match(name)
    if match is None:
        return False
    stem = match.group(1)
    readings = {stem, stem[3:]} if stem.startswith("lib") else {stem}
    return not readings & local


def profile_dirs(target: Path) -> list[Path]:
    """`target`'s dev profile directories, relative to it: `debug`, and `<triple>/debug` for
    each triple a build named. A worker's first `napi build` compiles its whole graph under the
    triple, so an extra worker that got only `debug` would build all of it again (the module
    docstring's `build` has what that costs)."""
    if not target.is_dir():
        return []
    triples = [
        Path(entry.name, "debug")
        for entry in sorted(target.iterdir(), key=lambda e: e.name)
        if TRIPLE.match(entry.name) and (entry / "debug").is_dir()
    ]
    return ([Path("debug")] if (target / "debug").is_dir() else []) + triples


def dependency_units(target: Path, local: set[str]) -> list[Path]:
    """The dependency units in `target`'s dev profiles, relative to `target`."""
    out: list[Path] = []
    for profile in profile_dirs(target):
        for kind in UNIT_DIRS:
            base = target / profile / kind
            if base.is_dir():
                out += [
                    profile / kind / entry.name
                    for entry in sorted(base.iterdir(), key=lambda e: e.name)
                    if dependency_unit(entry.name, local)
                ]
    return out


def copy_units(source: Path, dest: Path, units: list[Path]) -> int:
    """Copy each unit `dest` doesn't have from `source`, keeping mtimes (cargo compares
    them); the count copied."""
    copied = 0
    for unit in units:
        src, dst = source / unit, dest / unit
        if dst.exists() or dst.is_symlink():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir() and not src.is_symlink():
            shutil.copytree(src, dst, symlinks=True)
        else:
            shutil.copy2(src, dst, follow_symlinks=False)
        copied += 1
    for name in (".rustc_info.json", "CACHEDIR.TAG"):
        if (source / name).is_file() and not (dest / name).exists():
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / name, dest / name)
    return copied


def seed_targets(first: Path, others: list[Path], local: set[str] | None) -> str:
    """Give each extra worker's empty target the first target's dependency units, before any
    worker builds, so four workers don't each build every dependency. The line to print."""
    if not others:
        return ""
    if local is None:
        return (
            "guards: cargo metadata didn't answer, so the other workers start from empty "
            "targets"
        )
    started = time.monotonic()
    units = dependency_units(first, local)
    for target in others:
        copy_units(first, target, units)
    return (
        f"guards: the other {len(others)} workers start from the first target's "
        f"{len(units)} dependency units, without the workspace's own "
        f"({time.monotonic() - started:.1f} s)"
    )


# ── bindings environments ────────────────────────────────────────────────────

# What a bindings environment this script makes holds, pinned exactly: the Python tests' one
# dependency besides the extension, which `maturin develop` installs. maturin and napi's CLI
# arrive through `uvx` and `npx`, pinned in the entries' own argvs.
VENV_PACKAGES = ("pytest==9.1.1",)


def make_venv(path: Path, env: dict[str, str]) -> None:
    """A fresh environment at `path` holding VENV_PACKAGES, through uv. Run from `path`'s
    parent, so uv reads no project the directory it was called from happens to have."""
    for argv in (
        ["uv", "venv", "--quiet", str(path)],
        [
            "uv",
            "pip",
            "install",
            "--quiet",
            "--python",
            str(path / "bin" / "python"),
            *VENV_PACKAGES,
        ],
    ):
        try:
            done = subprocess.run(
                argv, cwd=path.parent, env=env, capture_output=True, text=True
            )
        except FileNotFoundError:
            raise SystemExit("guards: uv isn't on PATH, so no bindings environment")
        if done.returncode != 0:
            raise SystemExit(
                f"guards: `{' '.join(argv)}` exits {done.returncode}\n"
                f"{tail(done.stdout + done.stderr)}"
            )


def activated(env: dict[str, str], venv: Path) -> dict[str, str]:
    """`env` with `venv` active, as its `bin/activate` would leave it."""
    out = dict(env)
    out["VIRTUAL_ENV"] = str(venv)
    out["PATH"] = os.pathsep.join([str(venv / "bin"), env.get("PATH", "")])
    return out


def npx_packages(faults: list[Fault]) -> list[str]:
    """Each package spec the entries' `npx -p` (or `--package`) commands install, in
    registry order. Only npx's own options count, the ones before the command it runs: the
    `--package microvms-js` after `napi build` is napi's."""
    out: dict[str, None] = {}
    for fault in faults:
        for argv in fault.run:
            if not argv or Path(argv[0]).name != "npx":
                continue
            rest = iter(argv[1:])
            for arg in rest:
                if arg in ("-p", "--package"):
                    out.setdefault(next(rest, ""))
                elif arg.startswith("--package="):
                    out.setdefault(arg.split("=", 1)[1])
                elif arg in ("-c", "--call"):
                    next(rest, None)
                elif arg == "--" or not arg.startswith("-"):
                    break
    out.pop("", None)
    return list(out)


def fetch_npx(specs: list[str], cwd: Path, env: dict[str, str]) -> str:
    """Install each npx package into npm's cache, one at a time, and run nothing from it; the
    line to print. The module docstring's `--venv-per-worker` says why before the workers."""
    started = time.monotonic()
    for spec in specs:
        argv = ["npx", "--yes", "--package", spec, "--", "node", "-e", "0"]
        try:
            done = subprocess.run(
                argv, cwd=cwd, env=env, capture_output=True, text=True
            )
        except FileNotFoundError:
            raise SystemExit(
                "guards: npx isn't on PATH, so no bindings entry can build"
            )
        if done.returncode != 0:
            raise SystemExit(
                f"guards: `{' '.join(argv)}` exits {done.returncode}\n"
                f"{tail(done.stdout + done.stderr)}"
            )
    return (
        f"guards: installed {', '.join(specs)} for npx once, before the workers "
        f"({time.monotonic() - started:.1f} s)"
    )


def extension_builds(faults: list[Fault]) -> list[list[str]]:
    """The bindings entries' extension builds, once each in registry order: every command
    before an entry's last, which is its test run. An entry with one command builds inside its
    own check and has none to run alone."""
    out: dict[tuple[str, ...], None] = {}
    for fault in faults:
        if fault.suite == "bindings":
            for argv in fault.run[:-1]:
                out.setdefault(tuple(argv))
    return [list(argv) for argv in out]


def fire_target(root: Path, env: dict[str, str], given: str | None) -> str:
    """Where cargo builds: `--target-dir`, or `guards-fire` under the caller's target."""
    if given:
        return str(Path(given).resolve())
    return str(
        Path(env.get("CARGO_TARGET_DIR") or root / "target").resolve() / "guards-fire"
    )


def build_argv(argv: list[str]) -> list[str] | None:
    """What a cargo command compiles, as a command that only compiles it: the argv before
    `--`, with `--no-run` for `test` and `build` for `run`. None for any other program, and for
    a doc test run, which cargo can't build without running (rustdoc compiles each doc test as
    it runs it, against the library's normal build that other commands compile)."""
    if not argv or Path(argv[0]).name != "cargo":
        return None
    head = argv[: argv.index("--")] if "--" in argv else list(argv)
    at = next(
        (i for i, a in enumerate(head[1:], 1) if not a.startswith(("-", "+"))), None
    )
    if at is None:
        return None
    if head[at] == "test" and "--doc" in head:
        return None
    if head[at] == "test" and "--no-run" not in head:
        head.append("--no-run")
    elif head[at] == "run":
        head[at] = "build"
    return head


def cmd_build(root: Path, args: argparse.Namespace) -> int:
    faults, problems = load(root)
    if problems:
        for problem in problems:
            print(f"guards: {problem}", file=sys.stderr)
        return 1
    selected = [
        f
        for f in faults
        if (not args.only or f.id in args.only)
        and (not args.suite or f.suite in args.suite)
    ]
    builds: dict[tuple[str, ...], None] = {}
    for fault in selected:
        for argv in fault.run:
            if (built := build_argv(argv)) is not None:
                builds.setdefault(tuple(built))
    extensions = extension_builds(selected)
    count = len(builds) + len(extensions)
    if not count:
        print(
            "guards: no selected entry runs cargo or builds an extension, so nothing to "
            "build",
            file=sys.stderr,
        )
        return 1
    env = clean_env()
    env["CARGO_TARGET_DIR"] = fire_target(root, env, args.target_dir)
    env.pop("VIRTUAL_ENV", None)
    print(
        f"guards: building what {len(selected)} entries' commands compile, "
        f"{count} builds, CARGO_TARGET_DIR={env['CARGO_TARGET_DIR']}"
    )

    def built(argv: list[str], cwd: Path, benv: dict[str, str]) -> bool:
        started = time.monotonic()
        print(f"$ {' '.join(argv)}")
        code = subprocess.run(argv, cwd=cwd, env=benv).returncode
        if code != 0:
            print(f"guards: `{' '.join(argv)}` exits {code}", file=sys.stderr)
            return False
        print(f"guards: built in {time.monotonic() - started:.1f} s")
        return True

    def stop(signum: int, _frame: object) -> None:
        raise SystemExit(128 + signum)

    # So a signal still removes the scratch tree below.
    signal.signal(signal.SIGTERM, stop)
    total = time.monotonic()
    for argv in builds:
        if not built(list(argv), root, env):
            return 1
    if extensions:
        # A scratch tree, since `napi build` writes the addon and its declarations into the
        # tree it runs in, and an environment of the build's own for `maturin develop` to
        # install into. The dependencies compile the same from any tree.
        tree = Tree.make(root)
        try:
            venv = tree.scratch / "venv"
            make_venv(venv, env)
            for argv in extensions:
                if not built(argv, tree.path, activated(env, venv)):
                    return 1
        finally:
            tree.remove()
    print(f"guards: {count} builds ({time.monotonic() - total:.1f} s)")
    return 0


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
    if venv is None and not args.venv_per_worker:
        bindings = [f for f in selected if f.suite == "bindings"]
        if bindings and (args.only or args.suite):
            print(
                "guards: bindings entries rebuild the extension into the active "
                "environment; pass --venv-per-worker, or --venv DIR",
                file=sys.stderr,
            )
            return 1
        if bindings:
            print(
                f"guards: skipping {len(bindings)} bindings entries; they run with "
                "`--venv-per-worker` (CI's guards job) or `--venv DIR`"
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
    env["CARGO_TARGET_DIR"] = fire_target(root, env, args.target_dir)
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
    boards: list[Board] = []
    jobs = min(args.jobs, len(selected))
    # One shared `--venv` keeps the bindings entries on worker 1; environments of the
    # workers' own let them go anywhere.
    pin = venv is not None
    own_venvs = args.venv_per_worker and any(f.suite == "bindings" for f in selected)
    try:
        making = 0.0
        for number in range(1, jobs + 1):
            tree = Tree.make(root)
            wenv = dict(env)
            if number > 1:
                # Its own target, removed with its tree: cargo's build lock would otherwise
                # queue every worker behind one build.
                wenv["CARGO_TARGET_DIR"] = str(tree.scratch / "target")
            worker = Worker(number, tree, wenv, dict(wenv))
            # Listed before its environment is made, so `finally` removes the tree if uv
            # fails.
            workers.append(worker)
            if venv is not None:
                worker.binding_env = activated(wenv, venv)
            elif own_venvs:
                # Beside the worktree rather than in it, so the reset between faults leaves
                # it alone; removed with the worktree.
                own = tree.scratch / "venv"
                started = time.monotonic()
                make_venv(own, env)
                making += time.monotonic() - started
                worker.binding_env = activated(wenv, own)
        first = workers[0].tree
        first_target = Path(env["CARGO_TARGET_DIR"])
        extra_targets = [Path(w.env["CARGO_TARGET_DIR"]) for w in workers[1:]]
        # Nothing to copy from a first target no build has used (a cold cache).
        warm = bool(extra_targets) and bool(profile_dirs(first_target))
        local = local_packages(first.path) if warm else None
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
        if own_venvs:
            print(
                f"guards: each worker's bindings entries build into and test from its own "
                f"environment beside its worktree, holding {', '.join(VENV_PACKAGES)}, "
                f"removed when the run ends ({making:.1f} s)"
            )
            if specs := npx_packages(selected):
                print(fetch_npx(specs, first.path, env))
        if warm:
            # Before the first build of this run: see `dependency_unit` for why only a
            # dependency's units are copied.
            print(seed_targets(first_target, extra_targets, local))

        def clean(worker: Worker, key: tuple) -> tuple:
            fault = by_key[key]
            started = time.monotonic()
            fenv = worker.binding_env if fault.suite == "bindings" else worker.env
            worker.touched.setdefault(key)
            code, output, said = run_commands(
                fault, worker.tree.path, fenv, args.timeout, False, procs
            )
            return code, output, said, time.monotonic() - started, worker.number

        def run_clean(worker: Worker, task: Task):
            return [(task.key, clean(worker, task.key))], []

        def phase(
            queues: list[list[Task]],
            steal: bool,
            do,
            restores: list[dict[tuple, None]] | None = None,
        ) -> Board:
            board = Board(queues, steal, restores)
            boards.append(board)
            threads.extend(start(workers, board, do))
            return board

        def join() -> None:
            while threads:
                threads.pop().join()

        by_key: dict[tuple, Fault] = {}
        for fault in selected:
            by_key.setdefault(command_key(fault), fault)
        clean_queues = [
            [Task(k, pin and k[0] == "bindings") for k in q]
            for q in assign(selected, jobs, pin)
        ]
        counts = dict.fromkeys(by_key, 1)
        board = phase(clean_queues, True, run_clean)
        ok = check_pass(selected, board, counts, log, "clean")
        join()
        if not ok:
            return 1
        # Each fault starts on the worker that built its command clean.
        owner = {k: n for n, w in enumerate(workers) for k in w.touched}
        # A command's lint batch takes its first entry's place: see the module docstring.
        batches = lint_batches(selected)
        batched = {index for members in batches.values() for index in members}
        fault_queues: list[list[Task]] = [[] for _ in workers]
        for index, fault in enumerate(selected):
            key = command_key(fault)
            if index not in batched:
                fault_queues[owner[key]].append(
                    Task(index, pin and fault.suite == "bindings", build_of(key))
                )
            elif batches[key][0] == index:
                fault_queues[owner[key]].append(
                    Task(
                        ("batch", key), pin and fault.suite == "bindings", build_of(key)
                    )
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

        def run_batch(worker: Worker, task: Task):
            """Seed a command's lint batch, run the command once, and put a verdict for each
            entry the run proves; the others go back on this worker's queue to run alone."""
            key = task.key[1]
            members = batches[key]
            faults = [selected[index] for index in members]
            worker.touched.setdefault(key)
            worker.tree.reset()
            plan = plan_batch(worker.tree.path, faults)
            alone = dict(plan.alone)
            proven: dict[int, str] = {}
            elapsed = 0.0
            ran = len(plan.regions) > 1
            if ran:
                plan.write(worker.tree.path)
                first = faults[0]
                fenv = worker.binding_env if first.suite == "bindings" else worker.env
                started = time.monotonic()
                code, output, said = run_commands(
                    dataclasses.replace(first, run=[json_argv(first.run[0])]),
                    worker.tree.path,
                    fenv,
                    args.timeout,
                    True,
                    procs,
                )
                elapsed = time.monotonic() - started
                proven, unproven = attribute(
                    said,
                    code,
                    plan.regions,
                    [fault.message for fault in faults],
                    worker.tree.path,
                )
                alone.update(unproven)
                ids = ", ".join(faults[p].id for p in sorted(plan.regions))
                for position, at in proven.items():
                    log(
                        f"{faults[position].id}.fault.log",
                        f"guards: fired in one run of its batch, seeded together ({ids}); "
                        f"its error is at {at}, on lines its own fault wrote\n"
                        + rendered_log(output),
                    )
            else:
                alone.update(
                    dict.fromkeys(plan.regions, "no other entry could share its run")
                )
            results: list[tuple[object, object]] = [
                (members[p], (f"fired: {faults[p].id} ({elapsed:.1f} s)", False))
                for p in sorted(proven)
            ]
            results.append((task.key, (ran, sorted(proven), alone, elapsed)))
            follow = [
                Task(members[p], pin and faults[p].suite == "bindings", task.build)
                for p in sorted(alone)
            ]
            return results, follow

        def run_restore(worker: Worker, task: Task):
            """The restored run of each command of one build this worker ran, clean."""
            build = task.key[1]
            worker.tree.reset()
            return [
                (key, clean(worker, key))
                for key in list(worker.touched)
                if build_of(key) == build
            ], []

        def run_task(worker: Worker, task: Task):
            if isinstance(task.key, int):
                return [(task.key, run_fault(worker, task))], []
            if task.key[0] == "batch":
                return run_batch(worker, task)
            return run_restore(worker, task)

        state["seeded"] = True
        failures = 0
        total = time.monotonic()
        # The restored pass runs in this phase too, a build at a time: see `Board` and the
        # module docstring. Every worker owes each build it ran clean, and each it faults.
        restores = [dict.fromkeys(build_of(k) for k in w.touched) for w in workers]
        board = phase(fault_queues, True, run_task, restores)
        for index in range(len(selected)):
            [(line, failed)] = board.get(index)
            print(line)
            failures += failed
        print(
            f"guards: {len(selected) - failures} of {len(selected)} fired "
            f"({time.monotonic() - total:.1f} s of faults)"
        )
        for key, members in batches.items():
            [(ran, proven, alone, elapsed)] = board.get(("batch", key))
            command = " ".join(by_key[key].run[0])
            print(
                f"guards: {len(proven)} of {len(members)} lint entries on `{command}` "
                f"fired in one run ({elapsed:.1f} s)"
                if ran
                else f"guards: no two lint entries on `{command}` could share a run"
            )
            for position, why in sorted(alone.items()):
                print(f"guards: {selected[members[position]].id} ran alone: {why}")
        # What keeps the caller's target and venv clean: see the module docstring. Every
        # worker ran each command it ran again, once no fault that builds it was left, so each
        # scratch tree is shown to come back clean, and worker 1's target ends on clean builds.
        # A worker's touched commands are final here: every fault has put its verdict.
        started = time.monotonic()
        counts = {k: sum(k in w.touched for w in workers) for k in by_key}
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
        # A worker waiting for a build to be ready wakes to this and stops.
        for board in boards:
            board.fail(Stopped())
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
    building = sub.add_parser(
        "build", help="compile what the entries' cargo commands build, and run nothing"
    )
    building.add_argument("--only", action="append", default=[], metavar="ID")
    building.add_argument("--suite", action="append", default=[], choices=SUITES)
    building.add_argument(
        "--target-dir", help="where cargo builds (default: as for `fire`)"
    )
    fire = sub.add_parser("fire", help="seed each fault and require its guard to fail")
    fire.add_argument("--only", action="append", default=[], metavar="ID")
    fire.add_argument("--suite", action="append", default=[], choices=SUITES)
    environments = fire.add_mutually_exclusive_group()
    environments.add_argument(
        "--venv",
        help="the one environment every bindings entry builds into and tests from, which "
        "keeps them all on worker 1",
    )
    environments.add_argument(
        "--venv-per-worker",
        action="store_true",
        help="give each worker its own environment for the bindings entries, made with uv "
        "beside its worktree and removed with it",
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
    if args.command == "build":
        return cmd_build(root, args)
    return cmd_fire(root, args)


if __name__ == "__main__":
    sys.exit(main())
