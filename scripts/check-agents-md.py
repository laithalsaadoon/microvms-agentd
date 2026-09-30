#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
# SPDX-License-Identifier: Apache-2.0
"""Every name the docs cite, and every path the hooks, tasks and workflows name, exists.

AGENTS.md states each rule with the check that enforces it, so a rule whose check was renamed
or deleted reads as enforced and isn't (#278). This reads the root AGENTS.md, every AGENTS.md below
it, CONTRIBUTING.md and the pull request template, and fails on a reference that resolves to
nothing. What counts as a reference, all inside backticks:

- `mise run <task>`, in an inline span or a fenced block, and a bare task name with a colon
  (`guards:list`), against the tasks of mise.toml and of the TOML files its
  `[task_config] includes` names (`mise_task_files`). A name holding a placeholder
  (`ci:<job>`) isn't one, and a bare name that isn't a task and starts with a mise tool
  backend or Node's module scheme (`cargo:cargo-mutants`, `node:test`) is that, not a task.
- A task followed by "in `check`" or "in `mise run check`", against the tasks `check`
  depends on, directly or through a task it depends on. The doc says the local gate runs it.
- The id after `--only` and after `fired:` (`guards:fire -- --only agentd-fs-pop`), against
  the `id` of each `[[fault]]` in the registry's files (guards/faults/*.toml), read by
  check-guards-fire.py's loader, so this and `guards:list` can't read different entries.
- A word with a `/` in it (`guards/unregistered.txt`, `src/lib.rs`, `site/authored/`), inline
  only, against the tracked and untracked files git knows, resolved from the doc's own
  directory first and then the root. A `:line` or `:line:col` suffix is dropped first. A glob
  (`docs/*.md`) must match some file. A path .gitignore names (the live tier's Terraform
  state) is a local file on purpose and passes. Absolute paths, URLs, flags and package specs
  (`@napi-rs/cli@3`) aren't paths.
- A bare file name (`microvms.pyi`, `Cargo.toml`), against the names of the files in the tree:
  some file anywhere has it. That's all a bare name can promise, since `Cargo.toml` names one
  in every crate.
- A lowercase snake_case identifier (`normalize_rejects_escapes_and_absorbs_benign_traversal`),
  against the words of every file in the tree except the docs themselves: a test, function or
  key the docs name must still be spelled somewhere else.
- `` `<name>` job ``, against the job ids and display names in .github/workflows/ci.yml.
- `Results.<name>` or `results.<name>`, with or without call parentheses, against the methods
  of `Results` in conformance/harness/results.py, read with stdlib `ast`.

A check over nothing reports nothing, so it also fails when the docs, the tasks, the jobs, the
fault ids or the `Results` methods come back empty, when no reference of some kind was found
at all (an extractor that stopped matching reads that way), when a doc yields no reference or
the root AGENTS.md lacks one of the kinds its rules use, when a fence is never closed (the
rest of that doc would read as code), when the root AGENTS.md isn't among the docs, and when
the sentinel `mise run check` isn't among the task references. The job names are read with a
regex over ci.yml's `jobs:` block; the job ids sit at a fixed indent that actionlint already
holds.

The path census holds the files that run things to the same standard, since a stale path there
doesn't fail: a hook's glob that matches nothing turns the hook off, and a script a task names
fails only when someone runs the task. Each path must match a tracked or new file or directory:

- lefthook.yml: each job's and command's `glob` and `exclude`, one brace alternative at a time
  under lefthook's own matcher (`glob_regex`), its `root`, and the paths its `run` and `files`
  commands name;
- mise.toml and each TOML file its `[task_config] includes` names, read with `tomllib` rather
  than `mise tasks ls`, since CI's `security` job runs this without mise: every task's `dir`,
  `file`, `sources` and `outputs`, and the paths its `run` names, relative to its `dir`;
- each workflow: `paths` and `paths-ignore`, under GitHub's filter syntax; every
  `working-directory`; a local `uses`; and the paths each step's `run` and `with` values name,
  relative to the step's working directory;
- .github/dependabot.yml: each update's `directory` and `directories`;
- each gate script's module-level path constants (`constant_names` has the rule).

A path in a command is a word `run_words` reads as one. A path git ignores (a build output, the
live tier's state) passes, except in a hook's or a workflow's filter, which never sees an
ignored file. A word that names nothing here on purpose (a Semgrep ruleset, a directory a job
creates) is in `CENSUS_NOT_PATHS` with what it is, and an entry no source names fails. Each
source has a floor, naming no path at all, and a sentinel it always names (`CENSUS_SENTINELS`),
and a construct the YAML reader doesn't take fails by file and line rather than reading as
something else. The reader (`load_yaml`) is stdlib, so this stays offline and needs no install.

It also holds each decision id the tree cites, such as `(D14)` in a comment, to a table in
docs/decisions.toml (`decision_problems`), and fails when the register defines nothing, when
the tree cites no id, or when the sentinel `D35` isn't cited.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import posixpath
import re
import runpy
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DOC_PATHSPECS = (
    "AGENTS.md",
    "*/AGENTS.md",
    "CONTRIBUTING.md",
    ".github/PULL_REQUEST_TEMPLATE.md",
)
# The file every rule starts from. A doc set without it came from the wrong tree.
ROOT_DOC = "AGENTS.md"
MISE = "mise.toml"
WORKFLOW = ".github/workflows/ci.yml"
# The fault registry's loader is check-guards-fire.py's: its files, their order and what makes
# one unreadable are that script's to say.
GUARDS = runpy.run_path(str(Path(__file__).with_name("check-guards-fire.py")))
REGISTRY = GUARDS["REGISTRY"]
# The class whose helpers the docs cite by name, and the file it lives in.
SYMBOL_CLASS = "Results"
SYMBOL_FILE = "conformance/harness/results.py"
# A reference every healthy tree has, so a pass means the task extractor and mise.toml's
# parser both worked on this tree. It's also the task "in `check`" means.
SENTINEL = "check"
KINDS = ("task", "member", "fault", "path", "file", "identifier", "job", "symbol")
# The kinds the root guide's own rules name. Losing one there means its text stopped being
# read, which the doc-set floor can't see while another doc still names that kind.
ROOT_KINDS = ("task", "member", "path", "job", "symbol")

# A fence opens with three or more backticks or tildes, and closes on a line of the same
# character at least as long, with nothing after it.
FENCE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
# An inline span: a run of backticks, its text, the same run. It may wrap a line, but a blank
# line ends the paragraph it's in.
SPAN = re.compile(r"(`+)(?!`)((?:(?!\n\s*\n).)+?)(?<!`)\1(?!`)", re.DOTALL)
MISE_RUN = re.compile(r"\bmise run (\S+)")
ONLY = re.compile(r"(?:--only[ =]|\bfired: )(\S+)")
BARE_TASK = re.compile(r"^[a-z][a-z0-9-]*(?::[a-z0-9-]+)+$")
# A bare `a:b` with one of these prefixes that isn't a task is a mise tool key or a Node
# module specifier. Every mise backend is listed, since the tools table can gain any of them.
NOT_TASK_PREFIXES = frozenset(
    "aqua asdf cargo conda core dotnet gem github gitlab go http npm pipx spm ubi vfox"
    " node".split()
)
JOB_AFTER = re.compile(r"^\s+jobs?\b")
MEMBER_AFTER = re.compile(rf"^\s+in\s+`(?:mise run )?{SENTINEL}`")
# The class itself or the instance a check calls it through (`results.eq`).
SYMBOL = re.compile(rf"^(?:{SYMBOL_CLASS}|{SYMBOL_CLASS.lower()})\.\w+$")
CALL = re.compile(r"\(.*\)$")
LINE_SUFFIX = re.compile(r":\d+(?::\d+)?:?$")
FILE_NAME = re.compile(r"^[A-Za-z0-9_-][\w.-]*\.[a-z]{1,5}$")
IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*_[a-z0-9_]*$")
WORD = re.compile(r"\w+")
SLASH_COMMENTS = (".rs", ".ts", ".mts", ".js", ".mjs", ".cjs")
PLACEHOLDER = set("<>{}$*?[]")
# Names shaped like a path or a file that aren't one in this tree, with what each is. An
# entry the docs no longer name fails, so the list can't outlive its reason.
NOT_PATHS = {
    "rust/hard-coded-cryptographic-value": "a CodeQL query id (microvms-app/AGENTS.md)",
    "mutants.out": "cargo-mutants' output directory, gitignored (CONTRIBUTING.md)",
    "missed.txt": "a file cargo-mutants writes into mutants.out (CONTRIBUTING.md)",
    "timeout.txt": "a file cargo-mutants writes into mutants.out (CONTRIBUTING.md)",
}
NOT_A_PATH = set(":@=<>{}$[]|&;()")
GLOB = set("*?")
JOB_ID = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$")
JOB_NAME = re.compile(r"^    name:\s*(.+?)\s*$")


@dataclass(frozen=True)
class Ref:
    kind: str
    text: str
    doc: str
    line: int
    # How the doc spelled it, for the message: `mise run guards:fire` or `guards:list`.
    shown: str = ""


def git(
    root: Path, *args: str, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


def listed(root: Path, *pathspecs: str) -> list[str]:
    """Tracked and untracked-but-not-ignored files, so a new file in a worktree counts."""
    out = git(
        root, "ls-files", "--cached", "--others", "--exclude-standard", "--", *pathspecs
    )
    if out.returncode != 0:
        raise SystemExit(
            f"agents:check: `git ls-files` failed in {root}: {out.stderr.strip()}"
        )
    return sorted({line for line in out.stdout.splitlines() if line})


def read(root: Path, doc: str) -> str:
    return (root / doc).read_text(encoding="utf-8")


@dataclass(frozen=True)
class Span:
    text: str
    line: int
    fenced: bool
    # The span is followed by "job", so it names a CI job.
    job: bool = False
    # The span is followed by "in `check`", so it names a task the gate runs.
    member: bool = False


def spans(text: str) -> tuple[list[Span], int | None]:
    """Each backticked span and each line of a fenced block, and the line of a fence left open."""
    found: list[Span] = []
    prose: list[str] = []
    fence: tuple[str, int, int] | None = None
    for number, line in enumerate(text.splitlines(), start=1):
        mark = FENCE.match(line)
        if fence is None and mark:
            fence = (mark.group(1)[0], len(mark.group(1)), number)
            prose.append("")
        elif fence is None:
            prose.append(line)
        elif (
            mark
            and mark.group(1)[0] == fence[0]
            and len(mark.group(1)) >= fence[1]
            and not mark.group(2).strip()
        ):
            fence = None
            prose.append("")
        else:
            found.append(Span(line, number, fenced=True))
            prose.append("")
    joined = "\n".join(prose)
    for match in SPAN.finditer(joined):
        line = joined.count("\n", 0, match.start()) + 1
        after = joined[match.end() :]
        found.append(
            Span(
                " ".join(match.group(2).split()),
                line,
                False,
                job=bool(JOB_AFTER.match(after)),
                member=bool(MEMBER_AFTER.match(after)),
            )
        )
    return found, fence[2] if fence else None


def references(doc: str, found: list[Span]) -> list[Ref]:
    refs: list[Ref] = []
    for span in found:
        body, line = span.text, span.line
        if span.job:
            refs.append(Ref("job", body, doc, line))
            continue
        for match in MISE_RUN.finditer(body):
            task = match.group(1).rstrip(".,;)")
            if not PLACEHOLDER & set(task):
                refs.append(Ref("task", task, doc, line, f"mise run {task}"))
                if span.member and match.group(0) == body.strip():
                    refs.append(Ref("member", task, doc, line))
        for match in ONLY.finditer(body):
            for fault in match.group(1).rstrip(".,;)\"'").split(","):
                if fault and not PLACEHOLDER & set(fault):
                    refs.append(Ref("fault", fault, doc, line))
        if span.fenced:
            continue
        if BARE_TASK.match(body):
            refs.append(Ref("task", body, doc, line, body))
            if span.member:
                refs.append(Ref("member", body, doc, line))
        bare = CALL.sub("", body)
        if SYMBOL.match(bare):
            refs.append(Ref("symbol", bare, doc, line))
            continue
        for word in body.split():
            word = LINE_SUFFIX.sub("", word.strip("\"'").rstrip(".,;"))
            if word in NOT_PATHS:
                continue
            if SYMBOL.match(CALL.sub("", word)):
                refs.append(Ref("symbol", CALL.sub("", word), doc, line))
            elif is_path(word):
                refs.append(Ref("path", word.removeprefix("./"), doc, line))
            elif FILE_NAME.match(word):
                refs.append(Ref("file", word, doc, line))
            elif IDENTIFIER.match(CALL.sub("", word)):
                refs.append(Ref("identifier", CALL.sub("", word), doc, line))
    return refs


def is_path(word: str) -> bool:
    """A repo-relative path or glob: it has a `/`, and nothing that makes it a URL or flag."""
    return (
        "/" in word
        and not word.startswith(("-", "/", "~", "../"))
        and not NOT_A_PATH & set(word)
    )


def mise_tasks(root: Path) -> dict[str, dict]:
    """mise.toml's tasks and those of the TOML files its `[task_config] includes` names."""
    tasks, _, _ = mise_task_files(root)
    return {name: task for _, name, task in tasks}


