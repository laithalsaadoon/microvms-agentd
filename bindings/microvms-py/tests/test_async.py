# SPDX-License-Identifier: Apache-2.0
"""The awaitable twins: every `*_async` method and function, against local HTTP.

Each method that calls AWS or the daemon has a twin with the same arguments that returns a
coroutine (`src/runtime.rs` has the design). What a unit run can say about them:

* the **census**: which twins exist, and that each has its blocking spelling with the same
  signature, so the two can't drift apart unnoticed;
* the **wire**: a twin sends what its blocking spelling sends and answers the same result or
  the same exception class, against a loopback transcription of the daemon's routes (not
  `agentd`; `conftest.py` says why that boundary is stated rather than hidden);
* the **event loop**: the work runs on the shared runtime, so the loop keeps running and
  concurrent awaits overlap;
* **cancellation**: a cancelled read or wait stops its task, and a cancelled lifecycle
  transition runs to completion. The second is observed through a proxy that holds the
  control plane's connection open: a task that runs on keeps it open, an aborted one closes it.

What a unit run can't say is whether a twin launches a VM, which is the live tier's.
"""

from __future__ import annotations

import asyncio
import contextlib
import http.server
import inspect
import json
import socket
import socketserver
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

import microvms
from conftest import exit_frame, output_frame

# Every twin, by owner. A twin added without a line here fails the census, and a line whose
# twin is gone fails it too, so the set is a reviewed decision rather than whatever compiled.
TWINS: dict[str, set[str]] = {
    "AgentVm": {
        "adopt_async",
        "build_image_async",
        "create_async",
        "ensure_image_async",
        "find_image_async",
        "from_name_async",
        "install_access_async",
        "launch_async",
        "prompt_async",
        "prompt_sync_async",
        "terminate_async",
    },
    "ControlPlane": {
        "create_async",
        "delete_image_async",
        "get_async",
        "get_image_build_async",
        "list_async",
        "list_image_builds_async",
        "list_image_versions_async",
        "list_images_async",
        "resume_async",
        "set_image_version_status_async",
        "suspend_async",
        "terminate_async",
        "wait_for_state_async",
    },
    "ExecHandle": {
        "ack_async",
        "close_stdin_async",
        "kill_async",
        "poll_async",
        "wait_and_ack_async",
        "wait_async",
        "write_stdin_async",
    },
    "ExecProcess": {"kill_async", "wait_async"},
    "KeepAwake": {"stop_async", "wait_async"},
    "NameRegistry": {"import_record_async"},
    "PortForward": {"stop_async"},
    "Sandbox": {
        "adopt_async",
        "build_image_async",
        "create_async",
        "ensure_image_async",
        "from_name_async",
        "managed_base_versions_async",
        "resume_async",
        "run_async",
        "suspend_async",
        "terminate_async",
        "wait_until_running_async",
    },
    "Session": {
        "attach_async",
        "connect_headers_async",
        "connect_subprotocols_async",
        "download_dir_async",
        "download_file_async",
        "download_tar_async",
        "file_exists_async",
        "health_async",
        "kill_async",
        "port_forward_async",
        "procs_async",
        "run_async",
        "run_sync_async",
        "run_to_completion_async",
        "spawn_async",
        "sync_dir_async",
        "tunnel_async",
        "upload_file_async",
        "upload_tar_async",
        "wait_until_ready_async",
    },
    "Tunnel": {"stop_async"},
    "<module>": {
        "install_agent_access_async",
        "installed_agents_async",
        "mint_bedrock_token_async",
        "preflight_async",
        "prompt_agent_async",
        "provision_agentd_async",
        "provision_agentd_report_async",
    },
}

# The context managers that tear down or stop on the way out, and the async iterators.
ASYNC_CONTEXT_MANAGERS = {"AgentVm", "KeepAwake", "PortForward", "Sandbox", "Tunnel"}
ASYNC_ITERATORS = {"ByteStream", "ExecStream"}


@pytest.fixture(autouse=True)
def offline_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default chain reads these without a network call, so every object is the real one."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.delenv("AWS_PROFILE", raising=False)


