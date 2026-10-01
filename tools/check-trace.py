#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
# SPDX-License-Identifier: Apache-2.0
"""Check that each traced requirement appears in every verification layer.

Requirements are defined in `verify/spec/core.symspec.json` and `verify/spec/agentd.symspec.json`. A
requirement is traced when its group's file in `verify/spec/traced/` lists it: one file per key prefix
the specs define (`CLI.toml` lists the CLI keys), so the changes that trace different groups
each edit their own file, and a group with no key traced yet has a file that lists nothing.
Each traced key must appear in six places, and this script reports where:

  model    a Stateright property whose name starts with the key (`crates/model/src/`)
  gherkin  a tag `@KEY` on a scenario (`<crate>/tests/features/*.feature`)
  fuzz     a harness the key names: the name or the `///` doc of the `#[test]` function
           that calls `bolero::check!`, or the doc above a top-level `fuzz_target!`
  test     a test the key names, in a file under a Rust crate's `tests/`, a source
           file, a file under `crates/microvms-cli/src/guards/`, or a binding test under
           `bindings/microvms-py/tests/` or `bindings/microvms-js/__test__/`. In Rust that's a name that
           starts with the key (`fn image_5_...`) or the `///` doc of a `#[test]` item,
           `proptest!` bodies included; in Python the `def test_image_5_...` name, a
           `@pytest.mark.req("IMAGE-5")` marker, or the function's own docstring; in Node
           the title of a `test`, `it`, `describe` or `suite` call that has a body. A
           test that never runs doesn't count: `#[ignore]` outside the live tier's
           `live_*.rs` files, a `cfg` that isn't a platform, pytest's `skip`, and Node's
           `.skip`, `.todo` or `{ skip: true }`
  impl     a mention in production Rust source of the CLI, core, the domain, the app,
           the edges, the daemon, protocol, or either binding
  live     a live conformance check whose name starts with the key, in any module of the
           suite under `conformance/` (`conformance/run_rs.py` and the packages it imports,
           run against AWS by `mise run live`)

A `//` or `//!` comment, a module docstring, a header comment and an assertion message don't
count for the test or fuzz layer: a file that mentioned a key and tested nothing would score
the same as one that tests it. ast-grep (pinned in `mise.toml`) and stdlib `ast` tell them
apart, so the script needs ast-grep on PATH and nothing from PyPI.

An entry is a TOML table named for its key, with the issue that traced it and, for each layer
the key waives, the reason:

  [IMAGE-1]
  issue = "#220"
  waive.live = "a pure function of Dockerfile text; it makes no AWS call"

A key waives a layer only with a reason, for example the live layer of a pure function that
makes no AWS call. The waiver and its reason are rendered in the matrix, so an absent layer is
a stated decision rather than a gap. The matrix lists the keys by group and then by number,
whatever order the files hold them in.

Every key a spec defines is traced: a key no group file lists fails, naming the file that would
list it, and so does a listed key that waives every layer, since no layer checks either. That's
the rule for tools/ratchet.py's untraced category, which is enforced, so it carries no drift.

`load_traced` reads the files, for this script and for tools/ratchet.py's untraced category,
and refuses to load a file that isn't named `<GROUP>.toml` for a group the specs define, a key
outside its file's group, a key listed twice (TOML refuses one repeated in a file, and the
loader one listed in two), an entry of any other shape, and a directory with no group file.
Each refusal names its file. This script reads past a refusal: it prints the refusals first and
still holds the entries the other files give, every layer and the threat table, so one bad file
doesn't hide another finding. Only the sentinel key, the keys no file lists and the rendered
matrix, which read every file, wait for a load with no refusal.

It also refuses a mention of an unknown key anywhere in a file it reads, comments included,
so a typo such as `CLI-10` for `CLI-9` cannot pass as coverage. Keys are recognized by the
prefixes the two specs define. And it refuses to pass on input it didn't read: every
directory it lists must yield a file and, unless KEYLESS says why not, a key; every layer's
collector must find a key; and the sentinel key must still be traced.
It also holds the threat table in `docs/TRUST.md` ("Threats and the tests that guard them") to
the specs and the tests. Each row names a threat, its requirement keys, its guards as
`path::test`, and a status. A key must be one a spec defines; a guard must be a test that runs,
by the rules above, in that Rust file, and its own name or doc must name one of the row's keys.
The file has to compile, too: a chain of `mod` declarations must reach it from a crate target
(`src/lib.rs`, `src/main.rs`, `src/bin/`, `tests/`), or its tests never run. A `guarded` row
names at least one of each, and a `known gap` names the issue that closes it. The table is one
block of rows under its header and delimiter row: a missing delimiter or a row past a break
fails rather than being skipped. A table that parses to no rows fails, and so does one without
the row naming THREAT_SENTINEL.

`docs/TRACEABILITY.md` is the rendered matrix, with the threat table after it:

  ./tools/check-trace.py           print the matrix; fail if a layer is missing
  ./tools/check-trace.py --write   also render docs/TRACEABILITY.md
  ./tools/check-trace.py --check   also fail if docs/TRACEABILITY.md is stale
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPECS = (
    ROOT / "verify" / "spec" / "core.symspec.json",
    ROOT / "verify" / "spec" / "agentd.symspec.json",
)
DOC = ROOT / "docs" / "TRACEABILITY.md"
TRUST = Path("docs") / "TRUST.md"
THREATS = "Threats and the tests that guard them"
THREAT_COLUMNS = ("Threat", "Requirement", "Guard", "Status")
# A row the threat table always carries, for SENTINEL's reason: a parser that kept only some rows
# (the first, or only the gaps) would leave the floor satisfied and the rest of the table unread.
THREAT_SENTINEL = "BIND-18"
# The live suite's entry point, and the directory whose modules are the suite: the entry and
# the packages beside it that it imports. A check counts in whichever module its call is in.
LIVE = ROOT / "conformance" / "run_rs.py"
LIVE_SUITE = ROOT / "conformance"

# One file per requirement group the specs define, each named for its group (`CLI.toml`).
# tools/ratchet.py runs this file with runpy to call `load_traced` and read `LAYERS` (its
# untraced category), so the module level imports only the standard library.
TRACED_DIR = ROOT / "verify" / "spec" / "traced"
# The fields an entry may hold, and the issue as the tracker spells it.
TRACED_FIELDS = {"issue", "waive"}
TRACED_ISSUE = re.compile(r"#\d+")

LAYERS = ("model", "gherkin", "fuzz", "test", "impl", "live")

# Production Rust source, and the directories whose every file is a test.
IMPL_DIRS = (
    "crates/microvms-cli/src",
    "crates/microvms-core/src",
    "crates/microvms-domain/src",
    "crates/microvms-app/src",
    "crates/microvms-edges/src",
    "crates/agentd/src",
    "crates/protocol/src",
    "bindings/microvms-py/src",
    "bindings/microvms-js/src",
)
RUST_TEST_DIRS = (
    "crates/microvms-cli/tests",
    "crates/microvms-core/tests",
    "crates/microvms-edges/tests",
    "crates/agentd/tests",
    "crates/agentd/fuzz/fuzz_targets",
    # The app's proof crate: its tests replay the models against `Sandbox` and the daemon.
    "crates/model-conformance/tests",
)
BINDING_TESTS = (
    ("bindings/microvms-py/tests", "*.py"),
    ("bindings/microvms-js/__test__", "*.mjs"),
)
# The CLI's in-crate guards: test code under `src/`, so none of it counts as production code.
CLI_GUARDS = Path("crates/microvms-cli/src/guards")

# Listed entries that yield files but no requirement key today, each with the reason.
# Every other entry must give up at least one key, because a directory that still has
# files but no keyed ones (tests moved into a subdirectory, leaving `conftest.py`) drops
# out of the matrix as quietly as one that vanished. An entry here that starts yielding
# a key is reported, so this list can only shrink.
KEYLESS = {
    "crates/agentd/fuzz/fuzz_targets": "the cargo-fuzz tar harness guards extraction, which no "
    "spec requirement covers; the keyed fuzz harnesses are bolero targets under src/ and "
    "tests/",
}

# A key the traced files always carry. The per-key loop in `main` checks every layer of
# every traced key, this one included, so a table that lost its entries (a group file
# emptied, or a loader that read none) would pass that loop vacuously; this is what notices.
SENTINEL = "CLI-7"

TEST_MODULE = re.compile(r"^#\[cfg\(test\)\]\s*$", re.MULTILINE)

# The ast-grep rules that find what names a test or a harness, written as JSON (which is YAML)
# so the fragments below can be shared between rules. ast-grep is pinned in mise.toml and
# installed by checksum in CI; tree-sitter underneath it is what tells a doc comment from a
# line comment and a test title from an assertion message.
#
# Attributes and comments are siblings of the item they sit on, so "on a test" means: in the
# run of attributes and comments that ends at the item, and the run carries a test attribute.
_RUN_ENDS = {
    "not": {
        "any": [
            {"kind": "attribute_item"},
            {"kind": "line_comment"},
            {"kind": "block_comment"},
        ]
    }
}


def _in_run(attribute: dict) -> dict:
    return {"follows": {"kind": "attribute_item", **attribute, "stopBy": _RUN_ENDS}}


# `bolero::check!`, `::bolero::check!`, or a bare `check!`. A bare one must be bolero's, imported
# by a `use`; `named_keys` refuses a file where it isn't, rather than guess which layer it is.
_BOLERO = {
    "kind": "macro_invocation",
    "has": {"field": "macro", "regex": r"^(?:(?:::\s*)?bolero\s*::\s*)?check$"},
}
_BARE_CHECK = {
    "kind": "macro_invocation",
    "has": {"field": "macro", "regex": r"^check$"},
}
_BOLERO_IMPORT = {
    "kind": "use_declaration",
    "regex": r"\bbolero\s*::\s*(?:check\b|\{[^}]*\bcheck\b|\*)",
}
_CALLS_BOLERO = {"has": {**_BOLERO, "stopBy": "end"}}
_FUZZ_TARGET = {
    "kind": "macro_invocation",
    "has": {"field": "macro", "regex": r"^(?:[A-Za-z_]\w*\s*::\s*)*fuzz_target$"},
}
# A test counts only if something runs it. `#[ignore]` runs only under `--ignored`, which the
# live tier passes for the `live_*.rs` files and nothing passes for any other file. A `cfg` on
# a test counts when it picks a platform CI runs; any other predicate (`any()`, a feature)
# could leave the test compiled out everywhere, so it doesn't.
_PLATFORM_CFG = (
    r"^#\[\s*cfg\s*\(\s*(?:not\s*\(\s*)?(?:unix|windows|test)\s*\)?\s*\)\s*\]$"
)
_OFF = [_in_run({"regex": r"^#\[\s*cfg\s*\(", "not": {"regex": _PLATFORM_CFG}})]
_IGNORED = [_in_run({"regex": r"^#\[\s*ignore\b"})]


def _runs(*off: dict) -> dict:
    return {
        "kind": "function_item",
        **_in_run({"regex": r"^#\[\s*(?:[A-Za-z_][A-Za-z0-9_]*\s*::\s*)*test\b"}),
        "not": {"any": list(off)},
    }


# A function that calls bolero is a harness, which counts for the fuzz layer and not the test
# layer, so one harness can't stand in for a test.
_TEST_FN = {"all": [_runs(*_OFF, *_IGNORED), {"not": _CALLS_BOLERO}]}
_LIVE_TEST_FN = {"all": [_runs(*_OFF), {"not": _CALLS_BOLERO}]}
_HARNESS_FN = {"all": [_runs(*_OFF, *_IGNORED), _CALLS_BOLERO]}
_LIVE = ["**/tests/live_*.rs"]
# `///`, `/** */` and `#[doc = ...]` are the item's documentation; `//`, `////`, `//!` and
# `/*! */` aren't.
_DOC = {
    "any": [
        {"kind": "line_comment", "regex": r"^///([^/]|$)"},
        {"kind": "block_comment", "regex": r"^/\*\*[^*/]"},
        {"kind": "attribute_item", "regex": r"^#\[\s*doc\s*="},
    ]
}
_NAME = {"has": {"field": "name", "pattern": "$NAME"}}
# `test`, `it`, `describe` and `suite`, or their `.only` forms. Only the bare call is a title:
# `re.test('IMAGE-5')` in a test body is an assertion, and `t.test` subtests aren't used here.
_JS_CALLEE = r"^(?:test|it|describe|suite)(?:\.only)?$"
# `.skip` and `.todo`, or `{ skip: true }` and `{ todo: 'why' }`, never run the body. A skip
# whose value is an expression (`process.platform === 'win32'`) runs somewhere, so it counts.
_JS_OFF = {
    "kind": "call_expression",
    "any": [
        {
            "has": {
                "field": "function",
                "regex": r"^(?:test|it|describe|suite)\.(?:skip|todo)$",
            }
        },
        {
            "all": [
                {"has": {"field": "function", "regex": _JS_CALLEE}},
                {
                    "has": {
                        "field": "arguments",
                        "has": {
                            "kind": "object",
                            "has": {
                                "kind": "pair",
                                "all": [
                                    {
                                        "has": {
                                            "field": "key",
                                            "regex": r"""^['"]?(?:skip|todo)['"]?$""",
                                        }
                                    },
                                    {
                                        "has": {
                                            "field": "value",
                                            "any": [
                                                {"kind": "true"},
                                                {"kind": "string"},
                                                {"kind": "template_string"},
                                            ],
                                        }
                                    },
                                ],
                            },
                        },
                    }
                },
            ]
        },
    ],
}
RULES = {
    "rust-test-name": ("Rust", {"all": [_TEST_FN, _NAME]}),
    "rust-test-doc": ("Rust", {**_DOC, "precedes": {**_TEST_FN, "stopBy": _RUN_ENDS}}),
    "rust-live-test-name": ("Rust", {"all": [_LIVE_TEST_FN, _NAME]}, _LIVE),
    "rust-live-test-doc": (
        "Rust",
        {**_DOC, "precedes": {**_LIVE_TEST_FN, "stopBy": _RUN_ENDS}},
        _LIVE,
    ),
    "rust-harness-name": ("Rust", {"all": [_HARNESS_FN, _NAME]}),
    # A top-level `fuzz_target!` has no function to name, so its own doc comment counts.
    "rust-harness-doc": (
        "Rust",
        {
            **_DOC,
            "precedes": {
                "any": [
                    _HARNESS_FN,
                    _FUZZ_TARGET,
                    {"kind": "expression_statement", "has": _FUZZ_TARGET},
                ],
                "stopBy": _RUN_ENDS,
            },
        },
    ),
    # A file with one of these is a fuzz file, whatever its comments say.
    "rust-fuzz-call": ("Rust", {"any": [_BOLERO, _FUZZ_TARGET]}),
    "rust-bare-check": ("Rust", _BARE_CHECK),
    "rust-bolero-import": ("Rust", _BOLERO_IMPORT),
    # `mod name;`, which makes the compiler read `name.rs` or `name/mod.rs`. A threat guard's
    # file has to be reached by a chain of these, or it never compiles and its tests never run.
    # One under a `cfg` that could be off everywhere doesn't count, and neither does one with a
    # `#[path]`, since the file it reads isn't the one its name spells.
    "rust-mod-decl": (
        "Rust",
        {
            "kind": "mod_item",
            **_NAME,
            "not": {
                "any": [
                    {"has": {"field": "body", "kind": "declaration_list"}},
                    *_OFF,
                    _in_run({"regex": r"^#\[\s*path\s*="}),
                ]
            },
        },
    ),
    # tree-sitter leaves a macro's body as unparsed tokens, so a `proptest! {}` body is
    # parsed again on its own to find the tests inside it.
    "rust-proptest": (
        "Rust",
        {
            "kind": "macro_invocation",
            "has": {"field": "macro", "regex": r"^(?:[A-Za-z_]\w*\s*::\s*)*proptest$"},
        },
    ),
    "js-test-title": (
        "JavaScript",
        {
            "kind": "call_expression",
            "all": [
                {"has": {"field": "function", "regex": _JS_CALLEE}},
                {
                    "has": {
                        "field": "arguments",
                        "has": {
                            "nthChild": {
                                "position": 1,
                                "ofRule": {"not": {"kind": "comment"}},
                            },
                            "any": [{"kind": "string"}, {"kind": "template_string"}],
                            "pattern": "$TITLE",
                        },
                    }
                },
                # A test has a body; a call with a title and no function is something else.
                {
                    "has": {
                        "field": "arguments",
                        "has": {
                            "any": [
                                {"kind": "arrow_function"},
                                {"kind": "function_expression"},
                            ]
                        },
                    }
                },
                {"not": _JS_OFF},
                {"not": {"inside": {**_JS_OFF, "stopBy": "end"}}},
            ],
        },
    ),
}


