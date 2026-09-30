// SPDX-License-Identifier: Apache-2.0
//! The bootstrap and exec model, replayed against the daemon's real routes.
//!
//! `agentd-model` proves its properties over every interleaving of platform, client and in-VM
//! attacker, but of the model: a handler that stopped making the model's transitions would
//! leave `cargo test -p agentd-model` green. These tests tie the two. Stateright walks
//! `Config::deployment_invariant_held()` breadth first, and `PathRecorder` keeps one path to
//! each reachable state; [`walk`] adds a path for each step the daemon could tell apart that
//! none of those takes. Each path is replayed from a fresh `AppState` through
//! `routes::app(state).oneshot(request)`, the shape `tests/bdd_exec_start.rs` drives, and after
//! every step the daemon has to show what the model's next state holds: the response class
//! (200, 409, 401 or 503), which token is installed, each exec id's phase, how many children
//! the id has spawned, and a polled result exactly while the child has exited and the model
//! still holds its output.
//!
//! The walk is replayed in [`SLICES`] runs of its order, each its own test, so the harness
//! runs them at once. `the_slices_replay_every_walked_path_once` holds them to the walk: each
//! path is in one slice, each slice is one test's, and the steps they take cover [`COVERED`].
//!
//! The replay doesn't see the ack release output. `exec::poll` hides the result of any acked
//! entry, whatever the entry still holds, and a second ack answers 409 either way, so an ack
//! that kept its buffer answers every request the same as one that freed it. What the replay
//! holds is the wire: the ack carries the output, and no later poll reports it.
//!
//! # How the model's actions map onto the daemon
//!
//! - `RunHook` posts the run hook with the principal's token. The principal itself is ghost
//!   state: the daemon can't tell the platform from an in-VM process, which is the model's
//!   point, so only the token reaches the request.
//! - `ExecStart`, `ExecPoll` and `ExecAck` are the control routes, each with the caller's
//!   bearer token. A start runs [`SPAWN_SCRIPT`]: it appends a line to its id's marker file,
//!   so the replay can count the children an id spawned, and then becomes `cat` with a stdin
//!   pipe, so the child runs until the replay decides it exits.
//! - `Collect` isn't a route: the daemon's own loop calls `exec::collect_expired` on a TTL.
//!   The replay calls it directly, with `exec_ttl` zero so every acked entry is due, and only
//!   when the model's caller was authorized. The caller's authorization is observed through a
//!   read-only poll of an id no start ever names, which answers 404 once the token is accepted.
//! - `ChildExit` ends the `cat`: an even id gets its own id on stdin and then EOF, so its
//!   output is known; an odd id is killed. The replay waits for the poll to report `exited`,
//!   which is when the daemon has the result to hand back.

use std::collections::{BTreeSet, HashSet};
use std::ops::Range;
use std::sync::LazyLock;
use std::time::{Duration, Instant};

use agentd::exec::{self, Outcome, Phase, PollResponse};
use agentd::{AppState, Config, routes};
use agentd_model::{Action, Agentd, Boot, ControlOp, ExecPhase, Response, State, Token};
use axum::body::Body;
use axum::http::{Request, StatusCode};
use base64::Engine as _;
use futures_util::StreamExt as _;
use serde_json::{Value, json};
use stateright::{Checker, Expectation, Model, PathRecorder};
use tower::ServiceExt;

/// The runs the walk is cut into, each replayed by its own test.
const SLICES: usize = 8;

/// Paths one slice replays at once. Each owns its daemon, so they share nothing but the host.
/// The slices run at once too, so the bound is on their product, which keeps the live `cat`
/// children to a few dozen. It's low because a path waiting on a child polls for it: sixteen
/// a slice took more than twice as long as two a slice, on the 16-core devbox at load average
/// 22 (2026-09-30).
const CONCURRENT_PATHS: usize = 2;

/// How long a child gets to show `exited` after its stdin closes or it's killed, and how long
/// a spawned child gets to write its marker line.
const EXIT_DEADLINE: Duration = Duration::from_secs(10);

/// An exec id no model action names. Polling it asks only whether the caller is authorized.
const PROBE_ID: &str = "model-authorization-probe";

/// What every start runs, with its id's marker file as `$0`. The line is written before
/// `exec`, so a child that's still `sh` hasn't counted itself yet, and one that has become
/// `cat` has. `exec` keeps the pid, so a kill still reaches the `cat`.
const SPAWN_SCRIPT: &str = "echo spawned >> \"$0\"; exec cat";

/// One path: each state paired with the action taken from it, the last with none.
type Path = Vec<(State, Option<Action>)>;

