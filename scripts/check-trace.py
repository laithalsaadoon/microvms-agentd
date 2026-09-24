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
  fuzz     a mention in a fuzz harness (a Rust file calling `bolero::check!` or
           `fuzz_target!`)
  test     a mention in a test: a file under a Rust crate's `tests/`, a source
           file's test module, `microvms-cli/src/guards.rs`, or a binding test under
           `microvms-py/tests/` or `microvms-js/__test__/`
  impl     a mention in production Rust source of the CLI, core, daemon, protocol,
           or either binding
  live     a live conformance check whose name starts with the key
           (`conformance/run_rs.py`, run against AWS by `mise run live`)

A traced key may waive a layer with a reason, for example the live layer of a pure
function that makes no AWS call. The waiver and its reason are rendered in the matrix,
so an absent layer is a stated decision rather than a gap.

It also refuses a mention of an unknown key, so a typo such as `CLI-10` for `CLI-9`
cannot pass as coverage. Keys are recognized by the prefixes the two specs define.
`docs/TRACEABILITY.md` is the rendered matrix:

  ./scripts/check-trace.py           print the matrix; fail if a layer is missing
  ./scripts/check-trace.py --write   also render docs/TRACEABILITY.md
  ./scripts/check-trace.py --check   also fail if docs/TRACEABILITY.md is stale
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPECS = (
    ROOT / "spec" / "core.symspec.json",
    ROOT / "spec" / "agentd.symspec.json",
)
DOC = ROOT / "docs" / "TRACEABILITY.md"

# The fuzz waiver the ensure_image decisions share: their input space is interleavings.
INTERLEAVINGS = (
    "the input space is two callers interleaved against the platform, which "
    "model/src/image.rs checks exhaustively; the decision table is ten rows, all pinned "
    "by the_plan_table"
)

# Requirements traced end to end. The value is the issue that introduced the key, or
# `(issue, {layer: reason})` for a key that waives a layer; see the module docs.
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
}

LAYERS = ("model", "gherkin", "fuzz", "test", "impl", "live")

# Production Rust source, and the directories whose every file is a test.
IMPL_DIRS = (
    "microvms-cli/src",
    "microvms-core/src",
    "agentd/src",
    "protocol/src",
    "microvms-py/src",
    "microvms-js/src",
)
RUST_TEST_DIRS = (
    "microvms-cli/tests",
    "microvms-core/tests",
    "agentd/tests",
    "agentd/fuzz/fuzz_targets",
)
BINDING_TESTS = (
    ("microvms-py/tests", "*.py"),
    ("microvms-js/__test__", "*.mjs"),
    ("microvms-js/__test__", "*.ts"),
)

FUZZ_MARKER = re.compile(r"bolero::check!|fuzz_target!")
TEST_MODULE = re.compile(r"^#\[cfg\(test\)\]\s*$", re.MULTILINE)


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
        # A live conformance check whose name starts with the requirement key.
        self.live = re.compile(rf'results\.(?:check|eq)\(\s*f?"({key})\b')


def waivers(key: str) -> dict[str, str]:
    """The layers a traced key waives, each with its reason."""
    entry = TRACED[key]
    return entry[1] if isinstance(entry, tuple) else {}


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def rust_files(*directories: str) -> list[Path]:
    files: list[Path] = []
    for directory in directories:
        files.extend(sorted((ROOT / directory).rglob("*.rs")))
    return [path for path in files if "target" not in path.parts]


def split_test_region(text: str) -> tuple[str, str]:
    """A source file's production text and its trailing `#[cfg(test)]` module, if any."""
    for match in TEST_MODULE.finditer(text):
        following = text[match.end() :].lstrip()
        if following.startswith("mod ") and not following.split("\n", 1)[
            0
        ].rstrip().endswith(";"):
            return text[: match.start()], text[match.start() :]
    return text, ""


def collect(patterns: Patterns) -> dict[str, dict[str, set[str]]]:
    """For every key found anywhere, the files each layer found it in."""
    found: dict[str, dict[str, set[str]]] = {}

    def note(key: str, layer: str, path: Path) -> None:
        found.setdefault(key, {layer: set() for layer in LAYERS})[layer].add(rel(path))

    for path in rust_files("model/src"):
        for key in patterns.property.findall(path.read_text()):
            note(key, "model", path)

    for path in sorted(ROOT.glob("*/tests/features/*.feature")):
        for key in patterns.tag.findall(path.read_text()):
            note(key, "gherkin", path)

    rust_tests = rust_files(*RUST_TEST_DIRS)
    for path in rust_files(*IMPL_DIRS) + rust_tests:
        text = path.read_text()
        if FUZZ_MARKER.search(text):
            for key in patterns.key.findall(text):
                note(key, "fuzz", path)
            continue
        if path in rust_tests or path.name == "guards.rs":
            for key in patterns.key.findall(text):
                note(key, "test", path)
            continue
        production, tests = split_test_region(text)
        for key in patterns.key.findall(production):
            note(key, "impl", path)
        for key in patterns.key.findall(tests):
            note(key, "test", path)

    for directory, pattern in BINDING_TESTS:
        for path in sorted((ROOT / directory).glob(pattern)):
            for key in patterns.key.findall(path.read_text()):
                note(key, "test", path)

    live = ROOT / "conformance" / "run_rs.py"
    for key in patterns.live.findall(live.read_text()):
        note(key, "live", live)
    return found


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
    found = collect(Patterns(sentences))
    problems: list[str] = []

    for key in TRACED:
        if key not in sentences:
            problems.append(f"{key} is traced but not defined in either spec")
        for layer, reason in waivers(key).items():
            if layer not in LAYERS or not reason.strip():
                problems.append(
                    f"{key} waives {layer!r} without a known layer and reason"
                )
    for key, layers in sorted(found.items()):
        if key not in sentences:
            where = sorted({path for paths in layers.values() for path in paths})
            problems.append(
                f"{key} is mentioned but not defined in the spec: {', '.join(where)}"
            )
    for key in TRACED:
        for layer in LAYERS:
            if layer not in waivers(key) and not found.get(key, {}).get(layer):
                problems.append(f"{key} has no {layer} layer")

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
