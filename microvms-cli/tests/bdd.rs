// SPDX-License-Identifier: Apache-2.0
//! The Gherkin behavior spec for CLI-7, CLI-8, and CLI-9, run against the real binary.
//!
//! The scenarios live in `tests/features/*.feature`, tagged with the requirement each one
//! verifies; this file is their step definitions and runner. It is a `harness = false` test,
//! so `cargo test` runs it on every CI system, and it writes a JUnit report when
//! `CUCUMBER_JUNIT` names a file. Give that path absolutely: `cargo test` runs this binary from
//! the package directory, so a relative one resolves under `microvms-cli/`.
//!
//! # Closing a reader deterministically
//!
//! "Closed after 0 bytes" hands the child the write end of a pipe whose read end is already
//! gone, so the first write fails whatever the timing. "Closed after N bytes" reads N bytes
//! and then drops the read end; the commands used there write more than a pipe buffer holds,
//! so the child is still writing when the reader leaves. Closing the descriptor instead
//! (`>&-`) would not reproduce #216: a write to a closed descriptor is `EBADF`, which std's
//! stdout treats as success.
//!
//! # Live scenarios
//!
//! Scenarios tagged `@live` need a running VM, which the shipped binary reaches only through
//! the AWS control plane. They run when `MICROVM_BDD_ATTACH` holds the attach flags as a JSON
//! array (`["--endpoint", …, "--agent-token", …, "--microvm-id", …, "--region", …]`), which
//! `conformance/run_rs.py` sets for the live suite's kept VM, with the caller's AWS
//! credentials inherited. Without it they are left out, and the runner says so on stderr.

#[allow(dead_code)]
mod support;

use std::io::{self, Read};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::time::{Duration, Instant};

use cucumber::{World, WriterExt, cli, given, then, when, writer};

/// How long one invocation may take before the step fails. A command blocked writing into a
/// pipe nobody reads is the hang this bounds.
const DEADLINE: Duration = Duration::from_secs(60);

/// The exit table's rows, 0 through 16 (`EXIT_TABLE` in `src/exit.rs`).
const TABLE_ROWS: i32 = 17;

/// Rust's runtime exits 101 after a panic, outside the exit table.
const PANIC_STATUS: i32 = 101;

/// The environment variable that turns the `@live` scenarios on (see the module docs).
const LIVE_ATTACH: &str = "MICROVM_BDD_ATTACH";

/// A ticker that outlives any bound here, so a stream that stops did so because its reader
/// left and not because the command ended.
const TICKER: &str = r#"i=0; while [ "$i" -lt 600 ]; do echo "tick-$i"; i=$((i+1)); sleep 1; done"#;

#[derive(Debug, Default, World)]
struct Cli {
    /// The exit code, or `None` when the child died by a signal.
    code: Option<i32>,
    /// The signal that killed the child, where the platform reports one.
    signal: Option<i32>,
    /// Everything the child wrote to stderr, when stderr was read.
    stderr: Option<String>,
    /// The attach flags of a live VM, from [`LIVE_ATTACH`].
    attach: Option<Vec<String>>,
    /// The exec a live scenario started, killed after the scenario whatever its outcome.
    exec_id: Option<String>,
    /// How long the last invocation took.
    elapsed: Option<Duration>,
}

/// The arguments after `microvm` in a step's command text.
fn args_of(command: &str) -> Vec<String> {
    let mut words = command.split_whitespace();
    assert_eq!(
        words.next(),
        Some("microvm"),
        "a scenario's command starts with `microvm`: {command}"
    );
    words.map(str::to_string).collect()
}

/// `microvm` with `args`, a cleared environment, and a HOME no ledger write can reach.
fn command(args: &[String]) -> Command {
    let mut command = Command::new(support::binary());
    command
        .args(args)
        .env_clear()
        .env("HOME", "/nonexistent-microvm-test-home")
        // No credential lookup on this machine may reach instance metadata.
        .env("AWS_EC2_METADATA_DISABLED", "true")
        .stdin(Stdio::null());
    command
}

