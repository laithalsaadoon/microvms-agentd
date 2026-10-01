#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
# SPDX-License-Identifier: Apache-2.0
"""Hold the repo's drift to its merge base's: a change can't add drift (#281).

Drift is every place a driving adapter does work that belongs below it, every capability a
surface lacks until an issue closes it (#271), every answer the shared case corpus marks a surface
as giving wrongly, or can't reach on it (#320), and every requirement no layer traces yet (#295).
This script collects it from the working tree and from the tree of the commit it compares with,
with the same collectors, and fails when the working tree has drift that commit doesn't: the
head's drift is a subset of the base's. The base is the merge base of HEAD and origin/main
unless `--base` names one; CI passes the pull request's base branch, or the commit before on a
push to main.

A pass/fail rule has no memory of how much drift it accepted, so it can't tell whether the drift
is shrinking, and it can steer a violation sideways instead of down: the CLI's thinness guard
pushed an upload into an `aws s3 cp` subprocess it couldn't see. Comparing with the base makes
the count a ratchet with no list to keep: a fix removes drift by fixing the code and edits
nothing else, and a change that adds drift fails whatever else it edits.

# What's written by hand

- `verify/ratchet/decisions.toml`: the permanent exceptions, each a finding with its reason,
  and `enforced`, the categories whose rule has moved into enforcing config (`PROMOTE`).

      enforced = ["adapter-logic"]

      [[decision]]
      category = "subprocess"
      key = "crates/agentd/src/exec.rs: Command::new(program)"
      reason = "runs the caller's argv inside the VM; executing the user's commands is agentd's job"

  A decision covers one finding of its key and takes it out of the drift. A change can add
  one, which a reviewer sees with its reason, in any category but untraced. Two identical
  findings in one file share a key, so the key is decided once per occurrence.
- `verify/arch/placement.toml`: each adapter's and layer's allowed set, and in a crate's
  `drift` table its placement drift, work that belongs below it (none since #260 moved
  directory sync into core). Placement is `HELD` (below), so its drift is that record rather
  than a finding.

Keys never carry a line number, so moving code inside a file changes nothing.

# The snapshot

`verify/ratchet/drift.json` is generated: the drift by category, and the decisions' count, at
the commit that last rewrote it, which the docs site charts (`tools/ratchet-history.py`).
`snapshot` writes it and `snapshot --check` fails while it's stale. The check never reads it, so
a change that fixes drift leaves it alone and two such changes can't conflict in it; a change of
its own rewrites it for the chart.

# The rules

1. Drift the base doesn't have: new drift.

   A move isn't an addition. A subprocess, port-impl or adapter-logic key is `<path>: <text>`,
   so moving the code to another file (or another crate) or renaming what the text names
   re-keys it. A new key passes when it takes the place of one of the same category that the
   base has and the working tree doesn't, and the two share their path or their text. Each
   base key takes one replacement. Moving and renaming in one change shares neither, so it
   takes two changes. A placement, parity-gap, parity-drift or untraced key names an edge, a
   table row, a case or a requirement, not a place in the code, so it never pairs: renaming a
   row that carries a gap, or a case that carries a marker, is new drift.
2. A decision no finding matches: it's stale, so it goes. A `HELD` category's decisions are
   its check's to hold.
3. A crate added to an allowed set the base's `verify/arch/placement.toml` already has. A set
   can shrink, and a table for a crate the base has none for is fine, but widening one would
   clear a finding without a reason.
4. A collected category with no drift that isn't `enforced`: its rule is ready to enforce. An
   enforced category with drift fails too, so listing one never waives drift.

A base whose tree has no `verify/ratchet/decisions.toml` predates rule 1, so rule 1 is skipped
and the summary says so: that's the change that moved the decisions out of `drift.json`. Rule 3
still reads its sets. A base that can't be read fails: a ref that names no commit, or a tree
the collectors refuse.

An input the working tree's reading can't take is a refusal: a file that doesn't parse or has
the wrong shape, a decision it can't hold, or a collector refusing what it reads (a case it
can't read, a spec with no requirements). A refusal fails the run and takes out only the
rules that read that input: a collector's refusal its own categories, a decision's refusal its
category, a refusal of the decisions file's shape or of `enforced` every category, and one of
`verify/arch/placement.toml` placement and rule 3. Every other category is still collected and
held, so one bad input doesn't hide a finding elsewhere. The refusals print first, and the
summary marks each category they left unread. The base is measured whole: a base with a
refused category would compare as having no drift there.

Both trees are measured by this script's collectors, not the base's, so a change to a collector
moves both sides at once: a rule that reaches further finds drift the base already had, and one
that reaches less shrinks both. The sentinel below, each collector's unit tests, and review of
the collector's own diff are what hold the collectors.

# The collectors

- placement (`HELD`): not collected here. `crates/microvms-cli/tests/dependency_direction.rs`
  computes each direct normal and build dependency of every crate with a set in
  `verify/arch/placement.toml` from `cargo metadata`, and holds it to exactly that set, the
  crate's `drift` and its placement decisions, so a new dependency and a fixed one both fail
  there. This script reads the drift from each tree's `placement.toml`, and rule 1 holds it
  like the rest.
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
- adapter-logic: in the `src/` of a driving adapter, a string literal that is exactly a
  control-plane operation name (`operation-literal`), and a default retyped as a number
  (`literal-default`: a `DEFAULT_*` constant, `Duration::from_*`, `.unwrap_or`, `wait_opts` or
  a literal added to a timeout). The key is `<path>: <rule id>: <text>`, the matched code with
  its whitespace squashed. Each rule's file says which shapes it reaches and which it can't.
- parity-gap: each exemption in `verify/parity/capabilities.toml` that names an issue, a capability a
  surface lacks until that issue closes it (#271). The key is the record key
  `tools/check-parity.py --exemptions` prints: `<capability>/<surface>`,
  `<Type>.<member>/<surface>` or `<name>/<surface>`. An exemption without an issue is the
  table's own decision and isn't read. `parity:check` holds the table to the surfaces, so a gap
  the table doesn't record fails there, not here. Enforced since #280 closed the last gap:
  `parity:check` refuses an exemption with an issue, and one that reaches the table is drift in
  an enforced category here. A table whose records are all missing reads as no gaps, so the
  collector refuses a table with no exemption at all.
- parity-drift: each `known_drift` path and each `skip` in the shared case corpus,
  `verify/parity/cases/<area>/<case>.json` (#320). A marker says a surface gives another answer
  than `expect` at those dot paths until its issue fixes it, and a skip that a surface the row
  names can't be reached offline for the case; the runners hold both to their shape and to the
  answers, and nothing else counted them. The key is `<area>/<case>/<surface>: known_drift
  <path>`, one a path, or `<area>/<case>/<surface>: skip`. A marker is counted beside the
  table's exemption for the same gap, not instead of it: one measures the surface and the other
  the answer, and both go when the gap closes. A skip that no open issue will close is a
  decision, with the trace id it names in its reason. A directory with no case, or a marker or
  skip the collector can't read, is an error.
- untraced: each requirement key in `verify/spec/core.symspec.json` and `verify/spec/agentd.symspec.json`
  that no group file in `verify/spec/traced/` lists, a requirement no layer checks (#295). The files
  are read with `tools/check-trace.py`'s own loader. The key is the bare spec key (`TRAP-1`);
  its group is the prefix, and the file that would list it is `verify/spec/traced/TRAP.toml`.
  `trace:check` holds a listed key to its layers, so a traced key missing one fails there, not
  here. A requirement that can't carry a layer waives it in its group's file with its reason
  rather than staying drift, and the category takes no decisions, since a decision would take
  a requirement out of the count with no layer checking it. A listed key that waives every layer
  is still drift: no layer checks it either. A requirement with no key is an error, since no
  group file can list it.

The Rust collectors are ast-grep rules under `verify/ratchet/`, and test code is out of all of them: an
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

Each collector first runs against `verify/ratchet/fixtures/sentinel/`, which must produce exactly the
findings its `expected.json` lists. A collector that finds nothing there fails the check, so a
broken tool invocation can't report zero drift. Its categories then get no rule on the working
tree, since what it finds there isn't a measurement either, and the other categories still do.
A `HELD` category has no collector here, so the sentinel has nothing to prove for it: its
check's own tests do that.

Usage, from anywhere:

    ./tools/ratchet.py [check]            # the gate
    ./tools/ratchet.py --base origin/main # compare with this commit (default: the merge base)
    ./tools/ratchet.py --json             # the summary as JSON
    ./tools/ratchet.py snapshot           # rewrite verify/ratchet/drift.json
    ./tools/ratchet.py snapshot --check   # fail while it's stale, writing nothing

With `$GITHUB_STEP_SUMMARY` set, the summary is also appended there as Markdown.
"""

