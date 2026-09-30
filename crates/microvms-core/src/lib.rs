// SPDX-License-Identifier: Apache-2.0
//! The MicroVMs client: the control plane, the in-VM daemon, the cost engine, and
//! every trap closure, behind the one library crate the CLI and the bindings depend on
//! (ARCH-1).
//!
//! # What this crate is for
//!
//! `docs/PLATFORM.md` records seventeen measured findings about AWS Lambda MicroVMs,
//! fifteen of which a client can act on. Most of them are traps in the specific sense
//! that the platform's answer points away from the cause: an unsupported region
//! answers `AccessDeniedException` with a null message, a `clientToken` replay wedges
//! an image in `CREATING` for fifteen hours with no error at all, a
//! `minimumMemoryInMiB` of 512 produces a guest reporting 2 GB. Each finding cost a
//! measurement, and this crate is where that measurement is spent once so no caller
//! has to make it again.
//!
//! The Python client closed the same traps and stayed in the tree as the conformance
//! oracle and the API reference until this port had driven the live suite green; it is
//! git history now. The reason to port was that Rust can make several of those closures
//! *unavailable* rather than merely rejected — see the strength ladder below.
//!
//! # How strongly a trap is closed
//!
//! The spec ranks each closure, strongest first:
//!
//! * **S1, inexpressible** — the mistake cannot be written down. [`region::Region`] is
//!   an enum over the five regions that carry MicroVMs, so a typo'd region is a
//!   compile error rather than a runtime check; [`sizing::SizeClass`] is closed over
//!   the five documented baselines; [`hooks::RunHookTimeout`] and
//!   [`hooks::BuildHookTimeout`] are separate types with no conversion between them,
//!   so a 3600-second build timeout cannot reach a field that caps at 60.
//! * **S2, expressible but rejected** — the mistake can be written and the client
//!   refuses it locally, before any control-plane call, with an error naming the
//!   `docs/PLATFORM.md` finding. Weaker because the guard is code that can regress,
//!   but it costs seconds rather than a build cycle. Every boundary where a bare
//!   integer or string still has to be judged lands here:
//!   [`sizing::SizeClass::from_baseline_mib`], `Region::from_str`.
//! * **S3, correct by default and overridable** — weakest, because it protects the
//!   caller who does nothing and abandons the one who overrides. An S3 closure must
//!   say what the override costs: [`region::Region::unlisted`] says it costs you the
//!   diagnostic.
//!
//! # Every error message names its finding
//!
//! A local reject explains itself by naming the `docs/PLATFORM.md` section that
//! measured the behaviour, because the codes and the guards exist precisely so a
//! reader can go to the measurement rather than to a constraint. "region
//! 'eu-central-1' is invalid" sends someone to check their spelling; the message
//! [`region::Region`] actually raises sends them to the null-message finding.
//!
//! # A guard that cannot fail is worse than no guard
//!
//! Carried over from the Python era unchanged: every guard here has a falsification —
//! a specific plausible edit that must turn a specific test red. "Delete the feature
//! and the test fails" does not count. The clearest case is TRAP-13 in
//! [`sizing`]: every documented peak is exactly four times its baseline, so a test
//! against the shipped table cannot tell a table lookup from `baseline * 4`, and the
//! guard has to drive the lookup over a table where the pattern does not hold.
//!
//! # Layout
//!
//! This crate is the composition root (ARCH-8). The code lives in four crates below it, and
//! every item they export is re-exported here at the path it had in 0.10:
//!
//! * `microvms-domain`: the rules and values, with no I/O (ARCH-6): [`error`], [`region`],
//!   [`sizing`], [`hooks`], [`constants`], [`cost`], and the pure halves of [`names`],
//!   [`preflight`], [`identity`] and [`provision`].
//! * `microvms-app`: the use cases, written only against ports (ARCH-7): [`control`],
//!   [`session`], [`sandbox`], [`agents`], and the ports themselves, [`clock`], [`entropy`] and
//!   [`adapters`].
//! * `microvms-edges`: the production port implementations: the signed transport, the reqwest
//!   backend, the sockets, the name registry on disk, [`provision`]'s fetch, and
//!   [`env`](mod@env), the one process-environment lookup.
//! * `protocol`, the wire contract shared with the daemon.
//!
//! What this crate adds is the wiring: [`prelude`]'s constructors (`ControlPlane::new`,
//! `Sandbox::new`, `Session::connect` and the rest) put the production transport, clock,
//! entropy and adapters into each type's port-taking constructor, and the free functions that
//! read the production clock or pool keep their paths here. `use microvms_core::prelude::*;`
//! keeps a 0.10 call compiling. The shared test doubles are in `testing`, behind the
//! `test-support` feature.

