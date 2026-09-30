#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
# SPDX-License-Identifier: Apache-2.0
"""A pull request's body has the template's sections, and says why when it's over the size cap.

The written rules for a pull request (CONTRIBUTING.md, "Pull requests") are read at review
time, and a rule nothing checks is a convention: a finding left out of the body is a finding
nobody tracks, and an oversized change with no reason given is the one that turns a one-bug fix
into a week of review. This holds the body to the parts of those rules a script can read.

`./tools/check-pr-body.py [--base REF] [--event PATH | --body FILE [--author LOGIN]]`
(`mise run pr-body:check`, in `check`; CI's `security` job runs it on every event) reads one of:

- a pull request's event: `--event`, by default `$GITHUB_EVENT_PATH`, the payload file the
  runner writes for every step. Its `pull_request.body` is the body and its
  `pull_request.user.login` the author. A body edit starts no run, and the payload is the body
  as it was when the event fired, so after fixing the body, push a commit, or close and reopen
  the pull request, which starts a run with the new body.
- a body in a file: `--body FILE`, for a body written before the pull request exists, with
  `--author` when it's someone's in particular.
- neither: an event with no `pull_request` (a push to main), or no event at all (`check` on a
  workstation). There's no body to read, so it holds the template instead, which every body
  starts from: `.github/PULL_REQUEST_TEMPLATE.md` must have each section REQUIRED names. A
  required section the template lacks would fail every pull request that followed it.

A body fails when:

- a section REQUIRED names has no `## ` heading of that title (matched without case), or has
  nothing under it but HTML comments and blank lines. The template's instructions are comments,
  so a section left as the template gave it is empty: write what it asks, or `None.` when
  there's nothing. Follow-ups is the tracker rule's section: a finding the change doesn't need
  to be correct is written there, or it isn't written anywhere.
- its product diff is over SOFT_CAP changed lines and no line of it outside a comment or a code
  block opens `Size:` with a reason after it. The product diff is the lines added and removed
  under the shipped source directories tools/changelog.py names (`SHIPPED`, less the files
  `NOT_SHIPPED` matches and the generated type surfaces), between the merge base of HEAD and
  `--base` (origin/main by default) and the working tree, untracked files included, so a run
  before a commit reads what the commit will hold. Unit tests inside those directories count:
  they sit in the files the fix edits. The cap is soft: over it, the line says why the change
  can't be split, and review judges the reason.
- `--event` names a file that doesn't exist or isn't a JSON object, which a runner never writes.
  Passing there would check nothing.

`dependabot[bot]`'s pull requests are skipped, and no other login's: its bodies are its own
format, and a dependency bump has no follow-ups. The skip is the author's exact login, as
tools/changelog.py's is.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import runpy
import subprocess
import sys
from pathlib import Path

TEMPLATE = ".github/PULL_REQUEST_TEMPLATE.md"
# The sections every body has, as the template titles them.
REQUIRED = ("What and why", "Evidence", "Guards", "Follow-ups")
# Changed lines of product code a pull request carries before its body has to say why it's one
# change. CONTRIBUTING.md and the template state this figure, and the unit tests hold both to it.
SOFT_CAP = 400
DEPENDABOT = "dependabot[bot]"
# The shipped set is tools/changelog.py's: what a user of the crates, the CLI or the bindings
# runs. Its main doesn't run under runpy.
CHANGELOG = runpy.run_path(str(Path(__file__).with_name("changelog.py")))
# The directories of the shipped set. Its single files are the generated type surfaces, which
# `mise run stubs` and the napi build write, so their lines aren't anyone's to split.
PRODUCT_DIRS = tuple(p for p in CHANGELOG["SHIPPED"] if p.endswith("/"))

# An HTML comment, or one left open, which a renderer hides to the end of the body.
COMMENT = re.compile(r"<!--.*?(?:-->|\Z)", re.DOTALL)
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
SIZE = re.compile(r"^\s*Size:\s*\S")


class Failure(Exception):
    """An input this can't read, with the message that says which."""


def git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if done.returncode != 0:
        raise Failure(f"`git {' '.join(args)}` failed: {done.stderr.strip()}")
    return done.stdout


def repo_root() -> Path:
    done = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True
    )
    if done.returncode != 0:
        raise Failure("not in a git repository; run this from the repo")
    return Path(done.stdout.strip())


def title_key(title: str) -> str:
    return " ".join(title.split()).casefold()


def prose_lines(text: str) -> list[str | None]:
    """The body's lines outside HTML comments, with each code block's lines as None.

    So a `## ` or a `Size:` that a code block quotes is neither a heading nor a reason, and the
    block still counts as a section's content. Line endings are normalized first: a body edited
    in a browser arrives with CRLF.
    """
    text = COMMENT.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    lines: list[str | None] = []
    fence = ""
    for line in text.split("\n"):
        opened = FENCE.match(line)
        if not fence:
            if opened:
                fence = opened[1]
                lines.append(None)
            else:
                lines.append(line)
            continue
        # A block closes on a run of its own character at least as long, with nothing after.
        run = opened[1] if opened else ""
        if (
            run[:1] == fence[0]
            and len(run) >= len(fence)
            and not line.strip()[len(run) :]
        ):
            fence = ""
        lines.append(None)
    return lines


