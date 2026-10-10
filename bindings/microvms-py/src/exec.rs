// SPDX-License-Identifier: Apache-2.0
//! One exec, and the stream as a Python iterator.
//!
//! # The stream is the one shape that needed real work
//!
//! Everything else on this surface is "call an async method, block on it". A stream is
//! not: driving one means holding a future across a Python `__next__` that has to return
//! between items, and a borrow of the handle cannot outlive the call.
//!
//! The shape that works is a task and a channel. [`ExecStream::new`] spawns the stream's
//! driver onto the shared runtime with an owned [`microvms_core::session::ExecHandle`]
//! and a bounded `mpsc` sender, and `__next__` blocks on `recv`. The bound is 1, which is
//! deliberate: the daemon's SSE body is the backpressure signal, and an unbounded channel
//! would buffer a fast producer's whole output in the binding while the Python consumer
//! fell behind — which is the failure the core's byte-offset cursor exists to make
//! unnecessary.
//!
//! Dropping the iterator drops the receiver, the next `send` fails, and the drive ends on
//! `ControlFlow::Break`. That is what `for event in handle.stream(): break` has to do, and
//! it is why the task owns everything it touches rather than borrowing from the handle.
//!
//! The same object is an async iterator: `async for event in handle.stream()` awaits the
//! same `recv` from a coroutine instead of blocking on it. tokio's channel is runtime
//! agnostic, so the coroutine polls it on the event loop's thread and the sender's wake
//! reaches the loop through pyo3's waker; the drive itself stays on the shared runtime.
//!
//! # Events are classes, not tuples
//!
//! `ExecEvent` in the core is an enum with three shapes. A Python caller gets three
//! classes plus a `kind` tag, so `isinstance` and a `kind` check both work, and the
//! `Exit` event stays distinguishable from an output chunk that happens to be last —
//! which the core's docs call out as the difference between a finished command and a cut
//! connection.
//!
//! # The stream is driven by core's async callback driver
//!
//! [`ExecStream::new`]'s task calls `microvms_core::session::ExecHandle::for_each_event_async`
//! and `.await`s its capacity-1 `send` inside the callback, where `ControlFlow::Break` is the
//! dropped-iterator case. The `Stream` path — `stream_with` plus `StreamExt::next` — was
//! retired here on 2026-08-09, and `futures-util` came out of this crate's manifest with it.
//! The sync driver could not have served this: its only available send is `blocking_send`,
//! which would park the runtime worker the driver runs on, and with capacity 1 that is every
//! event the Python consumer has not drained yet.
//!
//! [`crate::cost`]'s `by_phase` took the same shape of fix at a smaller scale: core grew
//! `CostPhase::from_str` and both bindings' hand-rolled phase tables came out.

use std::future::Future;
use std::sync::Arc;
use std::time::Duration;

use microvms_core::session::{ExecEvent, ExecHandle, ExecResult, StreamOptions};
use pyo3::prelude::*;
use pyo3::types::PyBytes;

use crate::errors::{PyCoreResult, to_py_err};
use crate::runtime;

/// The default wait for `wait`/`wait_and_ack`, from the core so the two cannot drift.
const DEFAULT_WAIT: f64 = microvms_core::session::DEFAULT_EXEC_WAIT.as_secs_f64();

/// An exec's phase and, once it has one, its outcome.
///
/// `stdout` and `stderr` are `str` because the daemon's `Outcome` carries them as strings
/// — the protocol crate's shape, not a choice made here. `exit_code` and `signal` are
/// `None` rather than sentinel integers, which is the same distinction `models.py` makes:
/// a signal death has no exit code, and zero is not "no signal".
#[pyclass(frozen, name = "ExecResult", module = "microvms")]
pub struct PyExecResult {
    exec_id: String,
    phase: &'static str,
    exit_code: Option<i32>,
    signal: Option<i32>,
    stdout: String,
    stderr: String,
    truncated: bool,
    timed_out: bool,
    writers_may_be_alive: bool,
    done: bool,
    succeeded: bool,
    posix_exit_code: Option<i32>,
    notes: Vec<String>,
    synthesized: bool,
}

