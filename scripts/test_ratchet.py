# SPDX-License-Identifier: Apache-2.0
"""Tests for `scripts/ratchet.py`: the four failure rules, the collectors, and the seeded faults.

The rule tests drive `compare` with small in-memory files. The collector and seeded-fault tests
build throwaway cargo workspaces under a temporary directory and run the real tools over them
(`cargo metadata` and `ast-grep`), because a collector tested against a mocked tool proves the
mock. Both tools come from `mise.toml`, so run this through `mise run ratchet:check`.

The adapter lint tests are here too: each driving adapter's `clippy.toml` is the enforcing half
of the subprocess rule the ratchet counts (#285), and they run real clippy the same way.
"""

import json
import os
import re
import runpy
import shutil
import subprocess
import tempfile
import textwrap
import tomllib
import unittest
from collections import Counter
from datetime import datetime
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
RATCHET = runpy.run_path(str(HERE / "ratchet.py"))
HISTORY = runpy.run_path(str(HERE / "ratchet-history.py"))

Scope = RATCHET["Scope"]
compare = RATCHET["compare"]
collect = RATCHET["collect"]
parse = RATCHET["parse"]
updated = RATCHET["updated"]
summary = RATCHET["summary"]
render_text = RATCHET["render_text"]
sentinel = RATCHET["sentinel"]
read_base = RATCHET["read_base"]
read_base_sets = RATCHET["read_base_sets"]
read_base_collected = RATCHET["read_base_collected"]
dump = RATCHET["dump"]
grown_sets = RATCHET["grown_sets"]
sets_from = RATCHET["sets_from"]


def ratchet(entries=(), decisions=(), enforced=()):
    """A parsed drift file from `(category, key, issue)` and `(category, key, reason)` tuples."""
    return parse(
        {
            "version": 1,
            "enforced": list(enforced),
            "entries": [{"category": c, "key": k, "issue": i} for c, k, i in entries],
            "decisions": [
                {"category": c, "key": k, "reason": r} for c, k, r in decisions
            ],
        },
        "test",
    )


def found(*pairs):
    return Counter(pairs)


# One entry in each collected category, so rule 4 is quiet unless a test wants it.
BASELINE = [
    ("placement", "microvms-cli -> tar", 260),
    ("subprocess", 'microvms-cli/src/seam.rs: Command::new("aws")', 258),
    ("port-impl", "microvms-cli/src/seam.rs: TokenMinter for PlaneMinter", 270),
    (
        "adapter-logic",
        "microvms-js/src/control.rs: literal-default: options.timeout.unwrap_or(300.0)",
        273,
    ),
    ("parity-gap", "wait-until-running/cli", 269),
    ("untraced", "TRAP-1", 301),
]
BASELINE_FOUND = found(*((c, k) for c, k, _ in BASELINE))


class Workspace:
    """A throwaway cargo workspace: one directory per crate, named after the crate."""

    def __init__(self, test: unittest.TestCase):
        self._dir = tempfile.TemporaryDirectory()
        test.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        self.crates: list[str] = []

    def crate(self, name, deps="", build="", dev="", files=None):
        self.crates.append(name)
        manifest = [
            "[package]",
            f'name = "{name}"',
            'version = "0.0.0"',
            'edition = "2024"',
            "publish = false",
            "",
            "[dependencies]",
            deps,
            "",
            "[build-dependencies]",
            build,
            "",
            "[dev-dependencies]",
            dev,
        ]
        self.write(f"{name}/Cargo.toml", "\n".join(manifest) + "\n")
        # cargo refuses a package with no target, so every crate gets a `lib.rs`.
        for path, text in {"src/lib.rs": "", **(files or {})}.items():
            self.write(f"{name}/{path}", textwrap.dedent(text))
        return self

    def placement(self, text):
        self.write("placement.toml", textwrap.dedent(text))
        return self

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def scope(self, adapters=None, non_shipping=(), composed=(), composition_root=None):
        """The workspace as a scope. By default every crate with a set is an adapter."""
        members = ", ".join(f'"{name}"' for name in self.crates)
        self.write(
            "Cargo.toml", f'[workspace]\nmembers = [{members}]\nresolver = "3"\n'
        )
        placement = self.root / "placement.toml"
        if adapters is None:
            adapters = tuple(sets_from(placement.read_text(), "test"))
        return Scope(
            root=self.root,
            placement=placement,
            adapters=tuple(adapters),
            non_shipping=frozenset(non_shipping),
            composed=tuple(composed),
            composition_root=composition_root,
        )


def keys(counter, category):
    return sorted(
        key for (cat, key), n in counter.items() for _ in range(n) if cat == category
    )


class RuleTests(unittest.TestCase):
    """Each of the four failure rules, and the cases that must pass."""

    def test_a_file_that_matches_the_tree_passes(self):
        self.assertEqual(
            compare(BASELINE_FOUND, ratchet(BASELINE), ratchet(BASELINE), "main"), []
        )

    def test_rule_1_new_drift_fails(self):
        now = BASELINE_FOUND + found(("placement", "microvms-cli -> globset"))
        self.assertEqual(
            compare(now, ratchet(BASELINE), ratchet(BASELINE), "main"),
            [
                "new drift: [placement] microvms-cli -> globset. Move the work to the layer "
                "whose job it is (I/O belongs in microvms-edges, behind a port in "
                "microvms-app), or add a decision with its reason."
            ],
        )

    def test_rule_1_a_new_parity_gap_points_at_the_table(self):
        # A gap isn't work in the wrong layer, so the layering advice would send the reader to
        # the wrong file.
        now = BASELINE_FOUND + found(("parity-gap", "adopt/cli"))
        self.assertEqual(
            compare(now, ratchet(BASELINE), ratchet(BASELINE), "main"),
            [
                "new drift: [parity-gap] adopt/cli. Give that surface the capability, or, if "
                "the gap is permanent, drop the exemption's issue in "
                "parity/capabilities.toml so it reads as a decision."
            ],
        )

    def test_rule_1_an_untraced_requirement_points_at_traced(self):
        # A requirement with no trace isn't work in the wrong layer either: the fix is a TRACED
        # entry and the layers it names.
        now = BASELINE_FOUND + found(("untraced", "TRAP-14"))
        self.assertEqual(
            compare(now, ratchet(BASELINE), ratchet(BASELINE), "main"),
            [
                "new drift: [untraced] TRAP-14. Trace the requirement: list its key in TRACED "
                "in scripts/check-trace.py, give it each layer or a waiver with its reason (a key "
                "that waives every layer stays untraced), and run ./scripts/check-trace.py --write."
            ],
        )

    def test_rule_1_a_decision_covers_its_key(self):
        now = BASELINE_FOUND + found(
            ("subprocess", "agentd/src/exec.rs: Command::new(shell)")
        )
        file = ratchet(
            BASELINE,
            [("subprocess", "agentd/src/exec.rs: Command::new(shell)", "agentd's job")],
        )
        self.assertEqual(compare(now, file, ratchet(BASELINE), "main"), [])

    def test_rule_2_an_unrecorded_fix_fails(self):
        now = BASELINE_FOUND - found(("placement", "microvms-cli -> tar"))
        self.assertEqual(
            compare(now, ratchet(BASELINE), ratchet(BASELINE), "main"),
            [
                "fixed: [placement] microvms-cli -> tar. Run `mise run ratchet:update` "
                "and commit the file."
            ],
        )

    def test_rule_2_a_stale_decision_fails_too(self):
        file = ratchet(
            BASELINE, [("subprocess", "gone.rs: Command::new(x)", "was needed")]
        )
        failures = compare(BASELINE_FOUND, file, ratchet(BASELINE), "main")
        self.assertEqual(len(failures), 1)
        self.assertTrue(
            failures[0].startswith("fixed: [subprocess] gone.rs: Command::new(x).")
        )

    def test_rule_3_an_entry_absent_from_the_base_fails(self):
        added = ("placement", "microvms-cli -> sha2", 260)
        now = BASELINE_FOUND + found(("placement", "microvms-cli -> sha2"))
        failures = compare(
            now, ratchet([*BASELINE, added]), ratchet(BASELINE), "origin/main"
        )
        self.assertEqual(len(failures), 1)
        self.assertTrue(
            failures[0].startswith(
                "not in the base: [placement] microvms-cli -> sha2 is an entry here but not "
                "in origin/main's ratchet/drift.json,"
            ),
            failures[0],
        )

    def test_rule_3_a_decision_absent_from_the_base_passes(self):
        now = BASELINE_FOUND + found(
            ("subprocess", 'doctor.rs: Command::new("terraform")')
        )
        file = ratchet(
            BASELINE,
            [("subprocess", 'doctor.rs: Command::new("terraform")', "reads tf state")],
        )
        self.assertEqual(compare(now, file, ratchet(BASELINE), "main"), [])

    def test_rule_3_points_an_untraced_entry_at_traced(self):
        # An untraced requirement can't take a decision, so rule 3's layering advice ("or add
        # a decision") would send the reader to a file that refuses it.
        added = ("untraced", "TRAP-14", 301)
        now = BASELINE_FOUND + found(added[:2])
        failures = compare(now, ratchet([*BASELINE, added]), ratchet(BASELINE), "main")
        self.assertEqual(len(failures), 1, failures)
        self.assertTrue(failures[0].startswith("not in the base: [untraced] TRAP-14"))
        self.assertTrue(
            failures[0].endswith(
                "Entries can only be removed: trace the requirement in TRACED in "
                "scripts/check-trace.py instead."
            ),
            failures[0],
        )

    def test_rule_3_is_skipped_when_the_base_has_no_file(self):
        # The bootstrap: the PR that creates ratchet/drift.json has no base copy to compare with.
        self.assertEqual(compare(BASELINE_FOUND, ratchet(BASELINE), None, "main"), [])

    #: What a base whose ratchet.py predates the adapter-logic collector collects.
    BEFORE_ADAPTER_LOGIC = ("placement", "subprocess", "port-impl")

    def test_rule_3_is_skipped_for_a_category_the_base_does_not_collect(self):
        # The bootstrap for a newly collected category: the base's file couldn't record its
        # findings, so the PR that starts collecting it has to be able to list them.
        first = (
            "adapter-logic",
            "microvms-js/src/exec.rs: literal-default: const DEFAULT_WAIT: f64 = 300.0;",
            273,
        )
        base = ratchet(BASELINE[:3])
        now = BASELINE_FOUND + found(first[:2])
        failures = compare(
            now,
            ratchet([*BASELINE, first]),
            base,
            "main",
            base_collected=self.BEFORE_ADAPTER_LOGIC,
        )
        self.assertEqual(failures, [])

    def test_rule_3_still_applies_to_a_category_the_base_collects(self):
        # The bootstrap is per category: a category the base collected can't use it, even in
        # the PR that bootstraps another one.
        added = ("subprocess", 'microvms-js/src/session.rs: Command::new("gh")', 258)
        now = BASELINE_FOUND + found(added[:2])
        failures = compare(
            now,
            ratchet([*BASELINE, added]),
            ratchet(BASELINE[:3]),
            "main",
            base_collected=self.BEFORE_ADAPTER_LOGIC,
        )
        self.assertEqual(len(failures), 1, failures)
        self.assertTrue(
            failures[0].startswith(
                'not in the base: [subprocess] microvms-js/src/session.rs: Command::new("gh")'
            ),
            failures[0],
        )

    def moved(self, old, new, issue=284):
        """compare() after the file re-keys `old` (in the base) as `new` (found now)."""
        base = ratchet([*BASELINE, ("subprocess", old, 284)])
        file = ratchet([*BASELINE, ("subprocess", new, issue)])
        now = BASELINE_FOUND + found(("subprocess", new))
        return compare(now, file, base, "main")

    def test_rule_3_a_move_to_another_file_or_crate_passes(self):
        old = "microvms-core/src/provision.rs: std::process::Command::new(&argv[0])"
        new = "microvms-edges/src/subprocess.rs: std::process::Command::new(&argv[0])"
        self.assertEqual(self.moved(old, new), [])

    def test_rule_3_a_rename_in_place_passes(self):
        old = "microvms-core/src/provision.rs: std::process::Command::new(&argv[0])"
        new = "microvms-core/src/provision.rs: std::process::Command::new(&command[0])"
        self.assertEqual(self.moved(old, new), [])

    def test_rule_3_an_unchanged_key_may_name_another_issue(self):
        key = "microvms-core/src/provision.rs: std::process::Command::new(&argv[0])"
        self.assertEqual(self.moved(key, key, issue=290), [])

    def test_rule_3_a_move_and_a_rename_at_once_fails(self):
        old = "microvms-core/src/provision.rs: std::process::Command::new(&argv[0])"
        new = (
            "microvms-edges/src/subprocess.rs: std::process::Command::new(&command[0])"
        )
        self.assertEqual(
            [f.split(":")[0] for f in self.moved(old, new)], ["not in the base"]
        )

    def test_rule_3_a_move_under_another_issue_fails(self):
        old = "microvms-core/src/provision.rs: std::process::Command::new(&argv[0])"
        new = "microvms-edges/src/subprocess.rs: std::process::Command::new(&argv[0])"
        self.assertEqual(
            [f.split(":")[0] for f in self.moved(old, new, issue=999)],
            ["not in the base"],
        )

    def test_rule_3_a_new_key_beside_its_original_fails(self):
        # The original is still listed, so there's nothing for the new key to replace.
        key = "microvms-core/src/provision.rs: std::process::Command::new(&argv[0])"
        copy = "microvms-edges/src/subprocess.rs: std::process::Command::new(&argv[0])"
        base = ratchet([*BASELINE, ("subprocess", key, 284)])
        file = ratchet([*BASELINE, ("subprocess", key, 284), ("subprocess", copy, 284)])
        now = BASELINE_FOUND + found(("subprocess", key), ("subprocess", copy))
        self.assertEqual(
            [f.split(":")[0] for f in compare(now, file, base, "main")],
            ["not in the base"],
        )

    def test_rule_3_a_placement_entry_cannot_be_swapped_for_another(self):
        # Placement keys name crates, not places, so they never re-key on a move.
        base = ratchet(BASELINE)
        entries = [("placement", "microvms-cli -> reqwest", 260), *BASELINE[1:]]
        now = found(*((c, k) for c, k, _ in entries))
        self.assertEqual(
            [f.split(":")[0] for f in compare(now, ratchet(entries), base, "main")],
            ["not in the base"],
        )

    def test_rule_4_an_empty_category_must_be_promoted(self):
        entries = [e for e in BASELINE if e[0] != "subprocess"]
        now = found(*((c, k) for c, k, _ in entries))
        failures = compare(now, ratchet(entries), ratchet(BASELINE), "main")
        self.assertEqual(len(failures), 1)
        self.assertTrue(
            failures[0].startswith("promote subprocess: move its rule into "),
            failures[0],
        )

    def test_rule_4_decisions_do_not_keep_a_category_open(self):
        entries = [e for e in BASELINE if e[0] != "subprocess"]
        decision = (
            "subprocess",
            "agentd/src/exec.rs: Command::new(shell)",
            "agentd's job",
        )
        now = found(*((c, k) for c, k, _ in entries), decision[:2])
        failures = compare(now, ratchet(entries, [decision]), ratchet(BASELINE), "main")
        self.assertEqual([f.split(":")[0] for f in failures], ["promote subprocess"])

    def test_an_enforced_category_is_still_collected(self):
        # `enforced` quiets rule 4 and nothing else, so listing a category can't waive drift.
        entries = [e for e in BASELINE if e[0] != "subprocess"]
        now = found(
            *((c, k) for c, k, _ in entries), ("subprocess", "x.rs: Command::new(y)")
        )
        file = ratchet(entries, enforced=["subprocess"])
        self.assertEqual(
            [f.split(":")[0] for f in compare(now, file, ratchet(BASELINE), "main")],
            ["new drift"],
        )

    def test_an_enforced_category_keeps_its_decisions(self):
        entries = [e for e in BASELINE if e[0] != "subprocess"]
        decision = (
            "subprocess",
            "agentd/src/exec.rs: Command::new(shell)",
            "agentd's job",
        )
        now = found(*((c, k) for c, k, _ in entries), decision[:2])
        file = ratchet(entries, [decision], enforced=["subprocess"])
        self.assertEqual(compare(now, file, ratchet(BASELINE), "main"), [])

    def test_an_enforced_category_cannot_carry_entries(self):
        with self.assertRaisesRegex(SystemExit, "subprocess is enforced"):
            ratchet(BASELINE, enforced=["subprocess"])

    def test_duplicate_keys_are_counted(self):
        # Two identical calls in one file share a key, so the file lists the entry twice.
        twice = BASELINE_FOUND + found(BASELINE[1][:2])
        self.assertEqual(
            [f.split(":")[0] for f in compare(twice, ratchet(BASELINE), None, "main")],
            ["new drift"],
        )
        self.assertEqual(
            compare(twice, ratchet([*BASELINE, BASELINE[1]]), None, "main"), []
        )

    def test_a_category_that_is_not_collected_cannot_carry_entries(self):
        with not_collected_yet():
            with self.assertRaisesRegex(SystemExit, "later-gap is not collected yet"):
                ratchet([*BASELINE, ("later-gap", "x", 271)])


