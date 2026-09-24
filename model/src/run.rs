// SPDX-License-Identifier: Apache-2.0
//! A checked model of `Session::run_to_completion`: one exec driven from start to exactly
//! one result, whatever the stream, the ack, the client deadline, and the kill do.
//!
//! Fourth sibling beside the daemon model in [`crate`], the client model in
//! [`crate::client`], and the output model in [`crate::output`]. It specifies BIND-6 through
//! BIND-10 in `spec/core.symspec.json`, which issue #222 asked for after every harness
//! (Harbor, Omnigent, an eve backend) reimplemented the same 150 lines: start with a
//! caller-minted id, stream to a callback, fall back to poll-and-ack when the stream is cut,
//! kill the process group on a client-side timeout, ack with a short grace, and synthesize
//! exit code 124 when even that fails.
//!
//! # What the model decides and what it leaves to the Rust tests
//!
//! The composition is a small state machine with a hostile environment: every network step
//! can fail, the stream can end without its terminal `exit` event, and the client deadline
//! can fire while the client is streaming or waiting. This model enumerates every such
//! interleaving for each [`Fate`] a command can have server-side and checks that the client
//! returns exactly one outcome, that the fallbacks are taken in the right order, and that
//! the POSIX exit code and notes it reports agree with what happened.
//!
//! Time is not in the state space. The client deadline is one action, [`Action::Deadline`],
//! offered wherever the real code has a deadline armed; the arithmetic (`timeout_sec` plus
//! `client_grace_sec`, the ceiling when there is no `timeout_sec`) is what the Rust tests
//! drive under tokio's paused clock.
//!
//! # The POSIX exit code is decided by what ended the command
//!
//! [`posix_exit_code`] is the specification. A deadline that ended the command reports 124,
//! the code GNU `timeout(1)` uses: the daemon's own deadline (`timed_out`), the client's kill
//! of a live process group, or a synthesized result. Any other signal death reports
//! `128 + signal`, the shell's convention. Anything else reports the exit code.
//!
//! Issue #222 stated the timeout rule as "a SIGTERM or SIGKILL signal and `timed_out`". That
//! wording leaves out a child that traps SIGTERM and exits by itself after the deadline fired:
//! it has an exit code, often 0, and the literal rule reports it as a success while
//! `ExecResult.ok` says it failed. [`Behavior::LiteralMapping`] is that rule, and the checker
//! finds the case. [`Behavior::HarvesterMapping`] is the eval harvester's rule, which reports
//! 124 for any SIGTERM or SIGKILL whenever a timeout was requested, and the checker finds it
//! calling an out-of-memory kill a timeout.
//!
//! # Every always-property has a sometimes-property beside it
//!
//! As in the sibling models: a safety property over a space that never reaches the
//! interesting state measures nothing, so each claim is paired with a witness that the checker
//! got there.

use stateright::{Model, Property};

/// SIGKILL on Linux, the guest's numbering.
pub const SIGKILL: i32 = 9;
/// SIGSEGV on Linux, a signal death no deadline causes.
pub const SIGSEGV: i32 = 11;
/// SIGTERM on Linux: the first signal of both the daemon's escalation and a client kill.
pub const SIGTERM: i32 = 15;
/// The exit code GNU `timeout(1)` reports for a command its deadline ended.
pub const TIMED_OUT: i32 = 124;

/// What the command does server-side, independent of what the client sees of it.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Fate {
    /// Exits by itself with this code before any deadline.
    Exits(i32),
    /// Dies to this signal before any deadline: an out-of-memory kill, a crash.
    Dies(i32),
    /// The daemon's `timeout_sec` fired and the group died to this signal.
    DaemonDeadline(i32),
    /// The daemon's deadline fired and the child trapped SIGTERM and exited with this code.
    DaemonDeadlineExit(i32),
    /// Still running at the client deadline: it ignores SIGTERM for longer than the client
    /// grace, or a grandchild holds its pipes. Only a kill ends it.
    Outlives,
}

