# SPDX-License-Identifier: Apache-2.0
"""Tests for `scripts/check-parity.py`, the capability table's gate (#271).

The rule cases feed the checker small surfaces in the shape each parser prints (a Griffe dump, a
TypeDoc project, the manifest envelope, the core snapshot) and a table written for them, then
break one thing at a time. A baseline case holds the untouched fixture to zero problems, so a
case that fails is failing for the thing it broke.

The reader cases run the real parsers (Griffe through `griffe_dump.py`, TypeDoc through npx)
over a tiny stub and declaration file, because a reader tested only against hand-written dumps
proves the hand-written dumps. They need uv and npx on PATH, so run this under `mise x` or a
mise task.
"""

import copy
import json
import runpy
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("check-parity.py")
PARITY = runpy.run_path(str(SCRIPT))

TABLE = """
[flag_groups]
connection = ["endpoint", "name"]

[[capability]]
id = "launch"
sentinel = true
core = "microvms_core::sandbox::Sandbox::run"
cli = "run"
py = "Sandbox.run"
ts = "Sandbox.run"

[[capability]]
id = "health"
sentinel = true
core = "microvms_core::session::Session::health"
cli = "health"
py = "Session.health"
ts = "Session.health"

[[capability]]
id = "kill"
sentinel = true
core = "microvms_core::session::Session::kill"
cli = "kill"
py = "Session.kill"
ts = "Session.kill"

[[capability]]
id = "run-report"
sentinel = true
core = "microvms_core::cost::run_report"
cli = "cost"
py = "run_report"
ts = "runReport"

[[capability]]
id = "attach"
core = "microvms_core::session::Session::attach"
cli = "@connection"
py = "Session.attach"
ts = "Session.attach"

[[capability]]
id = "file-exists"
core = "microvms_core::session::Session::file_exists"
cli = { exempt = "no command", issue = "#269" }
py = "Session.file_exists"
ts = "Session.fileExists"

[[capability]]
id = "ledger"
core = { exempt = "local CLI state" }
cli = "ls --remote"
py = { exempt = "local CLI state" }
ts = { exempt = "local CLI state" }

[[type]]
name = "Sandbox"
exempt_members.ts = { create = "the async factory idiom", region = { exempt = "Python lacks it", issue = "#267" } }

[exempt_names]
ts = { __napiBindingTarget = "a napi-rs build artifact" }
"""


def function(name):
    return {
        "kind": "function",
        "name": name,
        "labels": [],
        "special": name.startswith("__"),
    }


def attribute(name, labels=("property",)):
    return {"kind": "attribute", "name": name, "labels": list(labels)}


def pyclass(name, *members):
    return {"kind": "class", "name": name, "members": list(members)}


GRIFFE = {
    "griffe": "2.3.0",
    "module": "microvms",
    "members": [
        pyclass(
            "Sandbox", function("__new__"), function("run"), attribute("microvm_id")
        ),
        pyclass(
            "Session",
            function("__repr__"),
            function("health"),
            function("kill"),
            function("attach"),
            function("file_exists"),
        ),
        function("run_report"),
        attribute("__version__", ["module-attribute"]),
    ],
}

CLASS, METHOD, ACCESSOR, CONSTRUCTOR, FUNCTION, VARIABLE, NAMESPACE = (
    128,
    2048,
    262144,
    512,
    64,
    32,
    4,
)


def member(name, kind=METHOD, static=False):
    return {"kind": kind, "name": name, "flags": {"isStatic": True} if static else {}}


def tsclass(name, *members):
    return {
        "kind": CLASS,
        "name": name,
        "children": [member("constructor", CONSTRUCTOR), *members],
    }


TYPEDOC = {
    "schemaVersion": "2.0",
    "kind": 1,
    "children": [
        tsclass(
            "Sandbox",
            member("run"),
            member("microvmId"),
            member("region", ACCESSOR),
            member("create", static=True),
        ),
        tsclass(
            "Session",
            member("health"),
            member("kill"),
            member("fileExists"),
            member("attach", static=True),
        ),
        {"kind": FUNCTION, "name": "runReport"},
        {"kind": VARIABLE, "name": "__napiBindingTarget"},
    ],
}