/// The write end of a pipe whose read end is already closed.
fn closed_pipe() -> Stdio {
    let (reader, writer) = io::pipe().expect("an anonymous pipe");
    drop(reader);
    Stdio::from(writer)
}

/// Reads a stream to its end on a thread, so a full pipe never blocks the child.
fn drain(stream: impl Read + Send + 'static) -> std::thread::JoinHandle<String> {
    std::thread::spawn(move || {
        let mut stream = stream;
        let mut bytes = Vec::new();
        let _ = stream.read_to_end(&mut bytes);
        String::from_utf8_lossy(&bytes).into_owned()
    })
}

/// Waits for the child within [`DEADLINE`], killing it and failing the step past that.
fn wait(child: &mut Child) -> ExitStatus {
    let started = Instant::now();
    loop {
        if let Some(status) = child.try_wait().expect("try_wait") {
            return status;
        }
        if started.elapsed() > DEADLINE {
            let _ = child.kill();
            panic!("the CLI was still running after {DEADLINE:?} with a closed reader");
        }
        std::thread::sleep(Duration::from_millis(20));
    }
}

impl Cli {
    fn record(&mut self, status: ExitStatus, stderr: Option<String>) {
        self.code = status.code();
        #[cfg(unix)]
        {
            use std::os::unix::process::ExitStatusExt;
            self.signal = status.signal();
        }
        self.stderr = stderr;
    }
}

#[when(expr = "I run {string} with stdout closed after {int} bytes")]
fn stdout_closed_after(world: &mut Cli, text: String, bytes: usize) {
    let mut command = command(&args_of(&text));
    command.stderr(Stdio::piped());
    if bytes == 0 {
        command.stdout(closed_pipe());
    } else {
        command.stdout(Stdio::piped());
    }
    let mut child = command.spawn().expect("the microvm binary spawns");
    drop(command);
    let stderr = drain(child.stderr.take().expect("piped stderr"));
    if let Some(mut stdout) = child.stdout.take() {
        let mut head = vec![0; bytes];
        let _ = stdout.read_exact(&mut head);
        // The reader leaves here, as `head -c N` would.
        drop(stdout);
    }
    let status = wait(&mut child);
    world.record(status, Some(stderr.join().expect("stderr reader")));
}

#[when(expr = "I run {string} with stderr closed")]
fn stderr_closed(world: &mut Cli, text: String) {
    let mut command = command(&args_of(&text));
    command.stdout(Stdio::piped()).stderr(closed_pipe());
    let mut child = command.spawn().expect("the microvm binary spawns");
    drop(command);
    let stdout = drain(child.stdout.take().expect("piped stdout"));
    let status = wait(&mut child);
    let _ = stdout.join();
    world.record(status, None);
}

#[when(expr = "I run {string} with stdout and stderr closed")]
fn both_closed(world: &mut Cli, text: String) {
    let mut command = command(&args_of(&text));
    command.stdout(closed_pipe()).stderr(closed_pipe());
    let mut child = command.spawn().expect("the microvm binary spawns");
    drop(command);
    let status = wait(&mut child);
    world.record(status, None);
}

// CLI-7: an exit-table row, never a signal death or a panic's 101. CLI-8: the row is the
// command's own outcome, which the scenario names.
#[then(expr = "the CLI exited with code {int}")]
fn exited_with(world: &mut Cli, expected: i32) {
    assert_eq!(
        world.signal, None,
        "CLI-7: the CLI died by signal {:?}; stderr: {:?}",
        world.signal, world.stderr
    );
    let code = world
        .code
        .unwrap_or_else(|| panic!("CLI-7: no exit code; stderr: {:?}", world.stderr));
    assert!(
        (0..TABLE_ROWS).contains(&code),
        "CLI-7: exit {code} is not a row of the exit table (101 is a panic); stderr: {:?}",
        world.stderr
    );
    assert_eq!(
        code, expected,
        "CLI-8: the exit code is the command's outcome; stderr: {:?}",
        world.stderr
    );
}

