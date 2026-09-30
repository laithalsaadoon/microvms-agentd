# SPDX-License-Identifier: Apache-2.0
"""Suspend and resume: the kept VM suspended and brought back whole, and a VM launched
`--auto-resume` resuming on an incoming request."""

from __future__ import annotations

import itertools
import secrets
import time
from typing import Any

from harness.cli import Cli, attach_args
from harness.constants import BASELINE_MEMORY_MIB, SERVICE, SUSPEND_WINDOW_SEC
from harness.envelope import Envelope, EnvelopeError, KindError
from harness.results import Results


def drive_suspend_resume(cli: Cli, launched: Envelope, results: Results) -> None:
    """Checks that a suspended sandbox comes back whole, driven entirely through the CLI.

    The evidence is the oracle's: a ticker writing epoch seconds once a second, and a gap in
    *its* timestamps is the suspension as the guest experienced it. The reads go through
    `microvm exec` — a shell redirect is a file write and `cat` is a file read — which is
    how this section worked before `microvm cp` existed, and it stays that way: the file
    surface has its own section now, and threading it through here would test it twice and
    the suspension no better.

    The ticker is started with a **stable id** so the pre-suspend exec record can be polled
    after the resume. That check was a documented substitute before `--poll` existed; it is
    now the assertion the oracle actually ran.
    """
    microvm_id = str(launched.data["microvmId"])
    attach = attach_args(cli, launched)

    print("\n== suspend / resume ==")
    # Detached, so the ticker's exec record is left **unacked** — which makes the
    # pre-suspend-record check below strictly stronger in two ways. An unacked entry has no
    # collection deadline at all (`agentd/src/exec.rs:214`: "an unacked entry has no deadline
    # and is never collected"), so its survival across the freeze is the daemon's registry
    # being intact rather than a 15-minute TTL not having elapsed; and its output is still
    # buffered, so the poll can assert the record came back *with* what it captured instead of
    # only that it answered.
    cli.call(
        "exec",
        "nohup sh -c 'i=0; while [ $i -lt 3000 ]; do date +%s >> /tmp/ticks.txt; "
        "i=$((i+1)); sleep 1; done' >/dev/null 2>&1 & echo started",
        "--exec-id",
        "ticker",
        "--detach",
        *attach,
    )
    cli.call("exec", "echo 'written before the suspend' > /tmp/survives.txt", *attach)
    # Workload hook handlers (#198), installed at runtime rather than baked into the
    # suite's image: the daemon looks for `<hooks dir>/<hook>` when the hook fires, and
    # an exec runs as root, so this is the same file an image would carry. Each handler
    # appends its hook name and the guest clock, so the file proves which ran and when.
    cli.call(
        "exec",
        "mkdir -p /etc/agentd/hooks.d && for hook in suspend resume; do "
        "printf '#!/bin/sh\\necho \"$AGENTD_HOOK $(date +%%s)\" >> /tmp/handlers.txt\\n' "
        "> /etc/agentd/hooks.d/$hook && chmod 0755 /etc/agentd/hooks.d/$hook; done",
        *attach,
    )
    time.sleep(5)

    print("  suspending")
    suspended = cli.call("suspend", microvm_id, "--region", cli.region)
    results.eq("suspend reached SUSPENDED", suspended.data.get("state"), "SUSPENDED")
    time.sleep(SUSPEND_WINDOW_SEC)

    print("  resuming")
    resumed = cli.call("resume", microvm_id, "--region", cli.region)
    results.eq("resume reached RUNNING", resumed.data.get("state"), "RUNNING")
    # The endpoint the service reported, which is measured not to change across a cycle.
    # Asserting it makes that measurement a fact this suite depends on rather than an
    # assumption either client encodes.
    results.eq(
        "the endpoint survived the cycle",
        resumed.data.get("endpoint"),
        launched.data.get("endpoint"),
    )

    # `resume` hands back the endpoint it read; re-attach through it rather than through
    # the launch's, so a changed endpoint is followed rather than silently failed on.
    after = attach_args(cli, launched, endpoint=str(resumed.data["endpoint"]))

    answered = None
    for _ in range(12):
        try:
            answered = cli.call("exec", "echo awake", *after)
            break
        except KindError as exc:
            print(f"    exec after resume: {exc!r}")
            time.sleep(5)
    results.check(
        "the daemon answers after a resume", answered is not None, repr(answered)
    )
    # The load-bearing one. An exec needs the installed agent token, so an exec that
    # works *is* the token having survived — if it had not, this is a 401 and every
    # consumer needs token re-delivery plumbing.
    results.check(
        "the agent token survived the suspend",
        answered is not None and answered.data.get("exitCode") == 0,
        "an authenticated exec succeeded after the resume",
    )

    survived = cli.call("exec", "cat /tmp/survives.txt", *after)
    results.eq(
        "the filesystem survived the suspend",
        (survived.data.get("stdout") or "").strip(),
        "written before the suspend",
    )

    dump = cli.call("exec", "cat /tmp/ticks.txt | tr '\\n' ' '", *after)
    stamps = [int(x) for x in (dump.data.get("stdout") or "").split() if x.isdigit()]
    gaps = [b - a for a, b in itertools.pairwise(stamps)]
    largest = max(gaps) if gaps else 0
    results.check(
        "the guest observed the suspension as a single gap in its own clock",
        largest >= 30,
        f"largest gap {largest}s across a ~{SUSPEND_WINDOW_SEC}s suspension",
    )

    # Differential liveness, the oracle's shape: two counts a few seconds apart rather than
    # a `pgrep` pattern threaded through two layers of shell quoting, where a false
    # negative is indistinguishable from a real one.
    first = cli.call("exec", "wc -l < /tmp/ticks.txt", *after)
    n1 = int((first.data.get("stdout") or "0").strip() or 0)
    time.sleep(6)
    second = cli.call("exec", "wc -l < /tmp/ticks.txt", *after)
    n2 = int((second.data.get("stdout") or "0").strip() or 0)
    results.check(
        "a backgrounded process resumed and kept running",
        n2 - n1 >= 3,
        f"ticks grew by {n2 - n1} over 6s after resume",
    )

    # The oracle's own check, restored. The ticker was started before the suspend with a
    # stable id, so polling it now asks the daemon for a record that existed on the other
    # side of a freeze — and a poll is read-only, so this costs the ticker nothing. Before
    # `--poll` existed this was a documented substitute (the ticker's *output* standing in
    # for its record); the substitute is still asserted above, and this is the real thing.
    #
    # The ticker was started `--detach`, so its record is unacked: the assertion is on the
    # *output it captured* coming back, not merely on the poll answering. An acked entry would
    # answer with an empty `stdout` and this would pass on nothing.
    survived_record = None
    try:
        survived_record = cli.call("exec", "--poll", "ticker", *after)
    except (KindError, EnvelopeError) as exc:
        results.check(
            "an exec record from before the suspend survived", False, repr(exc)
        )
    if survived_record is not None:
        results.check(
            "an exec record from before the suspend survived",
            "started" in (survived_record.data.get("stdout") or ""),
            f"phase={survived_record.data.get('phase')!r} "
            f"stdout={(survived_record.data.get('stdout') or '')[:40]!r}",
        )

    # The daemon's hook log across the cycle (issue #80). The suspend observation is
    # only ever readable after the thaw — a frozen VM answers nothing — so this is the
    # moment it becomes visible at all. Timestamps are bracketed by this section's own
    # wall clock: both hooks fired between the suspend call above and now.
    after_health = cli.call("health", *after)
    hooks = after_health.data.get("hooks") or []
    now = int(time.time())
    cycle_hooks = {
        name: [int(h.get("firedAt", 0)) for h in hooks if h.get("hook") == name]
        for name in ("suspend", "resume")
    }
    results.check(
        "health reports the suspend and resume hooks with plausible timestamps",
        bool(cycle_hooks["suspend"])
        and bool(cycle_hooks["resume"])
        and all(
            0 < stamp <= now + 60 for stamps in cycle_hooks.values() for stamp in stamps
        ),
        f"suspend={cycle_hooks['suspend']!r} resume={cycle_hooks['resume']!r}",
    )

    # The handlers' own evidence: the suspend handler ran before the freeze and the
    # resume handler after it, so the resume line's guest clock is at least the
    # suspension later than the suspend line's.
    ran = cli.call("exec", "cat /tmp/handlers.txt", *after)
    lines = [
        line.split() for line in (ran.data.get("stdout") or "").splitlines() if line
    ]
    stamps = {parts[0]: int(parts[1]) for parts in lines if len(parts) == 2}
    results.check(
        "the workload's suspend and resume handlers ran across the cycle",
        [parts[0] for parts in lines] == ["suspend", "resume"]
        and stamps["resume"] - stamps["suspend"] >= 20,
        f"handlers.txt={lines!r}",
    )
    outcomes = {
        h.get("hook"): h.get("handler") for h in hooks if h.get("hook") in stamps
    }
    results.check(
        "health reports each handler's outcome on its hook entry",
        all(
            (outcomes.get(name) or {}).get("succeeded") is True
            and (outcomes.get(name) or {}).get("exitCode") == 0
            for name in ("suspend", "resume")
        ),
        f"outcomes={outcomes!r}",
    )
    results.check(
        "a hook with no handler keeps the handler-free shape",
        all("handler" not in h for h in hooks if h.get("hook") == "run"),
        f"run entries={[h for h in hooks if h.get('hook') == 'run']!r}",
    )

    # And the local history, read the way a user would. The `resume` above polled
    # health and the `health` call just did too, each appending unseen observations
    # deduplicated on (hook, firedAt) — so the JSONL carries hookObserved events for
    # the cycle, and carrying each pair exactly once is the dedup working live.
    story = cli.call("history", microvm_id)
    observed = [
        (event.get("hook"), event.get("firedAt"))
        for event in (story.data.get("events") or [])
        if event.get("event") == "hookObserved"
    ]
    results.check(
        "the local history carries the cycle's hook observations exactly once each",
        {"suspend", "resume", "run"} <= {hook for hook, _ in observed}
        and len(observed) == len(set(observed)),
        f"hookObserved={observed!r}",
    )


