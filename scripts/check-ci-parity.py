#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml==6.0.3"]
# ///
# SPDX-License-Identifier: Apache-2.0
"""CI's environment and tool versions match the local gates' (#315).

`mise run check` and CI's jobs are meant to run the same checks, and they drifted in ways no
local run could show. ci.yml's top-level `env` set `CARGO_TERM_COLOR=always` and no local shell
did, so PR #314's first CI run failed `AdapterLintTests` on colored clippy output that passed
locally. mise pinned most tools to `latest` while CI ran `uvx ruff` and `uvx semgrep` unpinned,
and CI's bindings job ran a different Node major than mise did. CI doesn't install mise (D14), so
this script compares the files rather than running either side.

It fails when:

- ci.yml's top-level `env` is missing or empty, or a key there is missing from mise.toml's
  `[env]` or has another value there. mise may carry keys CI doesn't set.
- a tool CI installs runs a version other than the local one: ruff and semgrep through `uvx`
  (against mise.toml), maturin through `uvx` (against `MATURIN` in scripts/generate-py-stubs.py,
  the local gate that runs it), uv through `setup-uv`'s `version` input, Node through
  `setup-node` (majors compared), actionlint through `raven-actions/actionlint`'s `version`
  input, and the release downloads CI checks by sha256 (ast-grep, betterleaks, syft, grype,
  osv-scanner). The Rust channel in rust-toolchain.toml must match mise.toml and every
  `dtolnay/rust-toolchain` input. A mise task's own `tools` pin of a compared tool must equal
  the `[tools]` one, or that task runs a version CI doesn't.
- a checksummed download has no `sha256sum -c` in its step, or checks a hash other than the
  linux-x64 checksum mise.lock records for that tool and version, so the version, the hash and
  the lock move together.
- any `uvx` or `uv tool run` call in ci.yml, or in a guards/faults.toml `run` (CI's bindings
  job runs those), names no exact version, or can't be read. Each call is split like a shell
  word list: options before the tool are skipped (with their values), `--from <spec>` names
  the package, and `tool@X.Y.Z` or `tool==X.Y.Z` is the version.
- a mise.toml tool, in `[tools]` or in a task's `tools`, is `latest`. mise.lock records the
  exact version behind a fuzzy pin such as `node = "22"`.
- a compared tool isn't found in ci.yml at all, so a pattern that stops matching fails by name
  instead of comparing nothing.
- a `run:` step in a workflow `ci/local.toml` names (ci.yml must be one) has no entry there,
  so `mise run ci:local` wouldn't run it and nothing says why. Also: a job with no entry, an
  entry that names no step or lists steps out of the workflow's order, a skip or a local change
  with no reason, a `uses:` step listed with no local command, a `${{ }}` the planned steps use
  with no value in `[expressions]`, a workflow with no `run:` steps at all, and a `ci:<task>`
  mise task that's missing, doesn't run `scripts/ci-local.py <task>`, or isn't in `ci:local`'s
  `depends`. `check` mustn't depend on any of them: they build the whole tree once per job.
- a `uses:` step in a job ci:local runs is neither listed with a local `run` nor its action
  named in `[actions]` with a reason (the setup actions: checkout, toolchains, caches). A lint
  or scanner that ships as an action would otherwise not run locally while this check said
  "covered". An `[actions]` entry no planned job uses fails too.
- a workflow, job or step sets a key the runner doesn't model: top-level `defaults`, a job's
  `if`, `container`, `services`, `needs` or `continue-on-error`, a step's `continue-on-error`
  or `timeout-minutes`, or a job that runs on anything but ubuntu. Each would change what CI
  runs while ci:local ran the job as if it weren't there.
- a file it reads is missing, empty, or doesn't parse.

ci.yml is read as YAML, so a tool named only in a comment doesn't count as installed.

Not compared yet: trivy (trivy-action's bundled default), terraform (setup-terraform with no
version, in a job ci:local skips), cargo-deny (cargo-deny-action's bundled binary, 0.20.2 at
the pinned SHA, the same as mise's today), and cargo-fuzz (unpinned on both sides). Each is
installed by an action or command with its own default, so a comparison needs a `version`
input on the CI side first.

`plan()` is also what `scripts/ci-local.py` runs from, so the runner refuses a plan this check
would fail.
"""

from __future__ import annotations

import argparse
import ast
import re
import shlex
import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

import yaml

