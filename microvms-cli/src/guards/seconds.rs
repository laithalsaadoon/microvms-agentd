// SPDX-License-Identifier: Apache-2.0
//! #268: a seconds flag that isn't a duration.

#![cfg(test)]

use std::sync::Arc;

use microvms_core::testing::YieldingClock;

use super::support::{
    DaemonScript, RefusingSeam, STARTED_BODY, ScriptedSeam, ScriptedTransport, SyncSeam, TempDir,
    against_daemon, dispatch_with, full_infra, poll_body, sync_launch_script,
};
use crate::cli::Cli;
use crate::exit::Exit;

/// What one row of the seconds-flag guard records a dispatch against.
#[derive(Clone)]
enum SecondsRecorder {
    /// A scripted daemon, for the attached commands (`exec`, `sync`).
    Daemon(Arc<DaemonScript>),
    /// A scripted control plane, for `run`, `suspend` and `resume`.
    Plane(Arc<ScriptedTransport>),
    /// A seam whose every door refuses, for `cost`, which should enter none.
    Refusing(Arc<RefusingSeam>),
    /// A scripted control plane and the daemon behind the session it launches, for `run
    /// --exec`, which waits on its exec only after the launch.
    Launch(Arc<ScriptedTransport>, Arc<DaemonScript>),
}

impl SecondsRecorder {
    fn calls(&self) -> Vec<String> {
        match self {
            Self::Daemon(script) => script.paths(),
            Self::Plane(transport) => transport.calls(),
            Self::Launch(transport, script) => transport
                .calls()
                .into_iter()
                .chain(script.paths())
                .collect(),
            Self::Refusing(seam) => seam
                .doors()
                .iter()
                .map(|door| door.as_str().into())
                .collect(),
        }
    }
}

/// How a bad row ended: refused by the parser, or dispatched to its recorder.
enum SecondsOutcome {
    Refused(clap::Error),
    Answered(String),
}

/// Parses `argv` and, if the parse succeeds, dispatches it against `recorder`, on a thread of
/// its own with its own runtime so a panic in either step is caught and the recorder can still
/// be read afterwards.
fn parse_and_dispatch(
    argv: Vec<String>,
    recorder: &SecondsRecorder,
) -> Result<SecondsOutcome, String> {
    use clap::Parser as _;
    let recorder = recorder.clone();
    std::thread::spawn(move || {
        let command = match Cli::try_parse_from(&argv) {
            Ok(cli) => cli.command,
            Err(error) => return SecondsOutcome::Refused(error),
        };
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("a runtime");
        let result = runtime.block_on(async {
            match &recorder {
                SecondsRecorder::Daemon(script) => against_daemon(script, &command).await.0,
                SecondsRecorder::Plane(transport) => {
                    let seam = ScriptedSeam {
                        transport: Arc::clone(transport),
                        clock: Arc::new(YieldingClock::default()),
                    };
                    dispatch_with(&seam, &command, full_infra()).await.0
                }
                SecondsRecorder::Refusing(seam) => {
                    dispatch_with(seam.as_ref(), &command, full_infra()).await.0
                }
                SecondsRecorder::Launch(transport, script) => {
                    let seam = SyncSeam {
                        transport: Arc::clone(transport),
                        clock: Arc::new(YieldingClock::default()),
                        daemon: Arc::clone(script),
                    };
                    dispatch_with(&seam, &command, full_infra()).await.0
                }
            }
        });
        SecondsOutcome::Answered(match result {
            Ok(_) => "ok".into(),
            Err(failure) => failure.code().into(),
        })
    })
    .join()
    .map_err(|panic| {
        panic
            .downcast_ref::<String>()
            .cloned()
            .or_else(|| panic.downcast_ref::<&str>().map(|text| (*text).to_string()))
            .unwrap_or_default()
    })
}

/// A daemon for a sync whose guest manifest orders one deletion, so a dispatched sync reaches
/// the `rm` exec whose budget is the flag, and that exec exits and acks.
fn seconds_sync_daemon(tree: &std::path::Path) -> Arc<DaemonScript> {
    let mut remote = crate::sync::manifest(tree).expect("the tree manifests");
    remote.files.insert("removed.txt".into(), "1".repeat(64));
    let remote = String::from_utf8(serde_json::to_vec(&remote).expect("serializes")).expect("utf8");
    let script = DaemonScript::new();
    script
        .reply(200, &remote)
        .reply(200, STARTED_BODY)
        .reply(200, &poll_body("exited", "0", "", false))
        .reply(200, &poll_body("acked", "0", "", false))
        .reply(200, "");
    script
}

