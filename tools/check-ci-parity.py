#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml==6.0.3"]
# ///
# SPDX-License-Identifier: Apache-2.0
"""What `check` runs, CI runs (#315).

Every job in ci.yml and fuzz.yml installs mise and mise.lock's tools and runs `mise run
ci:<job>`, whose task holds the job's steps. So the tools, the environment and each check's
command are the same in CI and in a local `check` by construction. This script used to compare
the two files, because CI didn't install mise then (D14); what's left is what construction
doesn't give. It fails when:

- a task `check` depends on isn't reached from a task a workflow step runs, following `depends`,
  `depends_post` and the tasks a `run` calls (`{ task = ... }`, `{ tasks = [...] }`). Such a
  check passes here and never runs in CI. `wait_for` doesn't count: it waits for a task only
  when something else runs it. A step's `mise run ${{ matrix.<key> }}` is read once per matrix
  leg, `include` entries among them.
- a step runs `mise run` with a flag before the task (`-c`, `--skip-deps`), names its task
  through another expression, or names a task that doesn't exist.
- a task a workflow reaches runs `mise run` inside a command, where neither this check nor a
  failure follows it: a `|| true` there passes the job over a failing task. It calls the task
  from `run` (`{ task = ... }`) instead.
- no step in the workflows runs `mise run` (the floor, and what a reader that stopped matching
  looks like), or `check` depends on nothing.
- ci.yml's Rust toolchain isn't `rust-toolchain.toml`'s channel: each `dtolnay/rust-toolchain`
  step's `toolchain` input, and mise.toml's `rust`, must be it. The toolchain action stays
  because rust-cache keys on the toolchain it finds before mise installs anything. fuzz.yml
  runs nightly on purpose and isn't read here. No such step in ci.yml at all fails too.
- a tool floats: a mise.toml tool, in `[tools]` or in a task's `tools` (a `{{vars.<name>}}`
  read through `[vars]`), is `latest`, or a `uvx` or `uv tool run` call names no exact version,
  in a task's command, a workflow's `run:` step, or a seeded fault's `run` (CI's `guards` job
  runs those; the registry is read by check-guards-fire.py's loader). Each call is split like a
  shell word list: options before the tool are skipped (with their values), `--from <spec>`
  names the package, and `tool@X.Y.Z` or `tool==X.Y.Z` is the version. mise.lock records the
  exact version behind a pin such as `node = "22"`, and CI installs from it with `--locked`.
- a file it reads is missing, empty, or doesn't parse.

The tasks are mise.toml's and those of the TOML files its `[task_config] includes` names, read
by tools/mise_config.py, the loader every gate that reads them shares.

`tools/test_check_ci_parity.py` holds the other half, which reading the files can't: it runs
each workflow's `mise run` steps as written through real mise, over these tasks with every
command stubbed, and requires every command a job reaches to run and its failure to fail the
job. `tools/ci-local.py` reads the steps through `ci_commands` here.
"""

from __future__ import annotations

import argparse
import itertools
import re
import runpy
import shlex
import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

import mise_config
import yaml

WORKFLOWS = (".github/workflows/ci.yml", ".github/workflows/fuzz.yml")
# The workflow whose toolchain inputs are held to the channel: fuzz.yml's are nightly by design.
RUST_WORKFLOW = ".github/workflows/ci.yml"
MISE = mise_config.MISE
TOOLCHAIN = "rust-toolchain.toml"
# check-guards-fire.py's reader of the registry, so this reads the entries `guards:list` and
# `fire` do.
GUARDS = runpy.run_path(str(Path(__file__).with_name("check-guards-fire.py")))
# The task whose dependencies CI has to reach.
GATE = "check"

