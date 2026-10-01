# SPDX-License-Identifier: Apache-2.0
"""Tests for `tools/check-npm-package.py`'s readers: the pins, npm pack's answer, publint's exit,
attw's report, and where npx found each tool.

The gate itself runs over the built package in CI's bindings job (`npm:package`), and a broken
`types` or `exports` entry for each tool is a seeded fault in `verify/guards/faults/npm-package.toml`.
These cases hand the readers fakes: a tool that prints a canned report, a package directory
with a hand-written package.json, an npx that prints a path. `npm pack` is the real one, over a
throwaway package, so they need npm on PATH: run this under `mise x` or a mise task.
"""

import json
import os
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).with_name("check-npm-package.py")
NPM = runpy.run_path(str(SCRIPT), run_name="tools.check-npm-package")
PackageError = NPM["PackageError"]

# A tool that prints FAKE_OUTPUT and exits FAKE_CODE, and records its arguments in FAKE_ARGV.
FAKE_TOOL = """#!{python}
import json, os, sys
with open(os.environ["FAKE_ARGV"], "w") as out:
    json.dump(sys.argv[1:], out)
sys.stdout.write(os.environ["FAKE_OUTPUT"])
sys.exit(int(os.environ["FAKE_CODE"]))
"""

# Stands in for npx: prints FAKE_WHERE as the located binary.
FAKE_NPX = """#!{python}
import os, sys
if sys.argv[-2] != "-c" or not sys.argv[-1].startswith("command -v "):
    print("npx called as", sys.argv[1:])
    sys.exit(2)
print(os.environ["FAKE_WHERE"])
"""


def resolved(file_name):
    return {"resolution": {"fileName": file_name}}


def report(types=True, entrypoints=None, problems=None):
    """An attw JSON report shaped as 0.18.5 prints one."""
    if entrypoints is None:
        entrypoints = {
            ".": {
                "resolutions": {
                    kind: resolved("/node_modules/pkg/index.d.ts")
                    for kind in ("node10", "node16-cjs", "node16-esm", "bundler")
                }
            }
        }
    analysis = {"packageName": "pkg", "types": {"kind": "included"} if types else False}
    if types:
        analysis["entrypoints"] = entrypoints
    out = {"analysis": analysis}
    if problems is not None:
        out["problems"] = problems
    return json.dumps(out)


