// SPDX-License-Identifier: Apache-2.0
//! The name registry, through the shipped handlers: a name that's taken or torn, a name standing
//! in for the id, and the region its record names.

#![cfg(test)]

use std::sync::{Arc, Mutex};
use std::time::Duration;

use microvms_core::control::ControlPlane;
use microvms_core::sandbox::Sandbox;
use microvms_core::session::Session;
use microvms_core::testing::YieldingClock;
use microvms_core::{Error, ErrorKind, Region};

use super::support::{
    DaemonScript, RefusingSeam, ScriptedSeam, ScriptedSessionSeam, ScriptedTransport, TempDir,
    dispatch_with, dispatch_with_env, full_infra, region_flags, run_args_for_image,
};
use crate::cli::{AttachFlags, Cli, Command, HealthArgs, RegionFlags, SuspendArgs, TerminateArgs};
use crate::exit::Exit;
use crate::seam::futures_util_shim::BoxFuture;
use crate::seam::{Attach, CoreSeam, Door};

/// **`terminate` appends a `terminated` event carrying the teardown's own verdict.**
///
/// The values are the platform's: `terminateAccepted` reflects whether the call was accepted,
/// and the file survives the terminate — which is the whole reason history is not the ledger.
///
/// **Guard proof.** Delete the `History::for_vm(..).append(Event::Terminated {..})` block from
/// `lifecycle::terminate` and the read below is empty; the command's envelope and exit are
/// unchanged, which is why only this test catches it. Broken exactly so on 2026-08-25
/// (block commented out, test red on `read.len()`), then restored.
#[tokio::test]
async fn a_taken_vm_name_is_refused_before_any_door_with_its_own_row() {
    // The acceptance criterion, verbatim: collision on reuse of a live name is a local
    // refusal with a stable ERR_* code and **zero billable calls**. The seam is the
    // RefusingSeam, so any AWS reach shows up as an entered door — and the assertion below
    // is that none was.
    let dir = TempDir::new("name-collision");
    crate::ledger::Names::new(&dir.0)
        .register(&crate::ledger::NameRecord {
            name: "ci-runner".into(),
            microvm_id: "mvm-live".into(),
            endpoint: "https://mvm-live.example".into(),
            agent_token: "tok".into(),
            region: "us-east-1".into(),
            at: 1,
            identity_host_seed: None,
            identity_vm_public_key: None,
            egress_posture: None,
        })
        .expect("registers");

    let seam = RefusingSeam::new();
    let mut args = run_args_for_image(
        "arn:aws:lambda:us-east-1:123456789012:microvm-image:img",
        dir.0.clone(),
    );
    args.keep = true;
    args.vm_name = Some("ci-runner".into());
    let (result, _) = dispatch_with(&seam, &Command::Run(Box::new(args)), full_infra()).await;

    let failure = result.expect_err("a taken name is a refusal");
    assert_eq!(failure.exit, Exit::NameTaken);
    assert_eq!(failure.code(), "ERR_NAME_TAKEN");
    assert_eq!(failure.exit.as_u8(), 14);
    assert!(
        failure.message.contains("mvm-live"),
        "the holder is named, so the remedy is actionable: {}",
        failure.message
    );
    assert_eq!(
        seam.doors(),
        Vec::<Door>::new(),
        "the refusal must cost zero AWS calls — no door may have been entered"
    );

    // And an illegal name is the *other* row: fixed by editing the flag, not by a terminate.
    let seam = RefusingSeam::new();
    let mut args = run_args_for_image(
        "arn:aws:lambda:us-east-1:123456789012:microvm-image:img",
        dir.0.clone(),
    );
    args.keep = true;
    args.vm_name = Some("mvm-lookalike".into());
    let (result, _) = dispatch_with(&seam, &Command::Run(Box::new(args)), full_infra()).await;
    let failure = result.expect_err("an id-shaped name is refused");
    assert_eq!(failure.exit, Exit::InvalidArg);
    assert_eq!(seam.doors(), Vec::<Door>::new(), "still before any door");
}

