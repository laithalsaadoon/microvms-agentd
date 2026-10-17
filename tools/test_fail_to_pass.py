# SPDX-License-Identifier: Apache-2.0
"""FAIL_TO_PASS proves a fix's test by failing it with the fix taken out, and refuses the rest.

Each case builds a throwaway repo whose `origin/main` is its base commit: one product file
(`crates/agentd/src/lib.rs`, whose `answer` returns 41 on the base), one test target
(`crates/agentd/tests/regress.rs`), the finders' sentinel files copied from this repo, and a
registry with one unrelated entry. The branch fixes `answer` to 42. `check` runs as it is. `prove`
drives the real tools/check-guards-fire.py over the branch with a fake `cargo` on PATH, which
answers `cargo metadata` for the fixture and runs a test by reading the tree it's in: a test
passes when lib.rs has 42, a line `// <test>: passes anyway` in its test file makes it pass
regardless, and `// <test>: needs new_api` breaks the build where lib.rs has no `new_api`. The
finders are the real ast-grep, so run this through `mise run fail-to-pass:check`.
"""

import inspect
import json
import os
import re
import runpy
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SCRIPT = Path(__file__).with_name("fail-to-pass.py")
REPO = SCRIPT.parent.parent
TOOL = runpy.run_path(str(SCRIPT), run_name="tools.fail-to-pass")
# mutmut's trampoline has its own globals; fakes must replace the wrapped script's ports.
TOOL_GLOBALS = inspect.unwrap(TOOL["main"]).__globals__
SENTINELS = TOOL["SENTINELS"]
REGISTRY_DIR = TOOL["REGISTRY_DIR"]

# A sentinel is copied for the finders to read. Its Falsification blocks are registry entries
# naming files this fixture doesn't have, which the fire's loader would refuse, so they're left
# out of the copy.
BLOCKS = re.compile(
    r"^[ \t]*///[ \t]?```falsification[ \t]*\n(?:[ \t]*///.*\n)*?[ \t]*///[ \t]?```[ \t]*\n",
    re.MULTILINE,
)
PRODUCT = "crates/agentd/src/lib.rs"
TESTS = "crates/agentd/tests/regress.rs"
BASE_LIB = """\
pub fn answer() -> u32 {
    41
}

#[cfg(test)]
mod tests {
    #[test]
    fn inline_holds() {
        assert_eq!(super::answer(), 42);
    }
}
"""
FIXED_LIB = BASE_LIB.replace("    41\n", "    42\n")
TEST_FILE = """\
#[test]
fn the_answer_is_42() {
    assert_eq!(agentd::answer(), 42);
}
"""
ENTRY = """\
# ── base: an entry the fixture's base has ──

[[fault]]
id = "unrelated"
guard = "unrelated_guard"
run = ["cargo", "test", "-p", "agentd", "--lib", "--", "--exact", "unrelated_guard"]
expect = "test-failed"
suite = "rust"
transform = {{ file = "{product}", replace = "pub fn answer", with = "pub fn answered" }}
""".format(product=PRODUCT)

FAKE_CARGO = """\
#!{python}
import json, pathlib, re, sys
root = pathlib.Path.cwd()
args = sys.argv[1:]
if args[:1] == ["metadata"]:
    crate = root / "crates/agentd"
    print(json.dumps({{"packages": [{{"name": "agentd", "targets": [
        {{"name": "agentd", "kind": ["lib"], "src_path": str(crate / "src/lib.rs")}},
        {{"name": "regress", "kind": ["test"], "src_path": str(crate / "tests/regress.rs")}},
    ]}}]}}))
    sys.exit(0)
lib = (root / "crates/agentd/src/lib.rs").read_text()
tests = (root / "crates/agentd/tests/regress.rs").read_text()
name = args[args.index("--exact") + 1]
short = name.rsplit("::", 1)[-1]
if f"// {{short}}: needs new_api" in tests and "new_api" not in lib:
    print("error[E0425]: cannot find function `new_api` in crate `agentd`")
    print("error: could not compile `agentd`")
    sys.exit(101)
ok = f"// {{short}}: passes anyway" in tests or "    42\\n" in lib
print(f"test {{name}} ... {{'ok' if ok else 'FAILED'}}")
sys.exit(0 if ok else 1)
"""

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


def clean_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in GIT_ENV_LEAKS}
    env.update(extra)
    return env


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@localhost", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env=clean_env(),
    ).stdout