CI = ".github/workflows/ci.yml"
MISE = "mise.toml"
LOCK = "mise.lock"
TOOLCHAIN = "rust-toolchain.toml"
STUBS = "scripts/generate-py-stubs.py"
REGISTRY = "guards/faults.toml"
LOCAL = "ci/local.toml"
WORKFLOWS = ".github/workflows"

# Every tool compared, in the order the summary prints them.
TOOLS = (
    "uv",
    "ruff",
    "semgrep",
    "maturin",
    "node",
    "actionlint",
    "ast-grep",
    "betterleaks",
    "syft",
    "grype",
    "osv-scanner",
)
# The mise.toml `[tools]` key for each tool whose local version mise pins. maturin's local
# version is the stub generator's, since that's the local gate that runs it.
MISE_KEYS = {
    "uv": "uv",
    "ruff": "ruff",
    "semgrep": "semgrep",
    "node": "node",
    "actionlint": "aqua:rhysd/actionlint",
    "ast-grep": "aqua:ast-grep/ast-grep",
    "betterleaks": "betterleaks",
    "syft": "syft",
    "grype": "grype",
    "osv-scanner": "osv-scanner",
}
# The release downloads CI checks by sha256, by GitHub repository.
RELEASES = {
    "ast-grep/ast-grep": "ast-grep",
    "betterleaks/betterleaks": "betterleaks",
    "anchore/syft": "syft",
    "anchore/grype": "grype",
    "google/osv-scanner": "osv-scanner",
}
# The actions that install a compared tool at their `version` input, by action.
VERSION_INPUTS = {"raven-actions/actionlint": "actionlint", "astral-sh/setup-uv": "uv"}
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
RELEASE = re.compile(
    r"https://github\.com/([\w.-]+/[\w.-]+)/releases/download/v?([^/\s]+)/"
)
# `curl ... -o <file> <url>` and `echo "<sha256>  <file>" | sha256sum -c -`, paired by file.
DOWNLOAD = re.compile(r"-o\s+(\S+)\s+(https://github\.com/\S+)")
SHA256 = re.compile(r"([0-9a-f]{64})\s+(\S+)\"\s*\|\s*sha256sum\b")
EXACT = re.compile(r"\d+\.\d+\.\d+")
EXPRESSION = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")
TASK = re.compile(r"[a-z][a-z0-9-]*")
# The keys a `local."<label>"` table may carry.
OVERRIDES = {"run", "skip", "env", "unset", "reason"}
# The keys ci-local.py models at each level. Any other one (`defaults` at the top, a job's
# `if`, `container`, `services`, `needs`, a step's `continue-on-error`) changes what CI runs,
# and the runner would run the job as if it weren't there. YAML reads a bare `on` as True.
WORKFLOW_KEYS = {
    "name",
    "run-name",
    "on",
    "True",
    "permissions",
    "env",
    "concurrency",
    "jobs",
}
JOB_KEYS = {
    "name",
    "runs-on",
    "steps",
    "strategy",
    "timeout-minutes",
    "env",
    "defaults",
    "permissions",
}
STEP_KEYS = {
    "name",
    "id",
    "uses",
    "with",
    "run",
    "shell",
    "env",
    "working-directory",
    "if",
}
MAJOR = re.compile(r"(\d+)(?:\.\d+)*")


class Unreadable(Exception):
    """A file this script needs is missing, empty, or doesn't parse."""


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
        raise Unreadable(f"{label}: {path} doesn't parse as YAML: {error}") from None
    if not isinstance(data, dict):
        raise Unreadable(f"{label}: {path} isn't a YAML mapping")
    return data


def load_toml(path: Path, label: str) -> dict:
    text = read_text(path, label)
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise Unreadable(f"{label}: {path} doesn't parse as TOML: {error}") from None


def scalar(value: object) -> str:
    """A YAML or TOML scalar as the string a process sees in its environment."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def strings(node: object) -> Iterator[str]:
    """Every string under a step: `run`, `env` values, `with` inputs."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from strings(value)


def step_label(job: str, step: dict) -> str:
    name = step.get("name") or step.get("uses")
    if not name:
        lines = str(step.get("run", "")).strip().splitlines()
        name = lines[0] if lines else "unnamed"
    return f"ci.yml job `{job}` step `{name}`"