/// Every (action, response) pair the walk has to take, and so the replay has to drive. A walk
/// that shrank to a corner of the model would pass every step it took; this is what it would
/// miss. A retried start is split by the phase it finds, since each phase is a different way
/// for the daemon to decide the id is taken.
const COVERED: [&str; 19] = [
    "run-hook 200",
    "run-hook 409",
    "exec-start 200",
    "exec-start 200 retried running",
    "exec-start 200 retried exited",
    "exec-start 200 retried acked",
    "exec-start 401",
    "exec-start 503",
    "exec-poll 200",
    "exec-poll 401",
    "exec-ack 200",
    "exec-ack 409",
    "exec-ack 401",
    "collect 200",
    "collect 200 removed",
    "collect 401",
    "collect 503",
    "child-exit eof",
    "child-exit kill",
];

fn token(token: Token) -> &'static str {
    match token {
        Token::Harness => "model-harness-token",
        Token::Attacker => "model-attacker-token",
    }
}

fn exec_id(id: u8) -> String {
    format!("model-exec-{id}")
}

fn exec_ids() -> u8 {
    agentd_model::Config::deployment_invariant_held().exec_ids
}

fn status(response: Response) -> StatusCode {
    match response {
        Response::Ok => StatusCode::OK,
        Response::Conflict => StatusCode::CONFLICT,
        Response::Unauthorized => StatusCode::UNAUTHORIZED,
        Response::Unavailable => StatusCode::SERVICE_UNAVAILABLE,
    }
}

fn phase(phase: ExecPhase) -> Phase {
    match phase {
        ExecPhase::Running => Phase::Running,
        ExecPhase::Exited => Phase::Exited,
        ExecPhase::Acked => Phase::Acked,
    }
}

/// Whether `ChildExit` ends this id's `cat` by closing its stdin (even ids) rather than by a
/// kill (odd ids), so both ways a child ends are replayed.
fn ends_by_eof(id: u8) -> bool {
    id.is_multiple_of(2)
}

/// What an even id's `cat` echoes before its EOF.
fn echoed(id: u8) -> String {
    format!("{}\n", exec_id(id))
}

/// What the daemon holds of a model state: the installed token, and each exec's phase,
/// whether its output is held, and how many children it spawned (through the marker file).
/// Who installed the token, how often a start was called, and the model's audit flags are
/// ghost state; the daemon keeps none of them.
type Observable = (Option<Token>, Vec<(u8, ExecPhase, bool, u8)>);

fn observable(state: &State) -> Observable {
    let installed = match state.boot {
        Boot::Uninitialized => None,
        Boot::Ready { token, .. } => Some(token),
    };
    let mut execs: Vec<_> = state
        .execs
        .iter()
        .map(|e| (e.id, e.phase, e.output_held, e.spawns))
        .collect();
    execs.sort_by_key(|&(id, ..)| id);
    (installed, execs)
}

/// The children the model's entry for `id` has spawned, zero with no entry.
fn spawns(state: &State, id: u8) -> usize {
    state
        .execs
        .iter()
        .find(|e| e.id == id)
        .map_or(0, |e| usize::from(e.spawns))
}

/// A model action as the daemon receives it: the sender's principal doesn't reach the request.
#[derive(Clone, Copy, Eq, Hash, PartialEq)]
enum Sent {
    Hook(Token),
    Control(Token, ControlOp),
    ChildExit(u8),
}

fn sent(action: Action) -> Sent {
    match action {
        Action::RunHook { token, .. } => Sent::Hook(token),
        Action::Control { token, op, .. } => Sent::Control(token, op),
        Action::ChildExit(id) => Sent::ChildExit(id),
    }
}

/// The `COVERED` label of one model step, read off the model alone.
fn label(before: &State, action: Action, after: &State) -> String {
    let code = status(after.last.expect("a taken action records its response").1).as_u16();
    match action {
        Action::RunHook { .. } => format!("run-hook {code}"),
        Action::Control { op, .. } => match op {
            ControlOp::ExecStart(id) => match before.execs.iter().find(|e| e.id == id) {
                Some(retried) if code == 200 => {
                    let found = match retried.phase {
                        ExecPhase::Running => "running",
                        ExecPhase::Exited => "exited",
                        ExecPhase::Acked => "acked",
                    };
                    format!("exec-start 200 retried {found}")
                }
                _ => format!("exec-start {code}"),
            },
            ControlOp::ExecPoll(_) => format!("exec-poll {code}"),
            ControlOp::ExecAck(_) => format!("exec-ack {code}"),
            ControlOp::Collect
                if code == 200 && before.execs.iter().any(|e| e.phase == ExecPhase::Acked) =>
            {
                "collect 200 removed".to_string()
            }
            ControlOp::Collect => format!("collect {code}"),
        },
        Action::ChildExit(id) if ends_by_eof(id) => "child-exit eof".to_string(),
        Action::ChildExit(_) => "child-exit kill".to_string(),
    }
}

