// SPDX-License-Identifier: Apache-2.0
//! Named MicroVMs: records that let a later process find a VM by name and adopt it.
//!
//! The record and its rules are `microvms_domain::names`, re-exported here. A [`NameRecord`]
//! carries everything [`crate::sandbox::Sandbox::adopt`] needs (id, endpoint, agent token,
//! region), so a name replaces the whole triple rather than just the id.
//!
//! Storage is a [`NameStore`]. [`FileNameStore`] is the CLI's registry, one owner-only
//! JSON file per name under `<state dir>/names/`, byte-compatible with every record the
//! CLI has written. A caller with its own database implements [`NameStore`] or keeps
//! [`NameRecord`]'s JSON form wherever it likes.
//!
//! **A record holds a secret.** The agent token is a bearer credential for the VM, and
//! the optional identity seed can impersonate the launching host to it. Keep records in
//! private storage; neither value appears in `Debug` output or error messages.

use std::path::{Path, PathBuf};

pub use microvms_domain::names::*;

use crate::error::{Error, ErrorKind};
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

/// The CLI's registry: one JSON file per name in a directory, owner-only on Unix.
#[derive(Clone, Debug)]
pub struct FileNameStore {
    dir: PathBuf,
}

impl FileNameStore {
    /// A store over the directory that holds the name files themselves.
    pub fn new(dir: impl Into<PathBuf>) -> Self {
        Self { dir: dir.into() }
    }

    /// The registry under `state_root/names` — where the CLI keeps it.
    ///
    /// A subdirectory rather than the state root because the run ledger's `*.json` glob
    /// must never read a name record as a ledger.
    pub fn in_state_root(state_root: &Path) -> Self {
        Self::new(state_root.join("names"))
    }

    /// The registry the CLI would use in this environment.
    pub fn default_location(env: &dyn Fn(&str) -> Option<String>) -> Self {
        Self::in_state_root(&default_state_root(env))
    }

    /// The directory holding the name files.
    pub fn dir(&self) -> &Path {
        &self.dir
    }

    /// Where `name`'s record lives, whether or not one is registered.
    pub fn path_of(&self, name: &str) -> PathBuf {
        self.dir.join(format!("{name}.json"))
    }

    fn io(&self, action: &str, path: &Path, error: std::io::Error) -> Error {
        Error::new(
            ErrorKind::Precondition,
            format!("could not {action} name record {}: {error}", path.display()),
        )
        .with_source(error)
    }
}

impl NameStore for FileNameStore {
    /// A file that exists but does not parse is an error, not a free name: a torn record
    /// is what a killed write leaves, and treating it as free would let a second VM claim
    /// a name whose first holder may still be billing.
    fn get(&self, name: &str) -> Result<Option<NameRecord>, Error> {
        validate_name(name).map_err(Error::invalid_arg)?;
        let path = self.path_of(name);
        let text = match std::fs::read_to_string(&path) {
            Ok(text) => text,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(self.io("read", &path, error)),
        };
        serde_json::from_str(&text).map(Some).map_err(|_| {
            Error::new(
                ErrorKind::Precondition,
                format!(
                    "name record {} is unreadable; the name stays taken until you inspect \
                     or delete that file",
                    path.display()
                ),
            )
        })
    }

    fn put(&self, record: &NameRecord) -> Result<(), Error> {
        validate_name(&record.name).map_err(Error::invalid_arg)?;
        std::fs::create_dir_all(&self.dir).map_err(|error| self.io("create", &self.dir, error))?;
        let path = self.path_of(&record.name);
        let text = serde_json::to_string_pretty(record)
            .map_err(|_| Error::new(ErrorKind::Unexpected, "a name record failed to serialize"))?;
        write_private(&path, text.as_bytes()).map_err(|error| self.io("write", &path, error))
    }

    fn delete(&self, name: &str) -> Result<bool, Error> {
        validate_name(name).map_err(Error::invalid_arg)?;
        let path = self.path_of(name);
        match std::fs::remove_file(&path) {
            Ok(()) => Ok(true),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(false),
            Err(error) => Err(self.io("delete", &path, error)),
        }
    }

    fn describe(&self) -> String {
        self.dir.display().to_string()
    }

