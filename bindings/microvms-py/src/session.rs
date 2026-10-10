// SPDX-License-Identifier: Apache-2.0
//! The control API of one running MicroVM.
//!
//! # A session holds no state worth keeping
//!
//! Every exec record, every file, and the bootstrap token live in the VM. So a session
//! rebuilt from an endpoint and an agent token reattaches to everything a previous
//! process was doing, and `Session.exec(exec_id)` addresses the same server-side exec.
//! That is what [`PySession::direct`] is for: it is a supported shape rather than a
//! test-only hatch, and it is the path a caller inside the VM or on a tunnel takes.
//!
//! # `run` takes an argv, and a bare string is one element
//!
//! `session.run(["ls", "-la"])` and `session.run("ls -la", shell=True)` are the two
//! spellings. A bare string with `shell=False` becomes a **one-element** argv and is
//! never whitespace-split, which is `session.py`'s own rule: splitting on spaces is how a
//! path with a space in it becomes two arguments nobody meant.
//!
//! # The exec id is the idempotency key
//!
//! Omitted, one is minted (`x-<16 hex>`, the Python's shape). Supplied, the daemon
//! returns success for a known id without spawning a second child — so a caller whose
//! retry must be safe across its own restart passes a stable one.
//!
//! # A launched session lives inside its sandbox, and the lock is the borrow checker
//!
//! [`microvms_core::sandbox::Sandbox`] owns its `Session` by value and hands out only
//! `Option<&Session>`. A session obtained through that sandbox preserves its lifecycle
//! exclusion; use [`PySession::attach`] for independent supervisor traffic. A
//! session obtained from a sandbox borrows it under the sandbox's lock, and one built by
//! [`PySession::direct`] owns itself.
//!
//! Holding that lock across a session call is not a compromise — it is the core's own
//! discipline at runtime. `Sandbox::suspend`/`resume`/`terminate` take `&mut self`, so in
//! Rust you *cannot* terminate a sandbox while a `&Session` from it is alive. The lock
//! reproduces exactly that exclusion, including its cost: a `wait(timeout=300)` holds the
//! sandbox for up to five minutes, which is the same five minutes the borrow checker would
//! have held it for.
//!
//! The lock is tokio's, because an awaitable twin holds it across `.await` on a runtime
//! worker. A blocking call takes it inside the future it blocks on; a twin takes it inside the
//! task it spawns, so awaiting one blocks no event loop, and two twins through one
//! sandbox-held session take turns exactly as two threads do. A session from `attach` or
//! `direct` holds no lock, and its calls run at once.

use std::future::Future;
use std::sync::Arc;
use std::time::Duration;

use microvms_core::prelude::*;
use microvms_core::sandbox::Sandbox;
use microvms_core::session::serve;
use microvms_core::session::{
    CompletionOptions, CompletionPlan, DEFAULT_CLIENT_GRACE, ExecHandle, GapPolicy, OutputFlow,
    OutputSink, Session, StreamOptions, mint_exec_id,
};
use microvms_core::{Error, ErrorKind};
use pyo3::prelude::*;
use pyo3::pybacked::PyBackedBytes;
use pyo3::types::{PyBytes, PyDict};

use crate::errors::{CoreError, PyCoreResult};
use crate::exec::{PyExecHandle, PyExecResult, seconds};
use crate::region::PyRegion;
use crate::runtime;
use crate::sandbox::SharedSandbox;

/// How long to wait for a daemon to report bootstrapped: the core's default.
const DEFAULT_BOOTSTRAP_TIMEOUT: f64 =
    microvms_core::session::DEFAULT_BOOTSTRAP_TIMEOUT.as_secs_f64();

/// The default one-shot `run_sync` deadline: the core's exec wait, since `run_sync` waits the
/// same way.
const DEFAULT_RUN_SYNC_TIMEOUT: f64 = microvms_core::session::DEFAULT_EXEC_WAIT.as_secs_f64();

/// `run_to_completion`'s default `client_grace_sec`, from the core so the two cannot drift.
const DEFAULT_CLIENT_GRACE_SEC: f64 = DEFAULT_CLIENT_GRACE.as_secs_f64();

/// The daemon's liveness answer. `bootstrapped` is the useful field.
#[pyclass(frozen, name = "Health", module = "microvms")]
pub struct PyHealth {
    version: String,
    bootstrapped: bool,
    available_bytes: Option<u64>,
    reserve_bytes: Option<u64>,
    under_pressure: Option<bool>,
    identity_degraded: bool,
    identity_repaired: bool,
    busy: bool,
    execs: usize,
    hooks: Vec<protocol::health::HookObservation>,
    hooks_dropped: u64,
    identity_steps: Vec<protocol::health::IdentityStep>,
    image_env_keys: Option<usize>,
}

impl PyHealth {
    fn wrap(health: protocol::health::Health) -> Self {
        Self {
            version: health.version.into_owned(),
            bootstrapped: health.bootstrapped,
            available_bytes: health.disk.as_ref().map(|disk| disk.available_bytes),
            reserve_bytes: health.disk.as_ref().map(|disk| disk.reserve_bytes),
            under_pressure: health.disk.as_ref().map(|disk| disk.under_pressure),
            identity_degraded: health.identity_degraded,
            identity_repaired: health.identity_repaired,
            busy: health.busy,
            execs: health.execs,
            hooks: health.hooks,
            hooks_dropped: health.hooks_dropped,
            identity_steps: health.identity_steps,
            image_env_keys: health.image_env_keys,
        }
    }
}

#[pymethods]
impl PyHealth {
    /// The daemon's own version, distinct from the protocol version.
    #[getter]
    fn version(&self) -> &str {
        &self.version
    }

    /// Whether the run hook has landed and the control API is open.
    #[getter]
    fn bootstrapped(&self) -> bool {
        self.bootstrapped
    }

    /// Bytes available to an unprivileged writer, or `None` when free space could not be
    /// measured.
    ///
    /// `None` is deliberately distinct from zero: unmeasurable is not full, and a monitor
    /// that conflated them would page on a missing `statvfs`.
    #[getter]
    fn available_bytes(&self) -> Option<u64> {
        self.available_bytes
    }

    /// Bytes that must stay free before a write is refused. Zero means the guard is off.
    #[getter]
    fn reserve_bytes(&self) -> Option<u64> {
        self.reserve_bytes
    }

    /// Whether a write would be refused right now. Precomputed by the daemon so every
    /// consumer applies the same comparison the write path does.
    #[getter]
    fn under_pressure(&self) -> Option<bool> {
        self.under_pressure
    }

    /// Whether any startup identity repair step failed — a duplicate machine-id or
    /// boot_id still in place from the shared image.
    #[getter]
    fn identity_degraded(&self) -> bool {
        self.identity_degraded
    }

    /// False when identity repair was switched off by config. Separate from `degraded` so
    /// a monitor can tell "opted out" from "nothing to do".
    #[getter]
    fn identity_repaired(&self) -> bool {
        self.identity_repaired
    }

    /// Whether any exec is still running.
    ///
    /// For an orchestrator *outside* the VM deciding whether to keep it alive. The
    /// platform measures idleness by inbound traffic through the endpoint proxy, which
    /// terminates outside the guest, so an in-guest keepalive cannot reset the idle
    /// timer — polling this from outside is both the traffic and the decision. An exec
    /// that exited and is awaiting an ack is not busy.
    #[getter]
    fn busy(&self) -> bool {
        self.busy
    }

    /// How many execs are registered, in any phase. `busy` false with a non-zero count
    /// is a VM holding unacked output somebody still has to collect.
    #[getter]
    fn execs(&self) -> usize {
        self.execs
    }

    /// Every lifecycle-hook invocation the daemon observed, oldest first, each with its
    /// workload handler's outcome when the image carries one.
    ///
    /// The hook routes are reachable over loopback from inside the guest, so a workload
    /// can add entries; the earliest are the platform's.
    #[getter]
    fn hooks(&self) -> Vec<PyHookObservation> {
        self.hooks
            .iter()
            .cloned()
            .map(PyHookObservation::wrap)
            .collect()
    }

    /// How many hook invocations the daemon's log cap dropped.
    #[getter]
    fn hooks_dropped(&self) -> u64 {
        self.hooks_dropped
    }

    /// Each identity-repair step and its outcome. Empty until the run hook repairs this
    /// VM's identity.
    #[getter]
    fn identity_steps(&self) -> Vec<PyIdentityStep> {
        self.identity_steps
            .iter()
            .cloned()
            .map(|inner| PyIdentityStep { inner })
            .collect()
    }

    /// How many variables the daemon's image-environment snapshot holds, or `None` when it
    /// holds none. `None` is also what a daemon built before `inherit_image_env` reports,
    /// and such a daemon ignores that flag; the values are never reported.
    #[getter]
    fn image_env_keys(&self) -> Option<usize> {
        self.image_env_keys
    }

    fn __repr__(&self) -> String {
        format!(
            "Health(version={:?}, bootstrapped={}, identity_degraded={}, busy={}, execs={})",
            self.version, self.bootstrapped, self.identity_degraded, self.busy, self.execs
        )
    }
}

/// One lifecycle-hook invocation, as the daemon observed it.
#[pyclass(frozen, name = "HookObservation", module = "microvms")]
pub struct PyHookObservation {
    inner: protocol::health::HookObservation,
}

impl PyHookObservation {
    fn wrap(inner: protocol::health::HookObservation) -> Self {
        Self { inner }
    }
}

#[pymethods]
impl PyHookObservation {
    /// `ready`, `validate`, `run`, `suspend`, `resume`, or `terminate`.
    #[getter]
    fn hook(&self) -> &str {
        &self.inner.hook
    }

    /// Seconds since the epoch on the daemon's clock when the hook arrived.
    #[getter]
    fn fired_at(&self) -> u64 {
        self.inner.fired_at
    }

