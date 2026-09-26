// SPDX-License-Identifier: Apache-2.0
//! The process environment, as the lookup this crate's resolvers take.
//!
//! [`Region::from_env`](crate::Region::from_env),
//! [`FileNameStore::default_location`](crate::names::FileNameStore::default_location) and the
//! other resolvers take a `&dyn Fn(&str) -> Option<String>` rather than reading `std::env`
//! themselves, so a caller (and a test) decides where the variables come from. [`process`] is
//! that lookup in production. It lives here once so the CLI and both bindings pass the same
//! function: a driving adapter never reads the environment itself, and its `clippy.toml` bans
//! `std::env::var` to keep it that way. The adapters' `clippy.toml` bans calling [`process`]
//! by name too, since that's the same read with a different path. Each adapter hands it to a
//! resolver at the one place it composes them, under a listed `#[expect]`.

/// `name`'s value in the process environment, or `None` when it's unset or isn't valid Unicode.
pub fn process(name: &str) -> Option<String> {
    std::env::var(name).ok()
}