# `mise run <task>` in a step's text: the task is the next word, which may be an expression.
# Not after a backtick, where a message quotes the command it tells a reader to run.
MISE_RUN = re.compile(r"(?<![\w./`-])mise\s+run\s+(\$\{\{.*?\}\}|\S+)([^\n]*)")
MATRIX = re.compile(r"\$\{\{\s*matrix\.([\w-]+)\s*\}\}")
EXPRESSION = re.compile(r"\$\{\{.*?\}\}")
VAR = re.compile(r"\{\{\s*vars\.([\w-]+)\s*\}\}")
# A call of a uv tool runner, in a step's text or a registry argv joined with spaces.
UV_CALL = re.compile(r"(?<![\w./-])(uvx|uv\s+tool\s+run)(?=\s)")
# The `uvx` options that take a value as the next word (`uvx --help`, uv 0.12.13). `--from`
# is read on its own. Any other option is a flag; a value option missing here would be read as
# the tool, and a value that isn't a package name fails as unreadable rather than passing.
UVX_VALUED = {
    "-w", "--with", "--with-editable", "--with-requirements", "-c", "--constraints",
    "-b", "--build-constraints", "--overrides", "--env-file", "--python-platform",
    "--torch-backend", "--index", "--default-index", "-i", "--index-url",
    "--extra-index-url", "-f", "--find-links", "--index-strategy", "--keyring-provider",
    "-P", "--upgrade-package", "--upgrade-group", "--resolution", "--prerelease",
    "--prerelease-package", "--fork-strategy", "--exclude-newer",
    "--exclude-newer-package", "--no-sources-package", "--reinstall-package",
    "--link-mode", "-C", "--config-setting", "--config-settings-package",
    "--no-build-isolation-package", "--no-build-package", "--no-binary-package",
    "--cache-dir", "--refresh-package", "-p", "--python", "--color",
    "--allow-insecure-host", "--directory", "--project", "--config-file",
}  # fmt: skip
PACKAGE = re.compile(r"([A-Za-z0-9][A-Za-z0-9_.-]*)(?:\[[^\]]*\])?(.*)")
EXACT = re.compile(r"\d+\.\d+\.\d+")


class Unreadable(Exception):
    """A file this check needs is missing, empty or doesn't parse."""


class Command(NamedTuple):
    """One `mise run` a workflow step runs, on one matrix leg."""

    workflow: str
    job: str
    # The job's matrix values on this leg, empty for a job with no matrix.
    leg: dict[str, str]
    step: dict
    # The step's `run` with this leg's `${{ matrix.* }}` values in.
    run: str
    task: str
    # The text after the task on its line, which mise hands the task as arguments.
    args: str

    def where(self) -> str:
        leg = (
            f" ({', '.join(f'{k}={v}' for k, v in self.leg.items())})"
            if self.leg
            else ""
        )
        return f"{self.workflow} job `{self.job}`{leg} step `{step_label(self.step)}`"


def read_text(path: Path, label: str) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise Unreadable(f"{label}: can't read {path}: {error.strerror}") from None
    if not text.strip():
        raise Unreadable(f"{label}: {path} is empty")
    return text


def load_yaml(path: Path, label: str) -> dict:
    text = read_text(path, label)
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise Unreadable(f"{label}: {path} doesn't parse: {error}") from None
    if not isinstance(data, dict):
        raise Unreadable(f"{label}: {path} isn't a mapping")
    return data


def load_toml(path: Path, label: str) -> dict:
    text = read_text(path, label)
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise Unreadable(f"{label}: {path} doesn't parse: {error}") from None


def load_mise(root: Path) -> mise_config.Config:
    """mise.toml and the task files its includes name, read the way mise reads them."""
    read_text(root / MISE, MISE)
    try:
        return mise_config.load(root)
    except mise_config.Unreadable as error:
        raise Unreadable("; ".join(error.problems)) from None


def load_tasks(root: Path) -> dict[str, dict]:
    """Every task, by name, wherever it's defined."""
    return load_mise(root).tables()


def scalar(value: object) -> str:
    """A YAML or TOML value as text. YAML reads `true` as a bool."""
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


def step_label(step: dict) -> str:
    """A step's `name`, else the first line of its `run`, else its action."""
    if step.get("name"):
        return str(step["name"])
    lines = str(step.get("run") or "").strip().splitlines()
    if lines:
        return lines[0].strip()
    return str(step.get("uses", "unnamed")).split("@")[0]


