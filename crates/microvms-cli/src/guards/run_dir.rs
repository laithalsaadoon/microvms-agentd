// SPDX-License-Identifier: Apache-2.0
//! `run <DIR>`: the sync round trip (issue #72).
//!
//! These guards script *both* planes: `ScriptedTransport` answers the control-plane launch
//! and `DaemonScript` answers the session the launch builds, joined by
//! `Sandbox::with_session_backend` — the daemon-side half of the seam
//! `with_control_plane` opens. That is what lets one test drive pack → upload → exec(cwd)
//! → download → extract as the shipped `run` handler actually sequences them.

#![cfg(test)]

use std::sync::Arc;

use microvms_core::testing::YieldingClock;

use super::support::{
    ConfigFile, DaemonScript, RefusingSeam, STARTED_BODY, SyncSeam, TempDir, dispatch_with,
    full_infra, no_config, poll_body, run_args_for_image, sync_launch_script,
};
use crate::cli::{Command, RunArgs};
use crate::exit::Exit;

/// A project tree under a temp dir, removed on drop.
struct ProjectDir(std::path::PathBuf);

impl ProjectDir {
    fn new(label: &str) -> Self {
        let path = std::env::temp_dir().join(format!(
            "microvm-guard-sync-{label}-{}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        std::fs::create_dir_all(&path).expect("creates");
        Self(path)
    }
}

impl Drop for ProjectDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

/// The tar bytes a scripted daemon hands back from `GET /v1/fs/tar`.
fn daemon_archive(members: &[(&str, &[u8])]) -> Vec<u8> {
    let mut builder = tar::Builder::new(Vec::new());
    for (name, body) in members {
        let mut header = tar::Header::new_gnu();
        header.set_size(body.len() as u64);
        header.set_mode(0o644);
        header.set_cksum();
        builder
            .append_data(&mut header, name, *body)
            .expect("appends");
    }
    builder.into_inner().expect("finishes")
}

/// `run <DIR> --image … --exec …` args over a synced project.
fn sync_run_args(dir: &ProjectDir, state_dir: std::path::PathBuf) -> RunArgs {
    let mut args = run_args_for_image(
        "arn:aws:lambda:us-east-1:123456789012:microvm-image/img",
        state_dir,
    );
    args.binary = Some(dir.0.clone());
    args.exec = Some("make test".into());
    args
}

/// **`run <DIR>` uploads the packed tree, runs the exec in it, and brings back exactly
/// the glob-matched artifacts.**
///
/// One guard for the whole round trip, asserted on the daemon's recorded requests and the
/// local filesystem: the uploaded tar contains the project file and never `.git`; the
/// exec's start body carries `cwd: /workspace`; the downloaded archive's glob-matched
/// member lands in DIR and the unmatched one does not; and the envelope's `sync` key
/// reports all of it.
///
/// **Falsification** — three breaks, each made on 2026-08-28, each failing exactly where
/// stated, each restored: drop the `cwd` from `launch_and_exec`'s `StartSpec` and the
/// start-body assertion reads null; skip `.git` filtering in `sync::collect` and the
/// uploaded-bytes assertion finds the loose object; extract without the glob set and the
/// unmatched-member assertion finds `secrets.env` on disk.
#[tokio::test]
async fn run_dir_uploads_the_tree_execs_in_it_and_brings_back_matched_artifacts() {
    let state = TempDir::new("sync-round-trip");
    let project = ProjectDir::new("round-trip");
    std::fs::create_dir_all(project.0.join(".git")).expect("git dir");
    std::fs::write(project.0.join(".git/loose-object"), b"never uploaded").expect("blob");
    std::fs::write(project.0.join("main.py"), b"print('hi')").expect("source");
    let file = ConfigFile::new("sync-artifacts", "artifacts = [\"dist/**\"]\n");

    let daemon = DaemonScript::new();
    daemon
        // wait_until_ready's health poll.
        .reply(
            200,
            r#"{"version": "0.1.0", "bootstrapped": true, "disk": null,
                        "identity_degraded": false, "identity_repaired": true}"#,
        )
        // PUT /v1/fs/tar — the upload.
        .reply(200, "{}")
        // The exec: started, then exited 0, then acked.
        .reply(200, STARTED_BODY)
        .reply(200, &poll_body("exited", "0", "", false))
        .reply(200, &poll_body("acked", "0", "", false));
    // GET /v1/fs/tar answers raw tar bytes, not JSON, so it is queued as a raw reply.
    daemon.replies.lock().expect("not poisoned").push_back((
        200,
        daemon_archive(&[
            ("dist/report.txt", b"selected"),
            ("secrets.env", b"never asked for"),
        ]),
    ));
    // The teardown's own health poll (issue #80), carrying the daemon's hook log. The
    // hooks below are what a real launch reports: validate and ready fired in the
    // snapshot VM, run fired in this one.
    daemon.reply(
        200,
        r#"{"version": "0.1.0", "bootstrapped": true, "disk": null,
                    "identity_degraded": false, "identity_repaired": true,
                    "hooks": [{"hook": "validate", "fired_at": 1756500000},
                              {"hook": "run", "fired_at": 1756500100}],
                    "hooks_dropped": 0}"#,
    );

