# SPDX-License-Identifier: Apache-2.0
"""The suite's own VM: `run --keep` builds and launches it, and its build logs, its health,
and a second VM's machine identity are read against it."""

from __future__ import annotations

import secrets
import tempfile
import time
from pathlib import Path
from typing import Any

from harness.cli import Cli, attach_args
from harness.constants import BASELINE_MEMORY_MIB
from harness.envelope import Envelope
from harness.image import BAKED_WORKDIR
from harness.results import Results


def drive_lifecycle(
    cli: Cli,
    binary: Path,
    dockerfile: Path,
    results: Results,
    log_group: str,
    log_stream_prefix: str,
) -> Envelope:
    """`run --keep`, which is build plus launch plus exec in one invocation.

    `--keep` because every check after this one needs the VM, and the teardown is the
    caller's `finally` — the same shape the oracle used, for the same reason: a teardown
    that runs from inside the happy path is a teardown that does not run when it matters.

    The build carries the configured logging pair (issue #98) so `drive_build_logging`
    can assert the discriminator against real CloudWatch without a second 15-minute
    build. The group stays under `/aws/lambda-microvms/` because that is the only prefix
    the build role can write to (`conformance/infra/main.tf`, `WriteBuildLogs`) — a group
    outside it builds with no logs at all, which would make the stream checks assert
    against an IAM decision rather than against the client's suffixing.
    """
    print("\n== run (build + launch + exec) ==")
    launched = cli.call(
        "run",
        str(binary),
        "--name",
        f"microvm-cli-conformance-{secrets.token_hex(4)}",
        "--dockerfile",
        str(dockerfile),
        "--memory",
        str(BASELINE_MEMORY_MIB),
        "--repair-identity",
        "--log-group",
        log_group,
        "--log-stream",
        log_stream_prefix,
        "--keep",
        "--region",
        cli.region,
        "--exec",
        "echo live; pwd; id -u",
        "--max-idle-sec",
        "600",
        "--suspended-sec",
        "600",
        "--max-duration-sec",
        "3600",
        timeout=50 * 60,
    )

    results.eq("run emitted its namespaced envelope type", launched.type, "microvm.run")
    # `health reachable through the endpoint` and `platform ran the run hook before
    # forwarding traffic` were asserted here, weakly: a launch that returned at all implied
    # `wait_until_ready` had seen a bootstrapped daemon, which was the strongest reading
    # available without a `microvm health`. Both are now asserted directly in
    # `drive_health`, against the health envelope's own `version` and `bootstrapped`, which
    # is the oracle's original form. Duplicating them here would print each name twice and
    # make the report's own totals a lie — so the launch keeps only what only it can say.
    results.check(
        "the launch reported an endpoint to attach to",
        bool(launched.data.get("endpoint")),
        f"endpoint {launched.data.get('endpoint')!r}",
    )
    results.eq("exec exited 0", launched.data.get("execExitCode"), 0)
    stdout = launched.data.get("stdout") or ""
    results.check("exec start accepted", bool(stdout), repr(stdout[:80]))
    results.check("exec captured stdout", "live" in stdout, repr(stdout[:80]))
    results.check(
        "omitted cwd inherits the image WORKDIR",
        BAKED_WORKDIR in stdout,
        repr(stdout[:120]),
    )
    # `--keep` must say what the caller has taken responsibility for. A kept VM whose
    # identifiers were not reported is a bill with no id attached to it.
    results.check(
        "keep reported the identifiers the caller now owns",
        bool(launched.data.get("microvmId"))
        and bool(launched.data.get("imageIdentifier")),
        f"microvm={launched.data.get('microvmId')} image={launched.data.get('imageIdentifier')}",
    )
    # And the token, which is the one credential a kept VM's caller needs: without it the
    # VM is a bill nobody can exec into. Length only in the detail — the token itself is
    # never printed by this suite.
    token = launched.data.get("agentToken")
    results.check(
        "run --keep carries the agent token the kept VM needs (issue #161)",
        isinstance(token, str) and len(token) > 0,
        f"agentToken present, length {len(token) if isinstance(token, str) else 0}",
    )
    # Cost travels on the envelope whichever way the run ended, and every dollar is
    # labelled an estimate. A `$0.00` where a rate is unpublished is the failure COST-3
    # exists to prevent, so its absence is worth asserting on a real report.
    results.check(
        "the run envelope carries a labelled cost report",
        isinstance(launched.data.get("cost"), dict),
        f"{type(launched.data.get('cost')).__name__}",
    )
    check_run_cost(launched.data.get("cost") or {}, results)
    return launched


