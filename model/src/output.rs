// SPDX-License-Identifier: Apache-2.0
//! A checked model of what the `microvm` CLI does when a reader closes its stdout or stderr.
//!
//! Third sibling beside the daemon model in [`crate`] and the client model in
//! [`crate::client`]. It specifies CLI-7, CLI-8, and CLI-9 in `spec/core.symspec.json`, which
//! issue #216 asked for after `microvm keepalive --help | head -3` panicked with "failed
//! printing to stdout: Broken pipe" and exited 101.
//!
//! # The strategy this model specifies, and the two it rejects
//!
//! Rust's runtime ignores SIGPIPE before `main`, so a write to a pipe whose reader is gone
//! returns `ErrorKind::BrokenPipe` instead of killing the process. `println!` turns that
//! error into a panic. The CLI keeps std's ignored SIGPIPE and handles `BrokenPipe` at every
//! write, which is what ripgrep, bat, cargo, and clap's own `Error::exit` do.
//!
//! * **Resetting SIGPIPE to the default** kills the process on the first failed write. No
//!   destructor runs, so a `run` whose reader left mid-teardown leaves a billing VM (CLI-6),
//!   the raw-mode guard never restores the terminal, and a write to a child's stdin kills
//!   the CLI too. It needs `unsafe`, which the CLI forbids, and does nothing on Windows.
//!   [`Behavior::SigpipeReset`] is that strategy, and the checker finds the leak.
//! * **Reporting a closed reader as a failure** looks honest and is dangerous: a `run` that
//!   launched a VM and then lost its reader would exit non-zero, and a caller that retries on
//!   failure launches a second VM. A closed reader is the consumer's choice, not an outcome
//!   of the command. [`Behavior::ClosedIsFailure`] is that policy, and the checker finds it
//!   rewriting a completed outcome.
//!
//! Windows needs no separate path: std maps `ERROR_BROKEN_PIPE` and `ERROR_NO_DATA` to
//! `ErrorKind::BrokenPipe`, so the one check covers both platforms.
//!
//! # The specification is one pure function
//!
//! [`specified`] answers every question the CLI faces here: what to do when a write fails,
//! and what code to exit with. The configurations that must fail are other functions of the
//! same signature, selected by [`Behavior`]. The Rust guard tests in `microvms-cli` mirror
//! [`specified`]'s table, so the model and the binary are checked against one statement.
//!
//! # Streams are the one exception to "keep the outcome"
//!
//! A one-shot command decides its outcome before it writes its document, so a closed reader
//! can only lose the document, never change what happened. A streaming exec has no outcome
//! until the remote exec ends, and streaming into a closed pipe is pure waste. So CLI-9 stops
//! the stream at the first failed event write, leaves the remote exec running (it was never
//! the reader's to stop), and exits `ERR_INTERRUPTED` naming the exec id on stderr, so a
//! caller can reattach.
//!
//! # Every always-property has a sometimes-property beside it
//!
//! As in [`crate::client`]: a safety property over a space that never reaches the
//! interesting state measures nothing, so each claim is paired with a witness that the
//! checker got there.

use stateright::{Model, Property};

/// What the CLI was asked to do, reduced to the shapes that treat output differently.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Command {
    /// `--help` or `--version`: clap's text, printed and exit 0. The #216 site.
    Help,
    /// A one-shot command with one document on stdout (an envelope under `--json`).
    OneShot,
    /// A command that launched a VM and owes its teardown before it exits (`run`).
    Launch,
    /// `exec --stream`: events on stdout until the remote exec ends.
    Stream,
}

/// The output format. Recorded so a property or a rejected policy can depend on it; the
/// specified policy does not.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Format {
    /// Human text.
    Text,
    /// A JSON envelope (CLI-4).
    Json,
}

/// The two streams a reader can close.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Channel {
    /// The document, and stream events.
    Stdout,
    /// Progress, warnings, and the exec-id note.
    Stderr,
}

/// How a command ended, or that it has not.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum End {
    /// Still running.
    Running,
    /// Exited with this status.
    Exited(u8),
    /// Died by panic: status 101 from Rust's runtime, a code outside the exit table.
    Panicked,
    /// Killed by a signal (SIGPIPE under a reset disposition).
    Signaled,
}

