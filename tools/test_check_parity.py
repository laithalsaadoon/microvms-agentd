# SPDX-License-Identifier: Apache-2.0
"""Tests for `tools/check-parity.py`, the capability table's gate (#271).

The rule cases feed the checker small surfaces in the shape each parser prints (a Griffe dump, a
TypeDoc project, the manifest envelope, the core snapshot) and a table written for them, then
break one thing at a time. A baseline case holds the untouched fixture to zero problems, so a
case that fails is failing for the thing it broke.

The reader cases run the real parsers (Griffe through `griffe_dump.py`, TypeDoc through npx)
over a tiny stub and declaration file, because a reader tested only against hand-written dumps
proves the hand-written dumps. They need uv and npx on PATH, so run this under `mise x` or a
mise task, on a POSIX host (the npx lock cases use fcntl, as the TypeDoc run needs a POSIX shell).
"""

import contextlib
import copy
import fcntl
import inspect
import io
import json
import os
import runpy
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).with_name("check-parity.py")
PARITY = runpy.run_path(str(SCRIPT), run_name="tools.check-parity")
# The script's own namespace, which its functions read their globals from. mutmut wraps each
# function of a script it mutates in a trampoline defined in its own module, so under mutmut
# a function's `__globals__` are mutmut's and a patch to them reaches nothing the script
# reads. `inspect.unwrap` gives back the script's own function.
PARITY_GLOBALS = inspect.unwrap(PARITY["main"]).__globals__

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
cli = { exempt = "no command" }
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
exempt_members.ts = { create = "the async factory idiom", region = { exempt = "Python lacks it" } }

[exempt_names]
ts = { __napiBindingTarget = "a napi-rs build artifact" }

[[default]]
id = "exec-wait"
sentinel = true
core = "execWaitSeconds"
cli = "run --timeout"
py = ["Session.health(timeout)", "Session.attach(retries)"]

[[default]]
id = "launch-wait"
core = "launch.wait"
py = "Sandbox.run(wait)"

[[default]]
id = "watch-interval"
exempt = "a terminal affordance"
value = "2"
cli = "ls --interval-sec"

[[default]]
id = "offset"
core = "offset"
cli = "cost --running-sec"
py = "Session.kill(offset)"

[[default]]
id = "labels"
core = "label"
py = "run_report(label)"

[[default]]
id = "absent"
core = "image"
py = "Sandbox.run(image)"
ts = "Sandbox.run(image)"