def not_collected_yet():
    """The ratchet's globals with one category defined but not collected, as parity-gap was
    until #271. Nothing is in `NOT_COLLECTED` today, so the tests of that path plant one."""
    return mock.patch.dict(
        parse.__globals__,
        {
            "NOT_COLLECTED": {"later-gap": "until its collector lands"},
            "CATEGORIES": (*RATCHET["COLLECTED"], "later-gap"),
        },
    )


class FileTests(unittest.TestCase):
    """The file's schema, `update`, and the summary."""

    def test_an_entry_names_its_issue(self):
        with self.assertRaisesRegex(SystemExit, "issue"):
            parse(
                {
                    "version": 1,
                    "enforced": [],
                    "entries": [{"category": "placement", "key": "a -> b"}],
                    "decisions": [],
                },
                "test",
            )

    def test_a_decision_names_its_reason(self):
        with self.assertRaisesRegex(SystemExit, "reason"):
            ratchet(BASELINE, [("subprocess", "x.rs: Command::new(y)", "  ")])

    def test_an_unknown_category_is_refused(self):
        with self.assertRaisesRegex(SystemExit, "unknown category"):
            ratchet([("layering", "x", 1)])

    def test_an_untraced_decision_is_refused(self):
        # A decision would take a requirement out of the count with no layer checking it: a
        # new requirement could land that way, or the backlog could shrink with nothing traced.
        with self.assertRaisesRegex(
            SystemExit,
            r"\[untraced\] TRAP-14 can't be a decision: a requirement that can't carry a "
            "layer waives that layer in TRACED",
        ):
            ratchet(BASELINE, [("untraced", "TRAP-14", "not worth a test")])

    def test_a_key_is_either_an_entry_or_a_decision(self):
        with self.assertRaisesRegex(SystemExit, "both an entry and a decision"):
            ratchet(BASELINE, [(*BASELINE[0][:2], "why not")])

    def test_update_deletes_fixed_entries_and_never_adds_one(self):
        fixed = ("placement", "microvms-cli -> tar")
        new = ("placement", "microvms-cli -> globset")
        now = BASELINE_FOUND - found(fixed) + found(new)
        data, removed = updated(ratchet(BASELINE), now)
        self.assertEqual(removed, ["[placement] microvms-cli -> tar"])
        after = {(e["category"], e["key"]) for e in data["entries"]}
        self.assertNotIn(fixed, after)
        self.assertNotIn(new, after)
        self.assertEqual(len(data["entries"]), len(BASELINE) - 1)

    def test_update_removes_only_the_surplus_copy_of_a_duplicate(self):
        data, removed = updated(ratchet([*BASELINE, BASELINE[1]]), BASELINE_FOUND)
        self.assertEqual(len(removed), 1)
        self.assertEqual(len(data["entries"]), len(BASELINE))

    def test_the_summary_says_not_collected_rather_than_zero(self):
        with not_collected_yet():
            rows = summary(ratchet(BASELINE), ratchet(BASELINE))
            text = render_text(rows)
        self.assertEqual(rows["later-gap"]["status"], "not collected")
        self.assertIsNone(rows["later-gap"]["entries"])
        self.assertEqual(rows["placement"]["entries"], 1)
        line = next(line for line in text.splitlines() if line.startswith("later-gap"))
        self.assertIn("not collected", line)
        self.assertNotIn("0", line)

    def test_parity_gap_is_collected(self):
        rows = summary(ratchet(BASELINE), ratchet(BASELINE))
        self.assertEqual(rows["parity-gap"]["status"], "collected")
        self.assertEqual(rows["parity-gap"]["entries"], 1)

    def test_the_summary_says_new_for_a_category_the_base_does_not_collect(self):
        rows = summary(
            ratchet(BASELINE),
            ratchet(BASELINE[:3]),
            base_collected=("placement", "subprocess", "port-impl"),
        )
        self.assertIsNone(rows["adapter-logic"]["base"])
        line = next(
            line
            for line in render_text(rows).splitlines()
            if line.startswith("adapter-logic")
        )
        self.assertIn("new", line)

    def test_the_summary_reports_the_change_from_the_base(self):
        base = ratchet([*BASELINE, ("placement", "microvms-cli -> sha2", 260)])
        rows = summary(ratchet(BASELINE), base)
        self.assertEqual(
            (rows["placement"]["entries"], rows["placement"]["base"]), (1, 2)
        )
        self.assertIn("-1", render_text(rows))


class SetTests(unittest.TestCase):
    """Rule 3's other half: an allowed set can shrink but not grow."""

    BASE = sets_from('[microvms-py]\nnormal = ["microvms-core", "tokio"]\n', "base")

    def test_a_crate_added_to_a_set_fails(self):
        now = sets_from(
            '[microvms-py]\nnormal = ["microvms-core", "tokio", "reqwest"]\n', "now"
        )
        failures = grown_sets(now, self.BASE, "origin/main")
        self.assertEqual(len(failures), 1)
        self.assertTrue(
            failures[0].startswith(
                "set grew: arch/placement.toml adds reqwest to [microvms-py] normal"
            ),
            failures[0],
        )

    def test_a_set_that_shrinks_passes(self):
        now = sets_from('[microvms-py]\nnormal = ["microvms-core"]\n', "now")
        self.assertEqual(grown_sets(now, self.BASE, "origin/main"), [])

    def test_a_set_for_a_new_crate_passes(self):
        now = sets_from(
            '[microvms-py]\nnormal = ["microvms-core"]\n'
            '[microvms-domain]\nnormal = ["serde"]\n',
            "now",
        )
        self.assertEqual(grown_sets(now, self.BASE, "origin/main"), [])

    def test_no_base_sets_is_the_bootstrap(self):
        self.assertEqual(grown_sets(self.BASE, None, "origin/main"), [])


class LayoutTests(unittest.TestCase):
    """`update` writes the checked-in layout, so its diff is the entries it removed."""

    def test_the_checked_in_file_is_in_the_written_layout(self):
        text = (ROOT / "ratchet" / "drift.json").read_text(encoding="utf-8")
        self.assertEqual(dump(json.loads(text)), text)

    def test_removing_an_entry_changes_one_line(self):
        text = (ROOT / "ratchet" / "drift.json").read_text(encoding="utf-8")
        file = parse(json.loads(text), "drift.json")
        now = Counter(
            (e["category"], e["key"]) for e in file["entries"] + file["decisions"]
        )
        gone = (file["entries"][0]["category"], file["entries"][0]["key"])
        data, _ = updated(file, now - Counter([gone]))
        before, after = text.splitlines(), dump(data).splitlines()
        self.assertEqual(len(before) - len(after), 1)
        self.assertEqual([line for line in before if line not in after], [before[4]])

    def test_empty_lists_round_trip(self):
        data = ratchet()
        self.assertEqual(parse(json.loads(dump(data)), "test"), data)


class PlacementTests(unittest.TestCase):
    def test_direct_normal_and_build_dependencies_outside_the_set_are_reported(self):
        ws = (
            Workspace(self)
            .crate(
                "adapter",
                deps='serde = "1"\ntar = "0.4"\n\n[target.\'cfg(unix)\'.dependencies]\ntar = "0.4"',
                build='cc = "1"',
                dev='tempfile = "3"',
            )
            .placement('[adapter]\nnormal = ["serde"]\n')
        )
        now = collect(ws.scope())
        # Dev dependencies are out of scope, a target-specific repeat is one key, and a build
        # dependency is keyed apart from a normal one of the same name.
        self.assertEqual(
            keys(now, "placement"), ["adapter -> cc (build)", "adapter -> tar"]
        )

    def test_a_renamed_dependency_is_keyed_by_its_package_name(self):
        ws = (
            Workspace(self)
            .crate("adapter", deps='wire = { package = "serde_json", version = "1" }')
            .placement('[adapter]\nnormal = ["serde_json"]\n')
        )
        self.assertEqual(keys(collect(ws.scope()), "placement"), [])

    def test_a_set_for_a_crate_that_does_not_exist_is_refused(self):
        ws = (
            Workspace(self)
            .crate("adapter")
            .placement('[adaptor]\nnormal = ["serde"]\n')
        )
        with self.assertRaisesRegex(SystemExit, "adaptor"):
            collect(ws.scope())


RUST_TEST_FORMS = """\
    fn shipped() {
        std::process::Command::new("aws");
    }

    #[cfg(test)]
    fn item_level() {
        std::process::Command::new("item");
    }

    /// A doc comment between the attribute and the item.
    #[cfg(test)]
    /// And one after it.
    #[allow(dead_code)]
    fn documented() {
        std::process::Command::new("documented");
    }

    #[cfg(test)]
    mod tests {
        fn inner() {
            std::process::Command::new("tests");
        }
    }

    #[cfg(test)]
    mod fake {
        impl microvms_core::Fetch for Fake {}
        fn inner() {
            tokio::process::Command::new("fake");
        }
    }

    #[test]
    fn a_test() {
        std::process::Command::new("test-fn");
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn an_async_test() {
        std::process::Command::new("tokio-test");
    }

    #[cfg(test)]
    impl Fetch for OnlyInTests {}

    #[cfg(test)]
    fn before() {}

    fn after_a_test_item() {
        Command::new(
            program,
        );
    }

    #[cfg(test)]
    mod helpers;
    mod testing;
    mod shipped_module;
"""


class RustCollectorTests(unittest.TestCase):
    def workspace(self):
        return (
            Workspace(self)
            .crate(
                "microvms-core",
                files={
                    "src/lib.rs": """\
                        pub trait Fetch {}
                        pub trait TokenMinter {}
                        trait Private {}
                        #[cfg(test)]
                        pub trait OnlyInTests {}
                    """
                },
            )
            .crate(
                "adapter",
                deps='microvms-core = { path = "../microvms-core" }',
                files={
                    "src/lib.rs": RUST_TEST_FORMS,
                    "src/helpers.rs": """\
                        mod deeper;
                        fn declared_test_only() { std::process::Command::new("helpers"); }
                    """,
                    "src/helpers/deeper.rs": """\
                        fn under_a_test_module() { std::process::Command::new("deeper"); }
                    """,
                    "src/testing.rs": """\
                        //! A module that marks itself.
                        #![cfg(test)]
                        fn inner_attribute() { std::process::Command::new("testing"); }
                        impl Fetch for Scripted {}
                    """,
                    "src/shipped_module.rs": """\
                        impl microvms_core::session::TokenMinter for PlaneMinter {}
                        impl crate::provision::Fetch for Reexported {}
                        impl<T: Send> Fetch for Wrapper<T> where T: Sync {}
                        impl OnlyInTests for NotAPort {}
                        impl Private for NotPublic {}
                        impl std::fmt::Display for NotAPortEither {}
                        impl Plain {}
                    """,
                },
            )
            .placement('[adapter]\nnormal = ["microvms-core"]\n')
        )

    def test_test_code_is_not_reported_as_a_subprocess(self):
        now = collect(self.workspace().scope())
        self.assertEqual(
            keys(now, "subprocess"),
            [
                "adapter/src/lib.rs: Command::new(program)",
                'adapter/src/lib.rs: std::process::Command::new("aws")',
            ],
        )

    def test_port_impls_use_the_short_trait_name_and_skip_test_code(self):
        now = collect(self.workspace().scope())
        self.assertEqual(
            keys(now, "port-impl"),
            [
                "adapter/src/shipped_module.rs: Fetch for Reexported",
                "adapter/src/shipped_module.rs: Fetch for Wrapper<T>",
                "adapter/src/shipped_module.rs: TokenMinter for PlaneMinter",
            ],
        )

    def test_port_impls_are_collected_only_from_the_adapters(self):
        ws = self.workspace()
        ws.write("microvms-core/src/impls.rs", "impl Fetch for InCore {}\n")
        self.assertNotIn(
            "microvms-core/src/impls.rs: Fetch for InCore",
            keys(collect(ws.scope()), "port-impl"),
        )

    def test_test_support_items_are_test_code(self):
        # The shared doubles' feature (#283) is dev-only, so an item behind it never ships. A
        # feature with another name does ship.
        ws = self.workspace()
        ws.write(
            "adapter/src/doubles.rs",
            textwrap.dedent("""\
                #[cfg(feature = "test-support")]
                impl Fetch for FeatureOnly {}
                #[cfg(any(test, feature = "test-support"))]
                pub mod testing {
                    impl super::Fetch for Fake {}
                    fn f() { std::process::Command::new("fake"); }
                }
                #[cfg(any(test, feature = "test-support"))]
                pub mod scripted;
                #[cfg(feature = "tls")]
                impl Fetch for Tls {}
            """),
        )
        ws.write(
            "adapter/src/doubles/scripted.rs", "impl crate::Fetch for Scripted {}\n"
        )
        ws.write(
            "adapter/src/marked.rs",
            '#![cfg(feature = "test-support")]\nimpl Fetch for Marked {}\n',
        )
        now = collect(ws.scope())
        self.assertEqual(
            [k for k in keys(now, "port-impl") if "doubles" in k or "marked" in k],
            ["adapter/src/doubles.rs: Fetch for Tls"],
        )
        self.assertNotIn(
            'adapter/src/doubles.rs: std::process::Command::new("fake")',
            keys(now, "subprocess"),
        )

    def test_two_identical_calls_in_one_file_count_twice(self):
        ws = (
            Workspace(self)
            .crate(
                "adapter",
                files={
                    "src/lib.rs": """\
                        fn a() { Command::new("aws"); }
                        fn b() { Command::new("aws"); }
                    """
                },
            )
            .placement("[adapter]\nnormal = []\n")
        )
        self.assertEqual(
            collect(ws.scope())[
                ("subprocess", 'adapter/src/lib.rs: Command::new("aws")')
            ],
            2,
        )


