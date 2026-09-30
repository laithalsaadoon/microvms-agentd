# SPDX-License-Identifier: Apache-2.0
"""`Session.keep_awake` against a local fake of the daemon's health route."""

from __future__ import annotations

import gc
import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import microvms


class Health:
    """Answers `GET /v1/health` from a `busy` script; the last answer repeats."""

    def __init__(self, busy: list[bool]) -> None:
        self.busy = busy
        self.seen: list[tuple[str, str | None]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                owner.seen.append((self.path, self.headers.get("Authorization")))
                index = min(len(owner.seen), len(owner.busy)) - 1
                body = json.dumps(
                    {
                        "version": "0.1.0",
                        "bootstrapped": True,
                        "disk": None,
                        "identity_degraded": False,
                        "identity_repaired": True,
                        "busy": owner.busy[index],
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def session(self) -> microvms.Session:
        host, port = self.server.server_address[:2]
        return microvms.Session.direct(f"http://{host}:{port}", "agent-token")


@pytest.fixture
def health() -> Iterator[type[Health]]:
    made: list[Health] = []

    def factory(busy: list[bool]) -> Health:
        made.append(Health(busy))
        return made[-1]

    yield factory  # type: ignore[misc]
    for server in made:
        server.server.shutdown()


def test_while_busy_ends_on_the_first_idle_answer(health) -> None:
    daemon = health([True, True, False])
    keepalive = daemon.session.keep_awake(interval=1, while_busy=True)
    report = keepalive.wait(timeout=15)
    assert report is not None
    assert (report.end, report.polls, report.last_busy) == ("idle", 3, False)
    assert not keepalive.running
    # Unauthenticated health, and nothing else.
    assert daemon.seen == [("/v1/health", None)] * 3


def test_stop_ends_it_and_reports_what_it_did(health) -> None:
    daemon = health([True])
    keepalive = daemon.session.keep_awake(interval=1)
    assert keepalive.running
    assert keepalive.wait(timeout=1.5) is None
    report = keepalive.stop()
    assert report.end == "stopped" and report.polls >= 2
    assert report.last_busy is True
    polls = len(daemon.seen)
    time.sleep(1.5)
    assert len(daemon.seen) == polls, "a stopped keepalive kept polling"


def test_the_context_manager_stops_on_exit(health) -> None:
    daemon = health([True])
    with daemon.session.keep_awake(interval=1) as keepalive:
        assert keepalive.running
    assert not keepalive.running
    assert keepalive.stop().end == "stopped"


def test_dropping_the_handle_stops_polling(health) -> None:
    daemon = health([True])
    daemon.session.keep_awake(interval=1)
    gc.collect()
    time.sleep(0.5)
    polls = len(daemon.seen)
    time.sleep(1.5)
    assert len(daemon.seen) == polls


def test_max_duration_ends_it_even_while_busy(health) -> None:
    daemon = health([True])
    report = daemon.session.keep_awake(interval=1, max_duration=2.5).wait(timeout=10)
    assert report is not None and report.end == "elapsed"
    assert 2.4 <= report.elapsed_sec < 4


def test_an_interval_over_half_the_window_is_refused_before_polling(health) -> None:
    daemon = health([True])
    with pytest.raises(microvms.InvalidArgError, match="half the 60s idle window"):
        daemon.session.keep_awake(interval=31)
    with pytest.raises(microvms.InvalidArgError, match="below 1s"):
        daemon.session.keep_awake(interval=0.5)
    with pytest.raises(microvms.InvalidArgError, match="platform minimum"):
        daemon.session.keep_awake(idle_window=59)
    assert daemon.seen == []
    daemon.session.keep_awake(interval=31, idle_window=600).stop()


def test_an_unreachable_daemon_is_retried_then_raised() -> None:
    session = microvms.Session.direct("http://127.0.0.1:9", "agent-token")
    keepalive = session.keep_awake(interval=1)
    with pytest.raises(microvms.RetryableError):
        keepalive.wait(timeout=20)
    with pytest.raises(microvms.RetryableError):
        keepalive.stop()
