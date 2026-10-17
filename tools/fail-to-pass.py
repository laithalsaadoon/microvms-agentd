#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
# SPDX-License-Identifier: Apache-2.0
"""FAIL_TO_PASS: a fix's regression test fails with the fix taken out, and passes with it in.

A hand-written seeded fault for a regression test restates the bug the fix removes, and the
base commit already is that bug. This proves a fix's new or changed tests on it, the way
SWE-bench's FAIL_TO_PASS oracle does, and emits the proof as a registry entry, so every later
fire proves it again: an entry whose `patch` is the reverse of the fix's hunks in product
source, and whose `guard` is the test. Seeded, that patch turns the head back into the merge
base with the tests still in, and the test has to be reported failing there; a build that
breaks doesn't count, as in every fire.

The branch is the working tree, uncommitted and untracked files included, against its merge
base with `--base` (origin/main by default), as tools/changelog.py reads it.

- A fix is a branch that adds or changes a changelog fragment of type `fixed` or `security`
  (FIX_TYPES). The fragment is the one mark every fix already carries: `changelog:check` asks a
  change to shipped code for one, and its type is the author's statement that a defect is
  gone. It's in the tree, so a local run sees it and a push reruns it.
- The fix's hunks are its changes to product source: `.rs` files under the shipped source
  directories tools/changelog.py names (`SHIPPED`), less the test-only files `NOT_SHIPPED`
  matches. A hunk that lies inside a `#[cfg(test)]` module of such a file is a test's, and
  stays in. Everything else the branch changes (docs, scripts, manifests, the registry) stays
  in too: only the product change is taken out.
- Its tests are the test functions the branch adds or changes: a Rust `#[test]` (or
  `#[tokio::test]`, or any `#[...::test]`) function in a test target (`tests/`), in a test-only
  source file (`NOT_SHIPPED`), or in a `#[cfg(test)]` module of a product file, found with
  ast-grep; a pytest function or method under bindings/microvms-py/tests/, found with `ast`; a
  `test()` or `it()` call with a string title under bindings/microvms-js/__test__/, found with
  ast-grep. A function is changed when a line of it, its attributes or decorators included,
  differs from the merge base. Its guard is what its runner reports: the Rust path inside its
  target, the pytest node id, the node test's title.
- Changed JSON files in verify/parity/cases/ select each surface named by their capability,
  excluding explicit exemptions and skips. Each surface runs just that case against its one
  shared expectation. Every applicable surface must pass on the head; at least one must fail
  with the fix reverted. A surface passing both is unchanged, not a FAIL_TO_PASS proof.

Two subcommands:

  `check [--base REF]` (`mise run fail-to-pass:check`, in `check`; CI's `security` job with the
          pull request's base) builds nothing. Every selected source test needs a registry
          entry the branch adds or changes, with its owning runner and selector. One concrete
          pytest parameter can prove its source function; this does not claim every runtime
          parameter was independently proven. Every changed shared case needs a case-selected
          entry from at least one applicable surface. Source tests stay selected even when a
          shared case also changes; each must have a matching proof of its own runner and guard.
          CI's `guards`
          job fires that entry, since no recorded verdict covers a new one, and the fire is the
          FAIL_TO_PASS run: the patch seeded, the test must be reported failing, and a broken
          build isn't that. A fix that changes no product source, or adds or changes no test,
          has nothing to prove here and passes, saying so; review asks a fix for its test.
          It also fails when a finder reads nothing: each runs over its SENTINELS file every
          time, and has to find the test named there.

  `prove [--base REF] [--emit OWNER] [--any] [--jobs N] [--target-dir DIR] [--timeout S]`
          (`mise run fail-to-pass`; not in `check`, since it builds) makes a candidate entry
          for each of the fix's tests and fires them with tools/check-guards-fire.py in a
          scratch worktree of the branch: the fire's clean run is the head, where the test must
          pass, and its seeded run the head with the fix reverted. Each test gets a verdict:
          proven; passes with the fix reverted (no proof); the reverted tree doesn't build (no
          proof: a hand-written fault that reverts the fix where the test still compiles, or
          one R3's Falsification block generates, is the fallback); or it doesn't pass on the
          head. `--emit OWNER` appends each proven entry to verify/guards/faults/<OWNER>.toml,
          with its patch beside it; without it the entries are printed. Exits 1 when any
          ordinary test is unproven, any shared case lacks a proof, or any shared surface
          fails on the head. Partial evidence is still emitted. `--any` proves a branch that
          isn't a fix. Bindings entries fire with
          `--venv-per-worker`. Cargo builds in `--target-dir`, by default the `guards-fire`
          target `mise run guards:fire` builds in.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import json
import os
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
GUARDS_FIRE = HERE / "check-guards-fire.py"
# The registry's loader is the fire's, so this reads the entries `list` and `fire` do.
FIRE = runpy.run_path(str(GUARDS_FIRE))
# The shipped set and the fragment names are tools/changelog.py's. Its main doesn't run here.
CHANGELOG = runpy.run_path(str(HERE / "changelog.py"))
FRAGMENT = CHANGELOG["FRAGMENT"]
NOT_SHIPPED = CHANGELOG["NOT_SHIPPED"]
FRAGMENTS_DIR = "changelog.d/"
PRODUCT_DIRS = tuple(p for p in CHANGELOG["SHIPPED"] if p.endswith("/"))
REGISTRY_DIR = "verify/guards/faults"
# The fragment types that say a defect is gone.
FIX_TYPES = ("fixed", "security")

RUST_TESTS = re.compile(r"^(?:crates|bindings)/[^/]+/tests/.+\.rs$")
PY_TESTS = re.compile(r"^bindings/microvms-py/tests/(?:.+/)?test_[^/]*\.py$")
JS_TESTS = re.compile(r"^bindings/microvms-js/__test__/[^/]+\.mjs$")
# What the registry's bindings entries build before they test, which a bindings guard's
# extension has to be rebuilt by (check-guards-fire.py's docstring says why). The unit tests
# hold every bindings entry in the registry to one of these.
PY_BUILD = [
    "uvx",
    "maturin@1.14.1",
    "develop",
    "-q",
    "-m",
    "bindings/microvms-py/Cargo.toml",
]
JS_BUILD = [
    "npx", "-y", "-p", "@napi-rs/cli@3.10.8", "napi", "build", "--manifest-path", "Cargo.toml",
    "--package", "microvms-js", "--platform", "--output-dir", ".", "--cwd",
    "bindings/microvms-js",
]  # fmt: skip
PARITY_PY_BUILD = [*PY_BUILD, "--features", "parity-fixtures"]
PARITY_JS_BUILD = [
    *JS_BUILD,
    "--features",
    "parity-fixtures",
    "--dts",
    "parity-fixtures.d.ts",
]
PARITY_DIR = "verify/parity/cases"
PARITY_TABLE = "verify/parity/capabilities.toml"
PARITY_SENTINEL = f"{PARITY_DIR}/wrap-dockerfile/sentinel.json"
PARITY_SELECTOR = "MICROVMS_PARITY_CASE"
# Pytest's default parameter IDs escape Unicode with this Python codec.
PYTEST_ID_ENCODING = "unicode_escape"
PARITY_PEER = [
    "cargo",
    "build",
    "-p",
    "microvms-core",
    "--example",
    "parity-tunnel-peer",
]
# A test each finder must find in its file on every run, so a finder that stopped reading
# fails here instead of reading every fix as one with no test.
SENTINELS = {
    "rust": (
        "crates/microvms-cli/tests/dependency_direction.rs",
        "the_cli_exports_no_library_target_at_all",
    ),
    "inline": (
        "crates/protocol/src/exec.rs",
        "exec::tests::the_builder_defaults_are_the_ones_serde_gives_an_omitted_field",
    ),
    "pytest": (
        "bindings/microvms-py/tests/test_control.py",
        "bindings/microvms-py/tests/test_control.py::test_the_control_plane_checks_identifiers_before_the_wire",
    ),
    "node": (
        "bindings/microvms-js/__test__/control.mjs",
        "the control plane checks identifiers before the wire",
    ),
}

# Every attribute and comment between an item and the attribute a rule wants is skipped over.
_ATTRIBUTES = {
    "not": {
        "any": [
            {"kind": "attribute_item"},
            {"kind": "line_comment"},
            {"kind": "block_comment"},
        ]
    }
}
RULES = [
    {
        "id": "attribute",
        "language": "Rust",
        "rule": {"any": [{"kind": "attribute_item"}, {"kind": "block_comment"}]},
    },
    {
        "id": "test-fn",
        "language": "Rust",
        "rule": {
            "kind": "function_item",
            "has": {"field": "name", "pattern": "$NAME"},
            "follows": {
                "kind": "attribute_item",
                "regex": r"^#\[(?:\w+::)*test\b",
                "stopBy": _ATTRIBUTES,
            },
        },
    },
    {
        "id": "mod",
        "language": "Rust",
        "rule": {
            "kind": "mod_item",
            "all": [
                {"has": {"field": "name", "pattern": "$NAME"}},
                {"has": {"kind": "declaration_list"}},
            ],
        },
    },
    {
        "id": "cfg-test-mod",
        "language": "Rust",
        "rule": {
            "kind": "mod_item",
            "has": {"kind": "declaration_list"},
            "follows": {
                "kind": "attribute_item",
                "regex": r"^#\[cfg\(test\)\]$",
                "stopBy": _ATTRIBUTES,
            },
        },
    },
    {
        "id": "node-test",
        "language": "JavaScript",
        "rule": {
            "kind": "call_expression",
            "all": [
                {"has": {"field": "function", "regex": r"^(?:test|it)$"}},
                {
                    "has": {
                        "field": "arguments",
                        "has": {"nthChild": 1, "kind": "string", "pattern": "$TITLE"},
                    }
                },
            ],
        },
    },
]

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


class Failure(Exception):
    """An input this can't read, with the message that says which."""


