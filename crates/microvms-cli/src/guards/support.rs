// SPDX-License-Identifier: Apache-2.0
//! The fakes and builders more than one guard module uses: a seam that refuses every door, the
//! scripted control plane and its seams, the scripted daemon, and the arguments and reply bodies
//! the guards share. A helper only one module uses lives in that module.

#![cfg(test)]

use std::sync::{Arc, Mutex};
use std::time::Duration;

#[expect(
    clippy::disallowed_types,
    reason = "a scripted transport answers the calls core makes; it never sends one"
)]
use microvms_core::control::transport::{Call, Reply, Transport};
use microvms_core::control::{Clock, ControlPlane};
use microvms_core::prelude::*;
use microvms_core::sandbox::Sandbox;
use microvms_core::session::Session;
use microvms_core::testing::YieldingClock;
use microvms_core::{Error, ErrorKind, Region};

use crate::cli::{
    AttachFlags, BuildArgs, Command, ExecArgs, Explicit, InfraFlags, MemoryMib, RegionFlags,
    RunArgs,
};
use crate::commands::{Ctx, Rendered};
use crate::envelope::{Format, Output};
use crate::exit::CliError;
use crate::seam::futures_util_shim::BoxFuture;
use crate::seam::{Attach, CoreSeam, Door, Infra};

// ── a seam that fails closed, and the dispatchers every guard calls ──────────

/// The sentinel every refusal carries, so a test can tell "it failed" from "it failed *here*".
pub(super) const SENTINEL: &str = "seam-was-refused-a1b2c3";

/// A seam whose every door refuses, recording which one was entered.
///
/// The Rust counterpart of `test_cli.py:375`'s `refusing` factory, and it makes the same three
/// assertions possible. The third — *which* door — is the one the Python found it needed after
/// the second was defeated on purpose: a handler that constructed its own client would still
/// fail with the patched error while having bypassed the seam entirely.
///
/// (cli.py line numbers resolve at `git show 'c4d396e^:clients/python/src/microvms_agentd/cli.py'` — the retired oracle.)
pub(super) struct RefusingSeam {
    entered: Mutex<Vec<Door>>,
}

impl RefusingSeam {
    pub(super) fn new() -> Self {
        Self {
            entered: Mutex::new(Vec::new()),
        }
    }

    fn record(&self, door: Door) -> Error {
        self.entered.lock().expect("not poisoned").push(door);
        Error::new(
            // `Platform` rather than a kind under test, so a handler that swallowed the failure
            // and reported its own would produce a different code and be caught.
            ErrorKind::Platform,
            format!("{SENTINEL}: the {} door refused", door.as_str()),
        )
    }

    pub(super) fn doors(&self) -> Vec<Door> {
        self.entered.lock().expect("not poisoned").clone()
    }
}

impl CoreSeam for RefusingSeam {
    fn control_plane(&self, _region: Region) -> BoxFuture<'_, Result<ControlPlane, Error>> {
        let error = self.record(Door::ControlPlane);
        Box::pin(async move { Err(error) })
    }

    fn open_sandbox(
        &self,
        _region: Region,
        _port: Option<u16>,
    ) -> BoxFuture<'_, Result<Sandbox, Error>> {
        let error = self.record(Door::OpenSandbox);
        Box::pin(async move { Err(error) })
    }

    fn attach_session(
        &self,
        _region: Region,
        _attach: Attach,
    ) -> BoxFuture<'_, Result<Session, Error>> {
        let error = self.record(Door::AttachSession);
        Box::pin(async move { Err(error) })
    }

    fn put_artifact(&self, _uri: &str, _bytes: Vec<u8>) -> BoxFuture<'_, Result<(), Error>> {
        Box::pin(async move {
            Err(Error::new(
                ErrorKind::Platform,
                format!("{SENTINEL}: the artifact upload refused"),
            ))
        })
    }
}

/// Infrastructure that satisfies every `require`, so a command reaches the seam rather than
/// failing on a precondition.
///
/// Without this the behavioral guard would pass vacuously: every AWS command would fail at
/// `Infra::require` and never touch a door, and "it failed" would be true for the wrong reason.
pub(super) fn full_infra() -> Infra {
    Infra {
        bucket: Some("a-bucket".into()),
        build_role_arn: Some("arn:aws:iam::123456789012:role/build".into()),
        execution_role_arn: Some("arn:aws:iam::123456789012:role/execution".into()),
    }
}

