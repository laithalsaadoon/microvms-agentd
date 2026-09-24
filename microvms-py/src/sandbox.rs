// SPDX-License-Identifier: Apache-2.0
//! One MicroVM's whole life.
//!
//! # The sandbox is one object, and that is why it wraps cleanly
//!
//! `microvms-core`'s `Sandbox` is runtime-checked rather than typestate, and T-W3-6 chose
//! that *for this file*: a `Sandbox<Running>` returning a `Suspended` handle is stronger in
//! Rust, but a type whose identity changes on every transition cannot be one `#[pyclass]`,
//! so it would be re-erased into a runtime check here and the check would exist twice —
//! with the binding's copy being the one every Python caller hits. What survives the choice
//! is the part that costs nothing: the state check happens **before** the wire call, so a
//! suspend from SUSPENDED is refused with zero control-plane calls rather than answered by
//! AWS.
//!
//! # Every guard below belongs to the core
//!
//! There is no state check in this file. `run` twice, `suspend` from PENDING, `resume` past
//! the window, `resume` after `terminate` — every one of those is refused by the core's own
//! transition, with the core's own message naming the STATE requirement and the
//! `docs/PLATFORM.md` finding. A copy here would be the copy nothing else tests (BIND-2).
//!
//! # There is no context manager that tears down
//!
//! `__enter__`/`__exit__` are here and `__exit__` calls `terminate()`, matching
//! `sandbox.py`. That is a deliberate difference from the Rust core, which has **no** `Drop`
//! that tears down — `Drop` cannot await, so a teardown there would deadlock inside a
//! runtime or race the process exit. Python's `with` is not `Drop`: `__exit__` runs
//! synchronously on the calling thread, which is exactly where a blocking teardown belongs,
//! so the context manager is available here and is not a loosening.
//!
//! # `terminate` returns a report and never raises
//!
//! It runs where a caller's `finally` would, and an exception raised there replaces the real
//! failure with a teardown failure. `TeardownReport.undeleted` names what was left behind,
//! including the build log group, which this client **cannot** delete — CloudWatch is not in
//! the core's dependency set — so asking names it rather than removing it.

use std::sync::{Arc, Mutex, PoisonError};

use microvms_core::SizeClass;
use microvms_core::control::{BaseImage, CreateImageRequest};
use microvms_core::sandbox::{Detached, RunRequest, Sandbox, TeardownOpts, TeardownReport};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};

use crate::cost::PySizeClass;
use crate::errors::{CoreError, PyCoreResult};
use crate::exec::seconds;
use crate::hooks::{PyBuildHookTimeout, PyRunHookTimeout};
use crate::region::PyRegion;
use crate::runtime;
use crate::session::PySession;

/// What another process needs to adopt a VM handed off by `Sandbox.detach()`.
///
/// Pass the fields to `Sandbox.adopt(region, microvm_id, endpoint, agent_token, port=port)`.
/// `agent_token` is a credential: keep it (or `to_dict()`) in private encrypted storage. It
/// never appears in repr.
#[pyclass(frozen, name = "Detached", module = "microvms")]
pub struct PyDetached {
    pub(crate) inner: Detached,
}

#[pymethods]
impl PyDetached {
    /// The VM's identifier.
    #[getter]
    fn microvm_id(&self) -> String {
        self.inner.microvm_id.clone()
    }

    /// The HTTPS endpoint its daemon answers on.
    #[getter]
    fn endpoint(&self) -> String {
        self.inner.endpoint.clone()
    }

    /// The region the VM runs in.
    #[getter]
    fn region(&self) -> PyRegion {
        PyRegion {
            inner: self.inner.region.clone(),
        }
    }

    /// The daemon port the endpoint's proxy tokens are minted for.
    #[getter]
    fn port(&self) -> u16 {
        self.inner.port
    }

    /// The bearer the VM's daemon accepts. Store only in a private encrypted record.
    #[getter]
    fn agent_token(&self) -> String {
        self.inner.agent_token().to_string()
    }

    /// Every field, token included, as a JSON-safe dict for a private store.
    fn to_dict<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let dict = PyDict::new(py);
        dict.set_item("microvm_id", &self.inner.microvm_id)?;
        dict.set_item("endpoint", &self.inner.endpoint)?;
        dict.set_item("region", self.inner.region.as_str())?;
        dict.set_item("port", self.inner.port)?;
        dict.set_item("agent_token", self.inner.agent_token())?;
        Ok(dict)
    }

    fn __repr__(&self) -> String {
        format!(
            "Detached(microvm_id={:?}, endpoint={:?}, region={:?}, port={}, agent_token=<redacted>)",
            self.inner.microvm_id,
            self.inner.endpoint,
            self.inner.region.as_str(),
            self.inner.port
        )
    }
}

/// What `Sandbox.ensure_image` returns: the ready image and how this call got it.
#[pyclass(frozen, name = "EnsuredImage", module = "microvms")]
pub struct PyEnsuredImage {
    image: microvms_core::control::Image,
    reused: bool,
    artifact_uri: String,
    uploaded: bool,
    warnings: Vec<String>,
}

#[pymethods]
impl PyEnsuredImage {
    /// The ready image; pass `image.identifier` to `run`.
    #[getter]
    fn image(&self) -> PyImage {
        PyImage::wrap(&self.image)
    }

    /// True when this call's own create did not build the image: it was ready, a build
    /// already running was waited out, or a concurrent caller won the create race.
    #[getter]
    fn reused(&self) -> bool {
        self.reused
    }

    /// `s3://<bucket>/<prefix>/<name>/artifact.zip`, whether or not this call uploaded it.
    #[getter]
    fn artifact_uri(&self) -> &str {
        &self.artifact_uri
    }

    /// Whether this call uploaded the artifact.
    #[getter]
    fn uploaded(&self) -> bool {
        self.uploaded
    }

    /// What reading the build context skipped (symlinks, special files), one line each.
    #[getter]
    fn warnings(&self) -> Vec<String> {
        self.warnings.clone()
    }

