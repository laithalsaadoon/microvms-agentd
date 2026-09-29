// SPDX-License-Identifier: Apache-2.0
//! The client lifecycle model, replayed against `Sandbox` over the app's fake control plane.
//!
//! `model/src/client.rs` checks the client's side of one VM's life over every interleaving of
//! caller requests and platform reports, but of the model: a `Sandbox` that stopped making the
//! model's transitions would leave `cargo test -p agentd-model` green. This test ties the two.
//! Stateright walks `Config::guards_held()` breadth first, `PathRecorder` keeps one path to each
//! reachable state, and [`walk`] adds a path for each step none of those takes. Each path is
//! replayed from a fresh `Sandbox` over a `FakeControlPlane`. After every step the sandbox has to show what the model's next state holds: the symspec's
//! five variables (`lifecycle`, `token_installed`, `image_exists`, `was_terminated`,
//! `bootstrap_count`), the control-plane calls counted the way the model's `Wire` counts them
//! (`RunMicrovm`, `SuspendMicrovm`, `ResumeMicrovm`, `TerminateMicrovm`, and every request body
//! that carries a `runHookPayload`), one `CreateMicrovmAuthToken` per proxied request the model
//! sends, and whether a proxy token is cached while the VM is RUNNING. A request the model
//! refuses locally has to come back as an error with no call made at all.
//!
//! # How the model's actions map onto the sandbox
//!
//! Each lifecycle method of the sandbox issues its call and then waits for the platform, so one
//! method call is the model's request and, when the fake answers the state the wait wants, its
//! completion too:
//!
//! - `LaunchAccepted` is `run` with `wait` off, and `HookSucceeded` is `wait_until_running` with
//!   `GetMicrovm` answering RUNNING. A hook report the model ignores (the VM was terminated
//!   first) is a `wait_until_running` the sandbox refuses.
//! - `ExecRequested` asks the session's proxy auth for its headers, which mints on a cold cache.
//! - `SuspendRequested` is `suspend`. When `SuspendComplete` is the next step, `GetMicrovm`
//!   answers SUSPENDED and the two steps are one call. Otherwise it answers SUSPENDING until the
//!   wait times out, which leaves the sandbox SUSPENDING, where the model is.
//! - `ResumeRequested` is `resume`, the same way: RUNNING when `ResumeComplete` is next, and
//!   SUSPENDED until the wait times out otherwise. A closed window first moves the test clock
//!   past the launch's suspended window, which is the only way the sandbox can know it closed.
//! - `TerminateRequested` is `terminate`, waiting for TERMINATED when `TerminateComplete` is the
//!   next step and returning once the call is accepted otherwise.
//! - A platform report the model ignores changes nothing, so nothing is called.
//!
//! The proxy cache is compared only while the VM is RUNNING, the one state the model sends a
//! request from. The cache also expires by the clock, and a timed-out wait moves the clock past
//! its refresh window in states where no request follows.
//!
//! # What isn't replayed, and why
//!
//! A path where a completion arrives after some other step can't be driven. The sandbox holds
//! `&mut self` across its wait, so no second call can land between a request and its
//! completion, and a completion the sandbox stopped waiting for is never observed. Those
//! interleavings are the platform side of the model (a resume that completes after a
//! terminate), not our code. The same goes for a closed window that opens again: the model draws
//! the window per request, and the test clock only runs forward. Such paths are counted and
//! skipped. The planted configs (`guards_skipped`, `double_bootstrap_planted`) describe clients
//! other than this one, so they aren't replayed either.

use std::collections::{BTreeMap, BTreeSet, HashSet};
use std::sync::Arc;
use std::time::Duration;

use agentd_model::client::{Action, ClientLifecycle, Config, State, Verdict, VmState};
use microvms_app::control::transport::Transport;
use microvms_app::sandbox::{Lifecycle, RunRequest, Sandbox, TeardownOpts};
use microvms_app::testing::{
    Answer, FakeControlPlane, TestClock, auth_token_response, control_plane, empty_response,
    microvm_response,
};
use microvms_app::{Error, ErrorKind, Region};
use serde_json::Value;
use stateright::{Checker, Expectation, Model, PathRecorder};

