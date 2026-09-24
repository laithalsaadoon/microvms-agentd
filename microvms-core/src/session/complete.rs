// SPDX-License-Identifier: Apache-2.0
//! Run one command to exactly one result: [`Session::run_to_completion`].
//!
//! The composition every harness wrote for itself (issue #222): start with a caller-minted
//! exec id, stream output to a callback when there is one, fall back to wait-and-ack when the
//! stream ends without its terminal `exit` event, and on a client-side deadline kill the
//! process group, ack within a short grace, and synthesize exit code 124 when even that
//! fails. `model/src/run.rs` is the checked specification of this module; BIND-6 through
//! BIND-10 in `spec/core.symspec.json` are its requirements.

use std::time::Duration;

use futures_util::future::BoxFuture;

use super::Session;
use super::exec::{EndReason, ExecHandle, ExecResult, StreamEnd, StreamOptions};
use super::sse::ExecEvent;
use crate::error::{Error, WireKind};

/// The default [`CompletionOptions::client_grace`]: how long past the daemon's own deadline
/// the client waits before it kills, and how long it then waits for the killed exec's result.
///
/// Sixty seconds, the eval harvester's figure: the daemon escalates a timed-out group from
/// SIGTERM to SIGKILL after its ten-second `kill_grace`, and the rest covers a slow proxy.
pub const DEFAULT_CLIENT_GRACE: Duration = Duration::from_secs(60);

/// The client deadline of a request with no `timeout_sec`: the longest a MicroVM can live
/// (`MAX_DURATION_SEC`), since no command outlives the VM it runs in.
pub const NO_TIMEOUT_CEILING: Duration =
    Duration::from_secs(crate::constants::MAX_DURATION_SEC as u64);

/// A callback for output. Only [`ExecEvent::Output`] is delivered; the result carries the
/// exit status. Answering [`std::ops::ControlFlow::Break`] stops delivery, and the call
/// still waits for and acks the exec, so nothing is left running unobserved (BIND-8).
pub type OutputSink = Box<dyn FnMut(ExecEvent) -> OutputFlow + Send>;

/// What an [`OutputSink`] answers: a boxed future of continue-or-stop, named so a caller
/// needs no `futures` crate to spell it.
pub type OutputFlow = BoxFuture<'static, std::ops::ControlFlow<()>>;

/// The knobs of a run-to-completion call.
#[derive(Clone, Debug)]
pub struct CompletionOptions {
    /// Added to the request's `timeout_sec` to make the client deadline, and the budget of
    /// the wait-and-ack after a kill.
    pub client_grace: Duration,
    /// How the output stream behaves when a callback is given.
    pub stream: StreamOptions,
}

impl Default for CompletionOptions {
    fn default() -> Self {
        Self {
            client_grace: DEFAULT_CLIENT_GRACE,
            stream: StreamOptions::default(),
        }
    }
}

/// The daemon's answer to the kill the client sent when its deadline expired.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum KillAnswer {
    /// `killed: true`: a live process group was signalled.
    Signalled,
    /// `killed: false`: the group had already exited.
    AlreadyGone,
    /// The kill request failed, with this error.
    Failed(String),
}

/// What the client did when its own deadline expired (BIND-9, BIND-10).
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ClientDeadline {
    /// How long the client waited: `timeout_sec` plus the grace, or the ceiling.
    pub after: Duration,
    /// The daemon's answer to the kill.
    pub kill: KillAnswer,
    /// Why the post-kill wait-and-ack failed, when it did. Set means the result is
    /// synthesized: exit code 124, output unknown.
    pub ack_error: Option<String>,
}

/// A validated run-to-completion call, separable from the start request it was planned for.
///
/// Separate so a binding can start the exec under its sandbox lock and drive it after
/// releasing the lock: a callback that reaches back into the sandbox must not find the lock
/// held by the call that is invoking it.
#[derive(Clone, Debug)]
pub struct CompletionPlan {
    deadline: Duration,
    options: CompletionOptions,
}

