# SPDX-License-Identifier: Apache-2.0
"""STATE-12: the service decides whether a suspended VM can resume."""

from __future__ import annotations

import secrets
import time
from typing import Any

from harness.cli import Cli
from harness.constants import BASELINE_MEMORY_MIB, SERVICE
from harness.envelope import Envelope, KindError
from harness.results import Results

WINDOW_SECONDS = 60


def check_ended_resume(
    observed: dict[str, Any], refusal: Envelope | None, results: Results
) -> None:
    """Hold the CLI's refusal to the state and reason independently read from AWS."""
    results.eq(
        "STATE-12 idle suspension ended in TERMINATED",
        observed.get("state"),
        "TERMINATED",
    )
    results.eq(
        "STATE-12 ended resume reports ERR_WINDOW_CLOSED",
        refusal.code if refusal else None,
        "ERR_WINDOW_CLOSED",
    )
    results.eq(
        "STATE-12 ended resume exits 8", refusal.exit_code if refusal else None, 8
    )
    message = refusal.error if refusal else ""
    results.check(
        "STATE-12 ended resume names the observed state",
        isinstance(observed.get("state"), str) and observed["state"] in message,
        f"observed state={observed.get('state')!r}",
    )
    reason = observed.get("stateReason")
    results.check(
        "STATE-12 ended resume names the observed reason",
        isinstance(reason, str) and bool(reason) and reason in message,
        f"observed reason={reason!r}",
    )


def drive_state12(cli: Cli, launched: Envelope, aws: Any, results: Results) -> None:
    """Compare idle and explicit suspensions with the same short suspended window.

    Control-plane reads do not send traffic through the VM endpoint. The explicit
    suspension is kept beyond its configured window while the idle suspension ends,
    then each is resumed through the public CLI. Both VMs belong to this section and
    are terminated in its finally block, including when an assertion or call fails.
    """
    print("\n== STATE-12 suspended window ==")
    plane = aws.client(SERVICE)
    ids: list[tuple[str, str]] = []
    try:
        for mode in ("explicit", "idle"):
            vm = cli.call(
                "run",
                "--image",
                str(launched.data["imageIdentifier"]),
                "--name",
                f"microvm-conformance-state12-{mode}-{secrets.token_hex(4)}",
                "--memory",
                str(BASELINE_MEMORY_MIB),
                "--keep",
                "--region",
                cli.region,
                "--max-idle-sec",
                str(WINDOW_SECONDS),
                "--suspended-sec",
                str(WINDOW_SECONDS),
                "--max-duration-sec",
                "3600",
                timeout=900,
            )
            ids.append((mode, str(vm.data["microvmId"])))
            if mode == "explicit":
                suspended = cli.call("suspend", ids[-1][1], "--region", cli.region)
                results.eq(
                    "STATE-12 explicit suspension reaches SUSPENDED",
                    suspended.data.get("state"),
                    "SUSPENDED",
                )
                explicit_at = time.monotonic()
        (_, explicit_id), (_, idle_id) = ids
        seen_suspended = False
        observed: dict[str, Any] = {}
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            observed = plane.get_microvm(microvmIdentifier=idle_id)
            seen_suspended |= observed.get("state") == "SUSPENDED"
            if observed.get("state") == "TERMINATED":
                break
            time.sleep(5)
        results.check(
            "STATE-12 idle policy reaches SUSPENDED before ending",
            seen_suspended,
            "control-plane observations only",
        )
        refusal = None
        try:
            cli.call("resume", idle_id, "--region", cli.region)
        except KindError as error:
            refusal = error.envelope
        check_ended_resume(observed, refusal, results)

        # Even an unusually fast idle transition must not shorten the explicit test.
        while time.monotonic() - explicit_at <= WINDOW_SECONDS:
            time.sleep(5)
        before = plane.get_microvm(microvmIdentifier=explicit_id)
        results.eq(
            "STATE-12 explicit suspension survives its window",
            before.get("state"),
            "SUSPENDED",
        )
        resumed = cli.call("resume", explicit_id, "--region", cli.region)
        results.eq(
            "STATE-12 explicit suspension resumes past its window",
            resumed.data.get("state"),
            "RUNNING",
        )
        results.eq(
            "STATE-12 resumed VM is RUNNING in GetMicrovm",
            plane.get_microvm(microvmIdentifier=explicit_id).get("state"),
            "RUNNING",
        )
    finally:
        for mode, vm_id in ids:
            try:
                torn = cli.call(
                    "terminate", vm_id, "--wait", "--region", cli.region, timeout=300
                )
                results.eq(
                    f"STATE-12 {mode} VM teardown reports no leaks",
                    torn.data.get("leaked"),
                    [],
                )
            except Exception as error:  # noqa: BLE001 - cleanup failure is a result
                results.check(
                    f"STATE-12 {mode} VM teardown completed", False, repr(error)
                )
            try:
                results.eq(
                    f"STATE-12 {mode} VM is independently TERMINATED",
                    plane.get_microvm(microvmIdentifier=vm_id).get("state"),
                    "TERMINATED",
                )
            except Exception as error:  # noqa: BLE001 - still try the other VM
                results.check(
                    f"STATE-12 {mode} independent teardown check completed",
                    False,
                    repr(error),
                )
