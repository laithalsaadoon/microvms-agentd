// SPDX-License-Identifier: Apache-2.0
//! The per-VM history, through the shipped handlers.
//!
//! `src/history.rs`'s own tests prove the module; these prove the *wiring* — that the handlers
//! really append, with the platform's values, and that the record survives the command that
//! wrote it. A history module that worked perfectly and was never called would pass every unit
//! test and record nothing.

#![cfg(test)]

use std::sync::Arc;
use std::time::Duration;

use microvms_core::control::transport::Transport;
use microvms_core::control::{Clock, ControlPlane};
use microvms_core::prelude::*;
use microvms_core::sandbox::Sandbox;
use microvms_core::session::Session;
use microvms_core::testing::YieldingClock;
use microvms_core::{Error, ErrorKind, Region};

use super::support::{
    DaemonScript, STARTED_BODY, ScriptedSeam, ScriptedTransport, TempDir, against_daemon,
    attach_flags, dispatch_with, exec_command, full_infra, microvm_body, poll_body, region_flags,
};
use crate::cli::{AttachFlags, Command, HealthArgs, ResumeArgs, TerminateArgs};
use crate::exit::Exit;
use crate::seam::futures_util_shim::BoxFuture;
use crate::seam::{Attach, CoreSeam};

#[tokio::test]
async fn a_terminate_appends_a_terminated_event_that_survives_the_command() {
    let dir = TempDir::new("history-terminate");
    let transport = Arc::new(ScriptedTransport::new());
    transport.answer("TerminateMicrovm", 200, "{}");

    let seam = ScriptedSeam {
        transport: Arc::clone(&transport),
        clock: Arc::new(YieldingClock::default()),
    };
    let command = Command::Terminate(TerminateArgs {
        microvm_id: "mvm-1".into(),
        image_identifier: None,
        image_name: None,
        delete_image: false,
        wait: false,
        wait_sec: None,
        state_dir: Some(dir.0.clone()),
        region: region_flags(),
    });
    let (result, _) = dispatch_with(&seam, &command, full_infra()).await;
    result.expect("the terminate succeeds");

    let read = crate::history::read_events(&dir.0, "mvm-1");
    assert_eq!(read.len(), 1, "the handler must append: {read:?}");
    assert_eq!(read[0]["event"], "terminated");
    assert_eq!(read[0]["terminateAccepted"], true);
    assert_eq!(read[0]["undeleted"], serde_json::json!([]));
    assert_eq!(read[0]["seq"], 0);

    // And a terminate whose call is refused records that verdict rather than a clean one:
    // the failed teardown is exactly the run a caller wants the record of.
    let transport = Arc::new(ScriptedTransport::new());
    transport.answer(
        "TerminateMicrovm",
        409,
        r#"{"message": "ConflictException"}"#,
    );
    let seam = ScriptedSeam {
        transport: Arc::clone(&transport),
        clock: Arc::new(YieldingClock::default()),
    };
    let command = Command::Terminate(TerminateArgs {
        microvm_id: "mvm-1".into(),
        image_identifier: None,
        image_name: None,
        delete_image: false,
        wait: false,
        wait_sec: None,
        state_dir: Some(dir.0.clone()),
        region: region_flags(),
    });
    let (result, _) = dispatch_with(&seam, &command, full_infra()).await;
    result.expect("a failed teardown still reports rather than raising");

    let read = crate::history::read_events(&dir.0, "mvm-1");
    assert_eq!(read.len(), 2, "the second append continues the sequence");
    assert_eq!(
        read[1]["seq"], 1,
        "counting the file is what makes two processes one sequence"
    );
    assert_eq!(read[1]["terminateAccepted"], false);
    assert_eq!(read[1]["undeleted"], serde_json::json!(["mvm-1"]));
}

