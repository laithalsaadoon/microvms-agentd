# SPDX-License-Identifier: Apache-2.0
"""Exact declaration contracts for the Rustdoc shapes consumed by the inventory."""

import copy
import runpy
import unittest
from pathlib import Path
from types import SimpleNamespace

walk = SimpleNamespace(
    **runpy.run_path(
        str(Path(__file__).with_name("rustdoc_walk.py")),
        run_name="tools.rustdoc_walk",
    )
)


def item(what, inner, *, name="Item", identity=1, attrs=()):
    return {
        "id": identity,
        "name": name,
        "attrs": list(attrs),
        "inner": {what: inner},
    }


def reference(identity=42):
    return {"resolved_path": {"path": "Alias", "id": identity, "args": None}}


RESOLVED = {"resolved_path": {"path": "dependency::Options", "args": None}}


class StableTypes(unittest.TestCase):
    def test_nested_types_replace_known_ids_and_preserve_the_spelled_shape(self):
        source = {
            "tuple": [
                reference(),
                {
                    "borrowed_ref": {
                        "lifetime": "'a",
                        "is_mutable": True,
                        "type": {"primitive": "u8"},
                    }
                },
                {
                    "resolved_path": {
                        "path": "Unknown",
                        "id": 99,
                        "args": {"angle_bracketed": {"args": []}},
                    }
                },
                {"path": "literal::Path", "other": {"id": 42, "keep": 0}},
                None,
                False,
                7,
                "text",
                [],
                {},
            ],
            "id": "discarded",
        }
        before = copy.deepcopy(source)
        self.assertEqual(
            walk.stable_type(
                source, {"paths": {"42": {"path": ["dependency", "Options"]}}}
            ),
            {
                "tuple": [
                    RESOLVED,
                    {
                        "borrowed_ref": {
                            "lifetime": "'a",
                            "is_mutable": True,
                            "type": {"primitive": "u8"},
                        }
                    },
                    {
                        "resolved_path": {
                            "path": "Unknown",
                            "args": {"angle_bracketed": {"args": []}},
                        }
                    },
                    {"path": "literal::Path", "other": {"keep": 0}},
                    None,
                    False,
                    7,
                    "text",
                    [],
                    {},
                ]
            },
        )
        self.assertEqual(source, before)

    def test_missing_paths_leave_spelled_references_and_remove_only_ids(self):
        for doc in ({}, {"paths": {}}, {"paths": {"3": {"path": ["Other"]}}}):
            with self.subTest(doc=doc):
                self.assertEqual(
                    walk.stable_type(reference(), doc),
                    {"resolved_path": {"path": "Alias", "args": None}},
                )

    def test_origin_uses_canonical_paths_and_an_explicit_fallback(self):
        for identity in (42, "42"):
            self.assertEqual(
                walk._Walk.origin(
                    {"paths": {"42": {"path": ["dependency", "Options", "new"]}}},
                    {"id": identity},
                    "public::Alias::new",
                ),
                "dependency::Options::new",
            )
        for doc in ({}, {"paths": {}}, {"paths": {"42": {}}}):
            with self.subTest(doc=doc):
                self.assertEqual(
                    walk._Walk.origin(doc, {"id": 42}, "public::Alias::new"),
                    "public::Alias::new",
                )