def spec_keys() -> dict[str, str]:
    """Every requirement key in either spec, with its EARS sentence."""
    keys: dict[str, str] = {}
    for spec in SPECS:
        document = json.loads(spec.read_text())
        for entry in document["requirements"].values():
            if entry.get("key"):
                keys[entry["key"]] = entry["sentence"]
    return keys


class Patterns:
    """The key-matching expressions, built from the prefixes the specs define."""

    def __init__(self, keys: dict[str, str]) -> None:
        prefixes = sorted({key.rsplit("-", 1)[0] for key in keys})
        key = rf"(?:{'|'.join(map(re.escape, prefixes))})-\d+"
        self.key = re.compile(rf"\b({key})\b")
        self.property = re.compile(
            rf'Property::(?:<\w+>::)?(?:always|sometimes|eventually)\(\s*"({key})\b'
        )
        self.tag = re.compile(rf"@({key})\b")
        # A key that starts a name: `fn image_5_...`, `def test_image_5_...`. Only the start
        # counts, because the prefixes are words: `fn builds_image_2_times` names no key.
        lowered = "|".join(re.escape(prefix.lower()) for prefix in prefixes)
        self.ident = re.compile(rf"^(?:test_)?({lowered})_(\d+)(?=_|$)")
        # A live conformance check whose name starts with the requirement key.
        self.live = re.compile(rf'results\.(?:check|eq|absent)\(\s*f?"({key})\b')

    def in_name(self, name: str) -> set[str]:
        return {
            f"{prefix.upper()}-{number}"
            for prefix, number in self.ident.findall(name.lower())
        }