impl PyExecResult {
    pub(crate) fn wrap(result: ExecResult) -> Self {
        Self {
            phase: result.phase.as_str(),
            exit_code: result.exit_code(),
            signal: result.outcome.as_ref().and_then(|outcome| outcome.signal),
            stdout: result.stdout().to_string(),
            stderr: result.stderr().to_string(),
            timed_out: result
                .outcome
                .as_ref()
                .is_some_and(|outcome| outcome.timed_out),
            truncated: result
                .outcome
                .as_ref()
                .is_some_and(|outcome| outcome.truncated),
            writers_may_be_alive: result
                .outcome
                .as_ref()
                .is_some_and(|outcome| outcome.writers_may_be_alive),
            done: result.done(),
            succeeded: result.succeeded(),
            posix_exit_code: result.posix_exit_code(),
            notes: result.notes(),
            synthesized: result.synthesized(),
            exec_id: result.exec_id,
        }
    }
}

#[pymethods]
impl PyExecResult {
    #[getter]
    fn exec_id(&self) -> &str {
        &self.exec_id
    }

    /// `"running"`, `"exited"`, or `"acked"`.
    #[getter]
    fn phase(&self) -> &'static str {
        self.phase
    }

    /// `None` when the child died to a signal rather than exiting.
    #[getter]
    fn exit_code(&self) -> Option<i32> {
        self.exit_code
    }

    /// The signal that killed the child, when one did.
    #[getter]
    fn signal(&self) -> Option<i32> {
        self.signal
    }

    #[getter]
    fn stdout(&self) -> &str {
        &self.stdout
    }

    #[getter]
    fn stderr(&self) -> &str {
        &self.stderr
    }

    /// Set when either stream hit the output cap and was cut. A flag rather than a
    /// sentinel inside the bytes, which would be indistinguishable from output that
    /// happens to contain it.
    #[getter]
    fn truncated(&self) -> bool {
        self.truncated
    }

    /// True when the daemon execution deadline expired, distinct from cancellation.
    #[getter]
    fn timed_out(&self) -> bool {
        self.timed_out
    }

    /// Set when the post-exit linger deadline expired with the pipes still open: some
    /// grandchild is alive and may write more that nobody will see.
    #[getter]
    fn writers_may_be_alive(&self) -> bool {
        self.writers_may_be_alive
    }

    /// Whether the exec has finished, whichever way.
    #[getter]
    fn done(&self) -> bool {
        self.done
    }

    /// Whether the command exited zero. False for a signal death and for a still-running
    /// exec, since neither is a success.
    #[getter]
    fn ok(&self) -> bool {
        self.succeeded
    }

    /// The exit code a POSIX shell would report (BIND-6): 124 when a deadline ended the
    /// command (the daemon's, or `run_to_completion`'s client deadline), 128 plus the signal
    /// for any other signal death, otherwise `exit_code`. `None` only for a running exec.
    #[getter]
    fn posix_exit_code(&self) -> Option<i32> {
        self.posix_exit_code
    }

    /// Human-readable annotations, one per condition that changes how the output reads
    /// (BIND-7): truncation at the output cap, an expired deadline, writers left alive, a
    /// synthesized result. Empty for a clean result; append them to stderr as they are.
    #[getter]
    fn notes(&self) -> Vec<String> {
        self.notes.clone()
    }

    /// True when `run_to_completion` synthesized this result because nothing came back
    /// after its client-deadline kill (BIND-10): `posix_exit_code` is 124 and the output is
    /// unknown, not empty.
    #[getter]
    fn synthesized(&self) -> bool {
        self.synthesized
    }

    fn __repr__(&self) -> String {
        format!(
            "ExecResult(exec_id={:?}, phase={:?}, exit_code={:?}, posix_exit_code={:?})",
            self.exec_id, self.phase, self.exit_code, self.posix_exit_code
        )
    }
}

/// What a stdin write accomplished.
#[pyclass(frozen, name = "StdinAck", module = "microvms")]
pub struct PyStdinAck {
    exec_id: String,
    written: usize,
    eof: bool,
}

impl PyStdinAck {
    fn wrap(ack: protocol::exec::StdinResponse) -> Self {
        Self {
            exec_id: ack.exec_id,
            written: ack.written,
            eof: ack.eof,
        }
    }
}

#[pymethods]
impl PyStdinAck {
    #[getter]
    fn exec_id(&self) -> &str {
        &self.exec_id
    }