    /// The workload handler's outcome, or `None` when the image has no handler for
    /// this hook.
    #[getter]
    fn handler(&self) -> Option<PyHandlerOutcome> {
        self.inner
            .handler
            .clone()
            .map(|inner| PyHandlerOutcome { inner })
    }

    fn __repr__(&self) -> String {
        format!(
            "HookObservation(hook={:?}, fired_at={}, handler={})",
            self.inner.hook,
            self.inner.fired_at,
            self.handler()
                .map_or_else(|| "None".to_string(), |handler| handler.__repr__())
        )
    }
}

/// What a workload's hook handler did. Its output is in the daemon's log, not here.
#[pyclass(frozen, name = "HandlerOutcome", module = "microvms")]
pub struct PyHandlerOutcome {
    inner: protocol::health::HandlerOutcome,
}

#[pymethods]
impl PyHandlerOutcome {
    /// The exit code, or `None` when the handler was killed or never started.
    #[getter]
    fn exit_code(&self) -> Option<i32> {
        self.inner.exit_code
    }

    /// The signal that ended it, if one did.
    #[getter]
    fn signal(&self) -> Option<i32> {
        self.inner.signal
    }

    /// Whether the daemon killed it at its time budget.
    #[getter]
    fn timed_out(&self) -> bool {
        self.inner.timed_out
    }

    /// Milliseconds from spawn to exit or kill.
    #[getter]
    fn duration_ms(&self) -> u64 {
        self.inner.duration_ms
    }

    /// Why it could not run at all, such as `not executable`.
    #[getter]
    fn error(&self) -> Option<&str> {
        self.inner.error.as_deref()
    }

    /// Whether it ran and exited 0 within its budget.
    #[getter]
    fn succeeded(&self) -> bool {
        self.inner.succeeded()
    }

    fn __repr__(&self) -> String {
        format!(
            "HandlerOutcome(exit_code={:?}, signal={:?}, timed_out={}, duration_ms={}, error={:?})",
            self.inner.exit_code,
            self.inner.signal,
            self.inner.timed_out,
            self.inner.duration_ms,
            self.inner.error
        )
    }
}

/// One identity-repair step.
#[pyclass(frozen, name = "IdentityStep", module = "microvms")]
pub struct PyIdentityStep {
    inner: protocol::health::IdentityStep,
}

#[pymethods]
impl PyIdentityStep {
    /// `machine-id`, `hostname`, `boot-id`, `random-seed`, or `cached-credential`.
    #[getter]
    fn name(&self) -> &str {
        &self.inner.name
    }

    /// `repaired`, `not_applicable`, or `failed`.
    #[getter]
    fn outcome(&self) -> &str {
        &self.inner.outcome
    }

    /// The OS error for a failed step.
    #[getter]
    fn error(&self) -> Option<&str> {
        self.inner.error.as_deref()
    }

    fn __repr__(&self) -> String {
        format!(
            "IdentityStep(name={:?}, outcome={:?}, error={:?})",
            self.inner.name, self.inner.outcome, self.inner.error
        )
    }
}

/// One exec's process group, as `GET /v1/procs` reports it.
///
/// `child_exited` beside a non-empty `pids` is the shape worth reading: a command that
/// finished while something it backgrounded did not, which `Health.busy` cannot show.
/// The `exec_id` beside it is what `Session.kill` takes.
#[pyclass(frozen, name = "ProcGroup", module = "microvms")]
pub struct PyProcGroup {
    exec_id: String,
    pgid: Option<u32>,
    started_at: u64,
    child_exited: bool,
    reap: bool,
    pids: Vec<u32>,
}

impl PyProcGroup {
    fn wrap(group: protocol::exec::ProcGroup) -> Self {
        Self {
            exec_id: group.exec_id,
            pgid: group.pgid,
            started_at: group.started_at,
            child_exited: group.child_exited,
            reap: group.reap,
            pids: group.pids,
        }
    }
}

#[pymethods]
impl PyProcGroup {
    /// The exec this group belongs to — what `Session.kill` takes.
    #[getter]
    fn exec_id(&self) -> &str {
        &self.exec_id
    }

    /// The process group id captured at spawn, or `None` when the child was reaped before
    /// it could be read (then `pids` is empty: there is no group to scan for).
    #[getter]
    fn pgid(&self) -> Option<u32> {
        self.pgid
    }

    /// Seconds since the epoch on the daemon's clock when the child was spawned.
    #[getter]
    fn started_at(&self) -> u64 {
        self.started_at
    }

    /// Whether the exec's own child has exited. An acked exec still reads `True`.
    #[getter]
    fn child_exited(&self) -> bool {
        self.child_exited
    }

    /// Whether the exec was started with `reap_group_on_exit`.
    #[getter]
    fn reap(&self) -> bool {
        self.reap
    }

    /// Live pids whose process group is `pgid`, read from `/proc` inside the guest.
    /// Zombies are not listed. Empty once the group is gone.
    #[getter]
    fn pids(&self) -> Vec<u32> {
        self.pids.clone()
    }

    fn __repr__(&self) -> String {
        format!(
            "ProcGroup(exec_id={:?}, pgid={:?}, child_exited={}, reap={}, pids={:?})",
            self.exec_id, self.pgid, self.child_exited, self.reap, self.pids
        )
    }
}

/// Where a session lives, which decides how it is reached.
///
/// Two variants because there are two real cases and they cannot be unified without
/// giving something up. See the module docs.
#[derive(Clone)]
pub(crate) enum Held {
    /// A session this object owns, from [`PySession::direct`] or [`PySession::attach`].
    /// A clone of it is a clone of two `Arc`s and the endpoint, which is what each call takes.
    Owned(Session),
    /// A session inside a sandbox, reached under the sandbox's lock.
    ///
    /// The sandbox is the same lock [`crate::sandbox::PySandbox`] holds, so a `terminate()`
    /// on the sandbox and a `run()` on the session cannot interleave — which is the runtime
    /// spelling of the core's `&mut self`.
    InSandbox(SharedSandbox),
}

/// One call's hold on the live session: the owned one, or the sandbox's under its lock for as
/// long as the call runs.
pub(crate) enum Lease {
    Owned(Session),
    Locked(tokio::sync::OwnedMutexGuard<Sandbox>),
}

impl Held {
    /// Waits for the sandbox's lock when the session is a sandbox's. An owned guard rather
    /// than a borrow, so the future a call builds owns everything it touches and can run as a
    /// task on the shared runtime.
    pub(crate) async fn lease(self) -> Lease {
        match self {
            Held::Owned(session) => Lease::Owned(session),
            Held::InSandbox(sandbox) => Lease::Locked(sandbox.lock_owned().await),
        }
    }
}

impl Lease {
    /// The live session, or the error naming why a sandbox has none.
    pub(crate) fn session(&self) -> Result<&Session, Error> {
        match self {
            Lease::Owned(session) => Ok(session),
            Lease::Locked(sandbox) => live_session(sandbox),
        }
    }
}

/// A sandbox's session, or the reason it has none.
///
/// A sandbox that has been terminated has no session, and the error says which of the
/// two states it is in rather than "no session" — the core sets that up by clearing
/// the session in `terminate`.
pub(crate) fn live_session(sandbox: &Sandbox) -> Result<&Session, Error> {
    sandbox.session().ok_or_else(|| {
        Error::new(
            ErrorKind::Precondition,
            format!(
                "this sandbox holds no session: it is {} and terminate() drops the \
                 session because the only remaining use of its cached proxy token \
                 would be a request against a VM that is going away. A new VM needs \
                 a new Sandbox.",
                sandbox.lifecycle()
            ),
        )
    })
}

/// A future that leases the session for one call and runs `$body` with `$session` bound to
/// the live `&Session`: what each method's two spellings drive.
///
/// A macro because the body borrows the lease across `.await`, and a closure answering a
/// future that borrows its argument has no `Send` bound to name on stable Rust, which the
/// runtime's `spawn` needs. The expansion is an `async move` block that owns the lease, so
/// the borrow stays inside it.
macro_rules! session_op {
    ($held:expr, |$session:ident| $body:expr) => {{
        let held: $crate::session::Held = $held;
        async move {
            let lease = held.lease().await;
            let $session = lease.session()?;
            $body
        }
    }};
}
pub(crate) use session_op;

/// One running MicroVM's control API, with the proxy auth handled for you.
#[pyclass(frozen, name = "Session", module = "microvms")]
pub struct PySession {
    held: Held,
}

impl PySession {
    /// A session that reaches into `sandbox`.
    pub(crate) fn in_sandbox(sandbox: SharedSandbox) -> Self {
        Self {
            held: Held::InSandbox(sandbox),
        }
    }

    /// The session's place, for a future that runs one call.
    pub(crate) fn held(&self) -> Held {
        self.held.clone()
    }

    /// Runs `body` against the live session now, whichever way this object holds one: what
    /// the properties and the calls that make no request use.
    ///
    /// The closure shape is what keeps the lock scope honest: it is held for exactly one
    /// call and released before returning into Python, so nothing can hold it across a
    /// Python callback and deadlock against the GIL.
    pub(crate) fn with<T>(
        &self,
        py: Python<'_>,
        body: impl FnOnce(&Session) -> Result<T, Error>,
    ) -> Result<T, Error> {
        match &self.held {
            Held::Owned(session) => body(session),
            Held::InSandbox(sandbox) => body(live_session(&runtime::lock_now(py, sandbox))?),
        }
    }