def legs(body: dict) -> list[dict[str, str]]:
    """A job's matrix legs, as GitHub expands them: every combination of the list axes, each
    `include` entry added to the legs its axis values match (or as a leg of its own when it
    matches none), then `exclude`'s removed. A job with no matrix is one leg with no values."""
    matrix = (body.get("strategy") or {}).get("matrix")
    if not isinstance(matrix, dict):
        return [{}]
    axes = {
        k: [scalar(x) for x in v]
        for k, v in matrix.items()
        if k not in ("include", "exclude") and isinstance(v, list)
    }
    out = [dict(zip(axes, combo)) for combo in itertools.product(*axes.values())]
    if not axes:
        out = []
    for entry in matrix.get("include") or []:
        if not isinstance(entry, dict):
            continue
        entry = {k: scalar(v) for k, v in entry.items()}
        matched = [
            leg
            for leg in out
            if all(leg.get(k) == v for k, v in entry.items() if k in axes)
        ]
        if matched:
            for leg in matched:
                leg.update({k: v for k, v in entry.items() if k not in axes})
        else:
            out.append(entry)
    for entry in matrix.get("exclude") or []:
        if isinstance(entry, dict):
            drop = {k: scalar(v) for k, v in entry.items()}
            out = [
                leg for leg in out if not all(leg.get(k) == v for k, v in drop.items())
            ]
    return out or [{}]


def ci_commands(
    root: Path, problems: list[str], workflows: tuple[str, ...] = WORKFLOWS
) -> list[Command]:
    """Every `mise run` the workflows' steps run, one per matrix leg, in workflow order."""
    commands: list[Command] = []
    for wf in workflows:
        try:
            data = load_yaml(root / wf, wf)
        except Unreadable as error:
            problems.append(str(error))
            continue
        for job, body in (data.get("jobs") or {}).items():
            if not isinstance(body, dict):
                continue
            for leg in legs(body):
                for step in body.get("steps") or []:
                    if not isinstance(step, dict) or "run" not in step:
                        continue

                    def value(match: re.Match, leg: dict = leg) -> str:
                        return leg.get(match.group(1), match.group(0))

                    run = MATRIX.sub(value, str(step["run"]))
                    for found in MISE_RUN.finditer(run):
                        task, args = found.group(1), found.group(2)
                        command = Command(wf, job, leg, step, run, task, args)
                        if task.startswith("-"):
                            problems.append(
                                f"{command.where()} passes `mise run` the flag `{task}` "
                                "before its task; a job runs its task as the task says"
                            )
                            continue
                        if EXPRESSION.search(task):
                            problems.append(
                                f"{command.where()} names its task as `{task}`, an expression "
                                "this check can't read; name it, or through `matrix`"
                            )
                            continue
                        commands.append(command)
    return commands


def listed(value: object) -> list[str]:
    if isinstance(value, str):
        value = [value]
    return [
        str(v).split()[0] for v in value or [] if isinstance(v, str) and str(v).split()
    ]


def calls(task: dict) -> list[str]:
    """The tasks running `task` runs: its dependencies before and after, and the tasks its
    `run` calls. Not `wait_for`, which runs nothing."""
    out = listed(task.get("depends")) + listed(task.get("depends_post"))
    run = task.get("run")
    for entry in run if isinstance(run, list) else [run]:
        if isinstance(entry, dict):
            out += listed(entry.get("task")) + listed(entry.get("tasks"))
    return out


def reached(tasks: dict[str, dict], roots: list[str]) -> set[str]:
    seen: set[str] = set()
    stack = list(roots)
    while stack:
        name = stack.pop()
        if name in seen or name not in tasks:
            continue
        seen.add(name)
        stack += calls(tasks[name])
    return seen


