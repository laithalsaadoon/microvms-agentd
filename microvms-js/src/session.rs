// SPDX-License-Identifier: Apache-2.0
//! The control API of one running MicroVM.
//!
//! # A launched session lives inside its sandbox
//!
//! Same constraint as the Python side, and it comes from the landed core rather than from
//! either binding: `Sandbox` owns its `Session` by value and hands out only
//! `Option<&Session>`, so ordinary calls go through the sandbox. [`Held`] is the
//! consequence: `Session.direct(...)` owns its session, and a launched one reaches into the
//! sandbox through the same `Arc<tokio::sync::Mutex<Sandbox>>` [`crate::sandbox::Sandbox`]
//! holds. The one exception is `keepAwake`, which clones the session (a clone shares its
//! transport) and gates on the sandbox's lifecycle watch, because it must keep polling
//! while a long call holds the lock.
//!
//! The mutex is **tokio's** here and `std`'s on the Python side, and the difference is
//! forced: every method below is `async` and holds the guard across an `await`, which a
//! `std::sync::MutexGuard` cannot do (it is not `Send`). Holding it there is not a
//! compromise — the core's `suspend`/`resume`/`terminate` take `&mut self`, so in Rust a
//! `&Session` cannot be alive across them, and this reproduces exactly that exclusion.
//!
//! # `run` takes an argv, and a bare string is one element
//!
//! `session.run(["ls", "-la"])` and `session.run("ls -la", { shell: true })`. A bare string
//! with `shell` unset becomes a **one-element** argv and is never whitespace-split, which is
//! `session.py`'s own rule: splitting on spaces is how a path with a space in it becomes two
//! arguments nobody meant.

use std::collections::HashMap;
use std::sync::Arc;

use microvms_core::sandbox::Sandbox as CoreSandbox;
use microvms_core::session::{Session as CoreSession, StreamOptions, mint_exec_id};
use microvms_core::{Error, ErrorKind};
use napi::bindgen_prelude::Either;
use napi_derive::napi;
use tokio::sync::{Mutex, MutexGuard};

use crate::errors::{AsyncError, js, js_async};
use crate::exec::{ExecHandle, ExecResult, seconds_async};
use crate::process::{ExecProcess, GapPolicy};
use crate::region::Region;

/// How long to wait for a daemon to report bootstrapped, matching the core's default.
const DEFAULT_READY_TIMEOUT: f64 = 120.0;

/// The default one-shot `runSync` deadline, matching the Python client's 300s.
const DEFAULT_RUN_SYNC_TIMEOUT: f64 = 300.0;

/// The daemon's liveness answer. `bootstrapped` is the useful field.
#[napi(object)]
pub struct Health {
    /// The daemon's own version, distinct from the protocol version.
    pub version: String,
    /// Whether the run hook has landed and the control API is open.
    pub bootstrapped: bool,
    /// Bytes available to an unprivileged writer, or `null` when free space could not be
    /// measured.
    ///
    /// `null` is deliberately distinct from zero: unmeasurable is not full, and a monitor
    /// that conflated them would page on a missing `statvfs`.
    pub available_bytes: Option<i64>,
    /// Bytes that must stay free before a write is refused. Zero means the guard is off.
    pub reserve_bytes: Option<i64>,
    /// Whether a write would be refused right now. Precomputed by the daemon so every
    /// consumer applies the same comparison the write path does.
    pub under_pressure: Option<bool>,
    /// Whether any startup identity repair step failed — a duplicate machine-id or boot_id
    /// still in place from the shared image.
    pub identity_degraded: bool,
    /// False when identity repair was switched off by config. Separate from `degraded` so a
    /// monitor can tell "opted out" from "nothing to do".
    pub identity_repaired: bool,
    /// Whether any exec is still running.
    ///
    /// For an orchestrator *outside* the VM deciding whether to keep it alive. The platform
    /// measures idleness by inbound traffic through the endpoint proxy, which terminates
    /// outside the guest, so an in-guest keepalive cannot reset the idle timer — polling this
    /// from outside is both the traffic and the decision. An exec that exited and is awaiting
    /// an ack is not busy.
    pub busy: bool,
    /// How many execs are registered, in any phase. `busy: false` with a non-zero count is a
    /// VM holding unacked output somebody still has to collect.
    pub execs: i64,
    /// Every lifecycle-hook invocation the daemon observed, oldest first, each with its
    /// workload handler's outcome when the image carries one. The hook routes are
    /// reachable over loopback from inside the guest, so a workload can add entries; the
    /// earliest are the platform's.
    pub hooks: Vec<HookObservation>,
    /// How many hook invocations the daemon's log cap dropped.
    pub hooks_dropped: i64,
    /// Each identity-repair step and its outcome. Empty until the run hook repairs this
    /// VM's identity.
    pub identity_steps: Vec<IdentityStep>,
    /// How many variables the daemon's image-environment snapshot holds, or `null` when it
    /// holds none. `null` is also what a daemon built before `inheritImageEnv` reports, and
    /// such a daemon ignores that option. The values are never reported.
    pub image_env_keys: Option<i64>,
}

