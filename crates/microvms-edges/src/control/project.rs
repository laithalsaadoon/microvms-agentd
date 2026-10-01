// SPDX-License-Identifier: Apache-2.0
//! Reading a project directory's dependency files (#74, #264).
//!
//! Which pair counts, and every refusal, is `microvms_app::control::project`. This asks the
//! filesystem which names are present and reads the two files the rule names.

use std::path::Path;

use microvms_app::control::artifact::ProjectFiles;
use microvms_app::control::project::project_ecosystem;
use microvms_app::error::{Error, ErrorKind};

/// The one manifest+lockfile pair in `dir`, read into [`ProjectFiles`], or the refusal
/// [`project_ecosystem`] makes: no pair, a half pair, or two ecosystems.
pub fn read_project_files(dir: impl AsRef<Path>) -> Result<ProjectFiles, Error> {
    let dir = dir.as_ref();
    let ecosystem = project_ecosystem(&dir.display().to_string(), |name| dir.join(name).is_file())?;
    let read = |name: &str| {
        let path = dir.join(name);
        std::fs::read(&path).map_err(|error| {
            Error::new(
                ErrorKind::Precondition,
                format!("could not read {}: {error}", path.display()),
            )
            .with_source(error)
        })
    };
    Ok(ProjectFiles {
        ecosystem,
        manifest: read(ecosystem.manifest_name())?,
        lockfile: read(ecosystem.lockfile_name())?,
    })
}

#[cfg(test)]
mod tests {
    use microvms_app::control::artifact::Ecosystem;

    use super::*;

    /// **#74: the pair on disk is read whole**, the manifest and the lockfile under the
    /// ecosystem's names, and nothing else in the directory.
    #[test]
    fn a_project_dir_with_one_pair_reads_both_files() {
        let dir = tempfile::tempdir().expect("a temp dir");
        std::fs::write(dir.path().join("pyproject.toml"), b"[project]").expect("writes");
        std::fs::write(dir.path().join("uv.lock"), b"version = 1").expect("writes");
        std::fs::write(dir.path().join(".env"), b"SECRET=1").expect("writes");

        let files = read_project_files(dir.path()).expect("one pair, one ecosystem");
        assert_eq!(files.ecosystem, Ecosystem::Uv);
        assert_eq!(files.manifest, b"[project]");
        assert_eq!(files.lockfile, b"version = 1");
    }

    /// The rule's refusals reach a caller through the read, naming the directory.
    #[test]
    fn a_project_dir_without_a_pair_is_refused_naming_it() {
        let dir = tempfile::tempdir().expect("a temp dir");
        std::fs::write(dir.path().join("pyproject.toml"), b"[project]").expect("writes");
        let error = read_project_files(dir.path()).expect_err("no lockfile to key on");
        assert_eq!(error.kind(), ErrorKind::Precondition);
        let message = error.to_string();
        assert!(message.contains("uv lock"), "{message}");
        assert!(
            message.contains(&dir.path().display().to_string()),
            "{message}"
        );
    }
}