/// The walk: one path to each reachable state of the correct deployment, then one path for
/// each step the daemon could tell apart that none of those takes, all sorted shortest first.
///
/// `PathRecorder` keeps one path per state, so a step into a state a shorter path already
/// reached is never replayed. Almost every such step is one the daemon can't tell from a step
/// some path does take: the same request against the same observable state. The few left each
/// get the shortest recorded path to a state they leave from, plus the step. A `Collect` that
/// removes an acked entry is one of them: every state it reaches has a shorter path.
fn walk() -> (Vec<Path>, usize) {
    let model = Agentd::new(agentd_model::Config::deployment_invariant_held());
    let (recorder, accessor) = PathRecorder::new_with_accessor();
    model.clone().checker().visitor(recorder).spawn_bfs().join();
    let mut paths: Vec<Path> = accessor().into_iter().map(|path| path.into_vec()).collect();
    // The recorder is a set. Sorted, shortest first, so the path added for a step is the
    // shortest there is and a failure names the same path on every run.
    let order = |path: &Path| (path.len(), format!("{path:?}"));
    paths.sort_by_cached_key(order);
    let recorded = paths.len();

    let mut taken: HashSet<(Observable, Sent)> = paths
        .iter()
        .flatten()
        .filter_map(|(state, action)| action.map(|action| (observable(state), sent(action))))
        .collect();
    let mut untaken = Vec::new();
    for path in &paths {
        let (last, _) = path
            .last()
            .expect("a path holds at least its initial state");
        let mut actions = Vec::new();
        model.actions(last, &mut actions);
        for action in actions {
            let Some(next) = model.next_state(last, action) else {
                continue;
            };
            if taken.insert((observable(last), sent(action))) {
                let mut longer = path.clone();
                longer.last_mut().expect("not empty").1 = Some(action);
                longer.push((next, None));
                untaken.push(longer);
            }
        }
    }
    paths.extend(untaken);
    // Sorted in with the rest, not left at the end: they're all short, and a fault only one
    // of them reaches (collection keeping an acked entry) then fails in the first seconds
    // rather than after every recorded path.
    paths.sort_by_cached_key(order);
    (paths, recorded)
}

/// A fresh daemon for one path.
struct Daemon {
    state: AppState,
    /// `routes::app` over `state`, built once: assembling it walks the published route list,
    /// and a path takes a few dozen requests.
    app: axum::Router,
    /// This path's marker files, one per exec id, each a line per spawned child.
    markers: tempfile::TempDir,
}

impl Daemon {
    fn new(scratch: &std::path::Path) -> Self {
        let config = Config {
            // Every acked entry is due the moment `Collect` runs, as the model's is.
            exec_ttl: Duration::ZERO,
            kill_grace: Duration::from_secs(2),
            // No handler runs: the directory doesn't exist.
            hooks_dir: scratch.join("hooks.d"),
            ..Config::default()
        };
        let state = AppState::new(config);
        Self {
            app: routes::app(state.clone()),
            state,
            markers: tempfile::tempdir_in(scratch).expect("a marker directory"),
        }
    }

    fn marker(&self, id: u8) -> std::path::PathBuf {
        self.markers.path().join(exec_id(id))
    }

    /// How many children `id` has spawned so far, by its marker file's lines.
    fn spawned(&self, id: u8) -> usize {
        std::fs::read_to_string(self.marker(id)).map_or(0, |text| text.lines().count())
    }