@dataclass(frozen=True)
class Traced:
    """One entry in a group file: a requirement traced end to end."""

    #: The issue that traced the key.
    issue: str
    #: Each layer the key waives, with the reason.
    waive: dict[str, str]
    #: The file that lists it, relative to the root it was loaded under when it's in it.
    file: str


def _entry(value: object, file: str) -> Traced | None:
    """The entry a group file's table holds for a key, or None for any other shape."""
    if not isinstance(value, dict) or not set(value) <= TRACED_FIELDS:
        return None
    issue, waive = value.get("issue"), value.get("waive", {})
    if not isinstance(issue, str) or not TRACED_ISSUE.fullmatch(issue):
        return None
    if not isinstance(waive, dict) or not all(
        isinstance(reason, str) for reason in waive.values()
    ):
        return None
    return Traced(issue, dict(waive), file)


def load_traced(
    directory: Path, groups: set[str], root: Path = ROOT
) -> dict[str, Traced]:
    """Every entry in the group files in `directory`, by key, in key order.

    `groups` is the key prefixes the specs define. A file that's wrong fails the load rather
    than dropping out of it, since a group that loaded nothing would pass every per-key check
    and read as fewer keys to trace. So every problem is collected, each naming its file, and
    raised together. A file is named relative to `root`, or in full when it isn't under it.
    """
    traced, problems = read_traced(directory, groups, root)
    if problems:
        raise SystemExit("\n".join(f"trace: {problem}" for problem in problems))
    return traced


