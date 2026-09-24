// SPDX-License-Identifier: Apache-2.0
//! The fuzz harness for BIND-6 through BIND-10: the real `Session::run_to_completion` against
//! a scripted daemon whose command fate, stream, kill, and ack and poll faults are all drawn.
//!
//! `bolero::check!` runs this as an ordinary `#[test]` under stable `cargo test`, and as a
//! coverage-guided target under `cargo +nightly bolero test run_to_completion_plan -p
//! microvms-core --test run_to_completion_fuzz`. Each iteration builds a current-thread
//! runtime with tokio's clock paused, so a plan whose deadline is minutes away runs in
//! microseconds and every ordering is the script's.
//!
//! # What it checks
//!
//! * BIND-8: every plan returns one result; a result that is not synthesized is the acked
//!   one (phase `acked`, an outcome, and the daemon recorded a successful ack); a stream cut
//!   before its exit event is followed by a poll.
//! * BIND-9: a kill is sent only after the client deadline, and it precedes the last poll or
//!   ack.
//! * BIND-10: a result is synthesized exactly when no ack succeeded, and it reports 124 with a
//!   note saying so.
//! * BIND-6: the POSIX exit code equals [`model_posix`], a literal copy of the specification
//!   in `model/src/run.rs` (`posix_exit_code`). Core cannot depend on the model crate for the
//!   reason `microvms-cli/src/closed_output_fuzz.rs` gives: `deny.toml` refuses the wildcard.
//! * BIND-7: one note per condition, and none for a clean result.

#[allow(dead_code)]
mod sim_daemon;

use std::time::Duration;

use microvms_core::protocol::exec::{Phase, StartRequest};
use microvms_core::session::{
    ClientDeadline, CompletionOptions, ExecResult, KillAnswer, OutputSink, StreamOptions,
};
use sim_daemon::{Ending, KillMode, Script, SimDaemon, StreamMode};

/// One fuzzed call.
#[derive(Debug, bolero::TypeGenerator)]
struct Plan {
    /// A callback was given, so the call streams first.
    streaming: bool,
    /// Seconds until the command finishes by itself, or `None` for one only a kill ends.
    finishes_after: Option<u8>,
    /// 0 exits, 1 dies to a signal, 2 the daemon deadline's signal, 3 exits after the daemon
    /// deadline (a SIGTERM-trapping child).
    ending: u8,
    code: u8,
    cut: bool,
    kill_fails: bool,
    kill_ends_after: u8,
    ack_failures: u8,
    poll_failures: u8,
    timeout_sec: Option<u8>,
    grace: u8,
    truncated: bool,
}

/// The seconds a plan's times are reduced modulo, so deadlines and fates interleave.
const SPAN: u8 = 12;

fn script_of(plan: &Plan) -> Script {
    let signal = [9, 11, 15][usize::from(plan.code % 3)];
    let ending = match plan.ending % 4 {
        0 => Ending {
            exit_code: Some(i32::from(plan.code % 5)),
            ..Ending::default()
        },
        1 => Ending {
            signal: Some(signal),
            ..Ending::default()
        },
        2 => Ending {
            signal: Some(if plan.code.is_multiple_of(2) { 15 } else { 9 }),
            timed_out: true,
            ..Ending::default()
        },
        _ => Ending {
            exit_code: Some(0),
            timed_out: true,
            ..Ending::default()
        },
    };
    Script {
        stdout: "out".into(),
        finishes_after: plan
            .finishes_after
            .map(|after| Duration::from_secs(u64::from(after % SPAN))),
        ending,
        truncated: plan.truncated,
        stream: if plan.cut {
            StreamMode::Cut
        } else {
            StreamMode::Exit
        },
        kill: if plan.kill_fails {
            KillMode::Fails
        } else {
            KillMode::Ends(Duration::from_secs(u64::from(plan.kill_ends_after % 4)))
        },
        ack_failures: u32::from(plan.ack_failures % 3),
        poll_failures: u32::from(plan.poll_failures % 3),
    }
}

/// The specification of `posix_exit_code` in `model/src/run.rs`, copied literally.
fn model_posix(result: &ExecResult) -> Option<i32> {
    if let Some(client) = &result.client_deadline
        && (client.ack_error.is_some() || client.kill == KillAnswer::Signalled)
    {
        return Some(124);
    }
    let outcome = result.outcome.as_ref()?;
    if outcome.timed_out {
        return Some(124);
    }
    match (outcome.exit_code, outcome.signal) {
        (Some(code), _) => Some(code),
        (None, Some(signal)) => Some(128 + signal),
        (None, None) => None,
    }
}