def uv_words(text: str) -> Iterator[tuple[str, list[str] | None, str]]:
    """Each uv tool call in `text`, the words after it to the end of its command, and the
    rest of its line. The words are None when that line doesn't split like shell words."""
    joined = text.replace("\\\n", " ")
    for match in UV_CALL.finditer(joined):
        rest = joined[match.end() :].split("\n", 1)[0]
        lexer = shlex.shlex(rest, posix=True, punctuation_chars=";&|()`")
        lexer.whitespace_split = True
        lexer.commenters = "#"
        try:
            words = list(lexer)
        except ValueError:
            yield " ".join(match.group(1).split()), None, rest.strip()
            continue
        end = next(
            (i for i, w in enumerate(words) if set(w) <= set(";&|()`")), len(words)
        )
        yield " ".join(match.group(1).split()), words[:end], rest.strip()


def uv_tool(words: list[str]) -> tuple[str | None, str | None, str]:
    """The package a uv tool call runs, its version or None, and the call as written up to
    the tool. The package is None when the words don't name one."""
    source = None
    index = 0
    while index < len(words) and words[index].startswith("-"):
        word = words[index]
        name, eq, value = word.partition("=")
        if word == "--":
            index += 1
            break
        if name == "--from":
            source = (
                value if eq else (words[index + 1] if index + 1 < len(words) else "")
            )
            index += 1 if eq else 2
        elif name in UVX_VALUED and not eq:
            index += 2
        else:
            index += 1
    shown = " ".join(words[: index + 1])
    spec = (
        source if source is not None else (words[index] if index < len(words) else "")
    )
    match = PACKAGE.fullmatch(spec)
    if not match:
        return None, None, shown
    package, rest = match.group(1), match.group(2)
    # `ruff@X` and `ruff==X` name one version; a range (`>=`, `~=`) or nothing names none.
    for mark in ("@", "=="):
        if rest.startswith(mark):
            return package, rest.removeprefix(mark).strip() or None, shown
    return package, None, shown


def uvx_pins(
    text: str, where: str, problems: list[str]
) -> Iterator[tuple[str, str | None]]:
    """The (tool, version) of each `uvx` or `uv tool run` call in `text`. An unpinned or
    unreadable call is a problem, and its version is None."""
    for call, words, line in uv_words(text):
        package, version, shown = (
            (None, None, line) if words is None else uv_tool(words)
        )
        if package is None:
            problems.append(
                f"{where} runs `{call} {shown}`, and which tool and version that runs "
                "can't be read; write it as `uvx <tool>@X.Y.Z`"
            )
            continue
        if not version or not EXACT.fullmatch(version):
            problems.append(
                f"{package}: {where} runs `{call} {shown}`, which names no exact version "
                "(X.Y.Z)"
            )
            version = None
        yield package, version


class Observed:
    """What CI runs: each tool's versions, with where each one was read."""

    def __init__(self) -> None:
        self.tools: dict[str, list[tuple[str, str]]] = {tool: [] for tool in TOOLS}
        self.rust: list[tuple[str, str]] = []
        # Tools CI runs with no version to compare; each is already a problem.
        self.unpinned: set[str] = set()
        # (tool, version, the sha256 its step checks or None, where) per checksummed download.
        self.hashes: list[tuple[str, str, str | None, str]] = []

    def add(self, tool: str, version: str | None, where: str) -> None:
        if version is None:
            self.unpinned.add(tool)
        elif tool in self.tools:
            self.tools[tool].append((version, where))


def read_ci(ci: dict, problems: list[str]) -> Observed:
    seen = Observed()
    jobs = ci.get("jobs")
    if not isinstance(jobs, dict) or not jobs:
        problems.append("ci.yml has no jobs")
        return seen
    for job, body in jobs.items():
        steps = body.get("steps", []) if isinstance(body, dict) else []
        for step in steps if isinstance(steps, list) else []:
            if not isinstance(step, dict):
                continue
            where = step_label(job, step)
            for text in strings(step):
                for tool, version in uvx_pins(text, where, problems):
                    seen.add(tool, version, where)
                files = {url: name for name, url in DOWNLOAD.findall(text)}
                digests = {name: digest for digest, name in SHA256.findall(text)}
                for match in RELEASE.finditer(text):
                    tool = RELEASES.get(match.group(1))
                    if tool:
                        seen.add(tool, match.group(2), where)
                        url = next(
                            (u for u in files if u.startswith(match.group(0))), None
                        )
                        digest = digests.get(files[url]) if url else None
                        seen.hashes.append((tool, match.group(2), digest, where))
            uses = str(step.get("uses", "")).split("@")[0]
            inputs = step.get("with") if isinstance(step.get("with"), dict) else {}
            if uses == "actions/setup-node":
                seen.add("node", scalar(inputs.get("node-version", "")), where)
            elif uses in VERSION_INPUTS:
                tool = VERSION_INPUTS[uses]
                if "version" not in inputs:
                    problems.append(
                        f"{tool}: {where} has no `version` input, so it runs the latest "
                        "release"
                    )
                    seen.add(tool, None, where)
                else:
                    seen.add(tool, scalar(inputs["version"]), where)
            elif uses == "dtolnay/rust-toolchain":
                seen.rust.append((scalar(inputs.get("toolchain", "")), where))
            elif uses == "EmbarkStudios/cargo-deny-action" and "rust-version" in inputs:
                seen.rust.append((scalar(inputs["rust-version"]), where))
    return seen


