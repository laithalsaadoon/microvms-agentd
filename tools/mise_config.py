# SPDX-License-Identifier: Apache-2.0
"""mise's tasks as mise reads them: mise.toml's own, and those of each file its includes name.

A module, not a script: every gate that reads the tasks imports it (`check-agents-md.py`,
`check-ci-parity.py`, `check-live-wiring.py`, `check-publishable.py`, `ci-local.py` and
`test_check_targets.py`), so a task reads the same to each of them wherever it's defined. It
parses the TOML with `tomllib` rather than asking `mise tasks ls --json`, because CI's jobs run
those gates without mise (D14).

mise.toml holds `[tools]`, `[settings]`, `[env]`, `[vars]` and `[task_config]`, and its
`includes` name the task files, `.config/mise/tasks/<area>.toml`. An include is a path or a
glob relative to mise.toml's directory, and an included file holds one top-level table per
task. mise resolves an included task's `dir`, `sources` and `{{config_root}}` from mise.toml's
directory, as it does a task written in mise.toml, so a task's paths don't change with its
file (measured on mise 2026.8.1).

mise reads an include that matches nothing, and a task two files define, without a word: the
first reads no task, and the second runs whichever definition mise read last. A gate reading
either one would hold the tree to something mise doesn't run, so `load` refuses both, and
everything else it can't read the way mise does, each by file and line:

- an include that matches no file (a misspelled glob drops every task it held);
- a task defined twice, in mise.toml or any included file;
- an include that isn't a path in this tree (remote, absolute, or outside it), or that names a
  directory: a directory holds file tasks, which are scripts, not TOML;
- with no `[task_config] includes`, a default task directory (`DEFAULT_TASK_DIRS`) with a
  file in it, since mise reads that directory then;
- a file that doesn't parse, or a top-level value in an included file (or a `[tasks]` entry)
  that isn't a task table.
"""

from __future__ import annotations

import glob
import posixpath
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

MISE = "mise.toml"
# The directories mise reads tasks from when `[task_config] includes` isn't set.
DEFAULT_TASK_DIRS = (
    "mise-tasks",
    ".mise-tasks",
    "mise/tasks",
    ".mise/tasks",
    ".config/mise/tasks",
)
# A table header on a line of its own, `[tasks.lint]` or `["ci:rust"]`; `[[x]]` isn't one.
HEADER = re.compile(r"^\s*\[(?!\[)([^\]#]+)\]\s*(?:#.*)?$")
BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")