def gated(tasks: dict[str, dict], top: str) -> set[str]:
    """Every task `top` depends on, directly or through another task."""
    seen: set[str] = set()
    stack = [top]
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        depends = tasks.get(name, {}).get("depends", [])
        if isinstance(depends, str):
            depends = [depends]
        # A dependency may carry arguments (`"build --release"`); the task is the first word.
        stack += [d.split()[0] for d in depends if d.split()]
    seen.discard(top)
    return seen


def fault_ids(root: Path) -> set[str]:
    """The ids the registry's entries carry. A registry file the loader refuses is
    `guards:list`'s to report; the ids of the others still count here."""
    tables, _ = GUARDS["registry_tables"](root)
    return {t.data["id"] for t in tables if isinstance(t.data, dict) and "id" in t.data}


def ci_jobs(root: Path) -> set[str]:
    """The job ids and display names under the workflow's `jobs:` key."""
    path = root / WORKFLOW
    if not path.is_file():
        return set()
    names: set[str] = set()
    in_jobs = False
    for line in path.read_text(encoding="utf-8").splitlines():
        # A blank line or a column-0 comment inside the block doesn't end it; the next
        # top-level key does.
        if not line.strip() or line.startswith("#"):
            continue
        if not line.startswith(" "):
            in_jobs = line.rstrip() == "jobs:"
            continue
        if not in_jobs:
            continue
        if job := JOB_ID.match(line):
            names.add(job.group(1))
        elif name := JOB_NAME.match(line):
            names.add(name.group(1).strip("\"'"))
    return names


