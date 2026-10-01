// SPDX-License-Identifier: Apache-2.0
//! `Session::download_dir` against a real VM (#260).
//!
//! Invoked by `conformance/run_rs.py` (`drive_file_transfer`) against the suite's kept VM,
//! whose attach coordinates arrive in `MICROVM_LIVE_ATTACH` as a JSON object
//! (`{"microvmId", "endpoint", "agentToken", "region"}`). Launches nothing and terminates
//! nothing: the VM is the suite's, and the test removes the tree it plants.
//!
//! The daemon's own packer can't produce a `../` member, so traversal is the unit and binding
//! tiers' to cover. What only a live VM shows is what the real packer sends for a `.git` hook
//! and a symlink in the tree, and that core's extraction writes neither.

use std::time::Duration;

use microvms_core::prelude::*;
use microvms_core::protocol::exec::StartRequest;
use microvms_core::region::Region;
use microvms_core::session::{Session, mint_exec_id};
use microvms_core::workspace::DiskTree;

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

/// Runs a shell script in the VM and asserts it exited 0.
async fn shell(session: &Session, script: &str) {
    let request = StartRequest::new(mint_exec_id(), vec![script.to_string()])
        .with_shell(microvms_core::protocol::exec::Shell::Flag(true))
        .with_timeout_sec(Some(60.0));
    let result = session
        .run_sync(request, Duration::from_secs(90))
        .await
        .expect("the script ran");
    assert_eq!(
        result.exit_code(),
        Some(0),
        "{script}: {:?}",
        result.outcome
    );
}

/// A tree in the VM holding a `.git/hooks/pre-commit`, a symlink out of the tree and one regular
/// file: `download_dir` with `["**"]` writes the regular file and neither of the others.
#[tokio::test]
#[ignore = "needs the conformance suite's kept VM in MICROVM_LIVE_ATTACH"]
async fn download_dir_writes_only_the_regular_files_of_a_planted_tree() {
    let session = attached().await;
    let remote = format!("/tmp/microvm-download-dir-{}", mint_exec_id());
    shell(
        &session,
        &format!(
            "mkdir -p {remote}/.git/hooks {remote}/dist && \
             printf '#!/bin/sh\\necho pwned\\n' > {remote}/.git/hooks/pre-commit && \
             chmod +x {remote}/.git/hooks/pre-commit && \
             printf real > {remote}/dist/app.txt && \
             ln -s /etc/passwd {remote}/dist/link"
        ),
    )
    .await;

    let local = tempfile::tempdir().expect("a temp dir");
    let written = session
        .download_dir(&DiskTree, &remote, &["**".to_string()], local.path())
        .await;
    shell(&session, &format!("rm -rf {remote}")).await;
    let written = written.expect("the download");

    eprintln!("remote={remote} written={written:?}");
    let paths: Vec<&str> = written.iter().map(|file| file.path.as_str()).collect();
    assert_eq!(paths, ["dist/app.txt"], "only the regular file is written");
    assert_eq!(
        std::fs::read(local.path().join("dist/app.txt")).expect("written"),
        b"real"
    );
    assert!(
        !local.path().join(".git").exists(),
        "a .git hook from the VM landed on the host"
    );
    assert!(
        std::fs::symlink_metadata(local.path().join("dist/link")).is_err(),
        "a symlink from the VM landed on the host"
    );
}