def check_reach(
    tasks: dict[str, dict], commands: list[Command], problems: list[str]
) -> tuple[list[str], int]:
    """The tasks CI runs, and how many `check` dependencies they reach."""
    if not commands:
        problems.append(
            f"no step in {' or '.join(WORKFLOWS)} runs `mise run`, so CI runs no task"
        )
    roots: list[str] = []
    for command in commands:
        if command.task not in tasks:
            problems.append(
                f"{command.where()} runs `mise run {command.task}`, and mise.toml has no "
                f"`{command.task}` task"
            )
        elif command.task not in roots:
            roots.append(command.task)
    gate = tasks.get(GATE)
    depends = listed(gate.get("depends")) if isinstance(gate, dict) else []
    if not depends:
        problems.append(
            f"mise.toml's `{GATE}` depends on no task, so there's nothing to hold CI to"
        )
    seen = reached(tasks, roots)
    for name in sorted(seen):
        run = tasks[name].get("run")
        for entry in run if isinstance(run, list) else [run]:
            for found in MISE_RUN.finditer(entry if isinstance(entry, str) else ""):
                problems.append(
                    f"`{name}` runs `{found.group(0).strip()}` in a command; a task a CI job "
                    f'runs calls another as {{ task = "{found.group(1)}" }}, which this '
                    "check follows and whose failure fails the caller"
                )
    for dep in depends:
        if dep not in seen:
            problems.append(
                f"`{GATE}` depends on `{dep}`, which no task a CI job runs reaches, so CI "
                f"never runs it (the jobs run {', '.join(f'`{r}`' for r in roots) or 'nothing'})"
            )
    return roots, len([d for d in depends if d in seen])


def check_rust(
    ci: dict, mise: dict, toolchain: dict, problems: list[str]
) -> str | None:
    channel = scalar((toolchain.get("toolchain") or {}).get("channel", ""))
    if not channel:
        problems.append(f"rust: {TOOLCHAIN} has no [toolchain] channel")
        return None
    local = pin((mise.get("tools") or {}).get("rust", ""))
    if local != channel:
        problems.append(
            f"rust: mise.toml pins rust = {local!r}, and {TOOLCHAIN} says {channel}"
        )
    seen = 0
    for job, body in (ci.get("jobs") or {}).items():
        for step in (body or {}).get("steps") or []:
            if not isinstance(step, dict):
                continue
            if str(step.get("uses", "")).split("@")[0] == "dtolnay/rust-toolchain":
                seen += 1
                got = scalar((step.get("with") or {}).get("toolchain", ""))
                if got != channel:
                    problems.append(
                        f"rust: {RUST_WORKFLOW} job `{job}` installs {got!r}, and {TOOLCHAIN} "
                        f"says {channel}"
                    )
    if not seen:
        problems.append(f"rust: {RUST_WORKFLOW} has no dtolnay/rust-toolchain step")
    return channel


# ── pins: no `latest`, no unpinned uvx ──────────────────────────────────────


def pin(value: object) -> str:
    """A mise tool value, `"1.2.3"` or `{ version = "1.2.3", ... }`."""
    if isinstance(value, dict):
        return scalar(value.get("version", ""))
    return scalar(value)


def check_latest(config: mise_config.Config, problems: list[str]) -> int:
    mise = config.data
    variables = mise.get("vars") if isinstance(mise.get("vars"), dict) else {}
    places = [(f"{MISE} [tools]", mise.get("tools") or {})]
    places += [
        (f"{task.file} {task.header} tools", task.table["tools"])
        for task in config.tasks.values()
        if isinstance(task.table.get("tools"), dict)
    ]
    count = 0
    for place, tools in places:
        for key, value in tools.items():
            count += 1
            text = pin(value)
            text = VAR.sub(
                lambda m: scalar(variables.get(m.group(1), m.group(0))), text
            )
            if text == "latest":
                problems.append(f"{place} pins `{key}` to latest")
            elif VAR.search(text):
                problems.append(
                    f"{place} pins `{key}` to {pin(value)!r}, a variable [vars] doesn't set"
                )
    return count


def uv_words(text: str) -> Iterator[tuple[str, list[str] | None, str]]:
    """Each uv tool-runner call in `text`: the runner, the words after it, and the call as
    written (for messages). The words are None when they don't split, as with a quote left
    open."""
    for line in text.splitlines():
        for call in UV_CALL.finditer(line):
            rest = line[call.end() :]
            written = f"{call.group(1)}{rest}".strip()
            try:
                words = shlex.split(rest, comments=True)
            except ValueError:
                words = None
            yield call.group(1), words, written