impl CompletionPlan {
    /// Plans a call for `request`, refusing a `timeout_sec` no deadline can be built from
    /// before anything starts.
    pub fn new(
        request: &protocol::exec::StartRequest,
        options: CompletionOptions,
    ) -> Result<Self, Error> {
        let deadline = match request.timeout_sec {
            Some(seconds) => {
                crate::cost::duration_of_secs_f64(seconds)?.saturating_add(options.client_grace)
            }
            None => NO_TIMEOUT_CEILING,
        };
        Ok(Self { deadline, options })
    }

    /// The client deadline, measured from the start of [`Self::drive`].
    pub fn deadline(&self) -> Duration {
        self.deadline
    }

    /// Drives a started exec to exactly one result.
    ///
    /// The steps, each the specified behavior in `model/src/run.rs`:
    ///
    /// 1. With a callback, stream to it under the client deadline. The terminal `exit` event
    ///    means the output is final, so one ack returns it (BIND-8).
    /// 2. Without a callback, or when the stream ended without its `exit` event (a cut past its
    ///    reconnect budget, a fatal stream error, the callback answering `Break`), or when the
    ///    ack after the exit event failed: wait and ack under what is left of the deadline
    ///    (BIND-8). Polling is read-only, so this is safe whatever the stream saw.
    /// 3. When the client deadline expires in either step: kill the process group, then wait
    ///    and ack within the client grace (BIND-9). The kill is sent whatever its outcome turns
    ///    out to be, and a failed kill still gets the ack: the command may have finished by
    ///    itself, and its real result beats a synthesized one. The daemon answers the kill
    ///    only once the group is gone, SIGKILLing it after its ten-second `kill_grace` if it
    ///    must (measured 2026-09-24, us-east-1), so the kill can take that long before the
    ///    grace starts.
    /// 4. When that ack fails too: a synthesized result, exit code 124, naming both failures
    ///    (BIND-10). Nothing else synthesizes.
    ///
    /// An error is returned only for a failure that says nothing about a deadline: a fatal
    /// wait (the exec was collected, the token was refused). A retryable poll failure is
    /// retried inside the wait, as `ExecHandle::wait` always has.
    pub async fn drive(
        &self,
        handle: &ExecHandle,
        on_output: Option<OutputSink>,
    ) -> Result<ExecResult, Error> {
        let deadline = tokio::time::Instant::now() + self.deadline;
        if let Some(sink) = on_output {
            match tokio::time::timeout_at(deadline, self.stream(handle, sink)).await {
                Err(_elapsed) => return self.after_deadline(handle).await,
                Ok(true) => match handle.ack().await {
                    Ok(result) if result.done() && result.outcome.is_some() => return Ok(result),
                    // A failed or empty ack falls through to the wait, which re-reads the
                    // phase before acking again.
                    Ok(_) | Err(_) => {}
                },
                Ok(false) => {}
            }
        }
        match wait_and_ack_until(handle, deadline).await {
            Ok(result) => Ok(result),
            Err(error) if error.wire_kind() == Some(WireKind::ExecTimeout) => {
                self.after_deadline(handle).await
            }
            Err(error) => Err(error),
        }
    }

    /// Streams output to `sink`, answering whether the terminal `exit` event arrived.
    ///
    /// Everything else — a cut past the reconnect budget, a fatal stream error, the sink
    /// stopping — answers `false`, and the caller falls back to wait and ack. The error itself
    /// is dropped deliberately: the wait that follows re-reads the exec, and raises whatever
    /// is fatal about it there.
    async fn stream(&self, handle: &ExecHandle, mut sink: OutputSink) -> bool {
        let end = handle
            .for_each_event_async(self.options.stream.clone(), |event| match event {
                ExecEvent::Output { .. } => sink(event),
                ExecEvent::Gap { .. } | ExecEvent::Exit(_) => {
                    Box::pin(std::future::ready(std::ops::ControlFlow::Continue(())))
                }
            })
            .await;
        matches!(
            end,
            Ok(StreamEnd {
                reason: EndReason::Exited,
                ..
            })
        )
    }

