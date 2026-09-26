// SPDX-License-Identifier: Apache-2.0
//! The `Sandbox` lifecycle: one VM's whole life, with the state machine the symspec
//! model describes made true of the code.
//!
//! Composes [`crate::control`] and [`crate::session`] into the surface the CLI and the
//! bindings use: build, run, suspend, resume, terminate, the launch-time suspended
//! window (STATE-12), and teardown in the order that does not leak.
//!
//! # The lifecycle is a field, and every transition is guarded
//!
//! [`Lifecycle`] is the symspec's `vm_state` verbatim, and [`Sandbox`] carries the other
//! four variables beside it — `token_installed`, `image_exists`, `was_terminated`,
//! `bootstrap_count`. The Z3 proofs over that model (bootstrap at most once, suspend from
//! non-RUNNING unreachable, TERMINATED never returns to RUNNING) are proofs about *this*
//! struct's reachable states, which is only worth something if the transitions here are
//! the only way to move it. They are: every one of those fields is private, and every
//! mutation happens in one of the five methods below.
//!
//! # Runtime-checked rather than typestate, deliberately
//!
//! The packet offered `Sandbox<Running>` returning a `Suspended` handle, which would make
//! STATE-5's wrong call a compile error rather than a local refusal — strictly stronger on
//! the ladder in [`crate`]. It is not what landed, for the reason the packet names as
//! acceptable: T-W3-8 wraps **one** object for PyO3 and napi-rs, and a type whose Rust
//! identity changes on every transition cannot be one `#[pyclass]`. A typestate sandbox
//! would be re-erased into a runtime-checked enum at the binding boundary, so the check
//! would exist twice with the binding's copy being the one most callers actually hit.
//!
//! What is kept from the typestate idea is the part that costs nothing: the check happens
//! **before** the wire call, so a suspend from SUSPENDED is refused with zero
//! control-plane calls rather than answered by AWS. The test asserts the call count, which
//! is the observable that distinguishes the two.
//!
//! # The suspended window is checked before the call (STATE-12)
//!
//! The launch-time `idlePolicy` *terminates* a suspended VM once `suspendedDurationSeconds`
//! passes, which means "resume later" silently stops working. A resume past the window is
//! refused locally, before `ResumeMicrovm`, because the alternative is calling and reading
//! the failure: the service answers about a terminated id, which is not the same statement
//! as "the window you set at launch closed", and getting there costs the full poll timeout
//! first. The window comes from this sandbox's own `RunMicrovm` request, falling back to the
//! `idlePolicy` that `GetMicrovm` reports (measured 2026-08-15, `docs/PLATFORM.md`). Only
//! the suspend's start time is the client's alone.
//!
//! # Teardown never raises, and the log group is last
//!
//! [`Sandbox::terminate`] returns a [`TeardownReport`] rather than a `Result`. It runs
//! where a caller's `finally` would, and an error raised there replaces the real failure
//! with a teardown failure — the real one being the one worth reading.
//!
//! The order is VM, then image (retrying), then the log group **last**, because the
//! service can recreate a group deleted before its image. See
//! [`TeardownReport::undeleted`] for what this crate can and cannot delete.
//!
//! # There is no `Drop` that tears down
//!
//! Rust has no context manager and `Drop` cannot await. A `Drop` that blocked on a runtime
//! would deadlock inside one; a `Drop` that spawned would race the process exit. So
//! [`Sandbox`]'s `Drop` only **warns** about a live VM, naming the id, and the rule is
//! that a caller calls [`Sandbox::terminate`] explicitly.

use std::sync::Arc;
use std::time::Duration;

use crate::control::{
    ControlPlane, CreateImageRequest, Image, Microvm, RunHookPayload, RunMicrovmRequest, WaitOpts,
};
use crate::error::{Error, ErrorKind};
use crate::region::Region;
use crate::session::{Session, TokenMinter};

/// The default launch wait: five minutes, matching the Python client's `ready_timeout_sec`.
pub const DEFAULT_READY_TIMEOUT: Duration = Duration::from_secs(300);

/// The default lifecycle wait for suspend, resume, and terminate.
pub const DEFAULT_LIFECYCLE_TIMEOUT: Duration = Duration::from_secs(300);

/// How many times a teardown retries the image delete.
///
/// Twenty, from the Python client: an image in `CREATING` refuses deletion and a VM still
/// terminating holds a reference, so this is the difference between a clean account and a
/// billed leak rather than politeness.
pub const DEFAULT_DELETE_ATTEMPTS: u32 = 20;

/// The gap between image-delete attempts.
pub const DEFAULT_DELETE_BACKOFF: Duration = Duration::from_secs(15);

/// Where the advisory deny points a well-behaved client: a loopback port nothing serves.
///
/// Loopback rather than an unroutable public address, because a refused connection is
/// immediate and a black-holed one costs the workload a connect timeout per request. Port 1
/// is privileged, so a demoted workload cannot answer it by accident.
pub const DENY_EGRESS_PROXY_URL: &str = "http://127.0.0.1:1";

/// The environment variables the advisory deny sets, in both spellings clients read.
///
/// Both cases on purpose: `curl` and most Unix clients read the lowercase names, Python's
/// `requests`/`urllib3`, npm and the AWS SDKs read the uppercase ones, and a client that
/// reads only the spelling this list omits is a hole in a mechanism that is advisory to
/// begin with. `no_proxy` is deliberately absent: the launch environment starts empty
/// (`agentd` clears it and applies only what the hook delivered), so there is no inherited
/// exemption to overwrite, and each key spends payload budget.
pub const DENY_EGRESS_ENV_KEYS: [&str; 6] = [
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
];

/// How often a lifecycle wait polls.
const LIFECYCLE_POLL_INTERVAL: Duration = Duration::from_secs(5);

/// The symspec's `vm_state`, verbatim.
///
/// Six states and no others, which is the S1 half of this module: a lifecycle held as a
/// `String` would let `"RUNNING "` and `"Running"` both exist, and every guard below would
/// have to decide which it meant.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Lifecycle {
    /// The initial state, and the state a launch is accepted into (STATE-1).
    Pending,
    /// The run hook answered with a success status and the token is installed (STATE-2).
    Running,
    /// A suspend was accepted and the platform has not yet reported it complete (STATE-4).
    Suspending,
    /// The platform reported suspension complete (STATE-6).
    Suspended,
    /// A terminate was accepted (STATE-9).
    Terminating,
    /// The platform reported termination complete (STATE-10).
    Terminated,
}

impl Lifecycle {
    /// The name the service uses for this state, which is also what an error message says.
    pub fn as_str(self) -> &'static str {
        match self {
            Lifecycle::Pending => "PENDING",
            Lifecycle::Running => "RUNNING",
            Lifecycle::Suspending => "SUSPENDING",
            Lifecycle::Suspended => "SUSPENDED",
            Lifecycle::Terminating => "TERMINATING",
            Lifecycle::Terminated => "TERMINATED",
        }
    }

    /// The lifecycle the service's `state` names, or `None` for a spelling this client does
    /// not know. `constants::MICROVM_STATES` is the closed set, and a test ties the two.
    pub fn from_service(state: &str) -> Option<Self> {
        Some(match state {
            "PENDING" => Lifecycle::Pending,
            "RUNNING" => Lifecycle::Running,
            "SUSPENDING" => Lifecycle::Suspending,
            "SUSPENDED" => Lifecycle::Suspended,
            "TERMINATING" => Lifecycle::Terminating,
            "TERMINATED" => Lifecycle::Terminated,
            _ => return None,
        })
    }

    /// Whether a VM in this state is still billing, which is what a `Drop` warning is for.
    pub fn is_live(self) -> bool {
        matches!(
            self,
            Lifecycle::Pending | Lifecycle::Running | Lifecycle::Suspending | Lifecycle::Suspended
        )
    }
}

impl std::fmt::Display for Lifecycle {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

/// Everything a launch needs, with the defaults the Python client measured.
///
/// `agent_token` is optional because the common case is a per-VM secret nobody needs to
/// see; a caller who has one already — a harness minting its own, or a retry that must
/// reuse the first attempt's — passes it.
#[derive(Clone, Debug)]
pub struct RunRequest {
    /// The image to launch, or `None` for the one [`Sandbox::build_image`] built.
    pub image_identifier: Option<String>,
    /// `imageVersion`, or `None` for the image's own latest active version.
    ///
    /// Pinning is what makes a canary a canary: without it a launch takes whatever version is
    /// latest at the moment the call lands, which is not necessarily the one the caller just
    /// built. Paired with [`crate::control::ControlPlane::set_image_version_status`] it is also
    /// the rollback — set the bad version INACTIVE, re-pin here to the good one — and a version
    /// set INACTIVE refuses to launch when named here.
    ///
    /// See [`crate::control::RunMicrovmRequest::image_version`], which this forwards to.
    pub image_version: Option<String>,
    /// The execution role. Optional in the model; every real launch needs one.
    pub execution_role_arn: Option<String>,
    /// The bearer token the daemon will accept, or `None` to mint one.
    pub agent_token: Option<String>,
    /// Persist this unique launch key before sending a request; reuse only for that same
    /// launch and identical parameters. None mints a fresh key. A stable key requires
    /// an explicit agent_token and identity=false so retries carry identical payloads.
    pub client_token: Option<String>,
    /// Base environment for every exec in the launched VM, delivered in the same
    /// `runHookPayload` as the token.
    ///
    /// The daemon applies this *under* each request's own `env`, so a per-exec value
    /// wins on a key both set. Empty by default, and an empty map produces byte-for-byte
    /// the payload this client always sent — a caller who never touches this field
    /// cannot be affected by the field existing.
    ///
    /// It shares the token's 4096-byte payload budget, and the check is local:
    /// [`crate::control::RunHookPayload::for_launch`] refuses an over-ceiling payload
    /// before any call, naming the byte count and how much of it the env is. That
    /// matters here more than for the token, because one bearer token has always fit
    /// with room to spare and a map of credentials does not.
    pub launch_env: std::collections::HashMap<String, String>,
    /// Whether to generate and deliver a tunnel identity (#70 layer 3).
    ///
    /// On, the launch generates two fresh x25519 seeds, delivers the VM's seed and the
    /// host's public key in the same payload as the token, and [`RunOutcome`]-equivalent
    /// state on the session keeps the [`crate::identity::TunnelIdentity`] — the host secret
    /// and the VM's public pin — for `tunnel --verify-identity` to prove the far end.
    /// Off (the default) sends byte-for-byte the payload this client always sent.
    ///
    /// A flag rather than always-on, deliberately: the material spends 137 bytes of the
    /// payload's measured 4096-byte budget, and a caller near the ceiling with a launch env
    /// deserves to choose which feature gets the room.
    pub identity: bool,
    /// Whether to request the egress connector. Off omits it from the request; measured
    /// 2026-09-12 the platform gave a connector-less VM outbound network anyway
    /// (`docs/PLATFORM.md`).
    pub egress: bool,
    /// Customer-managed Lambda VPC egress connector ARNs. See
    /// [`RunMicrovmRequest::egress_network_connectors`].
    pub egress_network_connectors: Vec<String>,
    /// Sets advisory proxy-deny environment variables for clients that honor them.
    /// Workloads can bypass these variables. Internet isolation requires a VPC egress
    /// connector using a VPC without an internet gateway or NAT gateway.
    ///
    /// Refused with [`RunRequest::egress`]. Existing caller-supplied proxy variables win;
    /// the added variables share the 4096-byte run-hook payload budget.
    pub deny_egress: bool,
    /// Whether to launch shell-capable: the ingress set becomes the measured pair
    /// `[HTTP_INGRESS, SHELL_INGRESS]` instead of `ALL_INGRESS`, which is what
    /// `CreateMicrovmShellAuthToken` requires and what `microvm shell` attaches to.
    /// Off (the default) launches byte-for-byte what this client always sent.
    pub shell: bool,
    /// `idlePolicy.maxIdleDurationSeconds`.
    pub max_idle_sec: u32,
    /// `idlePolicy.suspendedDurationSeconds` — the window STATE-12 refuses past.
    pub suspended_sec: u32,
    /// `idlePolicy.autoResumeEnabled`.
    pub auto_resume: bool,
    /// `maximumDurationInSeconds`, checked against 1..=28800 before the call.
    pub max_duration_sec: u32,
    /// How long to wait for RUNNING.
    pub ready_timeout: Duration,
    /// A label for the run token (TRAP-1). Never the token.
    pub token_scope: Option<String>,
    /// Per-VM `logging` (#201). See [`RunMicrovmRequest::logging`].
    pub logging: Option<crate::control::ops::Logging>,
    /// Whether [`Sandbox::run`] waits for RUNNING before returning (the default).
    ///
    /// Off returns as soon as `RunMicrovm` is accepted, with the lifecycle still PENDING:
    /// the session is addressable, and [`Sandbox::wait_until_running`] finishes the wait
    /// later — from this process, or after a durable workflow's next step.
    pub wait: bool,
}

impl Default for RunRequest {
    fn default() -> Self {
        Self {
            image_identifier: None,
            image_version: None,
            execution_role_arn: None,
            agent_token: None,
            client_token: None,
            launch_env: std::collections::HashMap::new(),
            identity: false,
            egress: false,
            egress_network_connectors: Vec::new(),
            deny_egress: false,
            shell: false,
            max_idle_sec: 600,
            suspended_sec: 600,
            auto_resume: false,
            max_duration_sec: 3_600,
            ready_timeout: DEFAULT_READY_TIMEOUT,
            token_scope: None,
            logging: None,
            wait: true,
        }
    }
}

impl RunRequest {
    /// A launch with the measured defaults: ingress only, ten-minute idle and suspended
    /// windows, a one-hour maximum duration, no auto-resume.
    pub fn new() -> Self {
        Self::default()
    }

    /// Launches `identifier` rather than the built image.
    #[must_use]
    pub fn with_image(mut self, identifier: impl Into<String>) -> Self {
        self.image_identifier = Some(identifier.into());
        self
    }

    /// Requests the managed internet egress connector. Omission does not block internet access.
    #[must_use]
    pub fn with_egress(mut self) -> Self {
        self.egress = true;
        self
    }

    /// Routes egress through a customer-managed Lambda network connector.
    #[must_use]
    pub fn with_egress_network_connector(mut self, arn: impl Into<String>) -> Self {
        self.egress_network_connectors.push(arn.into());
        self
    }

    /// Replaces managed internet egress with customer-managed VPC connectors, when any are
    /// given; an empty list leaves the request unchanged.
    ///
    /// The two cannot be combined, so a launch surface that requests managed egress by
    /// default (an agent VM) uses this to switch rather than failing the launch.
    #[must_use]
    pub fn with_vpc_egress(mut self, arns: Vec<String>) -> Self {
        if !arns.is_empty() {
            self.egress = false;
            self.egress_network_connectors = arns;
        }
        self
    }

    /// Applies the advisory in-guest deny. See [`RunRequest::deny_egress`] for what it is
    /// and, more importantly, for what it is not.
    #[must_use]
    pub fn with_deny_egress(mut self) -> Self {
        self.deny_egress = true;
        self
    }

    /// What this launch's outbound network **is**, as opposed to what it asked for.
    ///
    /// The one derivation, so the CLI's envelope, the CLI's human line and any embedding
    /// consumer cannot disagree about the same launch.
    pub fn egress_posture(&self) -> crate::control::EgressPosture {
        crate::control::EgressPosture::for_launch(self.egress, self.deny_egress)
    }

    /// The launch environment as the guest will see it: the caller's, plus the advisory
    /// proxy deny when [`RunRequest::deny_egress`] is on.
    ///
    /// The caller's own value wins on a shared key. A launch that already routes through a
    /// proxy of its own has said where its traffic goes, and overwriting that with a black
    /// hole would break a working configuration in the name of a posture this client
    /// already declines to call a seal.
    pub fn effective_launch_env(&self) -> std::collections::HashMap<String, String> {
        let mut env = self.launch_env.clone();
        if self.deny_egress {
            for key in DENY_EGRESS_ENV_KEYS {
                env.entry(key.to_string())
                    .or_insert_with(|| DENY_EGRESS_PROXY_URL.to_string());
            }
        }
        env
    }

    /// Requests a shell-capable launch. See [`RunRequest::shell`].
    #[must_use]
    pub fn with_shell(mut self) -> Self {
        self.shell = true;
        self
    }

    /// Pins the launch to one `imageVersion`. See the field for why.
    #[must_use]
    pub fn with_image_version(mut self, version: impl Into<String>) -> Self {
        self.image_version = Some(version.into());
        self
    }

    /// Sets the suspended window this sandbox will refuse a resume past (STATE-12).
    #[must_use]
    pub fn with_suspended_sec(mut self, seconds: u32) -> Self {
        self.suspended_sec = seconds;
        self
    }

    /// Reuses a caller-supplied agent token rather than minting one.
    #[must_use]
    pub fn with_agent_token(mut self, token: impl Into<String>) -> Self {
        self.agent_token = Some(token.into());
        self
    }

    /// Generates and delivers a tunnel identity with the launch (#70 layer 3).
    ///
    /// After a `run` with this set, [`Sandbox::tunnel_identity`] holds what
    /// `tunnel --verify-identity` needs: the host's secret and the VM's public pin.
    #[must_use]
    pub fn with_identity(mut self) -> Self {
        self.identity = true;
        self
    }

    /// Adds one launch-environment variable, which every exec in the VM starts with.
    ///
    /// One pair per call rather than a whole map, because that is how a caller builds
    /// one — from flags, from a config file, one credential at a time — and a
    /// map-taking setter makes the second call silently discard the first. The field is
    /// public for a caller who really does hold a map.
    #[must_use]
    pub fn with_launch_env(mut self, key: impl Into<String>, value: impl Into<String>) -> Self {
        self.launch_env.insert(key.into(), value.into());
        self
    }
}

/// What a teardown should delete beyond the VM itself.
///
/// Both deletions are opt-in, because both destroy something a caller may still want: the
/// image is reusable across runs, and the log group is where a failed build's only evidence
/// lives.
#[derive(Clone, Copy, Debug)]
pub struct TeardownOpts {
    /// Whether to delete the image the sandbox built.
    pub delete_image: bool,
    /// Whether the build log group should be deleted.
    ///
    /// This crate **cannot** delete it — CloudWatch is not in the dependency set — so
    /// asking names the group in [`TeardownReport::undeleted`] rather than removing it.
    /// See that field for why naming is the honest answer rather than a silent success.
    pub delete_log_group: bool,
    /// How many times the image delete is retried.
    pub delete_attempts: u32,
    /// The gap between image-delete attempts.
    pub delete_backoff: Duration,
    /// How long to wait for TERMINATED, or `None` to return as soon as the terminate call
    /// is accepted.
    ///
    /// `None` by default, matching the Python client: the caller is on the way out, and a
    /// teardown that blocked five minutes on a state nobody reads is five minutes of a CI
    /// job. A caller that needs STATE-10 *observed* passes
    /// [`TeardownOpts::waiting_for_terminated`].
    pub wait_for_terminated: Option<Duration>,
}

impl Default for TeardownOpts {
    fn default() -> Self {
        Self {
            delete_image: false,
            delete_log_group: false,
            delete_attempts: DEFAULT_DELETE_ATTEMPTS,
            delete_backoff: DEFAULT_DELETE_BACKOFF,
            wait_for_terminated: None,
        }
    }
}

impl TeardownOpts {
    /// Deletes the image as well as the VM.
    #[must_use]
    pub fn deleting_image(mut self) -> Self {
        self.delete_image = true;
        self
    }

    /// Asks for the log group too, which names it rather than deleting it.
    #[must_use]
    pub fn deleting_log_group(mut self) -> Self {
        self.delete_log_group = true;
        self
    }

