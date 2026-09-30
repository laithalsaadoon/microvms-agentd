# SPDX-License-Identifier: Apache-2.0
"""The idle timer: outside polling holding an idle VM awake, and `microvm keepalive`
holding a busy one, each on its own VM run to the edge of the model's minimum idle window."""

from __future__ import annotations

import secrets
import time
from typing import Any

from harness.cli import Cli, attach_args
from harness.constants import BASELINE_MEMORY_MIB, SERVICE
from harness.envelope import Envelope
from harness.results import Results


def drive_idle_keepalive(
    cli: Cli, launched: Envelope, aws: Any, results: Results
) -> None:
    """External polling resets the idle timer: gap 6's unmeasured tail. Four checks —
    three measurements plus this section's own teardown, which is a recorded row for the
    same reason `drive_teardown`'s log-group delete is: a cleanup that quietly failed
    would leave a billing VM behind a green run.

    `docs/PLATFORM.md` measured this once by hand ("An outside poll of `/v1/health` does
    reset the idle timer") with a polled VM and an unpolled control; this is that
    measurement as a named check, so it cannot silently stop being true. Both halves run
    against **one** VM, sequentially — survive-while-polled first, suspend-once-unpolled
    second — because the second half doubles as this section's own control: a platform that
    stopped suspending idle VMs at all would pass the first half vacuously, and the second
    is what would catch it.

    Its own VM rather than the suite's, launched from the image the suite already built
    (`run --image`, so no second 15-minute build): the suite's VM carries `--max-idle-sec
    600` because every other section needs it to stay up, and running *it* to the edge of a
    10-minute window would cost more wall time than this whole file. 60 is the model's
    minimum (`IdlePolicy.maxIdleDurationSeconds` declares `min: 60`).

    This is the slow section and says so: about four minutes of deliberate waiting — ~90s
    polled, then up to ~150s waiting for the unpolled suspend — plus one VM launch. The
    teardown is in this function's own `finally`, not the caller's, because the caller's
    `finally` only knows the suite's VM; a section that launches must be the section that
    terminates, however it exits.
    """
    print("\n-- idle-timer reset via external polling (gap 6) --")
    idle_window = 60  # the model's minimum, and the whole reason this is affordable
    print(
        f"  slow check: ~4 minutes of deliberate waiting against a {idle_window}s idle window"
    )
    second = cli.call(
        "run",
        "--image",
        str(launched.data["imageIdentifier"]),
        "--name",
        f"microvm-cli-conformance-idle-{secrets.token_hex(4)}",
        "--memory",
        str(BASELINE_MEMORY_MIB),
        "--keep",
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
    # The control plane's own state read, because "still RUNNING" is the platform's claim
    # to make: a health answer alone could not distinguish a live VM from one the poll
    # itself just auto-resumed.
    plane = aws.client(SERVICE)

    try:
        # Half one: no exec traffic for 1.5x the idle window, while `microvm health` polls
        # from outside every 15 seconds — well under the window, with three missed polls of
        # margin. Each poll is one small inbound request through the endpoint proxy, which
        # is the thing the platform meters.
        deadline = time.monotonic() + idle_window * 1.5
        polls = 0
        while time.monotonic() < deadline:
            cli.call("health", *attach)
            polls += 1
            time.sleep(15)
        state = plane.get_microvm(microvmIdentifier=microvm_id)["state"]
        results.check(
            "a VM polled from outside outlives its idle window",
            state == "RUNNING",
            f"{state} after {int(idle_window * 1.5)}s against a {idle_window}s window, "
            f"{polls} health polls",
        )
        # And the poll was informed, not blind: `busy` reads false on a VM running nothing,
        # which is the field an orchestrator branches on before deciding to keep paying.
        quiet = cli.call("health", *attach)
        results.eq(
            "an idle VM reports itself not busy to its keepalive",
            quiet.data.get("busy"),
            False,
        )

        # Half two, the control: stop polling and let the window elapse. This is the half
        # that proves the first was the polling — a VM that also survived *this* would mean
        # the platform had stopped metering and the check above passed against nothing.
        # Sampled through the control plane only, because a health poll here would reset
        # the very timer being watched.
        print(f"  polling stopped; waiting for the {idle_window}s window to elapse")
        suspended_state = None
        wait_deadline = time.monotonic() + idle_window * 2.5
        while time.monotonic() < wait_deadline:
            time.sleep(20)
            suspended_state = plane.get_microvm(microvmIdentifier=microvm_id)["state"]
            if suspended_state != "RUNNING":
                break
        results.check(
            "the same VM suspends once the polling stops",
            suspended_state in {"SUSPENDING", "SUSPENDED"},
            f"{suspended_state} after the window elapsed unpolled",
        )
    finally:
        # This section's own VM, this section's own teardown. Terminate works from RUNNING,
        # SUSPENDING, and SUSPENDED alike, so however the checks above ended the VM goes.
        # No `--delete-image`: the image is the suite's and the caller's teardown owns it.
        # `data.leaked` is read rather than only "no exception", because `terminate` reports
        # a failed delete as a named leak on a success envelope — that is its contract, and
        # a check that only caught the raise would call a leaked VM torn down.
        try:
            torn = cli.call(
                "terminate", microvm_id, "--wait", "--region", cli.region, timeout=300.0
            )
        except Exception as exc:  # noqa: BLE001 - a teardown failure is a finding
            results.check(
                "the idle-check VM was terminated", False, f"{microvm_id}: {exc!r}"
            )
        else:
            results.check(
                "the idle-check VM was terminated",
                not torn.data.get("leaked"),
                f"{microvm_id} leaked={torn.data.get('leaked')!r}",
            )


def drive_keepalive_helper(
    cli: Cli, launched: Envelope, aws: Any, results: Results
) -> None:
    """`microvm keepalive` holds a *busy* VM awake past its idle window (#199).

    `drive_idle_keepalive` proves that outside polling resets the idle timer on an idle VM.
    This is the case the helper exists for: a VM whose exec keeps a CPU busy with no client
    traffic, which the platform still counts as idle (measured 2026-09-23: suspended 60 to 70
    seconds after the last request under a 60-second window). Three measurements and the
    section's own teardown:

    1. `keepalive --for` holds the busy VM RUNNING for nearly three idle windows, polling at
       the interval it chose, and reports the window it read from `GetMicrovm` and that the
       VM was busy.
    2. The control: once the keepalive stops, the same VM, still busy, suspends. Without
       this half, a platform that stopped metering would pass the first half vacuously.

    About six minutes on its own VM, launched from the image the suite already built.
    """
    print("\n-- keepalive helper holds a busy VM awake (#199) --")
    idle_window = 60
    hold = 170
    print(f"  slow check: ~{hold + 150}s against a {idle_window}s idle window")
    second = cli.call(
        "run",
        "--image",
        str(launched.data["imageIdentifier"]),
        "--name",
        f"microvm-cli-conformance-keepalive-{secrets.token_hex(4)}",
        "--memory",
        str(BASELINE_MEMORY_MIB),
        "--keep",
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
        # Busy for longer than both halves together, so the control half is a busy VM too.
        cli.call(
            "exec",
            f"end=$(( $(date +%s) + {hold + 240} )); "
            'while [ "$(date +%s)" -lt "$end" ]; do :; done',
            "--exec-id",
            "keepalive-busy",
            "--detach",
            *attach,
        )
        held = cli.call("keepalive", "--for", str(hold), *attach, timeout=hold + 120.0)
        state = plane.get_microvm(microvmIdentifier=microvm_id)["state"]
        results.check(
            "microvm keepalive holds a busy VM awake past its idle window",
            state == "RUNNING" and held.data.get("end") == "elapsed",
            f"{state} after {hold}s against a {idle_window}s window; "
            f"end={held.data.get('end')!r} polls={held.data.get('polls')!r} "
            f"interval={held.data.get('intervalSec')!r}",
        )
        results.eq(
            "the keepalive read the VM's idle window from the control plane",
            held.data.get("idleWindowSec"),
            float(idle_window),
        )
        results.eq(
            "the keepalive saw the busy exec",
            held.data.get("lastBusy"),
            True,
        )

        print(f"  keepalive stopped; waiting for the {idle_window}s window to elapse")
        suspended_state = None
        wait_deadline = time.monotonic() + idle_window * 2.5
        while time.monotonic() < wait_deadline:
            time.sleep(20)
            suspended_state = plane.get_microvm(microvmIdentifier=microvm_id)["state"]
            if suspended_state != "RUNNING":
                break
        results.check(
            "the same busy VM suspends once the keepalive stops",
            suspended_state in {"SUSPENDING", "SUSPENDED"},
            f"{suspended_state} after the window elapsed with the exec still busy",
        )
    finally:
        try:
            torn = cli.call(
                "terminate", microvm_id, "--wait", "--region", cli.region, timeout=300.0
            )
        except Exception as exc:  # noqa: BLE001 - a teardown failure is a finding
            results.check(
                "the keepalive-check VM was terminated", False, f"{microvm_id}: {exc!r}"
            )
        else:
            results.check(
                "the keepalive-check VM was terminated",
                not torn.data.get("leaked"),
                f"{microvm_id} leaked={torn.data.get('leaked')!r}",
            )
