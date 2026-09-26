// SPDX-License-Identifier: Apache-2.0
//! The Gherkin behavior spec for BIND-11, BIND-12, and BIND-13, run against the core.
//!
//! The scenarios live in `tests/features/egress_posture.feature`, tagged with the requirement
//! each one verifies; this file is their step definitions and runner. It is a `harness = false`
//! test, so `cargo test` runs it on every CI system.
//!
//! The launches go through `ControlPlane::with_transport` and a scripted transport that
//! answers a launch to RUNNING, so a scenario can assert what a launched session reports
//! without an AWS account. The bindings expose exactly these core values
//! (`microvms-cli/tests/thinness.rs` keeps them thin), and `microvms-cli/src/guards.rs` holds
//! the parity guard against the CLI envelope.

use std::sync::Arc;

use cucumber::{World, cli, given, then, when};
use microvms_app::testing::{Answer, FakeControlPlane};
use microvms_core::control::transport::Transport;
use microvms_core::control::{ControlPlane, EgressPosture, SystemClock, egress_posture_for};
use microvms_core::prelude::*;
use microvms_core::sandbox::{RunRequest, Sandbox};
use microvms_core::session::Session;
use microvms_core::{Error, ErrorKind, Region};

const ENDPOINT: &str = "https://mvm-abc123.microvm.us-east-1.amazonaws.com";

/// A connector ARN in us-east-1, the region every scenario launches in.
fn connector(index: usize) -> String {
    format!("arn:aws:lambda:us-east-1:123456789012:network-connector:private-{index}")
}

fn microvm_body(state: &str) -> String {
    format!(
        r#"{{"microvmId": "mvm-abc123", "state": "{state}", "endpoint": "{ENDPOINT}",
             "imageArn": "arn:aws:lambda:us-east-1:123456789012:microvm-image:img",
             "imageVersion": "1",
             "idlePolicy": {{"maxIdleDurationSeconds": 600, "suspendedDurationSeconds": 600,
                             "autoResumeEnabled": false}},
             "maximumDurationInSeconds": 3600, "startedAt": 1754524800}}"#
    )
}

