# SPDX-License-Identifier: Apache-2.0
"""What a log may print, across real subprocesses and the agent sections' drive paths."""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from harness.cli import Cli
from harness.envelope import Envelope, EnvelopeError, KindError
from harness.redact import SECRET_ARGUMENT_FLAGS
from harness.results import Results
from lanes.agents import AGENT_VM_CHECKS, BACKGROUND_AGENT_CHECKS, drive_agent_vm
from lanes.sessions import drive_stable_launch


class FakeArtifactBucket:
    """The S3 calls the agent section makes, over a set of keys."""

    def __init__(self, keys: set[str]) -> None:
        self.keys = set(keys)

    def list_objects_v2(self, *, Bucket: str, Prefix: str) -> dict[str, Any]:
        found = sorted(key for key in self.keys if key.startswith(Prefix))
        return {"Contents": [{"Key": key} for key in found], "KeyCount": len(found)}

    def delete_objects(self, *, Bucket: str, Delete: dict[str, Any]) -> None:
        for item in Delete["Objects"]:
            self.keys.discard(item["Key"])


def check_log_privacy(cli: Cli, results: Results, state_dir: Path) -> None:
    """Canaries cross real subprocesses and the complete named agent drive paths."""
    import io
    from contextlib import redirect_stderr, redirect_stdout
    from types import SimpleNamespace
    from unittest.mock import patch

    secret = "private-argument-canary"
    transcript = "private-transcript-canary"
    forwarded = True
    for flag in sorted(SECRET_ARGUMENT_FLAGS):
        for args in ((flag, secret), (flag + "=" + secret,)):
            response = cli.call("echoargs", *args)
            forwarded &= all(argument in response.data["argv"] for argument in args)
            cli.call_stream("stream", *args)
    task = cli.call("agent-prompt", secret, "--agent", "codex")
    forwarded &= secret in task.data["argv"]
    results.check("log redaction preserves the actual CLI arguments", forwarded)
    results.check(
        "ordinary and streaming command logs redact secret flags and agent tasks",
        secret not in "\n".join(cli.log) and "[REDACTED]" in "\n".join(cli.log),
    )

    failures = []
    for method, case in (
        (cli.call, "privatemalformed"),
        (cli.call, "privatemessage"),
        (cli.call, "privatemismatch"),
        (cli.call_stream, "privatemalformed"),
        (cli.call_stream, "privateempty"),
        (cli.call_stream, "privatelast"),
        (cli.call_stream, "privatefirst"),
        (cli.call_stream, "privatemessage"),
        (cli.call_stream, "privatemismatch"),
    ):
        try:
            method(case, "--agent-token", secret)
        except (EnvelopeError, KindError) as error:
            failures.append(str(error) + repr(error))
    results.check(
        "parse and protocol errors retain kinds without argument or transcript excerpts",
        len(failures) == 9
        and all(
            secret not in message and transcript not in message for message in failures
        )
        and any(
            "Conflict" in message and "ERR_PROTOCOL" in message for message in failures
        ),
    )
    timeouts = []
    for method in (cli.call, cli.call_stream):
        try:
            method("privatetimeout", "--agent-token=" + secret, timeout=0.1)
        except subprocess.TimeoutExpired as error:
            timeouts.append(error)
    results.check(
        "subprocess timeout exceptions omit secret argv and captured transcripts",
        len(timeouts) == 2
        and all(
            secret not in str(error) + repr(error)
            and error.output is None
            and error.stderr is None
            for error in timeouts
        ),
    )

    class AgentCli:
        region = "us-test-1"

        def __init__(self) -> None:
            self.up_count = 0
            self.polls: dict[str, int] = {}
            self.artifacts: dict[str, str] = {}
            self.log: list[str] = []

        def call(self, *args: str, **_kwargs: Any) -> Envelope:
            data: dict[str, Any] = {
                "exitCode": 0,
                "stdout": transcript,
                "stderr": transcript,
                "timedOut": False,
            }
            kind = "microvm.exec"
            process_exit = 0
            if args[0] == "agent-up":
                self.up_count += 1
                kind = "microvm.agent"
                data = {
                    "vmReused": self.up_count > 1,
                    "imageReused": False,
                    "imageIdentifier": "arn:image" if self.up_count == 1 else None,
                    "imageName": "agent-vm-claude-code-codex-0123456789ab",
                    "microvmId": "microvm-canary",
                    "credentialExpiresAt": int(time.time()) + 600,
                    "agents": [
                        {"agent": agent, "model": "synthetic." + agent}
                        for agent in BACKGROUND_AGENT_CHECKS
                    ],
                }
            elif args[0] == "agent-prompt":
                if "--agent" not in args:
                    raise KindError(
                        Envelope(
                            "error",
                            "1",
                            "",
                            {},
                            code="ERR_PRECONDITION",
                            exit_code=2,
                            error=transcript,
                        )
                    )
                agent = args[args.index("--agent") + 1]
                kind = "microvm.agent.prompt"
                timeout = (
                    int(args[args.index("--execution-timeout") + 1])
                    if "--execution-timeout" in args
                    else None
                )
                data.update(
                    agent=agent,
                    model="synthetic." + agent,
                    agentVersion="1.2.3",
                    uid=1000,
                    permissionMode="unrestricted" if timeout else "agent-default",
                    executionTimeoutSec=timeout,
                    reapGroupOnExit=timeout is not None,
                    stdout="12 " + transcript,
                )
                match = re.search(r"printf %s ([a-f0-9]+) > ([^`]+)", args[1])
                if match:
                    self.artifacts[match[2]] = match[1]
                if "--detach" in args:
                    exec_id = args[args.index("--exec-id") + 1]
                    self.polls.setdefault(exec_id, 0)
                    data.update(execId=exec_id, phase="running")
            elif args[0] == "exec":
                if args[1] == "--poll":
                    exec_id = args[2]
                    self.polls[exec_id] += 1
                    running = self.polls[exec_id] == 1
                    data.update(
                        execId=exec_id,
                        phase="running" if running else "exited",
                        timedOut=not running,
                        exitCode=None,
                        signal=15,
                        outcome={"timed_out": not running},
                    )
                    process_exit = 0 if running else 10
                elif args[1] == "cat /workspace/.agent-vm.json":
                    data["stdout"] = json.dumps(
                        {
                            "agents": [
                                {"agent": agent} for agent in BACKGROUND_AGENT_CHECKS
                            ],
                            "private": transcript,
                        }
                    )
                elif args[1].startswith("stat "):
                    data["stdout"] = "1000:600"
                elif args[1].startswith("sed "):
                    data["stdout"] = (
                        "export HOME PATH AWS_REGION CLAUDE_CODE_USE_BEDROCK ANTHROPIC_MODEL AWS_BEARER_TOKEN_BEDROCK OPENAI_API_KEY "
                        + transcript
                    )
                else:
                    data["stdout"] = self.artifacts.get(
                        args[1].removeprefix("cat "), transcript
                    )
            elif args[0] == "ps":
                data = {
                    "procs": [
                        {"execId": exec_id, "pids": [], "private": transcript}
                        for exec_id in self.polls
                    ]
                }
            elif args[0] == "terminate":
                kind = "microvm.teardown"
                data = {
                    "microvmId": "microvm-canary",
                    "leaked": [],
                    "undeletedLogGroups": ["/aws/lambda-microvms/canary"],
                }
            elif args[0] == "health":
                raise KindError(
                    Envelope(
                        "error",
                        "1",
                        "",
                        {},
                        code="ERR_PRECONDITION",
                        exit_code=2,
                        error=transcript,
                    )
                )
            return Envelope("ok", "1", kind, data, process_exit_code=process_exit)

    output, agent_results = io.StringIO(), Results()
    fake_cli = AgentCli()
    with redirect_stdout(output), redirect_stderr(output):
        drive_agent_vm(
            fake_cli,
            cli.binary,
            state_dir,
            SimpleNamespace(delete_log_group=lambda **_: None),
            FakeArtifactBucket(
                {"agent-vm-claude-code-codex-0123456789ab/artifact.zip"}
            ),
            agent_results,
        )
        with patch.object(
            subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, transcript, transcript),
        ):
            drive_stable_launch(
                fake_cli,
                Envelope("ok", "1", "microvm.run", {"imageIdentifier": "arn:image"}),
                agent_results,
            )
    expected_checks = (
        len(AGENT_VM_CHECKS) + sum(map(len, BACKGROUND_AGENT_CHECKS.values())) + 1
    )
    results.check(
        "agent and background drive paths retain all named checks without raw transcripts",
        len(agent_results.passed) == expected_checks
        and not agent_results.failed
        and transcript not in output.getvalue()
        and "stdoutChars=" in output.getvalue(),
        f"{len(agent_results.passed)}/{expected_checks} named checks passed",
    )
