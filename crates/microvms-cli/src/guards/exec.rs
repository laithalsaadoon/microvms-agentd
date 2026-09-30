// SPDX-License-Identifier: Apache-2.0
//! `exec` and the commands that address a running exec (`stdin`, `ack`, `kill`, `ps`), against
//! the scripted daemon in `support`.

#![cfg(test)]

use std::sync::Arc;
use std::time::Duration;

use super::support::{
    DaemonScript, STARTED_BODY, against_daemon, attach_flags, exec_command, poll_body, region_flags,
};
use crate::cli::{AckArgs, Command, StdinArgs};
use crate::envelope::{Format, Output};
use crate::exit::Exit;

/// A running exec's poll: no outcome fields at all, which is what a flattened `None` looks like.
///
/// Not `"result": null` — there is no `result` key on the wire. The distinction matters here more
/// than anywhere: `--poll`'s whole contract is rendering this shape as a success with a null exit
/// code, and a body with a `result` wrapper would deserialize to the same `None` by accident and
/// prove nothing about the real one.
const RUNNING_BODY: &str = r#"{"exec_id": "x-1", "phase": "running"}"#;

/// One SSE `output` frame, base64 as the daemon writes it.
///
/// The base64 is **precomputed literal text** rather than encoded here, for the reason above: an
/// encoder call in the fake would produce whatever the decoder accepts. `Y2h1bmstMQo=` is
/// `chunk-1\n` and `Y2h1bmstMgo=` is `chunk-2\n`, checked by hand against the round trip below.
fn sse_output(offset: u64, encoded: &str) -> Vec<u8> {
    format!(
        "event: output\ndata: {{\"offset\":{offset},\"stream\":\"stdout\",\
         \"output\":\"{encoded}\"}}\n\n"
    )
    .into_bytes()
}

/// The terminal SSE frame.
fn sse_exit(code: i32, total: u64) -> Vec<u8> {
    format!(
        "event: exit\ndata: {{\"exit_code\":{code},\"signal\":null,\"truncated\":false,\
         \"writers_may_be_alive\":false,\"offset\":{total}}}\n\n"
    )
    .into_bytes()
}

/// **`exec --exec-id` sends the caller's key verbatim, and a retry sends the identical one.**
///
/// The property an idempotency key *is*. The daemon returns success for a known id without
/// spawning a second child (`crates/agentd/src/exec.rs:366`, decided under the registry lock), so a retry
/// is safe only if the key on the wire is byte-identical — a CLI that prefixed, suffixed, or
/// namespaced it would address a different exec on the retry and spawn exactly the duplicate the
/// key exists to prevent. Asserted on the recorded request body rather than on the return value,
/// because the return value is the same either way.
///
/// **Guard proof.** Change `spec.exec_id.unwrap_or_else(..)` in `lifecycle::start_request` to
/// `mint_exec_id()` — dropping the caller's key — and both bodies below carry
/// generated ids: the first assertion goes red on the key, and the second on the two being equal.
#[tokio::test]
async fn a_supplied_exec_id_reaches_the_wire_unchanged_on_every_retry() {
    let mut sent: Vec<String> = Vec::new();
    for _ in 0..2 {
        let script = DaemonScript::new();
        script
            .reply(200, STARTED_BODY)
            .reply(200, &poll_body("exited", "0", "", false))
            // `wait_and_ack`: the poll reported `exited`, so an ack follows and carries the output.
            .reply(200, &poll_body("acked", "0", "", false));

        let command = exec_command(|args| args.exec_id = Some("conformance-retry-1".into()));
        let (result, _, _) = against_daemon(&script, &command).await;
        result.expect("the exec succeeds");

        let start = script
            .requests()
            .into_iter()
            .find(|request| request.path == "/v1/exec/start")
            .expect("a start went out");
        let body: serde_json::Value =
            serde_json::from_slice(&start.body).expect("the start body is JSON");
        assert_eq!(
            body["exec_id"], "conformance-retry-1",
            "the caller's idempotency key must reach the wire undecorated, or the retry addresses \
             a different exec and spawns a second child: {body}"
        );
        sent.push(body["exec_id"].as_str().expect("a string").to_string());
    }
    assert_eq!(
        sent[0], sent[1],
        "two invocations with the same --exec-id must send the same key; that identity is the \
         whole of what an idempotency key buys"
    );
}

/// **A generated exec id differs per invocation**, which is the other half of the same decision.
///
/// Without this the test above would pass against a CLI that ignored `--exec-id` and happened to
/// generate a constant — and a constant generated id is far worse than a wrong one: every exec in
/// a process would be answered from the first one's record.
#[tokio::test]
async fn two_invocations_without_an_exec_id_send_different_keys() {
    let mut sent: Vec<String> = Vec::new();
    for _ in 0..2 {
        let script = DaemonScript::new();
        script
            .reply(200, STARTED_BODY)
            .reply(200, &poll_body("exited", "0", "", false))
            .reply(200, &poll_body("acked", "0", "", false));
        let (result, _, _) = against_daemon(&script, &exec_command(|_| {})).await;
        result.expect("the exec succeeds");
        let start = script
            .requests()
            .into_iter()
            .find(|request| request.path == "/v1/exec/start")
            .expect("a start went out");
        let body: serde_json::Value = serde_json::from_slice(&start.body).expect("JSON");
        sent.push(body["exec_id"].as_str().expect("a string").to_string());
    }
    assert_ne!(
        sent[0], sent[1],
        "a constant generated id makes every exec after the first read the first one's output"
    );
}

