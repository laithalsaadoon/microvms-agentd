// SPDX-License-Identifier: Apache-2.0
//! A scripted exec surface behind `HttpBackend`, on tokio's clock, for the
//! run-to-completion tiers (`bdd_run_to_completion.rs`, `run_to_completion_fuzz.rs`).
//!
//! # What it is, and what it is not
//!
//! It is not `agentd`. It answers the five routes `Session::run_to_completion` uses
//! (`start`, the SSE `stream`, `poll`, `ack`, `kill`) with the `protocol` crate's wire shapes,
//! so a field renamed there breaks this file's compilation the same way it breaks the
//! daemon's. The command's fate is declared by the scenario — when it finishes, how, and what
//! a kill does to it — and every network fault is one the scenario asked for, at the request
//! it named. So the harness is the clock: an ordering a scenario needs is caused rather than
//! hoped for, and it holds under any tick. What the daemon itself does with a real child is
//! owned by `agentd`'s own tiers and by the live check in `conformance/run_rs.py`.
//!
//! Time is `tokio::time::Instant`, so a caller running under a paused clock gets deadlines
//! that fire in virtual time and a run that takes minutes finishes in milliseconds.

use std::collections::HashMap;
use std::sync::{Arc, Mutex, PoisonError};
use std::time::Duration;

use base64::Engine as _;
use futures_util::future::BoxFuture;
use microvms_core::error::{Error, WireKind};
use microvms_core::prelude::*;
use microvms_core::protocol::exec as wire;
use microvms_core::session::{
    ChunkSource, HttpBackend, HttpRequest, HttpResponse, OpenStream, Session,
};
use tokio::time::Instant;

/// How the command ends by itself.
#[derive(Clone, Copy, Debug, Default)]
pub struct Ending {
    pub exit_code: Option<i32>,
    pub signal: Option<i32>,
    /// The daemon's own deadline fired.
    pub timed_out: bool,
}

/// What an attach does after replaying the command's output.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub enum StreamMode {
    /// Waits for the command to finish, then sends the `exit` event.
    #[default]
    Exit,
    /// Ends the body without an `exit` event, on every attach.
    Cut,
}

/// What `POST /kill` does.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum KillMode {
    /// A live group dies to SIGTERM this long after the kill.
    Ends(Duration),
    /// The request fails at the transport.
    Fails,
}

impl Default for KillMode {
    fn default() -> Self {
        KillMode::Ends(Duration::ZERO)
    }
}

/// The scenario: the command's fate and the faults the network adds.
#[derive(Clone, Debug, Default)]
pub struct Script {
    pub stdout: String,
    /// When the command finishes by itself, measured from its start. `None` is a command
    /// that only a kill ends.
    pub finishes_after: Option<Duration>,
    pub ending: Ending,
    pub truncated: bool,
    pub stream: StreamMode,
    pub kill: KillMode,
    /// How many acks fail at the transport before one succeeds.
    pub ack_failures: u32,
    /// How many polls fail at the transport before one succeeds.
    pub poll_failures: u32,
}

#[derive(Debug, Default)]
struct Live {
    started_at: Option<Instant>,
    exec_id: String,
    /// When a kill will end the group, and that it was the kill.
    killed_at: Option<Instant>,
    acked: bool,
    ack_failures: u32,
    poll_failures: u32,
}

/// One scripted daemon. `log` is every request that reached it, as `METHOD /path`.
///
/// The state is behind an `Arc` of its own so an attach's chunk source, which must be
/// `'static`, can hold it.
pub struct SimDaemon {
    inner: Arc<Inner>,
}

pub struct Inner {
    script: Script,
    live: Mutex<Live>,
    log: Mutex<Vec<String>>,
}

impl std::ops::Deref for SimDaemon {
    type Target = Inner;

    fn deref(&self) -> &Inner {
        &self.inner
    }
}

impl SimDaemon {
    pub fn new(script: Script) -> Arc<Self> {
        Arc::new(Self {
            inner: Arc::new(Inner {
                live: Mutex::new(Live {
                    ack_failures: script.ack_failures,
                    poll_failures: script.poll_failures,
                    ..Live::default()
                }),
                script,
                log: Mutex::new(Vec::new()),
            }),
        })
    }