pub(super) fn region_flags() -> RegionFlags {
    RegionFlags {
        region: Some(crate::cli::RegionArg::UsEast1),
        unlisted_region: None,
    }
}

/// The identifier triple every attached command takes, filled with plausible values.
///
/// Plausible rather than empty, because the guard's question is whether the command reached the
/// seam — and a blank endpoint could plausibly be refused *before* the door by some future
/// validation, which would make the door assertion pass for the wrong reason.
pub(super) fn attach_flags() -> AttachFlags {
    AttachFlags {
        endpoint: Some("https://mvm-1.example".into()),
        agent_token: Some("t".into()),
        microvm_id: Some("mvm-1".into()),
        name: None,
        port: None,
        state_dir: None,
    }
}

/// A temp file that looks like an aarch64 ELF, so the binary precondition passes.
pub(super) struct FakeBinary(pub(super) std::path::PathBuf);

impl FakeBinary {
    pub(super) fn new(label: &str) -> Self {
        let mut header = vec![0u8; 20];
        header[..4].copy_from_slice(b"\x7fELF");
        header[5] = 1;
        header[18..20].copy_from_slice(&0xB7u16.to_le_bytes());
        let path = std::env::temp_dir().join(format!(
            "microvm-guard-{label}-{}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        std::fs::write(&path, header).expect("writes");
        Self(path)
    }
}

impl Drop for FakeBinary {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
    }
}

/// Runs `command` against `seam` and returns whatever the handler produced.
///
/// The fetch seam is [`crate::provision::PanickingFetch`], so every guard routed through
/// here is *also* asserting its command never provisions — a run or build handed a binary
/// that reached for GitHub anyway would panic the test rather than pass it quietly.
pub(super) async fn dispatch_with(
    seam: &dyn CoreSeam,
    command: &Command,
    infra: Infra,
) -> (Result<Rendered, CliError>, String) {
    dispatch_with_fetch(seam, command, infra, &crate::provision::PanickingFetch).await
}

/// [`dispatch_with`], with one environment variable set — for the flags that read one.
pub(super) async fn dispatch_with_env(
    seam: &dyn CoreSeam,
    command: &Command,
    infra: Infra,
    var: (&'static str, &'static str),
) -> (Result<Rendered, CliError>, String) {
    let mut out = Output::new(Format::Json, false, Vec::new(), Vec::new());
    let env = move |name: &str| (name == var.0).then(|| var.1.to_string());
    let result = {
        let mut ctx = Ctx {
            seam,
            out: &mut out,
            infra,
            env: &env,
            fetch: &crate::provision::PanickingFetch,
        };
        crate::handle(&mut ctx, command, crate::commands::lifecycle::never()).await
    };
    let stderr = String::from_utf8(out.into_streams().1).expect("utf8");
    (result, stderr)
}

/// [`dispatch_with`], with the provisioning seam scripted — for the guards whose subject
/// *is* the provisioning chain.
pub(super) async fn dispatch_with_fetch(
    seam: &dyn CoreSeam,
    command: &Command,
    infra: Infra,
    fetch: &dyn crate::provision::Fetch,
) -> (Result<Rendered, CliError>, String) {
    let mut out = Output::new(Format::Json, false, Vec::new(), Vec::new());
    let env = |_: &str| None;
    let result = {
        let mut ctx = Ctx {
            seam,
            out: &mut out,
            infra,
            env: &env,
            fetch,
        };
        // The *shipped* dispatcher, with the one substitution the guard needs: the interrupt for
        // `run` is [`crate::commands::lifecycle::never`], so this measures the seam rather than
        // racing a signal.
        crate::handle(&mut ctx, command, crate::commands::lifecycle::never()).await
    };
    let stderr = String::from_utf8(out.into_streams().1).expect("utf8");
    (result, stderr)
}

// ── the scripted control plane ───────────────────────────────────────────────

/// A transport that answers from a queue and can fire an interrupt when it sees an operation.
///
/// Hand-rolled rather than reusing core's own recorder, which is `#[cfg(test)]`-private to that
/// crate. That is a feature here rather than a cost: like core's fake, every body below is a
/// **literal** written from the service model, so a member this crate misreads cannot be
/// misread identically by the fake.
#[expect(
    clippy::disallowed_types,
    reason = "a scripted transport answers the calls core makes; it never sends one"
)]
pub(super) struct ScriptedTransport {
    calls: Mutex<Vec<Call>>,
    /// Answers per operation, front to back; the last repeats.
    answers: Mutex<std::collections::HashMap<String, std::collections::VecDeque<(u16, String)>>>,
    /// Fired the first time this operation is seen. The interrupt's trigger.
    trigger: Mutex<Option<(String, tokio::sync::oneshot::Sender<()>)>>,
    /// The URIs `put_artifact` was asked to fill. On the transport rather than the seam so
    /// the ordering guard can assert "zero uploads" through the handle it already holds —
    /// an upload is not a control-plane call, so it must not pollute `calls`.
    uploads: Mutex<Vec<String>>,
}