/// **A registered name substitutes for the id on the lifecycle wire, and a raw-id terminate
/// frees it.**
///
/// The suspend asserts the substitution where it matters — the `GetMicrovm` path the state
/// read hits carries `mvm-live`, never the name. The terminate then addresses the same VM by
/// its raw id and must release the registration anyway, because a registry that keeps
/// claiming a name for a dead VM turns every later `--vm-name` into a false collision.
#[tokio::test]
async fn a_name_resolves_on_the_lifecycle_wire_and_a_terminate_by_id_frees_it() {
    let dir = TempDir::new("name-lifecycle");
    crate::ledger::Names::new(&dir.0)
        .register(&crate::ledger::NameRecord {
            name: "ci-runner".into(),
            microvm_id: "mvm-live".into(),
            endpoint: "https://mvm-live.example".into(),
            agent_token: "tok".into(),
            region: "us-east-1".into(),
            at: 1,
            identity_host_seed: None,
            identity_vm_public_key: None,
            egress_posture: None,
        })
        .expect("registers");

    // suspend by name: the wire carries the id.
    let transport = Arc::new(ScriptedTransport::new());
    transport
        .answer(
            "GetMicrovm",
            200,
            r#"{"microvmId": "mvm-live", "state": "RUNNING",
                 "endpoint": "https://mvm-live.example",
                 "imageArn": "arn:aws:lambda:us-east-1:123456789012:microvm-image:img",
                 "imageVersion": "1", "maximumDurationInSeconds": 3600, "startedAt": 1}"#,
        )
        .answer("SuspendMicrovm", 200, "{}");
    // The post-suspend wait re-reads the state; the second GetMicrovm answer repeats, so the
    // wait sees RUNNING forever — script SUSPENDED as the settled answer instead.
    transport.answer(
        "GetMicrovm",
        200,
        r#"{"microvmId": "mvm-live", "state": "SUSPENDED",
             "endpoint": "https://mvm-live.example",
             "imageArn": "arn:aws:lambda:us-east-1:123456789012:microvm-image:img",
             "imageVersion": "1", "maximumDurationInSeconds": 3600, "startedAt": 1}"#,
    );
    let seam = ScriptedSeam {
        transport: Arc::clone(&transport),
        clock: Arc::new(YieldingClock::default()),
    };
    let command = Command::Suspend(SuspendArgs {
        microvm_id: "ci-runner".into(),
        timeout: Duration::from_secs(30),
        state_dir: Some(dir.0.clone()),
        region: region_flags(),
    });
    let (result, _) = dispatch_with(&seam, &command, full_infra()).await;
    result.expect("the suspend succeeds through the name");
    let suspends = transport.paths_of("SuspendMicrovm");
    assert!(
        suspends[0].contains("mvm-live") && !suspends[0].contains("ci-runner"),
        "the wire must carry the resolved id, never the local name: {}",
        suspends[0]
    );

    // terminate by raw id: the name is freed.
    let transport = Arc::new(ScriptedTransport::new());
    transport.answer("TerminateMicrovm", 200, "{}");
    let seam = ScriptedSeam {
        transport: Arc::clone(&transport),
        clock: Arc::new(YieldingClock::default()),
    };
    let command = Command::Terminate(TerminateArgs {
        microvm_id: "mvm-live".into(),
        image_identifier: None,
        image_name: None,
        delete_image: false,
        wait: false,
        wait_sec: None,
        state_dir: Some(dir.0.clone()),
        region: region_flags(),
    });
    let (result, stderr) = dispatch_with(&seam, &command, full_infra()).await;
    result.expect("the terminate succeeds");
    assert!(
        crate::ledger::Names::new(&dir.0)
            .lookup("ci-runner")
            .is_none(),
        "a terminate by raw id must free the name its VM held"
    );
    assert!(
        stderr.contains("released name ci-runner"),
        "the release is said, so the operator knows the name is reusable: {stderr}"
    );

    // An unknown name on the same surface fails locally, before any call.
    let transport = Arc::new(ScriptedTransport::new());
    let seam = ScriptedSeam {
        transport: Arc::clone(&transport),
        clock: Arc::new(YieldingClock::default()),
    };
    let command = Command::Suspend(SuspendArgs {
        microvm_id: "never-registered".into(),
        timeout: Duration::from_secs(30),
        state_dir: Some(dir.0.clone()),
        region: region_flags(),
    });
    let (result, _) = dispatch_with(&seam, &command, full_infra()).await;
    let failure = result.expect_err("an unknown name has nothing to suspend");
    assert_eq!(failure.exit, Exit::Precondition);
    assert_eq!(
        transport.calls(),
        Vec::<String>::new(),
        "the miss is local: {:?}",
        transport.calls()
    );
}

