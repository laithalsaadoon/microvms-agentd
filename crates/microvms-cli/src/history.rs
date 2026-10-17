// SPDX-License-Identifier: Apache-2.0
//! The shared client's durable VM history, composed with the local file store.
pub use microvms_core::history::*;

#[cfg(test)]
#[path = "history_tests.rs"]
mod tests;
