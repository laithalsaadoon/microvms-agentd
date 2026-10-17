# SPDX-License-Identifier: Apache-2.0
"""Exact public-contract fixtures for the declaration collectors."""

import copy
import runpy
import unittest
from pathlib import Path
from types import SimpleNamespace

inventory = SimpleNamespace(
    **runpy.run_path(
        str(Path(__file__).with_name("parity_inventory.py")),
        run_name="tools.parity_inventory",
    )
)


class CoreContract(unittest.TestCase):
    def test_complete_declarations_and_argument_origins(self):
        declarations = {
            "Options": {
                "kind": "struct",
                "origin": "api::Options",
                "fields": {"count": {"primitive": "u32"}, "label": "String"},
            },
            "Empty": {"kind": "enum", "origin": "api::Empty"},
            "call": {
                "origin": "api::call",
                "parameters": {
                    "options": {"type": "Options", "position": 0},
                    "enabled": {"type": "bool", "position": 1},
                },
            },
            "zero": {"parameters": {}},
            "LIMIT": {"kind": "constant", "value": "12"},
            "CACHE": {"kind": "static", "type": "Cache"},
            "Options::DEFAULT": {"kind": "assoc_const", "type": "Options"},
        }
        self.assertEqual(
            inventory.core({"declarations": declarations}),
            {
                "type:Options": declarations["Options"],
                "option:Options.count": {
                    "type": {"primitive": "u32"},
                    "kind": "field",
                    "origin": "api::Options.count",
                },
                "option:Options.label": {
                    "type": "String",
                    "kind": "field",
                    "origin": "api::Options.label",
                },
                "type:Empty": declarations["Empty"],
                "callable:call": declarations["call"],
                "option:call(options)": {
                    "type": "Options",
                    "position": 0,
                    "kind": "argument",
                    "origin": "api::call(options)",
                },
                "option:call(enabled)": {
                    "type": "bool",
                    "position": 1,
                    "kind": "argument",
                    "origin": "api::call(enabled)",
                },
                "callable:zero": {"parameters": {}},
                "value:LIMIT": declarations["LIMIT"],
                "value:CACHE": declarations["CACHE"],
                "value:Options::DEFAULT": declarations["Options::DEFAULT"],
            },
        )

    def test_missing_declarations_refuse_a_complete_inventory(self):
        for snapshot in ({}, {"declarations": {}}, {"declarations": []}):
            with self.subTest(snapshot=snapshot):
                with self.assertRaises(inventory.InventoryError) as error:
                    inventory.core(snapshot)
                self.assertEqual(
                    str(error.exception),
                    "core snapshot has no declarations; regenerate core-api",
                )


