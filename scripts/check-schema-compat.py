#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
# SPDX-License-Identifier: Apache-2.0
"""docs/schema.json against the previous release's copy: nothing a caller of either side can't read (#298).

`schema:check` holds the committed schema to what this tree serves; nothing held it to what the
last release served, which is what a caller meets during an upgrade: an older client against
this daemon, and this client against an older daemon. This compares `docs/schema.json` with
`git show <base>:docs/schema.json`, where the base is the highest `v*` release tag reachable
from HEAD (`vX.Y.Z` or `vX.Y.Z-pre`, ordered as semver orders them, so `v0.10.0` is above
`v0.9.0` and above `v0.10.0-rc.1`). `--base-ref` names another revision, and `--base-file`
reads the base from a file instead of git, for the unit tests. The seeded-fault registry
passes `--base-ref HEAD`: its CI checkout has no tags, and HEAD's copy is the clean schema a
fault is seeded over, whatever release it's on.

The rule follows serde's two contracts. A request or query shape is what the daemon
deserializes, so its schema lists as required only what a caller must send. A response or
stream-event shape is what the daemon serializes, so its schema lists every field it always
writes, and a field with a serde default carries `default`, which is what lets a reader do
without it. Shapes are compared route by route, never by `$defs` name: for each route both
schemas have, its request and query (the request side) and its response and each stream
event it kept (the response side) are resolved through `$ref` in each schema and compared old
against new, and so is every definition a field both have names in turn. So a route that
gains a body or query, or names another definition, is held like an edited one, and a
definition renamed with its shape kept passes. It fails on:

- a route (method and path) the base has and the current schema doesn't;
- a status (code and error) or a stream event (by name) that a surviving route lost;
- on the request side, a field required now that wasn't, a body or query the route didn't
  have counting as one with no fields (an older client doesn't send it, and this daemon
  refuses the request); a field that's gone (an older client still sends it and it's
  ignored, since no request denies unknown fields, or an older daemon still requires it and
  this client stopped sending it); and a field that was required and isn't now (this client
  may leave it out, and an older daemon refuses that);
- on the response side, a body the route had that's gone (an older client can't read the
  answer); a field the base required with no default that's gone, or that's still listed but
  no longer required (an older client can't read the answer without it), unless the base
  typed it nullable, since serde reads an absent `Option` as `None`; and a field required now
  with no default that wasn't (this client can't read an older daemon's answer, which lacks
  it).

A field-level rule that ignored the direction fails both v0.9.0 and v0.10.0: each made a
response field required with a serde default, which is compatible both ways. Every release
from v0.1.0 to v0.10.0 passes this rule against the one before it.

Out of scope: type narrowing and widening (a field's `type`, an enum losing a variant, the
string forms `user` and `group` gained, a body's `media_type`, a field that names a different
set of definitions than it did), `limits`, and `auth`. A change there passes.

`PROTOCOL_VERSION` with `docs/schema-breaks.toml` is the declared break. When the schema's
`protocol_version` differs from the base's, every break found has to be listed in that file
(each as the line this check prints, with a `why`), and every entry there has to be found:
then it prints them and exits 0, and otherwise it fails naming the ones that differ. The
base is the previous release tag, so a bump stays in force until the next tag; the list is
what keeps a later change's accidental break failing in that window. With the versions
equal, the file waives nothing, and a list left over from the last release is only noted.
No client reads the version yet, so a bump tells reviewers, not callers.

Exit status: 0 compatible (or every break declared, reported), 1 an incompatible change or a
declared list that doesn't match, each one named, 2 no comparison was possible. A check over
nothing passes everything, so 2 covers an empty or unparsable schema on either side, a
`docs/schema-breaks.toml` that isn't TOML or has an entry with no `change` or `why`, a schema with no `protocol_version`, a route list
that parses to nothing, routes that reach no request or no response definition, a base with
no `docs/schema.json`, and a clone with no `v*` release tag reachable from HEAD. That last
one is a shallow checkout (CI's default, and `git clone --depth 1`) or a fork cloned without
tags; the message says to fetch the tags and the history back to them, or to name the base
with `--base-ref`. CI's `rust` job checks out with `fetch-depth: 0` for this step.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

SCHEMA = "docs/schema.json"
BREAKS = "docs/schema-breaks.toml"
RELEASE_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$")
REQUEST_KEYS = ("request", "query")
RESPONSE_KEYS = ("response", "sse_events")


class Refused(Exception):
    """An input this check can't compare; it exits 2 naming why."""


