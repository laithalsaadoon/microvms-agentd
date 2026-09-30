#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["towncrier==26.9.0"]
# ///
# SPDX-License-Identifier: Apache-2.0
"""Changelog fragments: their rules, the branch rule, the draft and the release build.

The next release's changelog is fragments in changelog.d/, and a branch that changes shipped
code adds one. CHANGELOG.md's `## Unreleased` was one list that every product PR appended to,
so any two PRs in flight conflicted there. Each entry is now a fragment, a file of its own named
for its issue and type, and towncrier (towncrier.toml) writes the fragments into CHANGELOG.md at
release. This script is the one place towncrier runs, so the pin in its header is the version
every run gets.

Three subcommands:

  `check [--base REF] [--author LOGIN]` (`mise run changelog:check`, in `check`; CI's
          `security` job on a pull request, with the pull request's base and author) fails when:

          - a file in changelog.d/ isn't a fragment. towncrier's template and `.gitkeep` are
            the only others allowed. A fragment is `<issue>.<type>.md`, or `<issue>.<type>.<n>.md`
            for another of that issue and type (what `towncrier create` names it): the issue and
            n are numbers with no leading zero, and the type is one towncrier.toml defines.
          - a fragment's text isn't what its type renders as. A type that renders takes one or
            more Markdown list items, each opening `- **` (the bold lead) with every later line
            indented two spaces, since towncrier writes the text in as it is
            (`all_bullets = false`). An `internal` fragment is never rendered, and needs only a
            line saying why.
          - the fragments don't build: `towncrier build --draft`, the build `release:prepare`
            runs, fails on them.
          - the branch changes shipped code and adds no fragment. The branch is the working
            tree, uncommitted and untracked files included, against its merge base with
            `--base` (origin/main by default), so a run before a commit sees what the commit
            will hold. Shipped code is a path under SHIPPED that NOT_SHIPPED doesn't match. A
            fragment added or changed counts, of any type, `internal` included, and so does a
            release build: a branch that changes CHANGELOG.md and removes fragments, which is
            what `release:prepare` leaves. An edit to CHANGELOG.md with no fragment removed
            doesn't count, because it's the hand-written entry fragments replace.
          - `--author dependabot[bot]` skips the rule above, and no other login does:
            Dependabot's dependency bumps have never taken an entry, and a bump that reaches
            shipped code is still only a bump. It still runs the others.
          - a SHIPPED path matches no file, so a crate that moves or is renamed fails here
            rather than dropping out of the set. The floor is the same rule: a file listing
            that came back empty matches nothing.
          - a Rust file NOT_SHIPPED matches isn't declared `#[cfg(test)] mod <name>;` by its
            parent module, so the exclusion can't hide code that ships.
          - towncrier.toml names no fragment directory or no type.

  `draft` prints the next release's section as `release:prepare` would write it, and writes
          nothing (`mise run changelog:draft`).

  `build VERSION` (`mise run release:prepare X.Y.Z`) runs `check`'s fragment rules, then
          `towncrier build --version VERSION --yes`: the fragments go into CHANGELOG.md as that
          release, under towncrier's marker above the latest one, and are removed, with both
          changes staged. It refuses a VERSION that isn't semver (a leading `v` is dropped, so
          release:tag's argument works too), and a build with no fragment that renders, which
          would write a heading over nothing: running it a second time does that.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from pathlib import Path

CONFIG = "towncrier.toml"
NEWS = "CHANGELOG.md"
# The one login whose pull requests take no fragment.
DEPENDABOT = "dependabot[bot]"
# What ships: each published crate's and the daemon's source, and the two bindings' generated
# type surfaces, which ship in the wheel and the npm package. A manifest isn't here: a
# dependency or version bump changes Cargo.toml, pyproject.toml or package.json, and neither
# has taken an entry. A directory ends in `/`; anything else is one file.
SHIPPED = (
    "protocol/src/",
    "agentd/src/",
    "microvms-domain/src/",
    "microvms-app/src/",
    "microvms-edges/src/",
    "microvms-core/src/",
    "microvms-cli/src/",
    "microvms-py/src/",
    "microvms-js/src/",
    "microvms-py/microvms.pyi",
    "microvms-js/index.d.ts",
)
# Rust files under SHIPPED that only a test build compiles: the fuzz harnesses and the CLI's
# guards. `check` holds each to a `#[cfg(test)] mod <name>;` in its parent module.
NOT_SHIPPED = (
    re.compile(r"^[^/]+/src/(?:.+/)?[a-z0-9_]+_fuzz\.rs$"),
    re.compile(r"^microvms-cli/src/guards\.rs$"),
)
# The files in changelog.d/ that aren't fragments, besides the template towncrier.toml names.
NOT_FRAGMENTS = {".gitkeep"}
FRAGMENT = re.compile(r"^(?P<issue>[^.]+)\.(?P<type>[^.]+)(?:\.(?P<n>[^.]+))?\.md$")
NUMBER = re.compile(r"^[1-9][0-9]*$")
SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$")
# The label a draft renders its heading with.
DRAFT_VERSION = "Unreleased"


class Failure(Exception):
    """A rule this tree breaks, with the message that says which."""


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


class Config:
    """What towncrier.toml says about the fragments."""

    def __init__(self, root: Path) -> None:
        path = root / CONFIG
        if not path.is_file():
            raise Failure(f"{CONFIG} doesn't exist at {root}")
        table = tomllib.loads(path.read_text()).get("tool", {}).get("towncrier", {})
        self.directory = table.get("directory", "")
        self.template = table.get("template", "")
        self.types = {
            entry.get("directory", ""): bool(entry.get("showcontent", True))
            for entry in table.get("type", [])
        }
        if not self.directory or not self.types:
            raise Failure(
                f"{CONFIG} names no fragment directory or no type; towncrier would fall back"
                " to its own defaults, which this repo's fragments aren't"
            )


def fragment_problems(root: Path, config: Config) -> tuple[dict[str, str], list[str]]:
    """Each fragment in changelog.d/ with its type, and each problem with a file there."""
    directory = root / config.directory
    template = Path(config.template)
    exempt = set(NOT_FRAGMENTS)
    if template.parent == Path(config.directory):
        exempt.add(template.name)
    fragments: dict[str, str] = {}
    problems: list[str] = []
    types = ", ".join(config.types)
    paths = sorted(directory.iterdir()) if directory.is_dir() else []
    for path in paths:
        rel = f"{config.directory}/{path.name}"
        if path.name in exempt:
            continue
        if not path.is_file():
            problems.append(
                f"{rel}: not a file; fragments sit directly in {config.directory}/"
            )
            continue
        name = FRAGMENT.match(path.name)
        if name is None:
            problems.append(
                f"{rel}: not a fragment name; a fragment is <issue>.<type>.md, or"
                " <issue>.<type>.<n>.md for another of that issue and type"
            )
            continue
        kind = name["type"]
        if kind not in config.types:
            problems.append(f"{rel}: `{kind}` isn't a type; {CONFIG} defines {types}")
            continue
        if not NUMBER.match(name["issue"]):
            problems.append(
                f"{rel}: `{name['issue']}` isn't an issue number; name the fragment for the"
                " issue it closes, or the pull request's number when there's none"
            )
            continue
        if name["n"] is not None and not NUMBER.match(name["n"]):
            problems.append(
                f"{rel}: `{name['n']}` isn't a counter; it's 1, 2, and so on"
            )
            continue
        fragments[rel] = kind
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            problems.append(f"{rel}: empty")
        elif config.types[kind]:
            problems.extend(f"{rel}: {why}" for why in list_item_problems(text))
    return fragments, problems


def list_item_problems(text: str) -> list[str]:
    """Why `text` isn't a run of bold-lead list items, if it isn't."""
    lines = text.rstrip("\n").split("\n")
    if not lines[0].startswith("- **"):
        return [
            "line 1 doesn't open an entry; an entry is a list item with a bold lead,"
            " `- **What changed (#<issue>).** Why it matters.`"
        ]
    for number, line in enumerate(lines[1:], start=2):
        if line.startswith("- **") or (line.startswith("  ") and line.strip()):
            continue
        what = (
            "is blank" if not line.strip() else "neither opens an entry nor is indented"
        )
        return [
            f"line {number} {what}; an entry's later lines are indented two spaces, and the"
            " next entry opens with `- **`"
        ]
    return []


def draft(root: Path, version: str = DRAFT_VERSION) -> str:
    """The section `towncrier build` would write for `version`; raises when it can't build."""
    done = subprocess.run(
        [sys.executable, "-m", "towncrier", "build", "--draft", "--version", version],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if done.returncode != 0:
        raise Failure(
            "`towncrier build --draft` fails on these fragments:\n"
            + (done.stderr.strip() or done.stdout.strip())
        )
    return done.stdout


def is_shipped(path: str) -> bool:
    inside = any(path.startswith(p) if p.endswith("/") else path == p for p in SHIPPED)
    return inside and not any(rule.match(path) for rule in NOT_SHIPPED)


def shipped_set_problems(root: Path) -> list[str]:
    """A SHIPPED path that matches no file, or an exclusion that covers shipped code."""
    # Untracked files as well, as the branch rule reads them, so a new module is held before
    # it's committed.
    listing = git(root, "ls-files", "--cached", "--others", "--exclude-standard")
    files = listing.splitlines()
    problems = [
        f"SHIPPED names {p}, which matches no file in the tree; a crate moved or was"
        " renamed, so say where its source is now"
        for p in SHIPPED
        if not any(f.startswith(p) if p.endswith("/") else f == p for f in files)
    ]
    for path in files:
        inside = any(path.startswith(p) for p in SHIPPED if p.endswith("/"))
        if inside and any(rule.match(path) for rule in NOT_SHIPPED):
            if not declared_for_tests(root, Path(path)):
                problems.append(
                    f"NOT_SHIPPED covers {path}, and no `#[cfg(test)] mod {Path(path).stem};`"
                    " in its parent module declares it, so it may be compiled into what ships"
                )
    return problems


def declared_for_tests(root: Path, path: Path) -> bool:
    """Whether the module file `path` is declared `#[cfg(test)] mod <stem>;` by its parent."""
    folder = path.parent
    parents = [folder / name for name in ("lib.rs", "main.rs", "mod.rs")]
    parents.append(folder.parent / f"{folder.name}.rs")
    declaration = re.compile(rf"^\s*(?:pub(?:\([^)]*\))?\s+)?mod\s+{path.stem}\s*;")
    for parent in parents:
        file = root / parent
        if not file.is_file():
            continue
        lines = file.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines):
            if declaration.match(line):
                above = [text.strip() for text in lines[:number] if text.strip()]
                return bool(above) and above[-1] == "#[cfg(test)]"
    return False


