// SPDX-License-Identifier: Apache-2.0
//! Named VMs: the CLI's name registry, and records a caller can keep anywhere.
//!
//! A thin wrapper over `microvms_core::names`. A record holds the VM's agent token, a
//! bearer credential, so it never appears in repr; `to_dict` is the one way to read it out,
//! for storing in the caller's own private store.

use std::path::PathBuf;

use microvms_core::names::{FileNameStore, NameRecord, NameStore};
use microvms_core::prelude::*;
use pyo3::prelude::*;
use pyo3::types::PyDict;

use crate::errors::{CoreError, PyCoreResult};
use crate::region::PyRegion;
use crate::sandbox::PySandbox;

/// One named VM: everything `Sandbox.adopt` needs, under a name.
///
/// Holds a secret. `agent_token` is readable so the VM can be adopted, but it stays out of
/// `repr`; keep records (and `to_dict()` output) in private storage.
#[pyclass(frozen, skip_from_py_object, name = "NameRecord", module = "microvms")]
#[derive(Clone)]
pub struct PyNameRecord {
    pub(crate) inner: NameRecord,
}

#[pymethods]
impl PyNameRecord {
    /// A record for a VM this process can already address. Refuses an illegal name or an
    /// empty id, endpoint, or token.
    #[new]
    fn new(
        name: String,
        microvm_id: String,
        endpoint: String,
        agent_token: String,
        region: PyRegion,
    ) -> PyCoreResult<Self> {
        Ok(Self {
            inner: NameRecord::new(
                name,
                microvm_id,
                endpoint,
                agent_token,
                region.inner.as_str(),
            )
            .map_err(CoreError)?,
        })
    }

    /// A record naming the VM `sandbox` addresses — launched or adopted.
    #[staticmethod]
    fn for_sandbox(name: &str, sandbox: &PySandbox) -> PyCoreResult<Self> {
        let inner = sandbox
            .read(|sandbox| sandbox.name_record(name))
            .map_err(CoreError)?;
        Ok(Self { inner })
    }

    /// A record from `to_dict()` output (or a CLI registry file's JSON), checked.
    #[staticmethod]
    fn from_dict(py: Python<'_>, record: &Bound<'_, PyDict>) -> PyCoreResult<Self> {
        let text: String = py
            .import("json")
            .and_then(|json| json.call_method1("dumps", (record,)))
            .and_then(|dumped| dumped.extract())
            .map_err(|_| {
                CoreError(microvms_core::error::Error::invalid_arg(
                    "a name record dict must be JSON-serializable",
                ))
            })?;
        let value: serde_json::Value = serde_json::from_str(&text).map_err(|_| {
            CoreError(microvms_core::error::Error::invalid_arg(
                "a name record dict must be JSON-serializable",
            ))
        })?;
        Ok(Self {
            inner: NameRecord::from_json(value).map_err(CoreError)?,
        })
    }

    /// The record as a JSON-safe dict with the CLI registry's camelCase keys, **agent token
    /// included** — the form to store privately and read back with `from_dict`.
    fn to_dict<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        py.import("json")?
            .call_method1("loads", (self.inner.to_json().to_string(),))
    }

    #[getter]
    fn name(&self) -> &str {
        &self.inner.name
    }

    #[getter]
    fn microvm_id(&self) -> &str {
        &self.inner.microvm_id
    }

    #[getter]
    fn endpoint(&self) -> &str {
        &self.inner.endpoint
    }

    /// The VM's bearer credential. Store only privately; never in repr.
    #[getter]
    fn agent_token(&self) -> &str {
        &self.inner.agent_token
    }

    /// The region the VM runs in, as the record spells it.
    #[getter]
    fn region(&self) -> &str {
        &self.inner.region
    }

    /// Seconds since the epoch when the name was registered.
    #[getter]
    fn at(&self) -> u64 {
        self.inner.at
    }

    /// The launch's egress posture label, or `None` when the registering process did not
    /// know it.
    #[getter]
    fn egress_posture(&self) -> Option<&str> {
        self.inner.egress_posture.as_deref()
    }

    fn __eq__(&self, other: &Self) -> bool {
        self.inner == other.inner
    }

    fn __repr__(&self) -> String {
        format!(
            "NameRecord(name={:?}, microvm_id={:?}, region={:?}, agent_token=<redacted>)",
            self.inner.name, self.inner.microvm_id, self.inner.region
        )
    }
}

/// The CLI's name registry: one owner-only JSON file per name under `<state_dir>/names/`.
///
/// `state_dir` defaults to the CLI's — `$MICROVM_STATE_DIR`, else `~/.microvm/runs` — so a
/// name registered here resolves in `microvm exec --name` and the reverse. To keep names in
/// your own database instead, store `NameRecord.to_dict()` there and adopt with
/// `Sandbox.adopt` from the record's fields.
#[pyclass(frozen, name = "NameRegistry", module = "microvms")]
pub struct PyNameRegistry {
    pub(crate) store: FileNameStore,
}

#[pymethods]
impl PyNameRegistry {
    #[new]
    #[pyo3(signature = (state_dir=None))]
    fn new(state_dir: Option<PathBuf>) -> Self {
        let store = match state_dir {
            Some(dir) => FileNameStore::in_state_root(&dir),
            // The binding's one environment read: core's resolver takes the lookup, and this is
            // where the binding composes them.
            #[expect(
                clippy::disallowed_methods,
                reason = "hands core's process lookup to core's resolver; nothing else calls it"
            )]
            None => FileNameStore::default_location(&microvms_core::env::process),
        };
        Self { store }
    }

    /// The directory holding the name files.
    #[getter]
    fn directory(&self) -> String {
        self.store.dir().display().to_string()
    }

    /// The record registered as `name`, or `None`. A file that exists but does not parse
    /// raises rather than reading as free: its name stays taken until someone inspects it.
    fn get(&self, name: &str) -> PyCoreResult<Option<PyNameRecord>> {
        Ok(self
            .store
            .get(name)
            .map_err(CoreError)?
            .map(|inner| PyNameRecord { inner }))
    }

    /// Writes `record` under its name, replacing any earlier record, owner-only on Unix.
    fn put(&self, record: &PyNameRecord) -> PyCoreResult<()> {
        self.store.put(&record.inner).map_err(CoreError)
    }

    /// Names the VM `sandbox` addresses and writes the record; returns it.
    fn register(&self, name: &str, sandbox: &PySandbox) -> PyCoreResult<PyNameRecord> {
        let record = PyNameRecord::for_sandbox(name, sandbox)?;
        self.put(&record)?;
        Ok(record)
    }

    /// Removes `name`, answering whether a record was there.
    fn delete(&self, name: &str) -> PyCoreResult<bool> {
        self.store.delete(name).map_err(CoreError)
    }

    /// Every readable record, sorted by name.
    fn list(&self) -> PyCoreResult<Vec<PyNameRecord>> {
        Ok(self
            .store
            .list()
            .map_err(CoreError)?
            .into_iter()
            .map(|inner| PyNameRecord { inner })
            .collect())
    }

    /// Removes every name registered to `microvm_id` and returns them — the step after a
    /// terminate, so no name outlives its VM.
    fn release_by_vm(&self, microvm_id: &str) -> PyCoreResult<Vec<String>> {
        self.store.release_by_vm(microvm_id).map_err(CoreError)
    }

    fn __repr__(&self) -> String {
        format!("NameRegistry({:?})", self.store.dir().display().to_string())
    }
}
