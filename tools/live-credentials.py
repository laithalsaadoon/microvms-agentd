#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Run the complete hosted suite with refreshable, private web-identity tokens.

The SDKs reread the token file when their STS credentials expire. Their native
web-identity providers omit DurationSeconds, so STS uses its one-hour default,
below the observed two-hour role maximum. No duration setting here changes that.
The setup and cleanup workflow steps obtain their own two-hour sessions.
"""

from __future__ import annotations

import argparse
import base64
import json
import multiprocessing
import os
import runpy
import select
import signal
import subprocess
import sys
import tempfile
import time
from ctypes import c_int, c_ubyte
from pathlib import Path
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parent.parent
# Keep URL validation, response bounds and redirect refusal in one implementation.
OIDC = runpy.run_path(str(ROOT / "tools/recovery-credentials.py"))
REFRESH_SECONDS = 60
FETCH_SECONDS = 20
MIN_TOKEN_SECONDS = 120
RUN_SECONDS = 235 * 60
STOP_SECONDS = 4 * 60
POLL_SECONDS = 0.1
TOKEN_CODEC = "ascii"
COMMIT = b"1"


class Refused(Exception):
    """Only fixed text, never provider responses, crosses the wrapper boundary."""


def require(condition):
    if not condition:
        raise Refused


def token_worker(environ, buffer, length, reader, writer):
    """Publish only a complete token in private memory; failures print nothing."""
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT, signal.SIGTERM})
    os.close(reader)
    try:
        for attempt in range(2):
            try:
                token = OIDC["oidc_token"](environ).encode(TOKEN_CODEC)
                require(0 < len(token) <= len(buffer))
                buffer[: len(token)] = token
                length.value = len(token)
                os.write(writer, COMMIT)
                return
            except HTTPError as error:
                require(error.code in {429, 500, 502, 503, 504})
            except (URLError, TimeoutError, ConnectionError):
                pass
            if attempt == 0:
                time.sleep(0.2)
    except BaseException:  # noqa: BLE001 - no provider traceback may expose the request
        pass
    finally:
        os.close(writer)


def fetch_token(environ, timeout=None):
    # urllib's timeout bounds inactivity, not the whole response. A separate process
    # makes a slow response killable, including while the parent handles cancellation.
    context = multiprocessing.get_context("fork")
    buffer = context.RawArray(c_ubyte, 16384)
    length = context.RawValue(c_int)
    reader, writer = os.pipe()
    worker = context.Process(
        target=token_worker, args=(environ, buffer, length, reader, writer), daemon=True
    )
    deadline = time.monotonic() + (FETCH_SECONDS if timeout is None else timeout)
    try:
        previous = signal.pthread_sigmask(
            signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM}
        )
        try:
            worker.start()
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous)
        os.close(writer)
        writer = None
        remaining = deadline - time.monotonic()
        require(remaining > 0 and bool(select.select([reader], [], [], remaining)[0]))
        # A pipe's readable header does not guarantee a complete message. The
        # sole byte here commits the bounded buffer after every byte was copied.
        # Unlike a shared Event, no worker-held lock can delay this read.
        require(os.read(reader, len(COMMIT)) == COMMIT)
        require(0 < length.value <= len(buffer))
        return bytes(buffer[: length.value]).decode(TOKEN_CODEC)
    finally:
        previous = signal.pthread_sigmask(
            signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM}
        )
        try:
            for descriptor in (writer, reader):
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass  # interruption between closing and clearing writer
            if worker.pid is not None:
                worker.terminate()
                worker.join(1)
                if worker.is_alive():
                    worker.kill()
                worker.join()
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def token_expiration(token):
    # This is a freshness check, not JWT authentication: STS verifies the signature,
    # audience and role trust. Reject bool and fractional expiration claims too.
    parts = token.split(".")
    require(len(parts) == 3)
    payload = json.loads(base64.urlsafe_b64decode(parts[1] + "==="))
    expiration = payload.get("exp")
    require(type(expiration) is int and expiration - time.time() >= MIN_TOKEN_SECONDS)
    return expiration


def private_file(path, content):
    # Replace rather than truncate: a concurrent SDK reader sees one complete JWT.
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def child_environment(environ, directory):
    region = OIDC["safe_value"](
        environ["AWS_REGION"], r"[a-z]{2}-[a-z]+-[1-9][0-9]*", 32
    )
    role = OIDC["safe_value"](
        environ["LIVE_ROLE_ARN"],
        r"arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9_+=,.@/-]+",
        2048,
    )
    return {
        key: value
        for key, value in environ.items()
        if not key.startswith(("AWS_", "ACTIONS_ID_TOKEN_REQUEST_"))
        and key != "LIVE_ROLE_ARN"
    } | {
        "AWS_REGION": region,
        "AWS_DEFAULT_REGION": region,
        "AWS_ROLE_ARN": role,
        "AWS_ROLE_SESSION_NAME": "live-conformance",
        "AWS_WEB_IDENTITY_TOKEN_FILE": str(directory / "token"),
        "AWS_CONFIG_FILE": str(directory / "config"),
        "AWS_SHARED_CREDENTIALS_FILE": str(directory / "credentials"),
        "BOTO_CONFIG": str(directory / "credentials"),
        "AWS_EC2_METADATA_DISABLED": "true",
        "PYTHONUNBUFFERED": "1",
    }


def stop_group(child, refresh=None):
    # A leader can exit while a CLI grandchild survives. Signal the group regardless
    # of the leader's return code. Cleanup may itself need new STS credentials,
    # so keep publishing fresh tokens throughout the bounded graceful interval.
    previous = {
        sig: signal.signal(sig, signal.SIG_IGN)
        for sig in (signal.SIGINT, signal.SIGTERM)
    }
    healthy = True
    try:
        try:
            os.killpg(child.pid, signal.SIGINT)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + STOP_SECONDS
        while time.monotonic() < deadline:
            child.poll()
            try:
                os.killpg(child.pid, 0)
            except ProcessLookupError:
                break
            if refresh is not None:
                try:
                    refresh(deadline)
                except Exception:  # noqa: BLE001 - refresh failure forces termination
                    healthy = False
                    break
            time.sleep(POLL_SECONDS)
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return healthy


def supervise(command, environ, directory):
    child_env = child_environment(environ, directory)
    for name in ("config", "credentials"):
        private_file(directory / name, b"")
    token = fetch_token(environ)
    expiration = token_expiration(token)
    private_file(directory / "token", token.encode())
    del token
    deadline = time.monotonic() + RUN_SECONDS
    refresh_at = time.monotonic() + REFRESH_SECONDS

    def refresh(until):
        nonlocal expiration, refresh_at
        now = time.monotonic()
        require(time.time() < expiration)
        if now >= refresh_at and until - now > 1:
            token = fetch_token(environ, min(FETCH_SECONDS, until - now - 1))
            expiration = token_expiration(token)
            private_file(directory / "token", token.encode())
            refresh_at = time.monotonic() + REFRESH_SECONDS

    child = None
    try:
        # Own the PID before delivering pending cancellation. This process has no
        # threads; the child restores the original signal mask before exec.
        previous = signal.pthread_sigmask(
            signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM}
        )
        try:
            child = subprocess.Popen(
                command,
                env=child_env,
                start_new_session=True,
                preexec_fn=lambda: signal.pthread_sigmask(signal.SIG_SETMASK, previous),
            )
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous)
        while child.poll() is None:
            now = time.monotonic()
            require(now < deadline)
            refresh(deadline)
            time.sleep(POLL_SECONDS)
        require(child.returncode == 0)
    finally:
        if child is not None:
            require(stop_group(child, refresh))


def run(command, environ):
    require(
        environ.get("GITHUB_ACTIONS") == "true"
        and environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
        and environ.get("RUNNER_OS") == "Linux"
    )
    temporary = Path(environ["RUNNER_TEMP"])
    require(temporary.is_absolute() and temporary.is_dir())
    temporary = temporary.resolve()
    require(temporary != ROOT and ROOT not in temporary.parents)
    with tempfile.TemporaryDirectory(dir=temporary) as name:
        directory = Path(name)
        require(directory.stat().st_mode & 0o777 == 0o700)
        supervise(command, environ, directory)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__
    )  # pragma: no mutate (help text is not the credential boundary)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    previous = {
        sig: signal.signal(sig, interrupted) for sig in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        require(bool(args.command))
        run(args.command, dict(os.environ))
    except (Exception, KeyboardInterrupt):  # noqa: BLE001 - provider data is never public
        print("live credentials: failed", file=sys.stderr)
        return 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
