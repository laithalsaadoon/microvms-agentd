// SPDX-License-Identifier: Apache-2.0
//! One exec's output as two byte channels, stdout and stderr.
//!
//! # One drive, one cursor, two channels
//!
//! The daemon publishes stdout and stderr into **one** SSE stream with a `stream`
//! discriminator per output frame, sharing **one** offset space. That's what makes the byte
//! cursor work: a single cursor can't be split into two without inventing an ordering between
//! them that the wire never stated. So [`ExecHandle::split`] runs one
//! [`ExecHandle::for_each_event_async`] drive, reconnects and all, and routes each output
//! frame to its side's channel. Order is kept within each side and isn't recoverable between
//! them; a caller that needs the interleaving reads [`ExecHandle::stream_with`] instead.
//!
//! Both bindings' process shapes (Node's and Python's `Session.spawn`) read these channels,
//! so the routing, the gap attribution and the gap policy are written once, here.
//!
//! # A gap is attributed to the next output frame's stream
//!
//! A `gap` frame carries no discriminator: the offset space is shared, so a gap is a hole in
//! the combined stream and the daemon can't say which side's bytes were in it. The bytes that
//! resumed after the hole came out of one side, and that side's log is the one with a hole in
//! it, so a gap is held until the next output frame and takes its stream. A gap the stream
//! ends on has nothing after it, and is recorded with no stream rather than guessed at.
//!
//! # The gap policy
//!
//! [`GapPolicy::Error`], the default, ends **both** channels with an
//! [`WireKind::OutputGap`] error naming the range: both, because the shared offset space can't
//! say which side lost the bytes, and erroring one would leave the other looking complete when
//! it may be the truncated one. An error can't be ignored by the obvious consumer, a loop over
//! one side's chunks, and a swallowed gap would hand it a contiguous-looking stream that's
//! missing bytes. [`GapPolicy::Event`] records the gap in [`Split::gaps`] and keeps both
//! channels open, for a caller that wants the surviving bytes more than the completeness
//! guarantee (a log tail, a progress display).
//!
//! [`StreamOptions::error_on_gap`] is ignored here and always off: a drive that ended on the
//! gap would lose the range's attribution and never fill [`Split::gaps`]. The gap arrives as
//! an event and the policy is applied one layer up, in this module's drive.
//!
//! # One chunk per channel
//!
//! Each channel holds one chunk, for the reason `exec.rs` gives: the daemon's SSE body is the
//! backpressure signal, and buffering here would defeat the cursor the drive reconnects at.
//! Per channel rather than shared, so a slow stderr reader doesn't hold chunks stdout has
//! already been handed. A channel nobody reads still stalls the drive once it holds its one
//! chunk, the same bound a pipe has, so a caller reads both sides concurrently or drops the
//! one it doesn't want. Dropping either receiver ends the drive, since both share it.

use std::sync::{Arc, Mutex, PoisonError};

use futures_util::future::BoxFuture;
use protocol::exec::StreamKind;
use tokio::sync::mpsc;

use super::exec::{ExecHandle, StreamOptions};
use super::sse::ExecEvent;
use crate::error::{Error, WireKind};

/// What a split drive does when the daemon reports evicted output. See the module docs.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub enum GapPolicy {
    /// **The default.** A gap ends both channels with an `OutputGap` error naming the range.
    #[default]
    Error,
    /// A gap is recorded in [`Split::gaps`] and both channels stay open.
    Event,
}

impl GapPolicy {
    /// Both policies, the default first.
    pub const ALL: [GapPolicy; 2] = [GapPolicy::Error, GapPolicy::Event];

    /// The name a binding takes it by: `"error"` or `"event"`.
    pub const fn as_str(self) -> &'static str {
        match self {
            GapPolicy::Error => "error",
            GapPolicy::Event => "event",
        }
    }
}

impl std::str::FromStr for GapPolicy {
    type Err = Error;

