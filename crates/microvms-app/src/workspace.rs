// SPDX-License-Identifier: Apache-2.0
//! A project directory moved between a host and a VM: the manifest, the diff, one incremental
//! sync pass, and the guarded download of a remote tree (#260).
//!
//! # The daemon extracts uploads; the client extracts only what the caller chose
//!
//! The trust boundary is asymmetric and the code follows it. On the way *in*, the archive is
//! built on the host from a tree the caller owns, and the daemon, whose openat2 confinement is
//! the extraction surface this workspace hardened (`crates/agentd/src/fs.rs`), unpacks it. On
//! the way *out*, the archive describes the VM's filesystem, and the VM is where untrusted work
//! runs, so [`LocalTree::extract`] writes only the members the caller's globs selected, only
//! when they're regular files, never under `.git`, and never outside the destination. A
//! workload that appends `../../.ssh/authorized_keys`, a symlink or `.git/hooks/pre-commit` to
//! the archive gets it skipped rather than written.
//!
//! The same holds for the guest's manifest: it's read from the VM, so a deletion it orders is
//! executed only when [`deletable`] finds a plain relative path in it.
//!
//! # What lives here and what lives in the edges
//!
//! This module is the rules: which directories never travel, the daemon's budgets, what a
//! manifest is, what a diff between two manifests orders, which deletion paths are safe, and
//! the sync pass that composes them over a [`Session`]. It reads no file (ARCH-7). The walk,
//! the hashing, the tar packing and the extraction are [`LocalTree`]'s, the port
//! `microvms-edges` implements over the real filesystem, so a test can hand the pass a tree of
//! its own.

use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;
use std::time::Duration;

use crate::error::{Error, ErrorKind, WireKind};
use crate::session::Session;

/// Directory names never packed or hashed, whole.
///
/// `.git`, `target`, `node_modules` and `.venv`: a repository's object store and a build tree
/// blow through the daemon's budgets while contributing nothing to a build. `.git` also
/// protects the *extraction* side, see [`LocalTree::extract`], so removing it here without
/// reading that contract would reopen a hole, not just widen an upload.
pub const SKIPPED_DIRS: [&str; 4] = [".git", "target", "node_modules", ".venv"];

/// The pack's byte budget: the daemon's `max_body_bytes` (512 MiB), checked on file sizes
/// during the walk, so an over-budget tree is refused before its archive is allocated.
pub const MAX_PACK_BYTES: u64 = 512 * 1024 * 1024;

/// The pack's member budget: the daemon's `max_tar_members`.
pub const MAX_PACK_MEMBERS: usize = 100_000;

/// Whether a walk's running byte total is still within [`MAX_PACK_BYTES`]: at the budget is
/// within it, one byte past isn't.
pub fn within_byte_budget(total: u64) -> bool {
    total <= MAX_PACK_BYTES
}

/// Whether a walk's running member count is still within [`MAX_PACK_MEMBERS`].
pub fn within_member_budget(count: usize) -> bool {
    count <= MAX_PACK_MEMBERS
}

/// Where a synced tree lands in the guest, and the working directory of what runs in it.
///
/// The daemon's `write_tar` creates the root it's given. A constant rather than a parameter:
/// two callers against one VM must not disagree about where "the project" is.
pub const REMOTE_WORKDIR: &str = "/workspace";

/// Where the incremental manifest lives in the guest: inside the workspace, deliberately.
///
/// The manifest is a cache of what the last sync put in the workspace, and a cache must die
/// with the thing it describes. A workload that wipes the workspace wipes the manifest too, so
/// the next sync sees none and uploads everything instead of trusting a description of files
/// that are gone.
pub const MANIFEST_PATH: &str = "/workspace/.microvm-sync-manifest.json";

/// The manifest's member name relative to the workspace. It never travels and never appears
/// in a [`Delta`], so the manifest can't order its own deletion.
pub const MANIFEST_NAME: &str = ".microvm-sync-manifest.json";

/// The manifest format this build writes.
pub const MANIFEST_VERSION: u32 = 1;

/// The in-guest `rm`'s `timeout_sec` when a caller names none: `microvm sync --timeout`'s
/// default, and the bindings' `sync_dir`'s.
pub const DEFAULT_SYNC_DELETE_TIMEOUT: Duration = Duration::from_secs(60);

