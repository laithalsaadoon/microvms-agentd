// SPDX-License-Identifier: Apache-2.0
//! Keeping a VM awake from outside while work runs inside it.
//!
//! The platform measures idleness by inbound requests through the endpoint proxy. Work
//! inside the guest does not count: measured 2026-09-23 in us-east-1, a CPU-busy exec was
//! suspended between 60 and 70 seconds after the last inbound request under a 60-second
//! `maxIdleDurationSeconds`. Requests from inside the guest arrive over loopback and never
//! reach the meter. So the only keepalive is a caller outside the VM that keeps sending
//! requests, and [`KeepAwake`] is that caller: an unauthenticated `GET /v1/health` every
//! `interval`, optionally ending once the daemon stops reporting `busy`.
//!
//! # Why the interval is capped at half the idle window
//!
//! One missed poll must not suspend the VM. With the interval at most half the window, a
//! single failed or slow poll still leaves a second one inside the window. When the caller
//! does not know the VM's window, the cap assumes the smallest window the platform accepts
//! ([`MIN_IDLE_DURATION_SEC`]), which is the only assumption that cannot be too generous.
//!
//! # The probe is the caller's
//!
//! [`KeepAwake::run`] owns the policy (cadence, `busy`, deadline, error tolerance, stop)
//! and takes the poll as a closure. [`super::Session::keep_awake`] passes its own health
//! call. A binding holding a sandbox-owned session passes one that first checks, under the
//! sandbox's lock, that the VM is still running and answers `None` when it is not: a
//! health poll against a VM the caller just suspended would auto-resume it, which is the
//! opposite of what a suspend asked for.

use std::future::Future;
use std::sync::{Arc, Mutex, PoisonError};
use std::time::Duration;

use protocol::health::Health;
use tokio::sync::{oneshot, watch};

use super::Session;
use crate::constants::MIN_IDLE_DURATION_SEC;
use crate::error::{Error, ErrorKind};

/// The default cadence when the caller names none: a third of the window, at most this.
pub const DEFAULT_KEEP_AWAKE_INTERVAL: Duration = Duration::from_secs(20);

/// Consecutive retryable poll failures tolerated before the keepalive gives up.
pub const DEFAULT_TOLERATED_ERRORS: u32 = 3;

/// How soon a failed poll is retried. Shorter than any valid interval, so one transport
/// blip does not spend half the idle window.
const RETRY_AFTER: Duration = Duration::from_secs(1);

/// Why a keepalive ended without an error.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum KeepAwakeEnd {
    /// The caller stopped it.
    Stopped,
    /// `while_busy` was set and the daemon reported no running exec.
    Idle,
    /// `max_duration` passed.
    Elapsed,
    /// The probe reported the VM is no longer running (suspended or terminated).
    NotRunning,
}

impl KeepAwakeEnd {
    /// The wire spelling, shared by the bindings and the CLI envelope.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Stopped => "stopped",
            Self::Idle => "idle",
            Self::Elapsed => "elapsed",
            Self::NotRunning => "not-running",
        }
    }
}

impl std::fmt::Display for KeepAwakeEnd {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

/// What a finished keepalive did.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct KeepAwakeReport {
    pub end: KeepAwakeEnd,
    /// Health polls that answered.
    pub polls: u64,
    /// `busy` from the last answered poll, or `None` when none answered.
    pub last_busy: Option<bool>,
    pub elapsed: Duration,
}

/// A keepalive's policy. Build with [`KeepAwake::new`], check with [`KeepAwake::validate`].
#[derive(Clone, Debug)]
pub struct KeepAwake {
    interval: Duration,
    idle_window: Duration,
    while_busy: bool,
    max_duration: Option<Duration>,
    tolerated_errors: u32,
}

impl KeepAwake {
    /// A keepalive for a VM whose `maxIdleDurationSeconds` is `idle_window`, or the
    /// platform minimum when unknown. The interval defaults to a third of the window,
    /// at most [`DEFAULT_KEEP_AWAKE_INTERVAL`].
    pub fn new(idle_window: Option<Duration>) -> Self {
        let idle_window =
            idle_window.unwrap_or(Duration::from_secs(u64::from(MIN_IDLE_DURATION_SEC)));
        Self {
            interval: (idle_window / 3).min(DEFAULT_KEEP_AWAKE_INTERVAL),
            idle_window,
            while_busy: false,
            max_duration: None,
            tolerated_errors: DEFAULT_TOLERATED_ERRORS,
        }
    }