/// **Issue #160: `terminate --delete-image` reads the image off the kept run's ledger record
/// when `--image-identifier` is not given, and retires that record once the teardown
/// succeeds.**
///
/// `run --keep` leaves a record under the state directory naming the VM, the image, and the
/// image's name (`Ledger::mark_outstanding`), so demanding the identifier back from the
/// caller was asking for information the CLI already held. The derived name also lets the
/// handler name the build log group without `--image-name`, which the explicit path could
/// only do when the caller remembered both flags.
///
/// The record is retired on success because it recorded a kept VM as outstanding, and after
/// this command nothing is: leaving it would make `ls` report a leak that this very command
/// removed, which is the stale-ledger shape issue #159 measured at 68 entries.
#[tokio::test]
async fn a_terminate_with_delete_image_derives_the_image_from_the_kept_runs_ledger() {
    let dir = TempDir::new("terminate-derives-image");
    // What `run --keep` writes: outstanding, with both identifiers and the image's name.
    let mut ledger = crate::ledger::Ledger::new("us-east-1", &dir.0);
    ledger.record_image(
        "arn:aws:lambda:us-east-1:123456789012:microvm-image:img",
        "img",
    );
    ledger.record_microvm("mvm-1");
    ledger.mark_outstanding();
    assert_eq!(
        crate::ledger::read_all(&dir.0).len(),
        1,
        "the staged record exists"
    );

    let transport = Arc::new(ScriptedTransport::new());
    transport
        .answer("TerminateMicrovm", 200, "{}")
        // Core's delete lists the versions first (every page), then deletes the image.
        .answer(
            "ListMicrovmImageVersions",
            200,
            r#"{"items": [{
                 "baseImageArn": "arn:aws:lambda:us-east-1:aws:microvm-image:al2023-1",
                 "buildRoleArn": "arn:aws:iam::123456789012:role/build",
                 "codeArtifact": {"uri": "s3://bucket/img.zip"},
                 "imageArn": "arn:aws:lambda:us-east-1:123456789012:microvm-image:img",
                 "imageVersion": "1", "state": "SUCCESSFUL", "status": "ACTIVE",
                 "createdAt": 1754524800}]}"#,
        )
        .answer(
            "DeleteMicrovmImage",
            200,
            r#"{"imageIdentifier": "arn:aws:lambda:us-east-1:123456789012:microvm-image:img",
             "state": "DELETING"}"#,
        );
    let seam = ScriptedSeam {
        transport: Arc::clone(&transport),
        clock: Arc::new(YieldingClock::default()),
    };
    let command = Command::Terminate(TerminateArgs {
        microvm_id: "mvm-1".into(),
        image_identifier: None,
        image_name: None,
        delete_image: true,
        wait: false,
        wait_sec: None,
        state_dir: Some(dir.0.clone()),
        region: region_flags(),
    });
    let (result, _) = dispatch_with(&seam, &command, full_infra()).await;
    let rendered = result.expect("the derived identifier makes the teardown proceed");
    let envelope = crate::envelope::ok(rendered.kind, rendered.data.clone());
    assert_eq!(
        transport.called("DeleteMicrovmImage"),
        1,
        "the derived image must reach the wire: {:?}",
        transport.calls()
    );
    assert_eq!(
        envelope["data"]["imageIdentifier"],
        "arn:aws:lambda:us-east-1:123456789012:microvm-image:img",
        "the envelope reports the identifier it resolved, not the flag it was not given"
    );
    assert_eq!(
        envelope["data"]["undeletedLogGroups"],
        serde_json::json!(["/aws/lambda-microvms/img"]),
        "the derived name also names the build log group"
    );
    assert_eq!(envelope["data"]["leaked"], serde_json::json!([]));
    // The record is narrowed, not dropped: the VM and image are gone, and the one thing this
    // CLI could only name — the build log group — is what it now lists, exactly as a `run`
    // teardown's record does. `tools/verify-clean.py` reads that name back.
    let after = crate::ledger::read_all(&dir.0);
    assert_eq!(
        after.len(),
        1,
        "the record survives while a group is named: {after:?}"
    );
    assert_eq!(
        after[0]["leaked"],
        serde_json::json!(["/aws/lambda-microvms/img"]),
        "VM and image leave the outstanding list; the named group stays: {after:?}"
    );

    // And a terminate that deletes nothing but the VM leaves the image outstanding, because
    // it still bills: the record narrows to the image alone.
    let dir = TempDir::new("terminate-keeps-image");
    let mut ledger = crate::ledger::Ledger::new("us-east-1", &dir.0);
    ledger.record_image(
        "arn:aws:lambda:us-east-1:123456789012:microvm-image:img",
        "img",
    );
    ledger.record_microvm("mvm-2");
    ledger.mark_outstanding();
    let transport = Arc::new(ScriptedTransport::new());
    transport.answer("TerminateMicrovm", 200, "{}");
    let seam = ScriptedSeam {
        transport: Arc::clone(&transport),
        clock: Arc::new(YieldingClock::default()),
    };
    let command = Command::Terminate(TerminateArgs {
        microvm_id: "mvm-2".into(),
        image_identifier: None,
        image_name: None,
        delete_image: false,
        wait: false,
        wait_sec: None,
        state_dir: Some(dir.0.clone()),
        region: region_flags(),
    });
    let (result, _) = dispatch_with(&seam, &command, full_infra()).await;
    result.expect("a plain terminate succeeds");
    let after = crate::ledger::read_all(&dir.0);
    assert_eq!(
        after[0]["leaked"],
        serde_json::json!(["arn:aws:lambda:us-east-1:123456789012:microvm-image:img"]),
        "the image the caller kept is still outstanding: {after:?}"
    );
}