def owners() -> Iterator[tuple[str, object]]:
    yield "<module>", microvms
    for name in dir(microvms):
        value = getattr(microvms, name)
        if isinstance(value, type) and not issubclass(value, BaseException):
            yield name, value


def blocking_spelling(owner: object, twin: str) -> object:
    """The method a twin is the twin of: `create_async`'s is the constructor itself."""
    if twin == "create_async":
        return owner
    return getattr(owner, twin.removesuffix("_async"))


# -- the census -------------------------------------------------------------------------------


def test_bind_25_the_twins_are_exactly_the_census() -> None:
    """BIND-25: every method that calls AWS or the daemon has a twin, and only those do."""
    found: dict[str, set[str]] = {}
    for name, owner in owners():
        twins = {
            member
            for member in dir(owner)
            if member.endswith("_async") and not member.startswith("_")
        }
        if twins:
            found[name] = twins
    assert found == TWINS


def test_bind_25_each_twin_takes_exactly_its_blocking_spellings_arguments() -> None:
    """BIND-25: same names, same kinds, same defaults, so a call ports by adding `await`."""
    for name, twins in TWINS.items():
        owner = microvms if name == "<module>" else getattr(microvms, name)
        for twin in twins:
            awaitable = inspect.signature(getattr(owner, twin))
            blocking = inspect.signature(blocking_spelling(owner, twin))
            assert awaitable.parameters == blocking.parameters, f"{name}.{twin}"


def test_the_context_managers_and_iterators_have_their_async_protocols() -> None:
    for name in ASYNC_CONTEXT_MANAGERS:
        cls = getattr(microvms, name)
        assert hasattr(cls, "__aenter__") and hasattr(cls, "__aexit__"), name
    for name in ASYNC_ITERATORS:
        cls = getattr(microvms, name)
        assert hasattr(cls, "__aiter__") and hasattr(cls, "__anext__"), name


def test_a_twin_answers_a_coroutine_and_checks_its_arguments_before_it() -> None:
    """A call builds the coroutine and nothing else; a bad argument is a `TypeError` at once,
    as it is for the blocking spelling, rather than one raised by the first `await`."""
    session = microvms.Session.direct("http://127.0.0.1:9", "agent-token")
    coroutine = session.health_async()
    assert asyncio.iscoroutine(coroutine)
    coroutine.close()
    with pytest.raises(TypeError):
        session.run_sync_async()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        session.run_sync()  # type: ignore[call-arg]


# -- a loopback daemon ------------------------------------------------------------------------

Route = Callable[[str, str, bytes], tuple[int, bytes, str]]


