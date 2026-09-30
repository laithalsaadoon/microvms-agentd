# SPDX-License-Identifier: Apache-2.0
"""Tests for `tools/check-mutmut.py`: which functions a change touches, which suites measure
them, how mutmut's verdicts are counted, and the ratchet against the base.

Each case builds a throwaway repo whose `origin/main` is its first commit and runs the gate's
`main` in-process over it, from a directory outside any repo, so mutmut can credit these tests
with the gate's own functions and a git call that lost its `cwd` fails. A fake mutmut stands in
for the real one: it reads the `setup.cfg` the gate wrote, and for each function of each source
it names writes one verdict per exit code listed in a `# fake:` comment inside the function, for
the mutants the run's names match. The gate runs with git's identity unset and
`user.useConfigOnly` on, as in a CI checkout, and the repo's `diff.renames` off. One case runs
the real mutmut over a real suite, and one compares the hashes with mutmut's own function,
which is how the fake's contract is held to mutmut's.
"""

import contextlib
import io
import json
import os
import runpy
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

import yaml

SCRIPT = Path(__file__).with_name("check-mutmut.py")
GATE = runpy.run_path(str(SCRIPT), run_name="tools.check-mutmut")
Failure = GATE["Failure"]

FAKE_MUTMUT = """#!{python}
import ast, configparser, fnmatch, json, os, re, signal, sys, time
from pathlib import Path

def log(*entry):
    with open(os.environ["FAKE_MUTMUT_LOG"], "a") as out:
        out.write(json.dumps(entry) + "\\n")

log("argv", Path.cwd().name, *sys.argv[1:])
log("env", [k for k in {leaks!r} if k in os.environ])
if sys.argv[1] == "show":
    print("--- the diff of", sys.argv[2])
    sys.exit(0)
if os.environ.get("FAKE_MUTMUT_TERM"):
    os.kill(os.getppid(), signal.SIGTERM)
    time.sleep(10)
    sys.exit(0)
globs = [a for a in sys.argv[2:] if "__mutmut_" in a]
config = configparser.ConfigParser()
config.read("setup.cfg")
log("config", dict(config["mutmut"]))
for path in config["mutmut"]["source_paths"].split("\\n")[1:]:
    path = path.strip()
    source = Path(path).read_text()
    module = os.environ.get("FAKE_MUTMUT_MODULE") or path.removesuffix(".py").replace("/", ".")
    verdicts = {{}}
    for node in ast.parse(source).body:
        if not isinstance(node, ast.FunctionDef):
            continue
        found = re.search(r"# fake: (.*)", ast.get_source_segment(source, node))
        for n, code in enumerate(found.group(1).split() if found else [], 1):
            name = f"{{module}}.x_{{node.name}}__mutmut_{{n}}"
            run = any(fnmatch.fnmatchcase(name, g) for g in globs)
            verdicts[name] = None if code == "none" or not run else int(code)
    Path("mutants", path).write_text(source)
    if not os.environ.get("FAKE_MUTMUT_NO_META"):
        hashes = {{"x_add": "0" * 12}} if os.environ.get("FAKE_MUTMUT_HASH") else {{}}
        meta = {{"exit_code_by_key": verdicts, "hash_by_function_name": hashes}}
        Path("mutants", path + ".meta").write_text(json.dumps(meta))
print(os.environ.get("FAKE_MUTMUT_SAY", "done"), file=sys.stderr)
sys.exit(int(os.environ.get("FAKE_MUTMUT_EXIT", "0")))
"""

# Stands in for uv: records how it was called, then runs the fake mutmut with what follows
# `mutmut`.
FAKE_UV = """#!{python}
import json, os, sys
with open(os.environ["FAKE_MUTMUT_LOG"], "a") as out:
    out.write(json.dumps(["uv", *sys.argv[1:6]]) + "\\n")
os.execv({mutmut!r}, [{mutmut!r}, *sys.argv[6:]])
"""

SUITE = 'import runpy\nCALC = runpy.run_path("tools/calc.py", run_name="tools.calc")\n'
# A CI checkout's git: no identity, and no guessing one.
NO_IDENTITY = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "user.useConfigOnly",
    "GIT_CONFIG_VALUE_0": "true",
}


def calc(codes="1 1 0", body="return a + b"):
    """tools/calc.py with an `add` whose fake mutants end with `codes`, and an unchanging `mul`."""
    return textwrap.dedent(
        f"""\
        LIMIT = 1


        def add(a, b):
            # fake: {codes}
            {body}


        def mul(a, b):
            # fake: 1
            return a * b
        """
    )