/// **Issue #160, the override half: an explicit `--image-identifier` naming a different image
/// than the record's deletes that image, names no group for it, and leaves the record's own
/// image outstanding.**
///
/// The review of the first cut found the hole: the record's name was used for whatever
/// identifier was passed, so a deletion of `other-img` named `record-img`'s group and dropped
/// `record-img` — a billing image — from the record. Both halves are pinned here.
#[tokio::test]
async fn an_explicit_identifier_that_differs_from_the_record_leaves_the_records_image_outstanding()
{
    let dir = TempDir::new("terminate-override");
    let record_img = "arn:aws:lambda:us-east-1:123456789012:microvm-image:record-img";
    let other_img = "arn:aws:lambda:us-east-1:123456789012:microvm-image:other-img";
    let mut ledger = crate::ledger::Ledger::new("us-east-1", &dir.0);
    ledger.record_image(record_img, "record-img");
    ledger.record_microvm("mvm-1");
    ledger.mark_outstanding();

    let transport = Arc::new(ScriptedTransport::new());
    transport
        .answer("TerminateMicrovm", 200, "{}")
        .answer(
            "ListMicrovmImageVersions",
            200,
            r#"{"items": [{
                 "baseImageArn": "arn:aws:lambda:us-east-1:aws:microvm-image:al2023-1",
                 "buildRoleArn": "arn:aws:iam::123456789012:role/build",
                 "codeArtifact": {"uri": "s3://bucket/img.zip"},
                 "imageArn": "arn:aws:lambda:us-east-1:123456789012:microvm-image:other-img",
                 "imageVersion": "1", "state": "SUCCESSFUL", "status": "ACTIVE",
                 "createdAt": 1754524800}]}"#,
        )
        .answer(
            "DeleteMicrovmImage",
            200,
            r#"{"imageIdentifier": "arn:aws:lambda:us-east-1:123456789012:microvm-image:other-img",
             "state": "DELETING"}"#,
        );
    let seam = ScriptedSeam {
        transport: Arc::clone(&transport),
        clock: Arc::new(YieldingClock::default()),
    };
    let command = Command::Terminate(TerminateArgs {
        microvm_id: "mvm-1".into(),
        image_identifier: Some(other_img.into()),
        image_name: None,
        delete_image: true,
        wait: false,
        wait_sec: None,
        state_dir: Some(dir.0.clone()),
        region: region_flags(),
    });
    let (result, stderr) = dispatch_with(&seam, &command, full_infra()).await;
    let rendered = result.expect("the explicit identifier is deleted");
    let envelope = crate::envelope::ok(rendered.kind, rendered.data.clone());
    assert_eq!(envelope["data"]["imageIdentifier"], other_img);
    assert_eq!(
        envelope["data"]["undeletedLogGroups"],
        serde_json::json!([]),
        "the record's name belongs to the record's image, not to other-img: {envelope}"
    );
    assert!(
        stderr.contains("could not even be named"),
        "the unnamed group is warned about: {stderr}"
    );
    let after = crate::ledger::read_all(&dir.0);
    assert_eq!(after.len(), 1, "{after:?}");
    assert_eq!(
        after[0]["leaked"],
        serde_json::json!([record_img]),
        "record-img still bills and the record still says so: {after:?}"
    );
}