/// **#268: a seconds flag that isn't a duration is refused before any call.**
///
/// `exec`, `run`, `suspend`, `resume` and `sync` take `--timeout` in seconds, and `cost
/// --compare` takes `--hold-sec`. Each used to convert with `Duration::from_secs_f64(x.max(0.0))`
/// inside the handler, after the work had started: `inf` and `1e300` panicked after the exec
/// start, the launch, the `SuspendMicrovm` or the `ResumeMicrovm` (a `run` that panicked left its
/// VM billing with no teardown), and `NaN` or `=-5` silently meant zero. Each row here drives
/// the real argv through the real parser and, if it parses, through the real dispatcher against
/// a recorder, so a refusal is only a pass when clap gave it for the value and nothing reached a
/// door, a daemon or the control plane.
///
/// Every value goes in the `=` form, because a bare `-5` is a clap error for a reason of its own
/// (clap reads it as a flag). Each row has a valid twin, the same argv with the flag's default in
/// the bad value's place, which must parse: `from_parse_error` maps every clap error to
/// `ERR_INVALID_ARG`, so without the twin a row could pass on a parse failure it has for some
/// other reason, such as a renamed subcommand. The `1e300` row's twin is `0`, since zero stays
/// legal.
///
/// **Falsification**: `guards/faults.toml` entries `cli-seconds-flag-panics` (restore the old
/// `from_secs_f64(seconds.max(0.0))` inside `cli::parse_seconds`, so each bad row panics) and
/// `cli-seconds-flag-clamps` (turn the refusal into a silent zero, so each bad row is
/// dispatched and answers after its calls).
#[test]
fn a_seconds_flag_that_is_not_a_duration_is_refused_before_any_call() {
    use clap::Parser as _;
    let state = TempDir::new("seconds-flag-state");
    let state_dir = state.0.to_string_lossy().to_string();
    let tree = TempDir::new("seconds-flag-tree");
    std::fs::write(tree.0.join("same.txt"), b"unchanged").expect("writes");
    let tree_dir = tree.0.to_string_lossy().to_string();

    let attached = |command: &str| -> Vec<String> {
        [
            "microvm",
            command,
            "--endpoint",
            "https://mvm-1.example",
            "--agent-token",
            "t",
            "--microvm-id",
            "mvm-1",
            "--state-dir",
            &state_dir,
            "--region",
            "us-east-1",
        ]
        .map(String::from)
        .to_vec()
    };
    let exec = || {
        let mut argv = attached("exec");
        argv.extend(["--exec-id", "x-1", "true"].map(String::from));
        argv
    };
    let sync = || {
        let mut argv = attached("sync");
        argv.push(tree_dir.clone());
        argv
    };
    let run = || {
        [
            "microvm",
            "run",
            "--image",
            "arn:aws:lambda:us-east-1:123456789012:microvm-image:img",
            "--no-config",
            "--state-dir",
            &state_dir,
            "--region",
            "us-east-1",
        ]
        .map(String::from)
        .to_vec()
    };
    let lifecycle = |command: &str| {
        [
            "microvm",
            command,
            "mvm-live",
            "--state-dir",
            &state_dir,
            "--region",
            "us-east-1",
        ]
        .map(String::from)
        .to_vec()
    };
    let cost = || ["microvm", "cost", "--compare"].map(String::from).to_vec();

    // A daemon that would carry an exec to its first poll, so a dispatched exec answers (a zero
    // wait times out on the running poll) rather than dying on the script.
    let exec_daemon = || {
        let script = DaemonScript::new();
        script.reply(200, STARTED_BODY).reply(200, STARTED_BODY);
        SecondsRecorder::Daemon(script)
    };
    let sync_daemon = || SecondsRecorder::Daemon(seconds_sync_daemon(&tree.0));
    let plane = || {
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
            .answer("SuspendMicrovm", 200, "{}")
            .answer("ResumeMicrovm", 200, "{}")
            .answer("RunMicrovm", 400, r#"{"message": "scripted stop"}"#);
        SecondsRecorder::Plane(transport)
    };
    let refusing = || SecondsRecorder::Refusing(Arc::new(RefusingSeam::new()));

    type Argv<'a> = Box<dyn Fn() -> Vec<String> + 'a>;
    type Recorder<'a> = Box<dyn Fn() -> SecondsRecorder + 'a>;
    let rows: Vec<(&str, Argv, &str, &str, &str, Recorder)> = vec![
        (
            "exec",
            Box::new(exec),
            "--timeout",
            "inf",
            "300",
            Box::new(exec_daemon),
        ),
        (
            "exec",
            Box::new(exec),
            "--timeout",
            "NaN",
            "300",
            Box::new(exec_daemon),
        ),
        (
            "exec",
            Box::new(exec),
            "--timeout",
            "-5",
            "300",
            Box::new(exec_daemon),
        ),
        (
            "exec",
            Box::new(exec),
            "--timeout",
            "1e300",
            "0",
            Box::new(exec_daemon),
        ),
        (
            "run",
            Box::new(run),
            "--timeout",
            "inf",
            "300",
            Box::new(plane),
        ),
        (
            "suspend",
            Box::new(move || lifecycle("suspend")),
            "--timeout",
            "inf",
            "300",
            Box::new(plane),
        ),
        (
            "resume",
            Box::new(move || lifecycle("resume")),
            "--timeout",
            "inf",
            "300",
            Box::new(plane),
        ),
        (
            "sync",
            Box::new(sync),
            "--timeout",
            "inf",
            "60",
            Box::new(sync_daemon),
        ),
        (
            "cost",
            Box::new(cost),
            "--hold-sec",
            "inf",
            "3600",
            Box::new(refusing),
        ),
    ];

    let mut misses = Vec::new();
    for (command, argv, flag, bad, valid, recorder) in &rows {
        let label = format!("{command} {flag}={bad}");

        let mut twin = argv();
        twin.push(format!("{flag}={valid}"));
        if let Err(error) = Cli::try_parse_from(&twin) {
            misses.push(format!(
                "{command} {flag}={valid} (the valid twin) didn't parse: {}",
                error.render()
            ));
            continue;
        }

        let mut bad_argv = argv();
        bad_argv.push(format!("{flag}={bad}"));
        let recorder = recorder();
        match parse_and_dispatch(bad_argv, &recorder) {
            Ok(SecondsOutcome::Refused(error)) => {
                let rendering = error.render().to_string();
                if error.kind() != clap::error::ErrorKind::ValueValidation
                    || !rendering.contains(flag)
                    || !rendering.contains(bad)
                {
                    misses.push(format!(
                        "{label}: refused as {:?}, not as the value: {rendering}",
                        error.kind()
                    ));
                } else if crate::exit::from_parse_error(&error).exit != Exit::InvalidArg {
                    misses.push(format!("{label}: refused, but not as ERR_INVALID_ARG"));
                }
            }
            Ok(SecondsOutcome::Answered(code)) => misses.push(format!(
                "{label}: answered {code} after {:?}",
                recorder.calls()
            )),
            Err(payload) => misses.push(format!(
                "{label}: panicked ({payload}) after {:?}",
                recorder.calls()
            )),
        }
    }
    assert!(misses.is_empty(), "{}", misses.join("\n"));
}

/// **#268: an accepted seconds flag reaches the wire as typed, and a huge one waits.**
///
/// The other half of the refusal guard above. A value the parser accepts has to arrive where
/// it's used unchanged: `sync --timeout` is the one seconds flag that reaches the wire, as the
/// in-guest `rm`'s `timeout_sec`, so a fraction has to go out as that fraction and not as zero
/// (which the daemon refuses with 400 after the upload), and a whole figure has to go out at
/// all. And a value past what the clock can add to now, about 9.2e18 seconds, still has to
/// wait: core's deadlines saturate (`session::deadline_after`), where `Instant + Duration`
/// used to panic after the exec, the `rm` or `run`'s launch had started. `run` then has to tear
/// its VM down.
///
/// **Falsification**: `guards/faults.toml` entries `cli-huge-timeout-panics` and
/// `cli-huge-run-timeout-panics` (restore the unchecked add in `deadline_after`; the exec and
/// `run` rows panic), `cli-sync-rm-deadline-dropped` (send no `timeout_sec`) and
/// `cli-sync-rm-deadline-truncated` (send whole seconds).
#[test]
fn an_accepted_seconds_flag_reaches_the_wire_as_typed_and_a_huge_one_waits() {
    let state = TempDir::new("seconds-accepted-state");
    let state_dir = state.0.to_string_lossy().to_string();
    let tree = TempDir::new("seconds-accepted-tree");
    std::fs::write(tree.0.join("same.txt"), b"unchanged").expect("writes");
    let tree_dir = tree.0.to_string_lossy().to_string();
    let argv = |command: &str, tail: &[&str]| -> Vec<String> {
        [
            "microvm",
            command,
            "--endpoint",
            "https://mvm-1.example",
            "--agent-token",
            "t",
            "--microvm-id",
            "mvm-1",
            "--state-dir",
            &state_dir,
            "--region",
            "us-east-1",
        ]
        .iter()
        .chain(tail)
        .map(|part| (*part).to_string())
        .collect()
    };

    let mut misses = Vec::new();
    let mut outcome =
        |label: &str, argv: Vec<String>, recorder: &SecondsRecorder| match parse_and_dispatch(
            argv, recorder,
        ) {
            Ok(SecondsOutcome::Answered(code)) if code == "ok" => true,
            Ok(SecondsOutcome::Answered(code)) => {
                misses.push(format!(
                    "{label}: answered {code} after {:?}",
                    recorder.calls()
                ));
                false
            }
            Ok(SecondsOutcome::Refused(error)) => {
                misses.push(format!("{label}: refused: {}", error.render()));
                false
            }
            Err(payload) => {
                misses.push(format!(
                    "{label}: panicked ({payload}) after {:?}",
                    recorder.calls()
                ));
                false
            }
        };

    let script = DaemonScript::new();
    script
        .reply(200, STARTED_BODY)
        .reply(200, &poll_body("exited", "0", "", false))
        .reply(200, &poll_body("acked", "0", "", false));
    outcome(
        "exec --timeout=1e19",
        argv("exec", &["--exec-id", "x-1", "--timeout=1e19", "true"]),
        &SecondsRecorder::Daemon(script),
    );

    // `run` waits on its exec after the launch, so a panic there left the VM billing with no
    // teardown. The row has to answer, and the teardown has to go out.
    const HEALTH: &str = r#"{"version": "0.1.0", "bootstrapped": true, "disk": null,
                             "identity_degraded": false, "identity_repaired": true}"#;
    let launch = sync_launch_script();
    let daemon = DaemonScript::new();
    daemon
        .reply(200, HEALTH)
        .reply(200, STARTED_BODY)
        .reply(200, &poll_body("exited", "0", "", false))
        .reply(200, &poll_body("acked", "0", "", false))
        .reply(200, HEALTH);
    let run_argv = [
        "microvm",
        "run",
        "--image",
        "arn:aws:lambda:us-east-1:123456789012:microvm-image:img",
        "--no-config",
        "--state-dir",
        &state_dir,
        "--region",
        "us-east-1",
        "--exec",
        "true",
        "--timeout=1e19",
    ]
    .map(String::from)
    .to_vec();
    let run_answered = outcome(
        "run --exec true --timeout=1e19",
        run_argv,
        &SecondsRecorder::Launch(Arc::clone(&launch), daemon),
    );

    let mut sent = Vec::new();
    for (value, wire) in [("60", 60.0), ("0.5", 0.5), ("1e19", 1e19)] {
        let label = format!("sync --timeout={value}");
        let script = seconds_sync_daemon(&tree.0);
        let timeout = format!("--timeout={value}");
        let recorder = SecondsRecorder::Daemon(Arc::clone(&script));
        if !outcome(&label, argv("sync", &[&timeout, &tree_dir]), &recorder) {
            continue;
        }
        let start = script
            .requests()
            .into_iter()
            .find(|request| request.path == "/v1/exec/start");
        let got = start.map(|start| {
            let body: serde_json::Value =
                serde_json::from_slice(&start.body).expect("the start body is JSON");
            body["timeout_sec"].clone()
        });
        sent.push((label, got, wire));
    }
    for (label, got, wire) in sent {
        match got {
            Some(got) if got.as_f64() == Some(wire) => {}
            Some(got) => misses.push(format!(
                "{label}: the rm's timeout_sec was {got}, not {wire:?}"
            )),
            None => misses.push(format!("{label}: no rm started")),
        }
    }
    if run_answered && !launch.calls().iter().any(|call| call == "TerminateMicrovm") {
        misses.push(format!(
            "run --exec true --timeout=1e19: no teardown after {:?}",
            launch.calls()
        ));
    }
    assert!(misses.is_empty(), "{}", misses.join("\n"));
}
