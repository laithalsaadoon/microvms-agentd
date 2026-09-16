# SPDX-License-Identifier: Apache-2.0
"""Regression tests for loading the model used to check client constraints."""

import runpy
import unittest
from pathlib import Path
from unittest.mock import patch

DRIFT = runpy.run_path(str(Path(__file__).with_name("check-model-drift.py")))


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


if __name__ == "__main__":
    unittest.main()
