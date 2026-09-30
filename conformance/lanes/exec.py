# SPDX-License-Identifier: Apache-2.0
"""`microvm exec` against the suite's kept VM: the attach path, exec identity, the start
protocol, kill and process accounting, the output cap, streaming, stdin, and a reattach after
a token rotation."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from typing import Any

from harness.cli import Cli, attach_args
from harness.envelope import Envelope, KindError
from harness.image import (
    CONFORMANCE_HOME,
    CONFORMANCE_UID,
    CONFORMANCE_USER,
    IMAGE_ENV_KEY,
    IMAGE_ENV_VALUE,
)
from harness.redact import command_for_log
from harness.results import Results


def drive_exec(cli: Cli, launched: Envelope, results: Results) -> None:
    """`microvm exec` against the kept VM — the attach path, which `run` never exercises.

    Worth its own section: `exec` goes through `attach_session` rather than
    `open_sandbox`, so it is the only command that mints a proxy token for a VM this
    process did not launch. TRAP-9 lives on that path.
    """
    print("\n-- exec (the attach path) --")
    attach = attach_args(cli, launched)

    first = cli.call("exec", "echo attached", *attach)
    results.eq("exec exited 0 on the attach path", first.data.get("exitCode"), 0)
    results.check(
        "attached exec captured stdout",
        "attached" in (first.data.get("stdout") or ""),
        repr(first.data.get("stdout")),
    )

    # The oracle's three shell edge cases, same names. `shell: true` with a single script
    # string is what both clients send, so an unbalanced brace must stay one command.
    for name, script in (
        ("empty", ""),
        ("comment-only", "# nothing"),
        ("unbalanced brace", "echo A } echo B"),
    ):
        got = cli.call("exec", script, *attach)
        results.eq(f"{name} shell command exits 0", got.data.get("exitCode"), 0)
        if name == "unbalanced brace":
            results.check(
                "unbalanced brace did not escape into a second command",
                (got.data.get("stdout") or "").strip() == "A } echo B",
                repr(got.data.get("stdout")),
            )

    # A failing workload keeps its *success* envelope and earns ERR_EXEC_FAILED's code.
    # The distinction a CI caller needs: "your tests failed" is not "we never got a VM",
    # and one shared exit code cannot say both.
    proc = subprocess.run(
        cli.argv("exec", "exit 4", *attach), capture_output=True, text=True, check=False
    )
    envelope = Cli.parse_stdout(proc.stdout, cli.argv("exec", "exit 4"))
    results.check(
        "a failing workload reports success with a distinct exit code",
        envelope.status == "ok"
        and envelope.data.get("exitCode") == 4
        and proc.returncode == 13,
        f"status={envelope.status} exitCode={envelope.data.get('exitCode')} $?={proc.returncode}",
    )


def drive_exec_identity(cli: Cli, launched: Envelope, results: Results) -> None:
    """Exec identity: `--exec-id`, `--detach`, `--poll`, and `microvm ack`. Seven checks.

    The idempotency-key property is the interesting one and it needs a stable id to test at
    all — which is why the oracle could express it and the CLI could not until `--exec-id`
    landed. `MUST_NOT_RUN` in the retried command is the falsification: if the daemon
    spawned a second child the string would appear in the output, and the check is a
    substring search for its *absence*.

    **`--detach` is what makes the rest of this section possible**, and the first live round
    is what proved it. `microvm exec` without it is start-wait-**ack**: the ack releases the
    output, so a later explicit `microvm ack` correctly 409s (`already_acked`) and a poll
    correctly reports `acked` with nothing. Two checks here failed exactly that way. This
    section needs an exec whose lifecycle it owns — start, poll while the output is still
    buffered, ack once, watch the second ack refuse — which is the oracle's own
    start/poll/ack decomposition and is now `--detach`'s reason to exist.
    """
    print("\n-- exec identity (--exec-id, --detach, --poll, ack) --")
    attach = attach_args(cli, launched)

    # Detached: started and nothing else. Without `--detach` this invocation would ack its
    # own output and every check below would be reading an already-collected exec.
    started = cli.call(
        "exec", "echo identity-live", "--exec-id", "c1", "--detach", *attach
    )
    results.eq(
        "exec start accepted with a caller-supplied id",
        started.data.get("execId"),
        "c1",
    )
    results.eq(
        "a detached start reports running rather than a verdict",
        started.data.get("phase"),
        "running",
    )

    # The retry: the identical id, a *different* command. The daemon answers success for a
    # known id without spawning anything (`crates/agentd/src/exec.rs:366`, decided under the
    # registry lock), so this must succeed and must not run the new command. Detached again,
    # so the retry does not ack either.
    results.ok(
        "retried start accepted",
        lambda: cli.call(
            "exec", "echo MUST_NOT_RUN", "--exec-id", "c1", "--detach", *attach
        ),
    )

    # `echo` is quick but not instant, and a poll issued in the same breath as the start can
    # legitimately catch `running` with no output yet. Polled until it exits — which is also
    # a live demonstration that polling is repeatable, since that is the property it rests on.
    after = None
    for _ in range(12):
        after = cli.call("exec", "--poll", "c1", *attach)
        if after.data.get("phase") != "running":
            break
        time.sleep(1)
    assert after is not None
    results.check(
        "retried start did not spawn a second child",
        "MUST_NOT_RUN" not in (after.data.get("stdout") or ""),
        repr(after.data.get("stdout")),
    )

    # `--poll` is read-only, so the first exec's own output is still there — which is also
    # what makes the ack below meaningful rather than a no-op. This is the check that caught
    # the missing `--detach`: it read `''` because `exec` had already acked.
    results.check(
        "polling reads an exec without consuming it",
        "identity-live" in (after.data.get("stdout") or ""),
        repr((after.data.get("stdout") or "")[:80]),
    )

    results.ok("ack accepted", lambda: cli.call("ack", "c1", *attach))
    # The second ack is a 409 rather than a 200 with an empty body, because an empty body
    # would read as "the command produced no output" (`crates/agentd/src/exec.rs:854`).
    results.raises(
        "second ack refused with 409",
        "Conflict",
        lambda: cli.call("ack", "c1", *attach),
    )
    results.raises(
        "unknown exec id is 404",
        "NotFound",
        lambda: cli.call("exec", "--poll", "never-existed", *attach),
    )


def drive_exec_start_protocol(cli: Cli, launched: Envelope, results: Results) -> None:
    """Named users, groups and shells, and `--inherit-image-env` (#224, #225, #226). Sixteen checks.

    Against the suite's own VM, whose image (`conformance_dockerfile`) carries a
    `conformance` passwd row at uid 4242 with home `/home/conformance`, and one image `ENV`
    line, `MICROVMS_CONFORMANCE_IMAGE_ENV=from-image`. Every check name starts with its
    requirement key (AGENTD-7..16 in `verify/spec/agentd.symspec.json`).

    Live because the facts are the guest's: which rows the image's `/etc/passwd` holds, which
    shells the base image ships, and what environment the platform hands the daemon as the
    container `CMD`. The last is recorded by key name only in the AGENTD-13 detail, so a run's
    log says what the snapshot holds without printing a value.
    """
    print("\n-- exec start: named user, group, shell; the image env --")
    attach = attach_args(cli, launched)
    token = str(launched.data.get("agentToken") or "")

    def stdout(*args: str) -> str:
        return str(cli.call("exec", *args, *attach).data.get("stdout") or "")

    def refusal(*args: str) -> KindError | None:
        try:
            cli.call("exec", *args, *attach)
        except KindError as refused:
            return refused
        return None

    def marker_absent(path: str) -> bool:
        return (
            stdout(f"test -e {path} && echo present || echo absent").strip() == "absent"
        )

    results.eq(
        "AGENTD-7 exec --user by name runs as the passwd row's uid",
        stdout("id -u", "--user", CONFORMANCE_USER).strip(),
        str(CONFORMANCE_UID),
    )
    results.eq(
        "AGENTD-7 exec --group by name runs as the group row's gid",
        stdout("id -g", "--user", CONFORMANCE_USER, "--group", "root").strip(),
        "0",
    )

    marker = "/tmp/agentd-8-marker"
    refused = refusal(f"touch {marker}", "--user", "no-such-user-agentd8")
    results.check(
        "AGENTD-8 an unknown --user is refused unknown_user naming it",
        refused is not None
        and refused.kind == "ProtocolError"
        and "unknown_user" in refused.envelope.error
        and "no-such-user-agentd8" in refused.envelope.error,
        repr(refused.envelope.error if refused else "nothing raised"),
    )
    results.check(
        "AGENTD-8 the refused command never ran",
        marker_absent(marker),
        marker,
    )

    results.eq(
        "AGENTD-9 a named user gets HOME, USER and LOGNAME from its row",
        stdout('echo "$HOME $USER $LOGNAME"', "--user", CONFORMANCE_USER).strip(),
        f"{CONFORMANCE_HOME} {CONFORMANCE_USER} {CONFORMANCE_USER}",
    )
    results.eq(
        "AGENTD-9 --env HOME overrides the passwd HOME",
        stdout(
            'echo "$HOME $USER"', "--user", CONFORMANCE_USER, "--env", "HOME=/work"
        ).strip(),
        f"/work {CONFORMANCE_USER}",
    )

    results.eq(
        "AGENTD-10 without --inherit-image-env the image ENV does not reach the child",
        stdout(f'echo "[${IMAGE_ENV_KEY}]"').strip(),
        "[]",
    )
    results.eq(
        "AGENTD-11 --inherit-image-env puts the image ENV in the child",
        stdout(f'echo "[${IMAGE_ENV_KEY}]"', "--inherit-image-env").strip(),
        f"[{IMAGE_ENV_VALUE}]",
    )
    results.eq(
        "AGENTD-11 --env overrides the image ENV beneath it",
        stdout(
            f'echo "[${IMAGE_ENV_KEY}]"',
            "--inherit-image-env",
            "--env",
            f"{IMAGE_ENV_KEY}=override",
        ).strip(),
        "[override]",
    )

    inherited = stdout("env", "--inherit-image-env")
    keys = sorted(
        line.split("=", 1)[0] for line in inherited.splitlines() if "=" in line
    )
    results.check(
        "AGENTD-12 an inheriting child holds neither the token nor AGENTD_ configuration",
        bool(token) and token not in inherited and "AGENTD_" not in inherited,
        f"{len(keys)} keys: {', '.join(keys)}",
    )

    health = cli.call("health", *attach)
    count = health.data.get("imageEnvKeys")
    results.check(
        "AGENTD-13 health reports the image env key count and no value",
        isinstance(count, int)
        and count > 0
        and IMAGE_ENV_VALUE not in json.dumps(health.data),
        f"imageEnvKeys={count!r}",
    )

    pipefail = cli.call(
        "exec", "set -o pipefail; false | true", "--shell", "bash", *attach
    )
    results.eq(
        "AGENTD-14 --shell bash runs a pipefail pipeline",
        pipefail.data.get("exitCode"),
        1,
    )
    dollar0 = stdout("echo $0", "--shell", "bash").strip()
    results.check(
        "AGENTD-14 the named shell is the program that runs",
        dollar0.endswith("/bash"),
        repr(dollar0),
    )

    marker = "/tmp/agentd-15-marker"
    refused = refusal(f"touch {marker}", "--shell", "no-such-shell-agentd15")
    results.check(
        "AGENTD-15 an unknown --shell is refused unknown_shell naming it",
        refused is not None
        and refused.kind == "ProtocolError"
        and "unknown_shell" in refused.envelope.error
        and "no-such-shell-agentd15" in refused.envelope.error,
        repr(refused.envelope.error if refused else "nothing raised"),
    )
    results.check(
        "AGENTD-15 the refused command never ran",
        marker_absent(marker),
        marker,
    )

    results.eq(
        "AGENTD-16 a numeric --user still runs as that uid under /bin/sh",
        stdout("id -u; echo $0", "--user", str(CONFORMANCE_UID)).strip().splitlines(),
        [str(CONFORMANCE_UID), "/bin/sh"],
    )


def drive_kill_and_procs(cli: Cli, launched: Envelope, results: Results) -> None:
    """The stop button and process accounting (issues #156, #157). Sixteen checks.

    Four shapes, each a claim about processes the real daemon spawned in the real guest:

    (a) a live group — `sleep 300 & echo started; wait` — is listed by `ps` with the shell
        still running and at least two pids, `kill` signals it, the poll reports a non-zero
        exit or a signal, and the group reads empty within fifteen seconds;
    (b) the #157 shape: a backgrounded ticker whose exec exits 0 is listed with
        `childExited: true` and a live pid, its tick file grows across two reads four
        seconds apart, and a `kill` of *that exec id* — issued after the exec finished —
        freezes it, because the pgid was captured at spawn and the group is what is
        signalled;
    (c) the same shape under `exec --reap` leaves no live pid within `kill_grace` + 2 s and
        a tick file that stops on its own;
    (d) `exec --timeout 2` on `sleep 30` raises `ERR_TIMEOUT` whose suggestions name
        `microvm kill`, and `--kill-on-timeout` reports `data.killed: true` with the group
        empty afterwards.

    Every process fact is read through `microvm ps`, never a `ps` in the guest: the
    conformance image deliberately has none, which is the gap the route closes.
    """
    print("\n-- kill and procs (#156, #157) --")
    attach = attach_args(cli, launched)

    def group(exec_id: str) -> dict[str, Any] | None:
        listed = cli.call("ps", *attach)
        for entry in listed.data.get("procs") or []:
            if entry.get("execId") == exec_id:
                return entry
        return None

    def await_group(
        exec_id: str, ready: Callable[[dict[str, Any]], bool], deadline_sec: float
    ) -> dict[str, Any] | None:
        """Polls `ps` until `ready` holds for the entry, or the deadline passes."""
        deadline = time.monotonic() + deadline_sec
        last = None
        while time.monotonic() < deadline:
            last = group(exec_id)
            if last is not None and ready(last):
                return last
            time.sleep(1)
        return last

    def tick_count(path: str) -> int:
        read = cli.call("exec", f"wc -l < {path} 2>/dev/null || echo 0", *attach)
        raw = (read.data.get("stdout") or "0").strip()
        return int(raw) if raw.isdigit() else 0

    # (a) a live group, then a kill.
    cli.call(
        "exec",
        "sleep 300 & echo started; wait",
        "--exec-id",
        "kp-a",
        "--detach",
        *attach,
    )
    live = await_group("kp-a", lambda g: len(g.get("pids") or []) >= 2, 15)
    results.check(
        "ps lists a live group with its shell running and the sleep beside it",
        live is not None
        and live.get("childExited") is False
        and len(live.get("pids") or []) >= 2,
        repr(live),
    )
    killed = cli.call("kill", "kp-a", *attach)
    results.eq("kill reports the group was signalled", killed.data.get("killed"), True)
    after = None
    for _ in range(15):
        after = cli.call("exec", "--poll", "kp-a", *attach)
        if after.data.get("phase") != "running":
            break
        time.sleep(1)
    results.check(
        "the killed exec reports a non-zero exit or a signal death",
        after is not None
        and after.data.get("phase") != "running"
        and after.data.get("exitCode") != 0,
        f"phase={after.data.get('phase') if after else None} "
        f"exitCode={after.data.get('exitCode') if after else None}",
    )
    empty = await_group("kp-a", lambda g: not g.get("pids"), 15)
    results.check(
        "ps shows the killed group empty",
        empty is not None
        and empty.get("childExited") is True
        and not empty.get("pids"),
        repr(empty),
    )

    # (b) the #157 shape: a survivor the exec left behind.
    ticker = "(while true; do date +%s >> /tmp/night-tick; sleep 1; done &); sleep 1; echo bg"
    finished = cli.call("exec", ticker, "--exec-id", "kp-b", *attach)
    results.eq(
        "an exec that backgrounded a ticker exits 0", finished.data.get("exitCode"), 0
    )
    survivor = group("kp-b")
    results.check(
        "ps lists the finished exec with a live pid it left behind",
        survivor is not None
        and survivor.get("childExited") is True
        and len(survivor.get("pids") or []) >= 1,
        repr(survivor),
    )
    first = tick_count("/tmp/night-tick")
    time.sleep(4)
    second = tick_count("/tmp/night-tick")
    results.check(
        "the survivor keeps ticking after its exec finished",
        second > first,
        f"{first} then {second} lines, 4 s apart",
    )
    results.ok(
        "kill of the finished exec's id is accepted",
        lambda: cli.call("kill", "kp-b", *attach),
    )
    await_group("kp-b", lambda g: not g.get("pids"), 15)
    third = tick_count("/tmp/night-tick")
    time.sleep(4)
    fourth = tick_count("/tmp/night-tick")
    results.check(
        "the survivor is gone after the kill: the tick file stops",
        third == fourth,
        f"{third} then {fourth} lines, 4 s apart",
    )

    # (c) the same shape with --reap: the daemon signals the group when the shell exits.
    reaped_ticker = "(while true; do date +%s >> /tmp/night-tick-reap; sleep 1; done &); sleep 1; echo bg"
    reaped = cli.call("exec", reaped_ticker, "--exec-id", "kp-c", "--reap", *attach)
    results.eq("exec --reap exits 0", reaped.data.get("exitCode"), 0)
    # kill_grace is 10 s by default; the reap should be immediate because a sleeping shell
    # loop dies to SIGTERM, but the claim is bounded by the grace plus slack.
    gone = await_group("kp-c", lambda g: not g.get("pids"), 12)
    results.check(
        "ps shows no live pid for the reaped exec within kill_grace + 2 s",
        gone is not None
        and gone.get("childExited") is True
        and gone.get("reap") is True
        and not gone.get("pids"),
        repr(gone),
    )
    before = tick_count("/tmp/night-tick-reap")
    time.sleep(4)
    later = tick_count("/tmp/night-tick-reap")
    results.check(
        "the reaped ticker stopped on its own",
        before == later,
        f"{before} then {later} lines, 4 s apart",
    )

    # (d) a timeout is not a stop, and --kill-on-timeout makes it one.
    timed_out: KindError | None = None
    try:
        cli.call("exec", "sleep 30", "--timeout", "2", "--exec-id", "kp-d", *attach)
    except KindError as exc:
        timed_out = exc
    results.check(
        "exec --timeout raises ERR_TIMEOUT",
        timed_out is not None
        and timed_out.code == "ERR_TIMEOUT"
        and timed_out.kind == "ExecTimeout",
        repr(timed_out),
    )
    results.check(
        "the timeout's suggestions name microvm kill",
        timed_out is not None
        and any("microvm kill" in line for line in timed_out.envelope.suggestions),
        repr(timed_out.envelope.suggestions if timed_out else None),
    )
    # The abandoned exec is still running, as the suggestion says; stop it so it does not
    # sit in the group table for the rest of the run.
    cli.call("kill", "kp-d", *attach)

    stopped: KindError | None = None
    try:
        cli.call(
            "exec",
            "sleep 30",
            "--timeout",
            "2",
            "--kill-on-timeout",
            "--exec-id",
            "kp-e",
            *attach,
        )
    except KindError as exc:
        stopped = exc
    results.check(
        "exec --kill-on-timeout reports killed: true in the failure envelope",
        stopped is not None
        and stopped.code == "ERR_TIMEOUT"
        and stopped.envelope.data.get("killed") is True,
        repr(stopped.envelope.data if stopped else None),
    )
    emptied = await_group("kp-e", lambda g: not g.get("pids"), 15)
    results.check(
        "ps shows the timed-out-and-killed group empty",
        emptied is not None and not emptied.get("pids"),
        repr(emptied),
    )


def drive_output_cap(cli: Cli, launched: Envelope, results: Results) -> None:
    """The 8 MiB cap trio. 32 MiB of output against it.

    The daemon must truncate and **stay up**, not grow until the guest's OOM killer takes
    it. `health` after the fact is the survival probe and is the reason this trio needed
    `microvm health` to be expressible: an exec that answered would also prove the daemon
    lived, but only for a daemon that was still serving *that* route — health is the
    unauthenticated liveness question asked directly.
    """
    print("\n-- large output (the 8 MiB cap) --")
    attach = attach_args(cli, launched)
    noisy = cli.call(
        "exec",
        "dd if=/dev/zero bs=1M count=32 2>/dev/null | tr '\\0' 'x'",
        "--timeout",
        "180",
        *attach,
        timeout=300.0,
    )
    results.eq("noisy command still exits 0", noisy.data.get("exitCode"), 0)
    results.eq("output past the cap was truncated", noisy.data.get("truncated"), True)
    results.ok("daemon survived the truncation", lambda: cli.call("health", *attach))


def drive_streaming(cli: Cli, launched: Envelope, results: Results) -> None:
    """`exec --stream`: five checks, and the question is about AWS rather than the daemon.

    Streaming is the capability an agent harness needs and the one no local tier can fully
    validate: whether AWS's endpoint proxy actually **forwards** Server-Sent Events rather
    than buffering them until the command ends. Documentation says it does; this is the
    check, and it is the reason this section is worth its cost.

    `--exec-id` is what makes the last check possible: streaming must not consume the exec,
    so the same id is polled afterwards and its buffered output must still be there.
    """
    print("\n-- streaming --")
    attach = attach_args(cli, launched)

    events, envelope = cli.call_stream(
        "exec",
        "for i in 1 2 3 4 5; do echo chunk-$i; done; echo done-streaming",
        "--stream",
        "--exec-id",
        "stream1",
        *attach,
    )

    outputs = [event for event in events if event.get("event") == "output"]
    gaps = [event for event in events if event.get("event") == "gap"]
    exits = [event for event in events if event.get("event") == "exit"]
    streamed = "".join(str(event.get("text") or "") for event in outputs)

    results.check(
        "SSE reached us through the endpoint proxy",
        bool(outputs),
        f"{len(outputs)} chunk(s), {envelope.data.get('bytes')} bytes",
    )
    results.check(
        "streamed output is complete and ordered",
        "chunk-1" in streamed
        and "chunk-5" in streamed
        and "done-streaming" in streamed
        and streamed.index("chunk-1") < streamed.index("chunk-5"),
        repr(streamed[:160]),
    )
    results.check(
        "no gap was reported for a small stream", not gaps, f"{len(gaps)} gap(s)"
    )
    # The terminal event is why SSE was chosen over a raw byte stream: without it a client
    # cannot tell a finished command from a dropped connection. Asserted on the event
    # itself rather than only on the envelope's summary, because the summary is derived
    # from it and would agree with its own absence.
    results.check(
        "the terminal exit event carried the real exit code",
        bool(exits) and exits[-1].get("exitCode") == 0,
        repr(exits[-1] if exits else None),
    )

    # Streaming must not consume the exec: poll is a separate view onto the same
    # server-side object.
    polled = cli.call("exec", "--poll", "stream1", *attach)
    results.check(
        "the exec survived being streamed and is still pollable",
        "done-streaming" in (polled.data.get("stdout") or ""),
        repr((polled.data.get("stdout") or "")[:80]),
    )


def drive_stdin(cli: Cli, launched: Envelope, results: Results) -> None:
    """stdin: five checks. `cat` cannot exit until stdin closes, so this fails by hanging.

    That is the shape worth stating. If EOF never reaches the child, `cat` blocks until its
    timeout — which is exactly the trap where `Child::wait()` drops its own stdin handle but
    not the daemon's. So the `--timeout 30` is load-bearing: it turns a hang into a
    reported failure inside half a minute rather than at the suite's outer deadline.

    The refusal at the end is the opt-in property: a command that did not ask for stdin
    must not have one, or every task command inherits a surprise open descriptor. The daemon
    answers **409** for it (`crates/agentd/src/exec.rs:700`) — the request is well-formed and it is
    the exec that cannot accept it — which is a different fact from the 410 a write after
    EOF gets, and the kind is what says which.
    """
    print("\n-- stdin --")
    attach = attach_args(cli, launched)

    # `exec --stdin` feeds this process's stdin and closes it. Fed through the shell rather
    # than by writing to the child's stdin from Python, so the whole path — local read,
    # chunked write, EOF on the last chunk — is the one under test.
    proc = subprocess.run(
        cli.argv(
            "exec", "cat", "--stdin", "--exec-id", "cat1", "--timeout", "30", *attach
        ),
        input="hello via stdin\n",
        capture_output=True,
        text=True,
        check=False,
        timeout=120.0,
    )
    argv = cli.argv("exec", "cat", "--stdin")
    cli.log.append(
        command_for_log(cli.argv("exec", "cat", "--stdin", "--exec-id", "cat1"))
    )
    echoed = Cli.parse_stdout(proc.stdout, argv)

    results.check(
        "stdin write accepted",
        echoed.status == "ok",
        f"status={echoed.status} code={echoed.code!r}",
    )
    results.check(
        "stdin close accepted",
        echoed.status == "ok" and echoed.data.get("exitCode") is not None,
        f"exitCode={echoed.data.get('exitCode')!r}",
    )
    # The load-bearing pair. `cat` exiting at all *is* the EOF having arrived.
    results.eq(
        "a child reading stdin exits once stdin closes",
        echoed.data.get("exitCode"),
        0,
    )
    results.eq(
        "stdin round-tripped through the child",
        echoed.data.get("stdout"),
        "hello via stdin\n",
    )

    # Opt-in: an exec started without `--stdin` has /dev/null on its stdin.
    cli.call("exec", "true", "--exec-id", "nostdin", *attach)
    results.raises(
        "writing stdin to a command that did not request it is refused",
        "Conflict",
        lambda: cli.call("stdin", "nostdin", "--data", "x", *attach),
    )


def drive_token_rotation(cli: Cli, launched: Envelope, results: Results) -> None:
    """Reattach after a token rotation: gap 5 of `docs/HARNESS-CAPABILITIES.md`. Four checks.

    The contract under test is the one Harbor's hand-rolled daemon existed for: a detached
    exec must outlive the 60-minute proxy-token ceiling, because all exec state lives in the
    daemon keyed by `exec_id` and a re-minted token reattaches to it. Waiting a real hour to
    watch a token expire would cost more than every other section combined and would test
    AWS's clock, not this contract — so what is exercised is the *mechanism* the survival
    rests on: a fresh attach mints a fresh proxy token (`CoreSeam::attach_session` builds a
    new `PlaneMinter` per invocation, so every `microvm` process here is a new token), and
    the reattach carries **no client state at all** beyond the three identifiers a harness
    would have persisted. If the daemon's ack-before-TTL property held only for the process
    that started the exec, this is the section that would say so.

    The rotation is real, not simulated: each `Cli.call` is a separate process, so the
    start, the polls, and the ack below run under *different* proxy tokens by construction.
    What a 60-minute wait would add is only the proof that an **expired** token is refused,
    which is the platform's property (`crates/microvms-app/src/session/proxy.rs:63`), not the
    daemon's or this client's.

    The output produced *before* the reattach is the assertion that matters: bytes buffered
    under token A must be readable under token B, or a harness that rotates mid-run loses
    everything its workload said before minute sixty.
    """
    print("\n-- reattach after token rotation (gap 5) --")
    attach = attach_args(cli, launched)

    # Two echoes bracketing a sleep, detached: the first lands under the starting token,
    # the second lands while the polls below are already running under later ones. The
    # sleep is long enough that the start's own process has exited — and its token with
    # it, as far as any shared state goes — before the exec finishes.
    started = cli.call(
        "exec",
        "echo before-rotation; sleep 8; echo after-rotation",
        "--exec-id",
        "rot1",
        "--detach",
        *attach,
    )
    results.eq(
        "a detached exec accepted before the rotation",
        started.data.get("phase"),
        "running",
    )

    # The reattach: a new process, a new `attach_session`, a new proxy token, and nothing
    # carried over but the three identifiers. Polled to completion the same way the
    # identity section polls, because polling is the read a reattaching harness performs.
    final = None
    for _ in range(20):
        final = cli.call("exec", "--poll", "rot1", *attach)
        if final.data.get("phase") != "running":
            break
        time.sleep(1)
    assert final is not None
    rotated_stdout = final.data.get("stdout") or ""
    results.check(
        "a reattach from only the three identifiers reads the exec",
        final.data.get("phase") == "exited" and final.data.get("exitCode") == 0,
        f"phase={final.data.get('phase')!r} exitCode={final.data.get('exitCode')!r}",
    )
    # The load-bearing one. `before-rotation` was written under the starting token and
    # nothing acked it, so it must still be in the buffer the rotated attach reads. An
    # empty or truncated-at-the-front stdout here is output lost across a rotation, which
    # is exactly what the ack-before-TTL design exists to prevent.
    results.check(
        "no output produced before the reattach was lost",
        "before-rotation" in rotated_stdout and "after-rotation" in rotated_stdout,
        repr(rotated_stdout[:80]),
    )
    # And the exec is still one exec: the ack that releases it goes through yet another
    # fresh token, and it works exactly once — proving the rotated attaches were views
    # onto the daemon's one record rather than anything token-scoped.
    results.ok(
        "the rotated session acks the exec it did not start",
        lambda: cli.call("ack", "rot1", *attach),
    )
