# SPDX-License-Identifier: Apache-2.0
"""Offline runtime and credential-boundary checks; tokens here are unsigned fixtures."""

from __future__ import annotations

import base64
import contextlib
import importlib.util
import io
import json
import multiprocessing
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from ctypes import c_int, c_ubyte
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

SPEC = importlib.util.spec_from_file_location(
    "tools.live-credentials", Path(__file__).with_name("live-credentials.py")
)
assert SPEC and SPEC.loader
wrapper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wrapper)


def jwt(expiration=None, **extra):
    payload = {"exp": int(time.time()) + 3600 if expiration is None else expiration}
    payload.update(extra)
    encoded = (
        base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    )
    return f"unsigned.{encoded}.fixture"


def environment(directory):
    return {
        **os.environ,
        "GITHUB_ACTIONS": "true",
        "RUNNER_ENVIRONMENT": "github-hosted",
        "RUNNER_OS": "Linux",
        "RUNNER_TEMP": str(directory),
        "AWS_REGION": "us-east-1",
        "LIVE_ROLE_ARN": "arn:aws:iam::123456789012:role/TestOnly",
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://run.actions.githubusercontent.com/token",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-secret",
    }


class LiveCredentialTests(unittest.TestCase):
    def worker_result(self, provider):
        buffer = multiprocessing.RawArray(c_ubyte, 16384)
        length = multiprocessing.RawValue(c_int)
        reader, writer = os.pipe()
        retained = os.dup(reader)
        try:
            with (
                patch.dict(wrapper.OIDC, oidc_token=provider),
                patch.object(wrapper.signal, "signal"),
                patch.object(wrapper.signal, "pthread_sigmask"),
                patch.object(wrapper.time, "sleep") as sleep,
            ):
                wrapper.token_worker(self.environ, buffer, length, reader, writer)
            return (
                os.read(retained, 1),
                bytes(buffer[: length.value]),
                sleep.call_args_list,
            )
        finally:
            for descriptor in (reader, writer, retained):
                with contextlib.suppress(OSError):
                    os.close(descriptor)

    def successful_fetch(self):
        try:
            return wrapper.fetch_token(self.environ)
        except Exception as error:  # noqa: BLE001 - name the verdict without provider text
            self.fail(f"expected a completed token fetch, got {type(error).__name__}")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.environ = environment(self.directory)

    def test_internal_deadline_and_grace_fit_the_suite_step_ceiling(self):
        self.assertEqual(wrapper.RUN_SECONDS, 235 * 60)
        self.assertEqual(wrapper.STOP_SECONDS, 4 * 60)
        self.assertEqual(wrapper.FETCH_SECONDS, 20)
        self.assertEqual(wrapper.REFRESH_SECONDS, 60)
        self.assertGreater(
            wrapper.MIN_TOKEN_SECONDS, wrapper.REFRESH_SECONDS + wrapper.FETCH_SECONDS
        )
        self.assertLess(
            wrapper.RUN_SECONDS + wrapper.STOP_SECONDS + wrapper.FETCH_SECONDS + 2,
            240 * 60,
        )

    def test_existing_oidc_security_is_reused_without_aws_calls(self):
        self.assertEqual(
            Path(wrapper.OIDC["oidc_token"].__code__.co_filename).name,
            "recovery-credentials.py",
        )
        with patch.dict(wrapper.OIDC, assume_role=Mock(side_effect=AssertionError)):
            with patch.object(wrapper, "supervise") as supervise:
                wrapper.run(["suite"], self.environ)
            supervise.assert_called_once()

    def test_hosted_boundary_precedes_any_fetch_or_process(self):
        with patch.object(wrapper, "supervise") as supervise:
            for key in (
                "GITHUB_ACTIONS",
                "RUNNER_ENVIRONMENT",
                "RUNNER_OS",
                "RUNNER_TEMP",
            ):
                with self.subTest(key=key), self.assertRaises(wrapper.Refused):
                    wrapper.run(["suite"], self.environ | {key: "invalid"})
            supervise.assert_not_called()
            with self.assertRaises(wrapper.Refused):
                wrapper.run(
                    ["suite"], self.environ | {"RUNNER_TEMP": str(wrapper.ROOT)}
                )
            supervise.assert_not_called()

    def test_child_uses_only_web_identity_with_empty_alternate_sources(self):
        polluted = self.environ | {
            "AWS_ACCESS_KEY_ID": "static-key",
            "AWS_SECRET_ACCESS_KEY": "static-secret",
            "AWS_SESSION_TOKEN": "static-token",
            "AWS_SECURITY_TOKEN": "legacy-token",
            "AWS_PROFILE": "profile",
            "AWS_DEFAULT_PROFILE": "profile",
            "AWS_CONFIG_FILE": "ambient",
            "AWS_SHARED_CREDENTIALS_FILE": "ambient",
            "AWS_CONTAINER_CREDENTIALS_FULL_URI": "http://ambient",
            "AWS_CONTAINER_AUTHORIZATION_TOKEN": "container-secret",
            "AWS_ENDPOINT_URL_STS": "http://ambient",
            "BOTO_CONFIG": "ambient",
            "AWS_CREDENTIAL_EXPIRATION": "2099-01-01",
            "KEEP_ME": "value",
        }
        child = wrapper.child_environment(polluted, self.directory)
        self.assertEqual(
            {k: v for k, v in child.items() if k.startswith("AWS_")},
            {
                "AWS_REGION": "us-east-1",
                "AWS_DEFAULT_REGION": "us-east-1",
                "AWS_ROLE_ARN": self.environ["LIVE_ROLE_ARN"],
                "AWS_ROLE_SESSION_NAME": "live-conformance",
                "AWS_WEB_IDENTITY_TOKEN_FILE": str(self.directory / "token"),
                "AWS_CONFIG_FILE": str(self.directory / "config"),
                "AWS_SHARED_CREDENTIALS_FILE": str(self.directory / "credentials"),
                "AWS_EC2_METADATA_DISABLED": "true",
            },
        )
        self.assertEqual(child["BOTO_CONFIG"], str(self.directory / "credentials"))
        self.assertEqual(child["PYTHONUNBUFFERED"], "1")
        self.assertEqual(child["KEEP_ME"], "value")
        self.assertNotIn("LIVE_ROLE_ARN", child)
        self.assertFalse(any(k.startswith("ACTIONS_ID_TOKEN_REQUEST_") for k in child))
        self.assertEqual(polluted["AWS_ACCESS_KEY_ID"], "static-key")

    def test_expiry_requires_exact_integer_and_actual_freshness(self):
        now = 1700000000
        with patch.object(wrapper.time, "time", return_value=now):
            self.assertEqual(wrapper.token_expiration(jwt(now + 120)), now + 120)
            for expiration in (
                True,
                False,
                now + 119,
                now,
                now - 1,
                1.5,
                "later",
                None,
            ):
                token = jwt(now + 1000) if expiration is None else jwt(expiration)
                if expiration is None:
                    payload = base64.urlsafe_b64encode(b"{}").decode().rstrip("=")
                    token = f"x.{payload}.x"
                with (
                    self.subTest(expiration=expiration),
                    self.assertRaises(wrapper.Refused),
                ):
                    wrapper.token_expiration(token)
            for token in ("", "one.two", "one.two.three.four"):
                with self.subTest(token=token), self.assertRaises(wrapper.Refused):
                    wrapper.token_expiration(token)

    def test_token_file_replacement_is_private_complete_and_leaves_no_scratch(self):
        path = self.directory / "token"
        with patch.object(
            wrapper.tempfile, "mkstemp", wraps=tempfile.mkstemp
        ) as mkstemp:
            wrapper.private_file(path, b"first-complete-token")
        mkstemp.assert_called_once_with(dir=self.directory)
        old = path.open("rb")
        self.addCleanup(old.close)
        wrapper.private_file(path, b"second-complete-token")
        self.assertEqual(old.read(), b"first-complete-token")
        self.assertEqual(path.read_bytes(), b"second-complete-token")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.directory.iterdir()), [path])

    def test_sdk_selects_web_identity_and_rereads_rotated_file_without_duration_override(
        self,
    ):
        import botocore.session

        for name in ("config", "credentials"):
            wrapper.private_file(self.directory / name, b"")
        wrapper.private_file(self.directory / "token", b"first.fixture.token")
        child = wrapper.child_environment(self.environ, self.directory)
        client = Mock()
        client.assume_role_with_web_identity.return_value = {
            "Credentials": {
                "AccessKeyId": "fixture-key",
                "SecretAccessKey": "fixture-secret",
                "SessionToken": "fixture-session",
                # Inside the native advisory window: each access must refresh.
                "Expiration": datetime.now(timezone.utc) + timedelta(minutes=5),
            }
        }
        with patch.dict(os.environ, child, clear=True):
            session = botocore.session.get_session()
            with patch.object(session, "create_client", return_value=client):
                credentials = session.get_credentials()
                self.assertEqual(credentials.method, "assume-role-with-web-identity")
                credentials.get_frozen_credentials()
                wrapper.private_file(self.directory / "token", b"second.fixture.token")
                credentials.get_frozen_credentials()
        requests = [
            call.kwargs for call in client.assume_role_with_web_identity.call_args_list
        ]
        self.assertEqual(
            [request["WebIdentityToken"] for request in requests],
            [
                "first.fixture.token",
                "second.fixture.token",
            ],
        )
        for request in requests:
            self.assertEqual(request["RoleArn"], self.environ["LIVE_ROLE_ARN"])
            self.assertNotIn("DurationSeconds", request)

    def test_fetch_success_failure_and_stall_are_private_and_reaped(self):
        token = jwt()
        before = {p.pid for p in multiprocessing.active_children()}
        with patch.dict(wrapper.OIDC, oidc_token=lambda env: token):
            self.assertEqual(self.successful_fetch(), token)
        with patch.dict(
            wrapper.OIDC, oidc_token=Mock(side_effect=ValueError("secret"))
        ):
            with self.assertRaises(wrapper.Refused):
                wrapper.fetch_token(self.environ)
        with patch.dict(wrapper.OIDC, oidc_token=lambda env: time.sleep(3)):
            started = time.monotonic()
            with (
                patch.object(wrapper, "FETCH_SECONDS", 0.05),
                self.assertRaises(wrapper.Refused),
            ):
                wrapper.fetch_token(self.environ)
            self.assertLess(time.monotonic() - started, 2)
        self.assertEqual({p.pid for p in multiprocessing.active_children()}, before)

    def test_worker_retries_only_transient_errors_within_the_fetch_deadline(self):
        token = jwt()
        transient = [
            URLError("secret"),
            TimeoutError("secret"),
            ConnectionResetError("secret"),
        ]
        transient += [
            HTTPError("https://fixture.invalid", code, "secret", None, None)
            for code in (429, 500, 502, 503, 504)
        ]
        for error in transient:
            provider = Mock(side_effect=[error, token])
            committed, result, sleeps = self.worker_result(provider)
            self.assertEqual((committed, result), (b"1", token.encode()))
            self.assertEqual(provider.call_count, 2)
            self.assertEqual([call.args for call in sleeps], [(0.2,)])
            with (
                self.subTest(error=type(error)),
                patch.dict(wrapper.OIDC, oidc_token=Mock(side_effect=[error, token])),
            ):
                self.assertEqual(self.successful_fetch(), token)
        for error in [ValueError("secret"), wrapper.OIDC["Refused"]()] + [
            HTTPError("https://fixture.invalid", code, "secret", None, None)
            for code in (400, 401, 403, 404)
        ]:
            provider = Mock(side_effect=[error, token])
            self.assertEqual(self.worker_result(provider)[:2], (b"", b""))
            self.assertEqual(provider.call_count, 1)
            with (
                self.subTest(error=type(error)),
                patch.dict(wrapper.OIDC, oidc_token=Mock(side_effect=[error, token])),
                self.assertRaises(wrapper.Refused),
            ):
                wrapper.fetch_token(self.environ)
        with (
            patch.dict(
                wrapper.OIDC,
                oidc_token=Mock(side_effect=[URLError("one"), URLError("two"), token]),
            ),
            self.assertRaises(wrapper.Refused),
        ):
            wrapper.fetch_token(self.environ)
        provider = Mock(side_effect=[URLError("one"), URLError("two"), token])
        self.assertEqual(self.worker_result(provider)[:2], (b"", b""))
        self.assertEqual(provider.call_count, 2)

    def test_worker_never_publishes_oversized_or_non_ascii_tokens(self):
        for token in ("", "x" * 16385, "not-ascii-☃"):
            self.assertEqual(
                self.worker_result(Mock(return_value=token))[:2], (b"", b"")
            )
            with (
                self.subTest(length=len(token)),
                patch.dict(wrapper.OIDC, oidc_token=lambda env: token),
                self.assertRaises(wrapper.Refused),
            ):
                wrapper.fetch_token(self.environ)
        for token in ("x", "x" * 16384):
            self.assertEqual(
                self.worker_result(Mock(return_value=token))[:2], (b"1", token.encode())
            )

    def test_fetch_deadline_kills_an_unresponsive_worker(self):
        worker = Mock(pid=42)
        worker.is_alive.return_value = True
        context = Mock()
        context.RawArray.return_value = bytearray(16384)
        context.Process.return_value = worker
        with (
            patch.object(
                wrapper.multiprocessing, "get_context", return_value=context
            ) as get_context,
            patch.object(wrapper.time, "monotonic", return_value=0),
            patch.object(wrapper.os, "pipe", return_value=(11, 12)),
            patch.object(wrapper.os, "close"),
            patch.object(wrapper.select, "select", return_value=([], [], [])) as select,
            self.assertRaises(wrapper.Refused),
        ):
            wrapper.fetch_token(self.environ)
        select.assert_called_once_with([11], [], [], 20)
        worker.terminate.assert_called_once()
        worker.kill.assert_called_once()
        self.assertEqual(worker.join.call_count, 2)
        self.assertEqual([call.args for call in worker.join.call_args_list], [(1,), ()])
        get_context.assert_called_once_with("fork")
        self.assertIs(context.Process.call_args.kwargs["daemon"], True)
        self.assertEqual(context.Process.call_args.kwargs["args"][0], self.environ)

    def test_partial_worker_publication_never_blocks_past_fetch_deadline(self):
        def partial(environ, buffer, length, reader, writer):
            os.close(reader)
            buffer[:6] = b"secret"
            length.value = 6
            time.sleep(3)

        before = {p.pid for p in multiprocessing.active_children()}
        with patch.object(wrapper, "token_worker", partial):
            started = time.monotonic()
            with self.assertRaises(wrapper.Refused):
                wrapper.fetch_token(self.environ, timeout=0.05)
            self.assertLess(time.monotonic() - started, 2)
        self.assertEqual({p.pid for p in multiprocessing.active_children()}, before)

    def test_completion_never_accepts_an_expired_deadline_or_invalid_length(self):
        from types import SimpleNamespace

        for length in (-1, 0, 1, 16384, 16385):
            context = Mock()
            context.RawArray.return_value = bytearray(b"x" * 16384)
            context.RawValue.return_value = SimpleNamespace(value=length)
            context.Process.return_value = Mock(pid=42)
            with (
                self.subTest(length=length),
                patch.object(
                    wrapper.multiprocessing, "get_context", return_value=context
                ),
                patch.object(wrapper.time, "monotonic", return_value=0),
                patch.object(wrapper.os, "pipe", return_value=(11, 12)),
                patch.object(wrapper.os, "close"),
                patch.object(wrapper.os, "read", return_value=b"1"),
                patch.object(wrapper.select, "select", return_value=([11], [], [])),
            ):
                with self.assertRaises(wrapper.Refused):
                    wrapper.fetch_token(self.environ, timeout=0)
                if 0 < length <= 16384:
                    self.assertEqual(wrapper.fetch_token(self.environ), "x" * length)
                else:
                    with self.assertRaises(wrapper.Refused):
                        wrapper.fetch_token(self.environ)

    def test_parent_independently_refuses_non_ascii_shared_bytes(self):
        from types import SimpleNamespace

        context = Mock()
        context.RawArray.return_value = bytearray("é".encode())
        context.RawValue.return_value = SimpleNamespace(value=2)
        context.Process.return_value = Mock(pid=42)
        with (
            patch.object(wrapper.multiprocessing, "get_context", return_value=context),
            patch.object(wrapper.os, "pipe", return_value=(11, 12)),
            patch.object(wrapper.os, "close"),
            patch.object(wrapper.os, "read", return_value=b"1"),
            patch.object(wrapper.select, "select", return_value=([11], [], [])),
            self.assertRaises(UnicodeDecodeError),
        ):
            wrapper.fetch_token(self.environ)

    def test_complete_suite_inherits_streams_and_reads_rotated_token(self):
        report = self.directory / "verdict"
        code = """
import os,pathlib,time
p=pathlib.Path(os.environ['AWS_WEB_IDENTITY_TOKEN_FILE'])
first=p.read_text()
assert p.stat().st_mode & 0o777 == 0o600
assert p.parent.stat().st_mode & 0o777 == 0o700
for key in ('AWS_CONFIG_FILE','AWS_SHARED_CREDENTIALS_FILE','BOTO_CONFIG'):
    assert pathlib.Path(os.environ[key]).read_bytes() == b''
assert 'ACTIONS_ID_TOKEN_REQUEST_TOKEN' not in os.environ
assert 'AWS_ACCESS_KEY_ID' not in os.environ
deadline=time.monotonic()+3
while time.monotonic()<deadline:
    if p.read_text()!=first:
        pathlib.Path(os.environ['VERDICT']).write_text('rotated')
        break
    time.sleep(.01)
else: raise RuntimeError('no rotation')
"""
        with (
            patch.dict(wrapper.OIDC, oidc_token=lambda env: jwt(nonce=time.time_ns())),
            patch.object(wrapper, "REFRESH_SECONDS", 0.05),
            patch.object(wrapper, "RUN_SECONDS", 5),
        ):
            try:
                wrapper.run(
                    [sys.executable, "-c", code],
                    self.environ | {"VERDICT": str(report)},
                )
            except Exception as error:  # noqa: BLE001 - assert the suite verdict
                self.fail(f"complete suite failed: {type(error).__name__}")
        self.assertEqual(report.read_text(), "rotated")
        self.assertFalse(any(path.is_dir() for path in self.directory.iterdir()))

    def test_refresh_failure_stops_running_suite_and_keeps_failure(self):
        child = Mock(returncode=None, pid=123)
        child.poll.return_value = None
        with (
            patch.object(
                wrapper, "fetch_token", side_effect=[jwt(), RuntimeError("secret")]
            ),
            patch.object(wrapper, "REFRESH_SECONDS", 0),
            patch.object(wrapper.subprocess, "Popen", return_value=child) as popen,
            patch.object(wrapper, "stop_group") as stop,
            self.assertRaises(RuntimeError),
        ):
            wrapper.supervise(["suite", "--all"], self.environ, self.directory)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertNotIn("stdout", popen.call_args.kwargs)
        self.assertNotIn("stderr", popen.call_args.kwargs)
        self.assertIs(stop.call_args.args[0], child)

    def test_runtime_expiry_and_child_failure_are_nonzero_and_stop_the_group(self):
        for returncode, budget in ((None, -1), (None, 0), (7, 100)):
            child = Mock(returncode=returncode, pid=123)
            child.poll.return_value = returncode
            with (
                self.subTest(returncode=returncode),
                patch.object(wrapper, "fetch_token", return_value=jwt()),
                patch.object(wrapper, "RUN_SECONDS", budget),
                patch.object(wrapper.time, "monotonic", return_value=100),
                patch.object(wrapper.subprocess, "Popen", return_value=child),
                patch.object(wrapper, "stop_group") as stop,
                patch.object(
                    wrapper.time,
                    "sleep",
                    side_effect=AssertionError("deadline ignored"),
                ),
                self.assertRaises(wrapper.Refused),
            ):
                wrapper.supervise(["suite"], self.environ, self.directory)
            self.assertIs(stop.call_args.args[0], child)

    def test_token_keeps_rotating_during_graceful_cleanup(self):
        report = self.directory / "cleanup"
        code = """
import os,pathlib,signal,time
p=pathlib.Path(os.environ['AWS_WEB_IDENTITY_TOKEN_FILE'])
def cleanup(signum,frame):
    first=p.read_text()
    deadline=time.monotonic()+2
    while time.monotonic()<deadline:
        if p.read_text()!=first:
            pathlib.Path(os.environ['VERDICT']).write_text('refreshed during cleanup')
            raise SystemExit(0)
        time.sleep(.01)
    raise SystemExit(2)
signal.signal(signal.SIGINT,cleanup)
while True: time.sleep(.01)
"""
        with (
            patch.dict(wrapper.OIDC, oidc_token=lambda env: jwt(nonce=time.time_ns())),
            patch.object(wrapper, "REFRESH_SECONDS", 0.05),
            patch.object(wrapper, "RUN_SECONDS", 0.15),
            patch.object(wrapper, "STOP_SECONDS", 2),
            self.assertRaises(wrapper.Refused),
        ):
            wrapper.run(
                [sys.executable, "-c", code], self.environ | {"VERDICT": str(report)}
            )
        self.assertTrue(report.exists(), "cleanup did not observe a refreshed token")
        self.assertEqual(report.read_text(), "refreshed during cleanup")

    def test_pending_signal_after_spawn_still_owns_and_stops_the_child(self):
        child = Mock(returncode=None, pid=123)
        calls = []

        def mask(how, signals):
            calls.append(how)
            if how == signal.SIG_SETMASK:
                raise KeyboardInterrupt
            return set()

        with (
            patch.object(wrapper, "fetch_token", return_value=jwt()),
            patch.object(wrapper.signal, "pthread_sigmask", side_effect=mask),
            patch.object(wrapper.subprocess, "Popen", return_value=child) as popen,
            patch.object(wrapper, "stop_group", return_value=True) as stop,
            self.assertRaises(KeyboardInterrupt),
        ):
            wrapper.supervise(["suite"], self.environ, self.directory)
        self.assertEqual(calls, [signal.SIG_BLOCK, signal.SIG_SETMASK])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertIs(stop.call_args.args[0], child)

    def test_failed_spawn_restores_signal_mask_without_stopping_unowned_process(self):
        with (
            patch.object(wrapper, "fetch_token", return_value=jwt()),
            patch.object(wrapper.signal, "pthread_sigmask", return_value=set()) as mask,
            patch.object(wrapper.subprocess, "Popen", side_effect=OSError("failed")),
            patch.object(wrapper, "stop_group") as stop,
            self.assertRaises(OSError),
        ):
            wrapper.supervise(["suite"], self.environ, self.directory)
        self.assertEqual(
            [c.args[0] for c in mask.call_args_list],
            [signal.SIG_BLOCK, signal.SIG_SETMASK],
        )
        stop.assert_not_called()

    def test_refresh_failure_during_grace_forces_kill_and_reap(self):
        child = Mock(pid=123)
        with (
            patch.object(wrapper.os, "killpg") as kill,
            patch.object(wrapper.time, "monotonic", return_value=0),
        ):
            self.assertIs(
                wrapper.stop_group(child, Mock(side_effect=RuntimeError("secret"))),
                False,
            )
        self.assertEqual(
            [c.args for c in kill.call_args_list],
            [
                (123, signal.SIGINT),
                (123, 0),
                (123, signal.SIGKILL),
            ],
        )
        child.wait.assert_called_once()

    def test_stubborn_process_group_is_killed_and_leader_reaped(self):
        ready = self.directory / "ready"
        code = (
            "import signal,time,pathlib; signal.signal(signal.SIGINT,signal.SIG_IGN); pathlib.Path(%r).touch(); time.sleep(3)"
            % str(ready)
        )
        child = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
        self.addCleanup(lambda: child.kill() if child.poll() is None else None)
        deadline = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(ready.exists())
        with patch.object(wrapper, "STOP_SECONDS", 0.05):
            wrapper.stop_group(child)
        self.assertEqual(child.returncode, -signal.SIGKILL)
        with self.assertRaises(ProcessLookupError):
            os.killpg(child.pid, 0)

    def test_main_catches_cancellation_without_disclosing_values(self):
        for error in (KeyboardInterrupt(), RuntimeError("secret-token-request-url")):
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                self.subTest(error=type(error)),
                patch.object(sys, "argv", ["live-credentials.py", "suite"]),
                patch.object(wrapper, "run", side_effect=error),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                self.assertEqual(wrapper.main(), 1)
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(stderr.getvalue(), "live credentials: failed\n")

    def test_real_signals_stop_group_and_refresh_worker_without_secret_output(self):
        ready = self.directory / "ready"
        runner = """
import inspect,runpy,sys,time
w=runpy.run_path(sys.argv[1],run_name='tools.live-credentials')
g=inspect.unwrap(w['main']).__globals__
g['STOP_SECONDS']=.1
def fetch(env):
    if ready.exists(): time.sleep(3)
    return token
from pathlib import Path
ready=Path(sys.argv[2]); token=sys.argv[3]
g['OIDC']['oidc_token']=fetch
sys.argv=['live-credentials.py',sys.executable,'-c',
    'import pathlib,signal,time; signal.signal(signal.SIGINT,signal.SIG_IGN); pathlib.Path('+repr(str(ready))+').touch(); time.sleep(3)']
g['REFRESH_SECONDS']=.01
raise SystemExit(w['main']())
"""
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                runner,
                str(Path(wrapper.__file__)),
                str(ready),
                jwt(),
            ],
            env=self.environ,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(lambda: child.kill() if child.poll() is None else None)
        deadline = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(ready.exists())
        time.sleep(0.1)  # the refresh worker is now stalled while the suite is alive
        child.send_signal(signal.SIGTERM)
        time.sleep(0.03)
        child.send_signal(signal.SIGINT)
        stdout, stderr = child.communicate(timeout=3)
        self.assertEqual(child.returncode, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "live credentials: failed\n")
        self.assertFalse(any(path.is_dir() for path in self.directory.iterdir()))

    def test_require_and_main_success_preserve_the_exact_command_and_environment(self):
        wrapper.require(True)
        with self.assertRaises(wrapper.Refused):
            wrapper.require(False)
        command = ["suite", "--binary", "agentd", "--microvm-binary", "microvm"]
        with (
            patch.object(sys, "argv", ["live-credentials.py", *command]),
            patch.object(wrapper, "run") as run,
        ):
            self.assertEqual(wrapper.main(), 0)
        run.assert_called_once_with(command, dict(os.environ))
        with (
            patch.object(sys, "argv", ["live-credentials.py"]),
            patch.object(wrapper, "run") as run,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(wrapper.main(), 1)
        run.assert_not_called()

    def test_private_directory_is_created_under_runner_temp_and_removed(self):
        directories = []

        def supervise(command, environ, directory):
            directories.append(directory)
            self.assertEqual(directory.parent, self.directory)
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)

        with patch.object(wrapper, "supervise", side_effect=supervise):
            wrapper.run(["suite"], self.environ)
        self.assertEqual(len(directories), 1)
        self.assertFalse(directories[0].exists())
        for value in ("relative", str(self.directory / "missing")):
            with self.assertRaises(wrapper.Refused):
                wrapper.run(["suite"], self.environ | {"RUNNER_TEMP": value})

    def test_region_and_role_validation_boundaries(self):
        region = "aa-" + "b" * 27 + "-1"
        prefix = "arn:aws:iam::123456789012:role/"
        role = prefix + "r" * (2048 - len(prefix))
        accepted = wrapper.child_environment(
            self.environ | {"AWS_REGION": region, "LIVE_ROLE_ARN": role}, self.directory
        )
        self.assertEqual(accepted["AWS_REGION"], region)
        self.assertEqual(accepted["AWS_ROLE_ARN"], role)
        for key, value in (("AWS_REGION", region + "1"), ("LIVE_ROLE_ARN", role + "r")):
            with self.subTest(key=key), self.assertRaises(wrapper.OIDC["Refused"]):
                wrapper.child_environment(self.environ | {key: value}, self.directory)

    def test_worker_commits_the_exact_token_after_complete_copy(self):
        buffer = multiprocessing.RawArray(c_ubyte, 16384)
        length = multiprocessing.RawValue(c_int)
        reader, writer = os.pipe()
        retained = os.dup(reader)
        provider = Mock(return_value="first.fixture.token")
        try:
            with (
                patch.dict(wrapper.OIDC, oidc_token=provider),
                patch.object(wrapper.signal, "signal") as signals,
                patch.object(wrapper.signal, "pthread_sigmask") as mask,
            ):
                wrapper.token_worker(self.environ, buffer, length, reader, writer)
            provider.assert_called_once_with(self.environ)
            self.assertEqual(os.read(retained, 2), b"1")
            self.assertEqual(bytes(buffer[: length.value]), b"first.fixture.token")
            self.assertEqual(length.value, 19)
            self.assertEqual(
                [call.args for call in signals.call_args_list],
                [(signal.SIGTERM, signal.SIG_DFL), (signal.SIGINT, signal.SIG_IGN)],
            )
            mask.assert_called_once_with(
                signal.SIG_UNBLOCK, {signal.SIGINT, signal.SIGTERM}
            )
        finally:
            for descriptor in (reader, writer, retained):
                with contextlib.suppress(OSError):
                    os.close(descriptor)

    def test_exact_grace_deadline_never_waits_an_extra_poll(self):
        child = Mock(pid=123)

        def kill(pid, signum):
            self.assertNotEqual(
                signum, 0, "deadline must be checked before probing again"
            )

        with (
            patch.object(wrapper.os, "killpg", side_effect=kill) as kill,
            patch.object(wrapper.time, "monotonic", return_value=0),
            patch.object(wrapper, "STOP_SECONDS", 0),
        ):
            self.assertTrue(wrapper.stop_group(child))
        self.assertEqual(
            [call.args for call in kill.call_args_list],
            [(123, signal.SIGINT), (123, signal.SIGKILL)],
        )

    def test_refresh_schedule_and_remaining_grace_bound_each_request(self):
        clock = Mock()
        clock.now = 100
        clock.monotonic.side_effect = lambda: clock.now
        clock.time.side_effect = lambda: clock.now
        clock.sleep.side_effect = AssertionError("an exited child must not be polled")
        child = Mock(returncode=0)
        child.poll.return_value = 0
        tokens = [jwt(500), jwt(600), jwt(700)]
        fetch = Mock(side_effect=tokens)

        def stop(child, refresh):
            clock.now = 159
            refresh(300)
            self.assertEqual(fetch.call_count, 1)
            clock.now = 160
            refresh(170)
            self.assertEqual(fetch.call_args.args, (self.environ, 9))
            clock.now = 161
            refresh(300)
            self.assertEqual(fetch.call_count, 2)
            clock.now = 220
            refresh(240)
            self.assertEqual(fetch.call_args.args, (self.environ, 19))
            clock.now = 699
            refresh(700)
            self.assertEqual(fetch.call_count, 3)
            clock.now = 700
            with self.assertRaises(wrapper.Refused):
                refresh(900)
            return True

        with (
            patch.object(wrapper, "time", clock),
            patch.object(wrapper, "fetch_token", fetch),
            patch.object(wrapper.subprocess, "Popen", return_value=child),
            patch.object(wrapper, "stop_group", side_effect=stop),
        ):
            wrapper.supervise(["suite"], self.environ, self.directory)
        self.assertEqual(fetch.call_args_list[0].args, (self.environ,))
        self.assertEqual((self.directory / "token").read_text(), tokens[-1])


if __name__ == "__main__":
    unittest.main()
