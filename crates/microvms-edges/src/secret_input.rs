// SPDX-License-Identifier: Apache-2.0
//! Host credential input from private files or caller-owned byte streams.

use std::io::Read;
use std::path::Path;

use microvms_app::error::Error;

/// Read a credential from a regular file. On Unix, require no group or other permissions.
///
/// Validation uses the opened handle so the permission check and the read refer to the
/// same file even when the directory entry changes. Trailing CR and LF are removed.
pub fn read_secret_file(path: &Path) -> Result<String, Error> {
    let mut options = std::fs::OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        // A special file must not block open before its type can be checked. Regular
        // files ignore this flag, and metadata below validates the same opened handle.
        options.custom_flags(libc::O_NONBLOCK);
    }
    let mut file = options.open(path).map_err(read_error)?;
    let metadata = file.metadata().map_err(read_error)?;
    if !metadata.is_file() {
        return Err(Error::invalid_arg("secret input must name a regular file"));
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if metadata.permissions().mode() & 0o077 != 0 {
            return Err(Error::invalid_arg(
                "secret file must grant no permissions to group or other users",
            ));
        }
    }
    read_secret(&mut file)
}

/// Read a UTF-8 credential through EOF, removing trailing CR and LF and refusing empty input.
/// The caller owns stream selection and keeps other consumers from reading the same stream.
pub fn read_secret(reader: &mut dyn Read) -> Result<String, Error> {
    let mut value = String::new();
    reader.read_to_string(&mut value).map_err(read_error)?;
    let value = value.trim_end_matches(['\r', '\n']);
    if value.is_empty() {
        return Err(Error::invalid_arg("secret input must not be empty"));
    }
    Ok(value.to_owned())
}

fn read_error(error: std::io::Error) -> Error {
    Error::invalid_arg(format!("could not read secret input: {error}"))
}