/// What a command concluded, independent of whether anyone read it.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Outcome {
    /// The command did what it was asked.
    Success,
    /// A documented failure class, carried as its exit-table code.
    Failed(u8),
    /// A stream stopped because its reader left (CLI-9).
    Interrupted,
}

/// `ERR_INTERRUPTED`'s row in `microvms-cli/src/exit.rs`.
pub const INTERRUPTED: u8 = 11;
/// `ERR_PLATFORM`'s row: the failure a one-shot command reports here.
pub const PLATFORM: u8 = 9;
/// `ERR_LAUNCH_DIED`'s row: the failure a launch reports here.
pub const LAUNCH_DIED: u8 = 7;
/// `ERR_EXEC_FAILED`'s row: a remote exec that exited non-zero.
pub const EXEC_FAILED: u8 = 13;
/// `ERR_UNEXPECTED`'s row: what [`Behavior::ClosedIsFailure`] reports.
pub const UNEXPECTED: u8 = 1;
/// The exit table has 17 rows, 0 through 16 (`EXIT_TABLE` in `microvms-cli/src/exit.rs`).
pub const TABLE_ROWS: u8 = 17;

impl Outcome {
    /// The exit-table code this outcome is reported with.
    pub fn code(self) -> u8 {
        match self {
            Outcome::Success => 0,
            Outcome::Failed(code) => code,
            Outcome::Interrupted => INTERRUPTED,
        }
    }
}

/// A question the CLI has to answer.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Event {
    /// A write failed because the reader of `channel` is gone. `stream_event` marks a write
    /// of stream output, as opposed to a document, progress line, or note.
    FailedWrite {
        command: Command,
        channel: Channel,
        stream_event: bool,
    },
    /// The command is ready to exit with this outcome, with these readers still open.
    Exit {
        command: Command,
        format: Format,
        outcome: Outcome,
        stdout_open: bool,
        stderr_open: bool,
    },
}

/// The answer.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Decision {
    /// Drop the bytes and carry on.
    Continue,
    /// Stop streaming: detach from the remote exec, note its id on stderr (CLI-9).
    StopStream,
    /// Exit with this status.
    Exit(u8),
    /// Panic (today's `print!`).
    Panic,
    /// Die by signal (a SIGPIPE reset to its default).
    Kill,
}

/// **The specification.** Everything CLI-7, CLI-8, and CLI-9 require, as one pure function.
///
/// * A failed write is dropped, except a stream event on a closed stdout, which stops the
///   stream (CLI-9). Nothing panics and nothing dies by signal (CLI-7).
/// * The exit code is the outcome's code whatever the readers are doing: `stdout_open` and
///   `stderr_open` are parameters precisely so that it is visible they are ignored (CLI-8).
///
/// **Falsification** — 2026-09-24. Answering `Continue` instead of `StopStream` for a stream
/// event made `the_specified_cli_satisfies_every_property` fail with a nine-state
/// counterexample to `CLI-9 a stream stops within one event of its reader closing`: stdout
/// closes, then two stream events are written into it; restored after.
pub fn specified(event: Event) -> Decision {
    match event {
        Event::FailedWrite {
            command: Command::Stream,
            channel: Channel::Stdout,
            stream_event: true,
        } => Decision::StopStream,
        Event::FailedWrite { .. } => Decision::Continue,
        Event::Exit { outcome, .. } => Decision::Exit(outcome.code()),
    }
}

/// The CLI as it was when #216 was filed: clap's help and version text went through
/// `print!`, which panics on a closed stdout, and `exec --stream` kept draining events into
/// a pipe nobody read. Everything else already dropped failed writes.
fn today(event: Event) -> Decision {
    match event {
        Event::FailedWrite {
            command: Command::Help,
            channel: Channel::Stdout,
            ..
        } => Decision::Panic,
        Event::FailedWrite {
            command: Command::Stream,
            channel: Channel::Stdout,
            stream_event: true,
        } => Decision::Continue,
        other => specified(other),
    }
}