// ── the scripted control plane ───────────────────────────────────────────────

impl ScriptedTransport {
    pub(super) fn new() -> Self {
        Self {
            calls: Mutex::new(Vec::new()),
            answers: Mutex::new(std::collections::HashMap::new()),
            trigger: Mutex::new(None),
            uploads: Mutex::new(Vec::new()),
        }
    }

    pub(super) fn uploads(&self) -> Vec<String> {
        self.uploads.lock().expect("not poisoned").clone()
    }

    pub(super) fn answer(&self, operation: &str, status: u16, body: &str) -> &Self {
        self.answers
            .lock()
            .expect("not poisoned")
            .entry(operation.to_string())
            .or_default()
            .push_back((status, body.to_string()));
        self
    }

    /// Fires `sender` the first time `operation` is called.
    pub(super) fn fire_on(
        &self,
        operation: &str,
        sender: tokio::sync::oneshot::Sender<()>,
    ) -> &Self {
        *self.trigger.lock().expect("not poisoned") = Some((operation.to_string(), sender));
        self
    }

    pub(super) fn calls(&self) -> Vec<String> {
        self.calls
            .lock()
            .expect("not poisoned")
            .iter()
            .map(|call| call.operation.to_string())
            .collect()
    }

    pub(super) fn called(&self, operation: &str) -> usize {
        self.calls()
            .iter()
            .filter(|call| *call == operation)
            .count()
    }

    /// The paths requested for `operation`, in order — where the resolution guards read
    /// the `nameFilter` and `nextToken` query members.
    pub(super) fn paths_of(&self, operation: &str) -> Vec<String> {
        self.calls
            .lock()
            .expect("not poisoned")
            .iter()
            .filter(|call| call.operation == operation)
            .map(|call| call.path.clone())
            .collect()
    }

    /// The first body sent to `operation`, as generic JSON — the recorder shape core's own
    /// fake uses, so an assertion reads the wire member rather than a struct's opinion of it.
    pub(super) fn first_body(&self, operation: &str) -> serde_json::Value {
        let calls = self.calls.lock().expect("not poisoned");
        let call = calls
            .iter()
            .find(|call| call.operation == operation)
            .unwrap_or_else(|| panic!("no call to {operation}"));
        let body = call
            .body
            .as_deref()
            .unwrap_or_else(|| panic!("{operation} sent no body"));
        serde_json::from_slice(body).expect("a JSON body")
    }
}

// ── the scripted control plane ───────────────────────────────────────────────

#[expect(
    clippy::disallowed_types,
    reason = "a scripted transport answers the calls core makes; it never sends one"
)]
impl Transport for ScriptedTransport {
    fn send(&self, call: Call) -> BoxFuture<'_, Result<Reply, Error>> {
        let operation = call.operation.to_string();
        self.calls.lock().expect("not poisoned").push(call);

        // The interrupt fires *when the launch is accepted*, which is the instant CLI-6 is about:
        // a VM exists, its identifier is recorded, and the RUNNING wait has not finished.
        let fire = {
            let mut trigger = self.trigger.lock().expect("not poisoned");
            match trigger.take() {
                Some((wanted, sender)) if wanted == operation => Some(sender),
                other => {
                    *trigger = other;
                    None
                }
            }
        };
        if let Some(sender) = fire {
            let _ = sender.send(());
        }

        let answer = {
            let mut answers = self.answers.lock().expect("not poisoned");
            let queue = answers
                .get_mut(&operation)
                .unwrap_or_else(|| panic!("the fake has no answer for {operation}"));
            if queue.len() > 1 {
                queue.pop_front().expect("non-empty")
            } else {
                queue.front().cloned().expect("non-empty")
            }
        };
        Box::pin(async move {
            Ok(Reply {
                status: answer.0,
                body: answer.1.into_bytes(),
            })
        })
    }
}