/// One lifecycle-hook invocation, as the daemon observed it.
#[napi(object)]
pub struct HookObservation {
    /// `ready`, `validate`, `run`, `suspend`, `resume`, or `terminate`.
    pub hook: String,
    /// Seconds since the epoch on the daemon's clock when the hook arrived.
    pub fired_at: i64,
    /// The workload handler's outcome, or `null` when the image has no handler for it.
    pub handler: Option<HandlerOutcome>,
}

/// What a workload's hook handler did. Its output is in the daemon's log, not here.
#[napi(object)]
pub struct HandlerOutcome {
    /// The exit code, or `null` when the handler was killed or never started.
    pub exit_code: Option<i32>,
    /// The signal that ended it, if one did.
    pub signal: Option<i32>,
    /// Whether the daemon killed it at its time budget.
    pub timed_out: bool,
    /// Milliseconds from spawn to exit or kill.
    pub duration_ms: i64,
    /// Why it could not run at all, such as `not executable`.
    pub error: Option<String>,
    /// Whether it ran and exited 0 within its budget.
    pub succeeded: bool,
}

/// One identity-repair step.
#[napi(object)]
pub struct IdentityStep {
    /// `machine-id`, `hostname`, `boot-id`, `random-seed`, or `cached-credential`.
    pub name: String,
    /// `repaired`, `not_applicable`, or `failed`.
    pub outcome: String,
    /// The OS error for a failed step.
    pub error: Option<String>,
}

impl Health {
    fn wrap(health: protocol::health::Health) -> Self {
        Self {
            version: health.version.into_owned(),
            bootstrapped: health.bootstrapped,
            available_bytes: health.disk.as_ref().map(|disk| disk.available_bytes as i64),
            reserve_bytes: health.disk.as_ref().map(|disk| disk.reserve_bytes as i64),
            under_pressure: health.disk.as_ref().map(|disk| disk.under_pressure),
            identity_degraded: health.identity_degraded,
            identity_repaired: health.identity_repaired,
            busy: health.busy,
            execs: health.execs as i64,
            hooks: health
                .hooks
                .into_iter()
                .map(|hook| HookObservation {
                    hook: hook.hook,
                    fired_at: hook.fired_at as i64,
                    handler: hook.handler.map(|handler| HandlerOutcome {
                        succeeded: handler.succeeded(),
                        exit_code: handler.exit_code,
                        signal: handler.signal,
                        timed_out: handler.timed_out,
                        duration_ms: handler.duration_ms as i64,
                        error: handler.error,
                    }),
                })
                .collect(),
            hooks_dropped: health.hooks_dropped as i64,
            identity_steps: health
                .identity_steps
                .into_iter()
                .map(|step| IdentityStep {
                    name: step.name,
                    outcome: step.outcome,
                    error: step.error,
                })
                .collect(),
            image_env_keys: health.image_env_keys.map(|keys| keys as i64),
        }
    }
}

/// One exec's process group, as `GET /v1/procs` reports it.
///
/// `childExited` beside a non-empty `pids` is the shape worth reading: a command that finished
/// while something it backgrounded did not, which `Health.busy` cannot show. The `execId`
/// beside it is what `Session.kill` takes.
#[napi(object)]
pub struct ProcGroup {
    /// The exec this group belongs to — what `kill` takes.
    pub exec_id: String,
    /// The process group id captured at spawn, or `null` when the child was reaped before it
    /// could be read (then `pids` is empty: there is no group to scan for).
    pub pgid: Option<u32>,
    /// Seconds since the epoch on the daemon's clock when the child was spawned.
    pub started_at: i64,
    /// Whether the exec's own child has exited. An acked exec still reads `true`.
    pub child_exited: bool,
    /// Whether the exec was started with `reapGroupOnExit`.
    pub reap: bool,
    /// Live pids whose process group is `pgid`, read from `/proc` inside the guest. Zombies
    /// are not listed. Empty once the group is gone.
    pub pids: Vec<u32>,
}