// CLI-7.
#[then("the CLI did not panic")]
fn did_not_panic(world: &mut Cli) {
    assert_ne!(world.code, Some(PANIC_STATUS), "CLI-7: exit 101 is a panic");
    if let Some(stderr) = &world.stderr {
        assert!(
            !stderr.contains("panicked"),
            "CLI-7: the CLI panicked: {stderr}"
        );
    }
}

// ── live: CLI-9 through a real VM ───────────────────────────────────────────

/// `microvm` with `args` and the caller's environment, which carries the AWS credentials the
/// control plane needs to mint the proxy token.
fn live_command(args: &[&str], attach: &[String]) -> Command {
    let mut command = Command::new(support::binary());
    command.args(args).args(attach).stdin(Stdio::null());
    command
}

/// The `data` object of the envelope a `--json` invocation printed.
fn envelope_data(stdout: &[u8]) -> serde_json::Value {
    let envelope: serde_json::Value =
        serde_json::from_slice(stdout).expect("one JSON envelope on stdout");
    envelope["data"].clone()
}

#[given(expr = "a VM attached through MICROVM_BDD_ATTACH")]
fn live_vm(world: &mut Cli) {
    let raw = std::env::var(LIVE_ATTACH)
        .unwrap_or_else(|_| panic!("{LIVE_ATTACH} is unset; the runner leaves @live out then"));
    let attach: Vec<String> =
        serde_json::from_str(&raw).expect("MICROVM_BDD_ATTACH is a JSON array of flags");
    assert!(
        attach.iter().any(|flag| flag == "--microvm-id"),
        "MICROVM_BDD_ATTACH names no --microvm-id"
    );
    world.attach = Some(attach);
}

// CLI-9: the reader leaves after the first chunk, as `microvm exec … --stream | head -c N`.
#[when("I stream a ticker exec and close stdout after the first chunk")]
fn stream_and_close(world: &mut Cli) {
    let attach = world.attach.clone().expect("the Given step attached a VM");
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|since| since.subsec_nanos())
        .unwrap_or_default();
    let exec_id = format!("bdd-closed-output-{}-{nanos:x}", std::process::id());
    world.exec_id = Some(exec_id.clone());
    let mut command = live_command(
        &["--quiet", "exec", TICKER, "--stream", "--exec-id", &exec_id],
        &attach,
    );
    command.stdout(Stdio::piped()).stderr(Stdio::piped());
    let started = Instant::now();
    let mut child = command.spawn().expect("the microvm binary spawns");
    let stderr = drain(child.stderr.take().expect("piped stderr"));
    let mut stdout = child.stdout.take().expect("piped stdout");
    let mut chunk = [0_u8; 4096];
    let read = stdout
        .read(&mut chunk)
        .expect("the first chunk of the stream");
    assert!(read > 0, "the stream ended before its first chunk");
    // The reader leaves here, with the ticker still running in the VM.
    drop(stdout);
    let status = wait(&mut child);
    world.elapsed = Some(started.elapsed());
    world.record(status, Some(stderr.join().expect("stderr reader")));
}

// CLI-9: the stream stopped promptly rather than running out the ticker's ten minutes.
#[then(expr = "the CLI exited with code {int} within {int} seconds")]
fn exited_within(world: &mut Cli, expected: i32, seconds: u64) {
    exited_with(world, expected);
    let elapsed = world.elapsed.expect("a timed invocation");
    assert!(
        elapsed < Duration::from_secs(seconds),
        "CLI-9: the stream took {elapsed:?} to stop after its reader left"
    );
}

// CLI-9: the note on stderr names the exec and the command that reattaches to it.
#[then("stderr names the exec id and how to reattach")]
fn names_the_exec(world: &mut Cli) {
    let exec_id = world.exec_id.as_deref().expect("a started exec");
    let stderr = world.stderr.as_deref().unwrap_or_default();
    assert!(
        stderr.contains(exec_id) && stderr.contains(&format!("--exec-id {exec_id}")),
        "CLI-9: stderr does not name {exec_id} with a reattach command: {stderr}"
    );
}

