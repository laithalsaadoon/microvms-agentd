# SPDX-License-Identifier: Apache-2.0
"""What the report may print about an invocation, an exception or an agent's output."""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from typing import Any

from harness.envelope import KindError

# Logs are public evidence; the actual argv and envelope remain available in memory.
SECRET_ARGUMENT_FLAGS = frozenset(
    {
        "--agent-token",
        "--client-token",
        "--token",
        "--bearer-token",
        "--api-key",
        "--password",
        "--session-token",
        "--aws-session-token",
        "--access-key-id",
        "--secret-access-key",
        "--bedrock-token",
        "--authorization",
        "--header",
        "--env",
        "--launch-env",
    }
)


def redacted_argv(argv: Sequence[str]) -> list[str]:
    result = []
    hide_next = False
    for argument in argv:
        if hide_next:
            result.append("[REDACTED]")
            hide_next = False
            continue
        flag, equal, _ = argument.partition("=")
        if flag in SECRET_ARGUMENT_FLAGS:
            result.append(flag + "=[REDACTED]" if equal else flag)
            hide_next = not equal
        else:
            result.append(argument)
            # The harness always places the positional task directly after this verb.
            hide_next = argument == "agent-prompt"
    return result


def command_for_log(argv: Sequence[str]) -> str:
    return shlex.join(redacted_argv(argv))


def exception_summary(error: Exception) -> str:
    if isinstance(error, KindError):
        return repr(error)
    return type(error).__name__


def agent_output_summary(data: dict[str, Any]) -> str:
    def scalar(key: str) -> str:
        value = data.get(key)
        return (
            repr(value)
            if value is None or isinstance(value, (int, bool))
            else "invalid"
        )

    return (
        f"exit={scalar('exitCode')} timedOut={scalar('timedOut')} "
        f"truncated={scalar('truncated')} "
        f"stdoutChars={len(str(data.get('stdout') or ''))} "
        f"stderrChars={len(str(data.get('stderr') or ''))}"
    )