class Repo:
    """A throwaway repo whose `origin/main` is its first commit, with a fake mutmut beside it."""

    def __init__(self, case: unittest.TestCase, files: dict[str, str]):
        directory = tempfile.TemporaryDirectory()
        case.addCleanup(directory.cleanup)
        self.top = Path(directory.name)
        self.root = self.top / "repo"
        self.root.mkdir()
        self.log = self.top / "mutmut.log"
        self.log.touch()
        self.fake = self.top / "mutmut"
        self.fake.write_text(
            FAKE_MUTMUT.format(python=sys.executable, leaks=GATE["LEAKS"])
        )
        self.fake.chmod(0o755)
        self.git("init", "-q", "-b", "main")
        self.git("config", "diff.renames", "false")
        self.commit(files, "base")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        # The scratch directories land here, where cleanup takes them if a case fails early.
        self.scratch = self.top / "tmp"
        self.scratch.mkdir()
        patch = mock.patch.object(tempfile, "tempdir", str(self.scratch))
        patch.start()
        case.addCleanup(patch.stop)

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    def write(self, files: dict[str, str]) -> None:
        for name, text in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def commit(self, files: dict[str, str], message: str = "head") -> None:
        self.write(files)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def run(self, *args: str, env=None, jobs=True, mutmut=True) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        argv = ["--root", str(self.root), *args]
        argv += ["--mutmut", str(self.fake)] if mutmut else []
        argv += ["--jobs", "3"] if jobs else []
        fakes = NO_IDENTITY | {"FAKE_MUTMUT_LOG": str(self.log)} | (env or {})
        here = os.getcwd()
        os.chdir(self.top)
        try:
            with mock.patch.dict(os.environ, fakes):
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = GATE["main"](argv)
        finally:
            os.chdir(here)
        return code, out.getvalue(), err.getvalue()

    def calls(self, kind: str) -> list:
        entries = [json.loads(line) for line in self.log.read_text().splitlines()]
        return [e[1:] for e in entries if e[0] == kind]

    def worktrees(self) -> int:
        return len(self.git("worktree", "list").splitlines())