class Daemon:
    """A loopback server answering `route(method, path, body)` for every request, logging each
    as `"<METHOD> <path>"` and noting the thread each request ran on."""

    def __init__(self, route: Route) -> None:
        self.log: list[str] = []
        log = self.log

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def answer(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length) if length else b""
                path = self.path.split("?")[0]
                log.append(f"{self.command} {path}")
                status, reply, content_type = route(self.command, self.path, body)
                self.send_response(status)
                self.send_header("content-type", content_type)
                self.send_header("content-length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)
                self.wfile.flush()

            def do_GET(self) -> None:
                self.answer()

            def do_POST(self) -> None:
                self.answer()

            def do_PUT(self) -> None:
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
def daemon() -> Iterator[Callable[[Route], Daemon]]:
    built: list[Daemon] = []

    def factory(route: Route) -> Daemon:
        built.append(Daemon(route))
        return built[-1]

    yield factory
    for server in built:
        server.close()


def json_ok(value: object) -> tuple[int, bytes, str]:
    return 200, json.dumps(value).encode(), "application/json"


HEALTH = {
    "version": "0.1.0",
    "bootstrapped": True,
    "disk": None,
    "identity_degraded": False,
    "identity_repaired": True,
}


def outcome(
    phase: str, exit_code: int | None = 0, stdout: str = ""
) -> dict[str, object]:
    return {
        "exec_id": "x-async",
        "phase": phase,
        "exit_code": exit_code,
        "signal": None,
        "timed_out": False,
        "stdout": stdout,
        "stderr": "",
        "truncated": False,
        "writers_may_be_alive": False,
    }


def exec_routes(frames: list[bytes], stdout: str = "") -> Route:
    """Start, one stream of `frames`, polls that see the exec exited, and the ack."""

    def route(method: str, path: str, body: bytes) -> tuple[int, bytes, str]:
        if path.startswith("/v1/health"):
            return json_ok(HEALTH)
        if path.startswith("/v1/exec/start"):
            return json_ok({"exec_id": "x-async", "phase": "running"})
        if "/stream" in path:
            return 200, b"".join(frames), "text/event-stream"
        if path.endswith("/ack"):
            return json_ok(outcome("acked", 0, stdout))
        return json_ok(outcome("exited", 0, stdout))

    return route


# -- the wire ---------------------------------------------------------------------------------


def test_bind_25_a_twin_answers_what_its_blocking_spelling_answers(daemon) -> None:
    """BIND-25: one future behind both spellings, so one answer."""
    server = daemon(exec_routes([], stdout="hi"))
    session = server.session
    blocking = session.health()
    awaited = asyncio.run(session.health_async())
    assert (awaited.version, awaited.bootstrapped) == (
        blocking.version,
        blocking.bootstrapped,
    )

    result = asyncio.run(session.run_sync_async(["echo", "hi"], exec_id="x-async"))
    assert (result.exit_code, result.stdout, result.ok) == (0, "hi", True)
    assert "POST /v1/exec/start" in server.log
    assert server.log[-1] == "POST /v1/exec/x-async/ack"


def test_bind_25_a_twin_raises_the_class_its_blocking_spelling_raises() -> None:
    """BIND-25: a refused connection is the same retryable error either way, with the same
    code, so a retry policy written against one works for the other."""
    session = microvms.Session.direct("http://127.0.0.1:9", "agent-token")
    with pytest.raises(microvms.MicrovmError) as blocking:
        session.health()
    with pytest.raises(microvms.MicrovmError) as awaited:
        asyncio.run(session.health_async())
    assert type(awaited.value) is type(blocking.value)
    assert (awaited.value.code, awaited.value.retryable) == (
        blocking.value.code,
        blocking.value.retryable,
    )


def test_a_twin_refuses_locally_what_its_blocking_spelling_refuses() -> None:
    """Core's local refusals reach the twin unchanged: no state check of the binding's own."""
    region = microvms.Region.us_east_1()

    async def refusals() -> None:
        sandbox = await microvms.Sandbox.create_async(region)
        assert sandbox.lifecycle == "PENDING"
        with pytest.raises(microvms.PreconditionError):
            await sandbox.wait_until_running_async()
        with pytest.raises(microvms.InvalidArgError, match="pass log_group"):
            await sandbox.run_async(image_identifier="arn:image", log_stream="s")
        plane = await microvms.ControlPlane.create_async(region)
        with pytest.raises(microvms.InvalidArgError):
            await plane.get_async("")
        assert await plane.delete_image_async("") is False

    asyncio.run(refusals())


def test_files_and_stdin_cross_through_the_twins(daemon) -> None:
    stored: dict[str, bytes] = {}

    def route(method: str, path: str, body: bytes) -> tuple[int, bytes, str]:
        if path.startswith("/v1/fs/file") and method == "PUT":
            stored["file"] = body
            return json_ok({"path": "/tmp/f", "bytes": len(body)})
        if path.startswith("/v1/fs/file"):
            return 200, stored.get("file", b""), "application/octet-stream"
        if "/stdin" in path:
            return json_ok({"exec_id": "x-async", "written": len(body), "eof": True})
        return json_ok(outcome("exited"))

    server = daemon(route)
    session = server.session

    async def transfer() -> tuple[bytes, microvms.StdinAck]:
        await session.upload_file_async("/tmp/f", b"through the twin")
        data = await session.download_file_async("/tmp/f")
        ack = await session.exec("x-async").write_stdin_async(b"input", eof=True)
        return data, ack

    data, ack = asyncio.run(transfer())
    assert data == b"through the twin"
    assert stored["file"] == b"through the twin"
    assert ack.exec_id == "x-async"


def test_bind_25_async_for_over_a_stream_yields_what_for_yields(sse_server) -> None:
    """BIND-25: the iterator and the async iterator drain the same channel the same way."""
    frames = [output_frame(0, b"ab"), output_frame(2, b"cd"), exit_frame(4)]
    server = sse_server([frames, frames])  # type: ignore[operator]
    handle = microvms.Session.direct(server.endpoint, "agent-token").exec("x-async")
    blocking = [(event.kind, event.offset) for event in handle.stream(idle_timeout=5.0)]

    async def drain() -> list[tuple[str, int]]:
        return [
            (event.kind, event.offset)
            async for event in handle.stream(idle_timeout=5.0)
        ]

    assert asyncio.run(drain()) == blocking
    assert blocking == [("output", 0), ("output", 2), ("exit", 4)]


def test_async_for_over_a_spawned_process_splits_stdout_from_stderr(daemon) -> None:
    frames = [
        output_frame(0, b"out", "stdout"),
        output_frame(3, b"err", "stderr"),
        exit_frame(6),
    ]
    server = daemon(exec_routes(frames))

    async def split() -> tuple[bytes, bytes, int | None]:
        proc = await server.session.spawn_async(["sh", "-c", "true"], exec_id="x-async")

        async def read(side: microvms.ByteStream) -> bytes:
            return b"".join([chunk async for chunk in side])

        stdout, stderr = await asyncio.gather(read(proc.stdout), read(proc.stderr))
        result = await proc.wait_async(timeout=5)
        return stdout, stderr, result.exit_code

    assert asyncio.run(split()) == (b"out", b"err", 0)


def test_run_to_completion_async_calls_back_on_the_event_loops_thread(daemon) -> None:
    """The callback runs where an asyncio caller's code runs, one chunk at a time, in order."""
    frames = [output_frame(0, b"ab"), output_frame(2, b"cd"), exit_frame(4)]
    server = daemon(exec_routes(frames, stdout="abcd"))
    seen: list[tuple[int, bytes, int]] = []

    async def drive() -> microvms.ExecResult:
        return await server.session.run_to_completion_async(
            ["true"],
            on_output=lambda chunk: seen.append(
                (chunk.offset, chunk.data, threading.get_ident())
            ),
            exec_id="x-async",
        )

    loop_thread = threading.get_ident()
    result = asyncio.run(drive())
    assert [(offset, data) for offset, data, _ in seen] == [(0, b"ab"), (2, b"cd")]
    assert {thread for _, _, thread in seen} == {loop_thread}
    assert result.stdout == "abcd" and result.posix_exit_code == 0
    assert server.log[-1] == "POST /v1/exec/x-async/ack"
    assert server.log.count("POST /v1/exec/x-async/ack") == 1


def test_run_to_completion_async_acks_then_reraises_a_callbacks_exception(
    daemon,
) -> None:
    frames = [output_frame(0, b"a"), output_frame(1, b"b"), exit_frame(2)]
    server = daemon(exec_routes(frames, stdout="ab"))
    seen: list[bytes] = []

    def boom(chunk: microvms.OutputChunk) -> None:
        seen.append(chunk.data)
        raise ValueError("the harness callback failed")

    with pytest.raises(ValueError, match="the harness callback failed"):
        asyncio.run(
            server.session.run_to_completion_async(
                "true", on_output=boom, exec_id="x-async"
            )
        )
    assert seen == [b"a"], "delivery must stop at the first exception"
    assert server.log[-1] == "POST /v1/exec/x-async/ack", server.log


def test_the_async_context_managers_stop_what_they_started(daemon) -> None:
    server = daemon(exec_routes([]))
    region = microvms.Region.us_east_1()

    async def scoped() -> tuple[bool, bool, bool]:
        async with await microvms.Sandbox.create_async(region) as sandbox:
            assert isinstance(sandbox, microvms.Sandbox)
        session = server.session
        async with session.keep_awake(interval=1) as keepalive:
            assert keepalive.running
        async with await session.tunnel_async(9000) as tunnel:
            assert tunnel.running
        report = await (await session.port_forward_async(9000)).stop_async()
        assert report.served == 0
        return keepalive.running, tunnel.running, sandbox.was_terminated

    keepalive_running, tunnel_running, _ = asyncio.run(scoped())
    assert not keepalive_running and not tunnel_running


def test_the_daemon_fetch_twin_answers_a_caller_supplied_binary(tmp_path: Path) -> None:
    binary = tmp_path / "agentd"
    data = (
        bytes([0x7F, 0x45, 0x4C, 0x46, 2, 1, 1, 0])
        + bytes(10)
        + bytes([0xB7, 0])
        + bytes(44)
    )
    binary.write_bytes(data)
    try:
        expected = microvms.provision_agentd(state_dir=tmp_path, binary=binary)
    except microvms.MicrovmError as refused:
        # Whatever the blocking spelling decides about these bytes, the twin decides too.
        with pytest.raises(type(refused)):
            asyncio.run(
                microvms.provision_agentd_async(state_dir=tmp_path, binary=binary)
            )
        return
    assert (
        asyncio.run(microvms.provision_agentd_async(state_dir=tmp_path, binary=binary))
        == expected
    )


def test_a_refused_environment_region_stops_the_preflight_twin_before_any_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_REGION", "eu-central-1")
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    report = asyncio.run(microvms.preflight_async())
    assert report.to_dict() == microvms.preflight().to_dict()
    assert report.ok is False


# -- the event loop ---------------------------------------------------------------------------


def slow_health(delay: float) -> Route:
    def route(method: str, path: str, body: bytes) -> tuple[int, bytes, str]:
        time.sleep(delay)
        return json_ok(HEALTH)

    return route


def test_bind_25_the_event_loop_keeps_running_while_a_twin_waits(daemon) -> None:
    """BIND-25: the request runs on the shared runtime, so a ticker on the loop keeps ticking
    through it. A twin that blocked the loop would let it tick once, after."""
    server = daemon(slow_health(0.5))
    session = server.session

    async def measure() -> int:
        ticks = 0
        done = asyncio.Event()

        async def ticker() -> None:
            nonlocal ticks
            while not done.is_set():
                ticks += 1
                await asyncio.sleep(0.01)

        ticking = asyncio.create_task(ticker())
        await session.health_async()
        done.set()
        await ticking
        return ticks

    assert asyncio.run(measure()) >= 20


def test_concurrent_awaits_on_one_owned_session_overlap(daemon) -> None:
    """Four half-second requests through one owned session finish together, not in series."""
    server = daemon(slow_health(0.5))
    session = server.session

    async def gather() -> float:
        started = time.monotonic()
        answers = await asyncio.gather(*(session.health_async() for _ in range(4)))
        assert all(answer.bootstrapped for answer in answers)
        return time.monotonic() - started

    assert asyncio.run(gather()) < 1.5


# -- cancellation -----------------------------------------------------------------------------


def test_bind_26_a_cancelled_wait_stops_its_task(daemon) -> None:
    """BIND-26: once the awaiting task is cancelled, the wait polls no more.

    **Falsification**: make `Spawned`'s drop leave the task running (`abort_on_drop: false` in
    `runtime::spawn`) and the polls go on after the cancel until the wait's own deadline."""
    polls: list[float] = []

    def route(method: str, path: str, body: bytes) -> tuple[int, bytes, str]:
        polls.append(time.monotonic())
        return json_ok({"exec_id": "x-async", "phase": "running"})

    server = daemon(route)
    handle = server.session.exec("x-async")

    async def cancel_it() -> float:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(handle.wait_async(timeout=60), 1.0)
        return time.monotonic()

    cancelled_at = asyncio.run(cancel_it())
    assert polls, "the wait polled before it was cancelled"
    time.sleep(2.0)
    late = [at for at in polls if at > cancelled_at + 0.2]
    assert late == [], f"{len(late)} polls after the cancel"


class StallingProxy:
    """An HTTPS proxy that takes each `CONNECT`, never answers it, and notes when the client
    closes its side: the observable difference between a task that ran on and one that was
    aborted."""

    def __init__(self) -> None:
        self.connected = threading.Event()
        self.closed: list[float] = []
        self.release = threading.Event()
        owner = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                self.request.settimeout(0.05)
                received = b""
                while b"\r\n\r\n" not in received:
                    try:
                        chunk = self.request.recv(4096)
                    except TimeoutError:
                        continue
                    if not chunk:
                        return
                    received += chunk
                owner.connected.set()
                while not owner.release.is_set():
                    try:
                        if self.request.recv(4096) == b"":
                            owner.closed.append(time.monotonic())
                            return
                    except TimeoutError:
                        continue
                    except OSError:
                        owner.closed.append(time.monotonic())
                        return

        self._server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        threading.Thread(
            target=self._server.serve_forever, args=(0.01,), daemon=True
        ).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def close(self) -> None:
        self.release.set()
        self._server.shutdown()
        self._server.server_close()


@contextlib.contextmanager
def stalling_proxy(monkeypatch: pytest.MonkeyPatch) -> Iterator[StallingProxy]:
    proxy = StallingProxy()
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, proxy.url)
    try:
        yield proxy
    finally:
        proxy.close()


