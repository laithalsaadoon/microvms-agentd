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

use crate::error::{Error, ErrorKind};
use crate::region::Region;
use crate::session::Session;

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

// ── import: registering a record this store did not write (#270) ─────────────

/// The path an import's probe reads: zero bytes, present in every Linux guest.
///
/// **Not `/v1/health`.** That route is open (the platform forwards no external traffic until
/// the run hook returns 200, so reaching it implies nothing about the bearer), and a probe that
/// stopped there would register a record whose token the daemon refuses on every later adopt.
/// `GET /v1/fs/file` sits behind the daemon's token check, and `/dev/null` is the one path a
/// read can't fail on for a reason that's about the file.
pub const IMPORT_PROBE_PATH: &str = "/dev/null";

/// Who holds a name in a store, for a record that would take it.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum NameHolder {
    /// Nothing is registered under the name.
    Free,
    /// The same VM is: an import refreshes the record.
    Same,
    /// Another VM is, by its id, or `None` for a record that doesn't read (a torn file keeps
    /// its name taken, since its first holder may still be billing).
    Other(Option<String>),
}

/// Who holds `record`'s name in `store`.
pub fn holder(store: &dyn NameStore, record: &NameRecord) -> NameHolder {
    match store.get(&record.name) {
        Ok(None) => NameHolder::Free,
        Ok(Some(existing)) if existing.microvm_id == record.microvm_id => NameHolder::Same,
        Ok(Some(existing)) => NameHolder::Other(Some(existing.microvm_id)),
        Err(_) => NameHolder::Other(None),
    }
}

/// Proof that a record's VM answered one authenticated request with the record's token.
///
/// Only [`probe`] makes one, and [`import_probed`] takes one, so a record can't be written
/// without it: the rule is the types', not a caller's order of calls.
pub struct Probed {
    microvm_id: String,
    endpoint: String,
    agent_token: String,
}

/// Redacts the token, a bearer credential for the VM.
impl std::fmt::Debug for Probed {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Probed")
            .field("microvm_id", &self.microvm_id)
            .field("endpoint", &self.endpoint)
            .finish_non_exhaustive()
    }
}

/// What an import did.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Imported {
    /// Whether the name already held this VM's record, which the import refreshed.
    pub replaced: bool,
}

/// One authenticated read of [`IMPORT_PROBE_PATH`] over `session`, which must be attached with
/// `record`'s own endpoint and token.
///
/// An import authenticates the token, not the machine the record came from, so this read is
/// the only proof the triple is live. A dead endpoint, a refused token (401) or an
/// unbootstrapped daemon (503) fails here with the daemon's own class. Either answer to the
/// read proves the bearer: absence sits behind the same token check as presence.
pub async fn probe(session: &Session, record: &NameRecord) -> Result<Probed, Error> {
    if session.endpoint() != record.endpoint || session.agent_token() != record.agent_token {
        return Err(Error::invalid_arg(format!(
            "the session isn't attached with the record for {:?}: a probe proves the token it's              sent with, so attach with the record's own endpoint and token",
            record.name
        )));
    }
    session.file_exists(IMPORT_PROBE_PATH).await?;
    Ok(Probed {
        microvm_id: record.microvm_id.clone(),
        endpoint: record.endpoint.clone(),
        agent_token: record.agent_token.clone(),
    })
}

/// Writes `record` into `store` on the strength of `probed`, which must be the probe of this
/// record's triple.
///
/// Refuses a name `store` holds for another VM, or can't read, and writes nothing: a name is a
/// promise about which VM answers to it. The same VM under the same name is refreshed, and
/// [`Imported::replaced`] says so.
pub fn import_probed(
    store: &dyn NameStore,
    record: &NameRecord,
    probed: Probed,
) -> Result<Imported, Error> {
    if probed.microvm_id != record.microvm_id
        || probed.endpoint != record.endpoint
        || probed.agent_token != record.agent_token
    {
        return Err(Error::invalid_arg(format!(
            "the probe was of {}, not the record for {:?}: an import writes only the triple              that answered",
            probed.microvm_id, record.name
        )));
    }
    let record = NameRecord::from_json(record.to_json())?;
    let replaced = match holder(store, &record) {
        NameHolder::Free => false,
        NameHolder::Same => true,
        NameHolder::Other(holder) => return Err(taken(store, &record, holder)),
    };
    store.put(&record)?;
    Ok(Imported { replaced })
}

/// Registers `record` in `store` once `session`, attached with the record's own endpoint and
/// token, has answered one authenticated request (#270).
///
/// A name `store` holds for another VM is refused before the probe, so a refusal costs no
/// request; a probe that fails writes nothing. [`probe`] and [`import_probed`] are the two
/// halves, for a caller that checks more between them (the CLI's `attach --verify-identity`
/// runs its identity handshake there).
pub async fn import(
    store: &dyn NameStore,
    record: &NameRecord,
    session: &Session,
) -> Result<Imported, Error> {
    if let NameHolder::Other(holder) = holder(store, record) {
        return Err(taken(store, record, holder));
    }
    let probed = probe(session, record).await?;
    import_probed(store, record, probed)
}

