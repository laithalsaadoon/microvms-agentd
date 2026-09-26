// SPDX-License-Identifier: Apache-2.0
//! The MicroVMs client's rules and values, with no I/O (ARCH-6).
//!
//! Every surface has to agree on these: the size classes, the cost engine and its rate table,
//! region parsing, the service constraints, VM name validation, the error kinds, and the
//! tunnel identity's derivation and pin. A rule that read ambient state would give different
//! answers on different machines, and it couldn't be tested without faking that state
//! globally. So nothing here reads the network, the filesystem, the environment, the clock
//! or the OS random pool, or starts a subprocess; each input a rule needs is a parameter.
//! [`region::Region::from_env`] takes a lookup, [`cost::CalendarDate::from_unix_secs`] takes
//! the time, and [`identity::LaunchIdentity::from_seeds`] takes the seeds.
//!
//! Two checks hold that. `clippy.toml` beside this crate's manifest bans the std calls and
//! the clock and entropy calls of the crates this one uses, and the crate root below forbids
//! both lints. The crates that could do I/O through a dependency (tokio, getrandom, reqwest)
//! are kept out by the exact dependency set in `arch/placement.toml`, which
//! `microvms-cli/tests/dependency_direction.rs` asserts along with each dependency's features.
//!
//! `microvms-core` re-exports every module here at its old path (ARCH-1), so a consumer names
//! these items as `microvms_core::cost::CalendarDate` and never depends on this crate. Core's
//! `prelude` supplies the clock and entropy reads these types used to do themselves.

// CLI-7: a print macro panics when its stream's reader has gone (#216). Every write goes
// through a checked writer instead.
#![deny(clippy::print_stdout, clippy::print_stderr)]
// ARCH-6: `clippy.toml` beside this crate's manifest names the file, process, network,
// environment, clock and entropy items this crate may not touch. Forbidden rather than denied,
// so a hit fails the build and an inner `#[allow]` or `#[expect]` is itself an error (E0453):
// the adapters list each reviewed exception, and the domain has none to list.
#![forbid(clippy::disallowed_methods, clippy::disallowed_types)]

pub mod constants;
pub mod cost;
pub mod error;
pub mod hooks;
pub mod identity;
pub mod names;
pub mod preflight;
pub mod provision;
pub mod region;
pub mod sizing;

#[cfg(test)]
mod sizing_fuzz;

pub use error::{Error, ErrorKind, WireKind};
pub use hooks::{BuildHookTimeout, RunHookTimeout};
pub use region::Region;
pub use sizing::SizeClass;
