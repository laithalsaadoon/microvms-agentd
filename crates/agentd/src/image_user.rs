// SPDX-License-Identifier: Apache-2.0
//! Build-time capture of the builder's final Dockerfile USER.

use std::fs::{File, OpenOptions, Permissions};
use std::io::{self, Read, Write};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::Path;

use serde::{Deserialize, Serialize};

pub const SETTING: &str = "AGENTD_PRESERVE_IMAGE_USER";
const RECORD: &str = "/agentd-image-user.json";
const MAX_RECORD_BYTES: u64 = 128;

#[derive(Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
struct Identity {
    uid: u32,
    gid: u32,
}

/// These modes run only during the image build, before daemon initialization.
/// The builder resolves inherited stages, variables and USER names for us.
pub fn build_mode() -> Option<io::Result<()>> {
    let mut args = std::env::args_os().skip(1);
    let mode = args.next()?;
    if args.next().is_some() {
        return None;
    }
    match mode.to_str()? {
        "--capture-image-user" => Some(capture(Path::new(RECORD))),
        "--seal-image-user" => Some(seal(Path::new(RECORD))),
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