def results_methods(root: Path) -> set[str] | None:
    """The methods of `Results`, or None when the class isn't there."""
    path = root / SYMBOL_FILE
    if not path.is_file():
        return None
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=SYMBOL_FILE)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == SYMBOL_CLASS:
            return {
                item.name
                for item in node.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
    return None


def spelled(root: Path, words: set[str], docs: list[str]) -> set[str]:
    """Which of `words` some file git knows spells as code, the docs left out.

    A doc example is no evidence that a name exists, so prose doesn't count: Markdown, Python
    docstrings and comments, `//` comments, and a fault's `transform`, which is seeded broken
    text. Otherwise a test renamed in its source and its registry entry would still resolve
    through the stale example in check-guards-fire.py's docstring.
    """
    if not words:
        return set()
    patterns = [arg for word in sorted(words) for arg in ("-e", word)]
    excluded = [f":(exclude){doc}" for doc in docs] + [":(exclude)*.md"]
    out = git(
        root, "grep", "--untracked", "-I", "-l", "-w", "-F", *patterns,
        "--", ".", *excluded,
    )  # fmt: skip
    # Exit 1 means nothing matched; anything past 1 is git failing.
    if out.returncode > 1:
        raise SystemExit(f"agents:check: `git grep` failed: {out.stderr.strip()}")
    found: set[str] = set()
    for name in out.stdout.splitlines():
        path = root / name
        if name and path.is_file():
            text = path.read_text(encoding="utf-8")
            found |= set(WORD.findall(code_text(name, text)))
    return found & words


def code_text(name: str, text: str) -> str:
    """The part of a file that isn't prose about the code."""
    if GUARDS["registry_file"](name):
        tables, _ = GUARDS["parse_registry"]({name: text})
        return repr(
            [
                {k: v for k, v in t.data.items() if k != "transform"}
                for t in tables
                if isinstance(t.data, dict)
            ]
        )
    if name.endswith(".py"):
        return python_code(text)
    if name.endswith(SLASH_COMMENTS):
        return "\n".join(line.split("//", 1)[0] for line in text.splitlines())
    return text


def python_code(text: str) -> str:
    """Names and string values, without comments or docstrings."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return text
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        )
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    parts: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                parts.append(node.value)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            parts.append(node.name)
        elif isinstance(node, ast.Name):
            parts.append(node.id)
        elif isinstance(node, ast.Attribute):
            parts.append(node.attr)
        elif isinstance(node, (ast.arg, ast.keyword)) and node.arg:
            parts.append(node.arg)
        elif isinstance(node, ast.alias):
            parts.append(node.name)
    return "\n".join(parts)


def ignored(root: Path, paths: list[str]) -> set[str]:
    if not paths:
        return set()
    out = git(root, "check-ignore", "--stdin", stdin="\n".join(paths) + "\n")
    # Exit 1 means none matched; anything else past 1 is git failing.
    if out.returncode > 1:
        raise SystemExit(
            f"agents:check: `git check-ignore` failed: {out.stderr.strip()}"
        )
    return {line for line in out.stdout.splitlines() if line}


def resolve_paths(root: Path, refs: list[Ref], present: set[str]) -> list[str]:
    dirs = {p for f in present for p in (str(q) + "/" for q in Path(f).parents)}

    def exists(candidate: str) -> bool:
        if GLOB & set(candidate):
            return any(fnmatch.fnmatchcase(f, candidate) for f in present | dirs)
        return candidate in present or candidate.rstrip("/") + "/" in dirs

    unresolved: list[tuple[Ref, list[str]]] = []
    for ref in refs:
        here = str(Path(ref.doc).parent)
        candidates = [ref.text] if here == "." else [f"{here}/{ref.text}", ref.text]
        if not any(exists(c) for c in candidates):
            unresolved.append((ref, candidates))
    local = ignored(
        root, sorted({c for _, cs in unresolved for c in cs if not GLOB & set(c)})
    )
    return [
        f"{ref.doc}:{ref.line}: `{ref.text}` "
        + ("matches no file" if GLOB & set(ref.text) else "is no such path")
        + " in this tree"
        for ref, candidates in unresolved
        if not local & set(candidates)
    ]


# ── the path census: what the hooks, tasks, workflows, dependabot and scripts name ─────────


class YamlSubsetError(ValueError):
    """A construct the census's YAML reader doesn't take, named with its file and line."""


class Scalar(str):
    """A YAML scalar's text, and the line it starts on for the message that names it."""

    line: int

    def __new__(cls, text: str, line: int) -> Scalar:
        made = super().__new__(cls, text)
        made.line = line
        return made


def load_yaml(text: str, name: str) -> object:
    """Block mappings and sequences of plain, quoted, block and one-level flow scalars.

    That's every construct lefthook.yml, the workflows and dependabot.yml use, read into
    dicts, lists and `Scalar` strings (no scalar is typed: `true` stays text). Anything else
    (an anchor, an alias, a tag, a directive, a tab) raises rather than reading as something
    it isn't.
    """
    return _Yaml(text.splitlines(), name).document()


def _is_item(body: str) -> bool:
    return body == "-" or body.startswith("- ")


def _comment_at(text: str) -> int:
    """Where a plain scalar's trailing comment starts: a `#` at the start or after a space."""
    match = re.search(r"(?:^|\s)#", text)
    return match.start() if match else len(text)


def _closing(text: str, quote: str) -> int | None:
    """The index of the quote that closes a scalar whose opening quote is already consumed."""
    at = 0
    while at < len(text):
        char = text[at]
        if quote == '"' and char == "\\":
            at += 2
            continue
        if char == quote:
            if quote == "'" and text[at + 1 : at + 2] == "'":
                at += 2
                continue
            return at
        at += 1
    return None


def _split_key(body: str) -> tuple[str, str] | None:
    """`key: rest` as (key, rest), or None when the line isn't a mapping entry."""
    if body[0] in "\"'":
        end = _closing(body[1:], body[0])
        if end is None:
            return None
        after = body[end + 2 :]
        match = re.match(r"\s*:(\s|$)", after)
        if not match:
            return None
        key = body[1 : end + 1]
        return (key.replace("''", "'") if body[0] == "'" else key), after[match.end() :]
    if body[0] in "[]{}|>&*!%@`#," or _is_item(body):
        return None
    match = re.search(r":(\s|$)", body)
    if not match or _comment_at(body) < match.start():
        return None
    key = body[: match.start()].rstrip()
    if not key or "'" in key or '"' in key:
        return None
    return key, body[match.end() :]


DOUBLE_ESCAPES = {"\\": "\\", '"': '"', "/": "/", "n": "\n", "t": "\t", " ": " "}


class _Yaml:
    def __init__(self, lines: list[str], name: str) -> None:
        self.lines = lines
        self.name = name
        self.at = 0

    def fail(self, line: int, why: str) -> None:
        raise YamlSubsetError(
            f"{self.name}:{line}: {why}, which the census's YAML reader doesn't take; teach"
            " `load_yaml` in scripts/check-agents-md.py the construct, or write it another way"
        )

    def peek(self) -> tuple[int, str] | None:
        """The next line that holds content, as (indent, text), past blanks and comments."""
        while self.at < len(self.lines):
            raw = self.lines[self.at]
            body = raw.lstrip(" ")
            if body.startswith("\t"):
                self.fail(self.at + 1, "a tab in the indentation")
            if not body.strip() or body.startswith("#") or raw.rstrip() == "---":
                self.at += 1
                continue
            if body.startswith(("%", "? ", "...")):
                self.fail(self.at + 1, f"`{body.split()[0]}`")
            return len(raw) - len(body), body.rstrip()
        return None

    def document(self) -> object:
        head = self.peek()
        if head is None:
            return None
        value = self.node(head[0])
        if self.peek() is not None:
            self.fail(self.at + 1, "a line outside the document's indentation")
        return value

    def node(self, indent: int) -> object:
        head = self.peek()
        assert head is not None
        return self.sequence(indent) if _is_item(head[1]) else self.mapping(indent)

    def below(self, indent: int, key: bool) -> object:
        """The node under a key or dash that ends its line, or None when there isn't one."""
        head = self.peek()
        if head and head[0] > indent:
            return self.node(head[0])
        # A mapping's sequence may sit at the key's own indentation.
        if key and head and head[0] == indent and _is_item(head[1]):
            return self.sequence(indent)
        return None

    def sequence(self, indent: int) -> list[object]:
        items: list[object] = []
        while (head := self.peek()) and head[0] == indent and _is_item(head[1]):
            line = self.at + 1
            rest = head[1][1:]
            text = rest.lstrip(" ")
            if not text or text.startswith("#"):
                self.at += 1
                items.append(self.below(indent, key=False))
                continue
            column = indent + 1 + len(rest) - len(text)
            if _is_item(text) or _split_key(text):
                # A node that starts on the dash's line: read it as if the dash were a space.
                self.lines[self.at] = " " * column + text
                items.append(self.node(column))
            else:
                self.at += 1
                items.append(self.scalar(text, line, indent))
        if head and head[0] > indent:
            self.fail(self.at + 1, "a line indented past its sequence")
        return items

    def mapping(self, indent: int) -> dict[str, object]:
        out: dict[str, object] = {}
        while (head := self.peek()) and head[0] == indent and not _is_item(head[1]):
            line = self.at + 1
            split = _split_key(head[1])
            if split is None:
                self.fail(line, "a line that isn't `key: value`")
            assert split is not None
            key, rest = split
            self.at += 1
            if key in out:
                self.fail(line, f"a second `{key}`")
            rest = rest.strip()
            if not rest or rest.startswith("#"):
                out[key] = self.below(indent, key=True)
            else:
                out[key] = self.scalar(rest, line, indent)
        if head and head[0] > indent:
            self.fail(self.at + 1, "a line indented past its mapping")
        return out

    def scalar(self, text: str, line: int, indent: int) -> object:
        first = text[0]
        if first in "|>":
            return self.block(text, line, indent)
        if first in "\"'":
            return self.quoted(text, line)
        if first in "[{":
            return self.flow(text, line)
        if first in "&*!%@`":
            self.fail(line, f"a value that starts with `{first}`")
        parts = [text[: _comment_at(text)].rstrip()]
        # A plain scalar continues onto more-indented lines, folded with a space.
        while self.at < len(self.lines):
            raw = self.lines[self.at]
            body = raw.strip()
            if (
                not body
                or body.startswith("#")
                or len(raw) - len(raw.lstrip()) <= indent
            ):
                break
            parts.append(body[: _comment_at(body)].rstrip())
            self.at += 1
        return Scalar(" ".join(parts), line)

    def block(self, header: str, line: int, indent: int) -> Scalar:
        """A literal (`|`) or folded (`>`) block. Folding joins lines with a space."""
        match = re.fullmatch(r"([|>])([+-]?)([1-9]?)([+-]?)\s*(#.*)?", header)
        if not match:
            self.fail(line, f"the block header `{header}`")
        assert match is not None
        style, chomp = match.group(1), match.group(2) or match.group(4)
        content = indent + int(match.group(3)) if match.group(3) else None
        body: list[str] = []
        while self.at < len(self.lines):
            raw = self.lines[self.at]
            if not raw.strip():
                body.append("")
                self.at += 1
                continue
            lead = len(raw) - len(raw.lstrip(" "))
            if content is None:
                if lead <= indent:
                    break
                content = lead
            if lead < content:
                break
            body.append(raw[content:])
            self.at += 1
        kept = len(body)
        while body and not body[-1]:
            body.pop()
        if style == ">":
            text = "\n".join(
                " ".join(p.split("\n")) for p in "\n".join(body).split("\n\n")
            )
        else:
            text = "\n".join(body)
        if body and chomp == "+":
            text += "\n" * (kept - len(body) + 1)
        elif body and chomp != "-":
            text += "\n"
        return Scalar(text, line + 1)

    def quoted(self, text: str, line: int) -> Scalar:
        quote, rest, pieces = text[0], text[1:], []
        while (end := _closing(rest, quote)) is None:
            pieces.append(rest.strip())
            if self.at >= len(self.lines):
                self.fail(line, "a quoted scalar that never closes")
            rest = self.lines[self.at]
            self.at += 1
        pieces.append(rest[:end].strip() if pieces else rest[:end])
        tail = rest[end + 1 :].strip()
        if tail and not tail.startswith("#"):
            self.fail(line, "text after a quoted scalar")
        value = " ".join(pieces)
        if quote == "'":
            return Scalar(value.replace("''", "'"), line)
        out, at = [], 0
        while at < len(value):
            if value[at] == "\\":
                escaped = value[at + 1 : at + 2]
                if escaped not in DOUBLE_ESCAPES:
                    self.fail(line, f"the escape `\\{escaped}`")
                out.append(DOUBLE_ESCAPES[escaped])
                at += 2
            else:
                out.append(value[at])
                at += 1
        return Scalar("".join(out), line)

    def flow(self, text: str, line: int) -> object:
        """A flow sequence or mapping, `[a, 'b']` or `{ k: v }`, on one line or several."""
        body = text
        while (end := _flow_close(body)) is None:
            if self.at >= len(self.lines):
                self.fail(line, "a flow collection that never closes")
            body += " " + self.lines[self.at].strip()
            self.at += 1
        tail = body[end:].strip()
        if tail and not tail.startswith("#"):
            self.fail(line, "text after a flow collection")
        try:
            value, _ = _flow_node(body[:end], 0, line)
        except YamlSubsetError as error:
            self.fail(line, str(error))
        return value


def _flow_close(body: str) -> int | None:
    """The index just past the bracket that closes the collection `body` opens."""
    depth, quote = 0, ""
    for at, char in enumerate(body):
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
            if depth == 0:
                return at + 1
    return None


def _flow_node(text: str, at: int, line: int) -> tuple[object, int]:
    """One flow node from `text[at:]`, and the index after it."""
    while at < len(text) and text[at] == " ":
        at += 1
    if at >= len(text):
        return Scalar("", line), at
    first = text[at]
    if first in "[{":
        close = "]" if first == "[" else "}"
        items: list[object] = []
        pairs: dict[str, object] = {}
        at += 1
        while True:
            while at < len(text) and text[at] in " ,":
                at += 1
            if at >= len(text):
                raise YamlSubsetError("a flow collection that never closes")
            if text[at] == close:
                return (items if first == "[" else pairs), at + 1
            node, at = _flow_node(text, at, line)
            while at < len(text) and text[at] == " ":
                at += 1
            if text[at : at + 1] == ":":
                value, at = _flow_node(text, at + 1, line)
                if first == "[":
                    items.append({str(node): value})
                else:
                    pairs[str(node)] = value
            elif first == "[":
                items.append(node)
            else:
                pairs[str(node)] = None
    if first in "\"'":
        end = _closing(text[at + 1 :], first)
        if end is None:
            raise YamlSubsetError("a quoted flow scalar that never closes")
        raw = text[at + 1 : at + 1 + end]
        return Scalar(
            raw.replace("''", "'") if first == "'" else raw, line
        ), at + end + 2
    if first in "&*!%@`|>":
        raise YamlSubsetError(f"a flow value that starts with `{first}`")
    match = re.compile(r"(?:[^,\[\]{}:]|:(?![\s,\]}]))*").match(text, at)
    assert match is not None
    return Scalar(match.group(0).strip(), line), match.end()


LEFTHOOK = "lefthook.yml"
WORKFLOWS = ".github/workflows"
DEPENDABOT = ".github/dependabot.yml"
SCRIPT_GLOB = "scripts/*.py"
DECISIONS = "docs/decisions.toml"
# What each census source reads, for the floor's message.
CENSUS_SOURCES = {
    "lefthook": LEFTHOOK,
    "mise": MISE,
    "workflows": f"{WORKFLOWS}/*.yml",
    "dependabot": DEPENDABOT,
    "constants": SCRIPT_GLOB,
}
# A path each source always names, so a pass means its reader parsed this tree: the workflow
# lint hook's glob, this check's own script in its task and in CI's `security` job, the fuzz
# crate's lockfile directory, and the registry's directory check-guards-fire.py reads.
CENSUS_SENTINELS = {
    "lefthook": ".github/workflows/*.yml",
    "mise": "scripts/check-agents-md.py",
    "workflows": "scripts/check-agents-md.py",
    "dependabot": "agentd/fuzz",
    "constants": "guards/faults",
}
# A decision id cited in the tree, for the same reason: the `guards` job's shards in ci.yml.
DECISION_SENTINEL = "D35"
# Words the census reads as paths that don't resolve here, with what each is. A key ending in
# `/` covers every word under that directory. An entry no source names any more fails, so the
# list can't outlive its reason.
CENSUS_NOT_PATHS = {
    "p/rust": "a Semgrep registry ruleset, `semgrep --config p/rust` (mise.toml, ci.yml)",
    "p/secrets": "a Semgrep registry ruleset, `semgrep --config p/secrets` (mise.toml, ci.yml)",
    "cli-dist/": "where release.yml's `github-release` job downloads the CLI archives",
    "staging/": "where release.yml's `github-release` job gathers the release assets",
    "lychee/": "where links.yml's lychee step writes its report",
    "mise/tasks": "a file-task directory mise reads by default; the tree has none",
    ".mise/tasks": "a file-task directory mise reads by default; the tree has none",
    ".config/mise/tasks": "a file-task directory mise reads by default; the tree has none",
}
# A cited decision id: `D` and digits as a word, not inside a URL, a hex color or a path, so
# the `-D97757?` of a badge color in README.md isn't one.
DECISION_ID = re.compile(r"(?<![\w/#-])D\d+(?![\w/])")
# The files a decision id can't be cited in: lockfiles and JSON are generated, and a base64
# checksum or a fixture can spell a `D` and digits by chance.
NOT_CITATIONS = (
    f":(exclude){DECISIONS}",
    ":(exclude)*.lock",
    ":(exclude)*-lock.yaml",
    ":(exclude)*.json",
    ":(exclude)*/fixtures/*",
)
# A run word that expands (a shell variable, a workflow expression, a mise template) is
# read only up to its last `/` before the expansion.
EXPANSION = re.compile(r"\$|\{\{")
EXPRESSION = re.compile(r"\$\{\{.*?\}\}")
RUN_SPLIT = re.compile(r"[\s;|&()<>'\"`]+")
RUN_PART = re.compile(r"[=:#]|,(?![^{}]*\})")
PATH_CHARS = re.compile(r"^[A-Za-z0-9._/*?{},+@-]+$")
UPPER = re.compile(r"^_?[A-Z][A-Z0-9_]*$")
# The directories mise reads file tasks from when `[task_config] includes` isn't set.
MISE_TASK_DIRS = (
    "mise-tasks",
    ".mise-tasks",
    "mise/tasks",
    ".mise/tasks",
    ".config/mise/tasks",
)


class CensusError(ValueError):
    """A source the census can't read as written, with where and why."""


@dataclass(frozen=True)
class Named:
    """A path one of the census's sources names."""

    source: str
    # Where it's written, for the message: `lefthook.yml:36`.
    where: str
    # As written, which is what `CENSUS_NOT_PATHS` and the sentinels match.
    text: str
    # Repo-relative, resolved against the directory it's relative to.
    path: str
    # `file` (a path, or a glob a command is handed), `dir`, `name` (a bare file name), or
    # the dialect of a glob a tool matches itself (`glob_regex`): `gobwas` (lefthook's
    # default), `doublestar` (lefthook's opt-in, mise's `sources`, dependabot's
    # `directories`) or `github` (a workflow's `paths`).
    kind: str = "file"
    # A path git ignores is a generated or local file, and passes. A filter's glob gets no
    # such pass: a hook or workflow never sees an ignored file.
    local_ok: bool = True


def expand_braces(glob: str) -> list[str]:
    """`a/{b,c}/*.{rs,py}` as its alternatives, innermost group first."""
    match = re.search(r"\{([^{}]*,[^{}]*)\}", glob)
    if not match:
        return [glob]
    return [
        alternative
        for option in match.group(1).split(",")
        for alternative in expand_braces(
            glob[: match.start()] + option + glob[match.end() :]
        )
    ]


def glob_regex(glob: str, dialect: str) -> re.Pattern[str]:
    """A glob as a regex over repo-relative paths, in the matcher its source uses.

    `gobwas` is lefthook's default matcher: `*`, `**` and `?` cross `/`, and a path is
    lowercased first. `doublestar` is lefthook's opt-in and mise's: `*` and `?` stay inside a
    segment and `**/` spans any number of them. `github` is a workflow's filter: `*` stays in
    a segment, `**` crosses, and `?` and `+` quantify the character before them. `file` is a
    shell glob a command is handed, read as `gobwas` without the lowercasing, which accepts
    at least what bash's globstar does.
    """
    out, at = [], 0
    while at < len(glob):
        char = glob[at]
        if glob.startswith("**/", at) and dialect == "doublestar":
            out.append("(?:.*/)?")
            at += 3
            continue
        if glob.startswith("**", at):
            out.append(".*")
            at += 2
            continue
        if char == "*":
            out.append(".*" if dialect in ("gobwas", "file") else "[^/]*")
        elif char == "?" and dialect == "github":
            out.append("?")
        elif char == "+" and dialect == "github":
            out.append("+")
        elif char == "?":
            out.append("." if dialect in ("gobwas", "file") else "[^/]")
        elif char == "[" and (close := glob.find("]", at + 1)) > at + 1:
            inner = glob[at + 1 : close]
            out.append("[" + ("^" + inner[1:] if inner[0] == "!" else inner) + "]")
            at = close + 1
            continue
        else:
            out.append(re.escape(char))
        at += 1
    flags = re.IGNORECASE if dialect == "gobwas" else 0
    return re.compile("".join(out), flags)


def is_glob(text: str) -> bool:
    return bool(GLOB & set(text)) or "[" in text or bool(re.search(r"\{[^{}]*,", text))


def clean(path: str, base: str = "") -> str | None:
    """`path`, relative to `base`, as a path from the repo root; None when it leaves the tree."""
    path = path.strip()
    if path.startswith("/"):
        return None
    joined = posixpath.normpath(posixpath.join(base, path))
    return None if joined == ".." or joined.startswith("../") else joined


def run_words(text: str) -> list[tuple[str, int]]:
    """The words of a command that name paths, and the line of the command each is on.

    A shell comment is dropped. A word that expands (a shell variable, a workflow
    expression, a mise template) is read up to its last `/` before the expansion
    (`target/$T/x` reads `target/`), or up to the expansion when no `/` comes before it, so
    one that starts with an expansion (`$out/index.d.ts`) names nothing in the tree. What's
    left is split at `=`, `:`, `#` and a `,` outside braces, so a flag's value
    (`-chdir=conformance/infra`), a scanner's `sbom:` prefix, an upload's `#label` and a
    comma-separated input (`skip-dirs: a,b`) are read on their own. A part is a path when it
    has a `/`, only path characters, and doesn't start with `-` (a flag), `/` (absolute, or a
    URL's `//`), `~`, `@` (a package spec) or `\\`.
    """
    words: list[tuple[str, int]] = []
    for offset, line in enumerate(EXPRESSION.sub("$E", text).splitlines()):
        line = re.sub(r"(?:^|\s)#.*$", "", line)
        for word in RUN_SPLIT.split(line):
            if expansion := EXPANSION.search(word):
                cut = word.rfind("/", 0, expansion.start())
                word = word[: cut + 1] if cut >= 0 else word[: expansion.start()]
            for part in RUN_PART.split(word):
                part = part.removeprefix("./")
                if "/" not in part or not PATH_CHARS.match(part):
                    continue
                if part.startswith(("-", "/", "~", "@", "\\")):
                    continue
                words.append((part, offset))
    return words


def named_in_run(
    source: str, file: str, text: str, line: int, base: str = ""
) -> list[Named]:
    out: list[Named] = []
    for word, offset in run_words(text):
        path = clean(word, base)
        if path is not None:
            out.append(Named(source, f"{file}:{line + offset}", word, path))
    return out


def as_list(value: object) -> list[Scalar]:
    if isinstance(value, Scalar):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, Scalar)]
    return []