    /// Waits for TERMINATED before returning (STATE-10).
    #[must_use]
    pub fn waiting_for_terminated(mut self) -> Self {
        self.wait_for_terminated = Some(DEFAULT_LIFECYCLE_TIMEOUT);
        self
    }
}

/// What a teardown did, and what it left behind.
///
/// Returned rather than raised. See the module docs: this runs where a `finally` would.
#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct TeardownReport {
    /// Identifiers of everything a caller asked to have deleted that still exists.
    ///
    /// The CLI emits these (CLI-6), which is the whole reason they are identifiers rather
    /// than a boolean: a leak nobody can name is a leak nobody can clean up.
    ///
    /// Two things land here. A delete that was attempted and failed — an image whose twenty
    /// attempts all hit a conflict. And the build **log group**, which this crate cannot
    /// delete at all: no CloudWatch client exists anywhere in this workspace, and a logs
    /// API for one delete call is a bigger surface than the leak it would hide (an
    /// `aws-sdk-cloudwatchlogs` edge is a reasonable future add if teardown grows more
    /// log-group work). Naming it is strictly better than the
    /// alternatives: deleting it is unavailable, and silently succeeding would report a
    /// clean teardown over six accumulated log groups — which is how the leak was found in
    /// the first place.
    pub undeleted: Vec<String>,
    /// Whether the terminate call was accepted.
    pub terminate_accepted: bool,
    /// Whether the image was deleted, or `None` when deletion was not asked for.
    pub image_deleted: Option<bool>,
    /// The lifecycle state the sandbox ended in.
    pub lifecycle: Option<Lifecycle>,
    /// Every failure the teardown swallowed, in the order it hit them.
    ///
    /// Kept because a teardown that never raises is a teardown whose failures are invisible
    /// otherwise, and the first one is usually the cause of the rest.
    pub failures: Vec<String>,
}

impl TeardownReport {
    /// Whether anything a caller asked for was left behind.
    pub fn leaked(&self) -> bool {
        !self.undeleted.is_empty()
    }
}

/// Mints proxy tokens for one MicroVM through the control plane.
///
/// The bridge between the two lanes: `ControlPlane::mint_auth_token` answers a
/// `control::ProxyToken`, [`TokenMinter`] wants a `session::ProxyToken`, and the `From`
/// impl at `session/proxy.rs` is the conversion — one `.into()` at the boundary, which is
/// why that impl is `From` rather than a named function.
///
/// The plane sits behind an `Arc` rather than a reference because the minter must outlive
/// the call that built the session: minting happens inside the request path on every later
/// request, which is what makes it happen at all (TRAP-9).
///
/// Public so a driving adapter that builds its own session mints through this rather than a
/// copy of it.
pub struct ControlPlaneMinter {
    control: Arc<ControlPlane>,
    microvm_id: String,
}

impl ControlPlaneMinter {
    /// A minter for `microvm_id`'s tokens, through `control`.
    pub fn new(control: Arc<ControlPlane>, microvm_id: impl Into<String>) -> Self {
        Self {
            control,
            microvm_id: microvm_id.into(),
        }
    }
}

impl std::fmt::Debug for ControlPlaneMinter {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ControlPlaneMinter")
            .field("microvm_id", &self.microvm_id)
            .finish_non_exhaustive()
    }
}

impl TokenMinter for ControlPlaneMinter {
    fn mint(
        &self,
    ) -> futures_util::future::BoxFuture<'_, Result<crate::session::ProxyToken, Error>> {
        Box::pin(async move {
            let minted = self.control.mint_auth_token(&self.microvm_id).await?;
            Ok(minted.into())
        })
    }

    /// Overrides the default, which ignores the ports and delegates.
    ///
    /// This is the minter that has a control plane behind it, so it is the one that can
    /// actually widen a token's scope — and without this override
    /// `Session::connect_headers(8080)` would keep answering a header pair behind a token
    /// scoped to 9000 only, which the proxy refuses with 403 `Access to port denied`. See
    /// [`TokenMinter::mint_for_ports`] for the measurement.
    fn mint_for_ports(
        &self,
        ports: &[u16],
    ) -> futures_util::future::BoxFuture<'_, Result<crate::session::ProxyToken, Error>> {
        let specs: Vec<crate::control::ops::PortSpecification> = ports
            .iter()
            .map(|port| crate::control::ops::PortSpecification::port(*port))
            .collect();
        Box::pin(async move {
            let minted = self
                .control
                .mint_auth_token_for(&self.microvm_id, &specs)
                .await?;
            Ok(minted.into())
        })
    }
}

/// One MicroVM's whole life.
///
/// See the module docs for the state machine, the window, and why teardown is explicit.
pub struct Sandbox {
    control: Arc<ControlPlane>,
    image: Option<Image>,
    microvm: Option<Microvm>,
    session: Option<Session>,
    /// A backend for the session [`Sandbox::run`] builds, or `None` for the real HTTP one.
    ///
    /// The daemon-side sibling of `with_control_plane`'s transport seam: a test that
    /// scripts the control plane can launch, but the session `run` then builds would dial
    /// a real endpoint. This lets the same test script the daemon too. Never set in
    /// production — `Session::builder` picks reqwest when no backend is given.
    session_backend: Option<crate::session::SharedBackend>,

    // ── the symspec's five variables ─────────────────────────────────────────
    lifecycle: Lifecycle,
    /// Every transition of `lifecycle`, for a reader that must not take the caller's lock
    /// (a keepalive task running while the same sandbox is busy inside a long exec).
    lifecycle_watch: tokio::sync::watch::Sender<Lifecycle>,
    token_installed: bool,
    image_exists: bool,
    was_terminated: bool,
    bootstrap_count: u32,

    /// The window from *our own* `RunMicrovm` request. `GetMicrovm` also reports it (in
    /// `idlePolicy`), which is the fallback when this sandbox has no request of its own.
    suspended_window: Option<Duration>,
    /// Whether the accepted launch carried a caller client token, and so may have adopted
    /// a VM an earlier attempt launched (#195).
    launch_adoptable: bool,
    /// The egress posture of this sandbox's own accepted launch, or `None` for a sandbox
    /// that launched nothing (an adopted one never saw the launch options).
    launch_posture: Option<crate::control::EgressPosture>,
    /// The launch's agent token, held until the session that carries it is built. Kept out
    /// of `Debug` like every other credential here.
    pending_agent_token: Option<String>,
    /// `maxIdleDurationSeconds` from our own `RunMicrovm` request: a keepalive's cadence
    /// is checked against it.
    idle_window: Option<Duration>,
    /// The clock reading when the suspend call was accepted, or `None` when not suspended.
    suspended_at: Option<Duration>,
    /// Set by [`Sandbox::terminate`], so `Drop` can tell an abandoned VM from a torn-down
    /// one.
    torn_down: bool,
    /// Built by [`Sandbox::adopt`] around a VM another process launched. That process owns
    /// the VM's teardown, so dropping this handle is not a leak and `Drop` stays quiet.
    adopted: bool,
    /// Set by [`Sandbox::detach`]: the VM was handed to another process, which now owns its
    /// lifecycle and teardown. Every transition is refused and `Drop` stays quiet.
    detached: bool,
    /// The launch's tunnel identity, when [`RunRequest::identity`] asked for one.
    ///
    /// Holds the host secret and the VM's *public* pin — the VM's own secret was dropped
    /// the moment the payload was built ([`crate::identity::LaunchIdentity::keep`]), so
    /// nothing on the host side can impersonate the VM it launched.
    tunnel_identity: Option<crate::identity::TunnelIdentity>,
    /// The STS and S3 calls [`Sandbox::ensure_image`] makes, or `None` until the first
    /// call builds the real ones. The seam a test replaces them at.
    build_services: Option<Arc<dyn crate::control::BuildServices>>,
    /// The caller's account, resolved once per sandbox for image ARNs (IMAGE-8).
    account: Option<String>,
}

impl std::fmt::Debug for Sandbox {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        // No agent token: it is a credential, and a `Debug` that printed it would put it in
        // every log line that formats a sandbox.
        f.debug_struct("Sandbox")
            .field("lifecycle", &self.lifecycle)
            .field("microvm", &self.microvm.as_ref().map(|vm| &vm.id))
            .field("image", &self.image.as_ref().map(|image| &image.identifier))
            .field("token_installed", &self.token_installed)
            .field("bootstrap_count", &self.bootstrap_count)
            .field("was_terminated", &self.was_terminated)
            .field("suspended_window", &self.suspended_window)
            .field("adopted", &self.adopted)
            .field("detached", &self.detached)
            .finish_non_exhaustive()
    }
}

/// What another process needs to adopt a VM this sandbox handed off with
/// [`Sandbox::detach`]: pass the fields to [`Sandbox::adopt`] (or the prelude's `Sandbox::adopt_in`).
///
/// The agent token is a credential: readable through [`Detached::agent_token`] so it can be
/// persisted, and absent from `Debug`, so a log line that formats the record cannot leak it.
/// Store it the way [`crate::names::NameRecord`] asks: privately, encrypted.
#[derive(Clone)]
pub struct Detached {
    /// The VM's identifier.
    pub microvm_id: String,
    /// The HTTPS endpoint its daemon answers on.
    pub endpoint: String,
    /// The region the VM runs in.
    pub region: Region,
    /// The daemon port the endpoint's proxy tokens are minted for.
    pub port: u16,
    agent_token: String,
}

impl Detached {
    /// The bearer the VM's daemon accepts; required by [`Sandbox::adopt`].
    pub fn agent_token(&self) -> &str {
        &self.agent_token
    }
}

impl std::fmt::Debug for Detached {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Detached")
            .field("microvm_id", &self.microvm_id)
            .field("endpoint", &self.endpoint)
            .field("region", &self.region)
            .field("port", &self.port)
            .field("agent_token", &"<redacted>")
            .finish()
    }
}

impl Sandbox {
    /// A sandbox over an already-built [`ControlPlane`].
    ///
    /// The seam every test uses, and public for the same reason `ControlPlane::from_ports`
    /// is: a caller who wants a non-default port, or a fake transport, has already built the
    /// plane, and a sandbox constructible only from a [`Region`] would be a sandbox no test
    /// can drive without AWS. `Sandbox::new(region)`, `Sandbox::adopt_in` and
    /// `Sandbox::from_name`, which resolve credentials for a region, are in
    /// `microvms_core::prelude`.
    ///
    /// The plane's own clock is what times the suspended window, rather than a second one
    /// this type would own. Two clocks in one lifecycle is the trap the daemon's turmoil
    /// tests record: a window measured on one clock while the poll loop sleeps on another
    /// calls a closed window open.
    pub fn with_control_plane(control: ControlPlane) -> Self {
        Self {
            control: Arc::new(control),
            image: None,
            microvm: None,
            session: None,
            session_backend: None,
            lifecycle: Lifecycle::Pending,
            lifecycle_watch: tokio::sync::watch::Sender::new(Lifecycle::Pending),
            token_installed: false,
            image_exists: false,
            was_terminated: false,
            bootstrap_count: 0,
            suspended_window: None,
            launch_adoptable: false,
            launch_posture: None,
            pending_agent_token: None,
            idle_window: None,
            suspended_at: None,
            torn_down: false,
            adopted: false,
            detached: false,
            tunnel_identity: None,
            build_services: None,
            account: None,
        }
    }

    /// Routes [`Sandbox::ensure_image`]'s STS and S3 calls through `services`.
    ///
    /// The seam beside `with_control_plane`'s transport: a test that scripts the control
    /// plane replaces the two calls outside it here, and production builds
    /// [`crate::control::SignedBuildServices`] on the first `ensure_image`.
    pub fn with_build_services(mut self, services: Arc<dyn crate::control::BuildServices>) -> Self {
        self.build_services = Some(services);
        self
    }

    /// Routes the session [`Sandbox::run`] builds through `backend` instead of real HTTP.
    ///
    /// The daemon-side half of the test seam `with_control_plane` opens: scripting the
    /// control plane gets a test through the launch, and this gets it through everything
    /// the launched session then does — uploads, execs, downloads — without a daemon.
    pub fn with_session_backend(mut self, backend: crate::session::SharedBackend) -> Self {
        self.session_backend = Some(backend);
        self
    }

    // ── the symspec's five variables, readable ───────────────────────────────

    /// The lifecycle state, which is the symspec's `vm_state`.
    pub fn lifecycle(&self) -> Lifecycle {
        self.lifecycle
    }

    /// Whether the agent token has been installed (STATE-2).
    pub fn token_installed(&self) -> bool {
        self.token_installed
    }

    /// Whether an image is recorded as existing (STATE-1).
    pub fn image_exists(&self) -> bool {
        self.image_exists
    }

    /// Whether this VM was ever terminated (STATE-11).
    pub fn was_terminated(&self) -> bool {
        self.was_terminated
    }

    /// How many times the token has been installed. Never above one (STATE-3).
    pub fn bootstrap_count(&self) -> u32 {
        self.bootstrap_count
    }

    /// The image, once built.
    pub fn image(&self) -> Option<&Image> {
        self.image.as_ref()
    }

    /// The VM as the service last described it.
    pub fn microvm(&self) -> Option<&Microvm> {
        self.microvm.as_ref()
    }

    /// The session, once launched.
    pub fn session(&self) -> Option<&Session> {
        self.session.as_ref()
    }

    /// The suspended window this sandbox asked for at launch, once it has launched.
    pub fn suspended_window(&self) -> Option<Duration> {
        self.suspended_window
    }

    /// A receiver that sees every lifecycle transition without taking this sandbox's lock.
    ///
    /// A binding's keepalive reads it between polls: a sandbox busy inside a long exec
    /// holds the lock the whole time, which is exactly when the keepalive must keep polling,
    /// and a suspend or terminate must end the keepalive before its next poll auto-resumes
    /// the VM.
    pub fn watch_lifecycle(&self) -> tokio::sync::watch::Receiver<Lifecycle> {
        self.lifecycle_watch.subscribe()
    }

    fn set_lifecycle(&mut self, lifecycle: Lifecycle) {
        self.lifecycle = lifecycle;
        self.lifecycle_watch.send_replace(lifecycle);
    }

    /// The idle window this sandbox asked for at launch, once it has launched.
    pub fn idle_window(&self) -> Option<Duration> {
        self.idle_window
    }

    /// The tunnel identity, when the launch asked for one ([`RunRequest::identity`]).
    ///
    /// What `tunnel --verify-identity` verifies with: the host's secret and the VM's public
    /// pin. `None` on a launch that did not ask, and the tunnel route then refuses an
    /// identity request with close code 4401 rather than downgrading.
    pub fn tunnel_identity(&self) -> Option<&crate::identity::TunnelIdentity> {
        self.tunnel_identity.as_ref()
    }

    /// The egress posture of this sandbox's launch (BIND-12), which its session carries.
    ///
    /// The launch's own [`RunRequest::egress_posture`] once [`Sandbox::run`] was accepted.
    /// Otherwise [`EgressPosture::default`], `unsealed`: an adopted sandbox never saw the
    /// launch options, and the weakest true claim is the only one it can make.
    ///
    /// [`EgressPosture::default`]: crate::control::EgressPosture::default
    pub fn egress_posture(&self) -> crate::control::EgressPosture {
        self.launch_posture.unwrap_or_default()
    }

    /// The agent port the control plane was built with: the hooks port on every image
    /// this sandbox builds, and the port the daemon listens on in every VM it launches.
    /// Read-only; `agents::AgentVm` needs it to derive a Dockerfile whose `AGENTD_PORT`
    /// agrees with the launch.
    pub fn port(&self) -> u16 {
        self.control.port()
    }

    // ── build ────────────────────────────────────────────────────────────────

    /// Builds an image and waits for it to become usable.
    ///
    /// Every local guard runs inside [`ControlPlane::create_image`], before the call —
    /// which matters because the create happens *after* the caller's artifact upload, so a
    /// rejection AWS raises costs the upload first.
    pub async fn build_image(&mut self, request: CreateImageRequest) -> Result<&Image, Error> {
        let size = request.size;
        let created = self.control.create_image(request).await?;
        let mut built = self
            .control
            .wait_for_image(&created.identifier, size, WaitOpts::default())
            .await?;
        // The logging config survives the wait from the create's own record, because the
        // readback cannot carry it: `GetMicrovmImage` reports no logging member, and the
        // resolved log stream's discriminator was minted inside `create_image` — this is
        // the only copy, and dropping it here would leave the caller unable to name the
        // stream their build wrote to.
        built.log_group = created.log_group;
        built.log_stream = created.log_stream;
        // Recorded before any launch, because a built image exists whether or not anything
        // is ever run from it — and the teardown has to be able to name it either way.
        self.image_exists = true;
        self.image = Some(built);
        Ok(self.image.as_ref().expect("just assigned"))
    }

    /// Builds or reuses the content-addressed image for `request` (#221). Not yet
    /// implemented.
    pub async fn ensure_image(
        &mut self,
        request: crate::control::EnsureImageRequest,
    ) -> Result<crate::control::EnsuredImage, Error> {
        // Everything local first: a request this client refuses costs no call at all, the
        // caller-identity lookup included.
        let prepared = crate::control::ensure::prepare(&self.control, request)?;
        let services = match &self.build_services {
            Some(services) => Arc::clone(services),
            None => {
                let built = self
                    .control
                    .adapters()
                    .build_services(self.control.region().clone())
                    .await?;
                self.build_services = Some(Arc::clone(&built));
                built
            }
        };
        let account = match &self.account {
            Some(account) => account.clone(),
            None => {
                let account = services.caller_account().await?;
                self.account = Some(account.clone());
                account
            }
        };
        let ensured =
            crate::control::ensure::ensure(&self.control, services.as_ref(), &account, prepared)
                .await?;
        self.image_exists = true;
        self.image = Some(ensured.image.clone());
        Ok(ensured)
    }

    /// The artifact bytes to upload to the request's `code_artifact_uri`.
    ///
    /// The upload is the caller's on this path; [`Sandbox::ensure_image`] uploads for
    /// itself.
    pub fn build_artifact_for(&self, request: &CreateImageRequest) -> Result<Vec<u8>, Error> {
        self.control.build_artifact_for(request)
    }

    /// Every local guard [`Self::build_image`] runs, callable **before** the artifact upload.
    ///
    /// A delegation to [`ControlPlane::preflight`], on this type so the CLI's build path
    /// reaches it through the one sandbox it already holds. Local; zero calls. The point is
    /// ordering: `build_image`'s guards run after the caller's upload, so a request this
    /// client itself refuses would still cost the S3 PUT unless the caller checks first.
    pub fn preflight(&self, request: &CreateImageRequest) -> Result<(), Error> {
        self.control.preflight(request)
    }

    /// The ARN for `identifier`: an ARN passes through with zero calls, a bare name is
    /// resolved through the image listing by exact match.
    ///
    /// A read-only delegation to [`ControlPlane::resolve_image_arn`], on this type so the
    /// CLI's launch path reaches it through the one sandbox it already holds. It touches
    /// none of the five state-machine variables — resolution is a question about the
    /// account, not about this VM's lifecycle.
    pub async fn resolve_image_arn(&self, identifier: &str) -> Result<String, Error> {
        self.control.resolve_image_arn(identifier).await
    }

    /// The image with exactly `name`, or `None`. Read-only; see
    /// [`ControlPlane::find_image_by_name`] for the pagination and exact-match rules.
    pub async fn find_image_by_name(
        &self,
        name: &str,
    ) -> Result<Option<crate::control::ops::MicrovmImageSummaryWire>, Error> {
        self.control.find_image_by_name(name).await
    }

    /// The content hash `build --reuse` keys an image name to. Local; zero calls.
    pub fn artifact_content_hash_for(&self, request: &CreateImageRequest) -> String {
        self.control.artifact_content_hash_for(request)
    }