    /// A policy by its [`GapPolicy::as_str`] name, refused with `ERR_INVALID_ARG` otherwise.
    fn from_str(name: &str) -> Result<Self, Error> {
        GapPolicy::ALL
            .into_iter()
            .find(|policy| policy.as_str() == name)
            .ok_or_else(|| {
                Error::invalid_arg(format!(
                    "gap policy {name:?} is not one of \"error\" or \"event\""
                ))
            })
    }
}

/// One byte range the daemon couldn't replay, under [`GapPolicy::Event`].
///
/// `from` inclusive, `to` exclusive, so `to` is exactly the offset a resume would pass.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct OutputGap {
    /// The stream of the output frame that followed the gap, or `None` when the stream ended
    /// on it. Never read off the gap frame, which carries no discriminator (module docs).
    pub stream: Option<StreamKind>,
    pub from: u64,
    pub to: u64,
}

/// What a channel carries: a chunk of one side's bytes, or the error that ended it.
pub type SplitItem = Result<Vec<u8>, Error>;

/// The gaps a split drive has recorded so far, shared with the drive.
#[derive(Clone, Debug, Default)]
pub struct GapLog(Arc<Mutex<Vec<OutputGap>>>);

impl GapLog {
    /// Every gap recorded so far, oldest first. Empty under [`GapPolicy::Error`].
    pub fn snapshot(&self) -> Vec<OutputGap> {
        self.0
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .clone()
    }

    fn record(&self, gaps: impl IntoIterator<Item = OutputGap>) {
        self.0
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .extend(gaps);
    }
}

/// One exec's output as two channels, and the drive that fills them.
///
/// Built by [`ExecHandle::split`]. Nothing moves until [`Split::drive`] runs: the caller
/// spawns it on its own runtime (each binding has one, and this crate chooses none). A
/// channel closes, with no error, after the terminal `exit` event, and the drive ends then too.
pub struct Split {
    /// The child's standard output.
    pub stdout: mpsc::Receiver<SplitItem>,
    /// The child's standard error.
    pub stderr: mpsc::Receiver<SplitItem>,
    /// The gaps recorded under [`GapPolicy::Event`].
    pub gaps: GapLog,
    /// The one stream drive both channels share. Spawn it.
    pub drive: BoxFuture<'static, ()>,
}

impl ExecHandle {
    /// This exec's output as two byte channels over one drive. See the module docs.
    ///
    /// `options` is how the drive reads the stream (offset, reconnects, idle timeout), except
    /// `error_on_gap`, which `policy` replaces. A drive error (a spent reconnect budget, a
    /// refused reconnect) ends both channels with that error, so a truncated stream never
    /// reads as a finished one.
    pub fn split(self: Arc<Self>, options: StreamOptions, policy: GapPolicy) -> Split {
        let (out_tx, stdout) = mpsc::channel(1);
        let (err_tx, stderr) = mpsc::channel(1);
        let gaps = GapLog::default();
        let options = StreamOptions {
            error_on_gap: false,
            ..options
        };
        let drive = Box::pin(drive(self, options, policy, out_tx, err_tx, gaps.clone()));
        Split {
            stdout,
            stderr,
            gaps,
            drive,
        }
    }
}