/// **`exec --name` attaches with the registered record's triple.**
///
/// Asserted at the seam, which is where the substitution is observable: the `Attach` the
/// handler passes carries the record's endpoint, token, and id, and the caller typed none of
/// them.
#[tokio::test]
async fn an_attached_command_by_name_carries_the_registered_triple() {
    let dir = TempDir::new("name-attach");
    crate::ledger::Names::new(&dir.0)
        .register(&crate::ledger::NameRecord {
            name: "ci-runner".into(),
            microvm_id: "mvm-named".into(),
            endpoint: "https://mvm-named.example".into(),
            agent_token: "tok-named".into(),
            region: "us-west-2".into(),
            at: 1,
            identity_host_seed: None,
            identity_vm_public_key: None,
            egress_posture: None,
        })
        .expect("registers");

    /// A seam that records the `Attach` it was handed and then refuses.
    struct AttachRecorder {
        seen: Mutex<Vec<(Attach, String)>>,
    }
    impl CoreSeam for AttachRecorder {
        fn control_plane(&self, _region: Region) -> BoxFuture<'_, Result<ControlPlane, Error>> {
            panic!("an attached command never opens a control plane directly")
        }
        fn open_sandbox(
            &self,
            _region: Region,
            _port: Option<u16>,
        ) -> BoxFuture<'_, Result<Sandbox, Error>> {
            panic!("an attached command never opens a sandbox")
        }
        fn attach_session(
            &self,
            region: Region,
            attach: Attach,
        ) -> BoxFuture<'_, Result<Session, Error>> {
            self.seen
                .lock()
                .expect("not poisoned")
                .push((attach, region.as_str().to_string()));
            Box::pin(async move { Err(Error::new(ErrorKind::Platform, "recorded; stopping")) })
        }
        fn put_artifact(&self, _uri: &str, _bytes: Vec<u8>) -> BoxFuture<'_, Result<(), Error>> {
            panic!("no artifact on this path")
        }
    }

    let seam = AttachRecorder {
        seen: Mutex::new(Vec::new()),
    };
    let command = Command::Health(HealthArgs {
        attach: AttachFlags {
            endpoint: None,
            agent_token: None,
            microvm_id: None,
            name: Some("ci-runner".into()),
            port: None,
            state_dir: Some(dir.0.clone()),
        },
        region: RegionFlags::default(),
    });
    let (result, _) = dispatch_with(&seam, &command, full_infra()).await;
    result.expect_err("the recorder refuses after recording");

    let seen = seam.seen.lock().expect("not poisoned");
    assert_eq!(seen.len(), 1, "exactly one attach was attempted");
    let (attach, region) = &seen[0];
    assert_eq!(attach.endpoint, "https://mvm-named.example");
    assert_eq!(attach.agent_token, "tok-named");
    assert_eq!(attach.microvm_id, "mvm-named");
    assert_eq!(
        region, "us-west-2",
        "with no --region flag, the record's launch region is the default"
    );
}

/// A record for `name` in us-west-2, the region every #251 guard's flag or environment
/// disagrees with.
fn register_in_us_west_2(dir: &std::path::Path, name: &str) {
    crate::ledger::Names::new(dir)
        .register(&crate::ledger::NameRecord {
            name: name.into(),
            microvm_id: "mvm-west".into(),
            endpoint: "https://mvm-west.example".into(),
            agent_token: "tok-west".into(),
            region: "us-west-2".into(),
            at: 1,
            identity_host_seed: None,
            identity_vm_public_key: None,
            egress_posture: None,
        })
        .expect("registers");
}