def read_yaml(root: Path, name: str) -> object:
    path = root / name
    if not path.is_file():
        return None
    try:
        return load_yaml(path.read_text(encoding="utf-8"), name)
    except YamlSubsetError as error:
        raise CensusError(f"agents:check: {error}") from None


def lefthook_names(root: Path) -> list[Named]:
    """`glob`, `exclude`, `root`, `run` and `files` of every job, command and group."""
    data = read_yaml(root, LEFTHOOK)
    if not isinstance(data, dict):
        return []
    dialect = "doublestar" if data.get("glob_matcher") == "doublestar" else "gobwas"
    out: list[Named] = []

    def job(spec: dict) -> None:
        base = ""
        if isinstance(where := spec.get("root"), Scalar) and where.strip():
            base = clean(where) or ""
            out.append(
                Named("lefthook", f"{LEFTHOOK}:{where.line}", where, base, "dir")
            )
        for key in ("glob", "exclude"):
            for glob in as_list(spec.get(key)):
                for alternative in expand_braces(glob):
                    out.append(
                        Named(
                            "lefthook",
                            f"{LEFTHOOK}:{glob.line}",
                            alternative,
                            alternative.removeprefix("./"),
                            dialect,
                            local_ok=False,
                        )
                    )
        for key in ("run", "files"):
            if isinstance(text := spec.get(key), Scalar):
                out.extend(named_in_run("lefthook", LEFTHOOK, text, text.line, base))
        if isinstance(group := spec.get("group"), dict):
            hook(group)

    def hook(spec: dict) -> None:
        for item in spec.get("jobs") or []:
            if isinstance(item, dict):
                job(item)
        for item in (spec.get("commands") or {}).values():
            if isinstance(item, dict):
                job(item)

    for spec in data.values():
        if isinstance(spec, dict):
            hook(spec)
    return out