    /// The spawn count of `id` once it reaches `expected`, or when the deadline passes. A
    /// child writes its line just after its start answers, so a count below the model's can
    /// be a line on its way; one above it can't.
    async fn spawned_by(&self, id: u8, expected: usize) -> usize {
        let deadline = Instant::now() + EXIT_DEADLINE;
        loop {
            let count = self.spawned(id);
            if count >= expected || Instant::now() > deadline {
                return count;
            }
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
    }

    async fn call(&self, request: Request<Body>) -> (StatusCode, Value) {
        let response = self
            .app
            .clone()
            .oneshot(request)
            .await
            .expect("the router answers");
        let status = response.status();
        let bytes = axum::body::to_bytes(response.into_body(), 1 << 20)
            .await
            .expect("a body");
        (
            status,
            serde_json::from_slice(&bytes).unwrap_or(Value::Null),
        )
    }

    async fn run_hook(&self, presented: Token) -> (StatusCode, Value) {
        let body = json!({
            "runHookPayload": json!({"agent_token": token(presented)}).to_string(),
        });
        let request = Request::post(format!("{}/run", routes::HOOK_PREFIX))
            .header("content-type", "application/json")
            .body(Body::from(body.to_string()))
            .expect("a request");
        self.call(request).await
    }

    async fn post(&self, presented: Token, path: &str, body: Value) -> (StatusCode, Value) {
        let request = Request::post(path)
            .header("authorization", format!("Bearer {}", token(presented)))
            .header("content-type", "application/json")
            .body(Body::from(body.to_string()))
            .expect("a request");
        self.call(request).await
    }

    async fn get(&self, presented: Token, path: &str) -> (StatusCode, Value) {
        let request = Request::get(path)
            .header("authorization", format!("Bearer {}", token(presented)))
            .body(Body::empty())
            .expect("a request");
        self.call(request).await
    }

    async fn poll(&self, presented: Token, id: &str) -> (StatusCode, Value) {
        self.get(presented, &format!("/v1/exec/{id}")).await
    }

    /// Waits for `id` to poll `exited`, after its stdin closed or it was killed.
    async fn exited(&self, id: u8, how: &str) -> Result<(), String> {
        let deadline = Instant::now() + EXIT_DEADLINE;
        loop {
            let (_, body) = self.poll(Token::Harness, &exec_id(id)).await;
            if polled(&body).is_some_and(|poll| poll.phase == Phase::Exited) {
                return Ok(());
            }
            if Instant::now() > deadline {
                return Err(format!(
                    "exec {id} didn't reach exited within {EXIT_DEADLINE:?} of its {how}: {body}"
                ));
            }
            tokio::time::sleep(Duration::from_millis(2)).await;
        }
    }

    /// Ends a running `cat` the way `ChildExit` does for its id, and waits for `exited`.
    async fn finish(&self, id: u8) -> Result<(StatusCode, &'static str), String> {
        let (answer, how) = self.end(id).await;
        if answer != StatusCode::OK {
            return Err(format!("ending exec {id} by {how} answered {answer}"));
        }
        self.exited(id, how).await?;
        Ok((answer, how))
    }

    /// Closes the `cat`'s stdin after its echo, or kills it, by the id's parity.
    async fn end(&self, id: u8) -> (StatusCode, &'static str) {
        if ends_by_eof(id) {
            (self.eof(id, &echoed(id)).await, "eof")
        } else {
            let path = format!("/v1/exec/{}/kill", exec_id(id));
            let (answer, _) = self.post(Token::Harness, &path, json!({})).await;
            (answer, "kill")
        }
    }

    /// Writes `data` to the `cat`'s stdin and closes it.
    async fn eof(&self, id: u8, data: &str) -> StatusCode {
        let path = format!("/v1/exec/{}/stdin", exec_id(id));
        let data = base64::engine::general_purpose::STANDARD.encode(data);
        let (answer, _) = self
            .post(
                Token::Harness,
                &path,
                json!({"data_b64": data, "signal": "eof"}),
            )
            .await;
        answer
    }

    /// Takes one model action and holds the daemon's answer to the model's response class.
    async fn apply(
        &self,
        before: &State,
        action: Action,
        expected: StatusCode,
    ) -> Result<(), String> {
        let answered = |answer: StatusCode| {
            if answer == expected {
                Ok(())
            } else {
                Err(format!(
                    "answered {answer}; the model's response is {expected}"
                ))
            }
        };
        match action {
            Action::RunHook {
                token: presented, ..
            } => answered(self.run_hook(presented).await.0),
            Action::Control {
                token: presented,
                op,
                ..
            } => match op {
                ControlOp::ExecStart(id) => {
                    let marker = self.marker(id);
                    let command = json!(["sh", "-c", SPAWN_SCRIPT, marker]);
                    let (answer, _) = self
                        .post(
                            presented,
                            "/v1/exec/start",
                            json!({"exec_id": exec_id(id), "command": command, "stdin": true}),
                        )
                        .await;
                    answered(answer)
                }
                ControlOp::ExecPoll(id) => answered(self.poll(presented, &exec_id(id)).await.0),
                ControlOp::ExecAck(id) => {
                    let (answer, body) = self
                        .post(
                            presented,
                            &format!("/v1/exec/{}/ack", exec_id(id)),
                            json!({}),
                        )
                        .await;
                    answered(answer)?;
                    if answer == StatusCode::OK {
                        // The ack is where output is released, so it has to carry it.
                        check_outcome(id, polled(&body).and_then(|poll| poll.result).as_ref())
                            .map_err(|problem| format!("the ack's result: {problem}"))?;
                    }
                    Ok(())
                }
                ControlOp::Collect => {
                    let (probe, _) = self.poll(presented, PROBE_ID).await;
                    let answer = match probe {
                        StatusCode::NOT_FOUND => StatusCode::OK,
                        refused => refused,
                    };
                    answered(answer)?;
                    if answer != StatusCode::OK {
                        return Ok(());
                    }
                    let removed = exec::collect_expired(&self.state);
                    let due = before
                        .execs
                        .iter()
                        .filter(|e| e.phase == ExecPhase::Acked)
                        .count();
                    if removed != due {
                        return Err(format!(
                            "collect_expired removed {removed} entries; the model collects {due}"
                        ));
                    }
                    Ok(())
                }
            },
            Action::ChildExit(id) => answered(self.finish(id).await?.0),
        }
    }

    /// Holds the daemon to the model's state: the installed token, then each exec id, then
    /// each id's spawn count against `spawned`, the children the model's path has spawned.
    async fn observe(&self, model: &State, spawned: &[usize]) -> Result<(), String> {
        let (installed, _) = observable(model);
        match installed {
            None => {
                if self
                    .state
                    .token_matches(token(Token::Harness).as_bytes())
                    .is_some()
                {
                    return Err("a token is installed; the model has none".into());
                }
            }
            Some(installed) => {
                if self.state.token_matches(token(installed).as_bytes()) != Some(true) {
                    return Err(format!(
                        "the installed token isn't the model's {installed:?}"
                    ));
                }
            }
        }

        for id in 0..exec_ids() {
            let Some(reader) = installed else {
                let (answer, _) = self.poll(Token::Harness, &exec_id(id)).await;
                if answer != StatusCode::SERVICE_UNAVAILABLE {
                    return Err(format!("exec {id} polled {answer} before bootstrap"));
                }
                continue;
            };
            let (answer, body) = self.poll(reader, &exec_id(id)).await;
            let Some(exec) = model.execs.iter().find(|e| e.id == id) else {
                if answer != StatusCode::NOT_FOUND {
                    return Err(format!("exec {id} polled {answer}; the model has no entry"));
                }
                continue;
            };
            let poll = polled(&body);
            if answer != StatusCode::OK
                || poll.as_ref().map(|poll| poll.phase) != Some(phase(exec.phase))
            {
                return Err(format!(
                    "exec {id} polled {answer} {body}; the model's is {:?}",
                    exec.phase
                ));
            }
            // A poll reports output only while the model holds it and the child has exited:
            // nothing while running, and nothing once acked (see the module doc for why that
            // isn't the release itself).
            let readable = exec.output_held && exec.phase == ExecPhase::Exited;
            let result = poll.and_then(|poll| poll.result);
            match (readable, result.is_none()) {
                (true, false) => check_outcome(id, result.as_ref())
                    .map_err(|problem| format!("exec {id}'s polled result: {problem}"))?,
                (false, true) => {}
                (true, true) => {
                    return Err(format!("exec {id} is exited and held, but polls no result"));
                }
                (false, false) => {
                    return Err(format!(
                        "exec {id} polls a result the model doesn't hold ({:?}, held {})",
                        exec.phase, exec.output_held
                    ));
                }
            }
        }
        self.count_spawns(spawned).await
    }

    /// Each id's marker lines against the children the model spawned for it.
    async fn count_spawns(&self, spawned: &[usize]) -> Result<(), String> {
        for (id, &expected) in (0..exec_ids()).zip(spawned) {
            let count = self.spawned_by(id, expected).await;
            if count != expected {
                return Err(format!(
                    "exec {id} spawned {count} children; the model spawned {expected}"
                ));
            }
        }
        Ok(())
    }

    /// After a path that matched: ends every child the model still has running by EOF, waits
    /// for each to exit, and counts spawns once more. An EOF, unlike a kill, can't land before
    /// the child's marker line, so a child spawned by the path's last step has counted itself
    /// by the time it's exited.
    async fn drain(&self, last: &State, spawned: &[usize]) -> Result<(), String> {
        for exec in last.execs.iter().filter(|e| e.phase == ExecPhase::Running) {
            let answer = self.eof(exec.id, "").await;
            if answer != StatusCode::OK {
                return Err(format!(
                    "closing exec {}'s stdin answered {answer}",
                    exec.id
                ));
            }
            self.exited(exec.id, "eof").await?;
        }
        self.count_spawns(spawned).await
    }

    /// Ends every child the model still has running, so a replayed path leaves no `cat`
    /// behind. It doesn't wait: after a divergence the daemon's phase needn't be the model's.
    async fn close(&self, last: &State) {
        for exec in last.execs.iter().filter(|e| e.phase == ExecPhase::Running) {
            self.end(exec.id).await;
        }
    }
}

/// A poll or an ack body, read through the wire type: `PollResponse` flattens the outcome
/// into the body, so there's no `result` key to look for.
fn polled(body: &Value) -> Option<PollResponse> {
    serde_json::from_value(body.clone()).ok()
}

/// The result an ended `cat` has to report: its own echo for an even id, a signal for an odd.
fn check_outcome(id: u8, result: Option<&Outcome>) -> Result<(), String> {
    let Some(outcome) = result else {
        return Err("no result".into());
    };
    if ends_by_eof(id) {
        if outcome.stdout != echoed(id) || outcome.exit_code != Some(0) {
            return Err(format!(
                "expected exit 0 and {:?}, got {outcome:?}",
                echoed(id)
            ));
        }
    } else if outcome.signal.is_none() {
        return Err(format!("expected a signal, got {outcome:?}"));
    }
    Ok(())
}

/// Replays one path.
async fn replay(path: &Path, scratch: &std::path::Path) -> Result<(), String> {
    let daemon = Daemon::new(scratch);
    let actions = || -> Vec<Action> { path.iter().filter_map(|(_, action)| *action).collect() };
    // The children the model has spawned per id over the whole path. An entry's own counter
    // goes with the entry when it's collected, and a new start of the same id adds to the
    // marker file the old child wrote.
    let mut spawned = vec![0; usize::from(exec_ids())];
    let mut result = Ok(());
    for (step, pair) in path.windows(2).enumerate() {
        let [(before, Some(action)), (after, _)] = pair else {
            unreachable!("every state but the last carries the action taken from it");
        };
        let expected = status(after.last.expect("a taken action records its response").1);
        for (id, count) in (0..exec_ids()).zip(&mut spawned) {
            *count += spawns(after, id).saturating_sub(spawns(before, id));
        }
        let outcome = match daemon.apply(before, *action, expected).await {
            Ok(()) => daemon.observe(after, &spawned).await,
            Err(problem) => Err(problem),
        };
        if let Err(problem) = outcome {
            result = Err(format!(
                "step {step} ({action:?}): {problem}\n  path: {:?}\n  model state after: \
                 {after:?}",
                actions()
            ));
            break;
        }
    }
    let Some((last, _)) = path.last() else {
        return result;
    };
    match result {
        Ok(()) => daemon
            .drain(last, &spawned)
            .await
            .map_err(|problem| format!("after the last step: {problem}\n  path: {:?}", actions())),
        Err(failure) => {
            daemon.close(last).await;
            Err(failure)
        }
    }
}

/// The walk, taken once per test process: the slices replay shares of the same paths, and the
/// partition check reads them too.
struct Walk {
    paths: Vec<Path>,
    /// How many of `paths` the recorder kept; the rest are for untaken steps.
    recorded: usize,
    took: Duration,
}

static WALK: LazyLock<Walk> = LazyLock::new(|| {
    let started = Instant::now();
    let (paths, recorded) = walk();
    Walk {
        paths,
        recorded,
        took: started.elapsed(),
    }
});

/// Where slice `k` of `paths` starts: the first path at which the paths before it hold at least
/// `k`/[`SLICES`] of the walk's states. Slices are runs of the walk's order, so its shortest
/// paths, where a fault fails soonest, are all slice 0's. They're cut by states rather than by
/// paths because a later slice's paths are longer: a path's states count its steps and the
/// fresh daemon it starts with.
fn slice_start(paths: &[Path], k: usize) -> usize {
    let states: usize = paths.iter().map(Vec::len).sum();
    let mut before = 0;
    for (index, path) in paths.iter().enumerate() {
        if before * SLICES >= states * k {
            return index;
        }
        before += path.len();
    }
    paths.len()
}

/// The paths slice `k` replays, by index into the walk: from its start to the next slice's.
fn slice(paths: &[Path], k: usize) -> Range<usize> {
    slice_start(paths, k)..slice_start(paths, k + 1)
}

/// The slice each test in `items` replays: the number it passes to [`replay_slice`], by the
/// test's name.
fn replayed_slices(items: &[syn::Item]) -> Vec<(String, usize)> {
    items
        .iter()
        .filter_map(|item| match item {
            syn::Item::Fn(function) if is_test(&function.attrs) => Some(function),
            _ => None,
        })
        .flat_map(|function| {
            function.block.stmts.iter().filter_map(|statement| {
                let syn::Stmt::Expr(syn::Expr::Await(awaited), _) = statement else {
                    return None;
                };
                let syn::Expr::Call(call) = &*awaited.base else {
                    return None;
                };
                let syn::Expr::Path(callee) = &*call.func else {
                    return None;
                };
                if !callee.path.is_ident("replay_slice") || call.args.len() != 1 {
                    return None;
                }
                let Some(syn::Expr::Lit(syn::ExprLit {
                    lit: syn::Lit::Int(k),
                    ..
                })) = call.args.first()
                else {
                    return None;
                };
                Some((function.sig.ident.to_string(), k.base10_parse().ok()?))
            })
        })
        .collect()
}

/// Replays slice `k` of the walk against the real routes, holding the daemon to the model's
/// responses, token, phases, spawn counts and output.
async fn replay_slice(k: usize) {
    let started = Instant::now();
    let paths = &WALK.paths;
    let range = slice(paths, k);
    assert!(
        !range.is_empty(),
        "slice {k} holds no path, so it replays nothing"
    );
    let replayed = &paths[range.clone()];
    let scratch = tempfile::tempdir().expect("a scratch directory");
    let mut replays = futures_util::stream::iter(replayed)
        .map(|path| replay(path, scratch.path()))
        // In the walk's order, shortest first, so the failure reported is the slice's shortest.
        .buffered(CONCURRENT_PATHS);
    while let Some(outcome) = replays.next().await {
        // The first divergence ends the run. The replays still in flight go with the stream,
        // and the runtime's shutdown drops each waiter task with the stdin it holds, so every
        // `cat` left sees EOF.
        if let Err(failure) = outcome {
            panic!("a replayed path diverged from the model: {failure}");
        }
    }
    let steps: usize = replayed.iter().map(|path| path.len() - 1).sum();
    eprintln!(
        "slice {k}: replayed paths {range:?} of {} ({steps} steps) in {:?}",
        paths.len(),
        started.elapsed()
    );
}

/// Slice 0 of the walk: its shortest paths, where a fault fails first.
///
/// It's the daemon's side of AGENTD-1: every control request the model sends before bootstrap
/// is one it answers `Unavailable`, and each is replayed to a daemon that has to answer 503.
/// The model can send each from its initial state, and a one-step path sorts ahead of every
/// longer one, so each is this slice's.
///
/// **Falsification**: let `exec::start` spawn again for a retried id whose child is still
/// running (`contains_key` to a check that's true only once the entry has exited or been
/// acked) and the path that retries a running start fails on its spawn count, while
/// `cargo test -p agentd-model` and every agentd test outside this file stay green.
#[tokio::test]
async fn slice_0_of_the_walk_replays_against_the_daemon() {
    replay_slice(0).await;
}

/// Slice 1 of the walk; see [`replay_slice`].
#[tokio::test]
async fn slice_1_of_the_walk_replays_against_the_daemon() {
    replay_slice(1).await;
}

/// Slice 2 of the walk; see [`replay_slice`].
#[tokio::test]
async fn slice_2_of_the_walk_replays_against_the_daemon() {
    replay_slice(2).await;
}

/// Slice 3 of the walk; see [`replay_slice`].
#[tokio::test]
async fn slice_3_of_the_walk_replays_against_the_daemon() {
    replay_slice(3).await;
}

/// Slice 4 of the walk; see [`replay_slice`].
#[tokio::test]
async fn slice_4_of_the_walk_replays_against_the_daemon() {
    replay_slice(4).await;
}

/// Slice 5 of the walk; see [`replay_slice`].
#[tokio::test]
async fn slice_5_of_the_walk_replays_against_the_daemon() {
    replay_slice(5).await;
}

/// Slice 6 of the walk; see [`replay_slice`].
#[tokio::test]
async fn slice_6_of_the_walk_replays_against_the_daemon() {
    replay_slice(6).await;
}

/// Slice 7 of the walk; see [`replay_slice`].
#[tokio::test]
async fn slice_7_of_the_walk_replays_against_the_daemon() {
    replay_slice(7).await;
}

/// Every walked path is in exactly one slice, every slice is replayed by exactly one test, and
/// the steps the slices take cover [`COVERED`]: together the slices replay the whole walk, once.
///
/// The slice each test replays is read from this file's source, so a slice test deleted, or
/// two passing the same number, fails here rather than leaving a slice's paths unreplayed.
///
/// **Falsification**: start each slice one path early (`slice_start(paths, k)` to
/// `slice_start(paths, k).saturating_sub(1)` in [`slice`]) and this names the paths two slices
/// replay, while every slice test stays green.
#[test]
fn the_slices_replay_every_walked_path_once() {
    let Walk {
        paths,
        recorded,
        took,
    } = &*WALK;
    assert!(
        !paths.is_empty(),
        "the walk recorded no path, so nothing was replayed"
    );

    let mut holders = vec![Vec::new(); paths.len()];
    for k in 0..SLICES {
        for index in slice(paths, k) {
            let Some(slices) = holders.get_mut(index) else {
                panic!(
                    "slice {k} reaches path {index}; the walk holds {}",
                    paths.len()
                );
            };
            slices.push(k);
        }
    }
    let unplaced: Vec<usize> = (0..paths.len())
        .filter(|&index| holders[index].is_empty())
        .collect();
    assert!(
        unplaced.is_empty(),
        "paths {unplaced:?} are in no slice, so no test replays them"
    );
    let doubled: Vec<(usize, &Vec<usize>)> = holders
        .iter()
        .enumerate()
        .filter(|(_, slices)| slices.len() > 1)
        .collect();
    assert!(
        doubled.is_empty(),
        "paths are in more than one slice, as (path, slices): {doubled:?}"
    );

    let source: syn::File =
        syn::parse_str(include_str!("model_conformance.rs")).expect("this file parses");
    let wired = replayed_slices(&source.items);
    assert!(
        !wired.is_empty(),
        "no test in this file calls `replay_slice`, so no slice is replayed"
    );
    let mut tests = vec![Vec::new(); SLICES];
    for (name, k) in wired {
        let Some(replaying) = tests.get_mut(k) else {
            panic!("{name} replays slice {k}; there are {SLICES}");
        };
        replaying.push(name);
    }
    let unwired: Vec<usize> = (0..SLICES).filter(|&k| tests[k].is_empty()).collect();
    assert!(
        unwired.is_empty(),
        "slices {unwired:?} are replayed by no test; each needs a test that calls \
         `replay_slice` with its number"
    );
    let shared: Vec<(usize, &Vec<String>)> = tests
        .iter()
        .enumerate()
        .filter(|(_, names)| names.len() > 1)
        .collect();
    assert!(
        shared.is_empty(),
        "slices are replayed by more than one test: {shared:?}"
    );

    let taken: BTreeSet<String> = (0..SLICES)
        .flat_map(|k| &paths[slice(paths, k)])
        .flat_map(|path| {
            path.windows(2).filter_map(|pair| match pair {
                [(before, Some(action)), (after, _)] => Some(label(before, *action, after)),
                _ => None,
            })
        })
        .collect();
    let missing: Vec<&str> = COVERED
        .into_iter()
        .filter(|label| !taken.contains(*label))
        .collect();
    assert!(
        missing.is_empty(),
        "the walk never takes {missing:?}; it takes {taken:?}"
    );

    let steps: usize = paths.iter().map(|path| path.len() - 1).sum();
    let sizes: Vec<usize> = (0..SLICES).map(|k| slice(paths, k).len()).collect();
    eprintln!(
        "walked {} paths ({recorded} recorded, {} for untaken steps; {steps} steps) in {took:?}, \
         replayed in slices of {sizes:?} paths",
        paths.len(),
        paths.len() - recorded,
    );
}

/// Every requirement key either spec defines.
fn spec_keys() -> BTreeSet<String> {
    [
        include_str!("../../spec/agentd.symspec.json"),
        include_str!("../../spec/core.symspec.json"),
    ]
    .into_iter()
    .flat_map(|text| {
        let spec: Value = serde_json::from_str(text).expect("a spec parses");
        spec["requirements"]
            .as_object()
            .expect("a spec has requirements")
            .values()
            .filter_map(|requirement| requirement["key"].as_str().map(str::to_string))
            .collect::<Vec<_>>()
    })
    .collect()
}

/// Whether an item's attributes make it a test: `#[test]`, `#[tokio::test]` and the like.
fn is_test(attrs: &[syn::Attribute]) -> bool {
    attrs.iter().any(|attr| {
        attr.path()
            .segments
            .last()
            .is_some_and(|last| last.ident == "test")
    })
}

/// The keys the tests in `items` claim, read the way scripts/check-trace.py credits the test
/// layer: a key in a test's `///` doc, or one that starts its name (`fn agentd_1_...`).
fn claims(items: &[syn::Item], known: &BTreeSet<String>, claimed: &mut BTreeSet<String>) {
    for item in items {
        match item {
            syn::Item::Fn(function) if is_test(&function.attrs) => {
                let name = function.sig.ident.to_string();
                let name = name.strip_prefix("test_").unwrap_or(&name);
                claimed.extend(
                    known
                        .iter()
                        .filter(|key| {
                            let stem = key.to_lowercase().replace('-', "_");
                            name.strip_prefix(&stem)
                                .is_some_and(|rest| rest.is_empty() || rest.starts_with('_'))
                        })
                        .cloned(),
                );
                for attr in &function.attrs {
                    let syn::Meta::NameValue(doc) = &attr.meta else {
                        continue;
                    };
                    let syn::Expr::Lit(syn::ExprLit {
                        lit: syn::Lit::Str(text),
                        ..
                    }) = &doc.value
                    else {
                        continue;
                    };
                    if !doc.path.is_ident("doc") {
                        continue;
                    }
                    claimed.extend(
                        text.value()
                            .split(|c: char| !(c.is_ascii_alphanumeric() || c == '-'))
                            .filter(|word| known.contains(*word))
                            .map(str::to_string),
                    );
                }
            }
            syn::Item::Mod(module) => {
                if let Some((_, items)) = &module.content {
                    claims(items, known, claimed);
                }
            }
            _ => {}
        }
    }
}

/// Each requirement key this file's tests claim for the trace starts the name of an `always`
/// property of the model the replay walks.
///
/// The keys are read from this file's own source, as the trace reads them, rather than from a
/// list beside the docs that could drift from them.
///
/// **Falsification**: drop `AGENTD-1` from the name of the model's "control API is closed
/// before bootstrap" property and this fails, while `cargo test -p agentd-model` stays green.
#[test]
fn the_keys_the_replay_backs_name_safety_properties_of_the_model() {
    let properties = Agentd::new(agentd_model::Config::deployment_invariant_held()).properties();
    assert!(
        !properties.is_empty(),
        "the model states no property, so no key can be checked against it"
    );
    let source = syn::parse_file(include_str!("model_conformance.rs")).expect("this file parses");
    let mut claimed = BTreeSet::new();
    claims(&source.items, &spec_keys(), &mut claimed);
    assert!(
        !claimed.is_empty(),
        "no test in this file claims a requirement key, so there's nothing to check; the \
         replay's doc names AGENTD-1"
    );
    let unbacked: Vec<&String> = claimed
        .iter()
        .filter(|key| {
            !properties.iter().any(|property| {
                property.expectation == Expectation::Always
                    && property
                        .name
                        .strip_prefix(key.as_str())
                        .is_some_and(|rest| rest.starts_with(' '))
            })
        })
        .collect();
    assert!(
        unbacked.is_empty(),
        "no safety property of the model is named for {unbacked:?}"
    );
}
