// SPDX-License-Identifier: Apache-2.0
//! The Gherkin behavior spec for BIND-6 through BIND-10, run against `Session::run_to_completion`.
//!
//! The scenarios live in `tests/features/run_to_completion.feature`, tagged with the
//! requirement each one verifies; this file is their step definitions and runner. It is a
//! `harness = false` test, so `cargo test` runs it on every CI system, and it writes a JUnit
//! report when `CUCUMBER_JUNIT` names a file (give that path absolutely).
//!
//! The daemon is the scripted one in `tests/sim_daemon/mod.rs`, and the runtime starts with
//! tokio's clock paused, so a scenario that waits out a deadline of minutes finishes at once
//! and its orderings are caused by the script rather than by timing. The live half of these
//! requirements is `conformance/run_rs.py`, against a real VM.

#[allow(dead_code)]
mod sim_daemon;

use std::io;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use cucumber::{World, WriterExt, cli, given, then, when, writer};
use microvms_core::protocol::exec::StartRequest;
use microvms_core::session::{CompletionOptions, ExecEvent, ExecResult, OutputSink};
use sim_daemon::{Ending, KillMode, Script, SimDaemon, StreamMode};

/// Flags `cargo test` forwards to every test binary, which cucumber's own parser refuses.
const LIBTEST_FLAGS: [&str; 6] = [
    "--exact",
    "--nocapture",
    "--quiet",
    "--test-threads",
    "--color",
    "--ignored",
];

#[derive(Debug, Default, World)]
struct Run {
    script: Script,
    timeout_sec: Option<f64>,
    grace: Option<Duration>,
    /// The chunks the callback received, joined.
    received: Arc<Mutex<Vec<u8>>>,
    result: Option<ExecResult>,
    log: Vec<String>,
}

impl Run {
    fn result(&self) -> &ExecResult {
        self.result
            .as_ref()
            .expect("a `When I run it to completion` step ran first")
    }
}

#[given(
    regex = r#"^a command that prints "([^"]*)" and exits with code (\d+) after (\d+) seconds$"#
)]
fn exits(run: &mut Run, stdout: String, code: i32, after: u64) {
    run.script.stdout = stdout;
    run.script.finishes_after = Some(Duration::from_secs(after));
    run.script.ending = Ending {
        exit_code: Some(code),
        ..Ending::default()
    };
}

#[given(regex = r#"^a command that prints "([^"]*)" and never finishes by itself$"#)]
fn outlives(run: &mut Run, stdout: String) {
    run.script.stdout = stdout;
    run.script.finishes_after = None;
}

#[given(regex = r"^a command that dies to signal (\d+) after (\d+) seconds$")]
fn dies(run: &mut Run, signal: i32, after: u64) {
    run.script.finishes_after = Some(Duration::from_secs(after));
    run.script.ending = Ending {
        signal: Some(signal),
        ..Ending::default()
    };
}

#[given(
    regex = r"^a command that the daemon's deadline ends with signal (\d+) after (\d+) seconds$"
)]
fn daemon_deadline(run: &mut Run, signal: i32, after: u64) {
    run.script.stdout = "until the deadline".into();
    run.script.finishes_after = Some(Duration::from_secs(after));
    run.script.ending = Ending {
        exit_code: None,
        signal: Some(signal),
        timed_out: true,
    };
}

#[given("the stream is cut after its output on every attach")]
fn cut(run: &mut Run) {
    run.script.stream = StreamMode::Cut;
}

#[given("the first ack fails")]
fn first_ack_fails(run: &mut Run) {
    run.script.ack_failures = 1;
}

#[given(regex = r"^a kill ends it after (\d+) seconds$")]
fn kill_ends(run: &mut Run, after: u64) {
    run.script.kill = KillMode::Ends(Duration::from_secs(after));
}

#[given("the kill request fails")]
fn kill_fails(run: &mut Run) {
    run.script.kill = KillMode::Fails;
}

#[given("its output was truncated at the cap")]
fn truncated(run: &mut Run) {
    run.script.truncated = true;
}

#[given(regex = r"^a timeout of (\d+) seconds and a client grace of (\d+) seconds$")]
fn deadline(run: &mut Run, timeout: u64, grace: u64) {
    run.timeout_sec = Some(timeout as f64);
    run.grace = Some(Duration::from_secs(grace));
}

async fn complete(run: &mut Run, with_callback: bool) {
    let daemon = SimDaemon::new(run.script.clone());
    let session = daemon.session();
    let request = StartRequest {
        exec_id: microvms_core::session::mint_exec_id(),
        command: vec!["bash".into(), "-c".into(), "the scripted command".into()],
        shell: false,
        cwd: None,
        env: Default::default(),
        user: None,
        group: None,
        timeout_sec: run.timeout_sec,
        stdin: false,
        reap_group_on_exit: false,
    };
    let mut options = CompletionOptions::default();
    if let Some(grace) = run.grace {
        options.client_grace = grace;
    }
    let sink: Option<OutputSink> = with_callback.then(|| {
        let received = Arc::clone(&run.received);
        Box::new(move |event| {
            if let ExecEvent::Output { data, .. } = event {
                received
                    .lock()
                    .expect("unpoisoned")
                    .extend_from_slice(&data);
            }
            Box::pin(std::future::ready(std::ops::ControlFlow::Continue(())))
                as futures_util::future::BoxFuture<'static, _>
        }) as OutputSink
    });
    let result = session
        .run_to_completion(request, options, sink)
        .await
        .expect("run_to_completion returns a result on every scripted path");
    run.result = Some(result);
    run.log = daemon.log();
}