def toml_line(text: str, header: str, needle: str) -> int:
    """The line of `needle` in the table `header` opens, or of the header, or 1."""
    lines = text.splitlines()
    start = next((n for n, line in enumerate(lines) if line.strip() == header), None)
    if start is None:
        return 1
    for n in range(start + 1, len(lines)):
        if lines[n].startswith("["):
            break
        if needle in lines[n] and not lines[n].lstrip().startswith("#"):
            return n + 1
    return start + 1


def task_header(name: str, prefix: str) -> str:
    quoted = name if re.fullmatch(r"[A-Za-z0-9_-]+", name) else f'"{name}"'
    return f"[{prefix}{quoted}]"


def mise_task_files(
    root: Path,
) -> tuple[list[tuple[str, str, dict]], list[Named], list[str]]:
    """Every task as (file, name, table), the includes it read, and what it couldn't read.

    The tasks are mise.toml's `[tasks]` and the tables of each TOML file its
    `[task_config] includes` names, the shape mise gives an included file: a task per
    top-level table. An include that's a directory holds file tasks, which this doesn't
    read, so it's refused by name, and so is a remote one. With no includes, a default
    task directory with files in it is refused the same way.
    """
    path = root / MISE
    if not path.is_file():
        return [], [], []
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    tasks = data.get("tasks", {})
    found = (
        [(MISE, n, t) for n, t in tasks.items() if isinstance(t, dict)]
        if isinstance(tasks, dict)
        else []
    )
    names: list[Named] = []
    refused: list[str] = []
    config = data.get("task_config", {})
    includes = config.get("includes") if isinstance(config, dict) else None
    if includes is None:
        for directory in MISE_TASK_DIRS:
            if listed(root, f"{directory}/"):
                refused.append(
                    f"agents:check: {MISE}: mise reads file tasks from `{directory}/`, which"
                    " the census doesn't; teach mise_task_files in scripts/check-agents-md.py"
                    " to read them"
                )
    for include in includes if isinstance(includes, list) else []:
        line = toml_line(
            path.read_text(encoding="utf-8"), "[task_config]", str(include)
        )
        where = f"{MISE}:{line}"
        if not isinstance(include, str) or "::" in include or "://" in include:
            refused.append(
                f"{where}: the include `{include}` isn't a file in this tree"
            )
            continue
        included = clean(include)
        names.append(Named("mise", where, include, included or include))
        if included and (root / included).is_dir():
            refused.append(
                f"{where}: the include `{include}` is a directory of file tasks, which the"
                " census doesn't read; name its TOML files instead, or teach"
                " mise_task_files to read file tasks"
            )
        elif included and (root / included).is_file():
            table = tomllib.loads((root / included).read_text(encoding="utf-8"))
            found += [(included, n, t) for n, t in table.items() if isinstance(t, dict)]
    return found, names, refused