// CLI-9: the CLI detached; it did not kill the exec it stopped streaming.
#[then("the exec is still running on the daemon")]
fn still_running(world: &mut Cli) {
    let exec_id = world.exec_id.clone().expect("a started exec");
    let attach = world.attach.clone().expect("an attached VM");
    let output = live_command(&["--json", "--quiet", "exec", "--poll", &exec_id], &attach)
        .output()
        .expect("microvm exec --poll runs");
    let data = envelope_data(&output.stdout);
    assert_eq!(
        data["phase"], "running",
        "CLI-9: the exec should still be running after the stream stopped: {data}"
    );
}

/// Kills a live scenario's exec whether or not its steps passed, so the VM is left as found.
fn kill_exec(world: &Cli) {
    if let (Some(exec_id), Some(attach)) = (&world.exec_id, &world.attach) {
        let _ = live_command(&["--json", "--quiet", "kill", exec_id], attach).output();
    }
}

/// Flags `cargo test` forwards to every test binary, which cucumber's own parser refuses.
const LIBTEST_FLAGS: [&str; 6] = [
    "--exact",
    "--nocapture",
    "--quiet",
    "--test-threads",
    "--color",
    "--ignored",
];

#[tokio::main]
async fn main() {
    let features = concat!(env!("CARGO_MANIFEST_DIR"), "/tests/features");
    // `cargo test <filter>` and `cargo bolero test <name>` pass a libtest filter to every test
    // target. A filter naming something else selects nothing here, as libtest would; one that
    // selects these scenarios, or a libtest-only flag, runs them with cucumber's defaults.
    let args: Vec<String> = std::env::args().skip(1).collect();
    let filters: Vec<&str> = args
        .iter()
        .map(String::as_str)
        .filter(|arg| !arg.starts_with('-'))
        .collect();
    if !filters.is_empty()
        && !filters
            .iter()
            .any(|filter| "bdd closed_output".contains(filter))
    {
        return;
    }
    let libtest = !filters.is_empty()
        || args
            .iter()
            .any(|arg| LIBTEST_FLAGS.iter().any(|flag| arg.starts_with(flag)));
    // `@live` scenarios need a VM (see the module docs); without one they are left out, and
    // the report says why rather than listing them as skipped forever.
    let live = std::env::var_os(LIVE_ATTACH).is_some();
    if !live {
        eprintln!("bdd: @live scenarios left out; set {LIVE_ATTACH} to run them against a VM");
    }
    let keep = move |_: &cucumber::gherkin::Feature,
                     _: Option<&cucumber::gherkin::Rule>,
                     scenario: &cucumber::gherkin::Scenario| {
        live || !scenario.tags.iter().any(|tag| tag == "live")
    };
    macro_rules! run {
        ($cucumber:expr) => {
            if libtest {
                $cucumber
                    .with_cli(cli::Opts::<_, _, _, cli::Empty>::default())
                    .filter_run_and_exit(features, keep)
                    .await
            } else {
                $cucumber.filter_run_and_exit(features, keep).await
            }
        };
    }
    let cucumber = Cli::cucumber()
        .max_concurrent_scenarios(4)
        .after(|_, _, _, _, world| {
            Box::pin(async move {
                if let Some(world) = world {
                    kill_exec(world);
                }
            })
        });
    match std::env::var_os("CUCUMBER_JUNIT") {
        Some(path) => {
            let report = std::fs::File::create(&path).expect("the JUnit report file");
            run!(
                cucumber.with_writer(
                    writer::Basic::raw(io::stdout(), writer::Coloring::Never, 0)
                        .summarized()
                        .tee::<Cli, _>(writer::JUnit::for_tee(report, 0))
                        .normalized(),
                )
            )
        }
        None => run!(cucumber),
    }
}
