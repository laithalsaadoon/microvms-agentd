# SPDX-License-Identifier: Apache-2.0
"""Tests for `scripts/check-trace.py`: which mentions of a key count for the test and fuzz layers.

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

# Keys from the real specs' prefixes, so the key and name patterns are the ones the tree gets.
PATTERNS = Patterns(
    {key: "" for key in ("IMAGE-5", "IMAGE-12", "CLI-7", "AGENTD-7", "BIND-6")}
)

# Each fixture file sits where the collector looks: a Rust integration test, a source file's
# test module, a binding test in each language, and a fuzz harness.
RUST_TEST = "microvms-cli/tests/case.rs"
RUST_SOURCE = "microvms-cli/src/case.rs"
PY_TEST = "microvms-py/tests/test_case.py"
JS_TEST = "microvms-js/__test__/case.mjs"
FUZZ = "microvms-core/tests/case_fuzz.rs"


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
        self.assertIn(f"{key} has no test layer", gaps(found, {key: "#276"}))

    def assert_tested(self, path: str, text: str, key: str = "IMAGE-5") -> None:
        tree = Tree(self, {path: text})
        self.assertEqual(tree.layers(key).get("test"), {path})
        found = collect(PATTERNS, tree.root)
        self.assertNotIn(f"{key} has no test layer", gaps(found, {key: "#276"}))

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
            "microvms-core/tests/live_case.rs",
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
                "agentd/fuzz/fuzz_targets/case.rs": """
                    #![no_main]
                    /// **AGENTD-7.**
                    fuzz_target!(|data: &[u8]| {
                        let _ = data;
                    });
                    """
            },
        )
        self.assertEqual(
            tree.layers("AGENTD-7").get("fuzz"), {"agentd/fuzz/fuzz_targets/case.rs"}
        )


class InputFloors(unittest.TestCase):
    """The check refuses to pass on input it didn't read, or read and found nothing in."""

    def test_an_empty_binding_directory_is_reported(self):
        tree = Tree(self, {RUST_TEST: "#[test]\nfn a() {}\n"})
        (tree.root / "microvms-js" / "__test__").mkdir(parents=True)
        problems = enumerator_floors(tree.root)
        self.assertIn(
            "BINDING_TESTS entry ('microvms-js/__test__', '*.mjs') yields no file",
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
        problems = layer_floors(collect(PATTERNS, tree.root))
        self.assertIn(
            "the test collector found no requirement key in any file it read", problems
        )
        self.assertIn(
            "the entry microvms-js/__test__ (*.mjs) yields files but no requirement key",
            problems,
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
            "the entry microvms-js/__test__ (*.mjs) yields files but no requirement key",
            out.stderr,
        )
        self.assertIn("CLI-7 has no test layer", out.stderr)

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
