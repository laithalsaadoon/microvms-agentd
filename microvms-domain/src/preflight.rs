// SPDX-License-Identifier: Apache-2.0
//! The lines of a `preflight` or `doctor` report, and the region line's rule (#223).
//!
//! The checks that call something (credentials, the service listing) and `preflight` itself
//! are in `microvms_core::preflight`, which also documents the outcome rules: a check whose
//! precondition failed is reported as not run ([`Check::ran`] false), fatal, and makes no
//! call, and [`PreflightReport::ok`] is true exactly when no fatal check failed or was
//! skipped. The Stateright model in `model/src/preflight.rs` specifies this (BIND-15,
//! BIND-16).

use crate::error::Error;
use crate::region::{MICROVM_REGIONS, Region};

/// One line of a preflight or `doctor` report.
#[derive(Clone, Debug, PartialEq)]
pub struct Check {
    pub name: &'static str,
    pub ok: bool,
    /// Whether a failure decides the outcome. An advisory failure is reported and ignored.
    pub fatal: bool,
    /// False when an earlier check's failure kept this one from running.
    pub ran: bool,
    pub detail: String,
    /// What to do about a failure; empty on a pass.
    pub remedy: String,
}

impl Check {
    pub fn pass(name: &'static str, detail: impl Into<String>) -> Self {
        Self {
            name,
            ok: true,
            fatal: true,
            ran: true,
            detail: detail.into(),
            remedy: String::new(),
        }
    }

    pub fn fail(name: &'static str, detail: impl Into<String>, remedy: impl Into<String>) -> Self {
        Self {
            name,
            ok: false,
            fatal: true,
            ran: true,
            detail: detail.into(),
            remedy: remedy.into(),
        }
    }

    /// A check that did not run because `reason`; it fails, fatally, and made no call.
    pub fn not_run(name: &'static str, reason: impl Into<String>) -> Self {
        Self {
            ran: false,
            ..Self::fail(
                name,
                format!("not run: {}", reason.into()),
                "fix the check above",
            )
        }
    }

    /// A non-fatal finding: reported, but it does not decide the outcome.
    #[must_use]
    pub fn advisory(mut self) -> Self {
        self.fatal = false;
        self
    }
}

/// Whether every fatal check passed.
pub fn healthy(checks: &[Check]) -> bool {
    checks
        .iter()
        .filter(|check| check.fatal)
        .all(|check| check.ok)
}

/// What `microvms_core::preflight::preflight` found.
#[derive(Clone, Debug)]
pub struct PreflightReport {
    /// The region checked, or `None` when none resolved.
    pub region: Option<Region>,
    /// `region`, `credentials`, and `service`, in that order, always all three.
    pub checks: Vec<Check>,
}

impl PreflightReport {
    /// True exactly when no fatal check failed or was skipped: a launch could proceed.
    pub fn ok(&self) -> bool {
        healthy(&self.checks)
    }

    /// The check named `name`, if the report has one.
    pub fn check(&self, name: &str) -> Option<&Check> {
        self.checks.iter().find(|check| check.name == name)
    }
}

fn known_regions() -> String {
    MICROVM_REGIONS
        .iter()
        .map(Region::as_str)
        .collect::<Vec<_>>()
        .join(", ")
}

/// The region line: pass for a supported region, advisory for an unlisted one, fatal when
/// none resolved. `doctor` marks the last advisory too, because it must run in a broken
/// environment and report everything else.
pub fn region_check(resolved: &Result<Region, Error>) -> Check {
    match resolved {
        Ok(region) if region.is_supported() => {
            Check::pass("region", format!("{region} is a known MicroVMs region"))
        }
        Ok(region) => Check::fail(
            "region",
            format!("{region} is not in this client's list of MicroVMs regions"),
            format!(
                "known: {}; the service check below says whether it runs MicroVMs",
                known_regions()
            ),
        )
        .advisory(),
        Err(error) => Check::fail(
            "region",
            error.to_string(),
            format!("known: {}", known_regions()),
        ),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_a_fatal_failure_is_unhealthy() {
        let advisory = Check::fail("region", "unlisted", "see below").advisory();
        assert!(healthy(&[Check::pass("a", "fine"), advisory.clone()]));
        assert!(!healthy(&[
            advisory,
            Check::not_run("service", "no region")
        ]));
        assert!(!Check::not_run("service", "no region").ran);
    }

    #[test]
    fn the_region_line_passes_a_listed_region_and_advises_on_an_unlisted_one() {
        assert!(region_check(&Ok(Region::UsEast1)).ok);
        let unlisted = region_check(&Ok(Region::unlisted("xx-new-1")));
        assert!(!unlisted.ok && !unlisted.fatal, "{unlisted:?}");
        let refused = region_check(&Err(Error::invalid_arg("no region")));
        assert!(!refused.ok && refused.fatal, "{refused:?}");
        assert!(refused.remedy.contains("us-east-1"), "{refused:?}");
    }
}