    let seam = SyncSeam {
        transport: sync_launch_script(),
        clock: Arc::new(YieldingClock::default()),
        daemon: Arc::clone(&daemon),
    };
    let mut args = sync_run_args(&project, state.0.clone());
    args.config = crate::cli::ConfigFlags {
        config: Some(file.0.clone()),
        no_config: false,
    };

    let (result, _) = dispatch_with(&seam, &Command::Run(Box::new(args)), full_infra()).await;
    let rendered = result.expect("the sync run succeeds");

    // The upload: PUT /v1/fs/tar carrying the project, not the repository.
    let requests = daemon.requests();
    let upload = requests
        .iter()
        .find(|request| request.method == "PUT" && request.path.starts_with("/v1/fs/tar"))
        .expect("an upload went out");
    assert!(
        upload.path.contains("workspace"),
        "the tree lands in the constant workdir: {}",
        upload.path
    );
    let uploaded = upload.body.clone();
    let mut names = Vec::new();
    for entry in tar::Archive::new(uploaded.as_slice())
        .entries()
        .expect("parses")
    {
        names.push(
            entry
                .expect("a member")
                .path()
                .expect("a path")
                .display()
                .to_string(),
        );
    }
    assert!(names.iter().any(|name| name == "main.py"), "{names:?}");
    assert!(
        !names.iter().any(|name| name.contains(".git")),
        "the repository's object store must not ride along: {names:?}"
    );

    // The exec runs *in* the synced tree.
    let start = requests
        .iter()
        .find(|request| request.path == "/v1/exec/start")
        .expect("a start went out");
    let body: serde_json::Value =
        serde_json::from_slice(&start.body).expect("the start body is JSON");
    assert_eq!(
        body["cwd"], "/workspace",
        "the exec must run in the synced tree, not the image's WORKDIR: {body}"
    );

    // The glob-matched artifact came back; the unmatched member did not.
    assert_eq!(
        std::fs::read_to_string(project.0.join("dist/report.txt")).expect("landed"),
        "selected"
    );
    assert!(
        !project.0.join("secrets.env").exists(),
        "a member no glob asked for must never touch the local disk"
    );

    // And the envelope says what happened.
    assert_eq!(rendered.data["sync"]["workdir"], "/workspace");
    assert_eq!(
        rendered.data["sync"]["artifacts"][0]["path"],
        "dist/report.txt"
    );
    // One member: `main.py`. The `.git` tree was skipped whole.
    assert_eq!(rendered.data["sync"]["uploadedMembers"], 1);

