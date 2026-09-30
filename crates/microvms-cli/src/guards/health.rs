// SPDX-License-Identifier: Apache-2.0
//! `health` and `keepalive`, against the scripted daemon in `support`.

#![cfg(test)]

use std::time::Duration;

use super::support::{DaemonScript, against_daemon, attach_flags, region_flags};
use crate::cli::{Command, HealthArgs, KeepaliveArgs};
use crate::exit::Exit;

/// **`microvm health` reports the two identity flags and warns about a degraded one.**
///
/// The three facts no other command reports. `identityDegraded` is the one with a measurement
/// behind it: without `additionalOsCapabilities: ["ALL"]` the hostname and boot_id steps fail with
/// EPERM even as root, and this flag is how that surfaces — asserting it here is what makes the
/// capability requirement impossible to drop by accident.
///
/// Exit 0 despite the warning, and that is deliberate: the daemon's own contract is that a degraded
/// identity "is never a reason for the daemon to refuse to serve". A non-zero exit would tell a
/// caller their VM is broken when what is true is that an operator may want to drain it.
#[tokio::test]
async fn health_reports_the_identity_flags_and_warns_without_failing_on_a_degraded_one() {
    let script = DaemonScript::new();
    script.reply(
        200,
        r#"{"version": "0.1.0", "bootstrapped": true,
             "disk": {"available_bytes": 1024, "reserve_bytes": 4096, "under_pressure": true},
             "identity_degraded": true, "identity_repaired": true,
             "busy": true, "execs": 2}"#,
    );

    let command = Command::Health(HealthArgs {
        attach: attach_flags(),
        region: region_flags(),
    });
    let (result, _, stderr) = against_daemon(&script, &command).await;
    let rendered = result.expect("a degraded identity is reported, not raised");

    assert_eq!(script.paths(), ["GET /v1/health"]);
    assert_eq!(rendered.data["identityDegraded"], true);
    assert_eq!(rendered.data["identityRepaired"], true);
    assert_eq!(rendered.data["bootstrapped"], true);
    assert_eq!(rendered.data["diskUnderPressure"], true);
    assert_eq!(rendered.data["diskAvailableBytes"], 1024);
    // The activity pair an orchestrator polls on a loop, to decide whether to keep the VM
    // alive. Its own poll is the inbound traffic the platform's idle policy measures —
    // which is the only kind that counts, because the endpoint proxy terminates outside
    // the guest and an in-guest keepalive never reaches it.
    assert_eq!(rendered.data["busy"], true);
    assert_eq!(rendered.data["execs"], 2);
    assert_eq!(
        rendered.already_reported, None,
        "the daemon serves a degraded identity by design; failing here would report a working VM \
         as broken"
    );
    // Warnings rather than progress, so `--quiet` cannot buy silence about either.
    assert!(
        stderr.contains("warning: identityDegraded"),
        "a duplicate machine-id is a condition an operator has to be told about: {stderr}"
    );
    assert!(stderr.contains("warning: diskUnderPressure"), "{stderr}");
}

/// `microvm keepalive` polls only unauthenticated health, and ends when the VM goes idle.
///
/// The idle window comes from `GetMicrovm`; this seam refuses the control plane, so the command
/// must fall back to the platform minimum and say so rather than refuse to keep the VM awake.
#[tokio::test(start_paused = true)]
async fn keepalive_polls_health_until_idle_and_names_the_window_it_assumed() {
    let script = DaemonScript::new();
    for busy in [true, true, false] {
        script.reply(
            200,
            &format!(
                r#"{{"version": "0.1.0", "bootstrapped": true, "disk": null,
                     "identity_degraded": false, "identity_repaired": true,
                     "busy": {busy}, "execs": 1}}"#
            ),
        );
    }
    let command = Command::Keepalive(KeepaliveArgs {
        interval: Some(Duration::from_secs(1)),
        while_busy: true,
        for_sec: None,
        idle_window: None,
        attach: attach_flags(),
        region: region_flags(),
    });
    let (result, _, stderr) = against_daemon(&script, &command).await;
    let rendered = result.expect("keeps the VM awake");
    assert_eq!(script.paths(), ["GET /v1/health"; 3]);
    assert_eq!(rendered.kind, "microvm.keepalive");
    assert_eq!(rendered.data["end"], "idle");
    assert_eq!(rendered.data["polls"], 3);
    assert_eq!(rendered.data["lastBusy"], false);
    assert_eq!(rendered.data["idleWindowSec"], 60.0);
    assert!(
        stderr.contains("assuming the platform minimum of 60s"),
        "an assumed window must be stated: {stderr}"
    );
}