impl ProcGroup {
    fn wrap(group: protocol::exec::ProcGroup) -> Self {
        Self {
            exec_id: group.exec_id,
            pgid: group.pgid,
            started_at: group.started_at as i64,
            child_exited: group.child_exited,
            reap: group.reap,
            pids: group.pids,
        }
    }
}

/// How an exec should be started. Every field optional; the defaults are the daemon's.
#[napi(object)]
pub struct ExecOptions {
    /// `true` runs a single script string under `/bin/sh -c`; a string such as `"bash"`
    /// runs it under that shell, resolved by the daemon in the guest, and a shell the guest
    /// does not have is refused (`unknown_shell`) before anything starts.
    pub shell: Option<Either<bool, String>>,
    pub cwd: Option<String>,
    pub env: Option<HashMap<String, String>>,
    /// A numeric uid, or a name the daemon resolves against the guest's `/etc/passwd`. An
    /// unknown name is refused (`unknown_user`) before anything starts. A user with a passwd
    /// row gets `HOME`, `USER` and `LOGNAME` from it, beneath the launch environment and
    /// `env`.
    pub user: Option<Either<f64, String>>,
    /// A numeric gid, or a name the daemon resolves against the guest's `/etc/group`.
    pub group: Option<Either<f64, String>>,
    /// The daemon's own kill deadline for the child, distinct from a client-side `wait`.
    pub timeout_sec: Option<f64>,
    /// Whether to open a stdin pipe. Writing without this is a 409.
    pub stdin: Option<bool>,
    /// The idempotency key. Omitted, one is minted; supplied, the daemon returns success for
    /// a known id without spawning a second child — so a caller whose retry must be safe
    /// across its own restart passes a stable one.
    pub exec_id: Option<String>,
    /// The client-side deadline for `runSync` only.
    pub timeout: Option<f64>,
    /// Signal the whole process group once the command's own child exits, so nothing it
    /// backgrounded outlives it. Off by default, which keeps the backgrounded-grandchild-output
    /// guarantee for callers who rely on it.
    pub reap_group_on_exit: Option<bool>,
    /// Start the child's environment from the image's `ENV` (minus `AGENTD_*`, never the
    /// token), beneath everything else (AGENTD-11). Off by default, which keeps the child's environment
    /// exactly the launch environment plus `env`.
    pub inherit_image_env: Option<bool>,
}

impl ExecOptions {
    fn empty() -> Self {
        Self {
            shell: None,
            cwd: None,
            env: None,
            user: None,
            group: None,
            timeout_sec: None,
            stdin: None,
            exec_id: None,
            timeout: None,
            reap_group_on_exit: None,
            inherit_image_env: None,
        }
    }

    /// The daemon's start request.
    ///
    /// `command` arrives as `Either<String, Vec<String>>`, which is napi's tagged extraction
    /// — a number or an object is rejected by the conversion before this runs. A bare string
    /// becomes a **one-element** argv rather than being whitespace-split; see the module
    /// docs.
    fn into_request(
        self,
        command: Either<String, Vec<String>>,
    ) -> Result<protocol::exec::StartRequest, AsyncError> {
        Ok(protocol::exec::StartRequest {
            exec_id: self.exec_id.unwrap_or_else(mint_exec_id),
            command: match command {
                Either::A(single) => vec![single],
                Either::B(argv) => argv,
            },
            shell: match self.shell {
                None => protocol::exec::Shell::Flag(false),
                Some(Either::A(flag)) => protocol::exec::Shell::Flag(flag),
                Some(Either::B(name)) => protocol::exec::Shell::Named(name),
            },
            cwd: self.cwd,
            env: self.env.unwrap_or_default(),
            user: principal(self.user, "user").map_err(js_async)?,
            group: principal(self.group, "group").map_err(js_async)?,
            timeout_sec: self.timeout_sec,
            stdin: self.stdin.unwrap_or(false),
            reap_group_on_exit: self.reap_group_on_exit.unwrap_or(false),
            inherit_image_env: self.inherit_image_env.unwrap_or(false),
        })
    }
}

/// A user or group as a caller names it: a number is range-checked as a u32 id, a string is
/// passed through for the daemon to resolve in the guest (AGENTD-7, AGENTD-16).
fn principal(
    value: Option<Either<f64, String>>,
    field: &str,
) -> Result<Option<protocol::exec::NameOrId>, microvms_core::Error> {
    Ok(match value {
        None => None,
        Some(Either::A(id)) => crate::numbers::optional_u32(Some(id), field)?.map(Into::into),
        Some(Either::B(name)) => Some(name.into()),
    })
}