/// **A `--region` that disagrees with the name's record is refused with core's message, before
/// any door, on every command that reads a name (#251).**
///
/// `Sandbox.from_name` in both bindings refuses the same record through `names::resolve`, so
/// the CLI giving the same answer is the parity claim. Before #251 each of these commands let
/// the flag override the record and went on to mint or attach in the flag's region, where the
/// VM doesn't exist.
///
/// **Falsification** (#251). Pass `None` instead of `explicit_region(region).as_ref()` to
/// `names.resolve` in `resolve_attach` (the flag override's effect) and the `exec` row goes red
/// with `(Platform, [AttachSession])`. Make `explicit_region` ignore `--unlisted-region` and the
/// `exec --unlisted-region` row goes red the same way.
#[tokio::test]
async fn a_region_flag_that_disagrees_with_the_names_record_is_refused_before_any_door() {
    use clap::Parser as _;
    let dir = TempDir::new("name-region-flag");
    register_in_us_west_2(&dir.0, "x");
    let state = dir.0.to_string_lossy().to_string();
    let record = dir.0.join("names").join("x.json");
    let record = record.to_string_lossy().to_string();
    let typed = ("--region", "us-east-1");
    let rows: Vec<(Vec<&str>, (&str, &str))> = vec![
        (vec!["exec", "true", "--name", "x"], typed),
        (vec!["shell", "--name", "x"], typed),
        (vec!["agent-up", "--vm-name", "x"], typed),
        (vec!["attach", "--from", &record, "--name", "y"], typed),
        (vec!["suspend", "x"], typed),
        (vec!["resume", "x"], typed),
        (vec!["terminate", "x"], typed),
        // The other spelling of an explicit region: the rule reads both flags.
        (
            vec!["exec", "true", "--name", "x"],
            ("--unlisted-region", "eu-south-9"),
        ),
    ];
    for (row, (flag, value)) in rows {
        let mut argv = vec!["microvm"];
        argv.extend(row.iter().copied());
        argv.extend(["--state-dir", &state, flag, value]);
        let shown = if flag == "--region" {
            row[0].to_string()
        } else {
            format!("{} {flag}", row[0])
        };
        let command = Cli::try_parse_from(&argv).expect("parses").command;
        let seam = RefusingSeam::new();
        let (result, _) = dispatch_with(&seam, &command, full_infra()).await;
        let failure = result.expect_err("a region the record disagrees with is a refusal");
        assert_eq!(
            (failure.exit, seam.doors()),
            (Exit::InvalidArg, Vec::<Door>::new()),
            "{shown}: {}",
            failure.message
        );
        assert!(
            failure
                .message
                .contains(&format!("was registered in us-west-2, not {value}")),
            "{shown}: core's message: {}",
            failure.message
        );
    }
}

/// A seam that records the region each control plane or attach was asked for, then refuses.
struct PlaneRegions {
    asked: Mutex<Vec<String>>,
}

impl PlaneRegions {
    fn record(&self, region: &Region) {
        self.asked
            .lock()
            .expect("not poisoned")
            .push(region.as_str().to_string());
    }
}

impl CoreSeam for PlaneRegions {
    fn control_plane(&self, region: Region) -> BoxFuture<'_, Result<ControlPlane, Error>> {
        self.record(&region);
        Box::pin(async move { Err(Error::new(ErrorKind::Platform, "recorded; stopping")) })
    }
    fn open_sandbox(
        &self,
        _region: Region,
        _port: Option<u16>,
    ) -> BoxFuture<'_, Result<Sandbox, Error>> {
        panic!("no command here opens a sandbox")
    }
    fn attach_session(
        &self,
        region: Region,
        _attach: Attach,
    ) -> BoxFuture<'_, Result<Session, Error>> {
        self.record(&region);
        Box::pin(async move { Err(Error::new(ErrorKind::Platform, "recorded; stopping")) })
    }
    fn put_artifact(&self, _uri: &str, _bytes: Vec<u8>) -> BoxFuture<'_, Result<(), Error>> {
        panic!("no artifact on this path")
    }
}

/// **`suspend`, `resume` and `terminate` by name build their control plane in the record's
/// region, whatever the environment says (#251).**
///
/// A VM id addresses nothing outside its own region, so a lifecycle call built for the
/// shell's `AWS_REGION` would answer not-found for a VM that's running.
///
/// **Falsification** (#251). Make `terminate`'s region match `Some(_) | None =>
/// args.region.resolve(ctx.env)?` and the `terminate by name` row goes red with
/// `left: ["us-east-1"]`.
#[tokio::test]
async fn a_lifecycle_command_by_name_asks_for_the_records_region_over_the_environments() {
    use clap::Parser as _;
    let dir = TempDir::new("name-lifecycle-region");
    register_in_us_west_2(&dir.0, "x");
    let state = dir.0.to_string_lossy().to_string();
    for verb in ["suspend", "resume", "terminate"] {
        let argv = ["microvm", verb, "x", "--state-dir", &state];
        let command = Cli::try_parse_from(argv).expect("parses").command;
        let seam = PlaneRegions {
            asked: Mutex::new(Vec::new()),
        };
        let _ = dispatch_with_env(&seam, &command, full_infra(), ("AWS_REGION", "us-east-1")).await;
        assert_eq!(
            *seam.asked.lock().expect("not poisoned"),
            ["us-west-2"],
            "{verb} by name"
        );
    }
}

