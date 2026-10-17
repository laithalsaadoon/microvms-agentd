#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3>=1.40"]
# ///
# SPDX-License-Identifier: Apache-2.0
"""Run hosted recovery with process-local credentials and captured provider output."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import runpy
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = Path(__file__).resolve().parent.parent
DURATION_SECONDS = 3600
MAX_RESPONSE = 65536
REPORT_GRACE_SECONDS = 30
AUTHORIZATION_HEADER = "Authorization"
VALIDATE_RECEIPT = runpy.run_path(str(ROOT / "tools/recover-live.py"))[
    "validate_receipt"
]
OBSERVATION_CORE = runpy.run_path(str(ROOT / "tools/recover-live.py"))
VALIDATE_OBSERVATION = OBSERVATION_CORE["validate_observation"]


class Refused(Exception):
    """Only fixed reason codes cross the captured credential boundary."""


def require(ok):
    if not ok:
        raise Refused


def safe_value(value, pattern, maximum):
    require(
        isinstance(value, str)
        and len(value) <= maximum
        and re.fullmatch(pattern, value) is not None
    )
    return value


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # The bearer token must never follow a redirect to another authority.
        raise Refused


def oidc_token(environ):
    url = urlsplit(environ["ACTIONS_ID_TOKEN_REQUEST_URL"])
    require(
        url.scheme == "https"
        and url.hostname is not None
        and url.hostname.endswith(".actions.githubusercontent.com")
        and url.username is None
        and url.password is None
        and url.port is None
        and not url.fragment
    )
    query = [(k, v) for k, v in parse_qsl(url.query) if k != "audience"]
    query.append(("audience", "sts.amazonaws.com"))
    bearer = safe_value(
        environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"], r"[A-Za-z0-9_.-]+", 16384
    )
    request = Request(
        urlunsplit(url._replace(query=urlencode(query))),
        headers={AUTHORIZATION_HEADER: "Bearer " + bearer},
    )
    with build_opener(NoRedirect()).open(request, timeout=10) as response:
        raw = response.read(MAX_RESPONSE + 1)
    require(len(raw) <= MAX_RESPONSE)
    return safe_value(json.loads(raw)["value"], r"[A-Za-z0-9_.-]+", 16384)


def assume_role(region, role, token):
    # UNSIGNED prevents ambient credentials or metadata lookup before web identity.
    # The regional endpoint, timeouts and single attempt bound this one STS request.
    import botocore.session
    from botocore import UNSIGNED
    from botocore.config import Config

    client = botocore.session.get_session().create_client(
        "sts",
        region_name=region,
        endpoint_url=f"https://sts.{region}.amazonaws.com",
        config=Config(
            signature_version=UNSIGNED,
            connect_timeout=5,
            read_timeout=5,
            retries={"total_max_attempts": 1},
        ),
    )
    return client.assume_role_with_web_identity(
        RoleArn=role,
        RoleSessionName="live-recovery",
        WebIdentityToken=token,
        DurationSeconds=DURATION_SECONDS,
    )["Credentials"]


def child_environment(environ):
    # Drop ambient credential sources; only the minted session reaches apply.
    return {
        key: value
        for key, value in environ.items()
        if not key.startswith(("AWS_", "ACTIONS_ID_TOKEN_REQUEST_"))
    } | {
        "AWS_REGION": environ["AWS_REGION"],
        "AWS_DEFAULT_REGION": environ["AWS_REGION"],
        "AWS_EC2_METADATA_DISABLED": "true",
    }


def run_child(mode, args, environ):
    command = [
        sys.executable,
        str(ROOT / "tools/recover-live.py"),
        mode,
        "--source-run",
        args.source_run,
        "--manifest-sha256",
        args.manifest_sha256,
        "--report",
        str(args.report),
    ]
    if mode == "apply" and args.exclusive_names:
        command.append("--exclusive-names")
    remaining = (
        float(environ["RECOVERY_DEADLINE_MONOTONIC"])
        + REPORT_GRACE_SECONDS
        - time.monotonic()
    )
    require(math.isfinite(remaining) and remaining > 0)
    done = subprocess.run(
        command, env=environ, capture_output=True, timeout=remaining, check=False
    )
    # Neither stdout nor stderr is forwarded, even on errors or cancellation.
    report = read_receipt(args.report, mode == "observe")
    require(
        done.returncode == 0
        and report["status"] == ("completed" if mode == "observe" else "passed")
    )


def read_receipt(path, observe_only=False):
    maximum = (
        OBSERVATION_CORE["OBSERVATION_MAX_BYTES"] if observe_only else MAX_RESPONSE
    )
    require(path.stat().st_size <= maximum)
    if observe_only:
        return VALIDATE_OBSERVATION(json.loads(path.read_bytes()))
    return VALIDATE_RECEIPT(json.loads(path.read_bytes()))


def failure_receipt(path, observe_only=False):
    try:
        report = read_receipt(path, observe_only)
        if observe_only and report["status"] == "incomplete":
            return report
        if report["status"] == "failed":
            return report
    except Exception:  # noqa: BLE001 - invalid or missing child receipt is never forwarded
        pass
    if observe_only:
        return {
            "kind": "live-recovery-observation",
            "schemaVersion": 1,
            "conformance_verified": False,
            "recoveryAccepted": False,
            "mutationsPerformed": False,
            "status": "incomplete",
        }
    return {
        "kind": "live-recovery",
        "conformance_verified": False,
        "status": "failed",
        "reason": "credential_or_recovery_failure",
    }


def recover(args, environ):
    deadline = time.monotonic() + 1020
    require(
        environ.get("GITHUB_ACTIONS") == "true"
        and environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
        and environ.get("RUNNER_OS") == "Linux"
    )
    child = child_environment(environ)
    # Let the child finish its failure receipt before the parent enforces the budget.
    child["RECOVERY_DEADLINE_MONOTONIC"] = str(deadline - REPORT_GRACE_SECONDS)
    # Revalidate provenance in this job before requesting even an OIDC token.
    preflight = {k: v for k, v in child.items() if k != "RECOVERY_ROLE_ARN"}
    run_child("preflight", args, preflight)
    args.report.unlink()
    region = safe_value(environ["AWS_REGION"], r"[a-z]{2}-[a-z]+-[1-9][0-9]*", 32)
    role = safe_value(
        environ["RECOVERY_ROLE_ARN"],
        r"arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9_+=,.@/-]+",
        2048,
    )
    require(deadline - REPORT_GRACE_SECONDS - time.monotonic() > 20)
    credentials = assume_role(region, role, oidc_token(environ))
    expiration = credentials["Expiration"]
    require(
        isinstance(expiration, datetime)
        and expiration.tzinfo is not None
        and expiration > datetime.now(timezone.utc) + timedelta(minutes=20)
    )
    for source, target in (
        ("AccessKeyId", "AWS_ACCESS_KEY_ID"),
        ("SecretAccessKey", "AWS_SECRET_ACCESS_KEY"),
        ("SessionToken", "AWS_SESSION_TOKEN"),
    ):
        child[target] = safe_value(credentials[source], r"[A-Za-z0-9/+=]+", 16384)
    run_child(
        "observe" if getattr(args, "observe_only", False) else "apply", args, child
    )


def main():
    parser = argparse.ArgumentParser(
        description=__doc__
    )  # pragma: no mutate (help wording does not change the credential boundary)
    parser.add_argument("--source-run", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--exclusive-names", action="store_true")
    parser.add_argument("--observe-only", action="store_true")
    args = parser.parse_args()

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        args.report.unlink(missing_ok=True)
        recover(args, dict(os.environ))
    except (Exception, KeyboardInterrupt):  # noqa: BLE001 - provider text is never public
        report = failure_receipt(args.report, args.observe_only)
        args.report.write_text(json.dumps(report) + "\n")
        if args.observe_only:
            print("observation: incomplete")
        else:
            print("recovery: failed")
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)
    print("observation: completed" if args.observe_only else "recovery: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
