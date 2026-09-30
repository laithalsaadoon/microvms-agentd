// SPDX-License-Identifier: Apache-2.0
//! `cp` and `sync`, against the scripted daemon in `support`.

#![cfg(test)]

use std::sync::Arc;

use super::support::{
    DaemonScript, ScriptedSessionSeam, TempDir, against_daemon, attach_flags, full_infra,
    poll_body, region_flags, sync_command,
};
use crate::cli::{Command, CpArgs};
use crate::commands::Ctx;
use crate::envelope::{Format, Output};
use crate::exit::Exit;

/// **`cp` hands the daemon the archive bytes unexamined, byte for byte.**
///
/// The confinement guard proof, and the assertion is a **byte scan** of the recorded request rather
/// than a success code — which is the oracle's own falsification for this property. A CLI that
/// validated the archive first would refuse the hostile ones locally, and the four conformance
/// checks would then pass against *this file's* copy of the member rules while the daemon's
/// confined extractor — the thing that actually runs in production — went untested. The plan names
/// that substitution explicitly.
///
/// The payload here is a traversal member (`../../escaped.txt`), which is the first of the oracle's
/// four hostile archives and the one whose refusal the live tier asserts.
///
/// **Guard proof.** Add any pre-flight member check to `cp`'s upload path — even one that only
/// rejects `..` — and the "the bytes went out unchanged" assertion goes red with no request
/// recorded at all.
#[tokio::test]
async fn a_tar_upload_sends_the_archive_bytes_unexamined_including_a_hostile_one() {
    // A tar header naming `../../escaped.txt`, hand-built so the bytes are the test's own rather
    // than a library's opinion of them. Only the fields the assertion depends on are meaningful;
    // this never reaches a real extractor, and the point is that the CLI does not look.
    let mut archive = vec![0u8; 512];
    archive[..16].copy_from_slice(b"../../escaped.tx");
    archive[257..262].copy_from_slice(b"ustar");
    let hostile = TempFile::new("hostile-tar", &archive);

    // The daemon refuses it, which is the outcome the live check asserts: a 400 arriving as
    // `ProtocolError`. What is under test here is that the CLI got far enough to be refused.
    let script = DaemonScript::new();
    script.reply(
        400,
        r#"{"error": "tar_member_refused", "detail": "member escapes the extraction root"}"#,
    );

    let command = Command::Cp(CpArgs {
        src: hostile.path().to_string_lossy().to_string(),
        dst: "vm:/tmp/hostile".into(),
        tar: true,
        mode: None,
        attach: attach_flags(),
        region: region_flags(),
    });
    let (result, _, _) = against_daemon(&script, &command).await;

    let failure = result.expect_err("the daemon refuses a hostile member");
    assert_eq!(
        failure.wire_kind,
        Some(microvms_core::WireKind::ProtocolError),
        "a refused member is a 400/ProtocolError; a 413 would mean merely too big, which is a \
         different fact"
    );
    assert_eq!(failure.exit, Exit::Protocol);

    // The load-bearing assertion. The recorded body is byte-identical to the file, so nothing
    // inspected, rewrote, or sanitized it — the daemon's extractor is the only guard in the path.
    let sent = script
        .requests()
        .into_iter()
        .find(|request| request.path.starts_with("/v1/fs/tar"))
        .expect("the archive reached the tar route rather than being refused locally");
    assert_eq!(
        sent.body, archive,
        "the CLI must hand core the bytes it was given: a pre-flight check here would make the \
         hostile-archive checks test this file's copy of the member rules instead of the daemon's"
    );
    assert_eq!(sent.method, "PUT");
}