/// How long past the in-guest `rm`'s own `timeout_sec` a sync pass waits for its answer.
///
/// Thirty seconds, not [`crate::session::DEFAULT_CLIENT_GRACE`]'s sixty: that margin covers an
/// exec the daemon escalates from SIGTERM to SIGKILL, and a pass under `sync --watch` holds the
/// next one while it waits.
pub const SYNC_DELETE_GRACE: Duration = Duration::from_secs(30);

/// How long a sync pass waits for its in-guest `rm`: the daemon's own budget plus
/// [`SYNC_DELETE_GRACE`] for the answer to come back, so the daemon's deadline, not the
/// client's, is what ends a slow one.
fn delete_wait(budget: Duration) -> Duration {
    budget.saturating_add(SYNC_DELETE_GRACE)
}

/// A local tree that couldn't be packed, hashed or written to.
///
/// Its own type, carried as the [`Error::source`] of the `ERR_INVALID_ARG` it becomes, so an
/// adapter that reports local filesystem failures on a row of its own (the CLI's `ERR_SYNC`)
/// can tell one from a failure the daemon reported without reading a message.
#[derive(Debug, Clone, Eq, PartialEq)]
pub struct WorkspaceError(String);

impl WorkspaceError {
    pub fn new(message: impl Into<String>) -> Self {
        Self(message.into())
    }
}

impl std::fmt::Display for WorkspaceError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        self.0.fmt(f)
    }
}

impl std::error::Error for WorkspaceError {}

impl From<WorkspaceError> for Error {
    /// `ERR_INVALID_ARG`: the tree is the caller's, and so is the fix (a smaller tree, a
    /// readable path, a writable destination). Nothing the daemon said is involved.
    fn from(error: WorkspaceError) -> Self {
        Error::new(ErrorKind::InvalidArg, error.to_string()).with_source(error)
    }
}

/// What one sync left in the guest: every member's identity, keyed by relative path.
///
/// Paths are `/`-separated on every platform, since a manifest crosses machines. Maps are
/// ordered so the serialized form is deterministic and a test can assert on bytes.
#[derive(Clone, Debug, Default, PartialEq, serde::Serialize, serde::Deserialize)]
pub struct Manifest {
    /// The format version, for a later reader deciding whether it understands this.
    pub version: u32,
    /// Regular files: relative path to the sha256 of the contents, lowercase hex.
    pub files: BTreeMap<String, String>,
    /// Symlinks: relative path to the link target, verbatim. Two links to different targets
    /// are different members.
    pub symlinks: BTreeMap<String, String>,
    /// Directories, including empty ones a build script expects to exist.
    pub dirs: BTreeSet<String>,
}

/// What an incremental sync has to do.
#[derive(Debug, Default, PartialEq)]
pub struct Delta {
    /// Relative paths to pack and upload: new members, changed files, retargeted links.
    pub upload: Vec<String>,
    /// Relative paths present remotely and gone locally, deepest first, so a directory's
    /// contents are named before the directory.
    pub delete: Vec<String>,
}

impl Delta {
    /// Nothing to send and nothing to remove.
    pub fn is_empty(&self) -> bool {
        self.upload.is_empty() && self.delete.is_empty()
    }
}

/// What changed between the tree as it is (`local`) and as the last sync left it (`remote`).
///
/// Identity is category-scoped: a path that was a file and is now a symlink is uploaded and
/// not deleted, since the upload overwrites the name in place, which is the daemon's own
/// extraction contract. [`MANIFEST_NAME`] never appears in either set.
pub fn diff(local: &Manifest, remote: &Manifest) -> Delta {
    let mut delta = Delta::default();
    for (path, hash) in &local.files {
        if remote.files.get(path) != Some(hash) {
            delta.upload.push(path.clone());
        }
    }
    for (path, target) in &local.symlinks {
        if remote.symlinks.get(path) != Some(target) {
            delta.upload.push(path.clone());
        }
    }
    for path in &local.dirs {
        if !remote.dirs.contains(path) {
            delta.upload.push(path.clone());
        }
    }
    let lives_on = |path: &String| {
        local.files.contains_key(path)
            || local.symlinks.contains_key(path)
            || local.dirs.contains(path)
            || path == MANIFEST_NAME
    };
    delta.delete.extend(
        remote
            .files
            .keys()
            .chain(remote.symlinks.keys())
            .chain(remote.dirs.iter())
            .filter(|path| !lives_on(path))
            .cloned(),
    );
    delta.upload.sort();
    // Deepest first: `a/b/c` before `a/b`, so removing in order never needs recursion.
    delta.delete.sort_by(|a, b| b.cmp(a));
    delta
}