/// **`shell --name` and `agent-up`'s refresh reach the VM in the record's region, whatever the
/// environment says (#251).**
///
/// `shell` mints its token through the control plane, and `agent-up --vm-name` over a
/// registered name attaches and then mints the Bedrock token, each in the region it read. A
/// region taken from the shell's `AWS_REGION` would ask for a token for a VM that isn't there,
/// or mint the Bedrock token somewhere the VM never calls.
///
/// **Falsification** (#251). Make `shell`'s region `args.region.resolve(ctx.env)?` instead of
/// `record.region()` and the `shell` row goes red with `left: ["us-east-1"]`; the same change
/// in `agent-up`'s refresh turns the `agent-up` row red.
///
/// ```falsification
/// id = "names-shell-record-region"
/// file = "crates/microvms-cli/src/commands/attached.rs"
/// replace = "        let region = record.region();\n        (record.endpoint, record.microvm_id, region)\n"
/// with = "        let region = args.region.resolve(ctx.env)?;\n        (record.endpoint, record.microvm_id, region)\n"
/// message = "shell by name"
/// ```
#[tokio::test]
async fn a_shell_or_agent_up_by_name_asks_for_the_records_region_over_the_environments() {
    use clap::Parser as _;
    let dir = TempDir::new("name-shell-agent-region");
    register_in_us_west_2(&dir.0, "x");
    let state = dir.0.to_string_lossy().to_string();
    let rows: Vec<Vec<&str>> = vec![
        vec!["shell", "--name", "x"],
        vec!["agent-up", "--vm-name", "x"],
    ];
    for row in rows {
        let mut argv = vec!["microvm"];
        argv.extend(row.iter().copied());
        argv.extend(["--state-dir", &state]);
        let command = Cli::try_parse_from(&argv).expect("parses").command;
        let seam = PlaneRegions {
            asked: Mutex::new(Vec::new()),
        };
        let _ = dispatch_with_env(&seam, &command, full_infra(), ("AWS_REGION", "us-east-1")).await;
        assert_eq!(
            *seam.asked.lock().expect("not poisoned"),
            ["us-west-2"],
            "{} by name",
            row[0]
        );
    }
}

/// Control planes whose `GetMicrovm` reports a 600-second idle window in us-west-2 and a
/// not-found everywhere else, over [`ScriptedSeam`]; sessions over [`ScriptedSessionSeam`].
struct WindowInUsWest2 {
    west: ScriptedSeam,
    elsewhere: ScriptedSeam,
    sessions: ScriptedSessionSeam,
}

impl CoreSeam for WindowInUsWest2 {
    fn control_plane(&self, region: Region) -> BoxFuture<'_, Result<ControlPlane, Error>> {
        if region.as_str() == "us-west-2" {
            self.west.control_plane(region)
        } else {
            self.elsewhere.control_plane(region)
        }
    }
    fn open_sandbox(
        &self,
        _region: Region,
        _port: Option<u16>,
    ) -> BoxFuture<'_, Result<Sandbox, Error>> {
        panic!("keepalive never opens a sandbox")
    }
    fn attach_session(
        &self,
        region: Region,
        attach: Attach,
    ) -> BoxFuture<'_, Result<Session, Error>> {
        self.sessions.attach_session(region, attach)
    }
    fn put_artifact(&self, _uri: &str, _bytes: Vec<u8>) -> BoxFuture<'_, Result<(), Error>> {
        panic!("no artifact on this path")
    }
}

