// SPDX-License-Identifier: Apache-2.0
//! Build-time capture of the builder's final Dockerfile USER.

use std::ffi::OsString;
use std::fs::{File, OpenOptions, Permissions};
use std::io::{self, Read, Write};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::Path;

use serde::{Deserialize, Serialize};

pub const SETTING: &str = "AGENTD_PRESERVE_IMAGE_USER";
pub const RECORD: &str = "/agentd-image-user.json";
const MAX_RECORD_BYTES: u64 = 128;

#[derive(Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
struct Identity {
    uid: u32,
    gid: u32,
}

/// These modes run only during the image build, before daemon initialization.
/// The builder resolves inherited stages, variables and USER names for us.
pub fn build_mode(
    mut args: impl Iterator<Item = OsString>,
    record: &Path,
) -> Option<io::Result<()>> {
    let mode = args.next()?;
    if args.next().is_some() {
        return None;
    }
    match mode.to_str()? {
        "--capture-image-user" => Some(capture(record)),
        "--seal-image-user" => Some(seal(record)),
        _ => None,
    }
}

fn open(path: &Path, writable: bool) -> io::Result<File> {
    let file = OpenOptions::new()
        .read(true)
        .write(writable)
        .custom_flags(nix::libc::O_NOFOLLOW)
        .open(path)?;
    let metadata = file.metadata()?;
    if !metadata.is_file() || metadata.uid() != 0 {
        return Err(io::Error::other(
            "image user record must be a root-owned regular file",
        ));
    }
    Ok(file)
}

fn capture(path: &Path) -> io::Result<()> {
    // COPY prepares a writable file even when the final USER has no passwd row,
    // writable home, shell, or permission to create a file in the root directory.
    let mut file = open(path, true)?;
    let identity = Identity {
        uid: nix::unistd::geteuid().as_raw(),
        gid: nix::unistd::getegid().as_raw(),
    };
    let bytes = serde_json::to_vec(&identity)?;
    file.set_len(0)?;
    file.write_all(&bytes)?;
    file.sync_all()
}

fn read(file: &mut File) -> io::Result<Identity> {
    let mut bytes = Vec::new();
    file.take(MAX_RECORD_BYTES + 1).read_to_end(&mut bytes)?;
    if bytes.len() as u64 > MAX_RECORD_BYTES {
        return Err(io::Error::other("image user record is too large"));
    }
    serde_json::from_slice(&bytes).map_err(io::Error::other)
}

fn seal(path: &Path) -> io::Result<()> {
    if !nix::unistd::geteuid().is_root() {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "sealing the image user requires root",
        ));
    }
    let mut file = open(path, false)?;
    read(&mut file)?;
    // No workload runs between capture and sealing. Only the sealed record is
    // accepted at daemon startup; a missing or writable record never means root.
    file.set_permissions(Permissions::from_mode(0o444))
}

pub fn default_user() -> io::Result<String> {
    read_default(Path::new(RECORD))
}

fn read_default(path: &Path) -> io::Result<String> {
    read_sealed(open(path, false)?)
}