/// Whether a deletion path from the guest manifest is safe to hand to an in-guest `rm`.
///
/// The manifest is read *from the VM*, so a workload that rewrites it to claim
/// `../../etc/passwd` or `/root/.ssh` was synced would otherwise get the client to order that
/// deletion for it. Only a relative path with no `..`, no backslash and no empty component
/// qualifies; anything else is skipped, not executed.
pub fn deletable(path: &str) -> bool {
    !path.is_empty()
        && !path.starts_with('/')
        && !path.contains('\\')
        && path
            .split('/')
            .all(|component| !component.is_empty() && component != "..")
}

/// A packed archive and how many members it holds.
#[derive(Debug)]
pub struct Packed {
    pub archive: Vec<u8>,
    pub members: usize,
}

/// One member [`LocalTree::extract`] wrote.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Artifact {
    /// The member's path, relative to the destination.
    pub path: String,
    pub bytes: u64,
}

/// A local directory tree, packed, hashed and written to: the port `microvms-edges`
/// implements over the real filesystem.
///
/// Every implementation walks the same way: [`SKIPPED_DIRS`] skipped whole; sockets, fifos
/// and devices skipped individually; symlinks kept as links, never followed; the
/// [`MAX_PACK_BYTES`] and [`MAX_PACK_MEMBERS`] budgets enforced during the walk.
pub trait LocalTree: Send + Sync {
    /// The walk of `dir`, hashed into a [`Manifest`]: exactly the set [`Self::pack`] uploads.
    fn manifest(&self, dir: &Path) -> Result<Manifest, WorkspaceError>;

    /// The walk of `dir` as a tar archive, members in sorted path order so the same tree packs
    /// to the same bytes.
    fn pack(&self, dir: &Path) -> Result<Packed, WorkspaceError>;

    /// Exactly the named relative paths under `dir`, in sorted order.
    fn pack_paths(&self, dir: &Path, paths: &[String]) -> Result<Packed, WorkspaceError>;

    /// Writes the regular-file members of `archive` that match one of `globs` under `dir`,
    /// and nothing else: no symlink, hardlink, directory or special member, nothing under
    /// `.git` whatever the globs say, and nothing that would land outside `dir`. What it
    /// skips is skipped, not refused, since the archive is the VM's word.
    fn extract(
        &self,
        archive: &[u8],
        globs: &[String],
        dir: &Path,
    ) -> Result<Vec<Artifact>, WorkspaceError>;
}

/// What one sync pass did.
#[derive(Debug)]
pub struct SyncPass {
    /// The archive's size, when anything travelled.
    pub uploaded_bytes: usize,
    pub uploaded_members: usize,
    /// The deletions the in-guest `rm` carried out.
    pub deleted: usize,
    /// The deletions the guest's manifest ordered that [`deletable`] refused.
    pub refused_deletions: usize,
    /// No baseline was given, so the whole tree travelled.
    pub full: bool,
    /// The baseline already matched the tree; nothing travelled at all.
    pub unchanged: bool,
    /// The tree as this pass left it in the guest: the next pass's baseline.
    pub manifest: Manifest,
}

impl Session {
    /// The manifest the last sync left in the guest, or `None` when there's none.
    ///
    /// A manifest that doesn't parse reads as `None` too: a different version, or a workload,
    /// wrote that path, and the safe reading of "can't tell what's over there" is the same as
    /// "nothing is": a full upload, which writes a manifest this build understands.
    pub async fn sync_manifest(&self) -> Result<Option<Manifest>, Error> {
        match self.download_file(MANIFEST_PATH).await {
            Ok(bytes) => Ok(serde_json::from_slice(&bytes).ok()),
            Err(error) if error.wire_kind() == Some(WireKind::NotFound) => Ok(None),
            Err(error) => Err(error),
        }
    }