class RatchetTests(unittest.TestCase):
    def repo(self, codes="1 1 0") -> Repo:
        return Repo(self, {"tools/calc.py": calc(codes), "tools/test_calc.py": SUITE})

    def test_a_function_whose_survivors_grow_fails_and_shows_each(self):
        repo = self.repo()
        repo.commit({"tools/calc.py": calc("0 0 1", "return b + a")})
        code, out, err = repo.run()
        self.assertEqual(code, 1, out + err)
        self.assertEqual(
            out,
            "  tools/calc.py, measured by tools/test_calc.py:\n"
            "    add: 2 of 3 survive (1 on the base): MORE\n",
        )
        self.assertIn(
            "\ncheck-mutmut: add in tools/calc.py has 2 surviving mutants, 1 on the base."
            " Strengthen the test that should catch them, or mark one no test can tell from the"
            " original with a no-mutate pragma and its reason:\n"
            "\n  tools.calc.x_add__mutmut_1: survived\n"
            "--- the diff of tools.calc.x_add__mutmut_1\n"
            "\n  tools.calc.x_add__mutmut_2: survived\n"
            "--- the diff of tools.calc.x_add__mutmut_2\n",
            err,
        )
        self.assertNotIn("mutmut_3", err)

    def test_a_function_whose_survivors_dont_grow_passes(self):
        repo = self.repo()
        repo.commit({"tools/calc.py": calc("1 0 1", "return b + a")})
        code, out, err = repo.run()
        self.assertEqual(code, 0, err)
        self.assertEqual(
            out,
            "  tools/calc.py, measured by tools/test_calc.py:\n"
            "    add: 1 of 3 survive (1 on the base)\n"
            "check-mutmut: no changed function has more surviving mutants than on the base\n",
        )

    def test_each_side_mutates_only_the_changed_functions_with_their_suites(self):
        repo = self.repo()
        repo.commit({"tools/calc.py": calc("1 0 1", "return b + a")})
        repo.run()
        self.assertEqual(
            repo.calls("argv"),
            [
                ["head", "run", "--max-children", "3", "tools.calc.x_add__mutmut_*"],
                ["base", "run", "--max-children", "3", "tools.calc.x_add__mutmut_*"],
            ],
        )
        config = {
            "source_paths": "\ntools/calc.py",
            "pytest_add_cli_args_test_selection": "\ntools/test_calc.py",
            "pytest_add_cli_args": "\n-p\nno:cacheprovider",
            "use_git_change_detection": "false",
        }
        self.assertEqual(repo.calls("config"), [[config], [config]])

    def test_two_scripts_and_their_suites_are_named_on_each_side(self):
        files = {
            "tools/calc.py": calc(),
            "tools/test_calc.py": SUITE,
            "tools/test_a_calc.py": SUITE,
        }
        repo = Repo(self, files)
        repo.commit(
            {
                "tools/calc.py": calc("1 0 1", "return b + a"),
                "tools/new.py": "def f():\n    # fake: 1\n    return 1\n",
                "tools/test_new.py": 'X = "tools.new"\n',
            }
        )
        code, out, err = repo.run()
        self.assertEqual(code, 0, err)
        self.assertIn(
            "  tools/calc.py, measured by tools/test_a_calc.py, tools/test_calc.py:\n"
            "    add: 1 of 3 survive (1 on the base)\n"
            "  tools/new.py, measured by tools/test_new.py:\n"
            "    f: 0 of 1 survive (new)\n",
            out,
        )
        head, base = [c[0] for c in repo.calls("config")]
        self.assertEqual(head["source_paths"], "\ntools/calc.py\ntools/new.py")
        self.assertEqual(
            head["pytest_add_cli_args_test_selection"],
            "\ntools/test_a_calc.py\ntools/test_calc.py\ntools/test_new.py",
        )
        self.assertEqual(base["source_paths"], "\ntools/calc.py")
        self.assertEqual(
            base["pytest_add_cli_args_test_selection"],
            "\ntools/test_a_calc.py\ntools/test_calc.py",
        )

    def test_a_new_function_starts_from_none_and_the_base_isnt_run(self):
        repo = self.repo()
        repo.commit(
            {
                "tools/calc.py": calc()
                + "\n\ndef sub(a, b):\n    # fake: 1 0\n    return a - b\n"
            }
        )
        code, out, err = repo.run()
        self.assertEqual(code, 1, out + err)
        self.assertEqual(
            out,
            "  tools/calc.py, measured by tools/test_calc.py:\n    sub: 1 of 2 survive (new): MORE\n",
        )
        self.assertIn(
            "check-mutmut: sub in tools/calc.py has 1 surviving mutants, 0 on the base.",
            err,
        )
        self.assertEqual([c[0] for c in repo.calls("argv")], ["head", "head"])

    def test_unreached_mutants_are_listed_and_not_counted(self):
        repo = self.repo()
        repo.commit({"tools/calc.py": calc("33 5 0", "return b + a")})
        code, out, err = repo.run()
        self.assertEqual(code, 0, err)
        self.assertIn(
            "    add: 1 of 3 survive (1 on the base), and 2 no in-process test reached aren't counted\n",
            out,
        )

    def test_a_timeout_and_an_unknown_ending_count_and_the_caught_ones_dont(self):
        repo = self.repo("1 1 1 1 1 1 1 1 1")
        repo.commit(
            {"tools/calc.py": calc("1 3 37 -9 -11 34 36 99 24", "return b + a")}
        )
        code, out, err = repo.run()
        self.assertEqual(code, 1, out + err)
        self.assertIn("    add: 3 of 9 survive (0 on the base): MORE\n", out)
        self.assertIn("tools.calc.x_add__mutmut_7: timeout\n", err)
        self.assertIn("tools.calc.x_add__mutmut_8: suspicious\n", err)
        self.assertIn("tools.calc.x_add__mutmut_9: timeout\n", err)

    def test_every_status_mutmut_names_is_read_as_it_means(self):
        counted = {"survived", "timeout", "suspicious"}
        for code, status in GATE["STATUS"].items():
            data = {"exit_code_by_key": {"tools.calc.x_add__mutmut_1": code}}
            with self.subTest(code=code, status=status):
                if code in GATE["UNREAD"]:
                    with self.assertRaises(Failure):
                        GATE["tally"](data, "tools/calc.py", "x_add")
                    continue
                found = GATE["tally"](data, "tools/calc.py", "x_add")
                self.assertEqual(
                    (found.total, len(found.counted), found.unreached),
                    (1, int(status in counted), int(status == "no tests")),
                )

    def test_a_mutant_the_run_left_without_a_verdict_fails(self):
        for codes in ("0 none", "0 2"):
            with self.subTest(codes=codes):
                repo = self.repo()
                repo.commit({"tools/calc.py": calc(codes, "return b + a")})
                code, out, err = repo.run()
                self.assertEqual(code, 1, out + err)
                self.assertTrue(
                    err.endswith(
                        "check-mutmut: tools.calc.x_add__mutmut_2 has no verdict, so no count for"
                        " add in tools/calc.py can be read\n"
                    ),
                    err,
                )

    def test_mutmut_failing_over_mutants_fails_with_the_end_of_its_log(self):
        repo = self.repo()
        repo.commit({"tools/calc.py": calc("1 1 1", "return b + a")})
        code, out, err = repo.run(
            env={"FAKE_MUTMUT_EXIT": "1", "FAKE_MUTMUT_SAY": "failed\rto collect stats"}
        )
        self.assertEqual(code, 1, out + err)
        self.assertIn("check-mutmut: mutmut exited 1 in ", err)
        self.assertTrue(
            err.endswith("/head; the end of its log:\nfailed\nto collect stats\n"), err
        )

    def test_mutmut_failing_before_it_gave_every_verdict_fails_with_its_log(self):
        repo = self.repo()
        repo.commit({"tools/calc.py": calc("1 none", "return b + a")})
        code, out, err = repo.run(
            env={"FAKE_MUTMUT_EXIT": "1", "FAKE_MUTMUT_SAY": "stopped"}
        )
        self.assertEqual(code, 1, out + err)
        self.assertTrue(err.endswith("/head; the end of its log:\nstopped\n"), err)
        self.assertNotIn("has no verdict", err)

    def test_mutmut_refusing_names_that_match_no_mutant_passes(self):
        # A new function with nothing to mutate: mutmut exits 1 on names that match no mutant.
        repo = self.repo()
        repo.commit(
            {"tools/calc.py": calc() + "\n\ndef sub(a, b):\n    return a - b\n"}
        )
        code, out, err = repo.run(env={"FAKE_MUTMUT_EXIT": "1"})
        self.assertEqual(code, 0, err)
        self.assertIn("    sub: 0 of 0 survive (new)\n", out)

    def test_mutmut_failing_before_it_wrote_results_fails_with_its_log(self):
        repo = self.repo()
        repo.commit({"tools/calc.py": calc("1", "return b + a")})
        code, out, err = repo.run(
            env={
                "FAKE_MUTMUT_EXIT": "2",
                "FAKE_MUTMUT_NO_META": "1",
                "FAKE_MUTMUT_SAY": "boom",
            }
        )
        self.assertEqual(code, 1, out + err)
        self.assertIn("mutmut exited 2 in ", err)
        self.assertTrue(err.endswith("/head; the end of its log:\nboom\n"), err)

    def test_no_results_from_a_run_that_passed_fails(self):
        repo = self.repo()
        repo.commit({"tools/calc.py": calc("1", "return b + a")})
        code, out, err = repo.run(env={"FAKE_MUTMUT_NO_META": "1"})
        self.assertEqual(code, 1, out + err)
        self.assertIn("check-mutmut: mutmut wrote no results for tools/calc.py: ", err)
        self.assertIn("/head/mutants/tools/calc.py.meta: FileNotFoundError(", err)

    def test_results_that_name_another_modules_mutants_fail(self):
        repo = self.repo()
        repo.commit({"tools/calc.py": calc("1", "return b + a")})
        code, out, err = repo.run(env={"FAKE_MUTMUT_MODULE": "tools.other"})
        self.assertEqual(code, 1, out + err)
        self.assertTrue(
            err.endswith(
                "/head/mutants/tools/calc.py.meta names no mutant of tools/calc.py as"
                " `tools.calc.x<function>__mutmut_<n>`\n"
            ),
            err,
        )

    def test_results_that_name_no_mutant_fail(self):
        # The floor: no function of the script has a mutant, so the results name nothing.
        repo = Repo(
            self,
            {
                "tools/calc.py": "def add(a, b):\n    return a\n",
                "tools/test_calc.py": SUITE,
            },
        )
        repo.commit({"tools/calc.py": "def add(a, b):\n    return b\n"})
        code, out, err = repo.run()
        self.assertEqual(code, 1, out + err)
        self.assertTrue(
            err.endswith(
                "/head/mutants/tools/calc.py.meta names no mutant of tools/calc.py as"
                " `tools.calc.x<function>__mutmut_<n>`\n"
            ),
            err,
        )

    def test_hashes_that_arent_this_scripts_fail(self):
        repo = self.repo()
        repo.commit({"tools/calc.py": calc("1", "return b + a")})
        code, out, err = repo.run(env={"FAKE_MUTMUT_HASH": "1"})
        self.assertEqual(code, 1, out + err)
        self.assertTrue(
            err.endswith(
                "check-mutmut: mutmut hashes add in tools/calc.py differently than this script"
                " does, so what it reads as changed isn't what mutmut sees: restate"
                " `function_hashes` from the pinned mutmut's `compute_function_hashes`\n"
            ),
            err,
        )


