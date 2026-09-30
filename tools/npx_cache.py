# SPDX-License-Identifier: Apache-2.0
"""The lock beside npm's cache that a gate holds while npx installs its pinned packages (#347).

A module, not a script: `check-parity.py` (TypeDoc) and `check-dts-consumer.py` (tsc) import it,
so both installs take one lock and one implementation of it (#348).

npx installs into <cache>/_npx/<hash of the packages>, and two callers installing one set at
once extract over each other (TAR_ENTRY_ERROR, then no .bin/<tool> or a tree with files gone).
The lock sits beside the cache because that's what callers share: every worktree, `fire` worker
and `ci:local` clone uses the one `~/.npm`, while each agent on a shared host has its own
TMPDIR. The lock goes with the file, so a killed holder drops it. The two gates install
different sets but take the same lock file, so one waits for the other's install as well; each
holds it only for the locate that installs, and runs its tool after.

A waiter says so on stderr and then waits with no deadline, on purpose: a lock that gives up
and installs anyway brings the race back on exactly the slow cold install it's for, and a
waiter would only have made the same registry fetch. The caller's own timeout (`fire`'s per
command, CI's per job) bounds a hung holder.

The lock prevents new damage and doesn't repair old: a tree a lost race left half written
stays that way, because npx sees the directory and skips the install. Delete that
`_npx/<hash>` directory by hand.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@contextlib.contextmanager
def held(who: str, error: type[Exception]) -> Iterator[Path]:
    """Hold the lock for the body, and give it npm's cache directory.

    `who` starts the line a waiter prints, and `error` is the caller's exception type, raised
    when npm can't name its cache. The caller checks its tool's path against the directory it's
    given: a lost race can leave `command -v` to find another copy on PATH (#348).
    """
    # Imported here so a script that imports this module still loads where fcntl doesn't
    # exist: `check-parity.py --exemptions` reads no surface and never locks.
    import fcntl

    try:
        # From the root, where the gates run npx, so a project .npmrc is read the same way.
        done = subprocess.run(
            ["npm", "config", "get", "cache"],
            capture_output=True,
            text=True,
            cwd=ROOT,
            check=False,
        )
    except FileNotFoundError as missing:
        raise error(
            "npm locating its cache: npm isn't on PATH (`mise install` provides it)"
        ) from missing
    if done.returncode != 0:
        raise error(
            f"npm locating its cache exited {done.returncode}:\n{done.stdout}{done.stderr}"
        )
    printed = done.stdout.strip()
    cache = Path(printed)
    # A blank answer would lock a file in the working directory, which no other caller
    # shares, so the lock would hold nothing back.
    if not cache.is_absolute():
        raise error(
            f"npm config get cache printed {printed!r}, not a directory to lock"
        )
    cache.mkdir(parents=True, exist_ok=True)
    lock = cache / "microvms-agentd-npx.lock"
    with lock.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(
                f"{who}: waiting for {lock}, held by another npx install",
                file=sys.stderr,
            )
            fcntl.flock(handle, fcntl.LOCK_EX)
        yield cache
