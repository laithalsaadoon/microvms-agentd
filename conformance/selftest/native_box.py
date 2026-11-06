# SPDX-License-Identifier: Apache-2.0
"""Negative twins for the native Box receipt and live verdicts."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import importlib.util
import io
import json
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
    box_release_ok,
    native_box_checks,
    pinned_release,
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
    pin = pinned_release()
    assert pin
    version, archive_sha256 = pin
    receipt = {
        "version": version,
        "archive": f"box-{version}-linux-arm64.tar.gz",
        "archiveSha256": archive_sha256,
        "sha256": {
            name: hashlib.sha256(name.encode()).hexdigest() for name in BINARIES
        },
    }

    def write(value: dict) -> None:
        (directory / "release.json").write_text(json.dumps(value))

    write(receipt)
    results.check(
        "native Box accepts the pinned release and matching binary digests",
        box_release_ok(directory),
    )
    for field in receipt:
        broken = copy.deepcopy(receipt)
        broken.pop(field)
        write(broken)
        results.check(
            f"native Box rejects missing {field}", not box_release_ok(directory)
        )
    for field, value in (
        ("version", "v0.0.0"),
        ("archive", f"box-{version}-aarch64-apple-darwin.tar.gz"),
        ("archiveSha256", "0" * 64),
    ):
        write({**receipt, field: value})
        results.check(
            f"native Box rejects wrong {field}", not box_release_ok(directory)
        )
    write(receipt)
    check_release_receipt(results, directory, receipt)
    for name in BINARIES:
        (directory / name).write_bytes(b"changed")
        results.check(
            f"native Box rejects changed {name}", not box_release_ok(directory)
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


def check_release_receipt(results: Results, directory: Path, expected: dict) -> None:
    """download-box.sh's own receipt code writes what box_release_ok accepts."""
    script = (EXAMPLE / "download-box.sh").read_text()
    receipt_code = script.split("<<'PY_RECEIPT'\n", 1)[1].split("\nPY_RECEIPT", 1)[0]
    argv = [
        "receipt",
        str(directory),
        expected["version"],
        expected["archive"],
        expected["archiveSha256"],
    ]
    try:
        with (
            patch.object(sys, "argv", argv),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            exec(compile(receipt_code, str(EXAMPLE / "download-box.sh"), "exec"), {})
        actual = json.loads((directory / "release.json").read_text())
        valid = actual == expected and box_release_ok(directory)
    except (KeyError, OSError, ValueError):
        valid = False
    results.check("native Box records the release it downloaded", valid)
    (directory / "release.json").write_text(json.dumps(expected))


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
            and policy.count(f'context.input.host == "{endpoint}"') == 3
            and policy.count(f'input.host: "{endpoint}"') == 1
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