/// **`exec --env`, `--user`, and `--group` reach the start body verbatim, and their absence is
/// an absence.**
///
/// Asserted on the recorded request body, because that is the only place the claim lives: the
/// daemon `env_clear()`s and applies exactly this map (`crates/agentd/src/exec.rs:1003`), so a key
/// mangled between the flag and the wire is a variable the child silently does not have — the
/// PATH failure the coding-agents example documents, reintroduced through the fix. The second
/// invocation asserts the defaults stay defaults: `env` empty and `user`/`group` **null**, since
/// `Some(0)` where `None` belonged would ask the daemon to demote every exec to root.
///
/// **Guard proof.** Swap the tuple in `attached::exec`'s collection — `args.env.iter().map(|(k,
/// v)| (v.clone(), k.clone()))` — and the body carries `{"/usr/bin:/bin": "PATH"}`: the `env`
/// assertion goes red naming the missing key. Change `user: args.user` to `None` and the uid
/// assertion goes red. Both breaks were made on 2026-08-14, both failed exactly there, and both
/// were restored.
#[tokio::test]
async fn env_user_and_group_reach_the_wire_verbatim_and_default_to_absent() {
    let script = DaemonScript::new();
    script
        .reply(200, STARTED_BODY)
        .reply(200, &poll_body("exited", "0", "", false))
        .reply(200, &poll_body("acked", "0", "", false));

    let command = exec_command(|args| {
        args.env = vec![
            ("PATH".into(), "/usr/bin:/bin".into()),
            ("EMPTY".into(), String::new()),
        ];
        args.user = Some(1000.into());
        args.group = Some(2000.into());
    });
    let (result, _, _) = against_daemon(&script, &command).await;
    result.expect("the exec succeeds");

    let start = script
        .requests()
        .into_iter()
        .find(|request| request.path == "/v1/exec/start")
        .expect("a start went out");
    let body: serde_json::Value =
        serde_json::from_slice(&start.body).expect("the start body is JSON");
    assert_eq!(
        body["env"]["PATH"], "/usr/bin:/bin",
        "the key must stay the key and the value the value; a swap is a variable the child \
         silently lacks: {body}"
    );
    assert_eq!(
        body["env"]["EMPTY"], "",
        "an empty value is set-to-empty, not unset: {body}"
    );
    assert_eq!(body["user"], 1000, "{body}");
    assert_eq!(body["group"], 2000, "{body}");

    // And without the flags, the wire says nothing: an empty map and nulls. `Some(0)` here
    // would demote every exec to root, which is the opposite of a default.
    let script = DaemonScript::new();
    script
        .reply(200, STARTED_BODY)
        .reply(200, &poll_body("exited", "0", "", false))
        .reply(200, &poll_body("acked", "0", "", false));
    let (result, _, _) = against_daemon(&script, &exec_command(|_| {})).await;
    result.expect("the exec succeeds");
    let start = script
        .requests()
        .into_iter()
        .find(|request| request.path == "/v1/exec/start")
        .expect("a start went out");
    let body: serde_json::Value = serde_json::from_slice(&start.body).expect("JSON");
    assert_eq!(
        body["env"],
        serde_json::json!({}),
        "no --env means an empty environment on the wire: {body}"
    );
    assert_eq!(body["user"], serde_json::Value::Null, "{body}");
    assert_eq!(body["group"], serde_json::Value::Null, "{body}");
}

/// **`exec --detach` starts and stops: one POST, no wait, and above all no ack.**
///
/// The flag exists because every other `exec` shape ends in `wait_and_ack`, and that ack is the
/// irreversible step — it releases the output, a second one is a 409, and a poll afterwards reports
/// `acked` with nothing. A caller who wants to own an exec's lifecycle needs a start that stops
/// after starting, and the live round proved it: the conformance driver could not decompose
/// start/poll/ack without one, so `ack accepted` got a 409 from the exec `exec` had already acked.
///
/// The assertion is the **request list**, because that is the only place the difference shows. A
/// `--detach` that quietly waited would return the same envelope shape on a fast command.
///
/// **Guard proof.** Delete the `if args.detach` block from `attached::exec` so it falls through to
/// `wait_and_ack`, and this goes red on the request list: `GET /v1/exec/x-1` and
/// `POST /v1/exec/x-1/ack` appear where only the start belongs.
#[tokio::test]
async fn a_detached_exec_starts_without_waiting_and_without_acking() {
    let script = DaemonScript::new();
    script
        .reply(200, STARTED_BODY)
        // Two replies a correct `--detach` never asks for, queued on purpose. Without them a
        // `--detach` that fell through to `wait_and_ack` would die on "the script ran out of
        // replies" — a red that names the *fake* rather than the defect. With them it gets as far
        // as acking, and the break lands on the request-list assertion below, which says exactly
        // what went wrong: an ack happened that the caller did not ask for and cannot undo.
        .reply(200, &poll_body("exited", "0", "", false))
        .reply(200, &poll_body("acked", "0", "", false));

    let command = exec_command(|args| {
        args.detach = true;
        args.exec_id = Some("x-1".into());
    });
    let (result, _, _) = against_daemon(&script, &command).await;
    let rendered = result.expect("a detached start succeeds");

    assert_eq!(
        script.paths(),
        ["POST /v1/exec/start"],
        "a detached exec is exactly one request: no poll, and no ack — the ack releases the output \
         and cannot be undone, so a caller who asked not to wait must not have acked: {:?}",
        script.paths()
    );
    // Reported as running, which is what the daemon just said. Not `exited`: a fast command may
    // already be done, and claiming a phase this process did not observe would hand a caller an
    // `exited` envelope with no output — indistinguishable from a command that produced none.
    assert_eq!(rendered.data["phase"], "running");
    assert_eq!(rendered.data["exitCode"], serde_json::Value::Null);
    assert_eq!(
        rendered.data["execId"], "x-1",
        "the id is the only handle a later poll or ack has, so it has to be in the envelope"
    );
    assert_eq!(
        rendered.already_reported, None,
        "starting successfully is a success; the workload's verdict is not known yet"
    );
}