    /// A direct session whose every request reaches this daemon.
    pub fn session(self: &Arc<Self>) -> Session {
        Session::builder("https://sim.invalid", "sim-agent-token")
            .with_backend(Arc::clone(self) as Arc<dyn HttpBackend>)
            .build()
            .expect("a session over the simulator")
    }
}

impl Inner {
    /// Whether an ack succeeded, which is what a result that is not synthesized must carry.
    pub fn acked(&self) -> bool {
        self.live().acked
    }

    /// Every request, in order, as `METHOD /path` with the query dropped.
    pub fn log(&self) -> Vec<String> {
        self.log
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .clone()
    }

    fn note(&self, request: &HttpRequest) {
        let path = request.path.split('?').next().unwrap_or_default();
        self.log
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .push(format!("{} {path}", request.method));
    }

    fn live(&self) -> std::sync::MutexGuard<'_, Live> {
        self.live.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// The daemon's report, once the command has finished.
    fn outcome(&self, now: Instant) -> Option<wire::Outcome> {
        let live = self.live();
        let started = live.started_at?;
        let own_end = self.script.finishes_after.map(|after| started + after);
        let kill_end = live.killed_at;
        let (ending, _) = match (own_end, kill_end) {
            (Some(own), Some(kill)) if kill < own => (killed_ending(), kill),
            (Some(own), _) => (self.script.ending, own),
            (None, Some(kill)) => (killed_ending(), kill),
            (None, None) => return None,
        };
        let finished_at = match (own_end, kill_end) {
            (Some(own), Some(kill)) => own.min(kill),
            (Some(own), None) => own,
            (None, Some(kill)) => kill,
            (None, None) => unreachable!(),
        };
        (now >= finished_at).then(|| wire::Outcome {
            exit_code: ending.exit_code,
            signal: ending.signal,
            timed_out: ending.timed_out,
            stdout: self.script.stdout.clone(),
            stderr: String::new(),
            truncated: self.script.truncated,
            writers_may_be_alive: false,
        })
    }

    fn poll_body(&self, now: Instant, acked: bool) -> wire::PollResponse {
        let exec_id = self.live().exec_id.clone();
        match self.outcome(now) {
            Some(_) if acked => wire::PollResponse {
                exec_id,
                phase: wire::Phase::Acked,
                result: None,
            },
            Some(outcome) => wire::PollResponse {
                exec_id,
                phase: wire::Phase::Exited,
                result: Some(outcome),
            },
            None => wire::PollResponse {
                exec_id,
                phase: wire::Phase::Running,
                result: None,
            },
        }
    }

    fn answer(&self, request: &HttpRequest) -> Result<HttpResponse, Error> {
        let now = Instant::now();
        let path = request.path.split('?').next().unwrap_or_default();
        match (request.method, path) {
            ("POST", "/v1/exec/start") => {
                let start: wire::StartRequest =
                    serde_json::from_slice(&request.body).expect("a start body");
                let mut live = self.live();
                live.started_at.get_or_insert(now);
                live.exec_id = start.exec_id.clone();
                drop(live);
                ok(&wire::StartResponse {
                    exec_id: start.exec_id,
                    phase: wire::Phase::Running,
                })
            }
            ("POST", ack) if ack.ends_with("/ack") => {
                let mut live = self.live();
                if live.ack_failures > 0 {
                    live.ack_failures -= 1;
                    return Err(Error::wire(WireKind::Transport, "sim: ack reset"));
                }
                drop(live);
                match self.outcome(now) {
                    None => status(409, "not_exited", "the exec is still running"),
                    Some(_) if self.live().acked => {
                        status(409, "already_acked", "an earlier ack released the output")
                    }
                    Some(outcome) => {
                        self.live().acked = true;
                        ok(&wire::PollResponse {
                            exec_id: self.live().exec_id.clone(),
                            phase: wire::Phase::Acked,
                            result: Some(outcome),
                        })
                    }
                }
            }
            ("POST", kill) if kill.ends_with("/kill") => match self.script.kill {
                KillMode::Fails => Err(Error::wire(WireKind::Transport, "sim: kill reset")),
                KillMode::Ends(after) => {
                    let exec_id = self.live().exec_id.clone();
                    let alive = self.outcome(now).is_none();
                    if alive {
                        let mut live = self.live();
                        live.killed_at.get_or_insert(now + after);
                    }
                    ok(&wire::KillResponse {
                        exec_id,
                        killed: alive,
                    })
                }
            },
            ("GET", poll) if poll.starts_with("/v1/exec/") => {
                let mut live = self.live();
                if live.poll_failures > 0 {
                    live.poll_failures -= 1;
                    return Err(Error::wire(WireKind::Transport, "sim: poll reset"));
                }
                let acked = live.acked;
                drop(live);
                ok(&self.poll_body(now, acked))
            }
            (method, path) => panic!("the simulator has no route {method} {path}"),
        }
    }
}