def read_traced(
    directory: Path, groups: set[str], root: Path = ROOT
) -> tuple[dict[str, Traced], list[str]]:
    """The entries `load_traced` reads, and every problem it would raise, each naming its file.

    What a problem is about gives up no entry, and the rest still do: a file that doesn't
    parse or isn't named for a group gives up none, and a malformed entry or a key outside its
    file's group gives up that key there. A key listed in two files keeps the entry in its own
    group's file. So `main` can hold the entries that loaded while the problems fail the run.
    """

    def shown(path: Path) -> str:
        return rel(path, root) if path.is_relative_to(root) else path.as_posix()

    problems: list[str] = []
    listed: dict[str, list[Traced]] = {}
    read = 0
    try:
        paths = sorted(directory.iterdir())
    except FileNotFoundError:
        paths = []
    for path in paths:
        where = shown(path)
        group = path.stem
        # Only `<GROUP>.toml` is read, so anything else here is refused rather than skipped: a
        # misspelled name or suffix would take its group's keys out of the matrix.
        if not (path.is_file() and path.suffix == ".toml" and group in groups):
            problems.append(
                f"{where} isn't named for a requirement group the specs define: a group file "
                f"is <GROUP>.toml, for one of {', '.join(sorted(groups))}"
            )
            continue
        # TOML refuses a key declared twice in one file, so that duplicate fails here.
        try:
            table = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as error:
            problems.append(f"{where} doesn't parse: {error}")
            continue
        read += 1
        for key, value in table.items():
            if not re.fullmatch(rf"{re.escape(group)}-\d+", key):
                problems.append(
                    f"{where}: {key} isn't in the {group} group; a group file lists only its "
                    "group's keys"
                )
            entry = _entry(value, where)
            if entry is None:
                problems.append(
                    f'{where}: {key} is malformed: an entry holds `issue = "#N"` and, for '
                    'each layer it waives, `waive.<layer> = "reason"`, and nothing else'
                )
                continue
            listed.setdefault(key, []).append(entry)
    for key, entries in listed.items():
        if len(entries) > 1:
            files = ", ".join(entry.file for entry in entries)
            problems.append(f"{key} is listed in more than one file: {files}")
    if not read:
        problems.append(
            f"{shown(directory)} holds no group file, so no requirement is traced"
        )

    def order(key: str) -> tuple[str, int]:
        group, number = key.rsplit("-", 1)
        return group, int(number)

    # A key outside its file's group still counts toward listing a key twice, above, but it
    # isn't that file's to trace.
    kept = {
        key: own[0]
        for key, entries in listed.items()
        if (
            own := [
                entry
                for entry in entries
                if re.fullmatch(rf"{re.escape(Path(entry.file).stem)}-\d+", key)
            ]
        )
    }
    return {key: kept[key] for key in sorted(kept, key=order)}, problems


def rel(path: Path, root: Path = ROOT) -> str:
    return path.relative_to(root).as_posix()


def live_files(root: Path = ROOT) -> list[Path]:
    """The live suite's Python modules: its entry point and every module beside it."""
    return sorted((root / LIVE_SUITE.relative_to(ROOT)).rglob("*.py"))


def rust_files(*directories: str, root: Path = ROOT) -> list[Path]:
    files: list[Path] = []
    for directory in directories:
        files.extend(sorted((root / directory).rglob("*.rs")))
    return [path for path in files if "target" not in path.relative_to(root).parts]


def enumerator_floors(root: Path = ROOT) -> list[str]:
    """A listed directory that yields no file, which would drop its layer silently.

    A renamed `bindings/microvms-js/__test__` would take every Node test out of the matrix, and
    only a key covered by Node tests alone would notice.
    """
    problems: list[str] = []
    for table, directories in (
        ("IMPL_DIRS", IMPL_DIRS),
        ("RUST_TEST_DIRS", RUST_TEST_DIRS),
    ):
        for directory in directories:
            if not rust_files(directory, root=root):
                problems.append(f"{table} entry {directory!r} yields no .rs file")
    for directory, pattern in BINDING_TESTS:
        if not sorted((root / directory).glob(pattern)):
            problems.append(
                f"BINDING_TESTS entry ({directory!r}, {pattern!r}) yields no file"
            )
    live = root / LIVE.relative_to(ROOT)
    if not live.is_file():
        problems.append(f"the live suite {rel(live, root)} doesn't exist")
    return problems


def layer_floors(
    found: dict[str, dict[str, set[str]]],
    traced: dict[str, Traced],
    whole: bool = True,
) -> list[str]:
    """A layer or a listed entry that gave up no key, and traced files without the sentinel.

    The sentinel proves the traced files were all read, so it isn't held when they weren't
    (`whole` false): their refusal fails the run already, and says which file it was.
    """
    problems = [
        f"the {layer} collector found no requirement key in any file it read"
        for layer in LAYERS
        if not any(layers[layer] for layers in found.values())
    ]
    keyed = {
        path for layers in found.values() for paths in layers.values() for path in paths
    }
    entries = [*IMPL_DIRS, *RUST_TEST_DIRS]
    entries += [f"{directory} ({pattern})" for directory, pattern in BINDING_TESTS]
    for entry in entries:
        directory = entry.split(" ", 1)[0]
        yields = any(path.startswith(f"{directory}/") for path in keyed)
        if entry in KEYLESS or directory in KEYLESS:
            if yields:
                problems.append(
                    f"{directory} yields a requirement key now; drop it from KEYLESS"
                )
        elif not yields:
            problems.append(f"the entry {entry} yields files but no requirement key")
    if whole and SENTINEL not in traced:
        home = TRACED_DIR / f"{SENTINEL.rsplit('-', 1)[0]}.toml"
        problems.append(f"the sentinel {SENTINEL} is not in {rel(home)}")
    return problems


