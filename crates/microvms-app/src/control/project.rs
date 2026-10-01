// SPDX-License-Identifier: Apache-2.0
//! Which manifest+lockfile pair a project directory holds (#74, #264).
//!
//! The rule is here and the filesystem read is `microvms_edges::control::project`, the split
//! [`super::context`] makes: this decides from which names are present, and the edge reads the
//! two files the decision names. Every surface's project option (the CLI's `build --project`,
//! the bindings' `project_dir`) goes through the one rule, so each refusal reads the same.

use super::artifact::Ecosystem;
use crate::error::{Error, ErrorKind};

/// The one ecosystem whose manifest and lockfile are both in `dir`, as `present` reports each
/// file name, or the refusal naming the remedy.
///
/// Exactly one ecosystem, and both halves of its pair, because each miss has a different
/// remedy and the refusal names it:
///
/// * **No pair at all**: the directory is not a project this feature understands, and the
///   message lists all three pairs it looked for.
/// * **A manifest without its lockfile**: the environment layer is keyed on the lockfile
///   (#74), so there is nothing to key on, and the message names the command that writes one.
/// * **A lockfile without its manifest**: the install step reads both, so the layer could
///   never build. Most likely a partial copy.
/// * **Two ecosystems at once**: one image bakes one layer, and nothing says which, so the
///   refusal names both rather than picking one silently.
///
/// `dir` is the directory as the caller named it, for the messages.
pub fn project_ecosystem(dir: &str, present: impl Fn(&str) -> bool) -> Result<Ecosystem, Error> {
    let mut found = Vec::new();
    for ecosystem in Ecosystem::ALL {
        match (
            present(ecosystem.manifest_name()),
            present(ecosystem.lockfile_name()),
        ) {
            (true, true) => found.push(ecosystem),
            (true, false) => {
                return Err(Error::new(
                    ErrorKind::Precondition,
                    format!(
                        "{dir} has {} but no {}: the environment layer is keyed on the \
                         lockfile, so there is nothing to key on. Write one with `{}` and \
                         rebuild.",
                        ecosystem.manifest_name(),
                        ecosystem.lockfile_name(),
                        lock_command(ecosystem),
                    ),
                ));
            }
            (false, true) => {
                return Err(Error::new(
                    ErrorKind::Precondition,
                    format!(
                        "{dir} has {} but no {}: the install step reads both, so the layer \
                         could never build. This usually means a partial copy of the project.",
                        ecosystem.lockfile_name(),
                        ecosystem.manifest_name(),
                    ),
                ));
            }
            (false, false) => {}
        }
    }
    match found.as_slice() {
        [ecosystem] => Ok(*ecosystem),
        [] => Err(Error::new(
            ErrorKind::Precondition,
            format!(
                "{dir} has no dependency files a project build understands. It looks for one \
                 manifest+lockfile pair: pyproject.toml+uv.lock, \
                 package.json+package-lock.json, or Cargo.toml+Cargo.lock."
            ),
        )),
        several => Err(Error::new(
            ErrorKind::Precondition,
            format!(
                "{dir} has dependency files for more than one ecosystem ({}), and one image \
                 bakes one environment layer. Name the directory that owns the layer you want \
                 baked.",
                several
                    .iter()
                    .map(|ecosystem| ecosystem.lockfile_name())
                    .collect::<Vec<_>>()
                    .join(", "),
            ),
        )),
    }
}

/// The command that writes `ecosystem`'s lockfile from its manifest.
fn lock_command(ecosystem: Ecosystem) -> &'static str {
    match ecosystem {
        Ecosystem::Uv => "uv lock",
        Ecosystem::Npm => "npm install --package-lock-only",
        Ecosystem::Cargo => "cargo generate-lockfile",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn holding(names: &[&'static str]) -> impl Fn(&str) -> bool {
        let names = names.to_vec();
        move |name| names.contains(&name)
    }

    /// **#74: a directory with one pair reads as that pair's ecosystem**, whichever of the
    /// three it is.
    #[test]
    fn a_directory_with_one_pair_reads_as_its_ecosystem() {
        for ecosystem in Ecosystem::ALL {
            let found = project_ecosystem(
                "proj",
                holding(&[ecosystem.manifest_name(), ecosystem.lockfile_name()]),
            )
            .expect("one pair, one ecosystem");
            assert_eq!(found, ecosystem);
        }
    }

    /// **#74, the refusals, each naming its remedy.** A manifest without its lockfile has
    /// nothing to key the layer on, and the message names the command that writes one. A
    /// lockfile without its manifest could never install. No pair at all lists what was
    /// looked for. Two ecosystems at once names both, because picking one silently bakes a
    /// layer the caller did not choose.
    ///
    /// **Falsification**, run 2026-10-01, registered in
    /// verify/guards/faults/project-files.toml. Let a manifest without its lockfile count as a
    /// pair (`app-project-manifest-alone-counts`): red on the missing `uv lock` remedy.
    #[test]
    fn a_directory_that_cannot_key_a_layer_is_refused_naming_the_remedy() {
        let error = project_ecosystem("proj", holding(&[])).expect_err("nothing to detect");
        assert_eq!(error.kind(), ErrorKind::Precondition);
        let message = error.to_string();
        assert!(message.contains("pyproject.toml+uv.lock"), "{message}");
        assert!(
            message.contains("package.json+package-lock.json"),
            "{message}"
        );
        assert!(message.contains("Cargo.toml+Cargo.lock"), "{message}");

        let message = project_ecosystem("proj", holding(&["pyproject.toml"]))
            .expect_err("no lockfile to key on")
            .to_string();
        assert!(message.contains("uv lock"), "{message}");
        assert!(message.contains("keyed on the lockfile"), "{message}");

        let message = project_ecosystem("proj", holding(&["uv.lock"]))
            .expect_err("no manifest to install with")
            .to_string();
        assert!(message.contains("partial copy"), "{message}");

        let message = project_ecosystem(
            "proj",
            holding(&[
                "pyproject.toml",
                "uv.lock",
                "package.json",
                "package-lock.json",
            ]),
        )
        .expect_err("two layers, one image")
        .to_string();
        assert!(message.contains("uv.lock"), "{message}");
        assert!(message.contains("package-lock.json"), "{message}");
        assert!(
            message.starts_with("proj "),
            "names the directory: {message}"
        );
    }
}