    fn health_op(&self) -> impl Future<Output = Result<PyHealth, Error>> + Send + 'static {
        session_op!(self.held(), |session| session
            .health()
            .await
            .map(PyHealth::wrap))
    }

    fn wait_until_ready_op(
        &self,
        timeout: Duration,
    ) -> impl Future<Output = Result<PyHealth, Error>> + Send + 'static {
        session_op!(self.held(), |session| session
            .wait_until_ready(timeout)
            .await
            .map(PyHealth::wrap))
    }

    /// The exec start every one of `run`, `spawn` and `run_to_completion` sends.
    fn start_op(
        &self,
        request: protocol::exec::StartRequest,
    ) -> impl Future<Output = Result<ExecHandle, Error>> + Send + 'static {
        session_op!(self.held(), |session| session.run(request).await)
    }

    fn run_sync_op(
        &self,
        request: protocol::exec::StartRequest,
        timeout: Duration,
    ) -> impl Future<Output = Result<PyExecResult, Error>> + Send + 'static {
        session_op!(self.held(), |session| session
            .run_sync(request, timeout)
            .await
            .map(PyExecResult::wrap))
    }

    fn kill_op(
        &self,
        exec_id: String,
    ) -> impl Future<Output = Result<bool, Error>> + Send + 'static {
        session_op!(self.held(), |session| session.kill(&exec_id).await)
    }

    fn procs_op(&self) -> impl Future<Output = Result<Vec<PyProcGroup>, Error>> + Send + 'static {
        session_op!(self.held(), |session| Ok(session
            .procs()
            .await?
            .procs
            .into_iter()
            .map(PyProcGroup::wrap)
            .collect()))
    }

    /// Generic over the bytes so the blocking spelling lends its borrowed `bytes` and the
    /// awaitable one hands over an owned `PyBackedBytes`, with no copy on either path.
    fn upload_file_op<D>(
        &self,
        path: String,
        data: D,
        mode: Option<String>,
    ) -> impl Future<Output = Result<(), Error>> + Send + use<D>
    where
        D: AsRef<[u8]> + Send,
    {
        session_op!(self.held(), |session| session
            .upload_file(&path, data.as_ref(), mode.as_deref())
            .await)
    }

    fn download_file_op(
        &self,
        path: String,
        start_line: Option<u64>,
        end_line: Option<u64>,
    ) -> impl Future<Output = Result<Vec<u8>, Error>> + Send + 'static {
        session_op!(self.held(), |session| session
            .download_file_lines(&path, start_line, end_line)
            .await)
    }

    fn download_dir_op(
        &self,
        remote: String,
        local_dir: std::path::PathBuf,
        globs: Vec<String>,
    ) -> impl Future<Output = Result<Vec<crate::workspace::PyDownloadedFile>, Error>> + Send + 'static
    {
        session_op!(self.held(), |session| Ok(session
            .download_dir(
                &microvms_core::workspace::DiskTree,
                &remote,
                &globs,
                &local_dir,
            )
            .await?
            .into_iter()
            .map(Into::into)
            .collect()))
    }

    fn sync_dir_op(
        &self,
        local_dir: std::path::PathBuf,
        full: bool,
        delete_timeout: Duration,
    ) -> impl Future<Output = Result<crate::workspace::PySyncReport, Error>> + Send + 'static {
        session_op!(self.held(), |session| session
            .sync_dir(
                &microvms_core::workspace::DiskTree,
                &local_dir,
                full,
                delete_timeout,
            )
            .await
            .map(Into::into))
    }

    fn file_exists_op(
        &self,
        path: String,
    ) -> impl Future<Output = Result<bool, Error>> + Send + 'static {
        session_op!(self.held(), |session| session.file_exists(&path).await)
    }

    /// Generic over the bytes, for `upload_file_op`'s reason.
    fn upload_tar_op<D>(
        &self,
        remote: String,
        archive: D,
    ) -> impl Future<Output = Result<(), Error>> + Send + use<D>
    where
        D: AsRef<[u8]> + Send,
    {
        session_op!(self.held(), |session| session
            .upload_tar(&remote, archive.as_ref())
            .await)
    }

    fn download_tar_op(
        &self,
        remote: String,
    ) -> impl Future<Output = Result<Vec<u8>, Error>> + Send + 'static {
        session_op!(self.held(), |session| session.download_tar(&remote).await)
    }

    fn connect_headers_op(
        &self,
        port: u16,
    ) -> impl Future<Output = Result<std::collections::HashMap<String, String>, Error>> + Send + 'static
    {
        session_op!(self.held(), |session| Ok(session
            .connect_headers(port)
            .await?
            .into_iter()
            .collect()))
    }

    fn connect_subprotocols_op(
        &self,
        port: u16,
    ) -> impl Future<Output = Result<Option<Vec<String>>, Error>> + Send + 'static {
        session_op!(self.held(), |session| Ok(session
            .connect_subprotocols(port)
            .await?
            .map(|offered| offered.to_vec())))
    }

    /// The tunnel's start: the target is read under the lock and the listener bound after it,
    /// so a tunnel serving in the background holds no lock.
    fn tunnel_op(
        &self,
        bind: std::net::SocketAddr,
        guest_port: u16,
        identity: Option<microvms_core::identity::TunnelIdentity>,
        max_connections: Option<u32>,
    ) -> impl Future<Output = Result<crate::serve::PyTunnel, Error>> + Send + 'static {
        let held = self.held();
        async move {
            let target = {
                let lease = held.lease().await;
                serve::TunnelTarget::for_session(lease.session()?, guest_port, identity)
            };
            let limits = serve::ServeLimits { max_connections };
            serve::start_tunnel(bind, target, limits)
                .await
                .map(crate::serve::PyTunnel::new)
        }
    }

    /// The forward's start, for `tunnel_op`'s reason.
    fn port_forward_op(
        &self,
        bind: std::net::SocketAddr,
        guest_port: u16,
        max_connections: Option<u32>,
    ) -> impl Future<Output = Result<crate::serve::PyPortForward, Error>> + Send + 'static {
        let held = self.held();
        async move {
            let (endpoint, auth) = {
                let lease = held.lease().await;
                let session = lease.session()?;
                (
                    session.endpoint().to_string(),
                    session.proxy_auth().cloned(),
                )
            };
            let limits = serve::ServeLimits { max_connections };
            serve::start_forward(bind, &endpoint, guest_port, auth, limits)
                .await
                .map(crate::serve::PyPortForward::new)
        }
    }
}

/// The start request every exec-starting method's keywords build.
///
/// Every setter is called unconditionally, for the reason `run`'s signature gives.
#[allow(
    clippy::too_many_arguments,
    reason = "one parameter per protocol::exec::StartRequest field the keywords set"
)]
fn start_request(
    command: Command,
    shell: ShellArg,
    cwd: Option<String>,
    env: Option<std::collections::HashMap<String, String>>,
    user: Option<Principal>,
    group: Option<Principal>,
    timeout_sec: Option<f64>,
    stdin: bool,
    exec_id: Option<String>,
    reap_group_on_exit: bool,
    inherit_image_env: bool,
) -> protocol::exec::StartRequest {
    protocol::exec::StartRequest::new(exec_id.unwrap_or_else(mint_exec_id), command.into_argv())
        .with_shell(shell)
        .with_cwd(cwd)
        .with_env(env.unwrap_or_default())
        .with_user(user.map(Into::into))
        .with_group(group.map(Into::into))
        .with_timeout_sec(timeout_sec)
        .with_stdin(stdin)
        .with_reap_group_on_exit(reap_group_on_exit)
        .with_inherit_image_env(inherit_image_env)
}

/// `spawn`'s stream options and gap policy, checked before anything starts so a bad policy
/// or idle timeout costs no exec.
fn split_options(
    offset: u64,
    reconnect: bool,
    max_reconnects: Option<u32>,
    idle_timeout: Option<f64>,
    gap_policy: Option<&str>,
) -> Result<(StreamOptions, GapPolicy), Error> {
    let policy: GapPolicy = gap_policy.map(str::parse).transpose()?.unwrap_or_default();
    let defaults = StreamOptions::default();
    let options = StreamOptions {
        offset,
        reconnect,
        max_reconnects: max_reconnects.unwrap_or(defaults.max_reconnects),
        idle_timeout: match idle_timeout {
            Some(idle) => seconds(idle)?,
            None => defaults.idle_timeout,
        },
        ..defaults
    };
    Ok((options, policy))
}

/// `sync_dir`'s delete deadline, core's default when unset.
fn sync_delete_timeout(delete_timeout: Option<f64>) -> Result<Duration, Error> {
    Ok(delete_timeout
        .map(seconds)
        .transpose()?
        .unwrap_or(microvms_core::workspace::DEFAULT_SYNC_DELETE_TIMEOUT))
}

/// `run_to_completion`'s plan, both spellings'. Planned before the start, so a bad
/// `timeout_sec` is refused with nothing running.
fn completion_plan(
    request: &protocol::exec::StartRequest,
    client_grace_sec: f64,
) -> PyResult<CompletionPlan> {
    let options = CompletionOptions {
        client_grace: seconds(client_grace_sec).map_err(CoreError)?,
        ..CompletionOptions::default()
    };
    Ok(CompletionPlan::new(request, options).map_err(CoreError)?)
}

/// Hands one chunk to `on_output` under the GIL and answers whether the drive reads on. The
/// first exception is kept in `raised` for the caller to re-raise, and stops delivery.
fn deliver(
    callback: &Py<PyAny>,
    event: microvms_core::session::ExecEvent,
    raised: &std::sync::Mutex<Option<PyErr>>,
) -> std::ops::ControlFlow<()> {
    let delivered = Python::attach(|py| {
        let chunk = crate::exec::StreamEvent::from(event);
        callback.call1(py, (chunk,)).map(drop)
    });
    match delivered {
        Ok(()) => std::ops::ControlFlow::Continue(()),
        Err(error) => {
            *raised
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner) = Some(error);
            std::ops::ControlFlow::Break(())
        }
    }
}