def mise_names(root: Path) -> tuple[list[Named], list[str]]:
    """Each task's `dir`, `file`, `sources`, `outputs` and the paths its `run` names.

    Relative to the task's `dir` when it has one, as mise runs them; `{{config_root}}` is
    the repo root, and any other template is refused, since the census can't expand it.
    """
    tasks, out, refused = mise_task_files(root)
    texts: dict[str, str] = {}
    for file, name, task in tasks:
        text = texts.setdefault(file, (root / file).read_text(encoding="utf-8"))
        header = task_header(name, "tasks." if file == MISE else "")

        def where(needle: str) -> str:
            return f"{file}:{toml_line(text, header, needle)}"

        base = ""
        if isinstance(directory := task.get("dir"), str):
            expanded = re.sub(r"\{\{\s*config_root\s*\}\}/?", "", directory)
            if "{{" in expanded:
                refused.append(
                    f"{where('dir')}: task `{name}`'s `dir` is a template the census can't"
                    " expand"
                )
            else:
                base = clean(expanded) or ""
                out.append(Named("mise", where("dir"), directory, base or ".", "dir"))
        if isinstance(script := task.get("file"), str):
            path = clean(script, base)
            if path:
                out.append(Named("mise", where("file"), script, path))
        for key in ("sources", "outputs"):
            globs = task.get(key)
            for glob in globs if isinstance(globs, list) else []:
                if isinstance(glob, str) and (path := clean(glob, base)):
                    out.append(Named("mise", where(glob), glob, path, "doublestar"))
        for key in ("run", "run_windows"):
            commands = task.get(key)
            for command in [commands] if isinstance(commands, str) else commands or []:
                if not isinstance(command, str) or not command.strip():
                    continue
                lines = command.splitlines()
                first = next(n for n, line in enumerate(lines) if line.strip())
                start = int(where(lines[first].strip()).rsplit(":", 1)[1]) - first
                for word, offset in run_words(command):
                    if (path := clean(word, base)) is not None:
                        out.append(
                            Named("mise", f"{file}:{start + offset}", word, path)
                        )
    return out, refused


def workflow_names(root: Path) -> list[Named]:
    """`paths` and `paths-ignore`, every `working-directory`, local `uses`, and run steps."""
    out: list[Named] = []
    for name in listed(root, f"{WORKFLOWS}/"):
        if not name.endswith((".yml", ".yaml")) or "/" in name[len(WORKFLOWS) + 1 :]:
            continue
        data = read_yaml(root, name)
        if not isinstance(data, dict):
            continue
        events = data.get("on")
        for spec in events.values() if isinstance(events, dict) else []:
            if not isinstance(spec, dict):
                continue
            for key in ("paths", "paths-ignore"):
                for glob in as_list(spec.get(key)):
                    text = glob.removeprefix("!")
                    out.append(
                        Named(
                            "workflows",
                            f"{name}:{glob.line}",
                            glob,
                            text,
                            "github",
                            False,
                        )
                    )

        def directory(spec: object, fallback: str) -> str:
            run = (
                spec.get("defaults", {}).get("run", {})
                if isinstance(spec, dict)
                else {}
            )
            where = run.get("working-directory") if isinstance(run, dict) else None
            return working(where, fallback)

        def working(where: object, fallback: str) -> str:
            if not isinstance(where, Scalar) or not where.strip():
                return fallback
            if EXPANSION.search(where):
                out.extend(named_in_run("workflows", name, where, where.line))
                return fallback
            path = clean(where) or fallback
            out.append(Named("workflows", f"{name}:{where.line}", where, path, "dir"))
            return path

        top = directory(data, "")
        for job in (data.get("jobs") or {}).values():
            if not isinstance(job, dict):
                continue
            base = directory(job, top)
            if isinstance(uses := job.get("uses"), Scalar) and uses.startswith("./"):
                out.append(
                    Named("workflows", f"{name}:{uses.line}", uses, clean(uses) or uses)
                )
            for step in job.get("steps") or []:
                if not isinstance(step, dict):
                    continue
                here = working(step.get("working-directory"), base)
                uses = step.get("uses")
                if isinstance(uses, Scalar) and uses.startswith("./"):
                    out.append(
                        Named(
                            "workflows",
                            f"{name}:{uses.line}",
                            uses,
                            clean(uses) or uses,
                            "dir",
                        )
                    )
                if isinstance(run := step.get("run"), Scalar):
                    out.extend(named_in_run("workflows", name, run, run.line, here))
                for value in (
                    (step.get("with") or {}).values()
                    if isinstance(step.get("with"), dict)
                    else []
                ):
                    if isinstance(value, Scalar):
                        out.extend(named_in_run("workflows", name, value, value.line))
    return out


def dependabot_names(root: Path) -> list[Named]:
    """Each update's `directory` or `directories`, which are repo-rooted."""
    data = read_yaml(root, DEPENDABOT)
    updates = data.get("updates") if isinstance(data, dict) else None
    out: list[Named] = []
    for update in updates if isinstance(updates, list) else []:
        if not isinstance(update, dict):
            continue
        for key in ("directory", "directories"):
            for entry in as_list(update.get(key)):
                path = entry.strip().strip("/") or "."
                kind = "doublestar" if is_glob(path) else "dir"
                out.append(
                    Named("dependabot", f"{DEPENDABOT}:{entry.line}", entry, path, kind)
                )
    return out


