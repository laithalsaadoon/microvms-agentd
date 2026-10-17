# SPDX-License-Identifier: Apache-2.0
"""The values every lane shares: the service, the region, the daemon's port, the suite
VM's size, the suspend window, and the repository root."""

from __future__ import annotations

import json
import os
from pathlib import Path

#: The repository root: every `cargo` call and `tools/` path the lanes use is
#: relative to it, wherever the suite is run from.
REPO = Path(__file__).resolve().parents[2]


def _agent_port_from_manifest(manifest: Path) -> int:
    message = (
        f"{manifest}: expected data.clientDefaults.agentPort "
        "to be an integer in 1..65535"
    )
    try:
        port = json.loads(manifest.read_text(encoding="utf-8"))["data"][
            "clientDefaults"
        ]["agentPort"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError(message) from exc
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError(message)
    return port


SERVICE = "lambda-microvms"
REGION = os.environ.get("AWS_REGION", "us-east-1")
# The checked-in manifest is generated from core's defaults. Reading it keeps the
# suite and its offline self-tests independent of a built CLI, without a fallback
# that could silently diverge from the daemon's port.
AGENT_PORT = _agent_port_from_manifest(REPO / "docs" / "manifest.json")
BASELINE_MEMORY_MIB = 1024


# Long enough for a frozen guest and a running one to be distinguishable: a live
# ticker adds roughly forty entries across this window. The oracle used the same 40s.
SUSPEND_WINDOW_SEC = 40
