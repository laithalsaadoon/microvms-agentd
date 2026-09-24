// SPDX-License-Identifier: Apache-2.0
//! Named VMs: the CLI's name registry, and records a caller can keep anywhere.
//!
//! A thin wrapper over `microvms_core::names`. A record holds the VM's agent token, a
//! bearer credential: a class rather than an object, so `JSON.stringify` gives `{}` and
//! `toObject()` is the one deliberate way to read the token out for private storage.

use microvms_core::error::Error;
use microvms_core::names::{FileNameStore, NameRecord as CoreRecord, NameStore};
use napi_derive::napi;

use crate::errors::{AsyncError, js, js_async};
use crate::region::Region;
use crate::sandbox::Sandbox;

/// A record's JSON-safe form, with the CLI registry's camelCase keys. **Holds the agent
/// token**; store it privately.
#[napi(object)]
pub struct NameRecordObject {
    pub name: String,
    pub microvm_id: String,
    pub endpoint: String,
    pub agent_token: String,
    pub region: String,
    /// Seconds since the epoch when the name was registered.
    pub at: f64,
    pub identity_host_seed: Option<String>,
    pub identity_vm_public_key: Option<String>,
    pub egress_posture: Option<String>,
}

/// One named VM: everything `Sandbox.adopt` needs, under a name.
#[napi]
pub struct NameRecord {
    pub(crate) inner: CoreRecord,
}

#[napi]
impl NameRecord {
    /// A record for a VM this process can already address. Refuses an illegal name or an
    /// empty id, endpoint, or token.
    #[napi(constructor)]
    pub fn new(
        name: String,
        microvm_id: String,
        endpoint: String,
        agent_token: String,
        region: &Region,
    ) -> napi::Result<Self, String> {
        CoreRecord::new(
            name,
            microvm_id,
            endpoint,
            agent_token,
            region.inner.as_str(),
        )
        .map(|inner| Self { inner })
        .map_err(js)
    }

    /// A record naming the VM `sandbox` addresses — launched or adopted.
    #[napi(factory)]
    pub async fn for_sandbox(name: String, sandbox: &Sandbox) -> Result<NameRecord, AsyncError> {
        let inner = sandbox
            .inner
            .lock()
            .await
            .name_record(&name)
            .map_err(js_async)?;
        Ok(NameRecord { inner })
    }

    /// A record from `toObject()` output (or a CLI registry file's JSON), checked.
    #[napi(factory)]
    pub fn from_object(object: NameRecordObject) -> napi::Result<Self, String> {
        if !(object.at.is_finite() && object.at >= 0.0) {
            return Err(js(Error::invalid_arg(
                "a name record's `at` is seconds since the epoch, a non-negative number",
            )));
        }
        let value = serde_json::json!({
            "name": object.name,
            "microvmId": object.microvm_id,
            "endpoint": object.endpoint,
            "agentToken": object.agent_token,
            "region": object.region,
            "at": object.at as u64,
            "identityHostSeed": object.identity_host_seed,
            "identityVmPublicKey": object.identity_vm_public_key,
            "egressPosture": object.egress_posture,
        });
        CoreRecord::from_json(value)
            .map(|inner| Self { inner })
            .map_err(js)
    }

    /// The record as a plain object, **agent token included** — the form to store privately
    /// and read back with `fromObject`.
    #[napi]
    pub fn to_object(&self) -> NameRecordObject {
        let record = &self.inner;
        NameRecordObject {
            name: record.name.clone(),
            microvm_id: record.microvm_id.clone(),
            endpoint: record.endpoint.clone(),
            agent_token: record.agent_token.clone(),
            region: record.region.clone(),
            at: record.at as f64,
            identity_host_seed: record.identity_host_seed.clone(),
            identity_vm_public_key: record.identity_vm_public_key.clone(),
            egress_posture: record.egress_posture.clone(),
        }
    }