    #[must_use]
    pub fn interval(mut self, interval: Duration) -> Self {
        self.interval = interval;
        self
    }

    /// End once the daemon reports no running exec.
    #[must_use]
    pub fn while_busy(mut self, while_busy: bool) -> Self {
        self.while_busy = while_busy;
        self
    }

    /// End after this long even if still busy.
    #[must_use]
    pub fn max_duration(mut self, max_duration: Option<Duration>) -> Self {
        self.max_duration = max_duration;
        self
    }

    #[must_use]
    pub fn tolerated_errors(mut self, tolerated_errors: u32) -> Self {
        self.tolerated_errors = tolerated_errors;
        self
    }

    pub fn interval_value(&self) -> Duration {
        self.interval
    }

    pub fn idle_window_value(&self) -> Duration {
        self.idle_window
    }

    /// Refuses a policy that could let the VM suspend. See the module docs for the cap.
    pub fn validate(&self) -> Result<(), Error> {
        let minimum = Duration::from_secs(u64::from(MIN_IDLE_DURATION_SEC));
        if self.idle_window < minimum {
            return Err(Error::invalid_arg(format!(
                "idle window {}s is below the platform minimum of {MIN_IDLE_DURATION_SEC}s \
                 (IdlePolicy.maxIdleDurationSeconds)",
                self.idle_window.as_secs_f64()
            )));
        }
        if self.interval < Duration::from_secs(1) {
            return Err(Error::invalid_arg(format!(
                "keepalive interval {}s is below 1s",
                self.interval.as_secs_f64()
            )));
        }
        if self.interval > self.idle_window / 2 {
            return Err(Error::invalid_arg(format!(
                "keepalive interval {}s exceeds half the {}s idle window, so one missed poll \
                 could let the VM suspend; pass a shorter interval or the VM's actual window",
                self.interval.as_secs_f64(),
                self.idle_window.as_secs_f64()
            )));
        }
        if self.max_duration.is_some_and(|max| max.is_zero()) {
            return Err(Error::invalid_arg(
                "keepalive max duration must be positive",
            ));
        }
        Ok(())
    }

    /// Polls `probe` until `stop` resolves, the policy ends it, or a poll fails for good.
    ///
    /// The first poll is immediate. `probe` answers `Ok(None)` when the VM is no longer
    /// running. A retryable failure is retried after one second, up to `tolerated_errors`
    /// in a row; any other failure ends the keepalive with that error.
    pub async fn run<P, F, S>(&self, mut probe: P, stop: S) -> Result<KeepAwakeReport, Error>
    where
        P: FnMut() -> F,
        F: Future<Output = Result<Option<Health>, Error>>,
        S: Future<Output = ()>,
    {
        self.validate()?;
        let started = tokio::time::Instant::now();
        tokio::pin!(stop);
        let mut polls = 0u64;
        let mut errors = 0u32;
        let mut last_busy = None;
        let report = |end, polls, last_busy| KeepAwakeReport {
            end,
            polls,
            last_busy,
            elapsed: started.elapsed(),
        };
        loop {
            let answer = tokio::select! {
                biased;
                () = &mut stop => return Ok(report(KeepAwakeEnd::Stopped, polls, last_busy)),
                answer = probe() => answer,
            };
            let mut wait = self.interval;
            match answer {
                Ok(Some(health)) => {
                    polls += 1;
                    errors = 0;
                    last_busy = Some(health.busy);
                    if self.while_busy && !health.busy {
                        return Ok(report(KeepAwakeEnd::Idle, polls, last_busy));
                    }
                }
                Ok(None) => return Ok(report(KeepAwakeEnd::NotRunning, polls, last_busy)),
                Err(error) if error.retryable() && errors < self.tolerated_errors => {
                    errors += 1;
                    wait = RETRY_AFTER;
                }
                Err(error) => return Err(error),
            }
            if let Some(max) = self.max_duration {
                let elapsed = started.elapsed();
                if elapsed >= max {
                    return Ok(report(KeepAwakeEnd::Elapsed, polls, last_busy));
                }
                wait = wait.min(max - elapsed);
            }
            tokio::select! {
                biased;
                () = &mut stop => return Ok(report(KeepAwakeEnd::Stopped, polls, last_busy)),
                () = tokio::time::sleep(wait) => {}
            }
            if self
                .max_duration
                .is_some_and(|max| started.elapsed() >= max)
            {
                return Ok(report(KeepAwakeEnd::Elapsed, polls, last_busy));
            }
        }
    }
}

