// SPDX-License-Identifier: Apache-2.0
//! `microvm attach`: adopting a record this state directory did not write (#66).
//!
//! Seven claims, and each has a way of being wrong that the others would not catch. The record
//! is written only after the probe answers (a dead or mistyped triple leaves the registry
//! untouched); the probe is an *authenticated* read, not `/v1/health`, which is `Auth::Open`
//! and would accept a wrong token; a name held by a different VM is refused before any door
//! with its own row; the same VM under the same name is idempotent; the token stays in the
//! file and out of every rendering; and `--verify-identity` refuses up front without the pair,
//! and with it reaches the handshake step only after the probe and before any write.

#![cfg(test)]

use std::sync::{Arc, Mutex};

use microvms_core::control::ControlPlane;
use microvms_core::prelude::*;
use microvms_core::sandbox::Sandbox;
use microvms_core::session::Session;
use microvms_core::{Error, Region};

use super::support::{DaemonScript, TempDir, dispatch_with, full_infra, region_flags};
use crate::cli::{AttachArgs, Command, RegionFlags};
use crate::commands::{Ctx, Rendered};
use crate::envelope::{Format, Output};
use crate::exit::{CliError, Exit};
use crate::seam::futures_util_shim::BoxFuture;
use crate::seam::{Attach, CoreSeam};

/// The record another machine's `run --keep --vm-name` would have written.
fn adoptable_record(name: &str, id: &str) -> crate::ledger::NameRecord {
    crate::ledger::NameRecord {
        name: name.into(),
        microvm_id: id.into(),
        endpoint: format!("https://{id}.example"),
        agent_token: "tok-from-elsewhere".into(),
        region: "us-west-2".into(),
        at: 1_754_524_800,
        identity_host_seed: None,
        identity_vm_public_key: None,
        egress_posture: None,
    }
}

/// An `attach` invocation, every field defaulted to absent, shaped by the caller.
fn attach_command(shape: impl FnOnce(&mut AttachArgs)) -> Command {
    let mut args = AttachArgs {
        from: None,
        name: None,
        endpoint: None,
        agent_token: None,
        microvm_id: None,
        identity_host_seed: None,
        identity_vm_public_key: None,
        verify_identity: false,
        port: None,
        state_dir: None,
        region: RegionFlags::default(),
    };
    shape(&mut args);
    Command::Attach(args)
}

/// The probe's one request, as the script sees it.
const PROBE_REQUEST: &str = "GET /v1/fs/file?path=%2Fdev%2Fnull";

/// [`ScriptedSessionSeam`] that also keeps the `Attach` it was handed.
///
/// The recording is the point for this command: the probe must use the triple *being
/// registered* — not the record already on disk, not a blank — and only the seam can say
/// which one it was given.
struct RecordingSessionSeam {
    script: Arc<DaemonScript>,
    seen: Mutex<Vec<(Attach, String)>>,
}

#[expect(
    clippy::disallowed_methods,
    reason = "a fake seam, the test's stand-in for src/seam.rs: it builds its plane or session over a scripted transport"
)]
impl CoreSeam for RecordingSessionSeam {
    fn control_plane(&self, _region: Region) -> BoxFuture<'_, Result<ControlPlane, Error>> {
        panic!("attach never opens a control plane directly")
    }

    fn open_sandbox(
        &self,
        _region: Region,
        _port: Option<u16>,
    ) -> BoxFuture<'_, Result<Sandbox, Error>> {
        panic!("attach never opens a sandbox")
    }

    fn attach_session(
        &self,
        region: Region,
        attach: Attach,
    ) -> BoxFuture<'_, Result<Session, Error>> {
        // Attached with the triple it was handed, as the production seam is: core's probe
        // proves the token a session carries, and refuses one attached with another (#270).
        let built = Session::builder(attach.endpoint.clone(), attach.agent_token.clone())
            .with_backend(Arc::clone(&self.script) as Arc<dyn microvms_core::session::HttpBackend>)
            .build();
        self.seen
            .lock()
            .expect("not poisoned")
            .push((attach, region.as_str().to_string()));
        Box::pin(async move { built })
    }

    fn put_artifact(&self, _uri: &str, _bytes: Vec<u8>) -> BoxFuture<'_, Result<(), Error>> {
        panic!("no artifact on this path")
    }
}

