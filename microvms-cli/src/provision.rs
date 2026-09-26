// SPDX-License-Identifier: Apache-2.0
//! The CLI's door to `microvms_core::provision`: the daemon binary for a `run` or `build`
//! that was given none, reported in the envelope's vocabulary.
//!
//! # Why the CLI fetches its own daemon
//!
//! The original headline command made every first-time caller download the daemon by hand
//! and pass its path as a positional (`microvm run ./agentd`). No polished CLI hands its
//! user a path to its own plumbing, so `run`/`build` with no binary resolve one:
//!
//! 1. the typed positional or `binary` in `microvm.toml` (never reaches this module),
//! 2. `$MICROVM_AGENTD`, a path for the caller who manages the binary themselves,
//! 3. the version-matched cache under the state directory,
//! 4. a fetch from this repository's GitHub release for the CLI's **own** version.
//!
//! Steps 2 through 4 are `microvms_core::provision::resolve`, the same chain the Python
//! and Node bindings call (issue #219), so every surface agrees on what is verified and
//! what is refused. This module keeps what is the CLI's own: the envelope's source labels
//! (`env`, `cache`, `fetched`) and the exit-code row and remedies of each refusal.
//!
//! # The fetch goes through a subprocess, and CLI-2 is why
//!
//! `tests/thinness.rs` forbids this crate every HTTP client by name, and core's fetcher
//! downloads through `gh` or `curl` subprocesses, never a client of its own. The seam is
//! core's [`Fetch`]: the shipped binary carries [`SubprocessFetch`], and the guards script
//! it, so no test can open a socket to GitHub — the same arrangement that keeps them off
//! AWS.

use std::path::{Path, PathBuf};

use microvms_core::provision::{self as core, Failure, ProvisionError, Request};
pub use microvms_core::provision::{Fetch, SubprocessFetch, Verification};

use crate::exit::{CliError, Exit};

/// Where the resolved binary came from, reported on the envelope beside the path.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Source {
    /// `$MICROVM_AGENTD`.
    Env,
    /// Already installed under the state directory by an earlier fetch.
    Cache,
    /// Fetched from the GitHub release during this invocation.
    Fetched(Verification),
}

impl Source {
    pub fn as_str(self) -> &'static str {
        match self {
            Source::Env => "env",
            Source::Cache => "cache",
            Source::Fetched(_) => "fetched",
        }
    }
}

/// A resolved daemon binary: the path the build will read, and the story of how it got there.
#[derive(Debug)]
pub struct Resolved {
    pub path: PathBuf,
    pub source: Source,
}

/// A [`Fetch`] that panics, for tests whose invocation must never need one — the
/// [`crate::seam::PanickingSeam`] arrangement, applied to the second kind of egress.
/// `cfg(test)` for that struct's own reason: nothing in the shipped binary refuses a
/// fetch on purpose.
#[cfg(test)]
pub struct PanickingFetch;

#[cfg(test)]
impl Fetch for PanickingFetch {
    fn fetch(&self, tag: &str, _: &Path, _: &mut dyn FnMut(&str)) -> Result<Verification, String> {
        panic!("this invocation must not fetch (asked for {tag})");
    }
}

/// Resolve a daemon binary for `version` through core's chain.
///
/// `state_dir` is the same directory the run ledger and name registry use — resolved by the
/// caller through [`crate::seam::state_dir`], so `--state-dir` and `$MICROVM_STATE_DIR`
/// move the cache with everything else.
pub fn resolve(
    state_dir: &Path,
    version: &str,
    env: &dyn Fn(&str) -> Option<String>,
    fetch: &dyn Fetch,
    progress: &mut dyn FnMut(&str),
) -> Result<Resolved, CliError> {
    let request = Request {
        version: Some(version),
        state_dir: Some(state_dir),
        binary: None,
    };
    let provisioned = core::resolve(&request, env, fetch, progress).map_err(refusal)?;
    let source = match provisioned.source {
        core::Source::CallerSupplied(_) => Source::Env,
        core::Source::Cache(_) => Source::Cache,
        core::Source::Fetched(verification) => Source::Fetched(verification),
    };
    Ok(Resolved {
        path: provisioned.path,
        source,
    })
}

/// A core refusal as its exit-code row, with core's remedies as the envelope's suggestions.
fn refusal(error: ProvisionError) -> CliError {
    let exit = match error.failure {
        Failure::InvalidVersion => Exit::InvalidArg,
        Failure::CallerBinary(_) | Failure::NotAarch64 | Failure::Fetch | Failure::Io => {
            Exit::Precondition
        }
    };
    error
        .remedies
        .into_iter()
        .fold(CliError::new(exit, error.message), CliError::suggest)
}

#[cfg(test)]
mod tests {
    //! The policy's own tests moved to `microvms-edges/src/provision.rs` with the policy;
    //! these pin what this module adds: the envelope's labels and the exit-code rows.
    use super::*;
    use std::cell::Cell;

    /// A scripted fetch: writes an aarch64 ELF header to the destination and counts calls.
    struct Scripted {
        calls: Cell<usize>,
    }

    impl Fetch for Scripted {
        fn fetch(
            &self,
            _: &str,
            dest: &Path,
            _: &mut dyn FnMut(&str),
        ) -> Result<Verification, String> {
            self.calls.set(self.calls.get() + 1);
            std::fs::write(dest, elf_header(core::REQUIRED_ELF_MACHINE)).expect("writes");
            Ok(Verification::Attestation)
        }
    }

    /// A fetch that always fails, for the error-path assertions.
    struct Failing;