/// **`exec --poll` is read-only: it sends one GET and never an ack.**
///
/// Two claims, and the second is the one that would be silently wrong. A `--poll` implemented as
/// `wait_and_ack` would return the same envelope on the happy path *and* release the output, so the
/// next `microvm ack` would 409 and the caller's own later read would find nothing. The assertion
/// is therefore on the request list rather than on the result.
///
/// **Guard proof.** Change `poll_existing`'s `session.exec(exec_id).poll()` to `.wait_and_ack(..)`
/// and the `POST /v1/exec/x-1/ack` assertion goes red while the returned envelope stays identical.
#[tokio::test]
async fn polling_an_exec_reads_it_without_acking_it() {
    let script = DaemonScript::new();
    script.reply(200, RUNNING_BODY);

    let command = exec_command(|args| {
        args.command = None;
        args.poll = Some("x-1".into());
    });
    let (result, _, _) = against_daemon(&script, &command).await;
    let rendered = result.expect("polling a running exec is a success, not a failure");

    assert_eq!(
        script.paths(),
        ["GET /v1/exec/x-1"],
        "a poll must be exactly one read: {:?}",
        script.paths()
    );
    assert_eq!(rendered.data["phase"], "running");
    assert_eq!(
        rendered.data["exitCode"],
        serde_json::Value::Null,
        "a running exec has no exit code, and reporting 0 would make an unfinished command look \
         like a passing one"
    );
    assert_eq!(
        rendered.already_reported, None,
        "polling is read-only and repeating it costs nothing, so `not finished yet` is an answer \
         rather than a non-zero exit"
    );
}

/// **`exec --stream` writes one NDJSON record per event and the envelope LAST.**
///
/// The documented exception, asserted the way `conformance/run_rs.py` asserts it: every line of
/// stdout before the last parses as an event, and the last parses as the envelope. Three ways this
/// can be wrong and all three are covered — the envelope first (a caller reading line by line hits
/// the terminator before any output), the envelope pretty-printed (it becomes seven broken
/// records), and the events absent (the whole point of streaming).
///
/// **Guard proof.** Remove the `if self.streaming && self.format.is_json()` early return from
/// `Output::emit` and the last line becomes pretty-printed JSON: `lines.len()` reads 9 instead of
/// 4 and the final-line parse fails. Delete the `ctx.out.stream_line(..)` call in `stream_exec` and
/// the event-count assertion goes red with stdout holding only the envelope.
#[tokio::test]
async fn a_streamed_exec_writes_ndjson_events_then_the_envelope_as_the_final_line() {
    let script = DaemonScript::new();
    script.reply(200, STARTED_BODY).stream(
        200,
        vec![
            sse_output(0, "Y2h1bmstMQo="),
            sse_output(8, "Y2h1bmstMgo="),
            sse_exit(0, 16),
        ],
    );

    let command = exec_command(|args| args.stream = true);
    let (result, stdout, _) = against_daemon(&script, &command).await;
    let rendered = result.expect("the stream completes");

    // The envelope the dispatcher would write, appended here the way `main` does — this guard
    // exercises the handler, so the final write is staged rather than assumed.
    let lines: Vec<&str> = stdout.lines().collect();
    assert_eq!(
        lines.len(),
        3,
        "three events — two output frames and the exit — one line each: {stdout}"
    );

    // Every line is an event, in order, and the bytes are the child's.
    let events: Vec<serde_json::Value> = lines
        .iter()
        .map(|line| {
            serde_json::from_str(line)
                .unwrap_or_else(|error| panic!("an NDJSON line did not parse ({error}): {line:?}"))
        })
        .collect();
    assert_eq!(events[0]["event"], "output");
    assert_eq!(
        events[0]["text"], "chunk-1\n",
        "the base64 in the fake is literal text, so this also proves the decode: {}",
        events[0]
    );
    assert_eq!(events[0]["offset"], 0);
    assert_eq!(events[1]["text"], "chunk-2\n");
    assert_eq!(events[1]["offset"], 8);
    assert_eq!(events[2]["event"], "exit");
    assert_eq!(events[2]["exitCode"], 0);

    // The envelope's discriminant is the *streaming* one, so a consumer branching on `type` knows
    // which parse applied before it reads anything else.
    assert_eq!(rendered.kind, "microvm.exec.stream");
    assert_eq!(rendered.data["events"], 3);
    assert_eq!(rendered.data["bytes"], 16);
    assert_eq!(rendered.data["nextOffset"], 16);
    assert_eq!(rendered.data["gaps"], 0);
    assert_eq!(rendered.data["exitCode"], 0);
    assert_eq!(rendered.already_reported, None);
}