/// **`cp` names the direction from the argument, and a single-file upload carries its mode.**
///
/// The mode is octal **as a string** on the wire, which is the daemon's contract: `"644"` and
/// `"0644"` mean the same mode, and an integer would be read as decimal 644 by anything that
/// stringifies it. Asserted on the query string, because that is where it goes.
#[tokio::test]
async fn a_single_file_upload_uses_the_file_route_and_carries_its_octal_mode() {
    let payload = TempFile::new("cp-upload", b"written through the endpoint");
    let script = DaemonScript::new();
    script.reply(200, "");

    let command = Command::Cp(CpArgs {
        src: payload.path().to_string_lossy().to_string(),
        dst: "vm:/tmp/live.txt".into(),
        tar: false,
        mode: Some("0644".into()),
        attach: attach_flags(),
        region: region_flags(),
    });
    let (result, _, _) = against_daemon(&script, &command).await;
    let rendered = result.expect("the upload succeeds");

    let sent = script.requests().pop().expect("a request went out");
    assert_eq!(sent.method, "PUT");
    assert!(
        sent.path.starts_with("/v1/fs/file?"),
        "a single file goes to the file route, not the tar one: {}",
        sent.path
    );
    assert!(
        sent.path.contains("mode=0644"),
        "the mode is octal as a string on the wire: {}",
        sent.path
    );
    assert_eq!(sent.body, b"written through the endpoint");
    assert_eq!(rendered.data["direction"], "upload");
    assert_eq!(rendered.data["bytes"], 28);
}

/// **`cp vm:/path ./local` writes the bytes to disk, unmodified, including non-UTF-8 ones.**
///
/// The download half, and the non-UTF-8 payload is the case worth having: a `cp` that went through
/// a string anywhere would corrupt a tarball or a binary, and the corruption would be invisible
/// until someone tried to use the file.
#[tokio::test]
async fn a_download_writes_the_raw_bytes_to_the_local_path() {
    let bytes = vec![0xffu8, 0x00, 0xfe, b'a', b'\n'];
    let dir = TempDir::new("cp-download");
    let local = dir.0.join("nested").join("out.bin");

    let script = DaemonScript::new();
    script
        .replies
        .lock()
        .expect("not poisoned")
        .push_back((200, bytes.clone()));

    let command = Command::Cp(CpArgs {
        src: "vm:/tmp/bin".into(),
        dst: local.to_string_lossy().to_string(),
        tar: false,
        mode: None,
        attach: attach_flags(),
        region: region_flags(),
    });
    let (result, _, _) = against_daemon(&script, &command).await;
    let rendered = result.expect("the download succeeds");

    assert_eq!(
        std::fs::read(&local).expect("the file was written"),
        bytes,
        "a download that went through a string would corrupt a tarball invisibly"
    );
    assert_eq!(rendered.data["direction"], "download");
    assert_eq!(rendered.data["bytes"], 5);
    assert_eq!(script.requests().pop().expect("a request").method, "GET");
}

/// The guest-manifest body describing exactly `dir`'s current tree, as the last sync
/// would have written it — built by the same hasher the command runs, because the guard's
/// question is the *transport* consequence of a matching manifest, not the hashing.
fn manifest_body_for(dir: &std::path::Path) -> String {
    let manifest = crate::sync::manifest(dir).expect("the tree manifests");
    String::from_utf8(serde_json::to_vec(&manifest).expect("serializes")).expect("utf8")
}

/// **The second sync of an unchanged tree transfers no archive at all — issue #71's
/// acceptance line, asserted at the wire.**
///
/// The scripted daemon holds a manifest that matches the tree byte-for-byte, and the
/// whole invocation is then **one GET**: no tar route, no file writes, no exec. The
/// envelope says the same thing (`unchanged: true`, `uploadedBytes: 0`), but the request
/// log is the assertion that cannot be faked by rendering.
///
/// **Falsification** — make `sync_pass` upload unconditionally (drop the
/// `delta.is_empty()` early return) and this goes red before its own assertions run:
/// the script authorizes exactly one reply, so the unauthorized `PUT /v1/fs/tar` panics
/// the scripted daemon with "ran out of replies" naming that request. Done on
/// 2026-09-01; failed exactly there; restored.
#[tokio::test]
async fn a_second_sync_of_an_unchanged_tree_sends_no_archive() {
    let dir = TempDir::new("sync-unchanged");
    std::fs::write(dir.0.join("main.rs"), b"fn main() {}").expect("writes");

    let script = DaemonScript::new();
    script.reply(200, &manifest_body_for(&dir.0));

    let (result, _, _) = against_daemon(&script, &sync_command(&dir.0, |_| {})).await;
    let rendered = result.expect("an unchanged tree syncs successfully");

    assert_eq!(rendered.data["unchanged"], true);
    assert_eq!(rendered.data["uploadedBytes"], 0);
    assert_eq!(rendered.data["uploadedMembers"], 0);
    assert_eq!(rendered.data["deleted"], 0);
    let paths = script.paths();
    assert_eq!(
        paths.len(),
        1,
        "an unchanged tree must cost exactly the manifest read: {paths:?}"
    );
    assert!(
        paths[0].starts_with("GET /v1/fs/file?"),
        "the one request is the guest manifest: {paths:?}"
    );
}

