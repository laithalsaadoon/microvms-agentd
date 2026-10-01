// SPDX-License-Identifier: Apache-2.0
//! Directory transfer's answers: what `Session.download_dir` wrote and what `Session.sync_dir`
//! did. The work is core's `microvms_core::workspace`; these are its results as Python sees
//! them.

use microvms_core::workspace::{Artifact, SyncPass};
use pyo3::prelude::*;

/// One file `Session.download_dir` wrote, relative to the local directory.
#[pyclass(frozen, name = "DownloadedFile", module = "microvms")]
pub struct PyDownloadedFile {
    path: String,
    size: u64,
}

impl From<Artifact> for PyDownloadedFile {
    fn from(artifact: Artifact) -> Self {
        Self {
            path: artifact.path,
            size: artifact.bytes,
        }
    }
}

#[pymethods]
impl PyDownloadedFile {
    /// The file's path under the local directory, as the archive named it.
    #[getter]
    fn path(&self) -> &str {
        &self.path
    }

    /// The file's size in bytes.
    #[getter]
    fn size(&self) -> u64 {
        self.size
    }

    fn __repr__(&self) -> String {
        format!("DownloadedFile(path={:?}, size={})", self.path, self.size)
    }
}

/// What one `Session.sync_dir` did.
#[pyclass(frozen, name = "SyncReport", module = "microvms")]
pub struct PySyncReport {
    uploaded_bytes: usize,
    uploaded_members: usize,
    deleted: usize,
    refused_deletions: usize,
    full: bool,
    unchanged: bool,
}

impl From<SyncPass> for PySyncReport {
    fn from(pass: SyncPass) -> Self {
        Self {
            uploaded_bytes: pass.uploaded_bytes,
            uploaded_members: pass.uploaded_members,
            deleted: pass.deleted,
            refused_deletions: pass.refused_deletions,
            full: pass.full,
            unchanged: pass.unchanged,
        }
    }
}

#[pymethods]
impl PySyncReport {
    /// The uploaded archive's size, 0 when nothing travelled.
    #[getter]
    fn uploaded_bytes(&self) -> usize {
        self.uploaded_bytes
    }

    /// How many members the upload carried: the changed ones, or every one when `full`.
    #[getter]
    fn uploaded_members(&self) -> usize {
        self.uploaded_members
    }

    /// How many paths gone locally were removed in the VM.
    #[getter]
    fn deleted(&self) -> usize {
        self.deleted
    }

    /// How many deletions the VM's manifest ordered that weren't plain relative paths, and so
    /// weren't run: the manifest is the VM's word.
    #[getter]
    fn refused_deletions(&self) -> usize {
        self.refused_deletions
    }

    /// No manifest was read (`full=True`, or none in the VM), so the whole tree travelled.
    #[getter]
    fn full(&self) -> bool {
        self.full
    }

    /// The VM already held the tree as it is, so nothing travelled.
    #[getter]
    fn unchanged(&self) -> bool {
        self.unchanged
    }

    fn __repr__(&self) -> String {
        format!(
            "SyncReport(uploaded_members={}, uploaded_bytes={}, deleted={}, full={}, \
             unchanged={})",
            self.uploaded_members,
            self.uploaded_bytes,
            self.deleted,
            if self.full { "True" } else { "False" },
            if self.unchanged { "True" } else { "False" },
        )
    }
}