def clean_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in GIT_ENV_LEAKS}
    env.update(extra)
    return env


def git(root: Path, *args: str, env: dict[str, str] | None = None) -> str:
    done = subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        cwd=root,
        capture_output=True,
        text=True,
        env=env or clean_env(),
    )
    if done.returncode != 0:
        raise Failure(f"`git {' '.join(args)}` failed: {done.stderr.strip()}")
    return done.stdout


def repo_root() -> Path:
    done = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        env=clean_env(),
    )
    if done.returncode != 0:
        raise Failure("not in a git repository; run this from the repo")
    return Path(done.stdout.strip())


# ── the branch ───────────────────────────────────────────────────────────────


class Branch:
    """The working tree against its merge base with a base ref."""

    def __init__(self, root: Path, base: str) -> None:
        self.root = root
        self.base = base
        if subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"],
            cwd=root,
            capture_output=True,
            env=clean_env(),
        ).returncode:
            raise Failure(
                f"--base {base} doesn't name a commit here; fetch it (`git fetch origin"
                " main`), or pass the branch this one merges into"
            )
        self.fork = git(root, "merge-base", base, "HEAD").strip()
        changed = git(root, "diff", "--name-only", "--no-renames", self.fork)
        self.changed = set(changed.splitlines())
        self.changed |= set(
            git(root, "ls-files", "--others", "--exclude-standard").splitlines()
        )
        self.changed.discard("")

    def head_text(self, path: str) -> str | None:
        file = self.root / path
        return file.read_text(encoding="utf-8") if file.is_file() else None

    def base_text(self, path: str) -> str | None:
        done = subprocess.run(
            ["git", "show", f"{self.fork}:{path}"],
            cwd=self.root,
            capture_output=True,
            env=clean_env(),
        )
        return done.stdout.decode("utf-8") if done.returncode == 0 else None

    def changed_lines(self, path: str) -> set[int]:
        """The head's lines (from 1) that differ from the base, and the lines around a deletion."""
        head = (self.head_text(path) or "").splitlines()
        base = (self.base_text(path) or "").splitlines()
        lines: set[int] = set()
        matcher = difflib.SequenceMatcher(a=base, b=head, autojunk=False)
        for tag, _, _, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            if j1 == j2:
                lines |= {max(j1, 1), j1 + 1}
            else:
                lines |= set(range(j1 + 1, j2 + 1))
        return lines

    def fix_fragments(self) -> list[str]:
        found = []
        for path in sorted(self.changed):
            if not path.startswith(FRAGMENTS_DIR) or self.head_text(path) is None:
                continue
            name = FRAGMENT.match(path[len(FRAGMENTS_DIR) :])
            if name and name["type"] in FIX_TYPES:
                found.append(path)
        return found

    def product_files(self) -> list[str]:
        return sorted(p for p in self.changed if is_product(p))


def is_product(path: str) -> bool:
    return (
        path.endswith(".rs")
        and path.startswith(PRODUCT_DIRS)
        and CHANGELOG["is_shipped"](path)
    )


def is_rust_test_file(path: str) -> bool:
    return bool(RUST_TESTS.match(path)) or (
        path.endswith(".rs")
        and path.startswith(PRODUCT_DIRS)
        and any(rule.match(path) for rule in NOT_SHIPPED)
    )