    /// Skips files that are not records: a torn file is reported by [`NameStore::get`]
    /// for its own name, and must not make every other name unlistable.
    fn list(&self) -> Result<Vec<NameRecord>, Error> {
        let entries = match std::fs::read_dir(&self.dir) {
            Ok(entries) => entries,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
            Err(error) => return Err(self.io("list", &self.dir, error)),
        };
        let mut records: Vec<NameRecord> = entries
            .filter_map(Result::ok)
            .map(|entry| entry.path())
            .filter(|path| path.extension().is_some_and(|ext| ext == "json"))
            .filter_map(|path| std::fs::read_to_string(path).ok())
            .filter_map(|text| serde_json::from_str(&text).ok())
            .collect();
        records.sort_by(|a, b| a.name.cmp(&b.name));
        Ok(records)
    }
}

/// Writes `bytes` so no other user can read them at any point.
///
/// On Unix the file is created 0600 rather than written and then narrowed, which would
/// leave the token readable under the process umask for the length of the write. An
/// existing file is narrowed before it is overwritten. Elsewhere the state directory keeps
/// the profile's own ACLs.
fn write_private(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    use std::io::Write as _;
    let mut options = std::fs::OpenOptions::new();
    options.write(true).create(true).truncate(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::{OpenOptionsExt as _, PermissionsExt as _};
        options.mode(0o600);
        if path.exists() {
            std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600))?;
        }
    }
    let mut file = options.open(path)?;
    file.write_all(bytes)?;
    file.sync_all()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::prelude::*;

    struct TempDir(PathBuf);

    impl TempDir {
        fn new(tag: &str) -> Self {
            let unique = format!(
                "microvms-names-{tag}-{}-{}",
                std::process::id(),
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|d| d.as_nanos())
                    .unwrap_or(0)
            );
            let path = std::env::temp_dir().join(unique);
            std::fs::create_dir_all(&path).expect("temp dir");
            Self(path)
        }
    }

    impl Drop for TempDir {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    fn record(name: &str, id: &str) -> NameRecord {
        NameRecord::new(name, id, "https://vm.example", "secret-token", "us-east-1").expect("ok")
    }

    /// A record exactly as the CLI has written it since names shipped: pretty JSON,
    /// camelCase, optional fields present in one and absent in the other.
    #[test]
    fn records_written_by_earlier_cli_versions_still_resolve() {
        let dir = TempDir::new("compat");
        let store = FileNameStore::in_state_root(&dir.0);
        std::fs::create_dir_all(store.dir()).expect("dir");
        std::fs::write(
            store.path_of("old"),
            r#"{
  "name": "old",
  "microvmId": "microvm-1",
  "endpoint": "https://vm.example",
  "agentToken": "tok",
  "region": "us-east-1",
  "at": 1789000000
}"#,
        )
        .expect("write");
        std::fs::write(
            store.path_of("pinned"),
            r#"{"name":"pinned","microvmId":"microvm-2","endpoint":"e","agentToken":"t",
                "region":"us-west-2","at":1,"identityHostSeed":"c2VlZA==",
                "identityVmPublicKey":"cHVi","egressPosture":"managed"}"#,
        )
        .expect("write");
        let old = store.get("old").expect("reads").expect("present");
        assert_eq!(
            (old.microvm_id.as_str(), old.at, old.egress_posture),
            ("microvm-1", 1_789_000_000, None)
        );
        let pinned = store.get("pinned").expect("reads").expect("present");
        assert_eq!(pinned.identity_host_seed.as_deref(), Some("c2VlZA=="));
        assert_eq!(pinned.egress_posture.as_deref(), Some("managed"));
        // And what this version writes, an earlier reader can read: the same keys.
        let written = serde_json::to_value(&pinned).expect("json");
        for key in [
            "name",
            "microvmId",
            "endpoint",
            "agentToken",
            "region",
            "at",
        ] {
            assert!(written.get(key).is_some(), "{key} missing from {written}");
        }
    }

    #[test]
    fn put_get_list_delete_and_release_round_trip() {
        let dir = TempDir::new("crud");
        let store = FileNameStore::in_state_root(&dir.0);
        assert_eq!(store.get("ci").expect("ok"), None);
        assert!(store.list().expect("ok").is_empty());
        store.put(&record("ci", "microvm-a")).expect("put");
        store.put(&record("alias", "microvm-a")).expect("put");
        store.put(&record("other", "microvm-b")).expect("put");
        assert_eq!(
            store.get("ci").expect("ok").map(|r| r.microvm_id),
            Some("microvm-a".to_string())
        );
        let names: Vec<_> = store
            .list()
            .expect("ok")
            .into_iter()
            .map(|r| r.name)
            .collect();
        assert_eq!(names, ["alias", "ci", "other"]);
        assert_eq!(
            store.release_by_vm("microvm-a").expect("ok"),
            ["alias", "ci"]
        );
        assert!(store.delete("other").expect("ok"));
        assert!(!store.delete("other").expect("ok"));
        assert!(store.list().expect("ok").is_empty());
    }

    #[test]
    fn a_torn_record_keeps_its_name_taken_without_hiding_the_others() {
        let dir = TempDir::new("torn");
        let store = FileNameStore::in_state_root(&dir.0);
        store.put(&record("good", "microvm-a")).expect("put");
        std::fs::write(store.path_of("torn"), "{\"name\": \"to").expect("write");
        let error = store
            .get("torn")
            .expect_err("a torn file is not a free name");
        assert_eq!(error.kind(), ErrorKind::Precondition);
        assert!(error.to_string().contains("torn.json"), "{error}");
        let names: Vec<_> = store
            .list()
            .expect("ok")
            .into_iter()
            .map(|r| r.name)
            .collect();
        assert_eq!(names, ["good"]);
    }

    /// A path-shaped name must never become a path outside the registry.
    #[test]
    fn a_path_shaped_name_is_refused_before_it_reaches_the_filesystem() {
        let dir = TempDir::new("illegal");
        let store = FileNameStore::in_state_root(&dir.0);
        assert!(store.get("../escape").is_err());
        assert!(store.delete("../escape").is_err());
    }

    #[test]
    fn resolve_refuses_a_missing_name_and_a_foreign_region() {
        let dir = TempDir::new("resolve");
        let store = FileNameStore::in_state_root(&dir.0);
        let missing = resolve(&store, "ci", None).expect_err("no such name");
        assert_eq!(missing.kind(), ErrorKind::Precondition);
        assert!(missing.to_string().contains("names"), "{missing}");
        store.put(&record("ci", "microvm-a")).expect("put");
        assert_eq!(
            resolve(&store, "ci", Some(&Region::UsEast1))
                .expect("same region")
                .microvm_id,
            "microvm-a"
        );
        let foreign = resolve(&store, "ci", Some(&Region::UsWest2)).expect_err("other region");
        assert_eq!(foreign.kind(), ErrorKind::InvalidArg);
        assert!(!foreign.to_string().contains("secret-token"), "{foreign}");
    }

    #[cfg(unix)]
    #[test]
    fn a_record_file_is_owner_only_from_creation_and_after_overwrite() {
        use std::os::unix::fs::PermissionsExt as _;
        let dir = TempDir::new("mode");
        let store = FileNameStore::in_state_root(&dir.0);
        store.put(&record("ci", "microvm-a")).expect("put");
        let mode = |p: &Path| std::fs::metadata(p).expect("meta").permissions().mode() & 0o777;
        assert_eq!(mode(&store.path_of("ci")), 0o600);
        std::fs::set_permissions(store.path_of("ci"), std::fs::Permissions::from_mode(0o644))
            .expect("widen");
        store.put(&record("ci", "microvm-b")).expect("overwrite");
        assert_eq!(mode(&store.path_of("ci")), 0o600);
    }

    #[test]
    fn the_default_location_is_the_clis() {
        let env = |key: &str| match key {
            "HOME" => Some("/home/u".to_string()),
            _ => None,
        };
        assert_eq!(
            FileNameStore::default_location(&env).dir(),
            Path::new("/home/u/.microvm/runs/names")
        );
        let pinned = |key: &str| (key == "MICROVM_STATE_DIR").then(|| "/state".to_string());
        assert_eq!(default_state_root(&pinned), PathBuf::from("/state"));
    }
}