def command(name, *flags, positional=()):
    parameters = [{"name": flag, "positional": False} for flag in flags]
    parameters += [{"name": arg, "positional": True} for arg in positional]
    return {"name": name, "parameters": parameters}


MANIFEST = {
    "apiVersion": "microvm.v1",
    "type": "microvm.manifest",
    "data": {
        "commands": [
            command("run", "remote", positional=["binary"]),
            command("health", "endpoint", "name"),
            command("kill", "endpoint", "name", positional=["exec_id"]),
            command("cost", "estimate"),
            command("ls", "remote"),
        ],
        "globalFlags": [{"name": "json", "positional": False}],
    },
}

CORE = {
    "paths": {
        "microvms_core::sandbox::Sandbox::run": "method",
        "microvms_core::session::Session::health": "method",
        "microvms_core::session::Session::kill": "method",
        "microvms_core::session::Session::attach": "method via microvms_core::prelude::SessionExt",
        "microvms_core::session::Session::file_exists": "method",
        "microvms_core::cost::run_report": "function",
    },
}


class RuleTests(unittest.TestCase):
    """The rules over fixture surfaces, each case breaking one thing the baseline has."""

    def setUp(self):
        self.table = TABLE
        self.griffe = copy.deepcopy(GRIFFE)
        self.typedoc = copy.deepcopy(TYPEDOC)
        self.manifest = copy.deepcopy(MANIFEST)
        self.core = copy.deepcopy(CORE)

    def problems(self):
        table = PARITY["parse_table"](self.table)
        surfaces = {
            "core": PARITY["core_surface"](self.core),
            "cli": PARITY["cli_surface"](self.manifest),
            "py": PARITY["py_surface"](self.griffe),
            "ts": PARITY["ts_surface"](self.typedoc),
        }
        return PARITY["check"](table, surfaces)

    def assertProblem(self, fragment):
        problems = self.problems()
        self.assertTrue(
            any(fragment in problem for problem in problems),
            f"no problem mentions {fragment!r}; the check reported {problems}",
        )

    def test_the_baseline_fixture_passes(self):
        self.assertEqual(self.problems(), [])

    def test_an_unmapped_python_function_fails(self):
        self.griffe["members"].append(function("orphan"))
        self.assertProblem("py: orphan belongs to no row")

    def test_an_unmapped_method_of_a_class_a_row_names_fails(self):
        self.griffe["members"][1]["members"].append(function("procs"))
        self.assertProblem("py: Session.procs belongs to no row")

    def test_a_new_method_on_both_sides_of_a_type_needs_a_row(self):
        # The pairing alone would pass it: both bindings have it, and nothing asks about core
        # or the CLI.
        self.griffe["members"][0]["members"].append(function("snapshot"))
        self.typedoc["children"][0]["children"].append(member("snapshot"))
        problems = self.problems()
        self.assertIn("py: Sandbox.snapshot belongs to no row", problems)
        self.assertIn("ts: Sandbox.snapshot belongs to no row", problems)

    def test_a_type_accessor_is_held_by_the_pairing_alone(self):
        # napi writes the getter `microvmId` as a method; its Python twin is a property.
        problems = self.problems()
        self.assertFalse([problem for problem in problems if "microvm" in problem])
        self.griffe["members"][0]["members"].remove(attribute("microvm_id"))
        self.assertProblem("type Sandbox: ts: microvmId has no Python twin")

    def test_an_unmapped_function_in_a_typescript_namespace_fails(self):
        self.typedoc["children"].append(
            {
                "kind": NAMESPACE,
                "name": "snapshots",
                "children": [{"kind": FUNCTION, "name": "snapshotVm"}],
            }
        )
        self.assertProblem("ts: snapshots.snapshotVm belongs to no row")

    def test_an_unmapped_cli_command_fails(self):
        self.manifest["data"]["commands"].append(command("doctor"))
        self.assertProblem("cli: doctor belongs to no row")

    def test_a_row_naming_a_typescript_method_that_does_not_exist_fails(self):
        self.typedoc["children"][1]["children"] = [
            child
            for child in self.typedoc["children"][1]["children"]
            if child["name"] != "fileExists"
        ]
        self.assertProblem("file-exists: ts: Session.fileExists isn't on the surface")

    def test_a_row_naming_a_cli_flag_the_command_lacks_fails(self):
        self.table = self.table.replace('cli = "ls --remote"', 'cli = "ls --prune"')
        self.assertProblem("ledger: cli: ls --prune isn't on the surface")

    def test_a_row_naming_a_core_path_the_snapshot_lacks_fails(self):
        del self.core["paths"]["microvms_core::session::Session::file_exists"]
        self.assertProblem(
            "file-exists: core: microvms_core::session::Session::file_exists isn't on the surface"
        )

    def test_a_missing_cli_cell_with_no_exemption_fails(self):
        self.table = self.table.replace(
            'cli = { exempt = "no command", issue = "#269" }\n', ""
        )
        self.assertProblem("file-exists: cli: no cell")

    def test_an_exemption_with_an_empty_reason_fails(self):
        self.table = self.table.replace('exempt = "no command"', 'exempt = "  "')
        self.assertProblem("file-exists: cli: the exemption has an empty reason")

    def test_an_issue_that_is_not_a_number_fails(self):
        self.table = self.table.replace('issue = "#269"', 'issue = "TBD"')
        self.assertProblem("file-exists: cli: issue 'TBD' isn't of the form #<number>")

    def test_an_issue_on_an_exempt_member_is_held_to_the_same_form(self):
        self.table = self.table.replace('issue = "#267"', 'issue = "267"')
        self.assertProblem(
            "type Sandbox: ts: region: issue '267' isn't of the form #<number>"
        )

    def test_an_empty_griffe_dump_fails_on_the_sentinels(self):
        self.griffe["members"] = []
        problems = self.problems()
        for sentinel, name in (
            ("launch", "Sandbox.run"),
            ("health", "Session.health"),
            ("kill", "Session.kill"),
            ("run-report", "run_report"),
        ):
            self.assertIn(
                f"sentinel {sentinel}: py: {name} isn't on the surface", problems
            )
        self.assertIn("py: the parser returned no members", problems)
        # the cause, once, and the sentinels; not one line per row
        self.assertEqual(len(problems), 5, problems)

    def test_an_empty_typedoc_project_fails_on_the_sentinels(self):
        self.typedoc["children"] = []
        self.assertProblem("sentinel launch: ts: Sandbox.run isn't on the surface")

    def test_an_empty_table_fails_on_the_sentinels_alone(self):
        self.table = ""
        problems = self.problems()
        self.assertIn("the table marks no sentinel row launch", problems)
        self.assertIn("table: no [[capability]] rows", problems)
        self.assertFalse(
            [problem for problem in problems if "belongs to no row" in problem]
        )

    def test_a_sentinel_may_not_be_exempt(self):
        self.table = self.table.replace(
            'cli = "health"', 'cli = { exempt = "not needed" }'
        )
        self.assertProblem("sentinel health: cli: a sentinel can't be exempt")

    def test_a_table_without_one_of_the_four_sentinels_fails(self):
        self.table = self.table.replace('id = "kill"\nsentinel = true', 'id = "kill"')
        self.assertProblem("the table marks no sentinel row kill")

    def test_a_member_on_one_side_of_a_type_needs_an_exemption(self):
        self.griffe["members"][0]["members"].append(
            function("suspended_window_seconds")
        )
        self.assertProblem(
            "type Sandbox: py: suspended_window_seconds has no TypeScript twin and no exemption"
        )

    def test_an_exempt_member_that_has_a_twin_fails(self):
        self.griffe["members"][0]["members"].append(function("create"))
        self.assertProblem("type Sandbox: ts: create is exempt but has a Python twin")

    def test_an_exempt_member_that_is_gone_fails(self):
        self.typedoc["children"][0]["children"] = [
            child
            for child in self.typedoc["children"][0]["children"]
            if child["name"] != "region"
        ]
        self.assertProblem(
            "type Sandbox: ts: region is exempt but isn't on the surface"
        )

    def test_an_exempt_name_a_row_also_maps_fails(self):
        self.table = self.table.replace(
            "ts = { __napiBindingTarget",
            'py = { run_report = "stale" }\nts = { __napiBindingTarget',
        )
        self.assertProblem("exempt_names: py: run_report is exempt and also in a row")

    def test_an_unknown_capability_key_fails(self):
        self.table = self.table.replace('ts = "runReport"', 'tss = "runReport"')
        self.assertProblem("run-report: unknown key tss")

    def test_a_flag_group_naming_a_flag_no_command_has_fails(self):
        self.table = self.table.replace('["endpoint", "name"]', '["endpoint", "nmae"]')
        self.assertProblem("flag_groups: connection: nmae is no command's flag")

    def test_an_unknown_flag_group_fails(self):
        self.table = self.table.replace('cli = "@connection"', 'cli = "@infra"')
        self.assertProblem("attach: cli: @infra names no flag group")

    def test_a_duplicate_id_fails(self):
        self.table = self.table.replace('id = "ledger"', 'id = "attach"')
        self.assertProblem("attach: the id is used by more than one row")


