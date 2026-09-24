// SPDX-License-Identifier: Apache-2.0
//! `Session.keep_awake`: the core's keepalive, running on the binding's runtime.
//!
//! The poll loop and the background task are the core's
//! ([`microvms_core::session::KeepAwakeTask`]); this module only chooses the session and
//! the gate. Dropping the handle stops the task, so a keepalive cannot outlive the object
//! that owns it unnoticed.

use std::sync::{Mutex, PoisonError};
use std::time::Duration;

use microvms_core::Error;
use microvms_core::sandbox::{Lifecycle, Sandbox};
use microvms_core::session::{KeepAwake, KeepAwakeReport, KeepAwakeTask, RunningGate, Session};
use pyo3::prelude::*;
use tokio::sync::watch;

use crate::errors::PyCoreResult;
use crate::runtime;

/// What a finished keepalive did.
#[pyclass(frozen, name = "KeepAwakeReport", module = "microvms")]
pub struct PyKeepAwakeReport {
    inner: KeepAwakeReport,
}

#[pymethods]
impl PyKeepAwakeReport {
    /// Why it ended: `"stopped"`, `"idle"`, `"elapsed"`, or `"not-running"`.
    #[getter]
    fn end(&self) -> &'static str {
        self.inner.end.as_str()
    }

    /// Health polls that answered.
    #[getter]
    fn polls(&self) -> u64 {
        self.inner.polls
    }

    /// `busy` from the last answered poll, or `None` when none answered.
    #[getter]
    fn last_busy(&self) -> Option<bool> {
        self.inner.last_busy
    }

    /// Seconds from start to end.
    #[getter]
    fn elapsed_sec(&self) -> f64 {
        self.inner.elapsed.as_secs_f64()
    }

    fn __repr__(&self) -> String {
        format!(
            "KeepAwakeReport(end={:?}, polls={}, last_busy={:?}, elapsed_sec={:.1})",
            self.inner.end.as_str(),
            self.inner.polls,
            self.inner.last_busy,
            self.inner.elapsed.as_secs_f64()
        )
    }
}

/// A running keepalive. Use as a context manager, or call `stop()`.
///
/// Keep a reference: dropping this object stops the keepalive.
#[pyclass(frozen, name = "KeepAwake", module = "microvms")]
pub struct PyKeepAwake {
    task: KeepAwakeTask,
}

/// Where the keepalive reads the session from.
pub(crate) enum Source {
    Owned(Session),
    /// A sandbox's session, cloned once, gated on the sandbox's lifecycle watch.
    ///
    /// Not the sandbox lock: a sandbox busy inside a long exec holds it for the whole
    /// call, which is exactly when the keepalive has to keep polling. The watch is what
    /// makes a suspend or terminate through the sandbox end the keepalive instead of being
    /// undone by its next poll auto-resuming the VM.
    InSandbox(Session, watch::Receiver<Lifecycle>),
}

impl Source {
    /// The sandbox-held source and its launch idle window, or `None` without a session.
    pub(crate) fn in_sandbox(sandbox: &Mutex<Sandbox>) -> Option<(Self, Option<Duration>)> {
        let guard = sandbox.lock().unwrap_or_else(PoisonError::into_inner);
        let session = guard.session()?.clone();
        Some((
            Self::InSandbox(session, guard.watch_lifecycle()),
            guard.idle_window(),
        ))
    }
}

impl PyKeepAwake {
    /// Validates `policy` and starts polling at once on the binding's runtime.
    pub(crate) fn start(source: Source, policy: KeepAwake) -> Result<Self, Error> {
        let (session, running): (Session, Option<RunningGate>) = match source {
            Source::Owned(session) => (session, None),
            Source::InSandbox(session, lifecycle) => (
                session,
                Some(Box::new(move || *lifecycle.borrow() == Lifecycle::Running)),
            ),
        };
        let _runtime = runtime::handle().enter();
        Ok(Self {
            task: policy.spawn(session, running)?,
        })
    }
}

#[pymethods]
impl PyKeepAwake {
    /// Whether the keepalive is still polling.
    #[getter]
    fn running(&self) -> bool {
        self.task.is_running()
    }

    /// Stops polling and returns the report. Raises the poll's error if one ended it.
    fn stop(&self, py: Python<'_>) -> PyCoreResult<PyKeepAwakeReport> {
        self.task.request_stop();
        let inner = runtime::block_on(py, self.task.finished())?;
        Ok(PyKeepAwakeReport { inner })
    }

    /// Waits for the keepalive to end on its own (`while_busy` or `max_duration`).
    ///
    /// Returns `None` if it is still running when `timeout` seconds pass.
    #[pyo3(signature = (timeout=None))]
    fn wait(
        &self,
        py: Python<'_>,
        timeout: Option<f64>,
    ) -> PyCoreResult<Option<PyKeepAwakeReport>> {
        let timeout = timeout.map(crate::exec::seconds).transpose()?;
        let finished = runtime::block_on(py, async {
            match timeout {
                Some(limit) => tokio::time::timeout(limit, self.task.finished()).await.ok(),
                None => Some(self.task.finished().await),
            }
        });
        Ok(finished
            .transpose()?
            .map(|inner| PyKeepAwakeReport { inner }))
    }

    fn __enter__(slf: Py<Self>) -> Py<Self> {
        slf
    }

    /// Stops the keepalive. A poll error does not mask an exception already in flight.
    #[pyo3(signature = (exc_type=None, exc_value=None, traceback=None))]
    fn __exit__(
        &self,
        py: Python<'_>,
        exc_type: Option<Py<PyAny>>,
        exc_value: Option<Py<PyAny>>,
        traceback: Option<Py<PyAny>>,
    ) -> PyCoreResult<bool> {
        let _ = (exc_value, traceback);
        let stopped = self.stop(py);
        if exc_type.is_none() {
            stopped?;
        }
        Ok(false)
    }

    fn __repr__(&self) -> String {
        format!("KeepAwake(running={})", self.running())
    }
}

/// The policy from the binding's keyword arguments.
pub(crate) fn policy(
    idle_window: Option<Duration>,
    interval: Option<f64>,
    while_busy: bool,
    max_duration: Option<f64>,
) -> Result<KeepAwake, Error> {
    let mut policy = KeepAwake::new(idle_window).while_busy(while_busy);
    if let Some(interval) = interval {
        policy = policy.interval(crate::exec::seconds(interval)?);
    }
    let max_duration = max_duration.map(crate::exec::seconds).transpose()?;
    Ok(policy.max_duration(max_duration))
}
