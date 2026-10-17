# SPDX-License-Identifier: Apache-2.0
"""Reviewed inventory decisions fail closed, with actionable diagnostics."""

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
SURFACES = ("core", "cli", "py", "ts")


class Put(unittest.TestCase):
    def test_new_and_identical_declarations_preserve_the_complete_shape(self):
        shape = {"kind": "field", "type": "string", "required": False}
        observed = {"type:Result": {"kind": "class"}}
        inventory.put(observed, "option", "Result.value", shape)
        inventory.put(observed, "option", "Result.value", copy.deepcopy(shape))
        self.assertEqual(
            observed,
            {"type:Result": {"kind": "class"}, "option:Result.value": shape},
        )

    def test_conflicting_declaration_identifies_the_key_and_preserves_the_first(self):
        observed = {"option:Result.value": {"type": "string"}}
        with self.assertRaisesRegex(
            inventory.InventoryError,
            "^conflicting declarations for option:Result.value$",
        ):
            inventory.put(observed, "option", "Result.value", {"type": "number"})
        self.assertEqual(observed, {"option:Result.value": {"type": "string"}})


class Verdict(unittest.TestCase):
    def setUp(self):
        self.observed = {
            surface: {"option:launch(value)": {"type": "string", "required": True}}
            for surface in SURFACES
        }
        self.row = {
            "id": "input",
            "kind": "option",
            "capability": "launch",
            **{surface: ["option:launch(value)"] for surface in SURFACES},
        }
        self.manifest = {
            "schema": 1,
            "shapes": copy.deepcopy(self.observed),
            "contracts": [self.row],
            "exemptions": {},
        }

    def verdict(self):
        return inventory.problems(self.manifest, self.observed, {"launch"})

    def test_all_declaration_kinds_and_optional_capability_are_accepted(self):
        for kind in ("type", "option", "callable", "value"):
            with self.subTest(kind=kind):
                self.setUp()
                self.row["kind"] = kind
                del self.row["capability"]
                for surface in SURFACES:
                    name = f"{kind}:Public"
                    self.row[surface] = [name]
                    self.observed[surface] = {name: {"kind": kind}}
                self.manifest["shapes"] = copy.deepcopy(self.observed)
                self.assertEqual(self.verdict(), [])

    def test_missing_or_unsupported_schema_stops_before_reading_other_fields(self):
        for manifest in ({}, {"schema": 2}, {"schema": 0, "contracts": None}):
            with self.subTest(manifest=manifest):
                self.assertEqual(
                    inventory.problems(manifest, {}, set()),
                    ["inventory: unsupported or missing schema"],
                )

    def test_unknown_or_missing_top_level_fields_are_rejected(self):
        self.manifest["future"] = True
        self.assertEqual(
            self.verdict(), ["inventory: unknown or missing top-level keys"]
        )
        del self.manifest["future"]
        del self.manifest["exemptions"]
        self.assertEqual(
            self.verdict(), ["inventory: unknown or missing top-level keys"]
        )

    def test_missing_shapes_reports_every_unrecorded_declaration(self):
        del self.manifest["shapes"]
        self.assertEqual(
            self.verdict(),
            ["inventory: unknown or missing top-level keys"]
            + [
                problem
                for surface in SURFACES
                for problem in (
                    f"inventory {surface}: no expected declarations",
                    f"inventory {surface}: option:launch(value) has no recorded shape",
                )
            ],
        )

    def test_missing_or_empty_contracts_reports_every_unclaimed_declaration(self):
        for missing in (True, False):
            with self.subTest(missing=missing):
                self.setUp()
                if missing:
                    del self.manifest["contracts"]
                else:
                    self.manifest["contracts"] = []
                self.assertEqual(
                    self.verdict(),
                    (
                        ["inventory: unknown or missing top-level keys"]
                        if missing
                        else []
                    )
                    + ["inventory: no contract rows"]
                    + [
                        f"inventory {surface}: option:launch(value) has no contract row"
                        for surface in SURFACES
                    ],
                )

    def test_ids_must_be_nonempty_strings_and_unique_across_distinct_contracts(self):
        for identifier in (None, "", 17):
            with self.subTest(identifier=identifier):
                self.row["id"] = identifier
                self.assertEqual(
                    self.verdict(), [f"inventory {identifier}: missing or repeated id"]
                )
        self.row["id"] = "input"
        second = {"id": "input", "kind": "type"}
        for surface in SURFACES:
            second[surface] = ["type:Other"]
            self.observed[surface]["type:Other"] = {"kind": "class"}
        self.manifest["shapes"] = copy.deepcopy(self.observed)
        self.manifest["contracts"].append(second)
        self.assertEqual(self.verdict(), ["inventory input: missing or repeated id"])

    def test_unknown_kind_row_key_and_capability_have_specific_diagnostics(self):
        self.row.update(kind="unknown", capability="missing", undocumented=True)
        self.assertEqual(
            self.verdict(),
            [
                "inventory input: unknown kind",
                "inventory input: unknown row keys",
                "inventory input: unknown capability missing",
            ]
            + [
                f"inventory input: {surface}: option:launch(value) has the wrong kind"
                for surface in SURFACES
            ],
        )

    def test_duplicate_declarations_are_rejected_even_with_distinct_contract_ids(self):
        second = copy.deepcopy(self.row)
        second["id"] = "duplicate"
        self.manifest["contracts"].append(second)
        self.assertEqual(
            self.verdict(),
            [
                f"inventory duplicate: {surface}: option:launch(value) belongs to multiple rows"
                for surface in SURFACES
            ],
        )

    def test_invalid_cells_do_not_hide_later_surface_diagnostics(self):
        for cell in (
            None,
            [],
            "option:launch(value)",
            [7],
            {"exempt": "why", "extra": 1},
        ):
            with self.subTest(cell=cell):
                self.setUp()
                self.row["core"] = cell
                self.row["ts"] = []
                self.assertEqual(
                    self.verdict(),
                    [
                        "inventory input: core: name declarations or an exemption",
                        "inventory input: ts: name declarations or an exemption",
                        "inventory core: option:launch(value) has no contract row",
                        "inventory ts: option:launch(value) has no contract row",
                    ],
                )

    def exempt_core(self):
        self.row["core"] = {"exempt": "native-input"}
        self.manifest["exemptions"]["native-input"] = (
            "This surface uses a native input."
        )
        self.observed["core"] = {"type:Other": {"kind": "class"}}
        self.manifest["shapes"]["core"] = copy.deepcopy(self.observed["core"])
        self.manifest["contracts"].append(
            {
                "id": "other",
                "kind": "type",
                "core": ["type:Other"],
                **{surface: {"exempt": "native-input"} for surface in SURFACES[1:]},
            }
        )

    def test_exemptions_are_explicit_used_decisions_and_other_surfaces_still_match(
        self,
    ):
        self.exempt_core()
        self.assertEqual(self.verdict(), [])
        self.manifest["exemptions"]["unused"] = "Unreferenced decision."
        self.assertEqual(self.verdict(), ["inventory: unused exemption unused"])

    def test_exemption_reason_must_be_nonempty_prose_without_issue_deferral(self):
        for reason in (None, 7, "", "  \t", "Deferred to #123."):
            with self.subTest(reason=reason):
                self.setUp()
                self.exempt_core()
                self.manifest["exemptions"]["native-input"] = reason
                self.assertEqual(
                    self.verdict(),
                    ["inventory input: core: invalid exemption native-input"]
                    + [
                        f"inventory other: {surface}: invalid exemption native-input"
                        for surface in SURFACES[1:]
                    ],
                )

    def test_contract_cannot_be_exempt_on_every_surface(self):
        self.manifest["exemptions"]["native-input"] = (
            "This surface uses a native input."
        )
        self.manifest["contracts"].append(
            {
                "id": "unimplemented",
                "kind": "type",
                **{surface: {"exempt": "native-input"} for surface in SURFACES},
            }
        )
        self.assertEqual(
            self.verdict(),
            ["inventory unimplemented: no surface implements the contract"],
        )

    def test_missing_observed_surface_preserves_absent_shape_and_mapping_diagnostics(
        self,
    ):
        del self.observed["py"]
        self.assertEqual(
            self.verdict(),
            [
                "inventory py: collector returned no declarations",
                "inventory py: option:launch(value) is recorded but absent",
                "inventory py: option:launch(value) is mapped but absent",
            ],
        )

    def test_missing_expected_surface_requires_its_shapes(self):
        del self.manifest["shapes"]["cli"]
        self.assertEqual(
            self.verdict(),
            [
                "inventory cli: no expected declarations",
                "inventory cli: option:launch(value) has no recorded shape",
            ],
        )

    def test_changed_shape_and_unreviewed_names_are_reported_in_stable_order(self):
        self.observed["ts"]["option:launch(value)"]["required"] = False
        self.observed["ts"].update({"type:Zebra": {}, "type:Alpha": {}})
        self.manifest["shapes"]["ts"].update({"type:OldZ": {}, "type:OldA": {}})
        self.assertEqual(
            self.verdict(),
            [
                "inventory ts: type:Alpha has no recorded shape",
                "inventory ts: type:Zebra has no recorded shape",
                "inventory ts: type:OldA is recorded but absent",
                "inventory ts: type:OldZ is recorded but absent",
                "inventory ts: option:launch(value) changed shape",
                "inventory ts: type:Alpha has no contract row",
                "inventory ts: type:Zebra has no contract row",
            ],
        )


if __name__ == "__main__":
    unittest.main()
