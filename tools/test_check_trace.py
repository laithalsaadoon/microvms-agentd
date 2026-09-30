# SPDX-License-Identifier: Apache-2.0
"""Tests for `tools/check-trace.py`: which mentions of a key count for the test and fuzz layers.

The rule (#276) is that a test counts for a key only when the key names it: the test function's
name, its own doc comment or docstring, a pytest marker, or a Node test's title. A comment
elsewhere in the file, a module doc, or an assertion message doesn't count, because a file that
mentioned a key and tested nothing would score the same.

Each case builds a throwaway tree under a temporary directory and runs the real collector over
it, ast-grep included, because a collector tested against a mocked parser proves the mock.
ast-grep comes from `mise.toml`, so run this through `mise run trace:check`.
"""

import os
import runpy
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCRIPT = HERE / "check-trace.py"
TRACE = runpy.run_path(str(SCRIPT))

Patterns = TRACE["Patterns"]
collect = TRACE["collect"]
gaps = TRACE["gaps"]
enumerator_floors = TRACE["enumerator_floors"]
layer_floors = TRACE["layer_floors"]
load_traced = TRACE["load_traced"]
Traced = TRACE["Traced"]

# Keys from the real specs' prefixes, so the key and name patterns are the ones the tree gets.
PATTERNS = Patterns(
    {key: "" for key in ("IMAGE-5", "IMAGE-12", "CLI-7", "AGENTD-7", "BIND-6")}
)

# Each fixture file sits where the collector looks: a Rust integration test, a source file's
# test module, a binding test in each language, and a fuzz harness.
RUST_TEST = "crates/microvms-cli/tests/case.rs"
RUST_SOURCE = "crates/microvms-cli/src/case.rs"
PY_TEST = "bindings/microvms-py/tests/test_case.py"
JS_TEST = "bindings/microvms-js/__test__/case.mjs"
FUZZ = "crates/microvms-core/tests/case_fuzz.rs"


def traced(*keys: str) -> dict:
    """A traced table listing `keys`, each with no waiver."""
    return {key: Traced("#276", {}, "verify/spec/traced/test.toml") for key in keys}


class Tree:
    """A throwaway tree holding the given files at their paths."""

    def __init__(self, test: unittest.TestCase, files: dict[str, str]):
        directory = tempfile.TemporaryDirectory()
        test.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for path, text in files.items():
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(textwrap.dedent(text).lstrip())

    def layers(self, key: str) -> dict[str, set[str]]:
        return collect(PATTERNS, self.root).get(key, {})


