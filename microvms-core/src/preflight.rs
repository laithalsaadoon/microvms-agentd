// SPDX-License-Identifier: Apache-2.0
//! `preflight`: whether a harness can launch here, checked before it queues work (#223).
//!
//! Three checks, in order, each a [`Check`] the CLI's `doctor` renders too (it shares
//! [`region_check`] and [`credentials_check`] and prints the same lines):
//!
//! 1. **region**: the region resolves, from the caller or from `$AWS_REGION` /
//!    `$AWS_DEFAULT_REGION` ([`Region::from_env`]). Fatal when it does not resolve. Advisory
//!    when it is [`Region::unlisted`]: AWS adds regions faster than a constant is re-read, and
//!    a caller who wrote `unlisted` opted in at the call site; the service check below decides.
//! 2. **credentials**: the default credential chain resolves credentials
//!    ([`ControlPlane::resolve_credentials`]). No AWS API call; the chain may read an SSO
//!    cache, run a credential process, or ask the instance metadata service.
//! 3. **service**: one `ListManagedMicrovmImages` page in that region
//!    ([`ControlPlane::answers_listing`]): the endpoint resolves, the signature and the IAM
//!    permission are accepted, and the region runs MicroVMs. An unsupported region answers
//!    `AccessDeniedException` with a null message (TRAP-6).
//!
//! A check whose precondition failed is reported as not run ([`Check::ran`] false), fatal, and
//! makes no call. [`PreflightReport::ok`] is true exactly when no fatal check failed or was
//! skipped. The Stateright model in `model/src/preflight.rs` specifies this (BIND-15, BIND-16).
//!
//! # Nothing billable, and what is not checked
//!
//! The one AWS operation is a free read (`send_with_retry` may repeat it on a throttle); no
//! call creates, changes, or bills anything. The Python harvester's check that boto3 knows the
//! `lambda-microvms` service has no counterpart: this client is Rust and speaks the service
//! model it was built against ([`crate::constants::MODEL_API_VERSION`]), so the listing is the
//! equivalent evidence that the endpoint accepts that API version. A harness that also calls
//! the service through boto3 checks its own boto3. Preflight does not check the execution or
//! build role, the S3 artifact bucket, image quotas, or VPC connectors: each needs a call this
//! module does not make, and `doctor` reports the infrastructure it can see.

use std::future::Future;

use crate::control::ControlPlane;
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

/// What [`preflight`] found.
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

/// The credentials line, from a control plane that may not have been built.
pub async fn credentials_check(plane: &Result<ControlPlane, Error>, region: &Region) -> Check {
    let failure = match plane {
        Ok(plane) => plane
            .resolve_credentials()
            .await
            .err()
            .map(|e| e.to_string()),
        Err(error) => Some(error.to_string()),
    };
    match failure {
        None => Check::pass(
            "credentials",
            format!("the default chain resolved credentials for {region}"),
        ),
        Some(detail) => Check::fail(
            "credentials",
            detail,
            "`aws sso login`, or set AWS_PROFILE / AWS_ACCESS_KEY_ID",
        ),
    }
}

/// The service line: one free, read-only listing page.
pub async fn service_check(plane: &ControlPlane) -> Check {
    let region = plane.region();
    match plane.answers_listing().await {
        Ok(bases) => Check::pass(
            "service",
            format!(
                "ListManagedMicrovmImages answered in {region} ({bases} managed base(s)); API \
                 version {}",
                crate::constants::MODEL_API_VERSION
            ),
        ),
        Err(error) => Check::fail(
            "service",
            format!("ListManagedMicrovmImages in {region} failed: {error}"),
            "an AccessDeniedException with no message is also what a region without MicroVMs \
             answers; otherwise allow lambda-microvms:ListManagedMicrovmImages",
        ),
    }
}

/// Runs the three checks for `region`, or for the region the environment names.
///
/// The bindings' `preflight`. See the module docs for what it checks, what it does not, and
/// why nothing it does bills.
pub async fn preflight(region: Option<Region>) -> PreflightReport {
    let resolved = match region {
        Some(region) => Ok(region),
        None => Region::from_env(&crate::env::process),
    };
    preflight_with(resolved, ControlPlane::new).await
}

/// [`preflight`] over a resolved region and a control-plane constructor, for a caller (and a
/// test) that supplies the transport. `connect` is not called when no region resolved.
pub async fn preflight_with<F, Fut>(resolved: Result<Region, Error>, connect: F) -> PreflightReport
where
    F: FnOnce(Region) -> Fut,
    Fut: Future<Output = Result<ControlPlane, Error>>,
{
    let region_line = region_check(&resolved);
    let Ok(region) = resolved else {
        return PreflightReport {
            region: None,
            checks: vec![
                region_line,
                Check::not_run("credentials", "no region resolved"),
                Check::not_run("service", "no region resolved"),
            ],
        };
    };
    let plane = connect(region.clone()).await;
    let credentials = credentials_check(&plane, &region).await;
    let service = match (&plane, credentials.ok) {
        (Ok(plane), true) => service_check(plane).await,
        _ => Check::not_run("service", "credentials did not resolve"),
    };
    PreflightReport {
        region: Some(region),
        checks: vec![region_line, credentials, service],
    }
}