/// Runs one `attach` against `script`: the result, stderr, and the attaches the seam saw.
async fn adopt_against(
    script: &Arc<DaemonScript>,
    command: &Command,
) -> (Result<Rendered, CliError>, String, Vec<(Attach, String)>) {
    let seam = RecordingSessionSeam {
        script: Arc::clone(script),
        seen: Mutex::new(Vec::new()),
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
    let (_, stderr) = out.into_streams();
    let seen = seam.seen.lock().expect("not poisoned").clone();
    (result, String::from_utf8_lossy(&stderr).to_string(), seen)
}

/// Every string a caller could see from a rendered success: the data, both texts, stderr.
fn every_rendering(rendered: &Rendered, stderr: &str) -> Vec<String> {
    vec![
        serde_json::to_string(&rendered.data).expect("serializes"),
        rendered.text.clone(),
        rendered.dense_text.clone(),
        stderr.to_string(),
    ]
}

/// **`attach --from` adopts another registry's file: probe with its triple, write it here
/// owner-only under the new name, keep its identity material, and never print the token.**
///
/// The source registry is a second temp dir standing in for the other machine, and the
/// file is exactly what `Names::register` writes there — so the export format under test is
/// the real one, not a fixture of it.
///
/// **Falsification** — observed red three ways while writing it: with the `register` call
/// moved above the probe the file existed before the script was consulted (the ordering
/// assertion in the refused-probe guard below is the one that reads it); with the rename
/// dropped the record landed under `ci` and `lookup("adopted")` was `None`; with the token
/// added to `data` the every-rendering loop named it.
#[tokio::test]
async fn attach_from_a_record_file_probes_with_its_triple_and_registers_it_owner_only() {
    let other = TempDir::new("attach-other-machine");
    let here = TempDir::new("attach-here");
    let mut record = adoptable_record("ci", "mvm-adopted");
    // Carried on an unflagged adopt, verbatim: the pin is what a later `tunnel --name
    // adopted --verify-identity` checks against, and the seed rides with the token it
    // already shares a trust domain with.
    record.identity_host_seed = Some("c2VlZA==".into());
    record.identity_vm_public_key = Some("cGlu".into());
    crate::ledger::Names::new(&other.0)
        .register(&record)
        .expect("the other machine registered it");
    let source = other.0.join("names").join("ci.json");

    let script = DaemonScript::new();
    script.reply(200, "");
    let command = attach_command(|args| {
        args.from = Some(source.clone());
        args.name = Some("adopted".into());
        args.state_dir = Some(here.0.clone());
    });
    let (result, stderr, seen) = adopt_against(&script, &command).await;
    let rendered = result.expect("a live probe adopts the record");

    // One authenticated read, and it went out with the record's triple in the record's
    // region — the caller typed none of them.
    assert_eq!(script.paths(), [PROBE_REQUEST]);
    assert_eq!(seen.len(), 1, "exactly one attach");
    let (attach, region) = &seen[0];
    assert_eq!(attach.endpoint, "https://mvm-adopted.example");
    assert_eq!(attach.agent_token, "tok-from-elsewhere");
    assert_eq!(attach.microvm_id, "mvm-adopted");
    assert_eq!(region, "us-west-2", "the record's region is the default");

    // The envelope: the declared keys, and the token in none of them.
    assert_eq!(rendered.kind, "microvm.attach");
    let declared: std::collections::BTreeSet<&str> = crate::commands::response_type("attach")
        .1
        .iter()
        .copied()
        .collect();
    let carried: std::collections::BTreeSet<&str> =
        rendered.data.keys().map(String::as_str).collect();
    assert_eq!(carried, declared);
    assert_eq!(rendered.data["name"], "adopted");
    assert_eq!(rendered.data["microvmId"], "mvm-adopted");
    assert_eq!(rendered.data["endpoint"], "https://mvm-adopted.example");
    assert_eq!(rendered.data["region"], "us-west-2");
    assert_eq!(rendered.data["verifiedIdentity"], false);
    assert_eq!(rendered.data["replaced"], false);
    let expected_path = here.0.join("names").join("adopted.json");
    assert_eq!(
        rendered.data["statePath"],
        expected_path.display().to_string()
    );
    assert_eq!(
        rendered.text.lines().count(),
        1,
        "one line: {}",
        rendered.text
    );
    for rendering in every_rendering(&rendered, &stderr) {
        assert!(
            !rendering.contains("tok-from-elsewhere"),
            "the agent token is a bearer credential and belongs in the 0600 file, not in a \
             rendering: {rendering}"
        );
    }

    // On disk here, renamed, with the identity pair intact and the token where every
    // `--name` command reads it.
    let written = crate::ledger::Names::new(&here.0)
        .lookup("adopted")
        .expect("registered under the new name");
    assert_eq!(written.name, "adopted");
    assert_eq!(written.microvm_id, "mvm-adopted");
    assert_eq!(written.agent_token, "tok-from-elsewhere");
    assert_eq!(written.region, "us-west-2");
    assert_eq!(written.identity_host_seed.as_deref(), Some("c2VlZA=="));
    assert_eq!(written.identity_vm_public_key.as_deref(), Some("cGlu"));
    assert!(
        written.at >= record.at,
        "`at` is this registry's clock, not the launching machine's"
    );
    assert!(
        crate::ledger::Names::new(&here.0).lookup("ci").is_none(),
        "the record's own name is not registered when --name renames it"
    );
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt as _;
        let mode = std::fs::metadata(&expected_path)
            .expect("the file exists")
            .permissions()
            .mode();
        assert_eq!(
            mode & 0o777,
            0o600,
            "the adopted record carries the token and must be owner-only like a registered one"
        );
    }
    // And the other machine's registry is exactly as it was.
    assert_eq!(
        crate::ledger::Names::new(&other.0).lookup("ci"),
        Some(record),
        "adopting reads the source and never edits it"
    );
}