class ScopeTests(unittest.TestCase):
    """Which crates the collectors read comes from `cargo metadata`, not from a list."""

    def workspace(self):
        return (
            Workspace(self)
            .crate(
                "microvms-core",
                deps='microvms-app = { path = "../microvms-app" }',
                files={"src/lib.rs": "pub trait Fetch {}\n"},
            )
            .crate("microvms-app", files={"src/lib.rs": "pub trait TokenMinter {}\n"})
            .crate("guest", files={"src/lib.rs": "pub trait Platform {}\n"})
            .crate(
                "microvms-cli",
                deps='microvms-core = { path = "../microvms-core" }',
                files={
                    "src/lib.rs": """\
                        impl microvms_app::TokenMinter for PlaneMinter {}
                        impl Fetch for Session {}
                        impl Platform for NotBelow {}
                    """
                },
            )
            .placement('[microvms-cli]\nnormal = ["microvms-core"]\n')
        )

    def test_a_crate_nobody_listed_is_scanned(self):
        # The move #283 plans: core's spawn goes to a new crate. It's still found there.
        ws = self.workspace().crate(
            "microvms-edges",
            files={
                "src/lib.rs": "pub fn run(argv: &[String]) { std::process::Command::new(&argv[0]); }\n"
            },
        )
        self.assertEqual(
            keys(collect(ws.scope()), "subprocess"),
            ["microvms-edges/src/lib.rs: std::process::Command::new(&argv[0])"],
        )

    def test_a_non_shipping_crate_is_not_scanned(self):
        ws = self.workspace().crate(
            "harness", files={"src/lib.rs": 'fn f() { Command::new("x"); }\n'}
        )
        self.assertEqual(
            keys(collect(ws.scope(non_shipping=["harness"])), "subprocess"), []
        )

    def test_a_non_shipping_name_that_is_not_a_member_is_refused(self):
        with self.assertRaisesRegex(SystemExit, "harnes"):
            collect(self.workspace().scope(non_shipping=["harnes"]))

    def test_an_adapter_without_a_set_is_refused(self):
        ws = self.workspace()
        with self.assertRaisesRegex(SystemExit, "adapter guest has no set"):
            collect(ws.scope(adapters=["microvms-cli", "guest"]))

    def test_ports_are_the_public_traits_of_every_crate_below_an_adapter(self):
        # TokenMinter lives two hops down, in a crate core depends on; `guest` isn't below the
        # adapter at all, so its trait isn't a port.
        self.assertEqual(
            keys(collect(self.workspace().scope()), "port-impl"),
            [
                "microvms-cli/src/lib.rs: Fetch for Session",
                "microvms-cli/src/lib.rs: TokenMinter for PlaneMinter",
            ],
        )

    def test_the_composed_crates_and_the_root_are_read_for_port_impls(self):
        # #283: a use case implements a port only where a decision says why, and the composition
        # root implements none (ARCH-8). The root's own public traits are the prelude's
        # extension traits, so they aren't ports: the CLI's `Fetch for Session` drops out, and so
        # does the root's `PlaneExt` impl.
        ws = self.workspace()
        ws.write(
            "microvms-app/src/lib.rs",
            "pub trait TokenMinter {}\npub struct Minter;\nimpl TokenMinter for Minter {}\n",
        )
        ws.write(
            "microvms-core/src/lib.rs",
            textwrap.dedent("""\
                pub trait Fetch {}
                pub trait PlaneExt {}
                impl PlaneExt for microvms_app::Minter {}
                impl microvms_app::TokenMinter for Wired {}
            """),
        )
        scope = ws.scope(composed=["microvms-app"], composition_root="microvms-core")
        self.assertEqual(
            keys(collect(scope), "port-impl"),
            [
                "microvms-app/src/lib.rs: TokenMinter for Minter",
                "microvms-cli/src/lib.rs: TokenMinter for PlaneMinter",
                "microvms-core/src/lib.rs: TokenMinter for Wired",
            ],
        )

    def test_a_composed_crate_that_is_not_below_the_adapters_is_refused(self):
        with self.assertRaisesRegex(SystemExit, "guest is read for port impls"):
            collect(self.workspace().scope(composed=["guest"]))

    def test_a_tree_with_no_ports_below_the_adapters_is_refused(self):
        ws = (
            Workspace(self)
            .crate("microvms-core")
            .crate("microvms-cli", deps='microvms-core = { path = "../microvms-core" }')
            .placement('[microvms-cli]\nnormal = ["microvms-core"]\n')
        )
        with self.assertRaisesRegex(SystemExit, "no public trait"):
            collect(ws.scope(), require_ports=True)


class MacroTests(unittest.TestCase):
    """A `Command::new` in a macro's arguments, which tree-sitter leaves unparsed."""

    def collect_lib(self, text):
        ws = (
            Workspace(self)
            .crate("adapter", files={"src/lib.rs": text})
            .placement("[adapter]\nnormal = []\n")
        )
        return keys(collect(ws.scope()), "subprocess")

    def test_a_call_inside_a_macro_is_keyed_like_a_plain_one(self):
        self.assertEqual(
            self.collect_lib(
                """\
                async fn raced() {
                    tokio::select! {
                        _ = tokio::process::Command::new("aws").output() => {}
                    }
                }
                fn listed() {
                    let _ = vec![Command::new(
                        program,
                    ), std::process::Command::new(")")];
                }
                """
            ),
            [
                "adapter/src/lib.rs: Command::new(program)",
                'adapter/src/lib.rs: std::process::Command::new(")")',
                'adapter/src/lib.rs: tokio::process::Command::new("aws")',
            ],
        )

    def test_a_macro_in_test_code_is_skipped(self):
        self.assertEqual(
            self.collect_lib(
                """\
                #[cfg(test)]
                mod tests {
                    fn f() { let _ = vec![Command::new("aws")]; }
                }
                """
            ),
            [],
        )

    def test_a_name_that_only_ends_in_command_is_not_a_subprocess(self):
        self.assertEqual(
            self.collect_lib('fn f() { let _ = vec![MyCommand::new("x")]; }\n'), []
        )


#: The adapter-logic fixture, in semgrep's rule-test style: each `ruleid:` comment sits above a
#: line one of the two rules must report, and each `ok:` above one it must not. The tests below
#: list the keys by hand, one test per pattern alternative, so deleting an alternative from a
#: rule fails the test named after it.
ADAPTER_LOGIC_FORMS = """\
    // ruleid: literal-default
    const DEFAULT_WAIT: f64 = 300.0;
    // ruleid: literal-default
    pub(crate) const DEFAULT_PORT: u16 = 9000;
    // ok: literal-default (a zero default)
    const DEFAULT_OFFSET: f64 = 0.0;
    // ok: literal-default (derived from a lower layer's constant)
    const DEFAULT_CLIENT_GRACE_SEC: f64 = DEFAULT_CLIENT_GRACE.as_secs_f64();
    // ok: literal-default (a limit, not a default)
    const MAX_PACK_MEMBERS: usize = 100_000;
    // ruleid: literal-default
    const RESIZE_POLL_INTERVAL: Duration = Duration::from_millis(500);
    // ruleid: literal-default (a static)
    static DEFAULT_READY: f64 = 120.0;
    // ruleid: literal-default (named for a wait, with no DEFAULT_ prefix)
    const READY_WAIT_SEC: u64 = 120;
    // ruleid: literal-default (a float, whatever its name)
    const LIMIT_S: f64 = 300.0;
    // ruleid: literal-default (a literal inside the value)
    const DEFAULT_WAITS: [f64; 2] = [300.0, 5.0];
    // ok: literal-default (a name that isn't a wait, of an integer type)
    const MANIFEST_VERSION: u32 = 1;

    pub fn operations(id: &str) {
        // ruleid: operation-literal
        let _ = Call::post_json("RunMicrovm", "/microvms");
        // ruleid: operation-literal
        let _ = OPS["GetMicrovm"];
        // ruleid: operation-literal
        let _ = vec![r"ListMicrovmImageVersions"];
        // ruleid: operation-literal (a `Call { .. }` literal, which clippy's type ban can't see)
        let _ = Call { operation: "SuspendMicrovm", path: String::new() };
        // ok: operation-literal (prose that names an operation)
        let _ = format!("RunMicrovm failed for {id}");
        // ok: operation-literal (a longer name)
        let _ = "GetMicrovmImages";
        // ok: operation-literal (an operation named in a comment) GetMicrovm
    }

    pub async fn defaults(options: Options, delete_timeout: f64, count: u32) {
        // ruleid: literal-default
        let _ = tokio::time::timeout(Duration::from_secs(5), health()).await;
        // ruleid: literal-default
        let _ = std::time::Duration::from_secs_f64(2.5);
        // ruleid: literal-default
        let _ = options.timeout.unwrap_or(300.0);
        // ruleid: literal-default
        let _ = crate::numbers::optional_u32(cycles)
            .map_err(js)?
            .unwrap_or(1);
        // ruleid: literal-default
        let _ = plane.wait_for_state(&id, &["TERMINATED"], &[], wait_opts(300.0));
        // ruleid: literal-default
        let _ = Duration::from_secs_f64(delete_timeout.max(1.0) + 30.0);
        // ruleid: literal-default
        let _ = 5.0 + options.deadline;
        // ruleid: literal-default (an aliased Duration)
        let _ = D::from_secs(7);
        // ruleid: literal-default
        let _ = Duration::new(8, 0);
        // ruleid: literal-default
        let _ = options.wait.map_or(300.0, |t| t);
        // ruleid: literal-default
        let _ = options.poll.get_or_insert(5.0);
        // ruleid: literal-default
        let _ = options.timeout.unwrap_or_else(|| 300.0);
        // ruleid: literal-default
        let _ = options.poll.map_or_else(|| { 5.0 }, |t| t);
        // ok: literal-default (zero, a computed value, a name, not a number, not a timeout)
        let _ = Duration::from_secs(0);
        let _ = options.offset.unwrap_or(0.0);
        let _ = options.cycles.unwrap_or(0);
        let _ = Duration::from_secs_f64(options.timeout.max(0.0));
        let _ = options.timeout.unwrap_or(fallback);
        let _ = options.name.unwrap_or("x");
        let _ = count + 1;
        let _ = Buffer::new(5);
        let _ = options.timeout.unwrap_or_else(|| fallback);
        let _ = options.count.map_or(0, |c| c + 1);
    }

    #[cfg(test)]
    mod tests {
        // ok: operation-literal, literal-default (test code)
        fn f() {
            let _ = Call::get("ListMicrovms", "/microvms");
            let _ = Duration::from_secs(5);
        }
    }

    #[cfg(test)]
    mod guards;
"""


class AdapterLogicTests(unittest.TestCase):
    """The adapter-logic collector: `operation-literal` and `literal-default` (#273)."""

    @classmethod
    def setUpClass(cls):
        class Cleanups:
            addCleanup = cls.addClassCleanup

        ws = (
            Workspace(Cleanups())
            .crate("kernel", files={"src/lib.rs": KERNEL_LOGIC})
            .crate(
                "adapter",
                deps='kernel = { path = "../kernel" }',
                files={
                    "src/lib.rs": ADAPTER_LOGIC_FORMS,
                    # Test-only by its parent's declaration, like the CLI's guards.rs.
                    "src/guards.rs": 'fn f() { let _ = "TerminateMicrovm"; }\n',
                },
            )
            .placement('[adapter]\nnormal = ["kernel"]\n')
        )
        cls.found = keys(collect(ws.scope()), "adapter-logic")

    def assertFound(self, *texts, rule):
        for text in texts:
            self.assertIn(f"adapter/src/lib.rs: {rule}: {text}", self.found)

    def test_an_operation_literal_is_flagged_in_a_call_an_index_and_a_macro(self):
        self.assertFound(
            '"RunMicrovm"',
            '"GetMicrovm"',
            'r"ListMicrovmImageVersions"',
            rule="operation-literal",
        )

    def test_an_operation_in_a_struct_literal_is_flagged(self):
        # The backstop for the clippy bans: a struct expression isn't a type position, so
        # `disallowed-types` doesn't report a hand-built `Call`, but its operation is a literal.
        self.assertFound('"SuspendMicrovm"', rule="operation-literal")

    def test_a_default_const_is_flagged(self):
        self.assertFound(
            "const DEFAULT_WAIT: f64 = 300.0;",
            "pub(crate) const DEFAULT_PORT: u16 = 9000;",
            rule="literal-default",
        )

    def test_a_static_a_named_const_a_float_and_a_nested_literal_are_flagged(self):
        self.assertFound(
            "static DEFAULT_READY: f64 = 120.0;",
            "const READY_WAIT_SEC: u64 = 120;",
            "const LIMIT_S: f64 = 300.0;",
            "const DEFAULT_WAITS: [f64; 2] = [300.0, 5.0];",
            rule="literal-default",
        )

    def test_an_aliased_duration_and_duration_new_are_flagged(self):
        self.assertFound(
            "D::from_secs(7)", "Duration::new(8, 0)", rule="literal-default"
        )

    def test_a_map_or_or_a_closure_fallback_is_flagged(self):
        self.assertFound(
            "options.wait.map_or(300.0, |t| t)",
            "options.poll.get_or_insert(5.0)",
            "options.timeout.unwrap_or_else(|| 300.0)",
            "options.poll.map_or_else(|| { 5.0 }, |t| t)",
            rule="literal-default",
        )

    def test_a_duration_from_a_literal_is_flagged(self):
        self.assertFound(
            "Duration::from_millis(500)",
            "Duration::from_secs(5)",
            "std::time::Duration::from_secs_f64(2.5)",
            rule="literal-default",
        )

    def test_an_unwrap_or_literal_is_flagged(self):
        self.assertFound(
            "options.timeout.unwrap_or(300.0)",
            "crate::numbers::optional_u32(cycles).map_err(js)?.unwrap_or(1)",
            rule="literal-default",
        )

    def test_a_wait_opts_literal_is_flagged(self):
        self.assertFound("wait_opts(300.0)", rule="literal-default")

    def test_a_literal_added_to_a_timeout_is_flagged(self):
        self.assertFound(
            "delete_timeout.max(1.0) + 30.0",
            "5.0 + options.deadline",
            rule="literal-default",
        )

    def test_the_fixture_reports_exactly_its_ruleid_lines(self):
        # Every `ok:` line is out: prose, a longer name, a comment, zero, a computed value, a
        # name that isn't a default, test code, a test-only file and a crate below the adapters.
        self.assertEqual(
            self.found,
            sorted(
                [
                    'adapter/src/lib.rs: operation-literal: "GetMicrovm"',
                    'adapter/src/lib.rs: operation-literal: "RunMicrovm"',
                    'adapter/src/lib.rs: operation-literal: r"ListMicrovmImageVersions"',
                    'adapter/src/lib.rs: operation-literal: "SuspendMicrovm"',
                    "adapter/src/lib.rs: literal-default: const DEFAULT_WAIT: f64 = 300.0;",
                    "adapter/src/lib.rs: literal-default: "
                    "pub(crate) const DEFAULT_PORT: u16 = 9000;",
                    "adapter/src/lib.rs: literal-default: Duration::from_millis(500)",
                    "adapter/src/lib.rs: literal-default: Duration::from_secs(5)",
                    "adapter/src/lib.rs: literal-default: "
                    "std::time::Duration::from_secs_f64(2.5)",
                    "adapter/src/lib.rs: literal-default: options.timeout.unwrap_or(300.0)",
                    "adapter/src/lib.rs: literal-default: "
                    "crate::numbers::optional_u32(cycles).map_err(js)?.unwrap_or(1)",
                    "adapter/src/lib.rs: literal-default: wait_opts(300.0)",
                    "adapter/src/lib.rs: literal-default: delete_timeout.max(1.0) + 30.0",
                    "adapter/src/lib.rs: literal-default: 5.0 + options.deadline",
                    "adapter/src/lib.rs: literal-default: static DEFAULT_READY: f64 = 120.0;",
                    "adapter/src/lib.rs: literal-default: const READY_WAIT_SEC: u64 = 120;",
                    "adapter/src/lib.rs: literal-default: const LIMIT_S: f64 = 300.0;",
                    "adapter/src/lib.rs: literal-default: "
                    "const DEFAULT_WAITS: [f64; 2] = [300.0, 5.0];",
                    "adapter/src/lib.rs: literal-default: D::from_secs(7)",
                    "adapter/src/lib.rs: literal-default: Duration::new(8, 0)",
                    "adapter/src/lib.rs: literal-default: options.wait.map_or(300.0, |t| t)",
                    "adapter/src/lib.rs: literal-default: options.poll.get_or_insert(5.0)",
                    "adapter/src/lib.rs: literal-default: "
                    "options.timeout.unwrap_or_else(|| 300.0)",
                    "adapter/src/lib.rs: literal-default: "
                    "options.poll.map_or_else(|| { 5.0 }, |t| t)",
                ]
            ),
        )


