# SPDX-License-Identifier: Apache-2.0
"""Walk a crate's public paths out of rustdoc JSON, across the crates it re-exports (#271).

A module, not a script: `generate-core-api.py` and `generate-public-paths.py` import it.

The walk starts at the root crate's module tree, so it sees what a consumer can name: every
public item and re-export, each enum variant, each inherent method, associated constant and
associated type, and each item of a public trait. Struct fields and trait-impl methods aren't
paths a caller writes, so they're left out.

rustdoc JSON doesn't inline a re-export from another crate. Since #283, microvms-core is mostly
`pub use` of microvms-app, microvms-domain and microvms-edges, and its own JSON names each moved
item as an external id with nothing inside it. So the walk follows an external id through the
root's `paths` (its canonical path and kind) into the JSON of the crate that defines it, when
that crate is in the set it's told to read. A re-export into any other crate, std or a
third-party dependency, is named with the kind `external` and not walked.

Two rules make the result match what compiles:

- A module's own item or explicit re-export shadows a glob's item of the same name in the
  same namespace, the way core merges a module with halves in two crates. rustdoc happens to
  list a module's globs last, but the walk doesn't count on it.
- A method of an extension trait defined in the root crate is also named on the type it's
  implemented for. That's how core's prelude keeps `Sandbox::new` after the type moved to the
  app: callers write `microvms_core::sandbox::Sandbox::new`. A trait from a lower crate
  (`NameStore`, `Clone`) is an ordinary trait, and its impls aren't listed per type.

Only the `format_version`s in `ACCEPTED_FORMAT_VERSIONS` are read. The format changes with the
toolchain, and a walk over a format it wasn't written for can come back short without an error,
so a new version is refused by number. To accept one, read its entries in rustdoc's
`src/rustdoc-json-types` changelog (the "Latest feature" line beside `FORMAT_VERSION`),
regenerate `parity/core-api.json` with it, and add it here once the snapshot doesn't change.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# Stable 1.98 writes 60 and 1.99 (2026-10-01) writes 61. 61's one change makes `Stability`
# serialize without a self-describing format, and the walk reads no stability field: nightly's
# 61 gave the same paths with the same kinds (#271). CI and rust-toolchain.toml float `stable`,
# so the next stable's version has to be here before it ships, or every PR goes red that day.
ACCEPTED_FORMAT_VERSIONS = frozenset({60, 61})

# The module-level kinds a path can name. `impl`, `extern_crate` and the proc-macro kinds aren't
# reached by a path through a module.
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

# A member's rustdoc kind, and the name the snapshot gives it.
MEMBER_KINDS = {
    "function": "method",
    "assoc_const": "assoc_const",
    "assoc_type": "assoc_type",
}


# Each kind's namespace, for glob shadowing: a module's own item hides a glob's item of the
# same name only in the same namespace, as in rustc. A unit or tuple struct also takes the
# value namespace; that's left out, since it hasn't come up in this tree.
NAMESPACES = {
    "module": "type",
    "struct": "type",
    "enum": "type",
    "union": "type",
    "trait": "type",
    "type_alias": "type",
    "trait_alias": "type",
    "variant": "type",
    "function": "value",
    "constant": "value",
    "static": "value",
    "macro": "macro",
}


def shadowed(skip: frozenset, name: str, what: str | None) -> bool:
    """Whether a glob's item `name` of kind `what` is hidden by one of the names in `skip`."""
    namespace = NAMESPACES.get(what)
    return (
        (name, namespace) in skip
        or (name, None) in skip
        or (namespace is None and any(entry[0] == name for entry in skip))
    )


class StitchError(Exception):
    """The JSON can't be walked: missing, empty, in an unknown format, or inconsistent."""


@dataclass(frozen=True)
class Entry:
    """One public path.

    `owner` is the type or trait a member belongs to; `None` for a module-level item or an
    enum variant. `via` is `inherent`, `trait` (an item of the trait itself), or the path of
    the extension trait that puts the method on `owner`. `generic` means the path can't be
    named without type arguments: a generic type, a generic impl, or a method with a type
    parameter.
    """

    path: str
    kind: str
    owner: str | None = None
    via: str | None = None
    generic: bool = False

    @property
    def name(self) -> str:
        return self.path.rsplit("::", 1)[1]


