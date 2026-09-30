// SPDX-License-Identifier: Apache-2.0
//! `preflight`: core's preflight, as Node sees it (BIND-15, BIND-16).
//!
//! A pass-through: the checks, their order, their words, and the one free call are core's
//! (`microvms_core::preflight`), which the CLI's `doctor` shares.

use microvms_core::preflight::{Check, PreflightReport as CoreReport};
use napi_derive::napi;

use crate::region::Region;

/// One line of a preflight report.
#[napi(object)]
pub struct PreflightCheck {
    /// `"region"`, `"credentials"`, or `"service"`.
    pub name: String,
    pub ok: bool,
    /// Whether a failure decides the report's `ok`. An unlisted region's line is advisory.
    pub fatal: bool,
    /// False when an earlier check's failure kept this one from running (and calling AWS).
    pub ran: bool,
    pub detail: String,
    /// What to do about a failure; empty on a pass.
    pub remedy: String,
}

/// What `preflight` found: three checks and whether a launch could proceed.
#[napi(object)]
pub struct PreflightReport {
    /// True exactly when no fatal check failed or was skipped.
    pub ok: bool,
    /// The region checked, absent when none resolved.
    pub region: Option<String>,
    /// `region`, `credentials`, and `service`, in that order.
    pub checks: Vec<PreflightCheck>,
}

fn check(check: &Check) -> PreflightCheck {
    PreflightCheck {
        name: check.name.to_string(),
        ok: check.ok,
        fatal: check.fatal,
        ran: check.ran,
        detail: check.detail.clone(),
        remedy: check.remedy.clone(),
    }
}

fn report(report: &CoreReport) -> PreflightReport {
    PreflightReport {
        ok: report.ok(),
        region: report
            .region
            .as_ref()
            .map(|region| region.as_str().to_string()),
        checks: report.checks.iter().map(check).collect(),
    }
}

/// Whether a harness can launch in `region` (default: `$AWS_REGION`, `$AWS_DEFAULT_REGION`,
/// then us-east-1), checked before it queues work.
///
/// Three checks: the region resolves (advisory when `Region.unlisted`), the credential chain
/// resolves credentials (no AWS call), and one `ListManagedMicrovmImages` page answers in that
/// region, the only AWS operation, free and read-only. A check after a failure is reported
/// with `ran: false` and makes no call. Nothing billable, nothing mutating. It does not check
/// roles, the artifact bucket, quotas, or VPC connectors. Never rejects; read `report.ok`.
#[napi]
pub async fn preflight(region: Option<&Region>) -> PreflightReport {
    let region = region.map(|region| region.inner.clone());
    report(&microvms_core::preflight::preflight(region).await)
}