    /// Steps 3 and 4 of [`Self::drive`]: kill, then wait and ack within the grace.
    async fn after_deadline(&self, handle: &ExecHandle) -> Result<ExecResult, Error> {
        let kill = match handle.kill().await {
            Ok(true) => KillAnswer::Signalled,
            Ok(false) => KillAnswer::AlreadyGone,
            Err(error) => KillAnswer::Failed(error.to_string()),
        };
        let grace = tokio::time::Instant::now() + self.options.client_grace;
        match wait_and_ack_until(handle, grace).await {
            Ok(mut result) => {
                result.client_deadline = Some(ClientDeadline {
                    after: self.deadline,
                    kill,
                    ack_error: None,
                });
                Ok(result)
            }
            // Any failure, fatal or not: the caller's deadline has passed, and the contract
            // every harness reports for that is 124. The note names what failed, so a fatal
            // error is visible rather than swallowed.
            Err(error) => Ok(ExecResult {
                exec_id: handle.exec_id().to_string(),
                phase: protocol::exec::Phase::Running,
                outcome: None,
                client_deadline: Some(ClientDeadline {
                    after: self.deadline,
                    kill,
                    ack_error: Some(error.to_string()),
                }),
            }),
        }
    }
}

/// How long to wait before retrying an ack that failed with a retryable error.
const ACK_RETRY_INTERVAL: Duration = Duration::from_secs(1);

/// [`ExecHandle::wait_and_ack`] until `deadline`, retrying an ack that failed retryably.
///
/// `wait_and_ack` retries a dropped poll but not a dropped ack: its ack is one request, and
/// a transport failure there ends the call although the result is sitting in the daemon. The
/// fuzz harness found that (a cut stream, then one failed ack, raised instead of returning),
/// so here the pair is repeated until the deadline. Repeating is safe because the wait
/// re-reads the phase first: an ack whose response was lost reads as `acked` and is not sent
/// twice. Running out of time is the wait's own `ExecTimeout`, so the caller's deadline path
/// is the same whichever request was in flight.
async fn wait_and_ack_until(
    handle: &ExecHandle,
    deadline: tokio::time::Instant,
) -> Result<ExecResult, Error> {
    loop {
        let remaining = deadline.saturating_duration_since(tokio::time::Instant::now());
        match handle.wait_and_ack(remaining).await {
            Err(error) if error.retryable() && error.wire_kind() != Some(WireKind::ExecTimeout) => {
                let left = deadline.saturating_duration_since(tokio::time::Instant::now());
                if left.is_zero() {
                    return Err(Error::wire(
                        WireKind::ExecTimeout,
                        format!(
                            "exec {} was not acknowledged before the deadline; the last \
                             attempt failed: {error}",
                            handle.exec_id()
                        ),
                    ));
                }
                tokio::time::sleep(ACK_RETRY_INTERVAL.min(left)).await;
            }
            other => return other,
        }
    }
}

impl Session {
    /// Starts `request` and drives it to exactly one result. See the module docs.
    pub async fn run_to_completion(
        &self,
        request: protocol::exec::StartRequest,
        options: CompletionOptions,
        on_output: Option<OutputSink>,
    ) -> Result<ExecResult, Error> {
        let plan = CompletionPlan::new(&request, options)?;
        let handle = self.run(request).await?;
        plan.drive(&handle, on_output).await
    }
}

#[cfg(test)]
mod tests {
    use std::sync::{Arc, Mutex};

    use base64::Engine as _;

    use super::*;
    use crate::session::testing::{Recorder, Reply, session_with};

    fn output(offset: u64, bytes: &[u8]) -> Vec<u8> {
        let encoded = base64::engine::general_purpose::STANDARD.encode(bytes);
        format!(
            "event: output\ndata: {{\"offset\":{offset},\"stream\":\"stdout\",\
             \"output\":\"{encoded}\"}}\n\n"
        )
        .into_bytes()
    }

    fn exit_frame(code: Option<i32>, signal: Option<i32>, timed_out: bool, total: u64) -> Vec<u8> {
        let event = protocol::exec::ExitEvent {
            exit_code: code,
            signal,
            timed_out,
            truncated: false,
            writers_may_be_alive: false,
            offset: total,
        };
        format!(
            "event: exit\ndata: {}\n\n",
            serde_json::to_string(&event).expect("serializes")
        )
        .into_bytes()
    }

    fn started() -> Reply {
        Reply::ok(serde_json::json!({"exec_id": "e1", "phase": "running"}))
    }

