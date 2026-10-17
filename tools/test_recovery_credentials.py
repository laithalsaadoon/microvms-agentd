# SPDX-License-Identifier: Apache-2.0
"""Offline credential-boundary tests; no test obtains a real OIDC or AWS token."""

from __future__ import annotations

import argparse
import contextlib
import copy
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location(
    "tools.recovery-credentials", Path(__file__).with_name("recovery-credentials.py")
)
assert SPEC and SPEC.loader
wrapper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wrapper)

ENV = {
    "GITHUB_ACTIONS": "true",
    "RUNNER_ENVIRONMENT": "github-hosted",
    "RUNNER_OS": "Linux",
    "AWS_REGION": "us-east-1",
    "RECOVERY_ROLE_ARN": "arn:aws:iam::123456789012:role/Existing-Live",
    "ACTIONS_ID_TOKEN_REQUEST_URL": "https://run.actions.githubusercontent.com/token?x=1",
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "Bearer.sentinel",
    "GH_TOKEN": "github-secret-sentinel",
    "AWS_PROFILE": "ambient-profile",
    "AWS_ACCESS_KEY_ID": "ambient-key",
    "AWS_SECRET_ACCESS_KEY": "ambient-secret",
    "AWS_DEFAULT_REGION": "other-region",
}
RECEIPT = {"kind": "live-recovery", "conformance_verified": False, "status": "passed"}


