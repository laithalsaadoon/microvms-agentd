// SPDX-License-Identifier: Apache-2.0
//! Reading a build context from a directory, the way `docker build` reads one (#221, IMAGE-7).
//!
//! What enters the artifact, and why, is `microvms_app::control::context`: the rules, the
//! entry names and the ignore-file matcher are pure and live there. This is the walk over the
//! filesystem that feeds them.

use std::path::Path;

use microvms_app::control::context::{
    BuildContext, ContextEntry, DOCKERFILE_ENTRY, IGNORE_FILES, IgnoreRules, MAX_CONTEXT_BYTES,
};
use microvms_app::error::Error;

/// Reads `dir` as a build context: every regular file its ignore rules leave in, with the
/// root `Dockerfile` and ignore files left out and symlinks skipped with a warning.
///
/// Refuses a directory that does not exist, a malformed ignore file, a root file named
/// `agentd`, a path that is not UTF-8 (a zip entry name is text), and a context over
/// [`MAX_CONTEXT_BYTES`].
///
/// `BuildContext::from_dir(dir)` (`microvms_core::prelude::BuildContextExt`) is this under the name
/// it had, kept apart from the type because it reads the filesystem.
pub fn from_dir(dir: impl AsRef<Path>) -> Result<BuildContext, Error> {
    let root = dir.as_ref();
    if !root.is_dir() {
        return Err(Error::invalid_arg(format!(
            "the build context {} is not a directory. Pass the directory the task's \
             Dockerfile builds from.",
            root.display()
        )));
    }
    let rules = match IGNORE_FILES
        .iter()
        .map(|name| root.join(name))
        .find(|path| path.is_file())
    {
        Some(path) => {
            let text = std::fs::read_to_string(&path).map_err(|error| {
                Error::invalid_arg(format!("could not read {}: {error}", path.display()))
            })?;
            IgnoreRules::parse(&text)
                .map_err(|error| Error::invalid_arg(format!("{}: {error}", path.display())))?
        }
        None => IgnoreRules::default(),
    };

    let mut walk = Walk {
        rules,
        entries: Vec::new(),
        warnings: Vec::new(),
        total: 0,
    };
    walk.dir(root, "")?;
    Ok(BuildContext::from_entries(walk.entries)?.with_warnings(walk.warnings))
}

struct Walk {
    rules: IgnoreRules,
    entries: Vec<ContextEntry>,
    warnings: Vec<String>,
    total: u64,
}

impl Walk {
    fn dir(&mut self, dir: &Path, prefix: &str) -> Result<(), Error> {
        let listing = std::fs::read_dir(dir).map_err(|error| {
            Error::invalid_arg(format!("could not list {}: {error}", dir.display()))
        })?;
        let mut children: Vec<_> = listing.collect::<Result<_, _>>().map_err(|error| {
            Error::invalid_arg(format!("could not list {}: {error}", dir.display()))
        })?;
        children.sort_by_key(|child| child.file_name());
        for child in children {
            let file_name = child.file_name();
            let Some(file_name) = file_name.to_str() else {
                return Err(Error::invalid_arg(format!(
                    "the build context holds {}, whose name is not UTF-8; a zip entry name \
                     is text. Rename it or exclude it in .dockerignore.",
                    child.path().display()
                )));
            };
            let name = if prefix.is_empty() {
                file_name.to_string()
            } else {
                format!("{prefix}/{file_name}")
            };
            if prefix.is_empty()
                && (name == DOCKERFILE_ENTRY || IGNORE_FILES.contains(&name.as_str()))
            {
                continue;
            }
            let path = child.path();
            let kind = std::fs::symlink_metadata(&path).map_err(|error| {
                Error::invalid_arg(format!("could not read {}: {error}", path.display()))
            })?;
            let excluded = self.rules.excludes(&name);
            if kind.file_type().is_symlink() {
                if !excluded {
                    self.warnings.push(format!(
                        "skipped {name}: it is a symlink, which the build artifact cannot \
                         carry; copy the file it points to into the context instead"
                    ));
                }
                continue;
            }
            if kind.is_dir() {
                // An excluded directory is walked anyway only when an exception could bring
                // something under it back.
                if !excluded || self.rules.has_exceptions() {
                    self.dir(&path, &name)?;
                }
                continue;
            }
            if excluded {
                continue;
            }
            if !kind.is_file() {
                self.warnings.push(format!(
                    "skipped {name}: it is not a regular file (a socket, FIFO or device), \
                     which the build artifact cannot carry"
                ));
                continue;
            }
            self.total += kind.len();
            if self.total > MAX_CONTEXT_BYTES {
                return Err(Error::invalid_arg(format!(
                    "the build context is over {MAX_CONTEXT_BYTES} bytes (5 GiB) by {name}, \
                     which is S3's limit for the single PutObject the artifact is uploaded \
                     with. Exclude what the Dockerfile does not copy in .dockerignore."
                )));
            }
            let bytes = std::fs::read(&path).map_err(|error| {
                Error::invalid_arg(format!("could not read {}: {error}", path.display()))
            })?;
            self.entries.push(ContextEntry {
                name,
                mode: mode_of(&kind),
                bytes,
            });
        }
        Ok(())
    }
}

#[cfg(unix)]
fn mode_of(metadata: &std::fs::Metadata) -> u32 {
    use std::os::unix::fs::PermissionsExt as _;
    if metadata.permissions().mode() & 0o111 != 0 {
        0o755
    } else {
        0o644
    }
}