/// The suspended window every replayed launch asks for. Wide enough that the waits a path can
/// time out inside one suspension (three resumes at five minutes each) leave it open.
const WINDOW: Duration = Duration::from_secs(3_600);

/// How long `wait_until_running` may take. The fake answers RUNNING on the first poll.
const READY: Duration = Duration::from_secs(60);

/// One path: each state paired with the action taken from it, the last with none.
type Path = Vec<(State, Option<Action>)>;

/// What one sandbox call stands for.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum Call {
    Launch,
    Hook,
    HookRefused,
    Exec,
    Suspend { completes: bool },
    SuspendRefused,
    Resume { completes: bool },
    ResumeRefused { closes: bool },
    Terminate { completes: bool },
    Nothing,
}

impl Call {
    fn label(self) -> &'static str {
        match self {
            Call::Launch => "launch",
            Call::Hook => "hook",
            Call::HookRefused => "hook refused",
            Call::Exec => "proxied request",
            Call::Suspend { completes: true } => "suspend completed",
            Call::Suspend { completes: false } => "suspend unanswered",
            Call::SuspendRefused => "suspend refused",
            Call::Resume { completes: true } => "resume completed",
            Call::Resume { completes: false } => "resume unanswered",
            Call::ResumeRefused { closes: true } => "resume refused window closed",
            Call::ResumeRefused { closes: false } => "resume refused",
            Call::Terminate { completes: true } => "terminate completed",
            Call::Terminate { completes: false } => "terminate",
            Call::Nothing => "platform report ignored",
        }
    }

    fn refused(self) -> bool {
        matches!(
            self,
            Call::HookRefused | Call::SuspendRefused | Call::ResumeRefused { .. }
        )
    }
}

/// Every call the replay has to make. A walk that shrank to a corner of the model would pass
/// every step it took; this is what it would miss. A planner that came to skip paths is held
/// apart: every path the recorder kept has to plan, and [`SKIPPED`] pins the rest.
const COVERED: [&str; 14] = [
    "launch",
    "hook",
    "hook refused",
    "proxied request",
    "suspend completed",
    "suspend unanswered",
    "suspend refused",
    "resume completed",
    "resume unanswered",
    "resume refused",
    "resume refused window closed",
    "terminate",
    "terminate completed",
    "platform report ignored",
];

/// How many of the walk's untaken-step paths the planner skips, for each reason the module doc
/// gives. Every recorded path plans, so these are all paths that end in a step no recorded path
/// takes. A change to the model or the planner that moves these counts is a change to what the
/// replay drives, so it edits this table in the same diff.
const SKIPPED: [(&str, usize); 2] = [
    ("a closed window opens again", 14),
    ("a completion arrives after another step", 146),
];

/// One sandbox call, and the model's state after the steps it stands for.
struct Step<'a> {
    call: Call,
    before: &'a State,
    after: &'a State,
}

fn verdict(state: &State) -> Verdict {
    state.last.expect("a taken action records its verdict").1
}