    fn running() -> Reply {
        Reply::ok(serde_json::json!({"exec_id": "e1", "phase": "running"}))
    }

    fn killed(killed: bool) -> Reply {
        Reply::ok(serde_json::json!({"exec_id": "e1", "killed": killed}))
    }

    /// A poll or ack body carrying an outcome.
    fn finished(phase: &str, code: Option<i32>, signal: Option<i32>, stdout: &str) -> Reply {
        Reply::ok(serde_json::json!({
            "exec_id": "e1", "phase": phase, "exit_code": code, "signal": signal,
            "timed_out": false, "stdout": stdout, "stderr": "", "truncated": false,
            "writers_may_be_alive": false,
        }))
    }

    fn request(timeout_sec: Option<f64>) -> protocol::exec::StartRequest {
        protocol::exec::StartRequest {
            exec_id: "e1".into(),
            command: vec!["bash".into(), "-c".into(), "echo hi".into()],
            shell: false,
            cwd: None,
            env: Default::default(),
            user: None,
            group: None,
            timeout_sec,
            stdin: false,
            reap_group_on_exit: false,
        }
    }

    type Seen = Arc<Mutex<Vec<(u64, Vec<u8>)>>>;

    /// A sink that records every chunk, and answers `flow` for each.
    fn recording_sink(flow: std::ops::ControlFlow<()>) -> (OutputSink, Seen) {
        let seen: Seen = Arc::new(Mutex::new(Vec::new()));
        let into = Arc::clone(&seen);
        let sink: OutputSink = Box::new(move |event| {
            if let ExecEvent::Output { offset, data, .. } = event {
                into.lock().expect("unpoisoned").push((offset, data));
            }
            Box::pin(std::future::ready(flow))
        });
        (sink, seen)
    }

    /// The method and path of every request, the shape each test asserts its sequence on.
    fn route(recorder: &Recorder) -> Vec<String> {
        recorder
            .requests()
            .iter()
            .map(|request| {
                let path = request.path.split('?').next().unwrap_or_default();
                format!("{} {path}", request.method)
            })
            .collect()
    }

    fn options(grace_secs: u64) -> CompletionOptions {
        CompletionOptions {
            client_grace: Duration::from_secs(grace_secs),
            stream: StreamOptions {
                reconnect: false,
                ..StreamOptions::default()
            },
        }
    }

    // ── BIND-8: the stream, and the fallback when it ends without an exit event ──

    /// **BIND-8: an exit event is acked directly, and the callback saw every chunk in order.**
    ///
    /// The ack is the request that releases the output, so it is the one result; a poll here
    /// would be a wasted round trip on every command a harness runs.
    #[tokio::test(start_paused = true)]
    async fn a_streamed_exit_event_is_acked_directly_with_every_chunk_delivered() {
        let recorder = Recorder::with([
            started(),
            Reply::Chunks(
                200,
                vec![
                    output(0, b"AB"),
                    output(2, b"CD"),
                    exit_frame(Some(0), None, false, 4),
                ],
            ),
            finished("acked", Some(0), None, "ABCD"),
        ]);
        let (session, _, _) = session_with(Arc::clone(&recorder));
        let (sink, seen) = recording_sink(std::ops::ControlFlow::Continue(()));

        let result = session
            .run_to_completion(request(None), options(60), Some(sink))
            .await
            .expect("completes");

        assert_eq!(result.stdout(), "ABCD");
        assert_eq!(result.posix_exit_code(), Some(0));
        assert!(result.client_deadline.is_none());
        assert_eq!(
            *seen.lock().expect("unpoisoned"),
            [(0, b"AB".to_vec()), (2, b"CD".to_vec())],
            "the callback missed or reordered a chunk"
        );
        assert_eq!(
            route(&recorder),
            [
                "POST /v1/exec/start",
                "GET /v1/exec/e1/stream",
                "POST /v1/exec/e1/ack"
            ],
            "an exit event needs one ack and nothing else"
        );
    }