    #[napi(getter)]
    pub fn name(&self) -> String {
        self.inner.name.clone()
    }

    #[napi(getter)]
    pub fn microvm_id(&self) -> String {
        self.inner.microvm_id.clone()
    }

    #[napi(getter)]
    pub fn endpoint(&self) -> String {
        self.inner.endpoint.clone()
    }

    /// The VM's bearer credential. A method rather than a getter, so it is never read by
    /// accident; store it only privately.
    #[napi]
    pub fn agent_token(&self) -> String {
        self.inner.agent_token.clone()
    }

    #[napi(getter)]
    pub fn region(&self) -> String {
        self.inner.region.clone()
    }

    /// Seconds since the epoch when the name was registered.
    #[napi(getter)]
    pub fn at(&self) -> f64 {
        self.inner.at as f64
    }

    #[napi(getter)]
    pub fn egress_posture(&self) -> Option<String> {
        self.inner.egress_posture.clone()
    }

    /// The record without its secrets.
    #[napi(js_name = "toString")]
    pub fn describe(&self) -> String {
        format!(
            "NameRecord(name={:?}, microvmId={:?}, region={:?}, agentToken=<redacted>)",
            self.inner.name, self.inner.microvm_id, self.inner.region
        )
    }
}

/// The CLI's name registry: one owner-only JSON file per name under `<stateDir>/names/`.
///
/// `stateDir` defaults to the CLI's — `$MICROVM_STATE_DIR`, else `~/.microvm/runs` — so a
/// name registered here resolves in `microvm exec --name` and the reverse.
#[napi]
pub struct NameRegistry {
    pub(crate) store: FileNameStore,
}

#[napi]
impl NameRegistry {
    #[napi(constructor)]
    pub fn new(state_dir: Option<String>) -> Self {
        let store = match state_dir {
            Some(dir) => FileNameStore::in_state_root(std::path::Path::new(&dir)),
            None => FileNameStore::default_location(&|key| std::env::var(key).ok()),
        };
        Self { store }
    }

    /// The directory holding the name files.
    #[napi(getter)]
    pub fn directory(&self) -> String {
        self.store.dir().display().to_string()
    }

    /// The record registered as `name`, or `null`. A file that exists but does not parse
    /// throws rather than reading as free.
    #[napi]
    pub fn get(&self, name: String) -> napi::Result<Option<NameRecord>, String> {
        self.store
            .get(&name)
            .map(|found| found.map(|inner| NameRecord { inner }))
            .map_err(js)
    }

    /// Writes `record` under its name, replacing any earlier record, owner-only on Unix.
    #[napi]
    pub fn put(&self, record: &NameRecord) -> napi::Result<(), String> {
        self.store.put(&record.inner).map_err(js)
    }

    /// Names the VM `sandbox` addresses and writes the record; resolves with it.
    #[napi]
    pub async fn register(
        &self,
        name: String,
        sandbox: &Sandbox,
    ) -> Result<NameRecord, AsyncError> {
        let record = NameRecord::for_sandbox(name, sandbox).await?;
        self.store.put(&record.inner).map_err(js_async)?;
        Ok(record)
    }

    /// Removes `name`, answering whether a record was there.
    #[napi]
    pub fn delete(&self, name: String) -> napi::Result<bool, String> {
        self.store.delete(&name).map_err(js)
    }

    /// Every readable record, sorted by name.
    #[napi]
    pub fn list(&self) -> napi::Result<Vec<NameRecord>, String> {
        self.store
            .list()
            .map(|records| {
                records
                    .into_iter()
                    .map(|inner| NameRecord { inner })
                    .collect()
            })
            .map_err(js)
    }

    /// Removes every name registered to `microvmId` and returns them — the step after a
    /// terminate, so no name outlives its VM.
    #[napi]
    pub fn release_by_vm(&self, microvm_id: String) -> napi::Result<Vec<String>, String> {
        self.store.release_by_vm(&microvm_id).map_err(js)
    }
}