/// How a `spawn` should behave: the exec's own options, plus the stream's.
///
/// Nested rather than flattened onto [`ExecOptions`], and the reason is that the two sets are
/// answered by different people. `cwd`/`env`/`user` describe the *child* and are what a caller
/// composing a command supplies; `offset`/`reconnect`/`gapPolicy` describe how this client
/// reads the child's output and are what a caller composing a *resume* supplies. Flattening
/// them would put `maxReconnects` next to `user` in one bag, which is where a caller reaches
/// for the wrong one.
#[napi(object)]
#[derive(Default)]
pub struct SpawnOptions {
    /// How to start the child. Identical to `run`'s options, deliberately: `spawn` is a
    /// different view of one exec rather than a different exec.
    pub exec: Option<ExecOptions>,
    /// The byte to start reading at. Non-zero resumes output a previous process was reading —
    /// which, with a stable `exec.execId`, is how a spawned process survives *this* process
    /// restarting.
    pub offset: Option<f64>,
    /// Whether to reconnect after a cut. Defaults to true, and turning it off is what makes a
    /// suspend/resume look like a stream that ended.
    pub reconnect: Option<bool>,
    /// How many reconnects before the streams error. A bound rather than forever.
    pub max_reconnects: Option<f64>,
    /// How long the body may be silent before the connection is treated as dead, in seconds.
    pub idle_timeout: Option<f64>,
    /// What to do when the daemon reports evicted output. Defaults to `'error'`; see
    /// [`crate::process`] for why.
    pub gap_policy: Option<GapPolicy>,
}

impl SpawnOptions {
    fn empty() -> Self {
        Self::default()
    }

    /// The core's stream options.
    ///
    /// `errorOnGap` is deliberately **not** set from `gapPolicy`, and that is the one
    /// non-obvious mapping here. Core's `error_on_gap` ends the drive with a typed error, which
    /// would lose the byte range's attribution and would stop `gaps` from ever being populated;
    /// this handle needs to *see* the gap event in order to error both streams with the range in
    /// the message under `'error'`, and to record it under `'event'`. So the gap always arrives
    /// as an event from core and the policy is applied one layer up, in the drive.
    fn stream_options(&self) -> Result<StreamOptions, AsyncError> {
        let defaults = StreamOptions::default();
        Ok(StreamOptions {
            offset: crate::numbers::offset_number(self.offset.unwrap_or(0.0)).map_err(js_async)?,
            reconnect: self.reconnect.unwrap_or(defaults.reconnect),
            max_reconnects: crate::numbers::optional_u32(self.max_reconnects, "maxReconnects")
                .map_err(js_async)?
                .unwrap_or(defaults.max_reconnects),
            error_on_gap: false,
            idle_timeout: match self.idle_timeout {
                Some(idle) => seconds_async(idle)?,
                None => defaults.idle_timeout,
            },
        })
    }
}

/// Where a session lives, which decides how it is reached. See the module docs.
pub(crate) enum Held {
    /// A session this object owns, from [`Session::direct`].
    Owned(CoreSession),
    /// A session inside a sandbox, reached under the sandbox's lock.
    InSandbox(Arc<Mutex<CoreSandbox>>),
}

/// One running MicroVM's control API, with the proxy auth handled for you.
#[napi]
pub struct Session {
    held: Held,
}

/// A live session plus, when there is one, the guard keeping it alive.
///
/// The guard has to be *returned* rather than dropped inside a helper, because the reference
/// borrows from it — which is the whole reason this is an enum rather than a closure-taking
/// helper like the Python side's. A closure would work too, but it cannot be `async` without
/// boxing every call site's future.
pub(crate) enum Live<'a> {
    Owned(&'a CoreSession),
    Guarded(MutexGuard<'a, CoreSandbox>),
}

impl Live<'_> {
    pub(crate) fn session(&self) -> Result<&CoreSession, Error> {
        match self {
            Live::Owned(session) => Ok(session),
            Live::Guarded(guard) => guard.session().ok_or_else(|| {
                Error::new(
                    ErrorKind::Precondition,
                    format!(
                        "this sandbox holds no session: it is {} and terminate() drops the session \
                         because the only remaining use of its cached proxy token would be a \
                         request against a VM that is going away. A new VM needs a new Sandbox.",
                        guard.lifecycle()
                    ),
                )
            }),
        }
    }
}