    fn __repr__(&self) -> String {
        format!(
            "EnsuredImage(name={:?}, reused={}, uploaded={})",
            self.image.name,
            if self.reused { "True" } else { "False" },
            if self.uploaded { "True" } else { "False" },
        )
    }
}

/// A built image, and the log group the service created alongside it.
#[pyclass(frozen, name = "Image", module = "microvms")]
pub struct PyImage {
    identifier: String,
    name: String,
    version: String,
    state: String,
    size: SizeClass,
    build_log_group: String,
    log_stream: Option<String>,
}

impl PyImage {
    pub(crate) fn wrap(image: &microvms_core::control::Image) -> Self {
        Self {
            identifier: image.identifier.clone(),
            name: image.name.clone(),
            version: image.version.clone(),
            state: image.state.clone(),
            size: image.size,
            build_log_group: image.build_log_group(),
            log_stream: image.log_stream.clone(),
        }
    }
}

#[pymethods]
impl PyImage {
    /// The image ARN, which is what `imageIdentifier` takes.
    #[getter]
    fn identifier(&self) -> &str {
        &self.identifier
    }

    #[getter]
    fn name(&self) -> &str {
        &self.name
    }

    #[getter]
    fn version(&self) -> &str {
        &self.version
    }

    #[getter]
    fn state(&self) -> &str {
        &self.state
    }

    /// The class the requested baseline selected.
    ///
    /// Carried on the image because billing follows the baseline requested at *create*
    /// time, and by the time anyone asks what a run cost the request is gone.
    #[getter]
    fn size(&self) -> PySizeClass {
        PySizeClass { inner: self.size }
    }

    /// `/aws/lambda-microvms/<image-name>`.
    ///
    /// The service creates this itself, so no Terraform stack owns it and
    /// `terraform destroy` leaves it behind — "the stack destroyed cleanly" is not "the
    /// account is clean". Six accumulated before anyone noticed.
    #[getter]
    fn build_log_group(&self) -> &str {
        &self.build_log_group
    }

    /// The resolved exact log stream this build's logs went to, when `log_stream` was
    /// configured on the build — the configured prefix plus the per-build `/<16 hex>`
    /// discriminator the client appends. `None` for an unconfigured build (the service
    /// names the streams randomly).
    ///
    /// This is the only copy of the resolved name: the discriminator is minted fresh
    /// inside the create call, so a caller who wants to read their build's logs reads it
    /// here or never.
    #[getter]
    fn log_stream(&self) -> Option<&str> {
        self.log_stream.as_deref()
    }

    fn __repr__(&self) -> String {
        format!(
            "Image(identifier={:?}, state={:?})",
            self.identifier, self.state
        )
    }
}

/// What a teardown did, and what it left behind.
///
/// Returned rather than raised — see the module docs.
#[pyclass(frozen, name = "TeardownReport", module = "microvms")]
pub struct PyTeardownReport {
    inner: TeardownReport,
}

#[pymethods]
impl PyTeardownReport {
    /// Identifiers of everything a caller asked to have deleted that still exists.
    ///
    /// Identifiers rather than a boolean, because a leak nobody can name is a leak nobody
    /// can clean up. Two things land here: a delete that was attempted and failed, and the
    /// build **log group**, which this client cannot delete at all.
    #[getter]
    fn undeleted(&self) -> Vec<String> {
        self.inner.undeleted.clone()
    }

    /// Whether the terminate call was accepted.
    #[getter]
    fn terminate_accepted(&self) -> bool {
        self.inner.terminate_accepted
    }

    /// Whether the image was deleted, or `None` when deletion was not asked for.
    #[getter]
    fn image_deleted(&self) -> Option<bool> {
        self.inner.image_deleted
    }

    /// The lifecycle state the sandbox ended in.
    ///
    /// Commonly `"TERMINATING"` rather than `"TERMINATED"`: the default teardown does not
    /// wait, so claiming TERMINATED would claim an observation nobody made. Pass
    /// `wait_for_terminated=True` to observe it.
    #[getter]
    fn lifecycle(&self) -> Option<String> {
        self.inner
            .lifecycle
            .map(|lifecycle| lifecycle.as_str().to_string())
    }

    /// Every failure the teardown swallowed, in the order it hit them.
    ///
    /// Kept because a teardown that never raises is a teardown whose failures are
    /// invisible otherwise, and the first one is usually the cause of the rest.
    #[getter]
    fn failures(&self) -> Vec<String> {
        self.inner.failures.clone()
    }

    /// Whether anything a caller asked for was left behind.
    #[getter]
    fn leaked(&self) -> bool {
        self.inner.leaked()
    }

    /// The report as a dict, for a JSON envelope.
    fn to_dict<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let dict = PyDict::new(py);
        dict.set_item("terminateAccepted", self.inner.terminate_accepted)?;
        dict.set_item("imageDeleted", self.inner.image_deleted)?;
        dict.set_item("lifecycle", self.lifecycle())?;
        dict.set_item("undeleted", self.inner.undeleted.clone())?;
        dict.set_item("failures", self.inner.failures.clone())?;
        dict.set_item("leaked", self.inner.leaked())?;
        Ok(dict)
    }

    fn __repr__(&self) -> String {
        format!(
            "TeardownReport(terminate_accepted={}, leaked={}, undeleted={:?})",
            self.inner.terminate_accepted,
            self.inner.leaked(),
            self.inner.undeleted
        )
    }
}

/// The platform's managed base image, paired with the Dockerfile `FROM` it goes with.
///
/// One class rather than two strings, because the two **must** agree and used to be able
/// to disagree: the Python client's default named the managed base for `baseImageArn` while
/// its Dockerfile hardcoded an unrelated registry literal in its `FROM`, so changing either
/// left the other pointing somewhere else.
#[pyclass(frozen, from_py_object, name = "BaseImage", module = "microvms")]
#[derive(Clone)]
pub struct PyBaseImage {
    inner: BaseImage,
}

#[pymethods]
impl PyBaseImage {
    /// The managed base every `docs/PLATFORM.md` measurement from 2026-08-06 onward used.
    #[staticmethod]
    fn al2023() -> PyBaseImage {
        PyBaseImage {
            inner: BaseImage::al2023(),
        }
    }