def build(
    workspace: Path, packages: Iterable[str], target_dir: Path | None = None
) -> Path:
    """Build each package's rustdoc JSON on the current toolchain; return the doc directory.

    `RUSTC_BOOTSTRAP=1` lets stable rustdoc take `-Z unstable-options`, as
    `generate-public-paths.py`'s documented command already does, so no nightly is needed.
    One `cargo doc` builds every package, and `--no-deps` keeps it to their own JSON.
    `--cap-lints allow` quiets rustdoc's doc-link warnings, which are the docs build's to
    report and would bury `--check`'s diff in a CI log.
    """
    env = dict(os.environ, RUSTC_BOOTSTRAP="1")
    env["RUSTDOCFLAGS"] = "-Z unstable-options --output-format json --cap-lints allow"
    argv = ["cargo", "doc", "--no-deps", "--lib", "--quiet", "--color", "never"]
    argv += [arg for package in packages for arg in ("-p", package)]
    if target_dir is not None:
        env.pop("CARGO_TARGET_DIR", None)
        argv += ["--target-dir", str(target_dir)]
    subprocess.run(argv, cwd=workspace, env=env, check=True)
    if target_dir is None:
        metadata = subprocess.run(
            ["cargo", "metadata", "--format-version", "1", "--no-deps"],
            cwd=workspace,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        target_dir = Path(json.loads(metadata.stdout)["target_directory"])
    return Path(target_dir) / "doc"


def load(path: Path) -> dict:
    """Read one crate's rustdoc JSON, refusing a format or an index the walk can't trust."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise StitchError(
            f"{path} doesn't exist; the crate's rustdoc JSON wasn't built"
        ) from None
    version = doc.get("format_version")
    if version not in ACCEPTED_FORMAT_VERSIONS:
        raise StitchError(
            f"{path} has rustdoc format_version {version}, and this walk was written for"
            f" {sorted(ACCEPTED_FORMAT_VERSIONS)}. scripts/rustdoc_walk.py's docstring says"
            " how to accept a new one"
        )
    index = doc.get("index")
    if not index or str(doc.get("root")) not in index:
        raise StitchError(f"{path} has an empty index, or none holding its root module")
    # Each crate's own items by canonical path and kind, which is how another crate's JSON
    # names them in its `paths`.
    doc["_by_path"] = {
        (tuple(summary["path"]), summary["kind"]): item_id
        for item_id, summary in doc["paths"].items()
        if summary["crate_id"] == 0
    }
    doc["_name"] = index[str(doc["root"])]["name"]
    return doc


def kind(item: dict) -> str:
    return next(iter(item["inner"]))


class _Walk:
    def __init__(self, doc_dir: Path, read: Iterable[str]):
        self.doc_dir = doc_dir
        self.read = set(read)
        self.docs: dict[str, dict] = {}
        self.entries: dict[str, Entry] = {}
        self.seen: set[tuple[str, str]] = set()
        # Each type's public paths, keyed by its canonical path and kind, so an extension
        # trait's impl (which names the type by its canonical path) finds where it's named.
        self.homes: dict[tuple, list[str]] = defaultdict(list)
        self.extensions: dict[str, tuple[dict, dict, str]] = {}
        self.root: dict | None = None

    def doc(self, crate: str) -> dict | None:
        if crate not in self.read:
            return None
        if crate not in self.docs:
            self.docs[crate] = load(self.doc_dir / f"{crate}.json")
        return self.docs[crate]

    def add(self, entry: Entry) -> None:
        self.entries.setdefault(entry.path, entry)

    def canonical(self, doc: dict, item_id) -> tuple:
        summary = doc["paths"].get(str(item_id))
        if summary is None:
            return (doc["_name"], str(item_id))
        return (tuple(summary["path"]), summary["kind"])

    def target(self, doc: dict, item_id) -> tuple[dict, dict] | None:
        """The item a `use` names, in whichever read crate defines it; `None` if unread."""
        if item_id is None:
            return None
        item = doc["index"].get(str(item_id))
        if item is not None:
            return doc, item
        summary = doc["paths"].get(str(item_id))
        # A local id missing from the index is an item rustdoc left out (`#[doc(hidden)]`).
        if summary is None or summary["crate_id"] == 0:
            return None
        crate = doc["external_crates"][str(summary["crate_id"])]["name"]
        other = self.doc(crate)
        if other is None:
            return None
        found = other["_by_path"].get((tuple(summary["path"]), summary["kind"]))
        if found is None:
            raise StitchError(
                f"{doc['_name']} re-exports the {summary['kind']}"
                f" {'::'.join(summary['path'])}, which {crate}'s JSON doesn't define; the"
                " two crates' JSON were built from different trees"
            )
        return other, other["index"][found]

    def walk(self, doc: dict, module: dict, prefix: str, skip: frozenset = frozenset()):
        """Walk a module's public items; `skip` holds the (name, namespace) pairs a glob hides."""
        children = []
        for child_id in module["inner"]["module"]["items"]:
            child = doc["index"][str(child_id)]
            if child["visibility"] != "public":
                continue
            resolved = None
            if kind(child) == "use":
                resolved = self.target(doc, child["inner"]["use"]["id"])
            children.append((child, resolved))
        # What this module names itself, which shadows a glob's item in the same namespace.
        # A re-export into an unread crate has no kind here, so it shadows in every one.
        explicit = set()
        for child, resolved in children:
            if kind(child) != "use":
                explicit.add((child["name"], NAMESPACES.get(kind(child))))
            elif not child["inner"]["use"]["is_glob"]:
                target = kind(resolved[1]) if resolved else None
                explicit.add((child["inner"]["use"]["name"], NAMESPACES.get(target)))
        inherited = skip | frozenset(explicit)
        for child, resolved in children:
            what = kind(child)
            if what == "use":
                use = child["inner"]["use"]
                if use["is_glob"]:
                    if resolved is None:
                        self.add(Entry(f"{prefix}::*", "external"))
                        continue
                    glob_doc, glob = resolved
                    if kind(glob) == "module":
                        self.walk(glob_doc, glob, prefix, inherited)
                    elif kind(glob) == "enum":
                        for variant in glob["inner"]["enum"]["variants"]:
                            name = glob_doc["index"][str(variant)]["name"]
                            if not shadowed(inherited, name, "variant"):
                                self.add(Entry(f"{prefix}::{name}", "variant"))
                    continue
                target = kind(resolved[1]) if resolved else None
                if shadowed(skip, use["name"], target):
                    continue
                path = f"{prefix}::{use['name']}"
                if resolved is None:
                    self.add(Entry(path, "external"))
                else:
                    self.visit(*resolved, path)
                continue
            if what not in NAMED or shadowed(skip, child["name"], what):
                continue
            self.visit(doc, child, f"{prefix}::{child['name']}")

    def visit(self, doc: dict, item: dict, path: str) -> None:
        what = kind(item)
        if (path, what) in self.seen:
            return
        self.seen.add((path, what))
        self.add(Entry(path, what))
        inner = item["inner"][what]
        if what == "module":
            self.walk(doc, item, path)
        elif what == "enum":
            for variant in inner["variants"]:
                self.add(
                    Entry(f"{path}::{doc['index'][str(variant)]['name']}", "variant")
                )
        elif what == "trait":
            generic = bool(inner["generics"]["params"])
            for member_id in inner["items"]:
                self.member(path, doc["index"][str(member_id)], "trait", generic)
            if doc is self.root:
                self.extensions.setdefault(str(item["id"]), (doc, item, path))
        if what in ("struct", "enum", "union"):
            self.homes[self.canonical(doc, item["id"])].append(path)
            type_generic = bool(inner["generics"]["params"])
            for impl_id in inner.get("impls", []):
                impl = doc["index"][str(impl_id)]["inner"]["impl"]
                if impl["trait"] is not None:
                    continue
                generic = type_generic or bool(impl["generics"]["params"])
                for member_id in impl["items"]:
                    member = doc["index"][str(member_id)]
                    if member["visibility"] == "public":
                        self.member(path, member, "inherent", generic)

    def member(self, owner: str, member: dict, via: str, generic: bool) -> None:
        what = kind(member)
        if what not in MEMBER_KINDS:
            return
        if what == "function":
            generic = generic or any(
                "type" in param["kind"]
                for param in member["inner"]["function"]["generics"]["params"]
            )
        self.add(
            Entry(
                f"{owner}::{member['name']}",
                MEMBER_KINDS[what],
                owner=owner,
                via=via,
                generic=generic,
            )
        )

    def attribute_extensions(self) -> None:
        """Name each root-crate trait's items on every type it's implemented for."""
        for doc, trait, trait_path in self.extensions.values():
            inner = trait["inner"]["trait"]
            for impl_id in inner["implementations"]:
                impl = doc["index"][str(impl_id)]["inner"]["impl"]
                target = impl["for"].get("resolved_path")
                if target is None:
                    # A blanket impl over a type parameter names no one type.
                    continue
                generic = bool(impl["generics"]["params"])
                for home in self.homes.get(self.canonical(doc, target["id"]), []):
                    for member_id in inner["items"]:
                        self.member(
                            home, doc["index"][str(member_id)], trait_path, generic
                        )


def stitch(doc_dir: Path, root: str, read: Iterable[str]) -> list[Entry]:
    """Every public path of crate `root`, following re-exports into the crates in `read`.

    Each crate's JSON is `doc_dir/<crate>.json`, the name rustdoc writes. `read` must include
    `root`; a crate outside it is named but not walked.
    """
    walk = _Walk(Path(doc_dir), read)
    walk.root = walk.doc(root)
    if walk.root is None:
        raise StitchError(f"the root crate {root} isn't in the set to read")
    walk.walk(walk.root, walk.root["index"][str(walk.root["root"])], walk.root["_name"])
    walk.attribute_extensions()
    if not walk.entries:
        raise StitchError(f"the walk of {root} found no public paths")
    return sorted(walk.entries.values(), key=lambda entry: entry.path)


def surface(entries: Iterable[Entry]) -> dict[str, str]:
    """Each path and its kind, the shape `parity/core-api.json` stores."""
    out = {}
    for entry in entries:
        extension = entry.via not in (None, "inherent", "trait")
        out[entry.path] = f"{entry.kind} via {entry.via}" if extension else entry.kind
    return dict(sorted(out.items()))