// CLI-7: a print macro panics when its stream's reader has gone (#216). Every write goes
// through a checked writer instead.
#![deny(clippy::print_stdout, clippy::print_stderr)]

pub mod identity;
pub mod prelude;

// The rules and values live in `microvms-domain`, which can't do I/O (ARCH-6). Re-exported
// whole, so every `microvms_core::` path they had still resolves (ARCH-1).
pub use microvms_domain::{constants, cost, error, hooks, region, sizing};

// Whole modules that live in one crate below.
pub use microvms_app::sandbox;
pub use microvms_edges::{env, provision};

// Re-exported so consumers name wire types through this crate rather than
// depending on `protocol` directly — the CLI's thinness guard counts on that.
pub use protocol;

pub use error::{Error, ErrorKind, WireKind};
pub use hooks::{BuildHookTimeout, RunHookTimeout};
pub use region::Region;
pub use sizing::SizeClass;

/// The crate's own version, for a `doctor` or `manifest` command to report.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");

// The README's example is the first code a crates.io reader copies, so it compiles as a
// doctest (`no_run`: it launches a billed VM).
#[cfg(doctest)]
#[doc = include_str!("../README.md")]
struct ReadmeExample;

/// The edges' S3 and STS request helpers stay out of this crate's API: they were
/// `pub(crate)` in 0.10, and one of them returns an `aws-sigv4` type, so exporting it would
/// tie this crate's semver to that one's.
///
/// ```compile_fail
/// let _ = microvms_core::control::services::s3_signing_settings();
/// ```
#[cfg(doctest)]
struct EdgesHelpersStayPrivate;

// Each module below is a use-case module from `microvms-app` with the production pieces
// `microvms-edges` supplies for it, at the paths both had when they were one crate. A local
// item or module shadows a glob, which is how a submodule that has halves in both crates is
// merged.

pub mod adapters {
    //! The adapters port and its production implementation.
    pub use microvms_app::adapters::*;
    pub use microvms_edges::adapters::SystemAdapters;
}

pub mod clock {
    //! The one clock port and tokio's clock.
    pub use microvms_app::clock::*;
    pub use microvms_edges::clock::{SystemClock, TokioClock};
}

pub mod entropy {
    //! The entropy port and the OS random pool.
    pub use microvms_app::entropy::*;
    pub use microvms_edges::entropy::OsEntropy;
}

pub mod names {
    //! Named MicroVMs: the record, the store port, and the CLI's registry on disk.
    pub use microvms_app::names::*;
    pub use microvms_edges::names::FileNameStore;
}

pub mod preflight {
    //! Whether a harness can launch here, checked before it queues work (#223).
    //!
    //! The checks are `microvms_app::preflight`'s; [`preflight`] runs them over the
    //! production control plane.
    pub use microvms_app::preflight::*;

    use crate::control::ControlPlane;
    use crate::prelude::ControlPlaneExt as _;
    use crate::region::Region;

    /// Runs the three checks for `region`, or for the region the environment names.
    ///
    /// The bindings' `preflight`. See the module docs of `microvms_app::preflight` for what
    /// it checks, what it does not, and why nothing it does bills.
    pub async fn preflight(region: Option<Region>) -> PreflightReport {
        let resolved = match region {
            Some(region) => Ok(region),
            None => Region::from_env(&crate::env::process),
        };
        preflight_with(resolved, ControlPlane::new).await
    }
}

pub mod agents {
    //! The coding-agent helpers `docs/AGENT-VMS.md` specifies, and the Bedrock minter.
    pub use microvms_app::agents::*;

    use crate::error::Error;

    pub mod bedrock {
        //! A Bedrock bearer token from the caller's own AWS credentials (AGENT-4).
        pub use microvms_app::agents::bedrock::*;
        pub use microvms_edges::agents::bedrock::*;
    }

    /// The start request for one task (AGENT-7), with an exec id from the production clock
    /// when `options` carries none. [`prompt_request_with`] over [`crate::session::mint_exec_id`].
    pub fn prompt_request(
        spec: &AgentSpec,
        task: &str,
        options: &PromptOptions,
    ) -> Result<crate::protocol::exec::StartRequest, Error> {
        prompt_request_with(spec, task, options, crate::session::mint_exec_id)
    }
}