def sections(lines: list[str | None]) -> dict[str, list[str | None]]:
    """Each level-two heading's title, keyed without case, and the lines up to the next.

    A deeper heading is part of its section; a level-one or level-two heading ends it. A title
    written twice keeps its lines from both.
    """
    found: dict[str, list[str | None]] = {}
    current: list[str | None] | None = None
    for line in lines:
        heading = HEADING.match(line) if line is not None else None
        if heading and len(heading[1]) <= 2:
            current = (
                found.setdefault(title_key(heading[2]), [])
                if len(heading[1]) == 2
                else None
            )
            continue
        if current is not None:
            current.append(line)
    return found


def section_problems(text: str) -> list[str]:
    found = sections(prose_lines(text))
    problems = []
    for title in REQUIRED:
        body = found.get(title_key(title))
        if body is None:
            problems.append(
                f"no `## {title}` section; {TEMPLATE} has it, and every body keeps it"
            )
        elif not any(line is None or line.strip() for line in body):
            problems.append(
                f"`## {title}` has nothing in it but the template's comment; write what it"
                " asks, or `None.` when there's nothing"
            )
    return problems


def has_size_line(text: str) -> bool:
    return any(line is not None and SIZE.match(line) for line in prose_lines(text))


def is_product(path: str) -> bool:
    return path.startswith(PRODUCT_DIRS) and CHANGELOG["is_shipped"](path)


def product_lines(root: Path, base: str) -> dict[str, int]:
    """Each product file the branch changes against its merge base with `base`, and its lines."""
    if subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"],
        cwd=root,
        capture_output=True,
    ).returncode:
        raise Failure(
            f"--base {base} doesn't name a commit here; fetch it (`git fetch origin main`), or"
            " pass the branch this one merges into"
        )
    fork = git(root, "merge-base", base, "HEAD").strip()
    changed: dict[str, int] = {}
    for row in git(root, "diff", "--numstat", "--no-renames", fork).splitlines():
        added, removed, path = row.split("\t", 2)
        # A binary file's counts are `-`: it has no lines to split.
        if added != "-" and is_product(path):
            changed[path] = int(added) + int(removed)
    for path in git(root, "ls-files", "--others", "--exclude-standard").splitlines():
        if is_product(path):
            data = (root / path).read_bytes()
            if b"\0" not in data:
                changed[path] = len(data.decode("utf-8", "replace").splitlines())
    return changed


def check_body(root: Path, text: str, author: str | None, base: str) -> int:
    if author == DEPENDABOT:
        print(
            f"pr-body: {DEPENDABOT}'s pull requests keep their own format; not checked"
        )
        return 0
    problems = section_problems(text)
    changed = product_lines(root, base)
    total = sum(changed.values())
    if total > SOFT_CAP and not has_size_line(text):
        largest = sorted(changed.items(), key=lambda item: (-item[1], item[0]))[:5]
        problems.append(
            f"{total} changed lines of product code against {base}, over the soft cap of"
            f" {SOFT_CAP}, and no `Size:` line saying why it's one change (largest: "
            + ", ".join(f"{path} {lines}" for path, lines in largest)
            + "); split it, or add the line"
        )
    if problems:
        print(
            f"pr-body: {len(problems)} {'problem' if len(problems) == 1 else 'problems'}:"
        )
        for problem in problems:
            print(f"  {problem}")
        return 1
    over = " with its `Size:` line" if total > SOFT_CAP else ""
    print(
        f"pr-body: every section is there, and {total} changed lines of product code against"
        f" {base}{over}"
    )
    return 0


def check_template(root: Path, why: str) -> int:
    path = root / TEMPLATE
    if not path.is_file():
        raise Failure(f"{TEMPLATE} doesn't exist at {root}")
    found = sections(prose_lines(path.read_text(encoding="utf-8")))
    missing = [title for title in REQUIRED if title_key(title) not in found]
    if missing:
        print(
            f"pr-body: {TEMPLATE} has no "
            + ", ".join(f"`## {title}`" for title in missing)
            + ", which every pull request's body needs"
        )
        return 1
    print(f"pr-body: {why}; {TEMPLATE} has every section a body needs")
    return 0


def read_event(path: str) -> dict | None:
    """The event's pull request, or None for an event that isn't one."""
    file = Path(path)
    if not file.is_file():
        raise Failure(f"the event file {path} doesn't exist")
    try:
        event = json.loads(file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise Failure(f"the event file {path} isn't JSON: {error}") from None
    if not isinstance(event, dict):
        raise Failure(f"the event file {path} isn't a JSON object")
    pull = event.get("pull_request")
    return pull if isinstance(pull, dict) else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--event", default=os.environ.get("GITHUB_EVENT_PATH") or None)
    parser.add_argument("--body", type=Path)
    parser.add_argument("--author")
    args = parser.parse_args()
    try:
        root = repo_root()
        if args.body is not None:
            text = args.body.read_text(encoding="utf-8")
            return check_body(root, text, args.author, args.base)
        if args.event is None:
            return check_template(root, "no pull request event")
        pull = read_event(args.event)
        if pull is None:
            return check_template(root, "the event isn't a pull request's")
        author = (pull.get("user") or {}).get("login")
        return check_body(root, pull.get("body") or "", author, args.base)
    except Failure as failure:
        print(f"pr-body: {failure}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