def branch_changes(root: Path, base: str) -> tuple[set[str], set[str]]:
    """The paths the working tree changes against its merge base with `base`, and the deleted."""
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
    changed = set(git(root, "diff", "--name-only", "--no-renames", fork).splitlines())
    changed |= set(git(root, "ls-files", "--others", "--exclude-standard").splitlines())
    deleted = git(root, "diff", "--name-only", "--no-renames", "--diff-filter=D", fork)
    return changed, set(deleted.splitlines())


def check(root: Path, base: str, author: str | None) -> int:
    config = Config(root)
    problems = shipped_set_problems(root)
    fragments, named = fragment_problems(root, config)
    problems += named
    if not problems:
        try:
            draft(root)
        except Failure as failure:
            problems.append(str(failure))
    if problems:
        print(f"changelog: {len(problems)} problems:")
        for problem in problems:
            print(f"  {problem}")
        return 1
    print(
        f"changelog: {len(fragments)} fragments in {config.directory}/, and they build"
    )

    changed, deleted = branch_changes(root, base)
    shipped = sorted(p for p in changed if is_shipped(p))
    if not shipped:
        print(f"changelog: this branch changes no shipped code against {base}")
        return 0
    if author == DEPENDABOT:
        print(
            f"changelog: {len(shipped)} shipped files change, and {DEPENDABOT}'s pull requests"
            " take no fragment"
        )
        return 0
    added = sorted(set(fragments) & changed)
    prefix = f"{config.directory}/"
    removed = sorted(
        p for p in deleted if p.startswith(prefix) and FRAGMENT.match(p[len(prefix) :])
    )
    if added:
        print(
            f"changelog: {len(shipped)} shipped files change, with {', '.join(added)}"
        )
        return 0
    if removed and NEWS in changed:
        print(
            f"changelog: {len(shipped)} shipped files change in a release build, which writes"
            f" {len(removed)} fragments into {NEWS}"
        )
        return 0
    print(
        f"changelog: this branch changes shipped code against {base} and adds no fragment"
        f" in {config.directory}/:"
    )
    for path in shipped[:10]:
        print(f"  {path}")
    if len(shipped) > 10:
        print(f"  and {len(shipped) - 10} more")
    if NEWS in changed:
        print(
            f"{NEWS} changed too, and a hand-written entry there is what fragments replace."
        )
    print(
        f"Add {config.directory}/<issue>.<type>.md with the entry (CONTRIBUTING.md,"
        f' "Changelog"), or {config.directory}/<issue>.internal.md saying why no user of the'
        " crates, the CLI or the bindings can observe the change."
    )
    return 1