/// **The explicit spelling probes with the typed triple and registers exactly that.**
///
/// Distinct from the file path because the record is *built* here rather than parsed, so a
/// field written into the wrong slot (`attach_session(a, b, c)`'s hazard) would pass the
/// file guard and fail this one.
#[tokio::test]
async fn attach_by_explicit_triple_probes_with_that_triple_and_registers_it() {
    let here = TempDir::new("attach-explicit");
    let script = DaemonScript::new();
    script.reply(200, "");
    let command = attach_command(|args| {
        args.name = Some("typed".into());
        args.endpoint = Some("https://mvm-typed.example".into());
        args.agent_token = Some("tok-typed".into());
        args.microvm_id = Some("mvm-typed".into());
        args.state_dir = Some(here.0.clone());
        args.region = region_flags();
    });
    let (result, stderr, seen) = adopt_against(&script, &command).await;
    let rendered = result.expect("a live probe registers the triple");

    assert_eq!(script.paths(), [PROBE_REQUEST]);
    let (attach, region) = &seen[0];
    assert_eq!(attach.endpoint, "https://mvm-typed.example");
    assert_eq!(attach.agent_token, "tok-typed");
    assert_eq!(attach.microvm_id, "mvm-typed");
    assert_eq!(
        region, "us-east-1",
        "the flag's region, recorded and minted against"
    );

    let written = crate::ledger::Names::new(&here.0)
        .lookup("typed")
        .expect("registered");
    assert_eq!(written.endpoint, "https://mvm-typed.example");
    assert_eq!(written.agent_token, "tok-typed");
    assert_eq!(written.microvm_id, "mvm-typed");
    assert_eq!(written.region, "us-east-1");
    assert_eq!(written.identity_host_seed, None);
    assert_eq!(rendered.data["region"], "us-east-1");
    assert_eq!(rendered.data["replaced"], false);
    for rendering in every_rendering(&rendered, &stderr) {
        assert!(!rendering.contains("tok-typed"), "{rendering}");
    }
}