    /// Syncs `dir` into [`REMOTE_WORKDIR`] once: against the guest's own manifest, or with
    /// `full` against none, so everything travels. [`Self::sync_manifest`] then
    /// [`Self::sync_pass`], the whole of a one-shot sync.
    pub async fn sync_dir(
        &self,
        tree: &dyn LocalTree,
        dir: &Path,
        full: bool,
        delete_timeout: Duration,
    ) -> Result<SyncPass, Error> {
        let baseline = if full {
            None
        } else {
            self.sync_manifest().await?
        };
        self.sync_pass(tree, dir, baseline, delete_timeout).await
    }

    /// One incremental pass of `dir` into [`REMOTE_WORKDIR`]: hash, diff against `baseline`,
    /// upload what changed, delete what vanished, and rewrite the guest manifest.
    ///
    /// `baseline` is what the guest holds, usually [`Self::sync_manifest`]'s answer or the
    /// last pass's [`SyncPass::manifest`]; `None` uploads everything. The deletions run as one
    /// `rm -rf --` in the guest with `delete_timeout` as the daemon's deadline, and a failed
    /// one fails the pass with the guest manifest left as it was, so the next pass orders the
    /// deletions again. Only [`deletable`] paths are removed.
    pub async fn sync_pass(
        &self,
        tree: &dyn LocalTree,
        dir: &Path,
        baseline: Option<Manifest>,
        delete_timeout: Duration,
    ) -> Result<SyncPass, Error> {
        let local = tree.manifest(dir)?;
        let full = baseline.is_none();
        let delta = diff(&local, &baseline.unwrap_or_default());
        if delta.is_empty() && !full {
            return Ok(SyncPass {
                uploaded_bytes: 0,
                uploaded_members: 0,
                deleted: 0,
                refused_deletions: 0,
                full: false,
                unchanged: true,
                manifest: local,
            });
        }

        let mut uploaded_bytes = 0;
        let mut uploaded_members = 0;
        if !delta.upload.is_empty() {
            let packed = tree.pack_paths(dir, &delta.upload)?;
            self.upload_tar(REMOTE_WORKDIR, &packed.archive).await?;
            uploaded_bytes = packed.archive.len();
            uploaded_members = packed.members;
        }

        let doomed: Vec<String> = delta
            .delete
            .iter()
            .filter(|path| deletable(path))
            .cloned()
            .collect();
        let refused_deletions = delta.delete.len() - doomed.len();
        let mut deleted = 0;
        if !doomed.is_empty() {
            let count = doomed.len();
            let command = ["rm", "-rf", "--"]
                .into_iter()
                .map(String::from)
                .chain(doomed)
                .collect();
            let request = protocol::exec::StartRequest::new(self.mint_exec_id(), command)
                .with_cwd(Some(REMOTE_WORKDIR.into()))
                .with_timeout_sec(Some(delete_timeout.as_secs_f64()));
            let result = self.run_sync(request, delete_wait(delete_timeout)).await?;
            match result.outcome {
                Some(outcome) if outcome.exit_code == Some(0) => deleted = count,
                outcome => {
                    let detail = outcome
                        .map(|outcome| {
                            let stderr = outcome.stderr.trim();
                            if stderr.is_empty() {
                                format!("exit {:?}", outcome.exit_code)
                            } else {
                                stderr.to_string()
                            }
                        })
                        .unwrap_or_else(|| "still running at the deadline".into());
                    return Err(Error::new(
                        ErrorKind::ExecFailed,
                        format!(
                            "the in-guest removal of {count} deleted path(s) failed: {detail}. \
                             The uploaded members landed; the guest manifest was left as it \
                             was, so the next sync will order these deletions again."
                        ),
                    ));
                }
            }
        }

        let body = serde_json::to_vec(&local).map_err(|error| {
            Error::new(
                ErrorKind::Unexpected,
                format!("the manifest will not serialize: {error}"),
            )
        })?;
        self.upload_file(MANIFEST_PATH, &body, None).await?;

        Ok(SyncPass {
            uploaded_bytes,
            uploaded_members,
            deleted,
            refused_deletions,
            full,
            unchanged: false,
            manifest: local,
        })
    }