class Fixture(unittest.TestCase):
    """The fixture repo at its base, `origin/main` pointing there."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.top = Path(directory.name)
        self.root = self.top / "repo"
        self.bin = self.top / "bin"
        self.bin.mkdir()
        cargo = self.bin / "cargo"
        cargo.write_text(FAKE_CARGO.format(python=sys.executable))
        cargo.chmod(cargo.stat().st_mode | stat.S_IXUSR)
        (self.top / "tmp").mkdir()
        files = {
            PRODUCT: BASE_LIB,
            TESTS: TEST_FILE,
            f"{REGISTRY_DIR}/base.toml": ENTRY,
            "verify/guards/unregistered.txt": "",
            "changelog.d/.gitkeep": "",
        }
        for path in (
            TOOL["PARITY_SENTINEL"],
            TOOL["PARITY_TABLE"],
            "crates/microvms-core/tests/parity_corpus/mod.rs",
        ):
            files[path] = (REPO / path).read_text(encoding="utf-8")
        for path, _ in SENTINELS.values():
            files[path] = BLOCKS.sub("", (REPO / path).read_text(encoding="utf-8"))
        for path, text in files.items():
            self.write(path, text)
        git(self.top, "init", "-q", "-b", "main", str(self.root))
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "base")
        git(self.root, "update-ref", "refs/remotes/origin/main", "HEAD")

    def write(self, path: str, text: str) -> None:
        file = self.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text)

    def fix(self, fragment: str = "7.fixed.md", lib: str = FIXED_LIB) -> None:
        self.write(PRODUCT, lib)
        self.write(
            f"changelog.d/{fragment}", "- **The answer is 42 (#7).** It was 41.\n"
        )

    def run_script(self, *args: str):
        import contextlib
        import io
        from unittest.mock import patch

        output, errors = io.StringIO(), io.StringIO()
        env = clean_env(
            PATH=os.pathsep.join([str(self.bin), os.environ.get("PATH", "")]),
            TMPDIR=str(self.top / "tmp"),
        )
        # Exercise the command entry point in-process so mutation coverage sees its verdict.
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(sys, "argv", [str(SCRIPT), *args]),
            contextlib.chdir(self.root),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            code = TOOL["main"]()
        return subprocess.CompletedProcess(
            args, code, output.getvalue(), errors.getvalue()
        )

    def assertExit(self, done, code: int):
        self.assertEqual(done.returncode, code, done.stdout + done.stderr)


class CheckTests(Fixture):
    """`check`: a fix's tests need an entry the branch adds; anything else passes, saying why."""

    def new_test(self, name: str = "the_answer_is_42_now") -> str:
        self.write(
            TESTS, TEST_FILE + f"\n#[test]\nfn {name}() {{\n    assert!(true);\n}}\n"
        )
        return name

    def entry(self, guard: str, owner: str = "fix", run=None) -> None:
        run = run or [
            "cargo",
            "test",
            "-p",
            "agentd",
            "--test",
            "regress",
            "--",
            "--exact",
            guard,
        ]
        entry_id = "proof" if owner == "fix" else f"proof-{owner}"
        self.write(
            f"{REGISTRY_DIR}/{owner}.toml",
            f'# ── {owner} ──\n\n[[fault]]\nid = "{entry_id}"\nguard = "{guard}"\n'
            f'run = {json.dumps(run)}\nexpect = "test-failed"\nsuite = "rust"\n'
            f'patch = "{REGISTRY_DIR}/proof.patch"\n',
        )

    def test_a_branch_that_isnt_a_fix_passes(self):
        self.write(PRODUCT, FIXED_LIB)
        self.new_test()
        done = self.run_script("check")
        self.assertExit(done, 0)
        self.assertIn("so it isn't a fix", done.stdout)

    def test_a_fix_whose_test_has_no_entry_fails(self):
        self.fix()
        name = self.new_test()
        done = self.run_script("check")
        self.assertExit(done, 1)
        self.assertIn(f"  {name}, in {TESTS}", done.stdout)
        self.assertIn("mise run fail-to-pass -- --emit <owner>", done.stdout)

    def test_a_fix_whose_test_has_a_new_entry_passes(self):
        self.fix()
        self.entry(self.new_test())
        done = self.run_script("check")
        self.assertExit(done, 0)
        self.assertIn("proof (the_answer_is_42_now)", done.stdout)

    def test_every_changed_test_needs_its_own_proof(self):
        self.fix()
        self.write(
            TESTS, TEST_FILE + "\n#[test]\nfn first() {}\n#[test]\nfn second() {}\n"
        )
        self.entry("first")
        done = self.run_script("check")
        self.assertExit(done, 1)
        self.assertIn(f"  second, in {TESTS}", done.stdout)
        self.assertNotIn(f"  first, in {TESTS}", done.stdout)

    def test_all_changed_tests_with_proofs_pass(self):
        self.fix()
        self.write(
            TESTS, TEST_FILE + "\n#[test]\nfn first() {}\n#[test]\nfn second() {}\n"
        )
        self.entry("first")
        self.entry("second", owner="second")
        self.assertExit(self.run_script("check"), 0)

    def test_a_changed_entry_can_prove_a_changed_test(self):
        self.entry("changed")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "entry")
        git(self.root, "update-ref", "refs/remotes/origin/main", "HEAD")
        self.fix()
        self.new_test("changed")
        path = f"{REGISTRY_DIR}/fix.toml"
        self.write(
            path, (self.root / path).read_text().replace("proof.patch", "changed.patch")
        )
        self.assertExit(self.run_script("check"), 0)

    def test_a_wrong_rust_selector_cannot_prove_a_named_test(self):
        self.fix()
        name = self.new_test()
        for package, target, selected in (
            ("other-package", "regress", name),
            ("agentd", "other-target", name),
            ("agentd", "regress", "other-test"),
        ):
            with self.subTest(package=package, target=target, selected=selected):
                self.entry(
                    name,
                    run=[
                        "cargo",
                        "test",
                        "-p",
                        package,
                        "--test",
                        target,
                        "--",
                        "--exact",
                        selected,
                    ],
                )
                self.assertExit(self.run_script("check"), 1)

    def test_a_non_failure_entry_cannot_prove_a_test(self):
        self.fix()
        self.entry(self.new_test())
        path = f"{REGISTRY_DIR}/fix.toml"
        self.write(
            path,
            (self.root / path).read_text().replace('"test-failed"', '"exit-nonzero"'),
        )
        self.assertExit(self.run_script("check"), 1)

    def test_one_concrete_pytest_parameter_proves_its_source_function(self):
        self.fix()
        path = "bindings/microvms-py/tests/test_parameters.py"
        self.write(
            path,
            "import pytest\n@pytest.mark.parametrize('value', [1, 2])\ndef test_case(value):\n    assert value\n",
        )
        guard = f"{path}::test_case[1]"
        self.entry(guard, run=["python", "-m", "pytest", "-q", "-rA", guard])
        self.assertExit(self.run_script("check"), 0)

    def test_a_pytest_parameter_cannot_prove_a_near_prefix_or_other_selector(self):
        self.fix()
        path = "bindings/microvms-py/tests/test_parameters.py"
        self.write(path, "def test_case():\n    pass\n")
        for guard, selected in (
            (f"{path}::test_cases[1]", f"{path}::test_cases[1]"),
            (f"{path}::test_case[1", f"{path}::test_case[1"),
            (f"{path}::test_case[1]", f"{path}::test_case[2]"),
        ):
            with self.subTest(guard=guard, selected=selected):
                self.entry(guard, run=["python", "-m", "pytest", "-q", "-rA", selected])
                self.assertExit(self.run_script("check"), 1)

    def test_a_node_title_in_one_file_cannot_prove_another_file(self):
        self.fix()
        paths = [f"bindings/microvms-js/__test__/{name}.mjs" for name in ("one", "two")]
        for path in paths:
            self.write(
                path, "import test from 'node:test';\ntest('same title', () => {});\n"
            )
        self.entry(
            "same title", run=["node", "--test", "--test-reporter=tap", paths[0]]
        )
        done = self.run_script("check")
        self.assertExit(done, 1)
        self.assertIn(f"  same title, in {paths[1]}", done.stdout)
        self.assertNotIn(f"  same title, in {paths[0]}", done.stdout)

    def test_duplicate_changed_node_titles_cannot_share_a_proof(self):
        self.fix()
        path = "bindings/microvms-js/__test__/answer.mjs"
        self.write(
            path,
            "import test from 'node:test';\ntest('same title', () => {});\ntest('same title', () => {});\n",
        )
        self.entry("same title", run=["node", "--test", "--test-reporter=tap", path])
        done = self.run_script("check")
        self.assertExit(done, 1)
        self.assertIn("changed Node test title 'same title' is ambiguous", done.stderr)

    def test_a_changed_node_title_cannot_share_an_unchanged_tests_identity(self):
        path = "bindings/microvms-js/__test__/answer.mjs"
        source = "import test from 'node:test';\ntest('same title', () => { return 1; });\ntest('same title', () => {});\n"
        self.write(path, source)
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "duplicate titles")
        git(self.root, "update-ref", "refs/remotes/origin/main", "HEAD")
        self.fix()
        self.write(path, source.replace("return 1", "return 2"))
        self.entry("same title", run=["node", "--test", "--test-reporter=tap", path])
        done = self.run_script("check")
        self.assertExit(done, 1)
        self.assertIn("changed Node test title 'same title' is ambiguous", done.stderr)

    def test_a_security_fragment_is_a_fix_too(self):
        self.fix(fragment="7.security.md")
        self.new_test()
        self.assertExit(self.run_script("check"), 1)

    def test_an_entry_the_base_already_has_does_not_count(self):
        self.entry("the_answer_is_42")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "entry")
        git(self.root, "update-ref", "refs/remotes/origin/main", "HEAD")
        self.fix()
        self.write(
            TESTS, TEST_FILE.replace("assert_eq!", "assert_eq!(1, 1);\n    assert_eq!")
        )
        done = self.run_script("check")
        self.assertExit(done, 1)
        self.assertIn("  the_answer_is_42, in", done.stdout)

    def test_only_the_changed_tests_are_named(self):
        self.fix()
        self.new_test()
        done = self.run_script("check")
        self.assertIn("the_answer_is_42_now", done.stdout)
        self.assertNotIn("  the_answer_is_42, in", done.stdout)

    def test_a_changed_attribute_changes_its_test(self):
        self.fix()
        self.write(TESTS, TEST_FILE.replace("#[test]\n", "#[test]\n#[ignore]\n"))
        self.assertIn("  the_answer_is_42, in", self.run_script("check").stdout)

    def test_a_changed_multiline_attribute_selects_its_test(self):
        source = "#[cfg(\n    unix\n)]\n\n/* the runtime\n   attribute follows */\n#[tokio::test(\n    start_paused = false\n)]\n\nasync fn timed() {}\n"
        self.write(TESTS, source)
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "multiline attributes")
        git(self.root, "update-ref", "refs/remotes/origin/main", "HEAD")
        self.fix()
        self.write(TESTS, source.replace("unix", "windows").replace("false", "true"))
        done = self.run_script("check")
        self.assertExit(done, 1)
        self.assertIn(f"  timed, in {TESTS}", done.stdout)

    def test_javascript_unicode_titles_are_selected_as_reported(self):
        self.fix()
        path = "bindings/microvms-js/__test__/unicode.mjs"
        self.write(
            path,
            r"import test from 'node:test'; test('\u{1F680} answers', () => {});"
            + "\n",
        )
        done = self.run_script("check")
        self.assertExit(done, 1)
        self.assertIn(f"  🚀 answers, in {path}", done.stdout)

    def test_an_inline_test_in_a_test_module_is_named(self):
        self.fix(lib=FIXED_LIB.replace("    #[test]\n", "    #[test]\n    #[ignore]\n"))
        done = self.run_script("check")
        self.assertExit(done, 1)
        self.assertIn("tests::inline_holds (inline)", done.stdout)

    def test_pytest_and_node_tests_are_named(self):
        self.fix()
        self.write(
            "bindings/microvms-py/tests/test_answer.py",
            "class TestAnswer:\n    def test_is_42(self):\n        assert True\n\n"
            "def test_again():\n    pass\n\ndef helper():\n    pass\n",
        )
        self.write(
            "bindings/microvms-js/__test__/answer.mjs",
            "import test from 'node:test';\n\ntest('the answer is 42', () => {});\n",
        )
        done = self.run_script("check")
        path = "bindings/microvms-py/tests/test_answer.py"
        self.assertIn(f"{path}::TestAnswer::test_is_42", done.stdout)
        self.assertIn(f"{path}::test_again,", done.stdout)
        self.assertNotIn("helper", done.stdout)
        self.assertIn("  the answer is 42, in", done.stdout)

    def test_a_fix_with_no_test_passes_saying_so(self):
        self.fix()
        done = self.run_script("check")
        self.assertExit(done, 0)
        self.assertIn("adds or changes no test", done.stdout)

    def test_a_fix_with_no_product_change_passes_saying_so(self):
        self.write("changelog.d/7.fixed.md", "- **Docs (#7).** Fixed.\n")
        self.new_test()
        done = self.run_script("check")
        self.assertExit(done, 0)
        self.assertIn("changes no product source", done.stdout)

    def test_a_finder_that_misses_its_sentinel_fails(self):
        path, guard = SENTINELS["node"]
        self.write(path, (REPO / path).read_text().replace(guard, "renamed"))
        done = self.run_script("check")
        self.assertExit(done, 1)
        self.assertIn(f"the node finder doesn't find {guard}", done.stdout)