/// A seam that hands out sandboxes over `transport`.
pub(super) struct ScriptedSeam {
    pub(super) transport: Arc<ScriptedTransport>,
    pub(super) clock: Arc<YieldingClock>,
}

#[expect(
    clippy::disallowed_methods,
    reason = "a fake seam, the test's stand-in for src/seam.rs: it builds its plane or session over a scripted transport"
)]
impl CoreSeam for ScriptedSeam {
    fn control_plane(&self, region: Region) -> BoxFuture<'_, Result<ControlPlane, Error>> {
        let plane = ControlPlane::with_transport(
            Arc::clone(&self.transport) as Arc<dyn Transport>,
            region,
            Arc::clone(&self.clock) as Arc<dyn Clock>,
        );
        Box::pin(async move { Ok(plane) })
    }

    fn open_sandbox(
        &self,
        region: Region,
        _port: Option<u16>,
    ) -> BoxFuture<'_, Result<Sandbox, Error>> {
        let plane = ControlPlane::with_transport(
            Arc::clone(&self.transport) as Arc<dyn Transport>,
            region,
            Arc::clone(&self.clock) as Arc<dyn Clock>,
        );
        Box::pin(async move { Ok(Sandbox::with_control_plane(plane)) })
    }

    fn attach_session(
        &self,
        _region: Region,
        _attach: Attach,
    ) -> BoxFuture<'_, Result<Session, Error>> {
        Box::pin(async move {
            Err(Error::new(
                ErrorKind::Platform,
                "this guard does not attach sessions",
            ))
        })
    }

    fn put_artifact(&self, uri: &str, _bytes: Vec<u8>) -> BoxFuture<'_, Result<(), Error>> {
        self.transport
            .uploads
            .lock()
            .expect("not poisoned")
            .push(uri.to_string());
        Box::pin(async move { Ok(()) })
    }
}

/// `RunMicrovmResponse`/`GetMicrovmResponse`, in the model's own spelling.
pub(super) fn microvm_body(state: &str) -> String {
    format!(
        r#"{{"microvmId": "mvm-abc123", "state": "{state}",
             "endpoint": "https://mvm-abc123.microvm.us-east-1.amazonaws.com",
             "imageArn": "arn:aws:lambda:us-east-1:123456789012:microvm-image:img",
             "imageVersion": "1", "maximumDurationInSeconds": 3600, "startedAt": 1754524800}}"#
    )
}

/// `run --image`, so the launch reaches the wire without a build or an upload.
pub(super) fn interrupt_run_args(state_dir: std::path::PathBuf) -> RunArgs {
    run_args_for_image(
        "arn:aws:lambda:us-east-1:123456789012:microvm-image:img",
        state_dir,
    )
}

/// `run --image <identifier>` with everything else defaulted, for the resolution guards.
pub(super) fn run_args_for_image(identifier: &str, state_dir: std::path::PathBuf) -> RunArgs {
    RunArgs {
        binary: None,
        image: Some(identifier.into()),
        image_version: None,
        artifact_uri: None,
        exec: None,
        name: Some("img".into()),
        memory: MemoryMib::Mib2048,
        dockerfile: None,
        repair_identity: false,
        log_group: None,
        log_stream: None,
        egress: false,
        egress_network_connectors: Vec::new(),
        deny_egress: false,
        shell: false,
        launch_env: Vec::new(),
        user: None,
        group: None,
        keep: false,
        identity: false,
        vm_name: None,
        timeout: Duration::from_secs(30),
        max_idle_sec: 600,
        suspended_sec: 600,
        auto_resume: false,
        max_duration_sec: 3600,
        port: None,
        state_dir: Some(state_dir),
        // See `aws_commands`: an ambient microvm.toml must not leak into a guard.
        config: no_config(),
        explicit: Explicit::default(),
        region: region_flags(),
        infra: InfraFlags::default(),
        launch: Default::default(),
    }
}

/// `--no-config`, so a microvm.toml in the test runner's own cwd cannot reach a guard.
pub(super) fn no_config() -> crate::cli::ConfigFlags {
    crate::cli::ConfigFlags {
        config: None,
        no_config: true,
    }
}

/// A state directory that cleans itself up.
pub(super) struct TempDir(
    pub(super) std::path::PathBuf,
    #[allow(dead_code)] tempfile::TempDir,
);