/// A control plane that answers a launch to RUNNING and records every operation.
fn scripted() -> Arc<FakeControlPlane> {
    let fake = Arc::new(FakeControlPlane::new());
    fake.answer("RunMicrovm", Answer::ok(microvm_body("PENDING")))
        .answer("GetMicrovm", Answer::ok(microvm_body("RUNNING")))
        .answer(
            "CreateMicrovmAuthToken",
            Answer::ok(r#"{"authToken": {"X-aws-proxy-auth": "proxy"}}"#),
        )
        .answer("TerminateMicrovm", Answer::ok(microvm_body("TERMINATING")));
    fake
}

#[derive(Debug, World)]
#[world(init = Self::new)]
struct Harness {
    egress: bool,
    connectors: Vec<String>,
    deny: bool,
    region: Option<Region>,
    transport: Arc<FakeControlPlane>,
    answer: Option<Result<EgressPosture, Error>>,
    session: Option<EgressPosture>,
    adopted: Option<EgressPosture>,
}

impl Harness {
    fn new() -> Self {
        Self {
            egress: false,
            connectors: Vec::new(),
            deny: false,
            region: None,
            transport: scripted(),
            answer: None,
            session: None,
            adopted: None,
        }
    }

    fn plane(&self) -> ControlPlane {
        ControlPlane::with_transport(
            Arc::clone(&self.transport) as Arc<dyn Transport>,
            Region::UsEast1,
            Arc::new(SystemClock::default()),
        )
    }

    fn request(&self) -> RunRequest {
        let mut request = RunRequest::new().with_image("arn:image");
        request.egress = self.egress;
        request.egress_network_connectors = self.connectors.clone();
        request.deny_egress = self.deny;
        request
    }

    fn calls(&self) -> Vec<String> {
        self.transport
            .operations()
            .into_iter()
            .map(String::from)
            .collect()
    }
}

#[given(expr = "launch options with egress {word}, {int} VPC connectors, and deny {word}")]
fn options(world: &mut Harness, egress: String, connectors: usize, deny: String) {
    world.egress = egress == "true";
    world.connectors = (0..connectors).map(connector).collect();
    world.deny = deny == "true";
}

#[given(expr = "launch options with one VPC connector {string}")]
fn one_connector(world: &mut Harness, arn: String) {
    world.connectors = vec![arn];
}

#[when("the harness asks for their egress posture")]
fn ask(world: &mut Harness) {
    world.answer = Some(egress_posture_for(
        world.egress,
        &world.connectors,
        world.deny,
        world.region.as_ref(),
    ));
}

#[when(expr = "the harness asks for their egress posture in {word}")]
fn ask_in(world: &mut Harness, region: String) {
    world.region = Some(Region::unlisted(region));
    ask(world);
}

#[when("the sandbox launches them")]
async fn launch(world: &mut Harness) {
    let mut sandbox = Sandbox::with_control_plane(world.plane());
    let session = sandbox
        .run(world.request())
        .await
        .expect("the scripted launch reaches RUNNING");
    world.session = Some(session.egress_posture());
    // Nothing billable exists; detaching keeps the drop-time leak warning quiet.
    sandbox.detach().expect("hand the scripted VM off quietly");
}

#[when("the harness attaches a session directly")]
fn attach_directly(world: &mut Harness) {
    let session = Session::direct(ENDPOINT, "agent-token").expect("a direct session");
    world.session = Some(session.egress_posture());
}

#[when("another process adopts the VM")]
async fn adopt(world: &mut Harness) {
    let adopted = Sandbox::adopt(world.plane(), "mvm-abc123", ENDPOINT, "agent-token")
        .await
        .expect("the scripted VM is RUNNING");
    world.adopted = Some(
        adopted
            .session()
            .expect("an adopted RUNNING VM has a session")
            .egress_posture(),
    );
    drop(adopted);
}

fn answer(world: &Harness) -> &Result<EgressPosture, Error> {
    world
        .answer
        .as_ref()
        .expect("the scenario asked for the posture")
}

#[then(expr = "the answer is {string}")]
fn answer_is(world: &mut Harness, expected: String) {
    match answer(world) {
        Ok(posture) => assert_eq!(posture.as_str(), expected),
        Err(error) => panic!("expected {expected}, got the refusal {error}"),
    }
}

#[then(expr = "the answer is not {string}")]
fn answer_is_not(world: &mut Harness, refused: String) {
    let posture = answer(world).as_ref().expect("a posture");
    assert_ne!(posture.as_str(), refused);
    assert!(!posture.is_sealed(), "{posture} must not read as a seal");
}

#[then(expr = "the answer is an invalid-argument refusal mentioning {string}")]
fn answer_refused(world: &mut Harness, needle: String) {
    match answer(world) {
        Ok(posture) => panic!("expected a refusal mentioning {needle:?}, got {posture}"),
        Err(error) => {
            assert_eq!(error.kind(), ErrorKind::InvalidArg, "{error}");
            assert!(error.to_string().contains(&needle), "{error}");
        }
    }
}

#[then("the launch refuses the same options with the same message")]
async fn launch_refuses(world: &mut Harness) {
    let expected = answer(world)
        .as_ref()
        .expect_err("the answer was a refusal")
        .to_string();
    let mut sandbox = Sandbox::with_control_plane(world.plane());
    let error = sandbox
        .run(world.request())
        .await
        .expect_err("the launch refuses what the answer refused");
    assert_eq!(error.kind(), ErrorKind::InvalidArg, "{error}");
    assert_eq!(error.to_string(), expected);
}

#[then("no AWS call was made")]
fn no_calls(world: &mut Harness) {
    assert_eq!(world.calls(), Vec::<String>::new());
}

#[then(expr = "the session reports {string}")]
fn session_reports(world: &mut Harness, expected: String) {
    let posture = world.session.expect("a session was built");
    assert_eq!(posture.as_str(), expected);
}

#[then("the session's posture equals the answer")]
fn session_equals_answer(world: &mut Harness) {
    let answered = *answer(world).as_ref().expect("a posture");
    assert_eq!(world.session, Some(answered));
}

#[then(expr = "the adopted session reports {string}")]
fn adopted_reports(world: &mut Harness, expected: String) {
    let posture = world.adopted.expect("the VM was adopted");
    assert_eq!(posture.as_str(), expected);
}

#[tokio::main(flavor = "current_thread")]
async fn main() {
    // `cargo test <filter>` passes a libtest filter to every test target; one naming something
    // else selects nothing here, as libtest would.
    let args: Vec<String> = std::env::args().skip(1).collect();
    let filters: Vec<&str> = args
        .iter()
        .map(String::as_str)
        .filter(|arg| !arg.starts_with('-'))
        .collect();
    if !filters.is_empty()
        && !filters
            .iter()
            .any(|filter| "bdd_posture egress_posture".contains(filter))
    {
        return;
    }
    // The options are cucumber's defaults whatever the arguments: the libtest flags `cargo
    // test` forwards (`--nocapture`, `--ignored`, ...) are not cucumber's to parse.
    Harness::cucumber()
        .fail_on_skipped()
        .with_cli(cli::Opts::<_, _, _, cli::Empty>::default())
        .run_and_exit(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/tests/features/egress_posture.feature"
        ))
        .await;
}
