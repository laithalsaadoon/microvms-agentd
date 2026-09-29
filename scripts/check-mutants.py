#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
# SPDX-License-Identifier: Apache-2.0
"""Mutation-test the Rust a change touches, and fail on any mutant its tests don't catch (#275).

A test can run the code it names and assert nothing about the result. Coverage counts that as
tested, and so does the traceability matrix. cargo-mutants asks the direct question: it changes
one thing in the code (a return value, an operator, a deleted `!`), runs the tests, and reports
the change as missed when they still pass. This runs it over the lines a change touches, not the
whole tree, so a pull request pays for its own diff.

What it does:

  1. Writes `git diff <merge base of --base and HEAD> -- '*.rs'` against the working tree, so a
     local run sees uncommitted edits to tracked files (untracked ones aren't in it; `git add`
     them first). A base git can't resolve fails: a shallow checkout with no base ref must
     not read as a change with no Rust in it. The diff's `a/` and `b/` prefixes are set on
     the command line, since cargo-mutants reads each path off `+++ b/<path>` and a user's
     `diff.mnemonicPrefix` or `diff.noprefix` would otherwise hand it paths that match no file,
     which it reports as nothing to mutate and passes.
  2. With no `.rs` file in that diff, says so and passes without running anything.
  3. Holds `PACKAGES` and `LEFT_OUT` to the workspace's members (`cargo metadata --no-deps`):
     a member in neither fails, and so does a name in either that's no member. A new crate
     would otherwise never be mutated, and a renamed one only draws a warning from `-p`.
  4. Otherwise runs `cargo mutants --in-diff <diff> --no-shuffle` over `PACKAGES`, with any
     arguments after `--` appended (`-- --shard 1/4`, `-- -j 3`). The rest of the settings,
     the exclusions among them, are in `.cargo/mutants.toml`, which cargo-mutants reads itself.
     The package list is here because that file has no key for it.
  5. Passes only on cargo-mutants' exit 0. Every other code fails with its own exit code and a
     line saying what it means; a missed or timed-out mutant is printed by name from
     `mutants.out/`, where each one's diff and test log are.

`--detect` stops after step 1 and prints `rust=true` or `rust=false`, for CI's first step to
append to `$GITHUB_OUTPUT` before it installs a toolchain. It's the same code as a full run, so
the job's gate and the wrapper can't disagree about what counts as a change to Rust. It needs no
cargo and none of the script's dependencies, so the runner's `python3` runs it.

`--diff FILE` mutates a diff someone else wrote instead of the change against `--base` (the
registry's `fuzz_extract` entry hands it one function). A diff handed in must name a Rust
file, or it fails: cargo-mutants passes a diff with no Rust in it, so an empty file or a
generator that found nothing would read as a clean run.

cargo-mutants builds each mutant in a copy of the tree, and a copy's crates have the caller's
workspace-relative paths. Sharing `$CARGO_TARGET_DIR` with the caller, a later build of the
caller's tree can trust an artifact built from a mutated copy, since its sources are older.
So unless the run is `--in-place` (one tree, whose target is the one to reuse), the variable
is left out of cargo-mutants' environment and each copy builds in its own `target`.

A diff that touches only test code yields no mutants and passes. That's cargo-mutants'
documented behavior and it's right here: the question is about the code under test.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# Every package whose own tests can catch a mutant in it.
PACKAGES = (
    "microvms-protocol",
    "microvms-domain",
    "microvms-app",
    "microvms-edges",
    "microvms-core",
    "microvms-cli",
    "agentd",
)

# The workspace's other members, each with why it isn't mutated. Every member is in exactly one
# of the two; step 3 fails otherwise.
LEFT_OUT = {
    "agentd-model": "a Stateright checker whose properties are its tests, so a mutant there "
    "asks whether the model checks itself",
    "model-conformance": "tests only: its library is empty, so there's nothing in it to "
    "mutate. Its tests hold the app to the model, and the app's mutants run the app's own tests",
    "microvms-py": "no Rust tests: its suite is pytest, which cargo-mutants can't run, so "
    "every mutant in it would be reported missed",
    "microvms-js": "no Rust tests: its suite is `node --test`, which cargo-mutants can't run",
}

# cargo-mutants 27.1.0's exit codes (src/exit_code.rs) and what each means for a PR. The ones
# not named here (1 for bad arguments or config, 70 for an internal error) fail too.
MEANING = {
    2: "MISSED: a test suite passed with a mutant in the code it covers. Strengthen the test "
    "that should have caught it, or, for code no offline test can reach, add an exclusion "
    "to .cargo/mutants.toml with the reason and the check that does cover it",
    3: "TIMEOUT: a mutant made the tests run past the timeout. That's a mutant they didn't "
    "catch in time; resolve it the way a missed one is resolved, not with a longer timeout",
    4: "the unmutated baseline failed its build or its tests, so no mutant result means "
    "anything. mutants.out/log/baseline.log has the output",
    5: "the diff doesn't match the tree: rebase onto the base branch and rerun",
    6: "the diff doesn't parse",
}

# The `mutants.out` file that names the mutants each failing code is about.
LISTED = {2: "missed.txt", 3: "timeout.txt"}

# A unified diff's new-side header for a Rust file: `+++ b/agentd/src/fs.rs`.
RUST_HEADER = re.compile(r"^\+\+\+ (?:b/)?\S+\.rs\s*$", re.MULTILINE)


def git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(
            f"check-mutants: git {' '.join(args)} failed: {result.stderr.strip()}"
        )
    return result.stdout


# The shape cargo-mutants parses, whatever the user's git config says: `--- a/` and `+++ b/`
# headers, no color codes, and git's own diff rather than a `diff.external` program.
DIFF_FORMAT = ("--no-ext-diff", "--no-color", "--src-prefix=a/", "--dst-prefix=b/")


def rust_changes(root: Path, base: str) -> tuple[str, list[str]]:
    """The merge base with `base`, and the `.rs` files the working tree changes against it."""
    # From the root, since a pathspec is relative to the directory git runs in.
    merge_base = git("merge-base", base, "HEAD", cwd=root).strip()
    names = git("diff", "--name-only", merge_base, "--", "*.rs", cwd=root).split()
    return merge_base, names


def write_diff(root: Path, base: str, dest: Path) -> bool:
    """Write the `.rs` diff against `base`'s merge base to `dest`. False when it has no Rust."""
    merge_base, names = rust_changes(root, base)
    if not names:
        return False
    diff = git("diff", *DIFF_FORMAT, merge_base, "--", "*.rs", cwd=root)
    if not diff.strip():
        # `--name-only` listed files the diff itself doesn't show; handing over an empty diff
        # would mutate nothing and pass.
        raise SystemExit(
            f"check-mutants: git listed {names} but the diff against {base} is empty"
        )
    dest.write_text(diff)
    return True


