// SPDX-License-Identifier: Apache-2.0
//! `Session::run_to_completion` against a real VM (#222, BIND-6..10).
//!
//! Invoked by `conformance/run_rs.py` (`drive_run_to_completion`) against the suite's kept VM,
//! whose attach coordinates arrive in `MICROVM_LIVE_ATTACH` as a JSON object
//! (`{"microvmId", "endpoint", "agentToken", "region"}`). Launches nothing and terminates
//! nothing: the VM is the suite's. Each test leaves no exec un-acked.
//!
//! The deadline tests rely on two measured daemon constants: `kill_grace` is ten seconds
//! (`agentd/src/config.rs`), so a command that ignores SIGTERM survives the daemon's own
//! deadline for ten seconds before SIGKILL, which is the window a client deadline lands in.

use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use microvms_core::prelude::*;
use microvms_core::protocol::exec::StartRequest;
use microvms_core::region::Region;
use microvms_core::session::{
    CompletionOptions, ExecEvent, KillAnswer, OutputFlow, OutputSink, Session, mint_exec_id,
};

/// The suite's VM, from `MICROVM_LIVE_ATTACH`.
async fn attached() -> Session {
    let raw = std::env::var("MICROVM_LIVE_ATTACH")
        .expect("conformance must supply MICROVM_LIVE_ATTACH for the kept VM");
    let attach: serde_json::Value = serde_json::from_str(&raw).expect("attach JSON");
    let field = |name: &str| {
        attach[name]
            .as_str()
            .unwrap_or_else(|| panic!("MICROVM_LIVE_ATTACH has no {name}"))
            .to_string()
    };
    let region: Region = field("region").parse().expect("a supported region");
    Session::attach(
        region,
        field("microvmId"),
        field("endpoint"),
        field("agentToken"),
        None,
        None,
    )
    .await
    .expect("attach")
}

fn bash(script: &str, timeout_sec: Option<f64>) -> StartRequest {
    StartRequest {
        exec_id: mint_exec_id(),
        command: vec!["bash".into(), "-c".into(), script.into()],
        shell: false.into(),
        cwd: None,
        env: Default::default(),
        user: None,
        group: None,
        timeout_sec,
        stdin: false,
        reap_group_on_exit: false,
        inherit_image_env: false,
    }
}

fn collecting() -> (OutputSink, Arc<Mutex<Vec<u8>>>) {
    let seen = Arc::new(Mutex::new(Vec::new()));
    let into = Arc::clone(&seen);
    let sink: OutputSink = Box::new(move |event| {
        if let ExecEvent::Output { data, .. } = event {
            into.lock().expect("unpoisoned").extend_from_slice(&data);
        }
        Box::pin(std::future::ready(std::ops::ControlFlow::Continue(()))) as OutputFlow
    });
    (sink, seen)
}

fn grace(seconds: u64) -> CompletionOptions {
    CompletionOptions {
        client_grace: Duration::from_secs(seconds),
        ..CompletionOptions::default()
    }
}

/// **BIND-8, BIND-6.** A streamed bash command (with `pipefail`, which dash refuses) returns
/// its own exit code and output: the callback saw the output, the ack carried it, no deadline.
#[tokio::test]
#[ignore = "needs the conformance suite's kept VM in MICROVM_LIVE_ATTACH"]
async fn a_streamed_bash_command_returns_its_own_exit_code_and_output() {
    let session = attached().await;
    let (sink, seen) = collecting();
    let script = "set -o pipefail; echo out-one; echo err-one >&2; echo out-two | cat; exit 3";
    let result = session
        .run_to_completion(bash(script, Some(60.0)), grace(60), Some(sink))
        .await
        .expect("a result");
    eprintln!(
        "exec={} phase={:?} posix={:?} notes={:?}",
        result.exec_id,
        result.phase,
        result.posix_exit_code(),
        result.notes()
    );
    assert_eq!(result.stdout(), "out-one\nout-two\n");
    assert_eq!(result.stderr(), "err-one\n");
    assert_eq!(result.posix_exit_code(), Some(3));
    assert!(result.notes().is_empty(), "{:?}", result.notes());
    assert!(result.client_deadline.is_none());
    let streamed = String::from_utf8_lossy(&seen.lock().expect("unpoisoned")).into_owned();
    assert!(
        streamed.contains("out-one") && streamed.contains("err-one"),
        "the callback saw {streamed:?}"
    );
}

/// **BIND-6, BIND-7.** The daemon's own deadline ends the command: 124, with the deadline's
/// note, and no client kill.
#[tokio::test]
#[ignore = "needs the conformance suite's kept VM in MICROVM_LIVE_ATTACH"]
async fn a_daemon_deadline_reports_124_with_a_note() {
    let session = attached().await;
    let started = Instant::now();
    let result = session
        .run_to_completion(bash("echo started; sleep 60", Some(2.0)), grace(60), None)
        .await
        .expect("a result");
    eprintln!(
        "exec={} after={:?} outcome={:?} notes={:?}",
        result.exec_id,
        started.elapsed(),
        result.outcome,
        result.notes()
    );
    assert!(
        result.outcome.as_ref().is_some_and(|o| o.timed_out),
        "{result:?}"
    );
    assert_eq!(result.posix_exit_code(), Some(124));
    assert!(result.client_deadline.is_none(), "{result:?}");
    assert!(
        result
            .notes()
            .iter()
            .any(|note| note.contains("timeout_sec")),
        "{:?}",
        result.notes()
    );
    assert!(started.elapsed() < Duration::from_secs(40));
}