class TestLayerCountsOnlyNamedTests(unittest.TestCase):
    """A key that only a comment, a module doc or a literal carries has no test layer."""

    def assert_untested(self, path: str, text: str, key: str = "IMAGE-5") -> None:
        tree = Tree(self, {path: text})
        self.assertEqual(tree.layers(key).get("test", set()), set())
        found = collect(PATTERNS, tree.root)
        self.assertIn(f"{key} has no test layer", gaps(found, traced(key)))

    def assert_tested(self, path: str, text: str, key: str = "IMAGE-5") -> None:
        tree = Tree(self, {path: text})
        self.assertEqual(tree.layers(key).get("test"), {path})
        found = collect(PATTERNS, tree.root)
        self.assertNotIn(f"{key} has no test layer", gaps(found, traced(key)))

    def test_a_rust_line_comment_does_not_count(self):
        self.assert_untested(
            RUST_TEST,
            """
            // IMAGE-5: the refusals below are core's.
            #[test]
            fn a_refusal_is_invalid_argument() {}
            """,
        )

    def test_a_rust_module_doc_does_not_count(self):
        self.assert_untested(
            RUST_TEST,
            """
            //! The binding's pass-through (IMAGE-5).

            #[test]
            fn a_refusal_is_invalid_argument() {}
            """,
        )

    def test_a_rust_assertion_message_does_not_count(self):
        self.assert_untested(
            RUST_TEST,
            """
            #[test]
            fn a_refusal_is_invalid_argument() {
                assert!(true, "IMAGE-5 holds");
            }
            """,
        )

    def test_a_doc_on_a_function_that_is_not_a_test_does_not_count(self):
        self.assert_untested(
            RUST_TEST,
            """
            /// **IMAGE-5.** A helper, not a test.
            fn helper() {}

            #[test]
            fn a_refusal_is_invalid_argument() { helper() }
            """,
        )

    def test_a_comment_in_a_source_files_test_module_does_not_count(self):
        self.assert_untested(
            RUST_SOURCE,
            """
            pub fn wrap() {}

            #[cfg(test)]
            mod tests {
                // IMAGE-5
                #[test]
                fn wraps() { super::wrap() }
            }
            """,
        )

    def test_a_python_module_docstring_does_not_count(self):
        self.assert_untested(
            PY_TEST,
            '''
            """`wrap_dockerfile` (IMAGE-5): thin, and core's refusals intact."""


            def test_a_refusal_is_invalid_argument() -> None:
                assert True, "IMAGE-5"
            ''',
        )

    def test_a_node_header_comment_does_not_count(self):
        self.assert_untested(
            JS_TEST,
            """
            // `wrapDockerfile` (IMAGE-5): thin, and core's refusals intact.
            import { test } from 'node:test';

            test('a refusal is invalid argument', () => {
              assert.ok(true, 'IMAGE-5');
            });
            """,
        )

    def test_a_node_assertion_that_calls_a_test_method_does_not_count(self):
        for body in (
            "assert.ok(!cite.test('IMAGE-5 is refused'));",
            "size.describe('IMAGE-5');",
            # A callback doesn't make a method call a test: a validator's `.test(name, fn)`.
            "schema.test('IMAGE-5 shape', (value) => value.ok);",
        ):
            with self.subTest(body=body):
                self.assert_untested(
                    JS_TEST, f"test('a refusal', () => {{\n  {body}\n}});\n"
                )

    def test_a_node_test_that_never_runs_does_not_count(self):
        for text in (
            "test.todo('IMAGE-5: the binding keeps core refusals');",
            "test.skip('IMAGE-5: a refusal', () => {});",
            "it('IMAGE-5: a refusal', { skip: true }, () => {});",
            "test('IMAGE-5: a refusal', { todo: 'not written' }, () => {});",
            "describe.skip('wrap', () => { test('IMAGE-5: a refusal', () => {}); });",
            "test('IMAGE-5: a refusal');",
        ):
            with self.subTest(text=text):
                self.assert_untested(JS_TEST, text + "\n")

    def test_a_key_inside_a_rust_name_does_not_count(self):
        # The prefixes are words, so only a name that starts with the key names it.
        self.assert_untested(RUST_TEST, "#[test]\nfn builds_image_5_times() {}\n")

    def test_a_rust_inner_block_doc_does_not_count(self):
        self.assert_untested(
            RUST_TEST,
            "/*! IMAGE-5 */\n\n#[test]\nfn a_refusal_is_invalid_argument() {}\n",
        )

    def test_a_rust_test_that_never_runs_does_not_count(self):
        for attribute in (
            '#[ignore = "not written"]',
            "#[cfg(any())]",
            '#[cfg(feature = "x")]',
        ):
            with self.subTest(attribute=attribute):
                self.assert_untested(
                    RUST_TEST,
                    f"/// **IMAGE-5.**\n#[test]\n{attribute}\nfn wraps() {{}}\n",
                )

    def test_a_python_literal_in_a_decorator_does_not_count(self):
        for decorator in (
            '@pytest.mark.parametrize("note", ["IMAGE-5 is not what this checks"])',
            '@pytest.mark.skipif(False, reason="IMAGE-5 is not wired here")',
            '@pytest.mark.xfail(reason="IMAGE-5 later")',
        ):
            with self.subTest(decorator=decorator):
                self.assert_untested(
                    PY_TEST,
                    f"import pytest\n\n\n{decorator}\ndef test_wraps(note=None) -> None:\n    pass\n",
                )

    def test_a_python_test_that_never_runs_does_not_count(self):
        body = '''def test_wraps() -> None:\n    """IMAGE-5: a refusal."""\n'''
        for text in (
            "import pytest\n\n\n@pytest.mark.skip(reason='later')\n" + body,
            "import pytest\n\npytestmark = [pytest.mark.skip]\n\n\n" + body,
            "import pytest\n\npytestmark = (pytest.mark.skip,)\n\n\n" + body,
            # pytest collects neither a class that isn't `Test*` nor a nested def.
            "class Wrap:\n    "
            + body.replace("\n", "\n    ").replace("() ", "(self) "),
            "def helper() -> None:\n    " + body.replace("\n", "\n    "),
        ):
            with self.subTest(text=text):
                self.assert_untested(PY_TEST, text)

    def test_a_python_test_that_does_not_parse_is_reported_by_path(self):
        tree = Tree(self, {PY_TEST: "def test_a(:\n"})
        with self.assertRaises(SystemExit) as raised:
            collect(PATTERNS, tree.root)
        self.assertIn(f"trace: {PY_TEST} doesn't parse", str(raised.exception))

    def test_a_rust_test_functions_doc_counts(self):
        self.assert_tested(
            RUST_TEST,
            """
            /// **IMAGE-5.** Core's refusals, as invalid-argument errors.
            #[test]
            fn a_refusal_is_invalid_argument() {}
            """,
        )

    def test_a_doc_between_the_attributes_counts(self):
        self.assert_tested(
            RUST_SOURCE,
            """
            #[cfg(test)]
            mod tests {
                #[tokio::test(flavor = "multi_thread")]
                /// IMAGE-5
                #[allow(unused)]
                async fn wraps() {}
            }
            """,
        )

    def test_a_rust_test_functions_name_counts(self):
        self.assert_tested(
            RUST_TEST,
            """
            #[test]
            fn image_5_a_refusal_is_invalid_argument() {}
            """,
        )

    def test_a_test_inside_proptest_counts(self):
        self.assert_tested(
            RUST_SOURCE,
            """
            #[cfg(test)]
            mod tests {
                proptest! {
                    /// **IMAGE-5.** Any text either wraps or is refused.
                    #[test]
                    fn wraps_or_refuses(text in ".*") { prop_assert!(true); }
                }
            }
            """,
        )

    def test_a_pytest_functions_name_counts(self):
        self.assert_tested(
            PY_TEST,
            """
            def test_image_5_a_refusal_is_invalid_argument() -> None:
                pass
            """,
        )

    def test_a_pytest_marker_counts(self):
        self.assert_tested(
            PY_TEST,
            """
            import pytest


            @pytest.mark.req("IMAGE-5")
            def test_a_refusal_is_invalid_argument() -> None:
                pass
            """,
        )

    def test_a_pytest_functions_docstring_counts(self):
        self.assert_tested(
            PY_TEST,
            '''
            def test_a_refusal_is_invalid_argument() -> None:
                """IMAGE-5: core's refusals, as `InvalidArgError`."""
            ''',
        )

    def test_a_node_test_title_counts(self):
        self.assert_tested(
            JS_TEST,
            """
            import { test } from 'node:test';

            test('IMAGE-5: a refusal is invalid argument', () => {});
            """,
        )

    def test_a_node_only_form_and_a_template_title_count(self):
        self.assert_tested(
            JS_TEST,
            """
            for (const cause of ['a', 'b']) {
              test.only(`IMAGE-5: core refuses ${cause}`, () => {});
            }
            """,
        )

    def test_a_rust_block_doc_and_a_doc_attribute_count(self):
        for doc in ("/** IMAGE-5 */", '#[doc = "IMAGE-5"]'):
            with self.subTest(doc=doc):
                self.assert_tested(
                    RUST_TEST,
                    f"{doc}\n#[test]\nfn a_refusal_is_invalid_argument() {{}}\n",
                )

    def test_an_ignored_test_in_a_live_file_counts(self):
        # The live tier runs each `live_*.rs` file with `--ignored`.
        self.assert_tested(
            "crates/microvms-core/tests/live_case.rs",
            """
            /// **IMAGE-5.**
            #[tokio::test]
            #[ignore = "billable; run by mise run live"]
            async fn wraps_live() {}
            """,
        )

    def test_a_platform_cfg_on_a_test_counts(self):
        self.assert_tested(
            RUST_TEST,
            """
            /// **IMAGE-5.**
            #[cfg(not(unix))]
            #[test]
            fn wraps() {}
            """,
        )

    def test_a_pytest_test_skipped_on_some_platform_counts(self):
        self.assert_tested(
            PY_TEST,
            '''
            import os

            import pytest


            class TestWrap:
                @pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
                def test_wraps(self) -> None:
                    """IMAGE-5: the record is owner-only."""
            ''',
        )

    def test_a_node_test_skipped_on_some_platform_counts(self):
        self.assert_tested(
            JS_TEST,
            """
            test('IMAGE-5: owner-only', { skip: process.platform === 'win32' }, () => {});
            """,
        )