/// The path as sandbox calls, or why it can't be one.
fn plan(path: &Path) -> Result<Vec<Step<'_>>, &'static str> {
    let mut steps = Vec::new();
    // Whether the clock has passed the window of the suspension the sandbox is in.
    let mut window_closed = false;
    let mut at = 0;
    while at + 1 < path.len() {
        let (before, Some(action)) = &path[at] else {
            unreachable!("every state but the last carries the action taken from it");
        };
        // Whether the step after this one is the completion `wanted`, and the model applies it.
        let completed_by = |wanted: Action| match (path[at + 1].1, path.get(at + 2)) {
            (Some(next), Some((completed, _))) => {
                next == wanted && verdict(completed) == Verdict::Issued
            }
            _ => false,
        };
        // The call, and how many of the model's steps it stands for.
        let (call, taken) = match (*action, verdict(&path[at + 1].0)) {
            (Action::LaunchAccepted, Verdict::Issued) => (Call::Launch, 1),
            (Action::HookSucceeded, Verdict::Issued) => (Call::Hook, 1),
            (Action::HookSucceeded, Verdict::Ignored) => (Call::HookRefused, 1),
            (Action::ExecRequested, Verdict::Issued) => (Call::Exec, 1),
            (Action::SuspendRequested, Verdict::Issued) => {
                window_closed = false;
                let completes = completed_by(Action::SuspendComplete);
                (Call::Suspend { completes }, 1 + usize::from(completes))
            }
            (Action::SuspendRequested, Verdict::RefusedLocally) => (Call::SuspendRefused, 1),
            (Action::ResumeRequested { .. }, Verdict::Issued) if window_closed => {
                return Err("a closed window opens again");
            }
            (Action::ResumeRequested { .. }, Verdict::Issued) => {
                let completes = completed_by(Action::ResumeComplete);
                (Call::Resume { completes }, 1 + usize::from(completes))
            }
            (Action::ResumeRequested { window_open }, Verdict::RefusedLocally) => {
                let closes = !window_open && before.vm_state == VmState::Suspended;
                window_closed |= closes;
                (Call::ResumeRefused { closes }, 1)
            }
            (Action::ResumeComplete, Verdict::Ignored) => (Call::Nothing, 1),
            (Action::TerminateRequested, Verdict::Issued) => {
                let completes = completed_by(Action::TerminateComplete);
                (Call::Terminate { completes }, 1 + usize::from(completes))
            }
            (
                Action::SuspendComplete | Action::ResumeComplete | Action::TerminateComplete,
                Verdict::Issued,
            ) => return Err("a completion arrives after another step"),
            (action, verdict) => panic!(
                "the model answers {action:?} with {verdict:?}, which no sandbox call stands for"
            ),
        };
        at += taken;
        let after = &path[at].0;
        steps.push(Step {
            call,
            before,
            after,
        });
    }
    Ok(steps)
}

/// The walk: one path to each reachable state of the guarded client, then one path for each
/// step of the model none of those takes, all sorted shortest first, so a failure names the
/// same path on every run and the shortest one there is.
///
/// `PathRecorder` keeps one path per state, so a step into a state a shorter path already
/// reached would never be replayed. Each such step gets the recorded path to the state it
/// leaves from, plus the step, so every transition a sandbox call can stand for is replayed at
/// least once. The module doc lists the ones no call can stand for, and [`SKIPPED`] counts them.
/// Each path comes back with whether the recorder kept it (`true`) or it was added for an
/// untaken step.
fn walk() -> Vec<(Path, bool)> {
    let model = ClientLifecycle::new(Config::guards_held());
    let (recorder, accessor) = PathRecorder::new_with_accessor();
    model.clone().checker().visitor(recorder).spawn_bfs().join();
    let mut paths: Vec<Path> = accessor().into_iter().map(|path| path.into_vec()).collect();
    let order = |path: &Path| (path.len(), format!("{path:?}"));
    // Sorted before the untaken steps are found as well as after, so each untaken step extends
    // the same recorded path on every run.
    paths.sort_by_cached_key(order);

    let mut taken: HashSet<(State, Action)> = paths
        .iter()
        .flatten()
        .filter_map(|(state, action)| action.map(|action| (state.clone(), action)))
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
            if taken.insert((last.clone(), action)) {
                let mut longer = path.clone();
                longer.last_mut().expect("not empty").1 = Some(action);
                longer.push((next, None));
                untaken.push(longer);
            }
        }
    }
    let mut walked: Vec<(Path, bool)> = paths.into_iter().map(|path| (path, true)).collect();
    walked.extend(untaken.into_iter().map(|path| (path, false)));
    walked.sort_by_cached_key(|(path, _)| order(path));
    walked
}

/// How many steps of the model, out of every state the walk reaches, no path takes.
fn untaken_steps(paths: &[Path]) -> usize {
    let model = ClientLifecycle::new(Config::guards_held());
    let taken: HashSet<(&State, Action)> = paths
        .iter()
        .flatten()
        .filter_map(|(state, action)| action.map(|action| (state, action)))
        .collect();
    let reached: HashSet<&State> = paths.iter().flatten().map(|(state, _)| state).collect();
    reached
        .into_iter()
        .map(|state| {
            let mut actions = Vec::new();
            model.actions(state, &mut actions);
            actions
                .into_iter()
                .filter(|action| {
                    model.next_state(state, *action).is_some() && !taken.contains(&(state, *action))
                })
                .count()
        })
        .sum()
}