def check_members(cargo: str, root: Path) -> None:
    """Fail unless the workspace's members are exactly `PACKAGES` and `LEFT_OUT`."""
    answer = subprocess.run(
        [cargo, "metadata", "--no-deps", "--format-version", "1"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if answer.returncode != 0:
        raise SystemExit(
            f"check-mutants: cargo metadata failed: {answer.stderr.strip()}"
        )
    metadata = json.loads(answer.stdout) or {}
    ids = set(metadata.get("workspace_members") or [])
    members = {
        package["name"]
        for package in metadata.get("packages") or []
        if package["id"] in ids
    }
    listed = set(PACKAGES) | set(LEFT_OUT)
    problems = [
        f"{name} is a workspace member in neither PACKAGES nor LEFT_OUT: say which, and in "
        "LEFT_OUT why its tests can't catch a mutant in it"
        for name in sorted(members - listed)
    ] + [
        f"{name} is in {'PACKAGES' if name in PACKAGES else 'LEFT_OUT'} but no workspace "
        "member has that name"
        for name in sorted(listed - members)
    ]
    if problems:
        raise SystemExit(
            "check-mutants: the package list doesn't match the workspace:\n  "
            + "\n  ".join(problems)
        )


def given_diff(path: Path) -> Path:
    """`path`, once it's shown to name a Rust file."""
    try:
        text = path.read_text()
    except OSError as error:
        raise SystemExit(
            f"check-mutants: can't read {path}: {error.strerror}"
        ) from None
    if not RUST_HEADER.search(text):
        raise SystemExit(
            f"check-mutants: {path} names no Rust file, so there's nothing to mutate"
        )
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--base", default="origin/main", help="the branch the change goes into"
    )
    source.add_argument(
        "--diff",
        type=Path,
        help="mutate this diff instead of the change against --base",
    )
    parser.add_argument(
        "--detect",
        action="store_true",
        help="print rust=true or rust=false for the change against --base, and stop",
    )
    parser.add_argument(
        "--cargo", default="cargo", help="the cargo to run (the tests use a fake)"
    )
    parser.add_argument(
        "extra", nargs="*", help="after `--`: more cargo-mutants arguments"
    )
    args = parser.parse_args()
    # The tree the caller is in, not the one this script sits in, so the tests can run it
    # over a throwaway repo.
    root = Path(git("rev-parse", "--show-toplevel").strip())

    if args.detect:
        if args.diff:
            parser.error("--detect answers for --base, not for a diff handed in")
        _, names = rust_changes(root, args.base)
        print(
            "\n".join(names) or f"no Rust changes against {args.base}", file=sys.stderr
        )
        print(f"rust={'true' if names else 'false'}")
        return 0

    env = dict(os.environ)
    if "--in-place" not in args.extra and env.pop("CARGO_TARGET_DIR", None):
        print(
            "check-mutants: CARGO_TARGET_DIR is left out, so each copy builds in its own target"
        )

    with tempfile.TemporaryDirectory(prefix="check-mutants-") as scratch:
        if args.diff:
            diff = given_diff(args.diff.resolve())
        else:
            diff = Path(scratch) / "pr.diff"
            if not write_diff(root, args.base, diff):
                print(
                    f"check-mutants: no Rust changes against {args.base}; nothing to mutate"
                )
                return 0
        check_members(args.cargo, root)
        argv = [args.cargo, "mutants", "--in-diff", str(diff), "--no-shuffle"]
        for package in PACKAGES:
            argv += ["-p", package]
        argv += args.extra
        print("check-mutants:", " ".join(argv), flush=True)
        code = subprocess.run(argv, cwd=root, env=env).returncode

    if code == 0:
        return 0
    meaning = MEANING.get(code, "cargo-mutants failed; its output above says why")
    print(f"\ncheck-mutants: cargo-mutants exit {code}: {meaning}", file=sys.stderr)
    if code in LISTED:
        listed = Path("mutants.out") / LISTED[code]
        try:
            names = (root / listed).read_text().rstrip() or "(empty)"
        except FileNotFoundError:
            names = "(cargo-mutants wrote no list)"
        print(f"\n{listed}:\n{names}", file=sys.stderr)
    # A signal shows as a negative code, which isn't an exit status.
    return code if code > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