def check_run_cost(report: dict[str, Any], results: Results) -> None:
    """COST-1, COST-3 and COST-4 on a real run's cost report (`run`'s envelope `cost`).

    A function of the report alone, so the self-test holds each check to failing on a report
    that breaks its rule.
    """
    items = report.get("items") or []
    # A run's durations were timed, so each is `measured` and none is unlabelled. The set is
    # empty (and the check fails) when the report carries no duration at all.
    results.eq(
        "COST-1 every duration on the run's cost report is labelled measured",
        sorted(
            {
                str((item.get("duration") or {}).get("provenance"))
                for item in items
                if item.get("duration")
            }
        )
        or None,
        ["measured"],
    )
    # This run built its image, and AWS publishes no rate for the build: the line is unpriced,
    # with no dollar figure, rather than `$0.00`.
    builds = [
        item.get("amount") or {} for item in items if item.get("phase") == "image-build"
    ]
    results.eq(
        "COST-3 the run's image build is unpriced, not zero dollars",
        [(amount.get("kind"), "usd" in amount) for amount in builds] or None,
        [("unpriced", False)],
    )
    total = report.get("total") or {}
    results.eq(
        "COST-4 the run's total is a lower bound that names its unpriced line",
        (total.get("isLowerBound"), "image-build" in str(total.get("render"))),
        (True, True),
    )


def drive_build_logging(
    logs: Any,
    log_group: str,
    log_stream_prefix: str,
    results: Results,
) -> None:
    """The configured build-logging pair, asserted against real CloudWatch (issue #98).

    The suite's own build (in `drive_lifecycle`) configured `--log-group` and
    `--log-stream`, so by the time this runs the build has finished and its streams
    exist. What only a live read can say:

    - the client's resolved stream really exists in the configured group — the wire
      carried `<prefix>/<16 hex>`, not the caller's verbatim value, and the service
      accepted it;
    - the stream is non-empty — the build's three VMs actually wrote through the
      configured destination rather than falling back to the service default;
    - the name carries the user prefix, a `/`, and a 16-hex suffix — the discriminator
      contract, measured rather than assumed.

    Read through boto3 rather than the CLI for `read_daemon_logs`'s reason: `microvm
    logs` refuses CloudWatch by design (CLI-2), and this check is about what the
    *service* recorded. The suite's own credentials do the describe; the build role only
    ever needed to write.
    """
    print("\n-- build logging (the configured group and the resolved stream) --")
    try:
        streams = logs.describe_log_streams(
            logGroupName=log_group,
            logStreamNamePrefix=f"{log_stream_prefix}/",
        ).get("logStreams", [])
    except Exception as exc:  # noqa: BLE001 - an unreadable group is the finding
        results.check(
            "the configured build log group exists and is readable",
            False,
            f"{log_group}: {type(exc).__name__}: {exc}",
        )
        return
    results.check(
        "the configured build log group exists and is readable",
        True,
        log_group,
    )

    names = [str(stream["logStreamName"]) for stream in streams]
    suffixes = [name[len(log_stream_prefix) + 1 :] for name in names]
    results.check(
        "the resolved stream exists and carries the prefix, a slash, and 16 hex",
        bool(names)
        and all(
            len(suffix) == 16 and all(c in "0123456789abcdef" for c in suffix)
            for suffix in suffixes
        ),
        f"{names!r} — the client must suffix, never send the configured name verbatim",
    )
    results.check(
        "no stream carries the configured name verbatim",
        log_stream_prefix not in names,
        f"{names!r} — a verbatim name would collapse every build's streams into one",
    )

    # Non-empty: the three build VMs wrote through the configured destination. One
    # configured exact name means all three collapse into this stream, which is exactly
    # the behaviour the per-build discriminator exists to scope to a single build.
    events = 0
    for name in names:
        got = logs.get_log_events(
            logGroupName=log_group,
            logStreamName=name,
            limit=10,
            startFromHead=True,
        )
        events += len(got.get("events", []))
    results.check(
        "the resolved stream is non-empty",
        events > 0,
        f"{events} event(s) across {len(names)} stream(s)",
    )