fn lifecycle(state: VmState) -> Lifecycle {
    match state {
        VmState::Pending => Lifecycle::Pending,
        VmState::Running => Lifecycle::Running,
        VmState::Suspending => Lifecycle::Suspending,
        VmState::Suspended => Lifecycle::Suspended,
        VmState::Terminating => Lifecycle::Terminating,
        VmState::Terminated => Lifecycle::Terminated,
    }
}

/// A fresh sandbox for one path, over a fake that answers every call a path makes.
struct Replay {
    sandbox: Sandbox,
    plane: Arc<FakeControlPlane>,
    clock: Arc<TestClock>,
    /// The proxied requests the model has sent. Each mints once, since the model sends one
    /// only while the cache is cold.
    requests: u32,
}

impl Replay {
    fn new() -> Self {
        let plane = Arc::new(FakeControlPlane::new());
        plane
            .answer("RunMicrovm", Answer::ok(microvm_response("PENDING", None)))
            .answer("SuspendMicrovm", Answer::ok(empty_response()))
            .answer("ResumeMicrovm", Answer::ok(empty_response()))
            .answer("TerminateMicrovm", Answer::ok(empty_response()))
            .answer(
                "CreateMicrovmAuthToken",
                Answer::ok(auth_token_response("proxy-token")),
            );
        let clock = Arc::new(TestClock::new());
        let transport = Arc::clone(&plane) as Arc<dyn Transport>;
        let control = control_plane(transport, Region::UsEast1, clock.clone());
        Self {
            sandbox: Sandbox::with_control_plane(control),
            plane,
            clock,
            requests: 0,
        }
    }

    /// What `GetMicrovm` answers from the next wait on. The fake repeats its last answer and
    /// consumes the ones before it, so the answer the previous wait ended on is read once
    /// first. It's never a state the next wait wants or fails on: each wait starts from the
    /// state the previous one left.
    fn reports(&self, state: &str) {
        self.plane
            .answer("GetMicrovm", Answer::ok(microvm_response(state, None)));
    }

    fn count(&self, operation: &str) -> u32 {
        u32::try_from(self.plane.call_count(operation)).expect("a path makes few calls")
    }

    /// The request bodies that carried a run-hook payload, read off the wire member.
    fn payloads(&self) -> u32 {
        let carried = self
            .plane
            .bodies_as_text()
            .iter()
            .filter(|body| {
                serde_json::from_str::<Value>(body)
                    .is_ok_and(|value| value.get("runHookPayload").is_some())
            })
            .count();
        u32::try_from(carried).expect("a path makes few calls")
    }