class ExemptionRecordTests(unittest.TestCase):
    """`--json`'s exemption records, the contract the ratchet's parity-gap category reads."""

    def test_each_exemption_is_one_record_with_its_issue(self):
        records = PARITY["exemptions"](PARITY["parse_table"](TABLE))
        self.assertIn(
            {"key": "file-exists/cli", "reason": "no command", "issue": 269}, records
        )
        self.assertIn(
            {"key": "ledger/core", "reason": "local CLI state", "issue": None}, records
        )
        self.assertIn(
            {"key": "Sandbox.region/ts", "reason": "Python lacks it", "issue": 267},
            records,
        )
        self.assertIn(
            {
                "key": "__napiBindingTarget/ts",
                "reason": "a napi-rs build artifact",
                "issue": None,
            },
            records,
        )
        self.assertEqual(len({record["key"] for record in records}), len(records))

    def exemptions_mode(self, table: str) -> subprocess.CompletedProcess:
        # Every surface points at a file that doesn't exist, so a pass shows the mode reads the
        # table and nothing else: the ratchet runs it in a CI job with no Node for TypeDoc.
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "capabilities.toml"
            path.write_text(table, encoding="utf-8")
            missing = str(Path(scratch) / "missing")
            return subprocess.run(
                [sys.executable, str(SCRIPT), "--exemptions", "--table", str(path)]
                + [
                    f"--{flag}={missing}"
                    for flag in ("core-api", "manifest", "pyi", "dts")
                ],
                capture_output=True,
                text=True,
            )

    def test_the_exemptions_mode_prints_the_records_from_the_table_alone(self):
        done = self.exemptions_mode(TABLE)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(
            json.loads(done.stdout),
            {"exemptions": PARITY["exemptions"](PARITY["parse_table"](TABLE))},
        )

    def test_the_exemptions_mode_refuses_an_issue_it_cannot_read(self):
        # A malformed issue would read as a decision, and the ratchet would stop counting it.
        done = self.exemptions_mode(TABLE.replace('issue = "#269"', 'issue = "TBD"'))
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn(
            "file-exists: cli: issue 'TBD' isn't of the form #<number>", done.stdout
        )