def drive_health(cli: Cli, launched: Envelope, results: Results) -> None:
    """`microvm health` — seven checks, and the identity pair is the one with a measurement.

    `identity_degraded` is the only guard whose unit tests inject a fake layout, so this is
    the one place the real bind mount over real procfs is exercised. Measured 2026-08-06:
    without `additionalOsCapabilities: ["ALL"]` the hostname and boot_id steps fail with
    EPERM even though the daemon is root, and `identityDegraded` is how that surfaces.
    Asserting it here is what makes the capability requirement impossible to drop by
    accident — the launch above passes `--repair-identity`, and if core stopped injecting
    `["ALL"]` this check would be the thing that noticed.

    The hook checks (issue #80) split guarantee from hypothesis. A launched VM's daemon
    served the platform's run hook by definition — no traffic is forwarded before it
    answers 200 — so the run observation is asserted hard. Validate/ready fire in the
    *snapshot* VM, and their surviving into a launched VM rests on launch restoring the
    snapshot's memory: designed for, expected, and informational here rather than
    load-bearing until a live run confirms it. Its absence is printed as a finding, never
    a failure — a red suite over a hypothesis is a suite people stop reading.
    """
    print("\n-- health --")
    attach = attach_args(cli, launched)
    health = cli.call("health", *attach)

    results.check(
        "TRAP-7 health reachable through the endpoint",
        bool(health.data.get("version")),
        f"daemon version {health.data.get('version')!r}",
    )
    results.eq(
        "AGENTD-2 platform ran the run hook before forwarding traffic",
        health.data.get("bootstrapped"),
        True,
    )
    results.eq(
        "identity repair completed every step",
        health.data.get("identityDegraded"),
        False,
    )
    results.eq(
        "TRAP-3 identity repair actually ran", health.data.get("identityRepaired"), True
    )

    # The daemon's own record of the run hook, with a timestamp the wall clock brackets:
    # after this suite started (minus generous build time is unnecessary — the hook fired
    # during this run's launch) and not in the future. `bootstrapped: true` above says the
    # hook succeeded; this says the *observation log* recorded it, which is the surface
    # issue #80 added and the thing that would break independently.
    hooks = health.data.get("hooks") or []
    now = int(time.time())
    run_hooks = [h for h in hooks if h.get("hook") == "run"]
    results.check(
        "health reports the run hook the platform fired",
        bool(run_hooks)
        and all(0 < int(h.get("firedAt", 0)) <= now + 60 for h in run_hooks),
        f"hooks={hooks!r}",
    )
    # The snapshot-survival hypothesis, reported rather than asserted: validate and ready
    # fire in the snapshot VM, and a launched VM restores that VM's memory, so its hook
    # log should carry them. PASS either way — presence confirms the design, absence is a
    # finding worth reading in the report, not a defect in this client.
    build_hooks = sorted({h.get("hook") for h in hooks} & {"ready", "validate"})
    results.check(
        "build-time hook observations restored from the snapshot (informational)",
        True,
        f"present: {build_hooks!r}"
        if build_hooks
        else "ABSENT — the snapshot did not carry them; a finding, not a failure",
    )


