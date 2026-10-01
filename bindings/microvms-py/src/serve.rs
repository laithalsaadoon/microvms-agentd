// SPDX-License-Identifier: Apache-2.0
//! `Session.tunnel` and `Session.port_forward`: core's serving loops, running on the binding's
//! runtime, and the tunnel identity a verified tunnel checks the far end against.
//!
//! The loops, the stop, the grace, the counting and the list of connections that didn't end
//! clean are the core's ([`microvms_core::session::serve`]); this module holds the handle and
//! converts the report. Dropping a handle stops its loop and cuts its open connections, so a
//! relay can't outlive the object that owns it.

use microvms_core::identity::TunnelIdentity;
use microvms_core::session::serve::{
    ConnectionEnd, ForwardSummary, Serving, StopReason, TunnelSummary,
};
use pyo3::prelude::*;

use crate::errors::{CoreError, PyCoreResult};
use crate::exec::seconds;
use crate::runtime;

/// What a launcher keeps to verify its VM: the host's secret seed and the VM's public key,
/// both base64.
///
/// From `Sandbox.tunnel_identity` after a `run(identity=True)`, from a `NameRecord`, or built
/// from the two values `microvm run --identity` prints. Holds a secret: `host_seed` stays out
/// of `repr`; store it only where the agent token goes.
#[pyclass(frozen, from_py_object, name = "TunnelIdentity", module = "microvms")]
#[derive(Clone)]
pub struct PyTunnelIdentity {
    pub(crate) inner: TunnelIdentity,
}

impl From<TunnelIdentity> for PyTunnelIdentity {
    fn from(inner: TunnelIdentity) -> Self {
        Self { inner }
    }
}

#[pymethods]
impl PyTunnelIdentity {
    /// Rebuilds the pair from its base64 spellings. Refuses a value that doesn't decode, or a
    /// seed or key of the wrong length.
    #[new]
    fn new(host_seed: &str, vm_public_key: &str) -> PyCoreResult<Self> {
        Ok(Self {
            inner: TunnelIdentity::from_encoded_parts(host_seed, vm_public_key)
                .map_err(CoreError)?,
        })
    }

    /// The host's secret half, base64. Store only privately; never in repr.
    #[getter]
    fn host_seed(&self) -> String {
        self.inner.host_seed_base64()
    }

    /// The VM's public key, base64: the pin. Safe to print and compare.
    #[getter]
    fn vm_public_key(&self) -> String {
        self.inner.vm_public_key_base64()
    }

    fn __eq__(&self, other: &Self) -> bool {
        self.inner.host_seed() == other.inner.host_seed()
            && self.inner.vm_public_key() == other.inner.vm_public_key()
    }

    fn __repr__(&self) -> String {
        format!(
            "TunnelIdentity(vm_public_key={:?}, host_seed=<redacted>)",
            self.inner.vm_public_key_base64()
        )
    }
}

/// `stop()`'s grace, from its `timeout` keyword.
fn grace(timeout: Option<f64>) -> PyCoreResult<Option<std::time::Duration>> {
    timeout.map(seconds).transpose().map_err(CoreError)
}

/// A connection that didn't end clean.
#[pyclass(frozen, name = "ConnectionEnd", module = "microvms")]
pub struct PyConnectionEnd {
    inner: ConnectionEnd,
}

#[pymethods]
impl PyConnectionEnd {
    /// The local client's address, `host:port`.
    #[getter]
    fn peer(&self) -> String {
        self.inner.peer.to_string()
    }

    /// `"refused"`, `"truncated"`, `"unproven"`, or `"failed"`.
    #[getter]
    fn kind(&self) -> &'static str {
        self.inner.kind.as_str()
    }

    /// The close code or the HTTP status, when the end carried one.
    #[getter]
    fn code(&self) -> Option<u16> {
        self.inner.code
    }

    /// The daemon's reason, the forwarder's explanation, or the error.
    #[getter]
    fn detail(&self) -> &str {
        &self.inner.detail
    }

    fn __repr__(&self) -> String {
        format!(
            "ConnectionEnd(peer={:?}, kind={:?}, code={:?})",
            self.inner.peer.to_string(),
            self.inner.kind.as_str(),
            self.inner.code
        )
    }
}