/// SIGPIPE reset to its default: the first write to a closed pipe kills the process.
fn sigpipe_reset(event: Event) -> Decision {
    match event {
        Event::FailedWrite { .. } => Decision::Kill,
        other => specified(other),
    }
}

/// A closed stdout reported as a failure, overriding whatever the command concluded.
fn closed_is_failure(event: Event) -> Decision {
    match event {
        Event::Exit {
            stdout_open: false,
            outcome: Outcome::Success,
            ..
        } => Decision::Exit(UNEXPECTED),
        other => specified(other),
    }
}

/// Which answer function the model runs.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Behavior {
    /// [`specified`].
    Specified,
    /// The CLI at the time #216 was filed.
    Today,
    /// SIGPIPE reset to its default at startup.
    SigpipeReset,
    /// A closed stdout reported as a failure.
    ClosedIsFailure,
}

impl Behavior {
    fn decide(self, event: Event) -> Decision {
        match self {
            Behavior::Specified => specified(event),
            Behavior::Today => today(event),
            Behavior::SigpipeReset => sigpipe_reset(event),
            Behavior::ClosedIsFailure => closed_is_failure(event),
        }
    }
}

/// The model's knobs.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct Config {
    /// The answer function under test.
    pub behavior: Behavior,
    /// How many stream events the remote exec can produce before it must exit. Three is the
    /// least that separates "stopped after one" from "kept going".
    pub max_events: u8,
}

impl Config {
    /// The specified CLI.
    pub fn specified() -> Self {
        Self::with(Behavior::Specified)
    }

    /// Any behavior, with the default bounds.
    pub fn with(behavior: Behavior) -> Self {
        Self {
            behavior,
            max_events: 3,
        }
    }
}

/// One command's run, from the first write to the process ending.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct State {
    pub command: Command,
    pub format: Format,
    pub stdout_open: bool,
    pub stderr_open: bool,
    /// Decided outcome. For a stream, decided by the remote exec ending or by a stop.
    pub outcome: Option<Outcome>,
    /// A launch owes its teardown from the moment it starts (CLI-6).
    pub teardown_owed: bool,
    pub teardown_done: bool,
    /// A reader closed while teardown was owed and not yet done: the CLI-8 witness.
    pub closed_during_teardown: bool,
    /// The one progress line to stderr has been attempted.
    pub progress_attempted: bool,
    /// Stream events the reader received.
    pub events: u8,
    /// Stream event writes attempted after stdout's reader closed.
    pub events_after_close: u8,
    /// The remote exec is still running.
    pub remote_running: bool,
    /// The CLI stopped streaming (CLI-9).
    pub stream_stopped: bool,
    /// The exec-id note was attempted on stderr (CLI-9).
    pub exec_id_noted: bool,
    /// The final document (help text, envelope, summary) has been attempted on stdout.
    pub document_attempted: bool,
    pub end: End,
}

/// What can happen during a run.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Action {
    /// The reader of stdout goes away. Possible at any point while the process runs.
    CloseStdout,
    /// The reader of stderr goes away.
    CloseStderr,
    /// A progress line to stderr.
    Progress,
    /// A stream event to stdout.
    StreamEvent,
    /// The remote exec ends with this outcome.
    RemoteExits(Outcome),
    /// A non-streaming command concludes.
    Decide(Outcome),
    /// The teardown a launch owes, with its progress line on stderr.
    Teardown,
    /// The final document on stdout.
    WriteDocument,
    /// The process exits.
    Exit,
}

/// The model.
#[derive(Clone, Debug)]
pub struct OutputLifecycle {
    pub cfg: Config,
}

impl OutputLifecycle {
    pub fn new(cfg: Config) -> Self {
        Self { cfg }
    }

