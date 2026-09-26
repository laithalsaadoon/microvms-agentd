// SPDX-License-Identifier: Apache-2.0
//! Daemon provisioning: the `agentd` binary for the version this client drives.
//!
//! A thin wrapper over `microvms_core::provision` (BIND-17 through BIND-20). The chain,
//! the verification, the cache, and every refusal are core's; this file converts
//! arguments and releases the GIL while a fetch downloads and verifies.

use std::path::PathBuf;

use microvms_core::provision::{self, Provisioned, Request, Source};
use pyo3::prelude::*;
use pyo3::types::PyBytes;

use crate::errors::{CoreError, PyCoreResult};

/// A provisioned `agentd` binary and how it got here: `provision_agentd_report()`'s answer.
#[pyclass(
    frozen,
    skip_from_py_object,
    name = "ProvisionedAgentd",
    module = "microvms"
)]
pub struct PyProvisionedAgentd {
    inner: Provisioned,
}

#[pymethods]
impl PyProvisionedAgentd {
    /// The binary itself, an aarch64 ELF: the bytes to write into an image build context.
    #[getter]
    fn data<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        PyBytes::new(py, &self.inner.bytes)
    }

    /// Where the binary is on disk: the cache entry, or the caller's own path.
    #[getter]
    fn path(&self) -> String {
        self.inner.path.display().to_string()
    }

    /// `"caller-supplied"`, `"cache"`, or `"fetched"`.
    #[getter]
    fn source(&self) -> &'static str {
        self.inner.source.as_str()
    }

    /// For a caller-supplied binary, `"argument"` (the `binary` parameter) or `"env"`
    /// (`$MICROVM_AGENTD`); otherwise `None`.
    #[getter]
    fn supplied_by(&self) -> Option<&'static str> {
        match self.inner.source {
            Source::CallerSupplied(supplier) => Some(supplier.as_str()),
            Source::Cache(_) | Source::Fetched(_) => None,
        }
    }

    /// How the bytes were proven, when fetched or when the cache entry was installed:
    /// `"attestation"` (the release workflow's Sigstore attestation, provenance) or
    /// `"checksum"` (the release's `SHA256SUMS`, integrity). `None` for a caller-supplied
    /// binary.
    #[getter]
    fn verification(&self) -> Option<&'static str> {
        self.inner
            .verification()
            .map(|verification| verification.as_str())
    }

    /// The release version the binary was provisioned for, without a leading `v`.
    #[getter]
    fn version(&self) -> &str {
        &self.inner.version
    }

    /// The lowercase hex SHA-256 of `data`.
    #[getter]
    fn sha256(&self) -> &str {
        &self.inner.sha256
    }

    /// Everything but the bytes, which stay out so a printed report is one line.
    fn __repr__(&self) -> String {
        let verification = match self.inner.verification() {
            Some(verification) => format!("{:?}", verification.as_str()),
            None => "None".to_string(),
        };
        format!(
            "ProvisionedAgentd(source={:?}, version={:?}, verification={verification}, \
             path={:?}, size={})",
            self.inner.source.as_str(),
            self.inner.version,
            self.inner.path.display().to_string(),
            self.inner.bytes.len(),
        )
    }
}

fn provision(
    py: Python<'_>,
    version: Option<String>,
    state_dir: Option<PathBuf>,
    binary: Option<PathBuf>,
) -> PyCoreResult<Provisioned> {
    py.detach(|| {
        provision::agentd_with(&Request {
            version: version.as_deref(),
            state_dir: state_dir.as_deref(),
            binary: binary.as_deref(),
        })
    })
    .map_err(CoreError)
}

/// The `agentd` daemon binary for `version` (default: this client's own), as bytes.
///
/// Answered from `binary` or `$MICROVM_AGENTD` when either names a file, else the
/// version's cache entry under `state_dir` (default: the CLI's, so both share one cache),
/// else the GitHub release asset, verified in-process against the release workflow's
/// Sigstore attestation or, only when GitHub can't be reached for one, the release's
/// `SHA256SUMS`.
/// A fetch that cannot be verified raises `PreconditionError`, and so does any binary that
/// is not an aarch64 ELF. Neither `gh` nor `curl` is needed. Blocking: a fetch downloads a
/// few MiB and can take seconds.
#[pyfunction]
#[pyo3(signature = (version=None, state_dir=None, binary=None))]
pub fn provision_agentd<'py>(
    py: Python<'py>,
    version: Option<String>,
    state_dir: Option<PathBuf>,
    binary: Option<PathBuf>,
) -> PyCoreResult<Bound<'py, PyBytes>> {
    let provisioned = provision(py, version, state_dir, binary)?;
    Ok(PyBytes::new(py, &provisioned.bytes))
}

/// `provision_agentd`, answering with the bytes and how they got here: the source, the
/// verification, the path, the version, and the digest.
#[pyfunction]
#[pyo3(signature = (version=None, state_dir=None, binary=None))]
pub fn provision_agentd_report(
    py: Python<'_>,
    version: Option<String>,
    state_dir: Option<PathBuf>,
    binary: Option<PathBuf>,
) -> PyCoreResult<PyProvisionedAgentd> {
    Ok(PyProvisionedAgentd {
        inner: provision(py, version, state_dir, binary)?,
    })
}
