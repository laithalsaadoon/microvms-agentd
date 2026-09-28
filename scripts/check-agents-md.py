#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
# SPDX-License-Identifier: Apache-2.0
"""Every command, path, CI job and suite helper the contributor docs name exists (#278).

AGENTS.md states each rule with the check that enforces it, so a rule whose check was renamed
or deleted reads as enforced and isn't. This reads the root AGENTS.md, every AGENTS.md below
it, CONTRIBUTING.md and the pull request template, and fails on a reference that resolves to
nothing. What counts as a reference, all inside backticks:

- `mise run <task>`, in an inline span or a fenced block, and a bare task name with a colon
  (`guards:list`), against the `[tasks]` tables of mise.toml. A name holding a placeholder
  (`ci:<job>`) isn't one, and a bare name that isn't a task and starts with a mise tool
  backend or Node's module scheme (`cargo:cargo-mutants`, `node:test`) is that, not a task.
- A task followed by "in `check`" or "in `mise run check`", against the tasks `check`
  depends on, directly or through a task it depends on. The doc says the local gate runs it.
- The id after `--only` and after `fired:` (`guards:fire -- --only agentd-fs-pop`), against
  the `id` of each `[[fault]]` in guards/faults.toml.
- A word with a `/` in it (`guards/faults.toml`, `src/lib.rs`, `site/authored/`), inline
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
  of `Results` in conformance/run_rs.py, read with stdlib `ast`.

A check over nothing reports nothing, so it also fails when the docs, the tasks, the jobs, the
fault ids or the `Results` methods come back empty, when no reference of some kind was found
at all (an extractor that stopped matching reads that way), when a doc yields no reference or
the root AGENTS.md lacks one of the kinds its rules use, when a fence is never closed (the
rest of that doc would read as code), when the root AGENTS.md isn't among the docs, and when
the sentinel `mise run check` isn't among the task references. The workflow is read with a
regex over its `jobs:` block rather than a YAML parser, so this stays stdlib and offline; the
job ids sit at a fixed indent that actionlint already holds.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import re
import subprocess
import tomllib
from dataclasses import dataclass
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
REGISTRY = "guards/faults.toml"
# The class whose helpers the docs cite by name, and the file it lives in.
SYMBOL_CLASS = "Results"
SYMBOL_FILE = "conformance/run_rs.py"
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
    path = root / MISE
    if not path.is_file():
        return {}
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    tasks = data.get("tasks", {})
    return tasks if isinstance(tasks, dict) else {}


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
    path = root / REGISTRY
    if not path.is_file():
        return set()
    faults = tomllib.loads(path.read_text(encoding="utf-8")).get("fault", [])
    return {f["id"] for f in faults if isinstance(f, dict) and "id" in f}


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
    if name == REGISTRY:
        faults = tomllib.loads(text).get("fault", [])
        return repr([{k: v for k, v in f.items() if k != "transform"} for f in faults])
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
    for name, what in NOT_PATHS.items():
        if not any(f"`{name}`" in text for text in texts.values()):
            problems.append(
                f"agents:check: NOT_PATHS lists `{name}` ({what}), which no doc names; delete it"
            )
    if not problems:
        print(
            f"agents:check: every reference in {len(docs)} docs resolves ({len(refs)} read)"
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