class FuzzLayerCountsOnlyNamedHarnesses(unittest.TestCase):
    """A fuzz file counts for a key only where the key names the function calling bolero."""

    def fuzz(
        self,
        doc: str = "",
        name: str = "start_resolution",
        *,
        call: str = "bolero::check!()",
        head: str = "",
        attribute: str = "#[test]",
        tail: str = "",
    ) -> dict[str, set[str]]:
        text = (
            f"{head}//! The fuzz harness for AGENTD-7.\n\nfn helper() {{}}\n\n{doc}\n"
            f"{attribute}\nfn {name}() {{\n    {call}.for_each(|bytes: &[u8]| helper());\n}}\n"
            f"\n{tail}"
        )
        return Tree(self, {FUZZ: text}).layers("AGENTD-7")

    def test_a_fuzz_files_module_doc_does_not_count(self):
        self.assertEqual(self.fuzz().get("fuzz", set()), set())

    def test_a_harness_functions_doc_counts(self):
        self.assertEqual(self.fuzz("/// **AGENTD-7.**").get("fuzz"), {FUZZ})

    def test_a_harness_functions_name_counts(self):
        self.assertEqual(
            self.fuzz(name="agentd_7_start_resolution").get("fuzz"), {FUZZ}
        )

    def test_a_harness_does_not_also_count_for_the_test_layer(self):
        self.assertEqual(self.fuzz("/// **AGENTD-7.**").get("test", set()), set())

    def test_a_fuzz_files_other_test_counts_for_the_test_layer(self):
        layers = self.fuzz(
            "/// **AGENTD-7.**",
            tail="/// **AGENTD-7.** The table itself.\n#[test]\nfn the_table() {}\n",
        )
        self.assertEqual(layers.get("fuzz"), {FUZZ})
        self.assertEqual(layers.get("test"), {FUZZ})

    def test_every_spelling_of_the_bolero_call_is_a_harness(self):
        for call, head in (
            ("::bolero::check!()", ""),
            ("check!()", "use bolero::check;\n"),
            ("check!()", "use bolero::{check, TypeGenerator};\n"),
        ):
            with self.subTest(call=call, head=head):
                layers = self.fuzz("/// **AGENTD-7.**", call=call, head=head)
                self.assertEqual(layers.get("fuzz"), {FUZZ})
                self.assertEqual(layers.get("test", set()), set())

    def test_a_bare_check_without_bolero_is_refused(self):
        with self.assertRaises(SystemExit) as raised:
            self.fuzz("/// **AGENTD-7.**", call="check!()")
        self.assertIn(
            "calls a bare check! without importing bolero's", str(raised.exception)
        )

    def test_an_ast_grep_suppression_comment_is_refused(self):
        with self.assertRaises(SystemExit) as raised:
            self.fuzz("/// **AGENTD-7.**\n// ast-grep-ignore")
        self.assertIn("has an ast-grep-ignore comment", str(raised.exception))

    def test_a_harness_that_is_not_a_test_does_not_count(self):
        layers = self.fuzz("/// **AGENTD-7.**", attribute="")
        self.assertEqual(layers.get("fuzz", set()), set())

    def test_a_doc_above_a_top_level_fuzz_target_counts(self):
        tree = Tree(
            self,
            {
                "crates/agentd/fuzz/fuzz_targets/case.rs": """
                    #![no_main]
                    /// **AGENTD-7.**
                    fuzz_target!(|data: &[u8]| {
                        let _ = data;
                    });
                    """
            },
        )
        self.assertEqual(
            tree.layers("AGENTD-7").get("fuzz"),
            {"crates/agentd/fuzz/fuzz_targets/case.rs"},
        )