impl TempDir {
    pub(super) fn new(label: &str) -> Self {
        let dir = tempfile::Builder::new()
            .prefix(&format!("microvm-guard-{label}-"))
            .tempdir()
            .expect("a temp dir");
        Self(dir.path().to_path_buf(), dir)
    }
}

/// `ListMicrovmImagesResponse`, in the model's own spelling, with an optional `nextToken`.
///
/// A literal for the reason every body in this file is one: a response produced by the
/// same serializer the client deserializes with cannot catch a misspelled member.
pub(super) fn list_images_body(names: &[&str], next_token: Option<&str>) -> String {
    let items: Vec<String> = names
        .iter()
        .map(|name| {
            format!(
                r#"{{"imageArn": "arn:aws:lambda:us-east-1:123456789012:microvm-image:{name}",
                     "name": "{name}", "state": "ACTIVE", "createdAt": 1754524800}}"#
            )
        })
        .collect();
    let token = match next_token {
        Some(token) => format!(r#", "nextToken": "{token}""#),
        None => String::new(),
    };
    format!(r#"{{"items": [{}]{token}}}"#, items.join(", "))
}

/// A `microvm.toml` in a temp directory, removed on drop.
pub(super) struct ConfigFile(pub(super) std::path::PathBuf);

impl ConfigFile {
    pub(super) fn new(label: &str, text: &str) -> Self {
        let path = std::env::temp_dir().join(format!(
            "microvm-guard-config-{label}-{}-{:?}.toml",
            std::process::id(),
            std::thread::current().id()
        ));
        std::fs::write(&path, text).expect("writes");
        Self(path)
    }
}

impl Drop for ConfigFile {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
    }
}

/// A [`crate::provision::Fetch`] that writes an aarch64 ELF header and counts calls, for
/// the provisioning guards.
pub(super) struct CountingFetch(pub(super) std::sync::atomic::AtomicUsize);

impl crate::provision::Fetch for CountingFetch {
    fn fetch(
        &self,
        _: &str,
        dest: &std::path::Path,
        _: &mut dyn FnMut(&str),
    ) -> Result<crate::provision::Verification, String> {
        self.0.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        let mut header = vec![0u8; 20];
        header[..4].copy_from_slice(b"\x7fELF");
        header[5] = 1;
        header[18..20].copy_from_slice(&0xB7u16.to_le_bytes());
        std::fs::write(dest, header).map_err(|error| error.to_string())?;
        Ok(crate::provision::Verification::Attestation)
    }
}

/// `build` arguments with **no binary at all** — the headline case provisioning exists for.
pub(super) fn build_args_without_binary(state_dir: std::path::PathBuf) -> BuildArgs {
    BuildArgs {
        binary: None,
        state_dir: Some(state_dir),
        base_image_version: None,
        artifact_uri: None,
        name: Some("prov".into()),
        memory: MemoryMib::Mib2048,
        dockerfile: None,
        project: None,
        repair_identity: false,
        log_group: None,
        log_stream: None,
        reuse: false,
        port: None,
        region: region_flags(),
        infra: InfraFlags::default(),
    }
}

/// The scripted control-plane answers a provisioning build needs: one create, one poll.
pub(super) fn script_prov_build(transport: &ScriptedTransport) {
    transport
        .answer(
            "CreateMicrovmImage",
            201,
            r#"{"imageArn": "arn:aws:lambda:us-east-1:123456789012:microvm-image:prov",
                 "name": "prov", "state": "CREATING", "createdAt": 1754524800,
                 "baseImageArn": "arn:aws:lambda:us-east-1:aws:microvm-image:al2023-1",
                 "buildRoleArn": "arn:aws:iam::123456789012:role/build",
                 "codeArtifact": {"uri": "s3://a-bucket/prov.zip"},
                 "imageVersion": "1"}"#,
        )
        .answer(
            "GetMicrovmImage",
            200,
            r#"{"imageArn": "arn:aws:lambda:us-east-1:123456789012:microvm-image:prov",
                 "name": "prov", "state": "CREATED", "createdAt": 1754524800}"#,
        );
}

