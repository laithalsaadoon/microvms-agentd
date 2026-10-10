// SPDX-License-Identifier: Apache-2.0
//! One exec as two byte iterators, a `wait()`, and an idempotent `kill()`: `Session.spawn`.
//!
//! # Two iterators over core's split
//!
//! The daemon publishes stdout and stderr into one SSE stream that shares one offset space,
//! so the byte cursor stays one. Core's `ExecHandle::split` runs that one drive, reconnects
//! and all, and routes each output frame into a stdout or a stderr channel; it also attributes
//! a gap to a stream and applies the gap policy (`microvms-app`'s `session/split.rs` has the
//! reasons). This module spawns the drive on the shared runtime and hands each channel to
//! Python as a [`PyByteStream`], an iterator of `bytes`. Node's `ExecProcess` reads the same
//! channels, so the two bindings split a stream the same way by construction.
//!
//! # A gap raises, by default
//!
//! Under the default policy an evicted byte range raises `PlatformError` (wire kind
//! `OutputGap`) from **both** iterators, naming the range, because the wire can't say which
//! side lost the bytes. The obvious consumer, `for chunk in proc.stdout`, can't miss an
//! exception, and a swallowed gap would hand it a log with a hole in it. `gap_policy="event"`
//! records the range on [`PyExecProcess::gaps`] instead and keeps both iterators going.
//!
//! # Read both sides
//!
//! Each side holds one unread chunk, the backpressure the daemon's SSE body gives. A caller
//! that reads one iterator to its end before touching the other stalls once the other holds a
//! chunk and more arrives for it, the way two pipes do; read them from two threads when a
//! command writes much to both. `wait()` reads the daemon's exec record and needs neither.

use std::future::Future;
use std::sync::Arc;

use microvms_core::session::{
    DEFAULT_EXEC_WAIT, ExecHandle, GapLog, GapPolicy, SplitItem, StreamOptions,
};
use pyo3::prelude::*;
use pyo3::types::PyBytes;

use crate::errors::{PyCoreResult, to_py_err};
use crate::exec::{PyExecResult, seconds};
use crate::runtime;

/// One side of a spawned exec's output, as an iterator of `bytes`.
///
/// Ends with `StopIteration` after the command's `exit`, and raises the error that ended the
/// side otherwise: a gap under the default policy, or a failure the stream couldn't reconnect
/// through. The `receiver` is behind a `Mutex` for the reason `ExecStream`'s is: `recv` needs
/// `&mut`, and the lock is held only across one `recv`, never across a Python callback.
#[pyclass(frozen, name = "ByteStream", module = "microvms")]
pub struct PyByteStream {
    receiver: Arc<tokio::sync::Mutex<tokio::sync::mpsc::Receiver<SplitItem>>>,
}

impl PyByteStream {
    fn new(receiver: tokio::sync::mpsc::Receiver<SplitItem>) -> Self {
        Self {
            receiver: Arc::new(tokio::sync::Mutex::new(receiver)),
        }
    }
}

#[pymethods]
impl PyByteStream {
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    /// The next chunk, or `StopIteration` when the side ends.
    ///
    /// Blocks with the GIL released, so another Python thread (the one reading the other side)
    /// runs while this one waits on the daemon. The end is raised rather than returned as
    /// `None`, as `ExecStream`'s is, so the stub's element type is `bytes` and not
    /// `bytes | None`.
    fn __next__(&self, py: Python<'_>) -> PyResult<Py<PyBytes>> {
        let receiver = Arc::clone(&self.receiver);
        let received =
            py.detach(|| runtime::block_on_detached(async { receiver.lock().await.recv().await }));
        match received {
            Some(Ok(chunk)) => Ok(PyBytes::new(py, &chunk).unbind()),
            Some(Err(error)) => Err(to_py_err(py, &error)),
            None => Err(pyo3::exceptions::PyStopIteration::new_err(())),
        }
    }

    fn __aiter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    /// The next chunk, awaited, or `StopAsyncIteration` when the side ends: `async for`.
    /// Cancelling the await leaves the chunk unread, as `ExecStream.__anext__` does.
    fn __anext__<'py>(&self, py: Python<'py>) -> PyResult<runtime::Awaitable<'py, Py<PyBytes>>> {
        let next = Bound::new(
            py,
            NextChunk {
                receiver: Arc::clone(&self.receiver),
            },
        )?;
        runtime::Awaitable::call(next.as_any(), "recv")
    }
}

/// The awaitable `ByteStream.__anext__` answers, for `ExecStreamNext`'s reason. Not in the
/// module either.
#[pyclass(frozen, name = "ByteStreamNext", module = "microvms")]
struct NextChunk {
    receiver: Arc<tokio::sync::Mutex<tokio::sync::mpsc::Receiver<SplitItem>>>,
}

#[pymethods]
impl NextChunk {
    async fn recv(&self) -> PyResult<Py<PyBytes>> {
        let received = self.receiver.lock().await.recv().await;
        Python::attach(|py| match received {
            Some(Ok(chunk)) => Ok(PyBytes::new(py, &chunk).unbind()),
            Some(Err(error)) => Err(to_py_err(py, &error)),
            None => Err(pyo3::exceptions::PyStopAsyncIteration::new_err(())),
        })
    }
}

/// One byte range the daemon couldn't replay, under `gap_policy="event"`.
///
/// `start` inclusive and `end` exclusive, as on the stream's `Gap` event, so `end` is the
/// offset a resume passes.
#[pyclass(frozen, name = "OutputGap", module = "microvms")]
pub struct PyOutputGap {
    stream: Option<&'static str>,
    start: u64,
    end: u64,
}