/// **Two records naming one VM both stop naming it after the terminate.**
///
/// A retried launch leaves two records for one VM; narrowing only the newest leaves the
/// older one reporting a VM that is gone — the stale `ls` shape of issue #159.
#[tokio::test]
async fn every_record_naming_the_vm_is_narrowed_not_only_the_newest() {
    let dir = TempDir::new("terminate-two-records");
    let mut first = crate::ledger::Ledger::new("us-east-1", &dir.0);
    first.record_microvm("mvm-1");
    first.mark_outstanding();
    std::fs::write(
        dir.0.join("9999999999-1.json"),
        serde_json::to_string(&crate::ledger::Record {
            run_id: "9999999999-1".into(),
            region: "us-east-1".into(),
            image_identifier: None,
            image_name: None,
            microvm_id: Some("mvm-1".into()),
            leaked: vec!["mvm-1".into()],
            vm_log_group: None,
        })
        .expect("serializes"),
    )
    .expect("writes");
    assert_eq!(crate::ledger::read_all(&dir.0).len(), 2);

    let transport = Arc::new(ScriptedTransport::new());
    transport.answer("TerminateMicrovm", 200, "{}");
    let seam = ScriptedSeam {
        transport: Arc::clone(&transport),
        clock: Arc::new(YieldingClock::default()),
    };
    let command = Command::Terminate(TerminateArgs {
        microvm_id: "mvm-1".into(),
        image_identifier: None,
        image_name: None,
        delete_image: false,
        wait: false,
        wait_sec: None,
        state_dir: Some(dir.0.clone()),
        region: region_flags(),
    });
    let (result, _) = dispatch_with(&seam, &command, full_infra()).await;
    result.expect("the terminate succeeds");
    assert!(
        crate::ledger::read_all(&dir.0).is_empty(),
        "both records named only the VM, and the VM is gone: {:?}",
        crate::ledger::read_all(&dir.0)
    );
}

/// **Issue #160, the refusal half: with no record naming an image, `--delete-image` alone is
/// still `ERR_INVALID_ARG`, before any AWS call.**
///
/// Moved here from a clap `requires` so the state directory decides. The row is unchanged —
/// the request is refused locally — and so is the zero-call property: a terminate that
/// cannot name what it would delete must not terminate first and ask second.
#[tokio::test]
async fn a_terminate_with_delete_image_and_no_record_is_refused_before_any_call() {
    let dir = TempDir::new("terminate-no-record");
    let transport = Arc::new(ScriptedTransport::new());
    let seam = ScriptedSeam {
        transport: Arc::clone(&transport),
        clock: Arc::new(YieldingClock::default()),
    };
    let command = Command::Terminate(TerminateArgs {
        microvm_id: "mvm-unknown".into(),
        image_identifier: None,
        image_name: None,
        delete_image: true,
        wait: false,
        wait_sec: None,
        state_dir: Some(dir.0.clone()),
        region: region_flags(),
    });
    let (result, _) = dispatch_with(&seam, &command, full_infra()).await;
    let failure = result.expect_err("no record, no identifier: refused");
    assert_eq!(failure.exit, Exit::InvalidArg, "{}", failure.message);
    assert!(
        failure.message.contains("--image-identifier"),
        "the remedy is the flag: {}",
        failure.message
    );
    assert!(
        transport.calls().is_empty(),
        "refused locally means zero calls: {:?}",
        transport.calls()
    );
}

/// **`exec` appends an `exec` event with the daemon's own report, and `--detach` appends one
/// with a null exit code.**
///
/// The null is the honest half: a detached start does not know the outcome, and a record
/// claiming one would be a record this process never observed. The waited exec's fields are
/// read off the daemon's poll body, never off anything the child printed.
///
/// **Guard proof.** Delete the `history.append(Event::Exec {..})` after `wait_and_ack` in
/// `attached::exec` and the first read below is empty while the envelope is byte-identical.
#[tokio::test]
async fn an_exec_appends_the_daemons_report_and_a_detached_one_appends_a_null_code() {
    let dir = TempDir::new("history-exec");
    let script = DaemonScript::new();
    script
        .reply(200, STARTED_BODY)
        .reply(200, &poll_body("exited", "4", "out", true))
        .reply(200, &poll_body("acked", "4", "out", true));
    let command = exec_command(|args| {
        args.exec_id = Some("x-1".into());
        args.attach.state_dir = Some(dir.0.clone());
    });
    let (result, _, _) = against_daemon(&script, &command).await;
    result.expect("the exec completes");

    let read = crate::history::read_events(&dir.0, "mvm-1");
    assert_eq!(read.len(), 1, "{read:?}");
    assert_eq!(read[0]["event"], "exec");
    assert_eq!(read[0]["execId"], "x-1");
    assert_eq!(
        read[0]["exitCode"], 4,
        "the daemon's code, not a success default"
    );
    assert_eq!(read[0]["truncated"], true);
    assert_eq!(read[0]["writersMayBeAlive"], false);

    // The detached shape: started, not waited, so the outcome is honestly unknown.
    let script = DaemonScript::new();
    script.reply(200, STARTED_BODY);
    let command = exec_command(|args| {
        args.detach = true;
        args.exec_id = Some("x-1".into());
        args.attach.state_dir = Some(dir.0.clone());
    });
    let (result, _, _) = against_daemon(&script, &command).await;
    result.expect("a detached start succeeds");

    let read = crate::history::read_events(&dir.0, "mvm-1");
    assert_eq!(read.len(), 2);
    assert_eq!(read[1]["event"], "exec");
    assert_eq!(
        read[1]["exitCode"],
        serde_json::Value::Null,
        "a detached start has no outcome to record: {read:?}"
    );
}