impl Session {
    /// A session that reaches into `sandbox`.
    pub(crate) fn in_sandbox(sandbox: Arc<Mutex<CoreSandbox>>) -> Self {
        Self {
            held: Held::InSandbox(sandbox),
        }
    }

    /// Takes whatever this object needs in order to have a live session.
    ///
    /// For a sandbox-held session that is the sandbox lock, held until the returned value
    /// drops — so it spans exactly one method call and no more.
    pub(crate) async fn live(&self) -> Live<'_> {
        match &self.held {
            Held::Owned(session) => Live::Owned(session),
            Held::InSandbox(sandbox) => Live::Guarded(sandbox.lock().await),
        }
    }
}

#[napi]
impl Session {
    /// A session against a daemon reached **directly**, with no proxy headers.
    ///
    /// The shape for a local binary, a test server, or a VM reached over a tunnel. There is
    /// deliberately no constructor that takes a proxy token: minting one is the control
    /// plane's job and it happens inside every request (TRAP-9), so a caller handing a token
    /// in would be handing in one that expires.
    #[napi(factory)]
    pub fn direct(endpoint: String, agent_token: String) -> napi::Result<Session, String> {
        Ok(Session {
            held: Held::Owned(CoreSession::direct(endpoint, agent_token).map_err(js)?),
        })
    }

    /// Reattach using a private control record, without bootstrapping again.
    /// Owns its transport independently of sandbox locks; use a separate attach with
    /// a short requestTimeout for keepalives. Never publish or log agentToken.
    #[napi(factory)]
    pub async fn attach(
        region: &Region,
        microvm_id: String,
        endpoint: String,
        agent_token: String,
        port: Option<u16>,
        request_timeout: Option<f64>,
    ) -> Result<Self, AsyncError> {
        let timeout = request_timeout.map(seconds_async).transpose()?;
        let session = CoreSession::attach(
            region.inner.clone(),
            microvm_id,
            endpoint,
            agent_token,
            port,
            timeout,
        )
        .await
        .map_err(js_async)?;
        Ok(Self {
            held: Held::Owned(session),
        })
    }

    /// The guest bearer credential. Store only in a private encrypted control record.
    #[napi]
    pub async fn agent_token(&self) -> Result<String, AsyncError> {
        let live = self.live().await;
        Ok(live.session().map_err(js_async)?.agent_token().to_string())
    }

    /// The endpoint this session addresses.
    #[napi]
    pub async fn endpoint(&self) -> Result<String, AsyncError> {
        let live = self.live().await;
        Ok(live.session().map_err(js_async)?.endpoint().to_string())
    }

    /// The port the proxy token is scoped to.
    #[napi]
    pub async fn port(&self) -> Result<u16, AsyncError> {
        let live = self.live().await;
        Ok(live.session().map_err(js_async)?.port())
    }

    /// Unauthenticated liveness.
    #[napi]
    pub async fn health(&self) -> Result<Health, AsyncError> {
        let live = self.live().await;
        let session = live.session().map_err(js_async)?;
        Ok(Health::wrap(session.health().await.map_err(js_async)?))
    }

    /// Keeps the VM awake by polling health from this process until stopped.
    ///
    /// The platform counts only inbound requests as activity, so an exec working with no
    /// client traffic is suspended once `maxIdleDurationSeconds` passes. Resolves at once
    /// with a running handle. On a sandbox-held session a suspend or terminate through the
    /// sandbox ends the keepalive before its next poll; stop it before suspending through
    /// anything else, or the next poll auto-resumes the VM.
    #[napi]
    pub async fn keep_awake(
        &self,
        options: Option<crate::keepalive::KeepAwakeOptions>,
    ) -> Result<crate::keepalive::KeepAwake, AsyncError> {
        let options = options.unwrap_or_default();
        let (session, running, known): (_, Option<microvms_core::session::RunningGate>, _) =
            match &self.held {
                Held::Owned(session) => (session.clone(), None, None),
                Held::InSandbox(sandbox) => {
                    let guard = sandbox.lock().await;
                    let session = guard
                        .session()
                        .ok_or_else(|| {
                            Error::new(
                                ErrorKind::Precondition,
                                "this sandbox holds no running session to keep awake",
                            )
                        })
                        .map_err(js_async)?
                        .clone();
                    let lifecycle = guard.watch_lifecycle();
                    let gate: microvms_core::session::RunningGate = Box::new(move || {
                        *lifecycle.borrow() == microvms_core::sandbox::Lifecycle::Running
                    });
                    (session, Some(gate), guard.idle_window())
                }
            };
        let task = options
            .policy(known)?
            .spawn(session, running)
            .map_err(js_async)?;
        Ok(crate::keepalive::KeepAwake::wrap(task))
    }