fn ended(ends: &[ConnectionEnd]) -> Vec<PyConnectionEnd> {
    ends.iter()
        .map(|end| PyConnectionEnd { inner: end.clone() })
        .collect()
}

fn stopped(reason: &StopReason) -> String {
    match reason {
        StopReason::Requested => "stopped".to_string(),
        StopReason::Limit => "limit".to_string(),
        StopReason::ListenerFailed(error) => format!("listener-failed: {error}"),
    }
}

/// What a stopped tunnel did.
#[pyclass(frozen, name = "TunnelReport", module = "microvms")]
pub struct PyTunnelReport {
    inner: TunnelSummary,
}

#[pymethods]
impl PyTunnelReport {
    /// Connections accepted.
    #[getter]
    fn served(&self) -> u32 {
        self.inner.report.served
    }

    /// Connections the daemon refused, or that failed with an error.
    #[getter]
    fn refused(&self) -> u32 {
        self.inner.report.refused
    }

    /// Verified connections that ended without the daemon's end of stream, so their stream may
    /// have been cut short.
    #[getter]
    fn truncated(&self) -> u32 {
        self.inner.report.truncated
    }

    /// Verified connections into a daemon from before the end of stream, whose end nothing
    /// proved.
    #[getter]
    fn unproven(&self) -> u32 {
        self.inner.report.unproven
    }

    /// Proxy tokens the session minted by the time the tunnel stopped.
    #[getter]
    fn proxy_token_mints(&self) -> u64 {
        self.inner.report.proxy_token_mints
    }

    /// Why it stopped: `"stopped"`, `"limit"`, or `"listener-failed: <why>"`.
    #[getter]
    fn stopped(&self) -> String {
        stopped(&self.inner.report.stopped)
    }

    /// Each connection that didn't end clean, in the order they ended.
    #[getter]
    fn ended(&self) -> Vec<PyConnectionEnd> {
        ended(&self.inner.ended)
    }

    fn __repr__(&self) -> String {
        let report = &self.inner.report;
        format!(
            "TunnelReport(served={}, refused={}, truncated={}, unproven={}, stopped={:?})",
            report.served,
            report.refused,
            report.truncated,
            report.unproven,
            stopped(&report.stopped)
        )
    }
}

/// What a stopped port-forward did.
#[pyclass(frozen, name = "PortForwardReport", module = "microvms")]
pub struct PyPortForwardReport {
    inner: ForwardSummary,
}

#[pymethods]
impl PyPortForwardReport {
    /// Connections accepted.
    #[getter]
    fn served(&self) -> u32 {
        self.inner.report.served
    }

    /// Exchanges the endpoint proxy refused, a 403 or a 502 among them.
    #[getter]
    fn refused(&self) -> u32 {
        self.inner.report.refused
    }

    /// Exchanges that upgraded, a WebSocket among them.
    #[getter]
    fn upgrades(&self) -> u32 {
        self.inner.report.upgrades
    }

    /// Proxy tokens the session minted by the time the forward stopped.
    #[getter]
    fn proxy_token_mints(&self) -> u64 {
        self.inner.report.proxy_token_mints
    }

    /// Why it stopped: `"stopped"`, `"limit"`, or `"listener-failed: <why>"`.
    #[getter]
    fn stopped(&self) -> String {
        stopped(&self.inner.report.stopped)
    }

    /// Each connection that didn't end clean, in the order they ended.
    #[getter]
    fn ended(&self) -> Vec<PyConnectionEnd> {
        ended(&self.inner.ended)
    }

    fn __repr__(&self) -> String {
        let report = &self.inner.report;
        format!(
            "PortForwardReport(served={}, refused={}, upgrades={}, stopped={:?})",
            report.served,
            report.refused,
            report.upgrades,
            stopped(&report.stopped)
        )
    }
}

/// A running tunnel. Use as a context manager, or call `stop()`.
///
/// Keep a reference: dropping this object stops the tunnel and cuts its open connections.
#[pyclass(frozen, name = "Tunnel", module = "microvms")]
pub struct PyTunnel {
    task: Serving<TunnelSummary>,
}

