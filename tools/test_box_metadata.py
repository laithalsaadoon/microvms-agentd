# SPDX-License-Identifier: Apache-2.0
"""The example blocks metadata before starting its unprivileged driver."""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples/strands-box-codex-sdk"
spec = importlib.util.spec_from_file_location(
    "probe_mmds", EXAMPLE / "vm/probe_mmds.py"
)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

MICROVM = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["CALLS"], "a") as log:
    log.write(json.dumps(args) + "\\n")
if args[0] == "build":
    print(json.dumps({"data": {"imageIdentifier": "image"}}))
elif args[0] == "run":
    print(json.dumps({"data": {"microvmId": "vm"}}))
elif args[0] == "exec":
    command = args[1]
    if "route replace" in command:
        sys.exit(int(os.environ.get("ROUTE_STATUS", "0")))
    if "probe_mmds.py" in command:
        print("[outside the box] checked")
        sys.exit(int(os.environ.get("OUTSIDE_STATUS", "0")))
    if "probe-box.toml" in command:
        print("[inside the box] checked")
        sys.exit(int(os.environ.get("INSIDE_STATUS", "0")))
"""


class RunOrderingTests(unittest.TestCase):
    def run_example(self, **settings):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            example = root / "example"
            shutil.copytree(EXAMPLE, example)
            binaries = example / ".box-bin"
            binaries.mkdir(exist_ok=True)
            for name in (
                "strands-box",
                "strands-box-sock-alias",
                "strands-box-contain-trampoline",
            ):
                binary = binaries / name
                binary.write_text("#!/bin/sh\nexit 0\n")
                binary.chmod(0o755)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            for name, script in (
                ("microvm", MICROVM),
                ("uvx", "#!/bin/sh\nprintf fixture-placeholder\n"),
            ):
                binary = bin_dir / name
                binary.write_text(script)
                binary.chmod(0o755)
            calls = root / "calls.jsonl"
            env = {
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "CALLS": str(calls),
                "AWS_REGION": "us-east-1",
                **settings,
            }
            result = subprocess.run(
                ["bash", str(example / "run.sh")],
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
            return result, [json.loads(line) for line in calls.read_text().splitlines()]

    def test_root_route_precedes_driver_and_all_launchers_are_explicit_root(self):
        result, calls = self.run_example()
        self.assertEqual(result.returncode, 0, result.stderr)
        route = next(i for i, call in enumerate(calls) if "route replace" in call[1])
        outside = next(i for i, call in enumerate(calls) if "probe_mmds.py" in call[1])
        inside = next(i for i, call in enumerate(calls) if "probe-box.toml" in call[1])
        driver = next(i for i, call in enumerate(calls) if "driver.py" in call[1])
        upload = next(
            i for i, call in enumerate(calls) if "vm:/home/agent/box-bin-upload" in call
        )
        install = next(
            i
            for i, call in enumerate(calls)
            if "mv /home/agent/box-bin-upload /opt/box" in call[1]
        )
        self.assertLess(route, upload)
        self.assertLess(upload, install)
        self.assertLess(install, outside)
        self.assertFalse(any("vm:/opt/box" in call for call in calls))
        self.assertIn("chown -R 0:0 /opt/box", calls[install][1])
        self.assertIn("chmod 0755 /opt/box /opt/box/*", calls[install][1])
        self.assertLess(route, outside)
        self.assertLess(outside, inside)
        self.assertLess(inside, driver)
        self.assertEqual(
            calls[route][1], "ip -4 route replace blackhole 169.254.169.254/32"
        )
        for call in calls:
            if call[0] == "exec":
                self.assertIn("--user", call)
                self.assertEqual(call[call.index("--user") + 1], "0")
        dockerfile = (EXAMPLE / "Dockerfile").read_text()
        self.assertIn("ENV AGENTD_DEFAULT_USER=agent", dockerfile)
        self.assertIn(" iproute", dockerfile)

    def test_route_or_either_probe_failure_prevents_the_driver(self):
        for setting in ("ROUTE_STATUS", "OUTSIDE_STATUS", "INSIDE_STATUS"):
            with self.subTest(setting=setting):
                result, calls = self.run_example(**{setting: "1"})
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any("driver.py" in call[1] for call in calls))
                self.assertTrue(any(call[0] == "terminate" for call in calls))


class ProbeTests(unittest.TestCase):
    def verdict(
        self, *, inside=False, connected=False, issued=False, rule="", proxy=None
    ):
        env = {"PROBE_WHERE": "inside"} if inside else {}
        if proxy:
            env["HTTP_PROXY"] = proxy
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(probe, "raw_socket", return_value=(connected, "status")),
            patch.object(
                probe, "token_put", return_value=(issued, rule, "status")
            ) as token,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            result = probe.main()
            return result, token.call_args.args[0]

    def test_outside_requires_both_refusals_and_ignores_proxy_variables(self):
        self.assertEqual(self.verdict(proxy="http://proxy.invalid"), (0, None))
        self.assertEqual(self.verdict(connected=True)[0], 1)
        self.assertEqual(self.verdict(issued=True)[0], 1)

    def test_inside_still_requires_a_metadata_rule_from_the_gateway(self):
        self.assertEqual(
            self.verdict(
                inside=True, proxy="http://proxy.invalid", rule="metadata_hosts"
            )[0],
            0,
        )
        self.assertEqual(self.verdict(inside=True, proxy="http://proxy.invalid")[0], 1)
        self.assertEqual(self.verdict(inside=True, rule="metadata_hosts")[0], 1)
        self.assertEqual(
            self.verdict(
                inside=True,
                connected=True,
                proxy="http://proxy.invalid",
                rule="metadata_hosts",
            )[0],
            1,
        )
        self.assertEqual(
            self.verdict(inside=True, issued=True, proxy="http://proxy.invalid")[0], 1
        )


if __name__ == "__main__":
    unittest.main()