/// Python bytes as owned bytes a task can hold, without copying them.
fn backed(data: Py<PyBytes>) -> PyBackedBytes {
    Python::attach(|py| PyBackedBytes::from(data.into_bound(py)))
}

#[pymethods]
impl PySession {
    /// A session against a daemon reached **directly**, with no proxy headers.
    ///
    /// The shape for a local binary, a test server, or a VM reached over a tunnel. There
    /// is deliberately no constructor that takes a proxy token: minting one is the
    /// control plane's job and it happens inside every request (TRAP-9), so a caller
    /// handing a token in would be handing in one that expires.
    #[staticmethod]
    fn direct(endpoint: &str, agent_token: &str) -> PyCoreResult<PySession> {
        Ok(PySession {
            held: Held::Owned(Session::direct(endpoint, agent_token)?),
        })
    }

    /// Reattach using a private control record, without bootstrapping again.
    /// AWS credentials mint fresh proxy tokens. This session owns its transport and
    /// shares no sandbox lock; supervisors should attach separately for keepalives,
    /// with a short `request_timeout`. Never publish or log `agent_token`.
    #[staticmethod]
    #[pyo3(signature = (region, microvm_id, endpoint, agent_token, *, port=None, request_timeout=None))]
    #[allow(
        clippy::too_many_arguments,
        reason = "private attach record plus transport settings"
    )]
    fn attach(
        py: Python<'_>,
        region: PyRegion,
        microvm_id: String,
        endpoint: String,
        agent_token: String,
        port: Option<u16>,
        request_timeout: Option<f64>,
    ) -> PyCoreResult<Self> {
        let timeout = request_timeout.map(seconds).transpose()?;
        let session = runtime::block_on(
            py,
            Session::attach(
                region.inner,
                microvm_id,
                endpoint,
                agent_token,
                port,
                timeout,
            ),
        )?;
        Ok(Self {
            held: Held::Owned(session),
        })
    }

    /// The awaitable twin of `Session.attach`.
    #[staticmethod]
    #[pyo3(signature = (region, microvm_id, endpoint, agent_token, *, port=None, request_timeout=None))]
    #[allow(
        clippy::too_many_arguments,
        reason = "private attach record plus transport settings"
    )]
    async fn attach_async(
        region: PyRegion,
        microvm_id: String,
        endpoint: String,
        agent_token: String,
        port: Option<u16>,
        request_timeout: Option<f64>,
    ) -> PyCoreResult<Self> {
        let timeout = request_timeout.map(seconds).transpose()?;
        let session = runtime::spawn(Session::attach(
            region.inner,
            microvm_id,
            endpoint,
            agent_token,
            port,
            timeout,
        ))
        .await?;
        Ok(Self {
            held: Held::Owned(session),
        })
    }

    /// The guest bearer credential. Store only in a private encrypted control record.
    /// It is never included in repr or ordinary status output.
    #[getter]
    fn agent_token(&self, py: Python<'_>) -> PyCoreResult<String> {
        Ok(self.with(py, |session| Ok(session.agent_token().to_string()))?)
    }

    /// The endpoint this session addresses.
    ///
    /// A `String` rather than a `&str` because a sandbox-held session reads it under the
    /// lock, and a reference would outlive the guard.
    #[getter]
    fn endpoint(&self, py: Python<'_>) -> PyCoreResult<String> {
        Ok(self.with(py, |session| Ok(session.endpoint().to_string()))?)
    }

    /// The port the proxy token is scoped to.
    #[getter]
    fn port(&self, py: Python<'_>) -> PyCoreResult<u16> {
        Ok(self.with(py, |session| Ok(session.port()))?)
    }

    /// The launch's egress posture: `"open"`, `"unsealed"`, `"best-effort"`, or `"sealed"`.
    ///
    /// The value the CLI envelope's `egressPosture` reports for the same launch options, and
    /// what `egress_posture_for` answers before the launch. A session that does not hold its
    /// launch options (`Session.direct`, `Session.attach`, an adopted sandbox) reports
    /// `"unsealed"`. Advertise network isolation only for `"sealed"`.
    #[getter]
    fn egress_posture(&self, py: Python<'_>) -> PyCoreResult<&'static str> {
        Ok(self.with(py, |session| Ok(session.egress_posture().as_str()))?)
    }

    /// Unauthenticated liveness.
    fn health(&self, py: Python<'_>) -> PyCoreResult<PyHealth> {
        Ok(runtime::block_on(py, self.health_op())?)
    }

    /// The awaitable twin of `health`.
    async fn health_async(&self) -> PyCoreResult<PyHealth> {
        Ok(runtime::spawn(self.health_op()).await?)
    }

    /// Keeps the VM awake by polling health from this process until stopped.
    ///
    /// The platform counts only inbound requests as activity, so an exec working with no
    /// client traffic is suspended once `maxIdleDurationSeconds` passes. This polls
    /// `/v1/health` every `interval` seconds (default: a third of the idle window, at most
    /// 20) on a background task and returns at once. `while_busy` ends it once no exec is
    /// running; `max_duration` ends it after that many seconds. `idle_window` is the VM's
    /// `maxIdleDurationSeconds`: a sandbox-held session knows it, an attached one assumes
    /// the platform minimum of 60, and `interval` may be at most half of it.
    /// `tolerated_errors` is how many retryable poll failures in a row it retries, a second
    /// apart, before it ends with the error; omitted, it's the core's
    /// `DEFAULT_TOLERATED_ERRORS`.
    ///
    /// On a sandbox-held session a suspend or terminate through the sandbox ends the
    /// keepalive before its next poll. Stop it before suspending through anything else,
    /// or the next poll auto-resumes the VM. Dropping the returned handle stops it.
    #[pyo3(signature = (
        interval=None,
        *,
        while_busy=false,
        max_duration=None,
        idle_window=None,
        tolerated_errors=None,
    ))]
    fn keep_awake(
        &self,
        py: Python<'_>,
        interval: Option<f64>,
        while_busy: bool,
        max_duration: Option<f64>,
        idle_window: Option<f64>,
        tolerated_errors: Option<u32>,
    ) -> PyCoreResult<crate::keepalive::PyKeepAwake> {
        let explicit = idle_window.map(seconds).transpose()?;
        let (source, known) = match &self.held {
            Held::Owned(session) => (crate::keepalive::Source::Owned(session.clone()), None),
            Held::InSandbox(sandbox) => crate::keepalive::Source::in_sandbox(py, sandbox)
                .ok_or_else(|| {
                    Error::new(
                        ErrorKind::Precondition,
                        "this sandbox holds no running session to keep awake",
                    )
                })?,
        };
        let policy = crate::keepalive::policy(
            explicit.or(known),
            interval,
            while_busy,
            max_duration,
            tolerated_errors,
        )?;
        Ok(crate::keepalive::PyKeepAwake::start(source, policy)?)
    }

    /// Polls health until the daemon reports bootstrapped.
    ///
    /// Connection errors on the way are expected rather than exceptional: a VM that has
    /// just reached RUNNING commonly refuses a connection or two before the proxy path is
    /// wired up. A *fatal* error ends the wait at once, because retrying a 401 until the
    /// deadline is the mistake the retryable split exists to prevent.
    #[pyo3(signature = (timeout=DEFAULT_BOOTSTRAP_TIMEOUT))]
    fn wait_until_ready(&self, py: Python<'_>, timeout: f64) -> PyCoreResult<PyHealth> {
        let timeout = seconds(timeout)?;
        Ok(runtime::block_on(py, self.wait_until_ready_op(timeout))?)
    }

    /// The awaitable twin of `wait_until_ready`.
    #[pyo3(signature = (timeout=DEFAULT_BOOTSTRAP_TIMEOUT))]
    async fn wait_until_ready_async(&self, timeout: f64) -> PyCoreResult<PyHealth> {
        let timeout = seconds(timeout)?;
        Ok(runtime::spawn(self.wait_until_ready_op(timeout)).await?)
    }

    /// Starts a command and returns its handle. Does not wait.
    ///
    /// `command` is a list, or a string that becomes a one-element argv — never
    /// whitespace-split. `shell=True` wants a single script string for `/bin/sh -c`;
    /// `shell="bash"` runs it under a shell the daemon resolves in the guest, and a shell
    /// the guest does not have is refused (`unknown_shell`) before anything starts.
    ///
    /// `user` and `group` are a numeric id or a name the daemon resolves against the
    /// guest's `/etc/passwd` and `/etc/group`; an unknown name is refused (`unknown_user`,
    /// `unknown_group`) before anything starts. A user with a passwd row gets `HOME`,
    /// `USER` and `LOGNAME` from it, beneath the launch environment and `env`.
    ///
    /// `inherit_image_env` starts the child's environment from the image's `ENV` (minus
    /// `AGENTD_*`, never the token), beneath everything else; off by default, which keeps
    /// the child's environment exactly the launch environment plus `env`.
    ///
    /// `reap_group_on_exit` asks the daemon to signal the whole process group once the
    /// command's own child exits, so nothing it backgrounded outlives it; off by default,
    /// which keeps the backgrounded-grandchild-output guarantee for callers who rely on it.
    // `shell`, `stdin`, `reap_group_on_exit` and `inherit_image_env` restate the wire's defaults
    // (`StartRequest::new`'s) here and in `run_sync`, and `run_to_completion` restates `shell`,
    // `reap_group_on_exit` and `inherit_image_env`, because a Python signature shows a value: `Option<bool> = None`
    // would turn the stub's `bool = False` into `bool | None`, an API change. So these setters
    // are called unconditionally, and a wire default that changes has to change here too.
    // `parity:check`'s `[[default]]` rows are where that's caught (#300).
    #[pyo3(signature = (
        command,
        *,
        shell=ShellArg::Flag(false),
        cwd=None,
        env=None,
        user=None,
        group=None,
        timeout_sec=None,
        stdin=false,
        exec_id=None,
        reap_group_on_exit=false,
        inherit_image_env=false,
    ))]
    #[allow(
        clippy::too_many_arguments,
        reason = "one keyword-only parameter per \
         protocol::exec::StartRequest field, which is what keeps a caller from having to \
         build a dict whose keys are unchecked"
    )]
    fn run(
        &self,
        py: Python<'_>,
        command: Command,
        shell: ShellArg,
        cwd: Option<String>,
        env: Option<std::collections::HashMap<String, String>>,
        user: Option<Principal>,
        group: Option<Principal>,
        timeout_sec: Option<f64>,
        stdin: bool,
        exec_id: Option<String>,
        reap_group_on_exit: bool,
        inherit_image_env: bool,
    ) -> PyCoreResult<PyExecHandle> {
        // Built before the block rather than inside it: a `Command` extraction needs the GIL
        // and the future does not have it.
        let request = start_request(
            command,
            shell,
            cwd,
            env,
            user,
            group,
            timeout_sec,
            stdin,
            exec_id,
            reap_group_on_exit,
            inherit_image_env,
        );
        Ok(PyExecHandle::wrap(runtime::block_on(
            py,
            self.start_op(request),
        )?))
    }

    /// The awaitable twin of `run`, with its keywords. Cancelling it before the daemon
    /// answers may leave the exec started, under the `exec_id` the call was given or minted;
    /// `Session.exec(exec_id)` reaches it, which is why a caller that cancels passes one.
    #[pyo3(signature = (
        command,
        *,
        shell=ShellArg::Flag(false),
        cwd=None,
        env=None,
        user=None,
        group=None,
        timeout_sec=None,
        stdin=false,
        exec_id=None,
        reap_group_on_exit=false,
        inherit_image_env=false,
    ))]
    #[allow(
        clippy::too_many_arguments,
        reason = "`run`'s keywords, one per protocol::exec::StartRequest field"
    )]
    async fn run_async(
        &self,
        command: Command,
        shell: ShellArg,
        cwd: Option<String>,
        env: Option<std::collections::HashMap<String, String>>,
        user: Option<Principal>,
        group: Option<Principal>,
        timeout_sec: Option<f64>,
        stdin: bool,
        exec_id: Option<String>,
        reap_group_on_exit: bool,
        inherit_image_env: bool,
    ) -> PyCoreResult<PyExecHandle> {
        let request = start_request(
            command,
            shell,
            cwd,
            env,
            user,
            group,
            timeout_sec,
            stdin,
            exec_id,
            reap_group_on_exit,
            inherit_image_env,
        );
        Ok(PyExecHandle::wrap(
            runtime::spawn(self.start_op(request)).await?,
        ))
    }

    /// Starts a command and returns it as two byte iterators, a `wait()`, and a `kill()`.
    ///
    /// The **process** shape, as against `run`'s handle: the same start request and keyword
    /// arguments, so `spawn` and `run` with one `exec_id` address one server-side child.
    /// `proc.stdout` and `proc.stderr` iterate `bytes`, split out of the one stream the
    /// daemon sends, which reconnects at the byte cursor after a cut, so a suspend and resume
    /// doesn't end them early. Read both sides, from two threads when a command writes much
    /// to both: each holds one unread chunk, like a pipe.
    ///
    /// `gap_policy` is what an evicted byte range does. `"error"`, the default when it's
    /// `None`, raises `PlatformError` (wire kind `OutputGap`) from both iterators naming the
    /// range, since the wire can't say which side lost the bytes; `"event"` records it on
    /// `proc.gaps` and keeps both going. `offset`, `reconnect`, `max_reconnects` and
    /// `idle_timeout` are `ExecHandle.stream()`'s.
    // The start request's keyword defaults restate the wire's, for the reason `run` gives.
    // `offset` and `reconnect` restate `StreamOptions::default()`'s, as `ExecHandle.stream()`
    // does. `gap_policy` is `None` rather than `"error"`, so core's default stays the one copy.
    #[pyo3(signature = (
        command,
        *,
        shell=ShellArg::Flag(false),
        cwd=None,
        env=None,
        user=None,
        group=None,
        timeout_sec=None,
        stdin=false,
        exec_id=None,
        reap_group_on_exit=false,
        inherit_image_env=false,
        offset=0,
        reconnect=true,
        max_reconnects=None,
        idle_timeout=None,
        gap_policy=None,
    ))]
    #[allow(
        clippy::too_many_arguments,
        reason = "the run() signature plus the stream's options, deliberately"
    )]
    fn spawn(
        &self,
        py: Python<'_>,
        command: Command,
        shell: ShellArg,
        cwd: Option<String>,
        env: Option<std::collections::HashMap<String, String>>,
        user: Option<Principal>,
        group: Option<Principal>,
        timeout_sec: Option<f64>,
        stdin: bool,
        exec_id: Option<String>,
        reap_group_on_exit: bool,
        inherit_image_env: bool,
        offset: u64,
        reconnect: bool,
        max_reconnects: Option<u32>,
        idle_timeout: Option<f64>,
        gap_policy: Option<&str>,
    ) -> PyResult<crate::process::PyExecProcess> {
        let (options, policy) =
            split_options(offset, reconnect, max_reconnects, idle_timeout, gap_policy)
                .map_err(CoreError)?;
        let request = start_request(
            command,
            shell,
            cwd,
            env,
            user,
            group,
            timeout_sec,
            stdin,
            exec_id,
            reap_group_on_exit,
            inherit_image_env,
        );
        let handle = runtime::block_on(py, self.start_op(request)).map_err(CoreError)?;
        crate::process::PyExecProcess::start(py, handle, options, policy)
    }

    /// The awaitable twin of `spawn`, with its keywords. The process it answers iterates
    /// with `async for` as well as `for`; cancelling the call is `run_async`'s.
    #[pyo3(signature = (
        command,
        *,
        shell=ShellArg::Flag(false),
        cwd=None,
        env=None,
        user=None,
        group=None,
        timeout_sec=None,
        stdin=false,
        exec_id=None,
        reap_group_on_exit=false,
        inherit_image_env=false,
        offset=0,
        reconnect=true,
        max_reconnects=None,
        idle_timeout=None,
        gap_policy=None,
    ))]
    #[allow(
        clippy::too_many_arguments,
        reason = "the run() signature plus the stream's options, deliberately"
    )]
    async fn spawn_async(
        &self,
        command: Command,
        shell: ShellArg,
        cwd: Option<String>,
        env: Option<std::collections::HashMap<String, String>>,
        user: Option<Principal>,
        group: Option<Principal>,
        timeout_sec: Option<f64>,
        stdin: bool,
        exec_id: Option<String>,
        reap_group_on_exit: bool,
        inherit_image_env: bool,
        offset: u64,
        reconnect: bool,
        max_reconnects: Option<u32>,
        idle_timeout: Option<f64>,
        gap_policy: Option<String>,
    ) -> PyResult<crate::process::PyExecProcess> {
        let (options, policy) = split_options(
            offset,
            reconnect,
            max_reconnects,
            idle_timeout,
            gap_policy.as_deref(),
        )
        .map_err(CoreError)?;
        let request = start_request(
            command,
            shell,
            cwd,
            env,
            user,
            group,
            timeout_sec,
            stdin,
            exec_id,
            reap_group_on_exit,
            inherit_image_env,
        );
        let handle = runtime::spawn(self.start_op(request))
            .await
            .map_err(CoreError)?;
        Python::attach(|py| crate::process::PyExecProcess::start(py, handle, options, policy))
    }

    /// A handle for an exec started earlier, possibly by another process.
    ///
    /// The reattach path. Nothing is checked against the daemon here — the handle is an
    /// id plus a transport, and a poll is what discovers whether the exec exists.
    fn exec(&self, py: Python<'_>, exec_id: &str) -> PyCoreResult<PyExecHandle> {
        Ok(PyExecHandle::wrap(
            self.with(py, |session| Ok(session.exec(exec_id)))?,
        ))
    }

    /// Start, wait, ack. The one-shot shape, for when output is all you want.
    #[pyo3(signature = (
        command,
        *,
        timeout=DEFAULT_RUN_SYNC_TIMEOUT,
        shell=ShellArg::Flag(false),
        cwd=None,
        env=None,
        user=None,
        group=None,
        timeout_sec=None,
        stdin=false,
        exec_id=None,
        reap_group_on_exit=false,
        inherit_image_env=false,
    ))]
    #[allow(
        clippy::too_many_arguments,
        reason = "the run() signature plus the wait \
         deadline, deliberately"
    )]
    fn run_sync(
        &self,
        py: Python<'_>,
        command: Command,
        timeout: f64,
        shell: ShellArg,
        cwd: Option<String>,
        env: Option<std::collections::HashMap<String, String>>,
        user: Option<Principal>,
        group: Option<Principal>,
        timeout_sec: Option<f64>,
        stdin: bool,
        exec_id: Option<String>,
        reap_group_on_exit: bool,
        inherit_image_env: bool,
    ) -> PyCoreResult<PyExecResult> {
        let request = start_request(
            command,
            shell,
            cwd,
            env,
            user,
            group,
            timeout_sec,
            stdin,
            exec_id,
            reap_group_on_exit,
            inherit_image_env,
        );
        let timeout = seconds(timeout)?;
        Ok(runtime::block_on(py, self.run_sync_op(request, timeout))?)
    }

    /// The awaitable twin of `run_sync`, with its keywords. Cancelling it stops the wait,
    /// as a `timeout` would: the exec keeps running and its output stays until acked.
    #[pyo3(signature = (
        command,
        *,
        timeout=DEFAULT_RUN_SYNC_TIMEOUT,
        shell=ShellArg::Flag(false),
        cwd=None,
        env=None,
        user=None,
        group=None,
        timeout_sec=None,
        stdin=false,
        exec_id=None,
        reap_group_on_exit=false,
        inherit_image_env=false,
    ))]
    #[allow(
        clippy::too_many_arguments,
        reason = "the run() signature plus the wait deadline, deliberately"
    )]
    async fn run_sync_async(
        &self,
        command: Command,
        timeout: f64,
        shell: ShellArg,
        cwd: Option<String>,
        env: Option<std::collections::HashMap<String, String>>,
        user: Option<Principal>,
        group: Option<Principal>,
        timeout_sec: Option<f64>,
        stdin: bool,
        exec_id: Option<String>,
        reap_group_on_exit: bool,
        inherit_image_env: bool,
    ) -> PyCoreResult<PyExecResult> {
        let request = start_request(
            command,
            shell,
            cwd,
            env,
            user,
            group,
            timeout_sec,
            stdin,
            exec_id,
            reap_group_on_exit,
            inherit_image_env,
        );
        let timeout = seconds(timeout)?;
        Ok(runtime::spawn(self.run_sync_op(request, timeout)).await?)
    }

    /// Start, stream, and collect one command: exactly one `ExecResult` back (BIND-6..10).
    ///
    /// With `on_output`, each `OutputChunk` is handed to it as it arrives. The result then
    /// comes from the ack that follows the terminal `exit` event, or, when the stream ends
    /// without one, from a wait and ack. When `timeout_sec + client_grace_sec` passes first
    /// (or, with no `timeout_sec`, the VM's maximum lifetime), the process group is killed
    /// and the exec waited for and acked within `client_grace_sec` once more; if that fails
    /// too the result is synthesized with `posix_exit_code` 124. `posix_exit_code` and
    /// `notes` say what ended the command.
    ///
    /// An exception from `on_output` stops delivery; the exec is still waited for and acked
    /// so nothing is left behind, and then the exception is re-raised. `shell`, `user`,
    /// `group`, `reap_group_on_exit` and `inherit_image_env` mean what they mean on `run()`:
    /// `shell="bash"` with a script string runs it under bash, which dash-based images need for
    /// `pipefail`.
    #[pyo3(signature = (
        command,
        *,
        on_output=None,
        shell=ShellArg::Flag(false),
        cwd=None,
        env=None,
        user=None,
        group=None,
        timeout_sec=None,
        exec_id=None,
        reap_group_on_exit=false,
        inherit_image_env=false,
        client_grace_sec=DEFAULT_CLIENT_GRACE_SEC,
    ))]
    #[allow(
        clippy::too_many_arguments,
        reason = "the run() signature plus the callback and the client grace, one \
         keyword-only parameter each"
    )]
    fn run_to_completion(
        &self,
        py: Python<'_>,
        command: Command,
        on_output: Option<Py<PyAny>>,
        shell: ShellArg,
        cwd: Option<String>,
        env: Option<std::collections::HashMap<String, String>>,
        user: Option<Principal>,
        group: Option<Principal>,
        timeout_sec: Option<f64>,
        exec_id: Option<String>,
        reap_group_on_exit: bool,
        inherit_image_env: bool,
        client_grace_sec: f64,
    ) -> PyResult<PyExecResult> {
        // `stdin` is the wire's default: a run to completion feeds no stdin.
        let request = start_request(
            command,
            shell,
            cwd,
            env,
            user,
            group,
            timeout_sec,
            false,
            exec_id,
            reap_group_on_exit,
            inherit_image_env,
        );
        let plan = completion_plan(&request, client_grace_sec)?;
        // Started under the sandbox lock and driven after it is released: the callback runs
        // Python, and Python that reaches back into this sandbox must not find the lock held.
        let handle = runtime::block_on(py, self.start_op(request)).map_err(CoreError)?;
        let raised: Arc<std::sync::Mutex<Option<PyErr>>> = Arc::default();
        let sink = on_output.map(|callback| {
            let raised = Arc::clone(&raised);
            Box::new(move |event| {
                // The drive runs on this thread with the GIL released (`RUNTIME.block_on`),
                // so the callback reattaches for the one call.
                let flow = deliver(&callback, event, &raised);
                Box::pin(std::future::ready(flow)) as OutputFlow
            }) as OutputSink
        });
        let result = py.detach(|| runtime::block_on_detached(plan.drive(&handle, sink)));
        if let Some(error) = raised
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .take()
        {
            return Err(error);
        }
        Ok(PyExecResult::wrap(result.map_err(CoreError)?))
    }

    /// The awaitable twin of `run_to_completion`, with its keywords.
    ///
    /// `on_output` is a plain function, called on the event loop's thread between the
    /// coroutine's steps, one chunk at a time: the drive on the shared runtime waits for each
    /// call to return before it reads on, so a slow callback slows the stream rather than
    /// buffering it. An exception from it stops delivery and is re-raised once the exec is
    /// waited for and acked, as in `run_to_completion`. Cancelling the call stops the drive and
    /// leaves the exec running in the VM.
    #[pyo3(signature = (
        command,
        *,
        on_output=None,
        shell=ShellArg::Flag(false),
        cwd=None,
        env=None,
        user=None,
        group=None,
        timeout_sec=None,
        exec_id=None,
        reap_group_on_exit=false,
        inherit_image_env=false,
        client_grace_sec=DEFAULT_CLIENT_GRACE_SEC,
    ))]
    #[allow(
        clippy::too_many_arguments,
        reason = "the run() signature plus the callback and the client grace, one \
         keyword-only parameter each"
    )]
    async fn run_to_completion_async(
        &self,
        command: Command,
        on_output: Option<Py<PyAny>>,
        shell: ShellArg,
        cwd: Option<String>,
        env: Option<std::collections::HashMap<String, String>>,
        user: Option<Principal>,
        group: Option<Principal>,
        timeout_sec: Option<f64>,
        exec_id: Option<String>,
        reap_group_on_exit: bool,
        inherit_image_env: bool,
        client_grace_sec: f64,
    ) -> PyResult<PyExecResult> {
        let request = start_request(
            command,
            shell,
            cwd,
            env,
            user,
            group,
            timeout_sec,
            false,
            exec_id,
            reap_group_on_exit,
            inherit_image_env,
        );
        let plan = completion_plan(&request, client_grace_sec)?;
        // Each chunk crosses to this coroutine with a one-shot for the flow the callback
        // answers, so the callback runs where an asyncio caller's code runs, and capacity 1
        // keeps the drive one chunk ahead at most.
        let (chunks, mut delivered) = tokio::sync::mpsc::channel::<(
            microvms_core::session::ExecEvent,
            tokio::sync::oneshot::Sender<std::ops::ControlFlow<()>>,
        )>(1);
        let sink = on_output.is_some().then(|| {
            Box::new(move |event| {
                let chunks = chunks.clone();
                Box::pin(async move {
                    let (reply, answer) = tokio::sync::oneshot::channel();
                    if chunks.send((event, reply)).await.is_err() {
                        return std::ops::ControlFlow::Break(());
                    }
                    answer.await.unwrap_or(std::ops::ControlFlow::Break(()))
                }) as OutputFlow
            }) as OutputSink
        });
        let start = self.start_op(request);
        let mut drive = runtime::spawn(async move {
            let handle = start.await?;
            plan.drive(&handle, sink).await
        });
        let raised: Arc<std::sync::Mutex<Option<PyErr>>> = Arc::default();
        let mut open = true;
        let result = loop {
            let next = std::future::poll_fn(|cx| {
                if let std::task::Poll::Ready(result) = std::pin::Pin::new(&mut drive).poll(cx) {
                    return std::task::Poll::Ready(Ok(result));
                }
                if open {
                    match delivered.poll_recv(cx) {
                        std::task::Poll::Ready(Some(chunk)) => {
                            return std::task::Poll::Ready(Err(Some(chunk)));
                        }
                        // The drive dropped its sink: only its result is left to wait for.
                        std::task::Poll::Ready(None) => return std::task::Poll::Ready(Err(None)),
                        std::task::Poll::Pending => {}
                    }
                }
                std::task::Poll::Pending
            })
            .await;
            match next {
                Ok(result) => break result,
                Err(None) => open = false,
                Err(Some((event, reply))) => {
                    let flow = match &on_output {
                        Some(callback) => deliver(callback, event, &raised),
                        None => std::ops::ControlFlow::Break(()),
                    };
                    let _ = reply.send(flow);
                }
            }
        };
        if let Some(error) = raised
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .take()
        {
            return Err(error);
        }
        Ok(PyExecResult::wrap(result.map_err(CoreError)?))
    }

    /// Signals an exec's whole process group. Returns whether anything was signalled.
    fn kill(&self, py: Python<'_>, exec_id: &str) -> PyCoreResult<bool> {
        Ok(runtime::block_on(py, self.kill_op(exec_id.to_string()))?)
    }

    /// The awaitable twin of `kill`.
    async fn kill_async(&self, exec_id: String) -> PyCoreResult<bool> {
        Ok(runtime::spawn(self.kill_op(exec_id)).await?)
    }

    /// Process accounting: every registered exec with its group's live pids.
    ///
    /// The daemon reads `/proc` itself, so this needs no `ps` in the guest. A `ProcGroup`
    /// with `child_exited` and a non-empty `pids` is a command that finished while
    /// something it backgrounded did not; pass its `exec_id` to `kill`.
    fn procs(&self, py: Python<'_>) -> PyCoreResult<Vec<PyProcGroup>> {
        Ok(runtime::block_on(py, self.procs_op())?)
    }

    /// The awaitable twin of `procs`.
    async fn procs_async(&self) -> PyCoreResult<Vec<PyProcGroup>> {
        Ok(runtime::spawn(self.procs_op()).await?)
    }

    /// Writes one file, creating parents. `mode` is an **octal string** (`"0755"`), which
    /// is the daemon's shape — an integer here would be ambiguous between 0o755 and 755.
    #[pyo3(signature = (path, data, *, mode=None))]
    fn upload_file(
        &self,
        py: Python<'_>,
        path: &str,
        data: &[u8],
        mode: Option<&str>,
    ) -> PyCoreResult<()> {
        Ok(runtime::block_on(
            py,
            self.upload_file_op(path.to_string(), data, mode.map(str::to_string)),
        )?)
    }

    /// The awaitable twin of `upload_file`.
    #[pyo3(signature = (path, data, *, mode=None))]
    async fn upload_file_async(
        &self,
        path: String,
        data: Py<PyBytes>,
        mode: Option<String>,
    ) -> PyCoreResult<()> {
        Ok(runtime::spawn(self.upload_file_op(path, backed(data), mode)).await?)
    }

    /// Reads one file, or lines `start_line` through `end_line` of it.
    ///
    /// The range is 1-based and inclusive, and the daemon slices the file, so reading lines
    /// 40 to 60 of a large log reads those lines alone. Either bound may be `None` (line 1,
    /// through EOF), and an `end_line` past the last line reads through EOF. Line 0 and an end
    /// before the start raise `InvalidArgError` before any request.
    #[pyo3(signature = (path, *, start_line=None, end_line=None))]
    fn download_file<'py>(
        &self,
        py: Python<'py>,
        path: &str,
        start_line: Option<u64>,
        end_line: Option<u64>,
    ) -> PyCoreResult<Bound<'py, PyBytes>> {
        let bytes = runtime::block_on(
            py,
            self.download_file_op(path.to_string(), start_line, end_line),
        )?;
        Ok(PyBytes::new(py, &bytes))
    }

    /// The awaitable twin of `download_file`.
    #[pyo3(signature = (path, *, start_line=None, end_line=None))]
    async fn download_file_async(
        &self,
        path: String,
        start_line: Option<u64>,
        end_line: Option<u64>,
    ) -> PyCoreResult<Py<PyBytes>> {
        let bytes = runtime::spawn(self.download_file_op(path, start_line, end_line)).await?;
        Ok(Python::attach(|py| PyBytes::new(py, &bytes).unbind()))
    }

    /// Brings the files of a directory in the VM back under `local_dir`: the regular files
    /// `globs` select, and nothing else.
    ///
    /// The daemon packs `remote`, and the archive describes the VM's filesystem, where
    /// untrusted work runs, so core writes only regular-file members that match a glob, never
    /// under `.git` whatever the globs say, and never outside `local_dir`. A symlink, a special
    /// file or a `../` member is skipped, not refused. `["**"]` brings every regular file back.
    /// Returns what was written. A local directory that can't be written to raises
    /// `InvalidArgError`.
    fn download_dir(
        &self,
        py: Python<'_>,
        remote: &str,
        local_dir: std::path::PathBuf,
        globs: Vec<String>,
    ) -> PyCoreResult<Vec<crate::workspace::PyDownloadedFile>> {
        Ok(runtime::block_on(
            py,
            self.download_dir_op(remote.to_string(), local_dir, globs),
        )?)
    }

    /// The awaitable twin of `download_dir`. The local writes run on the shared runtime, not
    /// on the event loop.
    async fn download_dir_async(
        &self,
        remote: String,
        local_dir: std::path::PathBuf,
        globs: Vec<String>,
    ) -> PyCoreResult<Vec<crate::workspace::PyDownloadedFile>> {
        Ok(runtime::spawn(self.download_dir_op(remote, local_dir, globs)).await?)
    }

    /// Syncs `local_dir` into the VM's `/workspace` once, uploading only what changed.
    ///
    /// Core's one pass, `microvm sync`'s: the local tree is hashed and diffed against the
    /// manifest the last sync left in the VM, the changed members travel as one archive, the
    /// paths gone locally are removed in the VM with one `rm` whose deadline is
    /// `delete_timeout` (core's default when `None`), and the manifest is rewritten. `full=True`
    /// ignores the manifest and uploads everything. `.git`, `target`, `node_modules` and
    /// `.venv` never travel. A tree over the daemon's budgets raises `InvalidArgError` before
    /// anything is sent.
    #[pyo3(signature = (local_dir, *, full=false, delete_timeout=None))]
    fn sync_dir(
        &self,
        py: Python<'_>,
        local_dir: std::path::PathBuf,
        full: bool,
        delete_timeout: Option<f64>,
    ) -> PyCoreResult<crate::workspace::PySyncReport> {
        let delete_timeout = sync_delete_timeout(delete_timeout)?;
        Ok(runtime::block_on(
            py,
            self.sync_dir_op(local_dir, full, delete_timeout),
        )?)
    }

    /// The awaitable twin of `sync_dir`. The hashing and the local reads run on the shared
    /// runtime, not on the event loop.
    #[pyo3(signature = (local_dir, *, full=false, delete_timeout=None))]
    async fn sync_dir_async(
        &self,
        local_dir: std::path::PathBuf,
        full: bool,
        delete_timeout: Option<f64>,
    ) -> PyCoreResult<crate::workspace::PySyncReport> {
        let delete_timeout = sync_delete_timeout(delete_timeout)?;
        Ok(runtime::spawn(self.sync_dir_op(local_dir, full, delete_timeout)).await?)
    }

    /// Whether a path exists, distinguishing absence from every other refusal.
    fn file_exists(&self, py: Python<'_>, path: &str) -> PyCoreResult<bool> {
        Ok(runtime::block_on(
            py,
            self.file_exists_op(path.to_string()),
        )?)
    }

    /// The awaitable twin of `file_exists`.
    async fn file_exists_async(&self, path: String) -> PyCoreResult<bool> {
        Ok(runtime::spawn(self.file_exists_op(path)).await?)
    }

    /// Extracts pre-built tar bytes under `remote`.
    ///
    /// Bytes rather than a local path: packing a directory is the caller's, because the
    /// symlink and permission decisions in a pack belong to whoever knows what the tree
    /// means.
    fn upload_tar(&self, py: Python<'_>, remote: &str, archive: &[u8]) -> PyCoreResult<()> {
        Ok(runtime::block_on(
            py,
            self.upload_tar_op(remote.to_string(), archive),
        )?)
    }

    /// The awaitable twin of `upload_tar`.
    async fn upload_tar_async(&self, remote: String, archive: Py<PyBytes>) -> PyCoreResult<()> {
        Ok(runtime::spawn(self.upload_tar_op(remote, backed(archive))).await?)
    }

    /// The raw tar bytes of a remote tree.
    fn download_tar<'py>(
        &self,
        py: Python<'py>,
        remote: &str,
    ) -> PyCoreResult<Bound<'py, PyBytes>> {
        let bytes = runtime::block_on(py, self.download_tar_op(remote.to_string()))?;
        Ok(PyBytes::new(py, &bytes))
    }

    /// The awaitable twin of `download_tar`.
    async fn download_tar_async(&self, remote: String) -> PyCoreResult<Py<PyBytes>> {
        let bytes = runtime::spawn(self.download_tar_op(remote)).await?;
        Ok(Python::attach(|py| PyBytes::new(py, &bytes).unbind()))
    }

    /// Both proxy headers for `port`, so a caller can open its **own** connection.
    ///
    /// A `dict[str, str]`, for pairing with `endpoint` when something outside this client
    /// opens a socket to a port on this VM. Minted through the session's existing token
    /// cache, so this does not burn a second control-plane call and does not open a second
    /// refresh schedule (TRAP-9).
    ///
    /// **Empty for a direct session**, which is the true answer rather than a missing one: a
    /// daemon reached directly takes no proxy headers.
    ///
    /// The auth value is a bearer credential and this returns it. Nothing here logs or
    /// stores it, and no method *takes* a header map — so a caller cannot feed a forged or
    /// expired one back in.
    fn connect_headers(
        &self,
        py: Python<'_>,
        port: u16,
    ) -> PyCoreResult<std::collections::HashMap<String, String>> {
        Ok(runtime::block_on(py, self.connect_headers_op(port))?)
    }

    /// The awaitable twin of `connect_headers`.
    async fn connect_headers_async(
        &self,
        port: u16,
    ) -> PyCoreResult<std::collections::HashMap<String, String>> {
        Ok(runtime::spawn(self.connect_headers_op(port)).await?)
    }

    /// The three WebSocket subprotocols for `port`, in the order a handshake offers them.
    ///
    /// A MicroVM endpoint takes the auth and the target port as `Sec-WebSocket-Protocol`
    /// values rather than as headers, because the browser `WebSocket` constructor cannot set
    /// a header — and the platform strips all three before forwarding, so a server inside
    /// the VM never negotiates them.
    ///
    /// `None` for a direct session, and deliberately not an empty list: the subprotocol form
    /// exists only for a request through the endpoint proxy, so a list carrying the base
    /// value with no token would open a handshake refused for a reason naming neither the
    /// token nor the port.
    ///
    /// The middle string carries the credential. Same rule as `connect_headers`.
    fn connect_subprotocols(&self, py: Python<'_>, port: u16) -> PyCoreResult<Option<Vec<String>>> {
        Ok(runtime::block_on(py, self.connect_subprotocols_op(port))?)
    }

    /// The awaitable twin of `connect_subprotocols`.
    async fn connect_subprotocols_async(&self, port: u16) -> PyCoreResult<Option<Vec<String>>> {
        Ok(runtime::spawn(self.connect_subprotocols_op(port)).await?)
    }

    /// Serves a local TCP port as a tunnel to `guest_port` in the VM, on a background task,
    /// and returns its handle at once: `microvm tunnel`'s loop.
    ///
    /// Each local connection gets a WebSocket of its own through the endpoint proxy, and a
    /// connection the daemon refuses is listed in the report while the tunnel keeps serving.
    /// `bind` is a `host:port` to listen on, loopback on a port the OS picks by default, and
    /// the handle's `local_address` says which. `max_connections` stops accepting after that
    /// many. With `verify_identity` (a `TunnelIdentity`, such as `Sandbox.tunnel_identity`),
    /// each connection first proves the far end is the daemon of the VM that identity was
    /// launched with, and a connection that can't is refused.
    ///
    /// The tunnel carries this session's credentials to whoever connects, so bind beyond
    /// loopback only on a network you trust. Dropping the handle stops the tunnel.
    #[pyo3(signature = (guest_port, *, bind=None, verify_identity=None, max_connections=None))]
    fn tunnel(
        &self,
        py: Python<'_>,
        guest_port: u16,
        bind: Option<&str>,
        verify_identity: Option<crate::serve::PyTunnelIdentity>,
        max_connections: Option<u32>,
    ) -> PyCoreResult<crate::serve::PyTunnel> {
        let bind = serve::bind_address(bind)?;
        let identity = verify_identity.map(|identity| identity.inner);
        Ok(runtime::block_on(
            py,
            self.tunnel_op(bind, guest_port, identity, max_connections),
        )?)
    }

    /// The awaitable twin of `tunnel`: binds the listener on the shared runtime and answers
    /// the same handle, whose loop runs there either way.
    #[pyo3(signature = (guest_port, *, bind=None, verify_identity=None, max_connections=None))]
    async fn tunnel_async(
        &self,
        guest_port: u16,
        bind: Option<String>,
        verify_identity: Option<crate::serve::PyTunnelIdentity>,
        max_connections: Option<u32>,
    ) -> PyCoreResult<crate::serve::PyTunnel> {
        let bind = serve::bind_address(bind.as_deref())?;
        let identity = verify_identity.map(|identity| identity.inner);
        Ok(runtime::spawn(self.tunnel_op(bind, guest_port, identity, max_connections)).await?)
    }

    /// Serves a local port as an HTTP and WebSocket forward to `guest_port` in the VM, on a
    /// background task, and returns its handle at once: `microvm port-forward`'s loop.
    ///
    /// Connections are served at once rather than one after another, so a slow request
    /// doesn't hold the next. A request the endpoint proxy refuses is listed in the report
    /// with its status while the forward keeps serving. `bind` and `max_connections` are
    /// `tunnel`'s. Dropping the handle stops the forward.
    #[pyo3(signature = (guest_port, *, bind=None, max_connections=None))]
    fn port_forward(
        &self,
        py: Python<'_>,
        guest_port: u16,
        bind: Option<&str>,
        max_connections: Option<u32>,
    ) -> PyCoreResult<crate::serve::PyPortForward> {
        let bind = serve::bind_address(bind)?;
        Ok(runtime::block_on(
            py,
            self.port_forward_op(bind, guest_port, max_connections),
        )?)
    }

    /// The awaitable twin of `port_forward`, for `tunnel_async`'s reason.
    #[pyo3(signature = (guest_port, *, bind=None, max_connections=None))]
    async fn port_forward_async(
        &self,
        guest_port: u16,
        bind: Option<String>,
        max_connections: Option<u32>,
    ) -> PyCoreResult<crate::serve::PyPortForward> {
        let bind = serve::bind_address(bind.as_deref())?;
        Ok(runtime::spawn(self.port_forward_op(bind, guest_port, max_connections)).await?)
    }

    /// How many proxy tokens this session has minted, or `None` for a direct session.
    ///
    /// Exposed because it is the only observable that distinguishes a client which
    /// re-minted after a resume from one that kept a stale token (STATE-8), and a harness
    /// asserting on that behaviour needs to be able to read it. The **token itself** is
    /// not exposed and cannot be: the core's `ProxyToken` has no `Display`, no `as_str`,
    /// and no `Deref`, so "treat `authToken` as a string" is as inexpressible here as it
    /// is there (TRAP-7).
    #[getter]
    fn proxy_mint_count(&self, py: Python<'_>) -> PyCoreResult<Option<u64>> {
        Ok(self.with(py, |session| {
            Ok(session.proxy_auth().map(|auth| auth.mint_count()))
        })?)
    }

    fn __repr__(&self, py: Python<'_>) -> String {
        // A `Debug` that could fail is a `Debug` nobody can read at the moment it matters
        // most — a terminated sandbox — so the unreachable case renders as a state rather
        // than raising out of `repr()`.
        match self.with(py, |session| {
            Ok((session.endpoint().to_string(), session.port()))
        }) {
            Ok((endpoint, port)) => format!("Session(endpoint={endpoint:?}, port={port})"),
            Err(_) => "Session(<no live session: the sandbox was terminated>)".to_string(),
        }
    }
}