/// **`resume` polls the thawed daemon and lands its hook observations in history.**
/// (issue #80)
///
/// This is the moment suspend-hook firings become visible at all — a frozen VM cannot
/// answer a poll — so the wiring deserves its own guard: delete the post-RUNNING
/// health poll from `lifecycle::resume` and the hook read below is empty while the
/// resume's envelope and exit are byte-identical, which is why nothing else catches it.
#[tokio::test]
async fn a_resume_polls_the_thawed_daemon_and_lands_its_hook_observations() {
    /// `ScriptedSeam` for the control plane, `DaemonScript` for the attach the
    /// post-RUNNING poll makes — `resume` is the one lifecycle command that uses both.
    struct ResumeSeam {
        transport: Arc<ScriptedTransport>,
        clock: Arc<YieldingClock>,
        daemon: Arc<DaemonScript>,
    }
    #[expect(
        clippy::disallowed_methods,
        reason = "a fake seam, the test's stand-in for src/seam.rs: it builds its plane or session over a scripted transport"
    )]
    impl CoreSeam for ResumeSeam {
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
            _region: Region,
            _port: Option<u16>,
        ) -> BoxFuture<'_, Result<Sandbox, Error>> {
            Box::pin(async move { Err(Error::new(ErrorKind::Platform, "resume never launches")) })
        }
        fn attach_session(
            &self,
            _region: Region,
            _attach: Attach,
        ) -> BoxFuture<'_, Result<Session, Error>> {
            let backend = Arc::clone(&self.daemon) as Arc<dyn microvms_core::session::HttpBackend>;
            let built = Session::builder("https://mvm-1.example", "")
                .with_backend(backend)
                .build();
            Box::pin(async move { built })
        }
        fn put_artifact(&self, _uri: &str, _bytes: Vec<u8>) -> BoxFuture<'_, Result<(), Error>> {
            Box::pin(async move { Ok(()) })
        }
    }

    let dir = TempDir::new("history-resume-hooks");
    let transport = Arc::new(ScriptedTransport::new());
    transport.answer("ResumeMicrovm", 200, "{}").answer(
        "GetMicrovm",
        200,
        &microvm_body("RUNNING"),
    );
    let daemon = DaemonScript::new();
    daemon.reply(
        200,
        r#"{"version": "0.1.0", "bootstrapped": true, "disk": null,
             "identity_degraded": false, "identity_repaired": true,
             "hooks": [{"hook": "suspend", "fired_at": 1756500500},
                       {"hook": "resume", "fired_at": 1756500600}],
             "hooks_dropped": 0}"#,
    );
    let seam = ResumeSeam {
        transport,
        clock: Arc::new(YieldingClock::default()),
        daemon: Arc::clone(&daemon),
    };
    let command = Command::Resume(ResumeArgs {
        microvm_id: "mvm-abc123".into(),
        timeout: Duration::from_secs(30),
        state_dir: Some(dir.0.clone()),
        region: region_flags(),
    });
    let (result, _) = dispatch_with(&seam, &command, full_infra()).await;
    result.expect("the resume succeeds");

    assert_eq!(
        daemon.paths(),
        ["GET /v1/health"],
        "one poll, after RUNNING"
    );
    let read = crate::history::read_events(&dir.0, "mvm-abc123");
    let hooks: Vec<(&str, u64)> = read
        .iter()
        .filter(|event| event["event"] == "hookObserved")
        .map(|event| {
            (
                event["hook"].as_str().expect("a hook"),
                event["firedAt"].as_u64().expect("an epoch"),
            )
        })
        .collect();
    assert_eq!(
        hooks,
        [("suspend", 1_756_500_500), ("resume", 1_756_500_600)],
        "the thawed daemon's observations, verbatim: {read:?}"
    );
    // And the `resumed` event still precedes them — the poll is after RUNNING.
    assert_eq!(read[0]["event"], "resumed");
}