import argparse
import ast
import io
import json
import os
import re
import runpy
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import NamedTuple

ROOT = Path(__file__).resolve().parent.parent
DRIFT = "verify/ratchet/drift.json"
DECISIONS = "verify/ratchet/decisions.toml"
SCRIPT = "tools/ratchet.py"
PLACEMENT = "verify/arch/placement.toml"
SGCONFIG = ROOT / "verify" / "ratchet" / "sgconfig.yml"
CHECK_PARITY = ROOT / "tools" / "check-parity.py"
CHECK_TRACE = ROOT / "tools" / "check-trace.py"


class Scope(NamedTuple):
    """What the collectors read: a cargo workspace and the roles of the crates in it.

    Which crates are scanned comes from `cargo metadata`, not from here, so a crate the
    workspace gains is in scope without anyone remembering to list it.
    """

    root: Path
    #: The driving adapters, by package name. The port-impl and adapter-logic collectors read
    #: their code.
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
    #: The capability table whose tracked exemptions are parity gaps. None reads no gaps.
    parity_table: Path | None = None
    #: The case corpus whose `known_drift` markers and skips are parity drift. None reads none.
    parity_cases: Path | None = None
    #: The requirement specs whose keys the untraced collector reads.
    specs: tuple[Path, ...] = ()
    #: The directory of group files that list the traced keys. None reads no untraced keys.
    traced: Path | None = None
    #: The script whose `load_traced` reads `traced` and whose `LAYERS` names the layers.
    #: check-trace.py for every real scope, so this reads the files the way trace:check does.
    trace: Path = CHECK_TRACE


def repo_scope(root: Path) -> Scope:
    """The repository's scope over the tree at `root`: this checkout's, or a base's copy."""
    return Scope(
        root=root,
        adapters=("microvms-cli", "microvms-py", "microvms-js"),
        # `crates/model/` is a proof harness, and `crates/model-conformance/` holds only tests that drive the app
        # over the model's rows. Only tests depend on either, and neither is ever published.
        non_shipping=frozenset({"agentd-model", "model-conformance"}),
        # `microvms-edges` is the one crate the collector doesn't read below the adapters: it's
        # where port implementations belong.
        composed=("microvms-app",),
        composition_root="microvms-core",
        parity_table=root / "verify" / "parity" / "capabilities.toml",
        parity_cases=root / "verify" / "parity" / "cases",
        specs=(
            root / "verify" / "spec" / "core.symspec.json",
            root / "verify" / "spec" / "agentd.symspec.json",
        ),
        traced=root / "verify" / "spec" / "traced",
    )


REPO = repo_scope(ROOT)

SENTINEL_ROOT = ROOT / "verify" / "ratchet" / "fixtures" / "sentinel"
SENTINEL = Scope(
    root=SENTINEL_ROOT,
    adapters=("adapter",),
    composed=("kernel",),
    composition_root="root",
    parity_table=SENTINEL_ROOT / "parity" / "capabilities.toml",
    parity_cases=SENTINEL_ROOT / "parity" / "cases",
    specs=(
        SENTINEL_ROOT / "spec" / "core.symspec.json",
        SENTINEL_ROOT / "spec" / "agentd.symspec.json",
    ),
    traced=SENTINEL_ROOT / "spec" / "traced",
)

