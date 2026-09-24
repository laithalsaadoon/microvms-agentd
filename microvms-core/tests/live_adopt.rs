// SPDX-License-Identifier: Apache-2.0
//! Adopt-by-ID live test, invoked by conformance/run_rs.py with its existing image.
//! Never discovers or builds images. A 600-second VM lifetime bounds an interrupted test;
//! normal cleanup terminates and observes TERMINATED through GetMicrovm.

use std::time::Duration;

use microvms_core::control::{ControlPlane, WaitOpts, token};
use microvms_core::region::Region;
use microvms_core::sandbox::{Lifecycle, RunRequest, Sandbox, TeardownOpts};

fn env(name: &str) -> String {
    std::env::var(name).unwrap_or_else(|_| panic!("conformance must supply {name}"))
}

fn region() -> Region {
    std::env::var("AWS_REGION")
        .unwrap_or_else(|_| "us-east-1".into())
        .parse()
        .expect("a supported region")
}

fn opts(timeout: u64) -> WaitOpts {
    WaitOpts {
        timeout: Duration::from_secs(timeout),
        poll_interval: Duration::from_secs(2),
        ..WaitOpts::for_launch()
    }
}

/// **#196.** A VM launched by one handle is adopted by fresh ones, each a stand-in for a
/// new process: the lifecycle comes from the service, `run` is refused, the session works,
/// and suspend, a second adoption while SUSPENDED, resume, and terminate all go through
/// the adopted handles.
#[tokio::test]
#[ignore = "needs an explicit conformance image and AWS credentials; launches a bounded VM"]
async fn a_vm_launched_elsewhere_is_adopted_and_driven_by_id() {
    let image = env("MICROVM_BACKGROUND_TEST_IMAGE");
    let role = env("MICROVM_EXECUTION_ROLE_ARN");
    let agent_token = token::run_token("adopt-guest");
    let mut request = RunRequest::new().with_image(&image);
    request.execution_role_arn = Some(role);
    request.agent_token = Some(agent_token.clone());
    request.max_duration_sec = 600;
    request.max_idle_sec = 300;
    request.suspended_sec = 600;

    let plane = ControlPlane::new(region()).await.expect("credentials");
    let mut launcher = Sandbox::new(region()).await.expect("credentials");
    let launched = launcher.run(request).await.map(|_| ());
    let record = launcher
        .microvm()
        .map(|vm| (vm.id.clone(), vm.endpoint.clone()));
    let mut failures = Vec::new();

    if let (Ok(()), Some((id, endpoint))) = (&launched, &record) {
        eprintln!("launched microvmId={id}");
        let steps = async {
            let mut adopted = Sandbox::adopt_in(region(), id, endpoint, &agent_token, None).await?;
            if !adopted.adopted() || adopted.lifecycle() != Lifecycle::Running {
                return Err(Box::from(format!("adopted as {}", adopted.lifecycle())));
            }
            if adopted.bootstrap_count() != 1 {
                return Err(Box::from(format!(
                    "bootstrap_count {}",
                    adopted.bootstrap_count()
                )));
            }
            match adopted.run(RunRequest::new().with_image(&image)).await {
                Err(error) if error.kind() == microvms_core::ErrorKind::InvalidArg => {}
                other => {
                    return Err(Box::from(format!(
                        "run on an adopted VM was not refused: {other:?}"
                    )));
                }
            }
            let session = adopted.session().ok_or("no session")?;
            let health = session.health().await?;
            if !health.bootstrapped {
                return Err("the adopted session saw an unbootstrapped daemon".into());
            }
            if !session.file_exists("/agentd").await? {
                return Err("an authenticated call through the adopted session failed".into());
            }

            adopted.suspend().await?;
            if adopted.lifecycle() != Lifecycle::Suspended {
                return Err(Box::from(format!(
                    "suspend reached {}",
                    adopted.lifecycle()
                )));
            }
            eprintln!("suspended microvmId={id} through the adopted handle");

            let mut second = Sandbox::adopt_in(region(), id, endpoint, &agent_token, None).await?;
            if second.lifecycle() != Lifecycle::Suspended {
                return Err(Box::from(format!(
                    "the second adoption saw {}",
                    second.lifecycle()
                )));
            }
            second.resume().await?;
            let resumed = second.session().ok_or("no session after resume")?;
            if !resumed.file_exists("/agentd").await? {
                return Err("an authenticated call after resume failed".into());
            }
            eprintln!("resumed microvmId={id} through a second adopted handle");

            let report = second
                .terminate(TeardownOpts::default().waiting_for_terminated())
                .await;
            if !report.terminate_accepted || second.lifecycle() != Lifecycle::Terminated {
                return Err(Box::from(format!(
                    "terminate through the adopted handle: {report:?}"
                )));
            }
            Ok::<(), Box<dyn std::error::Error>>(())
        };
        if let Err(error) = steps.await {
            failures.push(error.to_string());
        }
    } else {
        failures.push(format!("launch failed: {launched:?}"));
    }

    // Cleanup before any assertion, observed through the control plane.
    let _ = launcher.terminate(TeardownOpts::default()).await;
    if let Some((id, _)) = &record {
        match plane
            .wait_for_state(id, &["TERMINATED"], &[], opts(120))
            .await
        {
            Ok(vm) => eprintln!("cleanup microvmId={} state={}", vm.id, vm.state),
            Err(error) => failures.push(format!("cleanup unverified for {id}: {error}")),
        }
    }
    assert!(failures.is_empty(), "{}", failures.join("; "));
}