    /// A base a caller built themselves.
    ///
    /// `working_dir` is what `docker inspect` reports for `WorkingDir`, and empty means the
    /// image declares none. It is a parameter because a caller with a purpose-built image
    /// is the only one who can say what theirs declares — this client cannot read it
    /// without pulling the manifest. Getting it wrong is what
    /// `inherit_workdir` refuses on.
    #[new]
    #[pyo3(signature = (name, docker_ref, working_dir=""))]
    fn new(name: &str, docker_ref: &str, working_dir: &str) -> PyBaseImage {
        PyBaseImage {
            inner: BaseImage {
                name: name.to_string(),
                docker_ref: docker_ref.to_string(),
                working_dir: working_dir.to_string(),
            },
        }
    }

    /// The base a task Dockerfile pairs with: the managed base's `name`, so `baseImageArn` is
    /// unchanged, and the Dockerfile's first `FROM` as `docker_ref`, digest pin included.
    ///
    /// `build_image` refuses a Dockerfile whose first `FROM` is not the base's `docker_ref`;
    /// a base derived from that Dockerfile passes by construction. Raises `InvalidArgError`
    /// for a Dockerfile with no `FROM`.
    #[staticmethod]
    fn from_dockerfile(dockerfile: &str) -> PyCoreResult<PyBaseImage> {
        // IMAGE-5: a pass-through; the derivation and its refusal are core's.
        Ok(PyBaseImage {
            inner: BaseImage::from_dockerfile(dockerfile)?,
        })
    }

    /// Goes into `baseImageArn` — the platform's managed base, not a registry ref.
    #[getter]
    fn name(&self) -> &str {
        &self.inner.name
    }

    /// Goes into the Dockerfile `FROM` — the registry ref measured alongside `name`.
    #[getter]
    fn docker_ref(&self) -> &str {
        &self.inner.docker_ref
    }

    #[getter]
    fn working_dir(&self) -> &str {
        &self.inner.working_dir
    }

    fn __repr__(&self) -> String {
        format!(
            "BaseImage(name={:?}, docker_ref={:?})",
            self.inner.name, self.inner.docker_ref
        )
    }
}

/// A task Dockerfile with the agentd stanza appended, ready for `build_image`.
///
/// The result is the task text, a newline if it lacked one, `USER root` when the task's last
/// `USER` is anyone else, then the stanza the default Dockerfile uses: `COPY agentd /agentd`,
/// the chmod, `ENV AGENTD_PORT`, `EXPOSE`, `ENTRYPOINT []` and `CMD ["/agentd"]`. Pass the
/// result to `build_image` with `base_image=BaseImage.from_dockerfile(result)`.
///
/// `port` is the agent port (9000 by default) and must match the sandbox's. `workdir`
/// creates and sets a working directory, as the default Dockerfile does. `inherit_workdir`
/// refuses a result with no `WORKDIR` anywhere.
///
/// Raises `InvalidArgError` for a task with no `FROM`, one that ends inside a line
/// continuation or an unterminated heredoc (either would swallow the stanza), a keepalive
/// the client cannot tolerate, a port of 0, a workdir that is not one absolute path, or
/// `inherit_workdir` with nothing to inherit.
#[pyfunction]
#[pyo3(signature = (task_dockerfile, *, port=None, workdir=None, inherit_workdir=false))]
pub(crate) fn wrap_dockerfile(
    task_dockerfile: &str,
    port: Option<u16>,
    workdir: Option<String>,
    inherit_workdir: bool,
) -> PyCoreResult<String> {
    // IMAGE-5: a pass-through; the stanza, the guards and their messages are core's.
    let defaults = microvms_core::control::WrapOptions::default();
    let opts = microvms_core::control::WrapOptions {
        port: port.unwrap_or(defaults.port),
        workdir,
        inherit_workdir,
    };
    Ok(microvms_core::control::wrap_dockerfile(
        task_dockerfile,
        &opts,
    )?)
}

/// The per-VM `logging` a binding's three keyword arguments ask for.
pub(crate) fn logging_for(
    log_group: Option<String>,
    log_stream: Option<String>,
    disable_logging: bool,
) -> Result<Option<microvms_core::control::ops::Logging>, microvms_core::Error> {
    microvms_core::control::ops::Logging::from_parts(log_group, log_stream, disable_logging)
}

/// One MicroVM's whole life.
///
/// The five transitions are `build_image`, `run`, `suspend`, `resume`, and `terminate`, and
/// every state guard lives in the core — see the module docs.
#[pyclass(frozen, name = "Sandbox", module = "microvms")]
pub struct PySandbox {
    /// Shared with every [`crate::session::PySession`] this sandbox hands out, so a
    /// `terminate()` and a session call cannot interleave. `frozen` on the pyclass plus the
    /// `Mutex` is what gives `&mut Sandbox` from a `&self` method — which the core's
    /// transitions require.
    inner: Arc<Mutex<Sandbox>>,
}

impl PySandbox {
    /// A sandbox wrapper over an `Arc` another object already holds: how
    /// [`crate::agents::PyAgentVm`] hands out the sandbox it drives.
    pub(crate) fn from_arc(inner: Arc<Mutex<Sandbox>>) -> Self {
        Self { inner }
    }