# ── the finders ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Test:
    kind: str  # rust | pytest | node | parity
    path: str
    guard: str
    name: str
    first: int  # its first line, attributes and decorators included, from 1
    last: int
    inline: bool = False  # in a `#[cfg(test)]` module of a product file
    case: str | None = None
    surface: str | None = None


def parity_tests(root: Path, files: list[str]) -> list[Test]:
    """Each changed case, once per surface its capability and skip table require."""
    if not files:
        return []
    table = tomllib.loads((root / PARITY_TABLE).read_bytes().decode())
    rows = {row["id"]: row for row in table["capability"]}
    found = []
    for path in files:
        relative = Path(path).relative_to(PARITY_DIR)
        if len(relative.parts) != 2:
            raise Failure(f"{path}: a case must be directly under its area")
        text = (root / path).read_bytes().decode()
        case = json.loads(text)
        case_id = relative.with_suffix("").as_posix()
        row = rows.get(case.get("capability"))
        if row is None:
            raise Failure(f"{path}: capability names no row in {PARITY_TABLE}")
        first_surface = len(found)
        for surface in ("core", "cli", "py", "ts"):
            cell = row.get(surface)
            if isinstance(cell, dict) and cell.get("exempt"):
                continue
            if not isinstance(cell, (str, list)) or not cell:
                raise Failure(f"{path}: capability has no {surface} surface")
            if surface in case.get("skip", {}):
                continue
            if surface == "core":
                guard = "core_answers_the_shared_case_corpus"
            elif surface == "cli":
                source = (
                    root / "crates/microvms-core/tests/parity_corpus/mod.rs"
                ).read_text()
                area = relative.parts[0]
                tiers = {}
                for tier in ("PROCESS", "FAKE"):
                    match = re.search(
                        rf"pub const CLI_{tier}_AREAS:.*?=\s*\[(.*?)\];", source, re.S
                    )
                    if match is None:
                        raise Failure(
                            f"cannot read CLI_{tier}_AREAS from parity_corpus"
                        )
                    tiers[tier] = re.findall(r'"([^"\\]+)"', match[1])
                if area in tiers["PROCESS"]:
                    guard = "the_cli_process_answers_the_shared_case_corpus"
                elif area in tiers["FAKE"]:
                    guard = "guards::parity::the_cli_answers_the_shared_case_corpus_against_its_fakes"
                else:
                    raise Failure(f"{path}: no CLI runner owns area {area!r}")
            elif surface == "py":
                pytest_id = case_id.encode(PYTEST_ID_ENCODING).decode()
                guard = f"bindings/microvms-py/tests/test_parity_cases.py::test_a_case[{pytest_id}]"
            else:
                guard = case_id
            found.append(
                Test(
                    "parity",
                    path,
                    guard,
                    f"parity-{surface}-{case_id}",
                    1,
                    len(text.splitlines()),
                    case=case_id,
                    surface=surface,
                )
            )
        if len(found) == first_surface:
            raise Failure(f"{path}: no applicable surface can prove this case")
    return found


def ast_grep(root: Path, files: list[str]) -> list[dict]:
    if not files:
        return []
    inline = "\n---\n".join(json.dumps(rule) for rule in RULES)
    try:
        done = subprocess.run(
            ["ast-grep", "scan", "--inline-rules", inline, "--json=stream", *files],
            cwd=root,
            capture_output=True,
            text=True,
            env=clean_env(),
        )
    except FileNotFoundError:
        raise Failure(
            "ast-grep isn't on PATH; run this through `mise run fail-to-pass:check`"
        ) from None
    if done.returncode != 0:
        raise Failure(f"ast-grep failed:\n{done.stderr}")
    return [json.loads(line) for line in done.stdout.splitlines() if line.strip()]


def with_attributes(lines: list[str], first: int, attributes: dict[int, int]) -> int:
    """The first line of the attributes and comments right above line `first` (from 0)."""
    while first > 0:
        if first - 1 in attributes:
            first = attributes[first - 1]
        elif not lines[first - 1].strip() or lines[first - 1].strip().startswith("//"):
            first -= 1
        else:
            break
    # Internal blank gaps join attributes and docs; leading separators belong to neither test.
    # The declaration we started from is nonblank, so this cannot run past it.
    while not lines[first].strip():
        first += 1
    return first


def module_path(path: str) -> list[str]:
    """The module a Rust file is inside its target: a test target's root is the target's own."""
    if RUST_TESTS.match(path):
        rest = path.split("/tests/", 1)[1].removesuffix(".rs").split("/")
        # `tests/x.rs` and `tests/x/main.rs` are roots; `tests/x/y.rs` is module `y` of `x`.
        if len(rest) == 1 or rest[1:] == ["main"]:
            return []
        rest = rest[1:]
    else:
        rest = path.split("/src/", 1)[1].removesuffix(".rs").split("/")
        if rest[0] == "bin":
            rest = rest[1:]
            if len(rest) == 1 or rest[1:] == ["main"]:
                return []
            rest = rest[1:]
        if rest in (["lib"], ["main"]):
            return []
    if rest[-1] == "mod":
        rest = rest[:-1]
    return rest


def rust_tests(root: Path, files: list[str], inline: bool) -> list[Test]:
    """The test functions in `files`; with `inline`, only those inside a `#[cfg(test)]` module."""
    found: list[Test] = []
    matches = ast_grep(root, files)
    by_file: dict[str, list[dict]] = {}
    for match in matches:
        by_file.setdefault(match["file"], []).append(match)
    for path in files:
        text = (root / path).read_text(encoding="utf-8").splitlines()
        items = by_file.get(path, [])
        attributes = {
            m["range"]["end"]["line"]: m["range"]["start"]["line"]
            for m in items
            if m["ruleId"] == "attribute"
        }
        mods = [
            (m["range"]["start"]["line"], m["range"]["end"]["line"], name(m, "NAME"))
            for m in items
            if m["ruleId"] == "mod"
        ]
        test_mods = [
            (m["range"]["start"]["line"], m["range"]["end"]["line"])
            for m in items
            if m["ruleId"] == "cfg-test-mod"
        ]
        prefix = module_path(path)
        for match in items:
            if match["ruleId"] != "test-fn":
                continue
            start = match["range"]["start"]["line"]
            end = match["range"]["end"]["line"]
            within = any(a <= start and end <= b for a, b in test_mods)
            if inline and not within:
                continue
            scope = [n for a, b, n in sorted(mods) if a < start and end <= b]
            fn = name(match, "NAME")
            found.append(
                Test(
                    kind="rust",
                    path=path,
                    guard="::".join([*prefix, *scope, fn]),
                    name=fn,
                    first=with_attributes(text, start, attributes) + 1,
                    last=end + 1,
                    inline=inline,
                )
            )
    return found