class SelectionTests(unittest.TestCase):
    def test_no_change_passes_without_running_mutmut(self):
        repo = Repo(self, {"tools/calc.py": calc(), "tools/test_calc.py": SUITE})
        self.assertEqual(
            repo.run(),
            (0, "check-mutmut: no script in tools/ changed against origin/main\n", ""),
        )
        self.assertEqual(repo.calls("argv"), [])

    def test_a_change_outside_the_functions_mutates_nothing(self):
        repo = Repo(self, {"tools/calc.py": calc(), "tools/test_calc.py": SUITE})
        repo.commit({"tools/calc.py": calc().replace("LIMIT = 1", "LIMIT = 2")})
        self.assertEqual(
            repo.run(),
            (
                0,
                "check-mutmut: tools/calc.py: no function changed, so nothing to mutate\n",
                "",
            ),
        )
        self.assertEqual(repo.calls("argv"), [])

    def test_a_comment_or_a_move_is_no_change(self):
        repo = Repo(self, {"tools/calc.py": calc(), "tools/test_calc.py": SUITE})
        moved = calc("2 2 2").replace("LIMIT = 1\n", "") + "\nLIMIT = 1\n"
        repo.commit({"tools/calc.py": moved})
        self.assertEqual(
            repo.run(),
            (
                0,
                "check-mutmut: tools/calc.py: no function changed, so nothing to mutate\n",
                "",
            ),
        )

    def test_suites_ignored_files_and_other_directories_arent_scripts(self):
        repo = Repo(
            self,
            {
                "tools/calc.py": calc(),
                "tools/test_calc.py": SUITE,
                ".gitignore": "tools/scratch.py\n",
            },
        )
        repo.commit(
            {
                "tools/test_calc.py": SUITE + "X = 1\n",
                "tools/fixtures/f.py": "def f():\n    return 1\n",
                "other.py": "def f():\n    return 1\n",
            }
        )
        repo.write(
            {
                "tools/scratch.py": "X = 1\n",
                "notes.py": "X = 1\n",
                "tools/fixtures/g.py": "X = 1\n",
            }
        )
        self.assertEqual(
            repo.run("--detect"),
            (0, "python=false\n", "no script changes against origin/main\n"),
        )

    def test_detect_names_each_changed_script_and_ignores_a_deleted_one(self):
        repo = Repo(
            self,
            {
                "tools/calc.py": calc(),
                "tools/gone.py": "GONE = 1\n",
                "tools/test_calc.py": SUITE,
            },
        )
        repo.git("rm", "-q", "tools/gone.py")
        repo.commit({"tools/calc.py": calc("2"), "tools/my sums.py": "SUMS = 2\n"})
        repo.write({"tools/new.py": "X = 1\n"})
        self.assertEqual(
            repo.run("--detect"),
            (0, "python=true\n", "tools/calc.py\ntools/my sums.py\ntools/new.py\n"),
        )

    def test_a_base_git_cant_resolve_fails(self):
        repo = Repo(self, {"tools/calc.py": calc(), "tools/test_calc.py": SUITE})
        code, out, err = repo.run("--base", "origin/nothing", "--detect")
        self.assertEqual((code, out), (1, ""))
        self.assertTrue(
            err.startswith("check-mutmut: git merge-base origin/nothing HEAD failed: "),
            err,
        )
        self.assertNotIn("\n", err[:-1])

    def test_an_uncommitted_and_an_untracked_change_are_measured_and_the_index_is_left(
        self,
    ):
        repo = Repo(self, {"tools/calc.py": calc(), "tools/test_calc.py": SUITE})
        repo.write(
            {
                "tools/calc.py": calc("0 0 0", "return b + a"),
                "tools/new.py": "def f():\n    # fake: 0\n    return 1\n",
                "tools/test_new.py": 'X = "tools.new"\n',
            }
        )
        repo.git("add", "tools/calc.py")
        code, out, err = repo.run()
        self.assertEqual(code, 1, out + err)
        self.assertEqual(
            out,
            "  tools/calc.py, measured by tools/test_calc.py:\n"
            "    add: 3 of 3 survive (1 on the base): MORE\n"
            "  tools/new.py, measured by tools/test_new.py:\n"
            "    f: 1 of 1 survive (new): MORE\n",
        )
        self.assertEqual(
            repo.git("status", "--porcelain"),
            "M  tools/calc.py\n?? tools/new.py\n?? tools/test_new.py\n",
        )

    def test_a_renamed_script_is_compared_with_its_old_path(self):
        repo = Repo(self, {"tools/calc.py": calc(), "tools/test_calc.py": SUITE})
        repo.git("mv", "tools/calc.py", "tools/sums.py")
        repo.commit(
            {
                "tools/sums.py": calc("1 0 1", "return b + a"),
                "tools/test_calc.py": SUITE.replace("calc", "sums"),
            }
        )
        code, out, err = repo.run()
        self.assertEqual(code, 0, err)
        self.assertIn(
            "  tools/sums.py, measured by tools/test_calc.py:\n    add: 1 of 3 survive (1 on the base)\n",
            out,
        )
        configs = [c[0]["source_paths"] for c in repo.calls("config")]
        self.assertEqual(configs, ["\ntools/sums.py", "\ntools/calc.py"])

    def test_a_script_no_suite_names_is_reported_and_not_mutated(self):
        repo = Repo(
            self,
            {"tools/calc.py": calc(), "tools/test_calc.py": "X = 'tools/calc.py'\n"},
        )
        repo.commit({"tools/calc.py": calc("0", "return b + a")})
        self.assertEqual(
            repo.run(),
            (
                0,
                "check-mutmut: tools/calc.py: no suite loads it as `tools.calc`, so its 1 changed"
                " functions aren't mutated\n",
                "",
            ),
        )
        self.assertEqual(repo.calls("argv"), [])

    def test_a_suite_that_stops_naming_its_script_fails(self):
        repo = Repo(
            self,
            {
                "tools/calc.py": calc(),
                "tools/test_calc.py": SUITE,
                "tools/test_b_calc.py": SUITE,
            },
        )
        repo.commit(
            {
                "tools/calc.py": calc("0", "return b + a"),
                "tools/test_calc.py": "X = 1\n",
                "tools/test_b_calc.py": "X = 1\n",
            }
        )
        self.assertEqual(
            repo.run(),
            (
                1,
                "",
                "check-mutmut: tools/test_b_calc.py, tools/test_calc.py loaded tools/calc.py under its"
                " module name on the base, and no suite loads tools/calc.py as `tools.calc`, so the"
                " change stopped measuring it\n",
            ),
        )

    def test_a_function_the_base_didnt_measure_isnt_held_to_a_count(self):
        repo = Repo(self, {"tools/calc.py": calc(), "tools/test_calc.py": "X = 1\n"})
        repo.commit(
            {"tools/calc.py": calc("0 0", "return b + a"), "tools/test_calc.py": SUITE}
        )
        code, out, err = repo.run()
        self.assertEqual(code, 0, err)
        self.assertIn("    add: 2 of 2 survive (not measured on the base)\n", out)
        self.assertEqual([c[0] for c in repo.calls("argv")], ["head"])

    def test_a_suite_that_doesnt_parse_is_passed_over(self):
        bad = SUITE + "def (:\n"
        repo = Repo(
            self,
            {
                "tools/calc.py": calc(),
                "tools/test_a_calc.py": bad,
                "tools/test_calc.py": SUITE,
            },
        )
        repo.commit(
            {
                "tools/calc.py": calc("1 0 1", "return b + a"),
                "tools/test_a_calc.py": bad + "\n",
            }
        )
        code, out, err = repo.run()
        self.assertEqual(code, 0, err)
        self.assertIn(
            "  tools/calc.py, measured by tools/test_calc.py:\n    add: 1 of 3 survive (1 on the base)\n",
            out,
        )

    def test_an_inherited_environment_reaches_neither_git_nor_mutmut(self):
        repo = Repo(self, {"tools/calc.py": calc(), "tools/test_calc.py": SUITE})
        other = Repo(self, {"tools/calc.py": calc(), "tools/test_calc.py": SUITE})
        repo.commit({"tools/calc.py": calc("0 0 0", "return b + a")})
        before = other.git("worktree", "list")
        leaks = {
            "GIT_DIR": str(other.root / ".git"),
            "GIT_INDEX_FILE": str(other.root / ".git/index"),
            "VIRTUAL_ENV": str(other.top),
        }
        code, out, err = repo.run(env=leaks)
        self.assertEqual(code, 1, out + err)
        self.assertIn("    add: 3 of 3 survive (1 on the base): MORE\n", out)
        self.assertEqual(other.git("worktree", "list"), before)
        self.assertEqual(repo.calls("env"), [[[]]] * 5)
        self.assertEqual(
            [c[:2] for c in repo.calls("argv")],
            [
                ["head", "run"],
                ["base", "run"],
                ["head", "show"],
                ["head", "show"],
                ["head", "show"],
            ],
        )

    def test_the_default_mutmut_is_this_repos_dev_group_and_the_default_jobs_are_mutmuts(
        self,
    ):
        repo = Repo(self, {"tools/calc.py": calc(), "tools/test_calc.py": SUITE})
        repo.commit({"tools/calc.py": calc("1 0 1", "return b + a")})
        bin_dir = repo.top / "bin"
        bin_dir.mkdir()
        uv = bin_dir / "uv"
        uv.write_text(FAKE_UV.format(python=sys.executable, mutmut=str(repo.fake)))
        uv.chmod(0o755)
        path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
        code, out, err = repo.run(env={"PATH": path}, jobs=False, mutmut=False)
        self.assertEqual(code, 0, out + err)
        home = str(SCRIPT.resolve().parent.parent)
        self.assertEqual(
            repo.calls("uv"), [["run", "--locked", "--project", home, "mutmut"]] * 2
        )
        self.assertEqual(
            repo.calls("argv"),
            [
                ["head", "run", "tools.calc.x_add__mutmut_*"],
                ["base", "run", "tools.calc.x_add__mutmut_*"],
            ],
        )

    def test_help_opens_with_the_docstrings_first_line(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as caught:
            GATE["main"](["--help"])
        self.assertEqual(caught.exception.code, 0)
        lines = GATE["__doc__"].splitlines()
        text = " ".join(out.getvalue().split())
        self.assertIn(" ".join(lines[0].split()), text)
        self.assertNotIn(" ".join(lines[1].split()), text)


class ScratchTests(unittest.TestCase):
    def test_the_worktrees_and_the_scratch_directory_go_after_a_run(self):
        repo = Repo(self, {"tools/calc.py": calc(), "tools/test_calc.py": SUITE})
        repo.commit({"tools/calc.py": calc("1 0 1", "return b + a")})
        code, _, err = repo.run()
        self.assertEqual(code, 0, err)
        self.assertEqual(repo.worktrees(), 1)
        self.assertEqual(list(repo.scratch.iterdir()), [])

    def test_keep_leaves_them_and_says_where(self):
        repo = Repo(self, {"tools/calc.py": calc(), "tools/test_calc.py": SUITE})
        repo.commit({"tools/calc.py": calc("1 0 1", "return b + a")})
        code, out, err = repo.run("--keep")
        self.assertEqual(code, 0)
        [work] = repo.scratch.iterdir()
        self.assertTrue(work.name.startswith("check-mutmut-"), work)
        merge_base = repo.git("rev-parse", "origin/main").strip()
        self.assertEqual(
            err,
            f"check-mutmut: mutating 1 changed functions against origin/main (merge base"
            f" {merge_base[:12]}) in {work}\n",
        )
        self.assertTrue(
            out.endswith(f"check-mutmut: the scratch directory is {work}\n"), out
        )
        self.assertEqual(repo.worktrees(), 3)
        head = work / "head"
        self.assertEqual(
            (head / "tools/calc.py").read_text(), calc("1 0 1", "return b + a")
        )
        self.assertEqual((head / "mutants/tools/test_calc.py").read_text(), SUITE)
        self.assertEqual((work / "base/tools/calc.py").read_text(), calc())
        for side in ("head", "base"):
            repo.git("worktree", "remove", "--force", str(work / side / "mutants"))

    def test_the_sigterm_handler_is_put_back(self):
        before = signal.getsignal(signal.SIGTERM)
        repo = Repo(self, {"tools/calc.py": calc(), "tools/test_calc.py": SUITE})
        repo.commit({"tools/calc.py": calc("1 0 1", "return b + a")})
        repo.run()
        self.assertIs(signal.getsignal(signal.SIGTERM), before)

    def test_sigterm_ends_the_run_with_143_and_removes_the_worktrees(self):
        repo = Repo(self, {"tools/calc.py": calc(), "tools/test_calc.py": SUITE})
        repo.commit({"tools/calc.py": calc("1 0 1", "return b + a")})
        started = time.monotonic()
        with self.assertRaises(SystemExit) as caught:
            repo.run(env={"FAKE_MUTMUT_TERM": "1"})
        self.assertEqual(caught.exception.code, 143)
        self.assertLess(
            time.monotonic() - started, 9, "the fake mutmut ran out its sleep"
        )
        self.assertEqual(repo.worktrees(), 1)
        self.assertEqual(list(repo.scratch.iterdir()), [])

    def test_an_interrupted_run_ends_mutmut_and_its_workers(self):
        repo = Repo(self, {"tools/calc.py": calc()})
        worker = repo.top / "worker.pid"
        sleeper = repo.top / "sleeper"
        sleeper.write_text(
            f"#!{sys.executable}\nimport subprocess, time\n"
            f"child = subprocess.Popen(['sleep', '60'])\n"
            f"open({str(worker)!r}, 'w').write(str(child.pid))\n"
            "time.sleep(60)\n"
        )
        sleeper.chmod(0o755)
        real_wait = subprocess.Popen.wait
        interrupted = []

        def wait(proc, *args, **kwargs):
            if not interrupted:
                interrupted.append(proc)
                while not worker.exists() or not worker.read_text():
                    time.sleep(0.05)
                raise KeyboardInterrupt
            return real_wait(proc, *args, **kwargs)

        with (
            mock.patch.object(subprocess.Popen, "wait", wait),
            self.assertRaises(KeyboardInterrupt),
        ):
            GATE["run_mutmut"]([str(sleeper)], repo.top, [], [])
        self.assertEqual(interrupted[0].returncode, -signal.SIGTERM)
        pid = int(worker.read_text())
        for _ in range(100):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            os.kill(pid, signal.SIGKILL)
            self.fail("mutmut's worker outlived the interrupted run")


class PragmaTests(unittest.TestCase):
    def problems(self, line: str) -> list[str]:
        return GATE["pragma_problems"]("tools/x.py", f"X = 1\n{line}\n".encode())

    def test_a_pragma_needs_a_reason_in_parentheses(self):
        pragma = "# pragma: no" + " mutate"
        for bare in (
            f"Y = 2  {pragma}",
            f"Y = 2  {pragma}: a reason",
            f"{pragma} block",
            f"{pragma} start",
        ):
            with self.subTest(line=bare):
                self.assertEqual(
                    self.problems(bare),
                    [
                        f"tools/x.py:2: `{bare[bare.index('#') :]}` gives no reason in parentheses"
                    ],
                )
        for reasoned in (
            f"Y = 2  {pragma} (why)",
            f"{pragma} block (why)",
            f"{pragma} start (why (a) x)",
            f"{pragma} end",
        ):
            with self.subTest(line=reasoned):
                self.assertEqual(self.problems(reasoned), [])

    def test_the_words_in_a_string_arent_a_pragma(self):
        self.assertEqual(self.problems(f'Y = "# pragma: no{" mutate"}"'), [])

    def test_each_changed_scripts_bare_pragma_fails_before_mutmut_runs(self):
        repo = Repo(self, {"tools/calc.py": calc(), "tools/test_calc.py": SUITE})
        repo.commit(
            {
                "tools/calc.py": calc() + "Z = 3  # pragma: no" + " mutate\n",
                "tools/sums.py": "Z = 3\n",
            }
        )
        code, out, err = repo.run()
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(
            err,
            "check-mutmut: a no-mutate pragma needs its reason:\n"
            "  tools/calc.py:12: `# pragma: no mutate` gives no reason in parentheses\n",
        )
        self.assertEqual(repo.calls("argv"), [])


class ReaderTests(unittest.TestCase):
    SOURCE = textwrap.dedent(
        """\
        def f():
            return 1


        class C:
            def m(self):
                return 2

            class D:
                async def n(self):
                    return 3


        async def g():
            return 4
        """
    )

    def test_function_hashes_are_mutmuts(self):
        from mutmut.mutation.file_mutation import compute_function_hashes

        hashes = GATE["function_hashes"](self.SOURCE.encode())
        self.assertEqual(list(hashes), ["x_f", "xǁCǁm", "xǁC.Dǁn", "x_g"])
        self.assertEqual(hashes, compute_function_hashes(self.SOURCE))

    def test_a_comment_or_a_move_keeps_a_hash_and_a_change_doesnt(self):
        hashes = GATE["function_hashes"](b"def f():\n    return 1\n")
        moved = GATE["function_hashes"](
            b"# a comment\n\n\ndef f():\n    return 1  # another\n"
        )
        changed = GATE["function_hashes"](b"def f():\n    return 2\n")
        self.assertEqual(moved, hashes)
        self.assertNotEqual(changed, hashes)

    def test_shown_is_the_name_the_source_spells(self):
        self.assertEqual(
            [GATE["shown"](k) for k in ("x_check_pins", "xǁCommandǁwhere", "xǁC.Dǁn")],
            ["check_pins", "Command.where", "C.D.n"],
        )

    def test_module_name_is_mutmuts(self):
        self.assertEqual(
            GATE["module_name"]("tools/check-ci-parity.py"), "tools.check-ci-parity"
        )

    def test_hashes_mutmut_computes_differently_fail_and_are_named(self):
        source = b"def f():\n    return 1\n\n\ndef g():\n    return 2\n"
        right = GATE["function_hashes"](source)
        GATE["check_hashes"]({"hash_by_function_name": right}, "tools/x.py", source)
        GATE["check_hashes"]({}, "tools/x.py", source)
        wrong = {"x_f": "0" * 12, "x_g": right["x_g"][::-1]}
        with self.assertRaises(Failure) as caught:
            GATE["check_hashes"]({"hash_by_function_name": wrong}, "tools/x.py", source)
        self.assertEqual(
            str(caught.exception),
            "mutmut hashes f, g in tools/x.py differently than this script does, so what it reads"
            " as changed isn't what mutmut sees: restate `function_hashes` from the pinned"
            " mutmut's `compute_function_hashes`",
        )

    def test_the_log_tail_is_the_last_lines_each_redraw_its_own(self):
        with tempfile.TemporaryDirectory() as scratch:
            side = Path(scratch)
            lines = [f"line {n}" for n in range(60)]
            text = "\r".join(lines[:30]) + "\n\n" + "\n".join(lines[30:]) + "\n\xff\n"
            (side / "mutmut.log").write_bytes(text.encode("latin-1"))
            self.assertEqual(GATE["log_tail"](side), "\n".join([*lines[21:], "�"]))


def step_env(env: dict[str, str]) -> dict[str, str]:
    """This environment for a step run in a throwaway repo. When mutmut runs this suite over the
    gate, its stats pass marks the environment, and a mutated copy of the gate run from another
    directory records its hits through mutmut's config, which mutmut reads from the working
    directory and doesn't find there; those hits would be lost with the child anyway. The
    mutants' own runs keep the mark, so the step runs each mutant."""
    out = dict(os.environ) | env
    if out.get("MUTANT_UNDER_TEST") == "stats":
        del out["MUTANT_UNDER_TEST"]
    return out


class CiJobTests(unittest.TestCase):
    def test_the_jobs_steps_ask_the_script_and_run_it_as_these_cases_do(self):
        # The first step decides whether any other runs, so a step that always answers `false`
        # turns the job into a green run that mutated nothing.
        root = SCRIPT.parent.parent
        steps = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())["jobs"][
            "mutmut"
        ]["steps"]
        ids = [step.get("id") for step in steps]
        detect, gated = steps[ids.index("diff")], steps[ids.index("diff") + 1 :]
        for step in gated:
            with self.subTest(step=step.get("name") or step.get("uses")):
                self.assertEqual(step.get("if"), "steps.diff.outputs.python == 'true'")
        (mutate,) = [s for s in gated if "mise run ci:mutmut" in s.get("run", "")]
        tasks = runpy.run_path(str(root / "tools/check-ci-parity.py"))["load_tasks"](
            root
        )
        self.assertEqual(tasks["ci:mutmut"]["run"], "./tools/check-mutmut.py")
        self.assertEqual(mutate["run"], 'mise run ci:mutmut --base "$BASE"')
        self.assertEqual(mutate["env"]["BASE"], detect["env"]["BASE"])

        # The step runs the checkout's own script, committed here so it isn't a change.
        repo = Repo(
            self, {"tools/calc.py": calc(), "tools/check-mutmut.py": SCRIPT.read_text()}
        )
        bin_dir = repo.top / "bin"
        bin_dir.mkdir()
        # A wrapper, not a symlink, so the environment this suite runs in is the one it finds.
        (bin_dir / "python3").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        (bin_dir / "python3").chmod(0o755)
        output = repo.top / "github-output"
        env = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "BASE": "origin/main",
            "GITHUB_OUTPUT": str(output),
        }
        for change, answer in (
            (lambda: None, "false"),
            (lambda: repo.write({"tools/calc.py": calc("0")}), "true"),
        ):
            with self.subTest(python=answer):
                change()
                output.write_text("")
                done = subprocess.run(
                    [
                        "bash",
                        "--noprofile",
                        "--norc",
                        "-eo",
                        "pipefail",
                        "-c",
                        detect["run"],
                    ],
                    cwd=repo.root,
                    capture_output=True,
                    text=True,
                    env=step_env(env),
                )
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                self.assertEqual(output.read_text(), f"python={answer}\n")