/// What the daemon reports about a finished exec: the fields of `protocol::exec::Outcome`
/// that the exit code and the notes depend on.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct Status {
    pub exit_code: Option<i32>,
    pub signal: Option<i32>,
    /// The daemon's execution deadline fired.
    pub timed_out: bool,
    /// Either stream hit the daemon's output cap.
    pub truncated: bool,
}

/// The daemon's answer to `POST /v1/exec/{id}/kill`, or its absence.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum KillAnswer {
    /// `killed: true`: a live process group was signalled.
    Signalled,
    /// `killed: false`: the group was already gone.
    AlreadyGone,
    /// The request failed, so nothing is known.
    Failed,
}

/// What the client records when its own deadline fired.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct ClientDeadline {
    pub kill: KillAnswer,
    /// The post-kill ack failed and the result was synthesized.
    pub synthesized: bool,
}

/// **The specification** of `ExecResult::posix_exit_code` (BIND-6). See the module docs.
///
/// `None` only for a result with no status and no client deadline: a running exec, which
/// `run_to_completion` never returns.
pub fn posix_exit_code(status: Option<Status>, client: Option<ClientDeadline>) -> Option<i32> {
    if let Some(client) = client
        && (client.synthesized || client.kill == KillAnswer::Signalled)
    {
        return Some(TIMED_OUT);
    }
    let status = status?;
    if status.timed_out {
        return Some(TIMED_OUT);
    }
    match (status.exit_code, status.signal) {
        (Some(code), _) => Some(code),
        (None, Some(signal)) => Some(128 + signal),
        (None, None) => None,
    }
}

/// The issue's literal rule: the exit code whenever there is one; 124 only for SIGTERM or
/// SIGKILL under a deadline.
fn literal_mapping(status: Option<Status>, client: Option<ClientDeadline>) -> Option<i32> {
    if client.is_some_and(|client| client.synthesized) {
        return Some(TIMED_OUT);
    }
    let status = status?;
    let deadline = status.timed_out || client.is_some_and(|c| c.kill == KillAnswer::Signalled);
    match (status.exit_code, status.signal) {
        (Some(code), _) => Some(code),
        (None, Some(signal)) if deadline && (signal == SIGTERM || signal == SIGKILL) => {
            Some(TIMED_OUT)
        }
        (None, Some(signal)) => Some(128 + signal),
        (None, None) => None,
    }
}

/// The eval harvester's rule: with a timeout requested, any SIGTERM or SIGKILL is 124.
fn harvester_mapping(status: Option<Status>, client: Option<ClientDeadline>) -> Option<i32> {
    if client.is_some_and(|client| client.synthesized) {
        return Some(TIMED_OUT);
    }
    let status = status?;
    match (status.exit_code, status.signal) {
        (Some(code), _) => Some(code),
        (None, Some(signal)) if signal == SIGTERM || signal == SIGKILL => Some(TIMED_OUT),
        (None, Some(signal)) => Some(128 + signal),
        (None, None) => None,
    }
}

/// The annotations a result carries (BIND-7), one flag per `ExecResult::notes` entry.
#[derive(Clone, Copy, Debug, Default, Eq, Hash, PartialEq)]
pub struct Notes {
    pub truncated: bool,
    pub daemon_deadline: bool,
    pub client_deadline: bool,
    pub synthesized: bool,
}

impl Notes {
    fn is_empty(self) -> bool {
        self == Notes::default()
    }
}

/// **The specification** of `ExecResult::notes` (BIND-7): one note per condition that
/// changes how the output reads.
pub fn notes(status: Option<Status>, client: Option<ClientDeadline>) -> Notes {
    Notes {
        truncated: status.is_some_and(|status| status.truncated),
        daemon_deadline: status.is_some_and(|status| status.timed_out),
        client_deadline: client.is_some(),
        synthesized: client.is_some_and(|client| client.synthesized),
    }
}

