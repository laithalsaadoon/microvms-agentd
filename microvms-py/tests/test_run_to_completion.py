# SPDX-License-Identifier: Apache-2.0
"""`Session.run_to_completion` and `ExecResult.posix_exit_code` / `notes` (BIND-6..10, #222).

The composition itself is `microvms-core`'s (`src/session/complete.rs`), checked there by a
Stateright model, Gherkin scenarios, a fuzz harness, and unit tests. What is asserted here is the
binding's half: the keyword signature, the callback reaching Python with `OutputChunk`s in order,
the new result fields, a raising callback, and the three deadline paths as the binding reaches
them. The server below is a loopback transcription of the five exec routes' shapes, not `agentd`;
see `conftest.py` for why that boundary is stated rather than hidden. The cut-stream fallback is
not repeated here: with the core's default reconnect budget it takes about a minute of real time,
and the core tiers drive it under a paused clock.
"""

from __future__ import annotations

import http.server
import json
import math
import socketserver
import threading
from collections.abc import Callable, Iterator

import pytest

import microvms
from conftest import output_frame

Route = Callable[[str, str], tuple[int, bytes, str]]


def exit_event(
    total: int,
    exit_code: int | None = 0,
    signal: int | None = None,
    timed_out: bool = False,
) -> bytes:
    """The terminal `exit` frame, with `timed_out`."""
    body = json.dumps(
        {
            "exit_code": exit_code,
            "signal": signal,
            "timed_out": timed_out,
            "truncated": False,
            "writers_may_be_alive": False,
            "offset": total,
        }
    )
    return f"event: exit\ndata: {body}\n\n".encode()


def outcome(
    phase: str,
    exit_code: int | None,
    signal: int | None = None,
    stdout: str = "",
    timed_out: bool = False,
    truncated: bool = False,
) -> bytes:
    """A poll or ack body carrying an outcome."""
    return json.dumps(
        {
            "exec_id": "x-rtc",
            "phase": phase,
            "exit_code": exit_code,
            "signal": signal,
            "timed_out": timed_out,
            "stdout": stdout,
            "stderr": "",
            "truncated": truncated,
            "writers_may_be_alive": False,
        }
    ).encode()


RUNNING = json.dumps({"exec_id": "x-rtc", "phase": "running"}).encode()


class ExecServer:
    """A loopback server answering `route(method, path)` for every request, logging each."""

    def __init__(self, route: Route) -> None:
        self.log: list[str] = []
        log = self.log

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def answer(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                if length:
                    self.rfile.read(length)
                path = self.path.split("?")[0]
                log.append(f"{self.command} {path}")
                status, body, content_type = route(self.command, path)
                self.send_response(status)
                self.send_header("content-type", content_type)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()

            def do_GET(self) -> None:
                self.answer()

            def do_POST(self) -> None:
                self.answer()

            def log_message(self, *args: object) -> None:
                """Silent: a passing test should print nothing."""

        self._server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, args=(0.01,), daemon=True
        )
        self._thread.start()

    @property
    def session(self) -> microvms.Session:
        port = self._server.server_address[1]
        return microvms.Session.direct(f"http://127.0.0.1:{port}", "agent-token")

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def exec_server() -> Iterator[Callable[[Route], ExecServer]]:
    built: list[ExecServer] = []

    def factory(route: Route) -> ExecServer:
        server = ExecServer(route)
        built.append(server)
        return server

    yield factory
    for server in built:
        server.close()


def json_ok(body: bytes) -> tuple[int, bytes, str]:
    return 200, body, "application/json"


def streamed(frames: list[bytes], ack: bytes) -> Route:
    """Start, one stream of `frames`, then `ack` for the ack; polls see the same outcome."""

    def route(method: str, path: str) -> tuple[int, bytes, str]:
        if path == "/v1/exec/start":
            return json_ok(RUNNING)
        if path.endswith("/stream"):
            return 200, b"".join(frames), "text/event-stream"
        if path.endswith("/ack"):
            return json_ok(ack)
        return json_ok(ack.replace(b'"acked"', b'"exited"'))

    return route


# -- BIND-8: the streamed path ----------------------------------------------------------------


def test_run_to_completion_streams_chunks_in_order_and_acks_once(exec_server) -> None:
    """BIND-8: every chunk reaches the callback as an `OutputChunk`, and one ack is the result."""
    server = exec_server(
        streamed(
            [output_frame(0, b"ab"), output_frame(2, b"cd"), exit_event(4)],
            outcome("acked", 0, stdout="abcd"),
        )
    )
    chunks: list[microvms.OutputChunk] = []
    result = server.session.run_to_completion(
        ["bash", "-c", "printf abcd"], on_output=chunks.append, exec_id="x-rtc"
    )
    assert [(chunk.offset, chunk.data) for chunk in chunks] == [(0, b"ab"), (2, b"cd")]
    assert all(isinstance(chunk, microvms.OutputChunk) for chunk in chunks)
    assert result.stdout == "abcd"
    assert result.posix_exit_code == 0
    assert result.notes == []
    assert result.synthesized is False
    assert server.log == [
        "POST /v1/exec/start",
        "GET /v1/exec/x-rtc/stream",
        "POST /v1/exec/x-rtc/ack",
    ]


def test_without_a_callback_it_waits_and_acks(exec_server) -> None:
    """BIND-8: no callback, no stream: one wait and one ack."""
    server = exec_server(streamed([], outcome("acked", 3, stdout="x")))
    result = server.session.run_to_completion("true", exec_id="x-rtc")
    assert result.posix_exit_code == 3
    assert not any("stream" in line for line in server.log)
    assert server.log[-1] == "POST /v1/exec/x-rtc/ack"