class Scratch(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)

    def tool(self, output, code):
        """A fake tool binary, and the file it records its arguments in."""
        path = self.dir / "tool"
        path.write_text(FAKE_TOOL.format(python=sys.executable), encoding="utf-8")
        path.chmod(0o755)
        argv = self.dir / "argv.json"
        patch = mock.patch.dict(
            os.environ,
            {"FAKE_OUTPUT": output, "FAKE_CODE": str(code), "FAKE_ARGV": str(argv)},
        )
        patch.start()
        self.addCleanup(patch.stop)
        return path, argv

    def package(self, manifest):
        package = self.dir / "package"
        package.mkdir()
        (package / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
        return package


class PinTests(Scratch):
    def test_exact_versions_are_read(self):
        package = self.package(
            {"devDependencies": {"publint": "1.2.3", "@arethetypeswrong/cli": "4.5.6"}}
        )
        self.assertEqual(
            NPM["pins"](package), {"publint": "1.2.3", "@arethetypeswrong/cli": "4.5.6"}
        )

    def test_a_range_or_a_missing_pin_fails_and_names_both(self):
        package = self.package({"devDependencies": {"publint": "^1.2.3"}})
        with self.assertRaises(PackageError) as caught:
            NPM["pins"](package)
        self.assertIn(
            "pins publint at '^1.2.3', not an exact version", str(caught.exception)
        )
        self.assertIn("pins @arethetypeswrong/cli at None", str(caught.exception))

    def test_the_repos_package_pins_both_exactly(self):
        pinned = NPM["pins"](NPM["PACKAGE"])
        self.assertEqual(sorted(pinned), sorted(NPM["TOOLS"]))


class PackTests(Scratch):
    def test_the_tarball_npm_pack_writes_is_returned(self):
        package = self.package({"name": "pkg", "version": "1.0.0", "main": "index.js"})
        (package / "index.js").write_text("module.exports = {};\n", encoding="utf-8")
        dest = self.dir / "out"
        dest.mkdir()
        self.assertEqual(NPM["pack"](package, dest), dest / "pkg-1.0.0.tgz")

    def test_a_package_npm_cant_pack_fails(self):
        package = self.package({"name": "pkg"})
        (package / "package.json").write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(PackageError, "npm pack exited"):
            NPM["pack"](package, self.dir)


class PublintTests(Scratch):
    def test_a_clean_run_is_no_problem_and_warnings_fail(self):
        tool, argv = self.tool("Suggestions: none\n", 0)
        self.assertEqual(NPM["publint"](tool, Path("pkg.tgz")), [])
        self.assertEqual(json.loads(argv.read_text()), ["run", "--strict", "pkg.tgz"])

    def test_a_failing_run_carries_publints_output(self):
        tool, _ = self.tool(
            "Errors:\n1. pkg.types is x but the file does not exist.\n", 1
        )
        self.assertEqual(
            NPM["publint"](tool, Path("pkg.tgz")),
            [
                "publint exited 1:\nErrors:\n1. pkg.types is x but the file does not exist."
            ],
        )


class AttwTests(Scratch):
    def attw(self, output, code=0):
        tool, argv = self.tool(output, code)
        found = NPM["attw"](tool, Path("pkg.tgz"))
        self.assertEqual(json.loads(argv.read_text()), ["pkg.tgz", "--format", "json"])
        return found

    def test_a_clean_report_is_no_problem(self):
        self.assertEqual(self.attw(report(problems={})), [])

    def test_each_problem_is_named_with_its_entrypoint_and_resolution(self):
        problems = {
            "NoResolution": [
                {
                    "kind": "NoResolution",
                    "entrypoint": "./sub",
                    "resolutionKind": "node10",
                }
            ],
            "FalseCJS": [
                {"kind": "FalseCJS", "entrypoint": ".", "resolutionKind": "node16-esm"}
            ],
        }
        self.assertEqual(
            self.attw(report(problems=problems), 1),
            [
                "attw: NoResolution at `./sub` under node10",
                "attw: FalseCJS at `.` under node16-esm",
            ],
        )

    def test_a_package_with_no_types_fails_though_attw_passes_it(self):
        self.assertEqual(
            self.attw(report(types=False)),
            [
                "attw: the package holds no type declarations, and attw reports that as a "
                "pass: `types` or `files` in package.json lost `index.d.ts`"
            ],
        )

    def test_a_report_without_the_root_entrypoint_fails(self):
        entrypoints = {"./package.json": {"resolutions": {}}}
        self.assertEqual(
            self.attw(report(entrypoints=entrypoints, problems={})),
            [
                "attw: the root entrypoint `.` isn't in the report, so an import of the "
                "package itself went unchecked (an `exports` without `.`)"
            ],
        )

    def test_a_root_that_resolves_to_no_declaration_fails_under_that_resolution(self):
        entrypoints = {
            ".": {
                "resolutions": {
                    "node10": resolved("/node_modules/pkg/index.d.ts"),
                    "node16-cjs": resolved("/node_modules/pkg/index.js"),
                    "node16-esm": {"resolution": None},
                    "bundler": resolved("/node_modules/pkg/index.d.ts"),
                }
            }
        }
        self.assertEqual(
            self.attw(report(entrypoints=entrypoints, problems={})),
            [
                "attw: `.` under node16-cjs resolves to '/node_modules/pkg/index.js', not to "
                "declarations",
                "attw: `.` under node16-esm resolves to None, not to declarations",
            ],
        )

    def test_a_failing_exit_with_no_problem_listed_fails(self):
        self.assertEqual(
            self.attw(report(problems={}), 1),
            ["attw exited 1 and reported no problem"],
        )

    def test_output_that_isnt_a_report_fails(self):
        self.assertEqual(
            self.attw("Error: no such file\n", 1),
            ["attw exited 1 with no report:\nError: no such file\n"],
        )


class LocateTests(Scratch):
    def locate(self, where):
        bin_dir = self.dir / "bin"
        bin_dir.mkdir()
        npx = bin_dir / "npx"
        npx.write_text(FAKE_NPX.format(python=sys.executable), encoding="utf-8")
        npx.chmod(0o755)
        cache = self.dir / "cache"
        (cache / "_npx").mkdir(parents=True)
        env = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            # the real npm answers `npm config get cache` from this, not the caller's ~/.npm
            "npm_config_cache": str(cache),
            "FAKE_WHERE": str(where(cache)),
        }
        with mock.patch.dict(os.environ, env):
            return NPM["locate"]("publint", "1.2.3"), where(cache)

    def test_a_binary_in_npxs_tree_is_located(self):
        found, printed = self.locate(
            lambda cache: cache / "_npx" / "0" / "node_modules" / ".bin" / "publint"
        )
        self.assertEqual(found, printed)

    def test_a_binary_outside_npxs_tree_is_refused(self):
        with self.assertRaisesRegex(
            PackageError, r"npx located publint at .*/global/publint, outside npm's npx"
        ):
            self.locate(lambda cache: cache.parent / "global" / "publint")


if __name__ == "__main__":
    unittest.main()