#: A crate below the adapters, where operation literals and defaults belong. Nothing here is
#: adapter logic.
KERNEL_LOGIC = """\
    const DEFAULT_READY_TIMEOUT: f64 = 120.0;
    pub fn run() {
        let _ = Call::post_json("RunMicrovm", "/microvms");
        let _ = Duration::from_secs(5);
    }
"""


PARITY_TABLE = """
[[capability]]
id = "adopt"
core = "microvms_core::sandbox::Sandbox::adopt"
cli = { exempt = "no command adopts a Sandbox", issue = "#269" }
py = "Sandbox.adopt"
ts = "Sandbox.adopt"

[[capability]]
id = "ledger"
core = { exempt = "local CLI state" }
cli = "ledger"
py = { exempt = "local CLI state" }
ts = { exempt = "local CLI state", issue = "#264" }

[[type]]
name = "Session"
exempt_members.ts = { spawn = { exempt = "TS only", issue = "#261" }, create = "the factory idiom" }

[exempt_names]
ts = { __napiBindingTarget = "a napi-rs build artifact" }
"""


class ParityGapTests(unittest.TestCase):
    """The parity-gap collector: `check-parity.py --exemptions`, the records with an issue."""

    def scope_with(self, text):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        table = Path(tmp.name) / "capabilities.toml"
        table.write_text(text)
        return RATCHET["SENTINEL"]._replace(parity_table=table)

    def test_a_tracked_exemption_is_a_gap_and_a_decision_is_not(self):
        gaps = RATCHET["parity_gaps"](self.scope_with(PARITY_TABLE))
        self.assertEqual(
            gaps,
            found(
                ("parity-gap", "adopt/cli"),
                ("parity-gap", "ledger/ts"),
                ("parity-gap", "Session.spawn/ts"),
            ),
        )

    def test_an_issue_the_check_cannot_read_is_an_error(self):
        # A placeholder issue would otherwise drop out of the count as though it were a decision.
        scope = self.scope_with(PARITY_TABLE.replace('"#264"', '"TBD"'))
        with self.assertRaisesRegex(
            SystemExit, "issue 'TBD' isn't of the form #<number>"
        ):
            RATCHET["parity_gaps"](scope)

    def test_a_missing_table_is_an_error(self):
        scope = RATCHET["SENTINEL"]._replace(
            parity_table=Path("/nonexistent/table.toml")
        )
        with self.assertRaisesRegex(SystemExit, "check-parity.py --exemptions"):
            RATCHET["parity_gaps"](scope)

    def test_a_scope_without_a_table_has_no_gaps(self):
        # The throwaway workspaces the other collector tests build have no table.
        ws = Workspace(self).crate("adapter").placement("[adapter]\nnormal = []\n")
        self.assertEqual(RATCHET["parity_gaps"](ws.scope()), Counter())

    def test_the_repo_scope_reads_the_real_table(self):
        self.assertEqual(
            RATCHET["REPO"].parity_table, ROOT / "parity" / "capabilities.toml"
        )


