// SPDX-License-Identifier: Apache-2.0
//! `microvm adopt`: core's `Sandbox::adopt` over the connection flags (#269).

#![cfg(test)]

use std::sync::Arc;

use microvms_core::testing::YieldingClock;

use super::support::{ScriptedSeam, ScriptedTransport, dispatch_with, full_infra, region_flags};
use crate::cli::{AdoptArgs, AttachFlags, Command};
use crate::exit::Exit;

/// The endpoint the scripted VM reports.
const ENDPOINT: &str = "https://mvm-abc123.microvm.us-east-1.amazonaws.com";

/// `GetMicrovmResponse` for a VM in `state`, with the idle policy the service reports.
fn microvm_with_policy(state: &str) -> String {
    format!(
        r#"{{"microvmId": "mvm-abc123", "state": "{state}", "stateReason": "idle",
             "endpoint": "{ENDPOINT}",
             "imageArn": "arn:aws:lambda:us-east-1:123456789012:microvm-image:img",
             "imageVersion": "1", "maximumDurationInSeconds": 3600, "startedAt": 1754524800,
             "idlePolicy": {{"maxIdleDurationSeconds": 900, "suspendedDurationSeconds": 600,
                             "autoResumeEnabled": false}}}}"#
    )
}

fn adopt_command(endpoint: &str) -> Command {
    Command::Adopt(AdoptArgs {
        attach: AttachFlags {
            endpoint: Some(endpoint.into()),
            agent_token: Some("t".into()),
            microvm_id: Some("mvm-abc123".into()),
            name: None,
            port: None,
            state_dir: None,
        },
        region: region_flags(),
    })
}

/// **`adopt` reports what core's adopted sandbox read from the service (#269):** the
/// lifecycle, the endpoint and image, the state reason, and the idle window from the VM's
/// own idle policy, through one `GetMicrovm` and nothing else.
///
/// **Falsification**: `verify/guards/faults/adopt-command.toml` entry
/// `cli-adopt-reports-the-flags` (the envelope echoes the triple's endpoint and a RUNNING
/// guess instead of the sandbox's reading).
#[tokio::test]
async fn adopt_reports_what_cores_sandbox_read() {
    let transport = Arc::new(ScriptedTransport::new());
    transport.answer("GetMicrovm", 200, &microvm_with_policy("SUSPENDED"));
    let seam = ScriptedSeam {
        transport: Arc::clone(&transport),
        clock: Arc::new(YieldingClock::default()),
    };
    let (result, _) = dispatch_with(&seam, &adopt_command(ENDPOINT), full_infra()).await;
    let adopted = result.expect("the triple agrees with the service");
    assert_eq!(adopted.kind, "microvm.adopt");
    assert_eq!(
        (&adopted.data["state"], &adopted.data["idleWindowSec"]),
        (&"SUSPENDED".into(), &900.into()),
        "the service's lifecycle and idle policy, as the sandbox holds them"
    );
    assert_eq!(adopted.data["microvmId"], "mvm-abc123");
    assert_eq!(adopted.data["endpoint"], ENDPOINT);
    assert_eq!(adopted.data["stateReason"], "idle");
    assert_eq!(transport.calls(), ["GetMicrovm"]);
}

/// **`adopt` refuses a triple whose endpoint isn't the one the service reports for its id
/// (#269),** with core's ERR_INVALID_ARG, which is the guard an attached command doesn't run.
///
/// **Falsification**: `verify/guards/faults/adopt-command.toml` entry
/// `cli-adopt-passes-no-endpoint` (the triple's endpoint never reaches core, so the mismatch
/// adopts).
#[tokio::test]
async fn adopt_refuses_a_triple_whose_endpoint_disagrees_with_the_service() {
    let transport = Arc::new(ScriptedTransport::new());
    transport.answer("GetMicrovm", 200, &microvm_with_policy("RUNNING"));
    let seam = ScriptedSeam {
        transport: Arc::clone(&transport),
        clock: Arc::new(YieldingClock::default()),
    };
    let (result, _) = dispatch_with(
        &seam,
        &adopt_command("https://mvm-other.microvm.us-east-1.amazonaws.com"),
        full_infra(),
    )
    .await;
    let failure = result.expect_err("the id and endpoint came from different records");
    assert_eq!(failure.exit, Exit::InvalidArg, "{}", failure.message);
    assert!(
        failure.message.contains("came from different records"),
        "core's refusal: {}",
        failure.message
    );
    assert_eq!(transport.calls(), ["GetMicrovm"]);
}