class ParityTests(Fixture):
    """Changed case files are proofs on every applicable surface, selected individually."""

    def add_case(self, name="changed"):
        path = f"{TOOL['PARITY_DIR']}/wrap-dockerfile/{name}.json"
        self.write(path, (REPO / TOOL["PARITY_SENTINEL"]).read_text())
        return path

    def test_a_changed_case_selects_each_real_surface_without_copying_expectations(
        self,
    ):
        self.fix()
        path = self.add_case()
        branch = TOOL["Branch"](self.root, "origin/main")
        tests = TOOL["branch_tests"](branch)
        self.assertEqual({test.surface for test in tests}, {"core", "cli", "py", "ts"})
        self.assertTrue(all(test.path == path for test in tests))
        made, skipped = TOOL["candidates"](branch, tests)
        self.assertEqual(skipped, [])
        self.assertEqual(len(made), 4)
        for candidate in made:
            argv = candidate.run[-1]
            case = "wrap-dockerfile/changed"
            self.assertIn(case, " ".join(argv).replace("\\", ""))
            self.assertNotIn("expect", " ".join(argv))
        self.assertIn("--test", made[1].run[-1])
        self.assertIn("parity_cases", made[1].run[-1])
        self.assertIn(
            "--test-name-pattern=^wrap\\-dockerfile/changed$", made[3].run[-1]
        )

    def test_shared_proof_commands_keep_exact_runner_selectors(self):
        self.add_case()
        tests = TOOL["parity_tests"](
            self.root, [f"{TOOL['PARITY_DIR']}/wrap-dockerfile/changed.json"]
        )
        configs = [
            "--config",
            'env.MICROVMS_PARITY_CASE.value="wrap-dockerfile/changed"',
            "--config",
            "env.MICROVMS_PARITY_CASE.force=true",
        ]
        expectations = {
            "core": (
                "rust",
                [
                    "cargo",
                    "test",
                    *configs,
                    "-p",
                    "microvms-core",
                    "--test",
                    "parity_cases",
                    "--",
                    "--exact",
                    "core_answers_the_shared_case_corpus",
                ],
            ),
            "cli": (
                "rust",
                [
                    "cargo",
                    "test",
                    *configs,
                    "-p",
                    "microvms-cli",
                    "--test",
                    "parity_cases",
                    "--",
                    "--exact",
                    "the_cli_process_answers_the_shared_case_corpus",
                ],
            ),
            "py": (
                "bindings",
                [
                    "python",
                    "-m",
                    "pytest",
                    "-q",
                    "-rA",
                    "bindings/microvms-py/tests/test_parity_cases.py::test_a_case[wrap-dockerfile/changed]",
                ],
            ),
            "ts": (
                "bindings",
                [
                    "node",
                    "--test",
                    "--test-reporter=tap",
                    r"--test-name-pattern=^wrap\-dockerfile/changed$",
                    "bindings/microvms-js/__test__/parity_cases.mjs",
                ],
            ),
        }
        for test in tests:
            with self.subTest(surface=test.surface):
                run, suite = TOOL["parity_command"](test)
                self.assertEqual((suite, run[-1]), expectations[test.surface])
                self.assertEqual(
                    run[0],
                    [
                        "cargo",
                        "build",
                        "-p",
                        "microvms-core",
                        "--example",
                        "parity-tunnel-peer",
                    ],
                )
                if test.surface in ("py", "ts"):
                    self.assertEqual(len(run), 3)
                    self.assertEqual(
                        run[1],
                        TOOL[
                            "PARITY_PY_BUILD"
                            if test.surface == "py"
                            else "PARITY_JS_BUILD"
                        ],
                    )
                else:
                    self.assertEqual(len(run), 2)
        fake = TOOL["Test"](
            "parity",
            "case.json",
            "guards::parity::the_cli_answers_the_shared_case_corpus_against_its_fakes",
            "fake",
            1,
            2,
            case="start/options",
            surface="cli",
        )
        self.assertEqual(
            TOOL["parity_command"](fake)[0][-1][-7:],
            ["-p", "microvms-cli", "--bin", "microvm", "--", "--exact", fake.guard],
        )

    def test_the_capability_and_case_skip_omit_only_their_surfaces(self):
        path = self.add_case()
        table = self.root / TOOL["PARITY_TABLE"]
        text = table.read_text()
        text = text.replace('py = "wrap_dockerfile"', 'py = { exempt = "fixture" }')
        table.write_text(text)
        case = json.loads((self.root / path).read_text())
        case["skip"] = {"ts": "fixture (IMAGE-1)"}
        self.write(path, json.dumps(case))
        tests = TOOL["parity_tests"](self.root, [path])
        self.assertEqual({test.surface for test in tests}, {"core", "cli"})

    def test_one_cases_entry_does_not_prove_another_case(self):
        self.fix()
        self.add_case()
        self.add_case("another")
        branch = TOOL["Branch"](self.root, "origin/main")
        tests = TOOL["branch_tests"](branch)
        made, _ = TOOL["candidates"](branch, tests)
        self.write(
            f"{REGISTRY_DIR}/partial.toml", TOOL["entry_toml"](made[0], branch.fork)
        )
        done = self.run_script("check")
        self.assertExit(done, 1)
        self.assertIn("wrap-dockerfile/changed", done.stdout)
        TOOL["write_entries"](self.root, "complete", made[4:5], "", branch.fork)
        self.assertExit(self.run_script("check"), 0)

    def test_a_shared_case_entry_must_report_a_test_failure(self):
        self.fix()
        self.add_case()
        branch = TOOL["Branch"](self.root, "origin/main")
        made, _ = TOOL["candidates"](branch, TOOL["branch_tests"](branch))
        entry = TOOL["entry_toml"](made[0], branch.fork).replace(
            'expect = "test-failed"', 'expect = "exit-nonzero"'
        )
        self.write(f"{REGISTRY_DIR}/wrong-verdict.toml", entry)
        self.assertExit(self.run_script("check"), 1)

    def test_the_parity_sentinel_refuses_a_finder_that_reads_no_cases(self):
        from unittest.mock import patch

        with patch.dict(TOOL_GLOBALS, {"parity_tests": lambda *_: []}):
            problems = TOOL["sentinel_problems"](self.root)
        self.assertIn(
            "the parity finder doesn't find every surface of wrap-dockerfile/sentinel",
            problems,
        )

    def test_a_shared_case_proof_does_not_cover_an_ordinary_test(self):
        self.fix()
        self.add_case()
        path = "bindings/microvms-py/tests/test_ordinary.py"
        self.write(path, "def test_ordinary():\n    pass\n")
        branch = TOOL["Branch"](self.root, "origin/main")
        made, _ = TOOL["candidates"](branch, TOOL["branch_tests"](branch))
        shared = next(c for c in made if c.test.kind == "parity")
        TOOL["write_entries"](self.root, "shared", [shared], "", branch.fork)
        done = self.run_script("check")
        self.assertExit(done, 1)
        self.assertIn(f"  {path}::test_ordinary, in {path}", done.stdout)
        ordinary = next(c for c in made if c.test.kind == "pytest")
        TOOL["write_entries"](self.root, "ordinary", [ordinary], "", branch.fork)
        self.assertExit(self.run_script("check"), 0)

    def test_a_mixed_proof_needs_both_ordinary_and_shared_regressions(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        self.fix()
        self.add_case()
        self.write(
            "bindings/microvms-py/tests/test_ordinary.py",
            "def test_ordinary():\n    pass\n",
        )
        branch = TOOL["Branch"](self.root, "origin/main")
        for ordinary, shared, expected in (
            (False, True, 1),
            (True, False, 1),
            (True, True, 0),
        ):
            with self.subTest(ordinary=ordinary, shared=shared):

                def fire(_branch, made, _patch, _args):
                    return {
                        c.id: (True, "proven")
                        if (
                            ordinary
                            if c.test.kind != "parity"
                            else shared and c.test.surface == "core"
                        )
                        else (
                            False,
                            "passes with the fix taken out, so it proves nothing about the fix",
                        )
                        for c in made
                    }

                with patch.dict(TOOL_GLOBALS, {"fire": fire}):
                    code = TOOL["prove"](branch, SimpleNamespace(any=False, emit=None))
                self.assertEqual(code, expected)

    def test_a_head_failure_is_rejected_even_if_another_surface_is_proven(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        self.fix()
        self.add_case()
        branch = TOOL["Branch"](self.root, "origin/main")

        def fire(_branch, made, _patch, _args):
            return {
                candidate.id: (candidate.test.surface != "ts", "fixture verdict")
                for candidate in made
            }

        with patch.dict(TOOL_GLOBALS, {"fire": fire}):
            code = TOOL["prove"](branch, SimpleNamespace(any=False, emit=None))
        self.assertEqual(code, 1)

    def test_an_adapter_only_fix_allows_other_surfaces_to_pass_on_both_trees(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        self.fix()
        self.add_case()
        branch = TOOL["Branch"](self.root, "origin/main")

        def fire(_branch, made, _patch, _args):
            return {
                candidate.id: (True, "proven")
                if candidate.test.surface == "py"
                else (
                    False,
                    "passes with the fix taken out, so it proves nothing about the fix",
                )
                for candidate in made
            }

        with patch.dict(TOOL_GLOBALS, {"fire": fire}):
            code = TOOL["prove"](branch, SimpleNamespace(any=False, emit="adapter"))
        self.assertEqual(code, 0)
        entries = tomllib.loads(
            (self.root / REGISTRY_DIR / "adapter.toml").read_text()
        )["fault"]
        self.assertEqual(len(entries), 1)
        self.assertIn("test_a_case[", entries[0]["guard"])
        self.assertExit(self.run_script("check"), 0)

    def test_a_skipped_node_surface_cannot_complete_a_shared_proof(self):
        import contextlib
        import io
        from types import SimpleNamespace
        from unittest.mock import patch

        self.fix()
        self.add_case()
        branch = TOOL["Branch"](self.root, "origin/main")
        guard = "wrap-dockerfile/changed"
        harness = TOOL["FIRE"]
        fault = harness["Fault"](
            id="node",
            guard=guard,
            expect="test-failed",
            suite="bindings",
            run=[["node", "--test", "--test-reporter=tap", "case.mjs"]],
        )
        key = harness["command_key"](fault)
        for directive in ("SKIP", "TODO"):
            with self.subTest(directive=directive):
                output = f"ok 1 - {guard} # {directive} reason\n"
                board = harness["Board"]([[]], False)
                board.done[key] = [(0, output, output, 0.1, 1)]
                with contextlib.redirect_stdout(io.StringIO()):
                    node_passed = harness["check_pass"](
                        [fault], board, {key: 1}, lambda *_: None, "clean"
                    )

                def fire(_branch, made, _patch, _args):
                    return {
                        candidate.id: (True, "proven")
                        if candidate.test.surface == "core"
                        else TOOL["judge"]("guard not found", "")
                        if candidate.test.surface == "ts" and not node_passed
                        else (
                            False,
                            "passes with the fix taken out, so it proves nothing about the fix",
                        )
                        for candidate in made
                    }

                with patch.dict(TOOL_GLOBALS, {"fire": fire}):
                    code = TOOL["prove"](branch, SimpleNamespace(any=False, emit=None))
                self.assertEqual(code, 1)

    def test_every_case_needs_a_regression_even_when_another_case_is_proven(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        self.fix()
        self.add_case()
        self.add_case("already-correct")
        branch = TOOL["Branch"](self.root, "origin/main")

        def fire(_branch, made, _patch, _args):
            return {
                candidate.id: (True, "proven")
                if candidate.test.case.endswith("/changed")
                and candidate.test.surface == "py"
                else (
                    False,
                    "passes with the fix taken out, so it proves nothing about the fix",
                )
                for candidate in made
            }

        with patch.dict(TOOL_GLOBALS, {"fire": fire}):
            code = TOOL["prove"](branch, SimpleNamespace(any=False, emit=None))
        self.assertEqual(code, 1)


class FireCompletionTests(Fixture):
    """Only a completed restored pass can support the earlier fault verdicts."""

    def fire_results(self, replies):
        from types import SimpleNamespace
        from unittest.mock import patch

        made = [
            TOOL["Candidate"](
                TOOL["Test"](
                    "parity",
                    TOOL["PARITY_SENTINEL"],
                    f"guard-{surface}",
                    surface,
                    1,
                    1,
                    case="wrap-dockerfile/sentinel",
                    surface=surface,
                ),
                surface,
                [["cargo", "test"]],
                "rust",
            )
            for surface in ("core", "py")
        ]
        real_run = subprocess.run
        calls = []

        def run(argv, **kwargs):
            if len(argv) > 1 and argv[1] == str(TOOL["GUARDS_FIRE"]):
                calls.append(argv)
                code, output = replies[len(calls) - 1]
                return subprocess.CompletedProcess(argv, code, output, "")
            return real_run(argv, **kwargs)

        with patch("subprocess.run", side_effect=run):
            verdicts = TOOL["fire"](
                TOOL["Branch"](self.root, "origin/main"),
                made,
                "",
                SimpleNamespace(
                    target_dir=str(self.top / "target"), timeout=30, jobs=1
                ),
            )
        self.assertEqual(len(calls), len(replies))
        return verdicts

    def test_an_unfinished_restored_pass_cannot_prove_a_fault(self):
        for code, ending in (
            (0, ""),
            (1, ""),
            (1, "guards: restored, every command passes again (0.1 s)\n"),
        ):
            with self.subTest(code=code, ending=ending):
                verdicts = self.fire_results(
                    [(code, "fired: core (0.1 s)\nfired: py (0.1 s)\n" + ending)]
                )
                self.assertFalse(verdicts["core"][0])
                self.assertFalse(verdicts["py"][0])
                self.assertIn("restored verification", verdicts["core"][1])

    def test_a_surviving_candidate_does_not_discard_an_independent_proof(self):
        verdicts = self.fire_results(
            [
                (
                    1,
                    "fired: core (0.1 s)\n"
                    "DID NOT FIRE: py: the command passed with the fault seeded (0.1 s)\n"
                    "guards: restored, every command passes again (0.1 s)\n",
                )
            ]
        )
        self.assertTrue(verdicts["core"][0])
        self.assertTrue(TOOL["parity_base_pass"](verdicts["py"]))

    def test_an_unrestored_unchanged_surface_is_not_a_passing_surface(self):
        verdicts = self.fire_results(
            [
                (
                    1,
                    "fired: core (0.1 s)\n"
                    "DID NOT FIRE: py: the command passed with the fault seeded (0.1 s)\n",
                )
            ]
        )
        self.assertFalse(verdicts["core"][0])
        self.assertFalse(TOOL["parity_base_pass"](verdicts["py"]))

    def test_an_already_red_candidate_is_removed_before_retrying_the_rest(self):
        verdicts = self.fire_results(
            [
                (
                    1,
                    "already red: py: the command exits 1 with no fault seeded (clean run)\n",
                ),
                (
                    0,
                    "fired: core (0.1 s)\n"
                    "guards: restored, every command passes again (0.1 s)\n",
                ),
            ]
        )
        self.assertTrue(verdicts["core"][0])
        self.assertFalse(verdicts["py"][0])
        self.assertIn("doesn't pass on the head", verdicts["py"][1])


class PatchTests(Fixture):
    """The patch takes out the product hunks and nothing else, and applies to the head."""

    def test_reverse_fix_keeps_multiline_test_module_attributes(self):
        attribute = '#[cfg_attr(\n    feature = "checks",\n    allow(dead_code)\n)]\n'
        base = BASE_LIB.replace("#[cfg(test)]", attribute + "#[cfg(test)]")
        self.write(PRODUCT, base)
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "test module attributes")
        git(self.root, "update-ref", "refs/remotes/origin/main", "HEAD")
        fixed = base.replace("    41\n", "    42\n").replace(
            "allow(dead_code)", "allow(unused)"
        )
        self.fix(lib=fixed)
        patch = TOOL["fix_patch"](TOOL["Branch"](self.root, "origin/main"))
        done = subprocess.run(
            ["git", "apply", "-"],
            cwd=self.root,
            input=patch,
            text=True,
            capture_output=True,
        )
        self.assertExit(done, 0)
        self.assertEqual(
            (self.root / PRODUCT).read_text(),
            fixed.replace("    42\n", "    41\n"),
            "reverting product behavior must retain the test module's changed attributes",
        )

    def test_reverted_keeps_hunks_inside_test_modules(self):
        base = ["a\n", "b\n", "mod t {\n", "x\n", "}\n"]
        head = ["a\n", "B\n", "mod t {\n", "x\n", "y\n", "}\n"]
        self.assertEqual(
            TOOL["reverted"](base, head, [(3, 6)]), [*base[:4], "y\n", "}\n"]
        )
        self.assertEqual(TOOL["reverted"](base, head, []), base)

    def test_the_emitted_patch_applies_to_the_head_and_leaves_the_base(self):
        self.fix(
            lib=FIXED_LIB.replace(
                "        assert_eq!", "        let _ = 1;\n        assert_eq!"
            )
        )
        self.write("crates/agentd/src/extra.rs", "pub fn new_api() {}\n")
        self.write(TESTS, TEST_FILE + "\n#[test]\nfn more() {}\n")
        branch = TOOL["Branch"](self.root, "origin/main")
        patch = TOOL["fix_patch"](branch)
        done = subprocess.run(
            ["git", "apply", "-"],
            cwd=self.root,
            input=patch,
            text=True,
            capture_output=True,
        )
        self.assertExit(done, 0)
        lib = (self.root / PRODUCT).read_text()
        self.assertIn("    41\n", lib)
        self.assertIn("let _ = 1;", lib, "the inline test's hunk stays in")
        self.assertFalse((self.root / "crates/agentd/src/extra.rs").exists())
        self.assertIn("fn more()", (self.root / TESTS).read_text())
        self.assertTrue((self.root / "changelog.d/7.fixed.md").exists())


class ProveTests(Fixture):
    """`prove` fires each candidate and keeps only what fails with the fix taken out."""

    def prove(self, *args: str):
        return self.run_script("prove", "--target-dir", str(self.top / "target"), *args)

    def test_a_test_the_fix_makes_pass_is_proven_and_emitted(self):
        self.fix()
        self.write(TESTS, TEST_FILE.replace("42()", "42_again()"))
        done = self.prove("--emit", "answer")
        self.assertExit(done, 0)
        self.assertIn("the_answer_is_42_again: proven", done.stdout)
        owner = self.root / REGISTRY_DIR / "answer.toml"
        entries = tomllib.loads(owner.read_text())["fault"]
        self.assertEqual([e["guard"] for e in entries], ["the_answer_is_42_again"])
        entry = entries[0]
        self.assertEqual(
            entry["run"],
            ["cargo", "test", "-p", "agentd", "--test", "regress", "--", "--exact",
             "the_answer_is_42_again"],
        )  # fmt: skip
        patch = (self.root / entry["patch"]).read_text()
        applies = subprocess.run(
            ["git", "apply", "--check", "-"], cwd=self.root, input=patch, text=True
        )
        self.assertEqual(applies.returncode, 0)
        self.assertIn("-    42", patch)
        self.assertExit(self.run_script("check"), 0)

    def test_without_emit_the_entries_are_printed(self):
        self.fix()
        self.write(TESTS, TEST_FILE.replace("42()", "42_again()"))
        done = self.prove()
        self.assertExit(done, 0)
        self.assertIn('guard = "the_answer_is_42_again"', done.stdout)
        self.assertFalse((self.root / REGISTRY_DIR / "answer.toml").exists())

    def test_a_partial_proof_fails_but_emits_the_completed_evidence(self):
        self.fix()
        self.write(
            TESTS,
            TEST_FILE
            + "\n#[test]\nfn fixed() {}\n// always: passes anyway\n#[test]\nfn always() {}\n",
        )
        done = self.prove("--emit", "partial")
        self.assertExit(done, 1)
        entries = tomllib.loads(
            (self.root / REGISTRY_DIR / "partial.toml").read_text()
        )["fault"]
        self.assertEqual([entry["guard"] for entry in entries], ["fixed"])
        self.assertExit(self.run_script("check"), 1)

    def test_all_ordinary_tests_must_be_proven(self):
        self.fix()
        self.write(
            TESTS, TEST_FILE + "\n#[test]\nfn first() {}\n#[test]\nfn second() {}\n"
        )
        self.assertExit(self.prove("--emit", "complete"), 0)
        self.assertExit(self.run_script("check"), 0)

    def test_a_selected_test_without_a_command_makes_the_proof_incomplete(self):
        self.fix()
        self.write(TESTS, TEST_FILE + "\n#[test]\nfn fixed() {}\n")
        self.write("crates/agentd/tests/unknown.rs", "#[test]\nfn unknown() {}\n")
        done = self.prove("--emit", "partial")
        self.assertExit(done, 1)
        self.assertIn("skipped unknown", done.stdout)
        entries = tomllib.loads(
            (self.root / REGISTRY_DIR / "partial.toml").read_text()
        )["fault"]
        self.assertEqual([entry["guard"] for entry in entries], ["fixed"])
        self.assertExit(self.run_script("check"), 1)

    def test_a_test_that_passes_on_the_base_is_no_proof(self):
        self.fix()
        self.write(
            TESTS, TEST_FILE + "\n// always: passes anyway\n#[test]\nfn always() {}\n"
        )
        done = self.prove()
        self.assertExit(done, 1)
        self.assertIn("always: passes with the fix taken out", done.stdout)
        self.assertIn("no test of this fix is proven", done.stdout)

    def test_a_base_that_does_not_build_is_not_a_failure(self):
        self.fix(lib=FIXED_LIB + "\npub fn new_api() {}\n")
        self.write(
            TESTS, TEST_FILE + "\n// uses: needs new_api\n#[test]\nfn uses() {}\n"
        )
        done = self.prove()
        self.assertExit(done, 1)
        self.assertIn("uses: the merge base with the tests doesn't build", done.stdout)

    def test_a_test_that_fails_on_the_head_is_reported(self):
        self.fix(lib=BASE_LIB + "\n// a fix that fixes nothing\n")
        self.write(TESTS, TEST_FILE.replace("42()", "42_again()"))
        done = self.prove()
        self.assertExit(done, 1)
        self.assertIn("the_answer_is_42_again: doesn't pass on the head", done.stdout)

    def test_a_branch_that_isnt_a_fix_proves_nothing_without_any(self):
        self.write(PRODUCT, FIXED_LIB)
        self.write(TESTS, TEST_FILE.replace("42()", "42_again()"))
        done = self.prove()
        self.assertExit(done, 0)
        self.assertIn("isn't a fix", done.stdout)
        self.assertIn("proven", self.prove("--any").stdout)


class EntryMatchingTests(unittest.TestCase):
    """A partial or malformed entry never provides a named failure proof."""

    def test_incomplete_entries_cannot_supply_a_proof(self):
        test = TOOL["Test"](
            "pytest", "test_case.py", "test_case.py::test_case", "test_case", 1, 2
        )
        entry = {
            "expect": "test-failed",
            "guard": test.guard,
            "run": ["python", "-m", "pytest", "-q", "-rA", test.guard],
        }
        self.assertTrue(TOOL["entry_proves"](test, entry, []))
        for field, value in (
            ("expect", None),
            ("guard", None),
            ("guard", ""),
            ("run", None),
            ("run", []),
            ("run", [["build"], [1]]),
            ("run", [["build"], "bad"]),
            ("run", [*entry["run"], 1]),
        ):
            with self.subTest(field=field, value=value):
                self.assertFalse(
                    TOOL["entry_proves"](test, {**entry, field: value}, [])
                )
        self.assertFalse(
            TOOL["entry_proves"](
                test, {"expect": "test-failed", "run": entry["run"]}, []
            )
        )
        self.assertTrue(
            TOOL["entry_proves"](
                test, {**entry, "run": [["build"], ["prepare"], entry["run"]]}, []
            )
        )

    def test_option_values_read_attached_and_missing_values(self):
        self.assertEqual(
            TOOL["option_values"](["--package"], ("-p", "--package")), [""]
        )
        for flag in ("-pagentd", "-p=agentd", "--package=agentd"):
            with self.subTest(flag=flag):
                self.assertEqual(
                    TOOL["option_values"]([flag], ("-p", "--package")), ["agentd"]
                )
        self.assertEqual(
            TOOL["option_values"](["--package-extra=agentd"], ("--package",)), []
        )

    def test_a_foreign_manifest_cannot_supply_a_named_rust_proof(self):
        test = TOOL["Test"]("rust", TESTS, "answer", "answer", 1, 2)
        targets = [
            {
                "package": "agentd",
                "name": "regress",
                "kind": ["test"],
                "src": TESTS,
                "features": [],
            }
        ]
        for manifest, expected in (
            ("Cargo.toml", True),
            ("crates/agentd/Cargo.toml", True),
            ("scratch/other/Cargo.toml", False),
        ):
            with self.subTest(manifest=manifest):
                run = [
                    "cargo",
                    "test",
                    "-pagentd",
                    "--manifest-path",
                    manifest,
                    "--test",
                    "regress",
                    "--",
                    "--exact",
                    "answer",
                ]
                self.assertEqual(
                    TOOL["entry_proves"](
                        test,
                        {"guard": "answer", "expect": "test-failed", "run": run},
                        targets,
                    ),
                    expected,
                )

    def test_extra_node_inputs_cannot_supply_the_named_files_proof(self):
        path = "bindings/microvms-js/__test__/answer.mjs"
        test = TOOL["Test"]("node", path, "answer", "answer", 1, 2)
        entry = {"guard": "answer", "expect": "test-failed"}
        self.assertTrue(
            TOOL["entry_proves"](
                test,
                {**entry, "run": ["node", "--test", path, "--test-reporter=tap"]},
                [],
            )
        )
        for run in (
            ["other", "--test", "--test-reporter=tap", path],
            ["node", "other", "--test-reporter=tap", path],
            ["node", "--test", path],
            ["node", "--test", "--test-reporter=tap", path, "--test-name-pattern"],
        ):
            with self.subTest(run=run):
                self.assertFalse(TOOL["entry_proves"](test, {**entry, "run": run}, []))
        for extra in ("decoy.cjs", "decoy.js", "decoy.mjs", "directory"):
            with self.subTest(extra=extra):
                run = ["node", "--test", "--test-reporter=tap", path, extra]
                self.assertFalse(
                    TOOL["entry_proves"](
                        test,
                        {"guard": "answer", "expect": "test-failed", "run": run},
                        [],
                    )
                )
        for option in (
            "--test-name-pattern",
            "--test-skip-pattern",
            "--test-reporter",
            "--test-reporter-destination",
            "--test-concurrency",
            "--test-timeout",
            "--test-shard",
        ):
            with self.subTest(option=option):
                run = ["node", "--test", "--test-reporter=tap", option, "fixture", path]
                self.assertTrue(
                    TOOL["entry_proves"](
                        test,
                        {"guard": "answer", "expect": "test-failed", "run": run},
                        [],
                    )
                )
        run = [
            "node",
            "--test",
            "--test-reporter=tap",
            "--test-name-pattern",
            "answer",
            path,
        ]
        self.assertTrue(
            TOOL["entry_proves"](
                test, {"guard": "answer", "expect": "test-failed", "run": run}, []
            )
        )

    def test_node_preloads_cannot_supply_another_sources_failure(self):
        path = "bindings/microvms-js/__test__/answer.mjs"
        test = TOOL["Test"]("node", path, "answer", "answer", 1, 2)
        for option in (
            "--import",
            "--require",
            "--loader",
            "--experimental-loader",
            "-r",
        ):
            forms = ([option, "./decoy.mjs"], [option + "=./decoy.mjs"])
            if option == "-r":
                forms += (["-r./decoy.mjs"],)
            for flags in forms:
                with self.subTest(flags=flags):
                    run = ["node", "--test", "--test-reporter=tap", *flags, path]
                    self.assertFalse(
                        TOOL["entry_proves"](
                            test,
                            {"guard": "answer", "expect": "test-failed", "run": run},
                            [],
                        )
                    )

    def test_semantic_selectors_preserve_existing_proof_commands(self):
        test = TOOL["Test"]("rust", TESTS, "answer", "answer", 1, 2)
        targets = [
            {
                "package": "agentd",
                "name": "regress",
                "kind": ["test"],
                "src": TESTS,
                "features": [],
            }
        ]
        for run in (
            [
                "cargo",
                "test",
                "--package=agentd",
                "--test=regress",
                "--",
                "--ignored",
                "--exact",
                "answer",
            ],
            [
                "cargo",
                "test",
                "-p",
                "agentd",
                "--features",
                "extra",
                "--test",
                "regress",
                "answer",
                "--",
                "--exact",
            ],
        ):
            with self.subTest(run=run):
                self.assertTrue(
                    TOOL["entry_proves"](
                        test,
                        {"guard": "answer", "expect": "test-failed", "run": run},
                        targets,
                    )
                )

        path = "bindings/microvms-py/tests/test_answer.py"
        test = TOOL["Test"]("pytest", path, f"{path}::test_answer", "test_answer", 1, 2)
        entry = {
            "guard": test.guard + "[one]",
            "expect": "test-failed",
            "run": ["python", "-m", "pytest", "-q", "-rA", path, "-k", "answer"],
        }
        self.assertTrue(TOOL["entry_proves"](test, entry, []))
        path = "bindings/microvms-js/__test__/answer.mjs"
        test = TOOL["Test"]("node", path, "answer", "answer", 1, 2)
        entry = {
            "guard": test.guard,
            "expect": "test-failed",
            "run": [
                "node",
                "--test",
                "--test-reporter=tap",
                "--test-name-pattern=answer",
                path,
            ],
        }
        self.assertTrue(TOOL["entry_proves"](test, entry, []))

    def test_conflicting_or_missing_rust_selectors_cannot_supply_a_proof(self):
        test = TOOL["Test"]("rust", TESTS, "answer", "answer", 1, 2)
        targets = [
            {
                "package": "agentd",
                "name": "regress",
                "kind": ["test"],
                "src": TESTS,
                "features": [],
            }
        ]
        command = [
            "cargo",
            "test",
            "-p",
            "agentd",
            "--test",
            "regress",
            "--",
            "--exact",
            "answer",
        ]
        runs = [
            command[1:],
            command[:6] + command[7:],
            command[:7] + ["answer"],
            command[:-1] + ["wrong"],
            command[:6] + ["--package", "another"] + command[6:],
            command[:6] + ["--bin", "regress"] + command[6:],
            command[:6] + ["--workspace"] + command[6:],
            ["cargo", "test", "--package"],
            command[:6] + ["--exact", "--", "answer"],
        ]
        runs += [
            command[:6] + [flag] + command[6:]
            for flag in ("--all-targets", "--tests", "--bins")
        ]
        for run in runs:
            with self.subTest(run=run):
                self.assertFalse(
                    TOOL["entry_proves"](
                        test,
                        {"guard": "answer", "expect": "test-failed", "run": run},
                        targets,
                    )
                )

    def test_implicit_library_selection_requires_one_runnable_target(self):
        test = TOOL["Test"](
            "rust", PRODUCT, "tests::answer", "answer", 1, 2, inline=True
        )
        library = {
            "package": "agentd",
            "name": "agentd",
            "kind": ["lib"],
            "src": PRODUCT,
            "features": [],
        }
        other = {
            "package": "agentd",
            "name": "regress",
            "kind": ["test"],
            "src": TESTS,
            "features": [],
        }
        run = ["cargo", "test", "-p", "agentd", "tests::answer", "--", "--exact"]
        entry = {"guard": test.guard, "expect": "test-failed", "run": run}
        self.assertTrue(TOOL["entry_proves"](test, entry, [library]))
        self.assertFalse(TOOL["entry_proves"](test, entry, [library, other]))
        self.assertFalse(
            TOOL["entry_proves"](test, entry, [library, {**other, "kind": ["bin"]}])
        )
        wrong = {
            **entry,
            "run": [
                "cargo",
                "test",
                "-p",
                "agentd",
                "--test",
                "not-the-library",
                "--",
                "--exact",
                test.guard,
            ],
        }
        self.assertFalse(TOOL["entry_proves"](test, wrong, [library]))
        explicit = {
            **entry,
            "run": [
                "cargo",
                "test",
                "-p",
                "agentd",
                "--lib",
                "--",
                "--exact",
                test.guard,
            ],
        }
        self.assertTrue(TOOL["entry_proves"](test, explicit, [library, other]))
        for kind in ("lib", "rlib", "cdylib", "staticlib"):
            with self.subTest(kind=kind):
                self.assertTrue(
                    TOOL["entry_proves"](
                        test, explicit, [{**library, "kind": [kind]}, other]
                    )
                )
                self.assertTrue(
                    TOOL["entry_proves"](test, entry, [{**library, "kind": [kind]}])
                )

    def test_binding_libraries_use_cargo_metadata_kinds(self):
        targets = TOOL["cargo_targets"](REPO)
        for package in ("microvms-py", "microvms-js"):
            with self.subTest(package=package):
                test = TOOL["Test"](
                    "rust",
                    f"bindings/{package}/src/lib.rs",
                    "tests::answer",
                    "answer",
                    1,
                    2,
                    inline=True,
                )
                command = TOOL["rust_command"](test, targets)
                self.assertIsNotNone(command)
                self.assertEqual(command[:5], ["cargo", "test", "-p", package, "--lib"])
                run = ["cargo", "test", "-p", package, "--", "--exact", test.guard]
                self.assertTrue(
                    TOOL["entry_proves"](
                        test,
                        {"guard": test.guard, "expect": "test-failed", "run": run},
                        targets,
                    )
                )

    def test_binary_source_proofs_select_the_binary_in_a_mixed_package(self):
        library = {
            "package": "demo",
            "name": "demo",
            "kind": ["lib"],
            "src": "crates/demo/src/lib.rs",
            "features": [],
        }
        for path in (
            "crates/demo/src/main.rs",
            "crates/demo/src/bin/worker.rs",
            "crates/demo/src/bin/worker/main.rs",
        ):
            with self.subTest(path=path):
                binary = {
                    "package": "demo",
                    "name": "worker",
                    "kind": ["bin"],
                    "src": path,
                    "features": [],
                }
                test = TOOL["Test"](
                    "rust", path, "tests::answer", "answer", 1, 3, inline=True
                )
                run = [
                    "cargo",
                    "test",
                    "-p",
                    "demo",
                    "--bin",
                    "worker",
                    "--",
                    "--exact",
                    test.guard,
                ]
                self.assertEqual(TOOL["rust_command"](test, [library, binary]), run)
                entry = {"guard": test.guard, "expect": "test-failed", "run": run}
                self.assertTrue(TOOL["entry_proves"](test, entry, [library, binary]))
                self.assertFalse(
                    TOOL["entry_proves"](
                        test,
                        {
                            **entry,
                            "run": [
                                "cargo",
                                "test",
                                "-p",
                                "demo",
                                "--lib",
                                "--",
                                "--exact",
                                test.guard,
                            ],
                        },
                        [library, binary],
                    )
                )
        test = TOOL["Test"](
            "rust",
            "crates/demo/src/bin/absent.rs",
            "tests::answer",
            "answer",
            1,
            3,
            inline=True,
        )
        self.assertIsNone(TOOL["rust_command"](test, [library, binary]))
        test = TOOL["Test"](
            "rust",
            "crates/demo/src/bin/worker/helper.rs",
            "helper::tests::answer",
            "answer",
            1,
            3,
            inline=True,
        )
        self.assertEqual(TOOL["module_path"](test.path), ["helper"])
        self.assertEqual(
            TOOL["rust_command"](test, [library, binary])[4:6], ["--bin", "worker"]
        )


class RegistryShapeTests(unittest.TestCase):
    """The emitted bindings commands are the ones the registry's bindings entries build with."""

    def test_the_build_commands_are_the_registrys(self):
        builds = []
        tables, problems = TOOL["FIRE"]["registry_tables"](REPO)
        self.assertEqual(problems, [])
        for entry in (table.data for table in tables):
            run = entry["run"]
            if entry.get("suite") == "bindings" and run and isinstance(run[0], list):
                builds.append(run[0])
        self.assertTrue(builds, "the registry has no bindings entry to compare with")
        known = [
            TOOL[name]
            for name in ("PY_BUILD", "JS_BUILD", "PARITY_PY_BUILD", "PARITY_JS_BUILD")
        ]
        for build in builds:
            if "maturin" in " ".join(build) or "napi" in build:
                self.assertIn(build, known)

    def test_rust_case_config_is_accepted_by_cargo(self):
        test = TOOL["parity_tests"](REPO, [TOOL["PARITY_SENTINEL"]])[0]
        run, _ = TOOL["parity_command"](test)
        argv = run[-1]
        configs = []
        for index, value in enumerate(argv):
            if value == "--config":
                configs += [value, argv[index + 1]]
        done = subprocess.run(
            ["cargo", "metadata", "--no-deps", "--format-version", "1", *configs],
            cwd=REPO,
            capture_output=True,
            text=True,
        )
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_a_shared_proof_requires_a_case_identifier(self):
        test = TOOL["Test"]("parity", "case.json", "guard", "case", 1, 2, surface="ts")
        with self.assertRaisesRegex(
            AssertionError, "a shared proof must name its case"
        ):
            TOOL["parity_command"](test)


class CandidateBuildTests(unittest.TestCase):
    def test_fixture_sources_choose_their_required_binding_builds(self):
        cases = [
            (
                "pytest",
                "bindings/microvms-py/tests/test_parity_cases.py",
                "PARITY_PY_BUILD",
            ),
            ("pytest", "bindings/microvms-py/tests/test_control.py", "PY_BUILD"),
            (
                "node",
                "bindings/microvms-js/__test__/parity_cases.mjs",
                "PARITY_JS_BUILD",
            ),
            ("node", "bindings/microvms-js/__test__/control.mjs", "JS_BUILD"),
        ]
        for kind, path, expected in cases:
            with self.subTest(kind=kind, path=path):
                guard = (
                    f"{path}::test_changed" if kind == "pytest" else "a changed test"
                )
                selected = TOOL["Test"](kind, path, guard, "changed", 1, 1)
                with patch.dict(
                    inspect.unwrap(TOOL["candidates"]).__globals__,
                    {"registry_texts": lambda *_args, **_kwargs: {}},
                ):
                    made, skipped = TOOL["candidates"](
                        SimpleNamespace(root=REPO), [selected]
                    )
                runner = (
                    ["python", "-m", "pytest", "-q", "-rA", guard]
                    if kind == "pytest"
                    else ["node", "--test", "--test-reporter=tap", path]
                )
                self.assertEqual(skipped, [])
                self.assertEqual(len(made), 1)
                self.assertEqual(made[0].run, [TOOL[expected], runner])
                self.assertEqual(made[0].suite, "bindings")
                self.assertEqual(made[0].test, selected)


class RegistryReaderTests(unittest.TestCase):
    """The branch's and the base's entries are read with the fire's loader, so a family's rows
    are entries here as they are to `list` and `fire`."""

    def test_a_familys_rows_are_entries(self):
        family = (
            '[[family]]\nguard = "g"\nrun = ["cargo", "test", "--", "--exact", "g"]\n'
            'expect = "test-failed"\nsuite = "rust"\n\n[[family.fault]]\nid = "row"\n'
            'transform = { file = "x", replace = "a", with = "b" }\n'
        )
        entries = TOOL["registry"]({f"{REGISTRY_DIR}/a.toml": family})
        self.assertIn("row", entries)
        self.assertEqual(entries["row"]["guard"], "g")


if __name__ == "__main__":
    unittest.main()
