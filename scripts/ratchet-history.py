#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
# SPDX-License-Identifier: Apache-2.0
"""Print the drift count's history as JSON, for the docs site's "Architecture drift" page.

One point per first-parent commit that changed `ratchet/drift.json`, oldest first: the commit,
its committer date, and the entry count per collected category. On main that's the series of
merges, one per PR that moved the count. A working tree whose file differs from HEAD's adds a
last point with no commit, so a local docs build shows the change being made.

The counts come from the file, not from rerunning the collectors at each commit: `ratchet:check`
holds the file equal to the tree on every commit that passes, so the file is the record.

Usage, from anywhere:

    ./scripts/ratchet-history.py              # JSON on stdout
    ./scripts/ratchet-history.py --root <dir> # a different checkout
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


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout


def point(sha: str | None, date: str, text: str) -> dict:
    file = RATCHET["parse"](json.loads(text), f"{sha or 'working tree'}:{DRIFT}")
    counts = Counter(entry["category"] for entry in file["entries"])
    return {
        "sha": sha,
        "date": date,
        "counts": {category: counts[category] for category in RATCHET["COLLECTED"]},
        "total": sum(counts.values()),
        "decisions": len(file["decisions"]),
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
                ["git", "-C", str(root), "cat-file", "-e", spec], capture_output=True
            ).returncode
            != 0
        ):
            continue
        last = git(root, "show", spec)
        points.append(point(sha, date, last))
    working = root / DRIFT
    if working.exists():
        text = working.read_text(encoding="utf-8")
        if last is None or json.loads(text) != json.loads(last):
            now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            points.append(point(None, now, text))
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