/// **The envelope really is the last line, written compact, through the real `Output`.**
///
/// Separate from the test above because that one asserts the *events* and this one asserts the
/// terminator — and the terminator is what `Output::emit`'s streaming branch is for. Written by
/// staging exactly what `main` does after a handler returns, so the pretty-versus-compact decision
/// under test is the shipped one.
#[test]
fn a_streams_envelope_is_one_compact_line_at_the_end_of_the_ndjson() {
    let mut out = Output::new(Format::Json, false, Vec::new(), Vec::new());
    out.stream_line(&serde_json::json!({"event": "output", "text": "a\n"}));
    out.stream_line(&serde_json::json!({"event": "exit", "exitCode": 0}));

    let mut data = serde_json::Map::new();
    data.insert("events".into(), serde_json::json!(2));
    out.emit(
        &crate::envelope::ok("microvm.exec.stream", data),
        "exit code: 0",
    );

    let stdout = String::from_utf8(out.into_streams().0).expect("utf8");
    let lines: Vec<&str> = stdout.lines().collect();
    assert_eq!(
        lines.len(),
        3,
        "two events plus one envelope line; a pretty-printed envelope would be nine: {stdout}"
    );
    for (index, line) in lines.iter().enumerate() {
        serde_json::from_str::<serde_json::Value>(line)
            .unwrap_or_else(|error| panic!("line {index} did not parse ({error}): {line:?}"));
    }
    let last: serde_json::Value =
        serde_json::from_str(lines[2]).expect("the last line is the envelope");
    assert_eq!(last["status"], "ok");
    assert_eq!(last["type"], "microvm.exec.stream");
    // And the two before it are not envelopes, so "the last one" is unambiguous.
    for line in &lines[..2] {
        let event: serde_json::Value = serde_json::from_str(line).expect("an event");
        assert!(
            event.get("status").is_none(),
            "an event must not look like an envelope: {line}"
        );
    }
}

/// **A stream that ends without an exit event reports failure rather than success.**
///
/// Core's own docs say the absence of the terminal event is the *only* thing distinguishing a cut
/// connection from a finished command. So a summary reporting `exitCode: 0` for a cut stream would
/// make a CI step pass on evidence it never received — and it is the plausible mistake, because
/// `Option::unwrap_or(0)` reads as a tidy default.
///
/// **Guard proof.** Change `data.insert("exitCode", json!(exit.and_then(..)))` to
/// `...unwrap_or(0)` and both the `exitCode` and the `already_reported` assertions go red.
///
/// `start_paused` because core's reconnect backoff tops out at four seconds and it makes twenty
/// attempts — a real clock spends about a minute here, which for one test in a hook-run suite is
/// the difference between a gate people run and one they skip. Tokio's auto-advance fires each
/// `sleep` the instant nothing else is runnable, so the *sequence* under test is unchanged.
#[tokio::test(start_paused = true)]
async fn a_cut_stream_reports_no_exit_code_and_earns_a_non_zero_exit() {
    let script = DaemonScript::new();
    script
        .reply(200, STARTED_BODY)
        // One output frame, then the body ends with no exit event. `reconnect` is on by default, so
        // core retries — the queue answers each attempt the same way until it is empty, which is
        // what a permanently cut stream looks like.
        .stream(200, vec![sse_output(0, "Y2h1bmstMQo=")]);
    for _ in 0..21 {
        script.stream(200, vec![]);
    }

    let command = exec_command(|args| args.stream = true);
    let (result, stdout, _) = against_daemon(&script, &command).await;

    // Core gives up after `max_reconnects` with a retryable error rather than a silent end, so this
    // surfaces as a failure — which is the honest outcome and is *also* fine for the property under
    // test: what must not happen is a success envelope claiming exit 0.
    match result {
        Err(failure) => {
            assert_eq!(
                failure.exit,
                Exit::Retryable,
                "a stream that kept dropping is retryable, not a passing command: {}",
                failure.message
            );
            // The one output frame it did deliver stays delivered: those bytes are real output the
            // caller received, and rewriting history would discard them.
            assert!(
                stdout.contains("chunk-1"),
                "events written before the cut must stay written: {stdout}"
            );
        }
        Ok(rendered) => {
            assert_eq!(
                rendered.data["exitCode"],
                serde_json::Value::Null,
                "a cut stream has no exit code; reporting 0 turns a truncated build into a green \
                 one: {:?}",
                rendered.data
            );
            assert_eq!(
                rendered.already_reported,
                Some(Exit::ExecFailed),
                "a stream with no terminal event must not exit 0"
            );
        }
    }
}