    /// The versions of a managed base image, for pinning
    /// [`CreateImageRequest::base_image_version`].
    ///
    /// A read-only delegation to [`crate::control::ControlPlane::managed_base_versions`], on
    /// this type for the reason [`Self::resolve_image_arn`] gives: it touches none of the five
    /// state-machine variables, because a question about AWS's published bases is not a
    /// question about this VM's lifecycle.
    pub async fn managed_base_versions(
        &self,
        base_image_arn: &str,
    ) -> Result<Vec<crate::control::ops::ManagedMicrovmImageVersionWire>, Error> {
        self.control.managed_base_versions(base_image_arn).await
    }

    /// One version's configuration, state, and availability status. Read-only.
    pub async fn image_version(
        &self,
        identifier: &str,
        version: &str,
    ) -> Result<crate::control::ops::MicrovmImageVersionSummaryWire, Error> {
        self.control.get_image_version(identifier, version).await
    }

    // ── run (STATE-1, STATE-2, STATE-3) ──────────────────────────────────────

    /// Launches a MicroVM, waits for RUNNING, and returns its session.
    ///
    /// # The three state requirements this is
    ///
    /// STATE-1: the accepted launch moves the lifecycle to PENDING and records the image as
    /// existing. STATE-2: the platform reporting RUNNING is what marks the token installed
    /// — not the launch call, because the run hook is what delivers it and a launch that
    /// dies during startup delivered nothing. STATE-3: `bootstrap_count` is incremented
    /// exactly here, and a second `run` on the same sandbox is refused, which is what makes
    /// "at most once per VM lifetime" a property of the type rather than of a caller's
    /// discipline.
    ///
    /// The agent token rides in `runHookPayload`, which is what keeps it out of the shared
    /// image snapshot. That is safe because the platform forwards no external traffic until
    /// the run hook returns 200, so a per-VM secret delivered at launch wins the
    /// first-writer race through the endpoint.
    pub async fn run(&mut self, request: RunRequest) -> Result<&mut Session, Error> {
        self.refuse_detached("run")?;
        // STATE-3's local half. A sandbox that has already bootstrapped cannot bootstrap
        // again, and the refusal is here rather than in a comment because `run` twice is
        // the plausible mistake — a retry loop around a launch that timed out.
        if self.bootstrap_count > 0 || self.microvm.is_some() {
            return Err(Error::invalid_arg(format!(
                "this sandbox has already launched a VM ({} bootstrap(s), lifecycle {}), and the \
                 agent token is installed at most once per VM lifetime (STATE-3). A second VM \
                 needs a second Sandbox — reusing this one would either re-deliver a run-hook \
                 payload to a daemon whose one-shot bootstrap refuses it, or silently address \
                 two guests through one handle.",
                self.bootstrap_count, self.lifecycle,
            )));
        }

        // The network options, refused and classified by the one function a harness asks
        // before a launch (BIND-13), so its answer and this launch cannot disagree. Opposite
        // intents are refused rather than resolved by a precedence rule nobody would find:
        // `--egress` asks the platform for outbound network and `--deny-egress` asks the
        // guest's own clients to refuse it, so a launch carrying both would report `open`
        // while its workload's tools failed closed.
        let posture = crate::control::egress_posture_for(
            request.egress,
            &request.egress_network_connectors,
            request.deny_egress,
            Some(self.control.region()),
        )?;

        let Some(identifier) = request
            .image_identifier
            .clone()
            .or_else(|| self.image.as_ref().map(|image| image.identifier.clone()))
        else {
            return Err(Error::new(
                ErrorKind::Precondition,
                "no image to launch: pass RunRequest::with_image or call build_image first."
                    .to_string(),
            ));
        };

        if request.client_token.is_some() && (request.agent_token.is_none() || request.identity) {
            return Err(Error::invalid_arg(
                "a stable client_token requires an explicit agent_token and identity=false; persist identical launch parameters before retrying",
            ));
        }
        let agent_token = match request.agent_token.clone() {
            Some(token) => token,
            None => mint_agent_token(self.control.entropy())?,
        };
        // Generated before the payload because the payload carries its two public-facing
        // fields. The VM's secret half lives exactly as long as this binding: `keep()` below
        // drops it before the launch call returns.
        let launch_identity = if request.identity {
            Some(crate::entropy::launch_identity(self.control.entropy())?)
        } else {
            None
        };
        // Checked even though this builds the JSON itself, because neither half is ours:
        // the token may be caller-supplied, so someone passing a signed blob rather than a
        // bearer token is exactly who this catches (TRAP-5), and the launch env is entirely
        // the caller's. This is the pre-flight refusal — over-ceiling fails here with the
        // byte count, before the launch, rather than as a `ValidationException` on a member
        // the caller did not know they were filling.
        let payload = RunHookPayload::for_launch_with_identity(
            &agent_token,
            &request.effective_launch_env(),
            launch_identity.as_ref(),
        )?;

        let mut wire = RunMicrovmRequest::new(&identifier, payload);
        wire.image_version = request.image_version.clone();
        wire.execution_role_arn = request.execution_role_arn.clone();
        wire.egress_network_connectors = request.egress_network_connectors.clone();
        wire.max_idle_sec = request.max_idle_sec;
        wire.suspended_sec = request.suspended_sec;
        wire.auto_resume = request.auto_resume;
        wire.max_duration_sec = request.max_duration_sec;
        wire.token_scope = request.token_scope.clone();
        wire.client_token = request.client_token.clone();
        wire.logging = request.logging.clone();
        if request.egress {
            wire = wire.with_egress();
        }
        if request.shell {
            wire = wire.with_shell();
        }

        let launched = self.control.run_microvm(wire).await?;

        // The durable half of the identity, kept the moment the launch is accepted: the host
        // secret and the VM's public pin. The VM's own secret ends here — `keep` drops it —
        // so from this line on the host can verify its VM and impersonate it never.
        self.tunnel_identity = launch_identity.map(crate::identity::LaunchIdentity::keep);

        // STATE-1: the launch was accepted.
        self.set_lifecycle(Lifecycle::Pending);
        self.image_exists = true;
        // Recorded here rather than after the wait, because the window the idlePolicy
        // enforces was set by *this* request and a launch that then fails still leaves a VM
        // the caller may have to reason about.
        self.suspended_window = Some(Duration::from_secs(u64::from(request.suspended_sec)));
        self.idle_window = Some(Duration::from_secs(u64::from(request.max_idle_sec)));
        self.microvm = Some(launched);
        // A caller-supplied client token can adopt the VM an earlier attempt launched, which
        // may have idle-suspended since (#195); a minted token always launches afresh.
        self.launch_adoptable = request.client_token.is_some();
        self.launch_posture = Some(posture);
        self.pending_agent_token = Some(agent_token);

        if request.wait {
            return self.wait_until_running(request.ready_timeout).await;
        }
        // Not waiting: the endpoint is in the launch reply, so the session is addressable
        // now, and a daemon request before RUNNING fails like any other unready request.
        self.build_session()?;
        Ok(self.session.as_mut().expect("just assigned"))
    }

    /// The session for the launched VM, from its endpoint and the launch's agent token.
    fn build_session(&mut self) -> Result<(), Error> {
        let (Some(vm), Some(agent_token)) = (&self.microvm, &self.pending_agent_token) else {
            return Err(Error::new(
                ErrorKind::Precondition,
                "no launch to build a session for",
            ));
        };
        let minter = Arc::new(ControlPlaneMinter::new(
            Arc::clone(&self.control),
            vm.id.clone(),
        ));
        // The plane's clock, so the session's exec ids and token refreshes read the same time
        // source as the lifecycle that launched it.
        let mut builder = Session::builder(vm.endpoint.clone(), agent_token.clone())
            .with_minter(minter)
            .with_port(self.control.port())
            .with_egress_posture(self.egress_posture())
            .with_clock(self.control.shared_clock());
        if let Some(backend) = &self.session_backend {
            builder = builder.with_backend(Arc::clone(backend));
        }
        self.session = Some(builder.build_with(self.control.adapters())?);
        Ok(())
    }

    /// Waits for a launch accepted by [`Sandbox::run`] to reach RUNNING.
    ///
    /// `run` calls this itself unless [`RunRequest::wait`] was off. A launch that adopted an
    /// existing VM through its client token resumes it if it idle-suspended (#195); a fresh
    /// launch that reaches any terminal state first fails fast with `stateReason` (TRAP-8).
    pub async fn wait_until_running(&mut self, timeout: Duration) -> Result<&mut Session, Error> {
        self.refuse_detached("wait until running")?;
        let id = self.require_microvm("wait_until_running")?;
        if self.lifecycle != Lifecycle::Pending {
            return Err(Error::invalid_arg(format!(
                "microvm {id} is {}; wait_until_running finishes a launch that is still PENDING",
                self.lifecycle,
            )));
        }
        let opts = WaitOpts {
            timeout,
            poll_interval: LIFECYCLE_POLL_INTERVAL,
            stall_grace: Duration::MAX,
        };
        let running = if self.launch_adoptable {
            self.control.wait_for_launch(&id, opts).await?
        } else {
            self.control.wait_for_running(&id, opts).await?
        };

        // STATE-2. The platform reported the run hook succeeded, so the token is in the
        // guest's memory — and this is the one place that counts it (STATE-3).
        self.set_lifecycle(Lifecycle::Running);
        self.token_installed = true;
        self.bootstrap_count += 1;
        self.microvm = Some(running);
        if self.session.is_none() {
            self.build_session()?;
        }
        Ok(self.session.as_mut().expect("just assigned"))
    }

    // ── adopt (STATE-3, with the lifecycle read from the service) ────────────

    /// A sandbox for a VM another process launched, rebuilt from its private record.
    ///
    /// The durable-workflow shape: every step runs in a fresh process, and each one needs
    /// suspend, resume, and terminate, not just the exec and file surface `Session::attach`
    /// (`microvms_core::prelude::SessionExt::attach`) gives. The lifecycle is read from `GetMicrovm`
    /// rather than assumed, so every guard below starts from what the service reports.
    ///
    /// # Bootstrap is counted, never repeated (STATE-3)
    ///
    /// The VM was bootstrapped by the launch that created it, so an adopted sandbox refuses
    /// [`Sandbox::run`] and never sends a run-hook payload. A VM adopted while still PENDING
    /// finishes through [`Sandbox::wait_until_running`], which counts the bootstrap once
    /// when the service reports RUNNING, the same as a launch this sandbox made itself.
    ///
    /// # The suspended window (STATE-12)
    ///
    /// The window comes from the `idlePolicy` `GetMicrovm` reports. A VM adopted while
    /// already SUSPENDED has no suspend time this client observed, so the local check has
    /// nothing to measure and the service answers the resume instead.
    ///
    /// `endpoint` must agree with the service's when the service reports one: a mismatch
    /// means the identifiers came from two different records. `agent_token` is the bearer
    /// credential the VM was launched with; it never appears in `Debug` or an error.
    pub async fn adopt(
        control: ControlPlane,
        microvm_id: impl Into<String>,
        endpoint: impl Into<String>,
        agent_token: impl Into<String>,
    ) -> Result<Self, Error> {
        let microvm_id = microvm_id.into();
        let endpoint = endpoint.into();
        let agent_token = agent_token.into();
        if agent_token.is_empty() {
            return Err(Error::invalid_arg(
                "adopt needs the agent token the VM was launched with: its daemon accepts only \
                 that bearer, and adopting without it would build a handle every exec refuses.",
            ));
        }
        let mut vm = control.get_microvm(&microvm_id).await?;
        if !endpoint.is_empty() && !vm.endpoint.is_empty() && vm.endpoint != endpoint {
            return Err(Error::invalid_arg(format!(
                "microvm {microvm_id} reports endpoint {}, not {endpoint}: the id and endpoint \
                 came from different records, and a session built from them would address one \
                 VM while its lifecycle calls reached another.",
                vm.endpoint,
            )));
        }
        if vm.endpoint.is_empty() {
            vm.endpoint = endpoint;
        }
        let Some(lifecycle) = Lifecycle::from_service(&vm.state) else {
            return Err(Error::new(
                ErrorKind::Platform,
                format!(
                    "microvm {microvm_id} reports state {}, which this client does not know; \
                     refusing to guess which lifecycle guards apply.",
                    vm.state,
                ),
            ));
        };

        let mut sandbox = Self::with_control_plane(control);
        sandbox.adopted = true;
        sandbox.set_lifecycle(lifecycle);
        // The window the VM was launched with, so a keepalive on the adopted session paces
        // itself against the real idle policy rather than the platform minimum.
        sandbox.idle_window = vm
            .idle_policy
            .as_ref()
            .map(|policy| Duration::from_secs(u64::from(policy.max_idle_duration_seconds)));
        // A VM exists, so the image it was launched from did (STATE-1).
        sandbox.image_exists = true;
        // PENDING may still idle-suspend before this handle sees RUNNING (#195).
        sandbox.launch_adoptable = true;
        sandbox.pending_agent_token = Some(agent_token);
        match lifecycle {
            Lifecycle::Pending => {}
            Lifecycle::Running | Lifecycle::Suspending | Lifecycle::Suspended => {
                sandbox.token_installed = true;
                sandbox.bootstrap_count = 1;
            }
            Lifecycle::Terminating | Lifecycle::Terminated => {
                sandbox.bootstrap_count = 1;
                sandbox.was_terminated = true;
            }
        }
        sandbox.microvm = Some(vm);
        if lifecycle.is_live() {
            sandbox.build_session()?;
        }
        Ok(sandbox)
    }

    /// A [`crate::names::NameRecord`] for this VM, to register under `name`.
    ///
    /// The egress posture is left unknown: the sandbox does not keep its launch request, and
    /// a record must not claim a network it cannot vouch for.
    pub fn name_record(&self, name: &str) -> Result<crate::names::NameRecord, Error> {
        let Some(vm) = self.microvm() else {
            return Err(Error::new(
                ErrorKind::Precondition,
                "there is no VM to name yet: launch or adopt one first",
            ));
        };
        let Some(session) = self.session() else {
            return Err(Error::new(
                ErrorKind::Precondition,
                format!(
                    "microvm {} has no session to take its agent token from",
                    vm.id
                ),
            ));
        };
        // Stamped on the plane's clock, the one every other time this sandbox reads comes from.
        crate::names::NameRecord::new_at(
            name,
            vm.id.as_str(),
            vm.endpoint.as_str(),
            session.agent_token(),
            self.control.region().as_str(),
            self.control.clock().unix_now().as_secs(),
        )
    }

    /// [`Sandbox::adopt`] from a [`crate::names::NameRecord`] kept in any store.
    pub async fn adopt_record(
        control: ControlPlane,
        record: crate::names::NameRecord,
    ) -> Result<Self, Error> {
        Self::adopt(
            control,
            record.microvm_id,
            record.endpoint,
            record.agent_token,
        )
        .await
    }

    /// Hands the VM off to another process and returns what that process needs to adopt it.
    ///
    /// For a workflow whose steps run in different processes (a durable function's launch
    /// step, then later steps that [`Sandbox::adopt`]): the launching process calls this
    /// instead of dropping the sandbox, which would warn that a live VM was abandoned. The
    /// VM keeps running and nothing is sent to AWS. From here on this sandbox is inert — its
    /// session is dropped and `run`, `wait_until_running`, `suspend`, `resume`, and
    /// `terminate` are refused — because the adopter now owns the lifecycle, and a second
    /// handle driving it would bypass the adopter's guards.
    ///
    /// Refused (`Precondition`) when there is no live VM to hand off: nothing launched yet,
    /// already torn down or terminated, or already detached.
    pub fn detach(&mut self) -> Result<Detached, Error> {
        self.refuse_detached("detach")?;
        let Some(vm) = self.microvm.as_ref() else {
            return Err(Error::new(
                ErrorKind::Precondition,
                "nothing to detach: this sandbox has not launched a VM.",
            ));
        };
        if self.torn_down || !self.lifecycle.is_live() {
            return Err(Error::new(
                ErrorKind::Precondition,
                format!(
                    "microvm {} is {} and there is no live VM to hand off; only a PENDING, \
                     RUNNING, or SUSPENDED VM can be adopted.",
                    vm.id, self.lifecycle,
                ),
            ));
        }
        let agent_token = self
            .session
            .as_ref()
            .map(|session| session.agent_token().to_string())
            .or_else(|| self.pending_agent_token.clone())
            .ok_or_else(|| {
                Error::new(
                    ErrorKind::Precondition,
                    format!(
                        "microvm {} has no agent token to hand off; an adopter could not \
                         reach its daemon.",
                        vm.id
                    ),
                )
            })?;
        let detached = Detached {
            microvm_id: vm.id.clone(),
            endpoint: vm.endpoint.clone(),
            region: self.control.region().clone(),
            port: self.control.port(),
            agent_token,
        };
        self.detached = true;
        self.session = None;
        self.pending_agent_token = None;
        Ok(detached)
    }

    /// Whether [`Sandbox::detach`] handed this sandbox's VM to another process.
    pub fn detached(&self) -> bool {
        self.detached
    }

    /// Refuses `what` on a sandbox whose VM was handed off.
    fn refuse_detached(&self, what: &str) -> Result<(), Error> {
        if !self.detached {
            return Ok(());
        }
        let id = self.microvm.as_ref().map_or("?", |vm| vm.id.as_str());
        Err(Error::new(
            ErrorKind::Precondition,
            format!(
                "cannot {what}: microvm {id} was detached and belongs to whoever adopts it. \
                 Adopt it with `Sandbox::adopt` and {what} through that handle."
            ),
        ))
    }

    /// Whether this sandbox was built by [`Sandbox::adopt`] rather than by its own launch.
    pub fn adopted(&self) -> bool {
        self.adopted
    }

    // ── suspend (STATE-4, STATE-5, STATE-6) ──────────────────────────────────

    /// Freezes the VM and waits for the platform to report it.
    ///
    /// A freeze and restore rather than a stop and start: the guest keeps its memory, so the
    /// token, the filesystem, and every exec record survive. The one thing that does not is
    /// the guest's view of time — it observes the whole suspension as a single jump, so any
    /// timeout, lease, or TLS session a running command holds expires at once on resume.
    ///
    /// # STATE-5 is checked before the wire, not after
    ///
    /// A suspend from anything but RUNNING is refused here with **zero** control-plane
    /// calls. That is the observable difference between this and a client that lets AWS
    /// answer, and it is what the test asserts on.
    pub async fn suspend(&mut self) -> Result<(), Error> {
        self.refuse_detached("suspend")?;
        let id = self.require_microvm("suspend")?;

        // STATE-5.
        if self.lifecycle != Lifecycle::Running {
            return Err(Error::invalid_arg(format!(
                "microvm {id} is {} and a suspend is only issued from RUNNING (STATE-5). Refused \
                 here rather than by the service, because the service's answer about a \
                 non-running id does not say which of the two things went wrong — and a suspend \
                 issued from SUSPENDED is a caller who believes they resumed.",
                self.lifecycle,
            )));
        }

        // STATE-4: accepted, so the lifecycle moves before the wait. Acceptance is the
        // wire call succeeding, so the assignment comes after it: moving to SUSPENDING
        // first would leave a failed call (a throttle, a dead transport) stuck in a state
        // neither suspend nor resume accepts, bricking the handle over one bad request.
        self.control.suspend(&id).await?;
        self.set_lifecycle(Lifecycle::Suspending);
        // Stamped after the call and before the wait, not after the wait: the idlePolicy's
        // window starts when the platform begins suspending, so timing it from SUSPENDED
        // would under-count the transition and call a closed window open.
        self.suspended_at = Some(self.control.clock().elapsed());

        let settled = self
            .control
            .wait_for_state(
                &id,
                &crate::control::microvm::SUSPEND_WANTED,
                &[],
                self.lifecycle_wait(),
            )
            .await?;

        // STATE-6, and the TERMINATED case beside it. A VM that dies while suspending is a
        // state to report rather than an exception out of the middle of a teardown, so the
        // wait *wants* TERMINATED — and recording it here is what stops a resume from being
        // offered afterwards (STATE-11).
        let settled_lifecycle = match settled.state.as_str() {
            "SUSPENDED" => Lifecycle::Suspended,
            "TERMINATED" => {
                self.was_terminated = true;
                Lifecycle::Terminated
            }
            other => {
                return Err(Error::new(
                    ErrorKind::Platform,
                    format!(
                        "the suspend wait returned {other}, which is neither SUSPENDED nor \
                         TERMINATED — and those two are what this client asked for."
                    ),
                ));
            }
        };
        self.set_lifecycle(settled_lifecycle);
        self.microvm = Some(settled);
        Ok(())
    }

