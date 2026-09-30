#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
# SPDX-License-Identifier: Apache-2.0
"""Print the drift count's history as JSON, for the docs site's "Architecture drift" page.

One point per first-parent commit that changed `verify/ratchet/drift.json`, oldest first: the commit,
its committer date, and the drift per collected category. A working tree whose file differs from
HEAD's adds a last point with no commit, so a local docs build shows a rewrite before it lands.

The file was two things over time, and both are read. Up to the merge-base rule it was the
hand-kept count (version 1), which `ratchet:check` held equal to the tree on every commit, so
each merge that moved the count is a point. Since then it's the generated snapshot (version 2),
the tree's drift at the commit that last rewrote it (`./tools/ratchet.py snapshot`), so each
rewrite is a point.

A category that commit's own `tools/ratchet.py` didn't collect yet has a null count there,
not a zero: nobody measured it, and its first drift arrives with the commit that starts
collecting it.

Usage, from anywhere:

    ./tools/ratchet-history.py              # JSON on stdout
    ./tools/ratchet-history.py --root <dir> # a different checkout
"""

import argparse
import json
import runpy
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
RATCHET = runpy.run_path(str(HERE / "ratchet.py"))
DRIFT = RATCHET["DRIFT"]
SCRIPT = RATCHET["SCRIPT"]


def git(root: Path, *args: str) -> str:
    # Without the hook's git pointers, which would override `-C root` (ratchet.py's
    # `GIT_ENV_LEAKS`).
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
        env=RATCHET["clean_env"](),
    ).stdout


def counts_of(text: str, where: str) -> tuple[Counter, int]:
    """The drift per category in a copy of `drift.json`, and how many decisions it counted.

    Either version: 1 lists `entries` and `decisions`, and 2 maps each category to its drift
    and its decisions' count. Anything else is an error naming `where`, not an empty point.
    """

    def fail(message: str):
        raise SystemExit(f"{where}: {message}")

    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        fail(f"not JSON: {error}")
    version = data.get("version") if isinstance(data, dict) else None
    if version == 1:
        entries, decisions = data.get("entries"), data.get("decisions")
        if not isinstance(entries, list) or not isinstance(decisions, list):
            fail("a version 1 file lists entries and decisions")
        if not all(
            isinstance(e, dict) and isinstance(e.get("category"), str) for e in entries
        ):
            fail("each entry names its category")
        return Counter(entry["category"] for entry in entries), len(decisions)
    if version == 2:
        drift, decisions = data.get("drift"), data.get("decisions")
        if (
            not isinstance(drift, dict)
            or not all(isinstance(keys, list) for keys in drift.values())
            or not isinstance(decisions, dict)
            or not all(isinstance(n, int) and n >= 0 for n in decisions.values())
        ):
            fail(
                "a version 2 file maps each category to its drift and its decisions' count"
            )
        return (
            Counter({category: len(keys) for category, keys in drift.items()}),
            sum(decisions.values()),
        )
    fail(f"unknown version {version!r}")


def point(sha: str | None, date: str, text: str, collected: tuple[str, ...]) -> dict:
    counts, decisions = counts_of(text, f"{sha or 'working tree'}:{DRIFT}")
    return {
        "sha": sha,
        "date": date,
        "counts": {
            category: counts[category] if category in collected else None
            for category in RATCHET["COLLECTED"]
        },
        "total": sum(counts.values()),
        "decisions": decisions,
    }


def history(root: Path) -> list[dict]:
    log = git(
        root, "log", "--first-parent", "--reverse", "--format=%H %cI", "--", DRIFT
    )
    points = []
    last = None
    for line in log.splitlines():
        sha, date = line.split(" ", 1)
        spec = f"{sha}:{DRIFT}"
        # The commit that deletes the file has no copy to count.
        if (
            subprocess.run(
                ["git", "-C", str(root), "cat-file", "-e", spec],
                capture_output=True,
                env=RATCHET["clean_env"](),
            ).returncode
            != 0
        ):
            continue
        last = git(root, "show", spec)
        points.append(point(sha, date, last, RATCHET["read_base_collected"](root, sha)))
    working = root / DRIFT
    if working.exists():
        text = working.read_text(encoding="utf-8")
        if last is None or json.loads(text) != json.loads(last):
            now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            script = root / SCRIPT
            collected = RATCHET["collected_from"](
                script.read_text(encoding="utf-8"), str(script)
            )
            points.append(point(None, now, text, collected))
    return points


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, default=HERE.parent)
    args = parser.parse_args(argv)
    document = {
        "source": DRIFT,
        "categories": list(RATCHET["COLLECTED"]),
        "notCollected": RATCHET["NOT_COLLECTED"],
        "points": history(args.root),
    }
    print(json.dumps(document, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
