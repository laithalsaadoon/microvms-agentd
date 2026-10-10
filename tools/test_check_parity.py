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
cli = "ls --interval-sec"
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
            {"name": "self", "default": None},
            *({"name": key, "default": value} for key, value in defaults.items()),
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
            function("run", wait="True", image=None),
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


def command(name, *flags, positional=(), defaults=None):
    """A manifest command; `defaults` maps a flag to the string the manifest prints for it."""
    defaults = defaults or {}
    parameters = [
        {"name": flag, "positional": False, "default": defaults.get(flag)}
        for flag in flags
    ]
    parameters += [{"name": arg, "positional": True} for arg in positional]
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
        "globalFlags": [{"name": "json", "positional": False}],
        "clientDefaults": {"execWaitSeconds": 300.0, "launch": {"wait": True}},
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
            "py: Session.kill(offset) = False belongs to no [[default]] row"
        )

    def test_a_zero_none_or_string_default_needs_no_row(self):
        # cost --running-sec 0, Sandbox.run(image=None), ls --format table and run_report's label
        # are in the baseline with no row, and it passes.
        self.assertEqual(self.problems(), [])

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

    def test_a_typescript_cell_is_refused(self):
        self.table = self.table.replace(
            'py = "Sandbox.run(wait)"',
            'py = "Sandbox.run(wait)"\nts = "Sandbox.run(wait)"',
        )
        self.assertProblem(
            "default launch-wait: ts: index.d.ts states defaults only in prose"
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
        # Defaults as the stub spells them, `...` for a named constant; `None` is read but not
        # held.
        self.assertEqual(
            surface.defaults,
            {
                "Session.health(timeout)": "300.0",
                "Session.health(wait)": "...",
                "Session.health(label)": "None",
            },
        )
        self.assertFalse(
            PARITY["held"]("py", surface.defaults["Session.health(label)"])
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
            env = {
                **first_on_path(bin_dir),
                "npm_config_cache": str(cache),
                # spelled here, not asked of the helper, so a lock on any other file fails
                "FAKE_NPX_LOCK": str(cache / "microvms-agentd-npx.lock"),
            }
            with mock.patch.dict(os.environ, env):
                try:
                    project = PARITY["run_typedoc"](declarations, root)
                except PARITY["ParityError"] as error:
                    self.fail(str(error))
        self.assertEqual(project, {"children": []})

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