    #[getter]
    fn written(&self) -> usize {
        self.written
    }

    #[getter]
    fn eof(&self) -> bool {
        self.eof
    }

    fn __repr__(&self) -> String {
        format!(
            "StdinAck(exec_id={:?}, written={}, eof={})",
            self.exec_id, self.written, self.eof
        )
    }
}

/// Output bytes, with the offset they start at.
///
/// `data` is `bytes` and not `str`: exec output is arbitrary bytes, and a decode here
/// would be a lossy step the caller cannot see. `end` is where a cursor resumes.
#[pyclass(frozen, name = "OutputChunk", module = "microvms")]
pub struct PyOutputChunk {
    stream: &'static str,
    offset: u64,
    data: Vec<u8>,
}

#[pymethods]
impl PyOutputChunk {
    /// `"output"` — the tag beside `isinstance`, so a caller can branch either way.
    #[getter]
    fn kind(&self) -> &'static str {
        "output"
    }

    /// `"stdout"` or `"stderr"`. Both share one offset space, so a caller holds one
    /// cursor rather than two that can disagree about ordering.
    #[getter]
    fn stream(&self) -> &'static str {
        self.stream
    }

    #[getter]
    fn offset(&self) -> u64 {
        self.offset
    }

    #[getter]
    fn data<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        PyBytes::new(py, &self.data)
    }

    /// One past this chunk's last byte: `offset + len(data)`.
    #[getter]
    fn end(&self) -> u64 {
        self.offset + self.data.len() as u64
    }

    /// The bytes as text, replacing anything undecodable.
    ///
    /// A method rather than a getter on `data`, so the lossy step is a call a reader sees
    /// — the same reasoning as the core's `break_even_seconds_f64`.
    fn text(&self) -> String {
        String::from_utf8_lossy(&self.data).into_owned()
    }

    fn __repr__(&self) -> String {
        format!(
            "OutputChunk(stream={:?}, offset={}, len={})",
            self.stream,
            self.offset,
            self.data.len()
        )
    }
}

/// A byte range that is gone for good — the replay ring evicted it, or this subscriber
/// lagged the live channel.
///
/// A typed event rather than a log line, because the alternative is reading a truncated
/// log as a complete one. `start` is inclusive and `end` exclusive, so `end` is where a
/// cursor resumes.
#[pyclass(frozen, name = "Gap", module = "microvms")]
pub struct PyGap {
    start: u64,
    end: u64,
}

#[pymethods]
impl PyGap {
    #[getter]
    fn kind(&self) -> &'static str {
        "gap"
    }

    #[getter]
    fn start(&self) -> u64 {
        self.start
    }

    #[getter]
    fn end(&self) -> u64 {
        self.end
    }

    #[getter]
    fn size(&self) -> u64 {
        self.end.saturating_sub(self.start)
    }

    fn __repr__(&self) -> String {
        format!("Gap(start={}, end={})", self.start, self.end)
    }
}

/// The terminal event. Its **absence** is what distinguishes a cut connection from a
/// finished command — the byte sequences are otherwise identical.
#[pyclass(frozen, name = "Exit", module = "microvms")]
pub struct PyExit {
    timed_out: bool,
    exit_code: Option<i32>,
    signal: Option<i32>,
    truncated: bool,
    writers_may_be_alive: bool,
    offset: u64,
}

#[pymethods]
impl PyExit {
    /// True when the remote execution deadline expired.
    #[getter]
    fn timed_out(&self) -> bool {
        self.timed_out
    }

    #[getter]
    fn kind(&self) -> &'static str {
        "exit"
    }

    #[getter]
    fn exit_code(&self) -> Option<i32> {
        self.exit_code
    }

    #[getter]
    fn signal(&self) -> Option<i32> {
        self.signal
    }

    #[getter]
    fn truncated(&self) -> bool {
        self.truncated
    }

    #[getter]
    fn writers_may_be_alive(&self) -> bool {
        self.writers_may_be_alive
    }

    /// Total bytes published. A total rather than a position to resume at, which is why
    /// `Exit` has no `end` getter while `Output` and `Gap` both do.
    #[getter]
    fn offset(&self) -> u64 {
        self.offset
    }

    fn __repr__(&self) -> String {
        format!(
            "Exit(exit_code={:?}, signal={:?}, offset={})",
            self.exit_code, self.signal, self.offset
        )
    }
}