    /// Applies a failed-or-successful write on `channel`, returning whether the process
    /// is still running afterwards.
    fn write(&self, state: &mut State, channel: Channel, stream_event: bool) -> bool {
        let open = match channel {
            Channel::Stdout => state.stdout_open,
            Channel::Stderr => state.stderr_open,
        };
        if open {
            if stream_event {
                state.events += 1;
            }
            return true;
        }
        if stream_event {
            state.events_after_close += 1;
        }
        let decision = self.cfg.behavior.decide(Event::FailedWrite {
            command: state.command,
            channel,
            stream_event,
        });
        match decision {
            Decision::Continue | Decision::Exit(_) => true,
            Decision::Panic => {
                state.end = End::Panicked;
                false
            }
            Decision::Kill => {
                state.end = End::Signaled;
                false
            }
            Decision::StopStream => {
                state.stream_stopped = true;
                state.outcome = Some(Outcome::Interrupted);
                state.exec_id_noted = true;
                // The note is itself a stderr write, answered like any other.
                self.write(state, Channel::Stderr, false)
            }
        }
    }

    fn outcomes(command: Command) -> &'static [Outcome] {
        match command {
            Command::Help => &[Outcome::Success],
            Command::OneShot => &[Outcome::Success, Outcome::Failed(PLATFORM)],
            Command::Launch => &[Outcome::Success, Outcome::Failed(LAUNCH_DIED)],
            Command::Stream => &[Outcome::Success, Outcome::Failed(EXEC_FAILED)],
        }
    }
}

impl Model for OutputLifecycle {
    type State = State;
    type Action = Action;

    fn init_states(&self) -> Vec<State> {
        let mut states = Vec::new();
        for command in [
            Command::Help,
            Command::OneShot,
            Command::Launch,
            Command::Stream,
        ] {
            let formats: &[Format] = match command {
                Command::Help => &[Format::Text],
                _ => &[Format::Text, Format::Json],
            };
            for &format in formats {
                states.push(State {
                    command,
                    format,
                    stdout_open: true,
                    stderr_open: true,
                    outcome: None,
                    teardown_owed: command == Command::Launch,
                    teardown_done: false,
                    closed_during_teardown: false,
                    progress_attempted: false,
                    events: 0,
                    events_after_close: 0,
                    remote_running: command == Command::Stream,
                    stream_stopped: false,
                    exec_id_noted: false,
                    document_attempted: false,
                    end: End::Running,
                });
            }
        }
        states
    }

    fn actions(&self, state: &State, actions: &mut Vec<Action>) {
        if state.end != End::Running {
            return;
        }
        if state.stdout_open {
            actions.push(Action::CloseStdout);
        }
        if state.stderr_open {
            actions.push(Action::CloseStderr);
        }
        if state.command != Command::Help && !state.progress_attempted {
            actions.push(Action::Progress);
        }
        let streaming = state.command == Command::Stream
            && state.remote_running
            && !state.stream_stopped
            && state.outcome.is_none();
        if streaming {
            if state.events + state.events_after_close < self.cfg.max_events {
                actions.push(Action::StreamEvent);
            }
            for &outcome in Self::outcomes(state.command) {
                actions.push(Action::RemoteExits(outcome));
            }
        }
        if state.command != Command::Stream && state.outcome.is_none() {
            for &outcome in Self::outcomes(state.command) {
                actions.push(Action::Decide(outcome));
            }
        }
        if state.teardown_owed && !state.teardown_done && state.outcome.is_some() {
            actions.push(Action::Teardown);
        }
        let ready = state.outcome.is_some() && (!state.teardown_owed || state.teardown_done);
        if ready && !state.document_attempted {
            actions.push(Action::WriteDocument);
        }
        if ready && state.document_attempted {
            actions.push(Action::Exit);
        }
    }

