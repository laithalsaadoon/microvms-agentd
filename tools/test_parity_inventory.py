# SPDX-License-Identifier: Apache-2.0
"""Independent declaration fixtures for the exhaustive parity inventory."""

import copy
import runpy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

inventory = SimpleNamespace(
    **runpy.run_path(
        str(Path(__file__).with_name("parity_inventory.py")),
        run_name="tools.parity_inventory",
    )
)
rustdoc_walk = SimpleNamespace(
    **runpy.run_path(
        str(Path(__file__).with_name("rustdoc_walk.py")),
        run_name="tools.rustdoc_walk",
    )
)


def argument(name, *, kind="positional or keyword", type_="str", default=None):
    return {"name": name, "kind": kind, "annotation": type_, "default": default}


def function(name, *parameters, returns="str"):
    return {
        "kind": "function",
        "name": name,
        "parameters": list(parameters),
        "returns": returns,
    }


def ts_function(name, *parameters, returns="string"):
    return {
        "id": 1,
        "kind": 64,
        "name": name,
        "signatures": [
            {
                "id": 2,
                "name": name,
                "parameters": list(parameters),
                "type": {"type": "intrinsic", "name": returns},
            }
        ],
    }


def ts_argument(name, *, optional=False):
    return {
        "id": 3,
        "kind": 32768,
        "name": name,
        "flags": {"isOptional": optional},
        "type": {"type": "intrinsic", "name": "string"},
    }


