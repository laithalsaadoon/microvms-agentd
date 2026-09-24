// SPDX-License-Identifier: Apache-2.0
//! MicroVM lifecycle by ID, for a process that holds only an identifier.
//!
//! A thin wrapper over the core's `ControlPlane`: every call is one of the core's own, with
//! its identifier checks and retries. It carries no lifecycle state and so enforces none of
//! the STATE guards a `Sandbox` does — it answers what the service says. A durable workflow
//! replaying in a fresh process, or a reaper sweeping a fleet, is the caller this is for.

use std::sync::Arc;
use std::time::{Duration, UNIX_EPOCH};

use microvms_core::control::{ControlPlane, Microvm, MicrovmFilter, WaitOpts};
use pyo3::prelude::*;

use crate::errors::PyCoreResult;
use crate::exec::seconds;
use crate::region::PyRegion;
use crate::runtime;

/// The idle policy the service reports a VM is running under.
#[pyclass(frozen, name = "IdlePolicy", module = "microvms")]
pub struct PyIdlePolicy {
    max_idle_sec: u32,
    suspended_sec: u32,
    auto_resume: bool,
}

#[pymethods]
impl PyIdlePolicy {
    /// `maxIdleDurationSeconds`: inbound-traffic silence before an auto-suspend.
    #[getter]
    fn max_idle_sec(&self) -> u32 {
        self.max_idle_sec
    }

    /// `suspendedDurationSeconds`: how long a suspended VM lasts before it is terminated.
    #[getter]
    fn suspended_sec(&self) -> u32 {
        self.suspended_sec
    }

    /// `autoResumeEnabled`: whether a request to a suspended VM resumes it.
    #[getter]
    fn auto_resume(&self) -> bool {
        self.auto_resume
    }

    fn __repr__(&self) -> String {
        format!(
            "IdlePolicy(max_idle_sec={}, suspended_sec={}, auto_resume={})",
            self.max_idle_sec,
            self.suspended_sec,
            if self.auto_resume { "True" } else { "False" },
        )
    }
}

fn epoch(at: Option<std::time::SystemTime>) -> Option<f64> {
    at.and_then(|at| at.duration_since(UNIX_EPOCH).ok())
        .map(|since| since.as_secs_f64())
}

/// A MicroVM as `GetMicrovm` last described it.
#[pyclass(frozen, name = "Microvm", module = "microvms")]
pub struct PyMicrovm {
    inner: Microvm,
}

#[pymethods]
impl PyMicrovm {
    #[getter]
    fn id(&self) -> &str {
        &self.inner.id
    }

    /// `"PENDING"`, `"RUNNING"`, `"SUSPENDING"`, `"SUSPENDED"`, `"TERMINATING"`, or
    /// `"TERMINATED"`, as the service spells it. Eventually consistent.
    #[getter]
    fn state(&self) -> &str {
        &self.inner.state
    }

    /// Why the VM is in this state, when the service said.
    #[getter]
    fn state_reason(&self) -> Option<&str> {
        self.inner.state_reason.as_deref()
    }

    /// The proxy endpoint. Pair it with the agent token in `Session.attach`.
    #[getter]
    fn endpoint(&self) -> &str {
        &self.inner.endpoint
    }

    #[getter]
    fn image_arn(&self) -> &str {
        &self.inner.image_arn
    }

    #[getter]
    fn image_version(&self) -> &str {
        &self.inner.image_version
    }

    /// The idle policy the service reports, or `None` when it sent none.
    #[getter]
    fn idle_policy(&self) -> Option<PyIdlePolicy> {
        self.inner.idle_policy.as_ref().map(|policy| PyIdlePolicy {
            max_idle_sec: policy.max_idle_duration_seconds,
            suspended_sec: policy.suspended_duration_seconds,
            auto_resume: policy.auto_resume_enabled,
        })
    }

    /// `maximumDurationInSeconds`. Suspended time counts toward it.
    #[getter]
    fn maximum_duration_seconds(&self) -> Option<u32> {
        self.inner.maximum_duration_seconds
    }

    /// When the VM first started, as Unix seconds.
    #[getter]
    fn started_at(&self) -> Option<f64> {
        epoch(self.inner.started_at)
    }

    /// When the VM terminated, as Unix seconds, once it has.
    #[getter]
    fn terminated_at(&self) -> Option<f64> {
        epoch(self.inner.terminated_at)
    }

    fn __repr__(&self) -> String {
        format!(
            "Microvm(id={:?}, state={:?})",
            self.inner.id, self.inner.state
        )
    }
}

/// One `ListMicrovms` item: narrower than `Microvm`, with no endpoint or reason.
#[pyclass(frozen, name = "MicrovmSummary", module = "microvms")]
pub struct PyMicrovmSummary {
    id: String,
    state: String,
    image_arn: String,
    image_version: String,
}

#[pymethods]
impl PyMicrovmSummary {
    #[getter]
    fn id(&self) -> &str {
        &self.id
    }

