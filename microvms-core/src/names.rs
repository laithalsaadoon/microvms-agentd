// SPDX-License-Identifier: Apache-2.0
//! Named MicroVMs: records that let a later process find a VM by name and adopt it.
//!
//! The platform offers no lookup of its own. `RunMicrovm` takes no tags and tagging a
//! running MicroVM fails (docs/PLATFORM.md, "Tagging works on images and not on
//! MicroVMs"), so a name has to live beside the caller. A [`NameRecord`] carries
//! everything [`crate::sandbox::Sandbox::adopt`] needs — id, endpoint, agent token,
//! region — so a name replaces the whole triple rather than just the id.
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

use serde::{Deserialize, Serialize};

use crate::error::{Error, ErrorKind};
use crate::region::Region;

/// A VM name's shape: ASCII letters, digits, `-`, `_`, at most 128 bytes, and never a
/// MicroVM id prefix.
///
/// The charset is the image-name pattern (`[a-zA-Z0-9-_]+`). It makes the CLI's
/// identifier resolution total — an identifier starting with `microvm-` can only be an id,
/// because a legal name is refused that prefix, and an ARN cannot match because `:` is
/// outside the set — and it makes every name a safe file name.
pub fn validate_name(name: &str) -> Result<(), String> {
    if name.is_empty() {
        return Err("a VM name cannot be empty".to_string());
    }
    if name.len() > 128 {
        return Err(format!(
            "a VM name is at most 128 bytes; this one is {}",
            name.len()
        ));
    }
    // Both spellings: `microvm-` is the id prefix the real service answers (measured
    // 2026-08-28, first live run of names — the fakes' `mvm-` fixture shape let a
    // passthrough keyed on `mvm-` alone pass every scripted test and fail against AWS),
    // and `mvm-` stays refused because it is the fixture shape every scripted body uses.
    if name.starts_with("microvm-") || name.starts_with("mvm-") {
        return Err(format!(
            "{name:?} starts with a MicroVM id prefix — a name shaped like an id would make \
             `microvm suspend <identifier>` ambiguous about which VM it addresses"
        ));
    }
    if let Some(bad) = name
        .chars()
        .find(|c| !c.is_ascii_alphanumeric() && *c != '-' && *c != '_')
    {
        return Err(format!(
            "{bad:?} is not a legal VM-name character: names take ASCII letters, digits, `-` \
             and `_`, the image-name pattern"
        ));
    }
    Ok(())
}

/// One kept VM's name, and everything an adopt needs to address it.
///
/// `camelCase` on the wire: this is the CLI registry's on-disk format, read by later
/// versions, so fields are only ever added, and optional ones default when absent.
#[derive(Clone, Deserialize, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct NameRecord {
    pub name: String,
    pub microvm_id: String,
    pub endpoint: String,
    /// The launch's agent token: a bearer credential for the VM.
    pub agent_token: String,
    pub region: String,
    /// Seconds since the epoch when the name was registered.
    pub at: u64,
    /// The launching host's identity secret, base64, when `run --identity` generated one.
    ///
    /// Persisted for the reason the agent token is: a later `tunnel --verify-identity`
    /// needs it. It raises what a stolen record can do from "call the VM" to "call the VM
    /// and impersonate the launching host to it", which is the same trust domain.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub identity_host_seed: Option<String>,
    /// The VM's public key, base64 — the pin `--verify-identity` checks the far end against.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub identity_vm_public_key: Option<String>,
    /// The egress posture label of the launch this name was registered for, when the
    /// registering command launched the VM. `None` means unknown, never "open".
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub egress_posture: Option<String>,
}

impl NameRecord {
    /// A record for a VM this process can already address, stamped with the current time.
    ///
    /// Refuses an illegal name or an empty id, endpoint, or token: a record missing any of
    /// them resolves to a VM nobody can adopt.
    pub fn new(
        name: impl Into<String>,
        microvm_id: impl Into<String>,
        endpoint: impl Into<String>,
        agent_token: impl Into<String>,
        region: impl Into<String>,
    ) -> Result<Self, Error> {
        let record = Self {
            name: name.into(),
            microvm_id: microvm_id.into(),
            endpoint: endpoint.into(),
            agent_token: agent_token.into(),
            region: region.into(),
            at: std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|since| since.as_secs())
                .unwrap_or(0),
            identity_host_seed: None,
            identity_vm_public_key: None,
            egress_posture: None,
        };
        record.check()?;
        Ok(record)
    }

    /// The record as JSON, secrets included — the form to store privately.
    pub fn to_json(&self) -> serde_json::Value {
        serde_json::to_value(self).unwrap_or(serde_json::Value::Null)
    }

    /// A record from its JSON form, checked the way [`NameRecord::new`] checks.
    pub fn from_json(value: serde_json::Value) -> Result<Self, Error> {
        // The serde message can quote the offending value, which may be the token itself.
        let record: Self = serde_json::from_value(value).map_err(|_| {
            Error::invalid_arg(
                "not a name record: it needs string name, microvmId, endpoint, agentToken, \
                 and region, and a numeric at",
            )
        })?;
        record.check()?;
        Ok(record)
    }

    /// The region the VM runs in. A record's region is one this client wrote, so a name it
    /// no longer lists is taken as written rather than refused.
    pub fn region(&self) -> Region {
        Region::unlisted(self.region.as_str())
    }

    fn check(&self) -> Result<(), Error> {
        validate_name(&self.name).map_err(Error::invalid_arg)?;
        for (field, value) in [
            ("microvmId", &self.microvm_id),
            ("endpoint", &self.endpoint),
            ("agentToken", &self.agent_token),
            ("region", &self.region),
        ] {
            if value.is_empty() {
                return Err(Error::invalid_arg(format!(
                    "name record {:?} has an empty {field}; a record must carry everything an \
                     adopt needs",
                    self.name
                )));
            }
        }
        Ok(())
    }
}

