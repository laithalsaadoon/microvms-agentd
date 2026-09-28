# SPDX-License-Identifier: Apache-2.0
"""The sentinel's stand-in for scripts/check-trace.py: a `TRACED` the untraced collector reads.

Shaped like the real table: a waiver names a module constant, so only running the file reads it.
GATE-4 waives every layer, so it's listed and still untraced.
"""

LAYERS = ("model", "gherkin", "fuzz", "test", "impl", "live")

REASON = "the fixture has no AWS call to check"

TRACED = {
    "GATE-1": "#1",
    "DAEMON-1": ("#2", {"live": REASON}),
    "GATE-4": ("#3", {layer: "nothing checks it" for layer in LAYERS}),
}
