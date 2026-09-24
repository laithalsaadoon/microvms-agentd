// SPDX-License-Identifier: Apache-2.0
//! Invoked by conformance/run_rs.py with its existing image. Never discovers or builds images.
//! A 300-second VM lifetime bounds an interrupted test; normal cleanup checks GetMicrovm.

use std::collections::BTreeSet;
use std::io::Write;
use std::time::Duration;

use microvms_core::control::{ControlPlane, RunHookPayload, RunMicrovmRequest, WaitOpts, token};
use microvms_core::region::Region;

#[tokio::test]
#[ignore = "needs an explicit conformance image and AWS credentials; launches a bounded VM"]
async fn persisted_launch_key_replays_one_vm() {
    let image = std::env::var("MICROVM_BACKGROUND_TEST_IMAGE")
        .expect("conformance must supply the image it owns");
    let role = std::env::var("MICROVM_EXECUTION_ROLE_ARN")
        .expect("conformance must supply the execution role");
    let region: Region = std::env::var("AWS_REGION")
        .unwrap_or_else(|_| "us-east-1".into())
        .parse()
        .expect("a supported region");
    let first_plane = ControlPlane::new(region.clone())
        .await
        .expect("credentials");
    let retry_plane = ControlPlane::new(region).await.expect("credentials");
    let launch_key = token::run_token("background-conformance");
    let key_path = std::env::temp_dir().join(format!("{launch_key}.receipt"));
    std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&key_path)
        .expect("create receipt exclusively")
        .write_all(launch_key.as_bytes())
        .expect("persist before first launch");
    let guest_token = token::run_token("background-guest");
    let request = || {
        let payload = RunHookPayload::for_agent_token(&guest_token).expect("payload fits");
        let mut request = RunMicrovmRequest::new(&image, payload);
        request.execution_role_arn = Some(role.clone());
        request.max_duration_sec = 300;
        request.max_idle_sec = 60;
        request.suspended_sec = 0;
        request.client_token =
            Some(std::fs::read_to_string(&key_path).expect("read persisted key"));
        request
    };
    let mut ids = BTreeSet::new();
    let mut replies = Vec::new();
    let mut failures = Vec::new();
    // Both calls read the disk receipt, through independent Rust control-plane instances.
    // A lost first response still reaches the second call, which recovers the VM to clean.
    for plane in [&first_plane, &retry_plane] {
        match tokio::time::timeout(Duration::from_secs(90), plane.run_microvm(request())).await {
            Ok(Ok(vm)) => {
                eprintln!("accepted microvmId={}", vm.id);
                ids.insert(vm.id.clone());
                replies.push(vm.id);
            }
            Ok(Err(error)) => failures.push(format!("launch failed: {:?}", error.kind())),
            Err(_) => failures.push("launch response deadline expired".into()),
        }
    }
    // One final identical replay reconciles two missing replies before declaring a leak.
    if ids.is_empty() {
        if let Ok(Ok(vm)) =
            tokio::time::timeout(Duration::from_secs(90), retry_plane.run_microvm(request())).await
        {
            ids.insert(vm.id);
        } else {
            failures.push(
                "cleanup could not recover an accepted launch; lifetime capped at 300s".into(),
            );
        }
    }
    // Do not assert until every accepted ID has been terminated and independently observed.
    for id in &ids {
        let cleanup = async {
            let _ = retry_plane.terminate(id).await;
            retry_plane
                .wait_for_state(
                    id,
                    &["TERMINATED"],
                    &[],
                    WaitOpts {
                        timeout: Duration::from_secs(70),
                        poll_interval: Duration::from_secs(2),
                        ..WaitOpts::for_launch()
                    },
                )
                .await
        };
        match tokio::time::timeout(Duration::from_secs(90), cleanup).await {
            Ok(Ok(vm)) => eprintln!("cleanup microvmId={} state={}", vm.id, vm.state),
            _ => failures.push(format!("cleanup unverified for {id}")),
        }
    }
    std::fs::remove_file(key_path).expect("remove test receipt");
    assert!(failures.is_empty(), "{}", failures.join("; "));
    assert_eq!(
        replies.len(),
        2,
        "both independent callers must receive a VM"
    );
    assert_eq!(
        replies[0], replies[1],
        "persisted launch key created a second VM"
    );
}