    /// Polls health until the daemon reports bootstrapped.
    ///
    /// Connection errors on the way are expected rather than exceptional: a VM that has just
    /// reached RUNNING commonly refuses a connection or two before the proxy path is wired
    /// up. A *fatal* error ends the wait at once, because retrying a 401 until the deadline
    /// is the mistake the retryable split exists to prevent.
    #[napi]
    pub async fn wait_until_ready(&self, timeout: Option<f64>) -> Result<Health, AsyncError> {
        let timeout = seconds_async(timeout.unwrap_or(DEFAULT_READY_TIMEOUT))?;
        let live = self.live().await;
        let session = live.session().map_err(js_async)?;
        Ok(Health::wrap(
            session.wait_until_ready(timeout).await.map_err(js_async)?,
        ))
    }

    /// Starts a command and returns its handle. Does not wait.
    #[napi]
    pub async fn run(
        &self,
        command: Either<String, Vec<String>>,
        options: Option<ExecOptions>,
    ) -> Result<ExecHandle, AsyncError> {
        let request = options
            .unwrap_or_else(ExecOptions::empty)
            .into_request(command)?;
        let live = self.live().await;
        let session = live.session().map_err(js_async)?;
        Ok(ExecHandle::wrap(
            session.run(request).await.map_err(js_async)?,
        ))
    }

    /// Starts a command and returns it as two byte streams, a `wait()`, and a `kill()`.
    ///
    /// The **process** shape, as against [`Self::run`]'s handle shape. Same start request and
    /// the same `ExecOptions` (`cwd`, `env`, `user`, `group`, `shell`, `execId`, `timeoutSec`
    /// all apply), so this is a different view of one exec rather than a different exec:
    /// `spawn` and `run` with the same `execId` address the same server-side child, and the
    /// daemon starts one.
    ///
    /// `stdout` and `stderr` come out as independent `ReadableStream<Uint8Array>`, which is
    /// deliberately the AI SDK harness's `SandboxProcess` shape — see [`crate::process`] for
    /// what that compatibility is and is not, for how one interleaved SSE channel becomes two
    /// streams, and for why an evicted byte range errors the streams by default instead of
    /// being reported out-of-band.
    ///
    /// The reconnect-at-cursor behaviour is unchanged from `stream()`: a stream cut by a
    /// suspend/resume rejoins at the byte offset rather than ending, so a suspend does not look
    /// like a clean exit.
    #[napi]
    pub async fn spawn(
        &self,
        command: Either<String, Vec<String>>,
        options: Option<SpawnOptions>,
    ) -> Result<ExecProcess, AsyncError> {
        let mut options = options.unwrap_or_else(SpawnOptions::empty);
        let policy = options.gap_policy.unwrap_or(GapPolicy::Error);
        let stream_options = options.stream_options()?;
        // Taken rather than destructured, because `stream_options` borrows `&self` above and the
        // exec half is consumed here.
        let request = options
            .exec
            .take()
            .unwrap_or_else(ExecOptions::empty)
            .into_request(command)?;
        let live = self.live().await;
        let session = live.session().map_err(js_async)?;
        let handle = session.run(request).await.map_err(js_async)?;
        Ok(ExecProcess::start(Arc::new(handle), stream_options, policy))
    }

    /// A handle for an exec started earlier, possibly by another process.
    ///
    /// The reattach path. Nothing is checked against the daemon here — the handle is an id
    /// plus a transport, and a poll is what discovers whether the exec exists.
    #[napi]
    pub async fn exec(&self, exec_id: String) -> Result<ExecHandle, AsyncError> {
        let live = self.live().await;
        let session = live.session().map_err(js_async)?;
        Ok(ExecHandle::wrap(session.exec(exec_id)))
    }

    /// Start, wait, ack. The one-shot shape, for when output is all you want.
    #[napi]
    pub async fn run_sync(
        &self,
        command: Either<String, Vec<String>>,
        options: Option<ExecOptions>,
    ) -> Result<ExecResult, AsyncError> {
        let options = options.unwrap_or_else(ExecOptions::empty);
        let timeout = seconds_async(options.timeout.unwrap_or(DEFAULT_RUN_SYNC_TIMEOUT))?;
        let request = options.into_request(command)?;
        let live = self.live().await;
        let session = live.session().map_err(js_async)?;
        Ok(ExecResult::wrap(
            session.run_sync(request, timeout).await.map_err(js_async)?,
        ))
    }