@dataclass
class ModulePaths:
    """The paths and strings a script's module-level constants have bound so far."""

    script: str
    paths: dict[str, str] = field(default_factory=dict)
    strings: dict[str, str] = field(default_factory=dict)

    def value(self, node: ast.expr) -> str | None:
        """The repo-relative path `node` builds, or None when it isn't a path expression.

        `Path(__file__)` is the script itself; `.resolve()` keeps it, `.parent` and
        `.parents[n]` go up, and `.with_name(s)` swaps the name. `Path(s, ...)` is relative to
        the repo root, where every task runs a script from. `x / "s"` joins onto a path this
        already resolved, and a name is a constant bound earlier: a path, or a string on the
        right of a `/` (`ROOT / PLACEMENT`) or inside `Path()`.
        """
        if isinstance(node, ast.Name):
            return self.paths.get(node.id)
        if isinstance(node, ast.Call):
            return self.call(node)
        if isinstance(node, ast.Attribute) and node.attr == "parent":
            inner = self.value(node.value)
            return None if inner is None else posixpath.dirname(inner)
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "parents"
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, int)
        ):
            inner = self.value(node.value.value)
            for _ in range(node.slice.value + 1 if inner is not None else 0):
                inner = posixpath.dirname(inner)
            return inner
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            left, right = self.value(node.left), self.string(node.right)
            if left is not None and right is not None:
                return posixpath.join(left, right)
        return None

    def call(self, node: ast.Call) -> str | None:
        func = node.func
        if isinstance(func, ast.Name) and func.id == "Path" and not node.keywords:
            if len(node.args) == 1 and isinstance(node.args[0], ast.Name):
                name = node.args[0].id
                if name == "__file__":
                    return self.script
                return self.paths.get(name, self.strings.get(name))
            parts = [
                part for arg in node.args if (part := self.string(arg)) is not None
            ]
            if parts and len(parts) == len(node.args):
                return posixpath.join(*parts)
            return None
        if isinstance(func, ast.Attribute):
            inner = self.value(func.value)
            if inner is not None and func.attr == "resolve" and not node.args:
                return inner
            if inner is not None and func.attr == "with_name" and len(node.args) == 1:
                name = self.string(node.args[0])
                if name is not None:
                    return posixpath.join(posixpath.dirname(inner), name)
        return None

    def string(self, node: ast.expr) -> str | None:
        """A string literal, or the name of a string constant bound earlier."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return self.strings.get(node.id)
        return None

    def within(self, node: ast.expr) -> list[str]:
        """Every outermost path expression in `node`, wherever it sits."""
        if (path := self.value(node)) is not None:
            return [path] if path else []
        children = [
            child.value if isinstance(child, ast.keyword) else child
            for child in ast.iter_child_nodes(node)
        ]
        return [
            path
            for child in children
            if isinstance(child, ast.expr)
            for path in self.within(child)
        ]


def literal_strings(node: ast.expr) -> list[str] | None:
    """The strings of a literal: a string, or a tuple, list, set or frozenset() holding only
    strings, path expressions and more of the same. None when it's anything else."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "frozenset"
        and len(node.args) == 1
        and not node.keywords
    ):
        node = node.args[0]
    if not isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return None
    out: list[str] = []
    for element in node.elts:
        inner = literal_strings(element)
        if inner is not None:
            out += inner
        elif not isinstance(element, (ast.Call, ast.BinOp, ast.Name, ast.Attribute)):
            return None
    return out


def constant_names(root: Path) -> tuple[list[Named], list[str]]:
    """The path constants at the top of each gate script, and a script that doesn't parse.

    A constant is a module-level assignment to an upper-case name (`REGISTRY`, `_LIVE`) in
    a `scripts/*.py` that isn't a `test_*.py` (those name fixture files in the throwaway
    repos they build). Two things in its value are paths:

    - every path expression, wherever it sits (`runpy.run_path(str(HERE / "ratchet.py"))`):
      `Path(__file__)` and what `.resolve()`, `.parent`, `.parents[n]` and `.with_name()`
      make of it, `Path("literal", ...)`, and a `/` join of a string literal onto one of
      those or onto a constant already read (`ROOT / "parity" / "capabilities.toml"`),
      as `ModulePaths.value` says;
    - when the value is a literal (a string, or a tuple, list, set or frozenset() of
      strings, path expressions and more of those), each string that's a path (`is_path`,
      in path characters only) or a bare file name (`mise.toml`, which some file in the
      tree must have).

    A string inside any other call isn't read (`re.compile("a/b")` is a pattern), and
    neither is a dict: its keys map names to meanings, and a GitHub `owner/repo` key reads
    as a path.
    """
    out: list[Named] = []
    problems: list[str] = []
    for script in listed(root, SCRIPT_GLOB):
        if Path(script).name.startswith("test_") or "/" in script[len("scripts/") :]:
            continue
        try:
            tree = ast.parse((root / script).read_text(encoding="utf-8"), script)
        except SyntaxError as error:
            problems.append(
                f"{script}:{error.lineno}: the census can't parse it: {error.msg}"
            )
            continue
        bound = ModulePaths(script)
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target, value = node.targets[0], node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                target, value = node.target, node.value
            else:
                continue
            if not isinstance(target, ast.Name) or not UPPER.match(target.id):
                continue
            where = f"{script}:{node.lineno}"
            if (path := bound.value(value)) is not None:
                bound.paths[target.id] = path
            elif (text := bound.string(value)) is not None:
                bound.strings[target.id] = text
            for path in bound.within(value):
                out.append(Named("constants", where, path, posixpath.normpath(path)))
            for text in literal_strings(value) or []:
                if is_path(text) and PATH_CHARS.match(text):
                    out.append(Named("constants", where, text, text.removeprefix("./")))
                elif FILE_NAME.match(text):
                    out.append(Named("constants", where, text, text, "name"))
    return out, problems


def resolve_census(
    root: Path, named: list[Named], present: set[str]
) -> tuple[list[str], set[str]]:
    """What resolves to nothing, and which `CENSUS_NOT_PATHS` entries some word matched."""
    dirs = {str(q) for f in present for q in Path(f).parents if str(q) != "."}
    names = {Path(f).name for f in present}
    regexes: dict[tuple[str, str], re.Pattern[str]] = {}
    excused: set[str] = set()

    def matches(path: str, dialect: str, with_dirs: bool) -> bool:
        pattern = regexes.setdefault((path, dialect), glob_regex(path, dialect))
        pool = present | dirs if with_dirs else present
        return any(pattern.fullmatch(p) for p in pool)

    unresolved: list[Named] = []
    for item in named:
        if item.kind == "name":
            found = item.path in names
        elif item.path == ".":
            found = True
        elif item.kind in ("gobwas", "doublestar", "github"):
            found = matches(item.path, item.kind, with_dirs=False)
        elif is_glob(item.path):
            found = any(matches(a, "file", True) for a in expand_braces(item.path))
        elif item.kind == "dir":
            found = item.path.rstrip("/") in dirs
        else:
            found = item.path in present or item.path.rstrip("/") in dirs
        if found:
            continue
        excuse = next(
            (
                key
                for key in CENSUS_NOT_PATHS
                if item.text == key or (key.endswith("/") and item.text.startswith(key))
            ),
            None,
        )
        if excuse is not None:
            excused.add(excuse)
            continue
        unresolved.append(item)

    # A generated or local path passes when git ignores it: the path, its directory form, and
    # for a glob the glob itself (`microvms-js/*.node` against the same ignore line) or the
    # directory it starts in (`site/dist/**/*.html`).
    def forms(path: str) -> set[str]:
        if not is_glob(path):
            return {path, path.rstrip("/") + "/"}
        head = re.split(r"[*?\[{]", path, maxsplit=1)[0]
        head = head[: head.rfind("/") + 1]
        return {path} | ({head} if head else set())

    local = ignored(
        root, sorted({f for i in unresolved if i.local_ok for f in forms(i.path)})
    )
    problems = []
    for item in unresolved:
        if item.local_ok and forms(item.path) & local:
            continue
        what = {
            "name": "is no file's name in this tree",
            "dir": "is no directory in this tree",
        }.get(
            item.kind,
            "matches no tracked file"
            if is_glob(item.path)
            else "is no such path in this tree",
        )
        shown = f"`{item.text}`" + (
            "" if item.text == item.path else f" (`{item.path}`)"
        )
        problems.append(f"{item.where}: {shown} {what}")
    return problems, excused


