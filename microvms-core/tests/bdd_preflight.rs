// SPDX-License-Identifier: Apache-2.0
//! The Gherkin behavior spec for BIND-14, BIND-15, and BIND-16, run against the core.
//!
//! The scenarios live in `tests/features/request_and_preflight.feature`; this file is their
//! step definitions and runner, a `harness = false` test that `cargo test` runs everywhere.
//! A preflight goes through [`preflight_with`] and a scripted transport, so a scenario can
//! choose how the credential chain and the service answer, and read back every call made,
//! without an AWS account. The bindings' `preflight` is [`microvms_core::preflight::preflight`],
//! which is `preflight_with` over the real control plane.

use std::future::Future;
use std::pin::Pin;
use std::sync::{Arc, Mutex};

use cucumber::{World, cli, given, then, when};
use microvms_core::control::transport::{Call, Reply, Transport};
use microvms_core::control::{ControlPlane, SystemClock};
use microvms_core::preflight::{PreflightReport, preflight_with};
use microvms_core::{Error, ErrorKind, Region, SizeClass};

/// Scripts the credential chain and the listing, and records every operation sent.
#[derive(Debug, Default)]
struct Scripted {
    credentials_fail: bool,
    listing_denied: bool,
    calls: Mutex<Vec<String>>,
}

impl Transport for Scripted {
    fn send(&self, call: Call) -> Pin<Box<dyn Future<Output = Result<Reply, Error>> + Send + '_>> {
        self.calls
            .lock()
            .expect("not poisoned")
            .push(call.operation.to_string());
        let (status, body) = match (call.operation, self.listing_denied) {
            ("ListManagedMicrovmImages", false) => (
                200,
                r#"{"items": [{"imageArn": "arn:aws:lambda:us-east-1:aws:microvm-image:al2023",
                               "createdAt": 1750000000, "updatedAt": 1753000000}]}"#,
            ),
            ("ListManagedMicrovmImages", true) => (
                403,
                r#"{"__type": "AccessDeniedException", "message": null}"#,
            ),
            _ => (
                400,
                r#"{"message": "the scenario scripts only the listing"}"#,
            ),
        };
        Box::pin(async move {
            Ok(Reply {
                status,
                body: body.as_bytes().to_vec(),
            })
        })
    }

    fn resolve_credentials(&self) -> Pin<Box<dyn Future<Output = Result<(), Error>> + Send + '_>> {
        let fail = self.credentials_fail;
        Box::pin(async move {
            if fail {
                Err(Error::new(
                    ErrorKind::Credentials,
                    "the scenario's credential chain resolves nothing",
                ))
            } else {
                Ok(())
            }
        })
    }
}

#[derive(Debug, Default, World)]
struct Harness {
    size: Option<Result<SizeClass, Error>>,
    region: Option<Region>,
    env_region: Option<String>,
    credentials_fail: bool,
    listing_denied: bool,
    transport: Option<Arc<Scripted>>,
    report: Option<PreflightReport>,
}

fn optional<T: std::str::FromStr>(text: &str) -> Option<T> {
    (text != "unset").then(|| text.parse().ok().expect("a number or `unset`"))
}

#[when(expr = "a task requests {word} vCPU and {word} MiB")]
fn request(world: &mut Harness, cpus: String, memory: String) {
    world.size = Some(SizeClass::from_request(
        optional::<f64>(&cpus),
        optional::<u32>(&memory),
    ));
}

#[then(expr = "the size class has a {int} MiB baseline")]
fn baseline(world: &mut Harness, mib: u32) {
    match world.size.as_ref().expect("a request") {
        Ok(class) => assert_eq!(class.baseline_mib(), mib),
        Err(error) => panic!("expected {mib} MiB, got {error}"),
    }
}

#[then("the request is refused naming the largest class")]
fn refused(world: &mut Harness) {
    match world.size.as_ref().expect("a request") {
        Ok(class) => panic!("expected a refusal, got {class:?}"),
        Err(error) => {
            assert_eq!(error.kind(), ErrorKind::InvalidArg, "{error}");
            assert!(
                error.to_string().contains(&SizeClass::Mib8192.to_string()),
                "{error}"
            );
        }
    }
}

#[given(expr = "the region {word}")]
fn region(world: &mut Harness, name: String) {
    world.region = Some(name.parse().expect("a supported region"));
}