/// One stream event as the class for its shape: what `ExecStream.__next__` yields and
/// `run_to_completion`'s `on_output` receives.
///
/// An enum rather than a `Py<PyAny>`, so the stub says `OutputChunk | Gap | Exit` where it
/// said `Any`: pyo3 introspects a derived `IntoPyObject` enum as the union of its variants,
/// and a typed caller's `isinstance` narrowing then reaches each class's getters (#337).
#[derive(IntoPyObject)]
pub(crate) enum StreamEvent {
    Output(PyOutputChunk),
    Gap(PyGap),
    Exit(PyExit),
}

impl From<ExecEvent> for StreamEvent {
    fn from(event: ExecEvent) -> Self {
        match event {
            ExecEvent::Output {
                stream,
                offset,
                data,
            } => StreamEvent::Output(PyOutputChunk {
                stream: stream.as_str(),
                offset,
                data,
            }),
            ExecEvent::Gap { from, to } => StreamEvent::Gap(PyGap {
                start: from,
                end: to,
            }),
            ExecEvent::Exit(exit) => StreamEvent::Exit(PyExit {
                timed_out: exit.timed_out,
                exit_code: exit.exit_code,
                signal: exit.signal,
                truncated: exit.truncated,
                writers_may_be_alive: exit.writers_may_be_alive,
                offset: exit.offset,
            }),
        }
    }
}

/// The receiving end of a stream's channel, shared between an iterator's `__next__` and its
/// `__anext__`.
type EventReceiver =
    Arc<tokio::sync::Mutex<tokio::sync::mpsc::Receiver<Result<ExecEvent, microvms_core::Error>>>>;

/// A Python iterator, and async iterator, over an exec's output.
///
/// See the module docs for why this is a task and a bounded channel rather than a stored
/// future. `receiver` is behind a `Mutex` because `#[pyclass]` methods take `&self` when
/// the class is shared, and `recv` needs `&mut`; the lock is held only across one `recv`
/// and never across a Python callback, so it cannot deadlock against the GIL. tokio's, so a
/// coroutine can hold it across the `recv` it awaits.
#[pyclass(frozen, name = "ExecStream", module = "microvms")]
pub struct ExecStream {
    receiver: EventReceiver,
}

impl ExecStream {
    /// Spawns the stream's consumer and returns the iterator that drains it.
    ///
    /// `handle` arrives as an `Arc` rather than by value because
    /// [`microvms_core::session::ExecHandle`] is neither `Clone` nor constructible outside
    /// its own crate — see the note in the packet — so the only way for the task to own
    /// something it can call `stream_with` on is to share the one handle. The borrow the
    /// stream takes lives inside the `async move` block, which owns the `Arc`.
    fn new(handle: Arc<ExecHandle>, options: StreamOptions) -> Self {
        // Capacity 1: the SSE body is the backpressure signal, and buffering a fast
        // producer here would defeat the cursor the core reconnects at.
        let (sender, receiver) = tokio::sync::mpsc::channel(1);
        runtime::handle().spawn(async move {
            let end = handle
                .for_each_event_async(options, |event| {
                    // Cloned per event rather than borrowed: core's callback future is a plain
                    // type parameter, which cannot name a borrow of this closure's captures —
                    // see `for_each_event_async`'s docs for why that signature and not
                    // `AsyncFnMut`. One atomic increment per event.
                    let sender = sender.clone();
                    async move {
                        // `.await`ed, not `blocking_send`ed. With capacity 1 the channel is
                        // full whenever the Python consumer is even slightly behind, and
                        // blocking here would park the runtime worker this driver runs on —
                        // which is the whole reason core grew the async overload.
                        match sender.send(Ok(event)).await {
                            Ok(()) => std::ops::ControlFlow::Continue(()),
                            // The Python iterator was dropped. `Break` ends the drive, which
                            // is what makes `break` out of a `for` loop stop the stream rather
                            // than leave a task reading a body nobody reads.
                            Err(_) => std::ops::ControlFlow::Break(()),
                        }
                    }
                })
                .await;
            // A stream error is delivered as an item so `__next__` raises it. The events
            // already sent stay sent: the bytes a caller received are real output, and the
            // asymmetry is the driver's own documented one.
            if let Err(error) = end {
                let _ = sender.send(Err(error)).await;
            }
        });
        Self {
            receiver: Arc::new(tokio::sync::Mutex::new(receiver)),
        }
    }
}

