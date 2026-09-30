// SPDX-License-Identifier: Apache-2.0
//! CLI-8 and CLI-9: a reader that closes.

#![cfg(test)]

use std::sync::{Arc, Mutex};
use std::time::Duration;

use microvms_core::control::ControlPlane;
use microvms_core::prelude::*;
use microvms_core::sandbox::Sandbox;
use microvms_core::session::Session;
use microvms_core::testing::YieldingClock;
use microvms_core::{Error, ErrorKind, Region};

use super::support::{
    ScriptedSeam, ScriptedTransport, TempDir, attach_flags, full_infra, interrupt_run_args,
    microvm_body, region_flags,
};
use crate::cli::{AttachFlags, Command, ExecArgs};
use crate::commands::Ctx;
use crate::envelope::{Format, Output};
use crate::exit::Exit;
use crate::seam::futures_util_shim::BoxFuture;
use crate::seam::{Attach, CoreSeam, Infra};

/// A writer whose reader leaves after `flushes` complete flushes: until then every write is
/// accepted, and after it every write is `BrokenPipe`. Counting flushes rather than write
/// calls keeps a whole record on one side of the close, because `Output` writes a record and
/// then flushes it.
struct LeavesAfter {
    accepted: Vec<u8>,
    flushes: usize,
    /// Writes attempted after the reader left, including the one that found it gone.
    after_close: usize,
}

impl LeavesAfter {
    fn new(flushes: usize) -> Self {
        Self {
            accepted: Vec::new(),
            flushes,
            after_close: 0,
        }
    }
}

impl std::io::Write for LeavesAfter {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        if self.flushes == 0 {
            self.after_close += 1;
            return Err(std::io::ErrorKind::BrokenPipe.into());
        }
        self.accepted.extend_from_slice(bytes);
        Ok(bytes.len())
    }

    fn flush(&mut self) -> std::io::Result<()> {
        if self.flushes == 0 {
            return Err(std::io::ErrorKind::BrokenPipe.into());
        }
        self.flushes -= 1;
        Ok(())
    }
}

/// **CLI-8, the guard proof.** An interrupted `run` whose stdout and stderr readers have both
/// gone still tears the VM down, still writes the leak to the ledger on disk, and still exits
/// with its own outcome, `ERR_INTERRUPTED`.
///
/// The scenario is CLI-6's (`an_interrupt_after_launch_tears_down_and_names_every_leaked_identifier`)
/// with both streams closed from the first byte, which is `microvm run … 2>&1 | head -c0`. The
/// SIGPIPE-kill strategy the model rejects would die at the first progress line and never send
/// `TerminateMicrovm`; the "closed is a failure" policy would report `ERR_UNEXPECTED`.
///
/// **Falsification** — 2026-09-24. Making `closed_output::exit_code` return `Exit::Unexpected`
/// when stdout is closed turned the exit assertion red; restored after.
#[tokio::test]
async fn a_run_whose_readers_left_still_tears_down_and_keeps_its_outcome() {
    let dir = TempDir::new("closed-readers");
    let transport = Arc::new(ScriptedTransport::new());
    let (fire, fired) = tokio::sync::oneshot::channel();
    transport
        .answer("RunMicrovm", 200, &microvm_body("PENDING"))
        .answer("GetMicrovm", 200, &microvm_body("PENDING"))
        .answer(
            "TerminateMicrovm",
            409,
            r#"{"message": "ConflictException"}"#,
        )
        .fire_on("RunMicrovm", fire);
    let seam = ScriptedSeam {
        transport: Arc::clone(&transport),
        clock: Arc::new(YieldingClock::default()),
    };
    let args = interrupt_run_args(dir.0.clone());
    let mut out = Output::new(
        Format::Json,
        false,
        LeavesAfter::new(0),
        LeavesAfter::new(0),
    );
    let env = |_: &str| None;
    let interrupt: crate::commands::lifecycle::Interrupt<'_> = Box::pin(async move {
        let _ = fired.await;
    });
    let result = {
        let mut ctx = Ctx {
            seam: &seam,
            out: &mut out,
            infra: full_infra(),
            env: &env,
            fetch: &crate::provision::PanickingFetch,
        };
        crate::commands::lifecycle::run(&mut ctx, &args, interrupt).await
    };

    let failure = result.expect_err("an interrupt is a failure");
    // CLI-8: the teardown the run owed went to the wire although nobody reads its output.
    assert_eq!(
        transport.called("TerminateMicrovm"),
        1,
        "CLI-8: the teardown must run with both readers gone: {:?}",
        transport.calls()
    );
    // CLI-8: the ledger write happened, which is the operator's remedy for the leak.
    let ledgers = crate::ledger::read_all(&dir.0);
    assert_eq!(ledgers.len(), 1, "{ledgers:?}");
    assert_eq!(
        ledgers[0]["leaked"],
        serde_json::json!(["mvm-abc123"]),
        "{ledgers:?}"
    );
    // CLI-8: the exit is the run's own outcome, not something the closed readers chose.
    let exit =
        crate::closed_output::exit_code(failure.exit, !out.stdout_closed(), !out.stderr_closed());
    assert_eq!(exit, Exit::Interrupted, "{}", failure.message);
    // CLI-7: each stream was tried once, found gone, and never written again.
    assert!(out.stderr_closed(), "the progress write found stderr gone");
    let (_, stderr) = out.into_streams();
    assert_eq!(
        stderr.after_close, 1,
        "CLI-7: stderr must not be written again once its reader left"
    );
}