/// **A name held here by a different VM is refused with `ERR_NAME_TAKEN`, before any door,
/// and the holder's record is byte-for-byte untouched.** A torn record reads as taken too.
///
/// `PanickingSeam`, so a refusal that came *after* a probe would be a panic rather than a
/// pass — the ordering is the claim, not just the row.
///
/// **Falsification** — observed red with the collision arm changed to compare names only
/// (every same-name attach became `replaced: true` and the holder's file was overwritten
/// with the new VM's triple: the `before == after` read below is what caught it).
#[tokio::test]
async fn attach_refuses_a_name_held_by_a_different_vm_before_any_door_and_writes_nothing() {
    let here = TempDir::new("attach-collision");
    let names = crate::ledger::Names::new(&here.0);
    names
        .register(&adoptable_record("ci", "mvm-holder"))
        .expect("registers");
    let path = here.0.join("names").join("ci.json");
    let before = std::fs::read_to_string(&path).expect("the holder's record");

    let command = attach_command(|args| {
        args.name = Some("ci".into());
        args.endpoint = Some("https://mvm-other.example".into());
        args.agent_token = Some("tok-other".into());
        args.microvm_id = Some("mvm-other".into());
        args.state_dir = Some(here.0.clone());
        args.region = region_flags();
    });
    let (result, _) = dispatch_with(&crate::seam::PanickingSeam, &command, full_infra()).await;
    let failure = result.expect_err("a name is a promise");
    assert_eq!(failure.exit, Exit::NameTaken);
    assert!(
        failure.message.contains("mvm-holder"),
        "{}",
        failure.message
    );
    assert!(failure.message.contains("mvm-other"), "{}", failure.message);
    assert_eq!(failure.data["holder"], "mvm-holder");
    assert!(
        !failure.message.contains("tok-other") && !failure.message.contains("tok-from-elsewhere"),
        "{}",
        failure.message
    );
    assert_eq!(
        std::fs::read_to_string(&path).expect("still there"),
        before,
        "the holder's record must be untouched"
    );

    // The torn record: a process killed mid-register leaves it, its VM may be billing, and
    // adopting over it would point the name at a second VM. Same refusal, same row.
    std::fs::write(here.0.join("names").join("torn.json"), "{\"name\": \"to").expect("writes");
    let torn = attach_command(|args| {
        args.name = Some("torn".into());
        args.endpoint = Some("https://mvm-other.example".into());
        args.agent_token = Some("tok-other".into());
        args.microvm_id = Some("mvm-other".into());
        args.state_dir = Some(here.0.clone());
        args.region = region_flags();
    });
    let (result, _) = dispatch_with(&crate::seam::PanickingSeam, &torn, full_infra()).await;
    let failure = result.expect_err("a torn record is a taken name");
    assert_eq!(failure.exit, Exit::NameTaken);
    assert!(
        failure.message.contains("unreadable"),
        "{}",
        failure.message
    );
}

/// **The same VM under the same name is an idempotent success that refreshes the record.**
///
/// The probe goes out with the *new* triple, not the one on disk: a re-attach exists for the
/// case where the token or endpoint changed (a resume moves the endpoint), so probing the
/// stale record would prove the wrong thing.
#[tokio::test]
async fn re_attaching_the_same_vm_under_its_name_refreshes_the_record_and_reports_replaced() {
    let here = TempDir::new("attach-idempotent");
    crate::ledger::Names::new(&here.0)
        .register(&adoptable_record("ci", "mvm-same"))
        .expect("registers");

    let script = DaemonScript::new();
    script.reply(200, "");
    let command = attach_command(|args| {
        args.name = Some("ci".into());
        args.endpoint = Some("https://mvm-same.resumed.example".into());
        args.agent_token = Some("tok-refreshed".into());
        args.microvm_id = Some("mvm-same".into());
        args.state_dir = Some(here.0.clone());
        args.region = region_flags();
    });
    let (result, _, seen) = adopt_against(&script, &command).await;
    let rendered = result.expect("the same VM under its own name is not a collision");
    assert_eq!(rendered.data["replaced"], true);
    assert_eq!(
        seen[0].0.agent_token, "tok-refreshed",
        "the probe used the new triple"
    );
    assert_eq!(seen[0].0.endpoint, "https://mvm-same.resumed.example");

    let written = crate::ledger::Names::new(&here.0)
        .lookup("ci")
        .expect("still registered");
    assert_eq!(written.microvm_id, "mvm-same");
    assert_eq!(written.agent_token, "tok-refreshed");
    assert_eq!(written.endpoint, "https://mvm-same.resumed.example");
    assert_eq!(
        written.region, "us-east-1",
        "the explicit spelling's region flag is what the refreshed record carries"
    );
}