class CliContract(unittest.TestCase):
    def test_complete_parameter_contracts_and_command_local_positions(self):
        full = {
            "name": "files",
            "type": "PathBuf",
            "required": True,
            "positional": True,
            "choices": ["source", "destination"],
            "default": "source",
            "help": "Paths to copy",
            "index": 3,
            "short": "f",
            "aliases": ["paths"],
            "shortAliases": ["p"],
            "action": "Append",
            "arity": {"min": 1, "max": 3},
            "valueDelimiter": ",",
            "valueTerminator": ";",
        }
        plain = {
            "name": "verbose",
            "type": "bool",
            "required": False,
            "positional": False,
        }
        second = {**plain, "name": "target", "positional": True}
        self.assertEqual(
            inventory.cli(
                {
                    "data": {
                        "commands": [
                            {"name": "copy", "parameters": [plain, full, second]},
                            {"name": "show", "parameters": [second]},
                            {"name": "empty"},
                        ],
                        "globalFlags": [plain],
                    }
                }
            ),
            {
                "option:copy --verbose": {
                    "kind": "flag",
                    "type": "bool",
                    "required": False,
                    "choices": None,
                    "position": None,
                    "index": None,
                    "short": None,
                    "aliases": None,
                    "shortAliases": None,
                    "action": None,
                    "arity": None,
                    "valueDelimiter": None,
                    "valueTerminator": None,
                },
                "option:copy <files>": {
                    "kind": "positional",
                    "type": "PathBuf",
                    "required": True,
                    "choices": ["source", "destination"],
                    "position": 1,
                    "index": 3,
                    "short": "f",
                    "aliases": ["paths"],
                    "shortAliases": ["p"],
                    "action": "Append",
                    "arity": {"min": 1, "max": 3},
                    "valueDelimiter": ",",
                    "valueTerminator": ";",
                },
                "option:copy <target>": {
                    "kind": "positional",
                    "type": "bool",
                    "required": False,
                    "choices": None,
                    "position": 2,
                    "index": None,
                    "short": None,
                    "aliases": None,
                    "shortAliases": None,
                    "action": None,
                    "arity": None,
                    "valueDelimiter": None,
                    "valueTerminator": None,
                },
                "option:show <target>": {
                    "kind": "positional",
                    "type": "bool",
                    "required": False,
                    "choices": None,
                    "position": 1,
                    "index": None,
                    "short": None,
                    "aliases": None,
                    "shortAliases": None,
                    "action": None,
                    "arity": None,
                    "valueDelimiter": None,
                    "valueTerminator": None,
                },
                "option:* --verbose": {
                    "kind": "flag",
                    "type": "bool",
                    "required": False,
                    "choices": None,
                    "position": None,
                    "index": None,
                    "short": None,
                    "aliases": None,
                    "shortAliases": None,
                    "action": None,
                    "arity": None,
                    "valueDelimiter": None,
                    "valueTerminator": None,
                },
            },
        )
        for manifest in ({}, {"data": {}}, {"data": {"commands": []}}):
            self.assertEqual(inventory.cli(manifest), {})

    def test_unrecognized_and_incomplete_parameters_fail_closed(self):
        plain = {"name": "x", "type": "str", "required": False, "positional": False}
        invalid = [{**plain, "newProperty": True}]
        invalid.extend(
            {key: value for key, value in plain.items() if key != missing}
            for missing in ("name", "type", "required")
        )
        for item in invalid:
            with self.subTest(item=item):
                with self.assertRaises(inventory.InventoryError) as error:
                    inventory.cli({"data": {"globalFlags": [item]}})
                self.assertEqual(
                    str(error.exception), f"unknown CLI parameter shape for *: {item}"
                )
        with self.assertRaises(inventory.InventoryError):
            inventory.cli(
                {
                    "data": {
                        "globalFlags": [{"name": "x", "type": "str", "required": False}]
                    }
                }
            )


