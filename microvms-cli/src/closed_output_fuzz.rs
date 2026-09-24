// SPDX-License-Identifier: Apache-2.0
//! The fuzz harness for CLI-7, CLI-8, and CLI-9: the real [`Output`] against sinks whose
//! reader leaves at an arbitrary byte.
//!
//! `bolero::check!` runs this as an ordinary `#[test]` under stable `cargo test`, and as a
//! coverage-guided target under
//! `cargo +nightly bolero test closed_output_fuzz::output_plan -p microvms-cli -T 120s`
//! (the `fuzz` job in `.github/workflows/fuzz.yml`). The CLI has no lib target
//! (ARCH-5), so a separate fuzz crate could not link [`Output`]; an in-crate test can.
//!
//! # What a plan is
//!
//! A format, a sequence of progress lines and stream events, a final envelope, and the byte
//! at which each reader leaves. [`Closing`] accepts writes up to its limit (splitting the
//! write that crosses it, so `write_all`'s retry path runs) and returns
//! `ErrorKind::BrokenPipe` after that, which is what a pipe whose reader has gone returns on
//! every platform: std maps Windows' `ERROR_BROKEN_PIPE` and `ERROR_NO_DATA` to it.
//!
//! # What it checks
//!
//! * CLI-7: nothing panics, whatever the plan.
//! * CLI-9: once stdout's reader is gone the output layer says so, the stream stops at that
//!   event, and stdout is written at most once more after the failure — the failing write
//!   itself — rather than once per remaining event and again for the envelope.
//! * CLI-8: a closed reader never changes the exit code of an outcome the command reached,
//!   and a closed stderr never stops a stream.
//!
//! The exit decision is checked against [`MODEL_TABLE`], a literal copy of the specification
//! table in `model/src/output.rs` (`the_specification_table`). The CLI cannot depend on the
//! model crate: a path dependency without a version is a wildcard `deny.toml` refuses, and one
//! with a version would have to be published for this crate to publish.

use std::io::{self, ErrorKind, Write};

use serde_json::{Map, json};

use crate::closed_output::{self, Channel, Command, Decision};
use crate::envelope::{self, Format, Output};
use crate::exit::Exit;

/// A writer whose reader leaves after `limit` bytes.
#[derive(Debug)]
struct Closing {
    accepted: Vec<u8>,
    limit: usize,
    /// Write calls made after the reader left, including the one that found it gone.
    after_close: usize,
}

impl Closing {
    fn new(limit: usize) -> Self {
        Self {
            accepted: Vec::new(),
            limit,
            after_close: 0,
        }
    }

    fn closed(&self) -> bool {
        self.accepted.len() >= self.limit
    }
}

impl Write for Closing {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if bytes.is_empty() {
            return Ok(0);
        }
        if self.closed() {
            self.after_close += 1;
            return Err(ErrorKind::BrokenPipe.into());
        }
        let room = self.limit - self.accepted.len();
        let taken = bytes.len().min(room);
        self.accepted.extend_from_slice(&bytes[..taken]);
        Ok(taken)
    }

    fn flush(&mut self) -> io::Result<()> {
        if self.closed() && self.after_close > 0 {
            return Err(ErrorKind::BrokenPipe.into());
        }
        Ok(())
    }
}

/// One fuzzed run of the output layer.
#[derive(Debug, bolero::TypeGenerator)]
struct Plan {
    /// 0 JSON, 1 dense, 2 plain. The TUI needs a terminal and is not a pipe's format.
    format: u8,
    quiet: bool,
    /// A streamed exec, as opposed to a one-shot command's single document.
    streaming: bool,
    /// Per step: a progress line of this many bytes when odd, a stream event of this many
    /// payload bytes when even (ignored unless `streaming`).
    steps: Vec<u16>,
    /// Whether the command's outcome is success or a documented failure.
    outcome_ok: bool,
    /// Bytes each reader accepts before it leaves.
    stdout_limit: u32,
    stderr_limit: u32,
}

/// At most this many steps per plan, so a run stays short.
const MAX_STEPS: usize = 12;

/// Payload sizes are reduced modulo this, so plans cross the pipe-buffer scale without
/// allocating megabytes.
const MAX_PAYLOAD: u16 = 16 * 1024;

/// Reader limits are reduced modulo this. A plan writes at most about twelve payloads, so a
/// limit drawn from the whole `u32` range would almost never close a reader and the harness
/// would check nothing.
const MAX_LIMIT: u32 = 96 * 1024;

fn format_of(plan: &Plan) -> Format {
    match plan.format % 3 {
        0 => Format::Json,
        1 => Format::Dense,
        _ => Format::Plain,
    }
}

/// What the harness observed about one plan.
struct Run {
    /// Whether the stream was stopped because a stream write found stdout gone.
    stopped: bool,
    stdout: Closing,
    stderr: Closing,
}

