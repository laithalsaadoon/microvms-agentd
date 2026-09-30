# SPDX-License-Identifier: Apache-2.0
"""Core's sessions, the layer both bindings wrap, through its ignored Rust live tests.

Run to completion on the kept VM, a persisted launch key, adoption and lifecycle by id, and a
CLI-registered name found through core. Each runs `cargo test` against the suite's image, and
each launch is bounded and cleaned up by the test or by the section.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import tempfile
import time
from typing import Any

from harness.cli import Cli, attach_args
from harness.constants import BASELINE_MEMORY_MIB, REPO, SERVICE
from harness.envelope import Envelope, KindError
from harness.redact import command_for_log
from harness.results import Results


def run_to_completion_live(cli: Cli, launched: Envelope, name: str) -> tuple[bool, str]:
    """One ignored test of `crates/microvms-core/tests/live_run_to_completion.rs` on the kept VM.

    The attach coordinates travel in `MICROVM_LIVE_ATTACH`, so the test attaches through core's
    `Session::attach` (a fresh control plane and proxy-token minter) rather than launching.
    Returns whether it passed and a detail line carrying the test's own `eprintln!` summary.
    """
    env = os.environ.copy()
    env["AWS_REGION"] = cli.region
    env["MICROVM_LIVE_ATTACH"] = json.dumps(
        {
            "microvmId": str(launched.data["microvmId"]),
            "endpoint": str(launched.data["endpoint"]),
            "agentToken": str(launched.data["agentToken"]),
            "region": cli.region,
        }
    )
    command = [
        "cargo",
        "test",
        "-q",
        "-p",
        "microvms-core",
        "--test",
        "live_run_to_completion",
        name,
        "--",
        "--ignored",
        "--exact",
        "--nocapture",
    ]
    cli.log.append(command_for_log(command) + "  # MICROVM_LIVE_ATTACH=<attach JSON>")
    try:
        run = subprocess.run(
            command,
            cwd=REPO,
            env=env,
            text=True,
            capture_output=True,
            timeout=15 * 60,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "the Rust live test exceeded 15 minutes"
    summary = [
        line.strip()
        for line in run.stderr.splitlines()
        if line.startswith(("exec=", "collected"))
    ]
    return run.returncode == 0, f"exit={run.returncode} {' | '.join(summary)[:400]}"


def drive_run_to_completion(cli: Cli, launched: Envelope, results: Results) -> None:
    """`Session::run_to_completion` against the kept VM (#222, BIND-6..10).

    Four Rust tests through core, the layer both bindings wrap: a streamed bash command with
    `pipefail` returns its own exit code and output; the daemon's deadline reports 124 with a
    note; a command that ignores SIGTERM outlives the daemon's deadline for its ten-second
    `kill_grace`, so a client deadline inside that window kills a live group and collects the
    result; and a client grace shorter than the output linger of a grandchild holding the pipes
    synthesizes 124, after which the test collects the exec itself. The daemon answers a kill
    only once the group is gone (measured 2026-09-24), so the linger, not the escalation, is
    what a short grace can lose to. The cut-stream fallback is not induced here: nothing cuts
    the platform proxy's stream on demand, so it is covered by the scripted tiers in core.
    """
    print("\n-- run_to_completion (#222: BIND-6..10) --")
    passed, detail = run_to_completion_live(
        cli, launched, "a_streamed_bash_command_returns_its_own_exit_code_and_output"
    )
    results.check(
        "BIND-8 a streamed bash command is collected once with its own output and exit code",
        passed,
        detail,
    )
    passed, detail = run_to_completion_live(
        cli, launched, "a_daemon_deadline_reports_124_with_a_note"
    )
    results.check(
        "BIND-6 a daemon-enforced timeout reports POSIX exit code 124", passed, detail
    )
    results.check(
        "BIND-7 a daemon-enforced timeout carries a note naming timeout_sec",
        passed,
        detail,
    )
    passed, detail = run_to_completion_live(
        cli, launched, "a_client_deadline_kills_a_command_that_ignores_sigterm"
    )
    results.check(
        "BIND-9 a client deadline kills a command that ignores SIGTERM and collects it",
        passed,
        detail,
    )
    passed, detail = run_to_completion_live(
        cli, launched, "a_client_grace_shorter_than_the_pipe_linger_synthesizes_124"
    )
    results.check(
        "BIND-10 a client grace shorter than the output linger synthesizes 124",
        passed,
        detail,
    )

    # The CLI form (#259): the same composition through `exec --complete`, with the daemon's
    # deadline from --timeout-sec. The daemon ends `sleep 30` at two seconds, so the envelope
    # is a result (`ok`) whose posixExitCode is 124 with a note naming timeout_sec, and the
    # exit is ERR_TIMEOUT (10), as for any deadline that ended the command.
    attach = attach_args(cli, launched)
    argv = cli.argv("exec", "sleep 30", "--complete", "--timeout-sec", "2", *attach)
    cli.log.append(command_for_log(argv))
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    envelope = Cli.parse_stdout(proc.stdout, argv)
    notes = envelope.data.get("notes") or []
    results.check(
        "exec --complete --timeout-sec 2 reports posixExitCode 124 with a note and ERR_TIMEOUT",
        envelope.status == "ok"
        and envelope.data.get("posixExitCode") == 124
        and any("timeout_sec" in note for note in notes)
        and envelope.data.get("synthesized") is False
        and proc.returncode == 10,
        f"status={envelope.status} posixExitCode={envelope.data.get('posixExitCode')} "
        f"notes={notes} synthesized={envelope.data.get('synthesized')} $?={proc.returncode}",
    )


def drive_stable_launch(cli: Cli, launched: Envelope, results: Results) -> None:
    """One Rust live test owns its bounded launch and verifies cleanup independently."""
    name = "a persisted Rust launch key replays one VM and cleanup reaches TERMINATED"
    env = os.environ.copy()
    env["MICROVM_BACKGROUND_TEST_IMAGE"] = str(launched.data["imageIdentifier"])
    env["AWS_REGION"] = cli.region
    command = [
        "cargo",
        "test",
        "-p",
        "microvms-core",
        "--test",
        "live_background",
        "persisted_launch_key_replays_one_vm",
        "--",
        "--ignored",
        "--exact",
        "--nocapture",
    ]
    cli.log.append(command_for_log(command))
    try:
        run = subprocess.run(
            command,
            cwd=REPO,
            env=env,
            text=True,
            capture_output=True,
            timeout=15 * 60,
            check=False,
        )
        results.check(
            name,
            run.returncode == 0,
            f"exit={run.returncode} stdoutChars={len(run.stdout)} stderrChars={len(run.stderr)}",
        )
    except subprocess.TimeoutExpired:
        results.check(
            name,
            False,
            "Rust live check exceeded 15 minutes; VM lifetime capped at 300s",
        )


def run_rust_live(
    cli: Cli,
    launched: Envelope,
    results: Results,
    test: str,
    name: str,
    check: str,
    extra_env: dict[str, str] | None = None,
) -> None:
    """One ignored Rust live test against the suite's image, recorded as one named check."""
    env = os.environ.copy()
    env["MICROVM_BACKGROUND_TEST_IMAGE"] = str(launched.data["imageIdentifier"])
    env["AWS_REGION"] = cli.region
    env.update(extra_env or {})
    command = [
        "cargo",
        "test",
        "-p",
        "microvms-core",
        "--test",
        test,
        name,
        "--",
        "--ignored",
        "--exact",
        "--nocapture",
    ]
    cli.log.append(command_for_log(command))
    try:
        run = subprocess.run(
            command,
            cwd=REPO,
            env=env,
            text=True,
            capture_output=True,
            timeout=20 * 60,
            check=False,
        )
    except subprocess.TimeoutExpired:
        results.check(
            check, False, "Rust live check exceeded 20 minutes; VM lifetime 600s"
        )
        return
    results.check(
        check,
        run.returncode == 0,
        f"exit={run.returncode} stdoutChars={len(run.stdout)} stderrChars={len(run.stderr)}",
    )


def drive_adopt_by_id(cli: Cli, launched: Envelope, results: Results) -> None:
    """A VM launched by one handle is adopted by fresh ones and driven by id (#196).

    The Rust half drives core, the layer both bindings wrap: adopt while RUNNING, a refused
    `run`, suspend through the adopted handle, a second adoption while SUSPENDED, resume,
    and terminate, with cleanup observed through GetMicrovm.
    """
    print("\n-- adopt by id --")
    run_rust_live(
        cli,
        launched,
        results,
        "live_adopt",
        "a_vm_launched_elsewhere_is_adopted_and_driven_by_id",
        "a VM launched elsewhere is adopted and driven by id",
    )


def drive_find_by_name(cli: Cli, launched: Envelope, results: Results) -> None:
    """A name the CLI registered is found and adopted by name through core (#202).

    `run --keep --vm-name` writes the record with the CLI; core's registry reads it, adopts
    the VM by name from a fresh handle, names it again, terminates it, and releases both
    names; the CLI then refuses the released name locally. One registry, three readers. The
    state directory is this section's own, so a developer's `~/.microvm/runs` is untouched.
    """
    print("\n-- find by name (CLI registry read and released by core) --")
    vm_name = f"conformance-byname-{secrets.token_hex(4)}"
    with tempfile.TemporaryDirectory(prefix="microvm-names-") as state_dir:
        try:
            cli.call(
                "run",
                "--image",
                str(launched.data["imageIdentifier"]),
                "--name",
                f"microvm-cli-conformance-byname-{secrets.token_hex(4)}",
                "--memory",
                str(BASELINE_MEMORY_MIB),
                "--keep",
                "--vm-name",
                vm_name,
                "--state-dir",
                state_dir,
                "--region",
                cli.region,
                "--max-duration-sec",
                "600",
                timeout=15 * 60,
            )
        except KindError as exc:
            results.check("run --keep --vm-name for find-by-name", False, repr(exc))
            return
        run_rust_live(
            cli,
            launched,
            results,
            "live_names",
            "a_name_the_cli_registered_is_adopted_by_name_and_released",
            "a CLI-registered name is adopted by name through core and released",
            {"MICROVM_NAMES_STATE_DIR": state_dir, "MICROVM_NAMES_NAME": vm_name},
        )
        try:
            cli.call("health", "--name", vm_name, "--state-dir", state_dir, timeout=60)
            released = False
        except KindError as exc:
            released = exc.code == "ERR_PRECONDITION"
        results.check(
            "the CLI refuses a name core released, locally", released, vm_name
        )


def drive_lifecycle_by_id(
    cli: Cli, launched: Envelope, aws: Any, results: Results
) -> None:
    """Lifecycle by ID, retry-safe launches, and per-VM logging (#195, #197, #201, #203).

    Each half launches its own bounded VM from the suite's image and verifies its own
    cleanup. The two Rust halves drive core directly, the layer both bindings wrap; the CLI
    halves drive `run --client-token` and `run --vm-log-group`.
    """
    print("\n-- lifecycle by id, client tokens, per-VM logging --")
    run_rust_live(
        cli,
        launched,
        results,
        "live_lifecycle",
        "an_adopted_suspended_launch_resumes_to_running",
        "a client-token launch that adopts its suspended VM resumes it to RUNNING",
    )
    run_rust_live(
        cli,
        launched,
        results,
        "live_lifecycle",
        "a_vm_is_managed_by_id_through_the_control_plane",
        "a VM is managed by id through the control plane alone",
    )

    # `run --client-token`, twice: the second is the retry and must adopt the first VM.
    key = f"conformance-{secrets.token_hex(8)}"
    previous = os.environ.get("MICROVM_AGENT_TOKEN")
    os.environ["MICROVM_AGENT_TOKEN"] = secrets.token_hex(32)
    ids: list[str] = []
    try:
        for _ in range(2):
            try:
                reply = cli.call(
                    "run",
                    "--image",
                    str(launched.data["imageIdentifier"]),
                    "--name",
                    f"microvm-cli-conformance-token-{key[-8:]}",
                    "--memory",
                    str(BASELINE_MEMORY_MIB),
                    "--keep",
                    "--client-token",
                    key,
                    "--region",
                    cli.region,
                    "--max-duration-sec",
                    "600",
                    timeout=15 * 60,
                )
                ids.append(str(reply.data["microvmId"]))
            except KindError as exc:
                print(f"    run --client-token: {exc!r}")
        results.check(
            "run --client-token twice returns the same VM",
            len(ids) == 2 and ids[0] == ids[1],
            f"{len(ids)} replies, {len(set(ids))} distinct",
        )
    finally:
        if previous is None:
            os.environ.pop("MICROVM_AGENT_TOKEN", None)
        else:
            os.environ["MICROVM_AGENT_TOKEN"] = previous
        for microvm_id in sorted(set(ids)):
            try:
                # `--wait-sec` alone, which implies `--wait` (#267): the envelope's state is
                # TERMINATED only if the bounded wait ran.
                torn = cli.call(
                    "terminate",
                    microvm_id,
                    "--wait-sec",
                    "240",
                    "--region",
                    cli.region,
                    timeout=300.0,
                )
                results.check(
                    "the client-token VM was terminated",
                    not torn.data.get("leaked"),
                    f"leaked={torn.data.get('leaked')!r}",
                )
                results.eq(
                    "terminate --wait-sec waits for TERMINATED",
                    torn.data.get("state"),
                    "TERMINATED",
                )
            except Exception as exc:  # noqa: BLE001 - a teardown failure is a finding
                results.check("the client-token VM was terminated", False, repr(exc))

    # `run --vm-log-group`: the VM's own logs land in the caller's group. Under the
    # service namespace and a conformance prefix, so the execution role may write to it
    # and verify-clean attributes it if this section dies before deleting it.
    logs = aws.client("logs")
    group = (
        f"/aws/lambda-microvms/microvm-cli-conformance-vmlogs-{secrets.token_hex(4)}"
    )
    vm_id = ""
    try:
        ran = cli.call(
            "run",
            "--image",
            str(launched.data["imageIdentifier"]),
            "--name",
            f"microvm-cli-conformance-vmlogs-{secrets.token_hex(4)}",
            "--memory",
            str(BASELINE_MEMORY_MIB),
            "--exec",
            "echo vm-log-probe",
            "--vm-log-group",
            group,
            "--region",
            cli.region,
            timeout=15 * 60,
        )
        vm_id = str(ran.data.get("microvmId") or "")
        streams: list[dict[str, Any]] = []
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and not streams:
            try:
                streams = logs.describe_log_streams(logGroupName=group).get(
                    "logStreams", []
                )
            except logs.exceptions.ResourceNotFoundException:
                streams = []
            if not streams:
                time.sleep(10)
        results.check(
            "a per-VM log group receives the VM's own log streams",
            bool(streams),
            f"{len(streams)} stream(s) in the configured group",
        )
    except KindError as exc:
        results.check(
            "a per-VM log group receives the VM's own log streams", False, repr(exc)
        )
    finally:
        deleted, detail = delete_vm_log_group(logs, aws.client(SERVICE), vm_id, group)
        results.check("the per-VM log group was deleted", deleted, detail)


def delete_vm_log_group(
    logs: Any, microvms: Any, vm_id: str, group: str
) -> tuple[bool, str]:
    """Deletes a per-VM log group once nothing can write to it, and proves it stays gone.

    `run --exec` returns while its VM is still TERMINATING, and the VM's last log flush
    recreates a group deleted before then. Measured 2026-09-24: a full live run deleted
    this group, the check passed, and the leak check found it again five minutes later.
    So wait for TERMINATED, delete, wait, and look again.
    """
    deadline = time.monotonic() + 300
    state = ""
    while vm_id and time.monotonic() < deadline:
        try:
            state = str(microvms.get_microvm(microvmIdentifier=vm_id).get("state"))
        except Exception:  # noqa: BLE001 - a VM already gone is terminated
            state = "TERMINATED"
        if state == "TERMINATED":
            break
        time.sleep(10)
    recreated = 0
    for attempt in range(3):
        try:
            logs.delete_log_group(logGroupName=group)
        except logs.exceptions.ResourceNotFoundException:
            pass
        except Exception as exc:  # noqa: BLE001 - reported as the check's detail
            return False, f"{group}: delete failed with {type(exc).__name__}"
        time.sleep(30)
        present = logs.describe_log_groups(logGroupNamePrefix=group).get(
            "logGroups", []
        )
        if not any(g.get("logGroupName") == group for g in present):
            return True, f"{group} absent 30 s after delete (vm {state or 'unknown'})"
        recreated = attempt + 1
    return False, f"{group} came back {recreated} time(s) after delete"
