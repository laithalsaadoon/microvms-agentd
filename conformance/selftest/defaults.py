# SPDX-License-Identifier: Apache-2.0
"""The conformance consumers follow the generated shared default, without a CLI."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from harness.constants import AGENT_PORT, REPO
from harness.results import Results


def check_example_agent_ports(results: Results) -> None:
    # These templates explicitly mirror the client's default daemon stanza;
    # application ports such as code-server's 8080 remain separate.
    for example in (
        "coding-agents-on-bedrock",
        "strands-box-codex-sdk",
        "s3-prefetch-at-build",
        "code-server-remote-dev",
    ):
        lines = (REPO / "examples" / example / "Dockerfile").read_text().splitlines()
        daemon_ports = [line for line in lines if line.startswith("ENV AGENTD_PORT=")]
        results.check(
            f"example {example} follows the shared daemon port",
            daemon_ports == [f"ENV AGENTD_PORT={AGENT_PORT}"]
            and f"EXPOSE {AGENT_PORT}" in lines,
        )


def check_agent_port(results: Results, temporary: Path) -> None:
    fixture = temporary / "agent-port"
    package = fixture / "conformance" / "harness"
    package.mkdir(parents=True)
    for name in ("__init__.py", "constants.py", "daemon.py", "image.py"):
        shutil.copyfile(REPO / "conformance" / "harness" / name, package / name)
    relay = fixture / "conformance" / "fixtures" / "delayed_ready.py"
    relay.parent.mkdir()
    shutil.copyfile(REPO / "conformance" / "fixtures" / "delayed_ready.py", relay)
    manifest = fixture / "docs" / "manifest.json"
    manifest.parent.mkdir()
    outside = temporary / "outside-agent-port"
    outside.mkdir()

    # Fresh imports exercise the module-level constant and defaults bound by its
    # consumers. A helper-only test would miss a consumer keeping its own literal.
    program = """
import json
import os
import runpy
import sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
from harness.constants import AGENT_PORT
from harness.daemon import Daemon
from harness.image import conformance_dockerfile
with patch.dict(os.environ, {"AGENTD_PORT": str(AGENT_PORT)}):
    with patch("http.server.ThreadingHTTPServer") as server:
        runpy.run_path(str(Path(sys.argv[1]) / "fixtures" / "delayed_ready.py"), run_name="__main__")
print(json.dumps({
    "constant": AGENT_PORT,
    "daemon": Daemon.__dataclass_fields__["port"].default,
    "dockerfile": conformance_dockerfile("fixture-base").splitlines(),
    "relay": server.call_args.args[0][1],
}))
"""

    def load() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-I", "-c", program, str(fixture / "conformance")],
            cwd=outside,
            capture_output=True,
            text=True,
            check=False,
        )

    for port in (9001, 1, 65535):
        manifest.write_text(
            json.dumps({"data": {"clientDefaults": {"agentPort": port}}})
        )
        process = load()
        observed = json.loads(process.stdout) if process.returncode == 0 else {}
        results.check(
            f"conformance follows changed shared agent port {port}",
            process.returncode == 0
            and observed["constant"] == port
            and observed["daemon"] == port
            and observed["relay"] == port
            and f"ENV AGENTD_PORT={port}" in observed["dockerfile"]
            and f"EXPOSE {port}" in observed["dockerfile"],
            process.stderr,
        )

    malformed = {
        "missing data": {},
        "missing clientDefaults": {"data": {}},
        "missing agentPort": {"data": {"clientDefaults": {}}},
        "non-object root": [],
        "non-object defaults": {"data": {"clientDefaults": []}},
    }
    for label, value in (
        ("null", None),
        ("boolean", True),
        ("false", False),
        ("string", "9001"),
        ("fractional", 9001.5),
        ("integral float", 9001.0),
        ("zero", 0),
        ("negative", -1),
        ("out-of-range", 65536),
    ):
        malformed[label] = {"data": {"clientDefaults": {"agentPort": value}}}
    inputs = [(label, json.dumps(value).encode()) for label, value in malformed.items()]
    inputs.extend(
        [("corrupt JSON", b"{"), ("invalid UTF-8", b"\xff"), ("missing manifest", None)]
    )
    for label, contents in inputs:
        if contents is None:
            manifest.unlink()
        else:
            manifest.write_bytes(contents)
        process = load()
        results.check(
            f"conformance rejects {label}",
            process.returncode != 0
            and "expected data.clientDefaults.agentPort to be an integer in 1..65535"
            in process.stderr,
            process.stdout,
        )

    manifest.mkdir()
    process = load()
    results.check(
        "conformance rejects unreadable manifest",
        process.returncode != 0
        and "expected data.clientDefaults.agentPort to be an integer in 1..65535"
        in process.stderr,
        process.stdout,
    )