/// Routes one drive's events into the two channels.
async fn drive(
    handle: Arc<ExecHandle>,
    options: StreamOptions,
    policy: GapPolicy,
    out_tx: mpsc::Sender<SplitItem>,
    err_tx: mpsc::Sender<SplitItem>,
    gaps: GapLog,
) {
    // Gaps seen and not yet attributed. Shared by handle rather than a plain local, because
    // the callback's future is a plain type parameter that can't borrow the closure's
    // captures (`for_each_event_async`'s docs), so state that spans events is shared.
    let held: Arc<Mutex<Vec<(u64, u64)>>> = Arc::default();
    let end = handle
        .for_each_event_async(options, |event| {
            // Cloned per event, for the same reason. One atomic increment each.
            let out_tx = out_tx.clone();
            let err_tx = err_tx.clone();
            let gaps = gaps.clone();
            let held = Arc::clone(&held);
            async move {
                match event {
                    ExecEvent::Output { stream, data, .. } => {
                        // Drained into its own statement, so the lock is released before the
                        // send below awaits.
                        let attributed: Vec<(u64, u64)> = held
                            .lock()
                            .unwrap_or_else(PoisonError::into_inner)
                            .drain(..)
                            .collect();
                        gaps.record(attributed.into_iter().map(|(from, to)| OutputGap {
                            stream: Some(stream),
                            from,
                            to,
                        }));
                        let side = match stream {
                            StreamKind::Stdout => &out_tx,
                            StreamKind::Stderr => &err_tx,
                        };
                        // Awaited, not `try_send`: the channel is full whenever its reader is
                        // behind, which is the normal case, and dropping there would lose
                        // output the cursor believes was delivered.
                        match side.send(Ok(data)).await {
                            Ok(()) => std::ops::ControlFlow::Continue(()),
                            // The reader went away. Ending the drive is what stops a task
                            // reading a body nobody reads.
                            Err(_) => std::ops::ControlFlow::Break(()),
                        }
                    }
                    ExecEvent::Gap { from, to } => match policy {
                        GapPolicy::Error => {
                            reject_both(&out_tx, &err_tx, gap_error(from, to)).await;
                            // Nothing more goes into channels that have already errored.
                            std::ops::ControlFlow::Break(())
                        }
                        GapPolicy::Event => {
                            // Held: the next output frame names the stream it belongs to.
                            held.lock()
                                .unwrap_or_else(PoisonError::into_inner)
                                .push((from, to));
                            std::ops::ControlFlow::Continue(())
                        }
                    },
                    // Nothing is sent for the terminal event: dropping the senders when the
                    // drive returns is what closes both channels.
                    ExecEvent::Exit(_) => std::ops::ControlFlow::Continue(()),
                }
            }
        })
        .await;

    // A gap the stream ended on has no following frame to take a stream from.
    let trailing: Vec<(u64, u64)> = held
        .lock()
        .unwrap_or_else(PoisonError::into_inner)
        .drain(..)
        .collect();
    gaps.record(trailing.into_iter().map(|(from, to)| OutputGap {
        stream: None,
        from,
        to,
    }));

    // A drive error reaches both readers, so a spent reconnect budget rejects a read rather
    // than ending it: a silent end reads as complete output.
    if let Err(error) = end {
        reject_both(&out_tx, &err_tx, error).await;
    }
}

/// The error a gap ends both channels with under [`GapPolicy::Error`].
///
/// Typed like [`StreamOptions::error_on_gap`]'s (`ERR_PLATFORM`, [`WireKind::OutputGap`]), so a
/// caller tells lost output from a transport failure it could retry, and it names the range
/// and where a resume starts.
fn gap_error(from: u64, to: u64) -> Error {
    Error::wire(
        WireKind::OutputGap,
        format!(
            "output bytes [{from}, {to}) are unrecoverable: the daemon evicted them before this \
             client read them. Resume from offset {to}, or read with the event gap policy to \
             keep the surviving bytes instead."
        ),
    )
}

/// Ends both channels with one failure.
///
/// [`Error`] isn't `Clone`, so stderr gets [`twin`] of it and stdout the original, source and
/// all. Both sends at once rather than one after the other: each channel holds one chunk, so
/// while a stdout chunk sits unread a stdout-first send parks, and a caller reading stderr to
/// the end before stdout would wait forever for a rejection that's never sent. A failed send
/// means that reader is gone, and there's nobody left to tell.
async fn reject_both(
    out_tx: &mpsc::Sender<SplitItem>,
    err_tx: &mpsc::Sender<SplitItem>,
    error: Error,
) {
    let copy = twin(&error);
    let _ = tokio::join!(out_tx.send(Err(error)), err_tx.send(Err(copy)));
}

/// The same failure again: its kind, its wire kind and its message.
///
/// Exact on all three, since [`Error::wire`] derives the kind from the wire kind and
/// [`Error::new`] sets none. The source stays with the original.
fn twin(error: &Error) -> Error {
    match error.wire_kind() {
        Some(wire) => Error::wire(wire, error.to_string()),
        None => Error::new(error.kind(), error.to_string()),
    }
}

