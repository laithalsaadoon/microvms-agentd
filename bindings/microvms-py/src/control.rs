// SPDX-License-Identifier: Apache-2.0
//! MicroVM lifecycle and image administration by ID, for a process that holds only an
//! identifier.
//!
//! A thin wrapper over the core's `ControlPlane`: every call is one of the core's own, with
//! its identifier checks and retries. It carries no lifecycle state and so enforces none of
//! the STATE guards a `Sandbox` does — it answers what the service says. A durable workflow
//! replaying in a fresh process, or a reaper sweeping a fleet, is the caller this is for.

use std::sync::Arc;
use std::time::{Duration, UNIX_EPOCH};

use std::collections::BTreeMap;

use microvms_core::control::{ControlPlane, Microvm, MicrovmFilter, WaitOpts, ops};
use microvms_core::prelude::*;
use microvms_core::sandbox::{DEFAULT_DELETE_ATTEMPTS, DEFAULT_DELETE_BACKOFF};
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

/// One `ListMicrovmImages` item: an image's ARN, name and state.
#[pyclass(frozen, name = "ImageSummary", module = "microvms")]
pub struct PyImageSummary {
    inner: ops::MicrovmImageSummaryWire,
}

#[pymethods]
impl PyImageSummary {
    #[getter]
    fn image_arn(&self) -> &str {
        &self.inner.image_arn
    }

    #[getter]
    fn name(&self) -> &str {
        &self.inner.name
    }

    /// As the service spells it, such as `"CREATING"` or `"CREATED"`.
    #[getter]
    fn state(&self) -> &str {
        &self.inner.state
    }

    fn __repr__(&self) -> String {
        format!(
            "ImageSummary(name={:?}, state={:?})",
            self.inner.name, self.inner.state
        )
    }
}

/// One image version as `ListMicrovmImageVersions` or `UpdateMicrovmImageVersion` reads it
/// back: its build state, whether `RunMicrovm` launches it, and what it was built with.
#[pyclass(frozen, name = "ImageVersion", module = "microvms")]
pub struct PyImageVersion {
    inner: ops::MicrovmImageVersionSummaryWire,
}

#[pymethods]
impl PyImageVersion {
    #[getter]
    fn image_arn(&self) -> &str {
        &self.inner.image_arn
    }

    #[getter]
    fn image_version(&self) -> &str {
        &self.inner.image_version
    }

    /// The version's build state, as the service spells it.
    #[getter]
    fn state(&self) -> &str {
        &self.inner.state
    }

    /// `"ACTIVE"` (`RunMicrovm` launches it) or `"INACTIVE"` (it refuses; running VMs keep
    /// running).
    #[getter]
    fn status(&self) -> &str {
        &self.inner.status
    }

    /// Whether `RunMicrovm` launches this version.
    #[getter]
    fn is_active(&self) -> bool {
        self.inner.is_active()
    }

    /// Why the version is in this state, when the service said. A failed build's reason is on
    /// its build (`list_image_builds`), and this one is usually absent.
    #[getter]
    fn state_reason(&self) -> Option<&str> {
        self.inner.state_reason.as_deref()
    }

    /// Unix seconds.
    #[getter]
    fn created_at(&self) -> f64 {
        self.inner.created_at
    }

    /// Unix seconds, when the service reported it.
    #[getter]
    fn updated_at(&self) -> Option<f64> {
        self.inner.updated_at
    }

    #[getter]
    fn base_image_arn(&self) -> &str {
        &self.inner.base_image_arn
    }

    /// The base version the build used, as the service spells it (`"1.0"` where the managed
    /// base lists `"1"`). A record of the build, not a value to pass back as a pin.
    #[getter]
    fn base_image_version(&self) -> Option<&str> {
        self.inner.base_image_version.as_deref()
    }

    #[getter]
    fn build_role_arn(&self) -> &str {
        &self.inner.build_role_arn
    }

    /// `codeArtifact.uri`: the artifact the version was built from.
    #[getter]
    fn code_artifact_uri(&self) -> &str {
        &self.inner.code_artifact.uri
    }

    #[getter]
    fn description(&self) -> Option<&str> {
        self.inner.description.as_deref()
    }