    /// Runs `body` against the sandbox with the GIL released.
    ///
    /// One shape for every transition: release the GIL, take the lock, run, drop both
    /// before returning into Python. Nothing here holds the lock across a Python callback,
    /// so it cannot deadlock against the GIL.
    ///
    /// `body` is **synchronous** and calls [`runtime::block_on_detached`] itself. A closure
    /// answering a future that borrows its `&mut Sandbox` argument needs a higher-ranked
    /// bound plus a boxed future at every call site, and the boxing would exist only to
    /// satisfy the signature; blocking inside keeps the borrow local.
    fn detached<T>(&self, py: Python<'_>, body: impl FnOnce(&mut Sandbox) -> T + Send) -> T
    where
        T: Send,
    {
        py.detach(|| {
            let mut guard = self.inner.lock().unwrap_or_else(PoisonError::into_inner);
            body(&mut guard)
        })
    }

    /// Reads something off the sandbox under the lock.
    pub(crate) fn read<T>(&self, body: impl FnOnce(&Sandbox) -> T) -> T {
        let guard = self.inner.lock().unwrap_or_else(PoisonError::into_inner);
        body(&guard)
    }
}

#[pymethods]
impl PySandbox {
    /// Resolves credentials for `region` and returns a sandbox with nothing launched.
    ///
    /// `region` is a [`PyRegion`] and not a string, which is TRAP-6 at this boundary: the
    /// five supported regions are named constructors and everything else goes through
    /// `Region.parse` (refused) or `Region.unlisted` (opted into, at the call site).
    #[new]
    fn new(py: Python<'_>, region: PyRegion) -> PyCoreResult<PySandbox> {
        let sandbox = runtime::block_on(py, Sandbox::new(region.inner))?;
        Ok(PySandbox {
            inner: Arc::new(Mutex::new(sandbox)),
        })
    }

    /// The lifecycle state: `"PENDING"`, `"RUNNING"`, `"SUSPENDING"`, `"SUSPENDED"`,
    /// `"TERMINATING"`, or `"TERMINATED"`.
    ///
    /// Spelled as the service spells it, because a reader compares it against a
    /// `GetMicrovm` response and `Suspended` beside `SUSPENDED` reads like two facts.
    #[getter]
    fn lifecycle(&self) -> String {
        self.read(|sandbox| sandbox.lifecycle().as_str().to_string())
    }

    /// Whether the agent token has been installed (STATE-2).
    ///
    /// Set by the platform reporting RUNNING, not by the launch call: the run hook is what
    /// delivers the token, and a launch that died during startup delivered nothing.
    #[getter]
    fn token_installed(&self) -> bool {
        self.read(Sandbox::token_installed)
    }

    /// Whether an image is recorded as existing (STATE-1).
    #[getter]
    fn image_exists(&self) -> bool {
        self.read(Sandbox::image_exists)
    }

    /// Whether this VM was ever terminated (STATE-11).
    #[getter]
    fn was_terminated(&self) -> bool {
        self.read(Sandbox::was_terminated)
    }

    /// How many times the token has been installed. Never above one (STATE-3).
    #[getter]
    fn bootstrap_count(&self) -> u32 {
        self.read(Sandbox::bootstrap_count)
    }

    /// A sandbox for a VM another process launched, from its private record.
    ///
    /// The lifecycle is read from `GetMicrovm`, so suspend, resume, and terminate start
    /// from the service's state and keep every guard. The VM was bootstrapped by its own
    /// launch, so `run` is refused and no run-hook payload is ever sent. Keep `agent_token`
    /// in private encrypted storage; it never appears in repr or an error.
    #[staticmethod]
    #[pyo3(signature = (region, microvm_id, endpoint, agent_token, *, port=None))]
    fn adopt(
        py: Python<'_>,
        region: PyRegion,
        microvm_id: String,
        endpoint: String,
        agent_token: String,
        port: Option<u16>,
    ) -> PyCoreResult<PySandbox> {
        let sandbox = runtime::block_on(
            py,
            Sandbox::adopt_in(region.inner, microvm_id, endpoint, agent_token, port),
        )?;
        Ok(PySandbox {
            inner: Arc::new(Mutex::new(sandbox)),
        })
    }

    /// Adopts the VM registered as `name` in `registry`; see `Sandbox.adopt`.
    ///
    /// `region` must match the record's: an id from another region addresses nothing here.
    #[staticmethod]
    #[pyo3(signature = (region, name, registry, *, port=None))]
    fn from_name(
        py: Python<'_>,
        region: PyRegion,
        name: String,
        registry: &crate::names::PyNameRegistry,
        port: Option<u16>,
    ) -> PyCoreResult<PySandbox> {
        let store = registry.store.clone();
        let sandbox = runtime::block_on(py, async move {
            Sandbox::from_name(&store, &name, Some(region.inner), port).await
        })?;
        Ok(PySandbox {
            inner: Arc::new(Mutex::new(sandbox)),
        })
    }

    /// Whether this sandbox was built by `adopt` rather than by its own launch.
    #[getter]
    fn adopted(&self) -> bool {
        self.read(Sandbox::adopted)
    }

    /// Hands the VM off to another process and returns what that process adopts it with.
    ///
    /// For a workflow whose steps run in different processes: the launching step calls this
    /// instead of dropping the sandbox (which warns that a live VM was abandoned), persists
    /// the returned record privately, and a later step calls `Sandbox.adopt` with it. The VM
    /// keeps running and no AWS call is made. Afterwards this sandbox is inert: its session
    /// is gone and `run`, `wait_until_running`, `suspend`, `resume`, and `terminate` are
    /// refused. Raises `PreconditionError` without a live VM or when already detached.
    fn detach(&self, py: Python<'_>) -> PyCoreResult<PyDetached> {
        let inner = self
            .detached(py, |sandbox| sandbox.detach())
            .map_err(CoreError)?;
        Ok(PyDetached { inner })
    }

    /// Whether `detach()` handed this sandbox's VM to another process.
    #[getter]
    fn is_detached(&self) -> bool {
        self.read(Sandbox::detached)
    }

    /// The VM id, once launched.
    #[getter]
    fn microvm_id(&self) -> Option<String> {
        self.read(|sandbox| sandbox.microvm().map(|vm| vm.id.clone()))
    }

    /// The proxy endpoint, once launched.
    #[getter]
    fn endpoint(&self) -> Option<String> {
        self.read(|sandbox| sandbox.microvm().map(|vm| vm.endpoint.clone()))
    }

    /// Why the VM is in its current state, when the service said.
    ///
    /// The absence is information: TRAP-8's message distinguishes "no stateReason" from an
    /// empty one.
    #[getter]
    fn state_reason(&self) -> Option<String> {
        self.read(|sandbox| sandbox.microvm().and_then(|vm| vm.state_reason.clone()))
    }