    // ── resume (STATE-7, STATE-8, STATE-12) ──────────────────────────────────

    /// Thaws the VM and returns a usable session.
    ///
    /// # What is deliberately not re-delivered (STATE-7)
    ///
    /// Nothing. No run-hook payload, no token, no bootstrap. The in-memory token survived
    /// the freeze, and re-delivering it would hit the daemon's one-shot bootstrap and be
    /// refused — a 409 that reads like a broken VM.
    ///
    /// # The window is checked first (STATE-12)
    ///
    /// Before any wire call, because the answer is already known and calling costs the poll
    /// timeout to learn something worse. The falsification is a service reporting
    /// TERMINATED: without this check the resume burns the full deadline and then reports a
    /// state the client could have named at once.
    ///
    /// # The proxy token is dropped (STATE-8)
    ///
    /// [`Session::rebind`] invalidates it. The endpoint URL does not change across
    /// suspend/resume, so the rebind is usually a no-op on the URL — but a token minted
    /// against the pre-suspend instance may no longer validate, and that rejection reads
    /// exactly like a dead daemon.
    pub async fn resume(&mut self) -> Result<&mut Session, Error> {
        self.refuse_detached("resume")?;
        let id = self.require_microvm("resume")?;

        // STATE-11's local half: a terminated VM never returns to RUNNING, so the refusal
        // comes before the window check and before any call.
        if self.was_terminated || self.lifecycle == Lifecycle::Terminated {
            return Err(Error::invalid_arg(format!(
                "microvm {id} was terminated, and a terminated VM never returns to RUNNING \
                 (STATE-11). There is nothing to resume: the guest's memory is gone, so even a \
                 call the service accepted would hand back a different machine."
            )));
        }
        if self.lifecycle != Lifecycle::Suspended {
            return Err(Error::invalid_arg(format!(
                "microvm {id} is {} and a resume is only issued from SUSPENDED (STATE-7).",
                self.lifecycle,
            )));
        }

        // STATE-12, first and locally.
        self.require_open_suspended_window(&id)?;

        self.control.resume(&id).await?;
        // `fail_on` is the *dead* states rather than the terminal ones: SUSPENDED is the
        // state this call was made from, so failing on it would fail every resume. A VM the
        // idlePolicy terminated during suspension never reaches RUNNING, and waiting only
        // for RUNNING there burns the full timeout and then reports a timeout message
        // hiding a cause the service had already stated in `stateReason`.
        let running = self
            .control
            .wait_for_state(
                &id,
                &["RUNNING"],
                &crate::constants::DEAD_STATES,
                self.lifecycle_wait(),
            )
            .await?;

        self.set_lifecycle(Lifecycle::Running);
        // STATE-8, through the endpoint the service just reported rather than the one held:
        // the URL is measured not to change, and reading it from the response is what makes
        // that a fact this code depends on rather than an assumption it encodes.
        let endpoint = running.endpoint.clone();
        self.microvm = Some(running);
        if let Some(session) = self.session.as_mut() {
            session.rebind(endpoint);
        }
        // Cleared on success so the next cycle's window is measured from the next suspend.
        // Leaving it set would accumulate every suspension's elapsed time into one total and
        // reject a resume whose own window is wide open.
        self.suspended_at = None;

        self.session.as_mut().ok_or_else(|| {
            Error::new(
                ErrorKind::Unexpected,
                "the VM resumed but this sandbox holds no session, which run() always builds"
                    .to_string(),
            )
        })
    }

    /// Rejects a resume the launch-time `idlePolicy` has already made impossible (STATE-12).
    ///
    /// The message names the elapsed time, the window, and the `idlePolicy` finding, because
    /// "cannot resume" alone sends a reader looking for the flag that reopens it.
    fn require_open_suspended_window(&self, id: &str) -> Result<(), Error> {
        let reported = self
            .microvm
            .as_ref()
            .and_then(|vm| vm.idle_policy.as_ref())
            .map(|policy| Duration::from_secs(u64::from(policy.suspended_duration_seconds)));
        let (Some(window), Some(since)) = (self.suspended_window.or(reported), self.suspended_at)
        else {
            // No window or no suspend time means this sandbox cannot know how long the VM
            // has been suspended, and guessing would refuse a resume the service would honour.
            return Ok(());
        };
        let elapsed = self.control.clock().elapsed().saturating_sub(since);
        if elapsed <= window {
            return Ok(());
        }
        Err(Error::new(
            ErrorKind::WindowClosed,
            format!(
                "microvm {id} has been suspended {}s, past the {}s suspendedDurationSeconds \
                 window set at launch — the idlePolicy terminates a suspended VM once that window \
                 passes, so there is nothing left to resume (docs/PLATFORM.md, '`idlePolicy`'). \
                 Refused before ResumeMicrovm, because calling would cost the full poll timeout \
                 to learn the same thing less clearly. A longer window has to be set at launch \
                 on the next VM; there is no call that extends this one.",
                elapsed.as_secs(),
                window.as_secs(),
            ),
        ))
    }

    // ── terminate (STATE-9, STATE-10) ────────────────────────────────────────

    /// Tears down, best-effort, never erroring.
    ///
    /// Order: VM, then image, then the log group **last**. The log group is last because the
    /// service can recreate a group deleted before its image — and see
    /// [`TeardownReport::undeleted`] for why this crate names it rather than deleting it.
    pub async fn terminate(&mut self, opts: TeardownOpts) -> TeardownReport {
        let mut report = TeardownReport::default();
        if let Err(error) = self.refuse_detached("terminate") {
            // Never erroring is `terminate`'s contract, so the refusal is reported rather
            // than raised; the VM is untouched and still belongs to its adopter.
            report.failures.push(error.to_string());
            report.lifecycle = Some(self.lifecycle);
            return report;
        }
        self.torn_down = true;

        // The session first: it holds a cached proxy token whose only remaining use would be
        // a request against a VM that is going away.
        self.session = None;

        // 1. The VM.
        if let Some(id) = self.microvm.as_ref().map(|vm| vm.id.clone()) {
            // STATE-9. Recorded before the call, so a terminate whose call fails still marks
            // the VM as one this client asked to destroy — which is what stops a later
            // resume (STATE-11) rather than leaving the sandbox looking resumable.
            self.set_lifecycle(Lifecycle::Terminating);
            self.was_terminated = true;

            match self.control.terminate(&id).await {
                Ok(()) => report.terminate_accepted = true,
                Err(error) => {
                    report.failures.push(format!("terminate {id}: {error}"));
                    report.undeleted.push(id.clone());
                }
            }

            if report.terminate_accepted
                && let Some(timeout) = opts.wait_for_terminated
            {
                let wait = WaitOpts {
                    timeout,
                    poll_interval: LIFECYCLE_POLL_INTERVAL,
                    stall_grace: Duration::MAX,
                };
                match self
                    .control
                    .wait_for_state(&id, &["TERMINATED"], &[], wait)
                    .await
                {
                    // STATE-10.
                    Ok(settled) => {
                        self.set_lifecycle(Lifecycle::Terminated);
                        self.microvm = Some(settled);
                    }
                    // Not a leak: the platform accepted the terminate, so the VM is on its
                    // way out and the lifecycle stays TERMINATING honestly.
                    Err(error) => report
                        .failures
                        .push(format!("waiting for {id} to reach TERMINATED: {error}")),
                }
            }
        }

        // 2. The image, retrying — an image in CREATING refuses deletion and a VM still
        //    terminating holds a reference.
        if opts.delete_image {
            match self.image.as_ref().map(|image| image.identifier.clone()) {
                Some(identifier) => {
                    let deleted = self
                        .control
                        .delete_image(&identifier, opts.delete_attempts, opts.delete_backoff)
                        .await;
                    report.image_deleted = Some(deleted);
                    if deleted {
                        self.image_exists = false;
                    } else {
                        report.failures.push(format!(
                            "the image {identifier} survived {} delete attempts",
                            opts.delete_attempts.max(1)
                        ));
                        report.undeleted.push(identifier);
                    }
                }
                None => report.image_deleted = Some(false),
            }
        }

        // 3. The log group, LAST. Named rather than deleted; see TeardownReport::undeleted.
        if opts.delete_log_group
            && let Some(group) = self.image.as_ref().map(Image::build_log_group)
        {
            report.failures.push(format!(
                "the build log group {group} was not deleted: CloudWatch Logs is not in this \
                 crate's dependency set, so it is reported rather than removed. It is \
                 service-created, which means no Terraform stack owns it and `terraform destroy` \
                 leaves it behind — six accumulated before anyone noticed."
            ));
            report.undeleted.push(group);
        }

        report.lifecycle = Some(self.lifecycle);
        report
    }

    // ── internals ────────────────────────────────────────────────────────────

    /// The VM id, or a precondition error naming what was attempted.
    fn require_microvm(&self, what: &str) -> Result<String, Error> {
        self.microvm
            .as_ref()
            .map(|vm| vm.id.clone())
            .ok_or_else(|| {
                Error::new(
                    ErrorKind::Precondition,
                    format!("nothing to {what}: this sandbox has not launched a VM."),
                )
            })
    }

    /// Five minutes at five-second polls, for suspend, resume, and terminate.
    fn lifecycle_wait(&self) -> WaitOpts {
        WaitOpts {
            timeout: DEFAULT_LIFECYCLE_TIMEOUT,
            poll_interval: LIFECYCLE_POLL_INTERVAL,
            stall_grace: Duration::MAX,
        }
    }
}

/// Warns about a live VM rather than tearing it down.
///
/// See the module docs: `Drop` cannot await, so a teardown here would either deadlock
/// inside a runtime or race the process exit. The warning names the id, because the only
/// useful thing a drop can do is tell whoever reads stderr what to go delete.
///
/// The warning goes through the plane's [`Adapters::warn`](crate::adapters::Adapters::warn),
/// which in production writes it to stderr.
impl Sandbox {
    /// The warning `Drop` writes for a live VM nobody took responsibility for, or `None`
    /// when dropping is not a leak: torn down, adopted (its launcher owns it), or detached
    /// (its adopter owns it).
    fn drop_warning(&self) -> Option<String> {
        if self.torn_down || self.adopted || self.detached {
            return None;
        }
        let vm = self.microvm.as_ref()?;
        self.lifecycle.is_live().then(|| {
            format!(
                "warning: the Sandbox for microvm {} was dropped in {} without terminate() or \
                 detach(). Nothing was torn down — Drop cannot await, so a teardown here would \
                 deadlock inside a runtime. The VM bills until its maximumDurationInSeconds \
                 ceiling: terminate it with `microvm terminate {}`.",
                vm.id, self.lifecycle, vm.id,
            )
        })
    }
}

impl Drop for Sandbox {
    fn drop(&mut self) {
        if let Some(warning) = self.drop_warning() {
            self.control.adapters().warn(&warning);
        }
    }
}