    fn next_state(&self, last: &State, action: Action) -> Option<State> {
        let mut next = *last;
        let owed = last.teardown_owed && !last.teardown_done && last.outcome.is_some();
        match action {
            Action::CloseStdout => {
                next.stdout_open = false;
                next.closed_during_teardown |= owed;
            }
            Action::CloseStderr => {
                next.stderr_open = false;
                next.closed_during_teardown |= owed;
            }
            Action::Progress => {
                next.progress_attempted = true;
                self.write(&mut next, Channel::Stderr, false);
            }
            Action::StreamEvent => {
                self.write(&mut next, Channel::Stdout, true);
            }
            Action::RemoteExits(outcome) => {
                next.remote_running = false;
                next.outcome = Some(outcome);
            }
            Action::Decide(outcome) => next.outcome = Some(outcome),
            Action::Teardown => {
                // The progress line first, then the teardown: a process killed by the line
                // never gets to tear down, which is the SIGPIPE-reset leak.
                if self.write(&mut next, Channel::Stderr, false) {
                    next.teardown_done = true;
                }
            }
            Action::WriteDocument => {
                next.document_attempted = true;
                self.write(&mut next, Channel::Stdout, false);
            }
            Action::Exit => {
                let outcome = last.outcome.expect("exit is offered only once decided");
                next.end = match self.cfg.behavior.decide(Event::Exit {
                    command: last.command,
                    format: last.format,
                    outcome,
                    stdout_open: last.stdout_open,
                    stderr_open: last.stderr_open,
                }) {
                    Decision::Exit(code) => End::Exited(code),
                    Decision::Panic => End::Panicked,
                    Decision::Kill => End::Signaled,
                    Decision::Continue | Decision::StopStream => End::Exited(outcome.code()),
                };
            }
        }
        (next != *last).then_some(next)
    }

