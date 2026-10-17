// SPDX-License-Identifier: Apache-2.0
//! What one invocation created, on disk, so an interrupt can name what it could not delete.
//!
//! # On disk rather than in memory only
//!
//! The identifiers are worthless to the operator if the process that held them is the process
//! that died. An image wedged in `CREATING` cannot be deleted afterward *at all*, and a
//! service-created log group outlives `terraform destroy`, so for both of those the
//! identifier **is** the remedy — there is no second way to find them.
//!
//! # Leaked is recorded before the delete is attempted
//!
//! Not after. The other order — try, then record on failure — loses the identifier when the
//! process dies inside the call, which is exactly the interrupt case this file exists for.
//! So [`Ledger::mark_outstanding`] writes first and [`Ledger::mark_deleted`] narrows the list
//! afterwards.
//!
//! # A file is only removed when nothing is outstanding
//!
//! [`Ledger::clear`] refuses while `leaked` is non-empty, because a leftover file is how
//! `microvm ls` knows there is something to tell the operator about. Clearing one that still
//! names a live resource would hide the one case the file exists for.

use std::sync::Arc;

use crate::run_store::RunStore;

use serde::{Deserialize, Serialize};
pub use serde_json::Value;

/// One invocation's record.
///
/// `camelCase` on the wire, matching `cli.py:550`'s `as_dict`, so a ledger written by either
/// client is readable by both — which matters because a caller debugging a leak will reach
/// for whichever `microvm ls` is on their PATH.
///
/// (cli.py line numbers resolve at `git show 'c4d396e^:clients/python/src/microvms_agentd/cli.py'` — the retired oracle.)
#[derive(Clone, Debug, Default, Deserialize, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Record {
    pub run_id: String,
    pub region: String,
    #[serde(default)]
    pub image_identifier: Option<String>,
    #[serde(default)]
    pub image_name: Option<String>,
    #[serde(default)]
    pub microvm_id: Option<String>,
    /// Identifiers teardown tried and failed to remove. The operator's to-do list.
    #[serde(default)]
    pub leaked: Vec<String>,
    /// The caller-chosen log group for the VM's own logs (`--vm-log-group`). Outside the
    /// service's namespace, so a namespace sweep cannot find it; `verify-clean.py` reads it
    /// here. Never deleted by teardown: it is the caller's destination, not our garbage.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub vm_log_group: Option<String>,
}

/// A record plus where it is written.
#[derive(Clone, Debug)]
pub struct Ledger {
    pub record: Record,
    key: String,
    store: Arc<dyn RunStore>,
}

impl Ledger {
    /// A ledger keyed by a caller-supplied per-invocation id in `store`.
    ///
    /// The id is `<epoch>-<pid>`, matching the Python. Not from a CSPRNG: this names a local
    /// file rather than an idempotency token, and core is emphatic that token minting is its
    /// own job (TRAP-1).
    pub fn from_store(region: &str, run_id: String, store: Arc<dyn RunStore>) -> Self {
        Self {
            key: run_id.clone(),
            record: Record {
                run_id,
                region: region.to_string(),
                ..Record::default()
            },
            store,
        }
    }

    /// Every record in `store` that names `microvm_id`, newest first — reopened so a later
    /// process can narrow and clear them.
    ///
    /// `terminate --delete-image` reads the image off the newest (issue #160): `run --keep`
    /// left the record with the VM, the image, and the image's name, so the flag that asked
    /// for the identifier back was asking for what the CLI already had. A retried launch can
    /// leave two records naming one VM; a teardown that narrowed only the newest would leave
    /// the older one reporting the VM forever, which is the stale `ls` shape issue #159
    /// measured, so callers narrow all of them.
    ///
    /// Name records live under `root/names/` and are not run ledgers, so `read_dir` on the
    /// root alone never sees them.
    pub fn open_all_for_vm_with_store(store: Arc<dyn RunStore>, microvm_id: &str) -> Vec<Self> {
        let mut documents = store.ledgers();
        documents.sort_by(|a, b| a.key.cmp(&b.key));
        documents
            .into_iter()
            .rev()
            .filter_map(|document| {
                let record: Record = serde_json::from_str(document.text.as_deref()?).ok()?;
                (record.microvm_id.as_deref() == Some(microvm_id)).then(|| Self {
                    record,
                    key: document.key,
                    store: store.clone(),
                })
            })
            .collect()
    }

    /// Writes the record without displacing a teardown failure with a storage failure.
    pub fn flush(&self) {
        if let Ok(text) = serde_json::to_string_pretty(&self.record) {
            let _ = self.store.write_ledger(&self.key, &text);
        }
    }

    /// Records the image, and flushes.
    pub fn record_image(&mut self, identifier: &str, name: &str) {
        self.record.image_identifier = Some(identifier.to_string());
        self.record.image_name = Some(name.to_string());
        self.flush();
    }

    /// Records the caller-chosen per-VM log group, and flushes.
    pub fn record_vm_log_group(&mut self, group: &str) {
        self.record.vm_log_group = Some(group.to_string());
        self.flush();
    }

    /// Records the VM, and flushes.
    pub fn record_microvm(&mut self, id: &str) {
        self.record.microvm_id = Some(id.to_string());
        self.flush();
    }

    /// Marks everything this invocation created as outstanding, **before** any delete.
    ///
    /// See the module docs: this order is what survives a process that dies inside the
    /// terminate call.
    pub fn mark_outstanding(&mut self) {
        self.record.leaked = [
            self.record.microvm_id.clone(),
            self.record.image_identifier.clone(),
        ]
        .into_iter()
        .flatten()
        .collect();
        self.flush();
    }

    /// Narrows the outstanding list to what a teardown did **not** report deleted.
    ///
    /// Takes the survivors rather than the deletions, because that is the shape
    /// [`crate::sandbox::TeardownReport::undeleted`] hands over — and translating
    /// between the two is where an inversion lives.
    pub fn mark_deleted(&mut self, still_undeleted: &[String]) {
        self.record.leaked = still_undeleted.to_vec();
        self.flush();
    }

    /// Removes the file, but only when nothing is outstanding.
    pub fn clear(&self) {
        if !self.record.leaked.is_empty() {
            return;
        }
        let _ = self.store.remove_ledger(&self.key);
    }
}

/// Removes the ledger file for `run_id` under `root` — `ls --remote --prune`'s one write (#159).
///
/// The id is checked against the ledger's own grammar (`<epoch>-<pid>`, so ASCII digits,
/// letters, `-`, `_`) before it becomes a path component. The value comes from a record's
/// `runId` field rather than from the file name, and a record is a file anyone can edit; a
/// `runId` of `../names/x` must not turn a prune into a deletion elsewhere in the state
/// directory.
pub fn remove(store: &dyn RunStore, run_id: &str) -> std::io::Result<()> {
    let legal = !run_id.is_empty()
        && run_id
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_');
    if !legal {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            format!("{run_id:?} is not a ledger run id"),
        ));
    }
    store.remove_ledger(run_id)
}

/// Every ledger, oldest first; an unreadable document remains visible to the operator.
pub fn read_all(store: &dyn RunStore) -> Vec<serde_json::Value> {
    let mut documents = store.ledgers();
    documents.sort_by(|a, b| a.key.cmp(&b.key));
    documents
        .into_iter()
        .map(|document| {
            document
                .text
                .and_then(|text| serde_json::from_str(&text).ok())
                .unwrap_or_else(
                    || serde_json::json!({"runId": document.key, "unreadable": document.location}),
                )
        })
        .collect()
}