def drive_auto_resume(cli: Cli, launched: Envelope, aws: Any, results: Results) -> None:
    """`run --auto-resume`: a suspended VM resumes itself on an incoming request (#68).

    **New in 0.6.0 and not yet run live** — written for the next live-conformance sweep.
    `autoResumeEnabled` was the field this client always sent `false` for. Its
    interaction with the idle timer after an auto-resume is still unmeasured: the
    2026-09-23 probe in `docs/PLATFORM.md` measured resume-on-request, not the timer after it.
    This section is that measurement as named checks, in two halves against one VM:

    1. Launch with `--auto-resume`, write a marker, suspend explicitly, and then send an
       exec **without ever calling `microvm resume`**. The exec completing is the platform
       having resumed the VM on the request itself; the marker read proves it resumed the
       same VM whole rather than answering from somewhere else.
    2. Then stop all traffic and let the idle window elapse, sampled through the control
       plane only (a health poll would reset the very timer being watched). The VM
       suspending *again* is the unmeasured interaction, answered: the idle timer runs
       after an auto-resume, so an auto-resumed VM left idle stops billing compute again
       rather than running to `maximumDurationInSeconds`.

    The billing record is the state transitions and their wall-clock brackets, printed as
    check detail: SUSPENDED (storage only) → the exec arrives → RUNNING (compute billing
    resumed, with the resume latency the caller paid measured as time-to-first-answer) →
    SUSPENDED again once idle. Like `drive_idle_keepalive` this is a slow section (~4
    minutes of deliberate waiting) on its own VM launched from the image the suite already
    built, torn down in this function's own `finally`.
    """
    print("\n-- auto-resume on an incoming request (#68) --")
    idle_window = 60  # the model's minimum, which is what makes half two affordable
    print(
        f"  slow check: suspend, one resume-by-exec, then a {idle_window}s idle window"
    )
    second = cli.call(
        "run",
        "--image",
        str(launched.data["imageIdentifier"]),
        "--name",
        f"microvm-cli-conformance-autoresume-{secrets.token_hex(4)}",
        "--memory",
        str(BASELINE_MEMORY_MIB),
        "--keep",
        "--auto-resume",
        "--region",
        cli.region,
        "--max-idle-sec",
        str(idle_window),
        "--suspended-sec",
        "600",
        "--max-duration-sec",
        "1800",
        timeout=15 * 60,
    )
    microvm_id = str(second.data["microvmId"])
    attach = attach_args(cli, second)
    plane = aws.client(SERVICE)

    try:
        # The marker: proof the exec after the thaw ran in the same VM, not a fresh one.
        cli.call("exec", "echo 'written before the suspend' > /tmp/marker.txt", *attach)

        print("  suspending")
        suspended = cli.call("suspend", microvm_id, "--region", cli.region)
        results.eq(
            "the auto-resume VM suspends explicitly",
            suspended.data.get("state"),
            "SUSPENDED",
        )
        suspended_at = time.monotonic()
        time.sleep(SUSPEND_WINDOW_SEC)

        # Half one: the exec IS the resume. No `microvm resume` anywhere in this section —
        # a retry loop because the thaw takes real seconds and the requests that land
        # during it may be refused rather than queued; either behavior is within contract.
        print("  sending an exec with no explicit resume")
        first_attempt = time.monotonic()
        answered = None
        for _ in range(12):
            try:
                answered = cli.call("exec", "cat /tmp/marker.txt", *attach)
                break
            except KindError as exc:
                print(f"    exec against the suspended VM: {exc!r}")
                time.sleep(5)
        answer_latency = time.monotonic() - first_attempt
        results.check(
            "an exec against a suspended auto-resume VM completes with no explicit resume",
            answered is not None and answered.data.get("exitCode") == 0,
            f"answered after {answer_latency:.0f}s against a VM suspended "
            f"{time.monotonic() - suspended_at:.0f}s ago",
        )
        results.check(
            "the auto-resumed VM is the same VM, filesystem intact",
            answered is not None
            and (answered.data.get("stdout") or "").strip()
            == "written before the suspend",
            repr(answered and answered.data.get("stdout")),
        )
        # The billing half of the record: the platform's own state, which is what meters.
        # RUNNING here is compute billing having resumed on the strength of one request.
        state = plane.get_microvm(microvmIdentifier=microvm_id)["state"]
        results.check(
            "the control plane reports RUNNING after the auto-resume (compute billing resumed)",
            state == "RUNNING",
            f"{state}, resume latency ~{answer_latency:.0f}s",
        )

        # Half two: the unmeasured interaction from docs/PLATFORM.md. Traffic stops, the
        # idle window elapses, and the question is whether the idle timer is live after an
        # auto-resume. Control-plane samples only — a health poll is inbound traffic and
        # would reset the timer under measurement.
        print(
            f"  traffic stopped; waiting for the {idle_window}s idle window to elapse"
        )
        resuspended_state = None
        wait_deadline = time.monotonic() + idle_window * 2.5
        while time.monotonic() < wait_deadline:
            time.sleep(20)
            resuspended_state = plane.get_microvm(microvmIdentifier=microvm_id)["state"]
            if resuspended_state != "RUNNING":
                break
        results.check(
            "the idle timer runs after an auto-resume: the VM suspends again unpolled",
            resuspended_state in {"SUSPENDING", "SUSPENDED"},
            f"{resuspended_state} after the window elapsed with no traffic — an "
            f"auto-resumed VM left idle stops billing compute again",
        )
    finally:
        # This section's own VM, this section's own teardown, whatever state it ended in.
        # `data.leaked` is read rather than only "no exception" for drive_idle_keepalive's
        # reason: terminate reports a failed delete as a named leak on a success envelope.
        try:
            torn = cli.call(
                "terminate", microvm_id, "--wait", "--region", cli.region, timeout=300.0
            )
        except Exception as exc:  # noqa: BLE001 - a teardown failure is a finding
            results.check(
                "the auto-resume VM was terminated", False, f"{microvm_id}: {exc!r}"
            )
        else:
            results.check(
                "the auto-resume VM was terminated",
                not torn.data.get("leaked"),
                f"{microvm_id} leaked={torn.data.get('leaked')!r}",
            )