/// Whether the VM behind a keepalive is still one it may poll. See the module docs.
pub type RunningGate = Box<dyn Fn() -> bool + Send + Sync>;

type Outcome = Result<KeepAwakeReport, (ErrorKind, String)>;

/// A keepalive running as a background task. Dropping it stops the task.
pub struct KeepAwakeTask {
    stop: Mutex<Option<oneshot::Sender<()>>>,
    outcome: Arc<Mutex<Option<Outcome>>>,
    done: watch::Receiver<bool>,
}

impl KeepAwake {
    /// Validates the policy and starts polling `session` on the current tokio runtime.
    ///
    /// `running`, when given, is checked before every poll; once it answers `false` the
    /// task ends with [`KeepAwakeEnd::NotRunning`] without polling again. Panics outside a
    /// tokio runtime, like `tokio::spawn`.
    pub fn spawn(
        self,
        session: Session,
        running: Option<RunningGate>,
    ) -> Result<KeepAwakeTask, Error> {
        self.validate()?;
        let (stop_tx, stop_rx) = oneshot::channel::<()>();
        let (done_tx, done_rx) = watch::channel(false);
        let outcome = Arc::new(Mutex::new(None));
        let written = Arc::clone(&outcome);
        tokio::spawn(async move {
            let probe = || {
                let session = running
                    .as_ref()
                    .is_none_or(|running| running())
                    .then(|| session.clone());
                async move {
                    match session {
                        Some(session) => session.health().await.map(Some),
                        None => Ok(None),
                    }
                }
            };
            // A dropped sender resolves the receiver too: dropping the task stops it.
            let stop = async move {
                let _ = stop_rx.await;
            };
            let result = self
                .run(probe, stop)
                .await
                .map_err(|error| (error.kind(), error.to_string()));
            *written.lock().unwrap_or_else(PoisonError::into_inner) = Some(result);
            done_tx.send_replace(true);
        });
        Ok(KeepAwakeTask {
            stop: Mutex::new(Some(stop_tx)),
            outcome,
            done: done_rx,
        })
    }
}

impl KeepAwakeTask {
    /// Whether the task is still polling.
    pub fn is_running(&self) -> bool {
        !*self.done.borrow()
    }

    /// Asks the task to stop. Idempotent; [`Self::finished`] waits for it.
    pub fn request_stop(&self) {
        if let Some(stop) = self
            .stop
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .take()
        {
            let _ = stop.send(());
        }
    }

    /// Waits for the task to end and returns its report, or the error that ended it.
    ///
    /// Callable any number of times, from any number of waiters.
    pub async fn finished(&self) -> Result<KeepAwakeReport, Error> {
        let mut done = self.done.clone();
        // The sender only drops after writing the outcome, so an error here still means done.
        let _ = done.wait_for(|finished| *finished).await;
        self.outcome()
            .expect("the task records its outcome before signalling done")
    }

    /// The report, once the task has ended.
    pub fn outcome(&self) -> Option<Result<KeepAwakeReport, Error>> {
        self.outcome
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .clone()
            .map(|outcome| outcome.map_err(|(kind, message)| Error::new(kind, message)))
    }
}

#[cfg(test)]
mod tests {
    use std::cell::RefCell;
    use std::collections::VecDeque;

    use super::*;
    use crate::error::{ErrorKind, WireKind};

    fn health(busy: bool) -> Health {
        serde_json::from_value(serde_json::json!({
            "version": "0.1.0",
            "bootstrapped": true,
            "disk": null,
            "identity_degraded": false,
            "identity_repaired": true,
            "busy": busy,
        }))
        .expect("health parses")
    }

    type Answer = Result<Option<Health>, Error>;