    #[getter]
    fn state(&self) -> &str {
        &self.state
    }

    #[getter]
    fn image_arn(&self) -> &str {
        &self.image_arn
    }

    #[getter]
    fn image_version(&self) -> &str {
        &self.image_version
    }

    fn __repr__(&self) -> String {
        format!("MicrovmSummary(id={:?}, state={:?})", self.id, self.state)
    }
}

/// MicroVM lifecycle by ID: get, list, suspend, resume, terminate, and wait.
///
/// Holds no lifecycle state, so it checks nothing a `Sandbox` would (STATE-5, STATE-7,
/// STATE-12): a suspend of a SUSPENDED VM is the service's to refuse. Use it when a
/// process has only an identifier, such as a durable workflow step in a fresh process.
#[pyclass(frozen, name = "ControlPlane", module = "microvms")]
pub struct PyControlPlane {
    inner: Arc<ControlPlane>,
}

#[pymethods]
impl PyControlPlane {
    /// Resolves credentials for `region` from the default chain.
    #[new]
    fn new(py: Python<'_>, region: PyRegion) -> PyCoreResult<PyControlPlane> {
        let plane = runtime::block_on(py, ControlPlane::new(region.inner))?;
        Ok(PyControlPlane {
            inner: Arc::new(plane),
        })
    }

    /// `GetMicrovm`.
    fn get(&self, py: Python<'_>, microvm_id: String) -> PyCoreResult<PyMicrovm> {
        let plane = Arc::clone(&self.inner);
        let inner = runtime::block_on(py, async move { plane.get_microvm(&microvm_id).await })?;
        Ok(PyMicrovm { inner })
    }

    /// `ListMicrovms`, every page, optionally narrowed to one image and version.
    #[pyo3(signature = (*, image_identifier=None, image_version=None))]
    fn list(
        &self,
        py: Python<'_>,
        image_identifier: Option<String>,
        image_version: Option<String>,
    ) -> PyCoreResult<Vec<PyMicrovmSummary>> {
        let plane = Arc::clone(&self.inner);
        let filter = MicrovmFilter {
            image_identifier,
            image_version,
        };
        let items = runtime::block_on(
            py,
            async move { plane.list_microvms_matching(&filter).await },
        )?;
        Ok(items
            .into_iter()
            .map(|item| PyMicrovmSummary {
                id: item.microvm_id,
                state: item.state,
                image_arn: item.image_arn,
                image_version: item.image_version,
            })
            .collect())
    }

    /// `SuspendMicrovm`. Returns once accepted; `wait_for_state` for SUSPENDED.
    fn suspend(&self, py: Python<'_>, microvm_id: String) -> PyCoreResult<()> {
        let plane = Arc::clone(&self.inner);
        Ok(runtime::block_on(py, async move {
            plane.suspend(&microvm_id).await
        })?)
    }

    /// `ResumeMicrovm`. Returns once accepted; `wait_for_state` for RUNNING.
    fn resume(&self, py: Python<'_>, microvm_id: String) -> PyCoreResult<()> {
        let plane = Arc::clone(&self.inner);
        Ok(runtime::block_on(py, async move {
            plane.resume(&microvm_id).await
        })?)
    }

    /// `TerminateMicrovm`. Returns once accepted; `wait_for_state` for TERMINATED.
    fn terminate(&self, py: Python<'_>, microvm_id: String) -> PyCoreResult<()> {
        let plane = Arc::clone(&self.inner);
        Ok(runtime::block_on(py, async move {
            plane.terminate(&microvm_id).await
        })?)
    }

    /// Polls `GetMicrovm` until the state is one of `wanted`.
    ///
    /// Reaching one of `fail_on` first raises `LaunchDiedError` naming the state and
    /// `stateReason`; running past `timeout` raises `TimeoutError`.
    #[pyo3(signature = (microvm_id, wanted, *, fail_on=None, timeout=300.0, poll_interval=5.0))]
    fn wait_for_state(
        &self,
        py: Python<'_>,
        microvm_id: String,
        wanted: Vec<String>,
        fail_on: Option<Vec<String>>,
        timeout: f64,
        poll_interval: f64,
    ) -> PyCoreResult<PyMicrovm> {
        let opts = WaitOpts {
            timeout: seconds(timeout)?,
            poll_interval: seconds(poll_interval)?,
            stall_grace: Duration::MAX,
        };
        let plane = Arc::clone(&self.inner);
        let fail_on = fail_on.unwrap_or_default();
        let inner = runtime::block_on(py, async move {
            let wanted: Vec<&str> = wanted.iter().map(String::as_str).collect();
            let fail_on: Vec<&str> = fail_on.iter().map(String::as_str).collect();
            plane
                .wait_for_state(&microvm_id, &wanted, &fail_on, opts)
                .await
        })?;
        Ok(PyMicrovm { inner })
    }

    fn __repr__(&self) -> String {
        format!("ControlPlane(region={:?})", self.inner.region().as_str())
    }
}
