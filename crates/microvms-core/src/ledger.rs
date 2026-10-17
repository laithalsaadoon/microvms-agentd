// SPDX-License-Identifier: Apache-2.0
//! Outstanding run records, with the local file store composed for CLI callers.

use std::path::Path;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};

pub use microvms_app::ledger::{Ledger, Record};
pub use microvms_app::run_store::{RunStore, StoredLedger};
pub use microvms_edges::run_store::FileRunStore;

/// Starts a run using the historic state directory layout.
///
/// The first key is `<epoch>-<pid>` as before. Further runs in the same process add
/// a sequence suffix so one application's concurrent runs cannot overwrite each other.
pub fn new(region: &str, root: &Path) -> Ledger {
    use crate::clock::Clock as _;
    let epoch = crate::clock::SystemClock::new().unix_now().as_secs();
    new_at(region, root, epoch)
}

fn new_at(region: &str, root: &Path, epoch: u64) -> Ledger {
    static NEXT_RUN: AtomicU64 = AtomicU64::new(0);
    let sequence = NEXT_RUN.fetch_add(1, Ordering::Relaxed);
    let run_id = if sequence == 0 {
        format!("{epoch}-{}", std::process::id())
    } else {
        format!("{epoch}-{}-{sequence:020}", std::process::id())
    };
    Ledger::from_store(region, run_id, Arc::new(FileRunStore::new(root)))
}

/// Reopens every run for a VM, newest first.
pub fn open_all_for_vm(root: &Path, microvm_id: &str) -> Vec<Ledger> {
    Ledger::open_all_for_vm_with_store(Arc::new(FileRunStore::new(root)), microvm_id)
}

/// Reads outstanding runs from the shared local state directory.
pub fn read_all(root: &Path) -> Vec<microvms_app::ledger::Value> {
    microvms_app::ledger::read_all(&FileRunStore::new(root))
}

/// Removes a run after verifying that its key cannot escape the ledger namespace.
pub fn remove(root: &Path, run_id: &str) -> std::io::Result<()> {
    microvms_app::ledger::remove(&FileRunStore::new(root), run_id)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn runs_started_in_one_process_and_second_keep_separate_records() {
        let root = tempfile::tempdir().unwrap();
        let mut first = new_at("us-east-1", root.path(), 1700000000);
        let mut second = new_at("us-east-1", root.path(), 1700000000);
        assert_ne!(first.record.run_id, second.record.run_id);
        first.record_microvm("vm-first");
        first.mark_outstanding();
        second.record_microvm("vm-second");
        second.mark_outstanding();
        let records = read_all(root.path());
        assert_eq!(records.len(), 2);
        assert_eq!(records[0]["microvmId"], "vm-first");
        assert_eq!(records[1]["microvmId"], "vm-second");
        first.mark_deleted(&[]);
        first.clear();
        assert_eq!(read_all(root.path())[0]["microvmId"], "vm-second");
    }
}