    /// **BIND-8: a stream cut before its exit event falls back to wait and ack.**
    ///
    /// **Falsification** — return the streamed bytes when the stream ends `Cut` and this is red
    /// on the route: no poll, no ack, and a result nobody acked.
    #[tokio::test(start_paused = true)]
    async fn a_cut_stream_falls_back_to_wait_and_ack() {
        let recorder = Recorder::with([
            started(),
            Reply::Chunks(200, vec![output(0, b"AB")]),
            finished("exited", Some(0), None, "ABCD"),
            finished("acked", Some(0), None, "ABCD"),
        ]);
        let (session, _, _) = session_with(Arc::clone(&recorder));
        let (sink, seen) = recording_sink(std::ops::ControlFlow::Continue(()));

        let result = session
            .run_to_completion(request(None), options(60), Some(sink))
            .await
            .expect("the fallback returns the result");

        assert_eq!(
            result.stdout(),
            "ABCD",
            "the ack's output, not the stream's"
        );
        assert_eq!(seen.lock().expect("unpoisoned").len(), 1);
        assert_eq!(
            route(&recorder),
            [
                "POST /v1/exec/start",
                "GET /v1/exec/e1/stream",
                "GET /v1/exec/e1",
                "POST /v1/exec/e1/ack"
            ],
            "a cut stream must fall back to wait and ack"
        );
    }

    /// **BIND-8: a failed ack after the exit event falls back to wait and ack.**
    #[tokio::test(start_paused = true)]
    async fn a_failed_ack_after_the_exit_event_falls_back_to_wait_and_ack() {
        let recorder = Recorder::with([
            started(),
            Reply::Chunks(
                200,
                vec![output(0, b"x"), exit_frame(Some(3), None, false, 1)],
            ),
            Reply::Cut("connection reset"),
            finished("exited", Some(3), None, "x"),
            finished("acked", Some(3), None, "x"),
        ]);
        let (session, _, _) = session_with(Arc::clone(&recorder));
        let (sink, _) = recording_sink(std::ops::ControlFlow::Continue(()));

        let result = session
            .run_to_completion(request(None), options(60), Some(sink))
            .await
            .expect("the fallback returns the result");
        assert_eq!(result.posix_exit_code(), Some(3));
        assert_eq!(route(&recorder).len(), 5, "{:?}", route(&recorder));
    }

    /// **BIND-8: a callback that stops still gets the exec waited for and acked.**
    ///
    /// Stopping delivery is the caller's choice; leaving the exec un-acked would leave its
    /// output buffered in the daemon until the TTL, and the call would return nothing.
    #[tokio::test(start_paused = true)]
    async fn a_callback_that_stops_still_gets_one_acked_result() {
        let recorder = Recorder::with([
            started(),
            Reply::Chunks(
                200,
                vec![
                    output(0, b"A"),
                    output(1, b"B"),
                    exit_frame(Some(0), None, false, 2),
                ],
            ),
            finished("exited", Some(0), None, "AB"),
            finished("acked", Some(0), None, "AB"),
        ]);
        let (session, _, _) = session_with(Arc::clone(&recorder));
        let (sink, seen) = recording_sink(std::ops::ControlFlow::Break(()));

        let result = session
            .run_to_completion(request(None), options(60), Some(sink))
            .await
            .expect("completes");
        assert_eq!(result.stdout(), "AB");
        assert_eq!(
            seen.lock().expect("unpoisoned").len(),
            1,
            "delivery stopped"
        );
        assert_eq!(
            route(&recorder).last().map(String::as_str),
            Some("POST /v1/exec/e1/ack")
        );
    }

    /// **BIND-8: without a callback the call is one wait and one ack.**
    #[tokio::test(start_paused = true)]
    async fn without_a_callback_the_call_waits_and_acks() {
        let recorder = Recorder::with([
            started(),
            running(),
            finished("exited", None, Some(11), ""),
            finished("acked", None, Some(11), ""),
        ]);
        let (session, _, _) = session_with(Arc::clone(&recorder));

        let result = session
            .run_to_completion(request(Some(30.0)), options(60), None)
            .await
            .expect("completes");
        assert_eq!(result.posix_exit_code(), Some(128 + 11));
        assert!(!route(&recorder).iter().any(|r| r.contains("stream")));
    }