    async fn apply(&mut self, step: &Step<'_>) -> Result<(), String> {
        let calls = self.plane.calls().len();
        match step.call {
            Call::Launch => {
                let mut request = RunRequest::new()
                    .with_image("arn:image")
                    .with_suspended_sec(u32::try_from(WINDOW.as_secs()).expect("fits"));
                request.wait = false;
                self.sandbox
                    .run(request)
                    .await
                    .map_err(|error| format!("run refused the launch: {error}"))?;
            }
            Call::Hook => {
                self.reports("RUNNING");
                self.sandbox
                    .wait_until_running(READY)
                    .await
                    .map_err(|error| format!("wait_until_running failed: {error}"))?;
            }
            Call::HookRefused => refusal(
                self.sandbox.wait_until_running(READY).await.map(|_| ()),
                None,
            )?,
            Call::Exec => {
                let auth = self
                    .sandbox
                    .session()
                    .and_then(|session| session.proxy_auth())
                    .ok_or("the sandbox holds no session with proxy auth to send a request")?;
                auth.headers()
                    .await
                    .map_err(|error| format!("the proxied request failed: {error}"))?;
                self.requests += 1;
            }
            Call::Suspend { completes } => {
                self.reports(if completes { "SUSPENDED" } else { "SUSPENDING" });
                waited(self.sandbox.suspend().await, completes, "suspend")?;
            }
            Call::SuspendRefused => refusal(self.sandbox.suspend().await, None)?,
            Call::Resume { completes } => {
                self.reports(if completes { "RUNNING" } else { "SUSPENDED" });
                waited(self.sandbox.resume().await.map(|_| ()), completes, "resume")?;
            }
            Call::ResumeRefused { closes, .. } => {
                if closes {
                    self.clock.advance(WINDOW + Duration::from_secs(1));
                }
                refusal(
                    self.sandbox.resume().await.map(|_| ()),
                    closes.then_some(ErrorKind::WindowClosed),
                )?;
            }
            Call::Terminate { completes } => {
                let opts = if completes {
                    self.reports("TERMINATED");
                    TeardownOpts::default().waiting_for_terminated()
                } else {
                    TeardownOpts::default()
                };
                let report = self.sandbox.terminate(opts).await;
                if !report.terminate_accepted || !report.failures.is_empty() {
                    return Err(format!("terminate reported {report:?}"));
                }
            }
            Call::Nothing => {}
        }
        let made = self.plane.calls().len() - calls;
        if step.call.refused() && made > 0 {
            return Err(format!(
                "the model refuses this locally, and the sandbox made {made} call(s): {:?}",
                &self.plane.operations()[calls..]
            ));
        }
        Ok(())
    }

    /// Whether the sandbox holds what the model's state does.
    fn observe(&self, model: &State) -> Result<(), String> {
        let sandbox = &self.sandbox;
        let mut problems = Vec::new();
        if sandbox.lifecycle() != lifecycle(model.vm_state) {
            problems.push(format!(
                "lifecycle: sandbox {:?}, model {:?}",
                sandbox.lifecycle(),
                model.vm_state
            ));
        }
        let flags = [
            (
                "token_installed",
                sandbox.token_installed(),
                model.token_installed,
            ),
            ("image_exists", sandbox.image_exists(), model.image_exists),
            (
                "was_terminated",
                sandbox.was_terminated(),
                model.was_terminated,
            ),
        ];
        for (name, seen, held) in flags {
            if seen != held {
                problems.push(format!("{name}: sandbox {seen}, model {held}"));
            }
        }
        let counts = [
            (
                "bootstrap_count",
                sandbox.bootstrap_count(),
                model.bootstrap_count,
            ),
            (
                "RunMicrovm calls",
                self.count("RunMicrovm"),
                model.wire.launches,
            ),
            (
                "SuspendMicrovm calls",
                self.count("SuspendMicrovm"),
                model.wire.suspends,
            ),
            (
                "ResumeMicrovm calls",
                self.count("ResumeMicrovm"),
                model.wire.resumes,
            ),
            (
                "TerminateMicrovm calls",
                self.count("TerminateMicrovm"),
                model.wire.terminates,
            ),
            ("run-hook payloads", self.payloads(), model.wire.payloads),
        ];
        for (name, seen, held) in counts {
            if seen != u32::from(held) {
                problems.push(format!("{name}: sandbox {seen}, model {held}"));
            }
        }
        let mints = self.count("CreateMicrovmAuthToken");
        if mints != self.requests {
            problems.push(format!(
                "CreateMicrovmAuthToken calls: sandbox {mints}, model {} (one per proxied \
                 request, each sent on a cold cache)",
                self.requests
            ));
        }
        if model.vm_state == VmState::Running {
            let cached = sandbox
                .session()
                .and_then(|session| session.proxy_auth())
                .is_some_and(|auth| auth.is_cached());
            if cached != model.proxy_token_cached {
                problems.push(format!(
                    "proxy token cached: sandbox {cached}, model {}",
                    model.proxy_token_cached
                ));
            }
        }
        if problems.is_empty() {
            Ok(())
        } else {
            Err(problems.join("; "))
        }
    }
}