class PinTests(unittest.TestCase):
    def test_the_typedoc_pins_equal_the_sites(self):
        self.assertEqual(PARITY["pin_problems"](), [])

    def test_a_pin_the_site_moved_fails(self):
        with tempfile.TemporaryDirectory() as scratch:
            package = Path(scratch) / "package.json"
            package.write_text(
                json.dumps(
                    {
                        "devDependencies": {
                            "typedoc": "0.28.21",
                            "typescript": "5.9.3",
                            "@types/node": "26.4.0",
                        }
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                PARITY["pin_problems"](package),
                ["pins: typedoc is 0.28.20 here and 0.28.21 in site/package.json"],
            )


STUB = """
# SPDX-License-Identifier: Apache-2.0
class Session:
    def __repr__(self, /) -> str: ...
    @property
    def endpoint(self, /) -> str: ...
    def health(self, /) -> bool: ...
    @staticmethod
    def attach(endpoint: str) -> Session: ...

def run_report() -> str: ...
__version__: str
"""

DECLARATIONS = """
// SPDX-License-Identifier: Apache-2.0
export declare class Session {
  get endpoint(): string
  health(): Promise<boolean>
  static attach(endpoint: string): Promise<Session>
  bytes(): Buffer
}
export declare function runReport(): string
export declare const __napiBindingTarget: string
export interface RunOptions { timeout?: number }
export declare namespace snapshots {
  export function snapshotVm(): void
}
"""


class ReaderTests(unittest.TestCase):
    """The readers over the real parsers' output, so a parser whose shape moved fails here."""

    def test_the_python_reader_over_griffe(self):
        with tempfile.TemporaryDirectory() as scratch:
            stub = Path(scratch) / "microvms.pyi"
            stub.write_text(textwrap.dedent(STUB), encoding="utf-8")
            surface = PARITY["py_surface"](PARITY["run_griffe"](stub))
        self.assertEqual(surface.functions, {"run_report"})
        self.assertEqual(surface.classes["Session"].methods, {"health", "attach"})
        self.assertEqual(
            surface.classes["Session"].members, {"health", "attach", "endpoint"}
        )

    def test_the_typescript_reader_over_typedoc(self):
        with tempfile.TemporaryDirectory() as scratch:
            declarations = Path(scratch) / "index.d.ts"
            declarations.write_text(DECLARATIONS, encoding="utf-8")
            surface = PARITY["ts_surface"](
                PARITY["run_typedoc"](declarations, Path(scratch))
            )
        self.assertEqual(
            surface.functions,
            {"runReport", "__napiBindingTarget", "snapshots.snapshotVm"},
        )
        self.assertEqual(
            surface.classes["Session"].methods, {"health", "attach", "bytes"}
        )
        self.assertEqual(
            surface.classes["Session"].members,
            {"health", "attach", "bytes", "endpoint"},
        )
        self.assertIn("RunOptions", surface.names)
        self.assertIn("RunOptions.timeout", surface.names)
        self.assertNotIn("RunOptions", surface.classes)

    def test_a_declaration_typedoc_would_drop_is_refused(self):
        # TypeDoc leaves out a declaration tagged `@hidden`, `@ignore` or `@private`, so the
        # check would pass a TS-only function it never saw. It refuses the file before TypeDoc
        # runs.
        for tag in ("@hidden", "@ignore", "@private"):
            with self.subTest(tag=tag), tempfile.TemporaryDirectory() as scratch:
                declarations = Path(scratch) / "index.d.ts"
                declarations.write_text(
                    DECLARATIONS
                    + f"/** Snapshots the VM. {tag} */\nexport declare function snapshotVm(): void\n",
                    encoding="utf-8",
                )
                # the tagged comment is the first line after DECLARATIONS
                line = DECLARATIONS.count("\n") + 1
                with self.assertRaisesRegex(
                    PARITY["ParityError"], f"line {line}: {tag}"
                ):
                    PARITY["run_typedoc"](declarations, Path(scratch))

    def test_a_tag_typedoc_keeps_is_read(self):
        self.assertEqual(
            PARITY["hidden_declarations"](
                "/** @internal @protected @see Sandbox, mail me@private.example */\n"
            ),
            [],
        )

    def test_an_empty_stub_reads_as_no_members(self):
        with tempfile.TemporaryDirectory() as scratch:
            stub = Path(scratch) / "microvms.pyi"
            stub.write_text("# SPDX-License-Identifier: Apache-2.0\n", encoding="utf-8")
            surface = PARITY["py_surface"](PARITY["run_griffe"](stub))
        self.assertEqual(surface.names, set())


if __name__ == "__main__":
    unittest.main()