/// **`microvm health` carries each hook handler's outcome and the identity steps.**
/// (#198, #205) A hook without a handler keeps its two-key shape; one with a handler
/// gains a camelCase `handler`; `identitySteps` names the failed step.
#[tokio::test]
async fn health_reports_handler_outcomes_and_identity_steps() {
    let dir = TempDir::new("health-handlers");
    let script = DaemonScript::new();
    script.reply(
        200,
        r#"{"version": "0.1.0", "bootstrapped": true, "disk": null,
             "identity_degraded": true, "identity_repaired": true,
             "hooks": [{"hook": "run", "fired_at": 1},
                       {"hook": "suspend", "fired_at": 2,
                        "handler": {"exit_code": 1, "signal": null, "timed_out": false,
                                    "duration_ms": 40}}],
             "hooks_dropped": 0,
             "identity_steps": [{"name": "boot-id", "outcome": "failed", "error": "EPERM"}]}"#,
    );
    let (result, _, _) = against_daemon(
        &script,
        &Command::Health(HealthArgs {
            attach: AttachFlags {
                state_dir: Some(dir.0.clone()),
                ..attach_flags()
            },
            region: region_flags(),
        }),
    )
    .await;
    let rendered = result.expect("health answers");
    assert_eq!(
        rendered.data["hooks"],
        serde_json::json!([
            {"hook": "run", "firedAt": 1},
            {"hook": "suspend", "firedAt": 2, "handler": {
                "exitCode": 1, "signal": null, "timedOut": false, "durationMs": 40,
                "error": null, "succeeded": false}},
        ])
    );
    assert_eq!(
        rendered.data["identitySteps"],
        serde_json::json!([{"name": "boot-id", "outcome": "failed", "error": "EPERM"}])
    );
}

/// **`microvm health` lands the daemon's hook observations in the VM's history,
/// deduplicated on the (hook, firedAt) pair.** (issue #80)
///
/// Three polls prove three claims. The first appends the body's two observations with
/// the daemon's own values — never anything the guest printed, which is the forgery
/// property's letter; the daemon-reported caveat (an in-guest caller can forge
/// *additional* firings by posting the unauthenticated hook paths) is documented in
/// `history.rs` and does not change what this asserts, because the values on file are
/// still exactly what the daemon reported. The second poll repeats the identical body
/// and appends nothing. The third carries one new firing and appends exactly it, with
/// `seq` continuing the one sequence.
///
/// **Guard proof.** Delete the `append_unseen_hooks` call from `attached::health` and
/// the first read below is empty while the envelope still carries `hooks`; drop the
/// dedup and the second read counts four. The dedup half was broken exactly so on
/// 2026-08-30 (in `history.rs`'s own falsification), failed as stated, restored.
#[tokio::test]
async fn a_health_poll_lands_hook_observations_in_history_and_a_repeat_appends_nothing() {
    let dir = TempDir::new("history-hooks");
    let health_command = || {
        Command::Health(HealthArgs {
            attach: AttachFlags {
                state_dir: Some(dir.0.clone()),
                ..attach_flags()
            },
            region: region_flags(),
        })
    };
    let body_two_hooks = r#"{"version": "0.1.0", "bootstrapped": true, "disk": null,
             "identity_degraded": false, "identity_repaired": true,
             "hooks": [{"hook": "validate", "fired_at": 1756500000},
                       {"hook": "run", "fired_at": 1756500100}],
             "hooks_dropped": 0}"#;

    // First poll: both observations land, with the daemon's values.
    let script = DaemonScript::new();
    script.reply(200, body_two_hooks);
    let (result, _, _) = against_daemon(&script, &health_command()).await;
    let rendered = result.expect("health answers");
    assert_eq!(
        rendered.data["hooks"],
        serde_json::json!([
            {"hook": "validate", "firedAt": 1756500000_u64},
            {"hook": "run", "firedAt": 1756500100_u64},
        ]),
        "the envelope carries the observations, camelCase like its neighbours"
    );
    assert_eq!(rendered.data["hooksDropped"], 0);
    let read = crate::history::read_events(&dir.0, "mvm-1");
    assert_eq!(read.len(), 2, "{read:?}");
    assert_eq!(read[0]["event"], "hookObserved");
    assert_eq!(read[0]["hook"], "validate");
    assert_eq!(read[0]["firedAt"], 1_756_500_000_u64);
    assert_eq!(read[1]["hook"], "run");

    // Second poll, identical body: dedup proven — nothing appends.
    let script = DaemonScript::new();
    script.reply(200, body_two_hooks);
    let (result, _, _) = against_daemon(&script, &health_command()).await;
    result.expect("health answers again");
    assert_eq!(
        crate::history::read_events(&dir.0, "mvm-1").len(),
        2,
        "a repeat poll must append nothing"
    );

    // Third poll, one new firing: exactly it appends, and seq continues.
    let script = DaemonScript::new();
    script.reply(
        200,
        r#"{"version": "0.1.0", "bootstrapped": true, "disk": null,
             "identity_degraded": false, "identity_repaired": true,
             "hooks": [{"hook": "validate", "fired_at": 1756500000},
                       {"hook": "run", "fired_at": 1756500100},
                       {"hook": "suspend", "fired_at": 1756500900}],
             "hooks_dropped": 0}"#,
    );
    let (result, _, _) = against_daemon(&script, &health_command()).await;
    result.expect("health answers a third time");
    let read = crate::history::read_events(&dir.0, "mvm-1");
    assert_eq!(read.len(), 3, "{read:?}");
    assert_eq!(read[2]["hook"], "suspend");
    assert_eq!(read[2]["firedAt"], 1_756_500_900_u64);
    assert_eq!(read[2]["seq"], 2, "one monotonic sequence across the polls");
}