    /// The tree at `remote` in the guest, written under `dir` through [`LocalTree::extract`]:
    /// only the regular files `globs` select, never under `.git`, never outside `dir`.
    ///
    /// The daemon packs the tree (`GET /v1/fs/tar`), so members are relative to `remote`.
    pub async fn download_dir(
        &self,
        tree: &dyn LocalTree,
        remote: &str,
        globs: &[String],
        dir: &Path,
    ) -> Result<Vec<Artifact>, Error> {
        let archive = self.download_tar(remote).await?;
        Ok(tree.extract(&archive, globs, dir)?)
    }
}

#[cfg(test)]
mod tests {
    use std::sync::{Arc, Mutex};

    use super::*;
    use crate::session::testing::{Recorder, Reply, session_with};

    /// A tree whose manifest the test chooses: the pass's rules, with no filesystem. Its packs
    /// name the members they carry, one per line, so a test reads what travelled.
    #[derive(Default)]
    struct FakeTree {
        manifest: Manifest,
        extracted: Mutex<Vec<(Vec<u8>, Vec<String>)>>,
    }

    impl LocalTree for FakeTree {
        fn manifest(&self, _dir: &Path) -> Result<Manifest, WorkspaceError> {
            Ok(self.manifest.clone())
        }

        fn pack(&self, _dir: &Path) -> Result<Packed, WorkspaceError> {
            Err(WorkspaceError::new(
                "a sync pass packs only the changed paths",
            ))
        }

        fn pack_paths(&self, _dir: &Path, paths: &[String]) -> Result<Packed, WorkspaceError> {
            Ok(Packed {
                archive: paths.join("\n").into_bytes(),
                members: paths.len(),
            })
        }

        fn extract(
            &self,
            archive: &[u8],
            globs: &[String],
            _dir: &Path,
        ) -> Result<Vec<Artifact>, WorkspaceError> {
            self.extracted
                .lock()
                .expect("not poisoned")
                .push((archive.to_vec(), globs.to_vec()));
            Ok(vec![Artifact {
                path: "dist/app".into(),
                bytes: 3,
            }])
        }
    }

    fn files(entries: &[(&str, &str)]) -> Manifest {
        Manifest {
            version: MANIFEST_VERSION,
            files: entries
                .iter()
                .map(|(path, hash)| ((*path).to_string(), (*hash).to_string()))
                .collect(),
            ..Manifest::default()
        }
    }

    fn exited(exit_code: i32) -> Reply {
        Reply::ok(serde_json::json!({
            "exec_id": "x", "phase": "exited", "exit_code": exit_code, "signal": null,
            "stdout": "", "stderr": "", "truncated": false, "writers_may_be_alive": false
        }))
    }

    /// **A second pass after one edit uploads that one member and deletes nothing (#260).**
    ///
    /// The whole incremental bet: an archive proportional to the edit. The guest manifest is
    /// rewritten to the tree as the pass left it, so the next pass diffs against it.
    ///
    /// **Falsification**: pack the whole local manifest instead of the delta's paths, and the
    /// upload carries both members.
    #[tokio::test]
    async fn a_pass_after_one_edit_uploads_that_member_and_deletes_nothing() {
        let recorder = Recorder::with([Reply::Body(200, Vec::new()), Reply::Body(200, Vec::new())]);
        let (session, _, _) = session_with(Arc::clone(&recorder));
        let tree = FakeTree {
            manifest: files(&[("a.txt", "edited"), ("b.txt", "same")]),
            ..FakeTree::default()
        };
        let baseline = files(&[("a.txt", "old"), ("b.txt", "same")]);

        let pass = session
            .sync_pass(
                &tree,
                Path::new("/tmp/project"),
                Some(baseline),
                Duration::from_secs(60),
            )
            .await
            .expect("syncs");

        let requests = recorder.requests();
        assert_eq!(requests.len(), 2, "{requests:?}");
        assert_eq!(requests[0].path, "/v1/fs/tar?path=%2Fworkspace");
        assert_eq!(requests[0].body, b"a.txt", "only the edited member travels");
        assert_eq!(
            requests[1].path,
            "/v1/fs/file?path=%2Fworkspace%2F.microvm-sync-manifest.json"
        );
        let written: Manifest = serde_json::from_slice(&requests[1].body).expect("a manifest");
        assert_eq!(written, tree.manifest);
        assert_eq!((pass.uploaded_members, pass.deleted), (1, 0));
        assert!(!pass.full && !pass.unchanged);
    }

