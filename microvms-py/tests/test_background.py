# SPDX-License-Identifier: Apache-2.0
"""Exercise real binding requests and thread scheduling against local HTTP.

This proves binding/wire behavior, not AWS availability or guest process cleanup.
Owned sessions are the transport shape returned by both Session.attach and direct;
sandbox-held sessions deliberately preserve lifecycle locking.
"""

from __future__ import annotations

import contextlib
import http.server
import json
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest

import microvms


@contextlib.contextmanager
def server() -> Iterator[tuple[str, list[dict], threading.Event, threading.Event]]:
    requests: list[dict] = []
    blocked, release = threading.Event(), threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            self.reply({"exec_id": request["exec_id"], "phase": "running"})

        def do_GET(self) -> None:
            if self.path.startswith("/v1/fs/file"):
                blocked.set()
                release.wait(5)
                self.reply(b"evidence")
            elif self.path == "/v1/health":
                self.reply(
                    {
                        "version": "test",
                        "bootstrapped": True,
                        "disk": None,
                        "identity_degraded": False,
                        "identity_repaired": True,
                    }
                )
            else:
                self.reply(
                    {
                        "exec_id": "deadline",
                        "phase": "exited",
                        "exit_code": None,
                        "signal": 9,
                        "stdout": "",
                        "stderr": "",
                        "truncated": False,
                        "writers_may_be_alive": False,
                        "timed_out": True,
                    }
                )

        def reply(self, value: object) -> None:
            body = value if isinstance(value, bytes) else json.dumps(value).encode()
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
        yield f"http://127.0.0.1:{httpd.server_port}", requests, blocked, release
    finally:
        release.set()
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)


@pytest.mark.parametrize("agent", ["claude-code", "codex"])
@pytest.mark.parametrize("mode", ["agent-default", "unrestricted"])
def test_prompt_options_reach_wire_without_changing_uid(agent: str, mode: str) -> None:
    with server() as (endpoint, requests, _, _):
        session = microvms.Session.direct(endpoint, "token")
        handle = microvms.prompt_agent(
            session,
            microvms.AgentSpec(agent),
            "it's 'quoted'; $(false)",
            permission_mode=mode,
            timeout_sec=17,
            exec_id="stable",
            reap_group_on_exit=True,
        )
        assert handle.exec_id == "stable"
        request = requests[0]
        assert request["user"] == request["group"] == 1000
        assert request["timeout_sec"] == 17
        assert request["reap_group_on_exit"] is True
        command = request["command"][0]
        assert "'it'\\''s " in command
        if mode == "unrestricted":
            assert "--dangerously-" in command
            assert "workspace-write" not in command and "--allowedTools" not in command
        elif agent == "codex":
            assert "-s workspace-write" in command
        else:
            assert "--allowedTools Bash,Read,Edit,Write,Grep,Glob" in command


def test_owned_session_transfer_does_not_starve_independent_heartbeat() -> None:
    with server() as (endpoint, _, blocked, release):
        transfer = microvms.Session.direct(endpoint, "token")
        heartbeat = microvms.Session.direct(endpoint, "token")
        with ThreadPoolExecutor(max_workers=2) as pool:
            download = pool.submit(transfer.download_file, "/evidence/report.json")
            assert blocked.wait(2), (
                "transfer must be blocked before the heartbeat starts"
            )
            try:
                health = pool.submit(heartbeat.health).result(timeout=2)
                assert health.bootstrapped
                assert not download.done(), (
                    "heartbeat completed while transfer remained blocked"
                )
            finally:
                release.set()
            assert download.result(timeout=2) == b"evidence"


def test_execution_deadline_remains_distinct_in_raw_result() -> None:
    with server() as (endpoint, _, _, _):
        result = microvms.Session.direct(endpoint, "token").exec("deadline").poll()
        assert result.timed_out and result.signal == 9 and not result.ok