impl PyTunnel {
    pub(crate) fn new(task: Serving<TunnelSummary>) -> Self {
        Self { task }
    }
}

#[pymethods]
impl PyTunnel {
    /// The local address to connect to, `host:port`, with the port the OS picked.
    #[getter]
    fn local_address(&self) -> String {
        self.task.local_address().to_string()
    }

    /// Whether the tunnel is still serving.
    #[getter]
    fn running(&self) -> bool {
        self.task.is_running()
    }

    /// Stops accepting, waits for the connections still open to end, and returns the report.
    ///
    /// With `timeout`, connections still open after that many seconds are cut and listed as
    /// `"failed"`. Without it, a client that keeps its connection open keeps this waiting.
    /// Callable again, with the same report.
    #[pyo3(signature = (timeout=None))]
    fn stop(&self, py: Python<'_>, timeout: Option<f64>) -> PyCoreResult<PyTunnelReport> {
        let grace = grace(timeout)?;
        let inner = runtime::block_on(py, self.task.stop(grace))?;
        Ok(PyTunnelReport { inner })
    }

    fn __enter__(slf: Py<Self>) -> Py<Self> {
        slf
    }

    /// Stops the tunnel, waiting for its open connections. An error stopping it doesn't mask
    /// an exception already in flight.
    #[pyo3(signature = (exc_type=None, exc_value=None, traceback=None))]
    fn __exit__(
        &self,
        py: Python<'_>,
        exc_type: Option<Py<PyAny>>,
        exc_value: Option<Py<PyAny>>,
        traceback: Option<Py<PyAny>>,
    ) -> PyCoreResult<bool> {
        let _ = (exc_value, traceback);
        let stopped = self.stop(py, None);
        if exc_type.is_none() {
            stopped?;
        }
        Ok(false)
    }

    fn __repr__(&self) -> String {
        format!(
            "Tunnel(local_address={:?}, running={})",
            self.task.local_address().to_string(),
            self.task.is_running()
        )
    }
}

/// A running port-forward. Use as a context manager, or call `stop()`.
///
/// Keep a reference: dropping this object stops the forward and cuts its open connections.
#[pyclass(frozen, name = "PortForward", module = "microvms")]
pub struct PyPortForward {
    task: Serving<ForwardSummary>,
}

impl PyPortForward {
    pub(crate) fn new(task: Serving<ForwardSummary>) -> Self {
        Self { task }
    }
}

#[pymethods]
impl PyPortForward {
    /// The local address to connect to, `host:port`, with the port the OS picked.
    #[getter]
    fn local_address(&self) -> String {
        self.task.local_address().to_string()
    }

    /// Whether the forward is still serving.
    #[getter]
    fn running(&self) -> bool {
        self.task.is_running()
    }

    /// Stops accepting, waits for the connections still open to end, and returns the report.
    ///
    /// With `timeout`, connections still open after that many seconds are cut and listed as
    /// `"failed"`. Without it, a client that keeps its connection open keeps this waiting.
    /// Callable again, with the same report.
    #[pyo3(signature = (timeout=None))]
    fn stop(&self, py: Python<'_>, timeout: Option<f64>) -> PyCoreResult<PyPortForwardReport> {
        let grace = grace(timeout)?;
        let inner = runtime::block_on(py, self.task.stop(grace))?;
        Ok(PyPortForwardReport { inner })
    }

    fn __enter__(slf: Py<Self>) -> Py<Self> {
        slf
    }

    /// Stops the forward, waiting for its open connections. An error stopping it doesn't mask
    /// an exception already in flight.
    #[pyo3(signature = (exc_type=None, exc_value=None, traceback=None))]
    fn __exit__(
        &self,
        py: Python<'_>,
        exc_type: Option<Py<PyAny>>,
        exc_value: Option<Py<PyAny>>,
        traceback: Option<Py<PyAny>>,
    ) -> PyCoreResult<bool> {
        let _ = (exc_value, traceback);
        let stopped = self.stop(py, None);
        if exc_type.is_none() {
            stopped?;
        }
        Ok(false)
    }

    fn __repr__(&self) -> String {
        format!(
            "PortForward(local_address={:?}, running={})",
            self.task.local_address().to_string(),
            self.task.is_running()
        )
    }
}