#[pymethods]
impl ExecStream {
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    /// The next event, or `StopIteration` when the stream ends.
    ///
    /// Blocks with the GIL released, so another Python thread can run while this one
    /// waits on the daemon.
    ///
    /// The end is raised rather than returned as `None`, which pyo3 would also turn into
    /// `StopIteration`, because the stub is read off this return type: `None` would put
    /// `| None` in an event loop's element type, where no `None` is ever yielded.
    fn __next__(&self, py: Python<'_>) -> PyResult<StreamEvent> {
        let receiver = Arc::clone(&self.receiver);
        let received =
            py.detach(|| runtime::block_on_detached(async { receiver.lock().await.recv().await }));
        match received {
            Some(Ok(event)) => Ok(event.into()),
            Some(Err(error)) => Err(to_py_err(py, &error)),
            None => Err(pyo3::exceptions::PyStopIteration::new_err(())),
        }
    }

    fn __aiter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    /// The next event, awaited, or `StopAsyncIteration` when the stream ends: `async for`.
    ///
    /// Cancelling the await leaves the event unread rather than lost: the channel keeps it
    /// until the next `__anext__` or `__next__`.
    fn __anext__<'py>(&self, py: Python<'py>) -> PyResult<runtime::Awaitable<'py, StreamEvent>> {
        let next = Bound::new(
            py,
            NextEvent {
                receiver: Arc::clone(&self.receiver),
            },
        )?;
        runtime::Awaitable::call(next.as_any(), "recv")
    }
}

/// The awaitable `ExecStream.__anext__` answers: one `recv`, as a pyo3 coroutine.
///
/// A class of its own because pyo3 0.29 turns an `async fn` into a coroutine for an ordinary
/// method and not for the `__anext__` slot, which must return an awaitable. It isn't in the
/// module, so it has no name a caller types or a stub prints.
#[pyclass(frozen, name = "ExecStreamNext", module = "microvms")]
struct NextEvent {
    receiver: EventReceiver,
}

#[pymethods]
impl NextEvent {
    async fn recv(&self) -> PyResult<StreamEvent> {
        let received = self.receiver.lock().await.recv().await;
        match received {
            Some(Ok(event)) => Ok(event.into()),
            Some(Err(error)) => Err(Python::attach(|py| to_py_err(py, &error))),
            None => Err(pyo3::exceptions::PyStopAsyncIteration::new_err(())),
        }
    }
}

/// One exec, addressed by its caller-minted id.
///
/// The id is the idempotency key, so a handle survives a process restart: rebuild it
/// through `Session.exec(exec_id)` and every method still addresses the same server-side
/// exec.
#[pyclass(frozen, name = "ExecHandle", module = "microvms")]
pub struct PyExecHandle {
    /// `Arc` because [`ExecStream`] needs an owned handle for its task and cloning the
    /// core handle is a clone of an `Arc<Transport>` plus a `String`.
    inner: Arc<ExecHandle>,
}

impl PyExecHandle {
    pub(crate) fn wrap(inner: ExecHandle) -> Self {
        Self {
            inner: Arc::new(inner),
        }
    }

    // The futures each method's two spellings drive. Each owns a clone of the handle's `Arc`,
    // so the awaitable twin can run it as a task on the shared runtime.