class PythonContract(unittest.TestCase):
    def test_functions_hold_overloads_order_and_every_parameter_kind(self):
        self.assertEqual(
            inventory.python(
                {
                    "members": [
                        {
                            "name": "launch",
                            "kind": "function",
                            "returns": "Result",
                            "decorators": ["public"],
                            "labels": ["async"],
                            "parameters": [
                                {"name": "self"},
                                {
                                    "name": "image",
                                    "kind": "positional-only",
                                    "annotation": "str",
                                },
                                {
                                    "name": "count",
                                    "kind": "positional or keyword",
                                    "annotation": "int",
                                    "default": "1",
                                },
                                {
                                    "name": "args",
                                    "kind": "variadic positional",
                                    "annotation": "str",
                                },
                                {
                                    "name": "wait",
                                    "kind": "keyword-only",
                                    "annotation": "bool",
                                    "default": "False",
                                },
                                {
                                    "name": "kwargs",
                                    "kind": "variadic keyword",
                                    "annotation": "object",
                                },
                            ],
                            "overloads": [
                                {
                                    "returns": "None",
                                    "parameters": [
                                        {"name": "cls"},
                                        {"name": "image", "kind": "keyword-only"},
                                    ],
                                }
                            ],
                        }
                    ]
                }
            ),
            {
                "callable:launch": {
                    "returns": "Result",
                    "decorators": ["public"],
                    "labels": ["async"],
                    "parameters": [
                        {"name": "self", "kind": None, "type": None, "required": True},
                        {
                            "name": "image",
                            "kind": "positional-only",
                            "type": "str",
                            "required": True,
                        },
                        {
                            "name": "count",
                            "kind": "positional or keyword",
                            "type": "int",
                            "required": False,
                        },
                        {
                            "name": "args",
                            "kind": "variadic positional",
                            "type": "str",
                            "required": True,
                        },
                        {
                            "name": "wait",
                            "kind": "keyword-only",
                            "type": "bool",
                            "required": False,
                        },
                        {
                            "name": "kwargs",
                            "kind": "variadic keyword",
                            "type": "object",
                            "required": True,
                        },
                    ],
                    "overloads": [
                        {
                            "returns": "None",
                            "parameters": [
                                {
                                    "name": "cls",
                                    "kind": None,
                                    "type": None,
                                    "required": True,
                                },
                                {
                                    "name": "image",
                                    "kind": "keyword-only",
                                    "type": None,
                                    "required": True,
                                },
                            ],
                        }
                    ],
                },
                "option:launch#1(image)": {
                    "kind": "positional-only",
                    "type": "str",
                    "position": 1,
                    "required": True,
                },
                "option:launch#1(count)": {
                    "kind": "positional or keyword",
                    "type": "int",
                    "position": 2,
                    "required": False,
                },
                "option:launch#1(args)": {
                    "kind": "variadic positional",
                    "type": "str",
                    "position": 3,
                    "required": False,
                },
                "option:launch#1(wait)": {
                    "kind": "keyword-only",
                    "type": "bool",
                    "position": 4,
                    "required": False,
                },
                "option:launch#1(kwargs)": {
                    "kind": "variadic keyword",
                    "type": "object",
                    "position": 5,
                    "required": False,
                },
                "option:launch#2(image)": {
                    "kind": "keyword-only",
                    "type": None,
                    "position": 1,
                    "required": True,
                },
            },
        )
        self.assertEqual(
            inventory.python({"members": [{"name": "empty", "kind": "function"}]}),
            {
                "callable:empty": {
                    "returns": None,
                    "decorators": [],
                    "labels": [],
                    "overloads": [],
                    "parameters": [],
                },
            },
        )

    def test_single_signature_inputs_have_no_overload_suffix(self):
        self.assertEqual(
            inventory.python(
                {
                    "members": [
                        {
                            "kind": "function",
                            "name": "call",
                            "parameters": [
                                {
                                    "name": "value",
                                    "kind": "positional or keyword",
                                    "annotation": "str",
                                }
                            ],
                        }
                    ]
                }
            ),
            {
                "callable:call": {
                    "returns": None,
                    "decorators": [],
                    "labels": [],
                    "overloads": [],
                    "parameters": [
                        {
                            "name": "value",
                            "kind": "positional or keyword",
                            "type": "str",
                            "required": True,
                        }
                    ],
                },
                "option:call(value)": {
                    "kind": "positional or keyword",
                    "type": "str",
                    "position": 0,
                    "required": True,
                },
            },
        )

    def test_class_shapes_fields_aliases_values_and_nested_names(self):
        self.assertEqual(
            inventory.python(
                {
                    "members": [
                        {
                            "name": "Result",
                            "kind": "class",
                            "bases": ["Base"],
                            "decorators": ["final"],
                            "members": [
                                {
                                    "name": "value",
                                    "kind": "attribute",
                                    "annotation": "str",
                                    "labels": ["property"],
                                },
                                {"name": "unknown", "kind": "attribute"},
                                {
                                    "name": "Alias",
                                    "kind": "attribute",
                                    "annotation": "typing.TypeAlias",
                                    "value": "str",
                                },
                                {"name": "Inner", "kind": "class"},
                                {"name": "get", "kind": "function"},
                            ],
                        },
                        {
                            "name": "MODE",
                            "kind": "attribute",
                            "annotation": "str",
                            "value": "'fast'",
                        },
                        {"name": "untyped", "kind": "attribute"},
                        {
                            "name": "Alias",
                            "kind": "attribute",
                            "annotation": "TypeAlias",
                            "value": "Result",
                        },
                        {
                            "name": "__doc__",
                            "kind": "attribute",
                            "annotation": "str",
                            "value": "doc",
                        },
                    ],
                    "aliases": [{"name": "Public", "target": "library.Public"}],
                }
            ),
            {
                "type:Result": {
                    "kind": "class",
                    "bases": ["Base"],
                    "decorators": ["final"],
                    "members": {
                        "value": {
                            "kind": "attribute",
                            "type": "str",
                            "labels": ["property"],
                        },
                        "unknown": {"kind": "attribute", "type": None, "labels": []},
                        "Alias": {
                            "kind": "attribute",
                            "type": "typing.TypeAlias",
                            "labels": [],
                        },
                        "Inner": {"kind": "class", "type": None, "labels": []},
                        "get": {"kind": "function", "type": None, "labels": []},
                    },
                },
                "option:Result.value": {
                    "kind": "field",
                    "type": "str",
                    "labels": ["property"],
                },
                "option:Result.unknown": {"kind": "field", "type": "", "labels": []},
                "type:Result.Alias": {"kind": "alias", "type": "str"},
                "type:Result.Inner": {
                    "kind": "class",
                    "bases": [],
                    "decorators": [],
                    "members": {},
                },
                "callable:Result.get": {
                    "returns": None,
                    "decorators": [],
                    "labels": [],
                    "overloads": [],
                    "parameters": [],
                },
                "value:MODE": {"type": "str", "value": "'fast'"},
                "value:untyped": {"type": "", "value": None},
                "type:Alias": {"kind": "alias", "type": "Result"},
                "type:Public": {"kind": "alias", "type": "library.Public"},
            },
        )
        self.assertEqual(inventory.python({}), {})

    def test_unknown_kinds_refuse_an_inventory(self):
        with self.assertRaises(inventory.InventoryError) as error:
            inventory.python({"members": [{"name": "bad", "kind": "module"}]})
        self.assertEqual(
            str(error.exception), "unknown Python declaration kind 'module': bad"
        )
        with self.assertRaises(inventory.InventoryError) as error:
            inventory.python(
                {
                    "members": [
                        {
                            "name": "call",
                            "kind": "function",
                            "parameters": [{"name": "bad", "kind": "new kind"}],
                        }
                    ]
                }
            )
        self.assertEqual(
            str(error.exception), "unknown Python parameter kind 'new kind': call"
        )