fn read_sealed(mut file: File) -> io::Result<String> {
    if file.metadata()?.mode() & 0o022 != 0 {
        return Err(io::Error::other("image user record is not sealed"));
    }
    let identity = read(&mut file)?;
    Ok(format!("{}:{}", identity.uid, identity.gid))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(target_os = "linux")]
    fn in_root_namespace(test: &str) -> bool {
        const CHILD: &str = "AGENTD_IMAGE_USER_TEST_CHILD";
        if std::env::var(CHILD).as_deref() == Ok(test) {
            assert!(nix::unistd::geteuid().is_root());
            return true;
        }
        // The child creates root-owned temporary files through its private UID
        // mapping; ownership and credentials in the parent namespace stay unchanged.
        let output = std::process::Command::new("unshare")
            .args(["--user", "--map-root-user"])
            .arg(std::env::current_exe().unwrap())
            .args(["--exact", test, "--nocapture"])
            .env(CHILD, test)
            .output()
            .expect("unshare is required for the isolated image-user test");
        assert!(
            output.status.success(),
            "isolated image-user test: {}\n{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
        false
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn build_modes_capture_and_seal_the_selected_record() {
        if !in_root_namespace("image_user::tests::build_modes_capture_and_seal_the_selected_record")
        {
            return;
        }
        let dir = tempfile::tempdir().unwrap();
        let record = dir.path().join("record");
        std::fs::write(&record, b"stale contents longer than the captured identity").unwrap();
        std::fs::set_permissions(&record, Permissions::from_mode(0o666)).unwrap();

        build_mode(
            [OsString::from("--capture-image-user")].into_iter(),
            &record,
        )
        .expect("capture mode must be dispatched")
        .unwrap();
        let expected = Identity {
            uid: nix::unistd::geteuid().as_raw(),
            gid: nix::unistd::getegid().as_raw(),
        };
        assert_eq!(
            serde_json::from_slice::<Identity>(&std::fs::read(&record).unwrap()).unwrap(),
            expected
        );
        assert!(
            read_default(&record).is_err(),
            "capture must still be unsealed"
        );

        build_mode([OsString::from("--seal-image-user")].into_iter(), &record)
            .expect("seal mode must be dispatched")
            .unwrap();
        assert_eq!(std::fs::metadata(&record).unwrap().mode() & 0o777, 0o444);
        assert_eq!(
            read_default(&record).unwrap(),
            format!("{}:{}", expected.uid, expected.gid)
        );
    }

    #[test]
    fn build_modes_ignore_other_arguments_and_preserve_errors() {
        use std::os::unix::ffi::OsStringExt;

        let dir = tempfile::tempdir().unwrap();
        let record = dir.path().join("absent");
        for args in [
            vec![],
            vec![OsString::from("--unknown")],
            vec![
                OsString::from("--capture-image-user"),
                OsString::from("extra"),
            ],
            vec![OsString::from("--seal-image-user"), OsString::from("extra")],
            vec![OsString::from_vec(vec![0xff])],
        ] {
            assert!(build_mode(args.into_iter(), &record).is_none());
            assert!(!record.exists());
        }
        for mode in ["--capture-image-user", "--seal-image-user"] {
            assert!(
                build_mode([OsString::from(mode)].into_iter(), &record)
                    .expect("recognized build mode")
                    .is_err()
            );
            assert!(!record.exists());
        }
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn open_requires_a_root_owned_regular_file() {
        if !in_root_namespace("image_user::tests::open_requires_a_root_owned_regular_file") {
            return;
        }
        let dir = tempfile::tempdir().unwrap();
        let record = dir.path().join("record");
        std::fs::write(&record, br#"{"uid":4242,"gid":4343}"#).unwrap();
        assert_eq!(open(&record, false).unwrap().metadata().unwrap().uid(), 0);
        assert!(
            open(dir.path(), false)
                .unwrap_err()
                .to_string()
                .contains("root-owned regular file")
        );
    }

    #[test]
    fn open_refuses_a_non_root_owned_regular_file() {
        let dir = tempfile::tempdir().unwrap();
        let record = dir.path().join("record");
        std::fs::write(&record, br#"{"uid":4242,"gid":4343}"#).unwrap();
        if nix::unistd::geteuid().is_root() {
            nix::unistd::chown(&record, Some(nix::unistd::Uid::from_raw(4242)), None).unwrap();
        }
        assert_ne!(std::fs::metadata(&record).unwrap().uid(), 0);
        assert!(
            open(&record, false)
                .unwrap_err()
                .to_string()
                .contains("root-owned regular file")
        );
    }

    #[test]
    fn captured_identity_is_bounded_and_explicit() {
        let mut file = tempfile::tempfile().unwrap();
        file.write_all(br#"{"uid":4242,"gid":4343}"#).unwrap();
        use std::io::{Seek, SeekFrom};
        file.seek(SeekFrom::Start(0)).unwrap();
        assert_eq!(
            read(&mut file).unwrap(),
            Identity {
                uid: 4242,
                gid: 4343
            }
        );
        for bytes in [b"{}".as_slice(), br#"{"uid":1,"gid":-1}"#, &[b' '; 129]] {
            let mut file = tempfile::tempfile().unwrap();
            file.write_all(bytes).unwrap();
            file.seek(SeekFrom::Start(0)).unwrap();
            assert!(read(&mut file).is_err());
        }
    }

    #[test]
    fn a_writable_capture_is_never_a_runtime_default() {
        let mut file = tempfile::tempfile().unwrap();
        file.write_all(br#"{"uid":4242,"gid":4343}"#).unwrap();
        file.set_permissions(Permissions::from_mode(0o666)).unwrap();
        assert!(
            read_sealed(file)
                .unwrap_err()
                .to_string()
                .contains("not sealed")
        );
        let mut file = tempfile::tempfile().unwrap();
        file.write_all(br#"{"uid":4242,"gid":4343}"#).unwrap();
        file.set_permissions(Permissions::from_mode(0o444)).unwrap();
        use std::io::{Seek, SeekFrom};
        file.seek(SeekFrom::Start(0)).unwrap();
        assert_eq!(read_sealed(file).unwrap(), "4242:4343");
    }

    #[test]
    fn a_symlink_cannot_select_an_image_user_record() {
        let dir = tempfile::tempdir().unwrap();
        let target = dir.path().join("target");
        std::fs::write(&target, br#"{"uid":0,"gid":0}"#).unwrap();
        let link = dir.path().join("link");
        std::os::unix::fs::symlink(&target, &link).unwrap();
        assert_eq!(
            read_default(&link).unwrap_err().raw_os_error(),
            Some(nix::libc::ELOOP)
        );
    }
}
