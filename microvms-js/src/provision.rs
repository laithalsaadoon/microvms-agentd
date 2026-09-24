// SPDX-License-Identifier: Apache-2.0
//! Daemon provisioning: the `agentd` binary for the version this client drives.
//!
//! A thin wrapper over `microvms_core::provision` (BIND-17 through BIND-20). The chain,
//! the verification, the cache, and every refusal are core's; this file converts the
//! options object and runs the blocking call off the event loop, since a fetch runs `gh` or
//! `curl` and can take seconds.

use std::path::PathBuf;

use microvms_core::provision::{self, Provisioned, Request, Source};
use napi::bindgen_prelude::Buffer;
use napi_derive::napi;

use crate::errors::{AsyncError, js_async};

/// What to provision. Every field is optional.
#[napi(object)]
#[derive(Default)]
pub struct ProvisionOptions {
    /// The release version, with or without a leading `v`. Defaults to `coreVersion()`.
    pub version: Option<String>,
    /// The state directory the cache lives under. Defaults to the CLI's
    /// (`$MICROVM_STATE_DIR`, else `~/.microvm/runs`), so every surface shares one cache.
    pub state_dir: Option<String>,
    /// A binary the caller manages. Outranks `$MICROVM_AGENTD`; must be an aarch64 ELF.
    pub binary: Option<String>,
}

/// A provisioned `agentd` binary and how it got here: `provisionAgentdReport()`'s answer.
#[napi(object)]
pub struct ProvisionedAgentd {
    /// The binary itself, an aarch64 ELF.
    pub data: Buffer,
    /// Where the binary is on disk: the cache entry, or the caller's own path.
    pub path: String,
    /// `"caller-supplied"`, `"cache"`, or `"fetched"`.
    pub source: String,
    /// For a caller-supplied binary, `"argument"` (the `binary` option) or `"env"`
    /// (`$MICROVM_AGENTD`).
    pub supplied_by: Option<String>,
    /// `"attestation"` (`gh attestation verify`, provenance) or `"checksum"` (the release's
    /// `SHA256SUMS`, integrity), when fetched or when the cache entry was installed. Absent
    /// for a caller-supplied binary.
    pub verification: Option<String>,
    /// The release version provisioned for, without a leading `v`.
    pub version: String,
    /// The lowercase hex SHA-256 of `data`.
    pub sha256: String,
}

async fn provisioned(options: Option<ProvisionOptions>) -> Result<Provisioned, AsyncError> {
    let options = options.unwrap_or_default();
    let outcome = tokio::task::spawn_blocking(move || {
        let state_dir = options.state_dir.map(PathBuf::from);
        let binary = options.binary.map(PathBuf::from);
        provision::agentd_with(&Request {
            version: options.version.as_deref(),
            state_dir: state_dir.as_deref(),
            binary: binary.as_deref(),
        })
    })
    .await
    .map_err(|joined| {
        js_async(microvms_core::Error::new(
            microvms_core::ErrorKind::Unexpected,
            format!("the provisioning task did not complete: {joined}"),
        ))
    })?;
    outcome.map_err(js_async)
}

/// The `agentd` daemon binary for `options.version` (default: this client's own).
///
/// Answered from `options.binary` or `$MICROVM_AGENTD` when either names a file, else the
/// version's cache entry, else the GitHub release asset, verified by `gh attestation
/// verify` or, when `gh` cannot download, by the release's `SHA256SUMS`. A fetch that
/// cannot be verified rejects with `ERR_PRECONDITION` (on `err.cause.message`), and so does
/// any binary that is not an aarch64 ELF.
#[napi]
pub async fn provision_agentd(options: Option<ProvisionOptions>) -> Result<Buffer, AsyncError> {
    Ok(provisioned(options).await?.bytes.into())
}

/// `provisionAgentd`, answering with the bytes and how they got here.
#[napi]
pub async fn provision_agentd_report(
    options: Option<ProvisionOptions>,
) -> Result<ProvisionedAgentd, AsyncError> {
    let provisioned = provisioned(options).await?;
    Ok(ProvisionedAgentd {
        path: provisioned.path.display().to_string(),
        source: provisioned.source.as_str().to_string(),
        supplied_by: match provisioned.source {
            Source::CallerSupplied(supplier) => Some(supplier.as_str().to_string()),
            Source::Cache(_) | Source::Fetched(_) => None,
        },
        verification: provisioned
            .verification()
            .map(|verification| verification.as_str().to_string()),
        version: provisioned.version,
        sha256: provisioned.sha256,
        data: provisioned.bytes.into(),
    })
}
