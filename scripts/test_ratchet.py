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

    def scope(self, adapters=None, non_shipping=()):
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
                "new drift: [placement] microvms-cli -> globset. Move it below the adapter, "
                "or add a decision with its reason."
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

    def test_rule_3_is_skipped_when_the_base_has_no_file(self):
        # The bootstrap: the PR that creates ratchet/drift.json has no base copy to compare with.
        self.assertEqual(compare(BASELINE_FOUND, ratchet(BASELINE), None, "main"), [])

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
        with self.assertRaisesRegex(SystemExit, "adapter-logic is not collected yet"):
            ratchet([*BASELINE, ("adapter-logic", "x", 273)])


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
        rows = summary(ratchet(BASELINE), ratchet(BASELINE))
        self.assertEqual(rows["adapter-logic"]["status"], "not collected")
        self.assertEqual(rows["parity-gap"]["status"], "not collected")
        self.assertIsNone(rows["parity-gap"]["entries"])
        self.assertEqual(rows["placement"]["entries"], 1)
        text = render_text(rows)
        line = next(line for line in text.splitlines() if line.startswith("parity-gap"))
        self.assertIn("not collected", line)
        self.assertNotIn("0", line)

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


class SentinelTests(unittest.TestCase):
    def test_the_checked_in_sentinel_matches_its_expectation(self):
        self.assertEqual(sentinel(RATCHET["SENTINEL"]), [])

    def test_a_collector_that_finds_nothing_fails(self):
        ws = Workspace(self).crate("adapter").placement("[adapter]\nnormal = []\n")
        empty = ws.scope()
        failures = sentinel(empty)
        for category in ("placement", "subprocess", "port-impl"):
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
        shutil.copy(ROOT / "arch" / "placement.toml", ws.root / "placement.toml")
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
            "new drift: [placement] microvms-py -> reqwest. Move it below the adapter, "
            "or add a decision with its reason.",
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
            "new drift: [placement] microvms-py -> globset. Move it below the adapter, "
            "or add a decision with its reason.",
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
            f"new drift: [subprocess] {key}. Move it below the adapter, "
            "or add a decision with its reason.",
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
    # `doctor`'s `terraform output`, a subprocess decision.
    ("microvms-cli/src/commands/doctor.rs", "clippy::disallowed_types"): 1,
    # Each binding's name store, the one place it composes core's process lookup.
    ("microvms-py/src/names.rs", "clippy::disallowed_methods"): 1,
    ("microvms-js/src/names.rs", "clippy::disallowed_methods"): 1,
}

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
    """Each driving adapter's `clippy.toml` refuses a subprocess and a direct environment read.

    The fault cases copy the adapter's real `clippy.toml` into a throwaway crate and run real
    clippy over it, because the rule's paths only mean something to the toolchain: a path
    clippy can't resolve is silently ignored. Cargo runs from the repository root so rustup
    picks the toolchain `rust-toolchain.toml` pins, clippy included.
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

        The crate depends on a stand-in `microvms-core` whose `env::process` has the real
        one's path, so the ban on calling it resolves the way it does in the workspace.
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        core = Path(tmp.name) / "microvms-core"
        (core / "src").mkdir(parents=True)
        (core / "Cargo.toml").write_text(
            '[package]\nname = "microvms-core"\nversion = "0.0.0"\nedition = "2024"\n'
            "publish = false\n\n[workspace]\n"
        )
        (core / "src" / "lib.rs").write_text(
            "pub mod env {\n"
            "    pub fn process(name: &str) -> Option<String> {\n"
            "        std::env::var(name).ok()\n"
            "    }\n"
            "}\n"
        )
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

    def test_every_adapter_root_denies_the_disallowed_lints(self):
        for adapter, root in self.roots().items():
            with self.subTest(adapter=adapter):
                lines = root.read_text(encoding="utf-8").splitlines()
                self.assertTrue(DENY in lines, f"{root} lacks {DENY}")

    def test_every_adapter_bans_both_commands_and_every_env_read(self):
        for adapter in RATCHET["REPO"].adapters:
            with self.subTest(adapter=adapter):
                config = tomllib.loads((ROOT / adapter / "clippy.toml").read_text())
                self.assertEqual(
                    sorted(t["path"] for t in config["disallowed-types"]),
                    ["std::process::Command", "tokio::process::Command"],
                )
                self.assertEqual(
                    sorted(m["path"] for m in config["disallowed-methods"]),
                    [
                        "microvms_core::env::process",
                        "std::env::var",
                        "std::env::var_os",
                        "std::env::vars",
                        "std::env::vars_os",
                    ],
                )
                for item in config["disallowed-types"] + config["disallowed-methods"]:
                    self.assertTrue(item.get("reason"), item)

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
        for rel, lint in LINT_EXCEPTIONS:
            if lint == "clippy::disallowed_types":
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

    def test_an_unknown_base_ref_is_an_error_rather_than_a_bootstrap(self):
        root = git_repo(self)
        commit(root, "first")
        with self.assertRaisesRegex(SystemExit, "no-such-ref"):
            read_base(root, "no-such-ref")


class HistoryTests(unittest.TestCase):
    def test_each_commit_that_changed_the_file_is_a_point(self):
        root = git_repo(self)
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
        self.assertEqual(points[1]["total"], 2)

    def test_an_uncommitted_change_is_the_last_point(self):
        root = git_repo(self)
        commit(root, "baseline", drift_file(BASELINE))
        (root / "ratchet" / "drift.json").write_text(
            json.dumps(drift_file(BASELINE[1:]))
        )
        points = HISTORY["history"](root)
        self.assertEqual([p["sha"] for p in points][1:], [None])
        self.assertEqual(points[-1]["total"], 2)


if __name__ == "__main__":
    unittest.main()
