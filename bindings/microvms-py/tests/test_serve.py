# SPDX-License-Identifier: Apache-2.0
"""`Session.tunnel` and `Session.port_forward` over a direct session, and the tunnel identity.

A direct session needs no endpoint proxy, so a forward reaches a local HTTP server at the guest
port, and a tunnel to an endpoint nothing listens on fails each connection it accepts. That is
enough to assert the handle's contract here: it listens where it says, serves until stopped, keeps
serving after a failed connection, lists each one that didn't end clean, and cuts what's left open
when `stop(timeout=...)` runs out. The relay through a real daemon and the endpoint proxy is
`crates/microvms-edges/tests/serve.rs`'s, and the live suite drives the core loops against AWS
(`drive_serve`).
"""

from __future__ import annotations

import base64
import socket
import threading
import time
import urllib.request
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import microvms

# Nothing listens on the discard port, so a direct session's upgrade there is refused at once.
UNREACHABLE = "http://127.0.0.1:9"
HOST_SEED = base64.b64encode(bytes([7] * 32)).decode()
VM_PUBLIC_KEY = base64.b64encode(bytes([9] * 32)).decode()


# `/slow` sets this when it arrives and holds its answer until the test ends.
SLOW_ARRIVED = threading.Event()
SLOW_RELEASE = threading.Event()


@pytest.fixture
def upstream() -> Iterator[int]:
    """A guest HTTP server on loopback that answers every GET with its path; yields its port.

    `/slow` holds its answer until the test ends, so a forwarded connection stays open."""
    SLOW_ARRIVED.clear()
    SLOW_RELEASE.clear()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/slow":
                SLOW_ARRIVED.set()
                SLOW_RELEASE.wait(30)
            body = f"guest saw {self.path}".encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    SLOW_RELEASE.set()
    server.shutdown()


def direct() -> microvms.Session:
    return microvms.Session.direct(UNREACHABLE, "serve-agent-token")


def address(local_address: str) -> tuple[str, int]:
    host, port = local_address.rsplit(":", 1)
    return host, int(port)


def connect_and_wait_for_the_close(local_address: str) -> None:
    """Opens a connection and reads until the handle closes it, so it was accepted and ended."""
    with socket.create_connection(address(local_address), timeout=10) as client:
        client.settimeout(10)
        while client.recv(4096):
            pass


def test_a_port_forward_serves_a_request_and_stop_returns_its_report(
    upstream: int,
) -> None:
    forward = direct().port_forward(upstream)
    host, port = address(forward.local_address)
    assert host == "127.0.0.1" and port != 0, forward.local_address
    assert forward.running

    with urllib.request.urlopen(
        f"http://{forward.local_address}/through", timeout=10
    ) as answer:
        assert answer.status == 200
        assert answer.read() == b"guest saw /through"

    report = forward.stop()
    assert (report.served, report.refused, report.upgrades) == (1, 0, 0)
    assert report.stopped == "stopped"
    assert report.ended == []
    assert report.proxy_token_mints == 0, "a direct session mints no proxy token"
    assert not forward.running
    again = forward.stop()
    assert (again.served, again.stopped) == (1, "stopped"), "stop() is repeatable"


def test_a_tunnel_lists_each_failed_connection_and_keeps_serving() -> None:
    tunnel = direct().tunnel(8080)
    connect_and_wait_for_the_close(tunnel.local_address)
    connect_and_wait_for_the_close(tunnel.local_address)
    assert tunnel.running, "a failed connection doesn't end the tunnel"

    report = tunnel.stop()
    assert (report.served, report.refused) == (2, 2), report
    assert (report.truncated, report.unproven) == (0, 0)
    assert [end.kind for end in report.ended] == ["failed", "failed"]
    assert all(end.code is None and end.detail for end in report.ended)
    assert all(end.peer.startswith("127.0.0.1:") for end in report.ended)
    assert "TunnelReport(served=2" in repr(report)