    // The teardown's health poll landed the daemon's hook observations in the VM's
    // history, with the daemon's own values (issue #80). The daemon lane is where the
    // values are proven; this is the wiring — a `run` that never polled would leave no
    // hookObserved lines while every other assertion above stayed green.
    let events = crate::history::read_events(&state.0, "mvm-abc123");
    let hooks: Vec<(&str, u64)> = events
        .iter()
        .filter(|event| event["event"] == "hookObserved")
        .map(|event| {
            (
                event["hook"].as_str().expect("a hook name"),
                event["firedAt"].as_u64().expect("an epoch"),
            )
        })
        .collect();
    assert_eq!(
        hooks,
        [("validate", 1_756_500_000), ("run", 1_756_500_100)],
        "the daemon's observations, verbatim: {events:?}"
    );
}

/// **An exec that fails still gets its artifacts downloaded — CI wants the logs.**
///
/// The run exits `ERR_EXEC_FAILED` exactly as a plain failing exec does, and the download
/// happens anyway: the report a failing test run produces is the artifact the caller most
/// wants, and a sync that only worked on green runs would be a sync nobody could debug
/// with.
///
/// **Falsification** — move the download inside an `exec_exit_code == 0` arm and the
/// artifact assertion finds nothing on disk while the exit assertion still reads 13. Done
/// on 2026-08-28; failed as stated; restored.
#[tokio::test]
async fn a_failing_exec_still_brings_the_artifacts_back() {
    let state = TempDir::new("sync-exec-fails");
    let project = ProjectDir::new("exec-fails");
    std::fs::write(project.0.join("main.py"), b"assert False").expect("source");
    let file = ConfigFile::new("sync-fail-artifacts", "artifacts = [\"report/**\"]\n");

    let daemon = DaemonScript::new();
    daemon
        .reply(
            200,
            r#"{"version": "0.1.0", "bootstrapped": true, "disk": null,
                        "identity_degraded": false, "identity_repaired": true}"#,
        )
        .reply(200, "{}")
        .reply(200, STARTED_BODY)
        .reply(200, &poll_body("exited", "1", "", false))
        .reply(200, &poll_body("acked", "1", "", false));
    daemon
        .replies
        .lock()
        .expect("not poisoned")
        .push_back((200, daemon_archive(&[("report/junit.xml", b"<failure/>")])));
    // The teardown's health poll (issue #80); no hooks in this body, so nothing lands.
    daemon.reply(
        200,
        r#"{"version": "0.1.0", "bootstrapped": true, "disk": null,
                    "identity_degraded": false, "identity_repaired": true}"#,
    );

    let seam = SyncSeam {
        transport: sync_launch_script(),
        clock: Arc::new(YieldingClock::default()),
        daemon: Arc::clone(&daemon),
    };
    let mut args = sync_run_args(&project, state.0.clone());
    args.config = crate::cli::ConfigFlags {
        config: Some(file.0.clone()),
        no_config: false,
    };

    let (result, _) = dispatch_with(&seam, &Command::Run(Box::new(args)), full_infra()).await;
    let rendered = result.expect("a failing workload keeps its success envelope");
    assert_eq!(
        rendered.already_reported,
        Some(Exit::ExecFailed),
        "the workload failed and the caller must see 13"
    );
    assert_eq!(
        std::fs::read_to_string(project.0.join("report/junit.xml")).expect("landed"),
        "<failure/>",
        "the failing run's report is the artifact the caller most wants"
    );
}

/// **A directory positional with nothing supplying an image is refused before any call.**
///
/// Sync mode launches; there is no binary to build from. The refusal is
/// `ERR_PRECONDITION` with the remedy named, and it costs zero doors — asserted the same
/// way the broken-config guard asserts it.
#[tokio::test]
async fn a_sync_dir_without_an_image_is_refused_before_any_call() {
    let state = TempDir::new("sync-no-image");
    let project = ProjectDir::new("no-image");
    let seam = RefusingSeam::new();

    let mut args = sync_run_args(&project, state.0.clone());
    args.image = None;
    args.config = no_config();

    let (result, _) = dispatch_with(&seam, &Command::Run(Box::new(args)), full_infra()).await;
    let failure = result.expect_err("nothing to launch from");
    assert_eq!(failure.exit, Exit::Precondition);
    assert!(failure.message.contains("sync mode"), "{}", failure.message);
    assert!(
        seam.doors().is_empty(),
        "a local refusal must cost zero doors"
    );
}