def read_registry(registry: dict, seen: Observed, problems: list[str]) -> None:
    for fault in registry.get("fault", []):
        run = fault.get("run", [])
        argvs = run if run and isinstance(run[0], list) else [run]
        where = f"{REGISTRY} entry `{fault.get('id', '?')}`"
        for argv in argvs:
            for tool, version in uvx_pins(" ".join(map(str, argv)), where, problems):
                seen.add(tool, version, where)


def stub_maturin(source: str, problems: list[str]) -> str | None:
    """The version in `MATURIN = "maturin@X"`, read with stdlib `ast`."""
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        problems.append(f"maturin: {STUBS} doesn't parse: {error}")
        return None
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "MATURIN" for t in node.targets)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            name, _, version = node.value.value.partition("@")
            if name == "maturin" and EXACT.fullmatch(version):
                return version
            problems.append(
                f"maturin: {STUBS} sets MATURIN = {node.value.value!r}, not maturin@X.Y.Z"
            )
            return None
    problems.append(f"maturin: {STUBS} has no MATURIN assignment")
    return None


def pin(value: object) -> str:
    """A mise tool value, `"1.2.3"` or `{ version = "1.2.3", ... }`."""
    if isinstance(value, dict):
        return scalar(value.get("version", ""))
    return scalar(value)


def local_versions(
    mise: dict, stubs: str, problems: list[str]
) -> dict[str, str | None]:
    tools = mise.get("tools")
    if not isinstance(tools, dict) or not tools:
        problems.append("mise.toml has no [tools]")
        tools = {}
    local: dict[str, str | None] = {}
    for tool, key in MISE_KEYS.items():
        if key not in tools:
            problems.append(f"{tool}: mise.toml [tools] has no `{key}`")
            local[tool] = None
            continue
        version = pin(tools[key])
        shape = MAJOR if tool == "node" else EXACT
        if version == "latest":
            local[tool] = None  # check_latest reports it
            continue
        if not shape.fullmatch(version):
            problems.append(
                f"{tool}: mise.toml pins `{key}` = {version!r}, not an exact version"
            )
            local[tool] = None
            continue
        local[tool] = version
    local["maturin"] = stub_maturin(stubs, problems)
    return local


def check_latest(mise: dict, local: dict[str, str | None], problems: list[str]) -> None:
    """No `latest` anywhere, and a task's own pin of a compared tool is the `[tools]` one."""
    places = [("[tools]", mise.get("tools", {}))]
    for name, task in (mise.get("tasks") or {}).items():
        if isinstance(task, dict) and isinstance(task.get("tools"), dict):
            places.append((f"[tasks.{name!r}] tools", task["tools"]))
    tools_by_key = {key: tool for tool, key in MISE_KEYS.items()}
    for place, tools in places:
        for key, value in tools.items():
            if pin(value) == "latest":
                problems.append(f"mise.toml {place} pins `{key}` to latest")
                continue
            tool = tools_by_key.get(key)
            want = local.get(tool) if tool else None
            if place == "[tools]" or want is None:
                continue
            same = (
                major(pin(value)) == major(want)
                if tool == "node"
                else pin(value) == want
            )
            if not same:
                problems.append(
                    f"{tool}: mise.toml {place} pins `{key}` = {pin(value)!r}, and [tools] "
                    f"pins {want}, the version CI is compared with"
                )