#[cfg(test)]
mod tests {
    use std::time::Duration;

    use base64::Engine as _;

    use super::*;
    use crate::error::ErrorKind;
    use crate::session::testing::{Recorder, Reply, session_with};

    /// One SSE `output` frame on `stream`.
    fn output(stream: &str, offset: u64, bytes: &[u8]) -> Vec<u8> {
        let encoded = base64::engine::general_purpose::STANDARD.encode(bytes);
        format!(
            "event: output\ndata: {{\"offset\":{offset},\"stream\":\"{stream}\",\
             \"output\":\"{encoded}\"}}\n\n"
        )
        .into_bytes()
    }

    fn gap(from: u64, to: u64) -> Vec<u8> {
        format!("event: gap\ndata: {{\"from\":{from},\"to\":{to}}}\n\n").into_bytes()
    }

    fn exit(total: u64) -> Vec<u8> {
        format!(
            "event: exit\ndata: {{\"exit_code\":0,\"signal\":null,\"truncated\":false,\
             \"writers_may_be_alive\":false,\"offset\":{total}}}\n\n"
        )
        .into_bytes()
    }

    /// One side read to its end: the bytes it carried, and the error that ended it, if one did.
    async fn drain(mut side: mpsc::Receiver<SplitItem>) -> (Vec<u8>, Option<Error>) {
        let mut bytes = Vec::new();
        while let Some(item) = side.recv().await {
            match item {
                Ok(chunk) => bytes.extend_from_slice(&chunk),
                Err(error) => return (bytes, Some(error)),
            }
        }
        (bytes, None)
    }

    /// A side that ended cleanly, as its bytes. Panics naming the error when it didn't.
    fn clean(name: &str, side: (Vec<u8>, Option<Error>)) -> Vec<u8> {
        if let Some(error) = side.1 {
            panic!("{name} ended with an error: {error}");
        }
        side.0
    }

    /// Splits a scripted exec's stream under `policy`, reads both sides at once to their ends,
    /// and waits for the drive.
    async fn split_and_read(
        replies: Vec<Reply>,
        policy: GapPolicy,
    ) -> (
        (Vec<u8>, Option<Error>),
        (Vec<u8>, Option<Error>),
        Vec<OutputGap>,
    ) {
        let (session, _, _) = session_with(Recorder::with(replies));
        let split = Arc::new(session.exec("x-split")).split(StreamOptions::default(), policy);
        let drive = tokio::spawn(split.drive);
        let (out, err) = tokio::join!(drain(split.stdout), drain(split.stderr));
        drive.await.expect("the drive ran to its end");
        (out, err, split.gaps.snapshot())
    }

    /// Each output frame goes to its own side, in order, and both close cleanly on `exit`.
    #[tokio::test(start_paused = true)]
    async fn each_output_frame_goes_to_its_own_side_and_both_close_on_exit() {
        let replies = vec![Reply::Chunks(
            200,
            vec![
                output("stdout", 0, b"out1 "),
                output("stderr", 5, b"err1 "),
                output("stdout", 10, b"out2"),
                output("stderr", 14, b"err2"),
                exit(18),
            ],
        )];
        let (out, err, gaps) = split_and_read(replies, GapPolicy::Error).await;

        assert_eq!(clean("stdout", out), b"out1 out2");
        assert_eq!(clean("stderr", err), b"err1 err2");
        assert!(gaps.is_empty());
    }

    /// **Under `Event`, a gap takes the stream of the output frame after it (#261).**
    ///
    /// The gap frame carries no discriminator, and the side whose bytes resumed after the
    /// hole is the side with the hole in its log. Here stdout wrote before the gap and stderr
    /// after it, so a gap attributed to the frame *before* it would name stdout.
    ///
    /// **Falsification**: record a held gap under stdout, the stream of the frame before it
    /// here, instead of the next frame's, and the recorded stream is `Some(Stdout)`.
    #[tokio::test(start_paused = true)]
    async fn under_event_a_gap_takes_the_stream_of_the_frame_after_it() {
        let replies = vec![Reply::Chunks(
            200,
            vec![
                output("stdout", 0, b"before"),
                gap(6, 900),
                output("stderr", 900, b"after"),
                exit(905),
            ],
        )];
        let (out, err, gaps) = split_and_read(replies, GapPolicy::Event).await;

        assert_eq!(
            gaps,
            [OutputGap {
                stream: Some(StreamKind::Stderr),
                from: 6,
                to: 900
            }],
            "the gap names the side whose log has the hole"
        );
        assert_eq!(clean("stdout", out), b"before");
        assert_eq!(clean("stderr", err), b"after");
    }