[[default]]
id = "table-format"
exempt = "a terminal-only rendering selection"
value = "table"
cli = "ls --format"
"""

# The table with two tracked gaps, one on a row and one on a type's member: the records
# `--exemptions` reports with their issues, and what the full check refuses since parity-gap is
# enforced at zero (#280).
TRACKED = TABLE.replace(
    'cli = { exempt = "no command" }', 'cli = { exempt = "no command", issue = "#269" }'
).replace(
    'region = { exempt = "Python lacks it" }',
    'region = { exempt = "Python lacks it", issue = "#267" }',
)
assert TRACKED.count("issue = ") == 2, "the tracked variant names both gaps"


def function(name, **defaults):
    """A Griffe function record; `defaults` are its parameters' defaults as the stub spells them."""
    return {
        "kind": "function",
        "name": name,
        "labels": [],
        "special": name.startswith("__"),
        "parameters": [
            {"name": "self", "default": None, "kind": "positional-only"},
            *(
                {"name": key, "default": value, "kind": "positional or keyword"}
                for key, value in defaults.items()
            ),
        ],
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
            "Sandbox",
            function("__new__"),
            function("run", wait="True", image="None"),
            attribute("microvm_id"),
        ),
        pyclass(
            "Session",
            function("__repr__"),
            function("health", timeout="300.0"),
            function("kill", offset="0"),
            function("attach", retries="..."),
            function("file_exists"),
        ),
        function("run_report", label='"run"'),
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
    "name": "Documentation",
    "kind": 1,
    "children": [
        tsclass(
            "Sandbox",
            {
                **member("run"),
                "signatures": [
                    {"parameters": [{"name": "image", "flags": {"isOptional": True}}]}
                ],
            },
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


def command(name, *flags, positional=(), defaults=None):
    """A manifest command; `defaults` maps a flag to the string the manifest prints for it."""
    defaults = defaults or {}
    parameters = [
        {
            "name": flag,
            "positional": False,
            "default": defaults.get(flag),
            "type": "string",
            "required": False,
        }
        for flag in flags
    ]
    parameters += [
        {"name": arg, "positional": True, "type": "string", "required": True}
        for arg in positional
    ]
    return {"name": name, "parameters": parameters}


MANIFEST = {
    "apiVersion": "microvm.v1",
    "type": "microvm.manifest",
    "data": {
        "commands": [
            command(
                "run",
                "remote",
                "timeout",
                positional=["binary"],
                defaults={"timeout": "300"},
            ),
            command("health", "endpoint", "name"),
            command("kill", "endpoint", "name", positional=["exec_id"]),
            command("cost", "estimate", "running-sec", defaults={"running-sec": "0"}),
            command(
                "ls",
                "remote",
                "interval-sec",
                "format",
                defaults={"interval-sec": "2", "format": "table"},
            ),
        ],
        "globalFlags": [
            {"name": "json", "positional": False, "type": "boolean", "required": False}
        ],
        "clientDefaults": {
            "execWaitSeconds": 300.0,
            "launch": {"wait": True},
            "offset": 0,
            "label": "run",
            "image": None,
        },
    },
}

CORE = {
    "declarations": {"microvms_core::sandbox::Sandbox::run": {"parameters": {}}},
    "paths": {
        "microvms_core::sandbox::Sandbox::run": "method",
        "microvms_core::session::Session::health": "method",
        "microvms_core::session::Session::kill": "method",
        "microvms_core::session::Session::attach": "method via microvms_core::prelude::SessionExt",
        "microvms_core::session::Session::file_exists": "method",
        "microvms_core::cost::run_report": "function",
    },
}


class FixtureCase(unittest.TestCase):
    """The fixture surfaces and table, and the checker over them."""

    def setUp(self):
        self.table = TABLE
        self.griffe = copy.deepcopy(GRIFFE)
        self.typedoc = copy.deepcopy(TYPEDOC)
        self.manifest = copy.deepcopy(MANIFEST)
        self.core = copy.deepcopy(CORE)

    def problems(self, unread=frozenset()):
        table = PARITY["parse_table"](self.table)
        surfaces = {
            "core": PARITY["core_surface"](self.core),
            "cli": PARITY["cli_surface"](self.manifest),
            "py": PARITY["py_surface"](self.griffe),
            "ts": PARITY["ts_surface"](self.typedoc),
        }
        return PARITY["check"](table, surfaces, unread)

    def assertProblem(self, fragment):
        problems = self.problems()
        self.assertTrue(
            any(fragment in problem for problem in problems),
            f"no problem mentions {fragment!r}; the check reported {problems}",
        )


class MainTests(FixtureCase):
    """The command must enforce the inventory it reads, and report what failed."""

    def setUp(self):
        super().setUp()
        self.observed = {
            surface: {f"type:{surface}Options": {"kind": "struct"}}
            for surface in ("core", "cli", "py", "ts")
        }
        self.inventory = {
            "schema": 1,
            "shapes": copy.deepcopy(self.observed),
            "exemptions": {},
            "contracts": [
                {
                    "id": "launch-options",
                    "kind": "type",
                    "capability": "launch",
                    **{
                        surface: list(items) for surface, items in self.observed.items()
                    },
                }
            ],
        }

    def run_main(
        self, *flags, inventory_error=False, surface_error=False, missing=False
    ):
        with tempfile.TemporaryDirectory() as scratch, contextlib.ExitStack() as stack:
            root = Path(scratch)
            table = root / "capabilities.toml"
            table.write_text(
                'inventory = "reviewed.json"\n' + self.table, encoding="utf-8"
            )
            if not missing:
                (root / "reviewed.json").write_text(
                    json.dumps(self.inventory), encoding="utf-8"
                )
            core = root / "core.json"
            core.write_text(json.dumps(self.core), encoding="utf-8")
            manifest = root / "commands.json"
            manifest.write_text(json.dumps(self.manifest), encoding="utf-8")
            pyi, dts = root / "api.pyi", root / "api.d.ts"
            argv = [
                "check-parity.py",
                "--table",
                str(table),
                "--core-api",
                str(core),
                "--manifest",
                str(manifest),
                "--pyi",
                str(pyi),
                "--dts",
                str(dts),
                *flags,
            ]
            stack.enter_context(mock.patch.object(sys, "argv", argv))
            stack.enter_context(
                mock.patch.dict(
                    PARITY_GLOBALS,
                    {
                        "run_griffe": mock.Mock(return_value=self.griffe),
                        "run_typedoc": mock.Mock(return_value=self.typedoc),
                        "pin_problems": mock.Mock(return_value=[]),
                    },
                )
            )
            readers = {}
            for surface, name in (
                ("core", "core"),
                ("cli", "cli"),
                ("py", "python"),
                ("ts", "typescript"),
            ):
                readers[surface] = stack.enter_context(
                    mock.patch.object(
                        PARITY_GLOBALS["parity_inventory"],
                        name,
                        return_value=self.observed[surface],
                    )
                )
            if inventory_error:
                readers["py"].side_effect = PARITY_GLOBALS[
                    "parity_inventory"
                ].InventoryError("unknown Python declaration")
            if surface_error:
                PARITY_GLOBALS["run_typedoc"].side_effect = PARITY["ParityError"](
                    "TypeScript unavailable"
                )
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = PARITY["main"]()
            if "--exemptions" not in flags:
                PARITY_GLOBALS["run_griffe"].assert_called_once_with(pyi)
                PARITY_GLOBALS["run_typedoc"].assert_called_once_with(dts, mock.ANY)
                readers["core"].assert_called_once_with(self.core)
                readers["cli"].assert_called_once_with(self.manifest)
            return code, out.getvalue(), err.getvalue()

    def test_reviewed_inventory_passes_in_human_and_json_modes(self):
        code, out, err = self.run_main()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(
            out,
            "parity: every row resolves on its surfaces, every name rule (a) holds has a row, and every default a surface states is core's or exempt\n",
        )
        code, out, err = self.run_main("--json")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(
            json.loads(out),
            {
                "ok": True,
                "problems": [],
                "exemptions": PARITY["exemptions"](PARITY["parse_table"](self.table)),
            },
        )

    def test_changed_shape_and_unknown_capability_fail_the_command(self):
        self.inventory["shapes"]["py"]["type:pyOptions"]["kind"] = "enum"
        self.inventory["contracts"][0]["capability"] = "not-a-capability"
        code, out, err = self.run_main("--json")
        self.assertEqual((code, err), (1, ""))
        self.assertEqual(json.loads(out)["ok"], False)
        self.assertEqual(
            json.loads(out)["problems"],
            [
                "inventory launch-options: unknown capability not-a-capability",
                "inventory py: type:pyOptions changed shape",
            ],
        )

    def test_missing_reviewed_inventory_is_a_failed_check(self):
        code, out, err = self.run_main("--json", missing=True)
        result = json.loads(out)
        self.assertEqual((code, err, result["ok"]), (1, "", False))
        self.assertEqual(len(result["problems"]), 1)
        self.assertIn("reviewed.json doesn't exist", result["problems"][0])

    def test_inventory_output_reports_all_collected_shapes_without_enrollment(self):
        code, out, err = self.run_main("--inventory", missing=True)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), self.observed)

    def test_inventory_output_refuses_incomplete_collectors_and_unread_surfaces(self):
        for options, message in (
            ({"inventory_error": True}, "inventory: unknown Python declaration"),
            ({"surface_error": True}, "TypeScript unavailable"),
        ):
            with self.subTest(options=options):
                code, out, err = self.run_main("--inventory", "--json", **options)
                self.assertEqual((code, out, err), (1, "", message + "\n"))

    def test_collector_failure_does_not_hide_a_missing_capability(self):
        self.griffe["members"].append(function("orphan"))
        code, out, err = self.run_main("--json", inventory_error=True)
        result = json.loads(out)
        self.assertEqual((code, err, result["ok"]), (1, "", False))
        self.assertIn("py: orphan belongs to no row", result["problems"])
        self.assertIn("inventory: unknown Python declaration", result["problems"])

    def test_unread_surface_remains_a_failure_in_json_mode(self):
        code, out, err = self.run_main("--json", surface_error=True)
        result = json.loads(out)
        self.assertEqual((code, err, result["ok"]), (1, "", False))
        self.assertEqual(result["problems"][0], "TypeScript unavailable")