/// **A probe the daemon refuses writes no record and surfaces the daemon's own class.**
///
/// A 401 — the bare status `auth::require_token` answers a wrong bearer with — is
/// `ERR_CREDENTIALS` with `data.kind: Unauthorized`, exactly as `exec` would report it; a
/// 503 (the token is not installed yet) keeps its own kind. Neither leaves a `names/`
/// directory behind, because `register` is what creates it and `register` never ran.
///
/// **Falsification** — observed red with the `register` call moved above the probe: the
/// 401 still surfaced, and `names/adopted.json` existed beside it, carrying a token the
/// daemon had just refused.
#[tokio::test]
async fn a_probe_the_daemon_refuses_writes_no_record_and_carries_the_daemons_class() {
    for (status, kind, exit) in [
        (
            401,
            microvms_core::WireKind::Unauthorized,
            Exit::Credentials,
        ),
        (
            503,
            microvms_core::WireKind::NotBootstrapped,
            Exit::Retryable,
        ),
    ] {
        let here = TempDir::new("attach-refused-probe");
        let script = DaemonScript::new();
        script.reply(status, "");
        let command = attach_command(|args| {
            args.name = Some("adopted".into());
            args.endpoint = Some("https://mvm-1.example".into());
            args.agent_token = Some("tok-wrong".into());
            args.microvm_id = Some("mvm-1".into());
            args.state_dir = Some(here.0.clone());
            args.region = region_flags();
        });
        let (result, stderr, _) = adopt_against(&script, &command).await;
        let failure = result.expect_err("a refused probe is a refused attach");
        assert_eq!(script.paths(), [PROBE_REQUEST]);
        assert_eq!(
            failure.wire_kind,
            Some(kind),
            "{status}: {}",
            failure.message
        );
        assert_eq!(failure.exit, exit, "{status}");
        assert!(
            !here.0.join("names").exists(),
            "{status}: nothing may be written before the VM answers — found {:?}",
            std::fs::read_dir(here.0.join("names")).map(|entries| entries
                .filter_map(Result::ok)
                .map(|e| e.path())
                .collect::<Vec<_>>())
        );
        assert!(
            !failure.message.contains("tok-wrong") && !stderr.contains("tok-wrong"),
            "a refused token is still a credential: {} / {stderr}",
            failure.message
        );
    }
}

/// **`--verify-identity` on a record with no identity pair is refused before any door,
/// naming the missing field, and writes nothing.**
///
/// Fails closed: the alternative — adopting on the token alone and reporting
/// `verifiedIdentity: false` — is a silent downgrade of the one flag whose whole meaning is
/// "do not adopt unless proven".
#[tokio::test]
async fn verify_identity_without_the_pair_is_refused_before_any_door_and_writes_nothing() {
    let here = TempDir::new("attach-no-pin");
    let command = attach_command(|args| {
        args.name = Some("adopted".into());
        args.endpoint = Some("https://mvm-1.example".into());
        args.agent_token = Some("t".into());
        args.microvm_id = Some("mvm-1".into());
        args.verify_identity = true;
        args.state_dir = Some(here.0.clone());
        args.region = region_flags();
    });
    let (result, _) = dispatch_with(&crate::seam::PanickingSeam, &command, full_infra()).await;
    let failure = result.expect_err("nothing to verify against");
    assert_eq!(failure.exit, Exit::Precondition);
    assert!(
        failure.message.contains("identityVmPublicKey"),
        "{}",
        failure.message
    );
    assert!(
        failure.message.contains("identityHostSeed"),
        "{}",
        failure.message
    );
    assert!(!here.0.join("names").exists(), "nothing written");

    // A file record carrying the pin but not the seed names only what is missing: the KK
    // pattern proves this side too, so the pin alone cannot run the handshake.
    let other = TempDir::new("attach-half-pair");
    let mut record = adoptable_record("ci", "mvm-1");
    record.identity_vm_public_key = Some("cGlu".into());
    crate::ledger::Names::new(&other.0)
        .register(&record)
        .expect("registers");
    let command = attach_command(|args| {
        args.from = Some(other.0.join("names").join("ci.json"));
        args.verify_identity = true;
        args.state_dir = Some(here.0.clone());
    });
    let (result, _) = dispatch_with(&crate::seam::PanickingSeam, &command, full_infra()).await;
    let failure = result.expect_err("half a pair is no pair");
    assert_eq!(failure.exit, Exit::Precondition);
    assert!(
        failure.message.contains("identityHostSeed"),
        "{}",
        failure.message
    );
    assert!(
        !failure.message.contains("identityVmPublicKey"),
        "only the missing half is named: {}",
        failure.message
    );
    assert!(!here.0.join("names").exists(), "nothing written");
}