    /// **Under `Error`, a gap ends both sides with an `OutputGap` error naming the range.**
    ///
    /// Both, because the shared offset space can't say which side lost the bytes. The bytes
    /// before the gap are still delivered, and nothing after it is.
    ///
    /// **Falsification**: send the gap's error to stdout alone and stderr ends cleanly, so
    /// `err.1` is `None`.
    #[tokio::test(start_paused = true)]
    async fn under_error_a_gap_ends_both_sides_naming_the_range() {
        let replies = vec![Reply::Chunks(
            200,
            vec![
                output("stdout", 0, b"before"),
                gap(6, 900),
                output("stderr", 900, b"after"),
                exit(905),
            ],
        )];
        let (out, err, gaps) = split_and_read(replies, GapPolicy::Error).await;

        assert_eq!(out.0, b"before", "the bytes before the gap were discarded");
        assert_eq!(
            err.0, b"",
            "a byte after the gap was delivered into an errored side"
        );
        for (name, error) in [("stdout", out.1), ("stderr", err.1)] {
            let error = error.unwrap_or_else(|| panic!("{name} ended cleanly with a hole in it"));
            assert_eq!(
                error.wire_kind(),
                Some(WireKind::OutputGap),
                "{name}: {error}"
            );
            assert_eq!(error.kind(), ErrorKind::Platform, "{name}: {error}");
            let message = error.to_string();
            assert!(message.contains("[6, 900)"), "{name}: the range: {message}");
            assert!(
                message.contains("offset 900"),
                "{name}: where to resume: {message}"
            );
        }
        assert!(gaps.is_empty(), "the error policy records nothing");
    }

    /// A gap the stream ends on has no frame after it, and is recorded with no stream.
    #[tokio::test(start_paused = true)]
    async fn a_gap_the_stream_ends_on_is_recorded_with_no_stream() {
        let replies = vec![Reply::Chunks(
            200,
            vec![output("stdout", 0, b"AA"), gap(2, 900), exit(900)],
        )];
        let (out, err, gaps) = split_and_read(replies, GapPolicy::Event).await;

        assert_eq!(
            gaps,
            [OutputGap {
                stream: None,
                from: 2,
                to: 900
            }]
        );
        assert_eq!(clean("stdout", out), b"AA");
        assert_eq!(clean("stderr", err), b"");
    }

    /// A drive error ends both sides with the same code, wire kind and message.
    ///
    /// Here the stream is cut and the reconnect is refused with a 401, which no reconnect can
    /// fix: both readers get `ERR_CREDENTIALS` and `Unauthorized`, so neither reads a cut
    /// stream as a finished one.
    #[tokio::test(start_paused = true)]
    async fn a_drive_error_ends_both_sides_with_its_code_and_message() {
        let replies = vec![
            Reply::Chunks(200, vec![output("stdout", 0, b"AA")]),
            Reply::Body(401, b"wrong token".to_vec()),
        ];
        let (out, err, _) = split_and_read(replies, GapPolicy::Error).await;

        assert_eq!(out.0, b"AA");
        let out = out.1.expect("stdout ended cleanly on a refused reconnect");
        let err = err.1.expect("stderr ended cleanly on a refused reconnect");
        for (name, error) in [("stdout", &out), ("stderr", &err)] {
            assert_eq!(
                error.wire_kind(),
                Some(WireKind::Unauthorized),
                "{name}: {error}"
            );
            assert_eq!(error.code(), "ERR_CREDENTIALS", "{name}: {error}");
            assert!(error.to_string().contains("401"), "{name}: {error}");
        }
        assert_eq!(
            out.to_string(),
            err.to_string(),
            "stderr's copy is the same failure"
        );
    }