/// A call the model refuses locally: an error, of `kind` when one is named.
fn refusal(result: Result<(), Error>, kind: Option<ErrorKind>) -> Result<(), String> {
    match (result, kind) {
        (Ok(()), _) => Err("the model refuses this locally, and the sandbox accepted it".into()),
        (Err(error), Some(kind)) if error.kind() != kind => Err(format!(
            "refused as {:?}, not {kind:?}: {error}",
            error.kind()
        )),
        (Err(_), _) => Ok(()),
    }
}

/// A call whose wait either ends in the state it wants or times out, as `completes` says.
fn waited(result: Result<(), Error>, completes: bool, what: &str) -> Result<(), String> {
    match (result, completes) {
        (Ok(()), true) => Ok(()),
        (Err(error), false) if error.kind() == ErrorKind::Timeout => Ok(()),
        (Ok(()), false) => Err(format!(
            "{what} returned although the platform never reported it complete"
        )),
        (Err(error), _) => Err(format!("{what} failed: {error}")),
    }
}

async fn replay(steps: &[Step<'_>]) -> Result<(), String> {
    let mut sandbox = Replay::new();
    for (index, step) in steps.iter().enumerate() {
        let outcome = match sandbox.apply(step).await {
            Ok(()) => sandbox.observe(step.after),
            Err(problem) => Err(problem),
        };
        if let Err(problem) = outcome {
            return Err(format!(
                "call {index} ({}): {problem}\n  model state before: {:?}\n  model state \
                 after: {:?}",
                step.call.label(),
                step.before,
                step.after
            ));
        }
    }
    Ok(())
}

/// Every path stateright walks through the guarded client lifecycle replays against
/// `Sandbox` with the model's five state variables, wire calls, run-hook payloads and proxy
/// token, after every call.
///
/// It's the sandbox's side of the model's keyed properties: STATE-3 (the bootstrap count
/// never passes one), STATE-5 (no suspend call leaves a state other than RUNNING), STATE-8 (a
/// completed resume leaves no proxy token cached) and STATE-11 (a terminated VM never reaches
/// RUNNING). Each is a comparison this test makes after every call. The guarded model offers a
/// launch only while none was made, so no path calls `run` twice: for STATE-3 the replay holds
/// the count against an extra bootstrap from a resume or a hook, and the refusal of a second
/// `run` is `a_second_run_on_one_sandbox_is_refused_before_any_call` in the app's sandbox.rs.
///
/// The walk is held three ways: every path the recorder kept has to plan, the untaken-step
/// paths the planner skips are pinned in [`SKIPPED`], and the calls made have to cover
/// [`COVERED`].
///
/// **Falsification**: let `Sandbox::resume` send the launch's run-hook payload again after its
/// `ResumeMicrovm` call, and the first path that resumes fails on its payload and `RunMicrovm`
/// counts, while `cargo test -p agentd-model` stays green.
#[tokio::test]
async fn every_walked_path_of_the_client_model_replays_against_the_sandbox() {
    let walked = walk();
    let paths: Vec<Path> = walked.iter().map(|(path, _)| path.clone()).collect();
    let recorded = walked.iter().filter(|(_, recorded)| *recorded).count();
    assert!(
        recorded > 0,
        "the walk recorded no path, so nothing was replayed"
    );
    let untaken = untaken_steps(&paths);
    assert!(
        untaken == 0,
        "the walk leaves {untaken} of the model's steps untaken, so no path replays them"
    );
    let mut planned = Vec::new();
    let mut unplanned = Vec::new();
    let mut skipped: BTreeMap<&str, usize> = BTreeMap::new();
    for (path, recorded) in &walked {
        match plan(path) {
            Ok(steps) => planned.push((path, steps)),
            Err(why) if *recorded => unplanned.push((why, path)),
            Err(why) => *skipped.entry(why).or_default() += 1,
        }
    }
    let taken: BTreeSet<&str> = planned
        .iter()
        .flat_map(|(_, steps)| steps.iter().map(|step| step.call.label()))
        .collect();
    let missing: Vec<&str> = COVERED
        .into_iter()
        .filter(|label| !taken.contains(label))
        .collect();
    assert!(
        missing.is_empty(),
        "the replay never makes {missing:?}; it makes {taken:?}"
    );
    if let Some((why, path)) = unplanned.first() {
        let actions: Vec<Action> = path.iter().filter_map(|(_, action)| *action).collect();
        panic!(
            "a path the recorder kept doesn't plan ({why}), so the replay would skip a state \
             the model reaches; {} such paths, the shortest:\n  path: {actions:?}",
            unplanned.len()
        );
    }
    assert_eq!(
        skipped,
        BTreeMap::from(SKIPPED),
        "the planner skips a different set of untaken-step paths than SKIPPED names"
    );

    for (path, steps) in &planned {
        if let Err(failure) = replay(steps).await {
            let actions: Vec<Action> = path.iter().filter_map(|(_, action)| *action).collect();
            panic!("a replayed path diverged from the model: {failure}\n  path: {actions:?}");
        }
    }

    let calls: usize = planned.iter().map(|(_, steps)| steps.len()).sum();
    eprintln!(
        "replayed {} of {} walked paths ({recorded} recorded, {} for untaken steps; {calls} \
         sandbox calls); skipped {skipped:?}",
        planned.len(),
        paths.len(),
        paths.len() - recorded
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
/// layer: a key in a test's `///` doc, or one that starts its name (`fn state_3_...`, or
/// `fn test_state_3_...`), in this file's modules at any depth.
fn claims(items: &[syn::Item], known: &BTreeSet<String>, claimed: &mut BTreeSet<String>) {
    for item in items {
        let function = match item {
            syn::Item::Fn(function) => function,
            syn::Item::Mod(syn::ItemMod {
                content: Some((_, inner)),
                ..
            }) => {
                claims(inner, known, claimed);
                continue;
            }
            _ => continue,
        };
        if !is_test(&function.attrs) {
            continue;
        }
        let ident = function.sig.ident.to_string();
        let name = ident.strip_prefix("test_").unwrap_or(&ident);
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
}

/// The client model's safety properties that carry a requirement key, each exactly as named in
/// `model/src/client.rs`. A key that moves onto another property changes this set, though the
/// key still starts some property's name.
const KEYED: [&str; 4] = [
    "STATE-3 bootstrap happens at most once",
    "STATE-5 no suspend call outside RUNNING",
    "STATE-8 a resume completion drops the proxy token",
    "STATE-11 a terminated VM never reaches RUNNING",
];

/// Each requirement key this file's tests claim starts the name of an `always` property of the
/// client lifecycle model the replay walks, and the keyed properties are the ones [`KEYED`]
/// names.
///
/// The claimed keys are read from this file's own source, as the trace reads a test file,
/// rather than from a list that could drift from the docs. `trace:check` doesn't read this
/// crate's tests and the STATE keys aren't in its `TRACED` yet (#302), so this is what notices
/// a property that loses its key, or a key that moves onto a property stating another rule.
///
/// **Falsification**: drop `STATE-8` from the name of the model's "a resume completion drops
/// the proxy token" property and this fails, while `cargo test -p agentd-model` stays green.
#[test]
fn the_keys_the_replay_backs_name_safety_properties_of_the_client_model() {
    let properties = ClientLifecycle::new(Config::guards_held()).properties();
    assert!(
        !properties.is_empty(),
        "the client model states no property, so no key can be checked against it"
    );
    let keys = spec_keys();
    let source = syn::parse_file(include_str!("client_lifecycle.rs")).expect("this file parses");
    let mut claimed = BTreeSet::new();
    claims(&source.items, &keys, &mut claimed);
    assert!(
        !claimed.is_empty(),
        "no test in this file claims a requirement key, so there's nothing to check; the \
         replay's doc names STATE-3, STATE-5, STATE-8 and STATE-11"
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
        "no safety property of the client model is named for {unbacked:?}"
    );
    let keyed: BTreeSet<&str> = properties
        .iter()
        .filter(|property| property.expectation == Expectation::Always)
        .map(|property| property.name)
        .filter(|name| {
            keys.iter().any(|key| {
                name.strip_prefix(key.as_str())
                    .is_some_and(|rest| rest.starts_with(' '))
            })
        })
        .collect();
    assert_eq!(
        keyed,
        BTreeSet::from(KEYED),
        "the client model's keyed safety properties aren't the ones KEYED names"
    );
}