    /// Signals an exec's whole process group. Returns whether anything was signalled.
    #[napi]
    pub async fn kill(&self, exec_id: String) -> Result<bool, AsyncError> {
        let live = self.live().await;
        let session = live.session().map_err(js_async)?;
        session.kill(&exec_id).await.map_err(js_async)
    }

    /// Process accounting: every registered exec with its group's live pids.
    ///
    /// The daemon reads `/proc` itself, so this needs no `ps` in the guest. A `ProcGroup` with
    /// `childExited` and a non-empty `pids` is a command that finished while something it
    /// backgrounded did not; pass its `execId` to `kill`.
    #[napi]
    pub async fn procs(&self) -> Result<Vec<ProcGroup>, AsyncError> {
        let live = self.live().await;
        let session = live.session().map_err(js_async)?;
        Ok(session
            .procs()
            .await
            .map_err(js_async)?
            .procs
            .into_iter()
            .map(ProcGroup::wrap)
            .collect())
    }

    /// Writes one file, creating parents. `mode` is an **octal string** (`"0755"`), which is
    /// the daemon's shape — a number here would be ambiguous between 0o755 and 755.
    #[napi]
    pub async fn upload_file(
        &self,
        path: String,
        data: napi::bindgen_prelude::Uint8Array,
        mode: Option<String>,
    ) -> Result<(), AsyncError> {
        let live = self.live().await;
        let session = live.session().map_err(js_async)?;
        session
            .upload_file(&path, &data, mode.as_deref())
            .await
            .map_err(js_async)
    }

    /// Reads one file.
    #[napi]
    pub async fn download_file(
        &self,
        path: String,
    ) -> Result<napi::bindgen_prelude::Buffer, AsyncError> {
        let live = self.live().await;
        let session = live.session().map_err(js_async)?;
        Ok(session.download_file(&path).await.map_err(js_async)?.into())
    }

    /// Whether a path exists, distinguishing absence from every other refusal.
    #[napi]
    pub async fn file_exists(&self, path: String) -> Result<bool, AsyncError> {
        let live = self.live().await;
        let session = live.session().map_err(js_async)?;
        session.file_exists(&path).await.map_err(js_async)
    }

    /// Extracts pre-built tar bytes under `remote`.
    ///
    /// Bytes rather than a local path: packing a directory is the caller's, because the
    /// symlink and permission decisions in a pack belong to whoever knows what the tree
    /// means.
    #[napi]
    pub async fn upload_tar(
        &self,
        remote: String,
        archive: napi::bindgen_prelude::Uint8Array,
    ) -> Result<(), AsyncError> {
        let live = self.live().await;
        let session = live.session().map_err(js_async)?;
        session
            .upload_tar(&remote, &archive)
            .await
            .map_err(js_async)
    }

    /// The raw tar bytes of a remote tree.
    #[napi]
    pub async fn download_tar(
        &self,
        remote: String,
    ) -> Result<napi::bindgen_prelude::Buffer, AsyncError> {
        let live = self.live().await;
        let session = live.session().map_err(js_async)?;
        Ok(session
            .download_tar(&remote)
            .await
            .map_err(js_async)?
            .into())
    }

    /// Both proxy headers for `port`, so a caller can open its **own** connection.
    ///
    /// The `{ url, headers }` shape a provider's `getPortEndpoint` answers with: pair these
    /// with `endpoint()` and nothing outside this addon has to know how a proxy token is
    /// minted or when it refreshes. Minted through the session's existing cache, so calling
    /// this does not burn a second control-plane call and does not open a second TRAP-9
    /// schedule.
    ///
    /// A plain `Record<string, string>` rather than a class, and that is the one place this
    /// crate's own "never `#[napi(object)]` for anything carrying a closure" rule does not
    /// apply: there is no closure here to protect. The value is a **bearer credential**, and
    /// unlike the token itself it necessarily comes out as a string — a header a caller has to
    /// put on a request cannot be opaque. What is still closed is the other direction: no
    /// function in this addon *takes* a header map, so a caller cannot feed a forged or expired
    /// one back in.
    ///
    /// **Empty for a direct session**, which is the true answer rather than a missing one: a
    /// daemon reached directly takes no proxy headers, so an empty object is exactly what its
    /// requests carry.
    #[napi]
    pub async fn connect_headers(&self, port: f64) -> Result<HashMap<String, String>, AsyncError> {
        let live = self.live().await;
        let session = live.session().map_err(js_async)?;
        Ok(session
            .connect_headers(crate::numbers::u16_number(port, "port").map_err(js_async)?)
            .await
            .map_err(js_async)?
            .into_iter()
            .collect())
    }