class Declarations(unittest.TestCase):
    def setUp(self):
        self.walker = walk._Walk(Path("unused"), ())
        self.doc = {
            "paths": {"42": {"path": ["dependency", "Options"]}},
            "index": {
                "10": {
                    "name": "visible",
                    "visibility": "public",
                    "inner": {"struct_field": reference()},
                },
                "11": {
                    "name": "secret",
                    "visibility": "default",
                    "inner": {"struct_field": {"primitive": "u8"}},
                },
                "12": {
                    "name": None,
                    "visibility": "public",
                    "inner": {"struct_field": {"primitive": "bool"}},
                },
                "13": {},
            },
        }

    def declaration(self, what, inner, **kwargs):
        return self.walker.declaration(
            self.doc, item(what, inner, **kwargs), "crate::Item"
        )

    def test_function_keeps_parameter_positions_receiver_return_generics_and_header(
        self,
    ):
        generic = {
            "params": [{"name": "T", "kind": {"type": {"default": reference()}}}],
            "where_predicates": [{"bound_predicate": {"type": reference()}}],
        }
        expected_generic = copy.deepcopy(generic)
        expected_generic["params"][0]["kind"]["type"]["default"] = RESOLVED
        expected_generic["where_predicates"][0]["bound_predicate"]["type"] = RESOLVED
        header = {"is_const": True, "is_unsafe": False, "is_async": True, "abi": "Rust"}
        self.assertEqual(
            self.declaration(
                "function",
                {
                    "sig": {
                        "inputs": [
                            ["first", {"primitive": "bool"}],
                            ["self", reference()],
                            ["options", reference()],
                        ],
                        "output": reference(),
                    },
                    "generics": generic,
                    "header": header,
                },
            ),
            {
                "origin": "crate::Item",
                "parameters": {
                    "first": {"position": 0, "type": {"primitive": "bool"}},
                    "options": {"position": 2, "type": RESOLVED},
                },
                "returns": RESOLVED,
                "generics": expected_generic,
                "receiver": RESOLVED,
                "header": header,
            },
        )

    def test_function_defaults_and_receiver_absence_are_explicit(self):
        expected = {
            "origin": None,
            "parameters": {},
            "returns": None,
            "generics": {},
            "receiver": None,
            "header": {},
        }
        self.assertEqual(
            self.walker.declaration(self.doc, item("function", {})), expected
        )
        self.assertEqual(
            self.walker.declaration(self.doc, item("function", {"sig": {}})), expected
        )
        expected["parameters"] = {
            "input": {"position": 0, "type": {"primitive": "u32"}}
        }
        self.assertEqual(
            self.walker.declaration(
                self.doc,
                item(
                    "function",
                    {
                        "sig": {"inputs": [["input", {"primitive": "u32"}]]},
                    },
                ),
            ),
            expected,
        )

    def test_constants_statics_and_associated_items_keep_their_kind_and_shape(self):
        for what in ("constant", "static", "assoc_const", "assoc_type"):
            with self.subTest(what=what):
                self.assertEqual(
                    self.declaration(
                        what,
                        {"type": reference(), "default": None, "is_mutable": False},
                    ),
                    {
                        "kind": what,
                        "origin": "crate::Item",
                        "type": RESOLVED,
                        "default": None,
                        "is_mutable": False,
                    },
                )

    def test_non_declaration_items_are_omitted(self):
        for what in ("module", "macro", "variant", "use", "impl", "struct_field"):
            with self.subTest(what=what):
                self.assertIsNone(self.declaration(what, {}))

    def test_struct_construction_fields_generics_and_relevant_attributes(self):
        generics = {"params": [], "where_predicates": [{"type": reference()}]}
        for shape, construction, expected_fields in (
            (
                {"plain": {"fields": [10, None, 11, 12, 13]}},
                "plain",
                {"visible": RESOLVED, "3": {"primitive": "bool"}},
            ),
            (
                {"tuple": [None, 11, 12, 10]},
                "tuple",
                {"2": {"primitive": "bool"}, "visible": RESOLVED},
            ),
            ("unit", "unit", {}),
        ):
            with self.subTest(shape=shape):
                self.assertEqual(
                    self.declaration(
                        "struct",
                        {"kind": shape, "generics": generics},
                        attrs=("#[derive(Clone)]", "#[non_exhaustive]", "#[repr(C)]"),
                    ),
                    {
                        "kind": "struct",
                        "origin": "crate::Item",
                        "attributes": ["#[non_exhaustive]", "#[repr(C)]"],
                        "generics": {
                            "params": [],
                            "where_predicates": [{"type": RESOLVED}],
                        },
                        "construction": construction,
                        "fields": expected_fields,
                    },
                )

    def test_union_keeps_only_public_fields_and_defaults_attributes_to_empty(self):
        value = item("union", {"fields": [None, 10, 11, 12, 13]})
        del value["attrs"]
        self.assertEqual(
            self.walker.declaration(self.doc, value, "crate::Union"),
            {
                "kind": "union",
                "origin": "crate::Union",
                "attributes": [],
                "fields": {"visible": RESOLVED, "3": {"primitive": "bool"}},
            },
        )

    def test_enum_variants_keep_all_fields_including_default_visibility(self):
        self.doc["index"].update(
            {
                "20": item("variant", {"kind": "plain"}, name="Empty"),
                "21": item(
                    "variant",
                    {"kind": "plain", "discriminant": {"value": "7", "expr": "7"}},
                    name="Value",
                ),
                "22": item(
                    "variant", {"kind": {"tuple": [None, 11, 12, 10]}}, name="Tuple"
                ),
                "23": item(
                    "variant",
                    {"kind": {"struct": {"fields": [10, 11, 12, 13]}}},
                    name="Record",
                ),
            }
        )
        self.assertEqual(
            self.declaration("enum", {"variants": [20, 21, 22, 23]}),
            {
                "kind": "enum",
                "origin": "crate::Item",
                "attributes": [],
                "variants": {
                    "Empty": {"kind": "plain", "discriminant": None},
                    "Value": {
                        "kind": "plain",
                        "discriminant": {"value": "7", "expr": "7"},
                    },
                    "Tuple": {
                        "kind": "tuple",
                        "discriminant": None,
                        "fields": {
                            "secret": {"primitive": "u8"},
                            "2": {"primitive": "bool"},
                            "visible": RESOLVED,
                        },
                    },
                    "Record": {
                        "kind": "struct",
                        "discriminant": None,
                        "fields": {
                            "visible": RESOLVED,
                            "secret": {"primitive": "u8"},
                            "2": {"primitive": "bool"},
                        },
                    },
                },
            },
        )

    def test_traits_keep_bounds_and_explicit_or_default_safety_flags(self):
        self.assertEqual(
            self.declaration(
                "trait",
                {
                    "bounds": [reference()],
                    "is_unsafe": True,
                    "is_auto": True,
                    "is_dyn_compatible": True,
                },
            ),
            {
                "kind": "trait",
                "origin": "crate::Item",
                "attributes": [],
                "bounds": [RESOLVED],
                "is_unsafe": True,
                "is_auto": True,
                "is_dyn_compatible": True,
            },
        )
        self.assertEqual(
            self.declaration("trait", {}),
            {
                "kind": "trait",
                "origin": "crate::Item",
                "attributes": [],
                "bounds": [],
                "is_unsafe": False,
                "is_auto": False,
                "is_dyn_compatible": False,
            },
        )
        self.assertEqual(
            self.declaration("trait_alias", {"bounds": [reference()]}),
            {
                "kind": "trait_alias",
                "origin": "crate::Item",
                "attributes": [],
                "bounds": [RESOLVED],
            },
        )
        self.assertEqual(
            self.declaration("trait_alias", {}),
            {
                "kind": "trait_alias",
                "origin": "crate::Item",
                "attributes": [],
                "bounds": [],
            },
        )

    def test_type_alias_records_its_target(self):
        self.assertEqual(
            self.declaration("type_alias", {"type": reference()}),
            {
                "kind": "type_alias",
                "origin": "crate::Item",
                "attributes": [],
                "type": RESOLVED,
            },
        )