def test_the_limit_stops_the_tunnel_on_its_own() -> None:
    tunnel = direct().tunnel(8080, max_connections=1)
    connect_and_wait_for_the_close(tunnel.local_address)
    deadline = time.monotonic() + 10
    while tunnel.running and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not tunnel.running, "the tunnel kept serving past its limit"
    report = tunnel.stop()
    assert (report.served, report.stopped) == (1, "limit")


def test_stop_with_a_timeout_cuts_a_connection_left_open(upstream: int) -> None:
    forward = direct().port_forward(upstream)
    # The guest holding its answer proves the forward accepted the connection, which stays
    # open until it's cut.
    with socket.create_connection(address(forward.local_address), timeout=10) as kept:
        kept.sendall(b"GET /slow HTTP/1.1\r\nHost: localhost\r\n\r\n")
        assert SLOW_ARRIVED.wait(10), "the forward never reached the guest"
        started = time.monotonic()
        report = forward.stop(timeout=0.2)
        assert time.monotonic() - started < 10, "the timeout didn't end the wait"
    assert report.served == 1
    assert [end.kind for end in report.ended] == ["failed"], report.ended
    assert "cut" in report.ended[0].detail


def test_the_context_manager_stops_the_forward(upstream: int) -> None:
    with direct().port_forward(upstream) as forward:
        local = forward.local_address
        assert forward.running
    assert not forward.running
    with pytest.raises(OSError):
        socket.create_connection(address(local), timeout=2).close()


def test_a_bad_bind_is_refused_before_anything_listens() -> None:
    with pytest.raises(microvms.InvalidArgError, match='"localhost"'):
        direct().tunnel(8080, bind="localhost")
    with pytest.raises(microvms.InvalidArgError):
        direct().port_forward(8080, bind="not an address")


def test_verify_identity_takes_a_tunnel_identity_only() -> None:
    with pytest.raises(TypeError):
        direct().tunnel(8080, verify_identity=HOST_SEED)  # type: ignore[arg-type]


def test_a_tunnel_identity_round_trips_and_keeps_its_seed_out_of_repr() -> None:
    identity = microvms.TunnelIdentity(HOST_SEED, VM_PUBLIC_KEY)
    assert identity.host_seed == HOST_SEED
    assert identity.vm_public_key == VM_PUBLIC_KEY
    assert identity == microvms.TunnelIdentity(HOST_SEED, VM_PUBLIC_KEY)
    shown = repr(identity)
    assert VM_PUBLIC_KEY in shown and HOST_SEED not in shown, shown
    with pytest.raises(microvms.InvalidArgError, match="base64"):
        microvms.TunnelIdentity("not base64!", VM_PUBLIC_KEY)
    with pytest.raises(microvms.InvalidArgError):
        microvms.TunnelIdentity(HOST_SEED, base64.b64encode(b"short").decode())


def test_a_name_records_tunnel_identity_is_the_pair_or_nothing() -> None:
    plain = microvms.NameRecord(
        "ci", "microvm-a", UNREACHABLE, "tok", microvms.Region.us_east_1()
    )
    assert plain.tunnel_identity is None
    stored = plain.to_dict()
    stored["identityHostSeed"] = HOST_SEED
    stored["identityVmPublicKey"] = VM_PUBLIC_KEY
    record = microvms.NameRecord.from_dict(stored)
    assert record.tunnel_identity == microvms.TunnelIdentity(HOST_SEED, VM_PUBLIC_KEY)
    assert HOST_SEED not in repr(record)
    del stored["identityVmPublicKey"]
    torn = microvms.NameRecord.from_dict(stored)
    with pytest.raises(microvms.InvalidArgError, match="one half"):
        _ = torn.tunnel_identity


def test_a_sandbox_that_launched_nothing_has_no_tunnel_identity() -> None:
    assert microvms.Sandbox(microvms.Region.us_east_1()).tunnel_identity is None
