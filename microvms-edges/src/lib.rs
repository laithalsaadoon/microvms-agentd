// SPDX-License-Identifier: Apache-2.0
//! The MicroVMs client's production port implementations (ARCH-7).
//!
//! `microvms-app` declares the ports its use cases reach the outside through; this crate
//! implements them over the real thing:
//!
//! * [`control::SignedTransport`] and [`control::SignedBuildServices`]: the default credential
//!   chain, SigV4, and reqwest, for the control plane and for STS and S3;
//! * [`session::http::ReqwestBackend`]: the daemon's HTTP API, one pooled client;
//! * [`session::forward`], [`session::tunnel`] and [`session::shell`]: the sockets, from the
//!   port forwarder's listener to the WebSocket tunnel and its Noise identity proof;
//! * [`names::FileNameStore`]: the CLI's name registry on disk;
//! * [`agents::bedrock::mint`]: a Bedrock bearer token from the caller's credentials;
//! * [`provision`]: the verified `agentd` binary, fetched and cached;
//! * [`clock::TokioClock`], [`entropy::OsEntropy`], and [`adapters::SystemAdapters`], which
//!   hands out the pieces a use case builds partway through.
//!
//! It's the one library crate in the workspace that may depend on a crate doing network, AWS,
//! filesystem, subprocess, clock or entropy I/O. `microvms-core` wires these into the use cases
//! and re-exports every item here at its `microvms_core::` path, so a consumer never depends
//! on this crate directly.

// CLI-7: a print macro panics when its stream's reader has gone (#216). Every write goes
// through a checked writer instead.
#![deny(clippy::print_stdout, clippy::print_stderr)]

pub mod adapters;
pub mod agents;
pub mod clock;
pub mod control;
pub mod entropy;
pub mod env;
pub mod identity;
pub mod names;
pub mod provision;
#[cfg(test)]
mod provision_fuzz;
pub mod session;

/// The version of the daemon release `provision` fetches by default.
///
/// This crate's own, which is also `microvms_core::VERSION`: `scripts/check-publishable.py`
/// holds every published crate in the workspace at one version, so the client and the daemon it
/// provisions come from the same release (BIND-17).
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
