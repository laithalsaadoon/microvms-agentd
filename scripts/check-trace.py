#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
# SPDX-License-Identifier: Apache-2.0
"""Check that each traced requirement appears in every verification layer.

Requirements are defined in `spec/core.symspec.json` and `spec/agentd.symspec.json`. A
requirement is traced when it is listed in `TRACED` below. Each traced key must appear
in six places, and this script reports where:

  model    a Stateright property whose name starts with the key (`model/src/`)
  gherkin  a tag `@KEY` on a scenario (`<crate>/tests/features/*.feature`)
  fuzz     a harness the key names: the name or the `///` doc of the `#[test]` function
           that calls `bolero::check!`, or the doc above a top-level `fuzz_target!`
  test     a test the key names, in a file under a Rust crate's `tests/`, a source
           file, `microvms-cli/src/guards.rs`, or a binding test under
           `microvms-py/tests/` or `microvms-js/__test__/`. In Rust that's a name that
           starts with the key (`fn image_5_...`) or the `///` doc of a `#[test]` item,
           `proptest!` bodies included; in Python the `def test_image_5_...` name, a
           `@pytest.mark.req("IMAGE-5")` marker, or the function's own docstring; in Node
           the title of a `test`, `it`, `describe` or `suite` call that has a body. A
           test that never runs doesn't count: `#[ignore]` outside the live tier's
           `live_*.rs` files, a `cfg` that isn't a platform, pytest's `skip`, and Node's
           `.skip`, `.todo` or `{ skip: true }`
  impl     a mention in production Rust source of the CLI, core, the domain, the app,
           the edges, the daemon, protocol, or either binding
  live     a live conformance check whose name starts with the key
           (`conformance/run_rs.py`, run against AWS by `mise run live`)

A `//` or `//!` comment, a module docstring, a header comment and an assertion message don't
count for the test or fuzz layer: a file that mentioned a key and tested nothing would score
the same as one that tests it. ast-grep (pinned in `mise.toml`) and stdlib `ast` tell them
apart, so the script needs ast-grep on PATH and nothing from PyPI.

A traced key may waive a layer with a reason, for example the live layer of a pure
function that makes no AWS call. The waiver and its reason are rendered in the matrix,
so an absent layer is a stated decision rather than a gap.

It also refuses a mention of an unknown key anywhere in a file it reads, comments included,
so a typo such as `CLI-10` for `CLI-9` cannot pass as coverage. Keys are recognized by the
prefixes the two specs define. And it refuses to pass on input it didn't read: every
directory it lists must yield a file and, unless KEYLESS says why not, a key; every layer's
collector must find a key; and the sentinel key must still be in TRACED.
`docs/TRACEABILITY.md` is the rendered matrix:

  ./scripts/check-trace.py           print the matrix; fail if a layer is missing
  ./scripts/check-trace.py --write   also render docs/TRACEABILITY.md
  ./scripts/check-trace.py --check   also fail if docs/TRACEABILITY.md is stale
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPECS = (
    ROOT / "spec" / "core.symspec.json",
    ROOT / "spec" / "agentd.symspec.json",
)
DOC = ROOT / "docs" / "TRACEABILITY.md"
LIVE = ROOT / "conformance" / "run_rs.py"

# The fuzz waiver the ensure_image decisions share: their input space is interleavings.
INTERLEAVINGS = (
    "the input space is two callers interleaved against the platform, which "
    "model/src/image.rs checks exhaustively; the decision table is ten rows, all pinned "
    "by the_plan_table"
)

# Requirements traced end to end. The value is the issue that introduced the key, or
# `(issue, {layer: reason})` for a key that waives a layer; see the module docs.
# scripts/ratchet.py runs this file with runpy to read `TRACED` and `LAYERS` (its untraced
# category), so the module level imports only the standard library.
TRACED: dict[str, str | tuple[str, dict[str, str]]] = {
    "CLI-7": "#216",
    "CLI-8": "#216",
    "CLI-9": "#216",
    "IMAGE-1": (
        "#220",
        {"live": "a pure function of Dockerfile text; it makes no AWS call"},
    ),
    "IMAGE-2": "#220",
    "IMAGE-3": (
        "#220",
        {"live": "a pure function of Dockerfile text; it makes no AWS call"},
    ),
    "IMAGE-4": "#220",
    "IMAGE-5": (
        "#220",
        {
            "model": "a binding pass-through has no states; core's are modeled as IMAGE-1..4",
            "gherkin": "the scenarios are core's (IMAGE-1..4); each binding's tests check the pass-through",
            "fuzz": "the binding hands the text unchanged to core's fuzzed function",
            "live": "a pure function of Dockerfile text; it makes no AWS call",
        },
    ),
    "IMAGE-6": (
        "#221",
        {
            "model": "a pure function of the build inputs; the name has no states to explore"
        },
    ),
    "IMAGE-7": (
        "#221",
        {
            "model": "reading a directory has no states; the ignore rules are fuzzed "
            "against moby's own regex translation instead"
        },
    ),
    "IMAGE-8": "#221",
    "IMAGE-9": ("#221", {"fuzz": INTERLEAVINGS}),
    "IMAGE-10": ("#221", {"fuzz": INTERLEAVINGS}),
    "IMAGE-11": ("#221", {"fuzz": INTERLEAVINGS}),
    "IMAGE-12": (
        "#221",
        {
            "model": "a binding pass-through has no states; core's are modeled as IMAGE-8..11",
            "gherkin": "the scenarios are core's (IMAGE-6..11); each binding's tests check the "
            "pass-through",
            "fuzz": "the binding hands its arguments unchanged to core's fuzzed functions",
            "live": "the conformance section drives core's ensure_image, which each binding "
            "forwards unchanged",
        },
    ),
    "AGENTD-7": "#224",
    "AGENTD-8": "#224",
    "AGENTD-9": "#224",
    "AGENTD-10": "#225",
    "AGENTD-11": "#225",
    "AGENTD-12": "#225",
    "AGENTD-13": "#225",
    "AGENTD-14": "#226",
    "AGENTD-15": "#226",
    "AGENTD-16": "#224",
    "BIND-11": "#227",
    "BIND-12": "#227",
    "BIND-13": (
        "#227",
        {
            "live": "a pure function that makes no AWS call; its refusals precede any call, "
            "so the service never sees them (zero calls asserted by the Gherkin scenarios "
            "and the fuzz harness)"
        },
    ),
    "BIND-17": "#219",
    "BIND-18": "#219",
    "BIND-19": "#219",
    "BIND-20": "#219",
    "BIND-6": "#222",
    "BIND-7": "#222",
    "BIND-8": "#222",
    "BIND-9": "#222",
    "BIND-10": "#222",
    "BIND-14": (
        "#223",
        {
            "model": "a stateless selection over the five-row size table; the bolero "
            "harness checks minimality and coverage over arbitrary requests instead",
            "live": "a pure function of the request and the documented table; it makes no "
            "AWS call",
        },
    ),
    "BIND-15": (
        "#223",
        {
            "fuzz": "the outcome space (3 region x 2 credential x 3 service worlds) is "
            "enumerated exhaustively by the Stateright model; there is no input stream to fuzz",
        },
    ),
    "BIND-16": (
        "#223",
        {
            "fuzz": "the outcome space (3 region x 2 credential x 3 service worlds) is "
            "enumerated exhaustively by the Stateright model; there is no input stream to fuzz",
        },
    ),
    "ARCH-6": (
        "#282",
        {
            "model": "a property of a crate's code and dependencies, not of a state",
            "gherkin": "no behavior to script: clippy and the dependency set enforce it at build "
            "time",
            "fuzz": "there is no input stream; the rule is over source and manifests",
            "live": "the domain makes no AWS call by construction",
        },
    ),
    "ARCH-7": (
        "#283",
        {
            "model": "a property of a crate's code and dependencies, not of a state",
            "gherkin": "no behavior to script: clippy and the dependency set enforce it at build "
            "time",
            "fuzz": "there is no input stream; the rule is over source and manifests",
            "live": "the app's AWS calls all go through ports, so the live tier exercises the "
            "edges' implementations, not this rule",
        },
    ),
    "ARCH-8": (
        "#283",
        {
            "model": "a property of a crate's code and dependencies, not of a state",
            "gherkin": "no behavior to script: the dependency set and the ratchet check it over "
            "source and manifests",
            "fuzz": "there is no input stream; the rule is over source and manifests",
            "live": "composition makes no AWS call of its own",
        },
    ),
}

LAYERS = ("model", "gherkin", "fuzz", "test", "impl", "live")

# Production Rust source, and the directories whose every file is a test.
IMPL_DIRS = (
    "microvms-cli/src",
    "microvms-core/src",
    "microvms-domain/src",
    "microvms-app/src",
    "microvms-edges/src",
    "agentd/src",
    "protocol/src",
    "microvms-py/src",
    "microvms-js/src",
)
RUST_TEST_DIRS = (
    "microvms-cli/tests",
    "microvms-core/tests",
    "microvms-edges/tests",
    "agentd/tests",
    "agentd/fuzz/fuzz_targets",
)
BINDING_TESTS = (
    ("microvms-py/tests", "*.py"),
    ("microvms-js/__test__", "*.mjs"),
)

# Listed entries that yield files but no requirement key today, each with the reason.
# Every other entry must give up at least one key, because a directory that still has
# files but no keyed ones (tests moved into a subdirectory, leaving `conftest.py`) drops
# out of the matrix as quietly as one that vanished. An entry here that starts yielding
# a key is reported, so this list can only shrink.
KEYLESS = {
    "microvms-edges/tests": "the release-bundle test names no requirement yet; listed so "
    "the first one that does is counted",
    "agentd/fuzz/fuzz_targets": "the cargo-fuzz tar harness guards extraction, which no "
    "spec requirement covers; the keyed fuzz harnesses are bolero targets under src/ and "
    "tests/",
}

# A key the traced table always carries. The per-key loop in `main` checks every layer
# of every key in TRACED, this one included, so a TRACED that lost its entries would
# pass that loop vacuously; this is what notices.
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


def waivers(key: str, traced=None) -> dict[str, str]:
    """The layers a traced key waives, each with its reason."""
    entry = (TRACED if traced is None else traced)[key]
    return entry[1] if isinstance(entry, tuple) else {}


def rel(path: Path, root: Path = ROOT) -> str:
    return path.relative_to(root).as_posix()


def rust_files(*directories: str, root: Path = ROOT) -> list[Path]:
    files: list[Path] = []
    for directory in directories:
        files.extend(sorted((root / directory).rglob("*.rs")))
    return [path for path in files if "target" not in path.relative_to(root).parts]


def enumerator_floors(root: Path = ROOT) -> list[str]:
    """A listed directory that yields no file, which would drop its layer silently.

    A renamed `microvms-js/__test__` would take every Node test out of the matrix, and
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


