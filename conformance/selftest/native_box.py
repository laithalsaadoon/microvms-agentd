# SPDX-License-Identifier: Apache-2.0
"""Negative twins for the native Box receipt and live verdicts."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from unittest.mock import patch

from harness.results import Results
from lanes.native_box import (
    BINARIES,
    EXAMPLE,
    SCENARIOS,
    NativeCleanupResults,
    native_box_checks,
    native_build_ok,
)


def check_native_box(results: Results, temporary: Path) -> None:
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        NativeCleanupResults(Results(), "native Box").check(
            "cleaned", True, "private-resource-id"
        )
    results.check(
        "native Box cleanup omits resource identifiers from logs",
        "private-resource-id" not in captured.getvalue(),
    )
    check_region_staging(results, temporary)
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
    check_build_receipt(results, directory, receipt)
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


def check_build_receipt(results: Results, directory: Path, expected: dict) -> None:
    script = (EXAMPLE / "build-box.sh").read_text()
    receipt_code = script.split("<<'PY_RECEIPT'\n", 1)[1].split("\nPY_RECEIPT", 1)[0]
    versions = {
        ("git", "rev-parse", "HEAD"): expected["sourceRevision"],
        ("rustc", "--version"): expected["rustc"],
        ("cargo-zigbuild", "--version"): expected["cargoZigbuild"],
        ("zig", "version"): expected["zig"],
    }

    def tool_version(args, **kwargs):
        if args not in versions:
            raise subprocess.CalledProcessError(2, args)
        return versions[args] + "\n"

    # Execute the receipt generator with the pinned tools' command contracts.
    # cargo injects a `zigbuild` subcommand, whose flags omit --version.
    try:
        with (
            patch.object(sys, "argv", ["receipt", str(directory)]),
            patch.object(subprocess, "check_output", tool_version),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            exec(compile(receipt_code, str(EXAMPLE / "build-box.sh"), "exec"), {})
        actual = json.loads((directory / "build.json").read_text())
        valid = all(
            actual[key] == expected[key]
            for key in ("sourceRevision", "rustc", "cargoZigbuild", "zig", "sha256")
        )
    except (subprocess.CalledProcessError, KeyError):
        valid = False
    results.check("native Box records supported tool version commands", valid)
    (directory / "build.json").write_text(json.dumps(expected))


def check_region_staging(results: Results, temporary: Path) -> None:
    spec = importlib.util.spec_from_file_location("stage_box", EXAMPLE / "stage_box.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = EXAMPLE / "box"
    before = {
        path.relative_to(source): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file()
    }
    for region in ("us-east-1", "us-west-2"):
        destination = temporary / region
        module.stage_box(region, source, destination)
        gateway = tomllib.loads((destination / "box.toml").read_text())
        model = tomllib.loads((destination / "codex-home/config.toml").read_text())
        policy = (destination / "policy.dw").read_text()
        endpoint = f"bedrock-runtime.{region}.amazonaws.com"
        results.check(
            f"native Box stages the gateway, policy and model for {region}",
            gateway["agent"]["env"]["AWS_REGION"] == region
            and gateway["egress"]["model"]["destinations"] == [endpoint]
            and model["model_providers"]["amazon-bedrock-runtime"]["aws"]["region"]
            == region
            and model["model"] == "global.openai.gpt-6.1-sol"
            and model["model_provider"] == "amazon-bedrock-runtime"
            and policy.count(f'context.input.host == "{endpoint}"') == 2
            and (region == "us-east-1" or "us-east-1" not in policy),
        )
    results.check(
        "native Box staging leaves the source template unchanged",
        all(
            (source / relative).read_bytes() == contents
            for relative, contents in before.items()
        ),
    )
    for region in ("", "eu-west-1", "us-west-2/../other"):
        try:
            module.stage_box(region, source, temporary / "invalid-region")
        except ValueError:
            rejected = True
        else:
            rejected = False
        results.check(
            f"native Box staging rejects unsupported region {region!r} before copying",
            rejected and not (temporary / "invalid-region").exists(),
        )
