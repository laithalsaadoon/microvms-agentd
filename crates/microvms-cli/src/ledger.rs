// SPDX-License-Identifier: Apache-2.0
//! CLI names adapter and shared durable run records.
pub use microvms_core::ledger::*;
use std::path::{Path, PathBuf};

// ── the name registry ────────────────────────────────────────────────────────

use microvms_core::names::{FileNameStore, NameStore as _};
/// The record type, name rules, and file store live in core, so the bindings read and write
/// the same registry the CLI does. See `microvms_core::names`.
pub use microvms_core::names::{NameRecord, validate_name};

/// The name→VM registry the CLI keeps: one JSON file per live name, under `<root>/names/`.
///
/// A thin adapter over core's [`FileNameStore`] with the CLI's semantics. A name lives
/// exactly as long as its VM: registered when `run --keep` succeeds, removed when a
/// terminate is accepted, and *refused* for reuse in between. That refusal is the
/// registry's whole job, and it costs zero AWS calls: the collision check is a local file
/// read.
#[derive(Clone, Debug)]
pub struct Names {
    store: FileNameStore,
}

impl Names {
    /// The registry under `state_root/names`.
    pub fn new(state_root: &Path) -> Self {
        Self {
            store: FileNameStore::in_state_root(state_root),
        }
    }

    /// Where `name`'s record lives, whether or not one is registered.
    pub fn path_of(&self, name: &str) -> PathBuf {
        self.store.path_of(name)
    }

    /// The store itself, for core's name rules that take one (`names::holder`,
    /// `names::import_probed`).
    pub fn store(&self) -> &FileNameStore {
        &self.store
    }

    /// The record registered under `name`, or `None`. For the collision checks only (`run
    /// --vm-name`, `attach`'s holder check, `agent-up`'s fresh-or-refresh choice); a command
    /// that goes on to use the record reads it through [`Names::resolve`].
    ///
    /// An unreadable file reads as registered — its name is taken by *something*, and
    /// treating a torn record as free would let a second VM claim a name whose first holder
    /// may still be billing. The caller sees the collision refusal and can inspect the file.
    /// An illegal name is never registered, and never becomes a path.
    pub fn lookup(&self, name: &str) -> Option<NameRecord> {
        if validate_name(name).is_err() {
            return None;
        }
        match self.store.get(name) {
            Ok(record) => record,
            Err(_) => Some(NameRecord {
                name: name.to_string(),
                microvm_id: String::new(),
                endpoint: String::new(),
                agent_token: String::new(),
                region: String::new(),
                at: 0,
                identity_host_seed: None,
                identity_vm_public_key: None,
                egress_posture: None,
            }),
        }
    }

    /// The record registered under `name`, checked by core's rule, or `None` when the name is
    /// free.
    ///
    /// The same rule `Sandbox.from_name` applies through `names::resolve`: a record registered
    /// in another region than `expected_region` is `ERR_INVALID_ARG` naming both. Unlike
    /// [`Names::lookup`] it doesn't pre-check the grammar, so an illegal name is the store's
    /// `ERR_INVALID_ARG` with the grammar's reason, and a torn file is its `ERR_PRECONDITION`
    /// naming the file rather than a record with empty fields. A free name comes back as
    /// `None` rather than core's refusal because each command's not-found message names its
    /// own remedies.
    pub fn resolve(
        &self,
        name: &str,
        expected_region: Option<&microvms_core::Region>,
    ) -> Result<Option<NameRecord>, microvms_core::Error> {
        let found = self.store.get(name)?;
        found
            .map(|record| {
                microvms_core::names::resolve_record(
                    name,
                    Some(record),
                    &self.store.describe(),
                    expected_region,
                )
            })
            .transpose()
    }

    /// Writes `record` under its name, owner-only from creation on Unix. The one registry
    /// write that reports failure, because it runs on the success path: a name that
    /// silently failed to register would make every later `exec --name` fail with "no VM
    /// named" while the VM bills on.
    pub fn register(&self, record: &NameRecord) -> std::io::Result<()> {
        self.store
            .put(record)
            .map_err(|error| std::io::Error::other(error.to_string()))
    }