def untraced(
    sentences: dict[str, str], traced: dict[str, Traced], whole: bool = True
) -> list[str]:
    """Each requirement no layer checks: a spec key no group file lists, and a listed key that
    waives every layer.

    The keys no file lists wait for a whole load (`whole`), as the sentinel does: a refused
    file's keys would all read as unlisted, repeating a refusal that fails the run already.
    """
    problems = [
        f"{entry.file}: {key} waives every layer, so no layer checks it: trace at least one"
        for key, entry in traced.items()
        if set(LAYERS) <= set(entry.waive)
    ]
    if whole:
        problems += [
            f"{key} is defined in a spec, but no file in {rel(TRACED_DIR)}/ lists it: list it "
            f"in {rel(TRACED_DIR / (key.rpartition('-')[0] + '.toml'))} with each layer, or a "
            "waiver and its reason"
            for key in sentences
            if key not in traced
        ]
    return problems


def split_test_region(text: str) -> tuple[str, str]:
    """A source file's production text and its trailing `#[cfg(test)]` module, if any."""
    for match in TEST_MODULE.finditer(text):
        following = text[match.end() :].lstrip()
        if following.startswith("mod ") and not following.split("\n", 1)[
            0
        ].rstrip().endswith(";"):
            return text[: match.start()], text[match.start() :]
    return text, ""


def ast_grep(files: list[Path], root: Path) -> list[dict]:
    """Every match of `RULES` in `files`, which are under `root`."""
    if not files:
        return []
    # ast-grep honors a suppression comment even for inline rules, and no flag turns that
    # off, so one `// ast-grep-ignore` line could hide a harness or move a key between layers.
    for path in files:
        if "ast-grep-ignore" in path.read_text(encoding="utf-8"):
            raise SystemExit(
                f"trace: {rel(path, root)} has an ast-grep-ignore comment, which would hide "
                "it from the rules that find tests"
            )
    inline = "\n---\n".join(
        json.dumps(
            {"id": rule, "language": language, "rule": body}
            | ({"files": globs[0]} if globs else {})
        )
        for rule, (language, body, *globs) in RULES.items()
    )
    try:
        out = subprocess.run(
            ["ast-grep", "scan", "--inline-rules", inline, "--json=stream"]
            + [rel(path, root) for path in files],
            cwd=root,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        raise SystemExit(
            "trace: ast-grep isn't on PATH; run this through `mise run trace:check`"
        ) from None
    if out.returncode != 0:
        raise SystemExit(f"trace: ast-grep failed in {root}:\n{out.stderr}")
    return [json.loads(line) for line in out.stdout.splitlines() if line.strip()]


@dataclass
class Named:
    """The keys that name a file's tests and its fuzz harnesses.

    A harness never counts as a test, so a fuzz file's other `#[test]`s land in `test` and
    its harnesses in `fuzz`.
    """

    test: set[str] = field(default_factory=set)
    fuzz: set[str] = field(default_factory=set)
    #: The file calls bolero or `fuzz_target!`.
    fuzz_file: bool = False


def named_keys(patterns: Patterns, files: list[Path], root: Path) -> dict[Path, Named]:
    """For each Rust or Node file, the keys that name its tests and its fuzz harnesses."""
    named = {path: Named() for path in files}
    bodies: list[tuple[Path, str]] = []
    bare: set[Path] = set()
    imports: set[Path] = set()
    for match in ast_grep(files, root):
        path = root / match["file"]
        rule = match["ruleId"]
        name = match.get("metaVariables", {}).get("single", {})
        if rule == "rust-bare-check":
            bare.add(path)
        elif rule == "rust-bolero-import":
            imports.add(path)
        elif rule == "rust-fuzz-call":
            named[path].fuzz_file = True
        elif rule == "rust-proptest":
            text = match["text"]
            bodies.append((path, text[text.index("!") + 1 :].strip()[1:-1]))
        elif rule in ("rust-test-name", "rust-live-test-name"):
            named[path].test |= patterns.in_name(name["NAME"]["text"])
        elif rule == "rust-harness-name":
            named[path].fuzz |= patterns.in_name(name["NAME"]["text"])
        elif rule in ("rust-test-doc", "rust-live-test-doc"):
            named[path].test |= set(patterns.key.findall(match["text"]))
        elif rule == "rust-harness-doc":
            named[path].fuzz |= set(patterns.key.findall(match["text"]))
        elif rule == "js-test-title":
            named[path].test |= set(patterns.key.findall(name["TITLE"]["text"]))
    if stray := sorted(bare - imports):
        raise SystemExit(
            f"trace: {rel(stray[0], root)} calls a bare check! without importing bolero's; "
            "spell it bolero::check! so the file is read as a fuzz harness"
        )
    if bodies:
        with tempfile.TemporaryDirectory() as scratch:
            origin: dict[Path, Path] = {}
            for index, (path, body) in enumerate(bodies):
                copy = Path(scratch) / f"proptest_{index}.rs"
                copy.write_text(body, encoding="utf-8")
                origin[copy] = path
            for copy, keys in named_keys(patterns, list(origin), Path(scratch)).items():
                named[origin[copy]].test |= keys.test
    return named


def _dotted(node: ast.expr) -> str:
    """`pytest.mark.skip` for the decorator `@pytest.mark.skip(reason=...)`."""
    if isinstance(node, ast.Call):
        node = node.func
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _marker(node: ast.expr, name: str) -> bool:
    """Whether `node` is the pytest marker `name`, spelled `pytest.mark.x` or `mark.x`."""
    return _dotted(node) in (f"pytest.mark.{name}", f"mark.{name}")


def _skipped(decorators: list[ast.expr]) -> bool:
    # `skipif` and `xfail` still run on some platform or report a result; `skip` never runs.
    return any(_marker(decorator, "skip") for decorator in decorators)


def python_test_keys(patterns: Patterns, text: str, path: str) -> set[str]:
    """The keys naming a pytest test: its name, a `req` marker, or its own docstring.

    A test is what pytest collects by default: a module-level `test*` function, or a `test*`
    method of a `Test*` class without `__init__`. One marked `skip`, on itself, its class or
    the module's `pytestmark`, never runs, so it doesn't count. Only the `req` marker names a
    key: a `parametrize` case or a `skipif` reason is a literal like any other.
    """
    try:
        module = ast.parse(text, filename=path)
    except SyntaxError as err:
        raise SystemExit(f"trace: {path} doesn't parse: {err}") from None

    def is_test(node: ast.stmt) -> bool:
        return isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            node.name.startswith("test")
        )

    for node in module.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "pytestmark"
            for target in node.targets
        ):
            marks = (
                node.value.elts
                if isinstance(node.value, (ast.List, ast.Tuple))
                else [node.value]
            )
            if _skipped(marks):
                return set()
    tests: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for node in module.body:
        if is_test(node):
            tests.append(node)
        elif (
            isinstance(node, ast.ClassDef)
            and node.name.startswith("Test")
            and not _skipped(node.decorator_list)
            and not any(
                isinstance(member, ast.FunctionDef) and member.name == "__init__"
                for member in node.body
            )
        ):
            tests += [member for member in node.body if is_test(member)]
    keys: set[str] = set()
    for test in tests:
        if _skipped(test.decorator_list):
            continue
        keys |= patterns.in_name(test.name)
        for decorator in test.decorator_list:
            if isinstance(decorator, ast.Call) and _marker(decorator, "req"):
                for argument in decorator.args:
                    if isinstance(argument, ast.Constant) and isinstance(
                        argument.value, str
                    ):
                        keys |= set(patterns.key.findall(argument.value))
        keys |= set(patterns.key.findall(ast.get_docstring(test) or ""))
    return keys