#[when("I run it to completion with an output callback")]
async fn run_streaming(run: &mut Run) {
    complete(run, true).await;
}

#[when("I run it to completion")]
async fn run_polling(run: &mut Run) {
    complete(run, false).await;
}

#[then(regex = r#"^the callback received "([^"]*)"$"#)]
fn callback_received(run: &mut Run, expected: String) {
    let received = run.received.lock().expect("unpoisoned").clone();
    assert_eq!(String::from_utf8_lossy(&received), expected);
}

#[then(regex = r#"^the result's stdout is "([^"]*)"$"#)]
fn stdout_is(run: &mut Run, expected: String) {
    assert_eq!(run.result().stdout(), expected);
}

#[then(regex = r"^the result's POSIX exit code is (\d+)$")]
fn posix_is(run: &mut Run, expected: i32) {
    assert_eq!(
        run.result().posix_exit_code(),
        Some(expected),
        "{:?}",
        run.result()
    );
}

#[then("the result has no notes")]
fn no_notes(run: &mut Run) {
    assert!(
        run.result().notes().is_empty(),
        "{:?}",
        run.result().notes()
    );
}

#[then(regex = r#"^the result has a note containing "([^"]*)"$"#)]
fn note_containing(run: &mut Run, needle: String) {
    let notes = run.result().notes();
    assert!(
        notes.iter().any(|note| note.contains(&needle)),
        "no note contains {needle:?}: {notes:?}"
    );
}

#[then("the result is synthesized")]
fn synthesized(run: &mut Run) {
    assert!(run.result().synthesized(), "{:?}", run.result());
}

#[then("the result is not synthesized")]
fn not_synthesized(run: &mut Run) {
    assert!(!run.result().synthesized(), "{:?}", run.result());
}

fn saw(run: &Run, suffix: &str) -> usize {
    run.log.iter().filter(|line| line.ends_with(suffix)).count()
}

fn polls(run: &Run) -> usize {
    run.log
        .iter()
        .filter(|line| {
            line.starts_with("GET ") && !line.ends_with("/stream") && line.matches('/').count() == 3
        })
        .count()
}

#[then("the daemon saw no poll")]
fn no_poll(run: &mut Run) {
    assert_eq!(polls(run), 0, "{:?}", run.log);
}

#[then("the daemon saw a poll")]
fn a_poll(run: &mut Run) {
    assert!(polls(run) > 0, "{:?}", run.log);
}

#[then("the daemon saw no kill")]
fn no_kill(run: &mut Run) {
    assert_eq!(saw(run, "/kill"), 0, "{:?}", run.log);
}

#[then("the daemon saw a kill")]
fn a_kill(run: &mut Run) {
    assert_eq!(saw(run, "/kill"), 1, "{:?}", run.log);
}

#[then("the exec was acked exactly once")]
fn acked_once(run: &mut Run) {
    // A failed ack still reached the daemon, so count the ones the script let through: the
    // result carries the acked output, and the log has one more ack per scripted failure.
    let acks = saw(run, "/ack");
    assert_eq!(acks, 1 + run.script.ack_failures as usize, "{:?}", run.log);
}

#[then("the kill was sent before the last ack")]
fn kill_before_ack(run: &mut Run) {
    let kill = run
        .log
        .iter()
        .position(|line| line.ends_with("/kill"))
        .expect("a kill was sent");
    let ack = run
        .log
        .iter()
        .rposition(|line| line.ends_with("/ack"))
        .expect("an ack was sent");
    assert!(kill < ack, "{:?}", run.log);
}

#[tokio::main(flavor = "current_thread", start_paused = true)]
async fn main() {
    let feature = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/features/run_to_completion.feature"
    );
    // `cargo test <filter>` passes a libtest filter to every test target. A filter naming
    // something else selects nothing here, as libtest would.
    let args: Vec<String> = std::env::args().skip(1).collect();
    let filters: Vec<&str> = args
        .iter()
        .map(String::as_str)
        .filter(|arg| !arg.starts_with('-'))
        .collect();
    if !filters.is_empty()
        && !filters
            .iter()
            .any(|filter| "bdd_run_to_completion".contains(filter))
    {
        return;
    }
    let libtest = !filters.is_empty()
        || args
            .iter()
            .any(|arg| LIBTEST_FLAGS.iter().any(|flag| arg.starts_with(flag)));
    macro_rules! run {
        ($cucumber:expr) => {
            if libtest {
                $cucumber
                    .with_cli(cli::Opts::<_, _, _, cli::Empty>::default())
                    .run_and_exit(feature)
                    .await
            } else {
                $cucumber.run_and_exit(feature).await
            }
        };
    }
    // One scenario at a time: the paused clock auto-advances only when every task is idle, so
    // scenarios sharing it would advance each other's time.
    let cucumber = Run::cucumber().max_concurrent_scenarios(1);
    match std::env::var_os("CUCUMBER_JUNIT") {
        Some(path) => {
            let report = std::fs::File::create(&path).expect("the JUnit report file");
            run!(
                cucumber.with_writer(
                    writer::Basic::raw(io::stdout(), writer::Coloring::Never, 0)
                        .summarized()
                        .tee::<Run, _>(writer::JUnit::for_tee(report, 0))
                        .normalized(),
                )
            )
        }
        None => run!(cucumber),
    }
}
