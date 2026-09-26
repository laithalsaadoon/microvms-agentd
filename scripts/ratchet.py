#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
# SPDX-License-Identifier: Apache-2.0
"""Hold the repo's layering drift to a checked-in count that can only go down (#281).

`ratchet/drift.json` records every place a driving adapter does work that belongs below it.
This script collects the same findings from the tree, compares, and fails on either side of a
mismatch. A pass/fail rule has no memory of how much drift it accepted, so it can't tell whether
the drift is shrinking, and it can steer a violation sideways instead of down: the CLI's
thinness guard pushed an upload into an `aws s3 cp` subprocess it couldn't see.

# The file

    {
      "version": 1,
      "enforced": [],
      "entries":   [{"category": "placement", "key": "microvms-cli -> tar", "issue": 260}],
      "decisions": [{"category": "subprocess", "key": "...", "reason": "..."}]
    }

- An entry is drift with the issue that removes it. The entries are the count.
- A decision is a permanent exception with its reason. It covers its key and isn't counted.
- `enforced` lists the categories whose rule has moved into enforcing config (`PROMOTE`). It
  only quiets rule 4. The ratchet keeps collecting an enforced category and rules 1 and 2 still
  apply, so a finding there fails unless a decision covers it. An enforced category can't carry
  entries, so listing one never waives drift: the tree has to be clean of it first.
- Keys never carry a line number, so moving code inside a file doesn't churn the file. Two
  identical findings in one file share a key, so the key is listed once per occurrence.
- `update` writes the layout the checked-in file uses, one line per entry, so its diff shows
  only the entries it removed.

# The rules

1. A finding with no entry or decision: new drift.
2. An entry or decision with no finding: a fix the file doesn't record yet. `update` removes it.
3. An entry absent from the base branch's copy of the file, or a crate added to an allowed set
   the base's `arch/placement.toml` already has. This is what makes the count a ratchet: a PR
   can delete entries but not add them, and it can't widen a set to make a finding disappear.
   It can add a decision, which a reviewer sees in the diff with its reason, and it can add a
   set for a crate the base has none for. When the base has no drift file at all, the rule is
   skipped: that's the one PR that creates it.

   A move isn't an addition. A subprocess or port-impl key is `<path>: <text>`, so moving the
   code to another file (or another crate) or renaming what the text names re-keys it. A new
   key passes when it takes the place of a base entry of the same category and issue that the
   file no longer lists, and the two share their path or their text. Each base entry takes one
   replacement. Moving and renaming in one change shares neither, so it takes two PRs. An entry
   whose key is unchanged may name a different issue.
4. A collected category with no entries that isn't `enforced`: its rule is ready to enforce.

# The collectors

- placement: `cargo metadata --no-deps`, each crate's direct normal and build dependencies
  against its set in `arch/placement.toml`.
- subprocess: every `Command::new` in the `src/` of every shipping crate, including one inside
  a macro invocation such as `tokio::select!` or `vec![...]`. The shipping crates are the
  workspace's members minus the ones `Scope.non_shipping` names, so a crate added to the
  workspace is scanned from the commit that adds it, and moving a call into it isn't a fix.
- port-impl: every impl of a port in the `src/` of a driving adapter, of the use cases
  (`microvms-app`), or of the composition root (`microvms-core`). A port is a trait that a crate
  below the adapters declares `pub`. "Below" is every workspace crate an adapter reaches through
  its dependencies, so a port trait that moves between those crates is still a port. The
  composition root's own public traits are the prelude's extension traits, not ports. Only
  `microvms-edges`, where the production implementations belong, isn't read.

The two Rust collectors are ast-grep rules under `ratchet/`, and test code is out of both: an
item after `#[cfg(test)]`, `#[cfg(feature = "test-support")]` or
`#[cfg(any(test, feature = "test-support"))]`, a `#[test]` function, anything inside any of
them, and whole files that mark themselves with one of those `cfg`s or that a parent declares
with one (`#[cfg(test)] mod x;`). The `test-support` feature is the shared test doubles, which a
dependent turns on in its `[dev-dependencies]` only, so nothing behind it ships.

What they can't see, none of which the tree does today:

- `Command` imported under another name (`use std::process::Command as Cmd`).
- An impl written inside a `macro_rules!` body, since only its expansion names the type.
- `build.rs`. A build script runs on the machine that compiles the crate, not in anything that
  ships, so its subprocesses aren't layering drift.

They over-report any other `#[cfg(any(test, ...))]` form, which isn't recognized as test code.
That's visible in the key, not silent.

Each collector first runs against `ratchet/fixtures/sentinel/`, which must produce exactly the
findings its `expected.json` lists. A collector that finds nothing there fails the check, so a
broken tool invocation can't report zero drift.

Usage, from anywhere:

    ./scripts/ratchet.py [check]            # the gate
    ./scripts/ratchet.py update             # delete fixed entries; never adds one
    ./scripts/ratchet.py --base origin/main # compare with this ref (default: the merge base)
    ./scripts/ratchet.py --json             # the summary as JSON

With `$GITHUB_STEP_SUMMARY` set, the summary is also appended there as Markdown.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import textwrap
import tomllib
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import NamedTuple

ROOT = Path(__file__).resolve().parent.parent
DRIFT = "ratchet/drift.json"
PLACEMENT = "arch/placement.toml"
SGCONFIG = ROOT / "ratchet" / "sgconfig.yml"


class Scope(NamedTuple):
    """What the collectors read: a cargo workspace and the roles of the crates in it.

    Which crates are scanned comes from `cargo metadata`, not from here, so a crate the
    workspace gains is in scope without anyone remembering to list it.
    """

    root: Path
    #: The allowed dependency sets, one table per crate.
    placement: Path
    #: The driving adapters, by package name. Each needs a set in `placement`, and the
    #: port-impl collector reads their code.
    adapters: tuple[str, ...]
    #: Workspace members that never ship, by package name. Every other member is scanned.
    non_shipping: frozenset[str] = frozenset()
    #: Crates below the adapters whose code the port-impl collector reads too: the use
    #: cases, which implement a port only where a decision says why.
    composed: tuple[str, ...] = ()
    #: The composition root, by package name, which the port-impl collector also reads: it
    #: wires implementations in and may hold none itself (ARCH-8). Its own public traits are
    #: the extension traits that keep old call syntax on the types below it, not ports.
    composition_root: str | None = None


REPO = Scope(
    root=ROOT,
    placement=ROOT / PLACEMENT,
    adapters=("microvms-cli", "microvms-py", "microvms-js"),
    # `model/` is a proof harness: nothing depends on it and it's never published.
    non_shipping=frozenset({"agentd-model"}),
    # `microvms-edges` is the one crate the collector doesn't read below the adapters: it's
    # where port implementations belong.
    composed=("microvms-app",),
    composition_root="microvms-core",
)

SENTINEL_ROOT = ROOT / "ratchet" / "fixtures" / "sentinel"
SENTINEL = Scope(
    root=SENTINEL_ROOT,
    placement=SENTINEL_ROOT / "placement.toml",
    adapters=("adapter",),
    composed=("kernel",),
    composition_root="root",
)

COLLECTED = ("placement", "subprocess", "port-impl")

#: Categories #281 defines whose collector doesn't exist yet. The summary says so rather than
#: printing zero, because a zero would read as a measurement.
NOT_COLLECTED = {
    "adapter-logic": "until #273's semgrep thinness rules land",
    "parity-gap": "until #271's `parity:check --json` lands",
}

CATEGORIES = (*COLLECTED, *NOT_COLLECTED)

#: Where each category's rule goes once the category is empty (rule 4).
PROMOTE = {
    "placement": "microvms-cli/tests/dependency_direction.rs as an exact set per adapter (#285)",
    "subprocess": "each crate's clippy.toml as a disallowed type (#285)",
    "port-impl": "the semgrep thinness rules (#273)",
}


def describe(category: str, key: str) -> str:
    return f"[{category}] {key}"


# ── the file ────────────────────────────────────────────────────────────────


def parse(data: object, where: str) -> dict:
    """Validate a drift file's contents. Anything off is a hard failure naming `where`."""

    def fail(message: str):
        raise SystemExit(f"{where}: {message}")

    if not isinstance(data, dict) or set(data) != {
        "version",
        "enforced",
        "entries",
        "decisions",
    }:
        fail("expected exactly the keys version, enforced, entries, decisions")
    if data["version"] != 1:
        fail(f"unknown version {data['version']!r}")

    enforced = data["enforced"]
    if not isinstance(enforced, list) or len(set(enforced)) != len(enforced):
        fail("enforced must be a list of distinct category names")
    for category in enforced:
        if category not in COLLECTED:
            fail(f"enforced names {category!r}, which isn't a collected category")

    def records(name: str, extra: str) -> list[dict]:
        items = data[name]
        if not isinstance(items, list):
            fail(f"{name} must be a list")
        for item in items:
            if not isinstance(item, dict) or set(item) != {"category", "key", extra}:
                fail(f"each of {name} has exactly category, key, {extra}: {item!r}")
            category, key = item["category"], item["key"]
            if category not in CATEGORIES:
                fail(f"unknown category {category!r}")
            if category in NOT_COLLECTED:
                fail(
                    f"{category} is not collected yet ({NOT_COLLECTED[category]}), so the file "
                    "can't record it"
                )
            # Decisions stay: an enforcing rule has exceptions too (agentd's subprocesses).
            if category in enforced and name == "entries":
                fail(
                    f"{category} is enforced by {PROMOTE[category]}, so the file can't carry "
                    "entries for it"
                )
            if not isinstance(key, str) or not key.strip():
                fail(f"a key must be a non-empty string: {item!r}")
        return items

    entries = records("entries", "issue")
    for entry in entries:
        issue = entry["issue"]
        if isinstance(issue, bool) or not isinstance(issue, int) or issue <= 0:
            fail(f"an entry names the issue that removes it, as a number: {entry!r}")
    decisions = records("decisions", "reason")
    for decision in decisions:
        if not isinstance(decision["reason"], str) or not decision["reason"].strip():
            fail(f"a decision states its reason: {decision!r}")

    both = {(e["category"], e["key"]) for e in entries} & {
        (d["category"], d["key"]) for d in decisions
    }
    for category, key in sorted(both):
        fail(f"{describe(category, key)} is both an entry and a decision")
    return data