    impl Fetch for Failing {
        fn fetch(
            &self,
            _: &str,
            _: &Path,
            _: &mut dyn FnMut(&str),
        ) -> Result<Verification, String> {
            Err("no network in tests".into())
        }
    }

    fn elf_header(machine: u16) -> Vec<u8> {
        let mut header = vec![0u8; 20];
        header[..4].copy_from_slice(b"\x7fELF");
        header[5] = 1;
        header[18..20].copy_from_slice(&machine.to_le_bytes());
        header
    }

    fn no_env(_: &str) -> Option<String> {
        None
    }

    fn sink() -> impl FnMut(&str) {
        |_: &str| {}
    }

    /// **A cache miss fetches once; the next resolve is a cache hit**, reported as the
    /// envelope's `fetched` and then `cache`.
    #[test]
    fn a_cache_miss_fetches_once_and_the_next_resolve_reads_the_cache() {
        let dir = tempfile::tempdir().expect("a temp dir");
        let fetch = Scripted {
            calls: Cell::new(0),
        };
        let mut progress = sink();

        let first = resolve(dir.path(), "9.9.9", &no_env, &fetch, &mut progress).expect("resolves");
        assert_eq!(first.source, Source::Fetched(Verification::Attestation));
        assert_eq!(first.source.as_str(), "fetched");
        assert_eq!(fetch.calls.get(), 1);
        assert_eq!(first.path, core::cache_path(dir.path(), "9.9.9"));

        let second =
            resolve(dir.path(), "9.9.9", &no_env, &fetch, &mut progress).expect("resolves");
        assert_eq!(second.source.as_str(), "cache");
        assert_eq!(fetch.calls.get(), 1, "a cache hit must not fetch again");
        assert_eq!(second.path, first.path);
    }

    /// **`$MICROVM_AGENTD` outranks the cache**, and is reported as `env`.
    #[test]
    fn the_environment_override_outranks_the_cache_and_never_fetches() {
        let dir = tempfile::tempdir().expect("a temp dir");
        resolve(
            dir.path(),
            "9.9.9",
            &no_env,
            &Scripted {
                calls: Cell::new(0),
            },
            &mut sink(),
        )
        .expect("caches");
        let own = dir.path().join("my-agentd");
        std::fs::write(&own, elf_header(core::REQUIRED_ELF_MACHINE)).expect("writes");

        let own_str = own.display().to_string();
        let env = move |name: &str| (name == core::ENV_OVERRIDE).then(|| own_str.clone());
        let resolved =
            resolve(dir.path(), "9.9.9", &env, &PanickingFetch, &mut sink()).expect("resolves");
        assert_eq!(resolved.source.as_str(), "env");
        assert_eq!(resolved.path, own);
    }

    /// A stale or non-aarch64 `$MICROVM_AGENTD` is `ERR_PRECONDITION` naming the variable,
    /// with core's remedies as suggestions.
    #[test]
    fn a_refused_environment_override_is_a_precondition_naming_the_variable() {
        let dir = tempfile::tempdir().expect("a temp dir");
        let env =
            |name: &str| (name == core::ENV_OVERRIDE).then(|| "/definitely/not/here".to_string());
        let failure = resolve(dir.path(), "9.9.9", &env, &PanickingFetch, &mut sink())
            .expect_err("a stale override must refuse");
        assert_eq!(failure.exit, Exit::Precondition);
        assert!(
            failure.message.contains(core::ENV_OVERRIDE),
            "{}",
            failure.message
        );
        assert!(
            failure.suggestions.iter().any(|s| s.contains("unset")),
            "{:?}",
            failure.suggestions
        );

        let x86 = dir.path().join("x86-agentd");
        std::fs::write(&x86, elf_header(0x3E)).expect("writes");
        let x86_str = x86.display().to_string();
        let env = move |name: &str| (name == core::ENV_OVERRIDE).then(|| x86_str.clone());
        let failure = resolve(dir.path(), "9.9.9", &env, &PanickingFetch, &mut sink())
            .expect_err("an x86 override must refuse");
        assert_eq!(failure.exit, Exit::Precondition);
        assert!(failure.message.contains("0x3e"), "{}", failure.message);
    }

    /// A failed fetch is `ERR_PRECONDITION` carrying the tag and every way out: the manual
    /// `gh` spelling, the override variable, and the self-build.
    #[test]
    fn a_failed_fetch_names_the_tag_and_every_way_out() {
        let dir = tempfile::tempdir().expect("a temp dir");
        let failure =
            resolve(dir.path(), "9.9.9", &no_env, &Failing, &mut sink()).expect_err("no network");
        assert_eq!(failure.exit, Exit::Precondition);
        assert!(failure.message.contains("v9.9.9"), "{}", failure.message);
        let remedies = failure.suggestions.join("\n");
        assert!(remedies.contains("gh release download"), "{remedies}");
        assert!(remedies.contains(core::ENV_OVERRIDE), "{remedies}");
        assert!(
            remedies.contains("cargo build --release -p agentd"),
            "{remedies}"
        );
    }

    /// A version that is not a tag is the `ERR_INVALID_ARG` row, not a precondition.
    #[test]
    fn a_version_that_is_not_a_tag_is_an_invalid_argument() {
        let dir = tempfile::tempdir().expect("a temp dir");
        let failure = resolve(dir.path(), "../x", &no_env, &PanickingFetch, &mut sink())
            .expect_err("must refuse");
        assert_eq!(failure.exit, Exit::InvalidArg);
    }
}
