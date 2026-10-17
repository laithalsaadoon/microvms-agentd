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


class LiveReleaseInputTests(unittest.TestCase):
    def workflow(self):
        return yaml.safe_load(
            (ROOT / ".github/workflows/live-conformance.yml").read_text()
        )

    def test_live_release_lookup_uses_exact_cli_version_and_fails_closed(self):
        workflow = self.workflow()
        downloads = [
            step
            for step in workflow["jobs"]["draft"]["steps"]
            if step.get("name")
            == "download the matching release and check its SHA256SUMS"
        ]
        self.assertEqual(
            len(downloads), 1, "the workflow needs its matching-release download"
        )
        script = downloads[0]["run"]
        scenarios = (
            ("branch", "1.1.0", "main", False, False, True, "v1.1.0"),
            ("branch", "1.1.1", "main", False, False, False, "v1.1.1"),
            ("branch", "1.1.0/../other", "main", False, False, False, None),
            ("branch", "1.1.0", "main", True, False, False, None),
            ("branch", "1.1.0", "main", False, True, False, "v1.1.0"),
            ("tag", "9.9.9", "v1.1.0", False, False, True, "v1.1.0"),
        )
        for kind, version, ref, api_fails, corrupt, success, wanted in scenarios:
            with self.subTest(
                kind=kind, version=version, api_fails=api_fails, corrupt=corrupt
            ):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    remote = root / "remote" / "v1.1.0"
                    remote.mkdir(parents=True)
                    (remote / "agentd").write_bytes(b"verified fixture bytes")
                    digest = hashlib.sha256(b"verified fixture bytes").hexdigest()
                    (remote / "SHA256SUMS").write_text(f"{digest}  agentd\n")
                    if corrupt:
                        (remote / "agentd").write_bytes(b"changed fixture bytes")
                    gh = root / "gh"
                    gh.write_text(
                        "#!/usr/bin/env python3\n"
                        "import base64, json, os, shutil, sys\n"
                        "from pathlib import Path\n"
                        "args = sys.argv[1:]\n"
                        "with open('calls.jsonl', 'a') as out: out.write(json.dumps(args) + '\\n')\n"
                        "if args[0] == 'api':\n"
                        "    expected = 'https://api.github.com/repos/owner/repo/contents/crates/microvms-cli/Cargo.toml?ref=' + 'a' * 40\n"
                        "    if args != ['api', expected, '--jq', '.content'] or os.environ['API_FAILS'] == '1': sys.exit(3)\n"
                        "    body = '[package]\\nversion = ' + json.dumps(os.environ['CLI_VERSION']) + '\\n'\n"
                        "    print(base64.b64encode(body.encode()).decode())\n"
                        "elif args[:2] == ['release', 'download']:\n"
                        "    if len(args) != 7 or args[3:5] != ['--repo', 'owner/repo'] or args[5] != '--dir': sys.exit(4)\n"
                        "    source = Path('remote') / args[2]\n"
                        "    if not source.is_dir(): sys.exit(5)\n"
                        "    shutil.copytree(source, args[6])\n"
                        "else: sys.exit(6)\n"
                    )
                    gh.chmod(0o755)
                    result = subprocess.run(
                        ["bash", "-c", script],
                        cwd=root,
                        env={
                            **os.environ,
                            "PATH": directory + os.pathsep + os.environ["PATH"],
                            "GITHUB_REPOSITORY": "owner/repo",
                            "HEAD_SHA": "a" * 40,
                            "REF_TYPE": kind,
                            "TAG": ref,
                            "CLI_VERSION": version,
                            "API_FAILS": str(int(api_fails)),
                        },
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode == 0, success, result.stderr)
                    calls = [
                        json.loads(line)
                        for line in (root / "calls.jsonl").read_text().splitlines()
                    ]
                    downloads = [
                        call for call in calls if call[:2] == ["release", "download"]
                    ]
                    self.assertEqual(
                        [call[2] for call in downloads], [wanted] if wanted else []
                    )
                    self.assertEqual(
                        sum(call[0] == "api" for call in calls), int(kind != "tag")
                    )
                    if success:
                        self.assertEqual(
                            (root / "draft-release" / wanted / "agentd").read_bytes(),
                            b"verified fixture bytes",
                        )

    def test_live_matching_release_preserves_privilege_and_binary_sources(self):
        workflow = self.workflow()
        draft = workflow["jobs"]["draft"]
        live = workflow["jobs"]["live"]
        self.assertEqual(draft["if"], "inputs.mode == 'full'")
        self.assertEqual(draft["needs"], "owner")
        self.assertEqual(draft["permissions"], {"contents": "write"})
        self.assertTrue(
            all("checkout" not in step.get("uses", "") for step in draft["steps"])
        )
        self.assertEqual(live["permissions"]["contents"], "read")
        self.assertNotIn("needs.draft.result == 'skipped'", live["if"])
        self.assertIn("needs.draft.result == 'success'", live["if"])
        steps = {step.get("name"): step for step in live["steps"]}
        for name in (
            "build the shipping binary",
            "build the microvm CLI",
            "the binaries under test are the ones just built",
        ):
            self.assertEqual(steps[name]["if"], "github.ref_type != 'tag'")
        source = steps["the binaries under test are the ones just built"]["run"]
        self.assertIn("AGENTD=target/aarch64-unknown-linux-musl/release/agentd", source)
        self.assertIn("MICROVM=target/release/microvm", source)
        directory = steps["the matching release is available for provisioning"]
        self.assertNotIn("if", directory)
        self.assertIn("MICROVM_RELEASE_DIR=$PWD/draft-release", directory["run"])
        download = next(
            step
            for step in live["steps"]
            if step.get("with", {}).get("name") == "draft-release"
        )
        self.assertNotIn("if", download)
        self.assertEqual(
            steps["the binaries under test are the draft's"]["if"],
            "github.ref_type == 'tag'",
        )
        self.assertEqual(
            steps["record that the tag's draft passed"]["if"],
            "github.ref_type == 'tag'",
        )
        marker = next(
            step
            for step in live["steps"]
            if step.get("with", {}).get("name") == "live-verified"
        )
        self.assertEqual(marker["if"], "github.ref_type == 'tag'")


class LiveRecoveryWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = yaml.safe_load(
            (ROOT / ".github/workflows/live-conformance.yml").read_text()
        )
        self.jobs = self.workflow["jobs"]

    def test_recovery_and_full_jobs_are_mutually_exclusive(self):
        inputs = self.workflow.get("on", self.workflow.get(True))["workflow_dispatch"][
            "inputs"
        ]
        self.assertEqual(inputs["mode"]["options"], ["full", "recover"])
        self.assertEqual(inputs["mode"]["default"], "full")
        self.assertTrue(inputs["mode"]["required"])
        for name in ("source_run_id", "manifest_sha256"):
            self.assertEqual(inputs[name]["type"], "string")
        self.assertIs(inputs["exclusive_names"]["default"], False)
        self.assertEqual(inputs["exclusive_names"]["type"], "boolean")
        for name in ("draft", "native-box"):
            self.assertEqual(self.jobs[name]["if"], "inputs.mode == 'full'")
        self.assertIn("inputs.mode == 'full' &&", self.jobs["live"]["if"])
        self.assertEqual(
            self.workflow["concurrency"],
            {"group": "live-conformance", "cancel-in-progress": False},
        )
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})

    def test_recovery_preflight_has_no_aws_authority(self):
        job = self.jobs["recovery-preflight"]
        self.assertEqual(job["needs"], "owner")
        self.assertEqual(
            job["if"], "inputs.mode == 'recover' && needs.owner.result == 'success'"
        )
        self.assertEqual(job["permissions"], {"contents": "read", "actions": "read"})
        self.assertNotIn("environment", job)
        self.assertNotIn("secrets.", json.dumps(job))
        self.assertNotIn("configure-aws-credentials", json.dumps(job))
        steps = job["steps"]
        self.assertEqual(
            steps[0]["with"], {"ref": "${{ github.sha }}", "persist-credentials": False}
        )
        commands = [step["run"] for step in steps if "run" in step]
        self.assertEqual(
            commands,
            [
                'python3 tools/recover-live.py preflight --source-run "$SOURCE_RUN" '
                '--manifest-sha256 "$MANIFEST_SHA" --report recovery-preflight.json'
            ],
        )
        self.assertEqual(
            job["env"],
            {
                "AWS_REGION": "${{ inputs.region }}",
                "SOURCE_RUN": "${{ inputs.source_run_id }}",
                "MANIFEST_SHA": "${{ inputs.manifest_sha256 }}",
                "GH_TOKEN": "${{ github.token }}",
            },
        )

    def test_recovery_apply_requires_provenance_owner_and_environment(self):
        job = self.jobs["recovery"]
        self.assertEqual(job["needs"], ["owner", "recovery-preflight"])
        self.assertEqual(
            job["if"],
            "${{ !cancelled() && inputs.mode == 'recover' && needs.owner.result == 'success' && needs.recovery-preflight.result == 'success' }}",
        )
        self.assertEqual(job["environment"], "live-aws")
        self.assertEqual(
            job["permissions"],
            {"contents": "read", "actions": "read", "id-token": "write"},
        )
        self.assertEqual(job["timeout-minutes"], 20)
        steps = job["steps"]
        self.assertEqual(
            steps[0]["with"], {"ref": "${{ github.sha }}", "persist-credentials": False}
        )
        self.assertNotIn("configure-aws-credentials", json.dumps(job))
        apply_step = next(
            step
            for step in steps
            if step.get("name") == "recover only the reviewed resource identities"
        )
        self.assertIn("tools/recovery-credentials.py", apply_step["run"])
        self.assertEqual(
            job["env"],
            {
                "AWS_REGION": "${{ inputs.region }}",
                "SOURCE_RUN": "${{ inputs.source_run_id }}",
                "MANIFEST_SHA": "${{ inputs.manifest_sha256 }}",
                "EXCLUSIVE_NAMES": "${{ inputs.exclusive_names }}",
                "RECOVERY_ROLE_ARN": "${{ secrets.LIVE_CONFORMANCE_ROLE_ARN }}",
                "GH_TOKEN": "${{ github.token }}",
            },
        )
        uv = next(step for step in steps if "setup-uv@" in step.get("uses", ""))
        full_uv = next(
            step
            for step in self.jobs["live"]["steps"]
            if "setup-uv@" in step.get("uses", "")
        )
        self.assertEqual(uv["uses"], full_uv["uses"])
        self.assertEqual(uv["with"], {"enable-cache": False})

    def test_recovery_inputs_and_exclusive_attestation_are_shell_arguments(self):
        commands = [
            step["run"]
            for step in self.jobs["recovery"]["steps"]
            if step.get("name") == "recover only the reviewed resource identities"
        ]
        self.assertEqual(len(commands), 1)
        script = commands[0]
        self.assertNotIn("${{", script)
        for exclusive in ("false", "true"):
            with (
                self.subTest(exclusive=exclusive),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                uv = root / "uv"
                uv.write_text(
                    "#!/usr/bin/env python3\nimport json, sys\n"
                    "from pathlib import Path\nPath('argv.json').write_text(json.dumps(sys.argv[1:]))\n"
                )
                uv.chmod(0o755)
                source = '123; $(touch injected); "quoted"'
                digest = "abc`touch injected`"
                result = subprocess.run(
                    ["bash", "-c", script],
                    cwd=root,
                    env={
                        **os.environ,
                        "PATH": directory + os.pathsep + os.environ["PATH"],
                        "SOURCE_RUN": source,
                        "MANIFEST_SHA": digest,
                        "EXCLUSIVE_NAMES": exclusive,
                    },
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                expected = [
                    "run",
                    "--script",
                    "tools/recovery-credentials.py",
                    "--source-run",
                    source,
                    "--manifest-sha256",
                    digest,
                    "--report",
                    "recovery.json",
                ]
                if exclusive == "true":
                    expected.append("--exclusive-names")
                self.assertEqual(json.loads((root / "argv.json").read_text()), expected)
                self.assertFalse((root / "injected").exists())

    def test_recovery_reports_never_publish_a_live_marker(self):
        for name, artifact, path, condition in (
            (
                "recovery-preflight",
                "recovery-preflight",
                "recovery-preflight.json",
                "always()",
            ),
            (
                "recovery",
                "recovery-report",
                "recovery.json",
                "always() && !inputs.observe_only",
            ),
            (
                "recovery",
                "recovery-observation",
                "recovery-observation.json",
                "always() && inputs.observe_only",
            ),
        ):
            job = self.jobs[name]
            with self.subTest(job=name):
                self.assertNotIn("live-verified", json.dumps(job))
                self.assertNotIn("terraform", json.dumps(job))
                self.assertNotIn("native-box", json.dumps(job))
                self.assertNotIn("run_rs.py", json.dumps(job))
                uploads = [
                    step
                    for step in job["steps"]
                    if "upload-artifact@" in step.get("uses", "")
                    and step["with"]["name"] == artifact
                ]
                self.assertEqual(len(uploads), 1)
                self.assertEqual(uploads[0]["if"], condition)
                self.assertEqual(
                    uploads[0]["with"],
                    {"name": artifact, "path": path, "if-no-files-found": "error"},
                )
        live_steps = self.jobs["live"]["steps"]
        checkout = next(
            step["uses"] for step in live_steps if "checkout@" in step.get("uses", "")
        )
        upload = next(
            step["uses"]
            for step in live_steps
            if "upload-artifact@" in step.get("uses", "")
        )
        uv = next(
            step["uses"] for step in live_steps if "setup-uv@" in step.get("uses", "")
        )
        for name, allowed in (
            ("recovery-preflight", [checkout, upload]),
            ("recovery", [checkout, uv, upload, upload]),
        ):
            self.assertNotIn("uses", self.jobs[name])
            self.assertEqual(
                [step["uses"] for step in self.jobs[name]["steps"] if "uses" in step],
                allowed,
            )
        release_tasks = tomllib.loads(
            (ROOT / ".config/mise/tasks/release.toml").read_text()
        )
        self.assertTrue(
            any(
                "tools/test_recover_live.py" in command
                and "tools/test_recovery_credentials.py" in command
                for command in release_tasks["release:check"]["run"]
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

    def test_live_credentials_follow_setup_suite_and_cleanup_boundaries(self):
        workflow = yaml.safe_load(
            (ROOT / ".github/workflows/live-conformance.yml").read_text()
        )
        job = workflow["jobs"]["live"]
        self.assertEqual(job["timeout-minutes"], 300)
        steps = job["steps"]
        credentials = [
            (index, step)
            for index, step in enumerate(steps)
            if str(step.get("uses", "")).split("@")[0]
            == "aws-actions/configure-aws-credentials"
        ]
        self.assertEqual(len(credentials), 2)
        setup_index, setup = credentials[0]
        cleanup_index, cleanup = credentials[1]
        for step in (setup, cleanup):
            self.assertEqual(step["with"].get("role-duration-seconds"), 7200)
            self.assertEqual(step["uses"], setup["uses"])
            self.assertEqual(
                step["with"]["role-to-assume"], setup["with"]["role-to-assume"]
            )
        suite_index = next(
            i for i, step in enumerate(steps) if step.get("name") == "conformance suite"
        )
        suite = steps[suite_index]
        self.assertLess(setup_index, suite_index)
        self.assertEqual(cleanup_index, suite_index + 1)
        self.assertEqual(suite["timeout-minutes"], 240)
        self.assertEqual(suite["env"]["PYTHONUNBUFFERED"], "1")
        self.assertEqual(suite["env"]["LIVE_ROLE_ARN"], setup["with"]["role-to-assume"])
        self.assertEqual(
            suite["run"],
            'python3 tools/live-credentials.py ./conformance/run_rs.py --binary "$AGENTD" --microvm-binary "$MICROVM"',
        )
        self.assertEqual(cleanup["if"], "always()")
        self.assertIs(cleanup["with"]["unset-current-credentials"], True)
        self.assertEqual(cleanup["with"]["action-timeout-s"], 120)
        self.assertEqual(cleanup["timeout-minutes"], 3)
        following = steps[cleanup_index + 1 : cleanup_index + 5]
        self.assertEqual(
            [step["name"] for step in following],
            [
                "pinned rates match the Pricing API",
                "verify nothing leaked",
                "destroy the stack",
                "verify again after destroy",
            ],
        )
        self.assertEqual(
            [step.get("if") for step in following],
            [
                None,
                "always()",
                "always() && inputs.destroy_after",
                "always() && inputs.destroy_after",
            ],
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
