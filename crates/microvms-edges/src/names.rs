// SPDX-License-Identifier: Apache-2.0
//! The CLI's name registry: one owner-only JSON file per name under `<state dir>/names/`.
//!
//! The record, its rules and the [`NameStore`] port are `microvms_app::names`. [`FileNameStore`]
//! is the implementation that reads and writes files, byte-compatible with every record the CLI
//! has written.
//!
//! **A record holds a secret.** The agent token is a bearer credential for the VM, and the
//! optional identity seed can impersonate the launching host to it. The files are owner-only
//! from creation, and neither value appears in `Debug` output or error messages.

use std::path::{Path, PathBuf};

use microvms_app::error::{Error, ErrorKind};
use microvms_app::names::{NameRecord, NameStore, validate_name};

/// Resolve the shared state root from an explicit setting or the user's absolute home.
///
/// A default never depends on the working directory. Explicit state settings may be
/// relative because the caller deliberately selected their location.
pub fn state_root(env: &dyn Fn(&str) -> Option<String>) -> Result<PathBuf, Error> {
    resolve_state_root(env, || {
        #[cfg(unix)]
        {
            // The Unix implementation consults the passwd database when HOME is unset.
            std::env::home_dir()
        }
        #[cfg(not(unix))]
        {
            None
        }
    })
}

fn resolve_state_root(
    env: &dyn Fn(&str) -> Option<String>,
    home: impl FnOnce() -> Option<PathBuf>,
) -> Result<PathBuf, Error> {
    if let Some(dir) = env("MICROVM_STATE_DIR").filter(|dir| !dir.is_empty()) {
        return Ok(PathBuf::from(dir));
    }
    let configured = if cfg!(windows) {
        env("USERPROFILE").or_else(|| env("HOME"))
    } else {
        env("HOME")
    };
    let home = configured.map(PathBuf::from).or_else(home);
    match home {
        Some(home) if home.is_absolute() => Ok(home.join(".microvm").join("runs")),
        _ => Err(Error::new(
            ErrorKind::Precondition,
            "could not determine an absolute user home directory; set MICROVM_STATE_DIR explicitly",
        )),
    }
}

/// The CLI's registry: one JSON file per name in a directory, owner-only on Unix.
#[derive(Clone, Debug)]
pub struct FileNameStore {
    dir: PathBuf,
    unavailable: Option<String>,
}

impl FileNameStore {
    /// A store over the directory that holds the name files themselves.
    pub fn new(dir: impl Into<PathBuf>) -> Self {
        Self {
            dir: dir.into(),
            unavailable: None,
        }
    }

    /// The registry under `state_root/names` — where the CLI keeps it.
    ///
    /// A subdirectory rather than the state root because the run ledger's `*.json` glob
    /// must never read a name record as a ledger.
    pub fn in_state_root(state_root: &Path) -> Self {
        Self::new(state_root.join("names"))
    }

    /// The registry the CLI would use in this environment.
    ///
    /// If no absolute home can be resolved, the returned store refuses every storage
    /// operation with a precondition error and its directory is empty.
    pub fn default_location(env: &dyn Fn(&str) -> Option<String>) -> Self {
        match state_root(env) {
            Ok(root) => Self::in_state_root(&root),
            Err(error) => Self {
                dir: PathBuf::new(),
                unavailable: Some(error.to_string()),
            },
        }
    }

    /// The directory holding the name files.
    pub fn dir(&self) -> &Path {
        &self.dir
    }

    /// Where `name`'s record lives, whether or not one is registered.
    pub fn path_of(&self, name: &str) -> PathBuf {
        self.dir.join(format!("{name}.json"))
    }

    fn ready(&self) -> Result<(), Error> {
        match &self.unavailable {
            Some(message) => Err(Error::new(ErrorKind::Precondition, message.clone())),
            None => Ok(()),
        }
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
        self.ready()?;
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
        self.ready()?;
        validate_name(&record.name).map_err(Error::invalid_arg)?;
        std::fs::create_dir_all(&self.dir).map_err(|error| self.io("create", &self.dir, error))?;
        let path = self.path_of(&record.name);
        let text = serde_json::to_string_pretty(record)
            .map_err(|_| Error::new(ErrorKind::Unexpected, "a name record failed to serialize"))?;
        write_private(&path, text.as_bytes()).map_err(|error| self.io("write", &path, error))
    }

    fn delete(&self, name: &str) -> Result<bool, Error> {
        self.ready()?;
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
        self.ready()?;
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
    use microvms_app::names::{default_state_root, resolve};
    use microvms_app::region::Region;

    #[test]
    fn an_unset_home_uses_the_platform_default_for_the_registry() {
        let store = FileNameStore::default_location(&|_| None);
        #[cfg(unix)]
        assert!(
            store.dir().is_absolute(),
            "the platform user home is absolute"
        );
        #[cfg(not(unix))]
        assert!(
            store.get("example").is_err(),
            "an unavailable home refuses storage"
        );
    }

    #[test]
    fn an_unavailable_default_registry_refuses_operations() {
        let store = FileNameStore::default_location(&|key| {
            matches!(key, "HOME" | "USERPROFILE").then(|| "relative-home".to_string())
        });
        assert!(store.get("example").is_err());
        assert!(store.list().is_err());
        assert!(store.delete("example").is_err());
        assert!(store.put(&record("example", "microvm-1")).is_err());
    }

    #[test]
    fn explicit_state_settings_override_home_and_empty_settings_use_home() {
        let home = TempDir::new("state-override");
        let absolute = home.0.join("chosen-state");
        for (setting, expected) in [
            ("chosen-state".to_string(), PathBuf::from("chosen-state")),
            (absolute.to_string_lossy().into_owned(), absolute),
            (String::new(), home.0.join(".microvm").join("runs")),
        ] {
            let env = |key: &str| match key {
                "MICROVM_STATE_DIR" => Some(setting.clone()),
                "HOME" | "USERPROFILE" => Some(home.0.to_string_lossy().into_owned()),
                _ => None,
            };
            assert_eq!(
                FileNameStore::default_location(&env).dir(),
                expected.join("names")
            );
        }
    }

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
        // 2026-01-01T00:00:00Z: the store keeps whatever time the record carries.
        NameRecord::new_at(
            name,
            id,
            "https://vm.example",
            "secret-token",
            "us-east-1",
            1_767_225_600,
        )
        .expect("ok")
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
            resolve(&store, "ci", Some(&Region::US_EAST_1))
                .expect("same region")
                .microvm_id,
            "microvm-a"
        );
        let foreign = resolve(&store, "ci", Some(&Region::US_WEST_2)).expect_err("other region");
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
        let home = TempDir::new("default-location");
        let env = |key: &str| match key {
            "HOME" => Some(home.0.to_string_lossy().into_owned()),
            _ => None,
        };
        assert_eq!(
            FileNameStore::default_location(&env).dir(),
            home.0.join(".microvm").join("runs").join("names")
        );
        let pinned = |key: &str| (key == "MICROVM_STATE_DIR").then(|| "/state".to_string());
        assert_eq!(default_state_root(&pinned), PathBuf::from("/state"));
    }
}