class Members(unittest.TestCase):
    def setUp(self):
        self.walker = walk._Walk(Path("unused"), ())
        self.doc = {
            "_name": "fixture",
            "paths": {"42": {"path": ["dependency", "Options"], "kind": "struct"}},
            "index": {},
        }

    def method(self, params=()):
        return item(
            "function",
            {
                "generics": {"params": list(params)},
                "sig": {"inputs": [["value", reference()]]},
            },
            name="call",
            identity=7,
        )

    def test_member_attaches_canonical_origin_and_complete_impl_constraints(self):
        member = self.method()
        self.doc["paths"]["7"] = {"path": ["dependency", "Options", "call"]}
        impl = {
            "generics": {"params": [], "where_predicates": [reference()]},
            "for": reference(),
            "trait": reference(),
            "is_unsafe": True,
            "is_negative": False,
            "items": [7],
        }
        self.walker.member(
            "public::Alias", member, "inherent", False, self.doc, "ignored::Owner", impl
        )
        self.assertEqual(list(self.walker.entries), ["public::Alias::call"])
        entry = self.walker.entries["public::Alias::call"]
        self.assertEqual(
            (entry.path, entry.kind, entry.owner, entry.via, entry.generic),
            ("public::Alias::call", "method", "public::Alias", "inherent", False),
        )
        self.assertEqual(
            entry.declaration,
            {
                "origin": "dependency::Options::call",
                "parameters": {"value": {"position": 0, "type": RESOLVED}},
                "returns": None,
                "generics": {"params": []},
                "receiver": None,
                "header": {},
                "impl": {
                    "generics": {"params": [], "where_predicates": [RESOLVED]},
                    "for": RESOLVED,
                    "trait": RESOLVED,
                    "is_unsafe": True,
                    "is_negative": False,
                },
            },
        )

    def test_member_generic_status_distinguishes_type_parameters_from_lifetimes(self):
        for inherited, params, expected in (
            (False, [], False),
            (True, [], True),
            (False, [{"kind": {"lifetime": {}}}], False),
            (False, [{"kind": {"type": {}}}], True),
            (False, [{"kind": {"lifetime": {}}}, {"kind": {"type": {}}}], True),
        ):
            with self.subTest(inherited=inherited, params=params):
                self.walker.entries.clear()
                self.walker.member(
                    "public::Alias",
                    self.method(params),
                    "trait",
                    inherited,
                    self.doc,
                    "canonical::Owner",
                )
                entry = self.walker.entries["public::Alias::call"]
                self.assertEqual(entry.generic, expected)
                self.assertEqual(entry.declaration["origin"], "canonical::Owner::call")
                self.assertNotIn("impl", entry.declaration)

    def test_associated_members_include_missing_impl_properties_as_null(self):
        for what in ("assoc_const", "assoc_type"):
            with self.subTest(what=what):
                self.walker.entries.clear()
                self.walker.member(
                    "public::Alias",
                    item(what, {"type": reference()}, name="VALUE"),
                    "trait",
                    True,
                    self.doc,
                    "canonical::Owner",
                    {},
                )
                entry = self.walker.entries["public::Alias::VALUE"]
                self.assertEqual(
                    (entry.kind, entry.owner, entry.via, entry.generic),
                    (what, "public::Alias", "trait", True),
                )
                self.assertEqual(
                    entry.declaration,
                    {
                        "kind": what,
                        "origin": "canonical::Owner::VALUE",
                        "type": RESOLVED,
                        "impl": {
                            "generics": None,
                            "for": None,
                            "trait": None,
                            "is_unsafe": None,
                            "is_negative": None,
                        },
                    },
                )

    def test_non_member_kinds_do_not_create_entries(self):
        self.walker.member(
            "Owner", item("struct_field", {}), "trait", False, self.doc, "Owner"
        )
        self.assertEqual(self.walker.entries, {})

    def test_extensions_attach_every_trait_item_to_every_public_home(self):
        method = self.method()
        constant = item(
            "assoc_const", {"type": {"primitive": "u32"}}, name="LIMIT", identity=8
        )
        trait = item(
            "trait",
            {"items": [7, 8], "implementations": [21, 20, 22]},
            name="Extension",
            identity=9,
        )
        concrete_impl = {
            "for": reference(),
            "generics": {"params": ["T"]},
            "trait": {"path": "Extension", "id": 9},
            "is_unsafe": False,
            "is_negative": False,
        }
        self.doc["index"].update(
            {
                "7": method,
                "8": constant,
                "20": item("impl", concrete_impl),
                "21": item(
                    "impl", {"for": {"generic": "T"}, "generics": {"params": []}}
                ),
                "22": item("impl", {"for": reference(99), "generics": {"params": []}}),
            }
        )
        self.doc["paths"]["9"] = {"path": ["canonical", "Extension"], "kind": "trait"}
        self.walker.extensions["Extension"] = self.doc, trait, "public::Extension"
        self.walker.homes[(("dependency", "Options"), "struct")] = [
            "public::First",
            "public::Second",
        ]
        self.walker.attribute_extensions()
        self.assertEqual(
            set(self.walker.entries),
            {
                "public::First::call",
                "public::First::LIMIT",
                "public::Second::call",
                "public::Second::LIMIT",
            },
        )
        for owner in ("public::First", "public::Second"):
            entry = self.walker.entries[f"{owner}::call"]
            self.assertEqual(
                (entry.owner, entry.via, entry.generic),
                (owner, "public::Extension", True),
            )
            self.assertEqual(entry.declaration["origin"], "canonical::Extension::call")
            self.assertEqual(
                entry.declaration["impl"],
                {
                    "for": RESOLVED,
                    "generics": {"params": ["T"]},
                    "trait": {"path": "canonical::Extension"},
                    "is_unsafe": False,
                    "is_negative": False,
                },
            )
            constant_entry = self.walker.entries[f"{owner}::LIMIT"]
            self.assertEqual(
                constant_entry.declaration["origin"], "canonical::Extension::LIMIT"
            )
            self.assertEqual(constant_entry.generic, True)

    def test_extensions_fall_back_to_public_trait_origin(self):
        trait = item("trait", {"items": [7], "implementations": [20]}, name="Extension")
        self.doc["index"].update(
            {
                "7": self.method(),
                "20": item("impl", {"for": reference(), "generics": {"params": []}}),
            }
        )
        self.walker.extensions["Extension"] = self.doc, trait, "public::Extension"
        self.walker.homes[(("dependency", "Options"), "struct")] = ["public::Alias"]
        self.walker.attribute_extensions()
        entry = self.walker.entries["public::Alias::call"]
        self.assertEqual(
            (entry.owner, entry.via, entry.generic),
            ("public::Alias", "public::Extension", False),
        )
        self.assertEqual(entry.declaration["origin"], "public::Extension::call")