/// Which composition the model runs: the specified one, or a rejected one.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Behavior {
    /// The specification.
    Specified,
    /// A stream that ends without its `exit` event returns what it saw instead of falling
    /// back to wait and ack.
    CutReturnsWhatItSaw,
    /// The client deadline goes straight to the grace ack without a kill.
    AckWithoutKill,
    /// A failed kill synthesizes 124 at once instead of trying the grace ack.
    SynthesizeOnFailedKill,
    /// The issue's literal exit-code rule.
    LiteralMapping,
    /// The eval harvester's exit-code rule.
    HarvesterMapping,
    /// No note for truncated output.
    SilentTruncation,
}

impl Behavior {
    fn posix(self, status: Option<Status>, client: Option<ClientDeadline>) -> Option<i32> {
        match self {
            Behavior::LiteralMapping => literal_mapping(status, client),
            Behavior::HarvesterMapping => harvester_mapping(status, client),
            _ => posix_exit_code(status, client),
        }
    }

    fn notes(self, status: Option<Status>, client: Option<ClientDeadline>) -> Notes {
        let mut found = notes(status, client);
        if self == Behavior::SilentTruncation {
            found.truncated = false;
        }
        found
    }
}

/// Where the composition is.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Step {
    /// `POST /v1/exec/start` is in flight.
    Start,
    /// Streaming output to the callback, with the client deadline armed.
    Streaming,
    /// The `exit` event arrived; `POST /ack` is in flight.
    AckAfterExit,
    /// `wait_and_ack` with the client deadline armed: no callback, a cut stream, or a
    /// failed ack after the exit event.
    Waiting,
    /// The client deadline fired; `POST /kill` is in flight.
    Killing,
    /// `wait_and_ack` with the client grace, after the kill.
    GraceAck,
    /// The call returned.
    Done,
}

/// What the call returned.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct Returned {
    pub status: Option<Status>,
    pub client: Option<ClientDeadline>,
    pub posix: Option<i32>,
    pub notes: Notes,
}

/// One call's run.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct State {
    /// A callback was given, so the call streams first.
    pub sink: bool,
    pub fate: Fate,
    /// The command's output hit the cap.
    pub truncated: bool,
    pub step: Step,
    /// The terminal `exit` event reached the client.
    pub exit_event: bool,
    /// The stream ended without its `exit` event (a cut, a fatal stream error, or the
    /// callback stopping).
    pub cut: bool,
    /// `wait_and_ack` was attempted under the client deadline.
    pub waited: bool,
    /// An ack returned the exec's outcome.
    pub acked: bool,
    pub deadline: bool,
    pub kill_sent: bool,
    pub kill: Option<KillAnswer>,
    pub grace_ack_sent: bool,
    pub grace_ack_failed: bool,
    /// The result is synthesized.
    pub synthesized: bool,
    /// Results returned. More than one is the bug the first BIND-8 property rules out.
    pub results: u8,
    /// Errors raised: a failed start, a fatal wait.
    pub errors: u8,
    pub returned: Option<Returned>,
}

/// What can happen during a call.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Action {
    /// The start request is accepted.
    Started,
    /// The start request fails; the call raises.
    StartFailed,
    /// The terminal `exit` event arrives on the stream.
    ExitEvent,
    /// The stream ends without its `exit` event.
    StreamCut,
    /// The ack after the exit event returns the outcome.
    AckOk,
    /// The ack after the exit event fails.
    AckFailed,
    /// `wait_and_ack` returns the outcome.
    WaitDone,
    /// `wait_and_ack` fails with something other than the deadline; the call raises.
    WaitFatal,
    /// The client deadline fires.
    Deadline,
    /// The daemon answers the kill, or the kill fails.
    KillAnswered(KillAnswer),
    /// The post-kill `wait_and_ack` returns the outcome.
    GraceAckOk,
    /// The post-kill `wait_and_ack` fails.
    GraceAckFailed,
}

/// The model's knobs.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct Config {
    pub behavior: Behavior,
}