def check_env(ci: dict, mise: dict, problems: list[str]) -> list[str]:
    ci_env = ci.get("env")
    if not isinstance(ci_env, dict) or not ci_env:
        problems.append(
            "ci.yml has no top-level env block, so there's nothing to compare"
        )
        return []
    mise_env = mise.get("env")
    if not isinstance(mise_env, dict):
        problems.append("mise.toml has no [env] table")
        mise_env = {}
    for key, value in ci_env.items():
        want = scalar(value)
        if key not in mise_env:
            problems.append(
                f"env: ci.yml sets {key}={want}, and mise.toml [env] has no {key}"
            )
        elif scalar(mise_env[key]) != want:
            problems.append(
                f"env: ci.yml sets {key}={want}, and mise.toml [env] sets "
                f"{key}={scalar(mise_env[key])}"
            )
    return list(ci_env)


def major(version: str) -> str | None:
    match = MAJOR.fullmatch(version)
    return match.group(1) if match else None


def check_tools(
    seen: Observed, local: dict[str, str | None], problems: list[str]
) -> None:
    for tool in TOOLS:
        runs = seen.tools[tool]
        if not runs and tool not in seen.unpinned:
            problems.append(
                f"{tool}: found nowhere in ci.yml, so there's nothing to compare the local "
                "pin with"
            )
            continue
        want = local.get(tool)
        if want is None:
            continue
        source = STUBS if tool == "maturin" else "mise.toml"
        for version, where in runs:
            if tool == "node":
                if major(version) != major(want):
                    problems.append(
                        f"node: {where} runs Node {version}, and mise.toml pins {want} "
                        "(majors compared)"
                    )
            elif version != want:
                problems.append(
                    f"{tool}: {where} runs {version}, and {source} pins {want}"
                )


def check_hashes(seen: Observed, lock: dict, problems: list[str]) -> None:
    locked = lock.get("tools") or {}
    for tool, version, digest, where in seen.hashes:
        if digest is None:
            problems.append(f"{tool}: {where} downloads {version} with no sha256 check")
            continue
        key = MISE_KEYS[tool]
        entry = next(
            (
                e
                for e in locked.get(key, [])
                if isinstance(e, dict) and e.get("version") == version
            ),
            None,
        )
        if entry is None:
            problems.append(
                f"{tool}: mise.lock has no `{key}` {version} (run `mise lock`)"
            )
            continue
        want = scalar((entry.get("platforms.linux-x64") or {}).get("checksum", ""))
        want = want.removeprefix("sha256:")
        if not want:
            problems.append(
                f"{tool}: mise.lock records no linux-x64 checksum for {version}"
            )
        elif want != digest:
            problems.append(
                f"{tool}: {where} checks sha256 {digest}, and mise.lock records {want} for "
                f"{version} on linux-x64"
            )


