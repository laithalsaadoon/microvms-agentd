// SPDX-License-Identifier: Apache-2.0
//! Lifecycle-by-ID live tests, invoked by conformance/run_rs.py with its existing image.
//! Never discovers or builds images. A 600-second VM lifetime bounds an interrupted test;
//! normal cleanup terminates and observes TERMINATED through GetMicrovm.

use std::time::Duration;

use microvms_core::control::{ControlPlane, MicrovmFilter, WaitOpts, token};
use microvms_core::prelude::*;
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

fn opts(timeout: u64) -> WaitOpts {
    WaitOpts {
        timeout: Duration::from_secs(timeout),
        poll_interval: Duration::from_secs(2),
        ..WaitOpts::for_launch()
    }
}

/// **#195.** A launch retried with its client token after the VM suspended resumes that VM
/// and reaches RUNNING, rather than reporting a startup death. The retry is a fresh
/// `Sandbox`, which is what a replaying durable workflow step is.
#[tokio::test]
#[ignore = "needs an explicit conformance image and AWS credentials; launches a bounded VM"]
async fn an_adopted_suspended_launch_resumes_to_running() {
    let image = env("MICROVM_BACKGROUND_TEST_IMAGE");
    let role = env("MICROVM_EXECUTION_ROLE_ARN");
    let launch_key = token::run_token("lifecycle-conformance");
    let agent_token = token::run_token("lifecycle-guest");
    let request = || {
        let mut request = RunRequest::new().with_image(&image);
        request.execution_role_arn = Some(role.clone());
        request.client_token = Some(launch_key.clone());
        request.agent_token = Some(agent_token.clone());
        request.max_duration_sec = 600;
        request.max_idle_sec = 300;
        request.suspended_sec = 600;
        request
    };

    let plane = ControlPlane::new(region()).await.expect("credentials");
    let mut first = Sandbox::new(region()).await.expect("credentials");
    let launched = first.run(request()).await.map(|_| ());
    let id = first.microvm().map(|vm| vm.id.clone());
    let mut failures = Vec::new();

    if let (Ok(()), Some(id)) = (&launched, &id) {
        eprintln!("launched microvmId={id}");
        match plane.suspend(id).await {
            Ok(()) => match plane
                .wait_for_state(id, &["SUSPENDED"], &[], opts(120))
                .await
            {
                Ok(_) => eprintln!("suspended microvmId={id}"),
                Err(error) => failures.push(format!("never suspended: {error}")),
            },
            Err(error) => failures.push(format!("suspend refused: {error}")),
        }
        if failures.is_empty() {
            let mut retry = Sandbox::new(region()).await.expect("credentials");
            match retry.run(request()).await {
                Ok(_) => {
                    let adopted = retry.microvm().map(|vm| vm.id.clone());
                    if adopted.as_deref() != Some(id.as_str()) {
                        failures.push(format!("the retry launched {adopted:?}, not {id}"));
                    }
                    if retry.lifecycle().as_str() != "RUNNING" {
                        failures.push(format!("the retry is {}", retry.lifecycle()));
                    }
                }
                Err(error) => failures.push(format!("the retry failed: {error}")),
            }
            // The same VM as `first`; its terminate below is the one that is verified.
            let _ = retry.terminate(TeardownOpts::default()).await;
        }
    } else {
        failures.push(format!("first launch failed: {launched:?}"));
    }

    // Cleanup before any assertion, observed through the control plane.
    let _ = first.terminate(TeardownOpts::default()).await;
    if let Some(id) = &id {
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

/// **#197.** Lifecycle by ID through a `ControlPlane` alone, as the bindings expose it: get
/// with the lifetime members, list filtered to the image, suspend, resume, terminate.
#[tokio::test]
#[ignore = "needs an explicit conformance image and AWS credentials; launches a bounded VM"]
async fn a_vm_is_managed_by_id_through_the_control_plane() {
    let image = env("MICROVM_BACKGROUND_TEST_IMAGE");
    let role = env("MICROVM_EXECUTION_ROLE_ARN");
    let mut sandbox = Sandbox::new(region()).await.expect("credentials");
    let mut request = RunRequest::new().with_image(&image);
    request.execution_role_arn = Some(role);
    request.max_duration_sec = 600;
    request.wait = false;
    let launched = sandbox.run(request).await.map(|_| ());
    let id = sandbox.microvm().map(|vm| vm.id.clone());
    let plane = ControlPlane::new(region()).await.expect("credentials");
    let mut failures = Vec::new();

    if let (Ok(()), Some(id)) = (&launched, &id) {
        eprintln!("accepted microvmId={id} lifecycle={}", sandbox.lifecycle());
        if sandbox.lifecycle().as_str() != "PENDING" {
            failures.push("run(wait=false) waited".into());
        }
        let steps = async {
            let running = plane
                .wait_for_state(id, &["RUNNING"], &[], opts(300))
                .await?;
            if running.started_at.is_none() || running.maximum_duration_seconds != Some(600) {
                return Err(microvms_core::Error::invalid_arg(format!(
                    "lifetime members missing: started_at={:?} max={:?}",
                    running.started_at, running.maximum_duration_seconds
                )));
            }
            let listed = plane
                .list_microvms_matching(&MicrovmFilter {
                    image_identifier: Some(running.image_arn.clone()),
                    image_version: None,
                })
                .await?;
            if !listed.iter().any(|item| item.microvm_id == *id) {
                return Err(microvms_core::Error::invalid_arg(
                    "the image filter did not list the VM",
                ));
            }
            plane.suspend(id).await?;
            plane
                .wait_for_state(id, &["SUSPENDED"], &[], opts(120))
                .await?;
            plane.resume(id).await?;
            plane
                .wait_for_state(id, &["RUNNING"], &[], opts(120))
                .await?;
            Ok::<(), microvms_core::Error>(())
        };
        if let Err(error) = steps.await {
            failures.push(format!("lifecycle by id: {error}"));
        }
    } else {
        failures.push(format!("launch failed: {launched:?}"));
    }

    let _ = sandbox.terminate(TeardownOpts::default()).await;
    if let Some(id) = &id {
        match plane
            .wait_for_state(id, &["TERMINATED"], &[], opts(120))
            .await
        {
            Ok(vm) => {
                eprintln!("cleanup microvmId={} state={}", vm.id, vm.state);
                if vm.terminated_at.is_none() {
                    failures.push("TERMINATED without terminatedAt".into());
                }
            }
            Err(error) => failures.push(format!("cleanup unverified for {id}: {error}")),
        }
    }
    assert!(failures.is_empty(), "{}", failures.join("; "));
}
