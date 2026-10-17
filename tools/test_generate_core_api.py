# SPDX-License-Identifier: Apache-2.0
"""The core snapshot includes declaration contracts and refuses incomplete walks."""

import inspect
import json
import runpy
import unittest
from pathlib import Path
from unittest import mock

GENERATOR = runpy.run_path(
    str(Path(__file__).with_name("generate-core-api.py")),
    run_name="tools.generate-core-api",
)
GLOBALS = inspect.unwrap(GENERATOR["render"]).__globals__


class Snapshot(unittest.TestCase):
    def test_snapshot_retains_declarations_and_omits_none(self):
        entries = [
            GLOBALS["rustdoc_walk"].Entry(
                path="api::Options",
                kind="struct",
                declaration={
                    "kind": "struct",
                    "fields": {"enabled": {"primitive": "bool"}},
                },
            ),
            GLOBALS["rustdoc_walk"].Entry(
                path="api::call",
                kind="function",
                declaration={
                    "parameters": {"options": {"type": "api::Options", "position": 0}}
                },
            ),
            GLOBALS["rustdoc_walk"].Entry(
                path="api::external", kind="external", declaration=None
            ),
        ]
        doc_dir = Path("built-docs")
        with (
            mock.patch.dict(GLOBALS, {"SENTINELS": {"api::call": "function"}}),
            mock.patch.object(
                GLOBALS["rustdoc_walk"], "stitch", return_value=entries
            ) as stitch,
        ):
            text = GENERATOR["render"](doc_dir)
        stitch.assert_called_once_with(
            doc_dir, GENERATOR["CRATES"][0], GENERATOR["CRATES"]
        )
        self.assertEqual(
            json.loads(text),
            {
                "generated_by": "tools/generate-core-api.py; regenerate with `mise run core-api`",
                "crates": list(GENERATOR["CRATES"]),
                "paths": {
                    "api::Options": "struct",
                    "api::call": "function",
                    "api::external": "external",
                },
                "declarations": {
                    "api::Options": entries[0].declaration,
                    "api::call": entries[1].declaration,
                },
            },
        )
        self.assertTrue(text.endswith("\n"))
        self.assertEqual(
            text,
            "{\n"
            '  "generated_by": "tools/generate-core-api.py; regenerate with `mise run core-api`",\n'
            '  "crates": [\n'
            '    "microvms_core",\n'
            '    "microvms_app",\n'
            '    "microvms_domain",\n'
            '    "microvms_edges",\n'
            '    "microvms_protocol"\n'
            "  ],\n"
            '  "paths": {\n'
            '    "api::Options": "struct",\n'
            '    "api::call": "function",\n'
            '    "api::external": "external"\n'
            "  },\n"
            '  "declarations": {\n'
            '    "api::Options": {"fields": {"enabled": {"primitive": "bool"}}, "kind": "struct"},\n'
            '    "api::call": {"parameters": {"options": {"position": 0, "type": "api::Options"}}}\n'
            "  }\n"
            "}\n",
        )

    def test_an_absent_or_wrong_kind_sentinel_prevents_a_snapshot(self):
        for entries in (
            [],
            [GLOBALS["rustdoc_walk"].Entry(path="api::call", kind="struct")],
        ):
            with (
                self.subTest(entries=entries),
                mock.patch.dict(GLOBALS, {"SENTINELS": {"api::call": "function"}}),
                mock.patch.object(
                    GLOBALS["rustdoc_walk"], "stitch", return_value=entries
                ),
            ):
                with self.assertRaises(GLOBALS["rustdoc_walk"].StitchError) as raised:
                    GENERATOR["render"](Path("built-docs"))
                observed_kind = "'struct'" if entries else "None"
                self.assertEqual(
                    str(raised.exception),
                    f"the walk found {len(entries)} paths, and these sentinels are missing or of"
                    f" another kind: {{'api::call': {observed_kind}}}. Every tree since #283 has them,"
                    " so the walk or the JSON's shape is what changed",
                )


if __name__ == "__main__":
    unittest.main()