def counted(items: list[dict]) -> Counter:
    return Counter((item["category"], item["key"]) for item in items)


def read_file(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"{path} is missing")
    return parse(
        json.loads(path.read_text(encoding="utf-8")), str(path.relative_to(ROOT))
    )


def dump(data: dict) -> str:
    """The file's text, in the layout it's checked in with: one line per entry.

    `update` writes this, so removing an entry is a one-line diff rather than a reflow of the
    whole file. Decisions spread over lines because their reasons are prose.
    """

    def one_line(item: dict) -> str:
        return (
            "{ "
            + ", ".join(f"{json.dumps(k)}: {json.dumps(item[k])}" for k in item)
            + " }"
        )

    def block(name: str, items: list[dict], render) -> str:
        if not items:
            return f'  "{name}": []'
        body = ",\n".join(render(item) for item in items)
        return f'  "{name}": [\n{body}\n  ]'

    entries = [{k: e[k] for k in ("category", "key", "issue")} for e in data["entries"]]
    decisions = [
        {k: d[k] for k in ("category", "key", "reason")} for d in data["decisions"]
    ]
    parts = [
        f'  "version": {json.dumps(data["version"])}',
        f'  "enforced": {json.dumps(data["enforced"])}',
        block("entries", entries, lambda e: "    " + one_line(e)),
        block(
            "decisions",
            decisions,
            lambda d: textwrap.indent(json.dumps(d, indent=2), "    "),
        ),
    ]
    return "{\n" + ",\n".join(parts) + "\n}\n"