#[pymethods]
impl PyOutputGap {
    /// `"stdout"` or `"stderr"`: the side of the output that followed the gap, whose log has
    /// the hole. `None` when the stream ended on the gap, with nothing after it to name one.
    #[getter]
    fn stream(&self) -> Option<&'static str> {
        self.stream
    }

    #[getter]
    fn start(&self) -> u64 {
        self.start
    }

    #[getter]
    fn end(&self) -> u64 {
        self.end
    }

    fn __repr__(&self) -> String {
        format!(
            "OutputGap(stream={:?}, start={}, end={})",
            self.stream, self.start, self.end
        )
    }
}

/// A running exec as two byte iterators, a `wait()`, and an idempotent `kill()`.
///
/// Built by `Session.spawn`, never by a constructor: a process with no exec behind it is one
/// whose every method fails in a way that looks like a dead VM.
#[pyclass(frozen, name = "ExecProcess", module = "microvms")]
pub struct PyExecProcess {
    handle: Arc<ExecHandle>,
    stdout: Py<PyByteStream>,
    stderr: Py<PyByteStream>,
    gaps: GapLog,
}

impl PyExecProcess {
    /// Splits `handle`'s output and starts the drive that fills both sides.
    ///
    /// Started here rather than on the first read, so a process a caller kills without reading
    /// a byte still has its stream attached.
    pub(crate) fn start(
        py: Python<'_>,
        handle: ExecHandle,
        options: StreamOptions,
        policy: GapPolicy,
    ) -> PyResult<Self> {
        let handle = Arc::new(handle);
        let split = Arc::clone(&handle).split(options, policy);
        runtime::handle().spawn(split.drive);
        Ok(Self {
            handle,
            stdout: Py::new(py, PyByteStream::new(split.stdout))?,
            stderr: Py::new(py, PyByteStream::new(split.stderr))?,
            gaps: split.gaps,
        })
    }
}

impl PyExecProcess {
    fn wait_op(
        &self,
        timeout: std::time::Duration,
    ) -> impl Future<Output = Result<PyExecResult, microvms_core::Error>> + Send + 'static {
        let handle = Arc::clone(&self.handle);
        async move { handle.wait(timeout).await.map(PyExecResult::wrap) }
    }

    fn kill_op(&self) -> impl Future<Output = Result<bool, microvms_core::Error>> + Send + 'static {
        let handle = Arc::clone(&self.handle);
        async move { handle.kill().await }
    }
}

#[pymethods]
impl PyExecProcess {
    /// The exec id, which is the idempotency key: `Session.exec(exec_id)` reattaches to it.
    #[getter]
    fn exec_id(&self) -> &str {
        self.handle.exec_id()
    }

    /// The child's standard output. The same iterator every time, since two over one side
    /// would split its bytes between them.
    #[getter]
    fn stdout(&self, py: Python<'_>) -> Py<PyByteStream> {
        self.stdout.clone_ref(py)
    }

    /// The child's standard error. Order is kept within each side and isn't recoverable
    /// between them; `ExecHandle.stream()` has the interleaving.
    #[getter]
    fn stderr(&self, py: Python<'_>) -> Py<PyByteStream> {
        self.stderr.clone_ref(py)
    }

    /// Every byte range the daemon couldn't replay, under `gap_policy="event"`. Empty under
    /// the default policy, where a gap raises from both iterators instead.
    #[getter]
    fn gaps(&self) -> Vec<PyOutputGap> {
        self.gaps
            .snapshot()
            .into_iter()
            .map(|gap| PyOutputGap {
                stream: gap.stream.map(|stream| stream.as_str()),
                start: gap.from,
                end: gap.to,
            })
            .collect()
    }

    /// Polls the daemon's exec record until the command is done, or raises `TimeoutError`.
    ///
    /// From the record, not from the iterators ending: a stream that stopped carrying bytes
    /// is the same observation for a cut connection and a finished command. A timeout hasn't
    /// touched the exec, so a caller can wait again. `timeout` defaults to `ExecHandle.wait`'s.
    #[pyo3(signature = (timeout=None))]
    fn wait(&self, py: Python<'_>, timeout: Option<f64>) -> PyCoreResult<PyExecResult> {
        let timeout = timeout
            .map(seconds)
            .transpose()?
            .unwrap_or(DEFAULT_EXEC_WAIT);
        Ok(runtime::block_on(py, self.wait_op(timeout))?)
    }

    /// The awaitable twin of `wait`. Cancelling it leaves the exec untouched.
    #[pyo3(signature = (timeout=None))]
    async fn wait_async(&self, timeout: Option<f64>) -> PyCoreResult<PyExecResult> {
        let timeout = timeout
            .map(seconds)
            .transpose()?
            .unwrap_or(DEFAULT_EXEC_WAIT);
        Ok(runtime::spawn(self.wait_op(timeout)).await?)
    }

    /// Signals the whole process group. Idempotent: `False` means nothing was signalled
    /// because the group was already gone, which is the outcome a kill wanted, so a caller
    /// can call this in a `finally` without guarding it.
    fn kill(&self, py: Python<'_>) -> PyCoreResult<bool> {
        Ok(runtime::block_on(py, self.kill_op())?)
    }

    /// The awaitable twin of `kill`.
    async fn kill_async(&self) -> PyCoreResult<bool> {
        Ok(runtime::spawn(self.kill_op()).await?)
    }

    fn __repr__(&self) -> String {
        format!("ExecProcess(exec_id={:?})", self.handle.exec_id())
    }
}