pub mod control {
    //! The control-plane client, with the signed transport and the build services.
    pub use microvms_app::control::*;
    pub use microvms_edges::clock::SystemClock;
    pub use microvms_edges::control::SignedBuildServices;

    pub mod context {
        //! A task's build context, and reading one from a directory (#221, IMAGE-7).
        pub use microvms_app::control::context::*;
        pub use microvms_edges::control::context::from_dir;
    }

    pub mod services {
        //! The STS and S3 calls `ensure_image` makes, and their SigV4 implementation.
        pub use microvms_app::control::services::*;
        // Named, not globbed: the edges' request helpers are `pub(crate)` there, and a glob
        // would make the next one that goes `pub` (and its aws-sigv4 types) core's API too.
        pub use microvms_edges::control::services::SignedBuildServices;
    }

    pub mod transport {
        //! The control plane's calls, the transport port, and the signed transport.
        pub use microvms_app::control::transport::*;
        pub use microvms_edges::control::transport::SignedTransport;
    }

    pub mod token {
        //! Idempotency tokens, unique per attempt by construction (TRAP-1).
        pub use microvms_app::control::token::*;
        use microvms_edges::entropy::OsEntropy;

        /// An image-create idempotency token, unique per attempt, from the OS random pool.
        ///
        /// [`create_token_with`] over the OS pool. `scope` is a label only, folded in beside
        /// the nonce; see the module docs of `microvms_app::control::token` for the
        /// fifteen-hour wedge that closure exists to prevent. An unavailable pool panics. A
        /// control plane mints through its own source instead, so a launch refuses rather
        /// than panics.
        pub fn create_token(scope: &str) -> String {
            create_token_with(scope, &OsEntropy).expect("the OS random pool is available")
        }

        /// A run idempotency token, unique per attempt, from the OS random pool. Same rule as
        /// [`create_token`].
        pub fn run_token(scope: &str) -> String {
            run_token_with(scope, &OsEntropy).expect("the OS random pool is available")
        }
    }
}

pub mod session {
    //! The in-VM client, with the reqwest backend and the sockets.
    pub use microvms_app::session::*;
    pub use microvms_edges::clock::TokioClock;
    pub use microvms_edges::session::*;

    pub mod http {
        //! The HTTP seam and the production backend.
        pub use microvms_app::session::http::*;
        pub use microvms_edges::session::http::ReqwestBackend;
    }

    pub mod proxy {
        //! The two-header proxy auth and the mint schedule, on tokio's clock in production.
        pub use microvms_app::session::proxy::*;
        pub use microvms_edges::clock::TokioClock;
    }

    pub mod exec {
        //! The exec handle and the cursor-driven stream, with the production exec id.
        pub use microvms_app::session::exec::*;

        pub use super::mint_exec_id;
    }

    /// A fresh exec id from the production clock: `x-` plus 16 hex characters.
    ///
    /// [`exec_id_at`] over tokio's clock. Code holding a [`Session`] mints through the
    /// session's clock instead, so a test that pins the clock pins the id.
    pub fn mint_exec_id() -> String {
        use microvms_app::clock::Clock as _;
        exec_id_at(TokioClock::new().unix_now())
    }
}

#[cfg(feature = "test-support")]
pub mod testing {
    //! The shared test doubles, for a dependent's tests: `microvms_app::testing`.
    pub use microvms_app::testing::*;
}

#[cfg(test)]
mod tests {
    use std::collections::HashSet;

    /// The OS-pool tokens are distinct per call. The nonce is the pool's draw, so this is the
    /// pool's distinctness as the token functions see it (TRAP-1).
    ///
    /// **Falsification**: mint `create_token` from a fixed source and the set collapses to one.
    #[test]
    fn the_os_pool_tokens_never_repeat() {
        let minted: HashSet<String> = (0..200)
            .flat_map(|_| {
                [
                    crate::control::token::create_token("img"),
                    crate::control::token::run_token("img"),
                ]
            })
            .collect();
        assert_eq!(minted.len(), 400);
    }

    /// Two production exec ids differ, even minted in one instant.
    #[test]
    fn production_exec_ids_are_distinct() {
        let minted: HashSet<String> = (0..200).map(|_| crate::session::mint_exec_id()).collect();
        assert_eq!(minted.len(), 200);
    }
}