#[cfg(test)]
mod tests {
    use std::sync::{Arc, Mutex};

    use super::*;
    use crate::control::fake::{Answer, FakeControlPlane, TestClock};
    use crate::control::transport::{Call, Reply, Transport};
    use crate::error::ErrorKind;

    /// A recorder whose credential chain fails.
    struct NoCredentials(Mutex<Vec<String>>);

    impl Transport for NoCredentials {
        fn send(
            &self,
            call: Call,
        ) -> std::pin::Pin<Box<dyn Future<Output = Result<Reply, Error>> + Send + '_>> {
            self.0
                .lock()
                .expect("not poisoned")
                .push(call.operation.into());
            Box::pin(async { panic!("no call may follow a failed credential chain") })
        }

        fn resolve_credentials(
            &self,
        ) -> std::pin::Pin<Box<dyn Future<Output = Result<(), Error>> + Send + '_>> {
            Box::pin(async { Err(Error::new(ErrorKind::Credentials, "nothing in the chain")) })
        }
    }

    fn plane(transport: Arc<dyn Transport>, region: Region) -> ControlPlane {
        ControlPlane::with_transport(transport, region, Arc::new(TestClock::new()))
    }

    /// **BIND-15, the model's table**: `ok` is true exactly when no line blocks, and an
    /// unlisted region's advisory line does not.
    #[tokio::test]
    async fn the_outcome_is_the_models_table() {
        for (region, listing, ok) in [
            (Region::UsEast1, Answer::ok(r#"{"items": []}"#), true),
            (
                Region::unlisted("ap-south-1"),
                Answer::ok(r#"{"items": []}"#),
                true,
            ),
            (
                Region::unlisted("ap-south-1"),
                Answer::failure(403, "AccessDeniedException"),
                false,
            ),
        ] {
            let recorder = Arc::new(FakeControlPlane::new());
            recorder.answer("ListManagedMicrovmImages", listing);
            let transport = Arc::clone(&recorder) as Arc<dyn Transport>;
            let report = preflight_with(
                Ok(region.clone()),
                |r| async move { Ok(plane(transport, r)) },
            )
            .await;
            assert_eq!(report.ok(), ok, "{region}: {report:#?}");
            assert_eq!(
                recorder.operations(),
                ["ListManagedMicrovmImages"],
                "BIND-16: one free read"
            );
            assert_eq!(report.checks.len(), 3);
        }
    }

    /// **BIND-16: no call after the credentials fail**, and the service line says it did not
    /// run rather than passing or vanishing.
    ///
    /// **Falsification** — 2026-09-24. Run `service_check` whatever the credentials line says
    /// and `NoCredentials::send` panics; restored.
    #[tokio::test]
    async fn failed_credentials_stop_the_preflight_before_any_call() {
        let transport = Arc::new(NoCredentials(Mutex::new(Vec::new())));
        let shared = Arc::clone(&transport) as Arc<dyn Transport>;
        let report =
            preflight_with(Ok(Region::UsEast1), |r| async move { Ok(plane(shared, r)) }).await;
        assert!(!report.ok());
        let credentials = report.check("credentials").expect("reported");
        assert!(!credentials.ok && credentials.ran && credentials.fatal);
        let service = report.check("service").expect("reported");
        assert!(!service.ok && !service.ran && service.fatal, "{service:?}");
        assert!(transport.0.lock().expect("not poisoned").is_empty());
    }

    /// **BIND-16: no region, no control plane, no call.**
    #[tokio::test]
    async fn an_unresolved_region_builds_no_control_plane() {
        let refused =
            Region::from_env(&|name| (name == "AWS_REGION").then(|| "eu-central-1".into()));
        let report = preflight_with(refused, |_| async {
            panic!("no control plane may be built without a region")
        })
        .await;
        assert!(!report.ok());
        assert_eq!(report.region, None);
        assert!(
            report
                .check("region")
                .is_some_and(|check| check.fatal && !check.ok)
        );
        for name in ["credentials", "service"] {
            assert!(report.check(name).is_some_and(|check| !check.ran), "{name}");
        }
    }

    /// A control plane that could not be built is a failed credentials line, and the service
    /// is not asked.
    #[tokio::test]
    async fn a_control_plane_that_fails_to_build_is_a_credentials_failure() {
        let report = preflight_with(Ok(Region::UsEast1), |_| async {
            Err(Error::new(ErrorKind::Credentials, "no provider"))
        })
        .await;
        assert!(!report.ok());
        assert!(
            report
                .check("credentials")
                .is_some_and(|check| check.ran && !check.ok)
        );
        assert!(report.check("service").is_some_and(|check| !check.ran));
    }

    /// The region line's three cases, which `doctor` shares.
    #[test]
    fn the_region_line_is_pass_advisory_or_fatal() {
        assert!(region_check(&Ok(Region::UsEast1)).ok);
        let unlisted = region_check(&Ok(Region::unlisted("ap-south-1")));
        assert!(!unlisted.ok && !unlisted.fatal);
        let refused = region_check(&Err(Error::invalid_arg("nope")));
        assert!(!refused.ok && refused.fatal);
    }
}
