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
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

REGISTRY = "guards/faults.toml"
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
    """The scratch worktree, reset to the caller's tree between faults."""

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


def run_commands(
    fault: Fault,
    tree: Path,
    env: dict[str, str],
    timeout: int,
    seeded: bool,
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
                done = subprocess.run(
                    argv,
                    cwd=tree,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    errors="replace",
                    timeout=timeout,
                )
            except FileNotFoundError:
                output.append(f"guards: {argv[0]} isn't on PATH\n")
                return 127, "".join(output), "".join(said)
            except subprocess.TimeoutExpired as error:
                partial = error.output or ""
                if isinstance(partial, bytes):
                    partial = partial.decode(errors="replace")
                said.append(ANSI.sub("", partial))
                output.append(said[-1])
                output.append(f"guards: timed out after {timeout} s\n")
                return 124, "".join(output), "".join(said)
            said.append(ANSI.sub("", done.stdout))
            output.append(said[-1])
            code = done.returncode
            if code != 0:
                break
    return code, "".join(output), "".join(said)


def tail(text: str, lines: int = 25) -> str:
    return "\n".join("    " + line for line in text.rstrip().splitlines()[-lines:])


def clean_pass(
    selected: list[Fault],
    tree: Tree,
    env: dict[str, str],
    binding_env: dict[str, str],
    timeout: int,
    log,
    label: str,
) -> bool:
    """Run each distinct command on the unseeded tree. False if any entry can't prove a thing.

    A red command, a guard the run never reports passing, or a `message` the passing run
    already prints each make an entry's later verdict meaningless.
    """
    runs: dict[tuple, tuple[int, str, str]] = {}
    ok = True
    for fault in selected:
        key = (fault.suite, tuple(map(tuple, fault.run)))
        if key not in runs:
            started = time.monotonic()
            fenv = binding_env if fault.suite == "bindings" else env
            runs[key] = run_commands(fault, tree.path, fenv, timeout, False)
            print(
                f"guards: {label} run for {fault.id} ({time.monotonic() - started:.1f} s)"
            )
            log(f"{fault.id}.{label}.log", runs[key][1])
        code, output, said = runs[key]
        if code != 0:
            print(
                f"already red: {fault.id}: the command exits {code} with no fault "
                f"seeded ({label} run)\n{tail(output)}"
            )
            ok = False
        elif fault.expect == "test-failed" and fault.guard not in reported(
            said, runner(fault.run[-1]) or "", passed=True
        ):
            print(
                f"guard not found: {fault.id}: the {label} run never reports "
                f"{fault.guard} passing\n{tail(output)}"
            )
            ok = False
        elif fault.message and fault.message in said:
            print(
                f"weak message: {fault.id}: the {label} run already prints "
                f"{fault.message!r}, so finding it with the fault seeded proves nothing; "
                "use a line only the failure prints"
            )
            ok = False
    return ok


def cmd_fire(root: Path, args: argparse.Namespace) -> int:
    faults, problems = load(root)
    if problems:
        for problem in problems:
            print(f"guards: {problem}", file=sys.stderr)
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
    if not selected:
        print("guards: no entry selected", file=sys.stderr)
        return 1
    env = clean_env()
    env["CARGO_TARGET_DIR"] = str(
        Path(args.target_dir).resolve()
        if args.target_dir
        else Path(env.get("CARGO_TARGET_DIR") or root / "target").resolve()
        / "guards-fire"
    )
    env.pop("VIRTUAL_ENV", None)
    binding_env = dict(env)
    if venv is not None:
        binding_env["VIRTUAL_ENV"] = str(venv)
        binding_env["PATH"] = os.pathsep.join([str(venv / "bin"), env.get("PATH", "")])
    logs = Path(args.logs).resolve() if args.logs else None
    if logs:
        logs.mkdir(parents=True, exist_ok=True)

    def log(name: str, text: str) -> None:
        if logs:
            (logs / name).write_text(text, encoding="utf-8")

    # A signal ends the run through `finally`, so the scratch worktree never outlives it.
    def stop(signum: int, _frame: object) -> None:
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, stop)
    head = git(root, "rev-parse", "--short", "HEAD").stdout.strip()
    state = {"seeded": False}
    tree = Tree.make(root)
    try:
        changed = git(tree.path, "diff", "--cached", "--name-only", "HEAD").stdout
        print(
            f"guards: tree {head} plus {len(changed.split())} uncommitted paths, "
            f"CARGO_TARGET_DIR={env['CARGO_TARGET_DIR']}"
        )
        if not clean_pass(selected, tree, env, binding_env, args.timeout, log, "clean"):
            return 1
        failures = 0
        total = time.monotonic()
        for fault in selected:
            tree.reset()
            state["seeded"] = True
            why = seed(tree.path, fault, dry=False)
            if why:
                print(f"stale anchor: {fault.id}: {why}")
                failures += 1
                continue
            started = time.monotonic()
            fenv = binding_env if fault.suite == "bindings" else env
            code, output, said = run_commands(
                fault, tree.path, fenv, args.timeout, True
            )
            elapsed = time.monotonic() - started
            log(f"{fault.id}.fault.log", output)
            why = verdict(fault, code, said)
            if why is None:
                print(f"fired: {fault.id} ({elapsed:.1f} s)")
            else:
                print(
                    f"DID NOT FIRE: {fault.id}: {why} ({elapsed:.1f} s)\n{tail(output)}"
                )
                failures += 1
        print(
            f"guards: {len(selected) - failures} of {len(selected)} fired "
            f"({time.monotonic() - total:.1f} s of faults)"
        )
        # The restore leg, and what keeps the caller's target and venv clean: see the
        # module docstring.
        tree.reset()
        started = time.monotonic()
        if not clean_pass(
            selected, tree, env, binding_env, args.timeout, log, "restored"
        ):
            print("guards: the tree didn't come back clean after the faults")
            return 1
        state["seeded"] = False
        print(
            f"guards: restored, every command passes again "
            f"({time.monotonic() - started:.1f} s)"
        )
        return 1 if failures else 0
    finally:
        tree.remove()
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
    args = parser.parse_args(argv)
    root = (
        Path(args.root).resolve() if args.root else Path(__file__).resolve().parents[1]
    )
    if args.command == "list":
        return cmd_list(root, args.base)
    return cmd_fire(root, args)


if __name__ == "__main__":
    sys.exit(main())