// ── the attached surfaces, against a scripted daemon ─────────────────────────
//
// `RefusingSeam` above answers the CLI-2 question — did the command go through the door — and
// answers nothing about what it *did* once through. The five attached commands need the second
// question, because each of them has a specific claim: `cp` sends bytes it did not inspect,
// `--stream` writes the envelope last, `stdin` surfaces a 409 as `Conflict`, `ack` maps a second
// 409 to the same code with a different detail, and `--exec-id` forwards the caller's key verbatim.
//
// So this section scripts the *daemon* rather than refusing at the seam:
// `Session::builder(..).with_backend(..)` is public, so a queue of canned HTTP replies is a real
// session over a fake wire. Every reply body below is a **literal** written from the protocol
// crate's own field names, for the reason `ScriptedTransport` gives above and the reason lesson #5
// in `.erpaval/solutions/test-failures/guards-that-passed-against-broken-code.md` gives: a fake
// built by calling the same serializer the code under test calls cannot disagree with it, and
// therefore cannot catch a shape error. These can.

/// A queue of canned HTTP replies, keeping every request that was sent.
///
/// A recorder rather than an assertion sink, matching core's own testing shape: the assertions live
/// at the call site where a reader can see them, not inside the fake where they are invisible.
pub(super) struct DaemonScript {
    seen: Mutex<Vec<microvms_core::session::HttpRequest>>,
    pub(super) replies: Mutex<std::collections::VecDeque<(u16, Vec<u8>)>>,
    /// Chunk sequences for `open_stream`, front to back.
    streams: Mutex<std::collections::VecDeque<(u16, Vec<Vec<u8>>)>>,
}

impl DaemonScript {
    pub(super) fn new() -> Arc<Self> {
        Arc::new(Self {
            seen: Mutex::new(Vec::new()),
            replies: Mutex::new(std::collections::VecDeque::new()),
            streams: Mutex::new(std::collections::VecDeque::new()),
        })
    }

    /// Queues one non-streaming reply.
    pub(super) fn reply(self: &Arc<Self>, status: u16, body: &str) -> Arc<Self> {
        self.replies
            .lock()
            .expect("not poisoned")
            .push_back((status, body.as_bytes().to_vec()));
        Arc::clone(self)
    }

    /// Queues one streaming reply: the head status, then these chunks in order.
    pub(super) fn stream(self: &Arc<Self>, status: u16, chunks: Vec<Vec<u8>>) -> Arc<Self> {
        self.streams
            .lock()
            .expect("not poisoned")
            .push_back((status, chunks));
        Arc::clone(self)
    }

    pub(super) fn requests(&self) -> Vec<microvms_core::session::HttpRequest> {
        self.seen.lock().expect("not poisoned").clone()
    }

    /// The paths that were requested, in order — the observable most assertions want.
    pub(super) fn paths(&self) -> Vec<String> {
        self.requests()
            .into_iter()
            .map(|request| format!("{} {}", request.method, request.path))
            .collect()
    }
}

/// A chunk source over a queue, for the streaming replies.
struct Chunks(std::collections::VecDeque<Vec<u8>>);

impl microvms_core::session::ChunkSource for Chunks {
    fn next_chunk(&mut self) -> BoxFuture<'_, Result<Option<Vec<u8>>, Error>> {
        Box::pin(async move { Ok(self.0.pop_front()) })
    }
}

impl microvms_core::session::HttpBackend for DaemonScript {
    fn send(
        &self,
        request: microvms_core::session::HttpRequest,
    ) -> BoxFuture<'_, Result<microvms_core::session::HttpResponse, Error>> {
        let described = format!("{} {}", request.method, request.path);
        self.seen.lock().expect("not poisoned").push(request);
        let reply = self
            .replies
            .lock()
            .expect("not poisoned")
            .pop_front()
            .unwrap_or_else(|| panic!("the script ran out of replies at {described}"));
        Box::pin(async move {
            Ok(microvms_core::session::HttpResponse {
                status: reply.0,
                headers: std::collections::HashMap::new(),
                body: reply.1,
            })
        })
    }

    fn open_stream(
        &self,
        request: microvms_core::session::HttpRequest,
        _idle_timeout: Duration,
    ) -> BoxFuture<'_, Result<microvms_core::session::OpenStream, Error>> {
        let described = format!("{} {}", request.method, request.path);
        self.seen.lock().expect("not poisoned").push(request);
        let (status, chunks) = self
            .streams
            .lock()
            .expect("not poisoned")
            .pop_front()
            .unwrap_or_else(|| panic!("the script ran out of stream replies at {described}"));
        Box::pin(async move {
            let head = microvms_core::session::HttpResponse {
                status,
                headers: std::collections::HashMap::new(),
                body: if (200..300).contains(&status) {
                    Vec::new()
                } else {
                    chunks.concat()
                },
            };
            let source: Box<dyn microvms_core::session::ChunkSource> =
                if (200..300).contains(&status) {
                    Box::new(Chunks(chunks.into_iter().collect()))
                } else {
                    Box::new(Chunks(std::collections::VecDeque::new()))
                };
            Ok((head, source))
        })
    }
}

