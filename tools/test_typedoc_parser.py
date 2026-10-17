# SPDX-License-Identifier: Apache-2.0
"""Exercise the TypeDoc and ambient-parser boundary without installing packages."""

import contextlib
import copy
import inspect
import json
import runpy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PARSER = runpy.run_path(
    str(Path(__file__).with_name("check-parity.py")),
    run_name="tools.check-parity",
)
GLOBALS = inspect.unwrap(PARSER["run_typedoc"]).__globals__


class TypeDocParser(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.scratch = self.root / "output"
        self.scratch.mkdir()
        self.dts = self.root / "public declarations é.d.ts"
        self.dts.write_text(
            "/** Public café API. */\nexport interface Kept {}\n", encoding="utf-8"
        )
        self.cache = self.root / "package cache" / "node_modules"
        self.invocations = []
        self.locked = False
        self.lock_entries = 0

    @contextlib.contextmanager
    def lock(self):
        self.assertFalse(self.locked)
        self.lock_entries += 1
        self.locked = True
        try:
            yield
        finally:
            self.locked = False

    def collect(self, project, extra, *, failure_at=None):
        def run(argv, purpose):
            stage = len(self.invocations)
            self.invocations.append((argv, purpose))
            self.assertEqual(self.locked, stage == 0)
            if failure_at == stage:
                raise PARSER["ParityError"](f"parser subprocess {stage} failed")
            if stage == 0:
                return f"  {self.cache / '.bin' / 'typedoc'}\n"
            if stage == 1:
                # Honor the requested output, as TypeDoc does; the separate argv assertion
                # holds the configured filename and every compiler input.
                Path(argv[argv.index("--json") + 1]).write_text(
                    json.dumps(project, ensure_ascii=False), encoding="utf-8"
                )
                return "TypeDoc generated the project\n"
            if stage == 2:
                return json.dumps(extra, ensure_ascii=False)
            self.fail("unexpected parser subprocess")

        with mock.patch.dict(
            GLOBALS,
            {
                "run": run,
                "npx_lock": self.lock,
                "npx": lambda: ["npx", "--test-packages"],
            },
        ):
            return PARSER["run_typedoc"](self.dts, self.scratch)

    def assert_inputs(self):
        self.assertEqual(self.lock_entries, 1)
        self.assertFalse(self.locked)
        self.assertEqual(
            [argv for argv, _ in self.invocations],
            [
                ["npx", "--test-packages", "-c", "command -v typedoc"],
                [
                    "npx",
                    "--test-packages",
                    "typedoc",
                    "--json",
                    str(self.scratch / "typedoc.json"),
                    "--tsconfig",
                    str(self.scratch / "tsconfig.json"),
                    "--entryPoints",
                    str(self.dts.resolve()),
                ],
                [
                    "node",
                    str(GLOBALS["TS_GLOBALS"]),
                    str(self.cache / "typescript" / "lib" / "typescript.js"),
                    str(self.dts.resolve()),
                ],
            ],
        )
        self.assertEqual(
            json.loads((self.scratch / "tsconfig.json").read_text(encoding="utf-8")),
            {
                "extends": str(GLOBALS["TSCONFIG"]),
                "compilerOptions": {"typeRoots": [str(self.cache / "@types")]},
                "files": [str(self.dts.resolve())],
            },
        )

    def test_regular_exports_and_ambient_declarations_are_both_retained(self):
        project = {
            "id": 0,
            "children": [{"id": 1, "name": "Kept", "kind": 256}],
            "globalDeclarations": {"stale": []},
        }
        extra = {
            "exports": ["Kept"],
            "globals": {"Console": ["interface Console { café: string }"]},
        }
        expected = copy.deepcopy(project)
        expected["globalDeclarations"] = extra["globals"]
        self.assertEqual(self.collect(project, extra), expected)
        self.assert_inputs()

    def test_an_ambient_only_file_needs_no_regular_children(self):
        extra = {"exports": [], "globals": {"Ambient": ["interface Ambient {}"]}}
        self.assertEqual(
            self.collect({"name": "ambient module"}, extra),
            {"name": "ambient module", "globalDeclarations": extra["globals"]},
        )
        self.assert_inputs()

    def test_export_omissions_refuse_a_partial_inventory_and_name_missing_exports(self):
        with self.assertRaises(PARSER["ParityError"]) as error:
            self.collect(
                {"children": [{"name": "Kept"}]},
                {"exports": ["Zebra", "Kept", "Alpha"], "globals": {"Ambient": []}},
            )
        self.assertEqual(
            str(error.exception),
            "TypeDoc omitted exported declarations: ['Alpha', 'Zebra']",
        )
        self.assert_inputs()

    def test_subprocess_failure_stops_before_returning_a_partial_project(self):
        for stage in range(3):
            with self.subTest(stage=stage):
                self.invocations = []
                self.lock_entries = 0
                with self.assertRaises(PARSER["ParityError"]) as error:
                    self.collect(
                        {"children": [{"name": "Kept"}]},
                        {"exports": ["Kept"], "globals": {}},
                        failure_at=stage,
                    )
                self.assertEqual(
                    str(error.exception), f"parser subprocess {stage} failed"
                )
                self.assertEqual(len(self.invocations), stage + 1)
                self.assertEqual(self.lock_entries, 1)
                self.assertFalse(self.locked)

    def test_hidden_declarations_fail_before_any_parser_is_started(self):
        self.dts.write_text(
            "/** @hidden */\nexport interface Secret {}\n", encoding="utf-8"
        )
        with self.assertRaises(PARSER["ParityError"]) as error:
            self.collect({}, {"exports": [], "globals": {}})
        self.assertIn("line 1: @hidden", str(error.exception))
        self.assertEqual(self.invocations, [])
        self.assertEqual(self.lock_entries, 0)