    fn properties(&self) -> Vec<Property<Self>> {
        vec![
            // ── CLI-7 ─────────────────────────────────────────────────────────
            Property::<Self>::always("CLI-7 never panics or dies by signal", |_, s| {
                !matches!(s.end, End::Panicked | End::Signaled)
            }),
            Property::<Self>::always(
                "CLI-7 exits with a status from the exit table",
                |_, s| match s.end {
                    End::Running => true,
                    End::Exited(code) => code < TABLE_ROWS,
                    End::Panicked | End::Signaled => false,
                },
            ),
            Property::<Self>::sometimes(
                "CLI-7 witness: help is written to a closed stdout",
                |_, s| s.command == Command::Help && s.document_attempted && !s.stdout_open,
            ),
            Property::<Self>::sometimes(
                "CLI-7 witness: progress is written to a closed stderr",
                |_, s| s.progress_attempted && !s.stderr_open && s.end != End::Running,
            ),
            // ── CLI-8 ─────────────────────────────────────────────────────────
            Property::<Self>::always("CLI-8 no exit while teardown is owed", |_, s| {
                s.end == End::Running || !s.teardown_owed || s.teardown_done
            }),
            Property::<Self>::always("CLI-8 a decided outcome keeps its code", |_, s| {
                match (s.end, s.outcome) {
                    (End::Exited(code), Some(outcome)) => code == outcome.code(),
                    _ => true,
                }
            }),
            Property::<Self>::sometimes(
                "CLI-8 witness: a reader closes during teardown",
                |_, s| s.closed_during_teardown && s.teardown_done,
            ),
            Property::<Self>::sometimes(
                "CLI-8 witness: a launch succeeds with its stdout closed",
                |_, s| s.command == Command::Launch && s.end == End::Exited(0) && !s.stdout_open,
            ),
            // ── CLI-9 ─────────────────────────────────────────────────────────
            Property::<Self>::always(
                "CLI-9 a stream stops within one event of its reader closing",
                |_, s| s.events_after_close <= 1,
            ),
            Property::<Self>::always(
                "CLI-9 a stopped stream leaves the remote exec running",
                |_, s| !s.stream_stopped || s.remote_running,
            ),
            Property::<Self>::always(
                "CLI-9 a stopped stream exits ERR_INTERRUPTED naming the exec",
                |_, s| match s.end {
                    End::Exited(code) if s.stream_stopped => code == INTERRUPTED && s.exec_id_noted,
                    _ => true,
                },
            ),
            Property::<Self>::sometimes("CLI-9 witness: the reader closes mid-stream", |_, s| {
                s.stream_stopped && s.events > 0 && s.end == End::Exited(INTERRUPTED)
            }),
            Property::<Self>::sometimes("CLI-9 witness: a stream runs to its end", |m, s| {
                s.command == Command::Stream
                    && !s.remote_running
                    && s.events == m.cfg.max_events
                    && s.end == End::Exited(0)
            }),
            // ── liveness ──────────────────────────────────────────────────────
            //
            // Sound here because the model is acyclic: every action changes a state that no
            // action changes back.
            Property::<Self>::eventually("every run ends", |_, s| s.end != End::Running),
        ]
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use stateright::{Checker, Model};

    fn checked(behavior: Behavior) -> impl Checker<OutputLifecycle> {
        OutputLifecycle::new(Config::with(behavior))
            .checker()
            .spawn_bfs()
            .join()
    }

    /// The headline: the specified CLI satisfies CLI-7, CLI-8, and CLI-9 over every
    /// interleaving of readers closing, and witnesses every case that makes that meaningful.
    #[test]
    fn the_specified_cli_satisfies_every_property() {
        let checker = checked(Behavior::Specified);
        checker.assert_properties();
        assert!(
            checker.unique_state_count() > 100,
            "a space this small could not reach the interleavings: {}",
            checker.unique_state_count()
        );
    }

    /// **#216 in the model.** The CLI as filed panics when help meets a closed stdout, and
    /// streams into a closed pipe; the checker hands back both paths.
    #[test]
    fn todays_cli_panics_on_help_and_streams_into_a_closed_pipe() {
        let checker = checked(Behavior::Today);
        let steps = checker
            .assert_any_discovery("CLI-7 never panics or dies by signal")
            .into_actions();
        assert!(
            steps.contains(&Action::CloseStdout) && steps.contains(&Action::WriteDocument),
            "the panic needs a closed stdout and a document write, got {steps:?}"
        );
        let steps = checker
            .assert_any_discovery("CLI-9 a stream stops within one event of its reader closing")
            .into_actions();
        assert!(
            steps.iter().filter(|a| **a == Action::StreamEvent).count() >= 2,
            "the stream must keep writing after the close, got {steps:?}"
        );
    }

    /// **The rejected SIGPIPE reset.** The first failed write kills the process, so a launch
    /// whose stderr reader leaves before teardown exits owing a VM.
    #[test]
    fn a_sigpipe_reset_dies_by_signal_and_skips_teardown() {
        let checker = checked(Behavior::SigpipeReset);
        checker.assert_any_discovery("CLI-7 never panics or dies by signal");
        let steps = checker
            .assert_any_discovery("CLI-8 no exit while teardown is owed")
            .into_actions();
        assert!(
            steps.contains(&Action::CloseStderr),
            "the kill needs a closed reader, got {steps:?}"
        );
    }

    /// **The rejected "closed means failed".** Every exit is still a table row, so CLI-7
    /// holds; what breaks is CLI-8, a completed outcome rewritten to a failure.
    #[test]
    fn reporting_a_closed_reader_as_failure_rewrites_a_completed_outcome() {
        let checker = checked(Behavior::ClosedIsFailure);
        checker.assert_no_discovery("CLI-7 exits with a status from the exit table");
        checker.assert_no_discovery("CLI-7 never panics or dies by signal");
        let steps = checker
            .assert_any_discovery("CLI-8 a decided outcome keeps its code")
            .into_actions();
        assert!(
            steps.contains(&Action::CloseStdout),
            "the rewrite needs a closed stdout, got {steps:?}"
        );
    }

    /// The table the CLI's guard tests mirror: the specification's answer to each failed
    /// write, and its exit code for each outcome with every reader closed.
    #[test]
    fn the_specification_table() {
        for command in [
            Command::Help,
            Command::OneShot,
            Command::Launch,
            Command::Stream,
        ] {
            for channel in [Channel::Stdout, Channel::Stderr] {
                for stream_event in [false, true] {
                    let expected =
                        if command == Command::Stream && channel == Channel::Stdout && stream_event
                        {
                            Decision::StopStream
                        } else {
                            Decision::Continue
                        };
                    assert_eq!(
                        specified(Event::FailedWrite {
                            command,
                            channel,
                            stream_event
                        }),
                        expected,
                        "{command:?} {channel:?} stream_event={stream_event}"
                    );
                }
            }
            for outcome in [
                Outcome::Success,
                Outcome::Failed(PLATFORM),
                Outcome::Interrupted,
            ] {
                assert_eq!(
                    specified(Event::Exit {
                        command,
                        format: Format::Json,
                        outcome,
                        stdout_open: false,
                        stderr_open: false,
                    }),
                    Decision::Exit(outcome.code())
                );
            }
        }
    }
}