    /// **BIND-8: a dropped ack in the wait is retried, not raised.**
    ///
    /// `wait_and_ack` retries a dropped poll but not a dropped ack, so a transient failure on
    /// the one request that releases the output ended the call with an error while the result
    /// sat in the daemon. The fuzz harness found it (a cut stream, then one failed ack).
    ///
    /// **Falsification** — remove the retry arm in `wait_and_ack_until` and this raises
    /// `connection reset`. Verified.
    #[tokio::test(start_paused = true)]
    async fn a_dropped_ack_in_the_wait_is_retried() {
        let recorder = Recorder::with([
            started(),
            finished("exited", Some(0), None, "out"),
            Reply::Cut("connection reset"),
            finished("exited", Some(0), None, "out"),
            finished("acked", Some(0), None, "out"),
        ]);
        let (session, _, _) = session_with(Arc::clone(&recorder));

        let result = session
            .run_to_completion(request(Some(30.0)), options(60), None)
            .await
            .expect("the retried ack returns the result");
        assert_eq!(result.stdout(), "out");
        assert!(result.client_deadline.is_none());
        assert_eq!(route(&recorder).len(), 5, "{:?}", route(&recorder));
    }

    // ── BIND-9: the client deadline kills before it acks ─────────────────────

    /// **BIND-9: the client deadline kills the group, then acks within the grace.**
    ///
    /// `timeout_sec` 1 plus grace 1 is a two-second deadline; the poll loop sees `running` at
    /// 0, 1, and 2 seconds and gives up. The kill must precede the post-deadline ack, and the
    /// result must say the client's deadline ended it.
    ///
    /// **Falsification** — skip the kill and the route has no `kill`; ack before killing and
    /// the order assertion is red.
    #[tokio::test(start_paused = true)]
    async fn a_client_deadline_kills_then_acks_within_the_grace() {
        let recorder = Recorder::with([
            started(),
            running(),
            running(),
            running(),
            killed(true),
            finished("exited", None, Some(15), "partial"),
            finished("acked", None, Some(15), "partial"),
        ]);
        let (session, _, _) = session_with(Arc::clone(&recorder));

        let result = session
            .run_to_completion(request(Some(1.0)), options(1), None)
            .await
            .expect("the killed exec's result");

        let seen = route(&recorder);
        let kill = seen
            .iter()
            .position(|r| r.ends_with("/kill"))
            .expect("a kill");
        let ack = seen
            .iter()
            .rposition(|r| r.ends_with("/ack"))
            .expect("an ack");
        assert!(kill < ack, "the ack went out before the kill: {seen:?}");
        assert_eq!(result.stdout(), "partial");
        assert_eq!(
            result.client_deadline,
            Some(ClientDeadline {
                after: Duration::from_secs(2),
                kill: KillAnswer::Signalled,
                ack_error: None,
            })
        );
        assert!(!result.synthesized());
        assert_eq!(
            result.posix_exit_code(),
            Some(124),
            "the client deadline ended it"
        );
        assert!(
            result
                .notes()
                .iter()
                .any(|note| note.contains("client deadline")),
            "{:?}",
            result.notes()
        );
    }

    /// **BIND-9: a deadline while streaming kills too.** The stream goes silent, so only the
    /// deadline ends it.
    #[tokio::test(start_paused = true)]
    async fn a_deadline_while_streaming_kills_then_acks() {
        let recorder = Recorder::with([
            started(),
            Reply::Stalled(vec![output(0, b"tick\n")]),
            killed(true),
            finished("exited", None, Some(15), "tick\n"),
            finished("acked", None, Some(15), "tick\n"),
        ]);
        let (session, _, _) = session_with(Arc::clone(&recorder));
        let (sink, seen) = recording_sink(std::ops::ControlFlow::Continue(()));

        let result = session
            .run_to_completion(request(Some(5.0)), options(5), Some(sink))
            .await
            .expect("the killed exec's result");
        assert_eq!(seen.lock().expect("unpoisoned").len(), 1);
        assert_eq!(result.posix_exit_code(), Some(124));
        assert_eq!(
            route(&recorder)[2..],
            [
                "POST /v1/exec/e1/kill",
                "GET /v1/exec/e1",
                "POST /v1/exec/e1/ack"
            ]
        );
    }

