#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Run Vale over maintained Markdown and enforce per-rule warning ceilings.

The input list comes from git so ignored build trees never enter the scan. The
traceability table is generated from requirement records; correct its generator
instead of editing its prose. Errors always fail. Warning ceilings are tracked
per rule so improving one rule cannot pay for a regression in another.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = Path(".vale/warnings.json")
GENERATED = {"docs/TRACEABILITY.md"}
TOP_LEVEL = {"README.md", "AGENTS.md", "CONTRIBUTING.md", "SECURITY.md"}


class Failure(Exception):
    """A missing input or an invalid linter response."""


def in_scope(name: str) -> bool:
    path = Path(name)
    if name in GENERATED:
        return False
    if name in TOP_LEVEL:
        return True
    if path.suffix not in {".md", ".mdx"}:
        return False
    if name.startswith(("docs/", "site/authored/")):
        return True
    return (
        name.startswith(("bindings/", "crates/", "examples/"))
        and path.name == "README.md"
    )


def git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=False
    )


def sources(root: Path) -> list[str]:
    result = git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    if result.returncode:
        raise Failure(f"Cannot enumerate prose: {result.stderr.strip()}")
    names = sorted(
        {p for p in result.stdout.split("\0") if in_scope(p) and (root / p).is_file()}
    )
    contents = [(root / name).read_text() for name in names]
    if not names or not any(text.strip() for text in contents):
        raise Failure("No nonempty prose inputs; refusing an empty scan.")
    return names


def ceilings(text: str) -> dict[str, int]:
    try:
        data = json.loads(text)
    except ValueError as error:
        raise Failure("Invalid warning baseline JSON.") from error
    if (
        not isinstance(data, dict)
        or type(data.get("version")) is not int
        or data["version"] != 1
    ):
        raise Failure("Warning baseline requires version 1.")
    rules = data.get("warnings")
    if not isinstance(rules, dict):
        raise Failure("Warning baseline must map rules to ceilings.")
    for rule, count in rules.items():
        if not isinstance(rule, str) or not rule or type(count) is not int or count < 0:
            raise Failure(
                "Each warning ceiling must name a rule and a nonnegative integer."
            )
    return rules


def alerts(text: str) -> list[dict]:
    try:
        data = json.loads(text)
    except ValueError as error:
        raise Failure("Vale returned invalid JSON.") from error
    if not isinstance(data, dict):
        raise Failure("Vale report must map files to alerts.")
    result = []
    for name, rows in data.items():
        if not isinstance(name, str) or not isinstance(rows, list):
            raise Failure("Vale report contains an invalid file entry.")
        for row in rows:
            if (
                not isinstance(row, dict)
                or row.get("Severity") not in {"error", "warning", "suggestion"}
                or not isinstance(row.get("Check"), str)
                or not row["Check"]
            ):
                raise Failure("Vale report contains an invalid alert.")
            result.append(dict(row, File=name))
    return result


def verdict(rows: list[dict], limits: dict[str, int]) -> list[str]:
    errors = [r for r in rows if r["Severity"] == "error"]
    warnings = Counter(r["Check"] for r in rows if r["Severity"] == "warning")
    failures = [
        f"{r['File']}:{r.get('Line', '?')}: {r['Check']}: {r.get('Message', 'prose error')}"
        for r in errors
    ]
    failures += [
        f"{rule}: {count} warnings exceed ceiling {limits.get(rule, 0)}"
        for rule, count in sorted(warnings.items())
        if count > limits.get(rule, 0)
    ]
    return failures


def baseline_growth(root: Path, current: dict[str, int], base: str) -> list[str]:
    ancestor = git(root, "merge-base", "HEAD", base)
    if ancestor.returncode:
        raise Failure(f"Cannot resolve prose baseline base {base}.")
    reference = ancestor.stdout.strip()
    tree = git(root, "ls-tree", reference, "--", str(BASELINE))
    if tree.returncode:
        raise Failure("Cannot inspect the base warning baseline.")
    if not tree.stdout.strip():
        return []
    previous = git(root, "show", f"{reference}:{BASELINE}")
    if previous.returncode:
        raise Failure("Cannot read the base warning baseline.")
    old = ceilings(previous.stdout)
    return [
        f"{rule}: warning ceiling increased from {old.get(rule, 0)} to {count}"
        for rule, count in sorted(current.items())
        if count > old.get(rule, 0)
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--lint",
        action="store_true",
        help="display every alert without warning ceilings",
    )
    parser.add_argument("--base", default=os.environ.get("GATE_BASE", "origin/main"))
    args = parser.parse_args(argv)
    try:
        names = sources(ROOT)
        level = "suggestion" if args.lint else "warning"
        command = ["vale", "--no-global", f"--minAlertLevel={level}"]
        if args.lint:
            return subprocess.run([*command, *names], cwd=ROOT, check=False).returncode
        limits = ceilings((ROOT / BASELINE).read_text())
        growth = baseline_growth(ROOT, limits, args.base)
        done = subprocess.run(
            [*command, "--output=JSON", *names],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if done.returncode not in (0, 1):
            raise Failure(f"Vale failed ({done.returncode}): {done.stderr.strip()}")
        rows = alerts(done.stdout)
        if done.returncode == 1 and not any(row["Severity"] == "error" for row in rows):
            raise Failure("Vale failed without an error alert.")
        counts = Counter(r["Severity"] for r in rows)
        print(
            f"Prose: {counts['error']} errors, {counts['warning']} warnings, {counts['suggestion']} suggestions."
        )
        failures = growth + verdict(rows, limits)
        for failure in failures:
            print(failure, file=sys.stderr)
        return 1 if failures else 0
    except (Failure, OSError) as error:
        print(f"prose: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
