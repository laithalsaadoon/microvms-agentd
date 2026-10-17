# SPDX-License-Identifier: Apache-2.0
"""Index-signature contracts from pinned TypeDoc and TypeScript output."""

import copy
import json
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
FIXTURE = Path(__file__).parent / "fixtures/parity-index-signatures/typedoc.json"
SURFACES = ("core", "cli", "py", "ts")
ENTRY = {"type": "reference", "target": "Entry", "name": "Entry"}
STRING_INDEX = {
    "name": "__index",
    "variant": "signature",
    "kind": 8192,
    "flags": {"isReadonly": True},
    "parameters": [
        {
            "name": "name",
            "variant": "param",
            "kind": 32768,
            "flags": {},
            "type": {"type": "intrinsic", "name": "string"},
        }
    ],
    "type": ENTRY,
}
NUMBER_INDEX = {
    "name": "__index",
    "variant": "signature",
    "kind": 8192,
    "flags": {"isReadonly": True},
    "parameters": [
        {
            "name": "index",
            "variant": "param",
            "kind": 32768,
            "flags": {},
            "type": {"type": "intrinsic", "name": "number"},
        }
    ],
    "type": ENTRY,
}
TABLE = {
    "kind": "interface",
    "type": None,
    "parameters": [],
    "extends": [],
    "implements": [],
    "flags": {},
    "members": {},
    "indexSignatures": [STRING_INDEX, NUMBER_INDEX],
}


class TypeScriptIndexSignatures(unittest.TestCase):
    def setUp(self):
        self.project = json.loads(FIXTURE.read_text())

    def table(self, name):
        children = {
            item["name"]: copy.deepcopy(item) for item in self.project["children"]
        }
        table = children[name]
        table["name"] = "Table"
        return inventory.typescript({"children": [children["Entry"], table]})

    def test_real_interface_and_class_keep_complete_index_signatures(self):
        observed = inventory.typescript(self.project)
        self.assertEqual(observed["type:Table"], TABLE)
        self.assertEqual(observed["type:TableClass"]["kind"], "class")
        self.assertEqual(observed["type:TableClass"]["indexSignatures"], [STRING_INDEX])
        self.assertNotIn("indexSignatures", observed["type:Entry"])

    def test_real_index_signatures_normalize_ids_sources_and_comments(self):
        def move(value):
            if isinstance(value, list):
                return [move(item) for item in value]
            if isinstance(value, dict):
                return {
                    key: child + 1000
                    if key in ("id", "target") and isinstance(child, int)
                    else [{"fileName": "moved.d.ts", "line": 99, "character": 5}]
                    if key == "sources"
                    else {"summary": [{"kind": "text", "text": "New docs."}]}
                    if key == "comment"
                    else move(child)
                    for key, child in value.items()
                }
            return value

        self.assertEqual(
            inventory.typescript(move(self.project)),
            inventory.typescript(self.project),
        )

    def test_real_index_signature_changes_reject_reviewed_shapes(self):
        symbol_index = copy.deepcopy(STRING_INDEX)
        symbol_index["parameters"][0]["type"]["name"] = "symbol"
        union_index = copy.deepcopy(STRING_INDEX)
        union_index["type"] = {
            "type": "union",
            "types": [ENTRY, {"type": "intrinsic", "name": "undefined"}],
        }
        writable_index = copy.deepcopy(STRING_INDEX)
        writable_index["flags"] = {}
        variants = {
            "ChangedKey": [symbol_index, NUMBER_INDEX],
            "ChangedValue": [union_index, NUMBER_INDEX],
            "Writable": [writable_index, NUMBER_INDEX],
            "Deleted": [NUMBER_INDEX],
            "Reordered": [NUMBER_INDEX, STRING_INDEX],
        }
        enrolled = self.table("Table")
        observed = {surface: copy.deepcopy(enrolled) for surface in SURFACES}
        manifest = {
            "schema": 1,
            "shapes": copy.deepcopy(observed),
            "contracts": [
                {
                    "id": name,
                    "kind": name.split(":", 1)[0],
                    **{surface: [name] for surface in SURFACES},
                }
                for name in enrolled
            ],
            "exemptions": {},
        }
        self.assertEqual(inventory.problems(manifest, observed, set()), [])
        for name, signatures in variants.items():
            with self.subTest(declaration=name):
                expected = copy.deepcopy(TABLE)
                expected["indexSignatures"] = signatures
                observed["ts"] = self.table(name)
                self.assertEqual(
                    inventory.problems(manifest, observed, set()),
                    ["inventory ts: type:Table changed shape"],
                )
                self.assertEqual(observed["ts"]["type:Table"], expected)


if __name__ == "__main__":
    unittest.main()
