// SPDX-License-Identifier: Apache-2.0
//! Named MicroVMs: records that let a later process find a VM by name and adopt it.
//!
//! The record and its rules are `microvms_domain::names`, re-exported here. A [`NameRecord`]
//! carries everything [`crate::sandbox::Sandbox::adopt`] needs (id, endpoint, agent token,
//! region), so a name replaces the whole triple rather than just the id.
//!
//! Storage is a [`NameStore`]. `FileNameStore` (in `microvms-edges`) is the CLI's registry. A
//! caller with its own database implements [`NameStore`] or keeps [`NameRecord`]'s JSON form
//! wherever it likes.
//!
//! **A record holds a secret.** The agent token is a bearer credential for the VM, and the
//! optional identity seed can impersonate the launching host to it. Keep records in private
//! storage; neither value appears in `Debug` output or error messages.

pub use microvms_domain::names::*;

use crate::error::Error;
use crate::region::Region;

/// Where names are kept. Implement it to keep records in your own database.
pub trait NameStore: Send + Sync {
    /// The record registered under `name`, or `None` when the name is free.
    fn get(&self, name: &str) -> Result<Option<NameRecord>, Error>;
    /// Writes `record` under its name, replacing any earlier record.
    fn put(&self, record: &NameRecord) -> Result<(), Error>;
    /// Removes `name`, answering whether a record was there.
    fn delete(&self, name: &str) -> Result<bool, Error>;
    /// Every readable record, sorted by name.
    fn list(&self) -> Result<Vec<NameRecord>, Error>;

    /// Where this store keeps names, for error messages.
    fn describe(&self) -> String {
        "the name store".to_string()
    }

    /// Removes every name registered to `microvm_id` and returns them, sorted.
    ///
    /// Every match rather than the first: one VM can carry two names in one registry
    /// (measured live 2026-09-02: terminating through one alias left the other pointing
    /// at a VM that no longer existed).
    fn release_by_vm(&self, microvm_id: &str) -> Result<Vec<String>, Error> {
        let mut released = Vec::new();
        for record in self.list()? {
            if record.microvm_id == microvm_id && self.delete(&record.name)? {
                released.push(record.name);
            }
        }
        released.sort();
        Ok(released)
    }
}

/// The record for `name`, refused when it is missing or registered in another region.
///
/// [`resolve_record`] over `store`'s answer.
pub fn resolve(
    store: &dyn NameStore,
    name: &str,
    expected_region: Option<&Region>,
) -> Result<NameRecord, Error> {
    resolve_record(name, store.get(name)?, &store.describe(), expected_region)
}
