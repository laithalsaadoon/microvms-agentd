// SPDX-License-Identifier: Apache-2.0
//! BIND-15 and BIND-16 live tests, invoked by `conformance/run_rs.py` (`drive_preflight`).
//!
//! `preflight` is what both bindings' `preflight` call. Three runs against the real account:
//! one in the suite's region, which must pass and leave the account's VMs and images as they
//! were; one in a region without MicroVMs, whose service check must fail; and one the driver
//! runs with every credential source removed from the environment, which must stop at the
//! credentials check. Each prints its report on stderr as `PREFLIGHT <name>=<ok|fail>` lines.
//! None launches or builds anything.

use microvms_core::control::ControlPlane;
use microvms_core::preflight::{PreflightReport, preflight};
use microvms_core::region::Region;

fn region() -> Region {
    std::env::var("AWS_REGION")
        .unwrap_or_else(|_| "us-east-1".into())
        .parse()
        .expect("a supported region")
}

fn print(label: &str, report: &PreflightReport) {
    for check in &report.checks {
        eprintln!(
            "PREFLIGHT {label} {}={} ran={} fatal={} detail={}",
            check.name,
            if check.ok { "ok" } else { "fail" },
            check.ran,
            check.fatal,
            check.detail
        );
    }
    eprintln!("PREFLIGHT {label} ok={}", report.ok());
}

/// **BIND-15 and BIND-16 in the suite's region.** Every check passes, and the account's VM and
/// image listings are unchanged across it. The conformance driver runs this under the live
/// lock, so no other suite launches in between.
#[tokio::test]
#[ignore = "needs AWS credentials; makes one free read-only call"]
async fn preflight_passes_in_the_suites_region_and_changes_nothing() {
    let plane = ControlPlane::new(region()).await.expect("credentials");
    let before = (
        plane.list_microvms().await.expect("ListMicrovms").len(),
        plane.list_images().await.expect("ListMicrovmImages").len(),
    );
    let report = preflight(Some(region())).await;
    print("suite", &report);
    let after = (
        plane.list_microvms().await.expect("ListMicrovms").len(),
        plane.list_images().await.expect("ListMicrovmImages").len(),
    );
    eprintln!("PREFLIGHT suite vms+images before={before:?} after={after:?}");
    assert!(report.ok(), "{report:#?}");
    assert_eq!(before, after, "a preflight must create nothing");
}

/// **BIND-15: an unlisted region that does not run MicroVMs fails the service check**, while
/// its region line is only advisory. ca-central-1 was not among the regions that answered
/// `ListMicrovms` when `MICROVM_REGIONS` was measured.
#[tokio::test]
#[ignore = "needs AWS credentials; makes one free read-only call in ca-central-1"]
async fn preflight_in_a_region_without_microvms_fails_its_service_check() {
    let report = preflight(Some(Region::unlisted("ca-central-1"))).await;
    print("elsewhere", &report);
    let region = report.check("region").expect("a region line");
    assert!(!region.ok && !region.fatal, "{region:?}");
    assert!(report.check("credentials").is_some_and(|check| check.ok));
    let service = report.check("service").expect("a service line");
    assert!(service.ran && !service.ok && service.fatal, "{service:?}");
    assert!(!report.ok());
}

/// **BIND-16: no credentials, no call.** The driver runs this with every credential source
/// removed (`AWS_*` unset, `HOME` empty, instance metadata disabled); the report stops at the
/// credentials line and never sends the listing.
#[tokio::test]
#[ignore = "run by the conformance driver with every credential source removed"]
async fn preflight_without_credentials_makes_no_call() {
    let report = preflight(Some(Region::UsEast1)).await;
    print("nocreds", &report);
    let credentials = report.check("credentials").expect("a credentials line");
    assert!(credentials.ran && !credentials.ok, "{credentials:?}");
    let service = report.check("service").expect("a service line");
    assert!(!service.ran, "{service:?}");
    assert!(!report.ok());
}