    /// The image, once built.
    #[getter]
    fn image(&self) -> Option<PyImage> {
        self.read(|sandbox| {
            sandbox.image().map(|image| PyImage {
                identifier: image.identifier.clone(),
                name: image.name.clone(),
                version: image.version.clone(),
                state: image.state.clone(),
                size: image.size,
                build_log_group: image.build_log_group(),
                log_stream: image.log_stream.clone(),
            })
        })
    }

    /// The suspended window this sandbox asked for at launch, in seconds.
    ///
    /// `None` before this sandbox launches a VM. This accessor reports the requested
    /// window; `GetMicrovm` also returns the service's idle policy.
    #[getter]
    fn suspended_window_seconds(&self) -> Option<f64> {
        self.read(|sandbox| {
            sandbox
                .suspended_window()
                .map(|window| window.as_secs_f64())
        })
    }

    /// The session, once launched.
    ///
    /// A new wrapper each call, all reaching the same session under the same lock. There is
    /// no cached `Py<PySession>`: caching one would mean a session object that outlives the
    /// VM it addresses, and the `Held::InSandbox` indirection exists precisely so a
    /// post-terminate call reports the lifecycle rather than a dangling handle.
    #[getter]
    fn session(&self) -> Option<PySession> {
        let has_session = self.read(|sandbox| sandbox.session().is_some());
        has_session.then(|| PySession::in_sandbox(Arc::clone(&self.inner)))
    }

