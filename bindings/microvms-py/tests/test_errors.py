# SPDX-License-Identifier: Apache-2.0
"""The exception hierarchy: `src/errors.rs`.

`test_smoke.py` asserts the thirteen classes exist under one base. This file asserts the part a
caller actually depends on: that the **mapping** from a core `ErrorKind` to a Python class is
right, and that the four attributes travel on every raised exception rather than on some of them.

Why that split matters. A binding where every error arrived as the base `MicrovmError` would pass
a hierarchy check and be useless — `except WindowClosedError` is the whole reason for a class per
condition, and it only works if the *right* class is raised. Likewise `.code` present on some
paths and absent on others is worse than absent everywhere, because it works in the first test
someone writes.

# The boundary

Only the locally-reachable kinds are exercised here: a unit run has no daemon and no AWS, so
`ERR_LAUNCH_DIED` and `ERR_BUILD_WEDGED` cannot be provoked without one. What is covered is every
kind this suite can *cause* — which is the set a caller hits before a request goes out — plus the
structural claim (one class per kind, no gaps) over the whole enum. The conformance suite covers
the wire-reachable half.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import microvms

# Every exception class, paired with the `ERR_*` code it stands for. Written out rather than
# derived, because this table *is* the contract: deriving it from the module would make the test
# agree with whatever the module does, including a mis-mapping.
KIND_TO_EXCEPTION = [
    ("ERR_UNEXPECTED", microvms.UnexpectedError),
    ("ERR_INVALID_ARG", microvms.InvalidArgError),
    ("ERR_RETRYABLE", microvms.RetryableError),
    ("ERR_CREDENTIALS", microvms.CredentialsError),
    ("ERR_PROTOCOL", microvms.ProtocolError),
    ("ERR_BUILD_WEDGED", microvms.BuildWedgedError),
    ("ERR_LAUNCH_DIED", microvms.LaunchDiedError),
    ("ERR_WINDOW_CLOSED", microvms.WindowClosedError),
    ("ERR_PLATFORM", microvms.PlatformError),
    ("ERR_TIMEOUT", microvms.TimeoutError),
    ("ERR_INTERRUPTED", microvms.InterruptedError),
    ("ERR_PRECONDITION", microvms.PreconditionError),
    ("ERR_EXEC_FAILED", microvms.ExecFailedError),
]


# -- the hierarchy is a hierarchy ---------------------------------------------


def test_every_exception_is_a_distinct_class_under_the_one_base() -> None:
    """Thirteen classes, thirteen identities.

    Two names bound to one class would make `except WindowClosedError` catch a launch failure,
    which is the specific confusion a class-per-condition hierarchy exists to prevent — and an
    aliasing mistake is invisible to a test that only checks `issubclass`.
    """
    classes = [exception for _, exception in KIND_TO_EXCEPTION]
    assert len({id(exception) for exception in classes}) == 13
    for exception in classes:
        assert issubclass(exception, microvms.MicrovmError)
        assert issubclass(exception, Exception)
        # Direct subclasses, so `except MicrovmError` is one hop and the MRO is readable.
        assert exception.__bases__ == (microvms.MicrovmError,)


def test_no_exception_is_a_subclass_of_another_so_no_except_clause_overlaps() -> None:
    """A flat hierarchy, deliberately.

    If `TimeoutError` were under `RetryableError`, a caller's `except RetryableError` would
    swallow timeouts and retry a deadline that had already passed. Flat means the only broad
    catch is the base one, and reaching for it is visible.
    """
    classes = [exception for _, exception in KIND_TO_EXCEPTION]
    for outer in classes:
        for inner in classes:
            if outer is inner:
                continue
            assert not issubclass(inner, outer), (inner.__name__, outer.__name__)


def test_the_library_exceptions_do_not_shadow_the_builtins_they_are_named_after() -> (
    None
):
    """`microvms.TimeoutError` is **not** the builtin, and neither is `InterruptedError`.

    Two of the thirteen collide with builtin names, which is a real hazard: a caller writing
    `except TimeoutError` after `from microvms import *` would catch a different class than they
    think. So the names are asserted distinct, and the base is what a caller should use.
    """
    assert microvms.TimeoutError is not TimeoutError
    assert microvms.InterruptedError is not InterruptedError
    assert not issubclass(microvms.TimeoutError, TimeoutError)
    assert not issubclass(microvms.InterruptedError, InterruptedError)


# -- the four attributes travel on every raised exception ---------------------


def raise_invalid_arg() -> microvms.MicrovmError:
    """A locally-refused call, which is the shortest path to a real raised exception."""
    with pytest.raises(microvms.MicrovmError) as raised:
        microvms.Region.parse("nope-1")
    return raised.value


def test_a_raised_exception_carries_all_four_attributes_and_not_a_parsed_message() -> (
    None
):
    """`.code`, `.kind`, `.wire_kind`, `.retryable` — the whole set, on the instance.

    Nobody parses a message. That rule is why these are attributes, and a missing one sends a
    caller straight back to string matching on a sentence that is free to change.
    """
    error = raise_invalid_arg()
    assert error.code == "ERR_INVALID_ARG"
    assert error.kind == "ERR_INVALID_ARG"
    assert error.wire_kind is None
    assert error.retryable is False
    # Real booleans and real strings, not truthy stand-ins a caller has to interpret.
    assert isinstance(error.retryable, bool)
    assert isinstance(error.code, str)


def test_code_and_kind_are_the_same_string_because_there_is_one_taxonomy() -> None:
    """Two names for one value, for a caller who thinks in either.

    They are asserted equal rather than each checked against a literal: a binding where they
    drifted would give two answers to one question, and whichever one a consumer branched on
    would be the one that was wrong.
    """
    error = raise_invalid_arg()
    assert error.code == error.kind
    assert error.code.startswith("ERR_")


def test_a_local_refusal_reports_no_wire_kind_because_nothing_reached_the_daemon() -> (
    None
):
    """`None` and not a guessed status.

    Inventing a wire kind for a local refusal would be a claim nobody made, and a monitor
    counting daemon errors would count client-side typos among them.
    """
    for call in (
        lambda: microvms.Region.parse("eu-central-1"),
        lambda: microvms.SizeClass.from_baseline_mib(1500),
        lambda: microvms.Duration.measured(-1.0),
        lambda: microvms.RunHookTimeout(3600),
    ):
        with pytest.raises(microvms.MicrovmError) as raised:
            call()
        assert raised.value.wire_kind is None
        assert raised.value.code == "ERR_INVALID_ARG"


def test_the_message_is_the_cores_own_and_survives_the_crossing_intact() -> None:
    """Not reworded here.

    The core's messages are the ones naming the `docs/PLATFORM.md` finding that measured the
    behaviour, and a binding that shortened them would discard the whole point of the closure.
    Checked on the region refusal, whose message has two halves that only mean something
    together.
    """
    with pytest.raises(microvms.InvalidArgError) as raised:
        microvms.Region.parse("eu-central-1")
    message = str(raised.value)
    # "AccessDeniedException" alone reads as an IAM problem; the word *null* is what says
    # otherwise. Both halves crossed the boundary.
    assert "AccessDeniedException" in message
    assert "null" in message
    assert len(message) > 80, f"the message was truncated on the way out: {message!r}"


def test_an_invalid_arg_is_not_retryable_because_repeating_it_changes_nothing() -> None:
    """`retryable` is the field a retry loop reads, so its value per kind is load-bearing.

    A refused argument marked retryable is an infinite loop over an unchanging refusal — the
    exact failure the retryable split exists to prevent, and one that looks like a hang rather
    than an error.
    """
    assert raise_invalid_arg().retryable is False


# -- the exceptions are catchable the way callers write catches ----------------


def test_a_specific_except_clause_catches_only_its_own_kind() -> None:
    """The point of the hierarchy, exercised as a caller writes it."""
    with pytest.raises(microvms.InvalidArgError):
        microvms.Region.parse("nope-1")

    # And a sibling clause does not catch it, which is what makes the specific catch worth
    # writing. `pytest.raises` around the wrong class would swallow the real exception, so this
    # is spelled as an explicit try.
    try:
        microvms.Region.parse("nope-1")
    except microvms.WindowClosedError:  # pragma: no cover — the failure path
        pytest.fail("a region typo was caught as a closed suspension window")
    except microvms.InvalidArgError as error:
        assert error.code == "ERR_INVALID_ARG"


def test_the_base_catches_everything_this_library_raises() -> None:
    """`except MicrovmError` is the one broad catch, and it has to be total."""
    for call in (
        lambda: microvms.Region.parse("nope-1"),
        lambda: microvms.SizeClass.from_baseline_mib(7),
        lambda: microvms.Duration.projected(float("nan")),
        lambda: microvms.BuildHookTimeout(0),
    ):
        with pytest.raises(microvms.MicrovmError):
            call()


def test_an_exception_instance_carries_its_attributes_after_being_re_raised() -> None:
    """The attributes are on the instance, not on the raising frame.

    A caller commonly catches, wraps, and re-raises. If the codes were attached by the raise
    site rather than to the object, they would be gone by the time a handler two frames up read
    them — and that handler is where the branching happens.
    """
    caught: microvms.MicrovmError | None = None
    try:
        try:
            microvms.Region.parse("nope-1")
        except microvms.MicrovmError as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as outer:
        cause = outer.__cause__
        assert isinstance(cause, microvms.InvalidArgError)
        caught = cause
    assert caught is not None
    assert caught.code == "ERR_INVALID_ARG"
    assert caught.retryable is False
    assert caught.wire_kind is None


# -- the codes are the shared taxonomy ----------------------------------------


def test_every_code_is_upper_snake_and_prefixed_so_a_switch_is_writable() -> None:
    """The shape callers pattern-match on, and thirteen distinct values.

    Two kinds sharing a code would make branching ambiguous in a way no type checker sees: the
    consumer's `if code == ...` would take one branch for two different conditions.
    """
    codes = [code for code, _ in KIND_TO_EXCEPTION]
    assert len(set(codes)) == 13
    for code in codes:
        assert code.startswith("ERR_")
        assert code == code.upper()
        assert " " not in code


def test_the_class_names_and_the_codes_agree_so_neither_has_to_be_looked_up() -> None:
    """`ERR_WINDOW_CLOSED` ↔ `WindowClosedError`, mechanically.

    A pairing that drifted — say `ERR_PLATFORM` raised as `PreconditionError` — would be
    invisible to every other test in this file, because both halves exist and both are
    well-formed. This is the check that ties them together, and it derives the expected name
    from the code so it cannot be satisfied by editing a table.
    """
    for code, exception in KIND_TO_EXCEPTION:
        stem = code.removeprefix("ERR_")
        expected = "".join(part.capitalize() for part in stem.split("_")) + "Error"
        assert exception.__name__ == expected, (code, exception.__name__)


def test_each_exception_documents_the_condition_it_stands_for() -> None:
    """A docstring per class, because the class name is not the whole story.

    `ERR_EXEC_FAILED` in particular means the sandbox *worked* and the command in it exited
    non-zero — the one failure that says nothing is wrong with the platform — and a caller
    reading only the name would treat it as an infrastructure error.
    """
    for code, exception in KIND_TO_EXCEPTION:
        doc = exception.__doc__ or ""
        assert code in doc, f"{exception.__name__}'s docstring does not name {code}"
        assert len(doc) > len(code) + 20, (
            f"{exception.__name__} has no real explanation"
        )


# -- a daemon status, through a loopback server ---------------------------------


def test_a_disk_pressure_507_is_a_platform_error_and_not_retryable(
    sse_server: object,
) -> None:
    """A 507 is `InsufficientStorage` on `.wire_kind`, `ERR_PLATFORM`, and `.retryable` False.

    The daemon answers 507 when a write would take the disk under its reserve, and it chose 507
    over 500 because a client retries a 500. Read as one more 5xx, it was a `RetryableError`
    here, so a loop keyed on `.retryable` repeated a write the full disk refuses (#256).
    """
    body = (
        b"refusing to write /workspace: 4096 bytes available on the target filesystem, "
        b"below the 1048576 byte reserve"
    )
    server = sse_server([[body]], 507, health_ok=True)  # type: ignore[operator]
    session = microvms.Session.direct(server.endpoint, "agent-token")

    with pytest.raises(microvms.PlatformError) as raised:
        session.upload_file("/workspace/big.bin", b"x")

    assert server.requested_methods[-1] == "PUT"
    assert server.requested_paths[-1].startswith("/v1/fs/file?")
    assert raised.value.code == "ERR_PLATFORM"
    assert raised.value.wire_kind == "InsufficientStorage"
    assert raised.value.retryable is False
    assert "4096 bytes available" in str(raised.value)


def test_every_other_five_hundred_is_still_retryable(sse_server: object) -> None:
    """A 500 stays `ServerError` and retryable: only the status the daemon chose on purpose moved."""
    server = sse_server([[b"spawn failed"]], 500, health_ok=True)  # type: ignore[operator]
    session = microvms.Session.direct(server.endpoint, "agent-token")

    with pytest.raises(microvms.RetryableError) as raised:
        session.upload_file("/workspace/f", b"x")

    assert server.requested_methods[-1] == "PUT"
    assert server.requested_paths[-1].startswith("/v1/fs/file?")
    assert raised.value.wire_kind == "ServerError"
    assert raised.value.retryable is True


def test_transport_sources_survive_as_python_exception_causes() -> None:
    session = microvms.Session.direct("http://127.0.0.1:9", "test-token")
    with pytest.raises(microvms.RetryableError) as raised:
        session.health()
    assert raised.value.code == "ERR_RETRYABLE"
    assert raised.value.wire_kind == "Transport"
    cause = raised.value.__cause__
    assert isinstance(cause, Exception)
    messages = []
    while cause is not None:
        messages.append(str(cause))
        cause = cause.__cause__
    assert any("connect" in message.lower() for message in messages), messages


@pytest.mark.skipif(
    not os.supports_bytes_environ, reason="requires byte-valued environment"
)
def test_malformed_credential_values_never_reach_exception_causes(
    tmp_path: Path,
) -> None:
    """A fresh provider reads only synthetic input, before any signed request can be sent."""
    code = """
import asyncio
import os
import microvms

os.environb[b"AWS_SECRET_ACCESS_KEY"] = b"SYNTHETIC_CREDENTIAL_SENTINEL\\xff"
os.environb[b"SECRET_ACCESS_KEY"] = b"SYNTHETIC_CREDENTIAL_SENTINEL\\xff"
sandbox = microvms.Sandbox(microvms.Region.us_east_1())
for asynchronous in (False, True):
    try:
        if asynchronous:
            asyncio.run(sandbox.run_async(image_identifier="wanted-image"))
        else:
            sandbox.run(image_identifier="wanted-image")
    except microvms.CredentialsError as error:
        assert error.code == "ERR_CREDENTIALS"
        assert not error.retryable
        messages = []
        while error is not None:
            messages.append(str(error))
            assert "SYNTHETIC_CREDENTIAL_SENTINEL" not in str(error) + repr(error)
            error = error.__cause__
        assert any("not valid Unicode" in message for message in messages), messages
    else:
        raise AssertionError("malformed credentials must fail before a request")
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        env={
            "AWS_ACCESS_KEY_ID": "AKIDEXAMPLE",
            "AWS_EC2_METADATA_DISABLED": "true",
            "AWS_CONFIG_FILE": str(tmp_path / "config"),
            "AWS_SHARED_CREDENTIALS_FILE": str(tmp_path / "credentials"),
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "NO_PROXY": "",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not result.stderr, result.stderr


def test_async_shutdown_retires_pending_wakes_and_refuses_later_awaits() -> None:
    """Pending Python wakes retire before finalization; later sync callers still work."""
    code = """
import atexit
import asyncio

def earlier_exit_handler():
    session = microvms.Session.direct("http://127.0.0.1:9", "test-token")
    try:
        session.health()
    except microvms.RetryableError:
        pass
    else:
        raise AssertionError("the closed local port must refuse the request")

atexit.register(earlier_exit_handler)
import microvms

async def main():
    connected = asyncio.Event()
    release = asyncio.Event()
    handlers = []

    async def hold(reader, writer):
        handlers.append(asyncio.current_task())
        connected.set()
        await release.wait()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(hold, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    session = microvms.Session.direct(f"http://127.0.0.1:{port}", "test-token")
    try:
        pending = session.health_async()
        pending.send(None)
        await asyncio.wait_for(connected.wait(), 5)
        atexit._run_exitfuncs()
        for operation in (pending, session.health_async()):
            try:
                await asyncio.wait_for(operation, 1)
            except RuntimeError as error:
                assert "asynchronous runtime is shutting down" in str(error)
            except TimeoutError as error:
                raise AssertionError("shutdown did not refuse the asynchronous operation") from error
            else:
                raise AssertionError("a closed asynchronous bridge must refuse the await")
    finally:
        server.close()
        release.set()
        await asyncio.gather(*handlers)
        await server.wait_closed()

asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not result.stderr, result.stderr


@pytest.mark.parametrize("finish", ["complete", "cancel", "shutdown"])
def test_finishing_an_await_drains_the_inflight_python_wake(finish: str) -> None:
    """The worker must finish its Python callback before the await or process can end."""
    code = r"""
import atexit
import asyncio
import socket
import sys
import threading
import microvms

finish = sys.argv[1]
entered = threading.Event()
release = threading.Event()
can_finish = threading.Event()
respond = threading.Event()

class HeldLoop(asyncio.SelectorEventLoop):
    def call_soon_threadsafe(self, callback, *args, context=None):
        if threading.current_thread() is not threading.main_thread():
            entered.set()
            assert release.wait(10), "the main thread did not release the worker"
        return super().call_soon_threadsafe(callback, *args, context=context)

listener = socket.socket()
listener.bind(("127.0.0.1", 0))
listener.listen()

def serve():
    connection, _ = listener.accept()
    assert respond.wait(10)
    connection.close()
    listener.close()

server_thread = threading.Thread(target=serve)
server_thread.start()
loop = HeldLoop()
asyncio.set_event_loop(loop)
port = listener.getsockname()[1]
session = microvms.Session.direct(f"http://127.0.0.1:{port}", "test-token")

async def prime():
    pending = session.health_async()
    pending.send(None)
    respond.set()
    assert entered.wait(10), "the worker did not enter the Python callback"
    return pending

pending = loop.run_until_complete(prime())

def release_worker():
    can_finish.wait()
    release.set()

releaser = threading.Thread(target=release_worker)
releaser.start()
# Keep the handoff to the native finishing operation on this thread. Its GIL
# release must let the releaser run; no scheduler delay or retry repairs a bug.
sys.setswitchinterval(1000)
can_finish.set()
if finish == "complete":
    try:
        pending.send(None)
    except microvms.RetryableError:
        pass
    else:
        raise AssertionError("the peer closed the request without a response")
elif finish == "cancel":
    pending.close()
else:
    atexit._run_exitfuncs()
    try:
        pending.send(None)
    except RuntimeError as error:
        assert "asynchronous runtime is shutting down" in str(error)
    else:
        raise AssertionError("the closed bridge must refuse the pending await")
assert release.is_set(), "finishing returned before the Python wake could finish"
releaser.join()
server_thread.join()
assert release.is_set()
loop.close()
"""
    result = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", code, finish],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not result.stderr, result.stderr


@pytest.mark.parametrize(
    "kind", ["keep_awake", "tunnel", "port_forward", "sandbox", "agent_vm"]
)
@pytest.mark.parametrize("original_error", [False, True])
def test_context_exit_preserves_exception_precedence_after_shutdown(
    kind: str, original_error: bool
) -> None:
    """Shutdown errors obey each context manager's existing teardown contract."""
    code = r"""
import atexit
import asyncio
import os
import sys
import microvms

os.environ["AWS_ACCESS_KEY_ID"] = "AKIDEXAMPLE"
os.environ["AWS_SECRET_ACCESS_KEY"] = "synthetic-test-secret"
os.environ.pop("AWS_PROFILE", None)
kind, original_error = sys.argv[1], sys.argv[2] == "True"
session = microvms.Session.direct("http://127.0.0.1:9", "test-token")
if kind == "sandbox":
    manager = microvms.Sandbox(microvms.Region.us_east_1())
elif kind == "agent_vm":
    manager = microvms.AgentVm(microvms.Region.us_east_1())
elif kind == "keep_awake":
    manager = session.keep_awake(interval=1)
else:
    manager = getattr(session, kind)(9000)

atexit._run_exitfuncs()

async def main():
    original = ValueError("the body failed") if original_error else None
    try:
        async with manager:
            if original is not None:
                raise original
    except ValueError as error:
        assert error is original
    except RuntimeError as error:
        assert not original_error and kind not in ("sandbox", "agent_vm")
        assert "asynchronous runtime is shutting down" in str(error)
    else:
        assert not original_error and kind in ("sandbox", "agent_vm")

asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", code, kind, str(original_error)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not result.stderr, result.stderr


@pytest.mark.parametrize("kind", ["event", "chunk", "callback"])
def test_cancelling_a_direct_receiver_drains_its_python_wake(kind: str) -> None:
    """Each direct channel poll owns its wake until the Python callback returns."""
    code = r"""
import asyncio
import http.server
import sys
import threading
import microvms

kind = sys.argv[1]
entered = threading.Event()
release = threading.Event()
close_started = threading.Event()
close_finished = threading.Event()
wake_finished = threading.Event()
send_output = threading.Event()
end_stream = threading.Event()
stream_open = threading.Event()
frame = b'event: output\ndata: {"offset":0,"stream":"stdout","output":"eA=="}\n\n'

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        body = b'{"exec_id":"x-async","phase":"running"}'
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        assert "/stream" in self.path, self.path
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        # The withheld byte keeps the drive alive, so only the channel can wake Python.
        self.send_header("Content-Length", str(len(frame) + 1))
        self.end_headers()
        self.wfile.flush()
        stream_open.set()
        assert send_output.wait(10)
        self.wfile.write(frame)
        self.wfile.flush()
        assert end_stream.wait(10)

class HeldLoop(asyncio.SelectorEventLoop):
    def call_soon_threadsafe(self, callback, *args, context=None):
        if threading.current_thread() is not threading.main_thread():
            entered.set()
            assert release.wait(10), "the main thread did not release the worker"
        result = super().call_soon_threadsafe(callback, *args, context=context)
        wake_finished.set()
        return result

server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
server_thread = threading.Thread(target=server.serve_forever)
server_thread.start()
session = microvms.Session.direct(f"http://127.0.0.1:{server.server_port}", "test-token")
loop = HeldLoop()
asyncio.set_event_loop(loop)
callbacks = []
async def prime():
    if kind == "callback":
        pending = session.run_to_completion_async(
            ["true"], on_output=callbacks.append, exec_id="x-async"
        )
    else:
        pending = (owner.stdout if kind == "chunk" else owner).__anext__()
    pending.send(None)
    assert stream_open.wait(10), "the producer did not open its stream"
    send_output.set()
    assert entered.wait(10), "the receiver did not wake Python"
    assert not callbacks
    return pending

closer = None
try:
    if kind == "event":
        owner = session.exec("x-async").stream(idle_timeout=5.0)
    elif kind == "chunk":
        owner = session.spawn(["true"], exec_id="x-async")
    pending = loop.run_until_complete(prime())

    def close_pending():
        close_started.set()
        pending.close()
        close_finished.set()

    # The closer cannot switch Python threads between its start handshake and native
    # close. Observing the handshake therefore means close has released the GIL.
    sys.setswitchinterval(1000)
    closer = threading.Thread(target=close_pending)
    closer.start()
    assert close_started.wait(10), "the closer did not start"
    # This bounded observation keeps the callback blocked throughout. Merely
    # releasing the GIL in an unrelated nested future cannot satisfy the verdict.
    finished_while_held = close_finished.wait(0.2)
finally:
    release.set()
    send_output.set()
    end_stream.set()
    if closer is not None and closer.ident is not None:
        closer.join(10)
    server.shutdown()
    server.server_close()
    server_thread.join()
    loop.close()
assert not closer.is_alive(), "cancellation did not drain after releasing the callback"
assert not finished_while_held, "cancellation returned with a Python callback still held"
assert close_finished.is_set() and wake_finished.is_set()
"""
    result = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", code, kind],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not result.stderr, result.stderr
