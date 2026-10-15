// SPDX-License-Identifier: Apache-2.0
#![cfg(target_os = "linux")]
//! Runs the client's exact guest script with distinct real owners. This tier needs
//! passwordless sudo; `test:upload-ownership` and Linux CI run it explicitly.

use std::fs;
use std::os::unix::fs::{MetadataExt, PermissionsExt, symlink};
use std::path::Path;
use std::process::{Command, Output};

const SCRIPT: &str = include_str!("../../microvms-app/src/session/upload.sh");
const USER: &str = "65534";

fn command(root: bool, cwd: &Path, program: &str, args: &[&str]) -> Output {
    let mut command = Command::new("sudo");
    command.arg("-n").current_dir(cwd);
    if !root {
        command.args(["-u", "#65534", "-g", "#65534"]);
    }
    command.arg("--").arg(program).args(args);
    command
        .output()
        .expect("passwordless sudo is required for this test tier")
}

fn script(root: bool, cwd: &Path, args: &[&str]) -> Output {
    let mut argv = vec!["-c", SCRIPT, "microvms-upload"];
    let relative: Vec<_> = args
        .iter()
        .map(|arg| {
            arg.strip_prefix(cwd.to_str().unwrap())
                .map(|arg| arg.trim_start_matches('/'))
                .unwrap_or(arg)
        })
        .collect();
    argv.extend_from_slice(&relative);
    command(root, cwd, "/bin/sh", &argv)
}