class Collectors(unittest.TestCase):
    def test_python_class_fields_are_individually_mapped(self):
        observed = inventory.python(
            {
                "members": [
                    {
                        "kind": "class",
                        "name": "Result",
                        "members": [
                            {
                                "kind": "attribute",
                                "name": "value",
                                "annotation": "str",
                                "labels": ["property"],
                            }
                        ],
                    }
                ]
            }
        )
        self.assertEqual(
            observed["option:Result.value"],
            {"kind": "field", "type": "str", "labels": ["property"]},
        )

    def test_core_collects_every_type_field_and_callable_argument(self):
        observed = inventory.core(
            {
                "declarations": {
                    "Options": {
                        "kind": "struct",
                        "fields": {"value": {"primitive": "u32"}},
                    },
                    "call": {
                        "parameters": {"options": {"type": "Options", "position": 0}}
                    },
                }
            }
        )
        self.assertEqual(
            set(observed),
            {
                "type:Options",
                "option:Options.value",
                "callable:call",
                "option:call(options)",
            },
        )

    def test_cli_includes_global_flags_positionals_choices_and_requiredness(self):
        def parameter(name, **kwargs):
            return {
                "name": name,
                "type": "string",
                "required": False,
                "positional": False,
                "choices": None,
                "default": None,
                "help": "Choose a value",
                **kwargs,
            }

        observed = inventory.cli(
            {
                "data": {
                    "globalFlags": [parameter("json", type="boolean")],
                    "commands": [
                        {
                            "name": "run",
                            "parameters": [
                                parameter("path", positional=True, required=True),
                                parameter("mode", choices=["first", "second"]),
                            ],
                        }
                    ],
                }
            }
        )
        self.assertEqual(
            set(observed), {"option:* --json", "option:run <path>", "option:run --mode"}
        )
        self.assertTrue(observed["option:run <path>"]["required"])
        self.assertEqual(observed["option:run --mode"]["choices"], ["first", "second"])

    def test_python_includes_unenrolled_types_all_inputs_and_aliases(self):
        observed = inventory.python(
            {
                "members": [
                    {
                        "kind": "class",
                        "name": "NewType",
                        "members": [
                            function("__new__", argument("cls"), argument("value")),
                            function(
                                "method",
                                argument("self"),
                                argument("value", kind="keyword-only"),
                            ),
                        ],
                    },
                    {
                        "kind": "attribute",
                        "name": "Alias",
                        "annotation": "TypeAlias",
                        "value": "NewType",
                    },
                    {
                        "kind": "attribute",
                        "name": "FLAG",
                        "annotation": "bool",
                        "value": "True",
                    },
                ],
                "aliases": [
                    {"kind": "alias", "name": "Public", "target": "elsewhere.Public"}
                ],
            }
        )
        self.assertEqual(
            set(observed),
            {
                "type:NewType",
                "type:Alias",
                "type:Public",
                "value:FLAG",
                "callable:NewType.__new__",
                "callable:NewType.method",
                "option:NewType.__new__(value)",
                "option:NewType.method(value)",
            },
        )
        self.assertEqual(
            observed["option:NewType.method(value)"]["kind"], "keyword-only"
        )

    def test_python_parameter_order_requiredness_and_returns_change_the_contract(self):
        original = function("f", argument("first"), argument("second"))
        before = inventory.python({"members": [original]})
        for changed in (
            function("f", argument("second"), argument("first")),
            function("f", argument("first", default="None"), argument("second")),
            function("f", argument("first"), argument("second"), returns="int"),
        ):
            with self.subTest(changed=changed):
                self.assertNotEqual(before, inventory.python({"members": [changed]}))

    def test_typescript_includes_alias_enum_interface_and_option_fields(self):
        observed = inventory.typescript(
            {
                "children": [
                    {
                        "id": 4,
                        "kind": 2097152,
                        "name": "Alias",
                        "type": {"type": "intrinsic", "name": "string"},
                    },
                    {
                        "id": 5,
                        "kind": 8,
                        "name": "Mode",
                        "children": [
                            {
                                "id": 6,
                                "kind": 16,
                                "name": "First",
                                "defaultValue": "'first'",
                            }
                        ],
                    },
                    {
                        "id": 7,
                        "kind": 256,
                        "name": "Options",
                        "children": [
                            {
                                "id": 8,
                                "kind": 1024,
                                "name": "value",
                                "flags": {"isOptional": True},
                                "type": {"type": "intrinsic", "name": "string"},
                            }
                        ],
                    },
                ]
            }
        )
        self.assertEqual(
            set(observed),
            {"type:Alias", "type:Mode", "type:Options", "option:Options.value"},
        )
        self.assertFalse(observed["option:Options.value"]["required"])
        self.assertEqual(observed["type:Mode"]["members"]["First"]["value"], "'first'")

    def test_typescript_zero_arguments_overloads_order_and_returns_are_held(self):
        base = {"children": [ts_function("f", ts_argument("a"), ts_argument("b"))]}
        before = inventory.typescript(base)
        for changed in (
            ts_function("f", ts_argument("b"), ts_argument("a")),
            ts_function("f", ts_argument("a"), ts_argument("b"), returns="number"),
        ):
            self.assertNotEqual(before, inventory.typescript({"children": [changed]}))
        zero = inventory.typescript({"children": [ts_function("f")]})
        self.assertIn("callable:f", zero)
        changed = copy.deepcopy(base)
        changed["children"][0]["signatures"].append(
            ts_function("f", ts_argument("c"))["signatures"][0]
        )
        self.assertNotEqual(before, inventory.typescript(changed))
        self.assertIn("option:f#2(c)", inventory.typescript(changed))

    def test_rust_canonical_targets_and_parameter_order_are_held(self):
        first = {"paths": {"1": {"path": ["one", "Foo"]}}}
        second = {"paths": {"1": {"path": ["two", "Foo"]}}}
        type_ = {"resolved_path": {"id": 1, "path": "Foo", "args": None}}
        self.assertNotEqual(
            rustdoc_walk.stable_type(type_, first),
            rustdoc_walk.stable_type(type_, second),
        )
        data = {
            "declarations": {
                "f": {
                    "parameters": {
                        "a": {"type": "str", "position": 0},
                        "b": {"type": "str", "position": 1},
                    }
                }
            }
        }
        changed = copy.deepcopy(data)
        changed["declarations"]["f"]["parameters"]["a"]["position"] = 1
        self.assertNotEqual(inventory.core(data), inventory.core(changed))

    def test_unknown_declaration_and_parameter_forms_fail_closed(self):
        with self.assertRaises(inventory.InventoryError):
            inventory.typescript({"children": [{"id": 1, "kind": 999, "name": "new"}]})
        with self.assertRaises(inventory.InventoryError):
            inventory.python(
                {"members": [function("f", argument("x", kind="unknown"))]}
            )
        with self.assertRaises(inventory.InventoryError):
            inventory.cli({"data": {"globalFlags": [{"name": "x", "unknown": True}]}})
        with self.assertRaises(inventory.InventoryError):
            inventory.core({"paths": {"f": "function"}})

    def test_cli_positional_order_and_aliases_change_the_contract(self):
        source = {"name": "src", "type": "path", "required": True, "positional": True}
        target = {**source, "name": "dst"}

        def read(parameters):
            return inventory.cli(
                {"data": {"commands": [{"name": "cp", "parameters": parameters}]}}
            )

        self.assertNotEqual(read([source, target]), read([target, source]))
        self.assertNotEqual(read([source]), read([{**source, "aliases": ["input"]}]))

    def test_python_runtime_protocol_methods_and_binding_kind_are_held(self):
        protocol = function("__exit__", argument("self"), argument("exception"))
        observed = inventory.python(
            {"members": [{"kind": "class", "name": "Stream", "members": [protocol]}]}
        )
        self.assertIn("option:Stream.__exit__(exception)", observed)
        method = function("f", argument("value"))
        static = inventory.python(
            {"members": [{**method, "decorators": ["staticmethod"]}]}
        )
        classmethod = inventory.python(
            {"members": [{**method, "decorators": ["classmethod"]}]}
        )
        self.assertNotEqual(static, classmethod)

    def test_rust_receiver_and_async_header_change_the_contract(self):
        method = {
            "inner": {
                "function": {
                    "sig": {
                        "inputs": [
                            [
                                "self",
                                {
                                    "borrowed_ref": {
                                        "type": {"generic": "Self"},
                                        "is_mutable": False,
                                    }
                                },
                            ]
                        ],
                        "output": None,
                    },
                    "header": {"is_async": False},
                }
            }
        }
        walk = rustdoc_walk._Walk(Path("."), [])
        before = walk.declaration({}, method)
        changed = copy.deepcopy(method)
        changed["inner"]["function"]["sig"]["inputs"][0][1]["borrowed_ref"][
            "is_mutable"
        ] = True
        self.assertNotEqual(before, walk.declaration({}, changed))
        changed = copy.deepcopy(method)
        changed["inner"]["function"]["header"]["is_async"] = True
        self.assertNotEqual(before, walk.declaration({}, changed))

    def test_typescript_inheritance_and_accessor_returns_are_held(self):
        base = {
            "id": 1,
            "kind": 256,
            "name": "Child",
            "extendedTypes": [{"type": "reference", "name": "A"}],
        }
        changed = {**base, "extendedTypes": [{"type": "reference", "name": "B"}]}
        self.assertNotEqual(
            inventory.typescript({"children": [base]}),
            inventory.typescript({"children": [changed]}),
        )
        getter = {
            "id": 2,
            "kind": 262144,
            "name": "value",
            "getSignature": {"type": {"type": "intrinsic", "name": "string"}},
        }
        base["children"] = [getter]
        original = inventory.typescript({"children": [base]})
        getter["getSignature"]["type"]["name"] = "number"
        self.assertNotEqual(original, inventory.typescript({"children": [base]}))

    def test_typescript_static_readonly_and_abstract_flags_are_held(self):
        method = ts_function("f")
        method["kind"] = 2048
        base = {"id": 5, "kind": 128, "name": "Example", "children": [method]}
        original = inventory.typescript({"children": [base]})
        for target, flag in ((base, "isAbstract"), (method, "isStatic")):
            with self.subTest(flag=flag):
                target["flags"] = {flag: True}
                self.assertNotEqual(
                    original, inventory.typescript({"children": [base]})
                )
                target.pop("flags")
        field = {"id": 6, "kind": 1024, "name": "value"}
        base["children"] = [field]
        original = inventory.typescript({"children": [base]})
        field["flags"] = {"isReadonly": True}
        self.assertNotEqual(original, inventory.typescript({"children": [base]}))

    def test_rust_trait_safety_and_struct_construction_are_held(self):
        walk = rustdoc_walk._Walk(Path("."), [])
        trait = {"inner": {"trait": {"is_unsafe": False, "is_auto": False}}}
        before = walk.declaration({}, trait)
        for flag in ("is_unsafe", "is_auto"):
            changed = copy.deepcopy(trait)
            changed["inner"]["trait"][flag] = True
            self.assertNotEqual(before, walk.declaration({}, changed))
        unit = {"inner": {"struct": {"kind": "unit"}}}
        plain = {"inner": {"struct": {"kind": {"plain": {"fields": []}}}}}
        self.assertNotEqual(walk.declaration({}, unit), walk.declaration({}, plain))

    def test_rust_discriminants_and_associated_type_bounds_are_held(self):
        walk = rustdoc_walk._Walk(Path("."), [])
        variant = {
            "name": "First",
            "inner": {
                "variant": {
                    "kind": "plain",
                    "discriminant": {"expr": "1", "value": "1"},
                }
            },
        }
        doc = {"index": {"1": variant}}
        enum = {"inner": {"enum": {"variants": [1]}}}
        before = walk.declaration(doc, enum)
        variant["inner"]["variant"]["discriminant"] = {"expr": "3", "value": "3"}
        self.assertNotEqual(before, walk.declaration(doc, enum))
        associated = {
            "inner": {"assoc_type": {"bounds": [], "generics": {}, "type": None}}
        }
        before = walk.declaration({}, associated)
        self.assertIsNotNone(before)
        associated["inner"]["assoc_type"]["bounds"] = ["Clone"]
        self.assertNotEqual(before, walk.declaration({}, associated))

    def test_rust_enclosing_impl_bounds_are_held(self):
        method = {
            "id": 1,
            "name": "make",
            "inner": {"function": {"generics": {"params": []}}},
        }
        impl = {"generics": {"params": [{"name": "T", "bounds": ["Clone"]}]}}

        def collect():
            walk = rustdoc_walk._Walk(Path("."), [])
            walk.member("Generic", method, "inherent", True, {}, "Generic", impl)
            return walk.entries["Generic::make"].declaration

        before = collect()
        impl["generics"]["params"][0]["bounds"] = ["Copy"]
        self.assertNotEqual(before, collect())