def uv_tool(words: list[str]) -> tuple[str | None, str | None, str]:
    """The tool a uv tool-runner call runs, its exact version or None, and why when it can't
    tell. Options before the tool are skipped with their values; `--from <spec>` names the
    package whose version counts."""
    spec = None
    i = 0
    while i < len(words):
        word = words[i]
        if word == "--":
            i += 1
            break
        if word == "--from":
            if i + 1 >= len(words):
                return None, None, "`--from` has no value"
            spec = words[i + 1]
            i += 2
            continue
        if word.startswith("--from="):
            spec = word.split("=", 1)[1]
        elif word.startswith("-"):
            name = word.split("=", 1)[0]
            i += 2 if name in UVX_VALUED and "=" not in word else 1
            continue
        else:
            break
        i += 1
    if spec is None:
        if i >= len(words):
            return None, None, "no tool follows the options"
        spec = words[i]
    match = PACKAGE.fullmatch(spec)
    if not match:
        return None, None, f"`{spec}` isn't a package name"
    tool, rest = match.groups()
    # `ruff@X` and `ruff==X` name one version; a range (`>=`, `~=`) or nothing names none.
    for sep in ("@", "=="):
        if rest.startswith(sep) and EXACT.fullmatch(rest[len(sep) :]):
            return tool, rest[len(sep) :], ""
    return tool, None, ""


def check_uvx(text: str, where: str, problems: list[str]) -> int:
    """Every uv tool-runner call in `text` names an exact version. How many calls it read."""
    count = 0
    for runner, words, written in uv_words(text):
        count += 1
        if words is None:
            problems.append(f"{where}: can't read the `{runner}` call `{written}`")
            continue
        tool, version, why = uv_tool(words)
        if tool is None:
            problems.append(
                f"{where}: can't read the `{runner}` call `{written}`: {why}"
            )
        elif version is None:
            problems.append(
                f"{where} runs `{written}`, which names no exact version of {tool}"
            )
    return count


def task_texts(config: mise_config.Config) -> Iterator[tuple[str, str]]:
    for task in config.tasks.values():
        run = task.table.get("run")
        for entry in run if isinstance(run, list) else [run]:
            if isinstance(entry, str):
                yield f"{task.file} {task.header}", entry


def check_pins(
    root: Path,
    config: mise_config.Config,
    workflows: dict[str, dict],
    problems: list[str],
) -> int:
    count = 0
    for where, text in task_texts(config):
        count += check_uvx(text, where, problems)
    for wf, data in workflows.items():
        for job, body in (data.get("jobs") or {}).items():
            for step in (body or {}).get("steps") or []:
                if isinstance(step, dict) and "run" in step:
                    where = f"{wf} job `{job}` step `{step_label(step)}`"
                    count += check_uvx(str(step["run"]), where, problems)
    tables, refused = GUARDS["registry_tables"](root)
    if refused:
        raise Unreadable(
            f"{GUARDS['REGISTRY']}: can't read the fault registry: {'; '.join(refused)}"
        )
    for table in tables:
        fault = table.data if isinstance(table.data, dict) else {}
        run = fault.get("run", [])
        argvs = run if run and isinstance(run[0], list) else [run]
        where = f"{table.file} entry `{fault.get('id', '?')}`"
        for argv in argvs:
            count += check_uvx(" ".join(map(str, argv)), where, problems)
    return count


def check(root: Path) -> tuple[list[str], str]:
    """The problems found, and a one-line summary of what was held."""
    problems: list[str] = []
    try:
        config = load_mise(root)
        mise, tasks = config.data, config.tables()
        toolchain = load_toml(root / TOOLCHAIN, TOOLCHAIN)
        workflows = {wf: load_yaml(root / wf, wf) for wf in WORKFLOWS}
        commands = ci_commands(root, problems)
        roots, gated = check_reach(tasks, commands, problems)
        channel = check_rust(workflows[RUST_WORKFLOW], mise, toolchain, problems)
        tools = check_latest(config, problems)
        calls_read = check_pins(root, config, workflows, problems)
    except Unreadable as error:
        return [str(error)], ""
    summary = (
        f"CI's jobs run {len(roots)} tasks, which reach all {gated} of `{GATE}`'s; rust "
        f"{channel}; {tools} tool pins and {calls_read} uv tool calls exact"
    )
    return problems, summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", help="the repository (default: this script's)")
    args = parser.parse_args(argv)
    root = Path(args.root) if args.root else Path(__file__).resolve().parents[1]
    problems, summary = check(root)
    if problems:
        print("ci parity: FAILED")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"ci parity: OK: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