/// A fresh per-VM bearer token: 32 bytes from `entropy` as 64 hex characters.
///
/// The hand-rolled `/dev/urandom` read this once replaced carried a clock-mixing fallback
/// for a failed read. An unavailable source refuses the launch instead: minting a bearer
/// token from a clock would be worse than not launching.
fn mint_agent_token(entropy: &dyn crate::entropy::Entropy) -> Result<String, Error> {
    let mut bytes = [0u8; 32];
    entropy.fill(&mut bytes)?;
    Ok(const_hex::encode(bytes))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::control::fake::{self as fake, Answer, FakeControlPlane, TestClock};
    use crate::control::transport::Method;

    /// A sandbox over the contract recorder, plus the two handles a test asserts through.
    ///
    /// The recorder is T-W2-4's, deliberately: its answers are literal JSON in the service
    /// model's spelling rather than values serialized from this crate's types, so a member
    /// this lane misreads cannot be misread identically by the fake.
    fn planted() -> (Sandbox, Arc<FakeControlPlane>, Arc<TestClock>) {
        let recorder = Arc::new(FakeControlPlane::new());
        let clock = Arc::new(TestClock::new());
        let plane = crate::testing::control_plane(
            Arc::clone(&recorder) as Arc<dyn crate::control::transport::Transport>,
            Region::UsEast1,
            Arc::clone(&clock) as Arc<dyn crate::control::Clock>,
        );
        (Sandbox::with_control_plane(plane), recorder, clock)
    }

    /// Queues everything a launch to RUNNING needs.
    fn answer_launch(recorder: &FakeControlPlane) {
        recorder
            .answer(
                "RunMicrovm",
                Answer::ok(fake::microvm_response("PENDING", None)),
            )
            .answer(
                "GetMicrovm",
                Answer::ok(fake::microvm_response("RUNNING", None)),
            )
            .answer(
                "CreateMicrovmAuthToken",
                Answer::ok(fake::auth_token_response("proxy-token")),
            );
    }

    #[tokio::test]
    async fn vpc_egress_configuration_survives_the_sandbox_launch() {
        let (mut sandbox, recorder, _) = planted();
        answer_launch(&recorder);
        let arn = "arn:aws:lambda:us-east-1:123456789012:network-connector:isolated-vpc";
        let request = RunRequest::new()
            .with_image("arn:image")
            .with_egress_network_connector(arn);
        assert_eq!(
            request.egress_posture(),
            crate::control::EgressPosture::Unsealed
        );
        sandbox.run(request).await.expect("launch");
        let body = recorder.first_body("RunMicrovm");
        assert_eq!(body["egressNetworkConnectors"], serde_json::json!([arn]));
    }

    /// **A sandbox's session reads the plane's clock.** Its exec ids carry the plane's wall
    /// reading, so the lifecycle and the session it hands out share one time source.
    ///
    /// **Falsification**: drop `with_clock(self.control.shared_clock())` from `build_session`
    /// and the session takes the adapters' clock, whose wall reading is another instant.
    #[tokio::test]
    async fn a_sandbox_session_reads_the_planes_clock() {
        let (mut sandbox, recorder, clock) = planted();
        answer_launch(&recorder);
        let session = sandbox
            .run(RunRequest::new().with_image("arn:image"))
            .await
            .expect("the launch reaches RUNNING");
        let pinned = Duration::from_secs(1_767_225_600) + Duration::from_nanos(987_654_321);
        clock.set_unix_now(pinned);
        let id = session.mint_exec_id();
        let value =
            u64::from_str_radix(id.strip_prefix("x-").expect("the x- shape"), 16).expect("hex");
        let low_bits = (1_u64 << 40) - 1;
        assert_eq!(
            value & low_bits,
            (pinned.as_nanos() as u64) & low_bits,
            "{id} wasn't minted from the plane's clock"
        );
    }

    /// **`ensure_image` builds its STS and S3 client through the plane's adapters**, so a
    /// plane over test adapters never reaches a real credential chain.
    ///
    /// **Falsification**: build `SignedBuildServices` inline again, and the refusal (or, with
    /// credentials in the environment, the call) is the real chain's rather than the
    /// adapters'.
    #[tokio::test]
    async fn ensure_image_builds_its_services_through_the_planes_adapters() {
        let (mut sandbox, recorder, _) = planted();
        let dockerfile = crate::control::artifact::wrap_dockerfile(
            "FROM python:3.12-slim\nWORKDIR /app\n",
            &crate::control::artifact::WrapOptions::default(),
        )
        .expect("a wrappable Dockerfile");
        let request = crate::control::EnsureImageRequest::new(
            "task",
            b"daemon".to_vec(),
            dockerfile,
            "artifact-bucket",
            "arn:aws:iam::123456789012:role/build",
        );
        let error = sandbox
            .ensure_image(request)
            .await
            .expect_err("the test adapters build no services");
        assert_eq!(error.kind(), ErrorKind::Credentials, "{error}");
        assert!(
            error
                .to_string()
                .contains("the test adapters build no STS or S3 client for us-east-1"),
            "{error}"
        );
        assert!(
            recorder.calls().is_empty(),
            "no control-plane call came first"
        );
    }

    /// **A launch draws its agent token, identity seeds and client token from the plane's
    /// entropy**, in that order, so the one source a caller or a test swaps in decides all
    /// three.
    ///
    /// **Falsification**: mint any of the three from `OsEntropy` instead of
    /// `self.control.entropy()` and its assertion below reads a value the scripted source
    /// never produced.
    #[tokio::test]
    async fn a_launch_draws_every_random_value_from_the_planes_entropy() {
        use crate::entropy::testing::SequenceEntropy;
        use base64::Engine as _;

        let recorder = Arc::new(FakeControlPlane::new());
        let entropy = Arc::new(SequenceEntropy::new());
        let plane = crate::testing::control_plane(
            Arc::clone(&recorder) as Arc<dyn crate::control::transport::Transport>,
            Region::UsEast1,
            Arc::new(TestClock::new()),
        )
        .with_entropy(Arc::clone(&entropy) as Arc<dyn crate::entropy::Entropy>);
        let mut sandbox = Sandbox::with_control_plane(plane);
        answer_launch(&recorder);
        let session = sandbox
            .run(RunRequest::new().with_image("arn:image").with_identity())
            .await
            .expect("the launch reaches RUNNING");

        assert_eq!(
            session.agent_token(),
            const_hex::encode(SequenceEntropy::draw(1, 32)),
            "the agent token"
        );
        let body = recorder.first_body("RunMicrovm");
        let payload: serde_json::Value =
            serde_json::from_str(body["runHookPayload"].as_str().expect("a string payload"))
                .expect("the payload is JSON");
        assert_eq!(
            payload[protocol::identity::SEED_KEY],
            base64::engine::general_purpose::STANDARD
                .encode(SequenceEntropy::draw(2, protocol::identity::SEED_BYTES)),
            "the VM seed"
        );
        let client_token = body["clientToken"].as_str().expect("a client token");
        assert!(
            client_token.ends_with(&const_hex::encode(SequenceEntropy::draw(4, 8))),
            "the client token's nonce: {client_token}"
        );
        assert_eq!(
            entropy.calls(),
            4,
            "one draw per value, the host seed included"
        );
        sandbox.detach().expect("hand the scripted VM off quietly");
    }

    /// An unavailable entropy source refuses the launch before anything reaches the plane.
    #[tokio::test]
    async fn an_unavailable_entropy_source_refuses_the_launch() {
        use crate::entropy::testing::SequenceEntropy;

        let recorder = Arc::new(FakeControlPlane::new());
        let plane = crate::testing::control_plane(
            Arc::clone(&recorder) as Arc<dyn crate::control::transport::Transport>,
            Region::UsEast1,
            Arc::new(TestClock::new()),
        )
        .with_entropy(Arc::new(SequenceEntropy::unavailable()));
        let mut sandbox = Sandbox::with_control_plane(plane);
        answer_launch(&recorder);
        let err = sandbox
            .run(RunRequest::new().with_image("arn:image"))
            .await
            .expect_err("no pool, no bearer token");
        assert_eq!(err.kind(), ErrorKind::Unexpected, "{err}");
        assert_eq!(recorder.call_count("RunMicrovm"), 0);
    }

    /// **BIND-12: the launched session carries its request's posture**, the value
    /// `egress_posture_for` answers for the same options and the CLI envelope reports
    /// (`guards.rs` holds that half). Each of the four launchable shapes.
    ///
    /// **Falsification** — 2026-09-24. Build the session without `with_egress_posture` and
    /// the `open` and `best-effort` rows read `unsealed`; restored.
    #[tokio::test]
    async fn a_launched_session_reports_its_requests_posture() {
        use crate::control::EgressPosture;
        let arn = "arn:aws:lambda:us-east-1:123456789012:network-connector:isolated-vpc";
        let rows = [
            (RunRequest::new(), EgressPosture::Unsealed),
            (RunRequest::new().with_egress(), EgressPosture::Open),
            (
                RunRequest::new().with_deny_egress(),
                EgressPosture::BestEffort,
            ),
            (
                RunRequest::new().with_egress_network_connector(arn),
                EgressPosture::Unsealed,
            ),
        ];
        for (request, expected) in rows {
            let answered = crate::control::egress_posture_for(
                request.egress,
                &request.egress_network_connectors,
                request.deny_egress,
                Some(&Region::UsEast1),
            )
            .expect("a launchable request");
            assert_eq!(answered, expected);
            let (mut sandbox, recorder, _) = planted();
            answer_launch(&recorder);
            let session = sandbox
                .run(request.with_image("arn:image"))
                .await
                .expect("the launch reaches RUNNING");
            assert_eq!(session.egress_posture(), expected, "BIND-12");
            assert_eq!(sandbox.egress_posture(), expected);
            sandbox.detach().expect("hand the scripted VM off quietly");
        }
    }

    /// **BIND-12: a session that does not hold its launch options reports `unsealed`**, the
    /// weakest true claim, whatever the VM was launched with: an adopted VM and a session
    /// attached directly both lack the request.
    #[tokio::test]
    async fn a_session_without_its_launch_options_reports_unsealed() {
        use crate::control::EgressPosture;
        let (sandbox, _recorder, _clock) = adopted_in("RUNNING").await;
        assert_eq!(
            sandbox.session().expect("RUNNING").egress_posture(),
            EgressPosture::Unsealed
        );
        assert_eq!(sandbox.egress_posture(), EgressPosture::Unsealed);
        let direct = Session::builder(ADOPT_ENDPOINT, ADOPT_TOKEN)
            .build_with(&crate::testing::TestAdapters::new())
            .expect("a direct session");
        assert_eq!(direct.egress_posture(), EgressPosture::Unsealed);
    }

    /// A launched sandbox in RUNNING, which is where four of the twelve keys start.
    async fn launched() -> (Sandbox, Arc<FakeControlPlane>, Arc<TestClock>) {
        let (mut sandbox, recorder, clock) = planted();
        answer_launch(&recorder);
        sandbox
            .run(RunRequest::new().with_image("arn:image"))
            .await
            .expect("the launch reaches RUNNING");
        (sandbox, recorder, clock)
    }

    /// **A dropped live sandbox warns through the plane's adapters**, not a stream of its
    /// own, so the driving adapter decides where the warning goes (ARCH-7).
    ///
    /// **Falsification**: write the warning straight to stderr in `Drop` and the recorded
    /// list comes back empty (and the crate's `clippy.toml` refuses `std::io::stderr`).
    #[tokio::test]
    async fn a_dropped_live_sandbox_warns_through_the_planes_adapters() {
        let recorder = Arc::new(FakeControlPlane::new());
        let clock = Arc::new(TestClock::new());
        let adapters = Arc::new(crate::testing::TestAdapters::new());
        let plane = crate::control::ControlPlane::from_ports(
            Arc::clone(&recorder) as Arc<dyn crate::control::transport::Transport>,
            Region::UsEast1,
            Arc::clone(&clock) as Arc<dyn crate::control::Clock>,
            Arc::new(crate::testing::SequenceEntropy::new()),
            Arc::clone(&adapters) as Arc<dyn crate::adapters::Adapters>,
        );
        let mut sandbox = Sandbox::with_control_plane(plane);
        answer_launch(&recorder);
        sandbox
            .run(RunRequest::new().with_image("arn:image"))
            .await
            .expect("the launch reaches RUNNING");
        let id = sandbox.microvm().expect("launched").id.clone();
        drop(sandbox);
        let warnings = adapters.warnings();
        assert_eq!(warnings.len(), 1, "{warnings:?}");
        assert!(warnings[0].contains(&id), "{warnings:?}");
    }

    /// **Detach hands off, and nothing warns or moves afterwards.** A launched sandbox
    /// warns on drop; once detached it returns what an adopter needs (with the token kept
    /// out of `Debug`), stops warning, makes no further control-plane call, and refuses every
    /// transition — including `terminate`, which reports the refusal instead of raising.
    ///
    /// **Falsification** — 2026-09-24. Leave `detached` out of `drop_warning`'s early
    /// return and the "no warning after detach" assertion goes red; drop the
    /// `refuse_detached` guard from `suspend` and the zero-calls assertion goes red (the
    /// suspend reaches `SuspendMicrovm`). Both restored.
    #[tokio::test]
    async fn a_detached_sandbox_hands_off_its_vm_and_stays_quiet() {
        let (mut sandbox, recorder, _clock) = launched().await;
        assert!(
            sandbox.drop_warning().is_some(),
            "a launched, live VM warns on drop"
        );
        let token = sandbox
            .session()
            .expect("launched")
            .agent_token()
            .to_string();
        let vm = sandbox.microvm().expect("launched").clone();

        let detached = sandbox.detach().expect("a RUNNING VM detaches");
        assert_eq!(detached.microvm_id, vm.id);
        assert_eq!(detached.endpoint, vm.endpoint);
        assert_eq!(detached.agent_token(), token);
        assert_eq!(detached.region, Region::UsEast1);
        let printed = format!("{detached:?} {sandbox:?}");
        assert!(!printed.contains(&token), "{printed}");
        assert!(printed.contains("detached: true"), "{printed}");

        assert!(sandbox.detached());
        assert!(
            sandbox.drop_warning().is_none(),
            "detached is not abandoned"
        );
        assert!(
            sandbox.session().is_none(),
            "the session went to the adopter"
        );

        let before = recorder.calls().len();
        for error in [
            sandbox.suspend().await.expect_err("detached"),
            sandbox.resume().await.map(|_| ()).expect_err("detached"),
            sandbox
                .run(RunRequest::new().with_image("arn:image"))
                .await
                .map(|_| ())
                .expect_err("detached"),
            sandbox
                .wait_until_running(Duration::from_secs(1))
                .await
                .map(|_| ())
                .expect_err("detached"),
            sandbox.detach().map(|_| ()).expect_err("already detached"),
        ] {
            assert_eq!(error.kind(), ErrorKind::Precondition, "{error}");
            assert!(error.to_string().contains("detached"), "{error}");
            assert!(!error.to_string().contains(&token), "{error}");
        }
        let report = sandbox.terminate(TeardownOpts::default()).await;
        assert!(!report.terminate_accepted);
        assert!(report.failures[0].contains("detached"), "{report:?}");
        assert_eq!(recorder.calls().len(), before, "detached makes no AWS call");
        assert!(
            sandbox.drop_warning().is_none(),
            "a refused terminate is still quiet"
        );
    }

    /// Detach needs a live VM: nothing launched, or already torn down, is refused.
    #[tokio::test]
    async fn detach_refuses_without_a_live_vm() {
        let (mut unlaunched, _recorder, _clock) = planted();
        let error = unlaunched.detach().expect_err("nothing launched");
        assert_eq!(error.kind(), ErrorKind::Precondition);
        assert!(!unlaunched.detached());

        let (mut sandbox, recorder, _clock) = launched().await;
        recorder
            .answer("TerminateMicrovm", Answer::ok("{}"))
            .answer(
                "GetMicrovm",
                Answer::ok(fake::microvm_response("TERMINATED", None)),
            );
        sandbox.terminate(TeardownOpts::default()).await;
        let error = sandbox.detach().expect_err("torn down");
        assert_eq!(error.kind(), ErrorKind::Precondition, "{error}");
        assert!(!sandbox.detached());
    }

    /// Drives a launched sandbox to SUSPENDED, which is where resume starts.
    ///
    /// Both `GetMicrovm` answers are queued **before** the launch rather than one before
    /// each wait, and that is not a style choice. The recorder repeats its last queued
    /// answer, so a queue left at `[RUNNING]` after the launch and then appended to becomes
    /// `[RUNNING, SUSPENDED]` — the suspend wait pops the stale RUNNING, sleeps a poll
    /// interval, and the fake's `sleep` **advances the clock**. That five seconds is
    /// invisible until it lands in the middle of the STATE-12 window arithmetic, which is
    /// exactly where it first showed up. Queueing both up front means every wait matches on
    /// its first poll and no test clock moves except when a test moves it.
    async fn suspended_with_window(
        suspended_sec: u32,
    ) -> (Sandbox, Arc<FakeControlPlane>, Arc<TestClock>) {
        let (mut sandbox, recorder, clock) = planted();
        recorder
            .answer(
                "RunMicrovm",
                Answer::ok(fake::microvm_response("PENDING", None)),
            )
            .answer(
                "GetMicrovm",
                Answer::ok(fake::microvm_response("RUNNING", None)),
            )
            .answer(
                "GetMicrovm",
                Answer::ok(fake::microvm_response("SUSPENDED", None)),
            )
            .answer(
                "CreateMicrovmAuthToken",
                Answer::ok(fake::auth_token_response("proxy-token")),
            )
            .answer("SuspendMicrovm", Answer::ok(fake::empty_response()));

        sandbox
            .run(
                RunRequest::new()
                    .with_image("arn:image")
                    .with_suspended_sec(suspended_sec),
            )
            .await
            .expect("launches");
        sandbox.suspend().await.expect("suspends");
        assert_eq!(
            clock.now(),
            Duration::ZERO,
            "no wait may have slept, or the window arithmetic below is measuring the fake's \
             poll interval as well as the elapsed suspension"
        );
        (sandbox, recorder, clock)
    }

    /// A suspended sandbox at the default ten-minute window.
    async fn suspended() -> (Sandbox, Arc<FakeControlPlane>, Arc<TestClock>) {
        suspended_with_window(600).await
    }

    // ── adopt (#196) ─────────────────────────────────────────────────────────

    /// A canary agent token, so a test can assert it appears in no `Debug` or error.
    const ADOPT_TOKEN: &str = "adopt-canary-token-9f1c";
    const ADOPT_ENDPOINT: &str = "https://mvm-abc123.microvm.us-east-1.amazonaws.com";

    /// A plane over the recorder, whose `GetMicrovm` answers the caller queues.
    fn adopt_plane() -> (ControlPlane, Arc<FakeControlPlane>, Arc<TestClock>) {
        let recorder = Arc::new(FakeControlPlane::new());
        let clock = Arc::new(TestClock::new());
        let plane = crate::testing::control_plane(
            Arc::clone(&recorder) as Arc<dyn crate::control::transport::Transport>,
            Region::UsEast1,
            Arc::clone(&clock) as Arc<dyn crate::control::Clock>,
        );
        (plane, recorder, clock)
    }

    async fn adopted_in(state: &str) -> (Sandbox, Arc<FakeControlPlane>, Arc<TestClock>) {
        let (plane, recorder, clock) = adopt_plane();
        recorder.answer(
            "GetMicrovm",
            Answer::ok(fake::microvm_response(state, None)),
        );
        let sandbox = Sandbox::adopt(plane, "mvm-abc123", ADOPT_ENDPOINT, ADOPT_TOKEN)
            .await
            .expect("adopts");
        (sandbox, recorder, clock)
    }

    /// **An adopted sandbox feeds its keepalive.** The idle window is the one `GetMicrovm`
    /// reports, and the lifecycle watch starts at the adopted state, so a keepalive on an
    /// adopted session paces against the real policy and ends when that sandbox suspends.
    ///
    /// **Falsification** — 2026-09-24. Assign `sandbox.lifecycle` directly instead of
    /// through `set_lifecycle`, or drop the `idle_window` line, and this goes red; restored.
    #[tokio::test]
    async fn an_adopted_sandbox_reports_the_services_idle_window_and_watches_its_lifecycle() {
        let (sandbox, _recorder, _clock) = adopted_in("RUNNING").await;
        assert_eq!(sandbox.idle_window(), Some(Duration::from_secs(1800)));
        assert_eq!(*sandbox.watch_lifecycle().borrow(), Lifecycle::Running);
    }

    /// Every service state has a lifecycle, and nothing else parses.
    #[test]
    fn every_service_state_maps_onto_one_lifecycle() {
        for state in crate::constants::MICROVM_STATES {
            let lifecycle = Lifecycle::from_service(state).expect("a known state");
            assert_eq!(lifecycle.as_str(), state);
        }
        assert_eq!(Lifecycle::from_service("Running"), None);
    }

    /// **STATE-3 for an adopted VM.** The lifecycle is the service's, the bootstrap is
    /// counted once, and `run` is refused before any wire call, so no run-hook payload is
    /// ever sent.
    ///
    /// **Falsification** — 2026-09-24. Leave the adopted lifecycle at PENDING instead of
    /// reading it from `GetMicrovm` and the lifecycle and `token_installed` assertions here
    /// go red, as does the suspend in the next test; restored after.
    #[tokio::test]
    async fn an_adopted_vm_takes_its_lifecycle_from_the_service_and_refuses_run() {
        let (mut sandbox, recorder, _) = adopted_in("RUNNING").await;
        assert!(sandbox.adopted());
        assert_eq!(sandbox.lifecycle(), Lifecycle::Running);
        assert!(sandbox.token_installed());
        assert_eq!(sandbox.bootstrap_count(), 1);
        assert!(sandbox.image_exists());
        let session = sandbox.session().expect("a live VM gets a session");
        assert_eq!(session.endpoint(), ADOPT_ENDPOINT);
        assert_eq!(session.agent_token(), ADOPT_TOKEN);

        let error = sandbox
            .run(RunRequest::new().with_image("arn:image"))
            .await
            .expect_err("an adopted VM was bootstrapped by its own launch");
        assert_eq!(error.kind(), ErrorKind::InvalidArg);
        assert!(error.to_string().contains("STATE-3"), "{error}");
        assert_eq!(recorder.call_count("RunMicrovm"), 0);
        assert_eq!(recorder.operations(), vec!["GetMicrovm"]);
    }

    /// A name resolves to the record's VM and adopts it; a missing name or a record from
    /// another region is refused before any AWS call.
    ///
    /// `Sandbox::from_name` itself builds a production plane, so its refusals are pinned in
    /// the prelude's tests. What's here is the resolve it runs first, and the adopt after.
    #[tokio::test]
    async fn a_name_adopts_its_records_vm_and_foreign_names_are_refused_locally() {
        struct One(crate::names::NameRecord);
        impl crate::names::NameStore for One {
            fn get(&self, name: &str) -> Result<Option<crate::names::NameRecord>, Error> {
                Ok((name == self.0.name).then(|| self.0.clone()))
            }
            fn put(&self, _: &crate::names::NameRecord) -> Result<(), Error> {
                unreachable!()
            }
            fn delete(&self, _: &str) -> Result<bool, Error> {
                unreachable!()
            }
            fn list(&self) -> Result<Vec<crate::names::NameRecord>, Error> {
                Ok(vec![self.0.clone()])
            }
        }
        let record = crate::names::NameRecord::new_at(
            "ci",
            "mvm-abc123",
            ADOPT_ENDPOINT,
            ADOPT_TOKEN,
            "us-west-2",
            1_767_225_600,
        )
        .expect("record");
        let store = One(record.clone());
        let foreign = crate::names::resolve(&store, "ci", Some(&Region::UsEast1))
            .expect_err("registered in another region");
        assert_eq!(foreign.kind(), ErrorKind::InvalidArg);
        let missing = crate::names::resolve(&store, "nope", None).expect_err("no such name");
        assert_eq!(missing.kind(), ErrorKind::Precondition);
        assert!(!format!("{foreign} {missing}").contains(ADOPT_TOKEN));

        let (plane, recorder, _) = adopt_plane();
        recorder.answer(
            "GetMicrovm",
            Answer::ok(fake::microvm_response("RUNNING", None)),
        );
        let resolved = crate::names::resolve(&store, "ci", Some(&Region::UsWest2)).expect("ok");
        let sandbox = Sandbox::adopt_record(plane, resolved)
            .await
            .expect("adopts");
        assert!(sandbox.adopted());
        assert_eq!(
            sandbox.microvm().map(|vm| vm.id.as_str()),
            Some("mvm-abc123")
        );
        assert_eq!(
            sandbox.session().expect("session").agent_token(),
            ADOPT_TOKEN
        );
        let named = sandbox
            .name_record("again")
            .expect("an adopted VM can be named");
        assert_eq!(
            (
                named.microvm_id.as_str(),
                named.agent_token.as_str(),
                named.region.as_str()
            ),
            ("mvm-abc123", ADOPT_TOKEN, "us-east-1")
        );
        let unlaunched = Sandbox::with_control_plane(adopt_plane().0);
        assert_eq!(
            unlaunched
                .name_record("x")
                .expect_err("nothing to name")
                .kind(),
            ErrorKind::Precondition
        );
    }

    /// Suspend, resume, and terminate all work through an adopted handle with the usual
    /// guards, and nothing about the launch is re-delivered (STATE-7).
    #[tokio::test]
    async fn an_adopted_vm_suspends_resumes_and_terminates_through_its_handle() {
        let (plane, recorder, _) = adopt_plane();
        recorder
            .answer(
                "GetMicrovm",
                Answer::ok(fake::microvm_response("RUNNING", None)),
            )
            .answer(
                "GetMicrovm",
                Answer::ok(fake::microvm_response("SUSPENDED", None)),
            )
            .answer(
                "GetMicrovm",
                Answer::ok(fake::microvm_response("RUNNING", None)),
            )
            .answer(
                "GetMicrovm",
                Answer::ok(fake::microvm_response("TERMINATED", None)),
            )
            .answer("SuspendMicrovm", Answer::ok(fake::empty_response()))
            .answer("ResumeMicrovm", Answer::ok(fake::empty_response()))
            .answer("TerminateMicrovm", Answer::ok(fake::empty_response()));
        let mut sandbox = Sandbox::adopt(plane, "mvm-abc123", ADOPT_ENDPOINT, ADOPT_TOKEN)
            .await
            .expect("adopts");

        sandbox.suspend().await.expect("suspends from RUNNING");
        assert_eq!(sandbox.lifecycle(), Lifecycle::Suspended);
        sandbox.resume().await.expect("resumes from SUSPENDED");
        assert_eq!(sandbox.lifecycle(), Lifecycle::Running);
        let report = sandbox
            .terminate(TeardownOpts::default().waiting_for_terminated())
            .await;
        assert!(report.terminate_accepted, "{report:?}");
        assert_eq!(sandbox.lifecycle(), Lifecycle::Terminated);
        assert!(sandbox.was_terminated());

        assert_eq!(recorder.call_count("RunMicrovm"), 0);
        assert!(
            recorder
                .bodies_as_text()
                .iter()
                .all(|body| !body.contains("runHookPayload") && !body.contains(ADOPT_TOKEN)),
            "no launch payload or agent token may reach the control plane"
        );
        assert_eq!(sandbox.bootstrap_count(), 1, "STATE-3: never above one");
    }

    /// **STATE-12 for an adopted VM.** With no window from a request of its own, the
    /// sandbox refuses a late resume using the `idlePolicy` window `GetMicrovm` reported.
    ///
    /// **Falsification** — 2026-09-24. Drop the reported-window fallback in
    /// `require_open_suspended_window` and this resume reaches `ResumeMicrovm`; restored
    /// after.
    #[tokio::test]
    async fn an_adopted_vm_refuses_a_resume_past_the_reported_window() {
        let (plane, recorder, clock) = adopt_plane();
        recorder
            .answer(
                "GetMicrovm",
                Answer::ok(fake::microvm_response("RUNNING", None)),
            )
            .answer(
                "GetMicrovm",
                Answer::ok(fake::microvm_response("SUSPENDED", None)),
            )
            .answer("SuspendMicrovm", Answer::ok(fake::empty_response()));
        let mut sandbox = Sandbox::adopt(plane, "mvm-abc123", ADOPT_ENDPOINT, ADOPT_TOKEN)
            .await
            .expect("adopts");
        assert_eq!(sandbox.suspended_window(), None, "no request of its own");
        sandbox.suspend().await.expect("suspends");

        clock.advance(Duration::from_secs(601));
        let error = sandbox
            .resume()
            .await
            .expect_err("past the 600 s window the service reported");
        assert_eq!(error.kind(), ErrorKind::WindowClosed);
        assert_eq!(recorder.call_count("ResumeMicrovm"), 0);
    }

    /// A VM adopted while already SUSPENDED has no locally observed suspend time, so the
    /// resume goes to the service rather than being refused on a guess.
    #[tokio::test]
    async fn an_adopted_suspended_vm_resumes_through_the_service() {
        let (plane, recorder, _) = adopt_plane();
        recorder
            .answer(
                "GetMicrovm",
                Answer::ok(fake::microvm_response("SUSPENDED", None)),
            )
            .answer(
                "GetMicrovm",
                Answer::ok(fake::microvm_response("RUNNING", None)),
            )
            .answer("ResumeMicrovm", Answer::ok(fake::empty_response()));
        let mut sandbox = Sandbox::adopt(plane, "mvm-abc123", ADOPT_ENDPOINT, ADOPT_TOKEN)
            .await
            .expect("adopts");
        assert_eq!(sandbox.lifecycle(), Lifecycle::Suspended);
        let session = sandbox
            .resume()
            .await
            .expect("the service answers the resume");
        assert_eq!(session.endpoint(), ADOPT_ENDPOINT);
        assert_eq!(recorder.call_count("ResumeMicrovm"), 1);
    }

    /// **STATE-11 for an adopted VM.** A terminated VM is offered neither a resume nor a
    /// suspend, and gets no session.
    #[tokio::test]
    async fn an_adopted_terminated_vm_refuses_every_transition_locally() {
        let (mut sandbox, recorder, _) = adopted_in("TERMINATED").await;
        assert!(sandbox.was_terminated());
        assert!(sandbox.session().is_none());
        let resume = sandbox.resume().await.expect_err("STATE-11");
        assert!(resume.to_string().contains("STATE-11"), "{resume}");
        let suspend = sandbox.suspend().await.expect_err("STATE-5");
        assert!(suspend.to_string().contains("STATE-5"), "{suspend}");
        assert_eq!(recorder.operations(), vec!["GetMicrovm"]);
    }

    /// A VM adopted while PENDING finishes through `wait_until_running`, which counts the
    /// bootstrap exactly once.
    #[tokio::test]
    async fn an_adopted_pending_vm_counts_its_bootstrap_once_on_running() {
        let (plane, recorder, _) = adopt_plane();
        recorder
            .answer(
                "GetMicrovm",
                Answer::ok(fake::microvm_response("PENDING", None)),
            )
            .answer(
                "GetMicrovm",
                Answer::ok(fake::microvm_response("RUNNING", None)),
            );
        let mut sandbox = Sandbox::adopt(plane, "mvm-abc123", ADOPT_ENDPOINT, ADOPT_TOKEN)
            .await
            .expect("adopts");
        assert_eq!(sandbox.lifecycle(), Lifecycle::Pending);
        assert_eq!(sandbox.bootstrap_count(), 0);
        sandbox
            .wait_until_running(Duration::from_secs(60))
            .await
            .expect("reaches RUNNING");
        assert_eq!(sandbox.bootstrap_count(), 1);
        assert!(sandbox.token_installed());
        assert!(
            sandbox
                .run(RunRequest::new().with_image("arn:image"))
                .await
                .is_err(),
            "still one bootstrap per VM"
        );
        assert_eq!(recorder.call_count("RunMicrovm"), 0);
    }

    /// The refusals: an empty token before any call, a mismatched endpoint, and a state
    /// this client does not know. None of them, and no `Debug`, prints the token.
    ///
    /// **Falsification** — 2026-09-24. Remove the endpoint comparison and the mismatch case
    /// adopts; remove the empty-token check and that case reaches `GetMicrovm`; print the
    /// held token in `Debug` and the canary assertion fails. Each restored after.
    #[tokio::test]
    async fn adopt_refuses_bad_records_and_never_prints_the_token() {
        let (plane, recorder, _) = adopt_plane();
        let empty = Sandbox::adopt(plane, "mvm-abc123", ADOPT_ENDPOINT, "")
            .await
            .expect_err("no token");
        assert_eq!(empty.kind(), ErrorKind::InvalidArg);
        assert_eq!(recorder.calls().len(), 0, "refused before any call");

        let (plane, recorder, _) = adopt_plane();
        recorder.answer(
            "GetMicrovm",
            Answer::ok(fake::microvm_response("RUNNING", None)),
        );
        let mismatch = Sandbox::adopt(plane, "mvm-abc123", "https://other.example", ADOPT_TOKEN)
            .await
            .expect_err("the endpoint belongs to another record");
        assert_eq!(mismatch.kind(), ErrorKind::InvalidArg);
        assert!(!mismatch.to_string().contains(ADOPT_TOKEN));

        let (plane, recorder, _) = adopt_plane();
        recorder.answer(
            "GetMicrovm",
            Answer::ok(fake::microvm_response("REBOOTING", None)),
        );
        let unknown = Sandbox::adopt(plane, "mvm-abc123", ADOPT_ENDPOINT, ADOPT_TOKEN)
            .await
            .expect_err("an unknown state");
        assert_eq!(unknown.kind(), ErrorKind::Platform);
        assert!(!unknown.to_string().contains(ADOPT_TOKEN));

        let (sandbox, _, _) = adopted_in("RUNNING").await;
        let printed = format!("{sandbox:?}");
        assert!(!printed.contains(ADOPT_TOKEN), "{printed}");
        assert!(printed.contains("adopted: true"), "{printed}");
    }

    /// **STATE-1 and STATE-2.** A launch accepted is PENDING with the image recorded as
    /// existing; the platform reporting the run hook succeeded is what marks the token
    /// installed.
    ///
    /// The two halves are asserted with the *same* fake because they are the same call, and
    /// what distinguishes them is which fact is recorded when: `image_exists` is true from
    /// the accepted launch, `token_installed` only from the RUNNING report. A client that
    /// set both at the launch call would pass an end-state assertion and be wrong about a
    /// VM that died during startup — which is the next test.
    #[tokio::test]
    async fn a_launch_records_the_image_and_the_running_report_installs_the_token() {
        let (mut sandbox, recorder, _) = planted();
        assert_eq!(sandbox.lifecycle(), Lifecycle::Pending);
        assert!(
            !sandbox.image_exists(),
            "nothing is recorded before a launch"
        );
        assert!(!sandbox.token_installed());

        answer_launch(&recorder);
        let session = sandbox
            .run(RunRequest::new().with_image("arn:image"))
            .await
            .expect("launches");
        assert_eq!(
            session.endpoint(),
            "https://mvm-abc123.microvm.us-east-1.amazonaws.com"
        );

        assert_eq!(sandbox.lifecycle(), Lifecycle::Running);
        assert!(sandbox.image_exists(), "STATE-1: the image is recorded");
        assert!(sandbox.token_installed(), "STATE-2: the token is installed");
        assert_eq!(sandbox.bootstrap_count(), 1);
        assert_eq!(sandbox.microvm().expect("launched").id, "mvm-abc123");

        // The payload really carried the token, read off the wire member rather than from
        // the request type — a field on a struct proves nothing about what was emitted.
        let body = recorder.first_body("RunMicrovm");
        let payload = body["runHookPayload"].as_str().expect("a string");
        assert!(payload.starts_with(r#"{"agent_token":"#), "{payload}");
        assert_eq!(body["idlePolicy"]["suspendedDurationSeconds"], 600);
    }

    /// A launch env reaches the wire member the daemon parses, and it is the only thing
    /// that changes about the request.
    ///
    /// Read off `runHookPayload` rather than off the request struct: a field on a struct
    /// proves nothing about what was emitted, which is the same argument the test above
    /// makes about the token.
    #[tokio::test]
    async fn a_launch_env_reaches_the_run_hook_payload_on_the_wire() {
        let (mut sandbox, recorder, _) = planted();
        answer_launch(&recorder);
        sandbox
            .run(
                RunRequest::new()
                    .with_image("arn:image")
                    .with_launch_env("ANTHROPIC_BASE_URL", "https://gateway.example")
                    .with_launch_env("PATH", "/usr/local/bin:/usr/bin:/bin"),
            )
            .await
            .expect("launches");

        let body = recorder.first_body("RunMicrovm");
        let payload = body["runHookPayload"].as_str().expect("a string");
        // One parse deeper, because that is where the daemon reads it from too.
        let inner: serde_json::Value =
            serde_json::from_str(payload).expect("the payload is itself JSON");
        assert!(
            inner["agent_token"].as_str().is_some_and(|t| !t.is_empty()),
            "the token still rides alongside: {payload}"
        );
        assert_eq!(
            inner["env"]["ANTHROPIC_BASE_URL"],
            "https://gateway.example"
        );
        assert_eq!(inner["env"]["PATH"], "/usr/local/bin:/usr/bin:/bin");
    }

    /// **The advisory deny reaches the guest's environment, and it changes nothing else
    /// about the request.**
    ///
    /// Read off `runHookPayload` for the reason the launch-env test gives, and the connector
    /// member is asserted absent in the same body: `deny_egress` must not quietly ask the
    /// platform for anything, because the whole claim is that the platform has nothing to
    /// ask for.
    ///
    /// **Falsification** — 2026-09-13. Make `run` build the payload from
    /// `request.launch_env` instead of `request.effective_launch_env()` and the two
    /// `https_proxy` assertions go red while every other launch test stays green; restored
    /// after.
    #[tokio::test]
    async fn the_advisory_deny_reaches_the_launch_environment_in_both_spellings() {
        let (mut sandbox, recorder, _) = planted();
        answer_launch(&recorder);
        let request = RunRequest::new().with_image("arn:image").with_deny_egress();
        assert_eq!(
            request.egress_posture(),
            crate::control::EgressPosture::BestEffort
        );
        sandbox.run(request).await.expect("launches");

        let body = recorder.first_body("RunMicrovm");
        let inner: serde_json::Value =
            serde_json::from_str(body["runHookPayload"].as_str().expect("a string"))
                .expect("the payload is itself JSON");
        for key in crate::sandbox::DENY_EGRESS_ENV_KEYS {
            assert_eq!(
                inner["env"][key], DENY_EGRESS_PROXY_URL,
                "{key} must point at the black hole: {inner}"
            );
        }
        assert!(
            body.get("egressNetworkConnectors").is_none(),
            "the advisory deny asks the platform for nothing: {body}"
        );
    }

    /// A caller's own proxy value survives the deny, and a launch that never asked for the
    /// deny carries none of the keys.
    #[test]
    fn the_deny_never_overwrites_a_callers_own_proxy_and_is_absent_by_default() {
        let kept = RunRequest::new()
            .with_deny_egress()
            .with_launch_env("https_proxy", "http://proxy.internal:3128")
            .effective_launch_env();
        assert_eq!(kept["https_proxy"], "http://proxy.internal:3128");
        assert_eq!(kept["http_proxy"], DENY_EGRESS_PROXY_URL);

        let plain = RunRequest::new().effective_launch_env();
        assert!(
            plain.is_empty(),
            "a launch that did not ask for the deny sends byte-for-byte what it always sent: \
             {plain:?}"
        );
        assert_eq!(
            RunRequest::new().egress_posture(),
            crate::control::EgressPosture::Unsealed,
            "and its posture is the measured one, not a seal"
        );
    }

    /// **`egress` and `deny_egress` together are refused locally, with zero calls.**
    ///
    /// The same shape as the over-budget refusal below: a launch carrying both would report
    /// `open` while the workload's own tools failed closed, and AWS has no opinion to
    /// return about it.
    ///
    /// **Falsification** — 2026-09-13. Delete the refusal in `run` and this test fails on
    /// `expect_err`; the launch then succeeds and reports posture `open`. Restored after.
    #[tokio::test]
    async fn asking_for_egress_and_the_deny_together_is_refused_before_any_call() {
        let (mut sandbox, recorder, _) = planted();
        answer_launch(&recorder);
        let mut request = RunRequest::new().with_image("arn:image").with_egress();
        request.deny_egress = true;

        let error = sandbox
            .run(request)
            .await
            .expect_err("opposite intents are not resolved by a precedence rule");
        assert_eq!(error.kind(), ErrorKind::InvalidArg);
        assert!(error.to_string().contains("opposite things"), "{error}");
        assert_eq!(
            recorder.calls().len(),
            0,
            "a locally-refused launch costs nothing"
        );
    }

    /// **A pinned `imageVersion` reaches the wire through the sandbox**, and an unpinned
    /// launch omits the member entirely.
    ///
    /// This is the layer a caller actually uses, so the forwarding is what needs asserting:
    /// [`RunRequest::image_version`] is a field on a struct and proves nothing about the
    /// emitted body until it is read off the wire member.
    ///
    /// **Falsification** — run 2026-08-16. Drop `wire.image_version = ...` from `run` and the
    /// pinned assertion goes red with the member absent, while every other launch test still
    /// passes — which is why this one is here.
    #[tokio::test]
    async fn a_pinned_image_version_is_forwarded_to_the_launch_request() {
        let (mut sandbox, recorder, _) = planted();
        answer_launch(&recorder);
        sandbox
            .run(
                RunRequest::new()
                    .with_image("arn:image")
                    .with_image_version("2.0"),
            )
            .await
            .expect("launches");
        assert_eq!(
            recorder.first_body("RunMicrovm")["imageVersion"],
            "2.0",
            "a canary has to launch against the version it means to test"
        );

        let (mut sandbox, recorder, _) = planted();
        answer_launch(&recorder);
        sandbox
            .run(RunRequest::new().with_image("arn:image"))
            .await
            .expect("launches");
        assert!(
            recorder
                .first_body("RunMicrovm")
                .get("imageVersion")
                .is_none(),
            "an unpinned launch sends what this client always sent"
        );
    }

    /// **The local refusal, with zero control-plane calls.**
    ///
    /// The observable difference between this client and one that lets AWS answer, and the
    /// same shape as STATE-5's suspend refusal: an over-budget launch env fails before the
    /// launch rather than as a `ValidationException` on a member the caller did not know
    /// they were filling. botocore does not enforce the ceiling client-side, so without
    /// this there is no local signal at all.
    ///
    /// **Falsification** — build the payload after `run_microvm` instead of before, and the
    /// call count assertion goes red.
    #[tokio::test]
    async fn an_over_budget_launch_env_is_refused_before_any_control_plane_call() {
        let (mut sandbox, recorder, _) = planted();
        answer_launch(&recorder);

        let error = sandbox
            .run(
                RunRequest::new()
                    .with_image("arn:image")
                    .with_launch_env("AWS_SESSION_TOKEN", "t".repeat(4096)),
            )
            .await
            .expect_err("a credential-scale launch env does not fit the payload");

        assert_eq!(error.kind(), ErrorKind::InvalidArg);
        let message = error.to_string();
        assert!(message.contains("ceiling of 4096"), "{message}");
        assert!(message.contains("launch env contributed"), "{message}");
        assert_eq!(
            recorder.calls().len(),
            0,
            "the refusal has to be local: a launch that reached AWS costs a VM and a bill"
        );
        // And the sandbox is left usable rather than half-launched, so a caller can trim
        // the env and retry through the same handle.
        assert_eq!(sandbox.lifecycle(), Lifecycle::Pending);
        assert_eq!(sandbox.bootstrap_count(), 0);
        assert!(sandbox.microvm().is_none());
    }

    /// **Issue #24's guards reach through `Sandbox`, which is the layer both bindings use.**
    ///
    /// `microvms-py`'s `run` and `microvms-js`'s `run` both build a [`RunRequest`] and hand it to
    /// [`Sandbox::run`], which fills a `RunMicrovmRequest` and calls
    /// [`crate::control::ControlPlane::run_microvm`]. So the guards there cover the bindings — but
    /// "cover" is a claim about a call chain, and a chain is what a refactor breaks. This asserts
    /// it at the layer the bindings actually enter, so a `Sandbox::run` that stopped delegating
    /// (or a binding-side default that bypassed the request type) fails a test rather than shipping
    /// an unguarded surface to Python and Node.
    ///
    /// `max_idle_sec` is the case that matters most here, because it is a plain `u32` keyword
    /// argument on both bindings with no type narrowing it: `sandbox.run(max_idle_sec=59)` is what
    /// a Python caller writes, and before this it reached the wire.
    ///
    /// **Guard proof.** Delete `require_idle_duration` from `run_microvm` and the idle row goes red
    /// on the call count; delete `require_valid_role_arn` and the role row does. Neither guard is in
    /// this file, which is the point — the test is about the chain.
    #[tokio::test]
    async fn the_bindings_layer_refuses_an_illegal_launch_with_zero_calls() {
        let cases: [(&str, RunRequest, &str); 3] = [
            (
                "max_idle_sec=59, which is what a Python keyword argument passes",
                RunRequest {
                    max_idle_sec: 59,
                    ..RunRequest::new().with_image("arn:image")
                },
                "maxIdleDurationSeconds",
            ),
            (
                "an execution role that is a bare name",
                RunRequest {
                    execution_role_arn: Some("execution-role".to_string()),
                    ..RunRequest::new().with_image("arn:image")
                },
                "role *name*",
            ),
            (
                "a pinned version with a trailing newline",
                RunRequest::new()
                    .with_image("arn:image")
                    .with_image_version("2.0\n"),
                "contains whitespace",
            ),
        ];

        for (label, request, expected) in cases {
            let (mut sandbox, recorder, _) = planted();
            // The launch answered, so a missing guard is a *successful* launch — a real VM and a
            // real bill — rather than a different failure.
            answer_launch(&recorder);

            // `expect_err` needs the Ok type to be `Debug`, and `run` answers `&mut Session`
            // (which is not) — so the discriminant is matched instead. That is also the more
            // honest assertion: it says the launch produced no session at all.
            let Err(error) = sandbox.run(request).await else {
                panic!("{label} must be refused");
            };
            assert_eq!(error.kind(), ErrorKind::InvalidArg, "{label}");
            assert!(
                error.to_string().contains(expected),
                "{label}: wanted {expected:?}, got {error}"
            );
            assert_eq!(
                recorder.calls().len(),
                0,
                "{label}: the refusal has to be local, because a launch that reached AWS costs a \
                 VM and a bill"
            );
            assert_eq!(sandbox.lifecycle(), Lifecycle::Pending);
            assert!(sandbox.microvm().is_none());
        }
    }

    /// A launch whose VM dies during startup leaves the token **not** installed, because
    /// the run hook is what delivers it and a dead VM ran no hook.
    ///
    /// The distinction this pins is the one the test above cannot: both a correct client and
    /// one that installs the token at the launch call reach RUNNING with the token set, and
    /// only this case separates them.
    #[tokio::test]
    async fn a_launch_that_dies_during_startup_installs_no_token() {
        let (mut sandbox, recorder, _) = planted();
        recorder
            .answer(
                "RunMicrovm",
                Answer::ok(fake::microvm_response("PENDING", None)),
            )
            .answer(
                "GetMicrovm",
                Answer::ok(fake::microvm_response(
                    "TERMINATED",
                    Some("run hook returned 500"),
                )),
            );

        let error = sandbox
            .run(RunRequest::new().with_image("arn:image"))
            .await
            .expect_err("a VM that died during startup is not a launch");
        assert_eq!(error.kind(), ErrorKind::LaunchDied);
        assert!(
            error.to_string().contains("run hook returned 500"),
            "{error}"
        );

        assert!(
            !sandbox.token_installed(),
            "the run hook never answered, so nothing was installed"
        );
        assert_eq!(
            sandbox.bootstrap_count(),
            0,
            "a bootstrap counted here would make STATE-3's ceiling unreachable for a retry"
        );
        assert_eq!(sandbox.lifecycle(), Lifecycle::Pending);
        assert!(
            sandbox.image_exists(),
            "STATE-1 still holds: the launch was accepted against a real image"
        );
    }

    /// **STATE-3, the guard proof.** A second `run` on one sandbox is refused, and it is
    /// refused with **zero** further control-plane calls.
    ///
    /// The call count is the assertion that matters. A client that let the second launch
    /// through would create a second VM and silently address two guests through one handle,
    /// and it would also deliver a second run-hook payload to a daemon whose one-shot
    /// bootstrap answers 409 — so "an error came back" is not enough to tell the two apart.
    ///
    /// **Falsification** — delete the `bootstrap_count > 0` branch from `run` and this test
    /// is red on both the count and the call total. Verified; see the packet's guard proofs.
    #[tokio::test]
    async fn a_second_run_on_one_sandbox_is_refused_before_any_call() {
        let (mut sandbox, recorder, _) = launched().await;
        assert_eq!(sandbox.bootstrap_count(), 1);
        let before = recorder.calls().len();

        let error = sandbox
            .run(RunRequest::new().with_image("arn:image"))
            .await
            .expect_err("the token is installed at most once per VM lifetime");
        assert_eq!(error.kind(), ErrorKind::InvalidArg);
        assert!(error.to_string().contains("STATE-3"), "{error}");
        assert!(
            error.to_string().contains("needs a second Sandbox"),
            "the remedy has to be nameable: {error}"
        );

        assert_eq!(
            sandbox.bootstrap_count(),
            1,
            "bootstrap_count <= 1 is the Z3-proved invariant"
        );
        assert_eq!(
            recorder.calls().len(),
            before,
            "the refusal must cost no control-plane call"
        );
        assert_eq!(recorder.call_count("RunMicrovm"), 1);
    }

    /// **STATE-4 and STATE-6.** A suspend from RUNNING moves to SUSPENDING before the wait,
    /// and the platform reporting SUSPENDED is what moves it to SUSPENDED.
    #[tokio::test]
    async fn a_suspend_from_running_passes_through_suspending_to_suspended() {
        let (sandbox, recorder, _) = suspended().await;
        assert_eq!(sandbox.lifecycle(), Lifecycle::Suspended);
        assert!(
            sandbox.token_installed(),
            "a freeze keeps the guest's memory, so the token survives"
        );
        assert_eq!(sandbox.bootstrap_count(), 1, "no re-bootstrap on a freeze");

        let calls = recorder.calls();
        let suspend = calls
            .iter()
            .find(|call| call.operation == "SuspendMicrovm")
            .expect("the suspend went out");
        assert_eq!(suspend.method, Method::Post);
        assert_eq!(suspend.path, "/2025-09-09/microvms/mvm-abc123/suspend");
    }

    /// A suspend whose VM dies while suspending reports TERMINATED rather than raising, and
    /// records it — which is what stops a later resume.
    ///
    /// TERMINATED is *wanted* by the suspend wait for a reason: a VM that dies mid-suspend
    /// is a state to report, not an exception out of the middle of a teardown.
    #[tokio::test]
    async fn a_vm_that_dies_while_suspending_is_recorded_rather_than_raised() {
        let (mut sandbox, recorder, _) = launched().await;
        recorder
            .answer("SuspendMicrovm", Answer::ok(fake::empty_response()))
            .answer(
                "GetMicrovm",
                Answer::ok(fake::microvm_response("TERMINATED", Some("idle policy"))),
            );

        sandbox
            .suspend()
            .await
            .expect("a death mid-suspend is a state, not an error");
        assert_eq!(sandbox.lifecycle(), Lifecycle::Terminated);
        assert!(
            sandbox.was_terminated(),
            "STATE-11's precondition is recorded"
        );
    }

    /// A suspend whose wire call fails leaves the sandbox in RUNNING, and a retry works.
    ///
    /// STATE-4's "accepted" is the wire call succeeding. A client that moved to SUSPENDING
    /// before the call would strand a failed call there — a state neither suspend nor
    /// resume accepts, so one throttled or dropped request would brick the handle.
    /// **Falsification** — move `self.lifecycle = Lifecycle::Suspending` back above the
    /// `control.suspend` call and the first assertion here reads SUSPENDING, and the retry
    /// is refused by the STATE-5 guard.
    #[tokio::test]
    async fn a_suspend_whose_call_fails_stays_running_and_can_be_retried() {
        let (mut sandbox, recorder, _) = planted();
        recorder
            .answer(
                "RunMicrovm",
                Answer::ok(fake::microvm_response("PENDING", None)),
            )
            .answer(
                "GetMicrovm",
                Answer::ok(fake::microvm_response("RUNNING", None)),
            )
            .answer(
                "GetMicrovm",
                Answer::ok(fake::microvm_response("SUSPENDED", None)),
            )
            .answer(
                "CreateMicrovmAuthToken",
                Answer::ok(fake::auth_token_response("proxy-token")),
            )
            // A 409 rather than a 429 or a transport cut, because those two are retried
            // inside `send_with_retry` and this test wants the call to fail once, fast.
            .answer("SuspendMicrovm", Answer::failure(409, "ConflictException"))
            .answer("SuspendMicrovm", Answer::ok(fake::empty_response()));

        sandbox
            .run(RunRequest::new().with_image("arn:image"))
            .await
            .expect("launches");

        sandbox
            .suspend()
            .await
            .expect_err("the control plane refused the suspend");
        assert_eq!(
            sandbox.lifecycle(),
            Lifecycle::Running,
            "a refused suspend must not move the lifecycle: SUSPENDING accepts neither a \
             suspend nor a resume, so recording it here would brick the handle"
        );

        sandbox.suspend().await.expect("the retry suspends");
        assert_eq!(sandbox.lifecycle(), Lifecycle::Suspended);
        assert_eq!(recorder.call_count("SuspendMicrovm"), 2);
    }

    /// **STATE-5, the guard proof.** A suspend from anything but RUNNING is refused with
    /// **zero** control-plane calls.
    ///
    /// Every non-RUNNING state, so a state added to `Lifecycle` later is not silently
    /// exempt. The count is the load-bearing assertion: letting the call go and reading the
    /// service's answer also produces an error, and that error is about a non-running id
    /// rather than about which of two things the caller got wrong.
    ///
    /// **Falsification** — delete the `lifecycle != Running` branch from `suspend` and the
    /// SUSPENDED case emits a `SuspendMicrovm` call, turning the count assertion red.
    /// Verified; see the packet's guard proofs.
    #[tokio::test]
    async fn a_suspend_from_a_non_running_state_reaches_no_control_plane_call() {
        // From SUSPENDED — the caller who believes they resumed.
        let (mut sandbox, recorder, _) = suspended().await;
        let before = recorder.call_count("SuspendMicrovm");

        let error = sandbox
            .suspend()
            .await
            .expect_err("a suspend is only issued from RUNNING");
        assert_eq!(error.kind(), ErrorKind::InvalidArg);
        assert!(error.to_string().contains("STATE-5"), "{error}");
        assert!(error.to_string().contains("SUSPENDED"), "{error}");
        assert_eq!(
            recorder.call_count("SuspendMicrovm"),
            before,
            "the refusal must be local: no second suspend went to the wire"
        );

        // And from PENDING, before anything launched at all.
        let (mut fresh, fresh_recorder, _) = planted();
        let error = fresh.suspend().await.expect_err("nothing to suspend");
        assert_eq!(error.kind(), ErrorKind::Precondition);
        assert_eq!(fresh_recorder.calls().len(), 0);
    }

    /// **STATE-7 and STATE-8.** A resume from SUSPENDED returns to RUNNING, re-delivers
    /// **nothing**, and drops the cached proxy token.
    ///
    /// Three assertions, and each one is a different failure mode. The bootstrap count says
    /// no run-hook payload was re-delivered — which a daemon's one-shot bootstrap would
    /// answer 409 to, reading like a broken VM. The `RunMicrovm` count says no second launch
    /// happened. And `is_cached` says the token was invalidated, which is the only
    /// observable difference on a path where the endpoint URL does not change: a client that
    /// kept the token would produce identical requests until the pre-suspend token stopped
    /// validating, and that rejection reads exactly like a dead daemon.
    #[tokio::test]
    async fn a_resume_reuses_the_token_redelivers_nothing_and_drops_the_proxy_token() {
        let (mut sandbox, recorder, _) = suspended().await;

        // The session mints once before the suspend, so there is a cached token to drop.
        let auth = Arc::clone(
            sandbox
                .session()
                .expect("launched")
                .proxy_auth()
                .expect("the launch wired a minter"),
        );
        auth.headers().await.expect("mints");
        assert!(
            auth.is_cached(),
            "there must be a token for the drop to matter"
        );
        assert_eq!(auth.mint_count(), 1);

        recorder
            .answer("ResumeMicrovm", Answer::ok(fake::empty_response()))
            .answer(
                "GetMicrovm",
                Answer::ok(fake::microvm_response("RUNNING", None)),
            );
        let session = sandbox.resume().await.expect("resumes");
        assert_eq!(
            session.endpoint(),
            "https://mvm-abc123.microvm.us-east-1.amazonaws.com"
        );

        assert_eq!(sandbox.lifecycle(), Lifecycle::Running);
        assert!(
            sandbox.token_installed(),
            "STATE-7: the installed token is reused"
        );
        assert_eq!(
            sandbox.bootstrap_count(),
            1,
            "STATE-7: a resume re-delivers no run-hook payload"
        );
        assert_eq!(
            recorder.call_count("RunMicrovm"),
            1,
            "a resume is not a second launch"
        );
        assert!(
            !auth.is_cached(),
            "STATE-8: the cached proxy token survived the resume"
        );
    }

    /// **STATE-8's guard proof, on the mint count.** The request after a resume mints a
    /// fresh token rather than reusing the pre-suspend one.
    ///
    /// Separate from the test above because `is_cached` is a fact about the cache and this is
    /// a fact about the wire: a client that dropped the cache but kept emitting the old
    /// header would pass the first assertion and fail every request.
    ///
    /// **Falsification** — delete the `proxy.invalidate()` line from `Session::rebind` and
    /// the mint count stays at 1 here. Verified; see the packet's guard proofs.
    #[tokio::test]
    async fn the_request_after_a_resume_mints_a_fresh_proxy_token() {
        let (mut sandbox, recorder, _) = suspended().await;
        let auth = Arc::clone(
            sandbox
                .session()
                .expect("launched")
                .proxy_auth()
                .expect("wired"),
        );
        auth.headers().await.expect("mints");
        assert_eq!(auth.mint_count(), 1);

        recorder
            .answer("ResumeMicrovm", Answer::ok(fake::empty_response()))
            .answer(
                "GetMicrovm",
                Answer::ok(fake::microvm_response("RUNNING", None)),
            );
        sandbox.resume().await.expect("resumes");

        // No clock movement at all, so a re-mint here is caused by the invalidation rather
        // than by the refresh window rolling over.
        auth.headers().await.expect("re-mints");
        assert_eq!(
            auth.mint_count(),
            2,
            "STATE-8: the resume did not force a fresh mint"
        );
    }

    /// **STATE-12, the guard proof.** A resume past the launch-time suspended window is
    /// refused locally, with **zero** resume calls, and the message names the window.
    ///
    /// Four assertions, and the packet asks for all four because each rules out a different
    /// wrong implementation. `ErrorKind::WindowClosed` rather than `Timeout` separates this
    /// from the client that waits. Zero `ResumeMicrovm` calls is what "before any wire call"
    /// means. The elapsed reading rules out an implementation that polled to the deadline
    /// first — the fake's clock advances on every `sleep`, so a poll loop here would show up
    /// as time passed. And the message naming both numbers plus `idlePolicy` is what sends a
    /// reader to the finding rather than looking for the flag that reopens the window.
    ///
    /// **Falsification** — delete the `require_open_suspended_window` call from `resume` and
    /// the fake answers TERMINATED, so this test fails with an `ErrorKind::LaunchDied` after
    /// a `ResumeMicrovm` call, naming neither the window nor the seconds elapsed. Verified;
    /// see the packet's guard proofs.
    /// A keepalive reads this receiver instead of the lock, so every transition must reach
    /// it: a suspend it missed would be undone by the keepalive's next poll auto-resuming
    /// the VM.
    #[tokio::test]
    async fn the_lifecycle_watch_follows_every_transition() {
        let (mut sandbox, recorder, _clock) = planted();
        let watch = sandbox.watch_lifecycle();
        assert_eq!(*watch.borrow(), Lifecycle::Pending);
        recorder
            .answer(
                "RunMicrovm",
                Answer::ok(fake::microvm_response("PENDING", None)),
            )
            .answer(
                "GetMicrovm",
                Answer::ok(fake::microvm_response("RUNNING", None)),
            )
            .answer(
                "GetMicrovm",
                Answer::ok(fake::microvm_response("SUSPENDED", None)),
            )
            .answer(
                "CreateMicrovmAuthToken",
                Answer::ok(fake::auth_token_response("proxy-token")),
            )
            .answer("SuspendMicrovm", Answer::ok(fake::empty_response()));
        sandbox
            .run(RunRequest::new().with_image("arn:image"))
            .await
            .expect("launches");
        assert_eq!(*watch.borrow(), Lifecycle::Running);
        sandbox.suspend().await.expect("suspends");
        assert_eq!(*watch.borrow(), Lifecycle::Suspended);
        assert_eq!(*watch.borrow(), sandbox.lifecycle());
    }

    #[tokio::test]
    async fn a_resume_past_the_suspended_window_is_refused_before_the_wire() {
        // A short window, so the arithmetic is legible: sixty seconds asked for at launch.
        let (mut sandbox, recorder, clock) = suspended_with_window(60).await;
        assert_eq!(sandbox.suspended_window(), Some(Duration::from_secs(60)));
        assert_eq!(
            sandbox.idle_window(),
            Some(Duration::from_secs(600)),
            "the launch's own maxIdleDurationSeconds, which a keepalive checks its cadence against"
        );

        // The window closes. This is the whole of what the clock injection buys.
        clock.advance(Duration::from_secs(61));

        // What a service says about a VM the idlePolicy already terminated — which is what a
        // client without the local check would spend its poll timeout discovering.
        recorder
            .answer("ResumeMicrovm", Answer::ok(fake::empty_response()))
            .answer(
                "GetMicrovm",
                Answer::ok(fake::microvm_response(
                    "TERMINATED",
                    Some("suspended window elapsed"),
                )),
            );
        let elapsed_before = clock.now();
        let resumes_before = recorder.call_count("ResumeMicrovm");

        let error = sandbox
            .resume()
            .await
            .expect_err("the window the launch set has closed");

        assert_eq!(error.kind(), ErrorKind::WindowClosed);
        assert_eq!(error.code(), "ERR_WINDOW_CLOSED");
        let message = error.to_string();
        assert!(message.contains("61s"), "the elapsed time: {message}");
        assert!(
            message.contains("60s suspendedDurationSeconds"),
            "the window has to be named: {message}"
        );
        assert!(message.contains("idlePolicy"), "the finding: {message}");
        assert!(
            message.contains("Refused before ResumeMicrovm"),
            "why the refusal is local: {message}"
        );
        assert_eq!(
            recorder.call_count("ResumeMicrovm"),
            resumes_before,
            "the refusal must come before ResumeMicrovm"
        );
        assert_eq!(
            clock.now(),
            elapsed_before,
            "the refusal must be immediate rather than polled to the deadline"
        );
    }

    /// **STATE-12's fallback.** With no window from its own request, the sandbox uses the
    /// `suspendedDurationSeconds` that `GetMicrovm` reported (600 in the fake).
    #[tokio::test]
    async fn the_window_falls_back_to_the_idle_policy_the_service_reported() {
        let (mut sandbox, recorder, clock) = suspended_with_window(60).await;
        sandbox.suspended_window = None;
        recorder.answer("ResumeMicrovm", Answer::ok(fake::empty_response()));
        clock.advance(Duration::from_secs(601));
        let error = sandbox
            .resume()
            .await
            .expect_err("past the reported window");
        assert_eq!(error.kind(), ErrorKind::WindowClosed);
        assert!(error.to_string().contains("600s"), "{error}");
        assert_eq!(recorder.call_count("ResumeMicrovm"), 0);
    }

    /// **#195 at the sandbox.** A client-token launch that adopts an idle-suspended VM is
    /// resumed and reaches RUNNING with the token counted once. The same poll on a launch
    /// without a caller token is still a startup death (TRAP-8).
    #[tokio::test]
    async fn a_client_token_launch_resumes_the_vm_it_adopted() {
        let (mut sandbox, recorder, _) = planted();
        recorder
            .answer(
                "RunMicrovm",
                Answer::ok(fake::microvm_response("SUSPENDED", None)),
            )
            .answer(
                "GetMicrovm",
                Answer::ok(fake::microvm_response("SUSPENDED", None)),
            )
            .answer(
                "GetMicrovm",
                Answer::ok(fake::microvm_response("RUNNING", None)),
            )
            .answer("ResumeMicrovm", Answer::ok(fake::empty_response()))
            .answer(
                "CreateMicrovmAuthToken",
                Answer::ok(fake::auth_token_response("proxy-token")),
            );
        let mut request = RunRequest::new().with_image("arn:image");
        request.client_token = Some("job-195".into());
        request.agent_token = Some("agent-token".into());
        sandbox
            .run(request)
            .await
            .expect("the adopted VM is resumed");
        assert_eq!(sandbox.lifecycle(), Lifecycle::Running);
        assert_eq!(sandbox.bootstrap_count(), 1);
        assert_eq!(recorder.call_count("ResumeMicrovm"), 1);

        let (mut fresh, recorder, _) = planted();
        recorder
            .answer(
                "RunMicrovm",
                Answer::ok(fake::microvm_response("PENDING", None)),
            )
            .answer(
                "GetMicrovm",
                Answer::ok(fake::microvm_response("SUSPENDED", Some("hook failed"))),
            );
        let error = fresh
            .run(RunRequest::new().with_image("arn:image"))
            .await
            .expect_err("a fresh launch that reads SUSPENDED died during startup");
        assert_eq!(error.kind(), ErrorKind::LaunchDied);
        assert_eq!(recorder.call_count("ResumeMicrovm"), 0);
    }

    /// **`wait: false`.** The launch returns once accepted, with an addressable session and
    /// the lifecycle still PENDING; `wait_until_running` finishes it, exactly once.
    #[tokio::test]
    async fn a_launch_that_does_not_wait_is_finished_by_wait_until_running() {
        let (mut sandbox, recorder, _) = planted();
        answer_launch(&recorder);
        let mut request = RunRequest::new().with_image("arn:image");
        request.wait = false;
        let endpoint = sandbox
            .run(request)
            .await
            .expect("accepted")
            .endpoint()
            .to_string();
        assert!(endpoint.starts_with("https://"), "{endpoint}");
        assert_eq!(sandbox.lifecycle(), Lifecycle::Pending);
        assert_eq!(sandbox.bootstrap_count(), 0);
        assert_eq!(recorder.call_count("GetMicrovm"), 0, "nothing polled yet");

        sandbox
            .wait_until_running(DEFAULT_READY_TIMEOUT)
            .await
            .expect("reaches RUNNING");
        assert_eq!(sandbox.lifecycle(), Lifecycle::Running);
        assert_eq!(sandbox.bootstrap_count(), 1);
        let error = sandbox
            .wait_until_running(DEFAULT_READY_TIMEOUT)
            .await
            .expect_err("a running launch has nothing to wait for");
        assert_eq!(error.kind(), ErrorKind::InvalidArg);
        assert_eq!(sandbox.bootstrap_count(), 1, "counted once (STATE-3)");
    }

    /// A resume **inside** the window goes through, so the guard is a comparison rather than
    /// a blanket refusal of every resume.
    ///
    /// The boundary is inclusive: elapsed exactly equal to the window is still open, matching
    /// the Python's `elapsed <= window`.
    #[tokio::test]
    async fn a_resume_inside_the_window_is_allowed_and_the_boundary_is_inclusive() {
        let (mut sandbox, recorder, clock) = suspended_with_window(60).await;

        // Exactly at the window.
        clock.advance(Duration::from_secs(60));
        recorder
            .answer("ResumeMicrovm", Answer::ok(fake::empty_response()))
            .answer(
                "GetMicrovm",
                Answer::ok(fake::microvm_response("RUNNING", None)),
            );
        sandbox
            .resume()
            .await
            .expect("60s == the 60s window is open");
        assert_eq!(sandbox.lifecycle(), Lifecycle::Running);
        assert_eq!(recorder.call_count("ResumeMicrovm"), 1);
    }

    /// A successful resume clears the stamp, so the next cycle's window is measured from the
    /// next suspend rather than from the first one.
    ///
    /// The failure this rules out is subtle and would only show on a second cycle:
    /// accumulating every suspension's elapsed time into one total refuses a resume whose own
    /// window is wide open. The clock is advanced past the window *between* the two cycles,
    /// which is what makes the accumulating implementation fail here.
    #[tokio::test]
    async fn a_successful_resume_clears_the_stamp_so_a_second_cycle_measures_its_own_window() {
        let (mut sandbox, recorder, clock) = planted();
        // Every `GetMicrovm` answer for both cycles, in the order the waits consume them:
        // the launch's RUNNING, then SUSPENDED / RUNNING twice. Queued up front for the
        // reason `suspended_with_window` documents — a stale answer left at the head makes a
        // wait poll twice, and the fake's `sleep` moves the very clock this test is measuring.
        recorder
            .answer(
                "RunMicrovm",
                Answer::ok(fake::microvm_response("PENDING", None)),
            )
            .answer(
                "CreateMicrovmAuthToken",
                Answer::ok(fake::auth_token_response("proxy-token")),
            )
            .answer("SuspendMicrovm", Answer::ok(fake::empty_response()))
            .answer("ResumeMicrovm", Answer::ok(fake::empty_response()))
            .answer(
                "GetMicrovm",
                Answer::ok(fake::microvm_response("RUNNING", None)),
            )
            .answer(
                "GetMicrovm",
                Answer::ok(fake::microvm_response("SUSPENDED", None)),
            )
            .answer(
                "GetMicrovm",
                Answer::ok(fake::microvm_response("RUNNING", None)),
            )
            .answer(
                "GetMicrovm",
                Answer::ok(fake::microvm_response("SUSPENDED", None)),
            )
            .answer(
                "GetMicrovm",
                Answer::ok(fake::microvm_response("RUNNING", None)),
            );
        sandbox
            .run(
                RunRequest::new()
                    .with_image("arn:image")
                    .with_suspended_sec(60),
            )
            .await
            .expect("launches");

        for cycle in 0..2 {
            sandbox
                .suspend()
                .await
                .unwrap_or_else(|error| panic!("cycle {cycle} suspend: {error}"));
            assert_eq!(sandbox.lifecycle(), Lifecycle::Suspended, "cycle {cycle}");

            // Forty-five seconds each time — inside the sixty-second window on its own, and
            // ninety in total. An implementation that never cleared the stamp measures the
            // total and refuses the second resume, which is the whole point of the loop.
            clock.advance(Duration::from_secs(45));

            sandbox.resume().await.unwrap_or_else(|error| {
                panic!("cycle {cycle} resume, whose own window is wide open: {error}")
            });
            assert_eq!(sandbox.lifecycle(), Lifecycle::Running, "cycle {cycle}");
            assert_eq!(
                sandbox.bootstrap_count(),
                1,
                "cycle {cycle}: no re-bootstrap"
            );
        }

        assert_eq!(
            clock.now(),
            Duration::from_secs(90),
            "ninety seconds of accumulated suspension against a sixty-second window: this is \
             the total an implementation that never cleared the stamp would have compared"
        );
        assert_eq!(
            recorder.call_count("ResumeMicrovm"),
            2,
            "both cycles resumed"
        );
    }

    /// **STATE-11, the resume-after-terminate case the packet names.** A terminated VM never
    /// returns to RUNNING, and the recorder sees **zero** resume wire calls.
    ///
    /// The zero count is the assertion the packet asks for, and it is the right one: a client
    /// that called and read the answer would also fail, with whatever the service says about
    /// a terminated id — which is a different statement from "this VM is gone and even a
    /// successful call would hand you a different machine".
    #[tokio::test]
    async fn a_resume_after_terminate_records_zero_resume_calls() {
        let (mut sandbox, recorder, _) = launched().await;
        recorder.answer("TerminateMicrovm", Answer::ok(fake::empty_response()));
        let report = sandbox.terminate(TeardownOpts::default()).await;
        assert!(report.terminate_accepted);
        assert!(sandbox.was_terminated());

        let error = sandbox
            .resume()
            .await
            .expect_err("a terminated VM never returns to RUNNING");
        assert_eq!(error.kind(), ErrorKind::InvalidArg);
        assert!(error.to_string().contains("STATE-11"), "{error}");
        assert_eq!(
            recorder.call_count("ResumeMicrovm"),
            0,
            "no resume may reach the wire once the VM was terminated"
        );
        assert_ne!(
            sandbox.lifecycle(),
            Lifecycle::Running,
            "the lifecycle must not return to RUNNING"
        );
    }

    /// **STATE-9 and STATE-10.** A terminate accepted is TERMINATING with the VM recorded
    /// terminated; the platform reporting termination complete is what marks TERMINATED.
    ///
    /// The two are separate states rather than one because the default teardown does not
    /// wait — so TERMINATING is a state a report really ends in, and calling it TERMINATED
    /// would claim an observation nobody made.
    #[tokio::test]
    async fn a_terminate_records_terminating_and_the_completion_report_marks_terminated() {
        let (mut sandbox, recorder, _) = launched().await;
        recorder.answer("TerminateMicrovm", Answer::ok(fake::empty_response()));

        let report = sandbox.terminate(TeardownOpts::default()).await;
        assert!(report.terminate_accepted, "STATE-9: the call was accepted");
        assert!(sandbox.was_terminated(), "STATE-9: recorded terminated");
        assert_eq!(
            report.lifecycle,
            Some(Lifecycle::Terminating),
            "the default teardown does not wait, so TERMINATED is not claimed"
        );
        assert!(!report.leaked(), "{report:?}");

        // And with the wait asked for, STATE-10 is observed.
        let (mut sandbox, recorder, _) = launched().await;
        recorder
            .answer("TerminateMicrovm", Answer::ok(fake::empty_response()))
            .answer(
                "GetMicrovm",
                Answer::ok(fake::microvm_response("TERMINATED", None)),
            );
        let report = sandbox
            .terminate(TeardownOpts::default().waiting_for_terminated())
            .await;
        assert_eq!(report.lifecycle, Some(Lifecycle::Terminated));
        assert_eq!(sandbox.lifecycle(), Lifecycle::Terminated);
    }

    /// The teardown order is VM, then image, then the log group **last** — asserted on the
    /// recorder's ledger rather than on the report, because the order is a property of what
    /// was emitted.
    ///
    /// The log group is last because the service can recreate a group deleted before its
    /// image. This crate cannot delete it at all, so the assertion is that it is *named
    /// after* the image work rather than that a delete call went out — see
    /// `TeardownReport::undeleted`.
    #[tokio::test]
    async fn teardown_deletes_the_vm_then_the_image_and_names_the_log_group_last() {
        let (mut sandbox, recorder, _) = planted();
        recorder
            .answer(
                "CreateMicrovmImage",
                Answer::created(fake::create_image_response("agentd-conformance")),
            )
            .answer(
                "GetMicrovmImage",
                Answer::ok(fake::get_image_response("agentd-conformance", "CREATED")),
            );
        sandbox
            .build_image(CreateImageRequest::new(
                "agentd-conformance",
                b"binary".to_vec(),
                "s3://bucket/img.zip",
                "arn:aws:iam::123456789012:role/build",
            ))
            .await
            .expect("builds");
        answer_launch(&recorder);
        sandbox
            .run(RunRequest::new())
            .await
            .expect("launches from the built image");

        recorder
            .answer("TerminateMicrovm", Answer::ok(fake::empty_response()))
            .answer(
                "ListMicrovmImageVersions",
                Answer::ok(fake::list_versions_response("1")),
            )
            .answer(
                "DeleteMicrovmImage",
                Answer::ok(fake::delete_image_response()),
            );

        let report = sandbox
            .terminate(
                TeardownOpts::default()
                    .deleting_image()
                    .deleting_log_group(),
            )
            .await;
        assert!(report.terminate_accepted);
        assert_eq!(report.image_deleted, Some(true));

        // The ledger, filtered to the three destructive operations.
        let order: Vec<&str> = recorder
            .operations()
            .into_iter()
            .filter(|operation| matches!(*operation, "TerminateMicrovm" | "DeleteMicrovmImage"))
            .collect();
        assert_eq!(
            order,
            ["TerminateMicrovm", "DeleteMicrovmImage"],
            "the VM goes before the image: a terminating VM holds a reference to it"
        );

        // The log group is named, and named last.
        assert_eq!(
            report.undeleted,
            ["/aws/lambda-microvms/agentd-conformance"],
            "{report:?}"
        );
        assert!(report.leaked());
        assert!(
            report
                .failures
                .last()
                .expect("a failure")
                .contains("log group"),
            "the log group's failure has to be the last one recorded: {report:?}"
        );
        assert!(
            report
                .failures
                .last()
                .expect("a failure")
                .contains("terraform destroy"),
            "the reason it leaks has to be named: {report:?}"
        );
    }

    /// An image delete that fails every attempt lands its identifier in the report rather
    /// than raising, so the CLI can emit what was left behind (CLI-6).
    ///
    /// The identifier rather than a flag is the whole point: a leak nobody can name is a leak
    /// nobody can clean up.
    #[tokio::test]
    async fn a_failing_image_delete_names_what_it_left_behind() {
        let (mut sandbox, recorder, _) = planted();
        recorder
            .answer(
                "CreateMicrovmImage",
                Answer::created(fake::create_image_response("img")),
            )
            .answer(
                "GetMicrovmImage",
                Answer::ok(fake::get_image_response("img", "CREATED")),
            );
        sandbox
            .build_image(CreateImageRequest::new(
                "img",
                b"binary".to_vec(),
                "s3://bucket/img.zip",
                "arn:aws:iam::123456789012:role/build",
            ))
            .await
            .expect("builds");
        answer_launch(&recorder);
        sandbox.run(RunRequest::new()).await.expect("launches");

        recorder
            .answer("TerminateMicrovm", Answer::ok(fake::empty_response()))
            .answer(
                "ListMicrovmImageVersions",
                Answer::ok(fake::list_versions_response("1")),
            )
            .answer(
                "DeleteMicrovmImage",
                Answer::failure(409, "image is in CREATING"),
            );

        let mut opts = TeardownOpts::default().deleting_image();
        opts.delete_attempts = 3;
        opts.delete_backoff = Duration::from_secs(1);
        let report = sandbox.terminate(opts).await;

        assert_eq!(report.image_deleted, Some(false));
        assert_eq!(
            report.undeleted,
            ["arn:aws:lambda:us-east-1:123456789012:microvm-image:img"],
            "{report:?}"
        );
        assert!(
            sandbox.image_exists(),
            "the image really is still there, so the flag must say so"
        );
        assert_eq!(
            recorder.call_count("DeleteMicrovmImage"),
            3,
            "every attempt ran"
        );
    }

    /// A terminate whose own call fails still records the VM as one this client asked to
    /// destroy, and names it as undeleted.
    ///
    /// Recording it is what stops a later resume from being offered against a VM the caller
    /// already tried to kill (STATE-11) — the alternative leaves the sandbox looking
    /// resumable, which is the worse of the two wrong answers.
    #[tokio::test]
    async fn a_terminate_whose_call_fails_still_records_the_intent_and_the_leak() {
        let (mut sandbox, recorder, _) = launched().await;
        recorder.answer(
            "TerminateMicrovm",
            Answer::failure(500, "InternalServerException"),
        );

        let report = sandbox.terminate(TeardownOpts::default()).await;
        assert!(!report.terminate_accepted);
        assert_eq!(report.undeleted, ["mvm-abc123"], "{report:?}");
        assert!(sandbox.was_terminated());
        assert_eq!(report.lifecycle, Some(Lifecycle::Terminating));
        assert!(
            !report.failures.is_empty(),
            "the swallowed failure is recorded"
        );
    }

    /// A teardown on a sandbox that never launched is a no-op report rather than a panic.
    ///
    /// The path a caller reaches by tearing down in a `finally` after a launch that failed
    /// before any VM existed, which is exactly when a teardown that assumed a VM would turn
    /// the real failure into a panic.
    #[tokio::test]
    async fn a_teardown_before_any_launch_is_an_empty_report() {
        let (mut sandbox, recorder, _) = planted();
        let report = sandbox.terminate(TeardownOpts::default()).await;
        assert!(!report.terminate_accepted);
        assert!(!report.leaked());
        assert_eq!(report.lifecycle, Some(Lifecycle::Pending));
        assert_eq!(recorder.calls().len(), 0);
    }

    /// A run with no image at all is a precondition error before any call, rather than a
    /// launch of an empty identifier.
    #[tokio::test]
    async fn a_run_with_no_image_is_refused_before_any_call() {
        let (mut sandbox, recorder, _) = planted();
        let error = sandbox
            .run(RunRequest::new())
            .await
            .expect_err("there is nothing to launch");
        assert_eq!(error.kind(), ErrorKind::Precondition);
        assert!(error.to_string().contains("build_image first"), "{error}");
        assert_eq!(recorder.calls().len(), 0);
    }

    /// The lifecycle's six states are the symspec's `vm_state` domain, spelled as the service
    /// spells them.
    ///
    /// Asserted rather than assumed because the spelling reaches an error message a reader
    /// compares against a `GetMicrovm` response: a lifecycle rendering `Suspended` beside a
    /// service saying `SUSPENDED` reads like two different facts.
    #[test]
    fn the_lifecycle_states_are_the_state_models_domain_in_the_services_spelling() {
        let all = [
            Lifecycle::Pending,
            Lifecycle::Running,
            Lifecycle::Suspending,
            Lifecycle::Suspended,
            Lifecycle::Terminating,
            Lifecycle::Terminated,
        ];
        assert_eq!(
            all.map(Lifecycle::as_str),
            [
                "PENDING",
                "RUNNING",
                "SUSPENDING",
                "SUSPENDED",
                "TERMINATING",
                "TERMINATED"
            ]
        );
        for state in all {
            assert_eq!(state.to_string(), state.as_str());
        }
        // The four live states are the ones a `Drop` warns about, and TERMINATING is not one
        // of them: the platform accepted the terminate, so nobody needs telling.
        assert_eq!(
            all.iter().filter(|state| state.is_live()).count(),
            4,
            "a state added to the enum defaults to not-live, which would silence the warning"
        );
        assert!(!Lifecycle::Terminating.is_live());
        assert!(!Lifecycle::Terminated.is_live());
    }

    /// An agent token is 64 hex characters and never repeats across draws.
    ///
    /// The distinctness matters because the token is the only thing separating one VM's
    /// control API from another's: two VMs sharing a token means either one's caller can
    /// drive the other's guest. The token is the source's draw, so a fresh source per launch
    /// is fresh tokens; the OS pool's own distinctness is `microvms-edges`'s
    /// `the_os_source_draws_distinct_bytes`.
    #[test]
    fn a_minted_agent_token_is_sixty_four_hex_characters_and_never_repeats() {
        let entropy = crate::entropy::testing::SequenceEntropy::new();
        let minted: std::collections::HashSet<String> = (0..200)
            .map(|_| mint_agent_token(&entropy).expect("scripted"))
            .collect();
        assert_eq!(minted.len(), 200, "a repeated token is a shared guest");
        for token in &minted {
            assert_eq!(token.len(), 64, "{token}");
            assert!(token.chars().all(|c| c.is_ascii_hexdigit()), "{token}");
            assert_ne!(
                token,
                &"0".repeat(64),
                "an all-zero draw is a read that did nothing"
            );
        }
    }

    /// A sandbox's `Debug` does not print the agent token.
    #[tokio::test]
    async fn a_sandbox_debug_does_not_print_the_agent_token() {
        let (mut sandbox, recorder, _) = planted();
        answer_launch(&recorder);
        sandbox
            .run(
                RunRequest::new()
                    .with_image("arn:image")
                    .with_agent_token("super-secret-agent-token"),
            )
            .await
            .expect("launches");

        let rendered = format!("{sandbox:?}");
        assert!(rendered.contains("mvm-abc123"), "{rendered}");
        assert!(rendered.contains("Running"), "{rendered}");
        assert!(
            !rendered.contains("super-secret"),
            "the agent token reached a Debug string: {rendered}"
        );
    }

    /// A torn-down sandbox does not warn on drop, and an abandoned one is the case the
    /// warning exists for.
    ///
    /// Asserted on the flag rather than on stderr — capturing another thread's stderr from a
    /// test is not something this crate can do without a dependency — so what is pinned is
    /// the condition the warning branches on. Both halves, so the branch cannot be
    /// vacuously true.
    #[tokio::test]
    async fn a_torn_down_sandbox_is_distinguishable_from_an_abandoned_one() {
        let (mut sandbox, recorder, _) = launched().await;
        assert!(
            sandbox.lifecycle().is_live() && !sandbox.torn_down,
            "an abandoned live VM is what the drop warning is for"
        );

        recorder.answer("TerminateMicrovm", Answer::ok(fake::empty_response()));
        sandbox.terminate(TeardownOpts::default()).await;
        assert!(
            sandbox.torn_down,
            "terminate must mark the sandbox torn down"
        );
        assert!(
            !sandbox.lifecycle().is_live(),
            "and the lifecycle must no longer read as live"
        );
    }

    #[tokio::test]
    async fn stable_launch_key_and_payload_survive_a_fresh_supervisor() {
        let mut bodies = Vec::new();
        for _ in 0..2 {
            let (mut sandbox, recorder, _) = planted();
            answer_launch(&recorder);
            let mut request = RunRequest::new().with_image("arn:image");
            request.client_token = Some("one-unique-job-launch".into());
            request.agent_token = Some("persisted-private-secret".into());
            sandbox.run(request).await.expect("accepted");
            bodies.push(recorder.first_body("RunMicrovm"));
        }
        assert_eq!(bodies[0]["clientToken"], "one-unique-job-launch");
        assert_eq!(bodies[0], bodies[1]);
    }

    #[tokio::test]
    async fn unsafe_or_malformed_stable_launch_is_refused_without_calls() {
        for (token, secret, identity) in [
            ("key".to_string(), None, false),
            ("key".to_string(), Some("secret".into()), true),
            ("".to_string(), Some("secret".into()), false),
            ("x".repeat(129), Some("secret".into()), false),
            ("line\nbreak".to_string(), Some("secret".into()), false),
        ] {
            let (mut sandbox, recorder, _) = planted();
            let mut request = RunRequest::new().with_image("arn:image");
            request.client_token = Some(token);
            request.agent_token = secret;
            request.identity = identity;
            let error = sandbox.run(request).await.expect_err("invalid");
            assert_eq!(error.kind(), ErrorKind::InvalidArg);
            assert!(recorder.calls().is_empty());
        }
    }
}
