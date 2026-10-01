// SPDX-License-Identifier: Apache-2.0
//! `run --no-wait` and `microvm wait` (#269): a launch the CLI accepts and finishes later.

#![cfg(test)]

use std::sync::Arc;
use std::time::Duration;

use microvms_core::control::transport::Transport;
use microvms_core::control::{Clock, ControlPlane};
use microvms_core::sandbox::Sandbox;
use microvms_core::session::Session;
use microvms_core::testing::{HealthyDaemon, SequenceEntropy, TestAdapters, YieldingClock};
use microvms_core::{Error, ErrorKind, Region};

use super::support::{
    RefusingSeam, ScriptedTransport, TempDir, dispatch_with, full_infra, microvm_body,
    region_flags, run_args_for_image,
};
use crate::cli::{AttachFlags, Cli, Command, WaitArgs};
use crate::exit::Exit;
use crate::seam::futures_util_shim::BoxFuture;
use crate::seam::{Attach, CoreSeam};

const IMAGE_ARN: &str = "arn:aws:lambda:us-east-1:123456789012:microvm-image:img";
/// The endpoint `microvm_body` reports, which `Sandbox::adopt` holds a triple to.
const ENDPOINT: &str = "https://mvm-abc123.microvm.us-east-1.amazonaws.com";
const PROXY_TOKEN: &str = r#"{"authToken": {"X-aws-proxy-auth": "opaque"}}"#;

/// A seam whose planes send through `transport` and whose sessions reach `daemon`, so a
/// command that adopts a VM through the control-plane door can wait for its daemon offline.
struct WaitSeam {
    transport: Arc<ScriptedTransport>,
    clock: Arc<YieldingClock>,
    daemon: Arc<HealthyDaemon>,
}

impl WaitSeam {
    fn new(transport: &Arc<ScriptedTransport>, daemon: &Arc<HealthyDaemon>) -> Self {
        Self {
            transport: Arc::clone(transport),
            clock: Arc::new(YieldingClock::default()),
            daemon: Arc::clone(daemon),
        }
    }

    #[expect(
        clippy::disallowed_methods,
        reason = "a fake seam, the test's stand-in for src/seam.rs: it builds its plane over a scripted transport and a healthy daemon"
    )]
    fn plane(&self, region: Region) -> ControlPlane {
        let backend = Arc::clone(&self.daemon) as microvms_core::session::SharedBackend;
        ControlPlane::from_ports(
            Arc::clone(&self.transport) as Arc<dyn Transport>,
            region,
            Arc::clone(&self.clock) as Arc<dyn Clock>,
            Arc::new(SequenceEntropy::new()),
            Arc::new(TestAdapters::new().with_backend(backend)),
        )
    }
}

impl CoreSeam for WaitSeam {
    fn control_plane(&self, region: Region) -> BoxFuture<'_, Result<ControlPlane, Error>> {
        let plane = self.plane(region);
        Box::pin(async move { Ok(plane) })
    }

    fn open_sandbox(
        &self,
        region: Region,
        _port: Option<u16>,
    ) -> BoxFuture<'_, Result<Sandbox, Error>> {
        let plane = self.plane(region);
        Box::pin(async move { Ok(Sandbox::with_control_plane(plane)) })
    }

    fn attach_session(
        &self,
        _region: Region,
        _attach: Attach,
    ) -> BoxFuture<'_, Result<Session, Error>> {
        Box::pin(async move {
            Err(Error::new(
                ErrorKind::Platform,
                "`wait` adopts through the control plane; it attaches no session",
            ))
        })
    }
}

/// `wait` against `attach`, with the default timeout.
fn wait_command(attach: AttachFlags) -> Command {
    Command::Wait(WaitArgs {
        timeout: Duration::from_secs(300),
        attach,
        region: region_flags(),
    })
}

/// The triple `microvm_body`'s VM answers to.
fn triple() -> AttachFlags {
    AttachFlags {
        endpoint: Some(ENDPOINT.into()),
        agent_token: Some("t".into()),
        microvm_id: Some("mvm-abc123".into()),
        name: None,
        port: None,
        state_dir: None,
    }
}

/// **`run --keep --no-wait` returns at acceptance, and `wait --name` finishes the launch
/// (#269).** The run sends `RunMicrovm` and nothing after it, polls no daemon, and still hands
/// back the identifiers and the registered name. `wait` then adopts the VM by that name, reads
/// PENDING from the service, waits for RUNNING and for one answered health poll.
///
/// **Falsification**: `verify/guards/faults/wait-command.toml` entry `cli-run-no-wait-waits`
/// (the run waits anyway, and its calls include `GetMicrovm`).
#[tokio::test]
async fn run_no_wait_returns_at_acceptance_and_wait_finishes_the_launch() {
    let dir = TempDir::new("no-wait");
    let transport = Arc::new(ScriptedTransport::new());
    transport
        .answer("RunMicrovm", 200, &microvm_body("PENDING"))
        .answer("GetMicrovm", 200, &microvm_body("PENDING"))
        .answer("GetMicrovm", 200, &microvm_body("RUNNING"))
        .answer("CreateMicrovmAuthToken", 200, PROXY_TOKEN);
    let daemon = HealthyDaemon::new();
    let seam = WaitSeam::new(&transport, &daemon);

    let mut run = run_args_for_image(IMAGE_ARN, dir.0.clone());
    run.keep = true;
    run.no_wait = true;
    run.vm_name = Some("box".into());
    let (result, _) = dispatch_with(&seam, &Command::Run(Box::new(run)), full_infra()).await;
    let launched = result.expect("the launch is accepted");
    assert_eq!(
        transport.calls(),
        ["RunMicrovm"],
        "no wait after the launch"
    );
    assert_eq!(daemon.polls(), 0);
    assert_eq!(launched.data["microvmId"], "mvm-abc123");
    assert_eq!(launched.data["endpoint"], ENDPOINT);
    assert_eq!(launched.data["vmName"], "box");
    assert!(
        launched.data["agentToken"]
            .as_str()
            .is_some_and(|token| !token.is_empty()),
        "the token a later command needs"
    );

    let by_name = AttachFlags {
        endpoint: None,
        agent_token: None,
        microvm_id: None,
        name: Some("box".into()),
        port: None,
        state_dir: Some(dir.0.clone()),
    };
    let (result, _) = dispatch_with(&seam, &wait_command(by_name), full_infra()).await;
    let waited = result.expect("the VM answers");
    assert_eq!(waited.kind, "microvm.wait");
    assert_eq!(
        (&waited.data["from"], &waited.data["state"]),
        (&"PENDING".into(), &"RUNNING".into())
    );
    assert_eq!(waited.data["endpoint"], ENDPOINT);
    assert_eq!(
        transport.calls(),
        [
            "RunMicrovm",
            "GetMicrovm",
            "GetMicrovm",
            "CreateMicrovmAuthToken"
        ],
        "the adopt's read, the RUNNING wait's, and the proxy token the health poll needs"
    );
    assert_eq!(daemon.polls(), 1);
}

