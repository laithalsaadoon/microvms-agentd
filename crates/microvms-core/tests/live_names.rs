// SPDX-License-Identifier: Apache-2.0
//! Find-by-name live test, invoked by conformance/run_rs.py after `microvm run --keep
//! --vm-name` registered a VM in a scratch state directory. Never launches or builds: the
//! VM is the CLI's, and cleanup terminates it and observes TERMINATED through GetMicrovm.

use std::path::Path;
use std::time::Duration;

use microvms_core::control::{ControlPlane, WaitOpts};
use microvms_core::names::{FileNameStore, NameStore as _};
use microvms_core::prelude::*;
use microvms_core::region::Region;
use microvms_core::sandbox::{Lifecycle, Sandbox, TeardownOpts};

fn env(name: &str) -> String {
    std::env::var(name).unwrap_or_else(|_| panic!("conformance must supply {name}"))
}

fn region() -> Region {
    std::env::var("AWS_REGION")
        .unwrap_or_else(|_| "us-east-1".into())
        .parse()
        .expect("a supported region")
}

/// **#202.** A name the CLI registered is read by core's registry, adopted by name from a
/// fresh handle, named again from that handle, and released for both names on terminate —
/// so the CLI, the bindings, and core share one registry.
#[tokio::test]
#[ignore = "needs a VM the CLI registered by name and AWS credentials"]
async fn a_name_the_cli_registered_is_adopted_by_name_and_released() {
    let state = env("MICROVM_NAMES_STATE_DIR");
    let name = env("MICROVM_NAMES_NAME");
    let store = FileNameStore::in_state_root(Path::new(&state));
    let plane = ControlPlane::new(region()).await.expect("credentials");
    let record = store
        .get(&name)
        .expect("the registry reads")
        .expect("the CLI registered the name");
    let id = record.microvm_id.clone();
    eprintln!("resolved {name} to microvmId={id}");
    let mut failures = Vec::new();

    let steps = async {
        let mut adopted = Sandbox::from_name(&store, &name, Some(region()), None).await?;
        if !adopted.adopted() || adopted.lifecycle() != Lifecycle::Running {
            return Err(Box::from(format!("adopted as {}", adopted.lifecycle())));
        }
        let session = adopted.session().ok_or("no session")?;
        if !session.file_exists("/agentd").await? {
            return Err("an authenticated call through the named VM failed".into());
        }
        let alias = adopted.name_record(&format!("{name}-alias"))?;
        store.put(&alias)?;
        let report = adopted
            .terminate(TeardownOpts::default().waiting_for_terminated())
            .await;
        if !report.terminate_accepted || adopted.lifecycle() != Lifecycle::Terminated {
            return Err(Box::from(format!("terminate by name: {report:?}")));
        }
        let released = store.release_by_vm(&id)?;
        let mut expected = vec![name.clone(), alias.name.clone()];
        expected.sort();
        if released != expected {
            return Err(Box::from(format!(
                "released {released:?}, not {expected:?}"
            )));
        }
        if store.get(&name)?.is_some() {
            return Err("the name outlived its VM".into());
        }
        Ok::<(), Box<dyn std::error::Error>>(())
    };
    if let Err(error) = steps.await {
        failures.push(error.to_string());
    }

    // Cleanup before any assertion, observed through the control plane.
    let _ = plane.terminate(&id).await;
    let wait = WaitOpts {
        timeout: Duration::from_secs(120),
        poll_interval: Duration::from_secs(2),
        ..WaitOpts::for_launch()
    };
    match plane.wait_for_state(&id, &["TERMINATED"], &[], wait).await {
        Ok(vm) => eprintln!("cleanup microvmId={} state={}", vm.id, vm.state),
        Err(error) => failures.push(format!("cleanup unverified for {id}: {error}")),
    }
    assert!(failures.is_empty(), "{}", failures.join("; "));
}