#: Every category the ratchet counts. `tools/ratchet-history.py` reads this tuple from each
#: commit's copy of this script, with `ast`, to tell a category nobody counted yet from a zero.
COLLECTED = (
    "placement",
    "subprocess",
    "port-impl",
    "adapter-logic",
    "parity-gap",
    "parity-drift",
    "untraced",
)

#: Collected categories whose findings another check computes, by the check. That check holds
#: the category's record to the tree, so this script has no collector for it: it reads the
#: record from each tree and compares the two like any drift. Placement is
#: `dependency_direction.rs`'s, which computes the edges from `cargo metadata` for every crate
#: with a set, so the edges are computed in one place.
HELD = {"placement": "crates/microvms-cli/tests/dependency_direction.rs"}

#: Categories defined before their collector exists. The summary says so rather than printing
#: zero, because a zero would read as a measurement. Empty since #271 collected parity-gap.
NOT_COLLECTED: dict[str, str] = {}

#: The ast-grep rules the adapter-logic collector reads, by rule id.
ADAPTER_LOGIC = ("operation-literal", "literal-default")

CATEGORIES = (*COLLECTED, *NOT_COLLECTED)

#: Where each category's rule goes once the category is empty (rule 4).
PROMOTE = {
    # dependency_direction.rs holds every set exactly, drift tables included, so with #260's
    # drift gone the promotion was only listing the category.
    "placement": "crates/microvms-cli/tests/dependency_direction.rs, which holds every set exactly (#260)",
    "subprocess": "each crate's clippy.toml as a disallowed type (#285)",
    # These two stay in the ratchet's own ast-grep rules: once enforced, the collector is the
    # hard gate, since a finding without a decision is drift, and an enforced category can't
    # carry any. Not semgrep: #281 measured that its `impl $T for $U` matches every impl, and it
    # can't skip inline test modules.
    "port-impl": "verify/ratchet/rules/port-impl.yml as a hard gate (#270)",
    "adapter-logic": (
        "verify/ratchet/rules/operation-literal.yml and literal-default.yml as a hard gate (#273)"
    ),
    "parity-gap": "tools/check-parity.py refusing an exemption with an issue (#280)",
    "parity-drift": (
        "every corpus runner refusing a known_drift marker, and a decision for each skip no "
        "issue will close (#258)"
    ),
    "untraced": (
        "tools/check-trace.py failing on a spec key no file in verify/spec/traced/ lists, once #301 "
        "to #307 trace the last key"
    ),
}


#: What rule 1 tells the reader to do with new drift. A parity gap or an untraced requirement
#: isn't work in the wrong layer, and placement drift is a record another check holds.
NEW_DRIFT_FIX = {
    "placement": (
        "A crate's drift in verify/arch/placement.toml can only shrink: move the work to the "
        f"layer whose job it is, or add a decision for the edge with its reason in {DECISIONS}."
    ),
    "parity-gap": (
        "Give that surface the capability, or, if the gap is permanent, drop the exemption's "
        "issue in verify/parity/capabilities.toml so it reads as a decision."
    ),
    "parity-drift": (
        "Make the surface give the case's answer rather than marking or skipping it there. A "
        "skip no open issue will close takes a decision naming the trace id it cites, with its "
        f"reason, in {DECISIONS}."
    ),
    "untraced": (
        "Trace the requirement: list its key in its group's file, verify/spec/traced/<GROUP>.toml, "
        "give it each layer or a waiver with its reason (a key that waives every layer stays "
        "untraced), and run ./tools/check-trace.py --write."
    ),
}
LAYERING_FIX = (
    "Move the work to the layer whose job it is (I/O belongs in microvms-edges, behind a port "
    f"in microvms-app), or add a decision with its reason in {DECISIONS}."
)

#: Categories a decision can't name, with what to do instead.
NO_DECISIONS = {
    "untraced": (
        "a requirement that can't carry a layer waives that layer in its group's file, "
        "verify/spec/traced/<GROUP>.toml, with its reason, so it stays traced"
    ),
}


def describe(category: str, key: str) -> str:
    return f"[{category}] {key}"


# ── the files ───────────────────────────────────────────────────────────────


class Refusal(NamedTuple):
    """An input a tree's reading couldn't take, and the categories whose rules read it."""

    #: What was refused, naming its file or its collector.
    message: str
    #: The categories it leaves unread: none of their rules run.
    categories: frozenset[str]


#: What a refusal of a whole file leaves unread: every category's rules read it.
EVERY = frozenset(COLLECTED)


def read_decisions(data: object, where: str) -> tuple[dict, list[Refusal]]:
    """A decisions file's contents, and each thing in it that's refused, naming `where`.

    The result has `enforced`, a list of categories, and `decisions`, a list of tables with
    exactly `category`, `key` and `reason`. A refused decision isn't in it and leaves its own
    category unread, or every category when it names none this reads. A refusal of the file's
    shape or of `enforced` leaves every category unread, since each one's rules read both.
    """
    refused: list[Refusal] = []

    def refuse(message: str, categories: frozenset[str] = EVERY) -> None:
        refused.append(Refusal(f"{where}: {message}", categories))

    # `decision` may be absent: TOML has no empty array of tables. `enforced` may not, so a
    # file that lost its first line doesn't read as nothing enforced.
    if (
        not isinstance(data, dict)
        or "enforced" not in data
        or not set(data) <= {"enforced", "decision"}
    ):
        refuse("expected `enforced` and the [[decision]] tables, and nothing else")
        return {"enforced": [], "decisions": []}, refused
    enforced = data["enforced"]
    if (
        not isinstance(enforced, list)
        or not all(isinstance(category, str) for category in enforced)
        or len(set(enforced)) != len(enforced)
    ):
        refuse("enforced must be a list of distinct category names")
        enforced = []
    elif unknown := [category for category in enforced if category not in COLLECTED]:
        for category in unknown:
            refuse(f"enforced names {category!r}, which isn't a collected category")
        enforced = []

    decisions = data.get("decision", [])
    if not isinstance(decisions, list):
        refuse("each decision is a [[decision]] table")
        decisions = []
    kept = []
    for item in decisions:
        named = item.get("category") if isinstance(item, dict) else None
        own = frozenset([named]) if named in CATEGORIES else EVERY
        if not isinstance(item, dict) or set(item) != {"category", "key", "reason"}:
            refuse(f"each decision has exactly category, key and reason: {item!r}", own)
            continue
        category, key, reason = item["category"], item["key"], item["reason"]
        if category not in CATEGORIES:
            refuse(f"unknown category {category!r}")
            continue
        if category in NOT_COLLECTED:
            refuse(
                f"{category} is not collected yet ({NOT_COLLECTED[category]}), so nothing can "
                "be decided in it",
                own,
            )
            continue
        if not isinstance(key, str) or not key.strip():
            refuse(f"a key must be a non-empty string: {item!r}", own)
            continue
        if not isinstance(reason, str) or not reason.strip():
            refuse(f"a decision states its reason: {item!r}", own)
            continue
        if category in NO_DECISIONS:
            refuse(
                f"{describe(category, key)} can't be a decision: {NO_DECISIONS[category]}",
                own,
            )
            continue
        kept.append(item)
    return {"enforced": list(enforced), "decisions": kept}, refused