def drive_identity_per_vm(cli: Cli, launched: Envelope, results: Results) -> None:
    """Two VMs from one image get distinct machine-ids (#205).

    The suite's VM and a second one launched from the same image. Before the fix both
    carried the machine-id the daemon wrote at startup in the image-build VM, which the
    snapshot captured (measured 2026-09-23). Now repair runs at each VM's run hook.
    """
    print("\n-- identity per VM (two VMs, one image) --")
    second = cli.call(
        "run",
        "--image",
        str(launched.data["imageIdentifier"]),
        "--name",
        f"microvm-cli-conformance-identity-{secrets.token_hex(4)}",
        "--memory",
        str(BASELINE_MEMORY_MIB),
        "--keep",
        "--region",
        cli.region,
        "--max-idle-sec",
        "600",
        "--suspended-sec",
        "600",
        "--max-duration-sec",
        "1800",
    )
    second_id = str(second.data["microvmId"])
    try:
        ids = [
            (
                cli.call("exec", "cat /etc/machine-id", *attach_args(cli, vm)).data.get(
                    "stdout"
                )
                or ""
            ).strip()
            for vm in (launched, second)
        ]
        results.check(
            "two VMs from one image have distinct machine-ids",
            all(len(value) == 32 for value in ids) and ids[0] != ids[1],
            f"lengths={[len(value) for value in ids]} equal={ids[0] == ids[1]}",
        )
        steps = [
            cli.call("health", *attach_args(cli, vm)).data.get("identitySteps") or []
            for vm in (launched, second)
        ]
        results.check(
            "health reports the machine-id step repaired on both VMs",
            all(
                any(
                    step.get("name") == "machine-id"
                    and step.get("outcome") == "repaired"
                    for step in vm_steps
                )
                for vm_steps in steps
            ),
            f"steps={steps!r}",
        )
    finally:
        gone = cli.call("terminate", second_id, "--wait", "--region", cli.region)
        results.check(
            "the second identity VM tore down clean",
            gone.data.get("microvmId") == second_id and not gone.data.get("leaked"),
            f"leaked={gone.data.get('leaked')}",
        )


def drive_launch_by_name(cli: Cli, launched: Envelope, results: Results) -> None:
    """`run --image <bare name>` launches the suite's image (#253).

    `RunMicrovm` answers a bare name with HTTP 400 "Malformed ARN", and every other launch in
    this suite passes the ARN. Core's `Sandbox::run` resolves a name through the image listing
    for the CLI and both bindings alike, so this one launch covers the three: the envelope
    reports the ARN the launch sent, and the service's own listing names the image the VM runs.
    """
    print("\n-- launch by bare image name --")
    arn = str(launched.data["imageIdentifier"])
    name = str(launched.data["imageName"])
    by_name = cli.call(
        "run",
        "--image",
        name,
        "--name",
        f"microvm-cli-conformance-by-name-{secrets.token_hex(4)}",
        "--memory",
        str(BASELINE_MEMORY_MIB),
        "--keep",
        "--region",
        cli.region,
        "--max-idle-sec",
        "600",
        "--suspended-sec",
        "600",
        "--max-duration-sec",
        "1800",
    )
    vm_id = str(by_name.data["microvmId"])
    try:
        results.eq(
            "a launch by bare image name sends the suite image's ARN",
            by_name.data.get("imageIdentifier"),
            arn,
        )
        results.eq(
            "a launch by bare image name reports the image's own name",
            by_name.data.get("imageName"),
            name,
        )
        listed = cli.call("ls", "--remote", "--region", cli.region)
        remote = listed.data.get("remote") or {}
        image_arns = [
            vm.get("imageArn")
            for vm in remote.get("microvms") or []
            if vm.get("microvmId") == vm_id
        ]
        results.eq(
            "the VM a bare image name launched runs the suite image",
            image_arns[0] if len(image_arns) == 1 else None,
            arn,
        )
    finally:
        gone = cli.call("terminate", vm_id, "--wait", "--region", cli.region)
        results.check(
            "the by-name VM tore down clean",
            gone.data.get("microvmId") == vm_id and not gone.data.get("leaked"),
            f"leaked={gone.data.get('leaked')}",
        )