#[cfg(not(unix))]
fn mode_of(_metadata: &std::fs::Metadata) -> u32 {
    0o644
}

#[cfg(test)]
mod tests {
    use super::*;
    use microvms_app::error::ErrorKind;

    /// A scratch directory under the system temp dir, removed on drop.
    struct Scratch(std::path::PathBuf);

    impl Scratch {
        fn new(label: &str) -> Self {
            let mut bytes = [0u8; 8];
            getrandom::fill(&mut bytes).expect("randomness");
            let dir = std::env::temp_dir().join(format!(
                "microvms-context-{label}-{}",
                const_hex::encode(bytes)
            ));
            std::fs::create_dir_all(&dir).expect("scratch dir");
            Self(dir)
        }

        fn write(&self, name: &str, bytes: &[u8]) -> &Self {
            let path = self.0.join(name);
            std::fs::create_dir_all(path.parent().expect("a parent")).expect("dirs");
            std::fs::write(path, bytes).expect("write");
            self
        }
    }

    impl Drop for Scratch {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    /// **IMAGE-7, the walk.** Every regular file the rules leave in, sorted by name, with
    /// the execute bit carried as `0o755` and everything else `0o644`; the root `Dockerfile`
    /// and ignore files are left out because the artifact carries the wrapped Dockerfile, and
    /// an ignore file inside the artifact would let the platform's own build exclude the
    /// daemon entry.
    #[cfg(unix)]
    #[test]
    fn a_directory_becomes_its_included_files_sorted_with_modes() {
        use std::os::unix::fs::PermissionsExt as _;

        let scratch = Scratch::new("walk");
        scratch
            .write("Dockerfile", b"FROM x\n")
            .write(".dockerignore", b"*.log\nsecret.txt\n")
            .write("secret.txt", b"do not ship")
            .write("app/run.sh", b"#!/bin/sh\necho hi\n")
            .write("app/data.txt", b"data")
            .write("debug.log", b"noise")
            .write("sub/Dockerfile", b"FROM nested\n")
            .write("zeta.txt", b"z");
        let run = scratch.0.join("app/run.sh");
        std::fs::set_permissions(&run, std::fs::Permissions::from_mode(0o750)).expect("chmod");

        let context = from_dir(&scratch.0).expect("a readable context");
        assert_eq!(
            names(&context),
            ["app/data.txt", "app/run.sh", "sub/Dockerfile", "zeta.txt"],
            "IMAGE-7"
        );
        let mode = |name: &str| {
            context
                .entries()
                .iter()
                .find(|entry| entry.name == name)
                .expect("present")
                .mode
        };
        assert_eq!(mode("app/run.sh"), 0o755);
        assert_eq!(mode("app/data.txt"), 0o644);
        assert!(context.warnings().is_empty(), "{:?}", context.warnings());
    }

    /// **IMAGE-7, the precedence.** `Dockerfile.dockerignore` beside the Dockerfile replaces
    /// `.dockerignore` entirely, as Docker's Dockerfile-specific ignore file does.
    #[test]
    fn the_dockerfile_specific_ignore_file_takes_precedence() {
        let scratch = Scratch::new("precedence");
        scratch
            .write(".dockerignore", b"a.txt\n")
            .write("Dockerfile.dockerignore", b"b.txt\n")
            .write("a.txt", b"a")
            .write("b.txt", b"b");
        let context = from_dir(&scratch.0).expect("readable");
        assert_eq!(names(&context), ["a.txt"], "IMAGE-7");
    }

    /// **IMAGE-7, the links.** A symlink cannot ride in the artifact, so it is skipped and
    /// named in a warning rather than followed (a link out of the context would copy files
    /// the caller never put there) or silently dropped.
    #[cfg(unix)]
    #[test]
    fn a_symlink_is_skipped_with_a_warning_naming_it() {
        let scratch = Scratch::new("links");
        scratch.write("real.txt", b"r");
        std::os::unix::fs::symlink("real.txt", scratch.0.join("link.txt")).expect("symlink");
        std::os::unix::fs::symlink("/etc", scratch.0.join("etc")).expect("symlink");
        let context = from_dir(&scratch.0).expect("readable");
        assert_eq!(names(&context), ["real.txt"]);
        let warnings = context.warnings().join("\n");
        assert!(warnings.contains("link.txt"), "IMAGE-7: {warnings}");
        assert!(warnings.contains("etc"), "IMAGE-7: {warnings}");
        assert!(warnings.contains("symlink"), "{warnings}");
    }

    /// **IMAGE-7, the collision.** A context file named `agentd` at the root would replace
    /// the daemon entry, so it is refused naming the file.
    #[test]
    fn a_context_file_named_like_the_daemon_is_refused() {
        let scratch = Scratch::new("collide");
        scratch.write("agentd", b"not the daemon");
        let error = from_dir(&scratch.0).expect_err("collides");
        assert_eq!(error.kind(), ErrorKind::InvalidArg);
        assert!(error.to_string().contains("agentd"), "{error}");

        let error = from_dir(scratch.0.join("missing")).expect_err("no dir");
        assert_eq!(error.kind(), ErrorKind::InvalidArg);
    }

    fn names(context: &BuildContext) -> Vec<&str> {
        context
            .entries()
            .iter()
            .map(|entry| entry.name.as_str())
            .collect()
    }
}
