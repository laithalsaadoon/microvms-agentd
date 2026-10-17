# SPDX-License-Identifier: Apache-2.0
"""Observation credentials and artifacts cannot attest successful cleanup."""

import argparse
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "tools.recovery-credentials", Path(__file__).with_name("recovery-credentials.py")
)
wrapper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wrapper)
REFUSED = (
    wrapper.Refused,
    wrapper.OBSERVATION_CORE["Refused"],
    wrapper.VALIDATE_RECEIPT.__globals__["Refused"],
)
ENV = {
    "GITHUB_ACTIONS": "true",
    "RUNNER_ENVIRONMENT": "github-hosted",
    "RUNNER_OS": "Linux",
    "AWS_REGION": "us-east-1",
    "RECOVERY_ROLE_ARN": "arn:aws:iam::000000000000:role/synthetic-role",
}


def observation(status="completed"):
    return {
        "kind": "live-recovery-observation",
        "schemaVersion": 1,
        "conformance_verified": False,
        "recoveryAccepted": False,
        "mutationsPerformed": False,
        "status": status,
        "sourceRun": 9,
        "sourceSha": "a" * 40,
        "region": "us-east-1",
        "manifestSha256": "b" * 64,
        "parserVersion": wrapper.OBSERVATION_CORE["PARSER_VERSION"],
        "sourceLogRepresentation": wrapper.OBSERVATION_CORE["LOG_REPRESENTATION"],
        "sections": {
            key: "completed" if status == "completed" else "not_run"
            for key in (
                "independentVerifier",
                "images",
                "microvms",
                "logGroups",
                "versions",
            )
        },
        "images": [],
        "microvms": [],
        "logGroups": [],
        "versions": [],
        "versionCoverage": [],
        "errors": [],
        **(
            {
                "independentVerifier": {
                    "resources": [],
                    "summary_present": True,
                    "leakCount": 0,
                    "exitCode": 0,
                }
            }
            if status == "completed"
            else {}
        ),
    }


def arguments(tmp_path, *, observe=True):
    args = argparse.Namespace(
        source_run="123",
        manifest_sha256="a" * 64,
        report=tmp_path / "result.json",
        exclusive_names=True,
        observe_only=observe,
    )
    return args


@pytest.mark.parametrize(
    "observe,mode", [(True, "observe"), (False, "apply"), (None, "apply")]
)
def test_credential_mode_routes_after_unchanged_preflight(
    tmp_path, monkeypatch, observe, mode
):
    args = arguments(tmp_path, observe=observe)
    events = []

    def child(actual_mode, actual_args, environ):
        assert actual_args is args
        events.append(actual_mode)
        args.report.write_text("{}")
        if actual_mode == "preflight":
            assert not any(
                key.startswith("AWS_")
                and key
                not in {"AWS_REGION", "AWS_DEFAULT_REGION", "AWS_EC2_METADATA_DISABLED"}
                for key in environ
            )
        else:
            assert environ["AWS_ACCESS_KEY_ID"] == "SYNTHETICACCESS"

    def token(environ):
        events.append("oidc")
        assert not args.report.exists()
        return "synthetic.oidc"

    def assume(*args):
        events.append("sts")
        return {
            "AccessKeyId": "SYNTHETICACCESS",
            "SecretAccessKey": "syntheticsecret",
            "SessionToken": "syntheticsession",
            "Expiration": datetime.now(timezone.utc) + timedelta(hours=1),
        }

    monkeypatch.setattr(wrapper, "run_child", child)
    monkeypatch.setattr(wrapper, "oidc_token", token)
    monkeypatch.setattr(wrapper, "assume_role", assume)
    wrapper.recover(args, ENV)
    assert events == ["preflight", "oidc", "sts", mode]


def test_observation_child_uses_distinct_validator_without_exclusive_flag(
    tmp_path, monkeypatch
):
    args = arguments(tmp_path)
    value = observation()
    args.report.write_text(json.dumps(value))
    run = Mock(
        return_value=subprocess.CompletedProcess(
            [], 0, b"provider-secret", b"provider-secret"
        )
    )
    monkeypatch.setattr(wrapper.subprocess, "run", run)
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        wrapper.run_child(
            "observe",
            args,
            {"RECOVERY_DEADLINE_MONOTONIC": str(wrapper.time.monotonic() + 20)},
        )
    command = run.call_args.args[0]
    assert command[2] == "observe"
    assert "--exclusive-names" not in command
    assert run.call_args.kwargs["capture_output"] is True
    assert output.getvalue() == ""
    args.report.write_text(
        json.dumps(
            {"kind": "live-recovery", "status": "passed", "conformance_verified": False}
        )
    )
    with pytest.raises(REFUSED):
        wrapper.run_child(
            "observe",
            args,
            {"RECOVERY_DEADLINE_MONOTONIC": str(wrapper.time.monotonic() + 20)},
        )


def test_wrapper_schemas_reject_cross_kind_artifacts_and_keep_incomplete_observation(
    tmp_path,
):
    path = tmp_path / "receipt.json"
    partial = observation("incomplete")
    path.write_text(json.dumps(partial))
    assert wrapper.read_receipt(path, True) == partial
    assert wrapper.failure_receipt(path, True) == partial
    with pytest.raises(REFUSED):
        wrapper.read_receipt(path, False)
    path.write_text(
        json.dumps(
            {
                "kind": "live-recovery",
                "status": "failed",
                "conformance_verified": False,
                "reason": "api_failed",
            }
        )
    )
    with pytest.raises(REFUSED):
        wrapper.read_receipt(path, True)
    fallback = wrapper.failure_receipt(path, True)
    assert fallback == {
        "kind": "live-recovery-observation",
        "schemaVersion": 1,
        "conformance_verified": False,
        "recoveryAccepted": False,
        "mutationsPerformed": False,
        "status": "incomplete",
    }


