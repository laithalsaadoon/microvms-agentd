# SPDX-License-Identifier: Apache-2.0
"""The example's native ARM64 Box build, metadata probes, and policy scenarios (#628)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
from pathlib import Path
from typing import Any

from harness.cli import Cli
from harness.constants import REPO
from harness.results import Results

from lanes.ensure_image import ensure_image_cleanup
from lanes.isolated_image import CleanupResults

EXAMPLE = REPO / "examples/strands-box-codex-sdk"
BINARIES = ("strands-box", "strands-box-sock-alias", "strands-box-contain-trampoline")
SCENARIOS = (
    "release-before-tests",
    "tests-then-release",
    "read-dotenv",
    "host-program",
    "instance-metadata",
    "delete",
)


def native_build_ok(directory: Path) -> bool:
    """The receipt names the pinned source, native host, tools, and exact shipped bytes."""
    try:
        receipt = json.loads((directory / "build.json").read_text())
        pin = re.search(
            r"BOX_COMMIT:-([0-9a-f]{40})", (EXAMPLE / "build-box.sh").read_text()
        )
        return (
            pin is not None
            and receipt.get("sourceRevision") == pin.group(1)
            and receipt.get("hostSystem") == "Linux"
            and receipt.get("hostArchitecture") == "aarch64"
            and receipt.get("target") == "aarch64-unknown-linux-gnu.2.34"
            and all(
                isinstance(receipt.get(k), str) and receipt[k]
                for k in ("rustc", "cargoZigbuild", "zig")
            )
            and all(
                receipt.get("sha256", {}).get(name)
                == hashlib.sha256((directory / name).read_bytes()).hexdigest()
                for name in BINARIES
            )
        )
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def native_box_checks(report: dict[str, Any], results: Results) -> None:
    results.eq("native Box example exits successfully (#628)", report.get("exit"), 0)
    for where in ("outside", "inside"):
        results.eq(
            f"native Box metadata probe passes {where} the box (#628)",
            report.get(f"metadata-{where}"),
            "passed\n",
        )
    scenarios = report.get("scenarios")
    rows = scenarios if isinstance(scenarios, list) else []
    results.eq(
        "native Box runs the complete scenario set (#628)",
        sorted(row.get("scenario", "") for row in rows),
        sorted(SCENARIOS),
    )
    for name in SCENARIOS:
        matches = [row for row in rows if row.get("scenario") == name]
        ok = len(matches) == 1 and matches[0].get("ok") is True
        checks = matches[0].get("checks") if matches else None
        results.check(
            f"native Box policy scenario {name} passes (#628)",
            ok
            and isinstance(checks, list)
            and bool(checks)
            and all(check.get("ok") is True for check in checks),
        )


def drive_native_box(cli: Cli, binary: Path, aws: Any, results: Results) -> None:
    directory = EXAMPLE / ".box-bin"
    if not results.check(
        "Box artifacts prove a native aarch64 Linux build (#628)",
        native_build_ok(directory),
    ):
        return
    # No raw model output or decision logs reach the suite's logs/artifacts. The
    # example's files can contain credentials if its containment regresses.
    prefix = f"microvm-cli-native-box-{secrets.token_hex(4)}"
    cleanup: dict[str, Any] = {}
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        try:
            built = cli.call(
                "build",
                str(binary),
                "--name",
                prefix,
                "--s3-key-prefix",
                prefix,
                "--repair-identity",
                "--dockerfile",
                str(EXAMPLE / "Dockerfile"),
                "--project",
                str(EXAMPLE / "guest"),
                "--region",
                cli.region,
                timeout=50 * 60,
            )
            cleanup["arn"] = built.data["imageIdentifier"]
            env = os.environ.copy()
            env.update(
                MICROVM=str(cli.binary.resolve()),
                AWS_REGION=cli.region,
                BOX_IMAGE=str(cleanup["arn"]),
                BOX_BIN_DIR=str(directory.resolve()),
                BOX_OUT_DIR=str(out),
            )
            run = cli.run_process([str(EXAMPLE / "run.sh")], timeout=25 * 60, env=env)
            report: dict[str, Any] = {"exit": run.returncode}
            for where in ("outside", "inside"):
                marker = out / f"metadata-{where}.ok"
                report[f"metadata-{where}"] = (
                    marker.read_text() if marker.exists() else None
                )
            scenario_file = out / "results.json"
            if scenario_file.exists():
                report["scenarios"] = json.loads(scenario_file.read_text())
            native_box_checks(report, results)
        finally:
            ensure_image_cleanup(
                cleanup,
                prefix,
                os.environ["MICROVM_BUCKET"],
                prefix,
                aws,
                CleanupResults(results, "native Box"),
            )