/// The model.
#[derive(Clone, Debug)]
pub struct RunToCompletion {
    pub cfg: Config,
}

impl RunToCompletion {
    pub fn new(behavior: Behavior) -> Self {
        Self {
            cfg: Config { behavior },
        }
    }

    /// Every fate the model explores. One exit code of each kind is enough: the mapping is
    /// uniform in the value, and 0 is the case the literal rule gets wrong.
    pub const FATES: [Fate; 8] = [
        Fate::Exits(0),
        Fate::Exits(3),
        Fate::Dies(SIGKILL),
        Fate::Dies(SIGSEGV),
        Fate::DaemonDeadline(SIGTERM),
        Fate::DaemonDeadline(SIGKILL),
        Fate::DaemonDeadlineExit(0),
        Fate::Outlives,
    ];

    /// What the daemon reports for `fate`, or `None` while the command still runs.
    fn status(fate: Fate, truncated: bool) -> Option<Status> {
        let (exit_code, signal, timed_out) = match fate {
            Fate::Exits(code) => (Some(code), None, false),
            Fate::Dies(signal) => (None, Some(signal), false),
            Fate::DaemonDeadline(signal) => (None, Some(signal), true),
            Fate::DaemonDeadlineExit(code) => (Some(code), None, true),
            Fate::Outlives => return None,
        };
        Some(Status {
            exit_code,
            signal,
            timed_out,
            truncated,
        })
    }

    /// What the daemon reports after a client kill: the command's own status if it had
    /// finished, SIGTERM if the kill ended it, and nothing if it still runs.
    fn status_after_kill(state: &State) -> Option<Status> {
        match (state.fate, state.kill) {
            (Fate::Outlives, Some(KillAnswer::Signalled)) => Some(Status {
                exit_code: None,
                signal: Some(SIGTERM),
                timed_out: false,
                truncated: state.truncated,
            }),
            (fate, _) => Self::status(fate, state.truncated),
        }
    }

    fn finish(&self, next: &mut State, status: Option<Status>, client: Option<ClientDeadline>) {
        next.step = Step::Done;
        next.results += 1;
        next.returned = Some(Returned {
            status,
            client,
            posix: self.cfg.behavior.posix(status, client),
            notes: self.cfg.behavior.notes(status, client),
        });
    }

    fn raise(next: &mut State) {
        next.step = Step::Done;
        next.errors += 1;
    }
}

impl Model for RunToCompletion {
    type State = State;
    type Action = Action;

    fn init_states(&self) -> Vec<State> {
        let mut states = Vec::new();
        for sink in [false, true] {
            for fate in Self::FATES {
                for truncated in [false, true] {
                    states.push(State {
                        sink,
                        fate,
                        truncated,
                        step: Step::Start,
                        exit_event: false,
                        cut: false,
                        waited: false,
                        acked: false,
                        deadline: false,
                        kill_sent: false,
                        kill: None,
                        grace_ack_sent: false,
                        grace_ack_failed: false,
                        synthesized: false,
                        results: 0,
                        errors: 0,
                        returned: None,
                    });
                }
            }
        }
        states
    }

    fn actions(&self, state: &State, actions: &mut Vec<Action>) {
        let finished = state.fate != Fate::Outlives;
        match state.step {
            Step::Start => actions.extend([Action::Started, Action::StartFailed]),
            Step::Streaming => {
                if finished {
                    actions.push(Action::ExitEvent);
                }
                actions.extend([Action::StreamCut, Action::Deadline]);
            }
            Step::AckAfterExit => actions.extend([Action::AckOk, Action::AckFailed]),
            Step::Waiting => {
                if finished {
                    actions.push(Action::WaitDone);
                }
                actions.extend([Action::WaitFatal, Action::Deadline]);
            }
            Step::Killing => {
                let answers: &[KillAnswer] = if finished {
                    &[KillAnswer::AlreadyGone, KillAnswer::Failed]
                } else {
                    &[KillAnswer::Signalled, KillAnswer::Failed]
                };
                for &answer in answers {
                    actions.push(Action::KillAnswered(answer));
                }
            }
            Step::GraceAck => {
                // The outcome exists once the command finished by itself or the kill ended it.
                if finished || state.kill == Some(KillAnswer::Signalled) {
                    actions.push(Action::GraceAckOk);
                }
                actions.push(Action::GraceAckFailed);
            }
            Step::Done => {}
        }
    }