/// A seam whose `attach_session` hands out a session over `script`.
///
/// The other three doors refuse: a test of an attached command that reached `open_sandbox` would be
/// a test of the wrong path, and a refusal says so loudly rather than succeeding quietly.
pub(super) struct ScriptedSessionSeam {
    pub(super) script: Arc<DaemonScript>,
}

#[expect(
    clippy::disallowed_methods,
    reason = "a fake seam, the test's stand-in for src/seam.rs: it builds its plane or session over a scripted transport"
)]
impl CoreSeam for ScriptedSessionSeam {
    fn control_plane(&self, _region: Region) -> BoxFuture<'_, Result<ControlPlane, Error>> {
        Box::pin(async move {
            Err(Error::new(
                ErrorKind::Platform,
                "this guard attaches sessions only",
            ))
        })
    }

    fn open_sandbox(
        &self,
        _region: Region,
        _port: Option<u16>,
    ) -> BoxFuture<'_, Result<Sandbox, Error>> {
        Box::pin(async move {
            Err(Error::new(
                ErrorKind::Platform,
                "this guard attaches sessions only",
            ))
        })
    }

    fn attach_session(
        &self,
        _region: Region,
        _attach: Attach,
    ) -> BoxFuture<'_, Result<Session, Error>> {
        // No minter, so no proxy headers — which is the shape core documents for a daemon reached
        // directly and is exactly right here: TRAP-9's mint is core's own tested property, and
        // adding a fake minter would put a second thing in the way of what this guard is asking.
        let backend = Arc::clone(&self.script) as Arc<dyn microvms_core::session::HttpBackend>;
        let built = Session::builder("https://mvm-1.example", "agent-token")
            .with_backend(backend)
            .build();
        Box::pin(async move { built })
    }

    fn put_artifact(&self, _uri: &str, _bytes: Vec<u8>) -> BoxFuture<'_, Result<(), Error>> {
        Box::pin(async move { Ok(()) })
    }
}

/// Runs one attached command against `script`, returning the result and both streams.
pub(super) async fn against_daemon(
    script: &Arc<DaemonScript>,
    command: &Command,
) -> (Result<Rendered, CliError>, String, String) {
    let seam = ScriptedSessionSeam {
        script: Arc::clone(script),
    };
    let mut out = Output::new(Format::Json, false, Vec::new(), Vec::new());
    let env = |_: &str| None;
    let result = {
        let mut ctx = Ctx {
            seam: &seam,
            out: &mut out,
            infra: full_infra(),
            env: &env,
            fetch: &crate::provision::PanickingFetch,
        };
        crate::handle(&mut ctx, command, crate::commands::lifecycle::never()).await
    };
    let (stdout, stderr) = out.into_streams();
    (
        result,
        String::from_utf8_lossy(&stdout).to_string(),
        String::from_utf8_lossy(&stderr).to_string(),
    )
}

/// An exec whose `--exec-id` and flags the caller chooses; everything else defaulted.
pub(super) fn exec_command(shape: impl FnOnce(&mut ExecArgs)) -> Command {
    let mut args = ExecArgs {
        command: Some("true".into()),
        timeout: Duration::from_secs(30),
        cwd: None,
        env: Vec::new(),
        user: None,
        group: None,
        shell: None,
        inherit_image_env: false,
        exec_id: None,
        poll: None,
        detach: false,
        stream: false,
        from_offset: None,
        stdin: false,
        reap: false,
        kill_on_timeout: false,
        attach: AttachFlags {
            state_dir: Some(std::env::temp_dir().join(format!(
                "microvm-guard-exec-history-{}-{:?}",
                std::process::id(),
                std::thread::current().id()
            ))),
            ..attach_flags()
        },
        region: region_flags(),
    };
    shape(&mut args);
    Command::Exec(args)
}