def collect(patterns: Patterns, root: Path = ROOT) -> dict[str, dict[str, set[str]]]:
    """For every key found anywhere, the files each layer found it in.

    The test and fuzz layers count a key only where it names a test or a harness. The impl
    layer counts any mention in production code: there a key is a pointer for readers, not
    a claim that something checks it.
    """
    found: dict[str, dict[str, set[str]]] = {}

    def note(key: str, layer: str, path: Path) -> None:
        found.setdefault(key, {layer: set() for layer in LAYERS})[layer].add(
            rel(path, root)
        )

    for path in rust_files("crates/model/src", root=root):
        for key in patterns.property.findall(path.read_text()):
            note(key, "model", path)

    for path in sorted(root.glob("crates/*/tests/features/*.feature")):
        for key in patterns.tag.findall(path.read_text()):
            note(key, "gherkin", path)

    rust_tests = rust_files(*RUST_TEST_DIRS, root=root)
    rust = rust_files(*IMPL_DIRS, root=root) + rust_tests
    bindings = [
        path
        for directory, pattern in BINDING_TESTS
        for path in sorted((root / directory).glob(pattern))
    ]
    named = named_keys(
        patterns, rust + [p for p in bindings if p.suffix == ".mjs"], root
    )

    for path in rust:
        for key in named[path].test:
            note(key, "test", path)
        for key in named[path].fuzz:
            note(key, "fuzz", path)
        # A fuzz file is test scaffolding, even under `src/`, so it isn't production code.
        if (
            named[path].fuzz_file
            or path in rust_tests
            or path.is_relative_to(root / CLI_GUARDS)
        ):
            continue
        production, _ = split_test_region(path.read_text())
        for key in patterns.key.findall(production):
            note(key, "impl", path)

    for path in bindings:
        if path.suffix == ".py":
            keys = python_test_keys(patterns, path.read_text(), rel(path, root))
        else:
            keys = named[path].test
        for key in keys:
            note(key, "test", path)

    for live in live_files(root):
        for key in patterns.live.findall(live.read_text()):
            note(key, "live", live)
    return found


def mentions(patterns: Patterns, root: Path = ROOT) -> dict[str, set[str]]:
    """Every key written anywhere in a file a layer reads, comments included.

    Only for refusing unknown keys: a typo in a comment is still a typo.
    """
    files = rust_files(*IMPL_DIRS, *RUST_TEST_DIRS, root=root)
    files += [
        path
        for directory, pattern in BINDING_TESTS
        for path in sorted((root / directory).glob(pattern))
    ]
    seen: dict[str, set[str]] = {}
    for path in files:
        for key in patterns.key.findall(path.read_text()):
            seen.setdefault(key, set()).add(rel(path, root))
    return seen


@dataclass
class Threat:
    """One row of the threat table in `docs/TRUST.md`."""

    line: int
    threat: str
    keys: list[str]
    guards: list[str]
    status: str


_CELL_ITEM = re.compile(r"^`([^`]+)`$")
_GUARD = re.compile(r"^(?P<path>[^:`\s]+\.rs)::(?P<name>[A-Za-z_][A-Za-z0-9_]*)$")
_ISSUE = re.compile(r"#\d+\b")
_DELIMITER = re.compile(r"^\|(?:\s*:?-{3,}:?\s*\|){%d}$" % len(THREAT_COLUMNS))


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _names(cell: str) -> list[str] | None:
    """`none` as no names, a comma-separated list of backticked names, or None for anything else."""
    if cell == "none":
        return []
    items = [_CELL_ITEM.match(item.strip()) for item in cell.split(",")]
    if not all(items):
        return None
    return [item.group(1) for item in items]


def parse_threats(text: str) -> tuple[list[Threat], list[str]]:
    """The rows of the threat table, and what's wrong with its shape."""
    where = TRUST.as_posix()
    lines = text.splitlines()
    heading = f"## {THREATS}"
    try:
        start = lines.index(heading)
    except ValueError:
        return [], [f"{where} has no {THREATS!r} section"]
    # The table is the first block of `|` lines in the section. A `|` line past the end of that
    # block is reported rather than skipped, since a blank line inside the table would otherwise
    # hide every row after it.
    table: list[tuple[int, str]] = []
    stray: list[int] = []
    ended = False
    for number, line in enumerate(lines[start + 1 :], start=start + 2):
        if line.startswith("## "):
            break
        if not line.startswith("|"):
            ended = ended or bool(table)
        elif ended:
            stray.append(number)
        else:
            table.append((number, line))
    if not table:
        return [], []
    header_line, header = table[0]
    if tuple(_cells(header)) != THREAT_COLUMNS:
        expected = "| " + " | ".join(THREAT_COLUMNS) + " |"
        return [], [
            f"{where}:{header_line}: the threat table's header is {header!r}, not {expected!r}"
        ]
    rows: list[Threat] = []
    problems = [
        f"{where}:{number}: this row is past a break in the threat table, so the table "
        "ends before it; keep the table one unbroken block of rows"
        for number in stray
    ]
    # Without the delimiter row Markdown renders no table, and reading the first row as the
    # delimiter would drop it unchecked, so the rows are read from the line after the header.
    delimited = len(table) > 1 and _DELIMITER.match(table[1][1].strip())
    if not delimited:
        problems.append(
            f"{where}:{header_line + 1}: the line after the threat table's header isn't its "
            "delimiter row, `|---|---|---|---|`"
        )
    body = table[2:] if delimited else table[1:]
    for number, line in body:
        cells = _cells(line)
        if len(cells) != len(THREAT_COLUMNS):
            problems.append(
                f"{where}:{number}: the threat row has {len(cells)} cells, not "
                f"{len(THREAT_COLUMNS)}"
            )
            continue
        threat, key_cell, guard_cell, status = cells
        keys, guards = _names(key_cell), _names(guard_cell)
        for column, names in (("Requirement", keys), ("Guard", guards)):
            if names is None:
                problems.append(
                    f"{where}:{number}: the {column} column isn't `none` or a list of "
                    "backticked names"
                )
        rows.append(Threat(number, threat, keys or [], guards or [], status))
    return rows, problems


