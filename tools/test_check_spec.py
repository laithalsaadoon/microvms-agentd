# SPDX-License-Identifier: Apache-2.0
"""The optional verifier wrapper fails closed and preserves the verifier verdict."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class VerifierTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "tools").mkdir()
        self.script = self.root / "tools/check-spec.sh"
        shutil.copyfile(ROOT / "tools/check-spec.sh", self.script)
        self.verifier = self.root / "verify/formal/node_modules/.bin/symspec"
        self.verifier.parent.mkdir(parents=True)
        self.log = self.root / "called.json"

    def fake(self):
        self.verifier.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "if sys.argv[1:] == ['--version']:\n"
            "    print(os.environ['VERIFIER_VERSION'])\n"
            "else:\n"
            "    Path(os.environ['VERIFIER_LOG']).write_text(json.dumps({\n"
            "        'args': sys.argv[1:], 'model': os.environ['SYMSPEC_MODEL_DIR']}))\n"
            "    sys.exit(int(os.environ['VERIFIER_EXIT']))\n"
        )
        self.verifier.chmod(0o755)

    def run_verifier(self, version="symspec v2.0.0", code=0):
        env = dict(os.environ)
        env.pop("SYMSPEC_MODEL_DIR", None)
        env.update(
            VERIFIER_VERSION=version,
            VERIFIER_EXIT=str(code),
            VERIFIER_LOG=str(self.log),
        )
        return subprocess.run(
            ["bash", str(self.script), "requirements with spaces.json", "--strict"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_missing_environment_reports_setup(self):
        result = self.run_verifier()
        self.assertEqual(result.returncode, 1)
        self.assertIn("mise run spec:setup", result.stderr)

    def test_an_unexpected_version_is_refused(self):
        self.fake()
        for version in ("symspec v1.2.1", "symspec v2.0.1", ""):
            with self.subTest(version=version):
                result = self.run_verifier(version)
                self.assertEqual(result.returncode, 1)
                self.assertIn("Expected symspec 2.0.0", result.stderr)
                self.assertFalse(self.log.exists())

    def test_arguments_assets_and_nonzero_verdict_are_preserved(self):
        self.fake()
        for code in (0, 1, 2, 3):
            with self.subTest(code=code):
                result = self.run_verifier(code=code)
                self.assertEqual(result.returncode, code)
                self.assertEqual(
                    json.loads(self.log.read_text()),
                    {
                        "args": ["check", "requirements with spaces.json", "--strict"],
                        "model": str(self.root / "verify/formal/model"),
                    },
                )


if __name__ == "__main__":
    unittest.main()