# The threat table's fixture: `docs/TRUST.md` holding a section with the given rows, and a
# release row naming the sentinel key, since every table must carry it.
SENTINEL_ROW = (
    "| A replaced release asset | `BIND-18` | `crates/microvms-edges/src/release.rs::a_bad_asset_is_refused` "
    "| guarded |"
)
SENTINEL_TEST = "/// BIND-18\n#[test]\nfn a_bad_asset_is_refused() {}\n"
THREAT_SENTENCES = {key: "" for key in ("AGENTD-17", "AGENTD-18", "BIND-18", "BIND-21")}


class ThreatTable(unittest.TestCase):
    """Each threat row's key is in a spec and its guard is a running test that names it."""

    def check(
        self,
        rows: list[str],
        files: dict[str, str] | None = None,
        *,
        sentinel: bool = True,
        section: str | None = None,
    ) -> list[str]:
        if section is None:
            table = [
                "| Threat | Requirement | Guard | Status |",
                "|---|---|---|---|",
                *rows,
                *([SENTINEL_ROW] if sentinel else []),
            ]
            section = (
                "## Threats and the tests that guard them\n\nProse.\n\n"
                + "\n".join(table)
            )
        trust = f"# Trust\n\n## The five defenses that remain\n\nText.\n\n{section}\n\n## Next\n"
        tree = Tree(
            self,
            {
                "docs/TRUST.md": trust,
                "crates/microvms-edges/src/lib.rs": "mod release;\n",
                "crates/microvms-edges/src/release.rs": SENTINEL_TEST,
                **(files or {}),
            },
        )
        _, problems = TRACE["check_threats"](PATTERNS, THREAT_SENTENCES, tree.root)
        return problems

    def row(self, key: str = "`AGENTD-17`", guard: str | None = None, status="guarded"):
        guard = guard or "`crates/agentd/tests/relay.rs::the_wrong_host_key_is_refused`"
        return f"| A caller without the host key | {key} | {guard} | {status} |"

    def relay(self, doc: str = "/// AGENTD-17", attribute: str = "#[test]") -> dict:
        return {
            "crates/agentd/tests/relay.rs": f"{doc}\n{attribute}\nfn the_wrong_host_key_is_refused() {{}}\n"
        }

    def assert_reported(self, problems: list[str], fragment: str) -> None:
        self.assertTrue(
            any(fragment in problem for problem in problems),
            f"no problem mentions {fragment!r}: {problems}",
        )

    def test_a_table_whose_keys_and_guards_resolve_passes(self):
        self.assertEqual(self.check([self.row()], self.relay()), [])

    def test_a_guard_named_by_its_name_and_a_row_with_two_keys_pass(self):
        files = {
            "crates/agentd/tests/relay.rs": "#[test]\nfn agentd_18_nothing_is_relayed() {}\n"
        }
        row = self.row(
            "`AGENTD-17`, `AGENTD-18`",
            "`crates/agentd/tests/relay.rs::agentd_18_nothing_is_relayed`",
        )
        self.assertEqual(self.check([row], files), [])

    def test_a_key_neither_spec_defines_is_reported(self):
        problems = self.check([self.row("`BIND-99`")], self.relay("/// BIND-99"))
        self.assert_reported(problems, "BIND-99 is not defined in either spec")

    def test_a_guard_whose_file_does_not_exist_is_reported(self):
        problems = self.check([self.row()])
        self.assert_reported(
            problems, "names crates/agentd/tests/relay.rs, which doesn't exist"
        )

    def test_a_guard_that_is_not_a_test_is_reported(self):
        problems = self.check([self.row()], self.relay(attribute=""))
        self.assert_reported(
            problems, "isn't a test that runs in crates/agentd/tests/relay.rs"
        )

    def test_a_guard_that_never_runs_is_reported(self):
        problems = self.check([self.row()], self.relay(attribute="#[test]\n#[ignore]"))
        self.assert_reported(
            problems, "isn't a test that runs in crates/agentd/tests/relay.rs"
        )

    def test_a_guard_that_does_not_name_its_rows_key_is_reported(self):
        for doc in ("/// The wrong key.", "// AGENTD-17", "/// AGENTD-18"):
            with self.subTest(doc=doc):
                problems = self.check([self.row()], self.relay(doc))
                self.assert_reported(problems, "doesn't name AGENTD-17")

    def test_a_key_on_a_neighboring_test_does_not_name_the_guard(self):
        files = {
            "crates/agentd/tests/relay.rs": "/// AGENTD-17\n#[test]\nfn a_neighbor() {}\n\n"
            "#[test]\nfn the_wrong_host_key_is_refused() {}\n"
        }
        problems = self.check([self.row()], files)
        self.assert_reported(problems, "doesn't name AGENTD-17")

    def test_a_guarded_row_without_a_key_or_a_guard_is_reported(self):
        for row in (self.row("none"), self.row(guard="none")):
            with self.subTest(row=row):
                problems = self.check([row], self.relay())
                self.assert_reported(problems, "a guarded row names a key and a guard")

    def test_a_known_gap_names_the_issue_that_closes_it(self):
        gap = self.row("none", "none", "known gap: nobody checks it")
        self.assert_reported(
            self.check([gap]), "a known gap names the issue that closes it"
        )
        self.assertEqual(self.check([self.row("none", "none", "known gap, #297")]), [])

    def test_a_status_that_is_neither_guarded_nor_a_gap_is_reported(self):
        problems = self.check([self.row(status="probably fine")], self.relay())
        self.assert_reported(
            problems, "the status starts with 'guarded' or 'known gap'"
        )

    def test_a_cell_that_is_not_backticked_names_is_reported(self):
        for row in (
            self.row("AGENTD-17"),
            self.row(
                guard="crates/agentd/tests/relay.rs::the_wrong_host_key_is_refused"
            ),
            self.row(guard="`the_wrong_host_key_is_refused`"),
        ):
            with self.subTest(row=row):
                problems = self.check([row], self.relay())
                self.assert_reported(
                    problems, "column isn't `none` or a list of backticked"
                )

    def test_a_row_with_the_wrong_cell_count_is_reported(self):
        problems = self.check(["| A threat | `AGENTD-17` | guarded |"], self.relay())
        self.assert_reported(problems, "has 3 cells, not 4")

    def test_a_header_that_is_not_the_four_columns_is_reported(self):
        section = (
            "## Threats and the tests that guard them\n\n| Threat | Key | Test | Status |\n"
            f"|---|---|---|---|\n{SENTINEL_ROW}"
        )
        self.assert_reported(
            self.check([], section=section), "the threat table's header"
        )

    def test_a_missing_trust_file_is_reported(self):
        tree = Tree(self, {"crates/microvms-edges/src/release.rs": SENTINEL_TEST})
        _, problems = TRACE["check_threats"](PATTERNS, THREAT_SENTENCES, tree.root)
        self.assertEqual(problems, ["docs/TRUST.md doesn't exist"])

    def test_a_missing_section_is_reported(self):
        problems = self.check([], section="## Something else\n\nNo table.")
        self.assert_reported(
            problems, "has no 'Threats and the tests that guard them' section"
        )

    def test_a_table_that_parses_to_no_rows_is_reported(self):
        for section in (
            "## Threats and the tests that guard them\n\nThe table moved.",
            "## Threats and the tests that guard them\n\n| Threat | Requirement | Guard | Status |"
            "\n|---|---|---|---|",
        ):
            with self.subTest(section=section):
                self.assert_reported(
                    self.check([], section=section), "parses to no rows"
                )

    def test_a_table_without_its_delimiter_row_is_reported_and_its_first_row_still_read(
        self,
    ):
        section = (
            "## Threats and the tests that guard them\n\n| Threat | Requirement | Guard | Status |"
            f"\n{self.row('`BIND-99`')}\n{SENTINEL_ROW}"
        )
        problems = self.check([], self.relay(), section=section)
        self.assert_reported(problems, "isn't its delimiter row")
        self.assert_reported(problems, "BIND-99 is not defined in either spec")

    def test_a_row_past_a_break_in_the_table_is_reported(self):
        for gap in ("", "Prose between rows."):
            with self.subTest(gap=gap):
                problems = self.check(
                    [self.row(), gap, self.row("`BIND-99`")], self.relay()
                )
                self.assert_reported(problems, "is past a break in the threat table")

    def guard_in(self, files: dict[str, str], path: str) -> list[str]:
        row = self.row(guard=f"`{path}::the_wrong_host_key_is_refused`")
        test = "/// AGENTD-17\n#[test]\nfn the_wrong_host_key_is_refused() {}\n"
        return self.check([row], {**files, path: test})

    def test_a_guard_that_mod_declarations_reach_passes(self):
        for files, path in (
            (
                {
                    "crates/agentd/src/lib.rs": "pub mod session;\n",
                    "crates/agentd/src/session/mod.rs": "#[cfg(test)]\nmod relay;\n",
                },
                "crates/agentd/src/session/relay.rs",
            ),
            (
                {
                    "crates/agentd/src/main.rs": "mod session;\n",
                    "crates/agentd/src/session.rs": "pub(crate) mod relay;\n",
                },
                "crates/agentd/src/session/relay.rs",
            ),
            (
                {
                    "crates/agentd/src/lib.rs": "mod session;\n",
                    "crates/agentd/src/session/mod.rs": "",
                },
                "crates/agentd/src/session/mod.rs",
            ),
            (
                {"crates/agentd/tests/relay.rs": "mod common;\n"},
                "crates/agentd/tests/common/mod.rs",
            ),
            ({}, "crates/agentd/src/bin/relay.rs"),
        ):
            with self.subTest(path=path, files=files):
                self.assertEqual(self.guard_in(files, path), [])

    def test_a_guard_no_mod_declaration_reaches_is_reported(self):
        for parent in (
            "",
            "// mod relay;\n",
            "mod relay {}\n",
            '#[cfg(feature = "slow")]\nmod relay;\n',
            '#[path = "other.rs"]\nmod relay;\n',
        ):
            with self.subTest(parent=parent):
                files = {
                    "crates/agentd/src/lib.rs": "pub mod session;\n",
                    "crates/agentd/src/session/mod.rs": parent,
                }
                problems = self.guard_in(files, "crates/agentd/src/session/relay.rs")
                self.assert_reported(
                    problems, "no `mod relay;` in crates/agentd/src/session.rs"
                )
        # A declaration whose own file nothing reaches doesn't reach its child either.
        files = {
            "crates/agentd/src/lib.rs": "",
            "crates/agentd/src/session/mod.rs": "mod relay;\n",
        }
        problems = self.guard_in(files, "crates/agentd/src/session/relay.rs")
        self.assert_reported(problems, "no `mod session;` in crates/agentd/src/lib.rs")

    def test_a_table_without_the_sentinel_row_is_reported(self):
        problems = self.check([self.row()], self.relay(), sentinel=False)
        self.assert_reported(
            problems, "the sentinel row naming BIND-18 is not in the threat table"
        )


