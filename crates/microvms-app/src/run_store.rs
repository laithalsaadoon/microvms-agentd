// SPDX-License-Identifier: Apache-2.0
//! Storage for outstanding runs and append-only VM history.
//!
//! The namespaces stay separate: clearing a resolved run must not erase its history.

use std::fmt::Debug;
use std::io;

/// One stored ledger document, including unreadable records that must stay visible.
#[derive(Clone, Debug)]
pub struct StoredLedger {
    pub key: String,
    pub location: String,
    pub text: Option<String>,
}

/// Durable storage used by the run ledger and history policies.
///
/// Writes report errors here; the policies deliberately swallow them on teardown so
/// housekeeping never replaces a command's real outcome. Keys are opaque store identifiers.
pub trait RunStore: Debug + Send + Sync {
    /// Lists every ledger, including an unreadable document as `text: None`.
    fn ledgers(&self) -> Vec<StoredLedger>;
    /// Replaces a ledger while keeping history separate.
    fn write_ledger(&self, key: &str, text: &str) -> io::Result<()>;
    /// Removes a ledger. The caller has decided whether its resources are resolved.
    fn remove_ledger(&self, key: &str) -> io::Result<()>;
    /// Reads the complete history text, preserving line order and malformed lines.
    fn read_history(&self, vm_id: &str) -> io::Result<String>;
    /// Appends one serialized record and a newline, without replacing earlier bytes.
    fn append_history(&self, vm_id: &str, line: &str) -> io::Result<()>;
}