#[given(expr = "the unlisted region {word}")]
fn unlisted(world: &mut Harness, name: String) {
    world.region = Some(Region::unlisted(name));
}

#[given(expr = "the environment names the region {word}")]
fn env_region(world: &mut Harness, name: String) {
    world.env_region = Some(name);
}

#[given("credentials that resolve")]
fn credentials_resolve(world: &mut Harness) {
    world.credentials_fail = false;
}

#[given("credentials that do not resolve")]
fn credentials_fail(world: &mut Harness) {
    world.credentials_fail = true;
}

#[given("a service that answers the listing")]
fn service_answers(world: &mut Harness) {
    world.listing_denied = false;
}

#[given("a service that denies the listing")]
fn service_denies(world: &mut Harness) {
    world.listing_denied = true;
}

async fn run(world: &mut Harness, resolved: Result<Region, Error>) {
    let transport = Arc::new(Scripted {
        credentials_fail: world.credentials_fail,
        listing_denied: world.listing_denied,
        calls: Mutex::new(Vec::new()),
    });
    world.transport = Some(Arc::clone(&transport));
    let report = preflight_with(resolved, move |region| async move {
        Ok(ControlPlane::with_transport(
            transport as Arc<dyn Transport>,
            region,
            Arc::new(SystemClock::default()),
        ))
    })
    .await;
    world.report = Some(report);
}

#[when("the harness runs a preflight")]
async fn preflight(world: &mut Harness) {
    let region = world.region.clone().expect("the scenario names a region");
    run(world, Ok(region)).await;
}

#[when("the harness runs a preflight with no region")]
async fn preflight_no_region(world: &mut Harness) {
    let named = world.env_region.clone();
    let env = move |name: &str| (name == "AWS_REGION").then(|| named.clone()).flatten();
    run(world, Region::from_env(&env)).await;
}

fn report(world: &Harness) -> &PreflightReport {
    world.report.as_ref().expect("a preflight ran")
}

fn calls(world: &Harness) -> Vec<String> {
    world
        .transport
        .as_ref()
        .map(|transport| transport.calls.lock().expect("not poisoned").clone())
        .unwrap_or_default()
}

#[then("the preflight is ok")]
fn is_ok(world: &mut Harness) {
    assert!(report(world).ok(), "{:#?}", report(world));
}

#[then("the preflight is not ok")]
fn is_not_ok(world: &mut Harness) {
    assert!(!report(world).ok(), "{:#?}", report(world));
}

#[then("the checks are region, credentials, and service, all passing")]
fn all_pass(world: &mut Harness) {
    let names: Vec<&str> = report(world)
        .checks
        .iter()
        .map(|check| check.name)
        .collect();
    assert_eq!(names, ["region", "credentials", "service"]);
    assert!(report(world).checks.iter().all(|check| check.ok));
}

#[then(expr = "the {word} check failed as {word}")]
fn failed_as(world: &mut Harness, name: String, severity: String) {
    let check = report(world).check(&name).expect("the check is reported");
    assert!(!check.ok, "{check:?}");
    assert!(check.ran, "{check:?}");
    assert_eq!(check.fatal, severity == "fatal", "{check:?}");
}

#[then(expr = "the {word} check was not run")]
fn not_run(world: &mut Harness, name: String) {
    let check = report(world).check(&name).expect("the check is reported");
    assert!(!check.ran && !check.ok && check.fatal, "{check:?}");
}

#[then("the only AWS call was ListManagedMicrovmImages")]
fn only_listing(world: &mut Harness) {
    assert_eq!(calls(world), ["ListManagedMicrovmImages"]);
}

#[then("no AWS call was made")]
fn no_calls(world: &mut Harness) {
    assert_eq!(calls(world), Vec::<String>::new());
}

#[tokio::main(flavor = "current_thread")]
async fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let filters: Vec<&str> = args
        .iter()
        .map(String::as_str)
        .filter(|arg| !arg.starts_with('-'))
        .collect();
    if !filters.is_empty()
        && !filters
            .iter()
            .any(|filter| "bdd_preflight request_and_preflight".contains(filter))
    {
        return;
    }
    // Cucumber's defaults whatever the arguments: the libtest flags `cargo test` forwards are
    // not cucumber's to parse.
    Harness::cucumber()
        .fail_on_skipped()
        .with_cli(cli::Opts::<_, _, _, cli::Empty>::default())
        .run_and_exit(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/tests/features/request_and_preflight.feature"
        ))
        .await;
}
