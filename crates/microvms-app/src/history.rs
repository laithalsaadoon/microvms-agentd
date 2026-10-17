// SPDX-License-Identifier: Apache-2.0
//! What was asked of one MicroVM and what the platform reported back, as a record that
//! outlives the VM.
//!
//! # A second file, not a wider ledger
//!
//! The obvious home for this was [`crate::ledger`], which already owns the state directory
//! and the camelCase wire shape. It was assessed and refused: the ledger's `clear()`
//! **deletes the file** when nothing leaked, because leak accounting wants records gone the
//! moment they are resolved — a stale entry is a phantom leak `microvm ls` reports forever.
//! History's whole value is the opposite property: the record survives terminate, because a
//! caller attesting over a run needs it precisely after the VM is gone. One file serving
//! both masters would corrupt one semantic to serve the other, so the two live apart and
//! neither touches the other's files.
//!
//! # Append-only, and nothing ever deletes a history file
//!
//! There is no `clear()` here and no remove anywhere in this module. A terminated VM's
//! history is the point, not the leftover.
//!
//! # `seq` is derived by counting, and single-writer is the assumption
//!
//! Each append reads the file and counts its lines. That is racy under two concurrent
//! writers to the same VM's history — both would claim one `seq` — and it is accepted,
//! stated here rather than papered over with a lock file: the realistic shape is one
//! process driving one VM at a time, and a lock file in the state directory would be one
//! more thing an interrupted process leaves behind. A reader that sees a duplicated `seq`
//! is seeing two writers, which is itself information.
//!
//! # Every value is the platform's
//!
//! Events carry what the control plane and the daemon reported — identifiers, endpoints,
//! exit codes, teardown verdicts — and never anything the guest printed. A record built
//! from guest output would be a record the workload can forge.
//!
//! [`Event::HookObserved`] carries one caveat that must be stated rather than implied.
//! The observation is the daemon's — a hook path posted, and the daemon's clock when it
//! arrived — so no guest-printed byte ever enters the line, and the letter of the rule
//! above holds. But the daemon's hook routes are unauthenticated and reachable over
//! loopback from inside the guest (`docs/PROTOCOL.md`, "Trust boundary"), so a hostile
//! workload can make the daemon observe *additional* firings by posting the hook paths
//! itself. It cannot remove or alter real ones: the daemon records before responding,
//! and its log keeps the earliest entries when capped — the platform's real lifecycle
//! firings — while later spam is dropped and counted. Read a hook line as "the daemon
//! saw this hook fire", never as "only the platform could have fired it".
//!
//! # Failures are swallowed, like the ledger's
//!
//! [`History::append`] runs on teardown paths, and a history write that raised would
//! replace the caller's real failure with a filesystem one. An unwritable state directory
//! costs the operator the record and nothing else.

use std::sync::Arc;

use crate::{clock::Clock, run_store::RunStore};

use serde::{Deserialize, Serialize};

/// One thing that happened to a VM, as the platform told it.
///
/// Internally tagged on `event` so a line reads `{"seq":0,"at":...,"event":"launched",...}` —
/// the discriminant is the field a consumer branches on, the same shape the envelope's `type`
/// takes. `camelCase` on the wire, matching the ledger.
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(
    tag = "event",
    rename_all = "camelCase",
    rename_all_fields = "camelCase"
)]
pub enum Event {
    /// This invocation's own build produced the image the VM launched from.
    ///
    /// Only `run` writes this, at run time, because only then is there a VM to key the
    /// record by — a standalone `microvm build` has no VM id and therefore no history file
    /// to append to.
    ImageBuilt {
        image_identifier: String,
        image_name: String,
    },
    /// The launch was accepted. The identifier and endpoint are the service's own answer.
    Launched {
        image_identifier: String,
        endpoint: String,
        region: String,
    },
    /// One exec, as the daemon reported it.
    ///
    /// `exit_code` is `None` for a detached start (the outcome is not known yet) and for a
    /// stream that was cut before its terminal event — reporting 0 for either would turn an
    /// unfinished command into a passing one. `writers_may_be_alive` travels only when the
    /// daemon's outcome carried it, which is why it is an `Option` rather than a defaulted
    /// bool.
    Exec {
        exec_id: String,
        exit_code: Option<i32>,
        truncated: bool,
        writers_may_be_alive: Option<bool>,
    },
    /// The daemon observed one lifecycle-hook invocation.
    ///
    /// Read off a health poll rather than pushed: the CLI appends the observations a
    /// `GET /v1/health` reported wherever it already holds both a health envelope and a
    /// VM id, deduplicated on the (hook, firedAt) pair so re-polling appends nothing.
    /// `hook` is the route's own spelling (`ready`, `validate`, `run`, `suspend`,
    /// `resume`, `terminate`) and `fired_at` is epoch seconds on the *daemon's* clock —
    /// distinct from the record's own `at`, which is when this CLI learned of it.
    /// See the module docs for the additive-forgery caveat this event carries.
    HookObserved { hook: String, fired_at: u64 },
    /// The service reported SUSPENDED.
    Suspended,
    /// The service reported RUNNING again.
    Resumed,
    /// A teardown ran. The fields are the [`crate::sandbox::TeardownReport`] shape:
    /// whether the terminate call was accepted, and what a delete was asked for and did not
    /// remove.
    Terminated {
        terminate_accepted: bool,
        undeleted: Vec<String>,
    },
}