/// **BIND-9.** A command that ignores SIGTERM outlives the daemon's deadline for its ten-second
/// `kill_grace`. With `timeout_sec` 1 and a six-second client grace the client deadline (7 s)
/// lands inside that window: the client kills a live group, waits, and collects the result
/// once the escalation's SIGKILL lands, about eleven seconds in.
#[tokio::test]
#[ignore = "needs the conformance suite's kept VM in MICROVM_LIVE_ATTACH"]
async fn a_client_deadline_kills_a_command_that_ignores_sigterm() {
    let session = attached().await;
    let (sink, _) = collecting();
    let started = Instant::now();
    let result = session
        .run_to_completion(
            bash("trap '' TERM; echo ignoring; sleep 60", Some(1.0)),
            grace(6),
            Some(sink),
        )
        .await
        .expect("a result");
    eprintln!(
        "exec={} after={:?} client={:?} outcome={:?} notes={:?}",
        result.exec_id,
        started.elapsed(),
        result.client_deadline,
        result.outcome,
        result.notes()
    );
    let deadline = result
        .client_deadline
        .as_ref()
        .expect("the client deadline fired");
    assert_eq!(deadline.after, Duration::from_secs(7));
    assert_eq!(deadline.kill, KillAnswer::Signalled, "{deadline:?}");
    assert!(!result.synthesized(), "{result:?}");
    assert_eq!(result.posix_exit_code(), Some(124));
    assert_eq!(result.stdout(), "ignoring\n");
    assert!(
        result
            .notes()
            .iter()
            .any(|note| note.contains("client deadline of 7s")),
        "{:?}",
        result.notes()
    );
}

/// **BIND-10.** Nothing comes back within the grace after a successful kill, so the result is
/// synthesized.
///
/// Measured 2026-09-24 (us-east-1): the daemon answers `POST /kill` only once the group is
/// gone, SIGKILLing it after its ten-second `kill_grace`. So a group the kill ends is always
/// collectable right after, and the first version of this test, which expected a short grace
/// to lose the race with the escalation, got the real result instead. What outlives the kill
/// is a process in its own session holding the output pipes: the daemon then waits its
/// five-second output linger before the exec reads `exited`. Here the group ignores SIGTERM,
/// the client deadline (3 s) sends the kill, the kill returns when the deadline's SIGKILL lands
/// (about 11 s), and the two-second grace ends inside the linger (to about 16 s). The exec is
/// then collected here so nothing is left un-acked.
#[tokio::test]
#[ignore = "needs the conformance suite's kept VM in MICROVM_LIVE_ATTACH"]
async fn a_client_grace_shorter_than_the_pipe_linger_synthesizes_124() {
    let session = attached().await;
    // `set -m` rather than `setsid`: bash's job control puts the background job in its own
    // process group, outside the one the daemon signals, and al2023-minimal ships no
    // util-linux. The echo is the evidence that the two groups differ.
    let request = bash(
        "trap '' TERM; set -m; sleep 30 & set +m; \
         echo \"grandchild pgrp $(cut -d' ' -f5 /proc/$!/stat) shell pgrp $(cut -d' ' -f5 /proc/$$/stat)\"; \
         sleep 60",
        Some(1.0),
    );
    let exec_id = request.exec_id.clone();
    let started = Instant::now();
    let result = session
        .run_to_completion(request, grace(2), None)
        .await
        .expect("a result");
    eprintln!(
        "exec={} after={:?} client={:?} notes={:?}",
        result.exec_id,
        started.elapsed(),
        result.client_deadline,
        result.notes()
    );
    let collected = session
        .exec(&exec_id)
        .wait_and_ack(Duration::from_secs(60))
        .await;
    eprintln!("collected afterwards: {collected:?}");
    assert!(result.synthesized(), "{result:?}");
    assert_eq!(
        result.client_deadline.as_ref().map(|d| &d.kill),
        Some(&KillAnswer::Signalled),
        "the kill reached a live group: {result:?}"
    );
    assert_eq!(result.posix_exit_code(), Some(124));
    assert!(result.outcome.is_none());
    assert!(
        result
            .notes()
            .iter()
            .any(|note| note.contains("synthesized")),
        "{:?}",
        result.notes()
    );
    let collected = collected.expect("the exec exits after the linger and is acked");
    eprintln!("collected stdout: {:?}", collected.stdout());
    assert!(
        collected
            .outcome
            .as_ref()
            .is_some_and(|o| o.writers_may_be_alive),
        "the setsid grandchild held the pipes past the linger: {collected:?}"
    );
}