/// A daemon that answers `POST /v1/exec/start` and streams output frames with no `exit`.
///
/// Records every request, so a guard can assert that nothing killed the exec, and counts the
/// chunks the stream consumed, so it can assert how soon a stopped stream stopped reading.
struct StreamingDaemon {
    requests: Mutex<Vec<(&'static str, String)>>,
    pulled: Arc<std::sync::atomic::AtomicUsize>,
    frames: usize,
}

/// `tick\n`, base64. A constant because this crate has no base64 dependency to encode it with.
const TICK_B64: &str = "dGljawo=";

struct CountedFrames {
    remaining: std::collections::VecDeque<Vec<u8>>,
    pulled: Arc<std::sync::atomic::AtomicUsize>,
}

impl microvms_core::session::ChunkSource for CountedFrames {
    fn next_chunk(&mut self) -> BoxFuture<'_, Result<Option<Vec<u8>>, Error>> {
        let next = self.remaining.pop_front();
        if next.is_some() {
            self.pulled
                .fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        }
        Box::pin(async move { Ok(next) })
    }
}

impl microvms_core::session::HttpBackend for StreamingDaemon {
    fn send(
        &self,
        request: microvms_core::session::HttpRequest,
    ) -> BoxFuture<'_, Result<microvms_core::session::HttpResponse, Error>> {
        self.requests
            .lock()
            .expect("not poisoned")
            .push((request.method, request.path.clone()));
        let (status, body) = if request.path == "/v1/exec/start" {
            (200, r#"{"exec_id":"e1","phase":"running"}"#.to_string())
        } else {
            (404, r#"{"error":"not scripted"}"#.to_string())
        };
        Box::pin(async move {
            Ok(microvms_core::session::HttpResponse {
                status,
                headers: std::collections::HashMap::new(),
                body: body.into_bytes(),
            })
        })
    }

    fn open_stream(
        &self,
        request: microvms_core::session::HttpRequest,
        _idle_timeout: Duration,
    ) -> BoxFuture<'_, Result<microvms_core::session::OpenStream, Error>> {
        self.requests
            .lock()
            .expect("not poisoned")
            .push((request.method, request.path.clone()));
        let frames = (0..self.frames)
            .map(|index| {
                format!(
                    "event: output\ndata: {{\"offset\":{},\"stream\":\"stdout\",\"output\":\"{TICK_B64}\"}}\n\n",
                    index * 5
                )
                .into_bytes()
            })
            .collect();
        let pulled = Arc::clone(&self.pulled);
        Box::pin(async move {
            let head = microvms_core::session::HttpResponse {
                status: 200,
                headers: std::collections::HashMap::new(),
                body: Vec::new(),
            };
            let source: Box<dyn microvms_core::session::ChunkSource> = Box::new(CountedFrames {
                remaining: frames,
                pulled,
            });
            Ok((head, source))
        })
    }
}

/// A seam whose only door is a session over [`StreamingDaemon`].
struct DaemonSeam {
    daemon: Arc<StreamingDaemon>,
}