    /// An unchanged tree sends nothing at all, not even the manifest.
    #[tokio::test]
    async fn an_unchanged_tree_sends_nothing() {
        let recorder = Recorder::with([]);
        let (session, _, _) = session_with(Arc::clone(&recorder));
        let tree = FakeTree {
            manifest: files(&[("a.txt", "same")]),
            ..FakeTree::default()
        };
        let pass = session
            .sync_pass(
                &tree,
                Path::new("/tmp/p"),
                Some(tree.manifest.clone()),
                Duration::from_secs(60),
            )
            .await
            .expect("syncs");
        assert!(pass.unchanged);
        assert!(recorder.requests().is_empty());
    }

    /// **A deletion the guest's manifest orders runs only for a plain relative path.**
    ///
    /// The manifest is the VM's word, so `../../etc/passwd` in it is refused, counted and
    /// never handed to the in-guest `rm`, which gets the one safe path, from the workspace.
    ///
    /// **Falsification**: hand every deletion path to the `rm`, and its argv carries the
    /// hostile one.
    #[tokio::test(start_paused = true)]
    async fn a_hostile_deletion_path_is_refused_not_executed() {
        let recorder = Recorder::with([
            Reply::ok(serde_json::json!({"exec_id": "x", "phase": "running"})),
            exited(0),
            exited(0),
            Reply::Body(200, Vec::new()),
        ]);
        let (session, _, _) = session_with(Arc::clone(&recorder));
        let tree = FakeTree {
            manifest: files(&[("kept.txt", "same")]),
            ..FakeTree::default()
        };
        let baseline = files(&[
            ("kept.txt", "same"),
            ("gone.txt", "x"),
            ("../../etc/passwd", "x"),
        ]);

        let pass = session
            .sync_pass(
                &tree,
                Path::new("/tmp/p"),
                Some(baseline),
                Duration::from_secs(60),
            )
            .await
            .expect("syncs");

        let start = recorder
            .requests()
            .into_iter()
            .find(|request| request.path == "/v1/exec/start")
            .expect("an rm went out");
        let body: serde_json::Value = serde_json::from_slice(&start.body).expect("JSON");
        assert_eq!(
            body["command"],
            serde_json::json!(["rm", "-rf", "--", "gone.txt"]),
            "the rm carries only the deletable path"
        );
        assert_eq!(body["cwd"], "/workspace");
        assert_eq!(body["timeout_sec"], 60.0);
        assert_eq!((pass.deleted, pass.refused_deletions), (1, 1));
    }

    /// A removal that fails fails the pass, and the manifest isn't rewritten, so the next pass
    /// orders the deletion again.
    #[tokio::test(start_paused = true)]
    async fn a_failed_removal_fails_the_pass_and_keeps_the_old_manifest() {
        let recorder = Recorder::with([
            Reply::ok(serde_json::json!({"exec_id": "x", "phase": "running"})),
            exited(1),
            exited(1),
        ]);
        let (session, _, _) = session_with(Arc::clone(&recorder));
        let tree = FakeTree::default();
        let err = session
            .sync_pass(
                &tree,
                Path::new("/tmp/p"),
                Some(files(&[("gone.txt", "x")])),
                Duration::from_secs(60),
            )
            .await
            .expect_err("the rm failed");
        assert_eq!(err.code(), "ERR_EXEC_FAILED", "{err}");
        assert!(err.to_string().contains("next sync will order"), "{err}");
        assert!(
            !recorder
                .requests()
                .iter()
                .any(|request| request.path.contains("manifest")),
            "the manifest was rewritten after a failed removal"
        );
    }