class RuleTests(FixtureCase):
    """The rules over fixture surfaces, each case breaking one thing the baseline has."""

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
        self.table = self.table.replace('cli = { exempt = "no command" }\n', "")
        self.assertProblem("file-exists: cli: no cell")

    def test_an_exemption_with_an_empty_reason_fails(self):
        self.table = self.table.replace('exempt = "no command"', 'exempt = "  "')
        self.assertProblem("file-exists: cli: the exemption has an empty reason")

    def test_an_issue_that_is_not_a_number_fails(self):
        self.table = TRACKED.replace('issue = "#269"', 'issue = "TBD"')
        self.assertProblem("file-exists: cli: issue 'TBD' isn't of the form #<number>")

    def test_an_issue_on_an_exempt_member_is_held_to_the_same_form(self):
        self.table = TRACKED.replace('issue = "#267"', 'issue = "267"')
        self.assertProblem(
            "type Sandbox: ts: region: issue '267' isn't of the form #<number>"
        )

    def test_an_exemption_with_an_issue_is_refused_as_a_gap(self):
        """Parity-gap is enforced at zero (#280), so a tracked gap on a row or on a type's
        member fails the check, which names the two ways out.

        **Falsification**: `verify/guards/faults/parity-gap-enforced.toml` entry
        `parity-check-takes-a-gap` (the refusal is dropped, and both gaps pass).
        """
        self.table = TRACKED
        gap = (
            "an exemption with an issue is a parity gap, and parity-gap is enforced at zero "
            "(verify/ratchet/decisions.toml): give the surface the capability, or drop the "
            "issue and say why the surface won't have it"
        )
        self.assertEqual(
            sorted(self.problems()),
            [f"file-exists: cli: {gap}", f"type Sandbox: ts: region: {gap}"],
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

    def test_an_unread_surface_draws_nothing_and_the_others_are_still_held(self):
        # Its reason is reported already; a line per sentinel would only repeat it.
        self.typedoc["children"] = []
        self.griffe["members"].append(function("orphan"))
        self.assertEqual(
            self.problems(unread=frozenset(["ts"])), ["py: orphan belongs to no row"]
        )

    def test_an_unreadable_surface_is_reported_first_and_the_rest_still_checked(self):
        self.griffe["members"].append(function("orphan"))

        def refuse(dts, scratch):
            raise PARITY["ParityError"](f"{dts} has JSDoc tags TypeDoc drops")

        with tempfile.TemporaryDirectory() as scratch:
            table = Path(scratch) / "capabilities.toml"
            table.write_text(self.table, encoding="utf-8")
            out = io.StringIO()
            with (
                mock.patch.dict(
                    PARITY_GLOBALS,
                    {
                        "read_json": lambda path: (
                            self.core if path.name == "core-api.json" else self.manifest
                        ),
                        "run_griffe": lambda pyi: self.griffe,
                        "run_typedoc": refuse,
                    },
                ),
                mock.patch.object(
                    sys, "argv", ["check-parity.py", "--table", str(table)]
                ),
                contextlib.redirect_stdout(out),
            ):
                code = PARITY["main"]()
        lines = out.getvalue().splitlines()
        self.assertEqual(code, 1)
        self.assertEqual(
            lines[0], f"parity: {PARITY['DTS']} has JSDoc tags TypeDoc drops"
        )
        self.assertEqual(
            lines[1:],
            [
                f"parity: {table} doesn't match the surfaces:",
                "  py: orphan belongs to no row",
                "  table: name the reviewed inventory file",
            ],
        )

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

    def test_an_awaitable_twin_pairs_with_its_blocking_spellings_typescript_twin(self):
        """`run_async` pairs with TypeScript's `run`, as `run` does, so a twin needs no
        exemption; and `create_async` is the Python twin of TypeScript's `create`."""
        members = self.griffe["members"][0]["members"]
        members.append(function("run_async"))
        unpaired = "type Sandbox: py: run_async has no TypeScript twin and no exemption"
        self.assertFalse(any(unpaired in problem for problem in self.problems()))
        self.assertProblem("py: Sandbox.run_async belongs to no row")
        members.append(function("create_async"))
        self.assertProblem("type Sandbox: ts: create is exempt but has a Python twin")

    def test_an_exempt_awaitable_twin_with_a_typescript_twin_fails(self):
        """An exemption for `run_async` is stale: it pairs with TypeScript's `run`."""
        self.griffe["members"][0]["members"].append(function("run_async"))
        self.table = self.table.replace(
            'name = "Sandbox"\n',
            'name = "Sandbox"\nexempt_members.py = { run_async = "stale" }\n',
        )
        self.assertProblem(
            "type Sandbox: py: run_async is exempt but has a TypeScript twin"
        )

    def test_only_a_python_name_drops_the_async_suffix(self):
        """The suffix rule is Python's: a TypeScript member spelled `run_async` keeps its whole
        name, so it doesn't pair with Python's `run` the way Python's `run_async` would."""
        self.typedoc["children"][0]["children"].append(member("run_async"))
        self.assertProblem(
            "type Sandbox: ts: run_async has no Python twin and no exemption"
        )

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


class DefaultValueComparisonTests(unittest.TestCase):
    """Default spellings retain each adapter's scalar semantics."""

    def test_cli_strings_keep_their_raw_text_and_case(self):
        for text, value, expected in [
            ("table", "table", False),
            ("", "", False),
            ('"run"', '"run"', False),
            ("table", "json", True),
            ("RUN", "run", True),
            ('"run"', "run", True),
        ]:
            with self.subTest(text=text, value=value):
                self.assertEqual(PARITY["differs"](text, value, "cli"), expected)

    def test_cli_booleans_accept_case_without_losing_the_value(self):
        for text, value, expected in [
            ("true", True, False),
            ("TRUE", True, False),
            ("fAlSe", False, False),
            ("false", True, True),
            ("true", False, True),
        ]:
            with self.subTest(text=text, value=value):
                self.assertEqual(PARITY["differs"](text, value, "cli"), expected)

    def test_invalid_python_and_typescript_literals_are_not_equal_defaults(self):
        for key, text in [("py", "'"), ("py", "missing"), ("ts", "'run'")]:
            with self.subTest(key=key, text=text):
                self.assertTrue(PARITY["differs"](text, None, key))

    def test_numeric_literals_compare_values_across_integer_and_float_spellings(self):
        for key in ("py", "ts"):
            for text, value, expected in [
                ("1", 1.0, False),
                ("1.0", 1, False),
                ("2.0", 1, True),
                ("1", 2.0, True),
            ]:
                with self.subTest(key=key, text=text, value=value):
                    self.assertEqual(PARITY["differs"](text, value, key), expected)

    def test_booleans_and_numbers_remain_distinct_defaults(self):
        for key, text, value in [
            ("py", "True", 1),
            ("py", "False", 0),
            ("py", "1", True),
            ("py", "0", False),
            ("ts", "true", 1),
            ("ts", "false", 0),
            ("ts", "1", True),
            ("ts", "0", False),
        ]:
            with self.subTest(key=key, text=text, value=value):
                self.assertTrue(PARITY["differs"](text, value, key))


class TypeScriptDefaultTests(unittest.TestCase):
    """Scalar defaults and overloads in TypeDoc's serialized signature shape."""

    def read(self, *parameters):
        return PARITY["ts_surface"](
            {
                "kind": 1,
                "name": "Documentation",
                "children": [
                    {
                        "kind": FUNCTION,
                        "name": "run",
                        "signatures": [
                            {"parameters": [parameter]} for parameter in parameters
                        ],
                    }
                ],
            }
        ).defaults

    def tagged(self, *parts):
        return {
            "name": "value",
            "comment": {
                "blockTags": [
                    {
                        "tag": "@defaultValue",
                        "content": [{"kind": "text", "text": part} for part in parts],
                    }
                ]
            },
        }

    def test_native_defaults_are_read_before_optional_omission(self):
        self.assertEqual(
            self.read(
                {"name": "value", "defaultValue": "42", "flags": {"isOptional": True}}
            ),
            {"run(value)": "42"},
        )

    def test_tag_parts_concatenate_and_fenced_scalars_keep_whitespace(self):
        for parts, expected in [
            (('"', "run", '"'), '"run"'),
            (("```json\n \n0\n \n```",), "0"),
            (("undefined",), "undefined"),
        ]:
            with self.subTest(parts=parts):
                self.assertEqual(
                    self.read(self.tagged(*parts)), {"run(value)": expected}
                )

    def test_fenced_tags_match_markdown_backtick_lengths(self):
        for value in (
            "```json\n0\n```",
            "```json\n0\n  ````  ",
            "````json\n0\n````",
        ):
            with self.subTest(value=value):
                self.assertEqual(self.read(self.tagged(value)), {"run(value)": "0"})

    def test_complete_fenced_scalars_accept_supported_lines_and_spacing(self):
        for newline in ("\n", "\r\n", "\r"):
            for info in ("", "json", "json extra-info"):
                for indent in range(4):
                    with self.subTest(newline=newline, info=info, indent=indent):
                        value = newline.join(
                            ["```" + info, "0", " " * indent + "``` \t"]
                        )
                        self.assertEqual(
                            self.read(self.tagged(value)), {"run(value)": "0"}
                        )

    def test_fenced_tags_reject_malformed_openers_and_line_boundaries(self):
        for value in (
            "```json`invalid\n0\n```",
            "``` `\n0\n```",
            "```json\n0\n    ```",
            "```json\n0\n\t```",
            "```json\u20280\u2028```",
            "```json\u00850\u0085```",
            "```json\x0b0\x0b```",
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    PARITY["ParityError"], "malformed @defaultValue"
                ):
                    self.read(self.tagged(value))

    def test_native_initializer_cannot_be_replaced_by_a_documentation_tag(self):
        for tag in ("3", "0", "invalid"):
            with self.subTest(tag=tag):
                parameter = self.tagged(tag)
                parameter["defaultValue"] = "3"
                self.assertEqual(self.read(parameter), {"run(value)": "3"})

    def test_callable_reference_cycles_keep_other_signature_branches(self):
        reference = {"type": "reference", "target": 1}
        surface = PARITY["ts_surface"](
            {
                "kind": 1,
                "name": "Documentation",
                "children": [
                    {"kind": 32, "name": "run", "type": reference},
                    {
                        "kind": 2097152,
                        "name": "Recursive",
                        "id": 1,
                        "type": {
                            "type": "union",
                            "types": [
                                reference,
                                {
                                    "type": "reflection",
                                    "declaration": {
                                        "signatures": [
                                            {
                                                "parameters": [
                                                    {
                                                        "name": "count",
                                                        "flags": {"isOptional": True},
                                                    }
                                                ]
                                            }
                                        ]
                                    },
                                },
                            ],
                        },
                    },
                ],
            }
        )
        self.assertEqual(surface.defaults, {"run(count)": "undefined"})

    def test_fenced_tags_require_a_complete_closing_fence(self):
        for value in (
            "```json\n0\nnot-a-closing-fence",
            "```json\n0\n``` trailing garbage",
            "```json\n0\n```\ntrailing garbage",
            "```json\n0",
            "````json\n0\n```",
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    PARITY["ParityError"], "malformed @defaultValue"
                ):
                    self.read(self.tagged(value))

    def test_invalid_tag_values_report_the_reason(self):
        for value, reason in [
            ("invalid", "malformed @defaultValue"),
            ("[]", "must be a scalar"),
            ("NaN", "nonfinite @defaultValue"),
        ]:
            with self.subTest(value=value):
                with self.assertRaisesRegex(PARITY["ParityError"], reason):
                    self.read(self.tagged(value))
        parameter = self.tagged("0")
        parameter["comment"]["blockTags"] *= 2
        with self.assertRaisesRegex(PARITY["ParityError"], "duplicate @defaultValue"):
            self.read(parameter)

    def test_overloads_agree_or_report_conflicting_defaults(self):
        self.assertEqual(
            self.read(self.tagged("0"), self.tagged("0")), {"run(value)": "0"}
        )
        with self.assertRaisesRegex(
            PARITY["ParityError"], "conflicting overload defaults"
        ):
            self.read(self.tagged("0"), self.tagged("1"))


class DefaultTests(FixtureCase):
    """The `[[default]]` rules (#300) over the same fixtures, which state defaults the rows hold."""

    def flag(self, command, name):
        return next(
            parameter
            for entry in self.manifest["data"]["commands"]
            if entry["name"] == command
            for parameter in entry["parameters"]
            if parameter["name"] == name
        )

    def parameter(self, cls, method, name):
        owner = next(item for item in self.griffe["members"] if item["name"] == cls)
        function = next(item for item in owner["members"] if item["name"] == method)
        return next(item for item in function["parameters"] if item["name"] == name)

    def default_problems(self, empty):
        surfaces = {
            "cli": PARITY["cli_surface"](self.manifest),
            "py": PARITY["py_surface"](self.griffe),
            "ts": PARITY["ts_surface"](self.typedoc),
        }
        return PARITY["default_problems"](
            PARITY["parse_table"](self.table)["default"], surfaces, empty
        )

    def test_the_baseline_states_defaults_and_holds_them(self):
        cli = PARITY["cli_surface"](self.manifest)
        self.assertEqual(cli.defaults["run --timeout"], "300")
        py = PARITY["py_surface"](self.griffe)
        self.assertEqual(py.defaults["Session.health(timeout)"], "300.0")
        self.assertEqual(py.defaults["Session.attach(retries)"], "...")
        self.assertEqual(self.problems(), [])

    def test_a_cli_default_no_row_holds_fails(self):
        self.manifest["data"]["commands"][1]["parameters"].append(
            {"name": "wait", "positional": False, "default": "120"}
        )
        self.assertProblem("cli: health --wait = 120 belongs to no [[default]] row")

    def test_a_python_default_no_row_holds_fails(self):
        self.parameter("Session", "kill", "offset")["default"] = "False"
        self.assertProblem(
            "Session.kill(offset) defaults to False, but clientDefaults.offset is 0"
        )

    def test_zero_absent_and_string_defaults_are_held(self):
        for value in ["0", "0.0", "None", "''", "'run'", "False"]:
            self.assertTrue(PARITY["held"]("py", value))

    def test_string_defaults_compare_values_and_do_not_coerce_numbers(self):
        self.manifest["data"]["clientDefaults"]["label"] = "other"
        self.assertProblem('run_report(label) defaults to "run"')
        self.assertTrue(PARITY["differs"]('"0"', 0))
        self.assertTrue(PARITY["differs"]("False", 0))
        self.assertFalse(PARITY["differs"]("''", ""))

    def test_zero_defaults_are_enrolled_and_compared(self):
        self.parameter("Session", "kill", "offset")["default"] = "1"
        self.assertProblem(
            "Session.kill(offset) defaults to 1, but clientDefaults.offset is 0"
        )
        self.table = self.table.replace('py = "Session.kill(offset)"', "")
        self.parameter("Session", "kill", "offset")["default"] = "0"
        self.assertProblem("Session.kill(offset) = 0 belongs to no [[default]] row")

    def test_absent_defaults_are_values_and_missing_defaults_are_refused(self):
        self.manifest["data"]["clientDefaults"]["image"] = "image"
        self.assertProblem("Sandbox.run(image) defaults to None")
        self.assertProblem("Sandbox.run(image) defaults to undefined")
        self.parameter("Sandbox", "run", "image")["default"] = None
        self.assertProblem("py: Sandbox.run(image) states no default")

    def test_exempt_omission_markers_are_pinned(self):
        self.flag("ls", "format")["default"] = "json"
        self.assertProblem("defaults to json, but its exemption pins table")

    def test_exempt_defaults_cannot_drop_their_value_pin(self):
        self.table = self.table.replace('value = "table"\n', "")
        self.assertProblem("default table-format: an exempt default must pin its value")

    def test_typescript_default_tags_are_compared_and_cannot_disappear(self):
        parameter = self.typedoc["children"][0]["children"][1]["signatures"][0][
            "parameters"
        ][0]
        parameter["comment"] = {
            "blockTags": [{"tag": "@defaultValue", "content": [{"text": '"changed"'}]}]
        }
        self.assertProblem('ts: Sandbox.run(image) defaults to "changed"')
        parameter["flags"] = {}
        del parameter["comment"]
        self.assertProblem("ts: Sandbox.run(image) states no default")

    def test_typescript_tags_refuse_malformed_duplicate_and_container_values(self):
        for values in [["bad"], ["{}"], ["NaN"], ["Infinity"], ["0", "0"]]:
            with self.subTest(values=values):
                parameter = self.typedoc["children"][0]["children"][1]["signatures"][0][
                    "parameters"
                ][0]
                parameter["comment"] = {
                    "blockTags": [
                        {"tag": "@defaultValue", "content": [{"text": value}]}
                        for value in values
                    ]
                }
                with self.assertRaises(PARITY["ParityError"]):
                    self.problems()

    def test_global_and_positional_cli_defaults_are_enrolled(self):
        self.manifest["data"]["globalFlags"][0]["default"] = "false"
        self.manifest["data"]["commands"][0]["parameters"][-1]["default"] = "binary"
        self.assertProblem("@global --json = false belongs to no [[default]] row")
        self.assertProblem("run binary = binary belongs to no [[default]] row")

    def test_a_cli_default_that_differs_from_cores_fails(self):
        self.flag("run", "timeout")["default"] = "301"
        self.assertProblem(
            "default exec-wait: cli: run --timeout defaults to 301, but "
            "clientDefaults.execWaitSeconds is 300.0"
        )

    def test_a_changed_core_value_fails_every_restatement(self):
        self.manifest["data"]["clientDefaults"]["execWaitSeconds"] = 301.0
        problems = self.problems()
        self.assertIn(
            "default exec-wait: cli: run --timeout defaults to 300, but "
            "clientDefaults.execWaitSeconds is 301.0",
            problems,
        )
        self.assertIn(
            "default exec-wait: py: Session.health(timeout) defaults to 300.0, but "
            "clientDefaults.execWaitSeconds is 301.0",
            problems,
        )

    def test_a_boolean_default_is_compared_as_a_boolean(self):
        self.parameter("Sandbox", "run", "wait")["default"] = "False"
        self.assertProblem(
            "default launch-wait: py: Sandbox.run(wait) defaults to False, but "
            "clientDefaults.launch.wait is true"
        )
        self.parameter("Sandbox", "run", "wait")["default"] = "1"
        self.assertProblem("Sandbox.run(wait) defaults to 1")

    def test_a_named_constant_default_is_declared_not_compared(self):
        # `...` is what the stub prints for a default named for a constant: its row is the check.
        self.manifest["data"]["clientDefaults"]["execWaitSeconds"] = 300.0
        problems = self.problems()
        self.assertFalse([problem for problem in problems if "attach" in problem])

    def test_ellipsis_is_only_a_python_named_constant_marker(self):
        self.flag("ls", "format")["default"] = "..."
        self.table = self.table.replace(
            'exempt = "a terminal-only rendering selection"\nvalue = "table"',
            'core = "label"',
        )
        self.assertProblem(
            "default table-format: cli: ls --format defaults to ..., but "
            'clientDefaults.label is "run"'
        )
        surfaces = {
            "cli": PARITY["cli_surface"](self.manifest),
            "py": PARITY["py_surface"](self.griffe),
            "ts": PARITY["ts_surface"](self.typedoc),
        }
        surfaces["ts"].defaults["Sandbox.run(image)"] = "..."
        self.assertIn(
            "default absent: ts: Sandbox.run(image) defaults to ..., but "
            "clientDefaults.image is null",
            PARITY["default_problems"](
                PARITY["parse_table"](self.table)["default"], surfaces, set()
            ),
        )

    def test_a_row_naming_a_default_the_surface_does_not_state_fails(self):
        self.flag("run", "timeout")["default"] = None
        self.assertProblem("default exec-wait: cli: run --timeout states no default")

    def test_a_default_two_rows_name_fails(self):
        self.table = self.table.replace(
            'cli = "ls --interval-sec"', 'cli = ["ls --interval-sec", "run --timeout"]'
        )
        self.assertProblem(
            "default watch-interval: cli: run --timeout is also in default exec-wait"
        )

    def test_a_core_key_client_defaults_lacks_fails(self):
        self.table = self.table.replace('core = "launch.wait"', 'core = "launch.wiat"')
        self.assertProblem("default launch-wait: clientDefaults has no launch.wiat")

    def test_a_manifest_without_client_defaults_fails(self):
        del self.manifest["data"]["clientDefaults"]
        self.assertProblem(
            "cli: the manifest carries no clientDefaults to compare with"
        )

    def test_a_row_with_both_a_core_key_and_an_exemption_fails(self):
        self.table = self.table.replace(
            'exempt = "a terminal affordance"',
            'exempt = "a terminal affordance"\ncore = "execWaitSeconds"',
        )
        self.assertProblem(
            "default watch-interval: a row has a core key or an exemption, not both"
        )

    def test_an_exempt_default_with_an_empty_reason_fails(self):
        self.table = self.table.replace(
            'exempt = "a terminal affordance"', 'exempt = " "'
        )
        self.assertProblem("default watch-interval: the exemption has an empty reason")

    def test_a_typescript_cell_must_name_a_stated_default(self):
        self.table = self.table.replace(
            'py = "Sandbox.run(wait)"',
            'py = "Sandbox.run(wait)"\nts = "Sandbox.run(wait)"',
        )
        self.assertProblem(
            "default launch-wait: ts: Sandbox.run(wait) states no default"
        )

    def test_a_surface_stating_no_default_fails_the_floor(self):
        for entry in self.manifest["data"]["commands"]:
            for parameter in entry["parameters"]:
                parameter["default"] = None
        self.assertProblem("cli: the surface states no default the rows hold")

    def test_a_table_with_no_sentinel_default_fails(self):
        self.table = self.table.replace(
            'id = "exec-wait"\nsentinel = true', 'id = "exec-wait"'
        )
        self.assertProblem("the table marks no sentinel [[default]] row")

    def test_a_sentinel_whose_defaults_are_not_compared_fails(self):
        # a reader that stopped returning values would leave the sentinel's defaults uncompared
        self.flag("run", "timeout")["default"] = None
        self.parameter("Session", "health", "timeout")["default"] = "..."
        self.assertProblem(
            "sentinel default exec-wait: no stated default was compared with core's"
        )

    def test_an_unknown_default_key_fails(self):
        self.table = self.table.replace(
            'core = "launch.wait"', 'core = "launch.wait"\nnote = "x"'
        )
        self.assertProblem("default launch-wait: unknown key note")

    def test_duplicate_default_ids_fail_with_distinct_selectors(self):
        self.table = self.table.replace('id = "launch-wait"', 'id = "exec-wait"')
        self.assertEqual(
            self.problems(),
            ["default exec-wait: the id is used by more than one row"],
        )

    def test_value_pins_require_an_exemption_and_source_spelling(self):
        for before, after, row_id in [
            (
                'core = "launch.wait"',
                'core = "launch.wait"\nvalue = "True"',
                "launch-wait",
            ),
            ('value = "table"', "value = 0", "table-format"),
        ]:
            with self.subTest(row_id=row_id):
                self.table = TABLE.replace(before, after)
                self.assertIn(
                    f"default {row_id}: value pins an exempt default's source spelling",
                    self.problems(),
                )

    def test_invalid_cell_does_not_hide_a_later_surface_mismatch(self):
        self.table = self.table.replace('cli = "run --timeout"', "cli = 123")
        self.parameter("Session", "health", "timeout")["default"] = "301"
        problems = self.problems()
        self.assertIn(
            "default exec-wait: cli: a cell is a default's name or a list of them",
            problems,
        )
        self.assertIn(
            "default exec-wait: py: Session.health(timeout) defaults to 301, but "
            "clientDefaults.execWaitSeconds is 300.0",
            problems,
        )

    def test_missing_default_does_not_hide_a_later_selector_mismatch(self):
        self.parameter("Session", "health", "timeout")["default"] = None
        self.parameter("Session", "attach", "retries")["default"] = "301"
        self.assertEqual(
            self.problems(),
            [
                "default exec-wait: py: Session.health(timeout) states no default",
                "default exec-wait: py: Session.attach(retries) defaults to 301, but "
                "clientDefaults.execWaitSeconds is 300.0",
            ],
        )

    def test_unread_surface_still_claims_every_selector(self):
        self.table = self.table.replace(
            'py = "Sandbox.run(wait)"',
            'py = ["Sandbox.run(wait)", "Session.attach(retries)"]',
        )
        self.assertEqual(
            self.default_problems(empty={"py"}),
            [
                "default launch-wait: py: Session.attach(retries) is also in "
                "default exec-wait"
            ],
        )

    def test_unread_manifest_does_not_report_missing_client_defaults(self):
        del self.manifest["data"]["clientDefaults"]
        self.assertEqual(
            self.default_problems(empty={"cli"}),
            ["sentinel default exec-wait: no stated default was compared with core's"],
        )

    def test_unread_surface_does_not_hide_later_unclaimed_defaults(self):
        self.table = self.table.replace('py = "Sandbox.run(wait)"', "")
        self.assertEqual(
            self.default_problems(empty={"cli"}),
            ["py: Sandbox.run(wait) = True belongs to no [[default]] row"],
        )


class ExemptionRecordTests(unittest.TestCase):
    """`--json`'s exemption records, the contract the ratchet's parity-gap category reads."""

    def test_each_exemption_is_one_record_with_its_issue(self):
        records = PARITY["exemptions"](PARITY["parse_table"](TRACKED))
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
        # The tracked variant: the mode reports a gap the full check refuses, which is how a
        # gap that reached the table still counts as drift in the ratchet's enforced category.
        done = self.exemptions_mode(TRACKED)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(
            json.loads(done.stdout),
            {"exemptions": PARITY["exemptions"](PARITY["parse_table"](TRACKED))},
        )

    def test_the_exemptions_mode_refuses_an_issue_it_cannot_read(self):
        # A malformed issue would read as a decision, and the ratchet would stop counting it.
        done = self.exemptions_mode(TRACKED.replace('issue = "#269"', 'issue = "TBD"'))
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
    def health(self, /, timeout: float = 300.0, wait: bool = ..., label: str | None = None) -> bool: ...
    @staticmethod
    def attach(endpoint: str) -> Session: ...

def run_report() -> str: ...
__version__: str
"""

DECLARATIONS = """
// SPDX-License-Identifier: Apache-2.0
export declare class Session {
  get endpoint(): string
  health(options?: RunOptions): Promise<boolean>
  static attach(endpoint: string): Promise<Session>
  bytes(): Buffer
}
export declare function runReport(): string
export declare const __napiBindingTarget: string
export interface BaseOptions {
  /** @defaultValue 9 */
  inherited?: number
}
export interface RunOptions extends BaseOptions {
  /** @defaultValue 0 */
  timeout?: number
  /** @defaultValue "" */
  label?: string
  nested?: {
    /** @defaultValue null */
    value?: string | null
  }
  other?: OtherOptions
}
export type OtherOptions = {
  /** @defaultValue false */
  enabled?: boolean
}
export declare namespace snapshots {
  export function snapshotVm(): void
}
export declare function accept(options: AliasOptions, callback: CallbackOptions): void
export type AliasOptions = BaseOptions | {
  local?: {
    /** @defaultValue true */
    enabled?: boolean
  }
}
export interface CallbackOptions {
  invoke?: {
    (flag?: boolean): OutputOnly
    optional?: number
  }
  next?: CallbackOptions
}
export interface OutputOnly {
  ignored?: number
}
export declare function list(options: Array<{
  /** @defaultValue 7 */
  size?: number
}>): void
export declare function readonlyList(options: readonly {
  /** @defaultValue 8 */
  size?: number
}[]): void
export interface GenericBase<T> {
  inherited?: T
}
export interface GenericInput extends GenericBase<{ flag?: boolean }> {}
export declare function generic(options: GenericInput): void
export declare class NativeDefaults {
  readonly count = 3
}
export declare function native(options: NativeDefaults): void
export declare const DEFAULT_COUNT = 7
export declare function wholeModule(value: typeof import("./index")): void
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
        # Defaults retain the stub's spelling, including named constants and absence.
        self.assertEqual(
            surface.defaults,
            {
                "Session.health(timeout)": "300.0",
                "Session.health(wait)": "...",
                "Session.health(label)": "None",
            },
        )
        self.assertTrue(PARITY["held"]("py", surface.defaults["Session.health(label)"]))

    def test_the_typescript_reader_over_typedoc(self):
        with tempfile.TemporaryDirectory() as scratch:
            declarations = Path(scratch) / "index.d.ts"
            declarations.write_text(DECLARATIONS, encoding="utf-8")
            project = PARITY["run_typedoc"](declarations, Path(scratch))
            surface = PARITY["ts_surface"](project)
        self.assertEqual(
            surface.functions,
            {
                "runReport",
                "__napiBindingTarget",
                "snapshots.snapshotVm",
                "accept",
                "list",
                "readonlyList",
                "generic",
                "native",
                "wholeModule",
                "DEFAULT_COUNT",
            },
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
        self.assertEqual(
            surface.defaults,
            {
                "Session.health(options)": "undefined",
                "RunOptions.timeout": "0",
                "RunOptions.label": '""',
                "RunOptions.nested": "undefined",
                "RunOptions.nested.value": "null",
                "RunOptions.other": "undefined",
                "OtherOptions.enabled": "false",
                "RunOptions.inherited": "9",
                "BaseOptions.inherited": "9",
                "AliasOptions.local": "undefined",
                "AliasOptions.local.enabled": "true",
                "CallbackOptions.invoke": "undefined",
                "CallbackOptions.invoke.optional": "undefined",
                "CallbackOptions.invoke(flag)": "undefined",
                "CallbackOptions.next": "undefined",
                "list(options).size": "7",
                "readonlyList(options).size": "8",
                "GenericBase.inherited": "undefined",
                "GenericInput.flag": "undefined",
                "GenericInput.inherited": "undefined",
                "GenericInput.inherited.flag": "undefined",
                "NativeDefaults.count": "3",
                f"{project['name']}.AliasOptions.local": "undefined",
                f"{project['name']}.AliasOptions.local.enabled": "true",
                f"{project['name']}.DEFAULT_COUNT": "7",
            },
        )

    def test_typescript_callable_values_read_inputs_but_not_return_records(self):
        declarations_text = """\
export interface OutputOnly {
  ignored?: number;
  callback: (hidden?: number) => void;
}
export declare function anchor(count?: number): OutputOnly;
export declare function factory(): (hidden?: number) => void;
export declare const run: (count?: number) => OutputOnly;
export type Runner = (count?: number) => OutputOnly;
export type RunnerAlias = Runner;
export declare const aliased: RunnerAlias;
export type UnionRunner = Runner | ((other?: boolean) => OutputOnly);
export declare const union: UnionRunner;
export declare const constants: readonly number[];
export interface Callable { (enabled?: boolean): OutputOnly; }
export declare const callableInterface: Callable;
export declare class Box {
  run: (count?: number) => OutputOnly;
  aliased: Runner;
}
export declare namespace nested {
  export const run: (options: { enabled?: boolean }) => OutputOnly;
}
"""
        with tempfile.TemporaryDirectory() as scratch:
            declarations = Path(scratch) / "index.d.ts"
            declarations.write_text(declarations_text, encoding="utf-8")
            surface = PARITY["ts_surface"](
                PARITY["run_typedoc"](declarations, Path(scratch))
            )
        self.assertEqual(
            surface.defaults,
            {
                "anchor(count)": "undefined",
                "run(count)": "undefined",
                "aliased(count)": "undefined",
                "union(count)": "undefined",
                "union(other)": "undefined",
                "Callable(enabled)": "undefined",
                "callableInterface(enabled)": "undefined",
                "Box.run(count)": "undefined",
                "Box.aliased(count)": "undefined",
                "nested.run(options).enabled": "undefined",
            },
        )

    def test_python_overloads_are_read_and_must_agree(self):
        stub_text = """\
from typing import Literal, overload
@overload
def run(mode: Literal["a"], timeout: float = 0.0) -> str: ...
@overload
def run(mode: Literal["b"], timeout: float = 0.0) -> bytes: ...
"""
        with tempfile.TemporaryDirectory() as scratch:
            stub = Path(scratch) / "microvms.pyi"
            stub.write_text(stub_text, encoding="utf-8")
            surface = PARITY["py_surface"](PARITY["run_griffe"](stub))
            self.assertEqual(surface.defaults, {"run(timeout)": "0.0"})
            stub.write_text(stub_text.replace("= 0.0) -> bytes", "= 1.0) -> bytes"))
            with self.assertRaisesRegex(
                PARITY["ParityError"],
                "run\\(timeout\\) has conflicting overload defaults",
            ):
                PARITY["py_surface"](PARITY["run_griffe"](stub))

    def test_python_constructor_defaults_use_the_class_name_and_must_agree(self):
        stub_text = """\
class Example:
    def __new__(cls, count: int = 0) -> Example: ...
    def __init__(self, count: int = 0) -> None: ...
"""
        with tempfile.TemporaryDirectory() as scratch:
            stub = Path(scratch) / "microvms.pyi"
            stub.write_text(stub_text, encoding="utf-8")
            surface = PARITY["py_surface"](PARITY["run_griffe"](stub))
            self.assertEqual(surface.defaults, {"Example(count)": "0"})
            stub.write_text(
                stub_text.replace("count: int = 0) -> None", "count: int = 1) -> None")
            )
            with self.assertRaisesRegex(
                PARITY["ParityError"],
                "Example\\(count\\) has conflicting overload defaults",
            ):
                PARITY["py_surface"](PARITY["run_griffe"](stub))

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


# Stands in for npx. Locating typedoc succeeds only while someone else holds the lock beside the
# npm cache exclusively, so a locate that runs unlocked, or under a shared lock, fails. The probe
# is shared because an exclusive probe is refused by a held shared lock too. The TypeDoc run
# writes an empty project.
FAKE_NPX = """#!{python}
import fcntl, json, os, sys
argv = sys.argv[1:]
if argv[-2:] == ["-c", "command -v typedoc"]:
    with open(os.environ["FAKE_NPX_LOCK"], "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            cache = os.environ["npm_config_cache"]
            print(os.path.join(cache, "_npx", "0", "node_modules", ".bin", "typedoc"))
            sys.exit(0)
    print("npx ran with the lock free")
    sys.exit(3)
with open(argv[argv.index("--json") + 1], "w") as out:
    json.dump({{"children": []}}, out)
"""


def first_on_path(directory: Path) -> dict[str, str]:
    return {"PATH": f"{directory}{os.pathsep}{os.environ['PATH']}"}


class NpxLockTests(unittest.TestCase):
    """Concurrent callers installing TypeDoc into one npx cache extract over each other (#347).

    The first case runs the real npm, which answers `npm config get cache` from
    `npm_config_cache`; the others put a fake npm first on PATH. None of them touches the
    caller's `~/.npm`.
    """

    def test_typedoc_is_located_with_the_npx_lock_held(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            npx = bin_dir / "npx"
            npx.write_text(FAKE_NPX.format(python=sys.executable), encoding="utf-8")
            npx.chmod(0o755)
            cache = root / "cache"
            # a used cache that lacks this package set, which is what the race needs, so a
            # lock taken only on an empty cache fails too
            (cache / "_npx" / "other").mkdir(parents=True)
            declarations = root / "index.d.ts"
            declarations.write_text(DECLARATIONS, encoding="utf-8")
            globals_reader = root / "globals.cjs"
            globals_reader.write_text(
                "process.stdout.write(JSON.stringify({globals: {}, exports: []}));",
                encoding="utf-8",
            )
            env = {
                **first_on_path(bin_dir),
                "npm_config_cache": str(cache),
                # spelled here, not asked of the helper, so a lock on any other file fails
                "FAKE_NPX_LOCK": str(cache / "microvms-agentd-npx.lock"),
            }
            with (
                mock.patch.dict(os.environ, env),
                mock.patch.dict(PARITY_GLOBALS, {"TS_GLOBALS": globals_reader}),
            ):
                try:
                    project = PARITY["run_typedoc"](declarations, root)
                except PARITY["ParityError"] as error:
                    self.fail(str(error))
        self.assertEqual(project, {"children": [], "globalDeclarations": {}})

    def test_a_second_caller_waits_for_the_lock(self):
        # The test holds the lock itself; flock locks belong to an open file, so the helper's
        # own open() in another thread conflicts with it. The helper's flock calls are recorded
        # and must be one try and then one blocking wait, both exclusive: a lock that raises,
        # polls and then gives up, or waits shared lets a second install in while the first
        # is still extracting. The wait itself has to say so on stderr.
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            cache = root / "cache"
            cache.mkdir()
            npm = root / "npm"
            npm.write_text(f"#!/bin/sh\necho '{cache}'\n", encoding="utf-8")
            npm.chmod(0o755)
            errors: list[BaseException] = []
            entered = threading.Event()
            waiting = threading.Event()
            calls: list[int] = []
            real_flock = fcntl.flock

            def recording_flock(fd, operation):
                if threading.current_thread() is caller:
                    calls.append(operation)
                    if not operation & fcntl.LOCK_NB:
                        waiting.set()
                return real_flock(fd, operation)

            def second_caller():
                try:
                    with PARITY["npx_lock"]():
                        entered.set()
                except BaseException as error:  # noqa: BLE001 - reported by the test below
                    errors.append(error)

            caller = threading.Thread(target=second_caller, daemon=True)
            stderr = io.StringIO()
            with (
                mock.patch.dict(os.environ, first_on_path(root)),
                (cache / "microvms-agentd-npx.lock").open("a") as held,
            ):
                fcntl.flock(held, fcntl.LOCK_EX)
                with (
                    mock.patch.object(fcntl, "flock", recording_flock),
                    contextlib.redirect_stderr(stderr),
                ):
                    try:
                        caller.start()
                        deadline = time.monotonic() + 30
                        while caller.is_alive() and not entered.is_set():
                            if waiting.is_set() or time.monotonic() > deadline:
                                break
                            time.sleep(0.02)
                        self.assertTrue(
                            caller.is_alive() and not entered.is_set(),
                            f"the second caller didn't wait for the held lock: {errors!r}",
                        )
                    finally:
                        real_flock(held, fcntl.LOCK_UN)
                    caller.join(timeout=30)
            self.assertFalse(caller.is_alive(), "the second caller never got the lock")
            self.assertEqual(errors, [])
            self.assertTrue(entered.is_set())
            self.assertEqual(
                calls,
                [fcntl.LOCK_EX | fcntl.LOCK_NB, fcntl.LOCK_EX],
                "the helper must try once, then block exclusively until the lock is free",
            )
            self.assertIn(f"parity: waiting for {cache}", stderr.getvalue())

    def test_an_npm_that_names_no_cache_is_refused(self):
        # A blank answer would lock a file in the working directory, which no other caller
        # shares, so the lock would hold nothing back.
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            npm = root / "npm"
            npm.write_text("#!/bin/sh\necho\n", encoding="utf-8")
            npm.chmod(0o755)
            with (
                mock.patch.dict(os.environ, first_on_path(root)),
                contextlib.chdir(root),
                self.assertRaisesRegex(
                    PARITY["ParityError"], "not a directory to lock"
                ),
                PARITY["npx_lock"](),
            ):
                pass


if __name__ == "__main__":
    unittest.main()
