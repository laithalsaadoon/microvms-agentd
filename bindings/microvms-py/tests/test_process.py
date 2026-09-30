# SPDX-License-Identifier: Apache-2.0
"""`Session.spawn`: one exec as two byte iterators, a `wait()`, and an idempotent `kill()`.

Ported from `bindings/microvms-js/__test__/process.mjs`, because both bindings read core's
`ExecHandle::split`: the routing, the gap attribution and the gap policy are core's, and these
tests assert them through Python's iterators. The core tier (`session/split.rs`) holds the same
properties without a binding in the way.

`spawn` starts an exec, so the server here answers the start route, the stream, a poll and a
kill by path. `conftest.py`'s `SseServer` answers every request from one queue, which a spawn's
concurrent stream and poll would interleave.
"""

from __future__ import annotations

import http.server
import json
import socketserver
import threading
from collections.abc import Iterator, Sequence
from typing import Any

import pytest

import microvms
from conftest import exit_frame, gap_frame, output_frame

# A syntactically valid exec id, in the `x-<16 hex>` shape the client mints.
EXEC_ID = "x-00000000000000ff"


class SpawnServer:
    """A loopback daemon for one spawned exec.

    `scripts` is one list of frames per stream attach, so two lists are a cut and a reconnect.
    An `int` in place of a list answers that attach with that status. `polls` is what each
    `GET /v1/exec/<id>` answers, the last one repeated.
    """

    def __init__(
        self,
        scripts: Sequence[Sequence[bytes] | int],
        polls: Sequence[dict[str, Any]] = (),
    ) -> None:
        self.requested: list[str] = []
        attaches = iter(list(scripts))
        answers = list(polls) or [{"exec_id": EXEC_ID, "phase": "running"}]
        polled = [0]
        seen = self.requested

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                self.answer()

            def do_POST(self) -> None:
                self.answer()

            def reply(self, status: int, content_type: str, body: bytes) -> None:
                self.send_response(status)
                self.send_header("content-type", content_type)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()

            def answer(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                if length:
                    self.rfile.read(length)
                seen.append(f"{self.command} {self.path}")
                if self.path == "/v1/exec/start":
                    body = {"exec_id": EXEC_ID, "phase": "running"}
                    self.reply(200, "application/json", json.dumps(body).encode())
                elif self.path.endswith("/kill"):
                    body = {"exec_id": EXEC_ID, "killed": True}
                    self.reply(200, "application/json", json.dumps(body).encode())
                elif "/stream" in self.path:
                    # A script that ran out answers an empty body, which the core reads as a
                    # cut, so an over-attaching test fails on its own assertion.
                    frames = next(attaches, [])
                    if isinstance(frames, int):
                        self.reply(
                            frames, "application/json", b'{"error":"unauthorized"}'
                        )
                    else:
                        self.reply(200, "text/event-stream", b"".join(frames))
                else:
                    answer = answers[min(polled[0], len(answers) - 1)]
                    polled[0] += 1
                    self.reply(200, "application/json", json.dumps(answer).encode())

            def log_message(self, *args: object) -> None:
                """Silent: a passing test should print nothing."""

        self._server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, args=(0.01,), daemon=True
        )
        self._thread.start()

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def offsets_requested(self) -> list[int]:
        """The `?offset=` each stream attach asked for, in order."""
        return [
            int(path.split("offset=")[1])
            for path in self.requested
            if "/stream" in path
        ]

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def spawn_server() -> Iterator[type[SpawnServer]]:
    """Hands the class back, and closes every server a test built."""
    built: list[SpawnServer] = []

    def factory(
        scripts: Sequence[Sequence[bytes] | int], polls: Sequence[dict[str, Any]] = ()
    ) -> SpawnServer:
        server = SpawnServer(scripts, polls)
        built.append(server)
        return server

    yield factory  # type: ignore[misc]
    for server in built:
        server.close()


def spawn(server: SpawnServer, **options: Any) -> microvms.ExecProcess:
    session = microvms.Session.direct(server.endpoint, "agent-token")
    return session.spawn(["bash", "-lc", "true"], exec_id=EXEC_ID, **options)


