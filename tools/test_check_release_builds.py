# SPDX-License-Identifier: Apache-2.0
"""Release build policy over the actual workflow and ordinary configuration changes."""

import contextlib
import copy
import hashlib
import io
import json
import os
import re
import runpy
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import mise_config
import yaml

ROOT = Path(__file__).resolve().parent.parent
policy = SimpleNamespace(
    **runpy.run_path(
        str(ROOT / "tools/check-release-builds.py"),
        run_name="tools.check-release-builds",
    )
)


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
                patch(
                    "sys.argv",
                    ["check-release-builds.py"]
                    + (["--root", str(root)] if expected else []),
                ),
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
                if not expected:
                    self.assertEqual(
                        output.getvalue(),
                        "release builds: fresh dependencies and pinned tools\n",
                    )

    def test_live_credentials_outlast_the_whole_job(self):
        # One static session serves the suite and its failure-path cleanup. Comparing
        # against the job budget catches both a shorter session and a longer job.
        workflow = yaml.safe_load(
            (ROOT / ".github/workflows/live-conformance.yml").read_text()
        )
        job = workflow.get("jobs", {}).get("live", {})
        timeout = job.get("timeout-minutes")
        self.assertIs(type(timeout), int, "live job needs an explicit numeric timeout")
        self.assertGreater(timeout, 0)
        credentials = [
            step
            for step in job.get("steps", [])
            if str(step.get("uses", "")).split("@")[0]
            == "aws-actions/configure-aws-credentials"
        ]
        self.assertEqual(len(credentials), 1, "live job needs its credential step")
        duration = credentials[0].get("with", {}).get("role-duration-seconds")
        self.assertIs(
            type(duration), int, "live credentials need an explicit numeric duration"
        )
        self.assertGreater(
            duration,
            timeout * 60,
            "live credentials must outlast the whole job, including cleanup",
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

    def test_uv_uses_the_repository_pin(self):
        expected = tomllib.loads((ROOT / "mise.toml").read_text())["tools"]["uv"]
        for job in self.workflow["jobs"].values():
            for step in job["steps"]:
                if str(step.get("uses", "")).startswith("astral-sh/setup-uv@"):
                    self.assertEqual(step["with"]["version"], expected)
                    step["with"]["version"] = "latest"
                    self.assert_refused("uv needs an exact version")
                    step["with"]["version"] = expected

    def test_draft_retry_reuses_the_first_complete_assets(self):
        script = self.step("draft", "draft the release")["run"]
        for state in (
            "",
            "true",
            "false",
            "incomplete",
            "cli-sidecar-missing",
            "changed",
            "list-error",
        ):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                remote = root / "remote"
                remote.mkdir()
                (remote / "agentd").write_bytes(b"original binary")
                for name in ("agentd.sigstore.json", "agentd.intoto.jsonl"):
                    (remote / name).write_text("bundle")
                cli = root / "cli-dist"
                cli.mkdir()
                for name in (
                    "microvm-linux.tar.gz",
                    "microvm-linux.tar.gz.sigstore.json",
                    "microvm-linux.tar.gz.intoto.jsonl",
                ):
                    (cli / name).write_text("CLI asset")
                    (remote / name).write_text("CLI asset")
                digest = hashlib.sha256(b"original binary").hexdigest()
                original = f"{digest}  agentd\n"
                (remote / "SHA256SUMS").write_text(original)
                if state == "incomplete":
                    (remote / "agentd.sigstore.json").unlink()
                if state == "cli-sidecar-missing":
                    (remote / "microvm-linux.tar.gz.sigstore.json").unlink()
                if state == "changed":
                    (remote / "agentd").write_bytes(b"changed binary")
                (root / "SHA256SUMS").write_text("0" * 64 + "  agentd\n")
                executable = root / "gh"
                executable.write_text(
                    "#!/bin/sh\nset -eu\n"
                    'case "$2" in\n'
                    'list) [ "$STATE" != list-error ] || exit 1; '
                    '[ -z "$STATE" ] || [ "$STATE" = false ] && echo "$STATE" || echo true ;;\n'
                    "download) cp -r remote retained ;;\n"
                    "create) touch created ;;\n"
                    "*) exit 2 ;;\nesac\n"
                )
                executable.chmod(0o755)
                result = subprocess.run(
                    ["bash", "-c", script],
                    cwd=root,
                    env={
                        **os.environ,
                        "PATH": directory + os.pathsep + os.environ["PATH"],
                        "STATE": state,
                        "TAG": "v1.2.3",
                        "GITHUB_REPOSITORY": "owner/repo",
                    },
                    capture_output=True,
                )
                self.assertEqual(
                    result.returncode == 0, state in ("", "true"), result.stderr
                )
                self.assertEqual((root / "created").exists(), state == "")
                if state == "true":
                    self.assertEqual((root / "SHA256SUMS").read_text(), original)

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
                        if broken == "":
                            self.assertEqual(
                                output.getvalue(),
                                f"release builds: FAILED\n  cannot read release build inputs: {path} is empty\n",
                            )
                        elif broken == "[]":
                            self.assertEqual(
                                output.getvalue(),
                                "release builds: FAILED\n  cannot read release build inputs: release inputs must be mappings\n",
                            )
                        else:
                            self.assertIn(f"inputs: {path}:", output.getvalue())
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

    def test_command_reports_policy_violations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path in (policy.WORKFLOW, policy.PACKAGE, policy.LOCK):
                (root / path).parent.mkdir(parents=True, exist_ok=True)
                (root / path).write_text((ROOT / path).read_text())
            package = copy.deepcopy(self.package)
            package["devDependencies"]["@napi-rs/cli"] = "^3.10.8"
            (root / policy.PACKAGE).write_text(json.dumps(package))
            with (
                patch("sys.argv", ["check-release-builds.py", "--root", directory]),
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(policy.main(), 1)
        self.assertEqual(
            output.getvalue(),
            "release builds: FAILED\n  the Node generator must have an exact version\n"
            "  the Node lock must match the package's development dependencies\n",
        )

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
        for jobs in (None, {}, [], "jobs"):
            self.workflow = {"jobs": jobs}
            self.assertEqual(self.problems(), ["release workflow has no jobs"])
        self.workflow = {}
        self.assertEqual(self.problems(), ["release workflow has no jobs"])
        self.setUp()
        del self.workflow["jobs"]["wheels"]
        self.assert_refused("missing artifact job wheels")
        self.setUp()
        self.workflow["jobs"]["wheels"] = {}
        self.assertEqual(self.problems(), [])

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
                self.assert_refused("caches must be disabled")
            self.workflow = workflow

    def test_independent_release_policy_violations(self):
        steps = self.workflow["jobs"]["draft"]["steps"]
        for step, reason in (
            ({"uses": "owner/CACHE@pin"}, "release jobs cannot restore caches"),
            (
                {"uses": "actions/checkout@pin"},
                "checkout must disable credential persistence",
            ),
            (
                {"uses": "actions/setup-node@pin"},
                "Node dependency caches must be disabled",
            ),
            ({"uses": "astral-sh/setup-uv@pin"}, "uv caches must be disabled"),
            (
                {"uses": "actions/checkout@pin", "with": {"persist-credentials": True}},
                "checkout must disable credential persistence",
            ),
            (
                {
                    "uses": "actions/setup-node@pin",
                    "with": {
                        "node-version": "24.21.0",
                        "package-manager-cache": False,
                        "cache": "npm",
                    },
                },
                "Node dependency caches must be disabled",
            ),
            (
                {"uses": "PyO3/maturin-action@pin"},
                "install only the pinned release tools explicitly",
            ),
            ({"run": "uvx maturin@1.14 build"}, "maturin needs an exact version"),
            (
                {"run": "cargo install tool --version 1.2.3"},
                "cargo install needs an exact version and --locked",
            ),
            (
                {"run": "cargo install tool --version 1 --locked"},
                "cargo install needs an exact version and --locked",
            ),
        ):
            with self.subTest(step=step):
                steps.append(step)
                self.assertTrue(self.problems())
                self.assert_refused(reason)
                steps.pop()

    def test_required_tools_tag_guard_and_lock_fields(self):
        for tool, text in (
            ("maturin", "uvx maturin"),
            ("cargo install", "cargo install"),
            ("napi", "npm exec --offline -- napi build"),
        ):
            original = copy.deepcopy(self.workflow)
            for job in self.workflow["jobs"].values():
                job["steps"] = [
                    s for s in job["steps"] if text not in str(s.get("run"))
                ]
            self.assert_refused(f"no {tool} tool step")
            self.workflow = original
        original = copy.deepcopy(self.workflow["jobs"]["guard"])
        for steps in (
            [],
            [{"run": "echo ready"}],
            [{"run": policy.TAG_PATTERN, "env": {"TAG": "another tag"}}],
        ):
            self.workflow["jobs"]["guard"]["steps"] = steps
            self.assertIn(
                "guard must validate the release tag before any other step",
                self.problems(),
            )
        self.workflow["jobs"]["guard"] = {"steps": [{"run": policy.TAG_PATTERN}]}
        self.assertIn(
            "guard must validate the release tag before any other step", self.problems()
        )
        del self.workflow["jobs"]["guard"]
        self.assertIn(
            "guard must validate the release tag before any other step", self.problems()
        )
        self.workflow["jobs"]["guard"] = original
        original_lock = copy.deepcopy(self.lock)
        for field, value in (("lockfileVersion", 2), ("packages", {})):
            self.lock[field] = value
            self.assert_refused("lock must")
            self.lock = copy.deepcopy(original_lock)
        for field in ("packages", "lockfileVersion"):
            del self.lock[field]
            self.assertIn(
                "the Node lock must match the package's development dependencies",
                self.problems(),
            )
            self.lock = copy.deepcopy(original_lock)
        self.lock["packages"].pop("node_modules/@napi-rs/cli")
        self.assertIn("the Node lock must contain the generator", self.problems())
        self.lock = copy.deepcopy(original_lock)
        self.lock["packages"].pop("")
        self.assertIn(
            "the Node lock must match the package's development dependencies",
            self.problems(),
        )
        self.lock = copy.deepcopy(original_lock)
        for resolved in (
            None,
            "http://registry.npmjs.org/cli.tgz",
            "https://example.com/cli.tgz",
        ):
            entry = self.lock["packages"]["node_modules/@napi-rs/cli"]
            if resolved is None:
                del entry["resolved"]
            else:
                entry["resolved"] = resolved
            self.assert_refused("registry integrity")
            self.lock = copy.deepcopy(original_lock)

    def test_a_missing_generator_manifest_field_is_refused(self):
        self.package = {}
        self.assertIn("the Node generator must have an exact version", self.problems())
        self.package = {"devDependencies": {}}
        self.assertIn("the Node generator must have an exact version", self.problems())

    def test_node_tool_install_must_precede_build_in_its_directory(self):
        install = self.step("addons", "install the locked Node build tools")
        install["working-directory"] = "another-directory"
        self.assert_refused("install the locked Node tools before building")
        install["working-directory"] = "bindings/microvms-js"
        steps = self.workflow["jobs"]["addons"]["steps"]
        steps.remove(install)
        steps.append(install)
        self.assert_refused("install the locked Node tools before building")

    def test_floating_tools_and_incomplete_lock_fail(self):
        self.step("cli", "install cargo-zigbuild")["run"] = (
            "cargo install cargo-zigbuild --locked"
        )
        self.assert_refused("exact version and --locked")
        self.step("addons", "napi build")["run"] = (
            "npx -y -p @napi-rs/cli@3.10.8 napi build"
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
        self.package["devDependencies"]["@napi-rs/cli"] = "^3.10.8"
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


class LambdaZigTests(unittest.TestCase):
    # Official 0.16.0 hashes from https://ziglang.org/download/index.json.
    HASHES = {
        "aarch64": "ea4b09bfb22ec6f6c6ceac57ab63efb6b46e17ab08d21f69f3a48b38e1534f17",
        "x86_64": "70e49664a74374b48b51e6f3fdfbf437f6395d42509050588bd49abe52ba3d00",
    }

    def setUp(self):
        self.config = mise_config.load(ROOT)
        self.task = self.config.tasks["npm:lambda:build"].table

    def test_zig_is_not_installed_by_mise_before_the_lambda_task(self):
        tools = [self.config.data.get("tools", {})]
        tools.extend(task.table.get("tools", {}) for task in self.config.tasks.values())
        for group in tools:
            self.assertFalse(set(group) & {"zig", "core:zig"})
        self.assertNotIn("zig@", self.task["run"])

    def run_installer(self, machine, corrupt=False):
        arch = "aarch64" if machine in {"aarch64", "arm64"} else "x86_64"
        version = self.config.data["vars"]["zig"]
        script = self.task["run"]
        for name, value in self.config.data["vars"].items():
            script = script.replace("{{vars." + name + "}}", str(value))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bin").mkdir()
            (root / "tmp").mkdir()
            archive = root / "fixture.tar.xz"
            binary = f"#!/bin/sh\nprintf '%s\\n' '{version}'\n".encode()
            with tarfile.open(archive, "w:xz") as tar:
                entry = tarfile.TarInfo(f"zig-{arch}-linux-{version}/zig")
                entry.mode = 0o755
                entry.size = len(binary)
                tar.addfile(entry, io.BytesIO(binary))
            # Use real checksum/extraction commands with a local archive, keeping
            # the production hash assertions independent of the fixture contents.
            self.assertIn(self.HASHES[arch], script)
            script = script.replace(
                self.HASHES[arch], hashlib.sha256(archive.read_bytes()).hexdigest()
            )
            if corrupt:
                with archive.open("ab") as target:
                    target.write(b"corrupt")
            stub = (
                f"#!{sys.executable}\n"
                + """import json, os, shutil, subprocess, sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
def record(value):
    with open(os.environ["EVENTS"], "a") as stream:
        stream.write(value + "\\n")
if name == "uname":
    print(os.environ["MACHINE"])
elif name == "curl":
    assert args[-1] == os.environ["EXPECTED_URL"], args
    record("download")
    shutil.copyfile(os.environ["ARCHIVE"], args[args.index("-o") + 1])
elif name == "tar":
    record("extract")
    sys.exit(subprocess.call([os.environ["REAL_TAR"], *args]))
elif name == "mise":
    assert args[0] == "exec" and args[1] == os.environ["DRIVER"], args
    assert args[2:4] == ["--", "npx"], args
    assert args[args.index("--target") + 1] == os.environ["TARGET"], args
    result = subprocess.run(["zig", "version"], text=True, capture_output=True, check=True)
    assert result.stdout.strip() == os.environ["VERSION"], result
    assert shutil.which("zig").startswith(os.environ["TMPDIR"] + "/"), shutil.which("zig")
    record("build")
else:
    sys.exit(91)
"""
            )
            for tool in ("uname", "curl", "tar", "mise", "zig"):
                path = root / "bin" / tool
                path.write_text(stub)
                path.chmod(0o755)
            events = root / "events"
            result = subprocess.run(
                ["bash", "-e", "-c", script],
                env={
                    **os.environ,
                    "PATH": str(root / "bin") + os.pathsep + os.environ["PATH"],
                    "TMPDIR": str(root / "tmp"),
                    "ARCHIVE": str(archive),
                    "EVENTS": str(events),
                    "MACHINE": machine,
                    "VERSION": version,
                    "REAL_TAR": shutil.which("tar"),
                    "TARGET": arch + "-unknown-linux-gnu",
                    "DRIVER": "cargo:cargo-zigbuild@"
                    + self.config.data["vars"]["cargo_zigbuild"],
                    "EXPECTED_URL": f"https://ziglang.org/download/{version}/zig-{arch}-linux-{version}.tar.xz",
                },
                text=True,
                capture_output=True,
                timeout=15,
            )
            self.assertEqual(
                list((root / "tmp").iterdir()), [], "staging directory leaked"
            )
            return result, events.read_text().splitlines() if events.exists() else []

    def test_lambda_zig_selects_and_verifies_each_linux_archive(self):
        for machine in ("aarch64", "arm64", "x86_64"):
            with self.subTest(machine=machine):
                result, events = self.run_installer(machine)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(events, ["download", "extract", "build"])

    def test_lambda_zig_rejects_corruption_before_extracting_or_building(self):
        result, events = self.run_installer("aarch64", corrupt=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(events, ["download"])


if __name__ == "__main__":
    unittest.main()