/// **A first sync — no guest manifest — uploads the whole tree and then writes the
/// manifest the next sync will diff against.**
///
/// The 404 is the daemon's honest "nothing there", and the command's answer is the whole
/// contract in one request log: tar the tree up, then persist its description. The
/// manifest travels to [`crate::sync::MANIFEST_PATH`] — inside the workspace, so a
/// workload that wipes the workspace also wipes the description of it.
#[tokio::test]
async fn a_first_sync_uploads_everything_and_writes_the_guest_manifest() {
    let dir = TempDir::new("sync-first");
    std::fs::write(dir.0.join("app.py"), b"print('hi')").expect("writes");

    let script = DaemonScript::new();
    script
        .reply(404, "no such file") // the manifest read: nothing has synced before
        .reply(200, "") // the tar upload
        .reply(200, ""); // the manifest write

    let (result, _, _) = against_daemon(&script, &sync_command(&dir.0, |_| {})).await;
    let rendered = result.expect("a first sync succeeds");

    assert_eq!(rendered.data["full"], true);
    assert_eq!(rendered.data["unchanged"], false);
    assert_eq!(rendered.data["uploadedMembers"], 1);

    let requests = script.requests();
    assert_eq!(requests.len(), 3, "{:?}", script.paths());
    assert!(
        requests[1].path.starts_with("/v1/fs/tar?path=%2Fworkspace"),
        "the tree goes to the workspace tar route: {}",
        requests[1].path
    );
    let members: Vec<String> = tar::Archive::new(requests[1].body.as_slice())
        .entries()
        .expect("parses")
        .map(|entry| {
            entry
                .expect("a member")
                .path()
                .expect("a path")
                .display()
                .to_string()
        })
        .collect();
    assert_eq!(members, ["app.py"]);
    assert!(
        requests[2]
            .path
            .contains("%2Fworkspace%2F.microvm-sync-manifest.json"),
        "the manifest lands beside the tree it describes: {}",
        requests[2].path
    );
    let written: crate::sync::Manifest =
        serde_json::from_slice(&requests[2].body).expect("the written manifest parses");
    assert!(written.files.contains_key("app.py"), "{written:?}");
}

