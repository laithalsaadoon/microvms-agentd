// SPDX-License-Identifier: Apache-2.0
//! BIND-12 live test, invoked by `conformance/run_rs.py` (`drive_posture_parity`) with its
//! existing image. Never discovers or builds images.
//!
//! Launches through [`Sandbox::run`], the call both bindings' `Sandbox.run` wrap, once with no
//! network options and once with managed egress, and prints each session's posture on stderr
//! as `POSTURE <launch>=<label>`; the driver compares them with the CLI envelope's
//! `egressPosture` for the same options. A second process's view is printed too: adopting the
//! egress VM without its launch options reports `unsealed`. A 600-second VM lifetime bounds an
//! interrupted test; cleanup terminates and observes TERMINATED through `GetMicrovm`.

use std::time::Duration;

use microvms_core::control::{ControlPlane, EgressPosture, WaitOpts, egress_posture_for, token};
use microvms_core::region::Region;
use microvms_core::sandbox::{RunRequest, Sandbox, TeardownOpts};

fn env(name: &str) -> String {
    std::env::var(name).unwrap_or_else(|_| panic!("conformance must supply {name}"))
}

fn region() -> Region {
    std::env::var("AWS_REGION")
        .unwrap_or_else(|_| "us-east-1".into())
        .parse()
        .expect("a supported region")
}

/// Launches `request`, reports the session's posture and an adopter's, and tears down.
async fn launch(label: &str, request: RunRequest, failures: &mut Vec<String>) {
    let answered = egress_posture_for(
        request.egress,
        &request.egress_network_connectors,
        request.deny_egress,
        Some(&region()),
    )
    .expect("launchable options");
    let token = request.agent_token.clone().expect("an explicit token");
    let plane = ControlPlane::new(region()).await.expect("credentials");
    let mut sandbox = Sandbox::new(region()).await.expect("credentials");
    let launched = sandbox
        .run(request)
        .await
        .map(|session| session.egress_posture());
    let record = sandbox
        .microvm()
        .map(|vm| (vm.id.clone(), vm.endpoint.clone()));
    match (&launched, &record) {
        (Ok(posture), Some((id, endpoint))) => {
            eprintln!("POSTURE {label}={posture} microvmId={id}");
            if *posture != answered {
                failures.push(format!(
                    "{label}: the session reports {posture}, egress_posture_for answered {answered}"
                ));
            }
            match Sandbox::adopt_in(region(), id, endpoint, &token, None).await {
                Ok(adopted) => {
                    let seen = adopted.session().map(|session| session.egress_posture());
                    eprintln!(
                        "POSTURE {label}-adopted={} microvmId={id}",
                        seen.map_or("none", EgressPosture::as_str)
                    );
                    if seen != Some(EgressPosture::Unsealed) {
                        failures.push(format!("{label}: an adopter reports {seen:?}"));
                    }
                }
                Err(error) => failures.push(format!("{label}: adopt failed: {error}")),
            }
        }
        _ => failures.push(format!("{label}: launch failed: {launched:?}")),
    }

    let _ = sandbox.terminate(TeardownOpts::default()).await;
    if let Some((id, _)) = &record {
        let opts = WaitOpts {
            timeout: Duration::from_secs(120),
            poll_interval: Duration::from_secs(2),
            ..WaitOpts::for_launch()
        };
        match plane.wait_for_state(id, &["TERMINATED"], &[], opts).await {
            Ok(vm) => eprintln!("cleanup microvmId={} state={}", vm.id, vm.state),
            Err(error) => failures.push(format!("cleanup unverified for {id}: {error}")),
        }
    }
}

/// **BIND-12, against AWS.** A launched session reports what the request-side answer and the
/// CLI envelope report for the same options; an adopter reports `unsealed`.
#[tokio::test]
#[ignore = "needs an explicit conformance image and AWS credentials; launches two bounded VMs"]
async fn a_launched_sessions_posture_is_its_requests() {
    let image = env("MICROVM_BACKGROUND_TEST_IMAGE");
    let role = env("MICROVM_EXECUTION_ROLE_ARN");
    let mut failures = Vec::new();
    for (label, egress) in [("default", false), ("egress", true)] {
        let mut request = RunRequest::new().with_image(&image);
        request.egress = egress;
        request.execution_role_arn = Some(role.clone());
        request.agent_token = Some(token::run_token("posture-guest"));
        request.max_duration_sec = 600;
        request.max_idle_sec = 300;
        launch(label, request, &mut failures).await;
    }
    assert!(failures.is_empty(), "{}", failures.join("; "));
}