def test_module_lines(root: Path, path: str) -> list[tuple[int, int]]:
    """Each `#[cfg(test)]` module's lines in a Rust file, its attributes included, from 1."""
    text = (root / path).read_text(encoding="utf-8").splitlines()
    matches = ast_grep(root, [path])
    attributes = {
        m["range"]["end"]["line"]: m["range"]["start"]["line"]
        for m in matches
        if m["ruleId"] == "attribute"
    }
    return [
        (
            with_attributes(text, m["range"]["start"]["line"], attributes) + 1,
            m["range"]["end"]["line"] + 1,
        )
        for m in matches
        if m["ruleId"] == "cfg-test-mod"
    ]


def name(match: dict, variable: str) -> str:
    return match["metaVariables"]["single"][variable]["text"]


def python_tests(root: Path, files: list[str]) -> list[Test]:
    """pytest's functions and methods: `test*` at the top, or in a `Test*` or TestCase class."""
    found: list[Test] = []
    for path in files:
        tree = ast.parse((root / path).read_text(encoding="utf-8"), filename=path)

        def add(node: ast.AST, scope: list[str]) -> None:
            first = min([node.lineno, *(d.lineno for d in node.decorator_list)])
            found.append(
                Test(
                    kind="pytest",
                    path=path,
                    guard="::".join([path, *scope, node.name]),
                    name=node.name,
                    first=first,
                    last=node.end_lineno or node.lineno,
                )
            )

        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("test"):
                    add(node, [])
            elif isinstance(node, ast.ClassDef) and is_test_class(node):
                for item in node.body:
                    if isinstance(
                        item, (ast.FunctionDef, ast.AsyncFunctionDef)
                    ) and item.name.startswith("test"):
                        add(item, [node.name])
    return found


def is_test_class(node: ast.ClassDef) -> bool:
    bases = [ast.unparse(b) for b in node.bases]
    return node.name.startswith("Test") or any(b.endswith("TestCase") for b in bases)


def node_tests(root: Path, files: list[str]) -> list[Test]:
    matches = [m for m in ast_grep(root, files) if m["ruleId"] == "node-test"]
    if not matches:
        return []
    # ast-grep admits only JavaScript string nodes, never expressions or templates.
    # Decode them with their own language: Python rejects valid JS Unicode escapes.
    done = subprocess.run(
        [
            "node",
            "-e",
            "const fs=require('node:fs'),vm=require('node:vm');"
            "const strings=JSON.parse(fs.readFileSync(0,'utf8'));"
            "process.stdout.write(JSON.stringify(strings.map(s=>vm.runInNewContext(s))));",
        ],
        cwd=root,
        input=json.dumps([name(m, "TITLE") for m in matches]),
        capture_output=True,
        text=True,
        env=clean_env(),
    )
    if done.returncode != 0:
        raise Failure(f"cannot decode JavaScript test titles:\n{done.stderr.strip()}")
    return [
        Test(
            kind="node",
            path=match["file"],
            guard=title,
            name=title,
            first=match["range"]["start"]["line"] + 1,
            last=match["range"]["end"]["line"] + 1,
        )
        for match, title in zip(matches, json.loads(done.stdout), strict=True)
    ]


def sentinel_problems(root: Path) -> list[str]:
    """A finder that doesn't find its sentinel, on this tree."""
    finders = {
        "rust": lambda files: rust_tests(root, files, inline=False),
        "inline": lambda files: rust_tests(root, files, inline=True),
        "pytest": lambda files: python_tests(root, files),
        "node": lambda files: node_tests(root, files),
    }
    problems = []
    for kind, (path, guard) in SENTINELS.items():
        if not (root / path).is_file():
            problems.append(f"the {kind} sentinel's file {path} doesn't exist")
            continue
        if guard not in {t.guard for t in finders[kind]([path])}:
            problems.append(
                f"the {kind} finder doesn't find {guard} in {path}, so it would read a fix's"
                " tests as none"
            )
    if not (root / PARITY_SENTINEL).is_file():
        problems.append(f"the parity sentinel's file {PARITY_SENTINEL} doesn't exist")
    elif {test.surface for test in parity_tests(root, [PARITY_SENTINEL])} != {
        "core",
        "cli",
        "py",
        "ts",
    }:
        problems.append(
            "the parity finder doesn't find every surface of wrap-dockerfile/sentinel"
        )
    return problems


def branch_tests(branch: Branch) -> list[Test]:
    """The tests the branch adds or changes, each once."""
    files = [p for p in sorted(branch.changed) if branch.head_text(p) is not None]
    candidates = [
        *rust_tests(branch.root, [p for p in files if is_rust_test_file(p)], False),
        *rust_tests(branch.root, [p for p in files if is_product(p)], True),
        *python_tests(branch.root, [p for p in files if PY_TESTS.match(p)]),
        *node_tests(branch.root, [p for p in files if JS_TESTS.match(p)]),
        *parity_tests(
            branch.root,
            [
                p
                for p in files
                if p.startswith(f"{PARITY_DIR}/") and p.endswith(".json")
            ],
        ),
    ]
    tests: dict[tuple[str, str], Test] = {}
    node_titles: dict[tuple[str, str], int] = {}
    for test in candidates:
        if test.kind == "node":
            key = (test.path, test.guard)
            node_titles[key] = node_titles.get(key, 0) + 1
    lines = {path: branch.changed_lines(path) for path in {t.path for t in candidates}}
    for test in candidates:
        if any(test.first <= n <= test.last for n in lines[test.path]):
            if test.kind == "node" and node_titles[(test.path, test.guard)] > 1:
                raise Failure(
                    f"{test.path}: changed Node test title {test.guard!r} is ambiguous;"
                    " use a unique title so its proof identifies one source test"
                )
            tests.setdefault((test.path, test.guard), test)
    return list(tests.values())


# ── the reverse of the fix ───────────────────────────────────────────────────