class UntracedTests(unittest.TestCase):
    """The untraced collector: every spec key that check-trace.py's `TRACED` doesn't list."""

    #: The keys the sentinel's two specs define that its `traced.py` doesn't list: one spec
    #: each at least, so a collector that read only the first spec would miss one.
    SENTINEL_UNTRACED = ("DAEMON-2", "GATE-2", "GATE-3", "GATE-4")

    def copy(self):
        """A copy of the sentinel fixture and a scope over it, for a test to break."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "sentinel"
        shutil.copytree(RATCHET["SENTINEL"].root, root)
        scope = RATCHET["SENTINEL"]._replace(
            root=root,
            placement=root / "placement.toml",
            parity_table=root / "parity" / "capabilities.toml",
            specs=tuple(
                root / spec.relative_to(RATCHET["SENTINEL"].root)
                for spec in RATCHET["SENTINEL"].specs
            ),
            traced=root
            / RATCHET["SENTINEL"].traced.relative_to(RATCHET["SENTINEL"].root),
        )
        return root, scope

    def test_the_fixture_spec_yields_exactly_its_untraced_keys(self):
        self.assertEqual(
            RATCHET["untraced"](RATCHET["SENTINEL"]),
            found(*(("untraced", key) for key in self.SENTINEL_UNTRACED)),
        )

    def test_the_sentinel_expects_the_same_keys(self):
        expected = json.loads(
            (RATCHET["SENTINEL"].root / "expected.json").read_text(encoding="utf-8")
        )
        self.assertEqual(sorted(expected["untraced"]), list(self.SENTINEL_UNTRACED))

    def test_an_empty_spec_fails_the_sentinel(self):
        # A spec the reader gets nothing from would otherwise report every entry fixed, and
        # `ratchet:update` would delete them all.
        root, scope = self.copy()
        for spec in scope.specs:
            spec.write_text(json.dumps({"requirements": {}}))
        with self.assertRaisesRegex(SystemExit, "defines no requirement keys"):
            sentinel(scope)

    def test_one_empty_spec_is_refused_too(self):
        root, scope = self.copy()
        scope.specs[1].write_text(json.dumps({"requirements": {}}))
        with self.assertRaisesRegex(
            SystemExit, "agentd.symspec.json defines no requirement keys"
        ):
            RATCHET["untraced"](scope)

    def test_a_spec_without_requirements_is_refused(self):
        root, scope = self.copy()
        scope.specs[0].write_text(json.dumps({"glossary": []}))
        with self.assertRaisesRegex(SystemExit, "has no requirements object"):
            RATCHET["untraced"](scope)

    def test_a_missing_spec_is_refused(self):
        root, scope = self.copy()
        scope.specs[0].unlink()
        with self.assertRaisesRegex(SystemExit, "core.symspec.json is missing"):
            RATCHET["untraced"](scope)

    def test_a_fully_traced_spec_fails_the_sentinel(self):
        # The fixture exists to have untraced keys; one that has none proves nothing.
        root, scope = self.copy()
        text = scope.traced.read_text(encoding="utf-8")
        scope.traced.write_text(
            text.replace(
                "TRACED = {",
                'TRACED = {\n    "GATE-2": "#3",\n    "GATE-3": "#3",'
                '\n    "DAEMON-2": "#3",',
            ).replace(
                '("#3", {layer: "nothing checks it" for layer in LAYERS})', '"#3"'
            )
        )
        self.assertEqual(RATCHET["untraced"](scope), Counter())
        failures = sentinel(scope)
        self.assertIn(
            f"sentinel: the untraced collector found nothing in {root}, so it can't be "
            "trusted to find drift in the tree either.",
            failures,
        )

    def test_an_empty_traced_is_refused(self):
        # An empty table reads as every key untraced, which is new drift, but a TRACED the
        # script lost is a broken read, not a hundred new requirements.
        root, scope = self.copy()
        scope.traced.write_text("TRACED = {}\n")
        with self.assertRaisesRegex(SystemExit, "TRACED is empty"):
            RATCHET["untraced"](scope)

    def test_a_script_without_traced_is_refused(self):
        root, scope = self.copy()
        scope.traced.write_text("TRACKED = {'GATE-1': '#1'}\n")
        with self.assertRaisesRegex(SystemExit, "has no TRACED dict"):
            RATCHET["untraced"](scope)

    def test_traced_is_read_by_running_the_script(self):
        # `TRACED` names module constants (`INTERLEAVINGS`), so a literal read can't take it;
        # the fixture's waiver does the same, and the key it waives stays traced.
        text = RATCHET["SENTINEL"].traced.read_text(encoding="utf-8")
        self.assertRegex(text, r'"DAEMON-1": \([^)]*\bREASON\b')
        self.assertNotIn(
            ("untraced", "DAEMON-1"), RATCHET["untraced"](RATCHET["SENTINEL"])
        )

    def test_a_key_that_waives_every_layer_stays_untraced(self):
        # trace:check passes a listed key whose every layer is waived, with nothing checking
        # it; counting it as traced would let the backlog shrink by a waiver.
        text = RATCHET["SENTINEL"].traced.read_text(encoding="utf-8")
        self.assertRegex(
            text, r'"GATE-4": \("#3", \{layer: [^}]* for layer in LAYERS\}\)'
        )
        self.assertIn(("untraced", "GATE-4"), RATCHET["untraced"](RATCHET["SENTINEL"]))

    def test_a_key_that_waives_all_but_one_layer_is_traced(self):
        root, scope = self.copy()
        text = scope.traced.read_text(encoding="utf-8")
        scope.traced.write_text(
            text.replace(
                "for layer in LAYERS}", 'for layer in LAYERS if layer != "test"}'
            )
        )
        self.assertNotIn(("untraced", "GATE-4"), RATCHET["untraced"](scope))

    def test_a_script_without_layers_is_refused(self):
        # Without LAYERS every key would read as waiving all of them, or none.
        root, scope = self.copy()
        text = scope.traced.read_text(encoding="utf-8")
        scope.traced.write_text(
            text.replace("LAYERS = (", "LAYER_NAMES = (").replace(
                "for layer in LAYERS}", "for layer in LAYER_NAMES}"
            )
        )
        with self.assertRaisesRegex(SystemExit, "has no LAYERS tuple"):
            RATCHET["untraced"](scope)

    def test_a_requirement_without_a_key_is_refused(self):
        # check-trace.py skips it too, so neither gate would count or name it.
        root, scope = self.copy()
        for key in ("", None):
            document = json.loads(scope.specs[1].read_text(encoding="utf-8"))
            requirement = {"sentence": "The daemon shall do a keyless thing."}
            if key is not None:
                requirement["key"] = key
            document["requirements"]["00000000-0000-4000-8000-000000000019"] = (
                requirement
            )
            scope.specs[1].write_text(json.dumps(document))
            with (
                self.subTest(key=key),
                self.assertRaisesRegex(
                    SystemExit,
                    "agentd.symspec.json: requirement 00000000-0000-4000-8000-000000000019 has "
                    "no key, so TRACED can't list it",
                ),
            ):
                RATCHET["untraced"](scope)

    def test_a_table_with_no_spec_is_refused(self):
        root, scope = self.copy()
        with self.assertRaisesRegex(SystemExit, "the scope names no spec"):
            RATCHET["untraced"](scope._replace(specs=()))

    def test_a_scope_without_specs_has_no_untraced_keys(self):
        # The throwaway workspaces the other collector tests build have no spec.
        ws = Workspace(self).crate("adapter").placement("[adapter]\nnormal = []\n")
        self.assertEqual(RATCHET["untraced"](ws.scope()), Counter())

    def test_the_repo_scope_reads_every_spec_check_trace_reads(self):
        trace = runpy.run_path(str(HERE / "check-trace.py"))
        self.assertEqual(RATCHET["REPO"].traced, HERE / "check-trace.py")
        self.assertEqual(RATCHET["REPO"].specs, trace["SPECS"])
        # A spec file check-trace.py doesn't list would be outside both scripts.
        self.assertEqual(
            sorted(RATCHET["REPO"].specs),
            sorted((ROOT / "spec").glob("*.symspec.json")),
        )


class SentinelTests(unittest.TestCase):
    def test_the_checked_in_sentinel_matches_its_expectation(self):
        self.assertEqual(sentinel(RATCHET["SENTINEL"]), [])

    def test_a_collector_that_finds_nothing_fails(self):
        ws = Workspace(self).crate("adapter").placement("[adapter]\nnormal = []\n")
        empty = ws.scope()
        failures = sentinel(empty)
        for category in (
            "placement",
            "subprocess",
            "port-impl",
            "adapter-logic",
            "parity-gap",
            "untraced",
        ):
            self.assertTrue(
                any(
                    f.startswith(f"sentinel: the {category} collector found nothing")
                    for f in failures
                ),
                failures,
            )

    def test_a_sentinel_that_reports_a_test_only_case_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "sentinel"
            shutil.copytree(RATCHET["SENTINEL"].root, copy)
            lib = copy / "adapter" / "src" / "lib.rs"
            lib.write_text(
                lib.read_text().replace("#[cfg(test)]\nmod tests", "mod tests")
            )
            failures = sentinel(
                RATCHET["SENTINEL"]._replace(
                    root=copy, placement=copy / "placement.toml"
                )
            )
            self.assertTrue(any("unexpected" in f for f in failures), failures)


class SeededFaultTests(unittest.TestCase):
    """The faults #281 and #285 name. Each also fired once by hand in the real tree (see the PR)."""

    def real_placement(self, ws):
        # The real file has sets for the layers below the adapters (#282, #283), and the ratchet
        # refuses a set for a crate the workspace doesn't have. An empty layer uses nothing
        # outside its set.
        placement = ROOT / "arch" / "placement.toml"
        shutil.copy(placement, ws.root / "placement.toml")
        for crate in sets_from(placement.read_text(), str(placement)):
            if crate not in ws.crates:
                ws.crate(crate)
        return ws

    def test_a_reqwest_in_microvms_py_fails_with_a_placement_key(self):
        ws = self.real_placement(
            Workspace(self)
            .crate(
                "microvms-py",
                deps='microvms-core = { path = "../microvms-core" }\nreqwest = "0.12"',
            )
            .crate("microvms-core")
            .crate("microvms-cli")
            .crate("microvms-js")
        )
        now = collect(ws.scope())
        self.assertEqual(keys(now, "placement"), ["microvms-py -> reqwest"])
        failures = compare(now, ratchet(), None, "main")
        self.assertIn(
            "new drift: [placement] microvms-py -> reqwest. Move the work to the layer "
            "whose job it is (I/O belongs in microvms-edges, behind a port in "
            "microvms-app), or add a decision with its reason.",
            failures,
        )

    def test_a_reqwest_in_microvms_app_fails_with_a_placement_key(self):
        # #283's fault: the use cases dialing the network themselves. The app's exact-set test
        # in `dependency_direction.rs` fails on it too; this is the ratchet's half.
        ws = self.real_placement(
            Workspace(self)
            .crate(
                "microvms-app",
                deps='microvms-domain = { path = "../microvms-domain" }\nreqwest = "0.12"',
            )
            .crate("microvms-domain")
            .crate("microvms-core")
            .crate("microvms-cli")
            .crate("microvms-py")
            .crate("microvms-js")
        )
        now = collect(ws.scope())
        self.assertEqual(keys(now, "placement"), ["microvms-app -> reqwest"])
        failures = compare(now, ratchet(), None, "main")
        self.assertIn(
            "new drift: [placement] microvms-app -> reqwest. Move the work to the layer "
            "whose job it is (I/O belongs in microvms-edges, behind a port in "
            "microvms-app), or add a decision with its reason.",
            failures,
        )

    def test_a_globset_in_microvms_py_fails_with_a_placement_key(self):
        # #285's fault. The binding's exact-set test in `dependency_direction.rs` fails on it
        # too; this is the ratchet's half.
        ws = self.real_placement(
            Workspace(self)
            .crate(
                "microvms-py",
                deps='microvms-core = { path = "../microvms-core" }\nglobset = "0.4"',
            )
            .crate("microvms-core")
            .crate("microvms-cli")
            .crate("microvms-js")
        )
        now = collect(ws.scope())
        self.assertEqual(keys(now, "placement"), ["microvms-py -> globset"])
        self.assertIn(
            "new drift: [placement] microvms-py -> globset. Move the work to the layer "
            "whose job it is (I/O belongs in microvms-edges, behind a port in "
            "microvms-app), or add a decision with its reason.",
            compare(now, ratchet(), None, "main"),
        )

    def test_an_aws_subprocess_in_microvms_js_fails_with_a_subprocess_key(self):
        ws = self.real_placement(
            Workspace(self)
            .crate(
                "microvms-js",
                files={
                    "src/session.rs": """\
                        pub fn upload() {
                            std::process::Command::new("aws");
                        }
                    """
                },
            )
            .crate("microvms-core")
            .crate("microvms-cli")
            .crate("microvms-py")
        )
        now = collect(ws.scope())
        key = 'microvms-js/src/session.rs: std::process::Command::new("aws")'
        self.assertEqual(keys(now, "subprocess"), [key])
        self.assertIn(
            f"new drift: [subprocess] {key}. Move the work to the layer "
            "whose job it is (I/O belongs in microvms-edges, behind a port in "
            "microvms-app), or add a decision with its reason.",
            compare(now, ratchet(), None, "main"),
        )

    def sentinel_copy(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        copy = Path(tmp.name) / "sentinel"
        shutil.copytree(RATCHET["SENTINEL"].root, copy)
        scope = RATCHET["SENTINEL"]._replace(
            root=copy, placement=copy / "placement.toml"
        )
        expected = json.loads((copy / "expected.json").read_text())
        entries = [(c, k, 1) for c, ks in expected.items() for k in ks]
        return scope, entries

    def test_an_entry_deleted_without_a_fix_fails_rule_1(self):
        scope, entries = self.sentinel_copy()
        now = collect(scope)
        self.assertEqual(compare(now, ratchet(entries), None, "main"), [])
        deleted = entries[0]
        failures = compare(now, ratchet(entries[1:]), None, "main")
        self.assertEqual([f.split(":")[0] for f in failures], ["new drift"])
        self.assertIn(deleted[1], failures[0])

    def test_a_fix_without_an_updated_file_fails_rule_2(self):
        scope, entries = self.sentinel_copy()
        lib = scope.root / "adapter" / "src" / "lib.rs"
        text = lib.read_text()
        self.assertIn('std::process::Command::new("aws");', text)
        lib.write_text(text.replace('std::process::Command::new("aws");', "", 1))
        failures = compare(collect(scope), ratchet(entries), None, "main")
        self.assertEqual(
            failures,
            [
                'fixed: [subprocess] adapter/src/lib.rs: std::process::Command::new("aws"). '
                "Run `mise run ratchet:update` and commit the file."
            ],
        )


# The crate-root attribute that turns the adapters' clippy rules into errors under a plain
# `cargo clippy`, not only under `lint`'s `-D warnings`.
DENY = "#![deny(clippy::disallowed_methods, clippy::disallowed_types)]"

# Every site in an adapter's `src/` that turns one of those lints off, by file and lint, with
# how many `#[expect]` attributes it carries there. Clippy itself can't tell a reviewed
# exception from a quiet bypass, so this list is the record: a new `expect` fails until it's
# added here, and an `allow` or `warn` fails outright. A `disallowed_types` site also needs its
# subprocess entry or decision in `ratchet/drift.json`. The environment reads have no drift
# category, so for them this list is the only record.
LINT_EXCEPTIONS = {
    # The CLI's composition root, which hands core's process lookup to every handler.
    ("microvms-cli/src/main.rs", "clippy::disallowed_methods"): 1,
    # `put_via_aws_cli`, which #258 deletes.
    ("microvms-cli/src/seam.rs", "clippy::disallowed_types"): 1,
    # The one door to AWS: `production_plane` and `production_session`, each holding one call
    # to a core constructor. Not the whole `impl CoreSeam for AwsSeam`, which would turn the
    # transport and environment bans off for every line of it.
    ("microvms-cli/src/seam.rs", "clippy::disallowed_methods"): 2,
    # The test-only guards: each fake seam builds a plane or session over a scripted transport,
    # and each scripted transport names `Call`. `cfg(test)`, so none of it ships.
    ("microvms-cli/src/guards.rs", "clippy::disallowed_methods"): 7,
    ("microvms-cli/src/guards.rs", "clippy::disallowed_types"): 4,
    # `doctor`'s `terraform output`, a subprocess decision.
    ("microvms-cli/src/commands/doctor.rs", "clippy::disallowed_types"): 1,
    # Each binding's name store, the one place it composes core's process lookup.
    ("microvms-py/src/names.rs", "clippy::disallowed_methods"): 1,
    ("microvms-js/src/names.rs", "clippy::disallowed_methods"): 1,
}

# What each adapter's `clippy.toml` bans, by path. Every adapter refuses a subprocess, a direct
# environment read, and the control plane's transport (#273): a wire call built in an adapter
# is one the other surfaces don't make, with retries they don't share. The CLI alone refuses
# core's doors too, since it reaches AWS through `src/seam.rs` and nothing else; the bindings
# call those doors legitimately. Paths are core's re-exports.
SUBPROCESS_TYPES = ["std::process::Command", "tokio::process::Command"]
ENV_METHODS = [
    "std::env::var",
    "std::env::var_os",
    "std::env::vars",
    "std::env::vars_os",
    "microvms_core::env::process",
]
TRANSPORT = "microvms_core::control::transport"
TRANSPORT_TYPES = [f"{TRANSPORT}::Call"]
TRANSPORT_METHODS = [
    *(
        f"{TRANSPORT}::Call::{name}"
        for name in ("get", "post_json", "patch_json", "post_empty", "delete")
    ),
    f"{TRANSPORT}::send_with_retry",
    f"{TRANSPORT}::send_accepting",
    f"{TRANSPORT}::Transport::send",
    "microvms_core::control::ControlPlane::from_ports",
    "microvms_core::prelude::ControlPlaneExt::with_transport",
]
DOOR_TYPES = [
    f"{TRANSPORT}::SignedTransport",
    "microvms_core::control::SignedBuildServices",
    "microvms_core::session::http::ReqwestBackend",
    "microvms_core::adapters::SystemAdapters",
]
# Every public core function that resolves credentials and builds a signed plane or session.
DOOR_METHODS = [
    "microvms_core::prelude::ControlPlaneExt::new",
    "microvms_core::prelude::SandboxExt::new",
    "microvms_core::prelude::SandboxExt::adopt_in",
    "microvms_core::prelude::SandboxExt::from_name",
    "microvms_core::prelude::AgentVmExt::adopt_in",
    "microvms_core::prelude::AgentVmExt::from_name",
    "microvms_core::prelude::SessionExt::connect",
    "microvms_core::prelude::SessionExt::direct",
    "microvms_core::prelude::SessionExt::attach",
    "microvms_core::session::Session::builder",
    "microvms_core::preflight::preflight",
    "microvms_core::adapters::Adapters::http_backend",
    "microvms_core::adapters::Adapters::build_services",
]


def banned(adapter):
    """`(types, methods)` the adapter's `clippy.toml` must list, exactly."""
    cli = adapter == "microvms-cli"
    types = SUBPROCESS_TYPES + TRANSPORT_TYPES + (DOOR_TYPES if cli else [])
    methods = ENV_METHODS + TRANSPORT_METHODS + (DOOR_METHODS if cli else [])
    return sorted(types), sorted(methods)


# A stand-in for core with the real one's paths, so the adapter's `clippy.toml` resolves in a
# throwaway crate the way it does in the workspace. The transport and the doors are defined in
# stand-ins for the app and the edges and reach core through `pub use`, globbed where core
# globs, because that's the shape clippy has to see through.
STAND_IN_APP = """\
    pub mod adapters {
        pub trait Adapters {
            fn http_backend(&self, endpoint: &str) -> u16;
            fn build_services(&self, region: crate::control::Region) -> u16;
        }
    }

    pub mod control {
        use std::sync::Arc;

        pub struct Region;

        pub struct ControlPlane;

        impl ControlPlane {
            pub fn from_ports(_transport: Arc<dyn transport::Transport>, _region: Region) -> Self {
                ControlPlane
            }
        }

        pub mod transport {
            pub trait Transport: Send + Sync {
                fn send(&self, call: Call) -> u16;
            }

            pub struct Call {
                pub operation: &'static str,
                pub path: String,
            }

            impl Call {
                pub fn get(operation: &'static str, path: &str) -> Self {
                    Call { operation, path: path.into() }
                }
                pub fn post_json(operation: &'static str, path: &str, _body: &str) -> Self {
                    Call { operation, path: path.into() }
                }
                pub fn patch_json(operation: &'static str, path: &str, _body: &str) -> Self {
                    Call { operation, path: path.into() }
                }
                pub fn post_empty(operation: &'static str, path: &str) -> Self {
                    Call { operation, path: path.into() }
                }
                pub fn delete(operation: &'static str, path: &str) -> Self {
                    Call { operation, path: path.into() }
                }
            }

            pub fn send_with_retry(_transport: &dyn Transport, _call: Call) {}

            pub fn send_accepting(_transport: &dyn Transport, _call: Call, _accept: &[u16]) {}
        }
    }

    pub mod sandbox {
        pub struct Sandbox;
    }

    pub mod session {
        pub struct Session;

        pub struct SessionBuilder;

        impl Session {
            pub fn builder(_endpoint: &str, _agent_token: &str) -> SessionBuilder {
                SessionBuilder
            }
        }
    }
"""

STAND_IN_EDGES = """\
    pub mod adapters {
        use microvms_app::control::Region;

        pub struct SystemAdapters;

        impl microvms_app::adapters::Adapters for SystemAdapters {
            fn http_backend(&self, _endpoint: &str) -> u16 {
                200
            }
            fn build_services(&self, _region: Region) -> u16 {
                200
            }
        }
    }

    pub mod control {
        use microvms_app::control::Region;

        pub struct SignedBuildServices {
            _region: Region,
        }

        impl SignedBuildServices {
            pub fn new(region: Region) -> Self {
                SignedBuildServices { _region: region }
            }
        }

        pub mod transport {
            use microvms_app::control::Region;

            pub struct SignedTransport {
                _region: Region,
            }

            impl SignedTransport {
                pub fn new(region: Region) -> Self {
                    SignedTransport { _region: region }
                }
            }

            impl microvms_app::control::transport::Transport for SignedTransport {
                fn send(&self, _call: microvms_app::control::transport::Call) -> u16 {
                    200
                }
            }
        }
    }

    pub mod session {
        pub mod http {
            pub struct ReqwestBackend {
                _base_url: String,
            }

            impl ReqwestBackend {
                pub fn new(base_url: &str) -> Self {
                    ReqwestBackend { _base_url: base_url.into() }
                }
            }
        }
    }
"""

STAND_IN_CORE = """\
    pub use microvms_app::sandbox;

    pub mod adapters {
        pub use microvms_app::adapters::*;
        pub use microvms_edges::adapters::SystemAdapters;
    }

    pub mod agents {
        pub struct AgentVm;
    }

    pub mod preflight {
        pub fn preflight(_region: Option<crate::control::Region>) -> bool {
            true
        }
    }

    pub mod env {
        pub fn process(name: &str) -> Option<String> {
            std::env::var(name).ok()
        }
    }

    pub mod control {
        pub use microvms_app::control::*;
        pub use microvms_edges::control::SignedBuildServices;

        pub mod transport {
            pub use microvms_app::control::transport::*;
            pub use microvms_edges::control::transport::SignedTransport;
        }
    }

    pub mod session {
        pub use microvms_app::session::*;

        pub mod http {
            pub use microvms_edges::session::http::ReqwestBackend;
        }
    }

    pub mod prelude {
        use std::sync::Arc;

        use crate::agents::AgentVm;
        use crate::control::transport::Transport;
        use crate::control::{ControlPlane, Region};
        use crate::sandbox::Sandbox;
        use crate::session::Session;

        pub trait ControlPlaneExt: Sized {
            fn new(region: Region) -> Self;
            fn with_transport(transport: Arc<dyn Transport>, region: Region) -> Self;
        }

        impl ControlPlaneExt for ControlPlane {
            fn new(region: Region) -> Self {
                let signed = microvms_edges::control::transport::SignedTransport::new(Region);
                ControlPlane::from_ports(Arc::new(signed), region)
            }
            fn with_transport(transport: Arc<dyn Transport>, region: Region) -> Self {
                ControlPlane::from_ports(transport, region)
            }
        }

        pub trait SandboxExt: Sized {
            fn new(region: Region) -> Self;
            fn adopt_in(region: Region, microvm_id: &str) -> Self;
            fn from_name(name: &str, region: Option<Region>) -> Self;
        }

        impl SandboxExt for Sandbox {
            fn new(_region: Region) -> Self {
                Sandbox
            }
            fn adopt_in(_region: Region, _microvm_id: &str) -> Self {
                Sandbox
            }
            fn from_name(_name: &str, _region: Option<Region>) -> Self {
                Sandbox
            }
        }

        pub trait AgentVmExt: Sized {
            fn adopt_in(region: Region, microvm_id: &str) -> Self;
            fn from_name(name: &str, region: Option<Region>) -> Self;
        }

        impl AgentVmExt for AgentVm {
            fn adopt_in(_region: Region, _microvm_id: &str) -> Self {
                AgentVm
            }
            fn from_name(_name: &str, _region: Option<Region>) -> Self {
                AgentVm
            }
        }

        pub trait SessionExt: Sized {
            fn connect(endpoint: &str) -> Self;
            fn attach(endpoint: &str, microvm_id: &str) -> Self;
            fn direct(endpoint: &str, agent_token: &str) -> Self;
        }

        impl SessionExt for Session {
            fn connect(_endpoint: &str) -> Self {
                Session
            }
            fn attach(_endpoint: &str, _microvm_id: &str) -> Self {
                Session
            }
            fn direct(_endpoint: &str, _agent_token: &str) -> Self {
                Session
            }
        }
    }
"""

# One clippy diagnostic of a ban: the path it names and the file it points at.
DISALLOWED = re.compile(
    r"^(?:error|warning): use of a disallowed (?:method|type) `([^`]+)`.*\n\s*--> ([^:\n]+):",
    re.MULTILINE,
)


def disallowed(stderr):
    """Each `(banned path, file name)` clippy reported."""
    return {(path, Path(file).name) for path, file in DISALLOWED.findall(stderr)}


# The lint names an attribute could use to turn the adapter rules off: the two lints and the
# groups they belong to.
SILENCEABLE = re.compile(r"clippy::(?:disallowed_methods|disallowed_types|style|all)\b")
LEVEL = re.compile(r"\b(allow|warn|expect|deny|forbid)\s*\(")


def lint_levels(text):
    """`(level, lint, line)` for each mention of a `SILENCEABLE` lint in a lint attribute.

    Line comments are dropped first, so prose naming a lint isn't counted. The level is the
    last one opened between the attribute's `#` and the lint, which also reads the level inside
    `cfg_attr(...)`.
    """
    code = re.sub(r"//.*", "", text)
    for match in SILENCEABLE.finditer(code):
        start = code.rfind("#", 0, match.start())
        levels = LEVEL.findall(code[start : match.start()])
        yield (
            levels[-1] if levels else None,
            match.group(0),
            code.count("\n", 0, match.start()) + 1,
        )


class AdapterLintTests(unittest.TestCase):
    """Each driving adapter's `clippy.toml` refuses a subprocess, a direct environment read and
    the control plane's transport, and the CLI's refuses core's doors.

    The fault cases copy the adapter's real `clippy.toml` into a throwaway crate and run real
    clippy over it, because the rule's paths only mean something to the toolchain: a path
    clippy can't resolve is only a warning, and bans nothing. Cargo runs from the repository
    root so rustup picks the toolchain `rust-toolchain.toml` pins, clippy included.
    """

    def roots(self):
        """Each adapter's crate root (its `lib` or `bin` target), from `cargo metadata`."""
        metadata = RATCHET["cargo_metadata"](RATCHET["REPO"])
        roots = {}
        for package in metadata["packages"]:
            if package["name"] not in RATCHET["REPO"].adapters:
                continue
            for target in package["targets"]:
                if {"lib", "cdylib", "bin"} & set(target["kind"]):
                    roots[package["name"]] = Path(target["src_path"])
        self.assertEqual(sorted(roots), sorted(RATCHET["REPO"].adapters))
        return roots

    def clippy(self, adapter, files):
        """Real clippy over a crate that carries `adapter`'s `clippy.toml` and `files`.

        The crate depends on the stand-in core above, so every banned path resolves the way it
        does in the workspace, and the run asserts none of them failed to.
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for name, deps, source in (
            ("microvms-app", "", STAND_IN_APP),
            (
                "microvms-edges",
                'microvms-app = { path = "../microvms-app" }\n',
                STAND_IN_EDGES,
            ),
            (
                "microvms-core",
                'microvms-app = { path = "../microvms-app" }\n'
                'microvms-edges = { path = "../microvms-edges" }\n',
                STAND_IN_CORE,
            ),
        ):
            stand_in = Path(tmp.name) / name
            (stand_in / "src").mkdir(parents=True)
            (stand_in / "Cargo.toml").write_text(
                f'[package]\nname = "{name}"\nversion = "0.0.0"\nedition = "2024"\n'
                f"publish = false\n\n[workspace]\n\n[dependencies]\n{deps}"
            )
            (stand_in / "src" / "lib.rs").write_text(textwrap.dedent(source))
        crate = Path(tmp.name) / adapter
        (crate / "src").mkdir(parents=True)
        (crate / "Cargo.toml").write_text(
            f'[package]\nname = "{adapter}"\nversion = "0.0.0"\nedition = "2024"\n'
            "publish = false\n\n[workspace]\n\n[dependencies]\n"
            'microvms-core = { path = "../microvms-core" }\n'
        )
        shutil.copy(ROOT / adapter / "clippy.toml", crate / "clippy.toml")
        for path, text in files.items():
            (crate / path).write_text(textwrap.dedent(text))
        env = {k: v for k, v in os.environ.items() if k != "CLIPPY_CONF_DIR"}
        env["CARGO_TARGET_DIR"] = str(Path(tmp.name) / "target")
        # CI sets CARGO_TERM_COLOR=always; colored diagnostics don't match DISALLOWED, and the
        # tests would read an empty set.
        out = subprocess.run(
            [
                "cargo",
                "clippy",
                "--offline",
                "--quiet",
                "--color",
                "never",
                "--manifest-path",
                str(crate / "Cargo.toml"),
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertNotIn("does not refer to a reachable", out.stderr)
        return out

    def test_every_adapter_root_denies_the_disallowed_lints(self):
        for adapter, root in self.roots().items():
            with self.subTest(adapter=adapter):
                lines = root.read_text(encoding="utf-8").splitlines()
                self.assertTrue(DENY in lines, f"{root} lacks {DENY}")

    def test_every_adapter_bans_exactly_its_listed_paths(self):
        for adapter in RATCHET["REPO"].adapters:
            with self.subTest(adapter=adapter):
                config = tomllib.loads((ROOT / adapter / "clippy.toml").read_text())
                # `get`, so a file that lost a table fails as a mismatch, not a KeyError.
                listed_types = config.get("disallowed-types", [])
                listed_methods = config.get("disallowed-methods", [])
                types, methods = banned(adapter)
                self.assertEqual(sorted(t["path"] for t in listed_types), types)
                self.assertEqual(sorted(m["path"] for m in listed_methods), methods)
                for item in listed_types + listed_methods:
                    self.assertTrue(item.get("reason"), item)
                    self.assertNotIn("allow-invalid", item)

    def test_an_aws_subprocess_in_microvms_js_session_fails_clippy(self):
        out = self.clippy(
            "microvms-js",
            {
                "src/lib.rs": f"{DENY}\npub mod session;\n",
                "src/session.rs": """\
                    pub fn upload() {
                        let _ = std::process::Command::new("aws");
                    }
                """,
            },
        )
        self.assertNotEqual(out.returncode, 0, out.stderr)
        self.assertIn("use of a disallowed type `std::process::Command`", out.stderr)
        self.assertIn("src/session.rs", out.stderr)

    def test_an_env_read_in_a_cli_handler_fails_clippy(self):
        out = self.clippy(
            "microvms-cli",
            {
                "src/lib.rs": f"{DENY}\npub mod commands;\n",
                "src/commands.rs": """\
                    pub fn run() -> Option<String> {
                        std::env::var("AWS_REGION").ok()
                    }
                """,
            },
        )
        self.assertNotEqual(out.returncode, 0, out.stderr)
        self.assertIn("use of a disallowed method `std::env::var`", out.stderr)

    def test_every_adapter_refuses_each_banned_call(self):
        # The same faults in each crate, so a `clippy.toml` edited in one of them fails here.
        # The iterator reads and the call to core's lookup by name are each a way to read
        # `AWS_REGION` that the two single-variable bans don't see.
        for adapter in RATCHET["REPO"].adapters:
            with self.subTest(adapter=adapter):
                out = self.clippy(
                    adapter,
                    {
                        "src/lib.rs": f"""\
                            {DENY}
                            pub fn spawn() {{
                                let _ = std::process::Command::new("aws");
                            }}
                            pub fn region() -> bool {{
                                std::env::var_os("AWS_REGION").is_some()
                            }}
                            pub fn region_by_scan() -> bool {{
                                std::env::vars().any(|(key, _)| key == "AWS_REGION")
                            }}
                            pub fn region_by_os_scan() -> bool {{
                                std::env::vars_os().any(|(key, _)| key == "AWS_REGION")
                            }}
                            pub fn region_from_core() -> Option<String> {{
                                microvms_core::env::process("AWS_REGION")
                            }}
                        """,
                    },
                )
                self.assertNotEqual(out.returncode, 0, out.stderr)
                for message in [
                    "disallowed type `std::process::Command`",
                    "disallowed method `std::env::var_os`",
                    "disallowed method `std::env::vars`",
                    "disallowed method `std::env::vars_os`",
                    "disallowed method `microvms_core::env::process`",
                ]:
                    self.assertIn(message, out.stderr)

    def test_every_adapter_refuses_each_transport_call(self):
        # Each constructor by its plain path, both send functions, the port's own `send`, and
        # both transport-taking constructors. In a file of its own, a `Call { .. }` literal
        # handed to a transport: clippy 1.98 doesn't report the literal itself (a struct
        # expression isn't a type position), so what refuses it is the ban on
        # `Transport::send`, and the ratchet's operation-literal rule sees its operation.
        source = """\
            use std::sync::Arc;

            use microvms_core::control::transport::{self, Transport};
            use microvms_core::control::{ControlPlane, Region};
            use microvms_core::prelude::*;

            pub fn calls(t: &dyn Transport) {
                transport::send_with_retry(t, transport::Call::get("ListMicrovms", "/x"));
                transport::send_with_retry(t, transport::Call::post_json("RunMicrovm", "/x", "{}"));
                transport::send_with_retry(t, transport::Call::patch_json("UpdateMicrovmImageVersion", "/x", "{}"));
                transport::send_with_retry(t, transport::Call::post_empty("SuspendMicrovm", "/x"));
                transport::send_accepting(t, transport::Call::delete("DeleteMicrovmImage", "/x"), &[404]);
            }

            pub fn planes(t: Arc<dyn Transport>) -> (ControlPlane, ControlPlane) {
                (
                    ControlPlane::from_ports(Arc::clone(&t), Region),
                    ControlPlane::with_transport(t, Region),
                )
            }
        """
        literal = """\
            pub fn send(t: &dyn microvms_core::control::transport::Transport) -> u16 {
                t.send(microvms_core::control::transport::Call { operation: "GetMicrovm", path: String::new() })
            }
        """
        for adapter in RATCHET["REPO"].adapters:
            with self.subTest(adapter=adapter):
                out = self.clippy(
                    adapter,
                    {
                        "src/lib.rs": f"{DENY}\npub mod wire;\npub mod literal;\n",
                        "src/wire.rs": source,
                        "src/literal.rs": literal,
                    },
                )
                self.assertNotEqual(out.returncode, 0, out.stderr)
                found = disallowed(out.stderr)
                self.assertEqual(
                    {path for path, file in found if file in ("wire.rs", "literal.rs")},
                    set(TRANSPORT_METHODS) | set(TRANSPORT_TYPES),
                    out.stderr,
                )
                self.assertIn(
                    (f"{TRANSPORT}::Transport::send", "literal.rs"), found, out.stderr
                )

    def test_an_alias_a_ufcs_call_and_a_glob_import_are_refused_too(self):
        # The shapes a text or tree scan misses, each in its own file so each is checked by
        # itself: a type renamed at the `use`, a function renamed at the `use`, a trait method
        # called by its fully qualified form, and a function reached through a glob import.
        files = {
            "src/lib.rs": f"{DENY}\npub mod alias;\npub mod renamed;\npub mod ufcs;\npub mod glob;\n",
            "src/alias.rs": """\
                use microvms_core::control::transport::Call as Request;

                pub fn request() -> Request {
                    Request::post_empty("ResumeMicrovm", "/x")
                }
            """,
            "src/renamed.rs": """\
                use microvms_core::control::transport::send_with_retry as send;

                pub fn go(t: &dyn microvms_core::control::transport::Transport) {
                    send(t, microvms_core::control::transport::Call::get("GetMicrovm", "/x"));
                }
            """,
            "src/ufcs.rs": """\
                use std::sync::Arc;

                use microvms_core::control::transport::Transport;
                use microvms_core::control::{ControlPlane, Region};
                use microvms_core::prelude::ControlPlaneExt;

                pub fn plane(t: Arc<dyn Transport>) -> ControlPlane {
                    <ControlPlane as ControlPlaneExt>::with_transport(t, Region)
                }
            """,
            "src/glob.rs": """\
                use microvms_core::control::transport::*;

                pub fn go(t: &dyn Transport, call: Call) {
                    send_accepting(t, call, &[200]);
                }
            """,
        }
        for adapter in RATCHET["REPO"].adapters:
            with self.subTest(adapter=adapter):
                out = self.clippy(adapter, files)
                self.assertNotEqual(out.returncode, 0, out.stderr)
                found = disallowed(out.stderr)
                for expected in [
                    (f"{TRANSPORT}::Call", "alias.rs"),
                    (f"{TRANSPORT}::Call::post_empty", "alias.rs"),
                    (f"{TRANSPORT}::send_with_retry", "renamed.rs"),
                    (
                        "microvms_core::prelude::ControlPlaneExt::with_transport",
                        "ufcs.rs",
                    ),
                    (f"{TRANSPORT}::send_accepting", "glob.rs"),
                    (f"{TRANSPORT}::Call", "glob.rs"),
                ]:
                    self.assertIn(expected, found, out.stderr)

    def test_the_cli_refuses_each_core_door_by_path_alias_and_ufcs(self):
        out = self.clippy(
            "microvms-cli",
            {
                "src/lib.rs": f"{DENY}\npub mod doors;\npub mod alias;\npub mod ufcs;\n",
                "src/doors.rs": """\
                    use microvms_core::adapters::{Adapters as _, SystemAdapters};
                    use microvms_core::agents::AgentVm;
                    use microvms_core::control::{ControlPlane, Region};
                    use microvms_core::prelude::*;
                    use microvms_core::sandbox::Sandbox;
                    use microvms_core::session::Session;

                    // A type position: clippy reports a type ban there, not at a unit value.
                    pub fn adapters(_: SystemAdapters) {}

                    pub fn doors() {
                        let _ = ControlPlane::new(Region);
                        let _ = Sandbox::new(Region);
                        let _ = Sandbox::adopt_in(Region, "mvm-1");
                        let _ = Sandbox::from_name("ci", None);
                        let _ = AgentVm::adopt_in(Region, "mvm-1");
                        let _ = AgentVm::from_name("ci", None);
                        let _ = microvms_core::preflight::preflight(None);
                        let _ = SystemAdapters.http_backend("https://x");
                        let _ = SystemAdapters.build_services(Region);
                        let _ = Session::connect("https://mvm-1.example");
                        let _ = Session::direct("https://mvm-1.example", "t");
                        let _ = Session::attach("https://mvm-1.example", "mvm-1");
                        let _ = Session::builder("https://mvm-1.example", "t");
                        let _ = microvms_core::control::transport::SignedTransport::new(Region);
                        let _ = microvms_core::control::SignedBuildServices::new(Region);
                        let _ = microvms_core::session::http::ReqwestBackend::new("https://x");
                    }
                """,
                "src/alias.rs": """\
                    use microvms_core::control::ControlPlane as Plane;
                    use microvms_core::control::Region;
                    use microvms_core::prelude::*;

                    pub fn plane() -> Plane {
                        Plane::new(Region)
                    }
                """,
                "src/ufcs.rs": """\
                    use microvms_core::prelude::SessionExt;
                    use microvms_core::session::Session;

                    pub fn session() -> Session {
                        <Session as SessionExt>::direct("https://mvm-1.example", "t")
                    }
                """,
            },
        )
        self.assertNotEqual(out.returncode, 0, out.stderr)
        found = disallowed(out.stderr)
        self.assertEqual(
            {path for path, file in found if file == "doors.rs"},
            set(DOOR_METHODS) | set(DOOR_TYPES),
            out.stderr,
        )
        self.assertIn(
            ("microvms_core::prelude::ControlPlaneExt::new", "alias.rs"), found
        )
        self.assertIn(("microvms_core::prelude::SessionExt::direct", "ufcs.rs"), found)

    def test_the_bindings_may_call_core_doors(self):
        # The door bans are the CLI's alone: a binding builds its own plane, sandbox and
        # session, and that's its job.
        for adapter in ("microvms-py", "microvms-js"):
            with self.subTest(adapter=adapter):
                out = self.clippy(
                    adapter,
                    {
                        "src/lib.rs": f"""\
                            {DENY}
                            use microvms_core::control::{{ControlPlane, Region}};
                            use microvms_core::prelude::*;
                            use microvms_core::session::Session;

                            pub fn doors() -> (ControlPlane, Session) {{
                                (ControlPlane::new(Region), Session::direct("https://mvm-1.example", "t"))
                            }}
                        """,
                    },
                )
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertEqual(disallowed(out.stderr), set(), out.stderr)

    def test_no_adapter_source_turns_the_lints_off_outside_its_listed_sites(self):
        roots = {root.resolve() for root in self.roots().values()}
        found = Counter()
        for adapter in RATCHET["REPO"].adapters:
            for path in sorted((ROOT / adapter / "src").rglob("*.rs")):
                rel = path.relative_to(ROOT).as_posix()
                for level, lint, line in lint_levels(path.read_text(encoding="utf-8")):
                    where = f"{rel}:{line}: {level}({lint})"
                    if (
                        level == "deny"
                        and path.resolve() in roots
                        and "disallowed" in lint
                    ):
                        continue
                    self.assertEqual(
                        level,
                        "expect",
                        f"{where} turns the adapter contract off. Move the call below the "
                        "adapter, or make it a reviewed #[expect] listed in LINT_EXCEPTIONS.",
                    )
                    self.assertIn(
                        (rel, lint),
                        LINT_EXCEPTIONS,
                        f"{where} isn't a listed exception. Move the call below the adapter, "
                        "or add the site to LINT_EXCEPTIONS with the record it points at.",
                    )
                    found[(rel, lint)] += 1
        self.assertEqual(dict(found), LINT_EXCEPTIONS)

        drift = json.loads(
            (ROOT / "ratchet" / "drift.json").read_text(encoding="utf-8")
        )
        subprocess_keys = [
            record["key"]
            for record in drift["entries"] + drift["decisions"]
            if record["category"] == "subprocess"
        ]
        # A test-only file's banned types are fakes (the scripted transports), and the ratchet
        # reads no test code, so there's no drift record for them to point at.
        for rel, lint in LINT_EXCEPTIONS:
            if (
                lint == "clippy::disallowed_types"
                and "#![cfg(test)]"
                not in (ROOT / rel).read_text(encoding="utf-8").splitlines()
            ):
                with self.subTest(site=rel):
                    self.assertTrue(
                        any(key.startswith(f"{rel}: ") for key in subprocess_keys),
                        f"{rel} expects a subprocess with no entry or decision in "
                        "ratchet/drift.json",
                    )

    def test_lint_levels_reads_the_level_and_skips_comments(self):
        text = textwrap.dedent("""\
            // #[allow(clippy::disallowed_methods)] in prose
            #![deny(clippy::disallowed_methods, clippy::disallowed_types)]
            #[cfg_attr(test, allow(clippy::disallowed_types))]
            #[expect(
                clippy::disallowed_methods,
                reason = "x"
            )]
            #[warn(clippy::style)]
        """)
        self.assertEqual(
            list(lint_levels(text)),
            [
                ("deny", "clippy::disallowed_methods", 2),
                ("deny", "clippy::disallowed_types", 2),
                ("allow", "clippy::disallowed_types", 3),
                ("expect", "clippy::disallowed_methods", 5),
                ("warn", "clippy::style", 8),
            ],
        )


# The domain's crate-root attribute (ARCH-6), and the app's (ARCH-7). `forbid`, not the
# adapters' `deny`: under `forbid` an inner `#[allow]` or `#[expect]` is itself an error, so
# neither crate needs an exception list for a scan to check against.
FORBID = "#![forbid(clippy::disallowed_methods, clippy::disallowed_types)]"

# Stand-ins for the domain's dependencies whose clock and entropy methods its `clippy.toml`
# bans, with the real items' paths, so the bans resolve without building the real crates (the
# CI job that runs these tests has no registry cache). `random` is behind a feature the domain
# leaves off, which is what a feature switched on elsewhere in the workspace would expose.
DOMAIN_STAND_INS = {
    "jiff": """\
        pub struct Timestamp;
        impl Timestamp {
            pub fn now() -> Timestamp { Timestamp }
        }
        pub struct Zoned;
        impl Zoned {
            pub fn now() -> Zoned { Zoned }
        }
        pub mod tz {
            pub struct TimeZone;
            impl TimeZone {
                pub fn system() -> TimeZone { TimeZone }
            }
        }
    """,
    "x25519-dalek": """\
        pub struct StaticSecret;
        impl StaticSecret {
            pub fn random() -> StaticSecret { StaticSecret }
        }
    """,
}


class DomainLintTests(unittest.TestCase):
    """`microvms-domain`'s `clippy.toml` and crate root refuse I/O, and nothing turns them off.

    Like `AdapterLintTests`, the fault cases run real clippy over a throwaway crate carrying the
    domain's real `clippy.toml`, since a path clippy can't resolve is ignored.
    """

    DOMAIN = ROOT / "microvms-domain"

    def clippy(self, lib):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        deps = []
        for name, text in DOMAIN_STAND_INS.items():
            stand_in = Path(tmp.name) / name
            (stand_in / "src").mkdir(parents=True)
            (stand_in / "Cargo.toml").write_text(
                f'[package]\nname = "{name}"\nversion = "0.0.0"\nedition = "2024"\n'
                "publish = false\n\n[workspace]\n"
            )
            (stand_in / "src" / "lib.rs").write_text(textwrap.dedent(text))
            deps.append(f'{name} = {{ path = "../{name}" }}\n')
        crate = Path(tmp.name) / "microvms-domain"
        (crate / "src").mkdir(parents=True)
        (crate / "Cargo.toml").write_text(
            '[package]\nname = "microvms-domain"\nversion = "0.0.0"\nedition = "2024"\n'
            "publish = false\n\n[workspace]\n\n[dependencies]\n" + "".join(deps)
        )
        shutil.copy(self.DOMAIN / "clippy.toml", crate / "clippy.toml")
        (crate / "src" / "lib.rs").write_text(textwrap.dedent(lib))
        env = {k: v for k, v in os.environ.items() if k != "CLIPPY_CONF_DIR"}
        env["CARGO_TARGET_DIR"] = str(Path(tmp.name) / "target")
        return subprocess.run(
            [
                "cargo",
                "clippy",
                "--offline",
                "--quiet",
                "--manifest-path",
                str(crate / "Cargo.toml"),
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )

    def test_the_domain_root_forbids_the_disallowed_lints(self):
        lib = self.DOMAIN / "src" / "lib.rs"
        self.assertIn(FORBID, lib.read_text(encoding="utf-8").splitlines())

    def test_no_domain_source_names_the_lints_except_the_root(self):
        # `forbid` already makes an inner `allow` or `expect` a compile error. This catches the
        # edit that would make one compile: the root weakened to `deny`, or a `cfg_attr` level.
        lib = (self.DOMAIN / "src" / "lib.rs").resolve()
        for path in sorted((self.DOMAIN / "src").rglob("*.rs")):
            rel = path.relative_to(ROOT).as_posix()
            for level, lint, line in lint_levels(path.read_text(encoding="utf-8")):
                if level == "forbid" and path.resolve() == lib and "disallowed" in lint:
                    continue
                self.fail(
                    f"{rel}:{line}: {level}({lint}). The domain has no exceptions: take the "
                    "input as a parameter and let microvms-core do the I/O."
                )

    def test_the_domain_bans_each_io_route(self):
        config = tomllib.loads((self.DOMAIN / "clippy.toml").read_text())
        paths = {
            key: sorted(item["path"] for item in config[key])
            for key in ("disallowed-types", "disallowed-methods")
        }
        self.assertEqual(
            paths["disallowed-types"],
            [
                "std::fs::DirBuilder",
                "std::fs::File",
                "std::fs::OpenOptions",
                "std::fs::ReadDir",
                "std::net::TcpListener",
                "std::net::TcpStream",
                "std::net::UdpSocket",
                "std::os::unix::net::UnixDatagram",
                "std::os::unix::net::UnixListener",
                "std::os::unix::net::UnixStream",
                "std::process::Command",
                "std::time::Instant",
                "std::time::SystemTime",
            ],
        )
        path_methods = [
            "canonicalize",
            "exists",
            "is_dir",
            "is_file",
            "is_symlink",
            "metadata",
            "read_dir",
            "read_link",
            "symlink_metadata",
            "try_exists",
        ]
        self.assertEqual(
            paths["disallowed-methods"],
            sorted(
                [
                    "jiff::Timestamp::now",
                    "jiff::Zoned::now",
                    "jiff::tz::TimeZone::get",
                    "jiff::tz::TimeZone::system",
                    "jiff::tz::TimeZone::try_system",
                    "jiff::tz::db",
                    "std::env::args",
                    "std::env::args_os",
                    "std::env::current_dir",
                    "std::env::current_exe",
                    "std::env::home_dir",
                    "std::env::set_current_dir",
                    "std::env::temp_dir",
                    "std::env::var",
                    "std::env::var_os",
                    "std::env::vars",
                    "std::env::vars_os",
                    "std::fs::canonicalize",
                    "std::fs::copy",
                    "std::fs::create_dir",
                    "std::fs::create_dir_all",
                    "std::fs::exists",
                    "std::fs::hard_link",
                    "std::fs::metadata",
                    "std::fs::read",
                    "std::fs::read_dir",
                    "std::fs::read_link",
                    "std::fs::read_to_string",
                    "std::fs::remove_dir",
                    "std::fs::remove_dir_all",
                    "std::fs::remove_file",
                    "std::fs::rename",
                    "std::fs::set_permissions",
                    "std::fs::symlink_metadata",
                    "std::fs::write",
                    "std::io::stderr",
                    "std::io::stdin",
                    "std::io::stdout",
                    "std::net::ToSocketAddrs::to_socket_addrs",
                    "std::os::unix::fs::symlink",
                    "std::thread::sleep",
                    "std::time::Instant::now",
                    "std::time::SystemTime::now",
                    "x25519_dalek::EphemeralSecret::random",
                    "x25519_dalek::ReusableSecret::random",
                    "x25519_dalek::StaticSecret::random",
                ]
                + [f"std::path::Path::{method}" for method in path_methods]
            ),
        )
        for item in config["disallowed-types"] + config["disallowed-methods"]:
            self.assertTrue(item.get("reason"), item)

    def test_the_domain_refuses_a_call_from_each_group(self):
        out = self.clippy(
            f"""\
            {FORBID}
            pub fn file() -> bool {{
                std::fs::read("x").is_ok() && std::path::Path::new("x").exists()
            }}
            pub fn subprocess() {{
                let _ = std::process::Command::new("aws");
            }}
            pub fn network() -> bool {{
                std::net::ToSocketAddrs::to_socket_addrs("localhost:80").is_ok()
            }}
            pub fn environment() -> bool {{
                std::env::var("AWS_REGION").is_ok()
            }}
            pub fn stream() {{
                let _ = std::io::stdin();
            }}
            pub fn clock() {{
                let _ = std::time::SystemTime::now();
                let _ = jiff::Timestamp::now();
                let _ = jiff::tz::TimeZone::system();
            }}
            pub fn entropy() {{
                let _ = x25519_dalek::StaticSecret::random();
            }}
            """
        )
        self.assertNotEqual(out.returncode, 0, out.stderr)
        for message in [
            "disallowed method `std::fs::read`",
            "disallowed method `std::path::Path::exists`",
            "disallowed type `std::process::Command`",
            "disallowed method `std::net::ToSocketAddrs::to_socket_addrs`",
            "disallowed method `std::env::var`",
            "disallowed method `std::io::stdin`",
            "disallowed method `std::time::SystemTime::now`",
            "disallowed method `jiff::Timestamp::now`",
            "disallowed method `jiff::tz::TimeZone::system`",
            "disallowed method `x25519_dalek::StaticSecret::random`",
        ]:
            self.assertIn(message, out.stderr)

    def test_an_allow_under_the_domain_root_does_not_compile(self):
        out = self.clippy(
            f"""\
            {FORBID}
            #[allow(clippy::disallowed_methods)]
            pub fn quiet() -> bool {{
                std::fs::read("x").is_ok()
            }}
            """
        )
        self.assertNotEqual(out.returncode, 0, out.stderr)
        self.assertIn(
            "allow(clippy::disallowed_methods) incompatible with previous forbid",
            out.stderr,
        )


# A stand-in for tokio carrying the `net`, `fs` and `process` items the app's `clippy.toml`
# bans, at their real paths. The app's own tokio doesn't enable those features; another member
# can turn them on through unification, and this is what the app would see then.
APP_STAND_INS = {
    "tokio": """\
        pub mod net {
            pub struct TcpStream;
            impl TcpStream {
                pub async fn connect(_addr: &str) -> Result<TcpStream, ()> { Ok(TcpStream) }
            }
            pub async fn lookup_host(_host: &str) -> Result<(), ()> { Ok(()) }
        }
        pub mod fs {
            pub async fn read(_path: &str) -> Result<Vec<u8>, ()> { Ok(Vec::new()) }
        }
        pub mod process {
            pub struct Command;
            impl Command {
                pub fn new(_program: &str) -> Command { Command }
            }
        }
        pub mod io {
            pub struct Stderr;
            pub fn stderr() -> Stderr { Stderr }
            pub fn stdout() -> Stderr { Stderr }
        }
        pub mod signal {
            pub async fn ctrl_c() -> Result<(), ()> { Ok(()) }
        }
    """,
}


class AppLintTests(unittest.TestCase):
    """`microvms-app`'s `clippy.toml` and crate root refuse I/O, and nothing turns them off.

    The same shape as `DomainLintTests`: the fault cases run real clippy over a throwaway crate
    carrying the app's real `clippy.toml`, with a tokio stand-in whose `net`, `fs`, `process`,
    `io` and `signal` items are what feature unification would hand the app. Two of them are
    #283's seeded faults: a `tokio::net::TcpStream::connect` in a use case, and a `Clock` over
    `std::time::Instant`.
    """

    APP = ROOT / "microvms-app"

    def clippy(self, lib):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        deps = []
        for name, text in APP_STAND_INS.items():
            stand_in = Path(tmp.name) / name
            (stand_in / "src").mkdir(parents=True)
            (stand_in / "Cargo.toml").write_text(
                f'[package]\nname = "{name}"\nversion = "0.0.0"\nedition = "2024"\n'
                "publish = false\n\n[workspace]\n"
            )
            (stand_in / "src" / "lib.rs").write_text(textwrap.dedent(text))
            deps.append(f'{name} = {{ path = "../{name}" }}\n')
        crate = Path(tmp.name) / "microvms-app"
        (crate / "src").mkdir(parents=True)
        (crate / "Cargo.toml").write_text(
            '[package]\nname = "microvms-app"\nversion = "0.0.0"\nedition = "2024"\n'
            "publish = false\n\n[workspace]\n\n[dependencies]\n" + "".join(deps)
        )
        shutil.copy(self.APP / "clippy.toml", crate / "clippy.toml")
        (crate / "src" / "lib.rs").write_text(textwrap.dedent(lib))
        env = {k: v for k, v in os.environ.items() if k != "CLIPPY_CONF_DIR"}
        env["CARGO_TARGET_DIR"] = str(Path(tmp.name) / "target")
        return subprocess.run(
            [
                "cargo",
                "clippy",
                "--offline",
                "--quiet",
                "--manifest-path",
                str(crate / "Cargo.toml"),
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )

    def test_the_app_root_forbids_the_disallowed_lints(self):
        lib = self.APP / "src" / "lib.rs"
        self.assertIn(FORBID, lib.read_text(encoding="utf-8").splitlines())

    def test_no_app_source_names_the_lints_except_the_root(self):
        # `forbid` already makes an inner `allow` or `expect` a compile error. This catches the
        # edit that would make one compile: the root weakened to `deny`, or a `cfg_attr` level.
        lib = (self.APP / "src" / "lib.rs").resolve()
        for path in sorted((self.APP / "src").rglob("*.rs")):
            rel = path.relative_to(ROOT).as_posix()
            for level, lint, line in lint_levels(path.read_text(encoding="utf-8")):
                if level == "forbid" and path.resolve() == lib and "disallowed" in lint:
                    continue
                self.fail(
                    f"{rel}:{line}: {level}({lint}). The app has no exceptions: put the I/O "
                    "behind a port and implement it in microvms-edges."
                )

    def test_the_app_bans_each_io_route(self):
        config = tomllib.loads((self.APP / "clippy.toml").read_text())
        paths = {
            key: sorted(item["path"] for item in config[key])
            for key in ("disallowed-types", "disallowed-methods")
        }
        self.assertEqual(
            paths["disallowed-types"],
            sorted(
                [
                    "std::fs::DirBuilder",
                    "std::fs::File",
                    "std::fs::OpenOptions",
                    "std::fs::ReadDir",
                    "std::net::TcpListener",
                    "std::net::TcpStream",
                    "std::net::UdpSocket",
                    "std::os::unix::net::UnixDatagram",
                    "std::os::unix::net::UnixListener",
                    "std::os::unix::net::UnixStream",
                    "std::process::Command",
                    "std::time::Instant",
                    "tokio::fs::DirBuilder",
                    "tokio::fs::File",
                    "tokio::fs::OpenOptions",
                    "tokio::fs::ReadDir",
                    "tokio::net::TcpListener",
                    "tokio::net::TcpSocket",
                    "tokio::net::TcpStream",
                    "tokio::net::UdpSocket",
                    "tokio::net::UnixDatagram",
                    "tokio::net::UnixListener",
                    "tokio::net::UnixStream",
                    "tokio::process::Child",
                    "tokio::process::Command",
                ]
            ),
        )
        fs_functions = [
            "canonicalize",
            "copy",
            "create_dir",
            "create_dir_all",
            "hard_link",
            "metadata",
            "read",
            "read_dir",
            "read_link",
            "read_to_string",
            "remove_dir",
            "remove_dir_all",
            "remove_file",
            "rename",
            "set_permissions",
            "symlink_metadata",
            "write",
        ]
        path_methods = [
            "canonicalize",
            "exists",
            "is_dir",
            "is_file",
            "is_symlink",
            "metadata",
            "read_dir",
            "read_link",
            "symlink_metadata",
            "try_exists",
        ]
        self.assertEqual(
            paths["disallowed-methods"],
            sorted(
                [
                    "std::env::args",
                    "std::env::args_os",
                    "std::env::current_dir",
                    "std::env::current_exe",
                    "std::env::home_dir",
                    "std::env::set_current_dir",
                    "std::env::temp_dir",
                    "std::env::var",
                    "std::env::var_os",
                    "std::env::vars",
                    "std::env::vars_os",
                    "std::fs::exists",
                    "std::io::stderr",
                    "std::io::stdin",
                    "std::io::stdout",
                    "std::net::ToSocketAddrs::to_socket_addrs",
                    "std::os::unix::fs::symlink",
                    "std::thread::sleep",
                    "std::time::Instant::now",
                    "std::time::SystemTime::elapsed",
                    "std::time::SystemTime::now",
                    "tokio::fs::try_exists",
                    "tokio::io::stderr",
                    "tokio::io::stdin",
                    "tokio::io::stdout",
                    "tokio::net::lookup_host",
                    "tokio::signal::ctrl_c",
                    "tokio::signal::unix::signal",
                ]
                + [f"std::fs::{name}" for name in fs_functions]
                + [f"tokio::fs::{name}" for name in fs_functions]
                + [f"std::path::Path::{method}" for method in path_methods]
            ),
        )
        for item in config["disallowed-types"] + config["disallowed-methods"]:
            self.assertTrue(item.get("reason"), item)
            # A tokio item exists only when some member turns its feature on, so each needs
            # `allow-invalid` or the app's own build warns that the path doesn't resolve.
            if item["path"].startswith("tokio::"):
                self.assertTrue(item.get("allow-invalid"), item)

    def test_the_app_refuses_a_call_from_each_group(self):
        out = self.clippy(
            f"""\
            {FORBID}
            pub fn file() -> bool {{
                std::fs::read("x").is_ok() && std::path::Path::new("x").exists()
            }}
            pub fn subprocess() {{
                let _ = std::process::Command::new("aws");
                let _ = tokio::process::Command::new("gh");
            }}
            pub fn network() -> bool {{
                std::net::ToSocketAddrs::to_socket_addrs("localhost:80").is_ok()
            }}
            pub async fn tokio_io() {{
                let _ = tokio::fs::read("x").await;
                let _ = tokio::net::lookup_host("localhost:80").await;
            }}
            pub fn environment() -> bool {{
                std::env::var("AWS_REGION").is_ok()
            }}
            pub fn stream() {{
                let _ = std::io::stdin();
            }}
            pub fn wall_clock() {{
                let _ = std::time::SystemTime::now();
            }}
            """
        )
        self.assertNotEqual(out.returncode, 0, out.stderr)
        for message in [
            "disallowed method `std::fs::read`",
            "disallowed method `std::path::Path::exists`",
            "disallowed type `std::process::Command`",
            "disallowed type `tokio::process::Command`",
            "disallowed method `std::net::ToSocketAddrs::to_socket_addrs`",
            "disallowed method `tokio::fs::read`",
            "disallowed method `tokio::net::lookup_host`",
            "disallowed method `std::env::var`",
            "disallowed method `std::io::stdin`",
            "disallowed method `std::time::SystemTime::now`",
        ]:
            self.assertIn(message, out.stderr)

    def test_a_tokio_connect_in_a_use_case_fails_clippy(self):
        # #283's seeded fault: a use case that dials the network itself instead of through its
        # transport or backend port.
        out = self.clippy(
            f"""\
            {FORBID}
            pub async fn health(endpoint: &str) -> bool {{
                tokio::net::TcpStream::connect(endpoint).await.is_ok()
            }}
            """
        )
        self.assertNotEqual(out.returncode, 0, out.stderr)
        self.assertIn("disallowed type `tokio::net::TcpStream`", out.stderr)

    def test_a_clock_over_std_instant_fails_clippy(self):
        # #283's seeded fault: the control plane's old clock, which a simulator can't move.
        out = self.clippy(
            f"""\
            {FORBID}
            pub trait Clock {{
                fn elapsed(&self) -> std::time::Duration;
            }}
            pub struct StdClock {{
                base: std::time::Instant,
            }}
            impl StdClock {{
                pub fn new() -> Self {{
                    Self {{ base: std::time::Instant::now() }}
                }}
            }}
            impl Clock for StdClock {{
                fn elapsed(&self) -> std::time::Duration {{
                    self.base.elapsed()
                }}
            }}
            """
        )
        self.assertNotEqual(out.returncode, 0, out.stderr)
        self.assertIn("disallowed type `std::time::Instant`", out.stderr)
        self.assertIn("disallowed method `std::time::Instant::now`", out.stderr)

    def test_a_clock_read_or_a_stream_that_skips_its_port_fails_clippy(self):
        # The routes past `SystemTime::now` and `std::io`: a wall reading from the epoch, a
        # sleep the simulator can't see, and tokio's streams and signals, which the CLI's and
        # agentd's features hand the app through unification.
        out = self.clippy(
            f"""\
            {FORBID}
            pub fn unix_now() -> std::time::Duration {{
                std::time::UNIX_EPOCH.elapsed().unwrap_or_default()
            }}
            pub fn pause() {{
                std::thread::sleep(std::time::Duration::from_millis(1));
            }}
            pub fn warn() {{
                let _ = tokio::io::stderr();
            }}
            pub async fn interrupted() -> bool {{
                tokio::signal::ctrl_c().await.is_ok()
            }}
            """
        )
        self.assertNotEqual(out.returncode, 0, out.stderr)
        for message in [
            "disallowed method `std::time::SystemTime::elapsed`",
            "disallowed method `std::thread::sleep`",
            "disallowed method `tokio::io::stderr`",
            "disallowed method `tokio::signal::ctrl_c`",
        ]:
            self.assertIn(message, out.stderr)

    def test_an_allow_under_the_app_root_does_not_compile(self):
        out = self.clippy(
            f"""\
            {FORBID}
            #[allow(clippy::disallowed_types)]
            pub fn quiet() {{
                let _ = std::process::Command::new("aws");
            }}
            """
        )
        self.assertNotEqual(out.returncode, 0, out.stderr)
        self.assertIn(
            "allow(clippy::disallowed_types) incompatible with previous forbid",
            out.stderr,
        )


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout


def git_repo(test):
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    root = Path(tmp.name)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "test")
    git(root, "config", "commit.gpgsign", "false")
    return root


def commit(root, message, drift=None, date="2026-09-25T12:00:00+00:00"):
    if drift is not None:
        (root / "ratchet").mkdir(exist_ok=True)
        (root / "ratchet" / "drift.json").write_text(json.dumps(drift))
    else:
        (root / "README").write_text(message)
    git(root, "add", "-A")
    subprocess.run(
        ["git", "-C", str(root), "commit", "-q", "-m", message],
        check=True,
        env={**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date},
    )
    return git(root, "rev-parse", "HEAD").strip()


def ratchet_script(root, collected=None):
    """A stand-in `scripts/ratchet.py` holding only the `COLLECTED` a commit's history reads."""
    (root / "scripts").mkdir(exist_ok=True)
    collected = tuple(RATCHET["COLLECTED"] if collected is None else collected)
    (root / "scripts" / "ratchet.py").write_text(f"COLLECTED = {collected!r}\n")