    /// A probe answering from a script, recording when each poll happened.
    struct Script {
        answers: RefCell<VecDeque<Answer>>,
        at: RefCell<Vec<Duration>>,
        start: tokio::time::Instant,
    }

    impl Script {
        fn new(answers: impl IntoIterator<Item = Answer>) -> Self {
            Self {
                answers: RefCell::new(answers.into_iter().collect()),
                at: RefCell::new(Vec::new()),
                start: tokio::time::Instant::now(),
            }
        }

        fn probe(&self) -> impl FnMut() -> std::future::Ready<Answer> + '_ {
            move || {
                self.at.borrow_mut().push(self.start.elapsed());
                std::future::ready(
                    self.answers
                        .borrow_mut()
                        .pop_front()
                        .unwrap_or(Ok(Some(health(true)))),
                )
            }
        }
    }

    fn secs(n: u64) -> Duration {
        Duration::from_secs(n)
    }

    #[tokio::test(start_paused = true)]
    async fn it_polls_at_the_interval_until_stopped() {
        let script = Script::new([]);
        let stop = tokio::time::sleep(secs(45));
        let report = KeepAwake::new(None)
            .interval(secs(10))
            .run(script.probe(), stop)
            .await
            .expect("runs");
        assert_eq!(report.end, KeepAwakeEnd::Stopped);
        assert_eq!(*script.at.borrow(), [0, 10, 20, 30, 40].map(secs));
        assert_eq!((report.polls, report.last_busy), (5, Some(true)));
    }

    #[tokio::test(start_paused = true)]
    async fn while_busy_ends_on_the_first_idle_answer() {
        let script = Script::new([
            Ok(Some(health(true))),
            Ok(Some(health(true))),
            Ok(Some(health(false))),
        ]);
        let report = KeepAwake::new(None)
            .while_busy(true)
            .run(script.probe(), std::future::pending())
            .await
            .expect("runs");
        assert_eq!(report.end, KeepAwakeEnd::Idle);
        assert_eq!((report.polls, report.last_busy), (3, Some(false)));
        assert_eq!(report.elapsed, secs(40));
    }

    #[tokio::test(start_paused = true)]
    async fn an_idle_answer_without_while_busy_keeps_polling() {
        let script = Script::new([Ok(Some(health(false)))]);
        let report = KeepAwake::new(None)
            .max_duration(Some(secs(50)))
            .run(script.probe(), std::future::pending())
            .await
            .expect("runs");
        assert_eq!(report.end, KeepAwakeEnd::Elapsed);
        assert_eq!(*script.at.borrow(), [0, 20, 40].map(secs));
        assert_eq!(report.elapsed, secs(50));
    }

    #[tokio::test(start_paused = true)]
    async fn a_vm_that_stopped_running_ends_the_keepalive_without_polling_it() {
        let script = Script::new([Ok(Some(health(true))), Ok(None)]);
        let report = KeepAwake::new(None)
            .run(script.probe(), std::future::pending())
            .await
            .expect("runs");
        assert_eq!((report.end, report.polls), (KeepAwakeEnd::NotRunning, 1));
    }

    #[tokio::test(start_paused = true)]
    async fn retryable_failures_are_retried_quickly_up_to_the_tolerance() {
        let cut = || Err(Error::wire(WireKind::Transport, "cut"));
        let script = Script::new([cut(), cut(), Ok(Some(health(true))), cut(), cut(), cut()]);
        let report = KeepAwake::new(None)
            .max_duration(Some(secs(30)))
            .run(script.probe(), std::future::pending())
            .await
            .expect("three in a row is tolerated, and a success resets the count");
        assert_eq!(report.end, KeepAwakeEnd::Elapsed);
        assert_eq!(script.at.borrow()[..4], [0, 1, 2, 22].map(secs));

        let script = Script::new([cut(), cut(), cut(), cut()]);
        let error = KeepAwake::new(None)
            .run(script.probe(), std::future::pending())
            .await
            .expect_err("a fourth consecutive failure ends it");
        assert!(error.retryable());
        assert_eq!(script.at.borrow().len(), 4);
    }

    #[tokio::test(start_paused = true)]
    async fn a_non_retryable_failure_ends_it_at_once() {
        let script = Script::new([Err(Error::invalid_arg("refused"))]);
        let error = KeepAwake::new(None)
            .run(script.probe(), std::future::pending())
            .await
            .expect_err("fails");
        assert_eq!(error.kind(), ErrorKind::InvalidArg);
        assert_eq!(script.at.borrow().len(), 1);
    }

    #[test]
    fn the_interval_is_capped_at_half_the_window() {
        assert_eq!(KeepAwake::new(None).interval_value(), secs(20));
        assert_eq!(KeepAwake::new(Some(secs(600))).interval_value(), secs(20));
        assert_eq!(KeepAwake::new(Some(secs(90))).interval_value(), secs(20));
        assert!(KeepAwake::new(None).interval(secs(30)).validate().is_ok());
        assert!(KeepAwake::new(None).interval(secs(31)).validate().is_err());
        assert!(
            KeepAwake::new(Some(secs(600)))
                .interval(secs(300))
                .validate()
                .is_ok()
        );
        assert!(
            KeepAwake::new(None)
                .interval(Duration::from_millis(500))
                .validate()
                .is_err()
        );
        assert!(KeepAwake::new(Some(secs(59))).validate().is_err());
        assert!(
            KeepAwake::new(None)
                .max_duration(Some(Duration::ZERO))
                .validate()
                .is_err()
        );
    }

    fn busy_session(
        replies: usize,
    ) -> (Session, std::sync::Arc<crate::session::testing::Recorder>) {
        use crate::session::testing::{Recorder, Reply, health_body, session_with};
        let mut body = health_body(true);
        body["busy"] = serde_json::json!(true);
        let recorder = Recorder::with((0..replies).map(|_| Reply::ok(body.clone())));
        let (session, _, _) = session_with(std::sync::Arc::clone(&recorder));
        (session, recorder)
    }

    #[tokio::test(start_paused = true)]
    async fn a_spawned_task_stops_on_request_and_reports() {
        let (session, recorder) = busy_session(10);
        let task = KeepAwake::new(None)
            .interval(secs(10))
            .spawn(session, None)
            .expect("spawns");
        tokio::time::sleep(secs(25)).await;
        assert!(task.is_running());
        task.request_stop();
        let report = task.finished().await.expect("stops cleanly");
        assert_eq!((report.end, report.polls), (KeepAwakeEnd::Stopped, 3));
        assert!(!task.is_running());
        assert_eq!(recorder.requests().len(), 3);
        assert_eq!(task.finished().await.expect("again").polls, 3);
    }

    #[tokio::test(start_paused = true)]
    async fn dropping_the_task_stops_polling() {
        let (session, recorder) = busy_session(10);
        let task = KeepAwake::new(None)
            .interval(secs(10))
            .spawn(session, None)
            .expect("spawns");
        tokio::time::sleep(secs(15)).await;
        drop(task);
        tokio::time::sleep(secs(60)).await;
        assert_eq!(recorder.requests().len(), 2);
    }

    #[tokio::test(start_paused = true)]
    async fn a_closed_gate_ends_the_task_without_another_poll() {
        let (session, recorder) = busy_session(10);
        let open = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(true));
        let gate = std::sync::Arc::clone(&open);
        let task = KeepAwake::new(None)
            .interval(secs(10))
            .spawn(
                session,
                Some(Box::new(move || {
                    gate.load(std::sync::atomic::Ordering::SeqCst)
                })),
            )
            .expect("spawns");
        tokio::time::sleep(secs(15)).await;
        open.store(false, std::sync::atomic::Ordering::SeqCst);
        let report = task.finished().await.expect("ends");
        assert_eq!((report.end, report.polls), (KeepAwakeEnd::NotRunning, 2));
        assert_eq!(
            recorder.requests().len(),
            2,
            "no poll after the gate closed"
        );
    }

    #[tokio::test]
    async fn an_invalid_policy_is_refused_before_spawning() {
        let (session, recorder) = busy_session(0);
        let error = KeepAwake::new(None)
            .interval(secs(31))
            .spawn(session, None)
            .err()
            .expect("refused");
        assert_eq!(error.kind(), ErrorKind::InvalidArg);
        assert!(recorder.requests().is_empty());
    }
}