/// An interval over half the window is refused before the first poll.
#[tokio::test]
async fn keepalive_refuses_an_interval_that_could_let_the_vm_suspend() {
    let script = DaemonScript::new();
    let command = Command::Keepalive(KeepaliveArgs {
        interval: Some(Duration::from_secs(31)),
        while_busy: false,
        for_sec: None,
        idle_window: None,
        attach: attach_flags(),
        region: region_flags(),
    });
    let (result, _, _) = against_daemon(&script, &command).await;
    let error = result.expect_err("refused");
    assert_eq!(error.exit, Exit::InvalidArg);
    assert!(
        error.message.contains("half the 60s idle window"),
        "{}",
        error.message
    );
    assert!(script.paths().is_empty(), "nothing may be polled first");
}

/// `--for` ends it while busy; an explicit `--idle-window` is used as given.
#[tokio::test(start_paused = true)]
async fn keepalive_for_ends_it_even_while_busy() {
    let script = DaemonScript::new();
    for _ in 0..3 {
        script.reply(
            200,
            r#"{"version": "0.1.0", "bootstrapped": true, "disk": null,
                 "identity_degraded": false, "identity_repaired": true,
                 "busy": true, "execs": 1}"#,
        );
    }
    let command = Command::Keepalive(KeepaliveArgs {
        interval: Some(Duration::from_secs(1)),
        while_busy: true,
        for_sec: Some(Duration::from_millis(2500)),
        idle_window: Some(Duration::from_secs(600)),
        attach: attach_flags(),
        region: region_flags(),
    });
    let (result, _, stderr) = against_daemon(&script, &command).await;
    let rendered = result.expect("runs");
    assert_eq!(rendered.data["end"], "elapsed");
    assert_eq!(rendered.data["polls"], 3);
    assert_eq!(rendered.data["idleWindowSec"], 600.0);
    assert!(!stderr.contains("assuming"), "{stderr}");
}

/// A daemon that has not bootstrapped is a success envelope with a non-zero code.
///
/// Reachable only from inside the VM or over a tunnel — the platform forwards no external traffic
/// until the run hook returns 200 — and a real answer when it happens: the daemon is up and the
/// token is not installed, which needs a different remedy from a dead VM. `null` disk, so the
/// unmeasurable case is covered too: it is distinct from zero, and a monitor that conflated them
/// would page on a missing `statvfs`.
#[tokio::test]
async fn an_unbootstrapped_daemon_is_reported_with_a_non_zero_code_and_a_null_disk() {
    let script = DaemonScript::new();
    script.reply(
        200,
        r#"{"version": "0.1.0", "bootstrapped": false, "disk": null,
             "identity_degraded": false, "identity_repaired": false}"#,
    );
    let command = Command::Health(HealthArgs {
        attach: attach_flags(),
        region: region_flags(),
    });
    let (result, _, _) = against_daemon(&script, &command).await;
    let rendered = result.expect("the daemon answered, so this is a report");
    assert_eq!(rendered.data["bootstrapped"], false);
    assert_eq!(
        rendered.data["diskAvailableBytes"],
        serde_json::Value::Null,
        "unmeasurable is not full, and zero would page a monitor on a missing statvfs"
    );
    assert_eq!(rendered.already_reported, Some(Exit::Platform));
    assert!(
        rendered.text.contains("repair switched off"),
        "identity_repaired: false means opted out, which is not the same as `nothing to do`: {}",
        rendered.text
    );
}
