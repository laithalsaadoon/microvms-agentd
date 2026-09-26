// SPDX-License-Identifier: Apache-2.0
//! The MicroVMs client's use cases, written only against ports (ARCH-7).
//!
//! The control-plane client, [`sandbox::Sandbox`], [`session::Session`], `ensure_image` and
//! the agent recipes are here. Everything they do outside the process goes through a trait
//! this crate declares: [`control::transport::Transport`] for the control plane,
//! [`control::BuildServices`] for STS and S3, [`session::HttpBackend`] for the daemon,
//! [`session::TokenMinter`] for proxy tokens, [`names::NameStore`] for the name registry,
//! [`clock::Clock`] for time, [`entropy::Entropy`] for randomness, and [`adapters::Adapters`]
//! for the pieces a use case builds partway through.
//!
//! # What this crate can't do
//!
//! Reach the network, AWS, the filesystem, a subprocess, the wall clock or the OS random pool.
//! A use case that did one of those directly couldn't be tested without the real thing, and it
//! would bypass the port a test replaces. These checks hold that:
//!
//! * the exact dependency set in `arch/placement.toml`, which
//!   `microvms-cli/tests/dependency_direction.rs` asserts along with each dependency's
//!   features, keeps reqwest, the AWS crates, getrandom and the socket crates out;
//! * `clippy.toml` beside this crate's manifest bans the std file, network, process,
//!   environment and clock calls, and tokio's `net`, `fs` and `process` items, which feature
//!   unification would otherwise let this crate name;
//! * the crate root below forbids both lints, so a hit fails the build and an inner `#[allow]`
//!   or `#[expect]` is itself an error.
//!
//! The production implementations are in `microvms-edges`. `microvms-core` wires them into the
//! constructors a caller has always used (`ControlPlane::new`, `Sandbox::new`,
//! `Session::connect`) and re-exports every item here at its `microvms_core::` path, so a
//! consumer names these types through core and never depends on this crate.
//!
//! The shared test doubles are in `testing`, behind the `test-support` feature.

// CLI-7: a print macro panics when its stream's reader has gone (#216). Every write goes
// through a checked writer instead.
#![deny(clippy::print_stdout, clippy::print_stderr)]
// ARCH-7: `clippy.toml` beside this crate's manifest names the file, process, network,
// environment and clock items this crate may not touch. Forbidden rather than denied, so a hit
// fails the build and an inner `#[allow]` or `#[expect]` is itself an error (E0453): a use case
// that needs one of them needs a port.
#![forbid(clippy::disallowed_methods, clippy::disallowed_types)]

pub mod adapters;
pub mod agents;
pub mod clock;
pub mod control;
pub mod entropy;
pub mod names;
pub mod preflight;
pub mod sandbox;
pub mod session;
#[cfg(any(test, feature = "test-support"))]
pub mod testing;

// The rules and values the use cases compute with, at the paths the moved code names them by.
pub use microvms_domain::{constants, cost, error, hooks, identity, region, sizing};

// Re-exported so the use cases and their callers name wire types through one crate.
pub use protocol;

pub use error::{Error, ErrorKind, WireKind};
pub use hooks::{BuildHookTimeout, RunHookTimeout};
pub use region::Region;
pub use sizing::SizeClass;
