# SPDX-License-Identifier: Apache-2.0
"""Release build policy over the actual workflow and ordinary configuration changes."""

import contextlib
import copy
import importlib.util
import io
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "tools.check-release-builds", ROOT / "tools/check-release-builds.py"
)
policy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(policy)


class ReleaseBuildTests(unittest.TestCase):
    def setUp(self):
        self.workflow = yaml.safe_load((ROOT / policy.WORKFLOW).read_text())
        self.package = json.loads((ROOT / policy.PACKAGE).read_text())
        self.lock = json.loads((ROOT / policy.LOCK).read_text())

    def problems(self):
        return policy.check(self.workflow, self.package, self.lock)

    def assert_refused(self, fragment):
        self.assertIn(fragment, "\n".join(self.problems()))

    def step(self, job, name):
        return next(
            s for s in self.workflow["jobs"][job]["steps"] if s.get("name") == name
        )

    def test_command_reports_success_and_input_errors(self):
        for root, expected in ((ROOT, 0), (ROOT / "absent-release-inputs", 1)):
            with (
                self.subTest(root=root),
                patch.object(policy, "ROOT", root),
                patch("sys.argv", ["check-release-builds.py"]),
            ):
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(policy.main(), expected)
                self.assertIn("release builds:", output.getvalue())
                self.assertIn(
                    "cannot read release build inputs"
                    if expected
                    else "fresh dependencies and pinned tools",
                    output.getvalue(),
                )

    def test_node_build_commands_use_the_manifest_generator_pin(self):
        expected = self.package["devDependencies"]["@napi-rs/cli"]
        paths = (
            ".config/mise/tasks/contracts.toml",
            ".github/workflows/addons.yml",
            "bindings/microvms-js/AGENTS.md",
            "tools/fail-to-pass.py",
        )
        for path in paths:
            with self.subTest(path=path):
                versions = re.findall(
                    r"@napi-rs/cli@([0-9.]+)", (ROOT / path).read_text()
                )
                self.assertTrue(versions, f"{path} names no Node generator")
                self.assertEqual(
                    set(versions), {expected}, f"{path} uses another Node generator"
                )

    def test_python_build_commands_use_the_stub_generator_pin(self):
        source = (ROOT / "tools/generate-py-stubs.py").read_text()
        expected = re.search(r'MATURIN = "maturin@([0-9.]+)"', source).group(1)
        paths = (
            ".config/mise/tasks/test.toml",
            ".github/workflows/release.yml",
            "bindings/microvms-py/AGENTS.md",
        )
        for path in paths:
            with self.subTest(path=path):
                versions = re.findall(r"maturin@([0-9.]+)", (ROOT / path).read_text())
                self.assertTrue(versions, f"{path} names no Python build tool")
                self.assertEqual(
                    set(versions), {expected}, f"{path} uses another Python build tool"
                )

    def test_repository_release_builds_pass(self):
        self.assertEqual(self.problems(), [])

    def test_command_reads_each_required_input_and_its_explicit_root(self):
        paths = (policy.WORKFLOW, policy.PACKAGE, policy.LOCK)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path in paths:
                (root / path).parent.mkdir(parents=True, exist_ok=True)
                (root / path).write_text((ROOT / path).read_text())
            for path in paths:
                original = (root / path).read_text()
                for broken in ("", "{", "[]"):
                    (root / path).write_text(broken)
                    with (
                        self.subTest(path=path, broken=broken),
                        patch(
                            "sys.argv", ["check-release-builds.py", "--root", str(root)]
                        ),
                    ):
                        with contextlib.redirect_stdout(io.StringIO()) as output:
                            self.assertEqual(policy.main(), 1)
                        self.assertTrue(
                            output.getvalue().startswith(
                                "release builds: FAILED\n  cannot read release build inputs:"
                            )
                        )
                    (root / path).write_text(original)

    def test_command_help_names_the_policy_and_root_option(self):
        with patch("sys.argv", ["check-release-builds.py", "--help"]):
            with (
                contextlib.redirect_stdout(io.StringIO()) as output,
                self.assertRaises(SystemExit) as result,
            ):
                policy.main()
        self.assertEqual(result.exception.code, 0)
        self.assertIn(policy.__doc__.splitlines()[0], output.getvalue())
        self.assertIn("--root ROOT", output.getvalue())

    def test_release_tags_are_validated_before_work_starts(self):
        step = self.workflow["jobs"]["guard"]["steps"][0]
        for tag, expected in (
            ("v1.2.3", 0),
            ("v1.2.3-rc.1", 0),
            ("v1.2.3+build", 1),
            ("1.2.3", 1),
            ("v1.2.3 extra", 1),
            ("", 1),
        ):
            with self.subTest(tag=tag):
                result = subprocess.run(
                    ["bash", "-c", step["run"]],
                    env={**os.environ, "TAG": tag},
                    capture_output=True,
                )
                self.assertEqual(result.returncode, expected, result.stdout)

    def test_empty_workflow_and_missing_artifact_job_fail(self):
        self.workflow = {}
        self.assert_refused("no jobs")
        self.setUp()
        del self.workflow["jobs"]["wheels"]
        self.assert_refused("missing artifact job wheels")

    def test_cache_restore_and_checkout_credentials_fail(self):
        self.workflow["jobs"]["draft"]["steps"].append(
            {"uses": "Swatinem/rust-cache@fixed"}
        )
        self.assert_refused("cannot restore caches")
        checkout = next(
            s
            for s in self.workflow["jobs"]["draft"]["steps"]
            if str(s.get("uses", "")).startswith("actions/checkout@")
        )
        del checkout["with"]["persist-credentials"]
        self.assert_refused("disable credential persistence")

    def test_automatic_caches_fail(self):
        for action, option in (
            ("actions/setup-node", "package-manager-cache"),
            ("astral-sh/setup-uv", "enable-cache"),
        ):
            workflow = copy.deepcopy(self.workflow)
            step = next(
                s
                for j in self.workflow["jobs"].values()
                for s in j["steps"]
                if str(s.get("uses", "")).startswith(action + "@")
            )
            step["with"][option] = True
            with self.subTest(action=action):
                self.assertTrue(self.problems())
            self.workflow = workflow

    def test_floating_tools_and_incomplete_lock_fail(self):
        self.step("cli", "install cargo-zigbuild")["run"] = (
            "cargo install cargo-zigbuild --locked"
        )
        self.assert_refused("exact version and --locked")
        self.step("addons", "napi build")["run"] = (
            "npx -y -p @napi-rs/cli@3.8.2 napi build"
        )
        self.assert_refused("locked local Node tools")
        self.lock["packages"]["node_modules/@napi-rs/cli"].pop("integrity")
        self.assert_refused("registry integrity")

    def test_pinned_generators_and_their_lock_are_required(self):
        node = next(
            s
            for s in self.workflow["jobs"]["addons"]["steps"]
            if str(s.get("uses", "")).startswith("actions/setup-node@")
        )
        node["with"]["node-version"] = "24"
        self.assert_refused("Node needs an exact version")
        step = self.step("wheels", "build the wheel")
        step["run"] = step["run"].replace("maturin@1.14.1", "maturin")
        self.assert_refused("maturin needs an exact version")
        self.package["devDependencies"]["@napi-rs/cli"] = "^3.8.2"
        self.assert_refused("generator must have an exact version")
        self.assert_refused("lock must match")
        self.workflow["jobs"]["addons"]["steps"].remove(
            self.step("addons", "install the locked Node build tools")
        )
        self.assert_refused("install the locked Node tools before building")

    def test_npm_version_boundary_is_checked_locally(self):
        run = self.step("npm", "the npm CLI can speak OIDC")["run"]
        script = run[run.index("NPM_VERSION=$(npm --version)") :].replace(
            "NPM_VERSION=$(npm --version)", "NPM_VERSION=$NPM_TEST_VERSION", 1
        )
        for version, expected in (
            ("11.5.1", 0),
            ("11.5.0", 1),
            ("11.4.9", 1),
            ("10.9.0", 1),
            ("11.6.0", 0),
            ("12.0.0", 0),
            ("11.5.1-beta", 1),
            ("", 1),
        ):
            with self.subTest(version=version):
                result = subprocess.run(
                    ["bash", "-c", script],
                    env={**os.environ, "NPM_TEST_VERSION": version},
                    capture_output=True,
                )
                self.assertEqual(result.returncode, expected, result.stderr)

    def test_tag_interpolation_is_refused(self):
        self.step("draft", "draft the release")["run"] += " ${{ github.ref_name }}"
        self.assert_refused("environment variable")


if __name__ == "__main__":
    unittest.main()