def decision_problems(root: Path) -> tuple[list[str], int]:
    """A decision id cited anywhere in the tree that `docs/decisions.toml` doesn't define.

    A citation is `DECISION_ID`, `D` and digits as a word outside a URL or a hex color
    (`(D14)`), in any tracked or new text file but the register, the lockfiles, JSON and
    fixtures. A definition is a table in the register named by the id, with a `decision`
    saying what was decided and a `source` saying where.
    """
    problems: list[str] = []
    path = root / DECISIONS
    table = tomllib.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    defined: set[str] = set()
    for key, entry in table.items():
        if not re.fullmatch(r"D\d+", key) or not isinstance(entry, dict):
            problems.append(f"{DECISIONS}: `{key}` isn't a decision table named `D<n>`")
            continue
        missing = [
            f
            for f in ("decision", "source")
            if not (isinstance(entry.get(f), str) and entry[f].strip())
        ]
        if missing:
            problems.append(f"{DECISIONS}: `{key}` has no {' or '.join(missing)}")
            continue
        defined.add(key)
    if not defined:
        problems.append(f"agents:check: {DECISIONS} defines no decision")
    out = git(
        root, "grep", "--untracked", "-I", "-n", "-w", "-E", r"D[0-9]+",
        "--", ".", *NOT_CITATIONS,
    )  # fmt: skip
    if out.returncode > 1:
        raise SystemExit(f"agents:check: `git grep` failed: {out.stderr.strip()}")
    cited: list[tuple[str, str]] = []
    for line in out.stdout.splitlines():
        name, number, text = line.split(":", 2)
        cited += [(f"{name}:{number}", d) for d in DECISION_ID.findall(text)]
    if not cited:
        problems.append("agents:check: found no decision id cited in the tree")
    elif not any(d == DECISION_SENTINEL for _, d in cited):
        problems.append(
            f"agents:check: the sentinel decision `{DECISION_SENTINEL}` isn't cited anywhere;"
            " the citation scan or ci.yml lost it"
        )
    for where, decision in cited:
        if defined and decision not in defined:
            problems.append(
                f"{where}: `{decision}` is a decision id {DECISIONS} doesn't define"
            )
    return problems, len(cited)


def census(root: Path, present: set[str]) -> tuple[list[str], int]:
    """Every path the census's sources name, resolved, and every decision id defined."""
    problems: list[str] = []
    named: list[Named] = []
    readers = {
        "lefthook": lambda: (lefthook_names(root), []),
        "mise": lambda: mise_names(root),
        "workflows": lambda: (workflow_names(root), []),
        "dependabot": lambda: (dependabot_names(root), []),
        "constants": lambda: constant_names(root),
    }
    for source in CENSUS_SOURCES:
        try:
            found, refused = readers[source]()
        except CensusError as error:
            problems.append(str(error))
            continue
        problems += refused
        if not found:
            problems.append(
                f"agents:check: the census read no paths from {CENSUS_SOURCES[source]}; its"
                " reader or the file stopped matching"
            )
        elif not any(n.path == CENSUS_SENTINELS[source] for n in found):
            problems.append(
                f"agents:check: the census sentinel `{CENSUS_SENTINELS[source]}` isn't among"
                f" the paths {CENSUS_SOURCES[source]} names; its reader lost it"
            )
        named += found
    unresolved, excused = resolve_census(root, named, present)
    problems += unresolved
    for word, what in CENSUS_NOT_PATHS.items():
        if word not in excused:
            problems.append(
                f"agents:check: CENSUS_NOT_PATHS lists `{word}` ({what}), which no source"
                " names as an unresolved path; delete it"
            )
    decided, cited = decision_problems(root)
    return problems + decided, len(named) + cited


def check(root: Path) -> list[str]:
    docs = listed(root, *DOC_PATHSPECS)
    docs = [d for d in docs if (root / d).is_file()]
    if not docs:
        return [
            "agents:check: found no docs to read (`git ls-files` returned nothing for"
            f" {', '.join(DOC_PATHSPECS)}); run this from the repo root or pass --root"
        ]
    problems: list[str] = []
    if ROOT_DOC not in docs:
        problems.append(
            f"agents:check: read {len(docs)} docs without {ROOT_DOC}, which this repo always"
            " has; the enumerator read the wrong tree"
        )

    texts = {d: read(root, d) for d in docs}
    refs: list[Ref] = []
    for doc, text in texts.items():
        found, open_fence = spans(text)
        if open_fence is not None:
            problems.append(
                f"{doc}:{open_fence}: this fence is never closed, so the rest of the doc"
                " reads as code and its references go unchecked"
            )
        own = references(doc, found)
        if not own:
            problems.append(
                f"agents:check: {doc} yields no references; it's empty or its text stopped"
                " being read"
            )
        if doc == ROOT_DOC:
            for kind in ROOT_KINDS:
                if not any(r.kind == kind for r in own):
                    problems.append(
                        f"agents:check: {ROOT_DOC} yields no {kind} references, and its"
                        " rules name some"
                    )
        refs += own
    for kind in KINDS:
        if not any(r.kind == kind for r in refs):
            problems.append(
                f"agents:check: found no {kind} references in {len(docs)} docs; either the"
                " extractor stopped matching or the docs stopped naming any"
            )
    if not any(r.kind == "task" and r.text == SENTINEL for r in refs):
        problems.append(
            f"agents:check: the sentinel `mise run {SENTINEL}` isn't among the task references;"
            " the extractor or the docs lost it"
        )

    tasks = mise_tasks(root)
    if not tasks:
        problems.append(f"agents:check: found no tasks in {MISE}")
    in_check = gated(tasks, SENTINEL)
    if tasks and not in_check:
        problems.append(f"agents:check: `{SENTINEL}` depends on no task in {MISE}")
    faults = fault_ids(root)
    if not faults:
        problems.append(f"agents:check: found no fault ids in {REGISTRY}")
    jobs = ci_jobs(root)
    if not jobs:
        problems.append(f"agents:check: found no jobs in {WORKFLOW}")
    methods = results_methods(root)
    if methods is None:
        problems.append(f"agents:check: no class {SYMBOL_CLASS} in {SYMBOL_FILE}")
    files = listed(root)
    present = {f for f in files if (root / f).exists()}
    names = {Path(f).name for f in present}
    words = spelled(root, {r.text for r in refs if r.kind == "identifier"}, docs)

    for ref in refs:
        where = f"{ref.doc}:{ref.line}:"
        if ref.kind == "task" and tasks and ref.text not in tasks:
            bare = ref.shown == ref.text
            if not (bare and ref.text.split(":", 1)[0] in NOT_TASK_PREFIXES):
                problems.append(f"{where} `{ref.shown}` names no task in {MISE}")
        elif ref.kind == "member" and in_check and ref.text not in in_check:
            problems.append(
                f"{where} `{ref.text}` isn't in `{SENTINEL}`: no task `{SENTINEL}` depends on"
                f" in {MISE} is it or reaches it"
            )
        elif ref.kind == "fault" and faults and ref.text not in faults:
            problems.append(f"{where} `{ref.text}` is no fault id in {REGISTRY}")
        elif ref.kind == "file" and ref.text not in names:
            problems.append(f"{where} `{ref.text}` is no file's name in this tree")
        elif ref.kind == "identifier" and ref.text not in words:
            problems.append(
                f"{where} `{ref.text}` isn't spelled in any code outside these docs"
            )
        elif ref.kind == "job" and jobs and ref.text not in jobs:
            problems.append(
                f"{where} the `{ref.text}` job isn't a job id or name in {WORKFLOW}"
            )
        elif ref.kind == "symbol" and methods is not None:
            name = ref.text.split(".", 1)[1]
            if name not in methods:
                problems.append(
                    f"{where} `{ref.text}` isn't a method of {SYMBOL_CLASS} in {SYMBOL_FILE}"
                )
    problems += resolve_paths(root, [r for r in refs if r.kind == "path"], present)
    census_problems, census_read = census(root, present)
    problems += census_problems
    for name, what in NOT_PATHS.items():
        if not any(f"`{name}`" in text for text in texts.values()):
            problems.append(
                f"agents:check: NOT_PATHS lists `{name}` ({what}), which no doc names; delete it"
            )
    if not problems:
        print(
            f"agents:check: every reference in {len(docs)} docs resolves ({len(refs)} read)"
        )
        print(
            "agents:check: every path the hooks, tasks, workflows, dependabot and scripts"
            f" name resolves, and every cited decision id is defined ({census_read} read)"
        )
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root", type=Path, default=Path.cwd(), help="the tree to read"
    )
    args = parser.parse_args()
    problems = check(args.root.resolve())
    for problem in problems:
        print(problem)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