def drift_file(entries):
    return {
        "version": 1,
        "enforced": [],
        "entries": [{"category": c, "key": k, "issue": i} for c, k, i in entries],
        "decisions": [],
    }


class BaseTests(unittest.TestCase):
    def test_a_base_without_the_file_is_the_bootstrap(self):
        root = git_repo(self)
        commit(root, "first")
        self.assertIsNone(read_base(root, "HEAD"))

    def test_a_base_with_the_file_is_parsed(self):
        root = git_repo(self)
        commit(root, "baseline", drift_file(BASELINE))
        base = read_base(root, "HEAD")
        self.assertEqual(len(base["entries"]), len(BASELINE))

    def test_the_base_sets_are_read_from_the_ref(self):
        root = git_repo(self)
        (root / "arch").mkdir()
        (root / "arch" / "placement.toml").write_text('[a]\nnormal = ["b"]\n')
        commit(root, "sets")
        self.assertEqual(
            read_base_sets(root, "HEAD"), {"a": {"normal": {"b"}, "build": set()}}
        )

    def test_a_base_without_sets_has_none(self):
        root = git_repo(self)
        commit(root, "first")
        self.assertIsNone(read_base_sets(root, "HEAD"))

    def test_the_base_collected_categories_are_read_from_its_script(self):
        root = git_repo(self)
        (root / "scripts").mkdir()
        (root / "scripts" / "ratchet.py").write_text(
            'COLLECTED = ("placement", "subprocess")\nNOT_COLLECTED = {}\n'
        )
        commit(root, "script")
        self.assertEqual(read_base_collected(root, "HEAD"), ("placement", "subprocess"))

    def test_a_base_script_without_a_collected_tuple_is_an_error(self):
        # A parser that finds nothing can't read as "the base collected nothing", which would
        # skip rule 3 for every category.
        root = git_repo(self)
        (root / "scripts").mkdir()
        (root / "scripts" / "ratchet.py").write_text("CATEGORIES = ()\n")
        commit(root, "script")
        with self.assertRaisesRegex(SystemExit, "no COLLECTED"):
            read_base_collected(root, "HEAD")
        (root / "scripts" / "ratchet.py").unlink()
        commit(root, "no script")
        with self.assertRaisesRegex(SystemExit, "no scripts/ratchet.py"):
            read_base_collected(root, "HEAD")

    def test_an_unknown_base_ref_is_an_error_rather_than_a_bootstrap(self):
        root = git_repo(self)
        commit(root, "first")
        with self.assertRaisesRegex(SystemExit, "no-such-ref"):
            read_base(root, "no-such-ref")