def check_rust(
    seen: Observed, mise: dict, toolchain: dict, problems: list[str]
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
    if not seen.rust:
        problems.append("rust: ci.yml has no dtolnay/rust-toolchain step")
    for version, where in seen.rust:
        if version != channel:
            problems.append(
                f"rust: {where} installs {version!r}, and {TOOLCHAIN} says {channel}"
            )
    return channel


# ── step coverage: every `run:` step has an entry in ci/local.toml ───────────


class Step(NamedTuple):
    """One step as `ci-local.py` runs it. `run` is None for a step it skips, and `note` says
    why; otherwise `note` is the reason it differs from the workflow, if it does."""

    label: str
    run: str | None
    shell: str | None
    cwd: str | None
    env: dict[str, str]
    unset: list[str]
    condition: str | None
    note: str | None


class Job(NamedTuple):
    workflow: str
    name: str
    task: str
    full_history: bool
    timeout_minutes: float | None
    env: dict[str, str]
    steps: list[Step]


def step_name(step: dict) -> str:
    """A step's label: its `name`, else the first line of its `run`, else its action."""
    if step.get("name"):
        return str(step["name"])
    lines = str(step.get("run") or "").strip().splitlines()
    if lines:
        return lines[0].strip()
    return str(step.get("uses", "unnamed")).split("@")[0]


def is_run(step: dict) -> bool:
    return "run" in step


def resolve(
    text: str, expressions: dict[str, str], where: str, problems: list[str]
) -> str:
    """`text` with each `${{ expr }}` replaced by its value in `[expressions]`."""

    def value(match: re.Match) -> str:
        expr = match.group(1)
        if expr not in expressions:
            problems.append(
                f"{where} uses `${{{{ {expr} }}}}`, which has no value in {LOCAL} [expressions]"
            )
            return match.group(0)
        return expressions[expr]

    return EXPRESSION.sub(value, text)


def condition(
    step: dict, expressions: dict[str, str], where: str, problems: list[str]
) -> str | None:
    """A step's `if`, looked up in `[expressions]`: "true", "false", or None for no `if`."""
    if "if" not in step:
        return None
    text = scalar(step["if"]).strip()
    match = EXPRESSION.fullmatch(text)
    expr = match.group(1) if match else text
    got = expressions.get(expr)
    if got not in ("true", "false"):
        problems.append(
            f"{where} runs `if: {text}`, and {LOCAL} [expressions] gives it no true or false "
            "value"
        )
        return None
    return got


def reason(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def plan_job(
    wf: str,
    job: str,
    body: dict,
    spec: dict,
    wf_env: dict[str, str],
    expressions: dict[str, str],
    actions: dict,
    used: set[str],
    problems: list[str],
) -> Job | None:
    where = f"{wf} job `{job}`"
    steps = [s for s in (body.get("steps") or []) if isinstance(s, dict)]
    labelled = [(step_name(s), s) for s in steps]
    labels = [label for label, _ in labelled]
    for label in sorted({label for label in labels if labels.count(label) > 1}):
        problems.append(
            f"{where} has more than one step labelled `{label}`; name them apart"
        )
    listed = spec.get("steps")
    if not isinstance(listed, list) or not all(isinstance(x, str) for x in listed):
        problems.append(f"{LOCAL}: {where} has no `steps` list")
        listed = []
    for label, step in labelled:
        if is_run(step) and label not in listed:
            problems.append(
                f"{where} step `{label}` has no entry in {LOCAL}, so ci:local wouldn't run "
                "it and nothing says why"
            )
    for label in listed:
        if label not in labels:
            problems.append(
                f"{LOCAL}: {where} lists `{label}`, which names no step there"
            )
    if [label for label in labels if label in listed] != [
        label for label in listed if label in labels
    ]:
        problems.append(
            f"{LOCAL}: {where} lists its steps in another order than {wf} runs them"
        )
    overrides = spec.get("local") or {}
    for label in overrides:
        if label not in listed:
            problems.append(
                f"{LOCAL}: {where} changes `{label}`, which its `steps` don't list"
            )
    if "skip" in spec:
        if not reason(spec["skip"]):
            problems.append(f"{LOCAL}: {where} is skipped and gives no reason")
        if "task" in spec:
            problems.append(f"{LOCAL}: {where} has both `skip` and `task`")
        return None
    task = spec.get("task")
    if not isinstance(task, str) or not TASK.fullmatch(task):
        problems.append(f"{LOCAL}: {where} names no `task` and isn't skipped")
        return None
    for key in sorted(set(map(str, body)) - JOB_KEYS):
        problems.append(
            f"{where} sets `{key}`, which ci:local doesn't model; skip the job with a "
            "reason, or teach scripts/ci-local.py the key"
        )
    runs_on = resolve(
        scalar(body.get("runs-on", "")), expressions, f"{where} runs-on", problems
    )
    if not runs_on.startswith("ubuntu"):
        problems.append(
            f"{where} runs on `{runs_on}`, and ci:local runs Linux jobs only; skip it "
            "with a reason"
        )
    # A `uses:` step it doesn't list runs nothing here, so it needs a reason too: a lint or
    # scanner shipped as an action would otherwise be CI-only while this check said covered.
    for label, step in labelled:
        if "uses" not in step or label in listed:
            continue
        action = str(step["uses"]).split("@")[0]
        used.add(action)
        if not reason(actions.get(action)):
            problems.append(
                f"{where} step `{label}` runs the action `{action}`, which {LOCAL} "
                "neither lists with a local `run` nor names in [actions] with a reason, "
                "so ci:local wouldn't run it and nothing says why"
            )
    checkout = next(
        (
            s
            for s in steps
            if str(s.get("uses", "")).split("@")[0] == "actions/checkout"
        ),
        None,
    )
    if checkout is None:
        problems.append(f"{where} has no actions/checkout step")
    inputs = (checkout or {}).get("with") or {}
    full = scalar(inputs.get("fetch-depth", "1")) == "0"
    env = dict(wf_env)
    for key, value in (body.get("env") or {}).items():
        env[key] = resolve(scalar(value), expressions, f"{where} env {key}", problems)
    defaults = (body.get("defaults") or {}).get("run") or {}
    planned: list[Step] = []
    for label, step in labelled:
        if label not in listed:
            continue
        at = f"{where} step `{label}`"
        change = overrides.get(label) or {}
        if not isinstance(change, dict):
            problems.append(f"{LOCAL}: {at}: `local` entry isn't a table")
            change = {}
        for key in sorted(set(change) - OVERRIDES):
            problems.append(f"{LOCAL}: {at}: unknown key `{key}`")
        for key in sorted(set(map(str, step)) - STEP_KEYS):
            problems.append(f"{at} sets `{key}`, which ci:local doesn't model")
        if "skip" in change:
            if not reason(change["skip"]):
                problems.append(f"{LOCAL}: {at} is skipped and gives no reason")
            planned.append(
                Step(label, None, None, None, {}, [], None, str(change["skip"]))
            )
            continue
        if {"run", "env", "unset"} & set(change) and not reason(change.get("reason")):
            problems.append(f"{LOCAL}: {at} runs differently here and gives no reason")
        run = change.get("run", step.get("run"))
        if run is None:
            problems.append(
                f"{LOCAL}: {at} is a `uses:` step, and {LOCAL} gives no local `run` for it"
            )
            continue
        shell = step.get("shell", defaults.get("shell"))
        if shell not in (None, "bash"):
            problems.append(f"{at} uses shell `{shell}`; ci:local runs bash only")
        step_env = {
            key: resolve(scalar(value), expressions, f"{at} env {key}", problems)
            for key, value in (step.get("env") or {}).items()
        }
        step_env.update(
            {key: scalar(v) for key, v in (change.get("env") or {}).items()}
        )
        cwd = step.get("working-directory", defaults.get("working-directory"))
        planned.append(
            Step(
                label=label,
                run=resolve(str(run), expressions, at, problems),
                shell=shell,
                cwd=resolve(str(cwd), expressions, at, problems) if cwd else None,
                env=step_env,
                unset=[str(k) for k in change.get("unset", [])],
                condition=condition(step, expressions, at, problems),
                note=change.get("reason"),
            )
        )
    timeout = body.get("timeout-minutes")
    return Job(
        workflow=wf,
        name=job,
        task=task,
        full_history=full,
        timeout_minutes=float(timeout) if isinstance(timeout, (int, float)) else None,
        env=env,
        steps=planned,
    )


def plan(
    root: Path, local: dict, problems: list[str], paths: dict[str, Path] | None = None
) -> list[Job]:
    """Every job `ci-local.py` runs, in workflow order. Problems go to `problems`. `paths`
    overrides where a workflow is read from (`--ci`)."""
    workflows = local.get("workflows")
    if not isinstance(workflows, list) or not workflows:
        problems.append(f"{LOCAL} names no `workflows`")
        return []
    if "ci.yml" not in workflows:
        problems.append(f"{LOCAL} doesn't name ci.yml in `workflows`")
    expressions = {
        str(k): scalar(v) for k, v in (local.get("expressions") or {}).items()
    }
    actions = local.get("actions") or {}
    if not isinstance(actions, dict):
        problems.append(f"{LOCAL}: [actions] isn't a table")
        actions = {}
    used: set[str] = set()
    specs = local.get("job") or {}
    for wf in specs:
        if wf not in workflows:
            problems.append(
                f"{LOCAL} has jobs for `{wf}`, which `workflows` doesn't name"
            )
    jobs: list[Job] = []
    for wf in workflows:
        try:
            data = load_yaml((paths or {}).get(wf, root / WORKFLOWS / wf), wf)
        except Unreadable as error:
            problems.append(str(error))
            continue
        for key in sorted(set(map(str, data)) - WORKFLOW_KEYS):
            problems.append(
                f"{wf} sets top-level `{key}`, which ci:local doesn't model"
            )
        wf_jobs = data.get("jobs")
        if not isinstance(wf_jobs, dict) or not wf_jobs:
            problems.append(f"{wf} has no jobs")
            continue
        wf_env = {
            str(k): resolve(scalar(v), expressions, f"{wf} env {k}", problems)
            for k, v in (data.get("env") or {}).items()
        }
        wf_specs = specs.get(wf) or {}
        runs = 0
        for job, body in wf_jobs.items():
            body = body if isinstance(body, dict) else {}
            runs += sum(
                1 for s in body.get("steps") or [] if isinstance(s, dict) and is_run(s)
            )
            spec = wf_specs.get(job)
            if spec is None:
                problems.append(
                    f"{wf} job `{job}` has no entry in {LOCAL}, so ci:local wouldn't run it "
                    "and nothing says why"
                )
                continue
            planned = plan_job(
                wf, job, body, spec, wf_env, expressions, actions, used, problems
            )
            if planned:
                jobs.append(planned)
        for job in wf_specs:
            if job not in wf_jobs:
                problems.append(
                    f"{LOCAL} has an entry for {wf} job `{job}`, which {wf} doesn't have"
                )
        if not runs:
            problems.append(f"{wf}: found no `run:` steps, so there's nothing to cover")
    for action in sorted(set(actions) - used):
        problems.append(
            f"{LOCAL}: [actions] names `{action}`, which no step ci:local runs uses"
        )
    return jobs


def check_tasks(jobs: list[Job], mise: dict, problems: list[str]) -> list[str]:
    """Each planned task has its `ci:<task>` mise task, and `ci:local` depends on them all."""
    tasks = mise.get("tasks") or {}
    names = sorted({job.task for job in jobs})
    for name in names:
        task = tasks.get(f"ci:{name}")
        runs = task.get("run") if isinstance(task, dict) else None
        runs = runs if isinstance(runs, list) else [runs]
        if not any(f"scripts/ci-local.py {name}" in str(run) for run in runs):
            problems.append(
                f"mise.toml has no `ci:{name}` task running `./scripts/ci-local.py {name}`"
            )
    everything = tasks.get("ci:local")
    depends = everything.get("depends", []) if isinstance(everything, dict) else []
    for name in names:
        if f"ci:{name}" not in depends:
            problems.append(f"mise.toml: `ci:local` doesn't depend on `ci:{name}`")
    gate = tasks.get("check")
    for dep in gate.get("depends", []) if isinstance(gate, dict) else []:
        if dep == "ci:local" or dep in {f"ci:{name}" for name in names}:
            problems.append(
                f"mise.toml: `check` depends on `{dep}`, which builds the tree once per CI job"
            )
    return names


def check(
    root: Path, ci_path: Path, mise_path: Path, local_path: Path
) -> tuple[list[str], str]:
    """The problems found, and a one-line summary of what was compared."""
    try:
        ci = load_yaml(ci_path, "ci.yml")
        mise = load_toml(mise_path, "mise.toml")
        lock = load_toml(root / LOCK, LOCK)
        toolchain = load_toml(root / TOOLCHAIN, TOOLCHAIN)
        registry = load_toml(root / REGISTRY, REGISTRY)
        stubs = read_text(root / STUBS, STUBS)
        local = load_toml(local_path, LOCAL)
    except Unreadable as error:
        return [str(error)], ""
    problems: list[str] = []
    keys = check_env(ci, mise, problems)
    seen = read_ci(ci, problems)
    read_registry(registry, seen, problems)
    versions = local_versions(mise, stubs, problems)
    check_latest(mise, versions, problems)
    check_tools(seen, versions, problems)
    check_hashes(seen, lock, problems)
    channel = check_rust(seen, mise, toolchain, problems)
    jobs = plan(root, local, problems, {"ci.yml": ci_path})
    tasks = check_tasks(jobs, mise, problems)
    summary = (
        f"env {', '.join(keys)}; "
        + ", ".join(f"{tool} {versions.get(tool)}" for tool in TOOLS)
        + f"; rust {channel}; steps of {', '.join(local.get('workflows') or [])} covered, "
        + f"ci:local runs {', '.join(f'ci:{t}' for t in tasks)}"
    )
    return problems, summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", help="the repository (default: this script's)")
    parser.add_argument("--ci", help=f"the workflow to read (default: <root>/{CI})")
    parser.add_argument(
        "--mise", help=f"the mise config to read (default: <root>/{MISE})"
    )
    parser.add_argument(
        "--local", help=f"the ci:local plan to read (default: <root>/{LOCAL})"
    )
    args = parser.parse_args(argv)
    root = Path(args.root) if args.root else Path(__file__).resolve().parents[1]
    ci_path = Path(args.ci) if args.ci else root / CI
    mise_path = Path(args.mise) if args.mise else root / MISE
    local_path = Path(args.local) if args.local else root / LOCAL
    problems, summary = check(root, ci_path, mise_path, local_path)
    if problems:
        print("ci parity: FAILED")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"ci parity: OK: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