    /// The guest's manifest: `None` when absent or unreadable, the manifest otherwise.
    #[tokio::test]
    async fn the_guest_manifest_is_none_when_absent_or_unreadable() {
        let written = files(&[("a.txt", "h")]);
        let recorder = Recorder::with([
            Reply::Body(404, b"no such file".to_vec()),
            Reply::Body(200, b"not json".to_vec()),
            Reply::Body(200, serde_json::to_vec(&written).expect("JSON")),
            Reply::Body(401, b"wrong token".to_vec()),
        ]);
        let (session, _, _) = session_with(recorder);
        assert_eq!(session.sync_manifest().await.expect("absent"), None);
        assert_eq!(session.sync_manifest().await.expect("unreadable"), None);
        assert_eq!(session.sync_manifest().await.expect("read"), Some(written));
        let err = session
            .sync_manifest()
            .await
            .expect_err("a 401 is not an absence");
        assert_eq!(err.code(), "ERR_CREDENTIALS");
    }

    /// A one-shot sync reads the guest's manifest first, and `full` skips it, so everything
    /// travels.
    ///
    /// **Falsification**: read the manifest whatever `full` says, and the full sync's first
    /// request is the manifest read instead of the upload.
    #[tokio::test]
    async fn sync_dir_diffs_against_the_guests_manifest_unless_full() {
        let tree = FakeTree {
            manifest: files(&[("a.txt", "same"), ("b.txt", "new")]),
            ..FakeTree::default()
        };
        let guest = files(&[("a.txt", "same")]);
        let recorder = Recorder::with([
            Reply::Body(200, serde_json::to_vec(&guest).expect("JSON")),
            Reply::Body(200, Vec::new()),
            Reply::Body(200, Vec::new()),
            Reply::Body(200, Vec::new()),
            Reply::Body(200, Vec::new()),
        ]);
        let (session, _, _) = session_with(Arc::clone(&recorder));

        let incremental = session
            .sync_dir(
                &tree,
                Path::new("/tmp/p"),
                false,
                DEFAULT_SYNC_DELETE_TIMEOUT,
            )
            .await
            .expect("syncs");
        assert_eq!(incremental.uploaded_members, 1, "only b.txt is new");
        assert!(!incremental.full);

        let full = session
            .sync_dir(
                &tree,
                Path::new("/tmp/p"),
                true,
                DEFAULT_SYNC_DELETE_TIMEOUT,
            )
            .await
            .expect("syncs");
        assert!(full.full);
        assert_eq!(full.uploaded_members, 2, "a full sync sends everything");
        let paths: Vec<String> = recorder.requests().into_iter().map(|r| r.path).collect();
        assert_eq!(
            paths[3], "/v1/fs/tar?path=%2Fworkspace",
            "a full sync starts with the upload, not a manifest read: {paths:?}"
        );
    }

    /// `download_dir` hands the daemon's archive of the remote tree and the caller's globs to
    /// the tree's guarded extraction.
    #[tokio::test]
    async fn download_dir_extracts_the_remote_archive_with_the_callers_globs() {
        let recorder = Recorder::with([Reply::Body(200, b"tar bytes".to_vec())]);
        let (session, _, _) = session_with(Arc::clone(&recorder));
        let tree = FakeTree::default();
        let got = session
            .download_dir(
                &tree,
                "/workspace/out",
                &["dist/**".into()],
                Path::new("/tmp/d"),
            )
            .await
            .expect("downloads");
        assert_eq!(got.len(), 1);
        assert_eq!(recorder.last().path, "/v1/fs/tar?path=%2Fworkspace%2Fout");
        assert_eq!(
            *tree.extracted.lock().expect("not poisoned"),
            [(b"tar bytes".to_vec(), vec!["dist/**".to_string()])]
        );
    }

    /// The pack budgets are the daemon's caps, `max_body_bytes` (512 MiB) and
    /// `max_tar_members` (`crates/agentd/src/config.rs`), with the cap itself inside them.
    #[test]
    fn the_pack_budgets_are_the_daemons_caps_inclusive() {
        assert_eq!(MAX_PACK_BYTES, 536_870_912);
        assert_eq!(MAX_PACK_MEMBERS, 100_000);
        assert!(within_byte_budget(536_870_912));
        assert!(!within_byte_budget(536_870_913));
        assert!(within_member_budget(100_000));
        assert!(!within_member_budget(100_001));
    }

