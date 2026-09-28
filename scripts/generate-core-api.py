#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
# SPDX-License-Identifier: Apache-2.0
"""Write `parity/core-api.json`, every public path of `microvms-core` (#271).

`parity:check` holds each capability row's core cell to this file, so it has to be today's
surface, not a release's (`microvms-core/tests/public_paths.rs` is v0.10.0's). Core is the
composition root: most of its API is `pub use` of the crates below it, and rustdoc JSON doesn't
inline a re-export from another crate. So this builds the rustdoc JSON of core and of each
workspace crate it re-exports, and `scripts/rustdoc_walk.py` stitches them into one list at the
paths a consumer writes. The walk's docstring has what it lists and the two rules it applies.

It's a committed snapshot rather than a build inside `parity:check` so the check stays a read
of generated files, like the stub and `index.d.ts`, and a change to core's surface shows up as
a diff in review. The build runs on the repo's stable toolchain with `RUSTC_BOOTSTRAP=1`, as
`generate-public-paths.py`'s documented command does, so no nightly is installed anywhere.

    ./scripts/generate-core-api.py            # rebuild the JSON and write the snapshot
    ./scripts/generate-core-api.py --check    # rebuild and fail on any difference
    ./scripts/generate-core-api.py --doc-dir D   # read already-built JSON from D instead

`--check` is CI's step: it prints the paths that appeared and went, and the command that
regenerates the file. The snapshot leaves out the rustdoc `format_version` it was read from:
the walk refuses a version it doesn't accept, and recording an accepted one would make a
contributor on one stable and CI on the next write different files for the same surface.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path

import rustdoc_walk

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "parity" / "core-api.json"

# Core first, then every workspace crate core re-exports from. A crate missing here is named
# with the kind `external` and not walked, so its paths would vanish from the snapshot.
CRATES = (
    "microvms_core",
    "microvms_app",
    "microvms_domain",
    "microvms_edges",
    "microvms_protocol",
)

# One path per branch of the walk that every tree since the #283 split has. A walk that comes
# back short (a stitch that stopped following re-exports, a format the walk misreads) passes
# nothing it can't see, so these must be present, with these kinds, before anything is written.
SENTINELS = {
    # core's own item
    "microvms_core::VERSION": "constant",
    # a module re-exported whole from the domain
    "microvms_core::cost::run_report": "function",
    # an inherent method of a type the app defines
    "microvms_core::sandbox::Sandbox::run": "method",
    # an extension trait's method, named on the type
    "microvms_core::sandbox::Sandbox::new": "method via microvms_core::prelude::SandboxExt",
    # a trait's own item
    "microvms_core::names::NameStore::release_by_vm": "method",
    # the protocol crate, re-exported as a module
    "microvms_core::protocol::exec::StartRequest::new": "method",
}


def render(doc_dir: Path) -> str:
    entries = rustdoc_walk.stitch(doc_dir, CRATES[0], CRATES)
    paths = rustdoc_walk.surface(entries)
    wrong = {
        path: paths.get(path)
        for path, kind in SENTINELS.items()
        if paths.get(path) != kind
    }
    if wrong:
        raise rustdoc_walk.StitchError(
            f"the walk found {len(paths)} paths, and these sentinels are missing or of"
            f" another kind: {wrong}. Every tree since #283 has them, so the walk or the"
            " JSON's shape is what changed"
        )
    snapshot = {
        "generated_by": "scripts/generate-core-api.py; regenerate with `mise run core-api`",
        "crates": list(CRATES),
        "paths": paths,
    }
    return json.dumps(snapshot, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail on any difference")
    parser.add_argument(
        "--doc-dir", type=Path, help="read built rustdoc JSON from here"
    )
    args = parser.parse_args()
    try:
        doc_dir = args.doc_dir or rustdoc_walk.build(
            ROOT, [crate.replace("_", "-") for crate in CRATES]
        )
        text = render(doc_dir)
    except rustdoc_walk.StitchError as error:
        print(f"core-api: {error}")
        return 1
    if not args.check:
        OUT.parent.mkdir(exist_ok=True)
        OUT.write_text(text, encoding="utf-8")
        print(f"core-api: wrote {OUT.relative_to(ROOT)}")
        return 0
    try:
        committed = OUT.read_text(encoding="utf-8")
    except FileNotFoundError:
        committed = ""
    if committed == text:
        print(f"core-api: {OUT.relative_to(ROOT)} matches core's public surface")
        return 0
    print(
        f"core-api: {OUT.relative_to(ROOT)} is stale; run `mise run core-api` and commit the"
        " result. The difference, committed then built:"
    )
    sys.stdout.writelines(
        difflib.unified_diff(
            committed.splitlines(keepends=True),
            text.splitlines(keepends=True),
            "committed",
            "built",
            n=0,
        )
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