/// A command as either an argv or a single string.
///
/// The `FromPyObject` derive on an untagged enum is what makes `run("ls")` and
/// `run(["ls", "-la"])` both work while `run(3)` is a `TypeError` from PyO3 rather than a
/// check written here. Order matters: `Argv` first, because a Python `str` is a sequence
/// and a `Vec<String>` extraction from `"ls"` would otherwise succeed as `["l", "s"]`.
#[derive(FromPyObject)]
pub enum Command {
    Argv(Vec<String>),
    One(String),
}

/// A user or group as a caller names it: an `int` id or a `str` name (AGENTD-7, AGENTD-16).
///
/// Passed through unchanged; the daemon resolves a name in the guest, because only the guest
/// has the `/etc/passwd` that answers it. `Id` first so an `int` is never read as a name.
#[derive(FromPyObject)]
pub enum Principal {
    Id(u32),
    Name(String),
}

impl From<Principal> for protocol::exec::NameOrId {
    fn from(principal: Principal) -> Self {
        match principal {
            Principal::Id(id) => protocol::exec::NameOrId::Id(id),
            Principal::Name(name) => protocol::exec::NameOrId::Name(name),
        }
    }
}

/// `shell` as a caller gives it: a `bool`, or the name of a shell for the daemon to resolve
/// (AGENTD-14).
///
/// `Flag` first: PyO3's `bool` extraction takes only a real `bool`, so a string always
/// reaches `Named`.
#[derive(FromPyObject)]
pub enum ShellArg {
    Flag(bool),
    Named(String),
}