/// **`keepalive --name` reads the idle window in the record's region (#251).**
///
/// A 60-second interval is legal only against the VM's real 600-second window. When the read
/// misses, the command falls back to the platform's 60-second minimum, and half of that is
/// under the interval, so a read in the wrong region turns into a refusal.
///
/// **Falsification** (#251). Pass `args.region.resolve(ctx.env).unwrap_or(region)` to
/// `idle_window_of` in `keepalive` (the flag or environment region back) and this goes red
/// with `refused: keepalive interval 60s exceeds half the 60s idle window`.
///
/// ```falsification
/// id = "names-keepalive-window-region"
/// file = "crates/microvms-cli/src/commands/attached.rs"
/// replace = "idle_window_of(ctx, region, &microvm_id)"
/// with = "idle_window_of(ctx, args.region.resolve(ctx.env).unwrap_or(region), &microvm_id)"
/// message = "exceeds half the 60s idle window"
/// ```
#[tokio::test(start_paused = true)]
async fn keepalive_by_name_reads_the_idle_window_in_the_records_region() {
    use clap::Parser as _;
    let dir = TempDir::new("name-keepalive-region");
    register_in_us_west_2(&dir.0, "x");
    let west = Arc::new(ScriptedTransport::new());
    west.answer(
        "GetMicrovm",
        200,
        r#"{"microvmId": "mvm-west", "state": "RUNNING",
             "endpoint": "https://mvm-west.example",
             "imageArn": "arn:aws:lambda:us-west-2:123456789012:microvm-image:img",
             "imageVersion": "1", "maximumDurationInSeconds": 3600, "startedAt": 1,
             "idlePolicy": {"maxIdleDurationSeconds": 600, "suspendedDurationSeconds": 600,
                            "autoResumeEnabled": false}}"#,
    );
    let elsewhere = Arc::new(ScriptedTransport::new());
    elsewhere.answer("GetMicrovm", 404, r#"{"message": "no such MicroVM here"}"#);
    let script = DaemonScript::new();
    script.reply(
        200,
        r#"{"version": "0.1.0", "bootstrapped": true, "disk": null,
             "identity_degraded": false, "identity_repaired": true,
             "busy": false, "execs": 0}"#,
    );
    let seam = WindowInUsWest2 {
        west: ScriptedSeam {
            transport: Arc::clone(&west),
            clock: Arc::new(YieldingClock::default()),
        },
        elsewhere: ScriptedSeam {
            transport: Arc::clone(&elsewhere),
            clock: Arc::new(YieldingClock::default()),
        },
        sessions: ScriptedSessionSeam {
            script: Arc::clone(&script),
        },
    };
    let state = dir.0.to_string_lossy().to_string();
    let argv = [
        "microvm",
        "keepalive",
        "--name",
        "x",
        "--interval",
        "60",
        "--while-busy",
        "--state-dir",
        &state,
    ];
    let command = Cli::try_parse_from(argv).expect("parses").command;
    let (result, stderr) =
        dispatch_with_env(&seam, &command, full_infra(), ("AWS_REGION", "us-east-1")).await;
    let rendered = result.unwrap_or_else(|failure| panic!("refused: {}", failure.message));
    assert_eq!(rendered.data["idleWindowSec"], 600.0, "{stderr}");
    assert_eq!(
        west.called("GetMicrovm"),
        1,
        "one read in the record's region"
    );
    assert_eq!(elsewhere.called("GetMicrovm"), 0, "no read anywhere else");
}

/// **A torn record under a name is refused with the store's error, before any door (#251).**
///
/// `Names::lookup` reads a torn file as a record with empty fields. That's right for the
/// collision check it was written for, and wrong for a read that goes on to use the record:
/// an attach with a blank endpoint and token, a terminate of id `""`, or a tunnel that blames
/// missing identity material. Each row names the file, which is the caller's remedy.
///
/// **Falsification** (#251). Make `Names::resolve` read `self.store.get(name).unwrap_or(None)`, and
/// the `health` row goes red with `no VM named "x"`, a message that doesn't name `x.json`.
///
/// ```falsification
/// id = "names-torn-record-refused"
/// file = "crates/microvms-cli/src/ledger.rs"
/// replace = "let found = self.store.get(name)?;"
/// with = "let found = self.store.get(name).unwrap_or(None);"
/// message = "health: no VM named \"x\""
/// ```
#[tokio::test]
async fn a_torn_record_under_a_name_is_refused_before_any_door() {
    use clap::Parser as _;
    let dir = TempDir::new("name-torn");
    std::fs::create_dir_all(dir.0.join("names")).expect("mkdir");
    std::fs::write(
        dir.0.join("names").join("x.json"),
        b"{\"name\": \"x\", \"micro",
    )
    .expect("writes a torn record");
    let state = dir.0.to_string_lossy().to_string();
    let rows: Vec<Vec<&str>> = vec![
        vec!["health", "--name", "x"],
        vec!["shell", "--name", "x"],
        vec!["tunnel", "18080:8080", "--name", "x", "--verify-identity"],
        vec!["terminate", "x"],
    ];
    for row in rows {
        let mut argv = vec!["microvm"];
        argv.extend(row.iter().copied());
        argv.extend(["--state-dir", &state]);
        let command = Cli::try_parse_from(&argv).expect("parses").command;
        let seam = RefusingSeam::new();
        let (result, _) =
            dispatch_with_env(&seam, &command, full_infra(), ("AWS_REGION", "us-east-1")).await;
        let failure = result.expect_err("a torn record is a refusal");
        assert_eq!(
            (failure.exit, seam.doors()),
            (Exit::Precondition, Vec::<Door>::new()),
            "{}: {}",
            row[0],
            failure.message
        );
        assert!(
            failure.message.contains("x.json"),
            "{}: {}",
            row[0],
            failure.message
        );
    }
}