class InputFloors(unittest.TestCase):
    """The check refuses to pass on input it didn't read, or read and found nothing in."""

    def test_an_empty_binding_directory_is_reported(self):
        tree = Tree(self, {RUST_TEST: "#[test]\nfn a() {}\n"})
        (tree.root / "bindings" / "microvms-js" / "__test__").mkdir(parents=True)
        problems = enumerator_floors(tree.root)
        self.assertIn(
            "BINDING_TESTS entry ('bindings/microvms-js/__test__', '*.mjs') yields no file",
            problems,
        )

    def test_files_that_name_no_test_leave_the_layer_empty_and_that_is_reported(self):
        tree = Tree(
            self,
            {
                RUST_TEST: "// IMAGE-5\n#[test]\nfn a() {}\n",
                PY_TEST: '"""IMAGE-5"""\n',
                JS_TEST: "// IMAGE-5\n",
            },
        )
        problems = layer_floors(collect(PATTERNS, tree.root), traced("CLI-7"))
        self.assertIn(
            "the test collector found no requirement key in any file it read", problems
        )
        self.assertIn(
            "the entry bindings/microvms-js/__test__ (*.mjs) yields files but no requirement key",
            problems,
        )


# The groups the loader tests' files are named for, and a directory of them that loads.
GROUPS = {"CLI", "IMAGE", "TRAP"}
CLI_FILE = """\
# The CLI group's traced requirements.

[CLI-9]
issue = "#216"

[CLI-10]
issue = "#216"
waive.live = "a pure function"
"""