    /// `resources[0].minimumMemoryInMiB`, the list's one member: the size class the version
    /// was built for, and the only place a built image reports it.
    #[getter]
    fn minimum_memory_mib(&self) -> Option<u32> {
        self.inner
            .resources
            .as_ref()
            .and_then(|resources| resources.first())
            .map(|resources| resources.minimum_memory_in_mib)
    }

    #[getter]
    fn egress_network_connectors(&self) -> Option<Vec<String>> {
        self.inner.egress_network_connectors.clone()
    }

    #[getter]
    fn additional_os_capabilities(&self) -> Option<Vec<String>> {
        self.inner.additional_os_capabilities.clone()
    }

    #[getter]
    fn environment_variables(&self) -> Option<BTreeMap<String, String>> {
        self.inner.environment_variables.clone()
    }

    #[getter]
    fn tags(&self) -> Option<BTreeMap<String, String>> {
        self.inner.tags.clone()
    }

    fn __repr__(&self) -> String {
        format!("ImageVersion({})", self.inner.describe())
    }
}

/// One build of an image version: one per Graviton generation, so a version's builds differ
/// in `chipset_generation`. `get_image_build` adds the snapshot sizes the listing lacks.
#[pyclass(frozen, name = "ImageBuild", module = "microvms")]
pub struct PyImageBuild {
    inner: ops::GetImageBuildResponseWire,
}

impl From<ops::MicrovmImageBuildSummaryWire> for PyImageBuild {
    fn from(build: ops::MicrovmImageBuildSummaryWire) -> Self {
        PyImageBuild {
            inner: ops::GetImageBuildResponseWire {
                image_arn: build.image_arn,
                image_version: build.image_version,
                build_id: build.build_id,
                build_state: build.build_state,
                architecture: build.architecture,
                chipset: build.chipset,
                chipset_generation: build.chipset_generation,
                created_at: build.created_at,
                state_reason: build.state_reason,
                snapshot_build: None,
            },
        }
    }
}

#[pymethods]
impl PyImageBuild {
    #[getter]
    fn image_arn(&self) -> &str {
        &self.inner.image_arn
    }

    #[getter]
    fn image_version(&self) -> &str {
        &self.inner.image_version
    }

    /// What `get_image_build` takes, and nothing else in the API mints one.
    #[getter]
    fn build_id(&self) -> &str {
        &self.inner.build_id
    }

    /// `buildState`, as the service spells it.
    #[getter]
    fn build_state(&self) -> &str {
        &self.inner.build_state
    }

    #[getter]
    fn architecture(&self) -> &str {
        &self.inner.architecture
    }

    #[getter]
    fn chipset(&self) -> &str {
        &self.inner.chipset
    }

    #[getter]
    fn chipset_generation(&self) -> &str {
        &self.inner.chipset_generation
    }

    /// Unix seconds.
    #[getter]
    fn created_at(&self) -> f64 {
        self.inner.created_at
    }

    /// Why the build is in this state, when the service said: where a failed build's reason
    /// lives.
    #[getter]
    fn state_reason(&self) -> Option<&str> {
        self.inner.state_reason.as_deref()
    }

    /// `snapshotBuild.memorySnapshotSizeInBytes`, from `get_image_build` only, and only when
    /// the service reported it.
    #[getter]
    fn memory_snapshot_size_in_bytes(&self) -> Option<u64> {
        self.inner
            .snapshot_build
            .and_then(|sizes| sizes.memory_snapshot_size_in_bytes)
    }

    /// `snapshotBuild.codeInstallSizeInBytes`, from `get_image_build` only.
    #[getter]
    fn code_install_size_in_bytes(&self) -> Option<u64> {
        self.inner
            .snapshot_build
            .and_then(|sizes| sizes.code_install_size_in_bytes)
    }

    /// `snapshotBuild.diskSnapshotSizeInBytes`, from `get_image_build` only.
    #[getter]
    fn disk_snapshot_size_in_bytes(&self) -> Option<u64> {
        self.inner
            .snapshot_build
            .and_then(|sizes| sizes.disk_snapshot_size_in_bytes)
    }

    fn __repr__(&self) -> String {
        format!("ImageBuild({})", self.inner.describe())
    }
}