#[expect(
    clippy::disallowed_methods,
    reason = "a fake seam, the test's stand-in for src/seam.rs: it builds its plane or session over a scripted transport"
)]
impl CoreSeam for DaemonSeam {
    fn control_plane(&self, _region: Region) -> BoxFuture<'_, Result<ControlPlane, Error>> {
        Box::pin(async move { Err(Error::new(ErrorKind::Platform, "no control plane here")) })
    }

    fn open_sandbox(
        &self,
        _region: Region,
        _port: Option<u16>,
    ) -> BoxFuture<'_, Result<Sandbox, Error>> {
        Box::pin(async move { Err(Error::new(ErrorKind::Platform, "no sandbox here")) })
    }

    fn attach_session(
        &self,
        _region: Region,
        attach: Attach,
    ) -> BoxFuture<'_, Result<Session, Error>> {
        let backend = Arc::clone(&self.daemon) as microvms_core::session::SharedBackend;
        let _ = attach;
        Box::pin(async move {
            Session::builder("https://mvm-1.example", "t")
                .with_backend(backend)
                .build()
        })
    }

    fn put_artifact(&self, _uri: &str, _bytes: Vec<u8>) -> BoxFuture<'_, Result<(), Error>> {
        Box::pin(async move { Err(Error::new(ErrorKind::Platform, "no uploads here")) })
    }
}

/// **CLI-9, the guard proof.** `exec --stream` whose stdout reader leaves after the first
/// event stops at the next event, never asks the daemon to kill the exec, names the exec id
/// and the reattach command on stderr, and exits `ERR_INTERRUPTED`. In every format.
///
/// The daemon streams twenty frames and no `exit`, so a stream that ignored the closed pipe
/// would read all twenty and then end as a cut stream (`ERR_EXEC_FAILED`).
///
/// **Falsification** — 2026-09-24. Removing the `ControlFlow::Break` in `stream_exec` made
/// the daemon hand over all 20 frames and the exit `ERR_EXEC_FAILED`; restored after.
#[tokio::test]
async fn a_stream_whose_reader_leaves_stops_detaches_and_exits_interrupted() {
    for format in [Format::Json, Format::Plain, Format::Dense] {
        let dir = TempDir::new("closed-stream");
        let daemon = Arc::new(StreamingDaemon {
            requests: Mutex::new(Vec::new()),
            pulled: Arc::new(std::sync::atomic::AtomicUsize::new(0)),
            frames: 20,
        });
        let seam = DaemonSeam {
            daemon: Arc::clone(&daemon),
        };
        let command = Command::Exec(ExecArgs {
            command: Some("yes".into()),
            timeout: Duration::from_secs(30),
            timeout_sec: None,
            complete: false,
            client_grace: None,
            cwd: None,
            env: Vec::new(),
            user: None,
            group: None,
            shell: None,
            inherit_image_env: false,
            exec_id: Some("e1".into()),
            poll: None,
            detach: false,
            stream: true,
            from_offset: None,
            stdin: false,
            reap: false,
            kill_on_timeout: false,
            attach: AttachFlags {
                state_dir: Some(dir.0.clone()),
                ..attach_flags()
            },
            region: region_flags(),
        });
        // stdout's reader takes the first event and leaves.
        let mut out = Output::new(format, false, LeavesAfter::new(1), Vec::new());
        let env = |_: &str| None;
        let result = {
            let mut ctx = Ctx {
                seam: &seam,
                out: &mut out,
                infra: Infra::default(),
                env: &env,
                fetch: &crate::provision::PanickingFetch,
            };
            crate::handle(&mut ctx, &command, crate::commands::lifecycle::never()).await
        };

        let rendered = result.unwrap_or_else(|failure| {
            panic!(
                "{format:?}: the stream should end reported, not fail: {}",
                failure.message
            )
        });
        // CLI-9: exit ERR_INTERRUPTED, through the same decision main makes.
        let exit = crate::closed_output::exit_code(
            rendered.already_reported.unwrap_or(Exit::Ok),
            !out.stdout_closed(),
            !out.stderr_closed(),
        );
        assert_eq!(exit, Exit::Interrupted, "{format:?}");
        // CLI-9: stopped within one further event: the delivered one and the one that failed.
        let pulled = daemon.pulled.load(std::sync::atomic::Ordering::SeqCst);
        assert!(
            pulled <= 2,
            "{format:?}: CLI-9: {pulled} frames were read after stdout's reader left"
        );
        // CLI-9: nothing asked the daemon to stop the exec.
        let requests = daemon.requests.lock().expect("not poisoned").clone();
        assert!(
            requests.iter().all(|(_, path)| !path.contains("/kill")),
            "{format:?}: CLI-9: the exec must be left running: {requests:?}"
        );
        // CLI-9: the exec id and how to reattach, on stderr.
        let (stdout, stderr) = out.into_streams();
        let stderr = String::from_utf8(stderr).expect("utf8");
        assert!(
            stderr.contains("exec e1 is still running")
                && stderr.contains("microvm exec --exec-id e1 --stream --from-offset"),
            "{format:?}: CLI-9: {stderr}"
        );
        // CLI-7: stdout was never written again after the failing write.
        assert_eq!(stdout.after_close, 1, "{format:?}");
    }
}