    fn poll_op(
        &self,
    ) -> impl Future<Output = Result<PyExecResult, microvms_core::Error>> + Send + 'static {
        let inner = Arc::clone(&self.inner);
        async move { inner.poll().await.map(PyExecResult::wrap) }
    }

    fn wait_op(
        &self,
        timeout: Duration,
    ) -> impl Future<Output = Result<PyExecResult, microvms_core::Error>> + Send + 'static {
        let inner = Arc::clone(&self.inner);
        async move { inner.wait(timeout).await.map(PyExecResult::wrap) }
    }

    /// Generic over the bytes, so the blocking spelling lends `bytes` and the awaitable one
    /// hands over an owned `PyBackedBytes`, with no copy on either path.
    fn write_stdin_op<D>(
        &self,
        data: D,
        eof: bool,
    ) -> impl Future<Output = Result<PyStdinAck, microvms_core::Error>> + Send + use<D>
    where
        D: AsRef<[u8]> + Send,
    {
        let inner = Arc::clone(&self.inner);
        async move {
            inner
                .write_stdin(data.as_ref(), eof)
                .await
                .map(PyStdinAck::wrap)
        }
    }

    fn close_stdin_op(
        &self,
    ) -> impl Future<Output = Result<PyStdinAck, microvms_core::Error>> + Send + 'static {
        let inner = Arc::clone(&self.inner);
        async move { inner.close_stdin().await.map(PyStdinAck::wrap) }
    }

    fn ack_op(
        &self,
    ) -> impl Future<Output = Result<PyExecResult, microvms_core::Error>> + Send + 'static {
        let inner = Arc::clone(&self.inner);
        async move { inner.ack().await.map(PyExecResult::wrap) }
    }

    fn kill_op(&self) -> impl Future<Output = Result<bool, microvms_core::Error>> + Send + 'static {
        let inner = Arc::clone(&self.inner);
        async move { inner.kill().await }
    }

    fn wait_and_ack_op(
        &self,
        timeout: Duration,
    ) -> impl Future<Output = Result<PyExecResult, microvms_core::Error>> + Send + 'static {
        let inner = Arc::clone(&self.inner);
        async move { inner.wait_and_ack(timeout).await.map(PyExecResult::wrap) }
    }
}

#[pymethods]
impl PyExecHandle {
    #[getter]
    fn exec_id(&self) -> &str {
        self.inner.exec_id()
    }

    /// Reads current status and output. Read-only server-side; safe to spin on.
    fn poll(&self, py: Python<'_>) -> PyCoreResult<PyExecResult> {
        Ok(runtime::block_on(py, self.poll_op())?)
    }

    /// The awaitable twin of `poll`.
    async fn poll_async(&self) -> PyCoreResult<PyExecResult> {
        Ok(runtime::spawn(self.poll_op()).await?)
    }

    /// Polls until the exec is done, or raises `TimeoutError`.
    ///
    /// A timeout has not touched the exec — polling is read-only and output lives until
    /// it is acked — so a caller that gives up can come back and poll again.
    #[pyo3(signature = (timeout=DEFAULT_WAIT))]
    fn wait(&self, py: Python<'_>, timeout: f64) -> PyCoreResult<PyExecResult> {
        let timeout = seconds(timeout)?;
        Ok(runtime::block_on(py, self.wait_op(timeout))?)
    }

    /// The awaitable twin of `wait`. Cancelling it is giving up as a timeout does: the exec
    /// is untouched.
    #[pyo3(signature = (timeout=DEFAULT_WAIT))]
    async fn wait_async(&self, timeout: f64) -> PyCoreResult<PyExecResult> {
        let timeout = seconds(timeout)?;
        Ok(runtime::spawn(self.wait_op(timeout)).await?)
    }