    /// The three WebSocket subprotocols for `port`, in the order a handshake offers them.
    ///
    /// `new WebSocket(url, await session.connectSubprotocols(port))` is the whole use. A
    /// MicroVM endpoint takes the auth and the target port as `Sec-WebSocket-Protocol` values
    /// rather than as headers, because the browser `WebSocket` constructor cannot set a header
    /// — and the platform strips all three before forwarding, so a server inside the VM never
    /// negotiates them and must not be written to expect them.
    ///
    /// `null` for a direct session, and deliberately not an empty array: the subprotocol form
    /// exists only for a request through the endpoint proxy, so there is nothing for a
    /// directly-reached daemon to offer. Answering `["lambda-microvms"]` with no token would
    /// open a handshake refused for a reason naming neither the token nor the port.
    ///
    /// The middle string **contains the credential**. Same rule as `connectHeaders`.
    #[napi]
    pub async fn connect_subprotocols(&self, port: f64) -> Result<Option<Vec<String>>, AsyncError> {
        let live = self.live().await;
        let session = live.session().map_err(js_async)?;
        Ok(session
            .connect_subprotocols(crate::numbers::u16_number(port, "port").map_err(js_async)?)
            .await
            .map_err(js_async)?
            // `Vec` rather than a three-tuple, because napi has no fixed-length array type and
            // a `[string, string, string]` would have to be hand-written into `index.d.ts`.
            // The order is the contract and the core's array type is what pins it.
            .map(|offered| offered.to_vec()))
    }

    /// How many proxy tokens this session has minted, or `null` for a direct session.
    ///
    /// Exposed because it is the only observable that distinguishes a client which re-minted
    /// after a resume from one that kept a stale token (STATE-8). The **token itself** is not
    /// exposed and cannot be: the core's `ProxyToken` has no `Display`, no `as_str`, and no
    /// `Deref`, so "treat `authToken` as a string" is as inexpressible here as it is there
    /// (TRAP-7).
    #[napi]
    pub async fn proxy_mint_count(&self) -> Result<Option<i64>, AsyncError> {
        let live = self.live().await;
        let session = live.session().map_err(js_async)?;
        Ok(session.proxy_auth().map(|auth| auth.mint_count() as i64))
    }
}

/// The daemon's protocol constants, as a JSON string, for a caller asserting against the
/// wire contract.
#[napi]
pub fn session_constants() -> String {
    // The closed sets come from the protocol enums rather than being spelled here: a
    // phase added to `protocol::exec::Phase` appears in this list without an edit.
    let phases = protocol::exec::Phase::ALL
        .iter()
        .map(|phase| format!(r#""{}""#, phase.as_str()))
        .collect::<Vec<_>>()
        .join(",");
    let stream_kinds = protocol::exec::StreamKind::ALL
        .iter()
        .map(|kind| format!(r#""{}""#, kind.as_str()))
        .collect::<Vec<_>>()
        .join(",");
    format!(
        concat!(
            r#"{{"defaultAgentPort":{},"proxyAuthHeader":"{}","proxyPortHeader":"{}","#,
            r#""maxTokenLifetimeSeconds":{},"defaultRefreshAfterSeconds":{},"#,
            // The WebSocket handshake's three values, from the core's constants rather than
            // spelled here: the platform matches them by exact string, so a test that
            // asserted its own copy would assert that the copy is self-consistent.
            r#""wsSubprotocol":"{}","wsAuthSubprotocolPrefix":"{}","#,
            r#""wsPortSubprotocolPrefix":"{}","#,
            r#""phases":[{}],"streamKinds":[{}]}}"#,
        ),
        microvms_core::session::DEFAULT_AGENT_PORT,
        microvms_core::session::PROXY_AUTH_HEADER,
        microvms_core::session::PROXY_PORT_HEADER,
        microvms_core::session::MAX_TOKEN_LIFETIME.as_secs(),
        microvms_core::session::DEFAULT_REFRESH_AFTER.as_secs(),
        microvms_core::session::WS_SUBPROTOCOL,
        microvms_core::session::WS_AUTH_SUBPROTOCOL_PREFIX,
        microvms_core::session::WS_PORT_SUBPROTOCOL_PREFIX,
        phases,
        stream_kinds,
    )
}