/// **`terminate --wait-sec` waits for TERMINATED on its own, for at most that long (#267).**
///
/// Without `--wait`: the flag implies it, so a VM the platform reports TERMINATED is reported
/// that way. Against a VM that stays TERMINATING, the wait ends at the given deadline rather
/// than the core's lifecycle default: the scripted clock advances by each five-second poll, so
/// a ten-second wait is three reads where the default would be sixty-one.
///
/// **Falsification**: drop `args.wait_sec` from the handler's wait (`--wait` alone decides) and
/// neither case waits: the first reports TERMINATING and the second reads nothing.
#[tokio::test]
async fn a_terminate_with_wait_sec_waits_for_terminated_up_to_that_deadline() {
    let terminate = |state_dir: std::path::PathBuf| {
        Command::Terminate(TerminateArgs {
            microvm_id: "mvm-abc123".into(),
            image_identifier: None,
            image_name: None,
            delete_image: false,
            wait: false,
            wait_sec: Some(Duration::from_secs(10)),
            state_dir: Some(state_dir),
            region: region_flags(),
        })
    };

    let dir = TempDir::new("terminate-wait-sec");
    let transport = Arc::new(ScriptedTransport::new());
    transport.answer("TerminateMicrovm", 200, "{}").answer(
        "GetMicrovm",
        200,
        &microvm_body("TERMINATED"),
    );
    let seam = ScriptedSeam {
        transport: Arc::clone(&transport),
        clock: Arc::new(YieldingClock::default()),
    };
    let (result, _) = dispatch_with(&seam, &terminate(dir.0.clone()), full_infra()).await;
    let rendered = result.expect("the terminate succeeds");
    assert_eq!(
        rendered.data["state"], "TERMINATED",
        "--wait-sec waits by itself"
    );

    let transport = Arc::new(ScriptedTransport::new());
    transport.answer("TerminateMicrovm", 200, "{}").answer(
        "GetMicrovm",
        200,
        &microvm_body("TERMINATING"),
    );
    let seam = ScriptedSeam {
        transport: Arc::clone(&transport),
        clock: Arc::new(YieldingClock::default()),
    };
    let (result, stderr) = dispatch_with(&seam, &terminate(dir.0.clone()), full_infra()).await;
    let rendered = result.expect("a missed deadline is a warning, not a failure");
    assert_eq!(rendered.data["state"], "TERMINATING");
    assert_eq!(
        transport.called("GetMicrovm"),
        3,
        "the wait reads until its own deadline, not the core default's"
    );
    assert!(stderr.contains("did not reach TERMINATED"), "{stderr}");
}