def build(root: Path, version: str) -> int:
    version = version.removeprefix("v")
    if not SEMVER.match(version):
        raise Failure(f"`{version}` isn't a version; release:prepare takes X.Y.Z")
    config = Config(root)
    fragments, problems = fragment_problems(root, config)
    if problems:
        print(f"changelog: {len(problems)} problems:")
        for problem in problems:
            print(f"  {problem}")
        return 1
    if not any(config.types[kind] for kind in fragments.values()):
        raise Failure(
            f"{config.directory}/ has no fragment that renders, so {version} would be a heading"
            " over nothing; a second run after a build finds exactly this"
        )
    done = subprocess.run(
        [sys.executable, "-m", "towncrier", "build", "--version", version, "--yes"],
        cwd=root,
    )
    if done.returncode == 0:
        print(
            f"changelog: {NEWS} has {version} and {len(fragments)} fragments are removed, both"
            " staged. Review the section, then commit it with the version bump."
        )
    return done.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    checking = commands.add_parser(
        "check", help="the fragment rules and the branch rule"
    )
    checking.add_argument(
        "--base", default="origin/main", help="the branch this one merges into"
    )
    checking.add_argument("--author", help="the pull request's author login")
    commands.add_parser("draft", help="print the next release's section")
    building = commands.add_parser(
        "build", help="write the fragments into CHANGELOG.md"
    )
    building.add_argument("version", help="X.Y.Z")
    args = parser.parse_args()
    try:
        root = repo_root()
        if args.command == "check":
            return check(root, args.base, args.author)
        if args.command == "draft":
            Config(root)
            print(draft(root), end="")
            return 0
        return build(root, args.version)
    except Failure as failure:
        print(f"changelog: {failure}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
