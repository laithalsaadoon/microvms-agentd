# SPDX-License-Identifier: Apache-2.0
"""Health exposes the daemon's hook log, handler outcomes, and identity steps."""

from __future__ import annotations

import contextlib
import http.server
import json
import threading
from collections.abc import Iterator

import microvms

BASE = {
    "version": "test",
    "bootstrapped": True,
    "disk": None,
    "identity_degraded": True,
    "identity_repaired": True,
}

CURRENT = BASE | {
    "hooks": [
        {"hook": "run", "fired_at": 10},
        {
            "hook": "suspend",
            "fired_at": 20,
            "handler": {
                "exit_code": None,
                "signal": 9,
                "timed_out": True,
                "duration_ms": 20000,
            },
        },
        {
            "hook": "resume",
            "fired_at": 30,
            "handler": {"exit_code": 0, "duration_ms": 12},
        },
    ],
    "hooks_dropped": 2,
    "identity_steps": [
        {"name": "machine-id", "outcome": "repaired"},
        {"name": "boot-id", "outcome": "failed", "error": "EPERM"},
    ],
}


@contextlib.contextmanager
def daemon(health: dict) -> Iterator[str]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = json.dumps(health).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)


def test_hooks_carry_handler_outcomes() -> None:
    with daemon(CURRENT) as endpoint:
        health = microvms.Session.direct(endpoint, "token").health()
    run, suspend, resume = health.hooks
    assert (run.hook, run.fired_at, run.handler) == ("run", 10, None)
    assert suspend.handler is not None
    assert suspend.handler.timed_out and suspend.handler.signal == 9
    assert suspend.handler.exit_code is None and not suspend.handler.succeeded
    assert resume.handler is not None and resume.handler.succeeded
    assert resume.handler.duration_ms == 12 and resume.handler.error is None
    assert health.hooks_dropped == 2
    assert "suspend" in repr(suspend)


def test_identity_steps_say_which_step_failed() -> None:
    with daemon(CURRENT) as endpoint:
        health = microvms.Session.direct(endpoint, "token").health()
    assert [(s.name, s.outcome, s.error) for s in health.identity_steps] == [
        ("machine-id", "repaired", None),
        ("boot-id", "failed", "EPERM"),
    ]


def test_an_older_daemon_reports_empty_lists() -> None:
    with daemon(BASE) as endpoint:
        health = microvms.Session.direct(endpoint, "token").health()
    assert health.hooks == [] and health.identity_steps == []
    assert health.hooks_dropped == 0
    assert health.image_env_keys is None