@dataclass
class Schema:
    where: str
    protocol_version: str
    routes: dict[tuple[str, str], dict]
    defs: dict[str, dict]


def parse(text: str, where: str) -> Schema:
    if not text.strip():
        raise Refused(f"{where} is empty")
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as error:
        raise Refused(f"{where} isn't JSON: {error}") from None
    if not isinstance(doc, dict):
        raise Refused(f"{where} isn't a JSON object")
    version = doc.get("protocol_version")
    if not isinstance(version, str) or not version:
        raise Refused(f"{where} has no protocol_version")
    routes = {
        (r["method"], r["path"]): r
        for r in doc.get("routes") or []
        if isinstance(r, dict)
        and isinstance(r.get("method"), str)
        and isinstance(r.get("path"), str)
    }
    if not routes:
        raise Refused(f"{where} has no routes with a method and a path")
    defs = doc.get("$defs")
    if not isinstance(defs, dict):
        defs = {}
    request_side = reach(
        defs, [r.get(k) for r in routes.values() for k in REQUEST_KEYS]
    )
    response_side = reach(
        defs, [r.get(k) for r in routes.values() for k in RESPONSE_KEYS]
    )
    # Every schema this repo has shipped has bodies on both sides; one without is a reader
    # that stopped following `$ref`, and it would compare no field at all. This floor only
    # sees a reader that finds nothing. One that goes partly blind (skips the stream events,
    # stops at the first definition) still clears it, so the unit suite holds those.
    if not request_side:
        raise Refused(f"{where}: its routes reach no request definition in $defs")
    if not response_side:
        raise Refused(f"{where}: its routes reach no response definition in $defs")
    return Schema(where, version, routes, defs)


