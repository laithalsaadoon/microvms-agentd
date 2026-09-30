# SPDX-License-Identifier: Apache-2.0
"""Regression tests for loading the model used to check client constraints, and for the
check that the ratchet's operation-literal rule names exactly the model's operations."""

import runpy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

DRIFT = runpy.run_path(
    str(Path(__file__).with_name("check-model-drift.py")),
    run_name="tools.check-model-drift",
)


class ModelLoaderTests(unittest.TestCase):
    def test_constraint_loader_rejects_a_new_api_version(self):
        with patch("boto3.Session") as session:
            loader = session.return_value._session.get_component.return_value
            loader.determine_latest_version.return_value = "2099-01-01"
            with self.assertRaisesRegex(
                SystemExit, "latest lambda-microvms API version"
            ):
                DRIFT["load_model"]()
            loader.load_service_model.assert_not_called()

    def test_constraint_loader_uses_the_effective_boto3_model(self):
        with patch("boto3.Session") as session:
            loader = session.return_value._session.get_component.return_value
            loader.determine_latest_version.return_value = "2025-09-09"
            loader.load_service_model.return_value = {"custom-model": True}
            model, _ = DRIFT["load_model"]()
            self.assertEqual(model, {"custom-model": True})
            loader.load_service_model.assert_called_once_with(
                "lambda-microvms", "service-2", api_version="2025-09-09"
            )


class OperationRuleTests(unittest.TestCase):
    """`operations_result` holds `verify/ratchet/rules/operation-literal.yml` to the model (#273)."""

    RULE = DRIFT["OPERATION_RULE"]

    def rule_with(self, text):
        """A rule file holding `text`."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "operation-literal.yml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_the_checked_in_rule_names_exactly_the_model_operations(self):
        model, _ = DRIFT["load_model"]()
        result = DRIFT["operations_result"](self.RULE, model)
        self.assertTrue(result.ok, (result.ours, result.model))
        self.assertEqual(len(result.ours), len(model["operations"]))

    def test_a_rule_missing_an_operation_is_drift(self):
        text = self.RULE.read_text(encoding="utf-8")
        dropped = text.replace("|ListTags|", "|")
        self.assertNotEqual(dropped, text)
        names = DRIFT["rule_operations"](self.rule_with(dropped))
        self.assertNotIn("ListTags", names)
        full = {"operations": {name: {} for name in (*names, "ListTags")}}
        result = DRIFT["operations_result"](self.rule_with(dropped), full)
        self.assertFalse(result.ok)
        self.assertEqual(set(result.model) - set(result.ours), {"ListTags"})

    def test_an_operation_only_the_rule_names_is_drift(self):
        names = DRIFT["rule_operations"](self.RULE)
        model = {"operations": {name: {} for name in names if name != "TagResource"}}
        result = DRIFT["operations_result"](self.RULE, model)
        self.assertFalse(result.ok)
        self.assertEqual(set(result.ours) - set(result.model), {"TagResource"})

    def test_a_repeated_name_is_drift(self):
        text = self.RULE.read_text(encoding="utf-8")
        repeated = text.replace("|ListTags|", "|ListTags|ListTags|")
        names = DRIFT["rule_operations"](self.RULE)
        model = {"operations": {name: {} for name in names}}
        result = DRIFT["operations_result"](self.rule_with(repeated), model)
        self.assertFalse(result.ok)

    def test_a_rule_the_parser_cant_read_is_an_error_not_an_empty_list(self):
        missing = Path(tempfile.mkdtemp()) / "gone.yml"
        cases = {
            "a missing file": missing,
            "an empty file": self.rule_with(""),
            "no regex": self.rule_with("id: operation-literal\nrule:\n  all: []\n"),
            "a regex with no group": self.rule_with(
                "rule:\n  all:\n    - regex: '^\"RunMicrovm\"$'\n"
            ),
            "an empty group": self.rule_with(
                "rule:\n  all:\n    - regex: '^\"()\"$'\n"
            ),
        }
        for case, path in cases.items():
            with self.subTest(case=case), self.assertRaises(SystemExit):
                DRIFT["rule_operations"](path)
        missing.parent.rmdir()


if __name__ == "__main__":
    unittest.main()