def parse_decisions(data: object, where: str) -> dict:
    """Validate a decisions file's contents. Anything off is a hard failure naming `where`: the
    first refusal `read_decisions` makes.

    The result has `enforced`, a list of categories, and `decisions`, a list of tables with
    exactly `category`, `key` and `reason`.
    """
    decisions, refused = read_decisions(data, where)
    if refused:
        raise SystemExit(refused[0].message)
    return decisions


def placement_from(text: str, where: str) -> dict[str, dict]:
    """A `verify/arch/placement.toml`'s tables: each crate's allowed set and its drift.

    Each crate maps to `normal` and `build`, the sets, and `drift`, the same two kinds for its
    placement drift. What else the file must hold is `dependency_direction.rs`'s to check.
    """
    try:
        tables = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise SystemExit(f"{where}: {error}") from None

    def names(table: dict, kind: str, name: str) -> set[str]:
        value = table.get(kind, [])
        if not isinstance(value, list) or not all(isinstance(n, str) for n in value):
            raise SystemExit(f"{where}: [{name}] {kind} is a list of crate names")
        return set(value)

    sets = {}
    for crate, table in tables.items():
        if not isinstance(table, dict) or not set(table) <= {
            "normal",
            "build",
            "drift",
        }:
            raise SystemExit(
                f"{where}: [{crate}] takes only normal and build lists and a drift table"
            )
        drift = table.get("drift", {})
        if not isinstance(drift, dict) or not set(drift) <= {"normal", "build"}:
            raise SystemExit(
                f"{where}: [{crate}.drift] takes only normal and build lists"
            )
        sets[crate] = {
            **{kind: names(table, kind, crate) for kind in ("normal", "build")},
            "drift": {
                kind: names(drift, kind, f"{crate}.drift")
                for kind in ("normal", "build")
            },
        }
    return sets


def edge(crate: str, dependency: str, kind: str) -> str:
    """A placement key. A build dependency's name carries ` (build)`, so a record for a normal
    edge never stands for a build edge or the other way round."""
    return f"{crate} -> {dependency}{' (build)' if kind == 'build' else ''}"


def placement_drift(sets: dict) -> Counter:
    """The placement drift `placement.toml` records, one finding per edge."""
    return Counter(
        ("placement", edge(crate, dependency, kind))
        for crate, table in sets.items()
        for kind in ("normal", "build")
        for dependency in table["drift"][kind]
    )


class Tree(NamedTuple):
    """One commit's drift, and what rules 2 to 4 read beside it."""

    #: What the collectors found, before any decision.
    findings: Counter
    #: `decisions.toml`, parsed.
    decisions: dict
    #: `placement.toml`'s sets and drift.
    sets: dict
    #: The findings no decision covers, and the placement drift: what rule 1 compares.
    drift: Counter
    #: What its reading refused, in the order it read the inputs.
    refused: tuple[Refusal, ...] = ()

    @property
    def unread(self) -> frozenset[str]:
        """The categories a refusal left unread."""
        return frozenset().union(*(refusal.categories for refusal in self.refused))


def read_text(path: Path, name: str) -> str:
    # Read, not stat then read: CodeQL flags the gap between the two.
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise SystemExit(f"{name} is missing") from None


def read_tree(scope: Scope, where: str = "") -> Tree:
    """The drift of the tree at `scope.root`. `where` prefixes a file's name in a refusal.

    An input it can't read is a refusal in the result, not the end of the reading: the
    placement file leaves placement unread, and rule 3 with it; the decisions file, or one of
    its decisions, what `read_decisions` says; and each collector its own categories. Every
    other input is still read, so a tree with one bad input still shows what the rest hold.
    """
    refused: list[Refusal] = []
    try:
        sets = placement_from(
            read_text(scope.root / PLACEMENT, f"{where}{PLACEMENT}"),
            f"{where}{PLACEMENT}",
        )
    except SystemExit as error:
        refused.append(Refusal(str(error), frozenset(["placement"])))
        sets = {}
    decisions: dict = {"enforced": [], "decisions": []}
    try:
        data = tomllib.loads(read_text(scope.root / DECISIONS, f"{where}{DECISIONS}"))
    except SystemExit as error:
        refused.append(Refusal(str(error), EVERY))
    except tomllib.TOMLDecodeError as error:
        refused.append(Refusal(f"{where}{DECISIONS}: {error}", EVERY))
    else:
        decisions, more = read_decisions(data, f"{where}{DECISIONS}")
        refused += more
    findings, more = collect_each(scope, require_ports=True)
    refused += more
    covered = Counter(
        (d["category"], d["key"])
        for d in decisions["decisions"]
        if d["category"] not in HELD
    )
    return Tree(
        findings,
        decisions,
        sets,
        findings - covered + placement_drift(sets),
        tuple(refused),
    )


def whole(tree: Tree) -> Tree:
    """`tree`, or its first refusal raised: a base is measured whole or not at all."""
    if tree.refused:
        raise SystemExit(tree.refused[0].message)
    return tree


