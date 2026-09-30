# SPDX-License-Identifier: Apache-2.0
"""The values every lane shares: the service, the region, the daemon's port, the suite
VM's size, the suspend window, and the repository root."""

from __future__ import annotations

import os
from pathlib import Path

SERVICE = "lambda-microvms"
REGION = os.environ.get("AWS_REGION", "us-east-1")
AGENT_PORT = 9000
BASELINE_MEMORY_MIB = 1024


# Long enough for a frozen guest and a running one to be distinguishable: a live
# ticker adds roughly forty entries across this window. The oracle used the same 40s.
SUSPEND_WINDOW_SEC = 40


#: The repository root: every `cargo` call and `scripts/` path the lanes use is
#: relative to it, wherever the suite is run from.
REPO = Path(__file__).resolve().parents[2]