    /// Builds an image and waits for it to become usable.
    ///
    /// Every local guard runs **before** the call, which matters because the create happens
    /// after the caller's artifact upload: a rejection AWS raises costs the upload first.
    ///
    /// # What is deliberately not a parameter
    ///
    /// A `client_token`. There is no such field on the core's request type and none here:
    /// a digest-derived token replays the original create and wedges an image in `CREATING`
    /// for fifteen hours with no error at all (TRAP-1). `token_scope` is a CloudTrail
    /// **label** folded in beside a fresh nonce and cannot become the token.
    ///
    /// A `capabilities` list. `repair_guest_identity` is a bool and the request injects
    /// `["ALL"]` itself, so `["CAP_SYS_ADMIN"]` — the request AWS rejects after the upload
    /// — is not something a caller can write (TRAP-3).
    ///
    /// An `architecture`. The model's enum has exactly one value, so the only thing a field
    /// could express is a rejected request.
    ///
    /// # `log_stream` is a prefix, never the exact stream name
    ///
    /// The platform's `logging.logStream` member is an EXACT stream name (prefixes
    /// unsupported), and one build is three VMs writing three streams — so a fixed name
    /// would collapse every build's logs into one stream. The client appends `/<16 hex>`
    /// of fresh randomness per build attempt, and the resolved exact name comes back on
    /// `Image.log_stream`. `log_stream` requires `log_group`.
    #[pyo3(signature = (
        *,
        name,
        binary,
        code_artifact_uri,
        build_role_arn,
        size=None,
        base_image=None,
        dockerfile=None,
        repair_guest_identity=false,
        inherit_workdir=false,
        run_hook_timeout=None,
        build_hook_timeout=None,
        tags=None,
        log_group=None,
        log_stream=None,
        token_scope=None,
    ))]
    #[allow(
        clippy::too_many_arguments,
        reason = "one keyword-only parameter per \
         CreateImageRequest field; the two hook timeouts are separate typed parameters on \
         purpose, because that is what makes transposing them impossible"
    )]
    fn build_image(
        &self,
        py: Python<'_>,
        name: &str,
        binary: Vec<u8>,
        code_artifact_uri: &str,
        build_role_arn: &str,
        size: Option<PySizeClass>,
        base_image: Option<PyBaseImage>,
        dockerfile: Option<String>,
        repair_guest_identity: bool,
        inherit_workdir: bool,
        run_hook_timeout: Option<PyRunHookTimeout>,
        build_hook_timeout: Option<PyBuildHookTimeout>,
        tags: Option<std::collections::BTreeMap<String, String>>,
        log_group: Option<String>,
        log_stream: Option<String>,
        token_scope: Option<String>,
    ) -> PyCoreResult<PyImage> {
        let mut request = CreateImageRequest::new(name, binary, code_artifact_uri, build_role_arn);
        if let Some(size) = size {
            request.size = size.inner;
        }
        if let Some(base) = base_image {
            request.base_image = base.inner;
        }
        request.dockerfile = dockerfile;
        request.repair_guest_identity = repair_guest_identity;
        request.inherit_workdir = inherit_workdir;
        if let Some(timeout) = run_hook_timeout {
            request.run_hook_timeout = timeout.inner;
        }
        if let Some(timeout) = build_hook_timeout {
            request.build_hook_timeout = timeout.inner;
        }
        if let Some(tags) = tags {
            request.tags = tags;
        }
        // The stream is a *prefix* by core's contract: the create call appends `/<16
        // hex>` per attempt (one build is three VMs writing three streams under an
        // exact-name member), and the resolved name comes back on `Image.log_stream`.
        request.log_group = log_group;
        request.log_stream = log_stream;
        request.token_scope = token_scope;

        let built = self.detached(py, move |sandbox| {
            runtime::block_on_detached(sandbox.build_image(request)).map(|image| PyImage {
                identifier: image.identifier.clone(),
                name: image.name.clone(),
                version: image.version.clone(),
                state: image.state.clone(),
                size: image.size,
                build_log_group: image.build_log_group(),
                log_stream: image.log_stream.clone(),
            })
        })?;
        Ok(built)
    }

    /// Builds or reuses the content-addressed image for a task: one call from build inputs
    /// to a ready image.
    ///
    /// The name is `<name_prefix>-<hash12>`, the hash over the daemon, the Dockerfile, the
    /// build context, the base image and the size class, so equal inputs name one image.
    /// The ARN is built from the caller's account (looked up once per sandbox). The image
    /// is described, then:
    ///
    /// - ready: returned, with `reused=True` and no upload;
    /// - building: waited out and returned, `reused=True`;
    /// - failed, or any state under `force=True`: deleted, the name awaited free, rebuilt;
    /// - absent: the artifact is uploaded to `s3://<s3_bucket>/<s3_key_prefix>/<name>/
    ///   artifact.zip` and the image created and waited for, `reused=False`.
    ///
    /// When a concurrent caller creates the name first, this call's create is refused; it
    /// describes again and waits for that build, returning it with `reused=True`.
    ///
    /// `dockerfile` is usually `wrap_dockerfile(task)`. `context_dir` is the directory the
    /// Dockerfile's `COPY` lines read, taken as `docker build` takes it:
    /// `Dockerfile.dockerignore`, else `.dockerignore`, is honoured, and symlinks are skipped
    /// with a line in `warnings`. `base_image` defaults to
    /// `BaseImage.from_dockerfile(dockerfile)`. `wait_timeout` is the build wait in seconds
    /// (45 minutes by default). Every local check runs before the first AWS call.
    #[pyo3(signature = (
        *,
        name_prefix,
        binary,
        dockerfile,
        s3_bucket,
        build_role_arn,
        context_dir=None,
        s3_key_prefix=None,
        size=None,
        base_image=None,
        force=false,
        tags=None,
        wait_timeout=None,
    ))]
    #[allow(
        clippy::too_many_arguments,
        reason = "one keyword-only parameter per EnsureImageRequest field"
    )]
    fn ensure_image(
        &self,
        py: Python<'_>,
        name_prefix: String,
        binary: Vec<u8>,
        dockerfile: String,
        s3_bucket: String,
        build_role_arn: String,
        context_dir: Option<std::path::PathBuf>,
        s3_key_prefix: Option<String>,
        size: Option<PySizeClass>,
        base_image: Option<PyBaseImage>,
        force: bool,
        tags: Option<std::collections::BTreeMap<String, String>>,
        wait_timeout: Option<f64>,
    ) -> PyCoreResult<PyEnsuredImage> {
        // IMAGE-12: a pass-through; the name, the context, the decisions and the race are
        // core's.
        let mut request = microvms_core::control::EnsureImageRequest::new(
            name_prefix,
            binary,
            dockerfile,
            s3_bucket,
            build_role_arn,
        );
        request.s3_key_prefix = s3_key_prefix;
        if let Some(size) = size {
            request.size = size.inner;
        }
        request.base_image = base_image.map(|base| base.inner);
        request.force = force;
        if let Some(tags) = tags {
            request.tags = tags;
        }
        if let Some(timeout) = wait_timeout {
            request.wait_timeout = Some(seconds(timeout)?);
        }
        let ensured = self.detached(py, move |sandbox| {
            if let Some(dir) = context_dir {
                request.context = Some(microvms_core::control::BuildContext::from_dir(dir)?);
            }
            runtime::block_on_detached(sandbox.ensure_image(request))
        })?;
        Ok(PyEnsuredImage {
            image: ensured.image,
            reused: ensured.reused,
            artifact_uri: ensured.artifact_uri,
            uploaded: ensured.uploaded,
            warnings: ensured.warnings,
        })
    }

    /// The artifact bytes to upload to `code_artifact_uri`.
    ///
    /// The upload is the caller's: S3 is not in the core's dependency set. Same parameters
    /// as [`Self::build_image`] so the bytes a caller puts in the bucket are the bytes the
    /// build will receive.
    #[pyo3(signature = (
        *,
        name,
        binary,
        code_artifact_uri,
        build_role_arn,
        base_image=None,
        dockerfile=None,
        inherit_workdir=false,
    ))]
    #[allow(
        clippy::too_many_arguments,
        reason = "the CreateImageRequest fields the \
         artifact actually depends on; fewer would mean the bytes a caller uploads could \
         differ from the bytes the build receives"
    )]
    fn build_artifact<'py>(
        &self,
        py: Python<'py>,
        name: &str,
        binary: Vec<u8>,
        code_artifact_uri: &str,
        build_role_arn: &str,
        base_image: Option<PyBaseImage>,
        dockerfile: Option<String>,
        inherit_workdir: bool,
    ) -> PyCoreResult<Bound<'py, PyBytes>> {
        let mut request = CreateImageRequest::new(name, binary, code_artifact_uri, build_role_arn);
        if let Some(base) = base_image {
            request.base_image = base.inner;
        }
        request.dockerfile = dockerfile;
        request.inherit_workdir = inherit_workdir;
        let bytes = self.read(|sandbox| sandbox.build_artifact_for(&request));
        Ok(PyBytes::new(py, &bytes.map_err(crate::errors::CoreError)?))
    }

    /// Launches a MicroVM, waits for RUNNING, and returns its session.
    ///
    /// `egress` requests the managed INTERNET_EGRESS connector. Omission does not block
    /// outbound traffic. For no egress, pass existing VPC connector ARNs through
    /// `egress_network_connectors`, using a VPC without an internet gateway or NAT
    /// gateway. `deny_egress` sets advisory proxy variables that workloads can bypass.
    /// # What the core refuses here, and this file does not
    ///
    /// A second `run` on one sandbox, with **zero** control-plane calls: the agent token is
    /// installed at most once per VM lifetime (STATE-3), and a second VM needs a second
    /// `Sandbox`. A run with no image at all, before any call. Neither check is in this
    /// file.
    ///
    /// `agent_token` is optional because the common case is a per-VM secret nobody needs to
    /// see; a caller who has one already — a harness minting its own, or a retry that must
    /// reuse the first attempt's — passes it. It rides in `runHookPayload`, which is what
    /// keeps it out of the shared image snapshot.
    #[pyo3(signature = (
        *,
        image_identifier=None,
        image_version=None,
        execution_role_arn=None,
        agent_token=None,
        client_token=None,
        launch_env=None,
        egress=false,
        egress_network_connectors=None,
        deny_egress=false,
        shell=false,
        max_idle_sec=None,
        suspended_sec=None,
        auto_resume=false,
        max_duration_sec=None,
        ready_timeout=None,
        token_scope=None,
        wait=true,
        log_group=None,
        log_stream=None,
        disable_logging=false,
    ))]
    #[allow(
        clippy::too_many_arguments,
        reason = "one keyword-only parameter per \
         RunRequest field"
    )]
    fn run(
        &self,
        py: Python<'_>,
        image_identifier: Option<String>,
        // `image_version` pins the launch to one `imageVersion` rather than taking the
        // image's latest active one. That is what makes a canary launch against the version
        // it means to test, and it is the half of a rollback that re-points at the
        // known-good build; a version the control plane has set INACTIVE refuses to launch
        // when named here. Not a doc comment: a doc comment on a function parameter is a
        // compile error.
        image_version: Option<String>,
        execution_role_arn: Option<String>,
        agent_token: Option<String>,
        client_token: Option<String>,
        // `launch_env` is the base environment for every exec in the launched VM,
        // delivered in the same `runHookPayload` as the token and applied *under* each
        // exec's own `env`. It shares the token's 4096-byte payload budget, checked
        // locally before the launch. Not a doc comment: a doc comment on a function
        // parameter is a compile error.
        launch_env: Option<std::collections::HashMap<String, String>>,
        egress: bool,
        egress_network_connectors: Option<Vec<String>>,
        // `deny_egress` is the advisory in-guest deny: proxy variables pointed at a black
        // hole in the launch env, so a well-behaved client refuses to leave the VM. Never a
        // seal — the platform gives a connector-less VM outbound network and the guest holds
        // no CAP_NET_ADMIN — and refused together with `egress`. Not a doc comment: a doc
        // comment on a function parameter is a compile error.
        deny_egress: bool,
        shell: bool,
        max_idle_sec: Option<u32>,
        suspended_sec: Option<u32>,
        auto_resume: bool,
        max_duration_sec: Option<u32>,
        ready_timeout: Option<f64>,
        token_scope: Option<String>,
        // `wait=False` returns once `RunMicrovm` is accepted, with the lifecycle PENDING;
        // `wait_until_running` finishes it. Not a doc comment: a doc comment on a function
        // parameter is a compile error.
        wait: bool,
        // Per-VM CloudWatch logging: a group, optionally an exact stream inside it, or
        // `disable_logging` for none. Omitted keeps the service's default destination.
        log_group: Option<String>,
        log_stream: Option<String>,
        disable_logging: bool,
    ) -> PyCoreResult<PySession> {
        let logging = logging_for(log_group, log_stream, disable_logging)?;
        // Every unset window falls back to the core's own default rather than to a number
        // written here: ten-minute idle and suspended windows, a one-hour ceiling, and the
        // five-minute ready wait are measured figures, and a second copy of them in a
        // binding is a second thing to keep in step (the JS binding defers the same way).
        let defaults = RunRequest::new();
        let request = RunRequest {
            image_identifier,
            image_version,
            execution_role_arn,
            agent_token,
            client_token,
            launch_env: launch_env.unwrap_or(defaults.launch_env),
            // The tunnel identity is a CLI/daemon surface (`microvm tunnel
            // --verify-identity`); the bindings keep the default (off) until a
            // binding-level verify API exists to consume the material.
            identity: defaults.identity,
            egress,
            egress_network_connectors: egress_network_connectors.unwrap_or_default(),
            deny_egress,
            shell,
            max_idle_sec: max_idle_sec.unwrap_or(defaults.max_idle_sec),
            suspended_sec: suspended_sec.unwrap_or(defaults.suspended_sec),
            auto_resume,
            max_duration_sec: max_duration_sec.unwrap_or(defaults.max_duration_sec),
            ready_timeout: match ready_timeout {
                Some(timeout) => seconds(timeout)?,
                None => defaults.ready_timeout,
            },
            token_scope,
            logging,
            wait,
        };
        // `run` answers `&mut Session`, which cannot cross back into Python — so the
        // return value is discarded and the session is reached through the sandbox. That
        // is not a workaround: it is what makes a post-terminate session call report the
        // lifecycle instead of addressing a VM that is gone.
        self.detached(py, move |sandbox| {
            runtime::block_on_detached(sandbox.run(request)).map(|_| ())
        })?;
        Ok(PySession::in_sandbox(Arc::clone(&self.inner)))
    }

    /// Finishes a `run(wait=False)`: waits for RUNNING and returns the session.
    ///
    /// A launch whose `client_token` adopted an existing, idle-suspended VM resumes it;
    /// a fresh launch that reaches a terminal state first raises `LaunchDiedError` with
    /// the service's `stateReason`. Refused unless the launch is still PENDING.
    #[pyo3(signature = (*, timeout=None))]
    fn wait_until_running(&self, py: Python<'_>, timeout: Option<f64>) -> PyCoreResult<PySession> {
        let timeout = match timeout {
            Some(timeout) => seconds(timeout)?,
            None => RunRequest::new().ready_timeout,
        };
        self.detached(py, move |sandbox| {
            runtime::block_on_detached(sandbox.wait_until_running(timeout)).map(|_| ())
        })?;
        Ok(PySession::in_sandbox(Arc::clone(&self.inner)))
    }

    /// Freezes the VM and waits for the platform to report it.
    ///
    /// A freeze and restore rather than a stop and start: the guest keeps its memory, so
    /// the token, the filesystem, and every exec record survive. The one thing that does
    /// not is the guest's view of time — it observes the whole suspension as a single jump,
    /// so any timeout, lease, or TLS session a running command holds expires at once on
    /// resume.
    ///
    /// A suspend from anything but RUNNING is refused by the core with zero control-plane
    /// calls (STATE-5). Returns the state reached, which may be `"TERMINATED"`: a VM that
    /// dies while suspending is a state to report rather than an exception out of the
    /// middle of a teardown.
    fn suspend(&self, py: Python<'_>) -> PyCoreResult<String> {
        self.detached(py, |sandbox| {
            runtime::block_on_detached(sandbox.suspend())?;
            Ok(sandbox.lifecycle().as_str().to_string())
        })
        .map_err(crate::errors::CoreError)
    }

    /// Thaws the VM and returns a usable session.
    ///
    /// # What the core refuses, before any wire call
    ///
    /// A resume after `terminate` (STATE-11) — a terminated VM never returns to RUNNING,
    /// and even a call the service accepted would hand back a different machine. A resume
    /// from anything but SUSPENDED (STATE-7). And a resume past the launch-time suspended
    /// window (STATE-12), which is the one worth knowing about: the `idlePolicy` terminates
    /// a suspended VM once that window passes, so there is nothing left to resume, and
    /// calling would cost the full poll timeout to learn something worse.
    ///
    /// Nothing is re-delivered: no run-hook payload, no token, no bootstrap. The in-memory
    /// token survived the freeze, and re-delivering it would hit the daemon's one-shot
    /// bootstrap and be refused — a 409 that reads like a broken VM.
    fn resume(&self, py: Python<'_>) -> PyCoreResult<PySession> {
        self.detached(py, |sandbox| {
            runtime::block_on_detached(sandbox.resume()).map(|_| ())
        })?;
        Ok(PySession::in_sandbox(Arc::clone(&self.inner)))
    }

    /// Tears down, best-effort, **never raising**.
    ///
    /// Order: VM, then image, then the log group last, because the service can recreate a
    /// group deleted before its image.
    ///
    /// Both deletions are opt-in, because both destroy something a caller may still want:
    /// the image is reusable across runs, and the log group is where a failed build's only
    /// evidence lives. `delete_log_group=True` **names** the group in
    /// `report.undeleted` rather than deleting it — CloudWatch is not in the core's
    /// dependency set, and reporting a leak beats reporting a clean teardown over one.
    ///
    /// `wait_for_terminated=False` by default: the caller is on the way out, and a teardown
    /// that blocked five minutes on a state nobody reads is five minutes of a CI job. The
    /// report then honestly ends in `"TERMINATING"`.
    #[pyo3(signature = (
        *,
        delete_image=false,
        delete_log_group=false,
        delete_attempts=None,
        delete_backoff=None,
        wait_for_terminated=false,
    ))]
    pub(crate) fn terminate(
        &self,
        py: Python<'_>,
        delete_image: bool,
        delete_log_group: bool,
        delete_attempts: Option<u32>,
        delete_backoff: Option<f64>,
        wait_for_terminated: bool,
    ) -> PyCoreResult<PyTeardownReport> {
        // The two retry knobs default to the core's own figures rather than to numbers
        // written here: twenty attempts fifteen seconds apart is the difference between a
        // clean account and a billed leak, and restating them would put a second copy of
        // that measurement in a binding.
        let defaults = TeardownOpts::default();
        let mut opts = TeardownOpts {
            delete_image,
            delete_log_group,
            delete_attempts: delete_attempts.unwrap_or(defaults.delete_attempts),
            delete_backoff: match delete_backoff {
                Some(backoff) => seconds(backoff)?,
                None => defaults.delete_backoff,
            },
            wait_for_terminated: defaults.wait_for_terminated,
        };
        if wait_for_terminated {
            opts = opts.waiting_for_terminated();
        }
        // `terminate` answers a report rather than a `Result`, so the `Ok` here is this
        // wrapper's and never the core's — a teardown cannot raise, which is the whole
        // point of the report.
        let report = self.detached(py, move |sandbox| {
            runtime::block_on_detached(sandbox.terminate(opts))
        });
        Ok(PyTeardownReport { inner: report })
    }

    /// `with sandbox as s:` — returns the sandbox itself.
    fn __enter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    /// Tears down on the way out, whatever happened inside the block.
    ///
    /// Returns `False`, so an exception raised inside the block propagates: the teardown is
    /// what has to happen, not what has to be reported. Any leak is on the report the
    /// caller can also get by calling `terminate()` themselves.
    #[pyo3(signature = (exc_type=None, exc_value=None, traceback=None))]
    fn __exit__(
        &self,
        py: Python<'_>,
        exc_type: Option<Py<PyAny>>,
        exc_value: Option<Py<PyAny>>,
        traceback: Option<Py<PyAny>>,
    ) -> bool {
        let _ = (exc_type, exc_value, traceback);
        let opts = TeardownOpts::default();
        // The report is discarded here on purpose. `__exit__` runs where a `finally`
        // would, and there is nowhere to return a value to; a caller who needs the report
        // calls `terminate()` explicitly, which is the documented path.
        let _ = self.detached(py, move |sandbox| {
            runtime::block_on_detached(sandbox.terminate(opts))
        });
        false
    }

    fn __repr__(&self) -> String {
        self.read(|sandbox| {
            format!(
                "Sandbox(lifecycle={:?}, microvm_id={:?}, bootstrap_count={})",
                sandbox.lifecycle().as_str(),
                sandbox.microvm().map(|vm| vm.id.as_str()),
                sandbox.bootstrap_count(),
            )
        })
    }
}