/// **`--from-offset` is the offset the stream request carries.**
///
/// The resume property, asserted on the request's query string. A `--from-offset` that parsed and
/// was then ignored would replay from zero: the caller would see every byte again, conclude the
/// resume worked, and have no way to notice — which is the failure mode E2B's cursorless
/// `connect(pid)` has and the reason core's cursor exists.
///
/// **Guard proof.** Change `stream_exec`'s `StreamOptions { offset, .. }` to
/// `StreamOptions::default()` and the offset assertion reads `offset=0`.
#[tokio::test]
async fn a_resume_offset_is_the_offset_the_stream_request_asks_for() {
    let script = DaemonScript::new();
    script.reply(200, STARTED_BODY).stream(
        200,
        vec![sse_output(4096, "Y2h1bmstMgo="), sse_exit(0, 4104)],
    );

    let command = exec_command(|args| {
        args.stream = true;
        args.from_offset = Some(4096);
    });
    let (result, _, _) = against_daemon(&script, &command).await;
    let rendered = result.expect("the resumed stream completes");

    let attach = script
        .requests()
        .into_iter()
        .find(|request| request.path.contains("/stream"))
        .expect("a stream attach went out");
    assert!(
        attach.path.contains("offset=4096"),
        "the resume offset must reach the wire, or the daemon replays from zero and the caller \
         cannot tell: {}",
        attach.path
    );
    // And the summary's `nextOffset` continues from there rather than from zero, so a second
    // resume is correct too.
    assert_eq!(rendered.data["nextOffset"], 4104);
}