    fn next_state(&self, last: &State, action: Action) -> Option<State> {
        let mut next = *last;
        let behavior = self.cfg.behavior;
        match action {
            Action::Started => {
                next.step = if last.sink {
                    Step::Streaming
                } else {
                    Step::Waiting
                };
                if !last.sink {
                    next.waited = true;
                }
            }
            Action::StartFailed => Self::raise(&mut next),
            Action::ExitEvent => {
                next.exit_event = true;
                next.step = Step::AckAfterExit;
            }
            Action::StreamCut => {
                next.cut = true;
                if behavior == Behavior::CutReturnsWhatItSaw {
                    // What the stream saw has no exit event, so there is no status to report.
                    self.finish(&mut next, None, None);
                } else {
                    next.step = Step::Waiting;
                    next.waited = true;
                }
            }
            Action::AckOk => {
                next.acked = true;
                self.finish(&mut next, Self::status(last.fate, last.truncated), None);
            }
            Action::AckFailed => {
                next.step = Step::Waiting;
                next.waited = true;
            }
            Action::WaitDone => {
                next.acked = true;
                self.finish(&mut next, Self::status(last.fate, last.truncated), None);
            }
            Action::WaitFatal => Self::raise(&mut next),
            Action::Deadline => {
                next.deadline = true;
                if behavior == Behavior::AckWithoutKill {
                    next.step = Step::GraceAck;
                    next.grace_ack_sent = true;
                } else {
                    next.step = Step::Killing;
                    next.kill_sent = true;
                }
            }
            Action::KillAnswered(answer) => {
                next.kill = Some(answer);
                if behavior == Behavior::SynthesizeOnFailedKill && answer == KillAnswer::Failed {
                    next.synthesized = true;
                    let client = ClientDeadline {
                        kill: answer,
                        synthesized: true,
                    };
                    self.finish(&mut next, None, Some(client));
                } else {
                    next.step = Step::GraceAck;
                    next.grace_ack_sent = true;
                }
            }
            Action::GraceAckOk => {
                next.acked = true;
                let client = ClientDeadline {
                    kill: last.kill.unwrap_or(KillAnswer::Failed),
                    synthesized: false,
                };
                self.finish(&mut next, Self::status_after_kill(last), Some(client));
            }
            Action::GraceAckFailed => {
                next.grace_ack_failed = true;
                next.synthesized = true;
                let client = ClientDeadline {
                    kill: last.kill.unwrap_or(KillAnswer::Failed),
                    synthesized: true,
                };
                self.finish(&mut next, None, Some(client));
            }
        }
        (next != *last).then_some(next)
    }