def layer_floors(found: dict[str, dict[str, set[str]]]) -> list[str]:
    """A layer or a listed entry that gave up no key, and a TRACED without the sentinel."""
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
    if SENTINEL not in TRACED:
        problems.append(f"the sentinel {SENTINEL} is not in TRACED")
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

    for path in rust_files("model/src", root=root):
        for key in patterns.property.findall(path.read_text()):
            note(key, "model", path)

    for path in sorted(root.glob("*/tests/features/*.feature")):
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
        if named[path].fuzz_file or path in rust_tests or path.name == "guards.rs":
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

    live = root / LIVE.relative_to(ROOT)
    if live.is_file():
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


def gaps(found: dict[str, dict[str, set[str]]], traced=None) -> list[str]:
    """Each layer a traced key neither covers nor waives."""
    traced = TRACED if traced is None else traced
    return [
        f"{key} has no {layer} layer"
        for key in traced
        for layer in LAYERS
        if layer not in waivers(key, traced) and not found.get(key, {}).get(layer)
    ]


def cell(found: dict[str, dict[str, set[str]]], key: str, layer: str) -> str:
    if layer in waivers(key):
        return "waived"
    return str(len(found.get(key, {}).get(layer, ())))


def render(found: dict[str, dict[str, set[str]]], sentences: dict[str, str]) -> str:
    lines = [
        "# Requirement traceability",
        "",
        "Generated by `./scripts/check-trace.py --write`; `mise run trace:check` fails when",
        "this file is stale or a traced requirement is missing a layer. Requirements are",
        "defined in `spec/core.symspec.json` and `spec/agentd.symspec.json`.",
        "",
        "| Requirement | " + " | ".join(LAYERS) + " |",
        "|---|" + "---|" * len(LAYERS),
    ]
    for key in TRACED:
        cells = [cell(found, key, layer) for layer in LAYERS]
        lines.append(f"| {key} | " + " | ".join(cells) + " |")
    for key in TRACED:
        lines += ["", f"## {key}", "", sentences[key], ""]
        for layer in LAYERS:
            files = sorted(found.get(key, {}).get(layer, ()))
            listed = ", ".join(f"`{f}`" for f in files)
            reason = waivers(key).get(layer)
            if reason:
                listed = f"waived: {reason}" + (f" ({listed})" if listed else "")
            lines.append(f"- **{layer}:** " + (listed or "none"))
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
    found = collect(patterns)
    problems = enumerator_floors() + layer_floors(found)

    for key in TRACED:
        if key not in sentences:
            problems.append(f"{key} is traced but not defined in either spec")
        for layer, reason in waivers(key).items():
            if layer not in LAYERS or not reason.strip():
                problems.append(
                    f"{key} waives {layer!r} without a known layer and reason"
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
    problems += gaps(found)

    width = max(len(layer) for layer in LAYERS)
    print("requirement  " + "  ".join(layer.ljust(width) for layer in LAYERS))
    for key in TRACED:
        counts = [cell(found, key, layer).ljust(width) for layer in LAYERS]
        print(f"{key:<11}  " + "  ".join(counts))

    rendered = render(found, sentences) if not problems or args.write else ""
    if args.write and rendered:
        DOC.write_text(rendered)
        print(f"wrote {rel(DOC)}")
    if (
        args.check
        and not problems
        and (not DOC.exists() or DOC.read_text() != rendered)
    ):
        problems.append(f"{rel(DOC)} is stale: run ./scripts/check-trace.py --write")

    for problem in problems:
        print(f"trace: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