def guard_tests(
    patterns: Patterns, files: list[Path], root: Path
) -> dict[str, dict[str, set[str]]]:
    """For each Rust file, its tests that run, each with the keys its name or own doc names.

    The same ast-grep rules the test and fuzz layers use, so a guard counts exactly when a test
    would: `#[ignore]` and non-platform `cfg`s are out, and a harness is a test here. A doc is
    the item's when the item is the first test after it, since the rules only match a doc in
    the run of attributes and comments that ends at a test.
    """
    tests: dict[str, list[tuple[int, str]]] = {}
    docs: dict[str, list[tuple[int, str]]] = {}
    for match in ast_grep(files, root):
        rule, path = match["ruleId"], match["file"]
        if rule in ("rust-test-name", "rust-live-test-name", "rust-harness-name"):
            name = match["metaVariables"]["single"]["NAME"]["text"]
            tests.setdefault(path, []).append((match["range"]["start"]["line"], name))
        elif rule in ("rust-test-doc", "rust-live-test-doc", "rust-harness-doc"):
            docs.setdefault(path, []).append(
                (match["range"]["end"]["line"], match["text"])
            )
    named: dict[str, dict[str, set[str]]] = {}
    for path, found in tests.items():
        found.sort()
        named[path] = {name: patterns.in_name(name) for _, name in found}
        for end, text in docs.get(path, []):
            following = [name for start, name in found if start > end]
            if following:
                named[path][following[0]] |= set(patterns.key.findall(text))
    return named


def _declared_by(path: str, root: Path) -> tuple[str, list[str]] | None:
    """The module name a Rust file compiles as, and the files a `mod` for it could be in.

    None for a file that's a crate target of its own: `src/lib.rs`, `src/main.rs`,
    `src/bin/*.rs`, and a file directly in a `tests/` directory.
    """
    file = Path(path)
    parent = file.parent
    if parent.name == "tests" or (parent.name == "bin" and parent.parent.name == "src"):
        return None
    if parent.name == "src" and file.name in ("lib.rs", "main.rs"):
        return None
    name, where = (
        (parent.name, parent.parent) if file.name == "mod.rs" else (file.stem, parent)
    )
    if where.name == "src":
        return name, [(where / "lib.rs").as_posix(), (where / "main.rs").as_posix()]
    if where.name == "tests":
        # A `tests/<dir>/mod.rs` helper is declared by the test targets beside it.
        return name, sorted(rel(target, root) for target in (root / where).glob("*.rs"))
    return name, [
        (where.parent / f"{where.name}.rs").as_posix(),
        (where / "mod.rs").as_posix(),
    ]


def unreached(paths: list[str], root: Path) -> dict[str, str]:
    """Each Rust file in `paths` no chain of `mod` declarations reaches from a crate target.

    The value names the first declaration missing on the way up. A file only a `#[cfg]`-gated
    or `#[path]` declaration reaches counts as unreached (see the `rust-mod-decl` rule).
    """
    parents: set[str] = set()
    todo, seen = list(paths), set()
    while todo:
        path = todo.pop()
        if path in seen:
            continue
        seen.add(path)
        up = _declared_by(path, root)
        for parent in up[1] if up else []:
            if (root / parent).is_file():
                parents.add(parent)
                todo.append(parent)
    declared: dict[str, set[str]] = {}
    for match in ast_grep([root / path for path in sorted(parents)], root):
        if match["ruleId"] == "rust-mod-decl":
            name = match["metaVariables"]["single"]["NAME"]["text"]
            declared.setdefault(match["file"], set()).add(name)

    def missing(path: str) -> str | None:
        up = _declared_by(path, root)
        if up is None:
            return None
        name, candidates = up
        above = [parent for parent in candidates if name in declared.get(parent, ())]
        if not above:
            return f"no `mod {name};` in {' or '.join(candidates) or 'a test target'}"
        reasons = [missing(parent) for parent in above]
        return None if None in reasons else reasons[0]

    return {path: why for path in paths if (why := missing(path))}


def check_threats(
    patterns: Patterns, sentences: dict[str, str], root: Path = ROOT
) -> tuple[list[Threat], list[str]]:
    """The threat table's rows, and each row whose key or guard doesn't resolve."""
    where = TRUST.as_posix()
    try:
        text = (root / TRUST).read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], [f"{where} doesn't exist"]
    rows, problems = parse_threats(text)
    guards = {guard: _GUARD.match(guard) for row in rows for guard in row.guards}
    files = sorted(
        {root / match["path"] for match in guards.values() if match}, key=str
    )
    present = [path for path in files if path.is_file()]
    tests = guard_tests(patterns, present, root)
    orphans = unreached([rel(path, root) for path in present], root)
    for row in rows:
        at = f"{where}:{row.line}"
        if row.status.startswith("guarded"):
            if not row.keys or not row.guards:
                problems.append(f"{at}: a guarded row names a key and a guard")
        elif row.status.startswith("known gap"):
            if not _ISSUE.search(row.status):
                problems.append(f"{at}: a known gap names the issue that closes it")
        else:
            problems.append(f"{at}: the status starts with 'guarded' or 'known gap'")
        for key in row.keys:
            if key not in sentences:
                problems.append(f"{at}: {key} is not defined in either spec")
        for guard in row.guards:
            match = guards[guard]
            if not match:
                problems.append(
                    f"{at}: the Guard column isn't `none` or a list of backticked names "
                    f"(`path.rs::test`): {guard!r}"
                )
                continue
            path, name = match["path"], match["name"]
            if not (root / path).is_file():
                problems.append(
                    f"{at}: the guard {guard} names {path}, which doesn't exist"
                )
            elif path in orphans:
                problems.append(
                    f"{at}: the guard {guard} is in {path}, which no `mod` declaration reaches "
                    f"from its crate, so it never compiles: {orphans[path]}"
                )
            elif name not in tests.get(path, {}):
                problems.append(
                    f"{at}: the guard {guard} isn't a test that runs in {path}"
                )
            elif not tests[path][name] & set(row.keys):
                problems.append(
                    f"{at}: the guard {guard} doesn't name {', '.join(row.keys) or 'a key'}"
                )
    if not rows:
        problems.append(f"the threat table in {where} parses to no rows")
    elif not any(THREAT_SENTINEL in row.keys for row in rows):
        problems.append(
            f"the sentinel row naming {THREAT_SENTINEL} is not in the threat table in {where}"
        )
    return rows, problems


