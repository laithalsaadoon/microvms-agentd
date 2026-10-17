// SPDX-License-Identifier: Apache-2.0
//! Durable VM history composed with the shared file store and production clock.

use std::path::Path;
use std::sync::Arc;

pub use microvms_app::history::{Event, History, Record};
use microvms_edges::run_store::FileRunStore;

/// Opens the history for a VM in the CLI's existing state directory layout.
pub fn for_vm(root: &Path, vm_id: &str) -> History {
    History::from_store(
        vm_id,
        Arc::new(FileRunStore::new(root)),
        Arc::new(crate::clock::SystemClock::new()),
    )
}

/// Reads each history line, retaining a diagnostic for an unreadable line.
pub fn read_events(root: &Path, vm_id: &str) -> Vec<microvms_app::ledger::Value> {
    microvms_app::history::read_events(&FileRunStore::new(root), vm_id)
}

/// Records only hook observations absent from this VM's history.
pub fn append_unseen_hooks(
    root: &Path,
    vm_id: &str,
    observed: &[crate::protocol::health::HookObservation],
) {
    microvms_app::history::append_unseen_hooks(&for_vm(root, vm_id), observed);
}