def refs(node: object, into: set[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if (
                key == "$ref"
                and isinstance(value, str)
                and value.startswith("#/$defs/")
            ):
                into.add(value.removeprefix("#/$defs/"))
            else:
                refs(value, into)
    elif isinstance(node, list):
        for value in node:
            refs(value, into)


def reach(defs: dict[str, dict], roots: list[object]) -> set[str]:
    """The `$defs` names the roots reference, and every name those reference in turn."""
    found: set[str] = set()
    refs(roots, found)
    pending = list(found)
    while pending:
        more: set[str] = set()
        refs(defs.get(pending.pop()), more)
        for name in more - found:
            found.add(name)
            pending.append(name)
    return found & defs.keys()


def fields(definition: dict | None) -> tuple[dict[str, dict], set[str]]:
    """A shape's properties and its required names; no shape (no body) has neither."""
    if definition is None:
        return {}, set()
    props = definition.get("properties")
    props = props if isinstance(props, dict) else {}
    required = definition.get("required")
    required = set(required) if isinstance(required, list) else set()
    return props, required


def defaulted(prop: object) -> bool:
    return isinstance(prop, dict) and "default" in prop


def nullable(prop: object) -> bool:
    """A field typed `null` too, which serde reads as `None` when it's absent."""
    if not isinstance(prop, dict):
        return False
    kind = prop.get("type")
    if kind == "null" or (isinstance(kind, list) and "null" in kind):
        return True
    return any(
        isinstance(branch, dict) and branch.get("type") == "null"
        for branch in prop.get("anyOf") or []
    )


Shape = tuple[str, dict]


class Comparison:
    """The routes both schemas have, compared shape by shape from the route down.

    A shape is held where a route uses it, not by its `$defs` name: a route whose request,
    query, response or stream event names another definition, or gains one it didn't have,
    is compared old shape against new shape, and so is each definition a shared field names
    in turn. A definition renamed with its shape kept is the same shape to a caller.
    """

    def __init__(self, base: Schema, current: Schema) -> None:
        self.base, self.current = base, current
        # Each break, with the routes it was found on, so a shape many routes share (FsQuery,
        # PollResponse) is named once.
        self.found: dict[str, list[str]] = {}

    def add(self, message: str, where: str | None = None) -> None:
        routes = self.found.setdefault(message, [])
        if where is not None and where not in routes:
            routes.append(where)

    @staticmethod
    def resolve(schema: Schema, node: object) -> Shape | None:
        """A body's schema as (name, definition), or None when there's no shape to read."""
        if not isinstance(node, dict):
            return None
        ref = node.get("$ref")
        if isinstance(ref, str):
            name = ref.removeprefix("#/$defs/")
            definition = schema.defs.get(name)
            return (name, definition) if isinstance(definition, dict) else None
        if isinstance(node.get("properties"), dict):
            return ("(inline)", node)
        return None

    def body(self, side: str, old: object, new: object, where: str) -> None:
        """One slot of a route (a `{media_type, schema}` body, or none) on both sides."""

        def schema_of(slot: object) -> object:
            return slot.get("schema") if isinstance(slot, dict) else None

        self.shape(
            side,
            self.resolve(self.base, schema_of(old)),
            self.resolve(self.current, schema_of(new)),
            where,
            frozenset(),
        )

    def shape(
        self,
        side: str,
        old: Shape | None,
        new: Shape | None,
        where: str,
        seen: frozenset[tuple[str, str]],
    ) -> None:
        if old is None and new is None:
            return
        if old is not None and new is not None:
            if (old[0], new[0]) in seen:
                return
            seen = seen | {(old[0], new[0])}
        old_props, old_required = fields(old[1] if old else None)
        new_props, new_required = fields(new[1] if new else None)
        old_name = old[0] if old else "(no body)"
        new_name = new[0] if new else "(no body)"
        if side == "request":
            # A route that had no body or query and gains one requires its required fields
            # of every older client, which sends none.
            for field in sorted(new_required - old_required):
                self.add(
                    f"request {new_name}.{field} is required now: an older client that "
                    "omits it is refused",
                    where,
                )
            for field in sorted(old_props.keys() - new_props.keys()):
                self.add(
                    f"request {old_name}.{field} is gone: an older client that sends it is "
                    "ignored, and an older daemon that needs it doesn't get it",
                    where,
                )
            # This client may leave such a field out now, and an older daemon still needs it.
            for field in sorted((old_required - new_required) & new_props.keys()):
                self.add(
                    f"request {new_name}.{field} isn't required now: this client may omit "
                    "it, and an older daemon that requires it refuses the request",
                    where,
                )
        else:
            if old is not None and new is None:
                self.add(
                    f"response {old_name} is gone: an older client can't read the answer",
                    where,
                )
                return
            for field in sorted(old_props.keys() - new_props.keys()):
                if field in old_required and not defaulted(old_props[field]):
                    self.add(
                        f"response {old_name}.{field} is gone: an older client requires it",
                        where,
                    )
            # Still there but no longer always written, which is what adding
            # `skip_serializing_if` does. A nullable field is exempt: serde reads an absent
            # `Option` as `None`, which is what the null it stops writing meant. A removed one
            # isn't (the rule above), since its value is gone, not only its null.
            for field in sorted((old_required - new_required) & new_props.keys()):
                prop = old_props.get(field)
                if not defaulted(prop) and not nullable(prop):
                    self.add(
                        f"response {new_name}.{field} may be absent now: an older client "
                        "requires it",
                        where,
                    )
            for field in sorted(new_required - old_required):
                if not defaulted(new_props.get(field)):
                    self.add(
                        f"response {new_name}.{field} is required now with no default: "
                        "this client can't read an older daemon's answer",
                        where,
                    )
        # A field both sides have holds the definitions it names to the same rule. One that
        # names one definition on each side is compared whatever the two names are; any
        # other change in what it names is a type change, which is out of scope.
        for field in sorted(old_props.keys() & new_props.keys()):
            before, after = set(), set()
            refs(old_props[field], before)
            refs(new_props[field], after)
            pairs = [(name, name) for name in sorted(before & after)]
            if not pairs and len(before) == len(after) == 1:
                pairs = [(before.pop(), after.pop())]
            for was, now in pairs:
                self.shape(
                    side,
                    self.resolve(self.base, {"$ref": f"#/$defs/{was}"}),
                    self.resolve(self.current, {"$ref": f"#/$defs/{now}"}),
                    where,
                    seen,
                )


def compare(base: Schema, current: Schema) -> dict[str, list[str]]:
    """Each break found, with the routes it was found on (none for a route-level one)."""
    comparison = Comparison(base, current)
    for key in sorted(base.routes.keys() - current.routes.keys()):
        comparison.add(f"route {key[0]} {key[1]} is gone")
    for key in sorted(base.routes.keys() & current.routes.keys()):
        old, new = base.routes[key], current.routes[key]
        label = f"{key[0]} {key[1]}"
        for code, error in sorted(statuses(old) - statuses(new)):
            named = f"{code} {error}" if error else f"{code}"
            comparison.add(f"status {named} of {label} is gone")
        old_events, new_events = events(old), events(new)
        for event in sorted(old_events.keys() - new_events.keys()):
            comparison.add(f"event {event} of {label} is gone")
        for slot in REQUEST_KEYS:
            comparison.body("request", old.get(slot), new.get(slot), label)
        comparison.body("response", old.get("response"), new.get("response"), label)
        for event in sorted(old_events.keys() & new_events.keys()):
            comparison.body(
                "response",
                old_events[event],
                new_events[event],
                f"{label} {event} event",
            )
    return comparison.found


def statuses(route: dict) -> set[tuple[int, str]]:
    return {
        (s.get("code"), s.get("error") or "")
        for s in route.get("statuses") or []
        if isinstance(s, dict)
    }


def events(route: dict) -> dict[str, dict]:
    return {
        e["event"]: e
        for e in route.get("sse_events") or []
        if isinstance(e, dict) and isinstance(e.get("event"), str)
    }


def git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
        )
    except FileNotFoundError:
        raise Refused(
            "git isn't on PATH, so there's no tag to read the base from"
        ) from None


def release_key(tag: str) -> tuple | None:
    match = RELEASE_TAG.match(tag)
    if match is None:
        return None
    major, minor, patch, pre = match.groups()
    # semver: a release sorts above its prereleases, numeric identifiers compare as numbers
    # and below alphanumeric ones, and a longer prerelease sorts above its own prefix.
    ids = tuple(
        (0, int(p), "") if p.isdigit() else (1, 0, p)
        for p in (pre or "").split(".")
        if p
    )
    return (int(major), int(minor), int(patch), pre is None, ids)


def previous_release(root: Path) -> str:
    listed = git(root, "tag", "--list", "v*", "--merged", "HEAD")
    if listed.returncode != 0:
        raise Refused(
            f"git tag --merged HEAD failed in {root}: {listed.stderr.strip()}"
        )
    keyed = [(k, t) for t in listed.stdout.split() if (k := release_key(t)) is not None]
    if keyed:
        return max(keyed)[1]
    shallow = git(root, "rev-parse", "--is-shallow-repository").stdout.strip() == "true"
    why = (
        "this is a shallow clone, so the tags' commits aren't in its history; "
        "`git fetch --unshallow --tags` fetches both"
        if shallow
        else "`git fetch --tags` fetches the tags if the clone skipped them"
    )
    raise Refused(
        f"no v* release tag is reachable from HEAD in {root}, so there's no previous "
        f"release to compare with: {why}, or name the base with --base-ref"
    )


def read_base(root: Path, ref: str) -> tuple[str, str]:
    where = f"{ref}:{SCHEMA}"
    # `--end-of-options` so a `--base-ref` that starts with a dash is read as a revision.
    shown = git(root, "show", "--end-of-options", where)
    if shown.returncode != 0:
        raise Refused(f"{ref} has no {SCHEMA}: {shown.stderr.strip()}")
    return shown.stdout, where


def read_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise Refused(f"can't read {path}: {error.strerror}") from None


def read_declared(path: Path) -> list[str]:
    """The breaks `docs/schema-breaks.toml` declares, each as the line this check prints."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as error:
        raise Refused(f"can't read {path}: {error.strerror}") from None
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise Refused(f"{path} isn't TOML: {error}") from None
    entries = doc.get("break", [])
    if not isinstance(entries, list):
        raise Refused(f"{path}: `break` is an array of tables")
    declared: list[str] = []
    for number, entry in enumerate(entries, 1):
        change = entry.get("change") if isinstance(entry, dict) else None
        why = entry.get("why") if isinstance(entry, dict) else None
        if not isinstance(change, str) or not change.strip():
            raise Refused(f"{path}: break {number} has no `change`")
        # The reason is what a reviewer approves, so a break without one isn't declared.
        if not isinstance(why, str) or not why.strip():
            raise Refused(f"{path}: break {number} ({change}) has no `why`")
        declared.append(change)
    return declared


def listed(problems: dict[str, list[str]], names: list[str]) -> None:
    for name in names:
        routes = problems.get(name) or []
        print(f"  - {name}" + (f" ({', '.join(routes)})" if routes else ""))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="the repository (default: this script's)",
    )
    parser.add_argument(
        "--schema", type=Path, help=f"the current schema (default: <root>/{SCHEMA})"
    )
    parser.add_argument(
        "--breaks",
        type=Path,
        help=f"the declared breaks (default: <root>/{BREAKS})",
    )
    base = parser.add_mutually_exclusive_group()
    base.add_argument(
        "--base-ref",
        help="the revision to compare with (default: the highest v* release tag reachable from HEAD)",
    )
    base.add_argument(
        "--base-file", type=Path, help="compare with this file instead of a revision"
    )
    args = parser.parse_args()

    try:
        if args.base_file is not None:
            base_text, base_where = read_file(args.base_file), str(args.base_file)
        else:
            ref = args.base_ref or previous_release(args.root)
            base_text, base_where = read_base(args.root, ref)
        schema_path = args.schema or args.root / SCHEMA
        old = parse(base_text, base_where)
        new = parse(read_file(schema_path), str(args.schema or SCHEMA))
        breaks_path = args.breaks or args.root / BREAKS
        declared = read_declared(breaks_path)
    except Refused as refused:
        print(f"schema-compat: {refused}", file=sys.stderr)
        return 2

    problems = compare(old, new)
    print(
        f"schema-compat: {new.where} against {old.where} "
        f"(protocol_version {old.protocol_version} -> {new.protocol_version})"
    )
    breaks_name = str(args.breaks or BREAKS)
    if old.protocol_version != new.protocol_version:
        undeclared = [p for p in problems if p not in declared]
        unmet = [d for d in declared if d not in problems]
        if undeclared or unmet:
            print(
                f"protocol_version changed from {old.protocol_version} to "
                f"{new.protocol_version}, and {breaks_name} has to list exactly the breaks "
                "it declares:"
            )
            if undeclared:
                print(
                    "not declared (add each as a [[break]] with its `change` and `why`):"
                )
                listed(problems, undeclared)
            if unmet:
                print("declared but not found (remove each):")
                for name in unmet:
                    print(f"  - {name}")
            return 1
        print(
            f"protocol_version changed from {old.protocol_version} to "
            f"{new.protocol_version}; each break below is declared in {breaks_name}"
            + ("" if problems else ", and there are none")
        )
        listed(problems, list(problems))
        return 0
    if problems:
        print("incompatible with the previous release:")
        listed(problems, list(problems))
        print(
            "Keep the old shape beside the new one, or, if the break is intended, bump "
            f"PROTOCOL_VERSION in protocol/src/lib.rs and declare the break in {BREAKS} "
            "(docs/PROTOCOL.md, Compatibility between releases)."
        )
        return 1
    if declared:
        # After the release that shipped them is tagged, the base carries the new version
        # too, and the entries waive nothing. Failing here would turn main red at the tag.
        print(
            f"note: {breaks_name} declares breaks, but protocol_version is "
            f"{new.protocol_version} on both sides, so they waive nothing; empty it"
        )
    print(
        "compatible: no route, status, event or field a caller of either side reads is gone or newly required"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