/// **An edited tree uploads only the changed member, and a locally-deleted path is
/// removed in the guest — by one exec, `rm` with `--`, rooted in the workspace.**
///
/// The proportionality claim at the wire: two files, one edited, and the archive carries
/// exactly one member. The deletion rides the exec route rather than a client-side
/// extraction rule, because only the guest can unlink in the guest.
#[tokio::test]
async fn an_edit_uploads_only_the_change_and_deletes_what_vanished() {
    let dir = TempDir::new("sync-edit");
    std::fs::write(dir.0.join("edited.txt"), b"new contents").expect("writes");
    std::fs::write(dir.0.join("same.txt"), b"unchanged").expect("writes");

    // The guest manifest: `same.txt` current, `edited.txt` stale, `removed.txt` present
    // remotely and absent locally.
    let mut remote = crate::sync::manifest(&dir.0).expect("manifests");
    remote
        .files
        .insert("edited.txt".into(), "0".repeat(64))
        .expect("the stale entry replaces the fresh one");
    remote.files.insert("removed.txt".into(), "1".repeat(64));
    let remote_body = String::from_utf8(serde_json::to_vec(&remote).expect("serializes")).unwrap();

    let script = DaemonScript::new();
    script
        .reply(200, &remote_body) // the manifest read
        .reply(200, "") // the tar upload
        .reply(200, r#"{"exec_id": "x-1", "phase": "running"}"#) // rm starts
        .reply(200, &poll_body("exited", "0", "", false)) // rm exits 0
        .reply(200, &poll_body("acked", "0", "", false)) // rm acked
        .reply(200, ""); // the manifest write

    let (result, _, _) = against_daemon(&script, &sync_command(&dir.0, |_| {})).await;
    let rendered = result.expect("an incremental sync succeeds");

    assert_eq!(rendered.data["uploadedMembers"], 1);
    assert_eq!(rendered.data["deleted"], 1);
    assert_eq!(rendered.data["full"], false);

    let requests = script.requests();
    let archive = &requests[1].body;
    let members: Vec<String> = tar::Archive::new(archive.as_slice())
        .entries()
        .expect("parses")
        .map(|entry| {
            entry
                .expect("a member")
                .path()
                .expect("a path")
                .display()
                .to_string()
        })
        .collect();
    assert_eq!(
        members,
        ["edited.txt"],
        "the archive is proportional to the edit, not to the tree"
    );
    let start: serde_json::Value =
        serde_json::from_slice(&requests[2].body).expect("the start request parses");
    assert_eq!(start["command"][0], "rm");
    assert_eq!(start["command"][2], "--");
    assert_eq!(start["command"][3], "removed.txt");
    assert_eq!(start["cwd"], "/workspace");
    assert_eq!(start["shell"], false, "argv, never a shell string");
}

/// **A hostile guest manifest cannot order deletions outside the workspace.**
///
/// The manifest is read *from the VM*, and the VM is where untrusted work runs — the same
/// trust direction [`crate::sync::extract_artifacts`] defends on the download path. A
/// workload that rewrites the manifest to claim `../../etc/passwd` and `/root/.ssh/key`
/// were synced is asking this CLI to run `rm -rf` on them on its behalf. Both are
/// filtered before the exec is built; with no legitimate deletion left, **no exec starts
/// at all**.
///
/// **Falsification** — drop the `deletable` filter in `sync_pass` and this goes red at
/// the unauthorized `POST /v1/exec/start`: the script holds no reply for an exec, so the
/// scripted daemon panics with "ran out of replies" naming that request. Done on
/// 2026-09-01; failed exactly there; restored.
#[tokio::test]
async fn a_hostile_guest_manifest_cannot_order_deletions_outside_the_workspace() {
    let dir = TempDir::new("sync-hostile-manifest");
    std::fs::write(dir.0.join("kept.txt"), b"still here").expect("writes");

    let mut remote = crate::sync::manifest(&dir.0).expect("manifests");
    remote
        .files
        .insert("../../etc/passwd".into(), "2".repeat(64));
    remote.files.insert("/root/.ssh/key".into(), "3".repeat(64));
    // A change, so the pass has something legitimate to do besides the hostile rows.
    remote
        .files
        .insert("kept.txt".into(), "4".repeat(64))
        .expect("the stale entry replaces the fresh one");
    let remote_body = String::from_utf8(serde_json::to_vec(&remote).expect("serializes")).unwrap();

    let script = DaemonScript::new();
    script
        .reply(200, &remote_body) // the manifest read
        .reply(200, "") // the tar upload (kept.txt changed)
        .reply(200, ""); // the manifest write — and NO exec in between

    let (result, _, stderr) = against_daemon(&script, &sync_command(&dir.0, |_| {})).await;
    let rendered = result.expect("the sync succeeds without executing the hostile rows");

    assert_eq!(rendered.data["deleted"], 0);
    let paths = script.paths();
    assert_eq!(
        paths.len(),
        3,
        "no exec may start for a filtered path: {paths:?}"
    );
    assert!(
        !paths.iter().any(|path| path.contains("/v1/exec")),
        "{paths:?}"
    );
    assert!(
        stderr.contains("skipping 2"),
        "the refusal is named, not silent: {stderr}"
    );
}

/// **The daemon's disk-pressure 507 surfaces as `ERR_PLATFORM` with the remedy, never as
/// `ERR_RETRYABLE`.**
///
/// The default wire mapping sends every 5xx to `ERR_RETRYABLE`, and `agentd/src/fs.rs`
/// documents why that is exactly wrong for this one: a retry against a full disk is
/// "actively harmful", and 507 exists to be distinguishable from a defect. The command
/// re-classifies it onto the existing `ERR_PLATFORM` row (no new exit code), keeps the
/// daemon's byte counts in the message, marks `data.diskUnderPressure`, and suggests the
/// path that actually helps — freeing space.
///
/// **Falsification** — drop `classify_upload` from the tar upload (plain `?`) and both
/// assertions go red: the exit arrives as `Exit::Retryable` and the marker is absent.
/// Done on 2026-09-01; failed as stated (left: Retryable, right: Platform); restored.
#[tokio::test]
async fn a_disk_pressure_507_is_platform_with_the_remedy_not_retryable() {
    let dir = TempDir::new("sync-507");
    std::fs::write(dir.0.join("big.bin"), vec![0u8; 1024]).expect("writes");

    let script = DaemonScript::new();
    script.reply(404, "no such file").reply(
        // The daemon's own body shape (`agentd/src/fs.rs::insufficient_storage`).
        507,
        "refusing to write /workspace: 4096 bytes available on the target filesystem, \
         below the 1048576 byte reserve",
    );

    let (result, _, _) = against_daemon(&script, &sync_command(&dir.0, |_| {})).await;
    let failure = result.expect_err("a 507 refuses the sync");

    assert_eq!(
        failure.exit,
        Exit::Platform,
        "a full disk is not retryable-unchanged: {}",
        failure.message
    );
    assert_eq!(failure.data["diskUnderPressure"], true);
    assert!(
        failure.message.contains("bytes available"),
        "the daemon's byte counts survive into the message: {}",
        failure.message
    );
    assert!(
        failure
            .suggestions
            .iter()
            .any(|suggestion| suggestion.contains("free space")),
        "{:?}",
        failure.suggestions
    );
}

/// **`sync --watch` re-syncs on a real filesystem event and reports the passes.**
///
/// End to end through a real `notify` watcher: the initial full sync lands, a file is
/// then written, and the loop's debounced re-hash uploads it — after which the interrupt
/// (Ctrl-C in the shipped binary) resolves into the summary envelope, the `port-forward`
/// precedent. The test observes progress through the shared script's request log and
/// fires the interrupt only once the second pass's manifest write has landed, so nothing
/// here depends on sleeping the right amount — only on the watcher delivering an event at
/// all, within a generous deadline.
#[tokio::test]
async fn watch_resyncs_on_a_filesystem_event_and_the_interrupt_reports_totals() {
    let dir = TempDir::new("sync-watch");
    std::fs::write(dir.0.join("first.txt"), b"one").expect("writes");

    let script = DaemonScript::new();
    script
        .reply(404, "no such file") // pass 1: no manifest yet
        .reply(200, "") // pass 1: tar upload
        .reply(200, "") // pass 1: manifest write
        .reply(200, "") // pass 2: tar upload (the watched change)
        .reply(200, ""); // pass 2: manifest write

    let (fire, fired) = tokio::sync::oneshot::channel::<()>();
    let seam = ScriptedSessionSeam {
        script: Arc::clone(&script),
    };
    let mut out = Output::new(Format::Json, false, Vec::new(), Vec::new());
    let env = |_: &str| None;
    let command = sync_command(&dir.0, |args| args.watch = true);
    let mut ctx = Ctx {
        seam: &seam,
        out: &mut out,
        infra: full_infra(),
        env: &env,
        fetch: &crate::provision::PanickingFetch,
    };
    let interrupt: crate::commands::lifecycle::Interrupt<'_> = Box::pin(async move {
        let _ = fired.await;
    });
    let run = crate::handle(&mut ctx, &command, interrupt);
    let driver = async {
        // Wait until the initial pass is fully applied (three requests), then edit.
        let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(20);
        while script.requests().len() < 3 {
            assert!(
                tokio::time::Instant::now() < deadline,
                "initial sync stalled"
            );
            tokio::time::sleep(std::time::Duration::from_millis(20)).await;
        }
        std::fs::write(dir.0.join("second.txt"), b"two").expect("writes");
        // The watcher, the debounce, and the second pass; done when its manifest lands.
        while script.requests().len() < 5 {
            assert!(
                tokio::time::Instant::now() < deadline,
                "the watcher never delivered the edit: {:?}",
                script.paths()
            );
            tokio::time::sleep(std::time::Duration::from_millis(20)).await;
        }
        let _ = fire.send(());
    };
    let (result, ()) = tokio::join!(run, driver);
    let rendered = result.expect("the watch ends as a success");

    assert_eq!(rendered.data["watched"], true);
    // `>= 2` rather than `== 2`: a watcher may split one write into events straddling the
    // debounce window, and the straggler costs an extra pass that hashes, matches, and
    // uploads nothing. The request log below is the exact assertion; the pass count only
    // says the loop actually looped.
    assert!(
        rendered.data["passes"].as_u64().expect("a count") >= 2,
        "{:?}",
        rendered.data["passes"]
    );
    assert_eq!(rendered.data["uploadedMembers"], 2, "one per pass, summed");
    let members: Vec<String> = tar::Archive::new(script.requests()[3].body.as_slice())
        .entries()
        .expect("parses")
        .map(|entry| {
            entry
                .expect("a member")
                .path()
                .expect("a path")
                .display()
                .to_string()
        })
        .collect();
    assert_eq!(
        members,
        ["second.txt"],
        "the watched pass uploads the edit and only the edit"
    );
}

