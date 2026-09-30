// SPDX-License-Identifier: Apache-2.0
//! `microvms-agentd` — exec and file transfer inside an AWS Lambda MicroVM.
//!
//! The service provides an isolated Firecracker VM and no way to run anything in
//! it: there is no exec API and no file-transfer API. This daemon supplies both
//! over the VM's endpoint, and every rule it enforces is documented in
//! `docs/PROTOCOL.md` alongside the defect that made the rule necessary.
//!
//! # Trust boundary
//!
//! Measured 2026-08-04 in us-east-1: the platform's own `/run` lifecycle hook
//! arrives from `127.0.0.1`, indistinguishable at the socket level from a request
//! sent by a process inside the VM. Source-address filtering on the bootstrap
//! route is therefore wrong rather than merely unverified — it would reject the
//! platform's legitimate bootstrap and break every launch. Do not add it. See
//! `docs/PLATFORM.md`.
//!
//! What defends the bootstrap instead:
//!
//! 1. Bootstrap is one-shot, so a losing racer never replaces the winner's token.
//! 2. A hijack attempt is refused at the hook with 409 and at the control API
//!    with 401.
//! 3. The agent token never enters an exec'd child's environment.
//!
//! The `agentd-model` crate's bootstrap model checks the first two over every
//! interleaving of platform, client, and in-VM attacker, and `model_conformance`
//! replays its paths against this daemon. The third is AGENTD-12, which the crate's
//! exec-start model checks in `crates/model/src/exec_start.rs` and `exec`'s tests check
//! against a real child. `docs/TRUST.md` has the threat table that names each
//! defense's requirement key and guard.
//!
//! The residual risk is that the daemon being the container `CMD` and the harness
//! issuing its first exec only after readiness is an *unenforced* invariant. A
//! base image that starts its own process before bootstrap breaks it. That case
//! is modeled explicitly, and enforcing it belongs to whoever builds the image.

pub mod auth;
pub mod config;
pub mod disk;
pub mod exec;
pub mod exec_start;
#[cfg(test)]
mod exec_start_fuzz;
pub mod fs;
pub mod hook_handlers;
pub mod identity;
pub mod routes;
pub mod schema;
pub mod serve;
pub mod state;
pub mod tunnel;
pub mod tunnel_identity;

pub use config::Config;
pub use state::AppState;