fn taken(store: &dyn NameStore, record: &NameRecord, holder: Option<String>) -> Error {
    Error::new(
        ErrorKind::Precondition,
        format!(
            "{:?} is already registered in {} to {}, and this import names {}: a name is a              promise about which VM answers to it, and nothing overwrites one silently",
            record.name,
            store.describe(),
            holder.as_deref().unwrap_or("an unreadable record"),
            record.microvm_id,
        ),
    )
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;
    use std::sync::{Arc, Mutex};

    use super::*;
    use crate::session::testing::{Recorder, Reply, session_with};

    /// A store in memory; `torn` names read as unreadable, as a killed write leaves one.
    #[derive(Default)]
    struct Memory {
        records: Mutex<BTreeMap<String, NameRecord>>,
        torn: Vec<String>,
    }

    impl NameStore for Memory {
        fn get(&self, name: &str) -> Result<Option<NameRecord>, Error> {
            if self.torn.iter().any(|torn| torn == name) {
                return Err(Error::new(ErrorKind::Precondition, "torn"));
            }
            Ok(self.records.lock().expect("unpoisoned").get(name).cloned())
        }
        fn put(&self, record: &NameRecord) -> Result<(), Error> {
            self.records
                .lock()
                .expect("unpoisoned")
                .insert(record.name.clone(), record.clone());
            Ok(())
        }
        fn delete(&self, name: &str) -> Result<bool, Error> {
            Ok(self
                .records
                .lock()
                .expect("unpoisoned")
                .remove(name)
                .is_some())
        }
        fn list(&self) -> Result<Vec<NameRecord>, Error> {
            Ok(self
                .records
                .lock()
                .expect("unpoisoned")
                .values()
                .cloned()
                .collect())
        }
    }

    /// The record `session_with`'s session is attached with.
    fn record(name: &str, microvm_id: &str) -> NameRecord {
        NameRecord::new_at(
            name,
            microvm_id,
            "https://vm.example",
            "agent-token-abcdef",
            "us-east-1",
            1,
        )
        .expect("a record")
    }

    fn answered(status: u16) -> Arc<Recorder> {
        Recorder::with([Reply::Body(status, Vec::new())])
    }

    /// An import with a token the daemon refuses writes nothing, and says the daemon's class;
    /// one it accepts writes the record, and a second of the same VM refreshes it (#270).
    ///
    /// **Falsification**: `verify/guards/faults/names-import.toml` entry
    /// `app-import-writes-before-the-probe` puts the record before the probe, and the refused
    /// row finds it written.
    #[tokio::test]
    async fn an_import_writes_only_after_the_vm_answers_with_the_records_token() {
        let store = Memory::default();
        let (refusing, _, _) = session_with(answered(401));
        let error = import(&store, &record("ci", "mvm-1"), &refusing)
            .await
            .expect_err("a refused token");
        assert_eq!(error.kind(), ErrorKind::Credentials, "{error}");
        assert!(store.get("ci").expect("reads").is_none(), "nothing written");

        let (answering, _, _) = session_with(answered(200));
        let imported = import(&store, &record("ci", "mvm-1"), &answering)
            .await
            .expect("an answered probe");
        assert_eq!(imported, Imported { replaced: false });
        assert_eq!(
            store.get("ci").expect("reads").map(|r| r.microvm_id),
            Some("mvm-1".into())
        );

        let (again, _, _) = session_with(answered(404));
        let refreshed = import(&store, &record("ci", "mvm-1"), &again)
            .await
            .expect("absence answers behind the same token check");
        assert_eq!(refreshed, Imported { replaced: true });
    }

    /// A name another VM holds, or an unreadable record holds, is refused before any request,
    /// and the holder's record is untouched.
    #[tokio::test]
    async fn an_import_refuses_a_taken_name_before_any_request() {
        let store = Memory {
            torn: vec!["torn".into()],
            ..Memory::default()
        };
        store.put(&record("ci", "mvm-holder")).expect("seeded");
        for name in ["ci", "torn"] {
            let recorder = Recorder::with([]);
            let (session, _, _) = session_with(Arc::clone(&recorder));
            let error = import(&store, &record(name, "mvm-new"), &session)
                .await
                .expect_err("a taken name");
            assert_eq!(error.kind(), ErrorKind::Precondition, "{name}: {error}");
            assert!(recorder.requests().is_empty(), "{name}: no request");
        }
        assert_eq!(
            store.get("ci").expect("reads").map(|r| r.microvm_id),
            Some("mvm-holder".into())
        );
    }

    /// A probe proves the session it was sent over, so it refuses a session attached with
    /// another triple, and a write refuses the probe of another record.
    #[tokio::test]
    async fn a_probe_is_of_one_record_and_its_write_takes_only_that_one() {
        let (session, _, _) = session_with(answered(200));
        let mut stranger = record("ci", "mvm-1");
        stranger.agent_token = "another-token".into();
        let error = probe(&session, &stranger).await.expect_err("another token");
        assert_eq!(error.kind(), ErrorKind::InvalidArg, "{error}");

        let probed = probe(&session, &record("ci", "mvm-1"))
            .await
            .expect("probed");
        let error = import_probed(&Memory::default(), &record("ci", "mvm-2"), probed)
            .expect_err("the probe of another VM");
        assert_eq!(error.kind(), ErrorKind::InvalidArg, "{error}");
    }
}