def drive_launch_without_waiting(
    cli: Cli, launched: Envelope, results: Results
) -> None:
    """`run --keep --no-wait` returns at acceptance, and `wait` finishes the launch (#269).

    On a bounded VM of its own from the suite's image, registered under a name in a state
    directory of its own. The run must hand back what a later command needs while the VM is
    still starting; `wait --name` then adopts it and waits through core's
    `Sandbox::wait_until_ready`, and the exec after it must succeed on its first attempt, which
    is the whole claim of the daemon wait (#254). A second `wait` on the now RUNNING VM must
    take the daemon-only branch.

    How long a launch stays PENDING is the service's, so the first wait may already find the
    VM RUNNING; its `from` is recorded either way, and only a value outside the two fails.
    """
    print("\n-- launch without waiting, then wait --")
    vm_name = f"conformance-no-wait-{secrets.token_hex(4)}"
    with tempfile.TemporaryDirectory() as state:
        accepted = cli.call(
            "run",
            "--image",
            str(launched.data["imageIdentifier"]),
            "--name",
            f"microvm-cli-conformance-no-wait-{secrets.token_hex(4)}",
            "--memory",
            str(BASELINE_MEMORY_MIB),
            "--keep",
            "--no-wait",
            "--vm-name",
            vm_name,
            "--state-dir",
            state,
            "--region",
            cli.region,
            "--max-idle-sec",
            "600",
            "--suspended-sec",
            "600",
            "--max-duration-sec",
            "1800",
        )
        vm_id = str(accepted.data["microvmId"])
        try:
            results.check(
                "run --keep --no-wait returns the endpoint, agent token and id at acceptance",
                bool(accepted.data.get("endpoint"))
                and bool(accepted.data.get("agentToken"))
                and vm_id.startswith(("microvm-", "mvm-")),
                f"microvm={vm_id} endpoint={accepted.data.get('endpoint')!r} "
                f"token_len={len(accepted.data.get('agentToken') or '')}",
            )
            results.eq(
                "run --keep --no-wait registers its --vm-name",
                accepted.data.get("vmName"),
                vm_name,
            )
            waited = cli.call("wait", "--name", vm_name, "--state-dir", state)
            results.check(
                "wait --name finishes a --no-wait launch: RUNNING, from PENDING or RUNNING",
                waited.data.get("microvmId") == vm_id
                and waited.data.get("state") == "RUNNING"
                and waited.data.get("from") in {"PENDING", "RUNNING"},
                f"from={waited.data.get('from')!r} state={waited.data.get('state')!r}",
            )
            results.eq(
                "wait reports the endpoint the launch reported",
                waited.data.get("endpoint"),
                accepted.data.get("endpoint"),
            )
            first = cli.call(
                "exec", "echo waited", "--name", vm_name, "--state-dir", state
            )
            results.check(
                "the first exec after wait succeeds on its first attempt",
                first.data.get("exitCode") == 0
                and "waited" in (first.data.get("stdout") or ""),
                f"exit={first.data.get('exitCode')} stdout={first.data.get('stdout')!r}",
            )
            again = cli.call("wait", "--name", vm_name, "--state-dir", state)
            results.eq(
                "wait on a RUNNING VM waits for the daemon alone",
                again.data.get("from"),
                "RUNNING",
            )
        finally:
            gone = cli.call(
                "terminate",
                vm_id,
                "--wait",
                "--state-dir",
                state,
                "--region",
                cli.region,
            )
            results.check(
                "the no-wait VM tore down clean",
                gone.data.get("microvmId") == vm_id and not gone.data.get("leaked"),
                f"leaked={gone.data.get('leaked')}",
            )