/// **`microvm stdin` against an exec that never asked for it surfaces the daemon's 409 as
/// `Conflict`.**
///
/// The opt-in property. The daemon answers 409 `stdin_not_requested` because "the request is
/// well-formed, it is the exec that cannot accept it" (`crates/agentd/src/exec.rs:700`), and that check
/// runs *before* the pipe lookup that answers 410 — so 409 is the status a caller reaches by
/// forgetting `--stdin`, and 410 is the one they reach by writing after EOF. Both collapse onto
/// `ERR_PROTOCOL`; `data.kind` is what separates them, which is why the assertion is on the wire
/// kind and not on the code.
///
/// **Guard proof.** Add a `WireKind::StdinClosed => WireKind::Conflict` remap anywhere on this
/// path and the 410 half of this test goes red while the 409 half stays green — the two really are
/// distinguished rather than coincidentally equal.
#[tokio::test]
async fn writing_stdin_to_an_exec_that_did_not_request_it_is_a_conflict_and_not_a_gone() {
    // The refusal: 409, because the exec was started without `stdin: true`.
    let refused = DaemonScript::new();
    refused.reply(
        409,
        r#"{"error": "stdin_not_requested", "detail": "this exec was started without stdin: true"}"#,
    );
    let command = Command::Stdin(StdinArgs {
        exec_id: "x-1".into(),
        data: Some("hello".into()),
        eof: false,
        attach: attach_flags(),
        region: region_flags(),
    });
    let (result, _, _) = against_daemon(&refused, &command).await;
    let failure = result.expect_err("an exec without a stdin pipe refuses the write");
    assert_eq!(failure.exit, Exit::Protocol);
    assert_eq!(failure.code(), "ERR_PROTOCOL");
    assert_eq!(
        failure.wire_kind,
        Some(microvms_core::WireKind::Conflict),
        "the opt-in refusal is 409/Conflict: the request is well-formed and it is the exec that \
         cannot accept it"
    );
    let envelope = crate::envelope::error(&failure);
    assert_eq!(envelope["data"]["kind"], "Conflict");

    // The other 409-adjacent case, which must NOT be the same kind: the pipe is gone, because an
    // earlier EOF closed it or the child exited. A CLI that mapped both onto one kind would make
    // "you forgot --stdin" indistinguishable from "you wrote too late".
    let gone = DaemonScript::new();
    gone.reply(410, r#"{"error": "stdin_closed"}"#);
    let (result, _, _) = against_daemon(&gone, &command).await;
    let failure = result.expect_err("a closed pipe refuses the write");
    assert_eq!(
        failure.wire_kind,
        Some(microvms_core::WireKind::StdinClosed),
        "410 is a different fact from 409 and the envelope has to say which"
    );
    assert_eq!(failure.exit, Exit::Protocol, "both share the coarse code");
}

/// **`microvm stdin` with neither data nor EOF is refused locally.**
///
/// The daemon answers 200 to a zero-byte write with no signal, which is worse than a refusal: the
/// caller reads the success as delivery. Refused before the call, so `data.kind` is absent — and
/// that absence is itself information, saying the CLI declined rather than the daemon.
#[tokio::test]
async fn a_stdin_write_with_nothing_to_write_is_refused_before_the_call() {
    let script = DaemonScript::new();
    let command = Command::Stdin(StdinArgs {
        exec_id: "x-1".into(),
        data: None,
        eof: false,
        attach: attach_flags(),
        region: region_flags(),
    });
    let (result, _, _) = against_daemon(&script, &command).await;
    let failure = result.expect_err("a write of nothing is not a write");
    assert_eq!(failure.exit, Exit::InvalidArg);
    assert_eq!(
        failure.wire_kind, None,
        "nothing reached the daemon, and that absence is what says the CLI refused"
    );
    assert!(
        script.requests().is_empty(),
        "a request went out for a write with no content: {:?}",
        script.paths()
    );
}

/// **`exec --stdin` writes the bytes and closes in one request, and asks for the pipe at start.**
///
/// Both halves matter and each fails differently. Without `stdin: true` on the start the daemon
/// gives the child `/dev/null` and the write is a 409. Without the EOF the child never sees end of
/// input — `cat` hangs until its timeout, and the daemon's copy of the pipe outlives the child's
/// own `wait()`, so nothing else would ever close it.
///
/// The EOF rides the *same* request as the final chunk, which is core's contract and is why there
/// are two requests here rather than three.
#[tokio::test]
async fn feeding_stdin_asks_for_the_pipe_at_start_and_closes_it_with_the_last_write() {
    let script = DaemonScript::new();
    script
        .reply(200, STARTED_BODY)
        .reply(200, r#"{"exec_id": "x-1", "written": 5, "eof": true}"#)
        .reply(200, &poll_body("exited", "0", "hello", false))
        .reply(200, &poll_body("acked", "0", "hello", false));

    // A pipe rather than the runner's stdin would be better, and is not reachable from here: the
    // handler reads `std::io::stdin()` directly. Under `cargo test` that is an empty or closed
    // descriptor, which reads as zero bytes — enough to exercise the ordering and the flags, which
    // is what this test is for. The byte-level round trip is a live check
    // (`stdin round-tripped through the child`).
    let command = exec_command(|args| args.stdin = true);
    let (result, _, _) = against_daemon(&script, &command).await;
    result.expect("the exec completes");

    let start = script
        .requests()
        .into_iter()
        .find(|request| request.path == "/v1/exec/start")
        .expect("a start went out");
    let body: serde_json::Value = serde_json::from_slice(&start.body).expect("JSON");
    assert_eq!(
        body["stdin"], true,
        "--stdin has to ask for the pipe at start time; the daemon cannot add one later and \
         answers 409 to the write: {body}"
    );

    let write = script
        .requests()
        .into_iter()
        .find(|request| request.path.ends_with("/stdin"))
        .expect("a stdin write went out");
    let body: serde_json::Value = serde_json::from_slice(&write.body).expect("JSON");
    assert_eq!(
        body["signal"], "eof",
        "the EOF must ride the write: nothing else closes the pipe, and a child blocked reading \
         stdin hangs until its timeout: {body}"
    );
}

/// **A second `ack` is a 409, and the two 409s carry different detail.**
///
/// The double-ack check the oracle ran. `crates/agentd/src/exec.rs:854` states why it is not a 200 with an
/// empty body: that "would read as 'the command produced no output'". Both 409s here map to
/// `Conflict` — correctly, since a shell cannot act differently on them — and the *message* is what
/// distinguishes `already_acked` from `still_running`, which is the field a driver reads.
#[tokio::test]
async fn a_second_ack_conflicts_and_says_which_conflict_it_is() {
    let first = DaemonScript::new();
    first
        // The ack, carrying the released output.
        .reply(200, &poll_body("acked", "0", "output", false))
        // A second reply the correct implementation never asks for, queued on purpose. Without it
        // an `ack` that re-polled instead of returning the ack response would die on "the script
        // ran out of replies" — a real failure, but one that names the fake rather than the defect.
        // With it, the break lands on the `stdout` assertion below and says what is wrong: the
        // daemon released the output to the ack, and a poll after it reports `acked` with none, so
        // reading the wrong response is a silent empty-output bug.
        .reply(200, r#"{"exec_id": "x-1", "phase": "acked"}"#);
    let command = Command::Ack(AckArgs {
        exec_id: "x-1".into(),
        attach: attach_flags(),
        region: region_flags(),
    });
    let (result, _, _) = against_daemon(&first, &command).await;
    let rendered = result.expect("the first ack releases the output");
    assert_eq!(script_ack_path(&first), "POST /v1/exec/x-1/ack");
    assert_eq!(rendered.data["phase"], "acked");
    assert_eq!(
        rendered.data["stdout"], "output",
        "the ack response carries the released output; a poll after it reports none, so returning \
         the wrong one is a silent empty-output bug"
    );
    assert_eq!(
        rendered.already_reported, None,
        "an ack's own success is the release; the workload's code is in data.exitCode"
    );

    let second = DaemonScript::new();
    second.reply(
        409,
        r#"{"error": "already_acked", "detail": "output was released by an earlier ack"}"#,
    );
    let (result, _, _) = against_daemon(&second, &command).await;
    let failure = result.expect_err("the second ack is refused");
    assert_eq!(failure.wire_kind, Some(microvms_core::WireKind::Conflict));
    assert_eq!(failure.exit, Exit::Protocol);
    assert!(
        failure.message.contains("already_acked"),
        "the daemon's detail is what separates an already-acked 409 from a still-running one: {}",
        failure.message
    );

    // And the other 409 on the same route, so the two are not conflated: this one means the exec
    // has not exited and the output is still being written, which is a *wait* rather than a
    // handover that already happened.
    let running = DaemonScript::new();
    running.reply(
        409,
        r#"{"error": "still_running", "detail": "exec has not exited"}"#,
    );
    let (result, _, _) = against_daemon(&running, &command).await;
    let failure = result.expect_err("acking a running exec is refused");
    assert!(
        failure.message.contains("still_running"),
        "{}",
        failure.message
    );
}

/// **`microvm kill` POSTs the kill route and reports the daemon's own `killed` verdict.**
///
/// Issue #156: until this landed the only stop button on the CLI was `terminate`, and the
/// route's `killed: false` with a 200 — "the group had already exited" — is a fact the envelope
/// has to carry rather than flatten into success. Both verdicts are asserted, and both exit 0:
/// a kill whose target was already gone got what it asked for.
///
/// **Guard proof.** Route the handler through `session.exec(id).poll()` instead of `kill()` and
/// the path assertion is red; hard-code `killed: true` and the second half is.
#[tokio::test]
async fn kill_posts_the_kill_route_and_reports_the_daemons_verdict() {
    let script = DaemonScript::new();
    script.reply(200, r#"{"exec_id": "x-1", "killed": true}"#);
    let command = Command::Kill(crate::cli::KillArgs {
        exec_id: "x-1".into(),
        attach: attach_flags(),
        region: region_flags(),
    });
    let (result, _, _) = against_daemon(&script, &command).await;
    let rendered = result.expect("a kill that signalled");
    assert_eq!(script.paths(), ["POST /v1/exec/x-1/kill"]);
    assert_eq!(rendered.kind, "microvm.kill");
    assert_eq!(rendered.data["microvmId"], "mvm-1");
    assert_eq!(rendered.data["execId"], "x-1");
    assert_eq!(rendered.data["killed"], true);
    assert_eq!(rendered.already_reported, None);

    // The group was already gone: still a success, and the envelope says nothing was signalled.
    let gone = DaemonScript::new();
    gone.reply(200, r#"{"exec_id": "x-1", "killed": false}"#);
    let (result, _, _) = against_daemon(&gone, &command).await;
    let rendered = result.expect("killing an exited group is the outcome a kill wanted");
    assert_eq!(
        rendered.data["killed"], false,
        "the daemon's false must not be flattened into a success that reads as a signal"
    );
    assert_eq!(rendered.already_reported, None);

    // And an unknown id is the daemon's 404, arriving as ERR_PROTOCOL / NotFound like every
    // other exec route.
    let missing = DaemonScript::new();
    missing.reply(404, r#"{"error": "unknown_exec", "detail": "x-1"}"#);
    let (result, _, _) = against_daemon(&missing, &command).await;
    let failure = result.expect_err("an unknown exec is refused");
    assert_eq!(failure.exit, Exit::Protocol);
    assert_eq!(failure.wire_kind, Some(microvms_core::WireKind::NotFound));
}

/// **`microvm ps` GETs `/v1/procs` and renders one row per process group, camelCase.**
///
/// Issue #157's enumeration half. The row that matters is the second one: `childExited: true`
/// with a live pid is a command that finished while something it backgrounded did not, which
/// no other command can show. The dense rendering is one TSV line per group so a shell can
/// `cut` the exec id out and hand it to `kill`.
///
/// **Guard proof.** Read `/v1/health` instead and the path assertion is red; drop the
/// `childExited` key and the envelope assertion is.
#[tokio::test]
async fn ps_gets_the_procs_route_and_renders_one_row_per_group() {
    let script = DaemonScript::new();
    script.reply(
        200,
        r#"{"procs": [
            {"exec_id": "x-1", "pgid": 100, "started_at": 1756500000, "child_exited": false,
             "reap": false, "pids": [100, 101]},
            {"exec_id": "x-2", "pgid": 200, "started_at": 1756500010, "child_exited": true,
             "reap": false, "pids": [201]},
            {"exec_id": "x-3", "pgid": null, "started_at": 1756500020, "child_exited": true,
             "reap": true, "pids": []}
        ]}"#,
    );
    let command = Command::Ps(crate::cli::PsArgs {
        attach: attach_flags(),
        region: region_flags(),
    });
    let (result, _, _) = against_daemon(&script, &command).await;
    let rendered = result.expect("ps lists");
    assert_eq!(script.paths(), ["GET /v1/procs"]);
    assert_eq!(rendered.kind, "microvm.procs");
    assert_eq!(rendered.data["microvmId"], "mvm-1");
    let procs = rendered.data["procs"].as_array().expect("an array");
    assert_eq!(procs.len(), 3);
    assert_eq!(procs[0]["execId"], "x-1");
    assert_eq!(procs[0]["pgid"], 100);
    assert_eq!(procs[0]["startedAt"], 1_756_500_000u64);
    assert_eq!(procs[0]["childExited"], false);
    assert_eq!(procs[0]["pids"], serde_json::json!([100, 101]));
    assert_eq!(
        (
            procs[1]["childExited"].as_bool(),
            procs[1]["pids"].as_array().map(Vec::len)
        ),
        (Some(true), Some(1)),
        "the survivor shape — exited child, live pid — is the reason the command exists"
    );
    // `Value::index` on a missing key also yields `Null`, so the key's presence has to be
    // asserted through the map, or a renderer that drops the key would pass here.
    assert_eq!(
        procs[2].as_object().and_then(|group| group.get("pgid")),
        Some(&serde_json::Value::Null),
        "a missing pgid is null, never absent"
    );
    assert_eq!(procs[2]["reap"], true);
    // Dense: one TSV row per group, exec id first so `cut -f1` feeds `kill`.
    let dense: Vec<&str> = rendered.dense_text.lines().collect();
    assert_eq!(dense.len(), 3, "{:?}", rendered.dense_text);
    assert_eq!(
        dense[1].split('\t').collect::<Vec<_>>(),
        ["x-2", "200", "true", "1", "1756500010"]
    );
    // An empty account is an empty list and a success.
    let idle = DaemonScript::new();
    idle.reply(200, r#"{"procs": []}"#);
    let (result, _, _) = against_daemon(&idle, &command).await;
    let rendered = result.expect("an idle daemon lists nothing");
    assert_eq!(rendered.data["procs"], serde_json::json!([]));
}

/// **`exec --reap` puts `reap_group_on_exit: true` on the start body, and its absence puts
/// `false`.**
///
/// The daemon defaults a missing key to false, so a CLI that dropped the flag would still get a
/// 200 and an exec that quietly kept today's leave-it-running behaviour. Asserted on the recorded
/// body in both directions: the control case is what proves the default is really off.
///
/// **Guard proof.** Stop forwarding `spec.reap` in `start_request` and the first assertion is red.
#[tokio::test]
async fn exec_reap_puts_the_flag_on_the_start_body_and_its_absence_puts_false() {
    for reap in [true, false] {
        let script = DaemonScript::new();
        script
            .reply(200, STARTED_BODY)
            .reply(200, &poll_body("exited", "0", "", false))
            .reply(200, &poll_body("acked", "0", "", false));
        let command = exec_command(|args| args.reap = reap);
        let (result, _, _) = against_daemon(&script, &command).await;
        result.expect("the exec completes");
        let start = script
            .requests()
            .into_iter()
            .find(|request| request.path == "/v1/exec/start")
            .expect("a start went out");
        let body: serde_json::Value = serde_json::from_slice(&start.body).expect("JSON");
        assert_eq!(
            body["reap_group_on_exit"],
            serde_json::Value::Bool(reap),
            "--reap={reap} must reach the wire as the daemon's own key: {body}"
        );
    }
}

/// **`exec --kill-on-timeout` kills the exec after `ERR_TIMEOUT` and says whether it did; a
/// plain timeout kills nothing and names `microvm kill` as the remedy.**
///
/// Issue #156's measurement: `exec --timeout` abandoned an exec and a reader took it for a stop.
/// The plain timeout keeps its fact — the exec is untouched — and now names the command that
/// stops it. The flag turns the timeout into a stop, and the failure envelope carries the kill's
/// own verdict in `data.killed` rather than implying it from the exit code, which stays
/// `ERR_TIMEOUT` because the deadline is still what ended the wait.
///
/// The wait is a zero deadline against a daemon that answers `running`: one poll, then the
/// timeout, then — with the flag — exactly one kill.
///
/// **Guard proof.** Drop the `kill_on_timeout` arm and the kill-path assertion is red with the
/// script's kill reply unconsumed; drop `microvm kill` from the suggestion and the text one is.
#[tokio::test]
async fn exec_kill_on_timeout_kills_after_the_deadline_and_a_plain_timeout_names_the_remedy() {
    let script = DaemonScript::new();
    script
        .reply(200, STARTED_BODY)
        .reply(200, STARTED_BODY)
        .reply(200, r#"{"exec_id": "x-1", "killed": true}"#);
    let command = exec_command(|args| {
        args.timeout = Duration::ZERO;
        args.kill_on_timeout = true;
        args.exec_id = Some("x-1".into());
    });
    let (result, _, _) = against_daemon(&script, &command).await;
    let failure = result.expect_err("the deadline still ends the wait");
    assert_eq!(failure.exit, Exit::Timeout);
    assert_eq!(
        failure.data.get("killed"),
        Some(&serde_json::Value::Bool(true)),
        "the kill's verdict rides the failure envelope: {:?}",
        failure.data
    );
    assert_eq!(
        script.paths().last().map(String::as_str),
        Some("POST /v1/exec/x-1/kill"),
        "the timeout must be followed by exactly one kill: {:?}",
        script.paths()
    );

    // Without the flag: no kill goes out, `killed` is absent, and the suggestion names the stop.
    let plain = DaemonScript::new();
    plain.reply(200, STARTED_BODY).reply(200, STARTED_BODY);
    let command = exec_command(|args| {
        args.timeout = Duration::ZERO;
        args.exec_id = Some("x-1".into());
    });
    let (result, _, _) = against_daemon(&plain, &command).await;
    let failure = result.expect_err("a plain timeout");
    assert_eq!(failure.exit, Exit::Timeout);
    assert!(
        !plain.paths().iter().any(|path| path.contains("/kill")),
        "a plain timeout must not stop the exec: {:?}",
        plain.paths()
    );
    assert!(
        failure.data.get("killed").is_none(),
        "no kill was attempted, so no verdict is claimed: {:?}",
        failure.data
    );
    assert!(
        failure
            .suggestions
            .iter()
            .any(|line| line.contains("microvm kill")),
        "a timeout that does not name `microvm kill` reads as a stop: {:?}",
        failure.suggestions
    );
    assert!(
        failure
            .suggestions
            .iter()
            .any(|line| line.contains("untouched")),
        "the fact that the exec is untouched stays: {:?}",
        failure.suggestions
    );
}

/// The ack route one script saw. A helper so the assertion above reads as one line.
fn script_ack_path(script: &Arc<DaemonScript>) -> String {
    script
        .paths()
        .into_iter()
        .find(|path| path.contains("/ack"))
        .unwrap_or_default()
}
