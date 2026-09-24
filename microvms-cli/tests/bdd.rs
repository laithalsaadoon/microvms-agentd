// SPDX-License-Identifier: Apache-2.0
//! The Gherkin behavior spec for CLI-7, CLI-8, and CLI-9, run against the real binary.
//!
//! The scenarios live in `tests/features/*.feature`, tagged with the requirement each one
//! verifies; this file is their step definitions and runner. It is a `harness = false` test,
//! so `cargo test` runs it on every CI system, and it writes a JUnit report when
//! `CUCUMBER_JUNIT` names a file.
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
//! Scenarios tagged `@needs-daemon` have steps no definition here matches, so they report
//! as skipped: the shipped binary reaches a daemon only through the AWS control plane.

#[allow(dead_code)]
mod support;

use std::io::{self, Read};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::time::{Duration, Instant};

use cucumber::{World, WriterExt, cli, then, when, writer};

/// How long one invocation may take before the step fails. A command blocked writing into a
/// pipe nobody reads is the hang this bounds.
const DEADLINE: Duration = Duration::from_secs(60);

/// The exit table's rows, 0 through 16 (`EXIT_TABLE` in `src/exit.rs`).
const TABLE_ROWS: i32 = 17;

/// Rust's runtime exits 101 after a panic, outside the exit table.
const PANIC_STATUS: i32 = 101;

#[derive(Debug, Default, World)]
struct Cli {
    /// The exit code, or `None` when the child died by a signal.
    code: Option<i32>,
    /// The signal that killed the child, where the platform reports one.
    signal: Option<i32>,
    /// Everything the child wrote to stderr, when stderr was read.
    stderr: Option<String>,
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
    macro_rules! run {
        ($cucumber:expr) => {
            if libtest {
                $cucumber
                    .with_cli(cli::Opts::<_, _, _, cli::Empty>::default())
                    .run_and_exit(features)
                    .await
            } else {
                $cucumber.run_and_exit(features).await
            }
        };
    }
    let cucumber = Cli::cucumber().max_concurrent_scenarios(4);
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
