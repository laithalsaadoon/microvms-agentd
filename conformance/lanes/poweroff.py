# SPDX-License-Identifier: Apache-2.0
"""AGENTD-25 through the public opt-in exec, on an independently cleaned image."""

from __future__ import annotations

import math
import re
import time
from pathlib import Path
from typing import Any

from botocore.config import Config
from harness.cli import Cli, attach_args
from harness.constants import SERVICE
from harness.image import conformance_dockerfile, resolve_base_ref
from harness.results import Results

from lanes.isolated_image import isolated_image, launch_image

REQUEST_SECONDS = 10
LEADER_SECONDS = 30
SHUTDOWN_SECONDS = 60
CHILD_FALLBACK_SECONDS = 180
RELEASE_PATH = "/tmp/microvm-poweroff-release"
BILLING_UNVERIFIED = "unverified: terminal state is not billing evidence"
STATES = {"PENDING", "RUNNING", "SUSPENDING", "SUSPENDED", "TERMINATING", "TERMINATED"}

# Redirect the child's handles so the leader's result can be read before the
# group ends. The marker gives the driver an observation window independent of
# machine speed. Its fallback is longer than all observation and shutdown bounds
# combined, so a missing release cannot masquerade as the expected shutdown.
WORKLOAD = (
    f"rm -f {RELEASE_PATH}; "
    f'(i=0; while [ ! -e {RELEASE_PATH} ] && [ "$i" -lt {CHILD_FALLBACK_SECONDS} ]; '
    "do sleep 1; i=$((i + 1)); done) </dev/null >/dev/null 2>&1 & "
    "printf 'poweroff-child:%s\\n' \"$!\""
)