    /// Releases every name registered to `microvm_id`, returning them.
    ///
    /// By VM id rather than by name, because the terminate path resolves its identifier
    /// before this runs. Failures are swallowed (a registry error must not displace the
    /// teardown's real outcome); a record that survives costs one stale collision refusal,
    /// whose message names the file.
    pub fn release_by_vm(&self, microvm_id: &str) -> Vec<String> {
        self.store.release_by_vm(microvm_id).unwrap_or_default()
    }

    /// Every readable record, sorted by name.
    pub fn list(&self) -> Result<Vec<NameRecord>, microvms_core::Error> {
        self.store.list()
    }

    /// Removes `name`, answering whether a record was there.
    pub fn delete(&self, name: &str) -> Result<bool, microvms_core::Error> {
        self.store.delete(name)
    }

    /// Where the registry lives, for a message that names it.
    pub fn describe(&self) -> String {
        self.store.describe()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A temporary directory that cleans itself up.
    ///
    /// To be replaced by the `tempfile` dev-dependency in the dependency sweep, along with
    /// the seven other copies of this pattern across the crate.
    struct TempDir(PathBuf, #[allow(dead_code)] tempfile::TempDir);

    impl TempDir {
        fn new(label: &str) -> Self {
            let dir = tempfile::Builder::new()
                .prefix(&format!("microvm-cli-{label}-"))
                .tempdir()
                .expect("a temp dir");
            Self(dir.path().to_path_buf(), dir)
        }
    }

    /// **CLI-6's persistence half.** The ledger survives the process, so `ls` can report a
    /// leak the run that created it never got to print.
    ///
    /// Written and read through the real file, not a mock: the whole property is that the
    /// bytes outlive the process, and an in-memory round trip would assert nothing about it.
    #[test]
    fn a_leaked_identifier_survives_into_a_file_a_later_invocation_reads() {
        let dir = TempDir::new("survives");
        let mut ledger = new("us-east-1", &dir.0);
        ledger.record_image("arn:image", "img");
        ledger.record_microvm("mvm-1");
        ledger.mark_outstanding();
        // The delete of the VM succeeded and the image's did not, which is the common shape:
        // an image in CREATING refuses deletion while the VM goes away fine.
        ledger.mark_deleted(&["arn:image".to_string()]);

        let read = read_all(&dir.0);
        assert_eq!(read.len(), 1, "{read:?}");
        assert_eq!(read[0]["microvmId"], "mvm-1");
        assert_eq!(read[0]["leaked"], serde_json::json!(["arn:image"]));
        assert_eq!(read[0]["region"], "us-east-1");
    }

    /// **Issue #160.** A later process reopens the record that names a VM, newest first, and
    /// sees nothing for a VM no record names.
    ///
    /// Newest first because a reused image launched twice under `--keep` leaves two records
    /// naming two VMs; when one VM appears in two records (a retried launch), the later one
    /// is the launch that succeeded. Name records under `names/` must never be read as run
    /// ledgers: the registry file for a name carries a `microvmId` too, and mistaking it for
    /// a run record would hand `terminate` a record with no image in it.
    #[test]
    fn a_record_reopens_by_vm_id_newest_first_and_never_from_the_name_registry() {
        let dir = TempDir::new("reopen");
        let mut older = new("us-east-1", &dir.0);
        older.record_image("arn:image-old", "img-old");
        older.record_microvm("mvm-1");
        older.mark_outstanding();
        // A second record for the same VM, written later. `Ledger::new` keys on epoch+pid, so
        // two records from one process in one second would collide; write this one by hand
        // with a later-sorting name, which is what a later process produces.
        let newer = Record {
            run_id: "9999999999-1".into(),
            region: "us-east-1".into(),
            image_identifier: Some("arn:image-new".into()),
            image_name: Some("img-new".into()),
            microvm_id: Some("mvm-1".into()),
            leaked: vec!["mvm-1".into(), "arn:image-new".into()],
            vm_log_group: None,
        };
        std::fs::write(
            dir.0.join("9999999999-1.json"),
            serde_json::to_string(&newer).expect("serializes"),
        )
        .expect("writes");
        // A name record naming the same VM, in the registry subdirectory.
        let names = Names::new(&dir.0);
        names
            .register(&NameRecord {
                name: "probe".into(),
                microvm_id: "mvm-1".into(),
                endpoint: "https://mvm-1.example".into(),
                agent_token: "tok".into(),
                region: "us-east-1".into(),
                at: 1,
                identity_host_seed: None,
                identity_vm_public_key: None,
                egress_posture: None,
            })
            .expect("registers");

        let mut all = open_all_for_vm(&dir.0, "mvm-1");
        assert_eq!(all.len(), 2, "both records naming the VM reopen");
        let mut reopened = all.remove(0);
        assert_eq!(
            reopened.record.image_identifier.as_deref(),
            Some("arn:image-new"),
            "the newest record wins"
        );
        assert!(
            open_all_for_vm(&dir.0, "mvm-unknown").is_empty(),
            "a VM no record names reopens nothing"
        );
        // And the reopened ledger writes back to the file it came from, so a narrow-and-clear
        // in a later process lands on the right record.
        reopened.mark_deleted(&[]);
        reopened.clear();
        assert!(
            !dir.0.join("9999999999-1.json").exists(),
            "cleared in place"
        );
        assert!(
            dir.0.join(format!("{}.json", older.record.run_id)).exists(),
            "the older record is untouched"
        );
    }

    /// A clean run leaves no ledger behind.
    ///
    /// The other half of the same rule: a file that stayed would make `ls` report a leak that
    /// does not exist, and an operator who is told about a phantom leak stops reading `ls`.
    #[test]
    fn a_clean_run_leaves_nothing_for_ls_to_report() {
        let dir = TempDir::new("clean");
        let mut ledger = new("us-east-1", &dir.0);
        ledger.record_microvm("mvm-1");
        ledger.mark_outstanding();
        assert_eq!(read_all(&dir.0).len(), 1, "the outstanding record is there");

        ledger.mark_deleted(&[]);
        ledger.clear();
        assert!(read_all(&dir.0).is_empty(), "a clean run clears its ledger");
    }

    /// `clear` refuses while something is outstanding.
    ///
    /// **Falsification** — drop the `is_empty` check from `clear` and this test loses the
    /// file, so `microvm ls` reports nothing about a VM that is still billing.
    #[test]
    fn clear_refuses_to_remove_a_ledger_that_still_names_a_live_resource() {
        let dir = TempDir::new("refuses");
        let mut ledger = new("us-east-1", &dir.0);
        ledger.record_microvm("mvm-1");
        ledger.mark_outstanding();
        ledger.clear();
        let read = read_all(&dir.0);
        assert_eq!(read.len(), 1, "the leak must survive a clear attempt");
        assert_eq!(read[0]["leaked"], serde_json::json!(["mvm-1"]));
    }

    /// A truncated ledger is reported as unreadable rather than skipped.
    ///
    /// It is what a process killed mid-write leaves, and that process is the one most likely
    /// to have leaked — so silence here is the worst available answer.
    #[test]
    fn a_truncated_ledger_is_reported_rather_than_skipped() {
        let dir = TempDir::new("truncated");
        std::fs::write(dir.0.join("1754524800-999.json"), "{\"runId\": \"175").expect("writes");
        let read = read_all(&dir.0);
        assert_eq!(read.len(), 1);
        assert_eq!(read[0]["runId"], "1754524800-999");
        assert!(read[0]["unreadable"].is_string(), "{read:?}");
    }

    /// Outstanding is recorded before a delete is attempted, so the order is observable.
    ///
    /// Asserted by reading the file *between* the two calls, which is the only way to
    /// distinguish this from the losing order — both end in the same final state.
    #[test]
    fn the_outstanding_list_is_on_disk_before_any_delete_is_attempted() {
        let dir = TempDir::new("order");
        let mut ledger = new("us-east-1", &dir.0);
        ledger.record_image("arn:image", "img");
        ledger.record_microvm("mvm-1");

        ledger.mark_outstanding();
        // This is the instant a killed process would die at, and what it leaves behind.
        let mid_flight = read_all(&dir.0);
        assert_eq!(
            mid_flight[0]["leaked"],
            serde_json::json!(["mvm-1", "arn:image"]),
            "both identifiers must already be on disk: {mid_flight:?}"
        );
    }

    /// An unwritable directory does not raise.
    ///
    /// The teardown path calls this, and a ledger write that raised would replace the
    /// caller's real failure with a filesystem one.
    #[test]
    fn an_unwritable_state_directory_is_survivable() {
        let root = Path::new("/proc/definitely-not-writable");
        let ledger = new("us-east-1", root);
        ledger.flush();
        ledger.clear();
        // The path really is under the unwritable root, so the flush above attempted a write that
        // could not succeed — rather than a ledger that quietly writes nowhere and would pass
        // this test for the wrong reason.
        assert!(
            !read_all(root)
                .iter()
                .any(|r| r["runId"] == ledger.record.run_id)
        );
        assert!(
            !root.join(format!("{}.json", ledger.record.run_id)).exists(),
            "nothing was written, and nothing raised"
        );
    }

    /// Multiple ledgers read back oldest-first, so `ls` output is stable.
    #[test]
    fn ledgers_read_back_in_a_deterministic_order() {
        let dir = TempDir::new("order-many");
        for id in ["1754524800-1", "1754524801-2", "1754524802-3"] {
            std::fs::write(
                dir.0.join(format!("{id}.json")),
                serde_json::to_string(&Record {
                    run_id: id.to_string(),
                    region: "us-east-1".to_string(),
                    ..Record::default()
                })
                .expect("serializes"),
            )
            .expect("writes");
        }
        let ids: Vec<String> = read_all(&dir.0)
            .iter()
            .map(|value| value["runId"].as_str().unwrap_or_default().to_string())
            .collect();
        assert_eq!(ids, ["1754524800-1", "1754524801-2", "1754524802-3"]);
    }

    fn a_record(name: &str, id: &str) -> NameRecord {
        NameRecord {
            name: name.to_string(),
            microvm_id: id.to_string(),
            endpoint: format!("https://{id}.example"),
            agent_token: "tok-1".to_string(),
            region: "us-east-1".to_string(),
            at: 1754524800,
            identity_host_seed: None,
            identity_vm_public_key: None,
            egress_posture: None,
        }
    }

    /// A registered name resolves to its record, from a different `Names` value.
    ///
    /// Two values rather than one reused, for history's reason: `run --keep` registers and a
    /// later `microvm exec --name` — a different process — looks up. The file is the handoff.
    #[test]
    fn a_registered_name_survives_into_a_later_lookup() {
        let dir = TempDir::new("names-roundtrip");
        Names::new(&dir.0)
            .register(&a_record("ci-runner", "mvm-1"))
            .expect("registers");

        let found = Names::new(&dir.0).lookup("ci-runner").expect("registered");
        assert_eq!(found, a_record("ci-runner", "mvm-1"));
        assert!(Names::new(&dir.0).lookup("other").is_none());
    }

    /// A release by VM id frees every name that VM holds and no other.
    ///
    /// Two names for one VM is the shape `attach` produces (the same record adopted twice
    /// under different names, or adopted beside the launch's own). Releasing only the first
    /// match left the second as a record pointing at a terminated VM — observed live on
    /// 2026-09-02 — so both must go.
    #[test]
    fn a_release_by_vm_id_frees_every_name_that_vm_holds_and_no_other() {
        let dir = TempDir::new("names-release");
        let names = Names::new(&dir.0);
        names.register(&a_record("a", "mvm-1")).expect("registers");
        names.register(&a_record("b", "mvm-2")).expect("registers");
        names
            .register(&a_record("b-alias", "mvm-2"))
            .expect("registers");

        assert_eq!(names.release_by_vm("mvm-2"), ["b", "b-alias"]);
        assert!(names.lookup("b").is_none());
        assert!(
            names.lookup("b-alias").is_none(),
            "every alias goes, not the first"
        );
        assert!(names.lookup("a").is_some(), "the other name is untouched");
        assert!(
            names.release_by_vm("mvm-99").is_empty(),
            "nothing to release"
        );
    }

    /// A torn record reads as taken rather than as free.
    ///
    /// A process killed mid-register leaves exactly this file, and its VM may be billing —
    /// so letting a second VM claim the name would point every later command at the wrong
    /// one. The refusal names the file; the operator deletes it deliberately.
    #[test]
    fn a_torn_name_record_reads_as_taken_not_free() {
        let dir = TempDir::new("names-torn");
        let names = Names::new(&dir.0);
        std::fs::create_dir_all(dir.0.join("names")).expect("mkdir");
        std::fs::write(dir.0.join("names").join("wedged.json"), "{\"name\": \"we").expect("writes");
        assert!(
            names.lookup("wedged").is_some(),
            "a torn record is a taken name"
        );
    }

    /// The name grammar: the image-name charset, no `mvm-` prefix, bounded length.
    ///
    /// The `mvm-` refusal is the one that makes bare-identifier resolution total: with it,
    /// `mvm-*` can only be an id, anything else in the charset can only be a name, and the
    /// two sets cannot collide.
    #[test]
    fn the_name_grammar_refuses_the_id_prefix_and_foreign_characters() {
        assert!(validate_name("ci-runner_2").is_ok());
        assert!(validate_name("a").is_ok());
        assert!(validate_name("").is_err());
        assert!(validate_name(&"x".repeat(129)).is_err());
        assert!(
            validate_name("mvm-lookalike").is_err(),
            "a name shaped like a MicroVM id would make resolution ambiguous"
        );
        assert!(
            validate_name("microvm-9536a532").is_err(),
            "the real service's id prefix is microvm-, measured on the first live run — \
             only the test fixtures spell it mvm-"
        );
        for bad in ["with space", "dot.name", "slash/name", "colon:name"] {
            assert!(validate_name(bad).is_err(), "{bad:?} must be refused");
        }
    }

    /// The name record's wire shape is `camelCase`, like every other file in the state dir.
    #[test]
    fn the_name_record_serializes_camel_case_and_round_trips() {
        let value = serde_json::to_value(a_record("ci", "mvm-1")).expect("serializes");
        let mut keys: Vec<&String> = value.as_object().expect("object").keys().collect();
        keys.sort();
        assert_eq!(
            keys,
            [
                "agentToken",
                "at",
                "endpoint",
                "microvmId",
                "name",
                "region"
            ]
        );
        let back: NameRecord = serde_json::from_value(value).expect("round trips");
        assert_eq!(back, a_record("ci", "mvm-1"));
    }

    /// The wire shape is `camelCase`, matching the Python's ledger key for key.
    ///
    /// A caller debugging a leak reaches for whichever `microvm ls` is on their PATH, and a
    /// ledger only one client can read is a ledger that fails exactly then.
    #[test]
    fn the_record_serializes_with_the_pythons_camel_case_keys() {
        let record = Record {
            run_id: "1-2".to_string(),
            region: "us-east-1".to_string(),
            image_identifier: Some("arn:image".to_string()),
            image_name: Some("img".to_string()),
            microvm_id: Some("mvm-1".to_string()),
            leaked: vec!["mvm-1".to_string()],
            vm_log_group: None,
        };
        let value = serde_json::to_value(&record).expect("serializes");
        let mut keys: Vec<&String> = value.as_object().expect("object").keys().collect();
        keys.sort();
        assert_eq!(
            keys,
            [
                "imageIdentifier",
                "imageName",
                "leaked",
                "microvmId",
                "region",
                "runId"
            ]
        );
        let back: Record = serde_json::from_value(value).expect("round trips");
        assert_eq!(back, record);
    }
}