def gaps(found: dict[str, dict[str, set[str]]], traced: dict[str, Traced]) -> list[str]:
    """Each layer a traced key neither covers nor waives."""
    return [
        f"{key} has no {layer} layer"
        for key, entry in traced.items()
        for layer in LAYERS
        if layer not in entry.waive and not found.get(key, {}).get(layer)
    ]


def cell(
    found: dict[str, dict[str, set[str]]],
    traced: dict[str, Traced],
    key: str,
    layer: str,
) -> str:
    if layer in traced[key].waive:
        return "waived"
    return str(len(found.get(key, {}).get(layer, ())))


def render(
    found: dict[str, dict[str, set[str]]],
    sentences: dict[str, str],
    traced: dict[str, Traced],
    threats: list[Threat] | None = None,
) -> str:
    lines = [
        "# Requirement traceability",
        "",
        "Generated by `./tools/check-trace.py --write`; `mise run trace:check` fails when",
        "this file is stale, a requirement is in no file under `verify/spec/traced/`, or a",
        "traced requirement is missing a layer. Requirements are defined in",
        "`verify/spec/core.symspec.json` and `verify/spec/agentd.symspec.json`.",
        "",
        "| Requirement | " + " | ".join(LAYERS) + " |",
        "|---|" + "---|" * len(LAYERS),
    ]
    for key in traced:
        cells = [cell(found, traced, key, layer) for layer in LAYERS]
        lines.append(f"| {key} | " + " | ".join(cells) + " |")
    for key, entry in traced.items():
        lines += ["", f"## {key}", "", sentences[key], ""]
        for layer in LAYERS:
            files = sorted(found.get(key, {}).get(layer, ()))
            listed = ", ".join(f"`{f}`" for f in files)
            reason = entry.waive.get(layer)
            if reason:
                listed = f"waived: {reason}" + (f" ({listed})" if listed else "")
            lines.append(f"- **{layer}:** " + (listed or "none"))
    lines += [
        "",
        "## Threats",
        "",
        f"The threat table in `{TRUST.as_posix()}`, each key marked when this matrix doesn't",
        "trace it.",
        "",
        "| " + " | ".join(THREAT_COLUMNS) + " |",
        "|---|" + "---|" * (len(THREAT_COLUMNS) - 1),
    ]
    for row in threats or []:
        keys = ", ".join(
            key if key in traced else f"{key} (not traced)" for key in row.keys
        )
        guards = ", ".join(f"`{guard}`" for guard in row.guards)
        lines.append(
            f"| {row.threat} | {keys or 'none'} | {guards or 'none'} | {row.status} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--write", action="store_true", help="render docs/TRACEABILITY.md"
    )
    mode.add_argument("--check", action="store_true", help="fail if the doc is stale")
    args = parser.parse_args()

    sentences = spec_keys()
    patterns = Patterns(sentences)
    traced, refused = read_traced(
        TRACED_DIR, {key.rsplit("-", 1)[0] for key in sentences}
    )
    # A traced file's problems print first, as they did when they ended the run. The layers,
    # the other files' entries and the threat table don't read that file, so they're still held.
    for problem in refused:
        print(f"trace: {problem}", file=sys.stderr)
    found = collect(patterns)
    problems = enumerator_floors() + layer_floors(found, traced, whole=not refused)
    problems += untraced(sentences, traced, whole=not refused)

    for key, entry in traced.items():
        if key not in sentences:
            problems.append(
                f"{entry.file}: {key} is traced but not defined in either spec"
            )
        for layer, reason in entry.waive.items():
            if layer not in LAYERS or not reason.strip():
                problems.append(
                    f"{entry.file}: {key} waives {layer!r} without a known layer and reason"
                )
    written = mentions(patterns)
    for key, layers in found.items():
        written.setdefault(key, set()).update(*layers.values())
    for key, where in sorted(written.items()):
        if key not in sentences:
            where = sorted(where)
            problems.append(
                f"{key} is mentioned but not defined in the spec: {', '.join(where)}"
            )
    problems += gaps(found, traced)
    threats, threat_problems = check_threats(patterns, sentences)
    problems += threat_problems

    width = max(len(layer) for layer in LAYERS)
    print("requirement  " + "  ".join(layer.ljust(width) for layer in LAYERS))
    for key in traced:
        counts = [cell(found, traced, key, layer).ljust(width) for layer in LAYERS]
        print(f"{key:<11}  " + "  ".join(counts))
    print(f"\nthreats in {TRUST.as_posix()}, by line")
    for row in threats:
        status = "guarded" if row.status.startswith("guarded") else "gap"
        print(f"{row.line:<11}  {status:<7}  {', '.join(row.keys) or 'none'}")

    # The matrix is rendered from every traced file and the specs' sentences, so a refused file
    # or a traced key no spec defines leaves nothing to compare the doc with. Any other problem
    # leaves the render whole, and a stale doc is a finding beside it.
    rendered = (
        render(found, sentences, traced, threats)
        if not refused and all(key in sentences for key in traced)
        else ""
    )
    if args.write and rendered:
        DOC.write_text(rendered)
        print(f"wrote {rel(DOC)}")
    if args.check and rendered and (not DOC.exists() or DOC.read_text() != rendered):
        problems.append(f"{rel(DOC)} is stale: run ./tools/check-trace.py --write")

    for problem in problems:
        print(f"trace: {problem}", file=sys.stderr)
    return 1 if problems or refused else 0


if __name__ == "__main__":
    sys.exit(main())