def show(root: Path, ref: str, path: str) -> str | None:
    """`path`'s text at `ref`, or None when `ref` has no such file. An unknown ref is an error."""
    commit = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "rev-parse",
            "--verify",
            "--quiet",
            f"{ref}^{{commit}}",
        ],
        capture_output=True,
        text=True,
    )
    if commit.returncode != 0:
        raise SystemExit(f"--base {ref} doesn't name a commit in {root}")
    spec = f"{commit.stdout.strip()}:{path}"
    if (
        subprocess.run(
            ["git", "-C", str(root), "cat-file", "-e", spec], capture_output=True
        ).returncode
        != 0
    ):
        return None
    return subprocess.run(
        ["git", "-C", str(root), "show", spec],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def read_base(root: Path, ref: str) -> dict | None:
    """The drift file at `ref`, or None when `ref` predates it."""
    text = show(root, ref, DRIFT)
    return None if text is None else parse(json.loads(text), f"{ref}:{DRIFT}")


def read_base_sets(root: Path, ref: str) -> dict | None:
    """The allowed sets at `ref`, or None when `ref` predates them."""
    text = show(root, ref, PLACEMENT)
    return None if text is None else sets_from(text, f"{ref}:{PLACEMENT}")


def default_base(root: Path) -> str:
    # Locally, rule 3 is only as good as `origin/main`: a fork whose origin predates the file
    # gets the bootstrap note and no rule 3. That's acceptable because the run that decides a
    # merge is CI's, which passes `--base` explicitly.
    out = subprocess.run(
        ["git", "-C", str(root), "merge-base", "HEAD", "origin/main"],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        raise SystemExit(
            "no merge base of HEAD and origin/main, so rule 3 has nothing to compare with. "
            "Fetch origin, or pass --base <ref>."
        )
    return out.stdout.strip()


# ── the rules ───────────────────────────────────────────────────────────────


def location_and_text(category: str, key: str) -> tuple[str | None, str | None]:
    """A Rust key's `<path>` and `<text>` halves. Placement keys name crates, not places."""
    if category == "placement" or ": " not in key:
        return None, None
    path, text = key.split(": ", 1)
    return path, text


def additions(file: dict, base: dict) -> list[dict]:
    """The file's entries that the base doesn't have and that no move accounts for (rule 3)."""
    here = Counter((e["category"], e["key"], e["issue"]) for e in file["entries"])
    there = Counter((e["category"], e["key"], e["issue"]) for e in base["entries"])
    added = sorted((here - there).elements())
    vacated = sorted((there - here).elements())

    def replaces(new: tuple, old: tuple) -> bool:
        if new[0] != old[0]:
            return False
        if new[1] == old[1]:
            return True
        if new[2] != old[2]:
            return False
        new_path, new_text = location_and_text(new[0], new[1])
        old_path, old_text = location_and_text(old[0], old[1])
        return new_path is not None and (new_path == old_path or new_text == old_text)

    unexplained = []
    for new in added:
        old = next((old for old in vacated if replaces(new, old)), None)
        if old is None:
            unexplained.append({"category": new[0], "key": new[1], "issue": new[2]})
        else:
            vacated.remove(old)
    return unexplained


def grown_sets(sets: dict, base_sets: dict | None, base_label: str) -> list[str]:
    """A crate added to an allowed set the base already has (rule 3's other half)."""
    if base_sets is None:
        return []
    failures = []
    for crate, kinds in sorted(sets.items()):
        if crate not in base_sets:
            continue
        for kind in ("normal", "build"):
            for dependency in sorted(kinds[kind] - base_sets[crate][kind]):
                failures.append(
                    f"set grew: {PLACEMENT} adds {dependency} to [{crate}] {kind}, which "
                    f"{base_label}'s copy doesn't allow. A set can only shrink: move the "
                    "work to the layer whose job it is, or add a decision for "
                    f"`{crate} -> {dependency}` with its reason."
                )
    return failures


def compare(
    current: Counter, file: dict, base: dict | None, base_label: str
) -> list[str]:
    """Every failure of the four rules over the drift file, in category order."""
    failures: list[str] = []
    entries = counted(file["entries"])
    recorded = entries + counted(file["decisions"])
    added = [] if base is None else additions(file, base)

    for category in COLLECTED:
        found = {k: n for (c, k), n in current.items() if c == category}
        listed = {k: n for (c, k), n in recorded.items() if c == category}
        for key in sorted(found.keys() | listed.keys()):
            if found.get(key, 0) > listed.get(key, 0):
                failures.append(
                    f"new drift: {describe(category, key)}. Move the work to the layer "
                    "whose job it is (I/O belongs in microvms-edges, behind a port in "
                    "microvms-app), or add a decision with its reason."
                )
            elif listed.get(key, 0) > found.get(key, 0):
                failures.append(
                    f"fixed: {describe(category, key)}. Run `mise run ratchet:update` "
                    "and commit the file."
                )
        for entry in added:
            if entry["category"] == category:
                failures.append(
                    f"not in the base: {describe(category, entry['key'])} is an entry here "
                    f"but not in {base_label}'s {DRIFT}, and it doesn't replace one there "
                    "that shares its path or its text. Entries can only be removed: fix the "
                    "code, or add a decision with its reason."
                )
        if category not in file["enforced"] and not any(
            c == category for c, _ in entries
        ):
            failures.append(
                f"promote {category}: move its rule into {PROMOTE[category]}, then list it "
                f"under enforced in {DRIFT}."
            )
    return failures


def updated(file: dict, current: Counter) -> tuple[dict, list[str]]:
    """The file with every entry and decision the tree no longer has removed. Adds nothing."""
    budget = Counter(current)
    removed: list[str] = []

    def keep(items: list[dict]) -> list[dict]:
        kept = []
        for item in items:
            finding = (item["category"], item["key"])
            if budget[finding] > 0:
                budget[finding] -= 1
                kept.append(item)
            else:
                removed.append(describe(*finding))
        return kept

    data = {
        **file,
        "entries": keep(file["entries"]),
        "decisions": keep(file["decisions"]),
    }
    return data, removed


# ── the summary ─────────────────────────────────────────────────────────────


def summary(file: dict, base: dict | None) -> dict:
    """Per category: its status, the drift count, the base's count, and its decisions."""
    entries = Counter(item["category"] for item in file["entries"])
    decisions = Counter(item["category"] for item in file["decisions"])
    base_entries = (
        None if base is None else Counter(e["category"] for e in base["entries"])
    )
    rows = {}
    for category in CATEGORIES:
        if category in NOT_COLLECTED:
            rows[category] = {
                "status": "not collected",
                "entries": None,
                "base": None,
                "decisions": None,
                "note": NOT_COLLECTED[category],
            }
            continue
        rows[category] = {
            "status": "enforced" if category in file["enforced"] else "collected",
            "entries": entries[category],
            "base": None if base_entries is None else base_entries[category],
            "decisions": decisions[category],
        }
    return rows


def change(row: dict) -> str:
    if row["base"] is None:
        return "new"
    delta = row["entries"] - row["base"]
    return f"{delta:+d}" if delta else "0"


def render_text(rows: dict) -> str:
    lines = [f"{'category':<14} {'drift':>5} {'change':>6} {'decisions':>9}"]
    for category, row in rows.items():
        if row["status"] == "not collected":
            lines.append(f"{category:<14} not collected ({row['note']})")
        else:
            line = f"{category:<14} {row['entries']:>5} {change(row):>6} {row['decisions']:>9}"
            if row["status"] == "enforced":
                line += f"  enforced, still collected (rule: {PROMOTE[category]})"
            lines.append(line)
    return "\n".join(lines)


def render_markdown(rows: dict, failures: list[str], base_note: str) -> str:
    lines = [
        "## Architecture drift",
        "",
        f"Compared with {base_note}.",
        "",
        "| Category | Drift | Change | Decisions |",
        "| --- | ---: | ---: | ---: |",
    ]
    for category, row in rows.items():
        if row["status"] == "not collected":
            lines.append(f"| {category} | not collected ({row['note']}) | | |")
        else:
            name = f"{category} (enforced)" if row["status"] == "enforced" else category
            lines.append(
                f"| {name} | {row['entries']} | {change(row)} | {row['decisions']} |"
            )
    if failures:
        lines += ["", "### Failures", "", *(f"- {f}" for f in failures)]
    return "\n".join(lines) + "\n"


# ── the collectors ──────────────────────────────────────────────────────────


def cargo_metadata(scope: Scope) -> dict:
    out = subprocess.run(
        [
            "cargo",
            "metadata",
            "--format-version",
            "1",
            "--no-deps",
            "--offline",
            "--manifest-path",
            str(scope.root / "Cargo.toml"),
        ],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        raise SystemExit(f"cargo metadata failed in {scope.root}:\n{out.stderr}")
    return json.loads(out.stdout)


def sets_from(text: str, where: str) -> dict[str, dict[str, set[str]]]:
    sets = tomllib.loads(text)
    for crate, table in sets.items():
        if not isinstance(table, dict) or not set(table) <= {"normal", "build"}:
            raise SystemExit(f"{where}: [{crate}] takes only normal and build lists")
    return {
        crate: {kind: set(table.get(kind, [])) for kind in ("normal", "build")}
        for crate, table in sets.items()
    }


def load_sets(scope: Scope) -> dict[str, dict[str, set[str]]]:
    return sets_from(scope.placement.read_text(encoding="utf-8"), str(scope.placement))


class Crates(NamedTuple):
    """The workspace's crates by role, each as its directory relative to the scope root."""

    #: Every member except `Scope.non_shipping`: the subprocess collector reads them all.
    shipping: dict[str, str]
    #: The driving adapters.
    adapters: dict[str, str]
    #: What the port-impl collector reads: the adapters, the composed crates and the root.
    readers: dict[str, str]
    #: The shipping crates the adapters reach through their dependencies: the ports live here.
    below: dict[str, str]


def crates(scope: Scope, metadata: dict, sets: dict) -> Crates:
    ids = set(metadata["workspace_members"])
    members = {p["name"]: p for p in metadata["packages"] if p["id"] in ids}
    root = scope.root.resolve()
    dirs = {
        name: Path(p["manifest_path"]).resolve().parent.relative_to(root).as_posix()
        for name, p in members.items()
    }
    for crate in sorted(sets):
        if crate not in members:
            raise SystemExit(
                f"{scope.placement} names [{crate}], which isn't a package in {scope.root}"
            )
    if stale := sorted(scope.non_shipping - members.keys()):
        raise SystemExit(f"non-shipping crates that aren't workspace members: {stale}")
    shipping = {n: d for n, d in dirs.items() if n not in scope.non_shipping}
    for crate in scope.adapters:
        if crate not in shipping:
            raise SystemExit(f"adapter {crate} isn't a shipping workspace member")
        if crate not in sets:
            raise SystemExit(f"adapter {crate} has no set in {scope.placement}")

    # Path dependencies between members, followed from the adapters.
    reached: set[str] = set()
    todo = list(scope.adapters)
    while todo:
        for dependency in members[todo.pop()]["dependencies"]:
            name = dependency["name"]
            if dependency["kind"] != "dev" and name in members and name not in reached:
                reached.add(name)
                todo.append(name)
    below = {
        n: d for n, d in shipping.items() if n in reached and n not in scope.adapters
    }
    inner = [*scope.composed, *filter(None, [scope.composition_root])]
    for crate in inner:
        if crate not in below:
            raise SystemExit(
                f"{crate} is read for port impls but isn't a shipping crate below the adapters"
            )
    return Crates(
        shipping=shipping,
        adapters={n: shipping[n] for n in scope.adapters},
        readers={n: shipping[n] for n in [*scope.adapters, *inner]},
        below=below,
    )


def placement(metadata: dict, sets: dict) -> Counter:
    # A set, then a Counter: a dependency repeated per target is one edge.
    edges = set()
    for package in metadata["packages"]:
        allowed = sets.get(package["name"])
        if allowed is None:
            continue
        for dependency in package["dependencies"]:
            kind = dependency["kind"] or "normal"
            if kind == "dev" or dependency["name"] in allowed[kind]:
                continue
            suffix = " (build)" if kind == "build" else ""
            edges.add(
                ("placement", f"{package['name']} -> {dependency['name']}{suffix}")
            )
    return Counter(edges)


def ast_grep(scope: Scope, shipping: dict[str, str]) -> list[dict]:
    dirs = [f"{directory}/src" for directory in sorted(shipping.values())]
    for directory in dirs:
        if not (scope.root / directory).is_dir():
            raise SystemExit(f"{scope.root / directory} doesn't exist")
    out = subprocess.run(
        ["ast-grep", "scan", "--config", str(SGCONFIG), "--json=stream", *dirs],
        cwd=scope.root,
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        raise SystemExit(f"ast-grep failed in {scope.root}:\n{out.stderr}")
    return [json.loads(line) for line in out.stdout.splitlines() if line.strip()]


def module_dir(path: PurePosixPath) -> PurePosixPath:
    """The directory a file's child modules live in, by Rust's module-path rules."""
    if path.name in ("lib.rs", "main.rs", "mod.rs"):
        return path.parent
    return path.parent / path.stem


def test_only_paths(matches: list[dict]) -> tuple[set[str], set[str]]:
    """Files compiled only for tests, and directories whose every file is.

    A declaration inside an inline module (`mod a { #[cfg(test)] mod b; }`) resolves as if it
    were at the file's top level. Nothing in this repo does that.
    """
    files: set[str] = set()
    dirs: set[str] = set()
    for match in matches:
        path = PurePosixPath(match["file"])
        if match["ruleId"] == "test-file":
            files.add(str(path))
            dirs.add(f"{module_dir(path)}/")
        elif match["ruleId"] == "test-mod-decl":
            child = module_dir(path) / variables(match)["NAME"]
            files |= {f"{child}.rs", f"{child}/mod.rs"}
            dirs.add(f"{child}/")
    return files, dirs


def variables(match: dict) -> dict[str, str]:
    return {
        k: v["text"]
        for k, v in match.get("metaVariables", {}).get("single", {}).items()
    }


def squash(text: str) -> str:
    return " ".join(text.split())


def trait_name(path: str) -> str:
    """`microvms_core::session::TokenMinter` -> `TokenMinter`, generics kept."""
    return re.sub(
        r"^(?:[A-Za-z_][A-Za-z0-9_]*::)+", "", re.sub(r"\s*::\s*", "::", path)
    )


def subprocess_key(file: str, function: str, arguments: str) -> tuple[str, str]:
    """One key for a call, whether it's written plainly or inside a macro's arguments."""
    function = re.sub(r"\s+", "", function)
    arguments = squash(arguments).strip().rstrip(",").strip()
    return ("subprocess", f"{file}: {function}({arguments})")


#: A `Command::new(` in a macro's raw tokens, with any path in front of it.
MACRO_CALL = re.compile(
    r"(?<![A-Za-z0-9_])((?:[A-Za-z_][A-Za-z0-9_]*\s*::\s*)*Command\s*::\s*new)\s*\("
)


def macro_calls(text: str) -> list[tuple[str, str]]:
    """Each `Command::new(...)` in a macro invocation's text, as its function and arguments.

    tree-sitter leaves a macro's arguments as unparsed tokens, so the `subprocess` rule can't
    see a call there. This reads the arguments to the matching parenthesis, stepping over
    string literals so a `)` inside one doesn't end them early.
    """
    calls = []
    for match in MACRO_CALL.finditer(text):
        depth, i, quoted = 1, match.end(), False
        while i < len(text) and depth:
            char = text[i]
            if quoted:
                if char == "\\":
                    i += 1
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            i += 1
        if depth == 0:
            calls.append((match.group(1), text[match.end() : i - 1]))
    return calls


def rust(scope: Scope, found: Crates, require_ports: bool) -> Counter:
    matches = ast_grep(scope, found.shipping)
    test_files, test_dirs = test_only_paths(matches)

    def shipped(match: dict) -> bool:
        path = match["file"]
        return path not in test_files and not any(path.startswith(d) for d in test_dirs)

    def under(match: dict, crate_dirs) -> bool:
        return any(match["file"].startswith(f"{d}/src/") for d in crate_dirs)

    matches = [m for m in matches if shipped(m)]
    declaring = [d for n, d in found.below.items() if n != scope.composition_root]
    ports = {
        variables(m)["NAME"]
        for m in matches
        if m["ruleId"] == "port-trait" and under(m, declaring)
    }
    # The sentinel proves the rule matches; this proves the tree still has ports to match, so
    # moving them somewhere the collector doesn't read can't empty the category.
    if require_ports and not ports:
        raise SystemExit(
            f"no public trait in the crates below the adapters ({sorted(found.below)}), so "
            "the port-impl collector has nothing to look for"
        )
    findings: Counter = Counter()
    for match in matches:
        mv = variables(match)
        if match["ruleId"] == "subprocess":
            findings[
                subprocess_key(match["file"], mv["FN"], mv["ARGS"].strip()[1:-1])
            ] += 1
        elif match["ruleId"] == "subprocess-in-macro":
            for function, arguments in macro_calls(match["text"]):
                findings[subprocess_key(match["file"], function, arguments)] += 1
        elif match["ruleId"] == "port-impl" and under(match, found.readers.values()):
            trait = squash(trait_name(mv["TRAIT"]))
            name = re.match(r"[A-Za-z_][A-Za-z0-9_]*", trait)
            if name is not None and name.group() in ports:
                findings[
                    ("port-impl", f"{match['file']}: {trait} for {squash(mv['TYPE'])}")
                ] += 1
    return findings


def collect(scope: Scope, require_ports: bool = False) -> Counter:
    """Every finding in `scope`, as a count per `(category, key)`."""
    metadata = cargo_metadata(scope)
    sets = load_sets(scope)
    found = crates(scope, metadata, sets)
    return placement(metadata, sets) + rust(scope, found, require_ports)


def sentinel(scope: Scope) -> list[str]:
    """Failures unless each collector reports exactly the scope's `expected.json`."""
    path = scope.root / "expected.json"
    expected = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    # No `require_ports`: an empty port-impl category is reported below as a failure.
    found = collect(scope)
    failures = []
    for category in COLLECTED:
        got = Counter({k: n for (c, k), n in found.items() if c == category})
        want = Counter(expected.get(category, []))
        if not got:
            failures.append(
                f"sentinel: the {category} collector found nothing in {scope.root}, so it "
                "can't be trusted to find drift in the tree either."
            )
        for key in sorted((got - want).elements()):
            failures.append(
                f"sentinel: unexpected {describe(category, key)} in {scope.root}"
            )
        for key in sorted((want - got).elements()):
            failures.append(
                f"sentinel: {describe(category, key)} was not reported in {scope.root}"
            )
    return failures


# ── entry point ─────────────────────────────────────────────────────────────


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "command", nargs="?", choices=("check", "update"), default="check"
    )
    parser.add_argument(
        "--base", help="the ref rule 3 compares with (default: the merge base)"
    )
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = parser.parse_args(argv)

    failures = sentinel(SENTINEL)
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1

    path = ROOT / DRIFT
    file = read_file(path)
    current = collect(REPO, require_ports=True)
    ref = args.base or default_base(ROOT)
    base_label = args.base or ref[:12]
    base = read_base(ROOT, ref)
    grown = grown_sets(load_sets(REPO), read_base_sets(ROOT, ref), base_label)

    if args.command == "update":
        data, removed = updated(file, current)
        path.write_text(dump(data), encoding="utf-8")
        for item in removed:
            print(f"removed {item}")
        file = parse(data, DRIFT)

    failures = compare(current, file, base, base_label) + grown
    rows = summary(file, base)
    base_note = (
        f"{base_label}, which has no {DRIFT}: this is the bootstrap, so rule 3 is skipped"
        if base is None
        else f"{base_label}"
    )
    if args.json:
        print(
            json.dumps(
                {
                    "base": base_label,
                    "bootstrap": base is None,
                    "categories": rows,
                    "failures": failures,
                },
                indent=2,
            )
        )
    else:
        print(f"ratchet: compared with {base_note}")
        print(render_text(rows))
    if step_summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(step_summary, "a", encoding="utf-8") as out:
            out.write(render_markdown(rows, failures, base_note))
    if failures:
        sys.stdout.flush()
        print("\n".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