def credentials():
    return {
        "AccessKeyId": "FAKEACCESSKEY",
        "SecretAccessKey": "FAKEsecret/+=",
        "SessionToken": "FAKEsession/+=",
        "Expiration": datetime.now(timezone.utc) + timedelta(hours=1),
    }


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.args = argparse.Namespace(
            source_run="123",
            manifest_sha256="a" * 64,
            report=Path(self.temp.name) / "recovery.json",
            exclusive_names=False,
            observe_only=False,
        )
        self.globals = vars(wrapper)
        self.clock = Mock()
        self.clock.monotonic.return_value = 100.0
        patcher = patch.dict(self.globals, time=self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def child(self, mode, args, environ):
        args.report.write_text(json.dumps(RECEIPT))

    def test_value_bounds_preserve_only_accepted_characters(self):
        self.assertIsNone(wrapper.require(True))
        with self.assertRaises(wrapper.Refused):
            wrapper.require(False)
        self.assertEqual(wrapper.safe_value("Ab/=+9", r"[A-Za-z0-9/+=]+", 6), "Ab/=+9")
        for value in (None, 1, "", "Ab/=+9x", "line\n", "é", "-"):
            with self.subTest(value=value), self.assertRaises(wrapper.Refused):
                wrapper.safe_value(value, r"[A-Za-z0-9/+=]+", 6)

    def test_provenance_precedes_oidc_sts_and_apply_with_process_local_credentials(
        self,
    ):
        events = []

        def child(mode, args, environ):
            events.append((mode, copy.deepcopy(environ)))
            self.child(mode, args, environ)

        def token(environ):
            self.assertEqual(environ, ENV)
            events.append(("oidc", None))
            self.assertFalse(self.args.report.exists())
            return "oidc.token"

        def assume(region, role, token):
            events.append(("sts", (region, role, token)))
            return credentials()

        with patch.dict(
            self.globals, run_child=child, oidc_token=token, assume_role=assume
        ):
            wrapper.recover(self.args, ENV)
        self.assertEqual([e[0] for e in events], ["preflight", "oidc", "sts", "apply"])
        preflight, applied = events[0][1], events[-1][1]
        self.assertNotIn("RECOVERY_ROLE_ARN", preflight)
        for env in (preflight, applied):
            self.assertNotIn("AWS_PROFILE", env)
            self.assertNotIn("ACTIONS_ID_TOKEN_REQUEST_TOKEN", env)
            self.assertNotIn("ACTIONS_ID_TOKEN_REQUEST_URL", env)
            self.assertEqual(env["AWS_REGION"], "us-east-1")
            self.assertEqual(env["AWS_DEFAULT_REGION"], "us-east-1")
            self.assertEqual(env["AWS_EC2_METADATA_DISABLED"], "true")
            self.assertEqual(env["GH_TOKEN"], ENV["GH_TOKEN"])
            self.assertEqual(env["RECOVERY_DEADLINE_MONOTONIC"], "1090.0")
        self.assertNotIn("AWS_ACCESS_KEY_ID", preflight)
        self.assertEqual(applied["AWS_ACCESS_KEY_ID"], "FAKEACCESSKEY")
        self.assertEqual(applied["AWS_SECRET_ACCESS_KEY"], "FAKEsecret/+=")
        self.assertEqual(applied["AWS_SESSION_TOKEN"], "FAKEsession/+=")
        self.assertEqual(
            events[2][1], ("us-east-1", ENV["RECOVERY_ROLE_ARN"], "oidc.token")
        )
        self.assertEqual(ENV["AWS_ACCESS_KEY_ID"], "ambient-key")

    def test_provenance_or_hosted_failure_prevents_token_and_sts(self):
        token, assume = Mock(), Mock()
        child = Mock(side_effect=self.child)
        with patch.dict(
            self.globals, oidc_token=token, assume_role=assume, run_child=child
        ):
            for key in ("GITHUB_ACTIONS", "RUNNER_ENVIRONMENT", "RUNNER_OS"):
                with self.subTest(key=key), self.assertRaises(wrapper.Refused):
                    wrapper.recover(self.args, ENV | {key: "invalid"})
                child.assert_not_called()
            with patch.dict(
                self.globals,
                run_child=Mock(side_effect=wrapper.Refused()),
            ):
                with self.assertRaises(wrapper.Refused):
                    wrapper.recover(self.args, ENV)
        token.assert_not_called()
        assume.assert_not_called()

    def test_invalid_role_region_expiration_or_credential_never_reaches_apply(self):
        for key, value in (
            ("RECOVERY_ROLE_ARN", "role\ninjected"),
            ("AWS_REGION", "us-east-1/other"),
        ):
            child = Mock(side_effect=self.child)
            with (
                patch.dict(self.globals, run_child=child),
                self.assertRaises(wrapper.Refused),
            ):
                wrapper.recover(self.args, ENV | {key: value})
            self.assertEqual(child.call_count, 1)
        bad = [
            {"Expiration": datetime.now(timezone.utc) + timedelta(minutes=10)},
            {"Expiration": datetime.now()},
            {"Expiration": "later"},
        ]
        for field in ("AccessKeyId", "SecretAccessKey", "SessionToken"):
            bad.extend(
                {field: value} for value in ("", "value\nsecret", "x" * 16385, 3)
            )
        for override in bad:
            child = Mock(side_effect=self.child)
            with (
                self.subTest(override=override),
                patch.dict(
                    self.globals,
                    run_child=child,
                    oidc_token=lambda env: "token",
                    assume_role=lambda *args: credentials() | override,
                ),
                self.assertRaises(wrapper.Refused),
            ):
                wrapper.recover(self.args, ENV)
            self.assertEqual(child.call_count, 1)

    def test_oidc_request_is_bounded_scoped_and_cannot_redirect(self):
        response = Mock()
        response.read.return_value = b'{"value":"jwt.token.value"}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        build = Mock(return_value=opener)
        with patch.dict(vars(wrapper), build_opener=build):
            self.assertEqual(wrapper.oidc_token(ENV), "jwt.token.value")
        self.assertIsInstance(build.call_args.args[0], wrapper.NoRedirect)
        request = opener.open.call_args.args[0]
        self.assertEqual(
            request.full_url,
            ENV["ACTIONS_ID_TOKEN_REQUEST_URL"] + "&audience=sts.amazonaws.com",
        )
        self.assertEqual(request.get_header("Authorization"), "Bearer Bearer.sentinel")
        self.assertEqual(opener.open.call_args.kwargs, {"timeout": 10})
        response.read.assert_called_once_with(65537)
        with self.assertRaises(wrapper.Refused):
            wrapper.NoRedirect().redirect_request(
                None, None, 302, None, None, "https://evil.example"
            )

    def test_oidc_rejects_untrusted_urls_oversize_and_malformed_tokens(self):
        for url in (
            "http://run.actions.githubusercontent.com/token",
            "https://evil.example/token",
            "https://actions.githubusercontent.com.evil.example/token",
            "https://user@run.actions.githubusercontent.com/token",
            "https://run.actions.githubusercontent.com:443/token",
            "https://run.actions.githubusercontent.com/token#fragment",
        ):
            with (
                self.subTest(url=url),
                patch.dict(
                    vars(wrapper),
                    build_opener=Mock(side_effect=AssertionError("network forbidden")),
                ),
                self.assertRaises(wrapper.Refused),
            ):
                wrapper.oidc_token(ENV | {"ACTIONS_ID_TOKEN_REQUEST_URL": url})
        for raw in (
            b"x" * 65537,
            b'{"value":"line\\nsecret"}',
            b'{"value":false}',
            b'{"value":""}',
        ):
            response = Mock()
            response.read.return_value = raw
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            opener = Mock()
            opener.open.return_value = response
            with (
                patch.dict(
                    vars(wrapper),
                    build_opener=Mock(return_value=opener),
                ),
                self.assertRaises(wrapper.Refused),
            ):
                wrapper.oidc_token(ENV)

    def test_oidc_replaces_audience_and_accepts_exact_response_limit(self):
        value = "Token.UPPERlower"
        raw = json.dumps({"value": value}).encode()
        raw += b" " * (65536 - len(raw))
        response = Mock()
        response.read.return_value = raw
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        with patch.dict(vars(wrapper), build_opener=Mock(return_value=opener)):
            self.assertEqual(
                wrapper.oidc_token(
                    ENV
                    | {
                        "ACTIONS_ID_TOKEN_REQUEST_URL": ENV[
                            "ACTIONS_ID_TOKEN_REQUEST_URL"
                        ]
                        + "&audience=wrong"
                    }
                ),
                value,
            )
            url = opener.open.call_args.args[0].full_url
            self.assertEqual(url.count("audience="), 1)
            self.assertNotIn("wrong", url)
            for key in ("ACTIONS_ID_TOKEN_REQUEST_TOKEN",):
                with self.assertRaises(wrapper.Refused):
                    wrapper.oidc_token(ENV | {key: "t" * 16385})
            response.read.return_value = json.dumps({"value": "t" * 16385}).encode()
            with self.assertRaises(wrapper.Refused):
                wrapper.oidc_token(ENV)

    def test_sts_is_unsigned_single_attempt_and_uses_exact_existing_role(self):
        import botocore.session
        from botocore import UNSIGNED

        session = Mock()
        client = session.create_client.return_value
        expected = credentials()
        client.assume_role_with_web_identity.return_value = {
            "Credentials": expected,
            "AssumedRoleUser": "never-forward",
        }
        with patch.object(botocore.session, "get_session", return_value=session):
            self.assertIs(
                wrapper.assume_role(
                    "us-east-1", ENV["RECOVERY_ROLE_ARN"], "oidc.token"
                ),
                expected,
            )
        args = session.create_client.call_args
        self.assertEqual(args.args, ("sts",))
        self.assertEqual(args.kwargs["region_name"], "us-east-1")
        self.assertEqual(
            args.kwargs["endpoint_url"], "https://sts.us-east-1.amazonaws.com"
        )
        config = args.kwargs["config"]
        self.assertEqual(config.signature_version, UNSIGNED)
        self.assertEqual(
            (config.connect_timeout, config.read_timeout, config.retries),
            (5, 5, {"total_max_attempts": 1}),
        )
        client.assume_role_with_web_identity.assert_called_once_with(
            RoleArn=ENV["RECOVERY_ROLE_ARN"],
            RoleSessionName="live-recovery",
            WebIdentityToken="oidc.token",
            DurationSeconds=3600,
        )

    def test_child_captures_every_stream_and_validates_receipt(self):
        self.args.exclusive_names = True
        self.args.report.write_text(json.dumps(RECEIPT))
        run = Mock(
            return_value=subprocess.CompletedProcess(
                [], 0, b"secret-output", b"secret-error"
            )
        )
        with patch.object(subprocess, "run", run):
            wrapper.run_child(
                "apply",
                self.args,
                {"only": "env", "RECOVERY_DEADLINE_MONOTONIC": "790"},
            )
        self.assertEqual(
            run.call_args.args[0][:2],
            [sys.executable, str(wrapper.ROOT / "tools/recover-live.py")],
        )
        self.assertEqual(run.call_args.args[0][-1], "--exclusive-names")
        self.assertEqual(
            run.call_args.args[0][2:-1],
            [
                "apply",
                "--source-run",
                "123",
                "--manifest-sha256",
                "a" * 64,
                "--report",
                str(self.args.report),
            ],
        )
        self.assertEqual(
            run.call_args.kwargs,
            {
                "env": {"only": "env", "RECOVERY_DEADLINE_MONOTONIC": "790"},
                "capture_output": True,
                "timeout": 720,
                "check": False,
            },
        )
        for bad in (
            {},
            [],
            RECEIPT | {"kind": "other"},
            RECEIPT | {"conformance_verified": True},
            RECEIPT | {"conformance_verified": 0},
        ):
            self.args.report.write_text(json.dumps(bad))
            with (
                patch.object(subprocess, "run", run),
                # Mutmut's trampoline owns different globals; assert the core's
                # public refusal reason without reaching into its implementation.
                self.assertRaisesRegex(Exception, "^receipt_schema$"),
            ):
                wrapper.run_child(
                    "apply", self.args, {"RECOVERY_DEADLINE_MONOTONIC": "790"}
                )
        self.args.report.write_text(json.dumps(RECEIPT | {"status": "failed"}))
        with patch.object(subprocess, "run", run), self.assertRaises(wrapper.Refused):
            wrapper.run_child(
                "apply", self.args, {"RECOVERY_DEADLINE_MONOTONIC": "790"}
            )
        self.args.report.write_bytes(b"x" * 65537)
        with (
            patch.object(subprocess, "run", run),
            self.assertRaises(wrapper.Refused),
        ):
            wrapper.run_child(
                "apply", self.args, {"RECOVERY_DEADLINE_MONOTONIC": "790"}
            )
        self.args.report.write_text(json.dumps(RECEIPT))
        run.return_value.returncode = 1
        with (
            patch.object(subprocess, "run", run),
            self.assertRaises(wrapper.Refused),
        ):
            wrapper.run_child(
                "preflight", self.args, {"RECOVERY_DEADLINE_MONOTONIC": "790"}
            )

    def test_main_never_prints_exception_token_identity_or_child_output(self):
        argv = [
            "recovery-credentials.py",
            "--source-run",
            "123",
            "--manifest-sha256",
            "a" * 64,
            "--report",
            str(self.args.report),
        ]
        for error in (
            RuntimeError("secret-token arn:secret provider-body"),
            subprocess.TimeoutExpired(
                "secret-command", 1, output="raw-output", stderr="raw-stderr"
            ),
            KeyboardInterrupt(),
        ):
            out, err = io.StringIO(), io.StringIO()
            with (
                patch("sys.argv", argv),
                patch.dict(vars(wrapper), recover=Mock(side_effect=error)),
                contextlib.redirect_stdout(out),
                contextlib.redirect_stderr(err),
            ):
                self.assertEqual(wrapper.main(), 1)
            self.assertEqual(out.getvalue(), "recovery: failed\n")
            self.assertEqual(err.getvalue(), "")
            self.assertEqual(
                json.loads(self.args.report.read_text()),
                {
                    "kind": "live-recovery",
                    "conformance_verified": False,
                    "status": "failed",
                    "reason": "credential_or_recovery_failure",
                },
            )
        with (
            patch("sys.argv", argv),
            patch.dict(vars(wrapper), recover=Mock()),
            contextlib.redirect_stdout(out := io.StringIO()),
        ):
            self.assertEqual(wrapper.main(), 0)
        self.assertEqual(out.getvalue(), "recovery: passed\n")

    def test_partial_apply_receipt_survives_only_after_schema_validation(self):
        partial = RECEIPT | {
            "status": "failed",
            "reason": "deadline",
            "resources": [
                {"kind": "image", "sha256": "a" * 64, "status": "deletion_requested"}
            ],
        }
        self.args.report.write_text(json.dumps(partial))
        run = Mock(
            return_value=subprocess.CompletedProcess(
                [], 1, b"secret stdout", b"secret stderr"
            )
        )
        with (
            patch.object(subprocess, "run", run),
            self.assertRaises(wrapper.Refused),
        ):
            wrapper.run_child(
                "apply", self.args, {"RECOVERY_DEADLINE_MONOTONIC": "790"}
            )
        argv = [
            "recovery-credentials.py",
            "--source-run",
            "123",
            "--manifest-sha256",
            "a" * 64,
            "--report",
            str(self.args.report),
        ]

        child_receipt = partial

        def apply_failure(args, environ):
            args.report.write_text(json.dumps(child_receipt))
            wrapper.run_child(
                "apply", args, environ | {"RECOVERY_DEADLINE_MONOTONIC": "790"}
            )

        with (
            patch("sys.argv", argv),
            patch.object(subprocess, "run", run),
            patch.dict(vars(wrapper), recover=apply_failure),
            contextlib.redirect_stdout(out := io.StringIO()),
        ):
            self.assertEqual(wrapper.main(), 1)
        self.assertEqual(json.loads(self.args.report.read_text()), partial)
        self.assertEqual(out.getvalue(), "recovery: failed\n")
        child_receipt = partial | {"raw": "secret"}
        with (
            patch("sys.argv", argv),
            patch.object(subprocess, "run", run),
            patch.dict(vars(wrapper), recover=apply_failure),
            contextlib.redirect_stdout(out),
        ):
            self.assertEqual(wrapper.main(), 1)
        self.assertNotIn("secret", self.args.report.read_text())
        self.assertNotIn("raw", json.loads(self.args.report.read_text()))

    def test_child_preflight_has_no_exclusive_flag_and_receipt_boundary_is_inclusive(
        self,
    ):
        raw = json.dumps(RECEIPT).encode()
        self.args.report.write_bytes(raw + b" " * (65536 - len(raw)))
        run = Mock(return_value=subprocess.CompletedProcess([], 0))
        for mode, exclusive in (
            ("preflight", True),
            ("preflight", False),
            ("apply", False),
        ):
            self.args.exclusive_names = exclusive
            with patch.object(subprocess, "run", run):
                wrapper.run_child(
                    mode, self.args, {"RECOVERY_DEADLINE_MONOTONIC": "790"}
                )
            self.assertNotIn("--exclusive-names", run.call_args.args[0])

    def test_one_deadline_is_not_renewed_and_reserves_time_for_the_child_receipt(self):
        def child(mode, args, environ):
            self.assertEqual(environ["RECOVERY_DEADLINE_MONOTONIC"], "1090.0")
            self.child(mode, args, environ)
            if mode == "preflight":
                self.clock.monotonic.return_value = 1071.0

        token = Mock()
        with (
            patch.dict(vars(wrapper), run_child=child, oidc_token=token),
            self.assertRaises(wrapper.Refused),
        ):
            wrapper.recover(self.args, ENV | {"RECOVERY_DEADLINE_MONOTONIC": "9999999"})
        token.assert_not_called()
        run = Mock()
        for deadline in ("70", "69", "nan", "inf", "-inf"):
            self.clock.monotonic.return_value = 100.0
            with (
                patch.object(subprocess, "run", run),
                self.assertRaises(wrapper.Refused),
            ):
                wrapper.run_child(
                    "apply", self.args, {"RECOVERY_DEADLINE_MONOTONIC": deadline}
                )
        run.assert_not_called()

    def test_grace_period_retains_partial_receipt_after_child_deadline(self):
        partial = RECEIPT | {
            "status": "failed",
            "reason": "deadline",
            "resources": [
                {"kind": "image", "sha256": "a" * 64, "status": "deletion_requested"}
            ],
        }

        def done(command, *, timeout, **kwargs):
            self.assertEqual(timeout, 720)
            self.clock.monotonic.return_value = 791
            self.args.report.write_text(json.dumps(partial))
            return subprocess.CompletedProcess(command, 1, b"raw output", b"raw error")

        with (
            patch.object(subprocess, "run", side_effect=done),
            self.assertRaises(wrapper.Refused),
        ):
            wrapper.run_child(
                "apply", self.args, {"RECOVERY_DEADLINE_MONOTONIC": "790"}
            )
        self.assertEqual(wrapper.failure_receipt(self.args.report), partial)

    def test_positive_remaining_budget_and_credential_boundary(self):
        self.args.report.write_text(json.dumps(RECEIPT))
        run = Mock(return_value=subprocess.CompletedProcess([], 0))
        with patch.object(subprocess, "run", run):
            wrapper.run_child(
                "preflight", self.args, {"RECOVERY_DEADLINE_MONOTONIC": "70.5"}
            )
        self.assertEqual(run.call_args.kwargs["timeout"], 0.5)
        for remaining, passes in ((20, False), (20.5, True)):
            self.clock.monotonic.return_value = 100.0

            def child(mode, args, environ):
                self.child(mode, args, environ)
                if mode == "preflight":
                    self.clock.monotonic.return_value = 1090 - remaining

            token = Mock(return_value="token")
            with patch.dict(
                vars(wrapper),
                run_child=child,
                oidc_token=token,
                assume_role=lambda *args: credentials(),
            ):
                if passes:
                    wrapper.recover(self.args, ENV)
                    token.assert_called_once_with(ENV)
                else:
                    with self.assertRaises(wrapper.Refused):
                        wrapper.recover(self.args, ENV)
                    token.assert_not_called()

    def test_region_role_and_credential_lifetime_boundaries(self):
        now = datetime.now(timezone.utc)

        class FixedDate(datetime):
            @classmethod
            def now(cls, tz):
                return now

        role_prefix = "arn:aws:iam::123456789012:role/"
        for field, value, passes in (
            ("AWS_REGION", "aa-" + "b" * 27 + "-1", True),
            ("AWS_REGION", "aa-" + "b" * 28 + "-1", False),
            ("RECOVERY_ROLE_ARN", role_prefix + "A" * (2048 - len(role_prefix)), True),
            ("RECOVERY_ROLE_ARN", role_prefix + "A" * (2049 - len(role_prefix)), False),
        ):
            child = Mock(side_effect=self.child)
            with patch.dict(
                vars(wrapper),
                run_child=child,
                oidc_token=lambda env: "token",
                assume_role=lambda *args: credentials(),
            ):
                if passes:
                    wrapper.recover(self.args, ENV | {field: value})
                    self.assertEqual(child.call_count, 2)
                else:
                    with self.assertRaises(wrapper.Refused):
                        wrapper.recover(self.args, ENV | {field: value})
        for seconds, passes in ((1200, False), (1201, True), (1230, True)):
            expiry = FixedDate.fromtimestamp(now.timestamp() + seconds, timezone.utc)
            with patch.dict(
                vars(wrapper),
                datetime=FixedDate,
                run_child=self.child,
                oidc_token=lambda env: "token",
                assume_role=lambda *args: credentials() | {"Expiration": expiry},
            ):
                if passes:
                    wrapper.recover(self.args, ENV)
                else:
                    with self.assertRaises(wrapper.Refused):
                        wrapper.recover(self.args, ENV)

    def test_command_requires_inputs_and_passes_exact_args_and_environment(self):
        base = [
            "recovery-credentials.py",
            "--source-run",
            "123",
            "--manifest-sha256",
            "a" * 64,
            "--report",
            str(self.args.report),
        ]
        for index in (1, 3, 5):
            argv = base[:index] + base[index + 2 :]
            with (
                patch("sys.argv", argv),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as stopped,
            ):
                wrapper.main()
            self.assertEqual(stopped.exception.code, 2)
        for exclusive in (False, True):
            captured = Mock()
            argv = base + (["--exclusive-names"] if exclusive else [])
            with (
                patch("sys.argv", argv),
                patch.dict(vars(wrapper), recover=captured),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(wrapper.main(), 0)
            args, env = captured.call_args.args
            self.assertEqual(
                vars(args),
                vars(self.args) | {"exclusive_names": exclusive, "observe_only": False},
            )
            self.assertEqual(env, dict(wrapper.os.environ))

    def test_interrupt_preserves_valid_current_failure_and_discards_stale_receipts(
        self,
    ):
        partial = RECEIPT | {"status": "failed", "reason": "deadline"}
        base = [
            "recovery-credentials.py",
            "--source-run",
            "123",
            "--manifest-sha256",
            "a" * 64,
            "--report",
            str(self.args.report),
        ]
        for current in (False, True):
            self.args.report.write_text(json.dumps(partial))

            def interrupted(args, env):
                self.assertFalse(args.report.exists())
                if current:
                    args.report.write_text(json.dumps(partial))
                raise KeyboardInterrupt

            with (
                patch("sys.argv", base),
                patch.dict(vars(wrapper), recover=interrupted),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(wrapper.main(), 1)
            report = json.loads(self.args.report.read_text())
            self.assertEqual(
                report["reason"],
                "deadline" if current else "credential_or_recovery_failure",
            )


if __name__ == "__main__":
    unittest.main()