@unittest.skipIf(
    "MUTANT_UNDER_TEST" in os.environ,
    "mutmut running this suite would run mutmut again inside each mutant's test",
)
class RealMutmutTests(unittest.TestCase):
    def test_the_real_mutmut_counts_an_untested_change_and_agrees_on_the_hashes(self):
        suite = textwrap.dedent(
            """\
            import runpy
            import unittest
            from pathlib import Path

            CALC = runpy.run_path(str(Path(__file__).with_name("calc.py")), run_name="tools.calc")


            class CalcTests(unittest.TestCase):
                def test_add(self):
                    self.assertEqual(CALC["add"](2, 3), 5)
            """
        )
        source = "def add(a, b):\n    return a + b\n"
        repo = Repo(self, {"tools/calc.py": source, "tools/test_calc.py": suite})
        repo.commit(
            {
                "tools/calc.py": "def add(a, b):\n    if a == 100:\n        return 0\n    return a + b\n"
            }
        )
        code, out, err = repo.run("--jobs", "2", jobs=False, mutmut=False)
        self.assertEqual(code, 1, out + err)
        self.assertRegex(
            out, r"    add: [1-9]\d* of \d+ survive \(0 on the base\): MORE\n"
        )
        self.assertIn("check-mutmut: add in tools/calc.py has ", err)
        self.assertIn("tools.calc.x_add__mutmut_", err)


if __name__ == "__main__":
    unittest.main()