def test_a_raising_callback_still_acks_then_reraises(exec_server) -> None:
    """BIND-8: a callback's exception stops delivery, the exec is still acked, then it raises."""
    server = exec_server(
        streamed(
            [output_frame(0, b"a"), output_frame(1, b"b"), exit_event(2)],
            outcome("acked", 0, stdout="ab"),
        )
    )
    seen: list[bytes] = []

    def boom(chunk: microvms.OutputChunk) -> None:
        seen.append(chunk.data)
        raise ValueError("the harness callback failed")

    with pytest.raises(ValueError, match="the harness callback failed"):
        server.session.run_to_completion("true", on_output=boom, exec_id="x-rtc")
    assert seen == [b"a"], "delivery must stop at the first exception"
    assert server.log[-1] == "POST /v1/exec/x-rtc/ack", server.log


# -- BIND-6 and BIND-7: the mapping and the notes ---------------------------------------------


def test_a_daemon_deadline_maps_to_124_with_a_note(exec_server) -> None:
    """BIND-6/BIND-7: `timed_out` plus SIGTERM is 124, and the note says the deadline fired."""
    server = exec_server(
        streamed(
            [output_frame(0, b"t"), exit_event(1, None, 15, timed_out=True)],
            outcome("acked", None, 15, stdout="t", timed_out=True),
        )
    )
    result = server.session.run_to_completion(
        "sleep 60", on_output=lambda _: None, timeout_sec=1.0, exec_id="x-rtc"
    )
    assert result.exit_code is None
    assert result.signal == 15
    assert result.posix_exit_code == 124
    assert result.ok is False
    assert any("timeout_sec" in note for note in result.notes), result.notes


def test_a_signal_death_maps_to_128_plus_the_signal(exec_server) -> None:
    """BIND-6: an out-of-memory SIGKILL with no deadline is 137, not a timeout."""
    server = exec_server(streamed([], outcome("acked", None, 9)))
    result = server.session.run_to_completion("oom", exec_id="x-rtc")
    assert result.posix_exit_code == 137
    assert result.notes == []


def test_truncated_output_carries_a_note(exec_server) -> None:
    """BIND-7: the truncation flag becomes a note a harness can append to stderr."""
    server = exec_server(streamed([], outcome("acked", 0, truncated=True)))
    result = server.session.run_to_completion("yes", exec_id="x-rtc")
    assert result.truncated is True
    assert any("output cap" in note for note in result.notes), result.notes


# -- BIND-9 and BIND-10: the client deadline --------------------------------------------------


def deadline_route(kill_ok: bool, dies: bool) -> tuple[Route, list[bool]]:
    """Polls see `running` until a successful kill when `dies`; the kill fails unless `kill_ok`."""
    killed: list[bool] = []

    def route(method: str, path: str) -> tuple[int, bytes, str]:
        if path == "/v1/exec/start":
            return json_ok(RUNNING)
        if path.endswith("/kill"):
            if not kill_ok:
                return (
                    500,
                    json.dumps({"error": "internal", "detail": "sim kill"}).encode(),
                    "application/json",
                )
            killed.append(True)
            return json_ok(json.dumps({"exec_id": "x-rtc", "killed": True}).encode())
        if killed and dies:
            phase = "acked" if path.endswith("/ack") else "exited"
            return json_ok(outcome(phase, None, 15, stdout="partial"))
        return json_ok(RUNNING)

    return route, killed


def test_a_client_deadline_kills_then_acks(exec_server) -> None:
    """BIND-9: past `timeout_sec + client_grace_sec` the group is killed, then acked."""
    route, _ = deadline_route(kill_ok=True, dies=True)
    server = exec_server(route)
    result = server.session.run_to_completion(
        "sleep 60", timeout_sec=0.2, client_grace_sec=0.3, exec_id="x-rtc"
    )
    kill = server.log.index("POST /v1/exec/x-rtc/kill")
    assert server.log[-1] == "POST /v1/exec/x-rtc/ack"
    assert kill < len(server.log) - 1
    assert result.synthesized is False
    assert result.stdout == "partial"
    assert result.posix_exit_code == 124
    assert any("client deadline" in note for note in result.notes), result.notes


def test_a_failed_kill_and_a_failed_ack_synthesize_124(exec_server) -> None:
    """BIND-10: nothing came back after the kill, so the result is synthesized."""
    route, _ = deadline_route(kill_ok=False, dies=False)
    server = exec_server(route)
    result = server.session.run_to_completion(
        "sleep 60", timeout_sec=0.2, client_grace_sec=0.3, exec_id="x-rtc"
    )
    assert "POST /v1/exec/x-rtc/kill" in server.log
    assert result.synthesized is True
    assert result.posix_exit_code == 124
    assert result.phase == "running"
    assert result.stdout == ""
    assert any("synthesized" in note for note in result.notes), result.notes


# -- argument validation ----------------------------------------------------------------------


@pytest.mark.parametrize("bad", [-1.0, math.nan, math.inf])
def test_a_bad_grace_or_timeout_is_refused_before_anything_starts(
    exec_server, bad: float
) -> None:
    """The core's duration rule, reached through both new numbers, before a request is built."""
    server = exec_server(streamed([], outcome("acked", 0)))
    with pytest.raises(microvms.InvalidArgError):
        server.session.run_to_completion("true", client_grace_sec=bad)
    with pytest.raises(microvms.InvalidArgError):
        server.session.run_to_completion("true", timeout_sec=bad)
    assert server.log == [], "a refused call started an exec"


def test_the_signature_is_keyword_only_past_the_command() -> None:
    """The issue's shape: everything after `command` is a keyword."""
    session = microvms.Session.direct("http://127.0.0.1:9", "agent-token")
    with pytest.raises(TypeError):
        session.run_to_completion("true", None)  # type: ignore[misc]