def reverted(
    base: list[str], head: list[str], keep: list[tuple[int, int]]
) -> list[str]:
    """`head` with every hunk against `base` taken back, but the hunks inside `keep`.

    `keep` is head lines, from 1, inclusive: each `#[cfg(test)]` module. A hunk is kept when
    every head line it writes is inside one range, or, for a deletion, when the lines on both
    sides of it are.
    """

    def inside(first: int, last: int) -> bool:
        return any(a <= first and last <= b for a, b in keep)

    out: list[str] = []
    matcher = difflib.SequenceMatcher(a=base, b=head, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            out += base[i1:i2]
        elif (j1 < j2 and inside(j1 + 1, j2)) or (j1 == j2 and inside(j1, j1 + 1)):
            out += head[j1:j2]
        else:
            out += base[i1:i2]
    return out


def fix_patch(branch: Branch) -> str:
    """The patch that takes the fix out of the head: the reverse of its product hunks."""
    scratch = Path(tempfile.mkdtemp(prefix="fail-to-pass-index-"))
    try:
        env = clean_env(GIT_INDEX_FILE=str(scratch / "index"))
        git(branch.root, "read-tree", "HEAD", env=env)
        git(branch.root, "add", "-A", env=env)
        head_tree = git(branch.root, "write-tree", env=env).strip()
        for path in branch.product_files():
            head = branch.head_text(path)
            base = branch.base_text(path)
            keep = test_module_lines(branch.root, path) if head is not None else []
            target = reverted(
                (base or "").splitlines(keepends=True),
                (head or "").splitlines(keepends=True),
                keep,
            )
            if base is None and not target:
                git(branch.root, "rm", "-q", "--cached", "--", path, env=env)
                continue
            blob = subprocess.run(
                ["git", "hash-object", "-w", "--stdin"],
                cwd=branch.root,
                input="".join(target).encode("utf-8"),
                capture_output=True,
                env=env,
                check=True,
            ).stdout.decode()
            git(
                branch.root,
                "update-index",
                "--add",
                "--cacheinfo",
                f"100644,{blob.strip()},{path}",
                env=env,
            )
        fixed_tree = git(branch.root, "write-tree", env=env).strip()
        return git(branch.root, "diff", "--binary", head_tree, fixed_tree)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


# ── the registry ─────────────────────────────────────────────────────────────


def registry(texts: dict[str, str]) -> dict[str, dict]:
    """Each entry of the registry files in `texts`, by id, as the fire's loader reads them: a
    family's rows and a scanner's entries are entries like a `[[fault]]` table's."""
    if not texts:
        return {}
    tables, problems = FIRE["parse_registry"](texts)
    if problems:
        raise Failure("; ".join(problems))
    return {str(t.data.get("id")): t.data for t in tables if isinstance(t.data, dict)}


def registry_texts(branch: Branch, at_base: bool) -> dict[str, str]:
    if at_base:
        listing = git(
            branch.root, "ls-tree", "--name-only", branch.fork, f"{REGISTRY_DIR}/"
        )
        return {
            path: branch.base_text(path) or ""
            for path in listing.splitlines()
            if path.endswith(".toml")
        }
    return {
        f"{REGISTRY_DIR}/{file.name}": file.read_text(encoding="utf-8")
        for file in sorted((branch.root / REGISTRY_DIR).glob("*.toml"))
    }


def new_entries(branch: Branch) -> dict[str, dict]:
    """The entries the branch adds or changes."""
    base = registry(registry_texts(branch, at_base=True))
    head = registry(registry_texts(branch, at_base=False))
    return {i: e for i, e in head.items() if base.get(i) != e}


# ── check ────────────────────────────────────────────────────────────────────


def option_values(command: list[str], names: tuple[str, ...]) -> list[str]:
    """The values of repeated long or short selectors, including `--name=value`."""
    values = []
    for index, argument in enumerate(command):
        if argument in names:
            values.append(command[index + 1] if index + 1 < len(command) else "")
        else:
            for name in names:
                prefix = name + "=" if name.startswith("--") else name
                if argument.startswith(prefix):
                    values.append(argument[len(prefix) :].removeprefix("="))
    return values


def rust_entry_selects(test: Test, command: list[str], targets: list[dict]) -> bool:
    expected = rust_command(test, targets)
    if expected is None or command[:2] != ["cargo", "test"] or "--" not in command:
        return False
    cargo = command[: command.index("--")]
    harness = command[command.index("--") + 1 :]
    if option_values(cargo, ("-p", "--package")) != [expected[3]]:
        return False
    manifests = option_values(cargo, ("--manifest-path",))
    if (
        manifests
        and manifests != ["Cargo.toml"]
        and manifests != ["/".join(test.path.split("/")[:2]) + "/Cargo.toml"]
    ):
        return False
    if "--exact" not in harness or test.guard not in cargo + harness:
        return False
    selected = [arg for arg in cargo if arg == "--lib"]
    selected += [
        f"{flag}={value}"
        for flag in ("--test", "--bin")
        for value in option_values(cargo, (flag,))
    ]
    wanted = ["--lib"] if expected[4] == "--lib" else [f"{expected[4]}={expected[5]}"]
    # Cargo's implicit target is unambiguous only in a library-only package.
    if not selected and wanted == ["--lib"]:
        runnable = [
            t
            for t in targets
            if t["package"] == expected[3]
            and set(t["kind"]) & {"lib", "rlib", "cdylib", "staticlib", "bin", "test"}
        ]
        if len(runnable) == 1:
            selected = ["--lib"]
    return selected == wanted and not any(
        flag in cargo for flag in ("--workspace", "--all-targets", "--tests", "--bins")
    )


def entry_proves(test: Test, entry: dict, targets: list[dict]) -> bool:
    """A named failure from this test's owning runner, not a same-named test elsewhere."""
    guard = entry.get("guard")
    if entry.get("expect") != "test-failed" or not isinstance(guard, str):
        return False
    parameter = (
        test.kind == "pytest"
        and guard.startswith(test.guard + "[")
        and guard.endswith("]")
    )
    if guard != test.guard and not parameter:
        return False
    run = entry.get("run")
    if not isinstance(run, list) or not run:
        return False
    command = run[-1] if isinstance(run[0], list) else run
    if not isinstance(command, list) or not all(
        isinstance(arg, str) for arg in command
    ):
        return False
    if test.kind == "rust":
        return rust_entry_selects(test, command, targets)
    if test.kind == "pytest":
        return (
            command[:3] == ["python", "-m", "pytest"]
            and "-rA" in command
            and any(selector in command for selector in (test.path, test.guard, guard))
        )
    if command[:2] != ["node", "--test"]:
        return False
    _, _, *arguments = command
    # A preload can register a same-title test from a different source file.
    preload_options = (
        "--import",
        "--require",
        "--loader",
        "--experimental-loader",
        "-r",
    )
    if any(arg.startswith(preload_options) for arg in arguments):
        return False
    value_options = {
        "--test-name-pattern",
        "--test-skip-pattern",
        "--test-reporter",
        "--test-reporter-destination",
        "--test-concurrency",
        "--test-timeout",
        "--test-shard",
    }
    files = []
    while arguments:
        arg = arguments.pop(0)
        if arg in value_options:
            if not arguments:
                return False
            arguments.pop(0)
        elif not arg.startswith("-"):
            files.append(arg)
    return "--test-reporter=tap" in command and files == [test.path]


def what_to_prove(branch: Branch, any_branch: bool) -> tuple[list[Test], str | None]:
    """The tests a fix has to prove, or why there's nothing to prove."""
    fragments = branch.fix_fragments()
    if not fragments and not any_branch:
        return [], (
            "this branch adds no changelog fragment of type "
            + " or ".join(f"`{t}`" for t in FIX_TYPES)
            + ", so it isn't a fix"
        )
    if not branch.product_files():
        return [], "this fix changes no product source, so nothing can be taken out"
    tests = branch_tests(branch)
    if not tests:
        return [], (
            "this fix adds or changes no test, so there's nothing to prove; review asks a"
            " fix for its regression test"
        )
    return tests, None


def check(branch: Branch) -> int:
    problems = sentinel_problems(branch.root)
    if problems:
        print(f"fail-to-pass: {len(problems)} problems:")
        for problem in problems:
            print(f"  {problem}")
        return 1
    tests, why = what_to_prove(branch, any_branch=False)
    if why:
        print(f"fail-to-pass: {why} (against {branch.base})")
        return 0
    entries = new_entries(branch)
    parity_cases = {test.case for test in tests if test.kind == "parity"}
    parity_proven = set()
    proven = set()
    for test in (test for test in tests if test.kind == "parity"):
        run, _ = parity_command(test)
        matches = {
            entry_id
            for entry_id, entry in entries.items()
            if entry.get("guard") == test.guard
            and entry.get("run") == run
            and entry.get("expect") == "test-failed"
        }
        if matches:
            parity_proven.add(test.case)
            proven.update(matches)
    parity_missing = sorted(parity_cases - parity_proven)
    if parity_missing:
        print(
            "fail-to-pass: changed parity cases lack a case-selected proof: "
            + ", ".join(parity_missing)
        )
        print(
            "Run `mise run fail-to-pass -- --emit <owner>` to test every applicable surface."
        )
        return 1
    ordinary = [test for test in tests if test.kind != "parity"]
    targets = (
        cargo_targets(branch.root) if any(t.kind == "rust" for t in ordinary) else []
    )
    matched = {
        test: [
            entry_id
            for entry_id, entry in entries.items()
            if entry_proves(test, entry, targets)
        ]
        for test in ordinary
    }
    missing = [test for test, entry_ids in matched.items() if not entry_ids]
    if not missing:
        proven.update(
            entry_id for entry_ids in matched.values() for entry_id in entry_ids
        )
        print(
            "fail-to-pass: this fix's tests have entries this branch adds, which CI's"
            " `guards` job fires: "
            + ", ".join(f"{i} ({entries[i]['guard']})" for i in sorted(proven))
        )
        return 0
    print(
        f"fail-to-pass: this fix ({', '.join(branch.fix_fragments())}) adds or changes these"
        " tests without a new or changed proof entry selecting their owning runner:"
    )
    for test in missing:
        where = " (inline)" if test.inline else ""
        print(f"  {test.guard}{where}, in {test.path}")
    print(
        "Run `mise run fail-to-pass -- --emit <owner>` to prove them against the merge base"
        f" and write the entries into {REGISTRY_DIR}/<owner>.toml. Where the base with the"
        " tests doesn't build, register a fault by hand that takes the fix out and leaves"
        " the test compiling, and name the test as its guard."
    )
    return 1


# ── prove ────────────────────────────────────────────────────────────────────


@dataclass
class Candidate:
    test: Test
    id: str
    run: list[list[str]]
    suite: str


def slug(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())
    out = "f2p"
    for word in words:
        if len(out) + 1 + len(word) > 60:
            break
        out += f"-{word}"
    return out


def cargo_targets(root: Path) -> list[dict]:
    done = subprocess.run(
        ["cargo", "metadata", "--no-deps", "--format-version", "1"],
        cwd=root,
        capture_output=True,
        text=True,
        env=clean_env(),
    )
    if done.returncode != 0:
        raise Failure(f"`cargo metadata` failed:\n{done.stderr.strip()}")
    targets = []
    for package in json.loads(done.stdout)["packages"]:
        for target in package["targets"]:
            src = Path(target["src_path"]).resolve()
            targets.append(
                {
                    "package": package["name"],
                    "name": target["name"],
                    "kind": target["kind"],
                    "src": src.relative_to(root.resolve()).as_posix(),
                    "features": target.get("required-features") or [],
                }
            )
    return targets


def rust_command(test: Test, targets: list[dict]) -> list[str] | None:
    crate = "/".join(test.path.split("/")[:2]) + "/"
    mine = [t for t in targets if t["src"].startswith(crate)]
    if RUST_TESTS.match(test.path):
        rest = test.path[len(crate) + len("tests/") :].split("/")
        root_file = (
            crate + "tests/" + (rest[0] if len(rest) == 1 else f"{rest[0]}/main.rs")
        )
        target = next(
            (t for t in mine if "test" in t["kind"] and t["src"] == root_file), None
        )
        flag = ["--test", target["name"]] if target else None
    else:
        binary = next(
            (
                t
                for t in mine
                if "bin" in t["kind"]
                and (
                    t["src"] == test.path
                    or (
                        "/src/bin/" in t["src"]
                        and t["src"].endswith("/main.rs")
                        and test.path.startswith(t["src"].removesuffix("main.rs"))
                    )
                )
            ),
            None,
        )
        target = binary or next(
            (
                t
                for t in mine
                if set(t["kind"]) & {"lib", "rlib", "cdylib", "staticlib"}
                and "/src/bin/" not in test.path
                and test.path != crate + "src/main.rs"
            ),
            None,
        )
        flag = ["--bin", binary["name"]] if binary else ["--lib"] if target else None
        if (
            target is None
            and "/src/bin/" not in test.path
            and test.path != crate + "src/main.rs"
        ):
            target = next(
                (
                    t
                    for t in mine
                    if "bin" in t["kind"] and t["src"].endswith("/main.rs")
                ),
                None,
            )
            flag = ["--bin", target["name"]] if target else None
    if target is None or flag is None:
        return None
    features = (
        ["--features", ",".join(target["features"])] if target["features"] else []
    )
    return [
        "cargo",
        "test",
        "-p",
        target["package"],
        *flag,
        *features,
        "--",
        "--exact",
        test.guard,
    ]


def candidates(branch: Branch, tests: list[Test]) -> tuple[list[Candidate], list[str]]:
    taken = set(registry(registry_texts(branch, at_base=False)))
    targets = cargo_targets(branch.root) if any(t.kind == "rust" for t in tests) else []
    made: list[Candidate] = []
    skipped: list[str] = []
    for test in tests:
        if test.kind == "parity":
            run, suite = parity_command(test)
        elif test.kind == "rust":
            argv = rust_command(test, targets)
            if argv is None:
                skipped.append(
                    f"{test.guard}: {test.path} is no test target's root and no source file of"
                    " a lib or bin target, so there's no one command that runs it"
                )
                continue
            run, suite = [argv], "rust"
        elif test.kind == "pytest":
            # Source-test discovery can select the whole parametrized corpus runner,
            # whose control-plane case needs the opt-in fixture constructor.
            build = (
                PARITY_PY_BUILD
                if test.path == "bindings/microvms-py/tests/test_parity_cases.py"
                else PY_BUILD
            )
            run = [build, ["python", "-m", "pytest", "-q", "-rA", test.guard]]
            suite = "bindings"
        else:
            # Node runs the whole source file, including the control-plane fixture,
            # even when the changed test covers another part of the corpus.
            build = (
                PARITY_JS_BUILD
                if test.path == "bindings/microvms-js/__test__/parity_cases.mjs"
                else JS_BUILD
            )
            run = [build, ["node", "--test", "--test-reporter=tap", test.path]]
            suite = "bindings"
        entry_id, n = slug(test.name), 2
        while entry_id in taken:
            entry_id, n = f"{slug(test.name)}-{n}", n + 1
        taken.add(entry_id)
        made.append(Candidate(test, entry_id, run, suite))
    return made, skipped


def parity_command(test: Test) -> tuple[list[list[str]], str]:
    """Run exactly this case through its real surface runner, retaining the shared expect."""
    assert test.case is not None, "a shared proof must name its case"
    if test.surface in ("core", "cli"):
        if test.surface == "core":
            target = ["-p", "microvms-core", "--test", "parity_cases"]
        elif test.guard.startswith("guards::"):
            target = ["-p", "microvms-cli", "--bin", "microvm"]
        else:
            target = ["-p", "microvms-cli", "--test", "parity_cases"]
        config = f"env.{PARITY_SELECTOR}.value={toml_str(test.case)}"
        force = f"env.{PARITY_SELECTOR}.force=true"
        return [
            PARITY_PEER,
            [
                "cargo",
                "test",
                "--config",
                config,
                "--config",
                force,
                *target,
                "--",
                "--exact",
                test.guard,
            ],
        ], "rust"
    if test.surface == "py":
        return [
            PARITY_PEER,
            PARITY_PY_BUILD,
            ["python", "-m", "pytest", "-q", "-rA", test.guard],
        ], "bindings"
    pattern = f"^{re.escape(test.case)}$"
    return [
        PARITY_PEER,
        PARITY_JS_BUILD,
        [
            "node",
            "--test",
            "--test-reporter=tap",
            f"--test-name-pattern={pattern}",
            "bindings/microvms-js/__test__/parity_cases.mjs",
        ],
    ], "bindings"


def toml_str(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def entry_toml(candidate: Candidate, fork: str) -> str:
    runs = candidate.run
    if len(runs) == 1:
        run = "[" + ", ".join(toml_str(a) for a in runs[0]) + "]"
    else:
        run = (
            "[\n"
            + "".join(
                "  [" + ", ".join(toml_str(a) for a in argv) + "],\n" for argv in runs
            )
            + "]"
        )
    return (
        f"# FAIL_TO_PASS: fails on {fork[:12]} with the fix taken out (tools/fail-to-pass.py).\n"
        "[[fault]]\n"
        f"id = {toml_str(candidate.id)}\n"
        f"guard = {toml_str(candidate.test.guard)}\n"
        f"run = {run}\n"
        'expect = "test-failed"\n'
        f"suite = {toml_str(candidate.suite)}\n"
        f'patch = "{REGISTRY_DIR}/{candidate.id}.patch"\n'
    )


def write_entries(
    root: Path, owner: str, made: list[Candidate], patch: str, fork: str
) -> None:
    file = root / REGISTRY_DIR / f"{owner}.toml"
    text = (
        file.read_text(encoding="utf-8")
        if file.is_file()
        else (f"# ── {owner}: FAIL_TO_PASS entries tools/fail-to-pass.py emitted ──\n")
    )
    for candidate in made:
        text = text.rstrip("\n") + "\n\n" + entry_toml(candidate, fork)
        (root / REGISTRY_DIR / f"{candidate.id}.patch").write_text(
            patch, encoding="utf-8"
        )
    file.write_text(text, encoding="utf-8")


def overlay(source: Path, tree: Path) -> None:
    """Make `tree` (a worktree at HEAD) hold `source`'s working tree, untracked files included."""
    for path in git(source, "diff", "--name-only", "--no-renames", "HEAD").splitlines():
        file = source / path
        if file.is_file():
            (tree / path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(file, tree / path)
        else:
            (tree / path).unlink(missing_ok=True)
    for path in git(source, "ls-files", "--others", "--exclude-standard").splitlines():
        (tree / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(source / path, tree / path)


VERDICT = re.compile(
    r"^(?P<kind>fired|DID NOT FIRE|already red|guard not found|stale anchor): (?P<id>[a-z0-9-]+)"
    r"(?:: (?P<why>.*?))?(?: \(\d+\.\d s\))?$",
    re.MULTILINE,
)
RESTORED = re.compile(
    r"^guards: restored, every command passes again \([^)]+\)$", re.MULTILINE
)


def judge(kind: str, why: str) -> tuple[bool, str]:
    """Whether a fire's line for a candidate proves its test, and what it means for it."""
    if kind == "fired":
        return True, "proven: it fails with the fix taken out, and passes with it in"
    if kind == "DID NOT FIRE" and why.startswith("the command passed"):
        return (
            False,
            "passes with the fix taken out, so it proves nothing about the fix",
        )
    if kind == "DID NOT FIRE" and "the build broke" in why:
        return False, (
            "the merge base with the tests doesn't build, and a broken build isn't the test"
            " failing: register a fault by hand that takes the fix out and leaves the test"
            " compiling, or generate one from its Falsification block"
        )
    if kind == "DID NOT FIRE":
        return False, f"fails with the fix taken out, but not as this test: {why}"
    if kind in ("already red", "guard not found"):
        return False, "doesn't pass on the head, where it has to"
    return False, f"its patch doesn't apply to the head: {why}"


def fire(
    branch: Branch, made: list[Candidate], patch: str, args
) -> dict[str, tuple[bool, str]]:
    """Each candidate's verdict from a fire of it in a scratch worktree of the branch."""
    scratch = Path(tempfile.mkdtemp(prefix="fail-to-pass-"))
    tree = scratch / "tree"
    verdicts: dict[str, tuple[bool, str]] = {}
    try:
        git(branch.root, "worktree", "add", "-q", "--detach", str(tree), "HEAD")
        overlay(branch.root, tree)
        pending = list(made)
        # A candidate whose clean run is red stops the fire before any fault, so it goes, and
        # the rest fire again.
        while pending:
            owner = "fail-to-pass-candidates"
            (tree / REGISTRY_DIR / f"{owner}.toml").unlink(missing_ok=True)
            write_entries(tree, owner, pending, patch, branch.fork)
            argv = [sys.executable, str(GUARDS_FIRE), "--root", str(tree), "fire"]
            for candidate in pending:
                argv += ["--only", candidate.id]
            argv += ["--target-dir", args.target_dir, "--timeout", str(args.timeout)]
            argv += ["--jobs", str(args.jobs), "--logs", str(scratch / "logs")]
            if any(c.suite == "bindings" for c in pending):
                argv.append("--venv-per-worker")
            done = subprocess.run(argv, capture_output=True, text=True, env=clean_env())
            print(done.stdout, end="")
            lines = {
                m["id"]: (m["kind"], m["why"] or "")
                for m in VERDICT.finditer(done.stdout)
            }
            red = [
                c
                for c in pending
                if lines.get(c.id, ("",))[0] in ("already red", "guard not found")
            ]
            for candidate in pending:
                if candidate.id in lines:
                    verdicts[candidate.id] = judge(*lines[candidate.id])
            if red and len(red) < len(pending):
                pending = [c for c in pending if c not in red]
                for candidate in pending:
                    verdicts.pop(candidate.id, None)
                continue
            # A surviving fault legitimately exits 1, but an incomplete restored pass
            # cannot prove even the candidates whose earlier fault verdict was green.
            expected_exit = int(any(kind != "fired" for kind, _ in lines.values()))
            completed = (
                bool(RESTORED.search(done.stdout)) and done.returncode == expected_exit
            )
            for candidate in pending:
                verdicts.setdefault(
                    candidate.id,
                    (
                        False,
                        f"the fire never judged it (exit {done.returncode}):\n{done.stderr[-2000:]}",
                    ),
                )
                verdict = verdicts[candidate.id]
                if not completed and (verdict[0] or parity_base_pass(verdict)):
                    verdicts[candidate.id] = (
                        False,
                        "the fire did not complete its restored verification "
                        f"(exit {done.returncode}):\n{done.stderr[-2000:]}",
                    )
            break
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(tree)],
            cwd=branch.root,
            capture_output=True,
            env=clean_env(),
        )
        shutil.rmtree(scratch, ignore_errors=True)
    return verdicts


def prove(branch: Branch, args) -> int:
    tests, why = what_to_prove(branch, args.any)
    if why:
        print(f"fail-to-pass: {why} (against {branch.base})")
        return 0
    made, skipped = candidates(branch, tests)
    for line in skipped:
        print(f"fail-to-pass: skipped {line}")
    if not made:
        print("fail-to-pass: no test here has a command to prove it with")
        return 1
    patch = fix_patch(branch)
    if not patch.strip():
        print(
            "fail-to-pass: the fix's hunks are all inside test modules; nothing to take out"
        )
        return 1
    check_patch(branch.root, patch)
    verdicts = fire(branch, made, patch, args)
    proven = [c for c in made if verdicts[c.id][0]]
    print(
        f"fail-to-pass: against {branch.fork[:12]}, the merge base with {branch.base}:"
    )
    for candidate in made:
        verdict = verdicts[candidate.id]
        label = (
            f"{candidate.test.case} on {candidate.test.surface}"
            if candidate.test.kind == "parity"
            else candidate.test.guard
        )
        message = (
            "passes on the head and merge base: unchanged surface, not FAIL_TO_PASS"
            if candidate.test.kind == "parity" and parity_base_pass(verdict)
            else verdict[1]
        )
        print(f"  {label}: {message}")
    if not proven:
        print("fail-to-pass: no test of this fix is proven")
        return 1
    if args.emit:
        write_entries(branch.root, args.emit, proven, patch, branch.fork)
        print(
            f"fail-to-pass: wrote {', '.join(c.id for c in proven)} into"
            f" {REGISTRY_DIR}/{args.emit}.toml, with their patches"
        )
    else:
        print("fail-to-pass: the entries, for `--emit <owner>` to write:")
        for candidate in proven:
            print(entry_toml(candidate, branch.fork))
    # A shared case can expose a bug in one adapter while the others already agree.
    # Every head must pass, and each case needs a real regression on some surface.
    cases = {c.test.case for c in made if c.test.kind == "parity"}
    proven_cases = {c.test.case for c in proven if c.test.kind == "parity"}
    parity_errors = any(
        c.test.kind == "parity"
        and not verdicts[c.id][0]
        and not parity_base_pass(verdicts[c.id])
        for c in made
    )
    ordinary_missing = any(
        c.test.kind != "parity" and not verdicts[c.id][0] for c in made
    )
    return int(
        bool(skipped) or ordinary_missing or parity_errors or bool(cases - proven_cases)
    )


def parity_base_pass(verdict: tuple[bool, str]) -> bool:
    return (
        not verdict[0]
        and verdict[1]
        == "passes with the fix taken out, so it proves nothing about the fix"
    )


def check_patch(root: Path, patch: str) -> None:
    """The patch applies to the head, as `guards:list` will hold it to."""
    done = subprocess.run(
        ["git", "apply", "--check", "-"],
        cwd=root,
        input=patch,
        capture_output=True,
        text=True,
        env=clean_env(),
    )
    if done.returncode != 0:
        raise Failure(
            f"the fix's reverse patch doesn't apply to the head:\n{done.stderr}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    checking = sub.add_parser(
        "check", help="a fix's tests have entries that prove them; no builds"
    )
    proving = sub.add_parser(
        "prove", help="prove a fix's tests, and emit their entries"
    )
    for command in (checking, proving):
        command.add_argument(
            "--base",
            default="origin/main",
            help="the branch this one merges into, through its merge base (origin/main)",
        )
    proving.add_argument("--emit", metavar="OWNER")
    proving.add_argument(
        "--any", action="store_true", help="prove a branch that isn't a fix"
    )
    proving.add_argument("--jobs", type=int, default=1)
    proving.add_argument("--timeout", type=int, default=1800)
    proving.add_argument("--target-dir")
    args = parser.parse_args()
    try:
        root = repo_root()
        branch = Branch(root, args.base)
        if args.command == "check":
            return check(branch)
        if args.emit and not re.fullmatch(r"[a-z0-9][a-z0-9-]*", args.emit):
            raise Failure(f"--emit {args.emit}: an owner is a file name, [a-z0-9-]")
        if args.target_dir is None:
            target = Path(os.environ.get("CARGO_TARGET_DIR") or root / "target")
            args.target_dir = str(target / "guards-fire")
        return prove(branch, args)
    except Failure as failure:
        print(f"fail-to-pass: {failure}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