    /// A sync's `rm` waits its budget plus the delete grace, with no floor of its own: the
    /// daemon's deadline is the budget, so a zero one is the daemon's to refuse.
    ///
    /// **Falsification**: `verify/guards/faults/seconds-flags.toml` entry `cli-sync-client-wait`
    /// subtracts the grace instead, and the 60 s row reads 30 s.
    #[test]
    fn a_sync_waits_its_budget_plus_the_delete_grace() {
        assert_eq!(
            delete_wait(Duration::from_secs(60)),
            Duration::from_secs(90)
        );
        assert_eq!(delete_wait(Duration::ZERO), Duration::from_secs(30));
    }

    /// A local tree's failure is `ERR_INVALID_ARG` and carries its [`WorkspaceError`] as the
    /// source, so an adapter can report it on a row of its own without reading the message.
    #[test]
    fn a_workspace_error_is_invalid_arg_with_itself_as_the_source() {
        let err = Error::from(WorkspaceError::new("the tree exceeds the budget"));
        assert_eq!(err.to_string(), "the tree exceeds the budget");
        assert_eq!(err.code(), "ERR_INVALID_ARG");
        assert_eq!(err.wire_kind(), None);
        let source = std::error::Error::source(&err).expect("a source");
        assert_eq!(
            source.downcast_ref::<WorkspaceError>(),
            Some(&WorkspaceError::new("the tree exceeds the budget"))
        );
    }

    /// Each change class lands in the right half of the delta: edits and additions
    /// upload, disappearances delete, and the unchanged member stays home.
    #[test]
    fn a_diff_names_changed_new_and_deleted_members_and_nothing_else() {
        let mut remote = Manifest {
            version: MANIFEST_VERSION,
            ..Manifest::default()
        };
        remote.files.insert("same.txt".into(), "hash-same".into());
        remote.files.insert("edited.txt".into(), "hash-old".into());
        remote.files.insert("removed.txt".into(), "hash-x".into());
        remote.dirs.insert("gone-dir".into());
        remote.dirs.insert("gone-dir/nested".into());
        remote.symlinks.insert("link".into(), "old-target".into());

        let mut local = Manifest {
            version: MANIFEST_VERSION,
            ..Manifest::default()
        };
        local.files.insert("same.txt".into(), "hash-same".into());
        local.files.insert("edited.txt".into(), "hash-new".into());
        local.files.insert("added.txt".into(), "hash-add".into());
        local.symlinks.insert("link".into(), "new-target".into());
        remote.dirs.insert("kept-dir".into());
        local.dirs.insert("kept-dir".into());
        local.dirs.insert("new-dir".into());

        let delta = diff(&local, &remote);
        assert_eq!(delta.upload, ["added.txt", "edited.txt", "link", "new-dir"]);
        // Deepest first, so a non-recursive remove works in this order.
        assert_eq!(delta.delete, ["removed.txt", "gone-dir/nested", "gone-dir"]);
    }

    /// The manifest never orders its own deletion.
    ///
    /// It lives in the workspace (see [`MANIFEST_PATH`] on why) and is therefore in the
    /// remote tree without being in any local one, the one permanent asymmetry the
    /// diff has to know about.
    #[test]
    fn a_diff_never_deletes_the_manifest_itself() {
        let mut remote = Manifest::default();
        remote.files.insert(MANIFEST_NAME.into(), "hash".into());
        let delta = diff(&Manifest::default(), &remote);
        assert!(delta.is_empty(), "{delta:?}");
    }

    /// A deletion path from the guest's manifest is removed only when it's a plain relative
    /// path: the manifest is the VM's word.
    #[test]
    fn only_a_plain_relative_path_is_deletable() {
        for kept in ["a.txt", "src/lib.rs", "a/b/c", ".hidden", "x..y"] {
            assert!(deletable(kept), "{kept}");
        }
        for refused in [
            "",
            "/etc/passwd",
            "../x",
            "a/../b",
            "a//b",
            "a/",
            "a\\b",
            "..",
        ] {
            assert!(!deletable(refused), "{refused:?}");
        }
    }
}