/// **With the pair present, `--verify-identity` probes first, then reaches the handshake
/// step, and writes nothing until that step passes; without the flag the same record is
/// adopted with `verifiedIdentity: false` and keeps the pair.**
///
/// The scripted session is *direct* — no proxy auth, the shape core documents for a daemon
/// reached without the endpoint proxy — so the handshake step's own refusal is the
/// observable here: it proves the flag routes to that step, after the probe (the script saw
/// exactly one request) and before any write (no `names/`). The handshake itself, a Noise
/// KK exchange in binary WebSocket frames through the real proxy, has no local stand-in and
/// is exercised live in `conformance/run_rs.py`'s `drive_tunnel_identity`, verified pin and
/// tampered pin both.
///
/// **Falsification** — observed red with the identity block moved above the probe (the
/// script saw no request) and with the write moved above it (`names/adopted.json` existed
/// on the refusal).
#[tokio::test]
async fn verify_identity_with_the_pair_probes_first_then_reaches_the_handshake_before_any_write() {
    let identity = microvms_core::identity::LaunchIdentity::generate()
        .expect("keys")
        .keep();
    let other = TempDir::new("attach-pinned-source");
    let mut record = adoptable_record("ci", "mvm-pinned");
    record.identity_host_seed = Some(identity.host_seed_base64());
    record.identity_vm_public_key = Some(identity.vm_public_key_base64());
    crate::ledger::Names::new(&other.0)
        .register(&record)
        .expect("registers");
    let source = other.0.join("names").join("ci.json");

    let here = TempDir::new("attach-pinned-verify");
    let script = DaemonScript::new();
    script.reply(200, "");
    let command = attach_command(|args| {
        args.from = Some(source.clone());
        args.name = Some("adopted".into());
        args.verify_identity = true;
        args.state_dir = Some(here.0.clone());
    });
    let (result, stderr, seen) = adopt_against(&script, &command).await;
    let failure = result.expect_err("a direct session has no proxy to hand the handshake");
    assert_eq!(
        script.paths(),
        [PROBE_REQUEST],
        "the probe ran, and ran first"
    );
    assert_eq!(seen.len(), 1);
    assert_eq!(failure.exit, Exit::Precondition);
    assert!(
        failure.message.contains("identity handshake"),
        "the refusal must come from the identity step, not the probe: {}",
        failure.message
    );
    assert!(
        stderr.contains("verifying mvm-pinned against the pinned VM key"),
        "{stderr}"
    );
    assert!(
        !here.0.join("names").exists(),
        "nothing is written before the handshake passes"
    );

    // The twin: the same record without the flag is adopted on the token alone, says so,
    // and keeps both halves for a later `tunnel --name adopted --verify-identity`.
    let script = DaemonScript::new();
    script.reply(200, "");
    let command = attach_command(|args| {
        args.from = Some(source.clone());
        args.name = Some("adopted".into());
        args.state_dir = Some(here.0.clone());
    });
    let (result, _, _) = adopt_against(&script, &command).await;
    let rendered = result.expect("a token-only adopt of a pinned record");
    assert_eq!(rendered.data["verifiedIdentity"], false);
    let written = crate::ledger::Names::new(&here.0)
        .lookup("adopted")
        .expect("registered");
    assert_eq!(written.identity_host_seed, record.identity_host_seed);
    assert_eq!(
        written.identity_vm_public_key,
        record.identity_vm_public_key
    );
}