/// One line on the wire: the counter, the clock, and the event flattened beside them.
///
/// `at` is seconds since the epoch, matching the ledger's `run_id` clock — the CLI's one
/// timestamp convention, so a reader correlating a history line against a ledger file does
/// no unit conversion.
#[derive(Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Record {
    pub seq: u64,
    pub at: u64,
    #[serde(flatten)]
    pub event: Event,
}

/// Where one VM's history is appended.
#[derive(Clone, Debug)]
pub struct History {
    vm_id: String,
    store: Arc<dyn RunStore>,
    clock: Arc<dyn Clock>,
}

impl History {
    /// The history for `vm_id`, using the supplied store and wall clock.
    ///
    /// A subdirectory rather than the root itself, so `ledger::read_all`'s `*.json` glob and
    /// this module's files can never be confused for one another by a tool listing the
    /// state directory.
    pub fn from_store(vm_id: &str, store: Arc<dyn RunStore>, clock: Arc<dyn Clock>) -> Self {
        Self {
            vm_id: vm_id.to_string(),
            store,
            clock,
        }
    }

    /// Appends one event. Failures are swallowed.
    ///
    /// Deliberately, and for the ledger's reason: this runs on teardown paths, and a history
    /// write that raised would replace the caller's real failure with a filesystem one. The
    /// values are still in the envelope; only the durable record is lost.
    ///
    /// `seq` is the current line count — see the module docs on the single-writer
    /// assumption that makes counting sufficient.
    pub fn append(&self, event: Event) {
        let seq = self
            .store
            .read_history(&self.vm_id)
            .map(|text| text.lines().count() as u64)
            .unwrap_or(0);
        let record = Record {
            seq,
            at: self.clock.unix_now().as_secs(),
            event,
        };
        let Ok(line) = serde_json::to_string(&record) else {
            return;
        };
        let _ = self.store.append_history(&self.vm_id, &line);
    }
}

/// Appends every hook observation from a health envelope that this VM's history has
/// not seen, deduplicated on the (hook, firedAt) pair.
///
/// The dedup is what makes hook recording safe to run opportunistically — `run`'s
/// teardown, `resume`, and `microvm health` all call this with whatever the daemon
/// reported, and a pair already on file is not re-appended, so polling twice appends
/// nothing. The comparison reads the file through [`read_events`] first, which keeps
/// the writes inside the single command that owns the moment (the module's
/// single-writer assumption) rather than adding a lock.
///
/// Failures are swallowed like every other append here: this runs on teardown paths.
pub fn append_unseen_hooks(
    history: &History,
    observed: &[crate::protocol::health::HookObservation],
) {
    if observed.is_empty() {
        return;
    }
    let mut seen: std::collections::HashSet<(String, u64)> =
        read_events(history.store.as_ref(), &history.vm_id)
            .iter()
            .filter(|event| event["event"] == "hookObserved")
            .filter_map(|event| {
                Some((
                    event["hook"].as_str()?.to_string(),
                    event["firedAt"].as_u64()?,
                ))
            })
            .collect();
    for observation in observed {
        // Inserted as it is appended, so a health body that repeats a pair — which a
        // hostile guest posting the hook routes can produce — lands once.
        if seen.insert((observation.hook.clone(), observation.fired_at)) {
            history.append(Event::HookObserved {
                hook: observation.hook.clone(),
                fired_at: observation.fired_at,
            });
        }
    }
}

/// Every event recorded for `vm_id` in `store`, oldest first.
///
/// A missing file is an empty result, not an error: asking about a VM this state dir never
/// saw is a question, not a mistake — the caller may hold an id from another machine's
/// state directory, and "no record here" is the honest answer.
///
/// An unparseable line becomes a record naming itself rather than being skipped, mirroring
/// the ledger's unreadable convention: a truncated last line is what a process killed
/// mid-append leaves, and that process is the one whose history most needs reading.
pub fn read_events(store: &dyn RunStore, vm_id: &str) -> Vec<serde_json::Value> {
    let Ok(text) = store.read_history(vm_id) else {
        return Vec::new();
    };
    text.lines()
        .enumerate()
        .map(|(index, line)| {
            serde_json::from_str::<serde_json::Value>(line)
                .ok()
                .filter(serde_json::Value::is_object)
                .unwrap_or_else(|| {
                    serde_json::json!({
                        "line": index + 1,
                        "unreadable": line,
                    })
                })
        })
        .collect()
}