def test_default_receipt_reader_preserves_legacy_recovery_schema(tmp_path):
    path = tmp_path / "receipt.json"
    value = {
        "kind": "live-recovery",
        "conformance_verified": False,
        "status": "failed",
        "reason": "api_failed",
    }
    path.write_text(json.dumps(value))
    assert wrapper.read_receipt(path) == value


def test_observation_reader_uses_observation_size_bound(tmp_path):
    path = tmp_path / "observation.json"
    value = observation()
    value["logGroups"] = [{"sha256": f"{index:064x}"} for index in range(900)]
    raw = json.dumps(value)
    assert (
        wrapper.MAX_RESPONSE
        < len(raw)
        < wrapper.OBSERVATION_CORE["OBSERVATION_MAX_BYTES"]
    )
    path.write_text(raw)
    assert wrapper.read_receipt(path, observe_only=True) == value


def test_observation_failure_never_preserves_completed_receipt(tmp_path):
    path = tmp_path / "observation.json"
    path.write_text(json.dumps(observation()))
    assert wrapper.failure_receipt(path, observe_only=True) == {
        "kind": "live-recovery-observation",
        "schemaVersion": 1,
        "conformance_verified": False,
        "recoveryAccepted": False,
        "mutationsPerformed": False,
        "status": "incomplete",
    }


def test_observation_cli_success_uses_distinct_fixed_summary(tmp_path, monkeypatch):
    args = arguments(tmp_path)

    def completed(actual, environ):
        assert actual.observe_only is True
        actual.report.write_text(json.dumps(observation()))

    monkeypatch.setattr(wrapper, "recover", completed)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "recovery-credentials.py",
            "--observe-only",
            "--source-run",
            args.source_run,
            "--manifest-sha256",
            args.manifest_sha256,
            "--report",
            str(args.report),
        ],
    )
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        assert wrapper.main() == 0
    assert output.getvalue() == "observation: completed\n"
    assert json.loads(args.report.read_text()) == observation()


def test_observation_cli_failure_is_distinct_and_never_prints_provider_text(
    tmp_path, monkeypatch
):
    args = arguments(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "recovery-credentials.py",
            "--observe-only",
            "--source-run",
            args.source_run,
            "--manifest-sha256",
            args.manifest_sha256,
            "--report",
            str(args.report),
        ],
    )
    monkeypatch.setattr(
        wrapper, "recover", Mock(side_effect=RuntimeError("synthetic-provider-secret"))
    )
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        assert wrapper.main() == 1
    assert output.getvalue() == "observation: incomplete\n"
    value = json.loads(args.report.read_text())
    assert (
        value["kind"] == "live-recovery-observation" and value["status"] == "incomplete"
    )
    assert value["recoveryAccepted"] is value["mutationsPerformed"] is False


def test_workflow_observation_input_is_recovery_only_and_defaults_off():
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/live-conformance.yml").read_text()
    )
    inputs = workflow.get("on", workflow.get(True))["workflow_dispatch"]["inputs"]
    assert (
        inputs["observe_only"]["type"] == "boolean"
        and inputs["observe_only"]["default"] is False
    )
    guard = next(
        step
        for step in workflow["jobs"]["owner"]["steps"]
        if step.get("name") == "refuse observation outside recovery mode"
    )
    assert guard["if"] == "inputs.observe_only && inputs.mode != 'recover'"
    result = subprocess.run(["bash", "-c", guard["run"]], capture_output=True)
    assert result.returncode != 0


def test_workflow_observation_has_separate_command_and_artifact(tmp_path):
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/live-conformance.yml").read_text()
    )
    job = workflow["jobs"]["recovery"]
    step = next(
        row
        for row in job["steps"]
        if row.get("name") == "observe the reviewed recovery incident"
    )
    assert step["if"] == "inputs.observe_only"
    normal = next(
        row
        for row in job["steps"]
        if row.get("name") == "recover only the reviewed resource identities"
    )
    assert normal["if"] == "!inputs.observe_only"
    assert "${{" not in step["run"]
    uv = tmp_path / "uv"
    uv.write_text(
        "#!/usr/bin/env python3\nimport json,sys\nfrom pathlib import Path\nPath('argv.json').write_text(json.dumps(sys.argv[1:]))\n"
    )
    uv.chmod(0o755)
    source, digest = "123; $(touch injected)", "abc`touch injected`"
    result = subprocess.run(
        ["bash", "-c", step["run"]],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "SOURCE_RUN": source,
            "MANIFEST_SHA": digest,
        },
        capture_output=True,
    )
    assert result.returncode == 0
    assert json.loads((tmp_path / "argv.json").read_text()) == [
        "run",
        "--script",
        "tools/recovery-credentials.py",
        "--observe-only",
        "--source-run",
        source,
        "--manifest-sha256",
        digest,
        "--report",
        "recovery-observation.json",
    ]
    assert not (tmp_path / "injected").exists()
    uploads = [row for row in job["steps"] if "upload-artifact@" in row.get("uses", "")]
    observe_upload = next(
        row for row in uploads if row["with"]["name"] == "recovery-observation"
    )
    assert observe_upload["if"] == "always() && inputs.observe_only"
    assert observe_upload["with"] == {
        "name": "recovery-observation",
        "path": "recovery-observation.json",
        "if-no-files-found": "error",
    }
    normal_upload = next(
        row for row in uploads if row["with"]["name"] == "recovery-report"
    )
    assert normal_upload["if"] == "always() && !inputs.observe_only"
    assert normal_upload["with"]["path"] == "recovery.json"
    assert "live-verified" not in json.dumps(job)