/// The egress posture `Sandbox.run` with these options would report, without launching.
///
/// One of `"open"`, `"unsealed"`, `"best-effort"`, or `"sealed"`: the value the launched
/// session's `egress_posture` and the CLI envelope's `egressPosture` carry. Raises the
/// launch's own `InvalidArgError` for options it would refuse. No AWS call and no credentials,
/// so a harness can decide before a build whether a no-network task is satisfiable.
///
/// Only `"sealed"` is network isolation, and no option answers it: isolation needs a VPC
/// egress connector and separately verified VPC routing without an internet gateway or NAT
/// gateway. Omitting `egress` is `"unsealed"`; `deny_egress` is `"best-effort"`. Without a
/// `region`, each connector ARN is checked against the region it names.
#[pyfunction]
#[pyo3(signature = (egress=false, connectors=None, deny_egress=false, *, region=None))]
pub(crate) fn egress_posture_for(
    egress: bool,
    connectors: Option<Vec<String>>,
    deny_egress: bool,
    region: Option<PyRegion>,
) -> PyCoreResult<&'static str> {
    Ok(microvms_core::control::egress_posture_for(
        egress,
        &connectors.unwrap_or_default(),
        deny_egress,
        region.as_ref().map(|region| &region.inner),
    )?
    .as_str())
}