def finite_seconds(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def check_poweroff(report: dict[str, Any], results: Results) -> None:
    """Fail closed on missing evidence, without treating termination as billing proof."""
    started = report.get("started")
    accepted = (
        isinstance(started, dict)
        and isinstance(started.get("execId"), str)
        and bool(started["execId"])
        and started.get("phase") == "running"
    )
    results.check("AGENTD-25 the opt-in detached exec is accepted", accepted)
    leader = report.get("leader")
    child = None
    if isinstance(leader, dict) and isinstance(leader.get("stdout"), str):
        matched = re.fullmatch(r"poweroff-child:([1-9][0-9]*)\n", leader["stdout"])
        if matched is not None:
            child = int(matched[1])
    exited = (
        accepted
        and isinstance(leader, dict)
        and leader.get("execId") == started["execId"]
        and leader.get("phase") == "exited"
        and type(leader.get("exitCode")) is int
        and leader["exitCode"] == 0
        and child is not None
    )
    results.check("AGENTD-25 the direct child exits before guest shutdown", exited)
    group = report.get("group")
    pids = group.get("pids") if isinstance(group, dict) else None
    held = (
        exited
        and isinstance(group, dict)
        and group.get("execId") == started["execId"]
        and group.get("childExited") is True
        and type(group.get("pgid")) is int
        and group["pgid"] > 0
        and isinstance(pids, list)
        and all(type(pid) is int and pid > 0 for pid in pids)
        and child in pids
        and report.get("heldState") == "RUNNING"
    )
    results.check(
        "AGENTD-25 the guest stays running while the same group has a child", held
    )
    observations = report.get("states")
    valid_states = (
        isinstance(observations, list)
        and bool(observations)
        and all(
            isinstance(row, dict)
            and isinstance(row.get("state"), str)
            and row.get("state") in STATES
            and finite_seconds(row.get("seconds"))
            for row in observations
        )
    )
    terminal = (
        held
        and report.get("before") == "RUNNING"
        and valid_states
        and observations[-1]["state"] == "TERMINATED"
        and [row["seconds"] for row in observations]
        == sorted(row["seconds"] for row in observations)
        and finite_seconds(report.get("shutdownSeconds"))
        and report["shutdownSeconds"] <= SHUTDOWN_SECONDS
        and finite_seconds(report.get("totalSeconds"))
        and report["totalSeconds"] < CHILD_FALLBACK_SECONDS
    )
    results.check(
        "AGENTD-25 the released group powers off the guest within the bound", terminal
    )
    results.eq(
        "AGENTD-25 terminal state does not claim a billing stop",
        report.get("billingStop"),
        BILLING_UNVERIFIED,
    )


def state_of(plane: Any, vm_id: str) -> str:
    state = plane.get_microvm(microvmIdentifier=vm_id).get("state")
    if not isinstance(state, str) or state not in STATES:
        raise ValueError("power-off observation has no recognized VM state")
    return state


def observe_poweroff(cli: Cli, launched: Any, plane: Any) -> dict[str, Any]:
    """Exercise the opt-in request; no diagnostic reboot or terminate is a success path."""
    vm_id = launched.data["microvmId"]
    attach = attach_args(cli, launched)
    report: dict[str, Any] = {"before": state_of(plane, vm_id)}
    experiment_start = time.monotonic()
    started = cli.call(
        "exec",
        WORKLOAD,
        "--detach",
        "--poweroff-on-exit",
        "--user",
        "0",
        *attach,
        timeout=REQUEST_SECONDS,
    )
    report["started"] = started.data
    exec_id = started.data.get("execId")
    if not isinstance(exec_id, str) or not exec_id:
        raise ValueError("power-off start has no exec id")
    deadline = time.monotonic() + LEADER_SECONDS
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("power-off direct child did not exit within the bound")
        leader = cli.call(
            "exec", "--poll", exec_id, *attach, timeout=min(REQUEST_SECONDS, remaining)
        )
        if leader.data.get("phase") != "running":
            report["leader"] = leader.data
            break
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    listed = cli.call("ps", *attach, timeout=REQUEST_SECONDS).data.get("procs")
    if not isinstance(listed, list):
        raise ValueError("power-off process observation has no group list")
    report["group"] = next(
        (
            group
            for group in listed
            if isinstance(group, dict) and group.get("execId") == exec_id
        ),
        None,
    )
    report["heldState"] = state_of(plane, vm_id)

    # Successful shutdown can interrupt this helper's response. Its response is
    # diagnostic only: the prior live-child evidence and bounded terminal read
    # below determine the verdict. Cleanup remains outside this observer.
    start = time.monotonic()
    try:
        cli.call(
            "exec",
            f"touch {RELEASE_PATH}",
            "--detach",
            "--user",
            "0",
            *attach,
            timeout=REQUEST_SECONDS,
        )
    except Exception as error:  # noqa: BLE001 - shutdown may race this response
        report["releaseErrorType"] = type(error).__name__
    observations = []
    while time.monotonic() - start < SHUTDOWN_SECONDS:
        state = state_of(plane, vm_id)
        elapsed = time.monotonic() - start
        observations.append({"state": state, "seconds": elapsed})
        if state == "TERMINATED":
            report.update(
                states=observations,
                shutdownSeconds=elapsed,
                totalSeconds=time.monotonic() - experiment_start,
                billingStop=BILLING_UNVERIFIED,
            )
            return report
        time.sleep(min(1, max(0, SHUTDOWN_SECONDS - elapsed)))
    raise TimeoutError("power-off guest did not terminate within the bound")


def drive_poweroff(cli: Cli, binary: Path, aws: Any, results: Results) -> None:
    """AGENTD-25 uses a repair image; ordinary images may lack CAP_SYS_BOOT."""
    plane = aws.client(
        SERVICE,
        config=Config(
            connect_timeout=5,
            read_timeout=REQUEST_SECONDS,
            retries={"total_max_attempts": 1},
        ),
    )
    with isolated_image(
        cli,
        binary,
        aws,
        results,
        "poweroff",
        conformance_dockerfile(resolve_base_ref()),
        repair=True,
    ) as (built, cleanup):
        launched = launch_image(cli, built, cleanup)
        report = observe_poweroff(cli, launched, plane)
        check_poweroff(report, results)
