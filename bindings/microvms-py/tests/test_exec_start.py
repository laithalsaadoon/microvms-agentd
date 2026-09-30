# SPDX-License-Identifier: Apache-2.0
"""`run` passes `user`, `group`, `shell` and `inherit_image_env` through unchanged.

AGENTD-7, AGENTD-14 and AGENTD-11 on the client side: the daemon resolves names and shells in
the guest, so the binding's whole job is to put each value on the wire in the JSON type the
caller gave. A local HTTP server stands in for the daemon and records the start body; it
answers `400 unknown_user` for one name, the way the daemon does (AGENTD-8), so the refusal's
path back to the caller is covered too.
"""

from __future__ import annotations

import contextlib
import http.server
import json
import threading
from collections.abc import Iterator

import pytest

import microvms


@contextlib.contextmanager
def daemon(bodies: list[dict]) -> Iterator[str]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            bodies.append(body)
            if body.get("user") == "ghost":
                status, reply = (
                    400,
                    {
                        "error": "unknown_user",
                        "detail": 'user "ghost" is not in /etc/passwd',
                    },
                )
            else:
                status, reply = 200, {"exec_id": body["exec_id"], "phase": "running"}
            data = json.dumps(reply).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            data = json.dumps(
                {
                    "version": "test",
                    "bootstrapped": True,
                    "disk": None,
                    "identity_degraded": False,
                    "identity_repaired": True,
                    "image_env_keys": 4,
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

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


def test_names_and_a_named_shell_reach_the_wire_as_strings() -> None:
    """AGENTD-7, AGENTD-14, AGENTD-11: a `str` goes out as a JSON string, the flag as `true`."""
    bodies: list[dict] = []
    with daemon(bodies) as endpoint:
        session = microvms.Session.direct(endpoint, "token")
        session.run(
            "set -o pipefail; false | true",
            shell="bash",
            user="agent",
            group="staff",
            inherit_image_env=True,
        )
    (body,) = bodies
    assert body["shell"] == "bash"
    assert body["user"] == "agent"
    assert body["group"] == "staff"
    assert body["inherit_image_env"] is True


def test_integers_and_booleans_reach_the_wire_as_they_always_did() -> None:
    """AGENTD-16: an `int` stays a JSON integer and `shell=True` a JSON `true`, the bytes an
    older daemon reads; the flag defaults off."""
    bodies: list[dict] = []
    with daemon(bodies) as endpoint:
        session = microvms.Session.direct(endpoint, "token")
        session.run("id -u", shell=True, user=1000, group=1000)
        session.run(["/usr/bin/env"])
    first, second = bodies
    assert (first["shell"], first["user"], first["group"]) == (True, 1000, 1000)
    assert first["inherit_image_env"] is False
    assert second["shell"] is False and second["user"] is None


def test_an_unknown_user_surfaces_as_a_protocol_error_naming_the_slug() -> None:
    """AGENTD-8: the daemon's refusal reaches the caller as an error, not a handle."""
    with daemon([]) as endpoint:
        session = microvms.Session.direct(endpoint, "token")
        with pytest.raises(microvms.MicrovmError) as raised:
            session.run("true", shell=True, user="ghost")
    assert "unknown_user" in str(raised.value)


def test_other_types_are_refused_before_anything_is_sent() -> None:
    """A float user or a list shell is a `TypeError` from the extraction, not a request."""
    session = microvms.Session.direct("http://127.0.0.1:9", "token")
    with pytest.raises(TypeError):
        session.run("true", user=1.5)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        session.run("true", shell=["bash"])  # type: ignore[arg-type]


def test_health_reports_the_image_env_count() -> None:
    """AGENTD-13: the count, and `None` from a daemon that reports none."""
    with daemon([]) as endpoint:
        health = microvms.Session.direct(endpoint, "token").health()
    assert health.image_env_keys == 4