/// Drives the real [`Output`] the way a command does: progress and stream events, stopping
/// the stream when the policy says to, then the one envelope.
fn drive(plan: &Plan) -> Run {
    let mut out = Output::new(
        format_of(plan),
        plan.quiet,
        Closing::new((plan.stdout_limit % MAX_LIMIT) as usize),
        Closing::new((plan.stderr_limit % MAX_LIMIT) as usize),
    );
    let command = if plan.streaming {
        Command::Stream
    } else {
        Command::OneShot
    };
    let mut stopped = false;
    for step in plan.steps.iter().take(MAX_STEPS) {
        let size = usize::from(step % MAX_PAYLOAD);
        if step % 2 == 1 {
            out.progress(&"p".repeat(size));
            continue;
        }
        if !plan.streaming {
            continue;
        }
        let payload = "x".repeat(size);
        match out.format() {
            Format::Json => out.stream_line(&json!({"stream": "stdout", "data": payload})),
            _ => out.stream_bytes(payload.as_bytes()),
        }
        if out.stdout_closed()
            && closed_output::on_failed_write(command, Channel::Stdout, true)
                == Decision::StopStream
        {
            stopped = true;
            break;
        }
    }
    let envelope = if plan.outcome_ok {
        envelope::ok("microvm.test", Map::new())
    } else {
        json!({"status": "error", "code": "ERR_PLATFORM"})
    };
    out.emit(&envelope, "done");
    let (stdout, stderr) = out.into_streams();
    Run {
        stopped,
        stdout,
        stderr,
    }
}

#[test]
fn output_plan() {
    bolero::check!().with_type::<Plan>().for_each(|plan| {
        // CLI-7: `drive` returning at all is the no-panic half.
        let run = drive(plan);

        // CLI-9: after the failing write, stdout is never written again: not by later stream
        // events, not by the envelope.
        assert!(
            run.stdout.after_close <= 1,
            "CLI-9: {} writes reached stdout after its reader left: {plan:?}",
            run.stdout.after_close
        );
        // CLI-8: the same for stderr's progress lines.
        assert!(
            run.stderr.after_close <= 1,
            "CLI-8: {} writes reached stderr after its reader left: {plan:?}",
            run.stderr.after_close
        );
        // CLI-9: a stream stops only because stdout's reader left, never because stderr's did.
        if run.stopped {
            assert!(
                run.stdout.closed(),
                "CLI-9: the stream stopped with stdout open: {plan:?}"
            );
        }

        // CLI-8 and CLI-9: the exit code is the outcome's, except a stopped stream, which is
        // ERR_INTERRUPTED; the readers' state never enters into it.
        let outcome = if run.stopped {
            Exit::Interrupted
        } else if plan.outcome_ok {
            Exit::Ok
        } else {
            Exit::Platform
        };
        let exit = closed_output::exit_code(outcome, !run.stdout.closed(), !run.stderr.closed());
        assert_eq!(
            exit, outcome,
            "CLI-8: a closed reader changed the exit: {plan:?}"
        );
    });
}

/// `model/src/output.rs`'s specification table, copied: for each command, channel, and kind of
/// write, what the specification does when that write fails.
const MODEL_TABLE: [(Command, Channel, bool, Decision); 16] = [
    (Command::Help, Channel::Stdout, false, Decision::Continue),
    (Command::Help, Channel::Stdout, true, Decision::Continue),
    (Command::Help, Channel::Stderr, false, Decision::Continue),
    (Command::Help, Channel::Stderr, true, Decision::Continue),
    (Command::OneShot, Channel::Stdout, false, Decision::Continue),
    (Command::OneShot, Channel::Stdout, true, Decision::Continue),
    (Command::OneShot, Channel::Stderr, false, Decision::Continue),
    (Command::OneShot, Channel::Stderr, true, Decision::Continue),
    (Command::Launch, Channel::Stdout, false, Decision::Continue),
    (Command::Launch, Channel::Stdout, true, Decision::Continue),
    (Command::Launch, Channel::Stderr, false, Decision::Continue),
    (Command::Launch, Channel::Stderr, true, Decision::Continue),
    (Command::Stream, Channel::Stdout, false, Decision::Continue),
    (Command::Stream, Channel::Stdout, true, Decision::StopStream),
    (Command::Stream, Channel::Stderr, false, Decision::Continue),
    (Command::Stream, Channel::Stderr, true, Decision::Continue),
];

/// CLI-7, CLI-8, CLI-9: the CLI's policy answers exactly what the model's specification does.
#[test]
fn the_policy_is_the_models_table() {
    for (command, channel, stream_event, expected) in MODEL_TABLE {
        assert_eq!(
            closed_output::on_failed_write(command, channel, stream_event),
            expected,
            "{command:?} {channel:?} stream_event={stream_event}"
        );
    }
    for outcome in [Exit::Ok, Exit::Platform, Exit::Interrupted] {
        for stdout_open in [false, true] {
            for stderr_open in [false, true] {
                // CLI-8: the model's exit decision ignores both readers.
                assert_eq!(
                    closed_output::exit_code(outcome, stdout_open, stderr_open),
                    outcome
                );
            }
        }
    }
}
