# SPDX-License-Identifier: Apache-2.0
"""Agent VMs (`docs/AGENT-VMS.md`): `agent-up` and `agent-prompt` with both profiles on
their own build and VM, and the background prompts' permission modes and daemon deadlines.
The only section that needs Bedrock."""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
import time
from pathlib import Path
from typing import Any

from harness.cli import Cli
from harness.constants import BASELINE_MEMORY_MIB
from harness.envelope import Envelope, KindError
from harness.redact import agent_output_summary, exception_summary
from harness.results import Results

#: The names `drive_agent_vm` records, in the order it records them. A tuple
#: rather than a literal at each call site because the section has a failure
#: mode the others do not: `agent-up` itself can fail before any VM exists (no Bedrock
#: access in the account, an expired credential chain), and the header's rule is that
#: nothing here is ever recorded SKIP. So a failed `agent-up` records every one of these
#: as FAIL with the same detail, and the denominator the summary prints stays the one the
#: header claims.
AGENT_VM_CHECKS = (
    "agent-up launched a fresh VM rather than reusing one",
    "the image reuse verdict is a boolean",
    "the agent image is named by the profile set and the content hash",
    "the envelope lists exactly claude-code then codex",
    "the credential expiry is in the future",
    "the guest marker names both agents",
    "the env file belongs to uid 1000 at mode 600",
    "the env file exports the variable each installed agent reads",
    "claude-code completed a Bash task with exit 0",
    "codex completed its task with exit 0",
    "codex wrote a file the workspace kept",
    "a second agent-up refreshes credentials without launching",
    "a prompt with two agents installed and no --agent is refused",
    "the agent VM and its image were deleted",
    "terminate released the agent VM's name",
    "the suite deleted the agent image's log group",
    "agent-up's artifact is at the image's content-addressed key (#258)",
    "the suite deleted the agent image's S3 artifact (#258)",
)


BACKGROUND_AGENT_CHECKS = {
    agent: tuple(
        f"{agent}: {claim}"
        for claim in (
            "an omitted permission mode preserves agent-default",
            "unrestricted mode performs a shell action without approval",
            "the prompt reports its actual version, uid, model and deadline",
            "a detached prompt retry keeps the same exec id",
            "the daemon deadline survives the submitting CLI process",
            "the timed-out prompt leaves no live process group",
        )
    )
    for agent in ("claude-code", "codex")
}


