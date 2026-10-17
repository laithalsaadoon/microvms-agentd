# SPDX-License-Identifier: Apache-2.0
"""Negative twins for the native Box receipt and live verdicts."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path

from harness.results import Results
from lanes.native_box import (
    BINARIES,
    EXAMPLE,
    SCENARIOS,
    native_box_checks,
    native_build_ok,
)


def check_native_box(results: Results, temporary: Path) -> None:
    directory = temporary / "native-box"
    directory.mkdir()
    for name in BINARIES:
        (directory / name).write_bytes(name.encode())
    pin = re.search(
        r"BOX_COMMIT:-([0-9a-f]{40})", (EXAMPLE / "build-box.sh").read_text()
    )
    assert pin
    receipt = {
        "sourceRevision": pin.group(1),
        "hostSystem": "Linux",
        "hostArchitecture": "aarch64",
        "target": "aarch64-unknown-linux-gnu.2.34",
        "rustc": "rustc 1.98.1",
        "cargoZigbuild": "cargo-zigbuild 0.23.4",
        "zig": "0.16.0",
        "sha256": {
            name: hashlib.sha256(name.encode()).hexdigest() for name in BINARIES
        },
    }

    def write(value: dict) -> None:
        (directory / "build.json").write_text(json.dumps(value))

    write(receipt)
    results.check(
        "native Box accepts matching provenance and binary digests",
        native_build_ok(directory),
    )
    for field in receipt:
        broken = copy.deepcopy(receipt)
        broken.pop(field)
        write(broken)
        results.check(
            f"native Box rejects missing {field}", not native_build_ok(directory)
        )
    for field, value in (
        ("hostArchitecture", "x86_64"),
        ("hostSystem", "Darwin"),
        ("sourceRevision", "0" * 40),
        ("target", "aarch64-unknown-linux-gnu"),
    ):
        write({**receipt, field: value})
        results.check(
            f"native Box rejects wrong {field}", not native_build_ok(directory)
        )
    write(receipt)
    for name in BINARIES:
        (directory / name).write_bytes(b"changed")
        results.check(
            f"native Box rejects changed {name}", not native_build_ok(directory)
        )
        (directory / name).write_bytes(name.encode())

    report = {
        "exit": 0,
        "metadata-outside": "passed\n",
        "metadata-inside": "passed\n",
        "scenarios": [
            {"scenario": name, "ok": True, "checks": [{"ok": True}]}
            for name in SCENARIOS
        ],
    }
    happy = Results(probe=True)
    native_box_checks(report, happy)
    results.check("native Box accepts complete passing live evidence", not happy.failed)
    for field in report:
        broken = copy.deepcopy(report)
        broken.pop(field)
        probe = Results(probe=True)
        native_box_checks(broken, probe)
        results.check(f"native Box rejects missing {field}", bool(probe.failed))
    for index, name in enumerate(SCENARIOS):
        for field, value in (
            ("ok", False),
            ("checks", []),
            ("checks", [{"ok": False}]),
        ):
            broken = copy.deepcopy(report)
            broken["scenarios"][index][field] = value
            probe = Results(probe=True)
            native_box_checks(broken, probe)
            results.check(
                f"native Box rejects {name} {field}={value!r}", bool(probe.failed)
            )
    broken = copy.deepcopy(report)
    broken["scenarios"].append(copy.deepcopy(report["scenarios"][0]))
    probe = Results(probe=True)
    native_box_checks(broken, probe)
    results.check("native Box rejects duplicate scenarios", bool(probe.failed))