fn drive(plan: &Plan) -> (ExecResult, Vec<String>, bool) {
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .start_paused(true)
        .build()
        .expect("a runtime");
    let script = script_of(plan);
    // A command only a kill ends needs a deadline this side of the ceiling, or the plan
    // polls for eight simulated hours.
    let timeout_sec = match (plan.timeout_sec, &script.finishes_after) {
        (Some(timeout), _) => Some(f64::from(timeout % SPAN)),
        (None, None) => Some(5.0),
        (None, Some(_)) => None,
    };
    runtime.block_on(async {
        let daemon = SimDaemon::new(script);
        let session = daemon.session();
        let request = StartRequest {
            exec_id: "x-fuzz".into(),
            command: vec!["bash".into(), "-c".into(), "fuzzed".into()],
            shell: false,
            cwd: None,
            env: Default::default(),
            user: None,
            group: None,
            timeout_sec,
            stdin: false,
            reap_group_on_exit: false,
        };
        let options = CompletionOptions {
            client_grace: Duration::from_secs(u64::from(plan.grace % SPAN)),
            // A small reconnect budget, so a cut stream gives up in a few simulated seconds
            // and the plan space still reaches the deadline on both sides of the fallback.
            stream: StreamOptions {
                max_reconnects: 2,
                ..StreamOptions::default()
            },
        };
        let sink: Option<OutputSink> = plan.streaming.then(|| {
            Box::new(|_| {
                Box::pin(std::future::ready(std::ops::ControlFlow::Continue(())))
                    as futures_util::future::BoxFuture<'static, _>
            }) as OutputSink
        });
        let result = session
            .run_to_completion(request, options, sink)
            .await
            .expect("BIND-8: every scripted plan returns a result");
        (result, daemon.log(), daemon.acked())
    })
}

#[test]
fn run_to_completion_plan() {
    bolero::check!().with_type::<Plan>().for_each(|plan| {
        let (result, log, acked) = drive(plan);
        let position = |suffix: &str| log.iter().position(|line| line.ends_with(suffix));
        let polls_or_acks: Vec<usize> = log
            .iter()
            .enumerate()
            .filter(|(_, line)| line.ends_with("/ack") || line.ends_with("/x-fuzz"))
            .map(|(index, _)| index)
            .collect();

        // BIND-10: synthesized exactly when no ack succeeded, and it says so.
        assert_eq!(
            result.synthesized(),
            !acked,
            "BIND-10: synthesized={} but the daemon recorded acked={acked}: {plan:?} {log:?}",
            result.synthesized()
        );
        if result.synthesized() {
            assert_eq!(result.posix_exit_code(), Some(124), "BIND-10: {plan:?}");
            assert!(
                result
                    .notes()
                    .iter()
                    .any(|note| note.contains("synthesized")),
                "BIND-10: {:?}",
                result.notes()
            );
            assert!(position("/kill").is_some(), "BIND-10: no kill: {log:?}");
        } else {
            // BIND-8: the one result is the acked one.
            assert_eq!(result.phase, Phase::Acked, "BIND-8: {plan:?} {result:?}");
            assert!(result.outcome.is_some(), "BIND-8: {plan:?} {result:?}");
        }

        // BIND-8: a cut stream is followed by a poll unless the deadline came first.
        if plan.streaming && plan.cut && result.client_deadline.is_none() {
            let stream = position("/stream").expect("a streaming plan attached");
            assert!(
                log[stream..].iter().any(|line| line.ends_with("/x-fuzz")),
                "BIND-8: a cut stream did not fall back to wait: {plan:?} {log:?}"
            );
        }

        // BIND-9: a kill only with the client deadline, and before the last poll or ack.
        match (&result.client_deadline, position("/kill")) {
            (None, None) => {}
            (Some(ClientDeadline { .. }), Some(kill)) => {
                assert!(
                    polls_or_acks.last().is_some_and(|last| *last > kill),
                    "BIND-9: nothing after the kill: {plan:?} {log:?}"
                );
                assert_eq!(
                    log.iter().filter(|line| line.ends_with("/kill")).count(),
                    1,
                    "BIND-9: one kill per deadline: {log:?}"
                );
            }
            (deadline, kill) => {
                panic!("BIND-9: deadline {deadline:?} and kill at {kill:?}: {plan:?} {log:?}")
            }
        }

        // BIND-6: the model's mapping.
        assert_eq!(
            result.posix_exit_code(),
            model_posix(&result),
            "BIND-6: {plan:?} {result:?}"
        );

        // BIND-7: one note per condition.
        let outcome = result.outcome.as_ref();
        let expected = usize::from(outcome.is_some_and(|o| o.truncated))
            + usize::from(outcome.is_some_and(|o| o.timed_out))
            + usize::from(outcome.is_some_and(|o| o.writers_may_be_alive))
            + usize::from(result.client_deadline.is_some());
        assert_eq!(
            result.notes().len(),
            expected,
            "BIND-7: {:?} for {result:?}",
            result.notes()
        );
    });
}