/// MicroVM lifecycle by ID (get, list, suspend, resume, terminate, and wait) and image
/// administration (list, delete, versions and their status, builds).
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
    // Literals, not core's `DEFAULT_LIFECYCLE_TIMEOUT` and `LIFECYCLE_POLL_INTERVAL`: the stub
    // generator writes a named default as `...`, which hides the value from a type checker's
    // hover and from #300's check that reads each default from `microvms.pyi`.
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

    /// `ListMicrovmImages`, every page: every image in the account and region.
    fn list_images(&self, py: Python<'_>) -> PyCoreResult<Vec<PyImageSummary>> {
        let plane = Arc::clone(&self.inner);
        let items = runtime::block_on(py, async move { plane.list_images().await })?;
        Ok(items
            .into_iter()
            .map(|inner| PyImageSummary { inner })
            .collect())
    }

    /// Deletes the image, its extra versions first, retrying while it refuses (an image still
    /// `CREATING`, or one a terminating VM holds).
    ///
    /// Returns `True` once the service took the deletion and `False` when every attempt failed
    /// or `identifier` is not one the service accepts. It doesn't raise, as a teardown's delete
    /// shouldn't. `attempts` and `backoff` (seconds) default to the core's teardown figures.
    #[pyo3(signature = (identifier, *, attempts=None, backoff=None))]
    fn delete_image(
        &self,
        py: Python<'_>,
        identifier: String,
        attempts: Option<u32>,
        backoff: Option<f64>,
    ) -> PyCoreResult<bool> {
        let attempts = attempts.unwrap_or(DEFAULT_DELETE_ATTEMPTS);
        let backoff = match backoff {
            Some(backoff) => seconds(backoff)?,
            None => DEFAULT_DELETE_BACKOFF,
        };
        let plane = Arc::clone(&self.inner);
        Ok(runtime::block_on(py, async move {
            Ok::<_, microvms_core::Error>(plane.delete_image(&identifier, attempts, backoff).await)
        })?)
    }

    /// `ListMicrovmImageVersions`, every page: each version, its status, and its build
    /// configuration.
    fn list_image_versions(
        &self,
        py: Python<'_>,
        identifier: String,
    ) -> PyCoreResult<Vec<PyImageVersion>> {
        let plane = Arc::clone(&self.inner);
        let items = runtime::block_on(
            py,
            async move { plane.list_image_versions(&identifier).await },
        )?;
        Ok(items
            .into_iter()
            .map(|inner| PyImageVersion { inner })
            .collect())
    }

    /// `UpdateMicrovmImageVersion`: `status` is `"ACTIVE"` or `"INACTIVE"`.
    ///
    /// `INACTIVE` is the non-destructive retire: `RunMicrovm` refuses the version, running VMs
    /// keep running, and the version's readback stays. Returns the readback, and raises when it
    /// doesn't carry the status asked for, so a 200 that didn't take isn't a rollback.
    fn set_image_version_status(
        &self,
        py: Python<'_>,
        identifier: String,
        version: String,
        status: &str,
    ) -> PyCoreResult<PyImageVersion> {
        let status: ops::VersionStatus = status.parse()?;
        let plane = Arc::clone(&self.inner);
        let inner = runtime::block_on(py, async move {
            plane
                .set_image_version_status(&identifier, &version, status)
                .await
        })?;
        Ok(PyImageVersion { inner })
    }

    /// `ListMicrovmImageBuilds` for one version, every page: one build per Graviton
    /// generation. Each `build_id` is what `get_image_build` takes.
    fn list_image_builds(
        &self,
        py: Python<'_>,
        identifier: String,
        version: String,
    ) -> PyCoreResult<Vec<PyImageBuild>> {
        let plane = Arc::clone(&self.inner);
        let items = runtime::block_on(py, async move {
            plane.list_image_builds(&identifier, &version).await
        })?;
        Ok(items.into_iter().map(PyImageBuild::from).collect())
    }

    /// `GetMicrovmImageBuild`: one build, with the snapshot sizes the listing doesn't carry.
    fn get_image_build(
        &self,
        py: Python<'_>,
        identifier: String,
        version: String,
        build_id: String,
    ) -> PyCoreResult<PyImageBuild> {
        let plane = Arc::clone(&self.inner);
        let inner = runtime::block_on(py, async move {
            plane
                .get_image_build(&identifier, &version, &build_id)
                .await
        })?;
        Ok(PyImageBuild { inner })
    }

    fn __repr__(&self) -> String {
        format!("ControlPlane(region={:?})", self.inner.region().as_str())
    }
}
