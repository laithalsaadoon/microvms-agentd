#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
# SPDX-License-Identifier: Apache-2.0
"""Write `microvms-core/tests/public_paths.rs` from a baseline's rustdoc JSON (#282).

The witness names every public path a release of `microvms-core` had, so it compiles only
while each still resolves. It's a test file rather than a comparison of two JSON dumps because
moving an item into `microvms-domain` and re-exporting it changes the JSON (the item becomes a
`use` of an external id) without changing a single path, and the compiler is the one judge of
whether a path resolves.

Produce the JSON from a checkout of the baseline tag, outside this tree:

    git archive v0.10.0 | tar -x -C /tmp/baseline
    cd /tmp/baseline && RUSTC_BOOTSTRAP=1 cargo rustdoc -p microvms-core --lib -- \\
        -Z unstable-options --output-format json

then, from this repository:

    ./scripts/generate-public-paths.py /tmp/baseline/target/doc/microvms_core.json v0.10.0

It walks the module tree from the crate root, so it sees what a consumer can name: every
public item and re-export, each enum variant, and each inherent method or associated constant
of a non-generic type. A method with a type parameter (an `impl Into<String>` argument is one)
can't be named without its arguments, so the in-repo callers compiling is its check instead.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

OUT = (
    Path(__file__).resolve().parent.parent
    / "microvms-core"
    / "tests"
    / "public_paths.rs"
)
NAMED = {
    "module",
    "struct",
    "enum",
    "union",
    "trait",
    "function",
    "constant",
    "static",
    "type_alias",
    "macro",
    "trait_alias",
}


def kind(item: dict) -> str:
    return next(iter(item["inner"]))


def main() -> int:
    source, baseline = sys.argv[1], sys.argv[2]
    doc = json.loads(Path(source).read_text(encoding="utf-8"))
    index = doc["index"]
    crate = index[str(doc["root"])]["name"]
    paths: set[str] = set()
    members: set[str] = set()
    seen: set[tuple[str, str]] = set()

    def items_of(type_item: dict) -> None:
        inner = type_item["inner"][kind(type_item)]
        if inner["generics"]["params"]:
            return
        yield from inner.get("impls", [])

    def walk(module: dict, prefix: str) -> None:
        for child_id in module["inner"]["module"]["items"]:
            child = index[str(child_id)]
            if child["visibility"] != "public":
                continue
            what = kind(child)
            if what == "use":
                use = child["inner"]["use"]
                target = index.get(str(use["id"])) if use["id"] is not None else None
                if use["is_glob"]:
                    if target is not None and kind(target) == "module":
                        walk(target, prefix)
                    continue
                name = f"{prefix}::{use['name']}"
                paths.add(name)
                if target is not None:
                    visit(target, name)
                continue
            if what not in NAMED:
                continue
            name = f"{prefix}::{child['name']}"
            paths.add(name)
            visit(child, name)

    def visit(item: dict, path: str) -> None:
        what = kind(item)
        if (path, what) in seen:
            return
        seen.add((path, what))
        if what == "module":
            walk(item, path)
        elif what == "enum":
            for variant in item["inner"]["enum"]["variants"]:
                paths.add(f"{path}::{index[str(variant)]['name']}")
        if what in ("struct", "enum", "union"):
            for impl_id in items_of(item):
                impl = index[str(impl_id)]["inner"]["impl"]
                if impl["trait"] is not None or impl["generics"]["params"]:
                    continue
                for member_id in impl["items"]:
                    member = index[str(member_id)]
                    if member["visibility"] != "public":
                        continue
                    member_kind = kind(member)
                    if member_kind == "function":
                        if any(
                            "type" in param["kind"]
                            for param in member["inner"]["function"]["generics"][
                                "params"
                            ]
                        ):
                            continue
                    elif member_kind != "assoc_const":
                        continue
                    members.add(f"<{path}>::{member['name']}")

    walk(index[str(doc["root"])], crate)
    lines = [
        "// SPDX-License-Identifier: Apache-2.0",
        f"//! Every public path `microvms-core` {baseline} had, named so this file compiles only while",
        "//! each still resolves (ARCH-1).",
        "//!",
        "//! #282 moved the rules and values into `microvms-domain`, and core re-exports them. A path",
        "//! that stopped resolving would break every consumer that named it, so the paths are",
        "//! checked by the compiler rather than by comparing documentation. Generated by",
        "//! `scripts/generate-public-paths.py`, whose docstring has the command; regenerate it on",
        "//! purpose when a release changes the API, never to make a failure go away.",
        "//!",
        "//! The methods a type lost to `microvms_core::prelude` (the ones that read the clock or the",
        "//! random pool) resolve here through the prelude import, which is how a caller keeps them.",
        "",
        "#![allow(unused_imports)]",
        "",
    ]
    lines += [f"use {path} as _;" for path in sorted(paths)]
    lines += [
        "",
        "#[test]",
        "fn every_baseline_method_and_associated_constant_still_resolves() {",
        "    use microvms_core::prelude::*;",
        "",
    ]
    lines += [f"    let _ = {member};" for member in sorted(members)]
    lines += ["}", ""]
    OUT.write_text("\n".join(lines), encoding="utf-8")
    # rustfmt's version sort orders imports differently from Python's (`Mib512` before
    # `Mib1024`), so let it have the last word rather than reimplementing it.
    subprocess.run(["rustfmt", "--edition", "2024", str(OUT)], check=True)
    print(f"public_paths: wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
