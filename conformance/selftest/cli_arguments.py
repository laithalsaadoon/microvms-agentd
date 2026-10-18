# SPDX-License-Identifier: Apache-2.0
"""The lanes' literal `build` calls, held to the CLI's flag dependencies, offline."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from harness.results import Results

CONFORMANCE = Path(__file__).resolve().parent.parent
CLI_SOURCE = CONFORMANCE.parent / "crates" / "microvms-cli" / "src" / "cli.rs"
# A `build` argument that clap refuses without `--reuse`. The parser rejects the whole
# invocation before anything is built, so a lane that passes one alone fails on its first
# live run and never offline, where every lane talks to a stub CLI. Read from cli.rs so a
# flag that gains the dependency there is held here too.
REUSE_ONLY = re.compile(r'#\[arg\([^\]]*requires = "reuse"[^\]]*\)\]\s*pub (\w+):')
SAMPLE = 'cli.call("build", "agentd", "--name", "n", "--s3-key-prefix", "n")'


def reuse_only_flags(source: str) -> set[str]:
    return {"--" + name.replace("_", "-") for name in REUSE_ONLY.findall(source)}


def build_calls(source: str) -> list[list[str]]:
    """The string literals of each `<cli>.call("build", ...)` in one module."""
    calls = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "call"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "build"
        ):
            calls.append(
                [
                    arg.value
                    for arg in node.args
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                ]
            )
    return calls


def missing_reuse(calls: list[list[str]], flags: set[str]) -> list[list[str]]:
    return [call for call in calls if flags & set(call) and "--reuse" not in call]


def check_build_flag_dependencies(results: Results) -> None:
    """Every lane's literal `build` call that passes a flag needing `--reuse` passes
    `--reuse` too, as the CLI's parser requires (#628's native Box lane didn't)."""
    flags = reuse_only_flags(CLI_SOURCE.read_text())
    results.check(
        "the CLI source names the build flags that need --reuse",
        {"--s3-key-prefix", "--force"} <= flags,
        f"found {sorted(flags)}",
    )
    calls = {
        path.name: build_calls(path.read_text())
        for path in sorted((CONFORMANCE / "lanes").glob("*.py"))
    }
    results.check(
        "the lanes' literal build calls are found, the native Box lane's among them",
        bool(calls.get("native_box.py")),
        f"{sum(len(found) for found in calls.values())} calls",
    )
    unmet = {
        name: missing
        for name, found in calls.items()
        if (missing := missing_reuse(found, flags))
    }
    results.check(
        "every lane build call that passes a --reuse-only flag also passes --reuse",
        not unmet,
        repr(unmet),
    )
    results.check(
        "a build call with --s3-key-prefix and no --reuse is caught",
        bool(missing_reuse(build_calls(SAMPLE), flags)),
    )