class TypeScriptContract(unittest.TestCase):
    def test_reference_identity_and_declaration_shapes(self):
        project = {
            "children": [
                {
                    "id": 1,
                    "kind": 2,
                    "name": "sdk",
                    "children": [
                        {
                            "id": 2,
                            "kind": 4,
                            "name": "api",
                            "children": [
                                {
                                    "id": 3,
                                    "kind": 128,
                                    "name": "ResultX",
                                    "type": {
                                        "type": "reference",
                                        "name": "Other",
                                        "target": 7,
                                    },
                                    "typeParameters": [
                                        {
                                            "id": 20,
                                            "name": "T",
                                            "type": {
                                                "type": "intrinsic",
                                                "name": "string",
                                            },
                                        }
                                    ],
                                    "extendedTypes": [
                                        {
                                            "type": "reference",
                                            "name": "Base",
                                            "target": {
                                                "packageName": "base",
                                                "qualifiedName": "Base",
                                            },
                                        }
                                    ],
                                    "implementedTypes": [
                                        {"type": "reference", "name": "Interface"}
                                    ],
                                    "flags": {"isAbstract": True},
                                    "children": [
                                        {
                                            "id": 4,
                                            "kind": 1024,
                                            "name": "value",
                                            "type": {
                                                "type": "intrinsic",
                                                "name": "string",
                                            },
                                            "flags": {
                                                "isOptional": True,
                                                "isReadonly": True,
                                            },
                                            "defaultValue": "'unset'",
                                        },
                                        {
                                            "id": 5,
                                            "kind": 512,
                                            "name": "constructor",
                                            "signatures": [
                                                {
                                                    "id": 30,
                                                    "name": "new ResultX",
                                                    "parameters": [],
                                                }
                                            ],
                                        },
                                        {
                                            "id": 6,
                                            "kind": 2048,
                                            "name": "read",
                                            "signatures": [],
                                        },
                                    ],
                                },
                                {
                                    "id": 7,
                                    "kind": 256,
                                    "name": "Other",
                                },
                                {
                                    "id": 8,
                                    "kind": 2097152,
                                    "name": "Alias",
                                    "type": {
                                        "type": "reference",
                                        "name": "ResultX",
                                        "target": 3,
                                    },
                                },
                                {
                                    "id": 9,
                                    "kind": 8,
                                    "name": "Mode",
                                    "children": [
                                        {
                                            "id": 10,
                                            "kind": 16,
                                            "name": "Fast",
                                            "defaultValue": "1",
                                        }
                                    ],
                                },
                            ],
                        }
                    ],
                }
            ],
            "globalDeclarations": {"External": ["interface External {}"]},
        }
        self.assertEqual(
            inventory.typescript(project),
            {
                "type:sdk.api.ResultX": {
                    "kind": "class",
                    "type": {
                        "type": "reference",
                        "name": "Other",
                        "target": "sdk.api.Other",
                    },
                    "parameters": [
                        {"name": "T", "type": {"type": "intrinsic", "name": "string"}}
                    ],
                    "extends": [
                        {
                            "type": "reference",
                            "name": "Base",
                            "target": {"packageName": "base", "qualifiedName": "Base"},
                        }
                    ],
                    "implements": [{"type": "reference", "name": "Interface"}],
                    "flags": {"isAbstract": True},
                    "members": {
                        "value": {
                            "kind": "property",
                            "type": {"type": "intrinsic", "name": "string"},
                            "optional": True,
                            "value": "'unset'",
                            "flags": {"isOptional": True, "isReadonly": True},
                        },
                        "constructor": {
                            "kind": "constructor",
                            "type": None,
                            "optional": False,
                            "value": None,
                            "flags": {},
                        },
                        "read": {
                            "kind": "method",
                            "type": None,
                            "optional": False,
                            "value": None,
                            "flags": {},
                        },
                    },
                },
                "option:sdk.api.ResultX.value": {
                    "kind": "field",
                    "type": {"type": "intrinsic", "name": "string"},
                    "required": False,
                    "flags": {"isOptional": True, "isReadonly": True},
                },
                "callable:sdk.api.ResultX": {
                    "signatures": [{"name": "new ResultX", "parameters": []}],
                    "flags": {},
                },
                "callable:sdk.api.ResultX.read": {"signatures": [], "flags": {}},
                "type:sdk.api.Other": {
                    "kind": "interface",
                    "type": None,
                    "parameters": [],
                    "extends": [],
                    "implements": [],
                    "flags": {},
                    "members": {},
                },
                "type:sdk.api.Alias": {
                    "kind": "alias",
                    "type": {
                        "type": "reference",
                        "name": "ResultX",
                        "target": "sdk.api.ResultX",
                    },
                    "parameters": [],
                    "extends": [],
                    "implements": [],
                    "flags": {},
                    "members": {},
                },
                "type:sdk.api.Mode": {
                    "kind": "enum",
                    "type": None,
                    "parameters": [],
                    "extends": [],
                    "implements": [],
                    "flags": {},
                    "members": {
                        "Fast": {
                            "kind": "enum member",
                            "type": None,
                            "optional": False,
                            "value": "1",
                            "flags": {},
                        },
                    },
                },
                "type:External": {
                    "kind": "ambient",
                    "declarations": ["interface External {}"],
                },
            },
        )
        noisy = copy.deepcopy(project)
        noisy["children"][0]["children"][0]["children"][0]["type"].update(
            {
                "comment": {"summary": "arbitrary"},
                "sources": [{"fileName": "other.ts"}],
                "groups": [1],
                "categories": [2],
                "id": 999,
            }
        )
        self.assertEqual(inventory.typescript(project), inventory.typescript(noisy))

    def test_signatures_overloads_accessors_and_callable_values(self):
        first = {
            "id": 50,
            "name": "call",
            "parameters": [
                {
                    "id": 51,
                    "name": "value",
                    "type": {"type": "intrinsic", "name": "string"},
                },
                {
                    "id": 52,
                    "name": "rest",
                    "type": {
                        "type": "array",
                        "elementType": {"type": "intrinsic", "name": "number"},
                    },
                    "flags": {"isRest": True, "isOptional": True},
                },
            ],
            "type": {"type": "intrinsic", "name": "void"},
        }
        second = {
            "id": 53,
            "name": "call",
            "parameters": [{"name": "options", "flags": {"isOptional": True}}],
        }
        clean_first = {
            "name": "call",
            "parameters": [
                {"name": "value", "type": {"type": "intrinsic", "name": "string"}},
                {
                    "name": "rest",
                    "type": {
                        "type": "array",
                        "elementType": {"type": "intrinsic", "name": "number"},
                    },
                    "flags": {"isRest": True, "isOptional": True},
                },
            ],
            "type": {"type": "intrinsic", "name": "void"},
        }
        clean_second = {
            "name": "call",
            "parameters": [{"name": "options", "flags": {"isOptional": True}}],
        }
        self.assertEqual(
            inventory.typescript(
                {
                    "children": [
                        {
                            "id": 1,
                            "kind": 64,
                            "name": "call",
                            "signatures": [first, second],
                            "flags": {"isExported": True},
                        },
                        {
                            "id": 2,
                            "kind": 262144,
                            "name": "current",
                            "getSignature": {"id": 54, "name": "get current"},
                            "setSignature": {
                                "id": 55,
                                "name": "set current",
                                "parameters": [{"name": "value"}],
                            },
                        },
                        {
                            "id": 3,
                            "kind": 32,
                            "name": "factory",
                            "type": {"type": "intrinsic", "name": "Function"},
                            "signatures": [first],
                        },
                        {
                            "id": 4,
                            "kind": 32,
                            "name": "VERSION",
                            "type": {"type": "literal", "value": "v1"},
                        },
                        {"id": 5, "kind": 256, "name": "Callable", "signatures": []},
                        {"id": 6, "kind": 1024, "name": "required"},
                    ]
                }
            ),
            {
                "callable:call": {
                    "signatures": [clean_first, clean_second],
                    "flags": {"isExported": True},
                },
                "option:call#1(value)": {
                    "kind": "argument",
                    "type": {"type": "intrinsic", "name": "string"},
                    "required": True,
                    "position": 0,
                },
                "option:call#1(rest)": {
                    "kind": "rest",
                    "type": {
                        "type": "array",
                        "elementType": {"type": "intrinsic", "name": "number"},
                    },
                    "required": False,
                    "position": 1,
                },
                "option:call#2(options)": {
                    "kind": "argument",
                    "type": None,
                    "required": False,
                    "position": 0,
                },
                "callable:current": {
                    "signatures": [
                        {"name": "get current"},
                        {"name": "set current", "parameters": [{"name": "value"}]},
                    ],
                    "flags": {},
                },
                "option:current#2(value)": {
                    "kind": "argument",
                    "type": None,
                    "required": True,
                    "position": 0,
                },
                "value:factory": {"type": {"type": "intrinsic", "name": "Function"}},
                "callable:factory": {"signatures": [clean_first], "flags": {}},
                "option:factory(value)": {
                    "kind": "argument",
                    "type": {"type": "intrinsic", "name": "string"},
                    "required": True,
                    "position": 0,
                },
                "option:factory(rest)": {
                    "kind": "rest",
                    "type": {
                        "type": "array",
                        "elementType": {"type": "intrinsic", "name": "number"},
                    },
                    "required": False,
                    "position": 1,
                },
                "value:VERSION": {"type": {"type": "literal", "value": "v1"}},
                "type:Callable": {
                    "kind": "interface",
                    "type": None,
                    "parameters": [],
                    "extends": [],
                    "implements": [],
                    "flags": {},
                    "members": {},
                },
                "callable:Callable": {"signatures": [], "flags": {}},
                "option:required": {
                    "kind": "field",
                    "type": None,
                    "required": True,
                    "flags": {},
                },
            },
        )
        self.assertEqual(inventory.typescript({}), {})

    def test_empty_modules_have_no_public_declarations(self):
        self.assertEqual(
            inventory.typescript(
                {
                    "children": [
                        {"id": 1, "kind": 2, "name": "module"},
                        {"id": 2, "kind": 4, "name": "namespace"},
                    ]
                }
            ),
            {},
        )

    def test_unknown_declaration_and_unresolved_reference_fail_closed(self):
        with self.assertRaises(inventory.InventoryError) as error:
            inventory.typescript(
                {"children": [{"id": 1, "name": "new", "kind": 123456}]}
            )
        self.assertEqual(
            str(error.exception), "unknown TypeScript declaration kind 123456: new"
        )
        with self.assertRaises(inventory.InventoryError) as error:
            inventory.typescript(
                {
                    "children": [
                        {
                            "id": 1,
                            "name": "Bad",
                            "kind": 2097152,
                            "type": {
                                "type": "reference",
                                "name": "missing",
                                "target": 55,
                            },
                        }
                    ]
                }
            )
        self.assertEqual(str(error.exception), "unresolved TypeScript reference 55")