/// Redacts the agent token and the identity seed: this type reaches logs through errors
/// and test output, and both values are credentials.
impl std::fmt::Debug for NameRecord {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("NameRecord")
            .field("name", &self.name)
            .field("microvm_id", &self.microvm_id)
            .field("endpoint", &self.endpoint)
            .field("agent_token", &"<redacted>")
            .field("region", &self.region)
            .field("at", &self.at)
            .field(
                "identity_host_seed",
                &self.identity_host_seed.as_ref().map(|_| "<redacted>"),
            )
            .field("identity_vm_public_key", &self.identity_vm_public_key)
            .field("egress_posture", &self.egress_posture)
            .finish()
    }
}

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
/// The region check is the guard: an id from one region addresses nothing in another, and
/// adopting it would fail at the first control-plane call with a not-found that says
/// nothing about the name.
pub fn resolve(
    store: &dyn NameStore,
    name: &str,
    expected_region: Option<&Region>,
) -> Result<NameRecord, Error> {
    let record = store.get(name)?.ok_or_else(|| {
        Error::new(
            ErrorKind::Precondition,
            format!(
                "no VM is named {name:?} in {}; register one first, or adopt by id",
                store.describe()
            ),
        )
    })?;
    if let Some(expected) = expected_region
        && expected.as_str() != record.region
    {
        return Err(Error::invalid_arg(format!(
            "{name:?} was registered in {}, not {}",
            record.region,
            expected.as_str()
        )));
    }
    Ok(record)
}

/// The state directory the CLI uses: `$MICROVM_STATE_DIR`, else `~/.microvm/runs`.
pub fn default_state_root(env: &dyn Fn(&str) -> Option<String>) -> PathBuf {
    if let Some(dir) = env("MICROVM_STATE_DIR") {
        return PathBuf::from(dir);
    }
    let home = env("HOME").unwrap_or_else(|| ".".to_string());
    PathBuf::from(home).join(".microvm").join("runs")
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

    #[test]
    fn neither_debug_nor_a_rejection_prints_a_secret() {
        let mut canaried = record("ci", "microvm-a");
        canaried.agent_token = "tok-CANARY".into();
        canaried.identity_host_seed = Some("seed-CANARY".into());
        let debug = format!("{canaried:?}");
        assert!(!debug.contains("CANARY"), "{debug}");
        assert!(debug.contains("<redacted>"), "{debug}");
        let error = NameRecord::from_json(serde_json::json!({
            "name": "ci", "microvmId": "microvm-a", "endpoint": "e",
            "agentToken": "tok-CANARY", "region": "us-east-1", "at": "not-a-number"
        }))
        .expect_err("at must be numeric");
        assert!(!error.to_string().contains("CANARY"), "{error}");
    }

    #[test]
    fn records_refuse_illegal_names_and_missing_fields() {
        assert!(NameRecord::new("microvm-x", "id", "e", "t", "us-east-1").is_err());
        assert!(NameRecord::new("has space", "id", "e", "t", "us-east-1").is_err());
        let error = NameRecord::new("ci", "id", "e", "", "us-east-1").expect_err("no token");
        assert!(error.to_string().contains("agentToken"), "{error}");
        let dir = TempDir::new("illegal");
        let store = FileNameStore::in_state_root(&dir.0);
        // A path-shaped name must never become a path outside the registry.
        assert!(store.get("../escape").is_err());
        assert!(store.delete("../escape").is_err());
        let round = NameRecord::from_json(record("ci", "microvm-a").to_json()).expect("ok");
        assert_eq!(round, record("ci", "microvm-a").clone_with_at(round.at));
    }

    impl NameRecord {
        fn clone_with_at(mut self, at: u64) -> Self {
            self.at = at;
            self
        }
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