/// `PollResponse`, in the protocol's own snake_case spelling, with the outcome **flattened**.
///
/// Written out rather than serialized from `microvms_core::protocol::exec::PollResponse`, which is the whole point:
/// a body produced by the same serializer the client deserializes with agrees with a renamed field
/// by construction. This one does not — and it earned its keep immediately. The first draft nested
/// the outcome under a `"result"` key, because that is what the Rust field is called. It is
/// `#[serde(flatten)]` (`crates/protocol/src/exec.rs:195`), so on the wire those fields sit **beside**
/// `exec_id` and `phase` with no wrapper at all. Every exec assertion in this file was reading an
/// absent outcome, and the ack test is the one that noticed: it asserted on released output and got
/// `""`. That is lesson #5 in the guards solution note reproducing itself in one edit — a fake more
/// forgiving than the real parser hides exactly the bug it was written to find.
pub(super) fn poll_body(phase: &str, exit_code: &str, stdout: &str, truncated: bool) -> String {
    format!(
        r#"{{"exec_id": "x-1", "phase": "{phase}", "exit_code": {exit_code},
             "signal": null, "stdout": "{stdout}", "stderr": "", "truncated": {truncated},
             "writers_may_be_alive": false}}"#
    )
}

/// `StartResponse`.
pub(super) const STARTED_BODY: &str = r#"{"exec_id": "x-1", "phase": "running"}"#;

/// A `sync` invocation over `dir`, defaulted like the other attached commands.
pub(super) fn sync_command(
    dir: &std::path::Path,
    shape: impl FnOnce(&mut crate::cli::SyncArgs),
) -> Command {
    let mut args = crate::cli::SyncArgs {
        dir: dir.to_path_buf(),
        watch: false,
        full: false,
        timeout: Duration::from_secs(60),
        attach: attach_flags(),
        region: region_flags(),
    };
    shape(&mut args);
    Command::Sync(args)
}

// ── both planes at once ──────────────────────────────────────────────────────

/// A seam that scripts the control plane *and* the daemon behind the launched session.
pub(super) struct SyncSeam {
    pub(super) transport: Arc<ScriptedTransport>,
    pub(super) clock: Arc<YieldingClock>,
    pub(super) daemon: Arc<DaemonScript>,
}

// ── both planes at once ──────────────────────────────────────────────────────

#[expect(
    clippy::disallowed_methods,
    reason = "a fake seam, the test's stand-in for src/seam.rs: it builds its plane or session over a scripted transport"
)]
impl CoreSeam for SyncSeam {
    fn control_plane(&self, region: Region) -> BoxFuture<'_, Result<ControlPlane, Error>> {
        let plane = ControlPlane::with_transport(
            Arc::clone(&self.transport) as Arc<dyn Transport>,
            region,
            Arc::clone(&self.clock) as Arc<dyn Clock>,
        );
        Box::pin(async move { Ok(plane) })
    }

    fn open_sandbox(
        &self,
        region: Region,
        _port: Option<u16>,
    ) -> BoxFuture<'_, Result<Sandbox, Error>> {
        let plane = ControlPlane::with_transport(
            Arc::clone(&self.transport) as Arc<dyn Transport>,
            region,
            Arc::clone(&self.clock) as Arc<dyn Clock>,
        );
        let backend = Arc::clone(&self.daemon) as Arc<dyn microvms_core::session::HttpBackend>;
        Box::pin(
            async move { Ok(Sandbox::with_control_plane(plane).with_session_backend(backend)) },
        )
    }

    fn attach_session(
        &self,
        _region: Region,
        _attach: Attach,
    ) -> BoxFuture<'_, Result<Session, Error>> {
        Box::pin(async move {
            Err(Error::new(
                ErrorKind::Platform,
                "these guards launch rather than attach",
            ))
        })
    }

    fn put_artifact(&self, _uri: &str, _bytes: Vec<u8>) -> BoxFuture<'_, Result<(), Error>> {
        Box::pin(async move { Ok(()) })
    }
}

/// The launch script every sync guard shares: launch succeeds, teardown succeeds.
pub(super) fn sync_launch_script() -> Arc<ScriptedTransport> {
    let transport = Arc::new(ScriptedTransport::new());
    transport
        .answer("RunMicrovm", 200, &microvm_body("PENDING"))
        .answer("GetMicrovm", 200, &microvm_body("RUNNING"))
        .answer(
            "CreateMicrovmAuthToken",
            200,
            r#"{"authToken": {"X-aws-proxy-auth": "opaque"}}"#,
        )
        .answer("TerminateMicrovm", 200, "{}")
        .answer(
            "DeleteMicrovmImage",
            200,
            r#"{"imageIdentifier": "arn:aws:lambda:us-east-1:123456789012:microvm-image:img",
                 "state": "DELETING"}"#,
        );
    transport
}