class HistoryTests(unittest.TestCase):
    def test_each_commit_that_changed_the_file_is_a_point(self):
        root = git_repo(self)
        ratchet_script(root)
        commit(root, "before", date="2026-09-01T00:00:00+00:00")
        first = commit(
            root, "baseline", drift_file(BASELINE), date="2026-09-02T00:00:00+00:00"
        )
        commit(root, "unrelated", date="2026-09-03T00:00:00+00:00")
        second = commit(
            root, "fix tar", drift_file(BASELINE[1:]), date="2026-09-04T00:00:00+00:00"
        )
        points = HISTORY["history"](root)
        self.assertEqual([p["sha"] for p in points], [first, second])
        self.assertEqual(
            datetime.fromisoformat(points[0]["date"]),
            datetime.fromisoformat("2026-09-02T00:00:00+00:00"),
        )
        self.assertEqual(points[0]["counts"]["placement"], 1)
        self.assertEqual(points[1]["counts"]["placement"], 0)
        self.assertEqual(points[1]["total"], len(BASELINE) - 1)

    def test_an_uncommitted_change_is_the_last_point(self):
        root = git_repo(self)
        ratchet_script(root)
        commit(root, "baseline", drift_file(BASELINE))
        (root / "ratchet" / "drift.json").write_text(
            json.dumps(drift_file(BASELINE[1:]))
        )
        points = HISTORY["history"](root)
        self.assertEqual([p["sha"] for p in points][1:], [None])
        self.assertEqual(points[-1]["total"], len(BASELINE) - 1)

    def test_a_category_a_commit_did_not_collect_is_null_there(self):
        # A zero would chart a category nobody counted yet as measured and clean, so a point
        # carries a count only for what that commit's own ratchet collected.
        root = git_repo(self)
        ratchet_script(root, ("placement", "subprocess", "port-impl"))
        before = [
            e for e in BASELINE if e[0] in ("placement", "subprocess", "port-impl")
        ]
        commit(root, "three categories", drift_file(before))
        ratchet_script(root)
        commit(root, "every category", drift_file(BASELINE))
        (root / "ratchet" / "drift.json").write_text(
            json.dumps(drift_file(BASELINE[1:]))
        )
        first, second, working = HISTORY["history"](root)
        self.assertIsNone(first["counts"]["adapter-logic"])
        self.assertIsNone(first["counts"]["parity-gap"])
        self.assertEqual(first["counts"]["placement"], 1)
        self.assertEqual(second["counts"]["parity-gap"], 1)
        self.assertEqual(working["counts"]["adapter-logic"], 1)
        self.assertEqual(working["counts"]["placement"], 0)

    def test_untraced_is_null_before_the_commit_that_collects_it(self):
        # #295's first PR adds every untraced key as an entry at once. A zero before it would
        # chart a jump from a clean count to the whole backlog, when nobody had counted it yet.
        root = git_repo(self)
        before = tuple(c for c in RATCHET["COLLECTED"] if c != "untraced")
        ratchet_script(root, before)
        commit(root, "before", drift_file([e for e in BASELINE if e[0] != "untraced"]))
        ratchet_script(root)
        commit(root, "collects untraced", drift_file(BASELINE))
        first, second = HISTORY["history"](root)
        self.assertIsNone(first["counts"]["untraced"])
        self.assertEqual(second["counts"]["untraced"], 1)
        self.assertEqual(second["total"], len(BASELINE))


if __name__ == "__main__":
    unittest.main()