class ManifestVerdict(unittest.TestCase):
    def setUp(self):
        self.observed = {
            surface: {"option:f(value)": {"type": "string", "required": True}}
            for surface in ("core", "cli", "py", "ts")
        }
        self.manifest = {
            "schema": 1,
            "shapes": copy.deepcopy(self.observed),
            "exemptions": {},
            "contracts": [
                {
                    "id": "value",
                    "kind": "option",
                    "capability": "f",
                    **{surface: ["option:f(value)"] for surface in self.observed},
                }
            ],
        }

    def verdict(self):
        return inventory.problems(self.manifest, self.observed, {"f"})

    def test_baseline_resolves_all_surfaces(self):
        self.assertEqual(self.verdict(), [])

    def test_each_surface_rejects_new_missing_and_changed_declarations(self):
        for surface in self.observed:
            with self.subTest(surface=surface):
                original = copy.deepcopy(self.observed[surface])
                self.observed[surface]["type:Unenrolled"] = {"kind": "class"}
                self.assertTrue(
                    any(
                        "type:Unenrolled has no recorded shape" in p
                        for p in self.verdict()
                    )
                )
                self.assertTrue(
                    any(
                        "type:Unenrolled has no contract row" in p
                        for p in self.verdict()
                    )
                )
                self.observed[surface] = copy.deepcopy(original)
                self.observed[surface]["option:f(value)"]["required"] = False
                self.assertTrue(any("changed shape" in p for p in self.verdict()))
                self.observed[surface] = {}
                self.assertTrue(
                    any(
                        "collector returned no declarations" in p
                        for p in self.verdict()
                    )
                )
                self.assertTrue(any("recorded but absent" in p for p in self.verdict()))
                self.observed[surface] = original

    def test_removed_contract_duplicate_mapping_and_unknown_capability_fail(self):
        self.manifest["contracts"] = []
        self.assertTrue(any("no contract row" in p for p in self.verdict()))
        self.setUp()
        self.manifest["contracts"].append(copy.deepcopy(self.manifest["contracts"][0]))
        self.assertTrue(any("multiple rows" in p for p in self.verdict()))
        self.setUp()
        self.manifest["contracts"][0]["capability"] = "missing"
        self.assertTrue(any("unknown capability" in p for p in self.verdict()))

    def test_exemptions_require_a_live_specific_decision(self):
        row = self.manifest["contracts"][0]
        self.observed["ts"] = {"type:Other": {"kind": "class"}}
        self.manifest["shapes"]["ts"] = copy.deepcopy(self.observed["ts"])
        row["ts"] = {"exempt": "input-bag"}
        self.manifest["exemptions"]["input-bag"] = ""
        self.assertTrue(any("invalid exemption" in p for p in self.verdict()))
        self.manifest["exemptions"]["unused"] = "An unused decision."
        self.assertTrue(any("unused exemption unused" in p for p in self.verdict()))