/// **`agent-up` over a torn record under its name stays `ERR_NAME_TAKEN`, before any door
/// (#251).**
///
/// `agent-up` reads the name to choose between a fresh launch and a refresh, so its read is a
/// collision check, not a record read: a torn record is a name that's taken, as
/// `docs/reference/cli.md` documents for `--vm-name`. Every other `--name` read now goes
/// through `Names::resolve`, whose answer for the same file is `ERR_PRECONDITION`, so tidying
/// `up` onto it would change this exit and no other guard would notice.
///
/// **Falsification** (#251). Make `up` read `names.resolve(&args.vm_name, None)?` instead of
/// `names.lookup(&args.vm_name)` and this goes red with `left: (Precondition, [])`.
#[tokio::test]
async fn agent_up_over_a_torn_record_stays_name_taken_before_any_door() {
    use clap::Parser as _;
    let dir = TempDir::new("name-torn-agent-up");
    std::fs::create_dir_all(dir.0.join("names")).expect("mkdir");
    std::fs::write(
        dir.0.join("names").join("x.json"),
        b"{\"name\": \"x\", \"micro",
    )
    .expect("writes a torn record");
    let state = dir.0.to_string_lossy().to_string();
    let argv = [
        "microvm",
        "agent-up",
        "--vm-name",
        "x",
        "--state-dir",
        &state,
    ];
    let command = Cli::try_parse_from(argv).expect("parses").command;
    let seam = RefusingSeam::new();
    let (result, _) =
        dispatch_with_env(&seam, &command, full_infra(), ("AWS_REGION", "us-east-1")).await;
    let failure = result.expect_err("a torn record is a taken name");
    assert_eq!(
        (failure.exit, seam.doors()),
        (Exit::NameTaken, Vec::<Door>::new()),
        "agent-up: {}",
        failure.message
    );
    assert!(
        failure.message.contains("torn record"),
        "agent-up: {}",
        failure.message
    );
}

/// **An illegal name gets core's grammar answer, and a free one the not-found answer (#251).**
///
/// `a/b` can't be a name at all, so "no VM named" is the wrong reason; core's store answers
/// `ERR_INVALID_ARG` with the grammar's reason, which is what `Sandbox.from_name` says. A
/// legal name this state directory never registered is "no VM named" on every command, the
/// tunnel's identity read included, whose older answer blamed missing identity material.
///
/// **Falsification** (#251). Make `tunnel`'s free-name `else` return a "carries no identity
/// material" refusal instead of `unregistered_name` and the `--name free` tunnel row goes red
/// on its message.
#[tokio::test]
async fn an_illegal_name_gets_cores_answer_and_a_free_one_the_not_found_answer() {
    use clap::Parser as _;
    let dir = TempDir::new("name-illegal-free");
    let state = dir.0.to_string_lossy().to_string();
    let illegal = "is not a legal VM-name character";
    let rows: Vec<(Vec<&str>, Exit, &str)> = vec![
        (
            vec!["exec", "true", "--name", "a/b"],
            Exit::InvalidArg,
            illegal,
        ),
        (vec!["shell", "--name", "a/b"], Exit::InvalidArg, illegal),
        (
            vec!["tunnel", "18080:8080", "--name", "a/b", "--verify-identity"],
            Exit::InvalidArg,
            illegal,
        ),
        (
            vec![
                "tunnel",
                "18080:8080",
                "--name",
                "free",
                "--verify-identity",
            ],
            Exit::Precondition,
            "no VM named \"free\"",
        ),
    ];
    for (row, exit, text) in rows {
        let mut argv = vec!["microvm"];
        argv.extend(row.iter().copied());
        argv.extend(["--state-dir", &state]);
        let command = Cli::try_parse_from(&argv).expect("parses").command;
        let seam = RefusingSeam::new();
        let (result, _) =
            dispatch_with_env(&seam, &command, full_infra(), ("AWS_REGION", "us-east-1")).await;
        let failure = result.expect_err("refused");
        let shown = row.join(" ");
        assert_eq!(
            (failure.exit, seam.doors()),
            (exit, Vec::<Door>::new()),
            "{shown}: {}",
            failure.message
        );
        assert!(
            failure.message.contains(text),
            "{shown}: {}",
            failure.message
        );
    }
}

