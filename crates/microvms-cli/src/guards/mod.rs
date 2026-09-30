// SPDX-License-Identifier: Apache-2.0
//! The guards that need to reach inside the crate: the behavioral thinness check (CLI-2), the
//! interrupt teardown (CLI-6), and the classification half of the exit catalogue (CLI-3).
//!
//! # Why these are here and the others are in `tests/`
//!
//! This crate has no lib target — that absence is ARCH-5's witness — so an integration test can
//! only reach it by spawning the binary. That is the right shape for the checks whose subject is
//! the *process*: an exit code (which `ExitCode` deliberately hides in-process), and the
//! single-document property of stdout. Those live in `tests/`.
//!
//! It is the wrong shape for the three below. The behavioral guard has to *inject* a refusing
//! seam, the interrupt guard has to fire an interrupt at a known instant mid-launch, and both are
//! assertions about which code path ran rather than about what the process printed. A spawned
//! binary can do neither without an environment variable that switches in a fake — which would be
//! a test hook in a shipping artifact, and a worse thing than the tests it enables.
//!
//! Compiled only under `cfg(test)`, so none of it is in the binary.
//!
//! # Layout
//!
//! One module per command area or requirement, named for it, and `support` for the fakes and
//! builders more than one of them uses.

#![cfg(test)]

mod artifact_uri;
mod attach;
mod build;
mod closed_output;
mod config;
mod doctor;
mod exec;
mod exit_codes;
mod files;
mod health;
mod history;
mod image;
mod interrupt;
mod ls;
mod names;
mod parity;
mod run;
mod run_dir;
mod seconds;
mod support;
mod thinness;