impl From<ShellArg> for protocol::exec::Shell {
    fn from(shell: ShellArg) -> Self {
        match shell {
            ShellArg::Flag(flag) => protocol::exec::Shell::Flag(flag),
            ShellArg::Named(name) => protocol::exec::Shell::Named(name),
        }
    }
}

impl Command {
    /// The argv the daemon receives.
    ///
    /// A bare string becomes a **one-element** argv rather than being whitespace-split,
    /// matching `session.py`: splitting on spaces turns a path with a space in it into
    /// two arguments nobody meant. `shell=True` is how a caller asks for a script.
    fn into_argv(self) -> Vec<String> {
        match self {
            Command::Argv(argv) => argv,
            Command::One(single) => vec![single],
        }
    }
}

/// The daemon's protocol constants, for a caller asserting against the wire contract.
#[pyfunction]
pub(crate) fn session_constants<'py>(py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new(py);
    dict.set_item(
        "defaultAgentPort",
        microvms_core::session::DEFAULT_AGENT_PORT,
    )?;
    dict.set_item("proxyAuthHeader", microvms_core::session::PROXY_AUTH_HEADER)?;
    dict.set_item("proxyPortHeader", microvms_core::session::PROXY_PORT_HEADER)?;
    dict.set_item(
        "maxTokenLifetimeSeconds",
        microvms_core::session::MAX_TOKEN_LIFETIME.as_secs(),
    )?;
    dict.set_item(
        "defaultRefreshAfterSeconds",
        microvms_core::session::DEFAULT_REFRESH_AFTER.as_secs(),
    )?;
    // The waits' defaults, core's (#266): the stub prints a default named for a core constant as
    // `...`, so a caller reads the figure here, and a signature that states one is tested
    // against it. `defaultReadyTimeoutSeconds` is the wait for the daemon to answer, core's
    // `DEFAULT_BOOTSTRAP_TIMEOUT`, and `defaultRunningTimeoutSeconds` the wait for RUNNING
    // before it (#254).
    dict.set_item(
        "defaultExecWaitSeconds",
        microvms_core::session::DEFAULT_EXEC_WAIT.as_secs_f64(),
    )?;
    dict.set_item(
        "defaultReadyTimeoutSeconds",
        microvms_core::session::DEFAULT_BOOTSTRAP_TIMEOUT.as_secs_f64(),
    )?;
    dict.set_item(
        "defaultRunningTimeoutSeconds",
        microvms_core::sandbox::DEFAULT_RUNNING_TIMEOUT.as_secs_f64(),
    )?;
    dict.set_item(
        "defaultLifecycleTimeoutSeconds",
        microvms_core::sandbox::DEFAULT_LIFECYCLE_TIMEOUT.as_secs_f64(),
    )?;
    dict.set_item(
        "lifecyclePollIntervalSeconds",
        microvms_core::sandbox::LIFECYCLE_POLL_INTERVAL.as_secs_f64(),
    )?;
    // The closed sets come from the protocol enums rather than being spelled here: a
    // phase added to `protocol::exec::Phase` appears in this list without an edit.
    dict.set_item(
        "phases",
        protocol::exec::Phase::ALL.map(protocol::exec::Phase::as_str),
    )?;
    dict.set_item(
        "streamKinds",
        protocol::exec::StreamKind::ALL.map(protocol::exec::StreamKind::as_str),
    )?;
    Ok(dict)
}