class Unreadable(ValueError):
    """What makes mise's config read differently here than in mise, one problem a line."""

    def __init__(self, problems: list[str]):
        super().__init__("\n".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Task:
    """One task: its name, the file that defines it, the line its table opens on, the table."""

    name: str
    file: str
    line: int
    table: dict

    @property
    def header(self) -> str:
        """The header that opens the table, in the file's own shape."""
        key = self.name if BARE_KEY.fullmatch(self.name) else f'"{self.name}"'
        return f"[tasks.{key}]" if self.file == MISE else f"[{key}]"


@dataclass(frozen=True)
class Include:
    """One `[task_config] includes` entry, its line in mise.toml, and the files it matched."""

    pattern: str
    line: int
    files: tuple[str, ...]


@dataclass(frozen=True)
class Config:
    """mise.toml's tables, its includes, and every task by name."""

    data: dict
    includes: tuple[Include, ...]
    tasks: dict[str, Task]

    @property
    def files(self) -> list[str]:
        """mise.toml, then each included file in the order mise reads them."""
        out = [MISE]
        for include in self.includes:
            out += [f for f in include.files if f not in out]
        return out

    def tables(self) -> dict[str, dict]:
        """Each task's table, by name."""
        return {name: task.table for name, task in self.tasks.items()}


def load(root: Path, path: Path | None = None) -> Config:
    """The config at `path` (mise.toml under `root` by default); includes resolve from `root`."""
    path = path or root / MISE
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise Unreadable([f"{MISE}: {path} can't be read: {error.strerror}"]) from None
    return parse(root, text)


def parse(root: Path, text: str) -> Config:
    """mise.toml's `text`, with the files its includes name read from under `root`."""
    problems: list[str] = []
    data = parse_toml(MISE, text, problems)
    if data is None:
        raise Unreadable(problems)
    tasks: dict[str, Task] = {}
    lines = header_lines(text)
    own = data.get("tasks", {})
    if not isinstance(own, dict):
        problems.append(f"{MISE}: `tasks` isn't a table")
        own = {}
    for name, table in own.items():
        line = lines.get(("tasks", name), lines.get(("tasks",), 1))
        add(tasks, Task(name, MISE, line, table), problems)
    includes = read_includes(root, text, data, problems)
    for include in includes:
        for file in include.files:
            included = (root / file).read_text(encoding="utf-8")
            table = parse_toml(file, included, problems)
            if table is None:
                continue
            at = header_lines(included)
            for name, value in table.items():
                add(tasks, Task(name, file, at.get((name,), 1), value), problems)
    if problems:
        raise Unreadable(problems)
    return Config(data, tuple(includes), tasks)


def parse_toml(file: str, text: str, problems: list[str]) -> dict | None:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        problems.append(f"{file}: doesn't parse as TOML: {error}")
        return None


def add(tasks: dict[str, Task], task: Task, problems: list[str]) -> None:
    where = f"{task.file}:{task.line}"
    if not isinstance(task.table, dict):
        problems.append(
            f"{where}: `{task.name}` is a {type(task.table).__name__}, not a task table"
        )
        return
    if (first := tasks.get(task.name)) is not None:
        problems.append(
            f"{where}: task `{task.name}` is also defined at {first.file}:{first.line}, and"
            " mise runs the one it reads last without a word; define it once"
        )
        return
    tasks[task.name] = task


def read_includes(
    root: Path, text: str, data: dict, problems: list[str]
) -> list[Include]:
    """Each include and the files it matches, or the default directories' refusal."""
    config = data.get("task_config", {})
    patterns = config.get("includes") if isinstance(config, dict) else None
    if patterns is None:
        for directory in DEFAULT_TASK_DIRS:
            if (root / directory).is_dir() and any(
                p.is_file() for p in (root / directory).rglob("*")
            ):
                problems.append(
                    f"{MISE}: mise reads tasks from `{directory}/` when `[task_config]"
                    " includes` isn't set, and the gates don't; name its TOML files in"
                    " `includes`"
                )
        return []
    start = header_lines(text).get(("task_config",), 1)
    if not isinstance(patterns, list):
        problems.append(f"{MISE}:{start}: `[task_config] includes` isn't a list")
        return []
    out: list[Include] = []
    seen: set[str] = set()
    for pattern in patterns:
        line = line_after(text, start, str(pattern))
        where = f"{MISE}:{line}"
        if not isinstance(pattern, str) or not in_tree(pattern):
            problems.append(
                f"{where}: the include `{pattern}` isn't a path in this tree"
            )
            continue
        matched = sorted(
            glob.glob(pattern, root_dir=root, recursive=True, include_hidden=True)
        )
        if not matched:
            problems.append(
                f"{where}: the include `{pattern}` matches no file, so mise reads no task"
                " from it"
            )
            continue
        for name in matched:
            if (root / name).is_dir():
                problems.append(
                    f"{where}: the include `{pattern}` names the directory `{name}`, whose"
                    " file tasks the gates don't read; name its TOML files instead"
                )
        # A file two includes match is read twice by mise, the second time to the same end.
        files = [
            posixpath.normpath(n)
            for n in matched
            if (root / n).is_file() and posixpath.normpath(n) not in seen
        ]
        seen.update(files)
        out.append(Include(pattern, line, tuple(files)))
    return out


def in_tree(pattern: str) -> bool:
    """A relative path that stays under the root: not remote, absolute or `..`-escaping."""
    if "::" in pattern or "://" in pattern or pattern.startswith(("/", "~")):
        return False
    normal = posixpath.normpath(pattern)
    return normal != ".." and not normal.startswith("../")


def header_lines(text: str) -> dict[tuple[str, ...], int]:
    """Each table header's key path and the line it's on (from 1)."""
    out: dict[tuple[str, ...], int] = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not (match := HEADER.match(line)):
            continue
        try:
            table = tomllib.loads(f"[{match.group(1)}]\n")
        except tomllib.TOMLDecodeError:
            continue
        path: list[str] = []
        while isinstance(table, dict) and len(table) == 1:
            key, table = next(iter(table.items()))
            path.append(key)
        out.setdefault(tuple(path), number)
    return out


def line_after(text: str, start: int, needle: str) -> int:
    """The first line at or after `start` that holds `needle`, or `start`."""
    lines = text.splitlines()
    for number in range(start, len(lines) + 1):
        if needle in lines[number - 1]:
            return number
    return start