/// A record for the `names` guards, with both secrets set to a canary.
fn canaried(name: &str, microvm_id: &str) -> crate::ledger::NameRecord {
    crate::ledger::NameRecord {
        name: name.into(),
        microvm_id: microvm_id.into(),
        endpoint: format!("https://{microvm_id}.example"),
        agent_token: "tok-CANARY".into(),
        region: "us-east-1".into(),
        at: 1,
        identity_host_seed: Some("seed-CANARY".into()),
        identity_vm_public_key: Some("pin".into()),
        egress_posture: None,
    }
}

/// **`microvm names` lists the registry without its secrets (#267).** Local only: the seam
/// refuses every door, and none is entered.
///
/// **Falsification**: list each record's `to_json` instead of `redacted_json` and the token
/// and the seed are in the envelope.
#[tokio::test]
async fn names_lists_the_registry_without_its_secrets() {
    let dir = TempDir::new("names-list");
    let registry = crate::ledger::Names::new(&dir.0);
    for (name, id) in [("beta", "mvm-2"), ("alpha", "mvm-1")] {
        registry.register(&canaried(name, id)).expect("registers");
    }
    let seam = RefusingSeam::new();
    let command = Command::Names(crate::cli::NamesArgs {
        delete: Vec::new(),
        state_dir: Some(dir.0.clone()),
    });
    let (result, _) = dispatch_with(&seam, &command, full_infra()).await;
    let rendered = result.expect("a local read");
    assert_eq!(rendered.kind, "microvm.names");
    let listed: Vec<&str> = rendered.data["names"]
        .as_array()
        .expect("an array")
        .iter()
        .filter_map(|record| record["name"].as_str())
        .collect();
    assert_eq!(listed, ["alpha", "beta"], "sorted by name");
    assert_eq!(rendered.data["names"][0]["microvmId"], "mvm-1");
    let everything = format!(
        "{} {} {}",
        serde_json::Value::Object(rendered.data.clone()),
        rendered.text,
        rendered.dense_text
    );
    assert!(
        !everything.contains("CANARY"),
        "a listing printed a secret: {everything}"
    );
    assert!(seam.doors().is_empty(), "entered {:?}", seam.doors());
}

/// **`names --delete` removes a name whose VM is gone, and refuses one it doesn't hold
/// (#267).** The refusal comes before any delete, so the good name in the same call survives
/// it.
///
/// **Falsification**: skip the `registry.delete` call and `alpha` is still listed, its file
/// still on disk.
#[tokio::test]
async fn names_delete_removes_a_held_name_and_refuses_an_unknown_one() {
    let dir = TempDir::new("names-delete");
    let registry = crate::ledger::Names::new(&dir.0);
    for (name, id) in [("alpha", "mvm-1"), ("beta", "mvm-2")] {
        registry.register(&canaried(name, id)).expect("registers");
    }
    let seam = RefusingSeam::new();
    let names = |delete: &[&str]| {
        Command::Names(crate::cli::NamesArgs {
            delete: delete.iter().map(|name| name.to_string()).collect(),
            state_dir: Some(dir.0.clone()),
        })
    };

    let (result, _) = dispatch_with(&seam, &names(&["alpha", "nobody"]), full_infra()).await;
    let error = result.expect_err("nobody holds that name");
    assert_eq!(error.exit, Exit::Precondition, "{}", error.message);
    assert!(error.message.contains("\"nobody\""), "{}", error.message);
    let looked_in = dir.0.join("names").display().to_string();
    assert!(
        error.message.contains(&looked_in),
        "the refusal names the registry it read, {looked_in}: {}",
        error.message
    );
    assert!(
        registry.lookup("alpha").is_some(),
        "a refused call deleted nothing"
    );

    let (result, _) = dispatch_with(&seam, &names(&["alpha"]), full_infra()).await;
    let rendered = result.expect("alpha is held");
    assert_eq!(rendered.data["deleted"], serde_json::json!(["alpha"]));
    assert_eq!(rendered.data["names"][0]["name"], "beta");
    assert_eq!(rendered.data["names"].as_array().map(Vec::len), Some(1));
    assert!(registry.lookup("alpha").is_none(), "the record is gone");
    assert!(!registry.path_of("alpha").exists());

    let (result, _) = dispatch_with(&seam, &names(&["../x"]), full_infra()).await;
    assert_eq!(
        result.expect_err("not a name").exit,
        Exit::InvalidArg,
        "an illegal name is the grammar's refusal"
    );
}