def cancel_once_connected(proxy: StallingProxy, call: Callable[[], object]) -> float:
    """Starts `call()`'s awaitable, cancels it once the proxy holds its connection, and answers
    when the cancel returned."""

    async def run() -> float:
        task = asyncio.ensure_future(call())  # type: ignore[arg-type]
        while not proxy.connected.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return time.monotonic()

    return asyncio.run(run())


def test_bind_26_a_cancelled_read_closes_its_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BIND-26: `get_async` cancelled mid-request drops the request, so the proxy sees its
    client close right after the cancel."""
    with stalling_proxy(monkeypatch) as proxy:
        plane = microvms.ControlPlane(microvms.Region.us_east_1())
        cancelled_at = cancel_once_connected(proxy, lambda: plane.get_async("mvm-1"))
        deadline = time.monotonic() + 3
        while not proxy.closed and time.monotonic() < deadline:
            time.sleep(0.02)
        assert proxy.closed, "the aborted request never closed its connection"
        assert proxy.closed[0] - cancelled_at < 1.0


def test_bind_27_a_cancelled_lifecycle_transition_runs_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BIND-27: `terminate_async` cancelled mid-request keeps its request open: the task runs to
    its answer with nobody awaiting it, as a launch must, so a VM it starts still has a handle.

    **Falsification**: make `runtime::spawn_shielded` abort on drop and the proxy sees the
    connection close right after the cancel, as it does for `get_async`."""
    with stalling_proxy(monkeypatch) as proxy:
        plane = microvms.ControlPlane(microvms.Region.us_east_1())
        cancel_once_connected(proxy, lambda: plane.terminate_async("mvm-1"))
        time.sleep(1.5)
        assert proxy.closed == [], (
            "the transition's request was dropped with its awaitable"
        )


def test_a_cancelled_anext_leaves_the_event_for_the_next_one(sse_server) -> None:
    frames = [output_frame(0, b"ab"), exit_frame(2)]
    server = sse_server([frames])  # type: ignore[operator]
    stream = (
        microvms.Session.direct(server.endpoint, "agent-token")
        .exec("x-async")
        .stream(idle_timeout=5.0)
    )

    async def resume() -> list[str]:
        first = asyncio.ensure_future(stream.__anext__())
        first.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await first
        return [event.kind async for event in stream]

    assert asyncio.run(resume()) == ["output", "exit"]


def test_an_unused_port_refuses_connections() -> None:
    """The refused-connection tests above rely on port 9 refusing on loopback."""
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", 9)) != 0