class Base(NamedTuple):
    """The commit a change is compared with."""

    #: How the summary and the failures name it: `--base` as given, or the merge base's sha.
    label: str
    #: Its `placement.toml`'s sets, for rule 3, or None when it has none.
    sets: dict | None
    #: Its drift, or None when its tree predates `decisions.toml` and rule 1 is skipped.
    tree: Tree | None


# The pointers a git hook exports, as in check-guards-fire.py. Every git call here names its
# repo with `-C root`, and an inherited `GIT_DIR` overrides that: the tests' throwaway repos
# would be read through the hook's repo instead (#311). In the real checkout the two name the
# same repo, so dropping them changes no answer there.
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
    """`os.environ` without the inherited git pointers, read at call time."""
    return {k: v for k, v in os.environ.items() if k not in GIT_ENV_LEAKS}


def commit_of(root: Path, ref: str) -> str:
    """The commit `ref` names. An unknown ref is an error, not a base with nothing in it."""
    out = subprocess.run(
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
        env=clean_env(),
    )
    if out.returncode != 0:
        raise SystemExit(f"--base {ref} doesn't name a commit in {root}")
    return out.stdout.strip()


def read_base(root: Path, ref: str, label: str) -> Base:
    """The base's tree, exported from git and measured with this script's collectors.

    Not its own copy of this script: both trees are measured one way, so a change to a
    collector moves both sides and never reads as drift added or fixed. Anything that stops the
    measurement is an error, since a base with no drift would pass every change.
    """
    commit = commit_of(root, ref)
    archive = subprocess.run(
        ["git", "-C", str(root), "archive", "--format=tar", commit],
        capture_output=True,
        env=clean_env(),
    )
    if archive.returncode != 0:
        raise SystemExit(
            f"can't export {label}'s tree to compare with:\n"
            + archive.stderr.decode(errors="replace")
        )
    with tempfile.TemporaryDirectory(prefix="ratchet-base-") as directory:
        tree = Path(directory)
        with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
            tar.extractall(tree, filter="data")
        where = f"{label}:"
        # Read, not stat then read: CodeQL flags the gap between the two.
        try:
            text = (tree / PLACEMENT).read_text(encoding="utf-8")
        except FileNotFoundError:
            sets = None
        else:
            sets = placement_from(text, f"{where}{PLACEMENT}")
        try:
            (tree / DECISIONS).read_bytes()
        except FileNotFoundError:
            return Base(label, sets, None)
        try:
            return Base(label, sets, whole(read_tree(repo_scope(tree), where)))
        except SystemExit as error:
            raise SystemExit(
                f"{label}'s tree can't be measured, so there's nothing to compare with: "
                f"{error}"
            ) from None


def show(root: Path, ref: str, path: str) -> str | None:
    """`path`'s text at `ref`, or None when `ref` has no such file. An unknown ref is an error."""
    spec = f"{commit_of(root, ref)}:{path}"
    if (
        subprocess.run(
            ["git", "-C", str(root), "cat-file", "-e", spec],
            capture_output=True,
            env=clean_env(),
        ).returncode
        != 0
    ):
        return None
    return subprocess.run(
        ["git", "-C", str(root), "show", spec],
        capture_output=True,
        text=True,
        check=True,
        env=clean_env(),
    ).stdout


def read_base_collected(root: Path, ref: str) -> tuple[str, ...]:
    """The categories `ref`'s own ratchet collects, from the `COLLECTED` in its script.

    The history reads it for each commit, to tell a category that commit didn't count from a
    zero. Read with `ast`, not run: the script is code from another commit. A script with no
    literal `COLLECTED` is an error rather than an empty tuple, which would read as nothing
    counted.
    """
    text = show(root, ref, SCRIPT)
    if text is None:
        raise SystemExit(
            f"{ref} has no {SCRIPT}, so there's no telling which categories it collected"
        )
    return collected_from(text, f"{ref}:{SCRIPT}")


def collected_from(text: str, where: str) -> tuple[str, ...]:
    """The literal `COLLECTED` tuple in a ratchet script's text. `where` names it in an error."""
    for node in ast.parse(text).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "COLLECTED"
            for target in node.targets
        ):
            try:
                value = ast.literal_eval(node.value)
            except ValueError:
                break
            if (
                isinstance(value, tuple)
                and value
                and all(isinstance(c, str) for c in value)
            ):
                return value
            break
    raise SystemExit(
        f"{where} has no COLLECTED tuple of category names, so there's no telling which "
        "categories it collected"
    )


def merge_base_with(root: Path, ref: str) -> str:
    """The merge base of HEAD and `ref`, which is what rule 1 compares with. Not `ref` itself:
    CI passes `--base origin/main`, and main can move past the commit a pull request's merge
    was made from while the job runs; comparing with main's tip would count drift main fixed
    in the meantime as drift the pull request adds."""
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
        env=clean_env(),
    )
    if commit.returncode != 0:
        raise SystemExit(f"--base {ref} doesn't name a commit")
    out = subprocess.run(
        ["git", "-C", str(root), "merge-base", "HEAD", commit.stdout.strip()],
        capture_output=True,
        text=True,
        env=clean_env(),
    )
    if out.returncode != 0:
        raise SystemExit(f"HEAD and --base {ref} have no merge base")
    return out.stdout.strip()


def default_base(root: Path) -> str:
    # Locally, rule 1 is only as good as `origin/main`: a clone whose origin is behind compares
    # with an older tree. That's acceptable because the run that decides a merge is CI's, which
    # passes `--base` explicitly.
    out = subprocess.run(
        ["git", "-C", str(root), "merge-base", "HEAD", "origin/main"],
        capture_output=True,
        text=True,
        env=clean_env(),
    )
    if out.returncode != 0:
        raise SystemExit(
            "no merge base of HEAD and origin/main, so rule 1 has nothing to compare with. "
            "Fetch origin, or pass --base <ref>."
        )
    return out.stdout.strip()


# ── the rules ───────────────────────────────────────────────────────────────