fn killed_ending() -> Ending {
    Ending {
        exit_code: None,
        signal: Some(15),
        timed_out: false,
    }
}

fn ok(body: &impl serde::Serialize) -> Result<HttpResponse, Error> {
    Ok(HttpResponse {
        status: 200,
        headers: HashMap::new(),
        body: serde_json::to_vec(body).expect("a body"),
    })
}

fn status(code: u16, error: &'static str, detail: &str) -> Result<HttpResponse, Error> {
    Ok(HttpResponse {
        status: code,
        headers: HashMap::new(),
        body: serde_json::to_vec(&wire::ErrorBody {
            error: error.into(),
            detail: detail.into(),
        })
        .expect("a body"),
    })
}

impl HttpBackend for SimDaemon {
    fn send(&self, request: HttpRequest) -> BoxFuture<'_, Result<HttpResponse, Error>> {
        self.note(&request);
        let answer = self.answer(&request);
        Box::pin(async move { answer })
    }

    fn open_stream(
        &self,
        request: HttpRequest,
        _idle_timeout: Duration,
    ) -> BoxFuture<'_, Result<OpenStream, Error>> {
        self.note(&request);
        let offset: u64 = request
            .path
            .split("offset=")
            .nth(1)
            .and_then(|offset| offset.parse().ok())
            .unwrap_or(0);
        Box::pin(async move {
            let head = HttpResponse {
                status: 200,
                headers: HashMap::new(),
                body: Vec::new(),
            };
            Ok((
                head,
                Box::new(Attach {
                    daemon: Arc::clone(&self.inner),
                    offset,
                    sent_output: false,
                    done: false,
                }) as Box<dyn ChunkSource>,
            ))
        })
    }
}

/// One attach: the output from the requested offset, then the scripted ending.
struct Attach {
    daemon: Arc<Inner>,
    offset: u64,
    sent_output: bool,
    done: bool,
}

impl ChunkSource for Attach {
    fn next_chunk(&mut self) -> BoxFuture<'_, Result<Option<Vec<u8>>, Error>> {
        Box::pin(async move {
            if self.done {
                return Ok(None);
            }
            let stdout = self.daemon.script.stdout.as_bytes();
            if !self.sent_output {
                self.sent_output = true;
                let from = (self.offset as usize).min(stdout.len());
                if from < stdout.len() {
                    let encoded = base64::engine::general_purpose::STANDARD.encode(&stdout[from..]);
                    return Ok(Some(
                        format!(
                            "event: output\ndata: {{\"offset\":{from},\"stream\":\"stdout\",\
                             \"output\":\"{encoded}\"}}\n\n"
                        )
                        .into_bytes(),
                    ));
                }
            }
            if self.daemon.script.stream == StreamMode::Cut {
                self.done = true;
                return Ok(None);
            }
            loop {
                if let Some(outcome) = self.daemon.outcome(Instant::now()) {
                    self.done = true;
                    let exit = wire::ExitEvent {
                        exit_code: outcome.exit_code,
                        signal: outcome.signal,
                        timed_out: outcome.timed_out,
                        truncated: outcome.truncated,
                        writers_may_be_alive: false,
                        offset: stdout.len() as u64,
                    };
                    let data = serde_json::to_string(&exit).expect("an exit event");
                    return Ok(Some(format!("event: exit\ndata: {data}\n\n").into_bytes()));
                }
                tokio::time::sleep(Duration::from_millis(100)).await;
            }
        })
    }
}