    /// An iterator over output as it arrives, reconnecting at the last good offset.
    ///
    /// `error_on_gap=True` turns an evicted byte range into an exception instead of a
    /// `Gap` event, which is what a caller that must have complete output wants.
    /// `reconnect=False` ends the iterator at a cut instead, for a caller doing its own
    /// reconnection.
    #[pyo3(signature = (
        *,
        offset=0,
        reconnect=true,
        max_reconnects=None,
        error_on_gap=false,
        idle_timeout=None,
    ))]
    fn stream(
        &self,
        offset: u64,
        reconnect: bool,
        max_reconnects: Option<u32>,
        error_on_gap: bool,
        idle_timeout: Option<f64>,
    ) -> PyCoreResult<ExecStream> {
        // Unset knobs fall back to the core's `StreamOptions::default()` rather than to
        // numbers written here: the reconnect budget and the keepalive-derived idle window
        // are the core's measurements, and a second copy of them in a binding is a second
        // thing to keep in step (the JS binding defers the same way).
        let defaults = StreamOptions::default();
        let options = StreamOptions {
            offset,
            reconnect,
            max_reconnects: max_reconnects.unwrap_or(defaults.max_reconnects),
            error_on_gap,
            idle_timeout: match idle_timeout {
                Some(idle) => seconds(idle)?,
                None => defaults.idle_timeout,
            },
        };
        Ok(ExecStream::new(Arc::clone(&self.inner), options))
    }

    /// Writes to the child's stdin. Requires the exec to have been started with
    /// `stdin=True`, or the daemon answers 409.
    ///
    /// `eof` in the same call is the common case for feeding a prompt: two round trips
    /// would leave a window where the child has the bytes but not the EOF that says the
    /// input is complete.
    #[pyo3(signature = (data, *, eof=false))]
    fn write_stdin(&self, py: Python<'_>, data: &[u8], eof: bool) -> PyCoreResult<PyStdinAck> {
        Ok(runtime::block_on(py, self.write_stdin_op(data, eof))?)
    }

    /// The awaitable twin of `write_stdin`.
    #[pyo3(signature = (data, *, eof=false))]
    async fn write_stdin_async(&self, data: Py<PyBytes>, eof: bool) -> PyCoreResult<PyStdinAck> {
        let data = Python::attach(|py| pyo3::pybacked::PyBackedBytes::from(data.into_bound(py)));
        Ok(runtime::spawn(self.write_stdin_op(data, eof)).await?)
    }

    /// Sends EOF. Nothing else closes stdin: the daemon's copy of the pipe outlives the
    /// child's wait, so a child blocked reading stdin hangs until its timeout otherwise.
    fn close_stdin(&self, py: Python<'_>) -> PyCoreResult<PyStdinAck> {
        Ok(runtime::block_on(py, self.close_stdin_op())?)
    }

    /// The awaitable twin of `close_stdin`.
    async fn close_stdin_async(&self) -> PyCoreResult<PyStdinAck> {
        Ok(runtime::spawn(self.close_stdin_op()).await?)
    }

    /// Releases the buffered output and starts the TTL clock.
    fn ack(&self, py: Python<'_>) -> PyCoreResult<PyExecResult> {
        Ok(runtime::block_on(py, self.ack_op())?)
    }

    /// The awaitable twin of `ack`.
    async fn ack_async(&self) -> PyCoreResult<PyExecResult> {
        Ok(runtime::spawn(self.ack_op()).await?)
    }

    /// Signals the whole process group. `False` means nothing was signalled because the
    /// child had already been reaped — which is the outcome a kill wanted.
    fn kill(&self, py: Python<'_>) -> PyCoreResult<bool> {
        Ok(runtime::block_on(py, self.kill_op())?)
    }

    /// The awaitable twin of `kill`.
    async fn kill_async(&self) -> PyCoreResult<bool> {
        Ok(runtime::spawn(self.kill_op()).await?)
    }

    /// Wait, then ack, returning the result that carries the output.
    ///
    /// Which result comes back matters: the ack response carries the released output and
    /// a poll issued after the ack reports `acked` with none, so returning the wrong one
    /// is a silent empty-output bug. The core sequences it.
    #[pyo3(signature = (timeout=DEFAULT_WAIT))]
    fn wait_and_ack(&self, py: Python<'_>, timeout: f64) -> PyCoreResult<PyExecResult> {
        let timeout = seconds(timeout)?;
        Ok(runtime::block_on(py, self.wait_and_ack_op(timeout))?)
    }

    /// The awaitable twin of `wait_and_ack`. Cancelled during the wait it leaves the exec
    /// untouched; cancelled during the ack, the ack may have landed.
    #[pyo3(signature = (timeout=DEFAULT_WAIT))]
    async fn wait_and_ack_async(&self, timeout: f64) -> PyCoreResult<PyExecResult> {
        let timeout = seconds(timeout)?;
        Ok(runtime::spawn(self.wait_and_ack_op(timeout)).await?)
    }

    fn __repr__(&self) -> String {
        format!("ExecHandle(exec_id={:?})", self.exec_id())
    }
}

/// A [`Duration`] from a caller's float seconds.
///
/// The core's `duration::of_secs_f64` is what refuses a negative or non-finite figure.
/// This is a call, not a check, which is the BIND-2 rule: the refusal and its message
/// stay in one place, and a wait's names no cost report (#338).
pub(crate) fn seconds(value: f64) -> Result<Duration, microvms_core::Error> {
    microvms_core::duration::of_secs_f64(value)
}