def location_and_text(category: str, key: str) -> tuple[str | None, str | None]:
    """A Rust key's `<path>` and `<text>` halves. Placement, parity-gap, parity-drift and untraced
    keys name edges, table rows, cases and requirements, not places."""
    if (
        category in ("placement", "parity-gap", "parity-drift", "untraced")
        or ": " not in key
    ):
        return None, None
    path, text = key.split(": ", 1)
    return path, text


def additions(head: Counter, base: Counter) -> list[tuple[str, str]]:
    """The head's drift the base doesn't have and no move accounts for (rule 1)."""
    added = sorted((head - base).elements())
    vacated = sorted((base - head).elements())

    def replaces(new: tuple[str, str], old: tuple[str, str]) -> bool:
        if new[0] != old[0]:
            return False
        new_path, new_text = location_and_text(*new)
        old_path, old_text = location_and_text(*old)
        return new_path is not None and (new_path == old_path or new_text == old_text)

    unexplained = []
    for new in added:
        old = next((old for old in vacated if replaces(new, old)), None)
        if old is None:
            unexplained.append(new)
        else:
            vacated.remove(old)
    return unexplained


def grown_sets(sets: dict, base_sets: dict | None, base_label: str) -> list[str]:
    """A crate added to an allowed set the base already has (rule 3)."""
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
                    f"`{edge(crate, dependency, kind)}` with its reason."
                )
    return failures


def rules(head: Tree, base: Base, untrusted: frozenset[str] = frozenset()) -> list[str]:
    """Every failure of the four rules, in category order, then rule 3's.

    A category the head left unread, or whose collector failed the sentinel (`untrusted`), gets
    no rule: its findings aren't a measurement, and the refusal or the sentinel's failure fails
    the run already. Rule 3 reads the placement file alone, so only its refusal skips rule 3.
    """
    skipped = head.unread | untrusted
    failures: list[str] = []
    enforced = head.decisions["enforced"]
    decided = Counter(
        (d["category"], d["key"])
        for d in head.decisions["decisions"]
        if d["category"] not in HELD
    )
    stale = decided - head.findings
    added = [] if base.tree is None else additions(head.drift, base.tree.drift)

    for category in COLLECTED:
        if category in skipped:
            continue
        for c, key in added:
            if c == category:
                failures.append(
                    f"new drift: {describe(category, key)}. "
                    + NEW_DRIFT_FIX.get(category, LAYERING_FIX)
                )
        for c, key in sorted(stale.elements()):
            if c == category:
                failures.append(
                    f"stale decision: {describe(category, key)} decides a finding the tree "
                    f"doesn't have. Delete it from {DECISIONS}."
                )
        drifting = sorted(k for c, k in head.drift.elements() if c == category)
        if category in enforced and drifting:
            failures.append(
                f"{category} is enforced by {PROMOTE[category]}, so it can't carry drift: "
                f"{'; '.join(drifting)}. Fix it, or take {category} out of enforced in "
                f"{DECISIONS}."
            )
        elif category not in enforced and not drifting:
            failures.append(
                f"promote {category}: move its rule into {PROMOTE[category]}, then list it "
                f"under enforced in {DECISIONS}."
            )
    if "placement" in head.unread:
        return failures
    return failures + grown_sets(head.sets, base.sets, base.label)


# ── the summary and the snapshot ───────────────────────────────────────────


def summary(head: Tree, base: Base, untrusted: frozenset[str] = frozenset()) -> dict:
    """Per category: its status, the drift here and at the base, and its decisions.

    With no base drift (rule 1 skipped), the base's count is None, so its change reads "new".
    A category `rules` skips (`untrusted`, or one the head left unread) has no count either:
    what its collector returned isn't a measurement.
    """
    drift = Counter(category for category, _ in head.drift.elements())
    base_drift = (
        None
        if base.tree is None
        else Counter(category for category, _ in base.tree.drift.elements())
    )
    decisions = Counter(d["category"] for d in head.decisions["decisions"])
    rows = {}
    for category in CATEGORIES:
        if category in NOT_COLLECTED:
            rows[category] = {
                "status": "not collected",
                "drift": None,
                "base": None,
                "decisions": None,
                "note": NOT_COLLECTED[category],
            }
            continue
        if category in head.unread or category in untrusted:
            rows[category] = {
                "status": "unread",
                "drift": None,
                "base": None,
                "decisions": None,
                "note": "an input it reads was refused"
                if category in head.unread
                else "its collector failed the sentinel",
            }
            continue
        rows[category] = {
            "status": "enforced"
            if category in head.decisions["enforced"]
            else "collected",
            "drift": drift[category],
            "base": None if base_drift is None else base_drift[category],
            "decisions": decisions[category],
        }
    return rows


def change(row: dict) -> str:
    if row["base"] is None:
        return "new"
    delta = row["drift"] - row["base"]
    return f"{delta:+d}" if delta else "0"


def render_text(rows: dict) -> str:
    lines = [f"{'category':<14} {'drift':>5} {'change':>6} {'decisions':>9}"]
    for category, row in rows.items():
        if row["drift"] is None:
            lines.append(f"{category:<14} {row['status']} ({row['note']})")
        else:
            line = f"{category:<14} {row['drift']:>5} {change(row):>6} {row['decisions']:>9}"
            if row["status"] == "enforced":
                line += f"  enforced (rule: {PROMOTE[category]})"
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
        if row["drift"] is None:
            lines.append(f"| {category} | {row['status']} ({row['note']}) | | |")
        else:
            name = f"{category} (enforced)" if row["status"] == "enforced" else category
            lines.append(
                f"| {name} | {row['drift']} | {change(row)} | {row['decisions']} |"
            )
    if failures:
        lines += ["", "### Failures", "", *(f"- {f}" for f in failures)]
    return "\n".join(lines) + "\n"


def snapshot(head: Tree) -> str:
    """`drift.json`'s text for a tree: its drift by category, one key a line, and how many
    decisions each category has. `tools/ratchet-history.py` charts it."""
    decisions = Counter(d["category"] for d in head.decisions["decisions"])
    data = {
        "version": 2,
        "drift": {
            category: sorted(k for c, k in head.drift.elements() if c == category)
            for category in COLLECTED
        },
        "decisions": {category: decisions[category] for category in COLLECTED},
    }
    return json.dumps(data, indent=2) + "\n"


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


