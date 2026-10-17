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
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=self.root,
            capture_output=True,
            text=True,
            env=clean_env(
                PATH=os.pathsep.join([str(self.bin), os.environ.get("PATH", "")]),
                TMPDIR=str(self.top / "tmp"),
            ),
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

    def entry(self, guard: str, owner: str = "fix") -> None:
        self.write(
            f"{REGISTRY_DIR}/{owner}.toml",
            f'# ── {owner} ──\n\n[[fault]]\nid = "proof"\nguard = "{guard}"\n'
            'run = ["cargo", "test"]\nexpect = "test-failed"\nsuite = "rust"\n'
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

    def test_the_parity_sentinel_refuses_a_finder_that_reads_no_cases(self):
        from unittest.mock import patch

        with patch.dict(
            TOOL["sentinel_problems"].__globals__, {"parity_tests": lambda *_: []}
        ):
            problems = TOOL["sentinel_problems"](self.root)
        self.assertIn(
            "the parity finder doesn't find every surface of wrap-dockerfile/sentinel",
            problems,
        )

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

        with patch.dict(TOOL["prove"].__globals__, {"fire": fire}):
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

        with patch.dict(TOOL["prove"].__globals__, {"fire": fire}):
            code = TOOL["prove"](branch, SimpleNamespace(any=False, emit="adapter"))
        self.assertEqual(code, 0)
        entries = tomllib.loads(
            (self.root / REGISTRY_DIR / "adapter.toml").read_text()
        )["fault"]
        self.assertEqual(len(entries), 1)
        self.assertIn("test_a_case[", entries[0]["guard"])
        self.assertExit(self.run_script("check"), 0)

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

        with patch.dict(TOOL["prove"].__globals__, {"fire": fire}):
            code = TOOL["prove"](branch, SimpleNamespace(any=False, emit=None))
        self.assertEqual(code, 1)


class PatchTests(Fixture):
    """The patch takes out the product hunks and nothing else, and applies to the head."""

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