class TracedFiles(unittest.TestCase):
    """The group files load through one loader, which refuses any file it can't read whole."""

    def load(self, files: dict[str, str]) -> dict:
        tree = Tree(
            self, {f"verify/spec/traced/{name}": text for name, text in files.items()}
        )
        (tree.root / "verify" / "spec" / "traced").mkdir(parents=True, exist_ok=True)
        return load_traced(tree.root / "verify" / "spec" / "traced", GROUPS, tree.root)

    def assert_refused(self, files: dict[str, str], *fragments: str) -> None:
        with self.assertRaises(SystemExit) as raised:
            self.load(files)
        for fragment in fragments:
            self.assertIn(fragment, str(raised.exception))

    def test_the_real_files_load_with_the_sentinel(self):
        sentences = TRACE["spec_keys"]()
        loaded = load_traced(
            TRACE["TRACED_DIR"], {key.rsplit("-", 1)[0] for key in sentences}
        )
        self.assertEqual(loaded[TRACE["SENTINEL"]].file, "verify/spec/traced/CLI.toml")
        self.assertEqual(loaded[TRACE["SENTINEL"]].issue, "#216")
        # Every file is named for a group, so every key sits in its own group's file.
        for key, entry in loaded.items():
            self.assertEqual(
                entry.file, f"verify/spec/traced/{key.rsplit('-', 1)[0]}.toml"
            )

    def test_a_table_without_the_sentinel_is_reported(self):
        problems = layer_floors({}, traced("CLI-8"))
        self.assertIn(
            "the sentinel CLI-7 is not in verify/spec/traced/CLI.toml", problems
        )
        self.assertNotIn(
            "the sentinel CLI-7 is not in verify/spec/traced/CLI.toml",
            layer_floors({}, traced("CLI-7")),
        )

    def test_entries_load_in_key_order_with_their_waivers(self):
        # CLI-10 after CLI-9, and the groups by name, whatever order the files hold them in.
        loaded = self.load(
            {
                "TRAP.toml": '[TRAP-2]\nissue = "#301"\n',
                "CLI.toml": CLI_FILE.replace("[CLI-9]", "[CLI-11]"),
                "IMAGE.toml": '[IMAGE-1]\nissue = "#220"\n',
            }
        )
        self.assertEqual(list(loaded), ["CLI-10", "CLI-11", "IMAGE-1", "TRAP-2"])
        self.assertEqual(
            loaded["CLI-10"],
            Traced("#216", {"live": "a pure function"}, "verify/spec/traced/CLI.toml"),
        )
        self.assertEqual(loaded["TRAP-2"].waive, {})

    def test_a_group_file_with_no_entry_loads_nothing(self):
        # A group no issue has traced yet keeps its file, which lists nothing.
        loaded = self.load({"CLI.toml": CLI_FILE, "TRAP.toml": "# Nothing yet.\n"})
        self.assertEqual(list(loaded), ["CLI-9", "CLI-10"])

    def test_a_key_in_another_groups_file_is_refused(self):
        self.assert_refused(
            {"CLI.toml": CLI_FILE + '\n[TRAP-1]\nissue = "#301"\n'},
            "trace: verify/spec/traced/CLI.toml: TRAP-1 isn't in the CLI group",
        )

    def test_a_key_that_isnt_a_group_and_a_number_is_refused(self):
        for key in ("cli-9", "CLI-9a", "CLI", "CLI-"):
            with self.subTest(key=key):
                self.assert_refused(
                    {"CLI.toml": f'["{key}"]\nissue = "#216"\n'},
                    f"verify/spec/traced/CLI.toml: {key} isn't in the CLI group",
                )

    def test_a_key_listed_twice_in_one_file_is_refused(self):
        self.assert_refused(
            {"CLI.toml": CLI_FILE + '\n[CLI-9]\nissue = "#216"\n'},
            "trace: verify/spec/traced/CLI.toml doesn't parse: Cannot declare ('CLI-9',) twice",
        )

    def test_a_key_listed_in_two_files_is_refused(self):
        self.assert_refused(
            {"CLI.toml": CLI_FILE, "IMAGE.toml": '[CLI-9]\nissue = "#216"\n'},
            "trace: CLI-9 is listed in more than one file: verify/spec/traced/CLI.toml, "
            "verify/spec/traced/IMAGE.toml",
        )

    def test_a_malformed_entry_is_refused(self):
        for entry in (
            'CLI-9 = "#216"',
            "[CLI-9]",
            '[CLI-9]\nissue = "216"',
            "[CLI-9]\nissue = 216",
            '[CLI-9]\nissues = "#216"',
            '[CLI-9]\nissue = "#216"\nwaives.live = "a pure function"',
            '[CLI-9]\nissue = "#216"\nwaive = "live"',
            '[CLI-9]\nissue = "#216"\nwaive.live = 1',
        ):
            with self.subTest(entry=entry):
                self.assert_refused(
                    {"CLI.toml": entry + "\n"},
                    "trace: verify/spec/traced/CLI.toml: CLI-9 is malformed",
                )

    def test_a_file_named_for_no_group_is_refused(self):
        for name in ("GATE.toml", "cli.toml", "CLI.tml", "README.md"):
            with self.subTest(name=name):
                self.assert_refused(
                    {"CLI.toml": CLI_FILE, name: '[GATE-1]\nissue = "#1"\n'},
                    f"trace: verify/spec/traced/{name} isn't named for a requirement group the specs "
                    "define: a group file is <GROUP>.toml, for one of CLI, IMAGE, TRAP",
                )

    def test_a_directory_in_the_traced_directory_is_refused(self):
        tree = Tree(
            self,
            {"verify/spec/traced/CLI.toml": CLI_FILE, "verify/spec/traced/TRAP/x": ""},
        )
        with self.assertRaises(SystemExit) as raised:
            load_traced(tree.root / "verify" / "spec" / "traced", GROUPS, tree.root)
        self.assertIn(
            "verify/spec/traced/TRAP isn't named for a requirement group",
            str(raised.exception),
        )

    def test_no_group_file_is_refused(self):
        # The floor: an empty or missing directory reads as nothing traced, which would pass
        # every per-key check vacuously.
        self.assert_refused({}, "trace: verify/spec/traced holds no group file")
        tree = Tree(self, {})
        with self.assertRaises(SystemExit) as raised:
            load_traced(tree.root / "verify" / "spec" / "traced", GROUPS, tree.root)
        self.assertIn("verify/spec/traced holds no group file", str(raised.exception))

    def test_every_problem_is_reported_together(self):
        self.assert_refused(
            {
                "CLI.toml": '[CLI-9]\nissues = "#216"\n',
                "GATE.toml": "",
                "IMAGE.toml": '[IMAGE-1]\nissue = "#220"\n\n[IMAGE-1]\n',
            },
            "verify/spec/traced/CLI.toml: CLI-9 is malformed",
            "verify/spec/traced/GATE.toml isn't named for a requirement group",
            "verify/spec/traced/IMAGE.toml doesn't parse",
        )