def crates(scope: Scope, metadata: dict) -> Crates:
    ids = set(metadata["workspace_members"])
    members = {p["name"]: p for p in metadata["packages"] if p["id"] in ids}
    root = scope.root.resolve()
    dirs = {
        name: Path(p["manifest_path"]).resolve().parent.relative_to(root).as_posix()
        for name, p in members.items()
    }
    if stale := sorted(scope.non_shipping - members.keys()):
        raise SystemExit(f"non-shipping crates that aren't workspace members: {stale}")
    shipping = {n: d for n, d in dirs.items() if n not in scope.non_shipping}
    for crate in scope.adapters:
        if crate not in shipping:
            raise SystemExit(f"adapter {crate} isn't a shipping workspace member")

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
        elif match["ruleId"] in ADAPTER_LOGIC and under(match, found.adapters.values()):
            text = re.sub(r"\s+(?=[.?])", "", squash(match["text"]))
            findings[
                ("adapter-logic", f"{match['file']}: {match['ruleId']}: {text}")
            ] += 1
        elif match["ruleId"] == "port-impl" and under(match, found.readers.values()):
            trait = squash(trait_name(mv["TRAIT"]))
            name = re.match(r"[A-Za-z_][A-Za-z0-9_]*", trait)
            if name is not None and name.group() in ports:
                findings[
                    ("port-impl", f"{match['file']}: {trait} for {squash(mv['TYPE'])}")
                ] += 1
    return findings