/// **`wait` on a RUNNING VM waits for its daemon alone (#269):** one control-plane read, the
/// proxy token, and one health poll.
///
/// **Falsification**: `verify/guards/faults/wait-command.toml` entry
/// `app-wait-until-ready-drops-running` (the RUNNING VM is refused as not PENDING).
#[tokio::test]
async fn wait_on_a_running_vm_waits_for_its_daemon_alone() {
    let transport = Arc::new(ScriptedTransport::new());
    transport
        .answer("GetMicrovm", 200, &microvm_body("RUNNING"))
        .answer("CreateMicrovmAuthToken", 200, PROXY_TOKEN);
    let daemon = HealthyDaemon::new();
    let seam = WaitSeam::new(&transport, &daemon);

    let (result, _) = dispatch_with(&seam, &wait_command(triple()), full_infra()).await;
    let waited = result.expect("the daemon answers");
    assert_eq!(
        (&waited.data["from"], &waited.data["state"]),
        (&"RUNNING".into(), &"RUNNING".into())
    );
    assert_eq!(transport.calls(), ["GetMicrovm", "CreateMicrovmAuthToken"]);
    assert_eq!(daemon.polls(), 1);
}

/// **`wait` refuses a VM that isn't starting, with no daemon poll (#269).** A suspended VM
/// would never answer, so waiting out the timeout on it would be a wrong answer that took
/// minutes.
#[tokio::test]
async fn wait_refuses_a_suspended_vm_without_polling_it() {
    let transport = Arc::new(ScriptedTransport::new());
    transport.answer("GetMicrovm", 200, &microvm_body("SUSPENDED"));
    let daemon = HealthyDaemon::new();
    let seam = WaitSeam::new(&transport, &daemon);

    let (result, _) = dispatch_with(&seam, &wait_command(triple()), full_infra()).await;
    let failure = result.expect_err("a suspended VM isn't starting");
    assert_eq!(failure.exit, Exit::InvalidArg, "{}", failure.message);
    assert!(failure.message.contains("SUSPENDED"), "{}", failure.message);
    assert_eq!(transport.calls(), ["GetMicrovm"]);
    assert_eq!(daemon.polls(), 0);
}

/// **`run --no-wait` refuses the work that needs the VM answering before any door (#269):** a
/// directory positional (sync mode) and an `exec` from microvm.toml, the two spellings clap's
/// `conflicts_with` can't see.
///
/// **Falsification**: `verify/guards/faults/wait-command.toml` entry
/// `cli-run-no-wait-takes-a-config-exec` (the config's exec is taken, and the run reaches the
/// sandbox door).
#[tokio::test]
async fn run_no_wait_refuses_sync_mode_and_a_config_exec_before_any_door() {
    use clap::Parser as _;
    let tree = TempDir::new("no-wait-tree");
    let config = tree.0.join("microvm.toml");
    std::fs::write(&config, "exec = \"make test\"\n").expect("writes the config");
    let state = TempDir::new("no-wait-state");
    let tree_arg = tree.0.to_string_lossy().to_string();
    let config_arg = config.to_string_lossy().to_string();
    let state_arg = state.0.to_string_lossy().to_string();
    let common = [
        "--image",
        IMAGE_ARN,
        "--keep",
        "--no-wait",
        "--state-dir",
        &state_arg,
    ];
    let rows: [(&str, Vec<&str>); 2] = [
        (
            "sync mode",
            [&[tree_arg.as_str(), "--no-config"][..], &common].concat(),
        ),
        (
            "an exec (from microvm.toml)",
            [&["--config", config_arg.as_str()][..], &common].concat(),
        ),
    ];
    for (what, rest) in rows {
        let argv = ["microvm", "run"].into_iter().chain(rest);
        let command = Cli::try_parse_from(argv).expect("parses").command;
        let seam = RefusingSeam::new();
        let (result, _) = dispatch_with(&seam, &command, full_infra()).await;
        let failure = result.expect_err("refused");
        assert_eq!(
            (failure.exit, seam.doors()),
            (Exit::InvalidArg, Vec::new()),
            "{what}: {}",
            failure.message
        );
        assert!(
            failure.message.contains(what),
            "{what}: {}",
            failure.message
        );
    }
}