class ParserFloors(unittest.TestCase):
    """A parser that returns nothing, or fails, fails the check on the real tree."""

    def run_with_ast_grep(self, body: str) -> subprocess.CompletedProcess:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        stub = Path(directory.name) / "ast-grep"
        stub.write_text(f"#!/bin/sh\n{body}\n")
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        env = dict(os.environ, PATH=f"{directory.name}{os.pathsep}{os.environ['PATH']}")
        return subprocess.run(
            [sys.executable, str(SCRIPT)],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )

    def test_the_real_tree_passes_with_the_real_parser(self):
        out = subprocess.run(
            [sys.executable, str(SCRIPT)], cwd=ROOT, capture_output=True, text=True
        )
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_a_parser_that_matches_nothing_fails_the_check(self):
        # Python's tests are read by stdlib `ast`, so the test layer keeps those; the Rust and
        # Node halves are what go missing.
        out = self.run_with_ast_grep("exit 0")
        self.assertEqual(out.returncode, 1, out.stderr)
        self.assertIn(
            "the fuzz collector found no requirement key in any file it read",
            out.stderr,
        )
        self.assertIn(
            "the entry bindings/microvms-js/__test__ (*.mjs) yields files but no requirement key",
            out.stderr,
        )
        self.assertIn("CLI-7 has no test layer", out.stderr)
        # The threat table's guards are Rust tests, so none of them resolves either.
        self.assertIn("isn't a test that runs in", out.stderr)

    def test_a_missing_parser_fails_the_check(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        out = subprocess.run(
            [sys.executable, str(SCRIPT)],
            cwd=ROOT,
            env=dict(os.environ, PATH=directory.name),
            capture_output=True,
            text=True,
        )
        self.assertEqual(out.returncode, 1, out.stderr)
        self.assertIn("trace: ast-grep isn't on PATH", out.stderr)

    def test_a_parser_that_fails_fails_the_check(self):
        out = self.run_with_ast_grep("echo broken >&2; exit 2")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("ast-grep failed", out.stderr)


if __name__ == "__main__":
    unittest.main()