class RealPythonParser(unittest.TestCase):
    def test_writable_fields_and_readonly_properties_have_distinct_shapes(self):
        parity = runpy.run_path(str(Path(__file__).with_name("check-parity.py")))
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fixture.pyi"
            path.write_text("class Result:\n    value: str\n")
            writable = inventory.python(parity["run_griffe"](path))
            path.write_text(
                "class Result:\n    @property\n    def value(self) -> str: ...\n"
            )
            readonly = inventory.python(parity["run_griffe"](path))
            path.write_text(
                path.read_text()
                + "    @value.setter\n    def value(self, value: str) -> None: ...\n"
            )
            setter = inventory.python(parity["run_griffe"](path))
        self.assertNotEqual(setter, readonly)
        self.assertIn("writable", setter["type:Result"]["members"]["value"]["labels"])
        self.assertNotEqual(writable, readonly)
        self.assertIn("property", readonly["type:Result"]["members"]["value"]["labels"])

    def test_griffe_preserves_overload_changes_and_explicit_reexports(self):
        script = Path(__file__).with_name("check-parity.py")
        parity = runpy.run_path(str(script))
        source = """from typing import overload
from elsewhere import Public as Public
@overload
def f(value: str) -> str: ...
@overload
def f(value: int) -> int: ...
def f(value): ...
"""
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fixture.pyi"
            path.write_text(source)
            first = inventory.python(parity["run_griffe"](path))
            path.write_text(source.replace("value: int", "value: bytes"))
            second = inventory.python(parity["run_griffe"](path))
            path.write_text(source.replace("def f(value): ...\n", ""))
            only_overloads = inventory.python(parity["run_griffe"](path))
        self.assertNotEqual(first, second)
        self.assertEqual(
            first["type:Public"], {"kind": "alias", "type": "elsewhere.Public"}
        )
        self.assertIn("callable:f", only_overloads)
        self.assertIn("option:f#2(value)", only_overloads)
        self.assertEqual(len(only_overloads["callable:f"]["overloads"]), 1)


class RealTypeScriptParser(unittest.TestCase):
    def test_exported_types_and_ambient_globals_both_enter_the_inventory(self):
        parity = runpy.run_path(str(Path(__file__).with_name("check-parity.py")))
        source = """export interface PublicOptions { value: string; }
declare global { interface GlobalOptions { enabled: boolean; } }
"""
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            path = directory / "fixture.d.ts"
            path.write_text(source)
            first = inventory.typescript(parity["run_typedoc"](path, directory))
            path.write_text(source.replace("enabled: boolean", "enabled: number"))
            second = inventory.typescript(parity["run_typedoc"](path, directory))
        self.assertIn("type:PublicOptions", first)
        self.assertIn("type:global.GlobalOptions", first)
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