def parity_gaps(scope: Scope) -> Counter:
    """Each exemption in the scope's capability table that names an issue (#271).

    The table's own gate, `check-parity.py`, holds the table to the four surfaces, so a gap it
    doesn't record fails there. This reads the records its `--exemptions` mode prints, which
    come from the table alone: the job the ratchet runs in has no Node for TypeDoc.

    Like every collector, it returns keys, not issues, so a drift.json entry's `issue` isn't
    compared with its exemption's. Rule 3 lets an unchanged key name a new issue in any
    category, and the table is where a reader looks up the gap: an entry names its exemption's
    issue because `--exemptions` printed it, not because anything holds the two together.
    """
    if scope.parity_table is None:
        return Counter()
    out = subprocess.run(
        [
            sys.executable,
            str(CHECK_PARITY),
            "--exemptions",
            "--table",
            str(scope.parity_table),
        ],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        raise SystemExit(
            f"check-parity.py --exemptions failed on {scope.parity_table}:\n"
            f"{out.stdout}{out.stderr}"
        )
    records = json.loads(out.stdout)["exemptions"]
    # The floor: the real table holds dozens of decisions, so no record at all is a reader that
    # found nothing, and with the category at zero it would pass as every gap closed.
    if not records:
        raise SystemExit(
            f"check-parity.py --exemptions printed no exemption for {scope.parity_table}: a "
            "capability table with none is one the collector didn't read, and every gap in it "
            "would read as closed"
        )
    return Counter(
        ("parity-gap", record["key"])
        for record in records
        if record["issue"] is not None
    )


def parity_drift(scope: Scope) -> Counter:
    """Each `known_drift` path and each `skip` in the scope's case corpus (#320).

    Cases are `<area>/<case>.json` directly under the corpus, the files the runners load. The
    runners hold a marker to the answers and both to their full shape; this reads only what it
    counts, and refuses what it can't read rather than counting it as nothing. A corpus with no
    case is an error, not an empty category: every marker would read as fixed.
    """
    if scope.parity_cases is None:
        return Counter()
    cases = sorted(scope.parity_cases.glob("*/*.json"))
    if not cases:
        raise SystemExit(
            f"{scope.parity_cases} holds no case, so no marker or skip can be counted"
        )
    found: Counter = Counter()
    for path in cases:
        name = f"{path.parent.name}/{path.stem}"
        try:
            case = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise SystemExit(f"{path}: {error}") from None
        if not isinstance(case, dict):
            raise SystemExit(f"{path}: a case is a JSON object")
        markers = case.get("known_drift", {})
        skips = case.get("skip", {})
        if not isinstance(markers, dict) or not isinstance(skips, dict):
            raise SystemExit(
                f"{path}: known_drift and skip are objects keyed by surface"
            )
        for surface, marker in markers.items():
            paths = marker.get("keys") if isinstance(marker, dict) else None
            if (
                not isinstance(paths, list)
                or not paths
                or not all(isinstance(p, str) and p for p in paths)
            ):
                raise SystemExit(
                    f"{path}: known_drift.{surface}.keys must be a non-empty list of dot paths"
                )
            for dotted in paths:
                found[("parity-drift", f"{name}/{surface}: known_drift {dotted}")] += 1
        for surface, reason in skips.items():
            if not isinstance(reason, str) or not reason.strip():
                raise SystemExit(f"{path}: skip.{surface} needs a reason")
            found[("parity-drift", f"{name}/{surface}: skip")] += 1
    return found


def untraced(scope: Scope) -> Counter:
    """Each requirement key in the scope's specs that no traced group file lists (#295).

    The files are read with the `load_traced` of the scope's `trace` script, run the way
    `ratchet-history.py` loads this one (the script imports nothing from outside the standard
    library). It's the loader trace:check reads them with, so the two gates can't disagree about
    what a file lists, and a file it refuses fails here too. A spec or a directory that gives up
    nothing is an error, not an empty category: every key would read as traced.
    """
    if scope.traced is None:
        return Counter()
    if not scope.specs:
        raise SystemExit(
            f"{scope.traced} is read for traced keys, but the scope names no spec"
        )
    keys: set[str] = set()
    for spec in scope.specs:
        # Read, not stat then read: CodeQL flags the gap between the two.
        try:
            text = spec.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise SystemExit(f"{spec} is missing") from None
        document = json.loads(text)
        requirements = (
            document.get("requirements") if isinstance(document, dict) else None
        )
        if not isinstance(requirements, dict):
            raise SystemExit(f"{spec} has no requirements object")
        # check-trace.py's `spec_keys` skips a requirement without a key, so nothing would
        # count it: it would land with no layer checking it and no entry saying so.
        found = set()
        for uuid, entry in requirements.items():
            key = entry.get("key") if isinstance(entry, dict) else None
            if not isinstance(key, str) or not key.strip():
                raise SystemExit(
                    f"{spec}: requirement {uuid} has no key, so no traced file can list it"
                )
            found.add(key)
        if not found:
            raise SystemExit(f"{spec} defines no requirement keys")
        keys |= found
    script = runpy.run_path(str(scope.trace))
    layers = script.get("LAYERS")
    if not isinstance(layers, tuple) or not layers:
        raise SystemExit(f"{scope.trace} has no LAYERS tuple")
    load = script.get("load_traced")
    if not callable(load):
        raise SystemExit(f"{scope.trace} has no load_traced")
    traced = load(scope.traced, {key.rsplit("-", 1)[0] for key in keys}, scope.root)
    if not traced:
        raise SystemExit(f"{scope.traced} lists no traced key")
    # A key that waives every layer passes trace:check with nothing checking it, so it stays
    # an entry.
    checked = {
        key for key, entry in traced.items() if not set(layers) <= set(entry.waive)
    }
    return Counter(("untraced", key) for key in keys - checked)


def collect_each(
    scope: Scope, require_ports: bool = False
) -> tuple[Counter, list[Refusal]]:
    """Every finding in `scope`, and each collector's refusal of its input.

    A collector refuses by raising `SystemExit`. Its refusal leaves its own categories unread
    and the other collectors still run, so one unreadable input doesn't hide what the rest
    find. The Rust collector's three categories come from one ast-grep scan, so they share its
    refusal.
    """
    collectors = (
        (
            ("subprocess", "port-impl", "adapter-logic"),
            lambda: rust(scope, crates(scope, cargo_metadata(scope)), require_ports),
        ),
        (("parity-gap",), lambda: parity_gaps(scope)),
        (("parity-drift",), lambda: parity_drift(scope)),
        (("untraced",), lambda: untraced(scope)),
    )
    findings: Counter = Counter()
    refused: list[Refusal] = []
    for categories, read in collectors:
        try:
            findings += read()
        except SystemExit as error:
            refused.append(Refusal(str(error), frozenset(categories)))
    return findings, refused


def collect(scope: Scope, require_ports: bool = False) -> Counter:
    """Every finding in `scope`, as a count per `(category, key)`. None is in a `HELD` category.
    A collector's refusal is raised, the first one's."""
    findings, refused = collect_each(scope, require_ports)
    if refused:
        raise SystemExit(refused[0].message)
    return findings


def check_sentinel(scope: Scope) -> tuple[list[str], frozenset[str]]:
    """Failures unless each collector reports exactly the scope's `expected.json`, and the
    categories that failed. A collector that refuses the sentinel fails with its refusal, and
    its categories aren't compared, since what it returned is nothing."""
    path = scope.root / "expected.json"
    expected = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    # No `require_ports`: an empty port-impl category is reported below as a failure.
    found, refused = collect_each(scope)
    failures = [refusal.message for refusal in refused]
    failed = set().union(*(refusal.categories for refusal in refused))
    for category in COLLECTED:
        if category in HELD:
            continue
        if category in failed:
            continue
        before = len(failures)
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
        if len(failures) > before:
            failed.add(category)
    return failures, frozenset(failed)


def sentinel(scope: Scope) -> list[str]:
    """Failures unless each collector reports exactly the scope's `expected.json`."""
    return check_sentinel(scope)[0]


# ── entry point ─────────────────────────────────────────────────────────────


def write_snapshot(path: Path, text: str, check: bool) -> int:
    """Write the snapshot to `path`, or with `check`, fail while `path` holds anything else."""
    if not check:
        path.write_text(text, encoding="utf-8")
        print(f"ratchet: wrote {DRIFT}")
        return 0
    try:
        current = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        current = None
    if current != text:
        print(
            f"stale: {DRIFT} isn't the tree's drift. `mise run ratchet:snapshot` rewrites it, "
            "in a change of its own.",
            file=sys.stderr,
        )
        return 1
    print(f"ratchet: {DRIFT} is the tree's drift")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "command", nargs="?", choices=("check", "snapshot"), default="check"
    )
    parser.add_argument(
        "--base",
        help="the commit to compare with (default: the merge base with origin/main)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=f"with snapshot: fail while {DRIFT} is stale, and write nothing",
    )
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = parser.parse_args(argv)
    if args.check and args.command != "snapshot":
        parser.error("--check goes with snapshot")

    # What breaks the reading prints first, as it did when it ended the run: a sentinel failure
    # and a refusal each take out only the categories they're about, and the rest still run.
    broken, untrusted = check_sentinel(SENTINEL)
    if broken:
        print("\n".join(broken), file=sys.stderr)
        if args.command == "snapshot":
            return 1
    head = read_tree(REPO)
    refusals = [refusal.message for refusal in head.refused]
    if refusals:
        print("\n".join(refusals), file=sys.stderr)
    broken += refusals

    if args.command == "snapshot":
        # The snapshot is the tree's drift, so it's written only from a whole measurement.
        if refusals:
            return 1
        return write_snapshot(ROOT / DRIFT, snapshot(head), args.check)

    ref = merge_base_with(ROOT, args.base) if args.base else default_base(ROOT)
    base = read_base(
        ROOT, ref, f"the merge base with {args.base}" if args.base else ref[:12]
    )
    failures = rules(head, base, untrusted)
    rows = summary(head, base, untrusted)
    base_note = (
        base.label
        if base.tree is not None
        else f"{base.label}, which has no {DECISIONS}: it predates rule 1, so rule 1 is "
        "skipped"
    )
    if args.json:
        print(
            json.dumps(
                {
                    "base": base.label,
                    "bootstrap": base.tree is None,
                    "categories": rows,
                    "refused": broken,
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
            out.write(render_markdown(rows, broken + failures, base_note))
    if failures:
        sys.stdout.flush()
        print("\n".join(failures), file=sys.stderr)
    return 1 if broken or failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