    /// **BIND-9: a failed kill still gets the post-kill ack, and a command that exited by
    /// itself keeps its own exit code.**
    #[tokio::test(start_paused = true)]
    async fn a_failed_kill_still_acks_and_keeps_the_commands_own_status() {
        let recorder = Recorder::with([
            started(),
            running(),
            running(),
            running(),
            Reply::Cut("connection reset"),
            finished("exited", Some(0), None, "done"),
            finished("acked", Some(0), None, "done"),
        ]);
        let (session, _, _) = session_with(Arc::clone(&recorder));

        let result = session
            .run_to_completion(request(Some(1.0)), options(1), None)
            .await
            .expect("the result arrives after the failed kill");
        assert!(matches!(
            result.client_deadline.as_ref().map(|d| &d.kill),
            Some(KillAnswer::Failed(_))
        ));
        assert_eq!(
            result.posix_exit_code(),
            Some(0),
            "nothing the client did ended it"
        );
        assert!(!result.synthesized());
    }

    // ── BIND-10: 124 is synthesized only when the post-kill ack fails ────────

    /// **BIND-10: a successful kill followed by a failed ack synthesizes 124.**
    #[tokio::test(start_paused = true)]
    async fn a_failed_post_kill_ack_synthesizes_124() {
        let recorder = Recorder::with([
            started(),
            running(),
            running(),
            running(),
            killed(true),
            running(),
            running(),
        ]);
        let (session, _, _) = session_with(Arc::clone(&recorder));

        let result = session
            .run_to_completion(request(Some(1.0)), options(1), None)
            .await
            .expect("a synthesized result, not an error");
        assert!(result.synthesized());
        assert_eq!(result.posix_exit_code(), Some(124));
        assert_eq!(
            result.phase,
            protocol::exec::Phase::Running,
            "nothing was observed"
        );
        assert!(result.outcome.is_none());
        assert!(
            result
                .notes()
                .iter()
                .any(|note| note.contains("synthesized")),
            "{:?}",
            result.notes()
        );
        assert_eq!(
            route(&recorder).last().map(String::as_str),
            Some("GET /v1/exec/e1")
        );
    }

    /// **BIND-10: a failed kill and a failed ack synthesize 124, naming both.**
    #[tokio::test(start_paused = true)]
    async fn a_failed_kill_and_a_failed_ack_synthesize_124_naming_both() {
        let recorder = Recorder::with([
            started(),
            running(),
            running(),
            running(),
            Reply::Cut("kill refused by the test"),
            Reply::Cut("poll refused by the test"),
            Reply::Cut("poll refused by the test"),
        ]);
        let (session, _, _) = session_with(Arc::clone(&recorder));

        let result = session
            .run_to_completion(request(Some(1.0)), options(1), None)
            .await
            .expect("a synthesized result");
        assert!(result.synthesized());
        assert_eq!(result.posix_exit_code(), Some(124));
        let notes = result.notes().join("\n");
        assert!(notes.contains("kill refused by the test"), "{notes}");
    }

    // ── the plan ─────────────────────────────────────────────────────────────

    /// The deadline is `timeout_sec` plus the grace, the ceiling without a `timeout_sec`,
    /// and a `timeout_sec` no duration can be made of is refused before anything starts.
    #[tokio::test]
    async fn the_plan_computes_the_deadline_and_refuses_before_starting() {
        let plan = CompletionPlan::new(&request(Some(30.0)), options(60)).expect("plans");
        assert_eq!(plan.deadline(), Duration::from_secs(90));
        let plan = CompletionPlan::new(&request(None), options(60)).expect("plans");
        assert_eq!(plan.deadline(), NO_TIMEOUT_CEILING);

        let recorder = Recorder::with([]);
        let (session, _, _) = session_with(Arc::clone(&recorder));
        for bad in [f64::NAN, -1.0, f64::INFINITY] {
            let error = session
                .run_to_completion(request(Some(bad)), options(60), None)
                .await
                .expect_err("refused");
            assert_eq!(error.kind(), crate::ErrorKind::InvalidArg, "{bad}: {error}");
        }
        assert!(
            recorder.requests().is_empty(),
            "a refused plan started an exec"
        );
    }
}