/// **The watch filter's table: the pack's skip list plus the manifest name, nothing else.**
///
/// Pinned directly because a filter miss is invisible in an integration run — a wrongly
/// filtered path just means no pass, which looks like no event. `target/` is the row that
/// earns the filter its keep: a Rust project's own build writes there constantly, and an
/// unfiltered watcher would re-hash the tree on every compile this CLI's user runs.
#[test]
fn the_watch_filter_ignores_the_skip_list_and_the_manifest_and_nothing_else() {
    let root = std::path::Path::new("/project");
    let relevant = |path: &str| crate::commands::attached::watch_relevant(root, &root.join(path));
    assert!(relevant("src/main.rs"));
    assert!(relevant("deep/nested/asset.css"));
    assert!(
        !relevant("target/debug/build.rs"),
        "the build tree never syncs"
    );
    assert!(!relevant(".git/index"));
    assert!(!relevant("node_modules/left-pad/index.js"));
    assert!(!relevant(".venv/bin/python"));
    assert!(
        !relevant(".microvm-sync-manifest.json"),
        "our own write is not an edit"
    );
    assert!(
        !crate::commands::attached::watch_relevant(root, std::path::Path::new("/elsewhere/f")),
        "a path outside the tree is never ours"
    );
}

/// **The watch filter accepts the kernel's spelling of the root, not only the caller's.**
///
/// macOS reproduces this unaided: `$TMPDIR` is `/var/folders/…`, FSEvents reports
/// `/private/var/folders/…`, and the first cut of `--watch` dropped every event as outside
/// the tree — the watch guard above failed on macos-latest and nowhere else, with the edit
/// never "delivered". Linux has no such default symlink, so the shape is built by hand: a
/// symlink is the caller's spelling, its target is what the kernel reports. The first
/// assertion is the falsification: the bare filter cannot see through the symlink.
#[cfg(unix)]
#[test]
fn the_watch_filter_accepts_the_kernels_spelling_of_a_symlinked_root() {
    let real = TempDir::new("watch-real");
    let holder = TempDir::new("watch-link");
    let link = holder.0.join("project");
    std::os::unix::fs::symlink(&real.0, &link).expect("symlinks");
    let kernel_root = std::fs::canonicalize(&real.0).expect("resolves");
    let reported = kernel_root.join("src/main.rs");

    assert!(
        !crate::commands::attached::watch_relevant(&link, &reported),
        "the bare filter drops the kernel's spelling — the miss WatchRoot exists to close"
    );
    let root = crate::commands::attached::WatchRoot::new(&link);
    assert!(root.relevant(&reported), "the kernel's spelling is ours");
    assert!(
        root.relevant(&link.join("src/main.rs")),
        "so is the caller's"
    );
    assert!(
        !root.relevant(&kernel_root.join("target/debug/x")),
        "the skip list applies under either spelling"
    );
    assert!(
        !root.relevant(std::path::Path::new("/elsewhere/f")),
        "a path outside both spellings is never ours"
    );
}

/// A file that cleans itself up, for the two `cp` tests that need real bytes on disk.
struct TempFile(std::path::PathBuf, #[allow(dead_code)] tempfile::TempPath);

impl TempFile {
    fn new(label: &str, bytes: &[u8]) -> Self {
        let file = tempfile::Builder::new()
            .prefix(&format!("microvm-guard-{label}-"))
            .tempfile()
            .expect("a temp file");
        std::fs::write(file.path(), bytes).expect("writes");
        let path = file.into_temp_path();
        Self(path.to_path_buf(), path)
    }

    fn path(&self) -> &std::path::Path {
        &self.0
    }
}