def prompt_metadata_ok(
    data: dict[str, Any], agent: str, model: str, mode: str, timeout: int | None
) -> bool:
    """Only recorded, typed facts count; absent fields cannot look like defaults."""
    return (
        data.get("agent") == agent
        and data.get("model") == model
        and bool(model)
        and bool(re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", str(data.get("agentVersion"))))
        and data.get("uid") == 1000
        and data.get("permissionMode") == mode
        and "executionTimeoutSec" in data
        and data["executionTimeoutSec"] == timeout
        and data.get("reapGroupOnExit") is (timeout is not None)
    )


def daemon_deadline_ok(data: dict[str, Any], process_exit_code: int | None) -> bool:
    # A child can handle SIGTERM and exit 0; the daemon's deadline still fired.
    # Cleanup is checked independently by the named process-group assertion.
    outcome = data.get("outcome")
    return (
        data.get("phase") in ("exited", "acked")
        and process_exit_code == 10
        and data.get("timedOut") is True
        and isinstance(outcome, dict)
        and outcome.get("timed_out") is True
    )


def drive_background_agents(
    cli: Cli,
    attach: tuple[str, ...],
    defaults: dict[str, Envelope],
    models: dict[str, str],
    results: Results,
) -> None:
    """Twelve checks; the CLI parent exits before each deadline is observed."""
    print("\n-- background agent permissions and daemon deadlines --")
    for agent, names in BACKGROUND_AGENT_CHECKS.items():
        try:
            results.check(
                names[0],
                prompt_metadata_ok(
                    defaults[agent].data, agent, models[agent], "agent-default", None
                ),
                "default permission policy and prompt metadata",
            )
            nonce = secrets.token_hex(12)
            path = f"/workspace/background-{agent}-{nonce}.txt"
            # A model can decline outright and still exit 0 (measured for Codex, see the
            # AGENT-7 hello.py check). One re-prompt keeps that coin flip from failing the
            # suite; a second decline fails it, and the detail names how many prompts ran.
            attempts = 0
            while True:
                attempts += 1
                prompt = cli.call(
                    "agent-prompt",
                    f"Use your shell tool to run `printf %s {nonce} > {path}`. "
                    "Then read the file and finish. Do not ask for approval.",
                    "--agent",
                    agent,
                    "--permission-mode",
                    "unrestricted",
                    "--execution-timeout",
                    "120",
                    "--timeout",
                    "150",
                    "--reap-group-on-exit",
                    *attach,
                    timeout=180,
                )
                kept = cli.call("exec", f"cat {path}", *attach, timeout=30)
                declined = (
                    prompt.data.get("exitCode") == 0
                    and kept.data.get("stdout") != nonce
                )
                if not declined or attempts == 2:
                    break
            results.check(
                names[1],
                prompt.data.get("exitCode") == 0
                and prompt.data.get("timedOut") is False
                and kept.data.get("exitCode") == 0
                and kept.data.get("stdout") == nonce,
                f"exit={prompt.data.get('exitCode')} prompts={attempts} "
                f"artifact matches={kept.data.get('stdout') == nonce}",
            )
            results.check(
                names[2],
                prompt_metadata_ok(
                    prompt.data, agent, models[agent], "unrestricted", 120
                ),
                f"version={prompt.data.get('agentVersion')} uid={prompt.data.get('uid')} "
                f"model={prompt.data.get('model')} executionTimeoutSec={prompt.data.get('executionTimeoutSec')}",
            )
            exec_id = f"background-deadline-{agent}-{nonce}"
            args = (
                "agent-prompt",
                "Use your shell tool to sleep for 300 seconds, then finish.",
                "--agent",
                agent,
                "--permission-mode",
                "unrestricted",
                "--execution-timeout",
                "5",
                "--timeout",
                "0.01",
                "--reap-group-on-exit",
                "--detach",
                "--exec-id",
                exec_id,
                *attach,
            )
            first = cli.call(*args, timeout=60)
            # This read happens in a new CLI process. Running here proves the parent
            # has already gone away while the daemon deadline is still pending.
            after_parent = cli.call("exec", "--poll", exec_id, *attach, timeout=30)
            retry = cli.call(*args, timeout=60)
            results.check(
                names[3],
                first.data.get("execId") == retry.data.get("execId") == exec_id
                and first.data.get("phase") == retry.data.get("phase") == "running",
                exec_id,
            )
            deadline = time.monotonic() + 25
            terminal: dict[str, Any] = {}
            process_exit_code = None
            group = None
            while time.monotonic() < deadline:
                polled = cli.call("exec", "--poll", exec_id, *attach, timeout=30)
                terminal = polled.data
                process_exit_code = polled.process_exit_code
                procs = cli.call("ps", *attach, timeout=30).data.get("procs") or []
                group = next(
                    (row for row in procs if row.get("execId") == exec_id), None
                )
                if (
                    daemon_deadline_ok(terminal, process_exit_code)
                    and group is not None
                    and not group.get("pids")
                ):
                    break
                time.sleep(1)
            results.check(
                names[4],
                after_parent.data.get("phase") == "running"
                and daemon_deadline_ok(terminal, process_exit_code),
                f"phase={terminal.get('phase')} timedOut={terminal.get('timedOut')} "
                f"exitCode={terminal.get('exitCode')} signal={terminal.get('signal')}",
            )
            results.check(
                names[5],
                group is not None and group.get("pids") == [],
                f"groupFound={group is not None} remainingPids={len(group.get('pids') or []) if group else None}",
            )
            cli.call("ack", exec_id, *attach, timeout=30)
        except Exception as exc:  # noqa: BLE001 - keep the named denominator on failure
            recorded = set(results.passed) | {name for name, _ in results.failed}
            for name in names:
                if name not in recorded:
                    results.check(
                        name, False, f"{type(exc).__name__}: background probe failed"
                    )


def drive_agent_vm(
    cli: Cli, binary: Path, state_dir: Path, logs: Any, s3: Any, results: Results
) -> None:
    """`agent-up` and `agent-prompt` (`docs/AGENT-VMS.md`), live, both profiles.

    The checks in `AGENT_VM_CHECKS` plus the background checks, against a VM this section
    launches and terminates itself, from an image this section builds (or reuses)
    itself: the agent image is derived from the
    profile set and the daemon bytes, so no launch from the suite's image can carry a
    coding agent. Live rather than only scripted for the reason the spec's Verification
    section gives: the local guards see the Dockerfile as text and the token as a shape,
    and only the real service says whether the arm64 build of two npm installs boots,
    whether Bedrock accepts a presigned token that is one canonical byte off, and whether
    a headless agent actually calls a tool when asked to.

    **This is the only section that needs Bedrock**, on both default models in the
    conformance account, and it prints the model ids it used to stderr so a red run
    names the model rather than the suite. An `agent-up` that fails before any VM
    exists records every check in `AGENT_VM_CHECKS` as FAIL, never SKIP: the header's
    claim is that nothing here is recorded SKIP, and an absent Bedrock entitlement is a
    finding about the account, not a gap in the client.

    Two prompts, one per agent, each shaped so the assertion is about the agent having
    *acted* rather than answered: Claude Code is asked for a number only a shell can
    produce, and Codex is asked to write a file a later `exec` can `cat`. The second
    `agent-up` under the same name is the AGENT-8 refresh path — no build, no launch,
    a later expiry — and the `--agent`-less prompt is the AGENT-10 refusal, which is a
    local read of the guest marker and costs no model call.

    `--keep` is implicit (an agent VM is kept by definition) and the teardown is an
    explicit `terminate <name> --delete-image` in this function's own `finally`, so
    however the checks above end the VM, its image, and the service-created log group go.
    The image is deleted rather than kept for the next run's `imageReused: true`,
    because a snapshot nobody owns bills for a week (`tools/verify-clean.py` knows the
    `agent-vm-` prefix, so a leak of one is visible, and that is the backstop rather than
    the plan); the reuse verdict is asserted as a boolean, which is the property that
    holds either way.
    """
    print("\n== agent VMs (agent-up / agent-prompt, own build, needs Bedrock) ==")
    vm_name = f"conformance-agent-{secrets.token_hex(4)}"
    up_args = (
        "agent-up",
        str(binary),
        "--vm-name",
        vm_name,
        "--agent",
        "claude-code",
        "--agent",
        "codex",
        "--memory",
        str(BASELINE_MEMORY_MIB),
        "--state-dir",
        str(state_dir),
        "--region",
        cli.region,
        "--max-idle-sec",
        "600",
        "--suspended-sec",
        "600",
        "--max-duration-sec",
        "3600",
    )
    attach = ("--name", vm_name, "--state-dir", str(state_dir))

    started = time.monotonic()
    try:
        up = cli.call(*up_args, timeout=50 * 60)
    except Exception as exc:  # noqa: BLE001 - the reason is every check's finding
        # Nothing launched, or the CLI already tore the unprovisioned VM down (agent.rs
        # terminates on a failed mint or install and names any leak in `data.leaked`).
        # Every check fails with the same detail so the denominator does not move.
        leaked = exc.envelope.data if isinstance(exc, KindError) else {}
        detail = f"agent-up failed: {exception_summary(exc)}" + (
            f" leaked={leaked.get('leaked')!r} microvm={leaked.get('microvmId')!r} "
            f"image={leaked.get('imageIdentifier')!r}"
            if leaked
            else ""
        )
        print(
            "  models: unknown (agent-up failed before reporting them)", file=sys.stderr
        )
        for name in AGENT_VM_CHECKS:
            results.check(name, False, detail)
        for names in BACKGROUND_AGENT_CHECKS.values():
            for name in names:
                results.check(name, False, detail)
        return
    up_seconds = time.monotonic() - started

    agents = up.data.get("agents") or []
    models = {str(row.get("agent")): str(row.get("model")) for row in agents}
    print(
        "  models: " + ", ".join(f"{agent}={model}" for agent, model in models.items()),
        file=sys.stderr,
    )
    image = str(up.data.get("imageIdentifier") or "")
    image_name = str(up.data.get("imageName") or "")
    microvm_id = str(up.data.get("microvmId") or "")
    first_expiry = up.data.get("credentialExpiresAt")
    # `agent-up` ensures its image through core (#258): the artifact lives at
    # `s3://<bucket>/<name>/artifact.zip`, uploaded by the build that made the image, a
    # reused one's included.
    bucket = os.environ.get("MICROVM_BUCKET", "")
    artifact_prefix = f"{image_name}/"
    try:
        listed = s3.list_objects_v2(Bucket=bucket, Prefix=artifact_prefix)
        keys = [item.get("Key") for item in listed.get("Contents") or []]
    except Exception as exc:  # noqa: BLE001 - the error class is the finding
        keys = [f"error: {type(exc).__name__}"]
    results.check(
        AGENT_VM_CHECKS[16],
        bool(image_name) and keys == [f"{artifact_prefix}artifact.zip"],
        f"bucket={bucket!r} keys={keys!r}",
    )
    try:
        results.check(
            AGENT_VM_CHECKS[0],
            up.type == "microvm.agent" and up.data.get("vmReused") is False,
            f"type={up.type} vmReused={up.data.get('vmReused')!r} in {up_seconds:.0f}s",
        )
        results.check(
            AGENT_VM_CHECKS[1],
            isinstance(up.data.get("imageReused"), bool),
            f"imageReused={up.data.get('imageReused')!r}",
        )
        prefix = "agent-vm-claude-code-codex-"
        suffix = image_name.removeprefix(prefix)
        results.check(
            AGENT_VM_CHECKS[2],
            image_name.startswith(prefix)
            and len(suffix) == 12
            and all(c in "0123456789abcdef" for c in suffix),
            f"{image_name!r}",
        )
        results.eq(
            AGENT_VM_CHECKS[3],
            [row.get("agent") for row in agents],
            ["claude-code", "codex"],
        )
        now = int(time.time())
        results.check(
            AGENT_VM_CHECKS[4],
            isinstance(first_expiry, int) and first_expiry > now,
            f"credentialExpiresAt={first_expiry!r} now={now}",
        )

        # AGENT-6: the marker a later process reads to learn what is installed.
        marker = cli.call("exec", "cat /workspace/.agent-vm.json", *attach)
        marker_agents: list[str] = []
        try:
            marker_agents = [
                str(row.get("agent"))
                for row in json.loads(marker.data.get("stdout") or "{}").get(
                    "agents", []
                )
            ]
        except json.JSONDecodeError:
            pass
        results.check(
            AGENT_VM_CHECKS[5],
            marker.data.get("exitCode") == 0
            and sorted(marker_agents) == ["claude-code", "codex"],
            agent_output_summary(marker.data),
        )
        # AGENT-5: the credential file is the agent's and nobody else's. `%u` rather
        # than `%U` because al2023-minimal need not resolve uid 1000 to a name.
        env_stat = cli.call("exec", "stat -c %u:%a /workspace/.agent-env", *attach)
        results.check(
            AGENT_VM_CHECKS[6],
            env_stat.data.get("exitCode") == 0
            and (env_stat.data.get("stdout") or "").strip() == "1000:600",
            agent_output_summary(env_stat.data),
        )

        # The variable names in the credential file, and only the names: `sed` strips
        # every value, so the token cannot reach this log. Codex reads
        # AWS_BEARER_TOKEN_BEDROCK on a bedrock-runtime host and ignores the `env_key` its
        # own config declares (measured 2026-09-10: without it, ten of ten tasks 401'd),
        # and a two-agent VM would get that variable from the Claude Code profile either
        # way — so this asserts the union the installed set requires, and the Codex-only
        # case is covered offline by `a_codex_only_vm_still_gets_the_variable_codex_reads`.
        env_names = cli.call("exec", "sed 's/=.*//' /workspace/.agent-env", *attach)
        names = set((env_names.data.get("stdout") or "").split())
        wanted = {
            "export",
            "HOME",
            "PATH",
            "AWS_REGION",
            "CLAUDE_CODE_USE_BEDROCK",
            "ANTHROPIC_MODEL",
            "AWS_BEARER_TOKEN_BEDROCK",
            "OPENAI_API_KEY",
        }
        results.check(
            AGENT_VM_CHECKS[7],
            env_names.data.get("exitCode") == 0 and wanted <= names,
            f"exit={env_names.data.get('exitCode')} missing={sorted(wanted - names)}",
        )

        # AGENT-7 through Claude Code: a number only a shell produces, so a reply with
        # a digit in it is a reply that ran the tool.
        claude = cli.call(
            "agent-prompt",
            "Run the shell command `ls /usr/bin | wc -l` with your Bash tool and "
            "reply with only the number.",
            "--agent",
            "claude-code",
            "--timeout",
            "600",
            *attach,
            timeout=700.0,
        )
        claude_out = claude.data.get("stdout") or ""
        results.check(
            AGENT_VM_CHECKS[8],
            claude.type == "microvm.agent.prompt"
            and claude.data.get("agent") == "claude-code"
            and claude.data.get("exitCode") == 0
            and any(ch.isdigit() for ch in claude_out),
            f"type={claude.type} agent={claude.data.get('agent')!r} "
            + agent_output_summary(claude.data),
        )

        # AGENT-7 through Codex: a file the workspace keeps, read back by a plain exec
        # so the assertion does not depend on what the agent chose to print.
        # The model can decline a task outright: measured once in five runs on
        # 2026-09-10 (Codex 0.154.0, global.openai.gpt-5.6-sol), the reply was a refusal with
        # zero tool calls and Codex exited 0, so the prompt check alone cannot see it.
        # One re-prompt keeps a model's coin flip from failing the suite; a second
        # decline fails it, and the detail names how many prompts it took.
        codex_task = (
            "Create hello.py in the current directory that prints hello from a "
            "microvm, run it, and show the output."
        )
        attempts = 0
        while True:
            attempts += 1
            codex = cli.call(
                "agent-prompt",
                codex_task,
                "--agent",
                "codex",
                "--timeout",
                "600",
                *attach,
                timeout=700.0,
            )
            kept = cli.call("exec", "cat /workspace/hello.py", *attach)
            declined = (
                codex.data.get("exitCode") == 0 and kept.data.get("exitCode") != 0
            )
            if not declined or attempts == 2:
                break
        results.check(
            AGENT_VM_CHECKS[9],
            codex.type == "microvm.agent.prompt"
            and codex.data.get("agent") == "codex"
            and codex.data.get("exitCode") == 0,
            f"type={codex.type} agent={codex.data.get('agent')!r} "
            f"prompts={attempts} " + agent_output_summary(codex.data),
        )
        results.check(
            AGENT_VM_CHECKS[10],
            kept.data.get("exitCode") == 0,
            f"prompts={attempts} " + agent_output_summary(kept.data),
        )

        drive_background_agents(
            cli, attach, {"claude-code": claude, "codex": codex}, models, results
        )

        # AGENT-8: the same command against the registered name is a refresh, not a
        # launch — no image in the envelope, the same VM, a token that expires no earlier.
        again = cli.call(*up_args, timeout=5 * 60)
        second_expiry = again.data.get("credentialExpiresAt")
        results.check(
            AGENT_VM_CHECKS[11],
            again.type == "microvm.agent"
            and again.data.get("vmReused") is True
            and again.data.get("imageIdentifier") is None
            and again.data.get("microvmId") == microvm_id
            and isinstance(second_expiry, int)
            and isinstance(first_expiry, int)
            and second_expiry >= first_expiry,
            f"vmReused={again.data.get('vmReused')!r} "
            f"image={again.data.get('imageIdentifier')!r} "
            f"microvm={again.data.get('microvmId')!r} "
            f"expiry {first_expiry!r} -> {second_expiry!r}",
        )

        # AGENT-10: two agents and no `--agent` is two answers to "which one", refused
        # from the marker before any model call. A local decision, so `data.kind` is
        # absent and the code is the right granularity.
        try:
            cli.call("agent-prompt", "anything", *attach, timeout=120.0)
            results.check(AGENT_VM_CHECKS[12], False, "no refusal")
        except KindError as exc:
            results.check(
                AGENT_VM_CHECKS[12],
                exc.code == "ERR_PRECONDITION" and exc.kind is None,
                exception_summary(exc),
            )
    finally:
        # This section's own VM, image, and log group, this section's own teardown.
        # Terminate by the **name**, with the image the first `agent-up` reported, so
        # the resolution path and the deletion are both asserted.
        try:
            torn = cli.call(
                "terminate",
                vm_name,
                "--image-identifier",
                image,
                "--image-name",
                image_name,
                "--delete-image",
                "--wait",
                "--state-dir",
                str(state_dir),
                "--region",
                cli.region,
                timeout=15 * 60,
            )
        except Exception as exc:  # noqa: BLE001 - a teardown failure is a finding
            results.check(
                AGENT_VM_CHECKS[13], False, f"{microvm_id}: {exception_summary(exc)}"
            )
            results.check(
                AGENT_VM_CHECKS[14],
                False,
                "terminate failed, so the name was never released",
            )
            results.check(
                AGENT_VM_CHECKS[15], False, "terminate failed, so no group was named"
            )
        else:
            results.check(
                AGENT_VM_CHECKS[13],
                torn.type == "microvm.teardown"
                and torn.data.get("microvmId") == microvm_id
                and not torn.data.get("leaked"),
                f"microvm={torn.data.get('microvmId')!r} leaked={torn.data.get('leaked')!r}",
            )
            # Released means the registry no longer resolves it: `health --name` fails
            # locally, with zero wire calls, and the record file is gone.
            try:
                cli.call("health", *attach, timeout=60.0)
                results.check(
                    AGENT_VM_CHECKS[14], False, "health --name still resolved"
                )
            except KindError as exc:
                results.check(
                    AGENT_VM_CHECKS[14],
                    exc.code == "ERR_PRECONDITION"
                    and exc.kind is None
                    and not (state_dir / "names" / f"{vm_name}.json").exists(),
                    f"code={exc.code} kind={exc.kind!r} "
                    f"record exists={(state_dir / 'names' / f'{vm_name}.json').exists()}",
                )
            # The service-created group, deleted by the party that caused it to exist
            # (`drive_teardown`'s argument). Already absent is the desired end state.
            groups = [str(group) for group in torn.data.get("undeletedLogGroups") or []]
            failures: list[str] = []
            for group in groups:
                try:
                    logs.delete_log_group(logGroupName=group)
                except Exception as exc:  # noqa: BLE001 - the reason is the finding
                    if type(exc).__name__ != "ResourceNotFoundException":
                        failures.append(f"{group}: {exception_summary(exc)}")
            results.check(
                AGENT_VM_CHECKS[15],
                bool(groups) and not failures,
                f"groups={groups!r} failures={failures!r}",
            )
        # The artifact the image was built from, which terminate doesn't delete: the image is
        # gone, so nothing reuses it.
        try:
            stale = s3.list_objects_v2(Bucket=bucket, Prefix=artifact_prefix)
            doomed = [item["Key"] for item in stale.get("Contents") or []]
            if doomed:
                s3.delete_objects(
                    Bucket=bucket, Delete={"Objects": [{"Key": key} for key in doomed]}
                )
            left = s3.list_objects_v2(Bucket=bucket, Prefix=artifact_prefix).get(
                "KeyCount", 0
            )
            results.check(
                AGENT_VM_CHECKS[17],
                bool(image_name) and left == 0,
                f"deleted={doomed!r} remaining={left}",
            )
        except Exception as exc:  # noqa: BLE001 - the error class is the finding
            results.check(AGENT_VM_CHECKS[17], False, type(exc).__name__)