    /// **A stderr reader gets the drive error while a stdout chunk sits unread.**
    ///
    /// Each side holds one chunk. A stdout-first rejection parks behind the unread chunk, so a
    /// caller reading stderr to its end before touching stdout would wait forever.
    ///
    /// **Falsification**: send the two rejections one after the other, stdout first, and this
    /// times out: stderr never settles.
    #[tokio::test(start_paused = true)]
    async fn a_stderr_reader_gets_the_drive_error_while_a_stdout_chunk_sits_unread() {
        let replies = vec![
            Reply::Chunks(200, vec![output("stdout", 0, b"AA")]),
            Reply::Body(401, b"wrong token".to_vec()),
        ];
        let (session, _, _) = session_with(Recorder::with(replies));
        let split =
            Arc::new(session.exec("x-split")).split(StreamOptions::default(), GapPolicy::Error);
        let drive = tokio::spawn(split.drive);

        let (bytes, error) = tokio::time::timeout(Duration::from_secs(60), drain(split.stderr))
            .await
            .expect("stderr never settled while stdout held a chunk");
        assert!(bytes.is_empty());
        assert_eq!(
            error.map(|error| error.code()),
            Some("ERR_CREDENTIALS"),
            "stderr ended without the drive error"
        );
        let (bytes, error) = drain(split.stdout).await;
        assert_eq!(bytes, b"AA", "stdout's unread chunk is still delivered");
        assert!(error.is_some(), "stdout got the drive error too");
        drive.await.expect("the drive ran to its end");
    }

    /// Dropping a side ends the drive rather than leaving a task reading a body nobody reads.
    #[tokio::test(start_paused = true)]
    async fn dropping_a_side_ends_the_drive() {
        let replies = vec![Reply::Stalled(vec![
            output("stdout", 0, b"AA"),
            output("stdout", 2, b"BB"),
        ])];
        let (session, _, _) = session_with(Recorder::with(replies));
        let split =
            Arc::new(session.exec("x-split")).split(StreamOptions::default(), GapPolicy::Error);
        drop(split.stdout);
        tokio::time::timeout(Duration::from_secs(60), split.drive)
            .await
            .expect("the drive kept reading after its stdout reader went away");
    }

    /// The policies' names are the ones the bindings take, and anything else is refused.
    #[test]
    fn a_gap_policy_is_named_error_or_event() {
        for policy in GapPolicy::ALL {
            assert_eq!(policy.as_str().parse::<GapPolicy>().ok(), Some(policy));
        }
        assert_eq!(GapPolicy::default(), GapPolicy::Error);
        let refused = "Error"
            .parse::<GapPolicy>()
            .expect_err("names are lowercase");
        assert_eq!(refused.code(), "ERR_INVALID_ARG");
        assert!(refused.to_string().contains("\"Error\""), "{refused}");
    }

    /// `error_on_gap` can't turn a split's gap into a drive error: the policy decides.
    #[tokio::test(start_paused = true)]
    async fn error_on_gap_is_replaced_by_the_policy() {
        let (session, _, _) = session_with(Recorder::with([Reply::Chunks(
            200,
            vec![
                output("stdout", 0, b"AA"),
                gap(2, 9),
                output("stdout", 9, b"ZZ"),
                exit(11),
            ],
        )]));
        let options = StreamOptions {
            error_on_gap: true,
            ..StreamOptions::default()
        };
        let split = Arc::new(session.exec("x-split")).split(options, GapPolicy::Event);
        let drive = tokio::spawn(split.drive);
        let (out, err) = tokio::join!(drain(split.stdout), drain(split.stderr));
        drive.await.expect("the drive ran to its end");
        assert_eq!(clean("stdout", out), b"AAZZ");
        assert_eq!(clean("stderr", err), b"");
        assert_eq!(split.gaps.snapshot().len(), 1);
    }
}