    fn properties(&self) -> Vec<Property<Self>> {
        vec![
            // ── BIND-6: the POSIX exit code ───────────────────────────────────
            Property::<Self>::always("BIND-6 a command a deadline ended reports 124", |_, s| {
                s.returned.is_none_or(|r| {
                    let ended_by_deadline = r.status.is_some_and(|st| st.timed_out)
                        || r.client
                            .is_some_and(|c| c.synthesized || c.kill == KillAnswer::Signalled);
                    !ended_by_deadline || r.posix == Some(TIMED_OUT)
                })
            }),
            Property::<Self>::always(
                "BIND-6 a signal death no deadline caused reports 128 plus the signal",
                |_, s| {
                    s.returned.is_none_or(|r| match r.status {
                        Some(Status {
                            exit_code: None,
                            signal: Some(signal),
                            timed_out: false,
                            ..
                        }) if r.client.is_none_or(|c| c.kill != KillAnswer::Signalled) => {
                            r.posix == Some(128 + signal)
                        }
                        _ => true,
                    })
                },
            ),
            Property::<Self>::always(
                "BIND-6 a command that exited by itself reports its exit code",
                |_, s| {
                    s.returned.is_none_or(|r| match r.status {
                        Some(Status {
                            exit_code: Some(code),
                            timed_out: false,
                            ..
                        }) if r.client.is_none_or(|c| c.kill != KillAnswer::Signalled) => {
                            r.posix == Some(code)
                        }
                        _ => true,
                    })
                },
            ),
            Property::<Self>::sometimes(
                "BIND-6 witness: a command that traps SIGTERM exits after the daemon deadline",
                |_, s| s.fate == Fate::DaemonDeadlineExit(0) && s.returned.is_some() && s.acked,
            ),
            Property::<Self>::sometimes(
                "BIND-6 witness: an out-of-memory kill with no deadline",
                |_, s| s.fate == Fate::Dies(SIGKILL) && s.acked,
            ),
            Property::<Self>::sometimes(
                "BIND-6 witness: a client deadline finds the group already gone",
                |_, s| s.kill == Some(KillAnswer::AlreadyGone) && s.acked,
            ),
            // ── BIND-7: the notes ─────────────────────────────────────────────
            Property::<Self>::always(
                "BIND-7 a truncated result carries the truncation note",
                |_, s| {
                    s.returned.is_none_or(|r| {
                        !r.status.is_some_and(|st| st.truncated) || r.notes.truncated
                    })
                },
            ),
            Property::<Self>::always("BIND-7 every deadline carries a note naming it", |_, s| {
                s.returned.is_none_or(|r| {
                    (!r.status.is_some_and(|st| st.timed_out) || r.notes.daemon_deadline)
                        && (r.client.is_none() || r.notes.client_deadline)
                        && (!r.client.is_some_and(|c| c.synthesized) || r.notes.synthesized)
                })
            }),
            Property::<Self>::always("BIND-7 a clean result carries no notes", |_, s| {
                s.returned.is_none_or(|r| {
                    let clean = r.client.is_none()
                        && r.status.is_some_and(|st| !st.truncated && !st.timed_out);
                    !clean || r.notes.is_empty()
                })
            }),
            Property::<Self>::sometimes("BIND-7 witness: a truncated result", |_, s| {
                s.returned.is_some_and(|r| r.notes.truncated)
            }),
            Property::<Self>::sometimes("BIND-7 witness: a clean result", |_, s| {
                s.returned
                    .is_some_and(|r| r.notes.is_empty() && r.posix == Some(0))
            }),
            // ── BIND-8: one result, and the cut-stream fallback ───────────────
            Property::<Self>::always("BIND-8 a call returns at most one outcome", |_, s| {
                s.results + s.errors <= 1
            }),
            Property::<Self>::always(
                "BIND-8 a result follows a successful ack or the synthesized timeout",
                |_, s| s.results == 0 || s.acked || s.synthesized,
            ),
            Property::<Self>::always(
                "BIND-8 a stream that ends without its exit event falls back to wait and ack",
                |_, s| !(s.cut && s.step == Step::Done) || s.waited,
            ),
            Property::<Self>::sometimes(
                "BIND-8 witness: a cut stream still returns the exec's result",
                |_, s| s.cut && s.acked && s.results == 1 && !s.deadline,
            ),
            Property::<Self>::sometimes(
                "BIND-8 witness: an exit event is acked without a wait",
                |_, s| s.exit_event && s.acked && !s.waited,
            ),
            Property::<Self>::eventually(
                "BIND-8 every call returns exactly one outcome",
                |_, s| s.step == Step::Done && s.results + s.errors == 1,
            ),
            // ── BIND-9: the client deadline kills before it acks ──────────────
            Property::<Self>::always(
                "BIND-9 no kill is sent before the client deadline",
                |_, s| !s.kill_sent || s.deadline,
            ),
            Property::<Self>::always("BIND-9 the post-deadline ack follows a kill", |_, s| {
                !s.grace_ack_sent || s.kill_sent
            }),
            Property::<Self>::always(
                "BIND-9 a failed kill still gets the post-kill ack",
                |_, s| {
                    !(s.kill == Some(KillAnswer::Failed) && s.step == Step::Done)
                        || s.grace_ack_sent
                },
            ),
            Property::<Self>::sometimes(
                "BIND-9 witness: a client deadline kills the group and collects its result",
                |_, s| s.kill == Some(KillAnswer::Signalled) && s.acked && !s.synthesized,
            ),
            Property::<Self>::sometimes(
                "BIND-9 witness: a failed kill is followed by a successful ack",
                |_, s| s.kill == Some(KillAnswer::Failed) && s.acked,
            ),
            // ── BIND-10: 124 is synthesized only when the post-kill ack fails ─
            Property::<Self>::always(
                "BIND-10 124 is synthesized only after the post-kill ack failed",
                |_, s| !s.synthesized || (s.kill_sent && s.grace_ack_failed),
            ),
            Property::<Self>::always(
                "BIND-10 a synthesized result reports 124 and says so",
                |_, s| {
                    s.returned.is_none_or(|r| {
                        !r.client.is_some_and(|c| c.synthesized)
                            || (r.posix == Some(TIMED_OUT) && r.notes.synthesized)
                    })
                },
            ),
            Property::<Self>::sometimes(
                "BIND-10 witness: a synthesized result after a failed kill",
                |_, s| s.synthesized && s.kill == Some(KillAnswer::Failed),
            ),
            Property::<Self>::sometimes(
                "BIND-10 witness: a synthesized result after a successful kill",
                |_, s| s.synthesized && s.kill == Some(KillAnswer::Signalled),
            ),
        ]
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use stateright::{Checker, Model};

    fn checked(behavior: Behavior) -> impl Checker<RunToCompletion> {
        RunToCompletion::new(behavior).checker().spawn_bfs().join()
    }

    /// The headline: the specified composition satisfies BIND-6 through BIND-10 over every
    /// interleaving of stream, ack, deadline, and kill outcomes, for every fate, and
    /// witnesses every case that makes that meaningful.
    #[test]
    fn the_specified_composition_satisfies_every_property() {
        let checker = checked(Behavior::Specified);
        checker.assert_properties();
        assert!(
            checker.unique_state_count() > 200,
            "a space this small could not reach the interleavings: {}",
            checker.unique_state_count()
        );
    }

    /// **The cut stream, rejected.** Returning what the stream saw reports a result nobody
    /// acked, with no status at all.
    #[test]
    fn a_cut_stream_that_returns_what_it_saw_is_caught() {
        let checker = checked(Behavior::CutReturnsWhatItSaw);
        for property in [
            "BIND-8 a result follows a successful ack or the synthesized timeout",
            "BIND-8 a stream that ends without its exit event falls back to wait and ack",
        ] {
            let steps = checker.assert_any_discovery(property).into_actions();
            assert!(
                steps.contains(&Action::StreamCut),
                "{property}: the counterexample needs a cut, got {steps:?}"
            );
        }
    }

    /// **The ack without a kill, rejected.** The grace ack of a command that outlives the
    /// deadline can only fail, so every such call synthesizes, and the ordering property
    /// finds the missing kill directly.
    #[test]
    fn a_deadline_that_acks_without_killing_is_caught() {
        let checker = checked(Behavior::AckWithoutKill);
        let steps = checker
            .assert_any_discovery("BIND-9 the post-deadline ack follows a kill")
            .into_actions();
        assert!(steps.contains(&Action::Deadline), "got {steps:?}");
        checker
            .assert_any_discovery("BIND-10 124 is synthesized only after the post-kill ack failed");
    }

    /// **The early synthesis, rejected.** A failed kill says nothing about whether the
    /// command finished; skipping the grace ack throws away a result that exists.
    #[test]
    fn synthesizing_on_a_failed_kill_is_caught() {
        let checker = checked(Behavior::SynthesizeOnFailedKill);
        for property in [
            "BIND-10 124 is synthesized only after the post-kill ack failed",
            "BIND-9 a failed kill still gets the post-kill ack",
        ] {
            let steps = checker.assert_any_discovery(property).into_actions();
            assert!(
                steps.contains(&Action::KillAnswered(KillAnswer::Failed)),
                "{property}: got {steps:?}"
            );
        }
    }

    /// **The issue's literal rule, rejected.** A child that traps SIGTERM and exits 0 after
    /// the daemon deadline reads as a success.
    #[test]
    fn the_literal_mapping_reports_a_timed_out_command_as_its_exit_code() {
        let checker = checked(Behavior::LiteralMapping);
        let path = checker.assert_any_discovery("BIND-6 a command a deadline ended reports 124");
        let last = path.last_state();
        assert!(
            matches!(last.fate, Fate::DaemonDeadlineExit(_)),
            "the counterexample is the SIGTERM-trapping child, got {last:?}"
        );
        checker.assert_no_discovery(
            "BIND-6 a signal death no deadline caused reports 128 plus the signal",
        );
    }

    /// **The harvester's rule, rejected.** An out-of-memory kill with no deadline reads as a
    /// timeout.
    #[test]
    fn the_harvester_mapping_reports_an_oom_kill_as_a_timeout() {
        let checker = checked(Behavior::HarvesterMapping);
        let path = checker.assert_any_discovery(
            "BIND-6 a signal death no deadline caused reports 128 plus the signal",
        );
        assert!(
            matches!(path.last_state().fate, Fate::Dies(SIGKILL)),
            "the counterexample is the SIGKILL with no deadline, got {:?}",
            path.last_state()
        );
    }

    /// **The silent truncation, rejected.**
    #[test]
    fn a_missing_truncation_note_is_caught() {
        let checker = checked(Behavior::SilentTruncation);
        checker.assert_any_discovery("BIND-7 a truncated result carries the truncation note");
        checker.assert_no_discovery("BIND-7 every deadline carries a note naming it");
    }

    /// The table `microvms-core`'s `posix_exit_code` tests mirror, one row per case.
    #[test]
    fn the_posix_exit_code_table() {
        let status = |exit_code, signal, timed_out| {
            Some(Status {
                exit_code,
                signal,
                timed_out,
                truncated: false,
            })
        };
        let client = |kill, synthesized| Some(ClientDeadline { kill, synthesized });
        let rows = [
            (status(Some(0), None, false), None, Some(0)),
            (status(Some(3), None, false), None, Some(3)),
            (
                status(None, Some(SIGKILL), false),
                None,
                Some(128 + SIGKILL),
            ),
            (
                status(None, Some(SIGSEGV), false),
                None,
                Some(128 + SIGSEGV),
            ),
            (status(None, Some(SIGTERM), true), None, Some(TIMED_OUT)),
            (status(None, Some(SIGKILL), true), None, Some(TIMED_OUT)),
            (status(Some(0), None, true), None, Some(TIMED_OUT)),
            (
                status(None, Some(SIGTERM), false),
                client(KillAnswer::Signalled, false),
                Some(TIMED_OUT),
            ),
            (
                status(Some(0), None, false),
                client(KillAnswer::AlreadyGone, false),
                Some(0),
            ),
            (
                status(None, Some(SIGSEGV), false),
                client(KillAnswer::Failed, false),
                Some(128 + SIGSEGV),
            ),
            (None, client(KillAnswer::Failed, true), Some(TIMED_OUT)),
            (None, client(KillAnswer::Signalled, true), Some(TIMED_OUT)),
            (None, None, None),
        ];
        for (status, client, expected) in rows {
            assert_eq!(
                posix_exit_code(status, client),
                expected,
                "{status:?} {client:?}"
            );
        }
    }
}