fn success(output: Output) {
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

struct Tree(tempfile::TempDir);

impl Tree {
    fn new() -> Self {
        let tree = Self(tempfile::tempdir().unwrap());
        // Model the guest's /tmp: users can remove their own handed-off stage.
        fs::set_permissions(tree.0.path(), fs::Permissions::from_mode(0o1777)).unwrap();
        tree
    }

    fn path(&self, name: &str) -> String {
        self.0.path().join(name).to_str().unwrap().to_string()
    }
}

impl Drop for Tree {
    fn drop(&mut self) {
        success(command(
            true,
            self.0.path(),
            "rm",
            &["-rf", "--", self.0.path().to_str().unwrap()],
        ));
    }
}

#[test]
#[ignore = "requires passwordless sudo; run mise run test:upload-ownership"]
fn private_tree_handoff_has_real_nonroot_ownership_and_preserves_link_targets() {
    let tree = Tree::new();
    let stage = tree.path("stage");
    let payload = tree.path("stage/payload");
    let destination = tree.path("destination");
    fs::create_dir_all(&payload).unwrap();
    fs::write(Path::new(&payload).join("file"), b"content").unwrap();
    fs::hard_link(
        Path::new(&payload).join("file"),
        Path::new(&payload).join("hardlink"),
    )
    .unwrap();
    let outside = tree.path("outside");
    fs::write(&outside, b"secret").unwrap();
    symlink(&outside, Path::new(&payload).join("link")).unwrap();
    success(command(
        true,
        tree.0.path(),
        "chown",
        &["-hR", "0:0", &stage, &outside],
    ));
    success(command(true, tree.0.path(), "chmod", &["0700", &stage]));
    assert!(
        !command(false, tree.0.path(), "touch", &["stage/payload/before"])
            .status
            .success()
    );

    success(script(
        true,
        tree.0.path(),
        &["handoff", &stage, USER, USER],
    ));
    for path in [&stage, &payload, &format!("{payload}/file")] {
        let owner = command(true, tree.0.path(), "stat", &["-c", "%u:%g", path]);
        assert!(owner.status.success());
        assert_eq!(
            String::from_utf8(owner.stdout).unwrap().trim(),
            "65534:65534"
        );
    }
    assert_eq!(
        fs::metadata(&outside).unwrap().uid(),
        0,
        "chown followed a symlink"
    );
    success(command(true, tree.0.path(), "mkdir", &[&destination]));
    success(command(
        true,
        tree.0.path(),
        "chown",
        &["65534:65534", &destination],
    ));
    success(script(
        false,
        tree.0.path(),
        &["install-tar", &stage, &destination],
    ));
    assert!(
        !Path::new(&stage).exists(),
        "successful install left a stage"
    );
    assert_eq!(fs::read(format!("{destination}/file")).unwrap(), b"content");
    assert_eq!(
        fs::metadata(format!("{destination}/file")).unwrap().ino(),
        fs::metadata(format!("{destination}/hardlink"))
            .unwrap()
            .ino()
    );
    assert!(
        fs::symlink_metadata(format!("{destination}/link"))
            .unwrap()
            .file_type()
            .is_symlink()
    );
    success(command(
        false,
        tree.0.path(),
        "touch",
        &["destination/created"],
    ));
    assert_eq!(
        fs::metadata(format!("{destination}/created"))
            .unwrap()
            .uid(),
        65534
    );
}

#[test]
#[ignore = "requires passwordless sudo; run mise run test:upload-ownership"]
fn planted_destination_symlinks_and_swapped_directories_are_refused() {
    let tree = Tree::new();
    let outside = tree.path("outside");
    fs::create_dir(&outside).unwrap();
    fs::write(format!("{outside}/secret"), b"unchanged").unwrap();
    fs::set_permissions(
        format!("{outside}/secret"),
        fs::Permissions::from_mode(0o666),
    )
    .unwrap();
    let original_owner = fs::metadata(format!("{outside}/secret")).unwrap().uid();
    let planted = tree.path("planted");
    symlink(&outside, &planted).unwrap();
    success(command(
        true,
        tree.0.path(),
        "chown",
        &["-hR", "65534:65534", &planted],
    ));
    for destination in [&planted, &format!("{planted}/child/file")] {
        let output = script(true, tree.0.path(), &["prepare", destination, USER, USER]);
        assert!(!output.status.success());
        assert!(String::from_utf8_lossy(&output.stderr).contains("symbolic link"));
    }

    let stage = tree.path("stage");
    let payload = tree.path("stage/payload/sub");
    fs::create_dir_all(&payload).unwrap();
    fs::write(format!("{payload}/file"), b"new").unwrap();
    fs::write(tree.path("stage/payload/file"), b"replacement").unwrap();
    success(command(
        true,
        tree.0.path(),
        "chown",
        &["-hR", "0:0", &stage],
    ));
    success(command(true, tree.0.path(), "chmod", &["0700", &stage]));
    success(script(
        true,
        tree.0.path(),
        &["handoff", &stage, USER, USER],
    ));
    let destination = tree.path("destination");
    fs::create_dir(&destination).unwrap();
    symlink(&outside, format!("{destination}/sub")).unwrap();
    success(command(
        true,
        tree.0.path(),
        "chown",
        &["-hR", "65534:65534", &destination],
    ));
    let output = script(false, tree.0.path(), &["install-tar", &stage, &destination]);
    assert!(
        !output.status.success(),
        "a swapped directory symlink was traversed"
    );
    assert_eq!(fs::read(format!("{outside}/secret")).unwrap(), b"unchanged");
    assert!(!Path::new(&outside).join("file").exists());
    assert_eq!(
        fs::metadata(format!("{outside}/secret")).unwrap().uid(),
        original_owner
    );

    success(command(false, tree.0.path(), "rm", &["destination/sub"]));
    success(command(
        false,
        tree.0.path(),
        "ln",
        &["-s", &format!("{outside}/secret"), "destination/file"],
    ));
    let output = script(false, tree.0.path(), &["install-tar", &stage, &destination]);
    assert!(
        !output.status.success(),
        "a file member followed a destination symlink"
    );
    assert_eq!(fs::read(format!("{outside}/secret")).unwrap(), b"unchanged");
    assert_eq!(
        fs::metadata(format!("{outside}/secret")).unwrap().uid(),
        original_owner
    );
}

#[test]
#[ignore = "requires passwordless sudo; run mise run test:upload-ownership"]
fn cleanup_after_handoff_cannot_remove_a_substituted_root_directory() {
    let tree = Tree::new();
    let stage = tree.path("stage");
    fs::create_dir_all(tree.path("stage/payload")).unwrap();
    success(command(
        true,
        tree.0.path(),
        "chown",
        &["-R", "0:0", &stage],
    ));
    success(command(true, tree.0.path(), "chmod", &["0700", &stage]));
    success(script(
        true,
        tree.0.path(),
        &["handoff", &stage, USER, USER],
    ));
    let parent = tree.path("writable");
    fs::create_dir(&parent).unwrap();
    success(command(
        true,
        tree.0.path(),
        "chown",
        &["65534:65534", &parent],
    ));
    let replacement = tree.path("writable/replacement");
    success(command(true, tree.0.path(), "mkdir", &[&replacement]));
    success(command(
        true,
        tree.0.path(),
        "mkdir",
        &[&format!("{replacement}/private")],
    ));
    success(command(
        true,
        tree.0.path(),
        "cp",
        &["/dev/null", &format!("{replacement}/private/secret")],
    ));
    success(command(
        true,
        tree.0.path(),
        "chown",
        &["-R", "0:0", &replacement],
    ));
    success(command(
        true,
        tree.0.path(),
        "chmod",
        &["0777", &replacement],
    ));
    success(command(
        true,
        tree.0.path(),
        "chmod",
        &["0700", &format!("{replacement}/private")],
    ));
    // Model a directory movable from a workload-writable parent into the old
    // stage name. A lost handoff reply must not make root recurse through it.
    success(command(
        false,
        tree.0.path(),
        "mv",
        &["stage", "handed-off"],
    ));
    success(command(
        false,
        tree.0.path(),
        "mv",
        &["writable/replacement", "stage"],
    ));
    assert!(
        !script(false, tree.0.path(), &["cleanup", &stage])
            .status
            .success()
    );
    let secret = command(true, tree.0.path(), "cat", &["stage/private/secret"]);
    assert!(secret.status.success());
    assert!(secret.stdout.is_empty());
}

#[test]
#[ignore = "requires passwordless sudo; run mise run test:upload-ownership"]
fn upload_to_a_writable_root_owned_directory_keeps_its_ownership_and_mode() {
    let tree = Tree::new();
    let stage = tree.path("stage");
    fs::create_dir_all(tree.path("stage/payload")).unwrap();
    fs::write(tree.path("stage/payload/file"), b"content").unwrap();
    success(command(
        true,
        tree.0.path(),
        "chown",
        &["-R", "0:0", &stage],
    ));
    success(command(true, tree.0.path(), "chmod", &["0700", &stage]));
    success(script(
        true,
        tree.0.path(),
        &["handoff", &stage, USER, USER],
    ));
    let destination = tree.path("destination");
    success(command(true, tree.0.path(), "mkdir", &[&destination]));
    success(command(
        true,
        tree.0.path(),
        "chmod",
        &["1777", &destination],
    ));
    success(script(
        false,
        tree.0.path(),
        &["install-tar", &stage, &destination],
    ));
    let metadata = fs::metadata(&destination).unwrap();
    assert_eq!(metadata.uid(), 0);
    assert_eq!(metadata.mode() & 0o7777, 0o1777);
    assert_eq!(fs::read(format!("{destination}/file")).unwrap(), b"content");
}

#[test]
#[ignore = "requires passwordless sudo; run mise run test:upload-ownership"]
fn root_handoff_ignores_commands_planted_in_the_inherited_path() {
    let tree = Tree::new();
    let stage = tree.path("stage");
    fs::create_dir_all(tree.path("stage/payload")).unwrap();
    fs::write(tree.path("stage/payload/file"), b"content").unwrap();
    let planted = tree.path("planted");
    fs::create_dir(&planted).unwrap();
    let trap = format!("{planted}/chown");
    fs::write(&trap, "#!/bin/sh\ntouch compromised\nexit 42\n").unwrap();
    fs::set_permissions(&trap, fs::Permissions::from_mode(0o755)).unwrap();
    success(command(
        true,
        tree.0.path(),
        "chown",
        &["-R", "0:0", &stage],
    ));
    success(command(true, tree.0.path(), "chmod", &["0700", &stage]));
    let malicious_path = format!("PATH={planted}:/usr/bin:/bin");
    success(command(
        true,
        tree.0.path(),
        "/usr/bin/env",
        &[
            &malicious_path,
            "/bin/sh",
            "-c",
            SCRIPT,
            "microvms-upload",
            "handoff",
            "stage",
            USER,
            USER,
        ],
    ));
    assert!(!Path::new(&tree.path("compromised")).exists());
    let owner = command(
        true,
        tree.0.path(),
        "stat",
        &["-c", "%u:%g", "stage/payload/file"],
    );
    assert!(owner.status.success());
    assert_eq!(
        String::from_utf8(owner.stdout).unwrap().trim(),
        "65534:65534"
    );
}

/// The shipped script runs unchanged in a private mount namespace with only its
/// install tools visible. In particular, no system directory can supply `find`.
fn minimal_install(tree: &Tree, tools: &Path, stage: &str, destination: &str) -> Output {
    command(
        true,
        tree.0.path(),
        "/usr/bin/env",
        &[
            "SHELLOPTS=noglob",
            "BASHOPTS=failglob:dotglob:nullglob",
            "GLOBIGNORE=*",
            "unshare",
            "--mount",
            "--fork",
            "/bin/sh",
            "-c",
            r#"set -eu
mount --make-rprivate /
for directory in /usr/bin /usr/sbin /bin /sbin; do
    mount --bind "$1" "$directory"
done
if command -v find; then exit 99; fi
# Model the helper's explicit request environment, whose keys the app tests pin.
exec setpriv --reuid=65534 --regid=65534 --clear-groups env SHELLOPTS= BASHOPTS= GLOBIGNORE= /bin/sh -c "$2" microvms-upload install-tar "$3" "$4"
"#,
            "minimal-install",
            tools.to_str().unwrap(),
            SCRIPT,
            stage,
            destination,
        ],
    )
}

#[test]
#[ignore = "requires passwordless sudo; run mise run test:upload-ownership"]
fn minimal_tools_install_hidden_and_unusual_names_without_following_links() {
    let tree = Tree::new();
    let tools = tree.0.path().join("tools");
    fs::create_dir(&tools).unwrap();
    // mount and setpriv belong to the test harness, not the guest script. Copies
    // keep ownership changes in this test from ever touching system binaries.
    for tool in ["mount", "setpriv", "env", "mkdir", "cp", "rm"] {
        fs::copy(format!("/usr/bin/{tool}"), tools.join(tool)).unwrap();
    }
    // Amazon Linux uses Bash for /bin/sh. Imported glob options must not change
    // traversal; the launch environment above deliberately starts with them set.
    fs::copy("/bin/bash", tools.join("sh")).unwrap();
    let names = [
        "plain/sub/file",
        ".hidden/file",
        "..dots/file",
        "space\nand tab\t/file",
    ];
    let payload = tree.0.path().join("stage/payload");
    for name in names {
        let path = payload.join(name);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, name.as_bytes()).unwrap();
    }
    fs::write(payload.join("safe"), b"safe").unwrap();
    symlink("missing", payload.join("dangling")).unwrap();
    let outside = tree.0.path().join("outside");
    fs::create_dir(&outside).unwrap();
    fs::write(outside.join("secret"), b"unchanged").unwrap();
    symlink(&outside, payload.join("source-link")).unwrap();
    success(command(
        true,
        tree.0.path(),
        "chown",
        &["-R", "0:0", "stage"],
    ));
    success(command(true, tree.0.path(), "chmod", &["0700", "stage"]));
    success(script(
        true,
        tree.0.path(),
        &["handoff", "stage", USER, USER],
    ));

    for (index, member) in [
        "plain/sub",
        ".hidden",
        "..dots",
        "space\nand tab\t",
        "plain/sub/file",
        ".hidden/file",
    ]
    .into_iter()
    .enumerate()
    {
        let destination = format!("refused-{index}");
        let link = tree.0.path().join(&destination).join(member);
        fs::create_dir_all(link.parent().unwrap()).unwrap();
        // Include dangling destination links as well as live directory links.
        let target = if index % 2 == 0 {
            outside.clone()
        } else {
            outside.join("missing")
        };
        symlink(target, &link).unwrap();
        success(command(
            true,
            tree.0.path(),
            "chown",
            &["-hR", "65534:65534", &destination],
        ));
        let output = minimal_install(&tree, &tools, "stage", &destination);
        assert!(
            !output.status.success(),
            "followed destination link {member:?}"
        );
        assert!(
            String::from_utf8_lossy(&output.stderr).contains("symbolic link"),
            "unexpected refusal for {member:?}: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        assert!(!tree.0.path().join(&destination).join("safe").exists());
        assert_eq!(fs::read(outside.join("secret")).unwrap(), b"unchanged");
    }

    fs::create_dir(tree.0.path().join("destination")).unwrap();
    symlink("old-missing", tree.0.path().join("destination/dangling")).unwrap();
    success(command(
        true,
        tree.0.path(),
        "chown",
        &["-hR", "65534:65534", "destination"],
    ));
    success(minimal_install(&tree, &tools, "stage", "destination"));
    for name in names {
        let path = tree.0.path().join("destination").join(name);
        assert_eq!(fs::read(&path).unwrap(), name.as_bytes());
        assert_eq!(fs::metadata(path).unwrap().uid(), 65534);
    }
    assert_eq!(
        fs::read_link(tree.0.path().join("destination/dangling")).unwrap(),
        Path::new("missing")
    );
    assert_eq!(
        fs::read_link(tree.0.path().join("destination/source-link")).unwrap(),
        outside
    );
    assert!(!tree.0.path().join("stage").exists());

    fs::create_dir_all(tree.0.path().join("empty/payload")).unwrap();
    success(command(
        true,
        tree.0.path(),
        "chown",
        &["-R", "0:0", "empty"],
    ));
    success(command(true, tree.0.path(), "chmod", &["0700", "empty"]));
    success(script(
        true,
        tree.0.path(),
        &["handoff", "empty", USER, USER],
    ));
    success(minimal_install(&tree, &tools, "empty", "empty-destination"));
    assert!(tree.0.path().join("empty-destination").is_dir());
    assert!(!tree.0.path().join("empty").exists());
}