def read_both(proc: microvms.ExecProcess) -> tuple[Any, Any]:
    """Each side read to its end on its own thread: the bytes, or the exception that ended it.

    Two threads because each side holds one unread chunk, like a pipe: reading one side to the
    end first would stall once the other filled.
    """
    results: dict[str, Any] = {}

    def read(name: str, side: microvms.ByteStream) -> None:
        chunks: list[bytes] = []
        try:
            for chunk in side:
                chunks.append(chunk)
        except microvms.MicrovmError as error:
            results[name] = (b"".join(chunks), error)
            return
        results[name] = (b"".join(chunks), None)

    threads = [
        threading.Thread(target=read, args=("stdout", proc.stdout)),
        threading.Thread(target=read, args=("stderr", proc.stderr)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive(), "a side never ended"
    return results["stdout"], results["stderr"]


def test_one_interleaved_stream_becomes_two_byte_iterators(
    spawn_server: object,
) -> None:
    """Each side gets its own bytes and none of the other's, in order, and ends cleanly."""
    server = spawn_server(  # type: ignore[operator]
        [
            [
                output_frame(0, b"out-one\n", "stdout"),
                output_frame(8, b"err-one\n", "stderr"),
                output_frame(16, b"out-two\n", "stdout"),
                output_frame(24, b"err-two\n", "stderr"),
                exit_frame(32),
            ]
        ]
    )
    proc = spawn(server)
    out, err = read_both(proc)
    assert out == (b"out-one\nout-two\n", None)
    assert err == (b"err-one\nerr-two\n", None)
    assert all(isinstance(chunk, bytes) for chunk in [out[0], err[0]])
    assert proc.gaps == []


def test_a_cut_rejoins_at_the_byte_cursor_on_both_sides(spawn_server: object) -> None:
    """A body with no `exit` frame is a cut, and the reconnect asks for the next byte."""
    server = spawn_server(  # type: ignore[operator]
        [
            [output_frame(0, b"o1", "stdout"), output_frame(2, b"e1", "stderr")],
            [
                output_frame(4, b"o2", "stdout"),
                output_frame(6, b"e2", "stderr"),
                exit_frame(8),
            ],
        ]
    )
    out, err = read_both(spawn(server))
    assert out == (b"o1o2", None)
    assert err == (b"e1e2", None)
    assert server.offsets_requested() == [0, 4]


def test_a_spawn_starts_reading_at_the_offset_it_is_given(spawn_server: object) -> None:
    server = spawn_server([[output_frame(64, b"tail", "stdout"), exit_frame(68)]])  # type: ignore[operator]
    out, _ = read_both(spawn(server, offset=64))
    assert out == (b"tail", None)
    assert server.offsets_requested() == [64]


def test_a_gap_raises_from_both_iterators_by_default_naming_the_range(
    spawn_server: object,
) -> None:
    """The default policy: an evicted range raises `PlatformError` from both sides.

    Both, because the wire can't say which side lost the bytes. The bytes before the gap are
    still delivered.
    """
    server = spawn_server(  # type: ignore[operator]
        [
            [
                output_frame(0, b"before", "stdout"),
                gap_frame(6, 900),
                output_frame(900, b"after", "stdout"),
                exit_frame(905),
            ]
        ]
    )
    proc = spawn(server)
    out, err = read_both(proc)

    assert out[0] == b"before"
    for name, (_, error) in [("stdout", out), ("stderr", err)]:
        assert isinstance(error, microvms.PlatformError), (
            f"{name} ended cleanly: {error}"
        )
        assert error.wire_kind == "OutputGap", name
        assert error.retryable is False, name
        assert "[6, 900)" in str(error), f"{name}: {error}"
        assert "offset 900" in str(error), f"{name}: {error}"
    assert proc.gaps == []


def test_gap_policy_event_records_the_gap_against_the_next_frames_stream(
    spawn_server: object,
) -> None:
    """Under `"event"`, a gap is recorded and both sides go on.

    The gap takes the stream of the output frame after it, the side whose log has the hole:
    stdout wrote before it and stderr after it, so this one is stderr's.
    """
    server = spawn_server(  # type: ignore[operator]
        [
            [
                output_frame(0, b"before", "stdout"),
                gap_frame(6, 900),
                output_frame(900, b"after", "stderr"),
                exit_frame(905),
            ]
        ]
    )
    proc = spawn(server, gap_policy="event")
    out, err = read_both(proc)

    assert out == (b"before", None)
    assert err == (b"after", None)
    assert [(gap.stream, gap.start, gap.end) for gap in proc.gaps] == [
        ("stderr", 6, 900)
    ]


def test_a_gap_the_stream_ends_on_is_recorded_with_no_stream(
    spawn_server: object,
) -> None:
    server = spawn_server(  # type: ignore[operator]
        [[output_frame(0, b"AA", "stdout"), gap_frame(2, 900), exit_frame(900)]]
    )
    proc = spawn(server, gap_policy="event")
    read_both(proc)
    assert [(gap.stream, gap.start, gap.end) for gap in proc.gaps] == [(None, 2, 900)]


def test_a_refused_reconnect_raises_its_code_from_both_iterators(
    spawn_server: object,
) -> None:
    """A drive error ends both sides with its own class, code and wire kind."""
    server = spawn_server([[output_frame(0, b"AA", "stdout")], 401])  # type: ignore[operator]
    out, err = read_both(spawn(server))

    assert out[0] == b"AA"
    for name, (_, error) in [("stdout", out), ("stderr", err)]:
        assert isinstance(error, microvms.CredentialsError), f"{name}: {error!r}"
        assert error.wire_kind == "Unauthorized", name
        assert "401" in str(error), f"{name}: {error}"


def test_wait_reads_the_daemon_record(spawn_server: object) -> None:
    """`wait()` polls the exec record: the exit code is the daemon's, not the stream's end."""
    server = spawn_server(  # type: ignore[operator]
        [[output_frame(0, b"hi", "stdout"), exit_frame(2)]],
        polls=[
            {"exec_id": EXEC_ID, "phase": "running"},
            {
                "exec_id": EXEC_ID,
                "phase": "exited",
                "exit_code": 3,
                "signal": None,
                "stdout": "",
                "stderr": "",
                "truncated": False,
                "writers_may_be_alive": False,
            },
        ],
    )
    proc = spawn(server)
    assert read_both(proc)[0] == (b"hi", None)
    result = proc.wait(timeout=30)
    assert result.exit_code == 3


def test_kill_is_idempotent(spawn_server: object) -> None:
    """A second `kill()` reaches the daemon and succeeds, so a `finally` needs no guard."""
    server = spawn_server([[output_frame(0, b"x", "stdout"), exit_frame(1)]])  # type: ignore[operator]
    proc = spawn(server)
    proc.kill()
    proc.kill()
    assert sum(1 for path in server.requested if path.endswith("/kill")) == 2


def test_a_process_exposes_its_exec_id_and_the_same_iterators(
    spawn_server: object,
) -> None:
    server = spawn_server([[exit_frame(0)]])  # type: ignore[operator]
    proc = spawn(server)
    assert proc.exec_id == EXEC_ID
    assert proc.stdout is proc.stdout, (
        "two iterators over one side would split its bytes"
    )
    assert proc.stderr is proc.stderr
    assert proc.stdout is not proc.stderr
    read_both(proc)


def test_an_unknown_gap_policy_is_refused_before_anything_starts(
    spawn_server: object,
) -> None:
    server = spawn_server([[exit_frame(0)]])  # type: ignore[operator]
    with pytest.raises(microvms.InvalidArgError, match="gap policy"):
        spawn(server, gap_policy="Error")
    assert server.requested == [], "a refused spawn still started an exec"


def test_a_process_has_no_constructor() -> None:
    """A process with no exec behind it is one whose every method looks like a dead VM."""
    with pytest.raises(TypeError):
        microvms.ExecProcess()  # type: ignore[call-arg]
