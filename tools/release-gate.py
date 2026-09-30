#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml==6.0.3"]
# ///
# SPDX-License-Identifier: Apache-2.0
"""Nothing a release makes public goes out before a live run passes on the tag's draft.

`release.yml` runs on a tag. It builds every artifact, attests them, and creates the GitHub
release as a draft, which only an account that can push can see. The live suite then runs on
that tag in `live-conformance.yml`, against the draft's own `agentd` and Linux CLI, and uploads
a `live-verified` marker when every step passed. `release.yml`'s `live-gate` job waits in the
`release` environment until a reviewer approves it, then runs `verify` below, and every job
that publishes needs it. The order exists because the live quickstart fetches the daemon of the
CLI's own version: before this split, the tag published to three registries at once, so a live
run on the release commit could only fail until the release it was meant to gate was out.

Two subcommands:

  `graph`   (`mise run release:check`, in `check`; release.yml's `guard` job) reads
            release.yml and fails when:
            - a job that publishes doesn't name `live-gate` in its own `needs`. Transitive
              isn't enough: a job that needs another publisher would lose the gate the day
              that one's `needs` changed;
            - a job that publishes doesn't run in the `release` environment, the one every
              registry's trusted publisher names and the one a reviewer approves;
            - a job that publishes has an `if:` calling `always()`, `failure()` or
              `cancelled()`: each runs the job after a needed job failed, the gate included;
            - `live-gate` is missing, doesn't run `tools/release-gate.py verify`, or sets
              `continue-on-error` on itself or on that step, which would let a refused gate
              count as passed;
            - the file has no jobs (the floor), or no job runs one of `SENTINELS`, the
              publishes every release makes. Either means the rules above read less than the
              release does.

            A job publishes when one of its steps matches `PUBLISH_RUN` or `PUBLISH_USES`:
            `cargo publish`, `npm publish`, `twine upload`, `uv publish`, `maturin publish`
            or `upload` (each but a `--dry-run`), `gh release create` without `--draft`,
            anything that sets `draft=false`, or the PyPI publish and crates.io auth actions.
            So dropping `--draft` from the job that drafts the release makes it a publishing
            job that doesn't wait on the gate, and that fails here. The rules read release.yml
            only: every trusted publisher names that file, so a publish from another workflow
            gets no credential.

  `verify`  (release.yml's `live-gate` job) asks the Actions API for live-conformance.yml's
            runs on the tagged commit and passes when one of them, newest first:
            - is live-conformance.yml, dispatched (`workflow_dispatch`) on the tag itself, on
              the tagged commit, and completed with `success`;
            - uploaded a `live-verified` artifact whose marker names the tag, the commit, and
              exactly this run's `SHA256SUMS` (`--sums`, the draft job's). That's what ties the
              run to this draft rather than to an earlier draft of the same tag.
            It prints why each other run didn't count. The workflow at the tagged commit is the
            reviewed one on main (`guard` refuses a tag that isn't on main), and only that run's
            own jobs can upload its artifacts, so the marker is as trustworthy as that file.

`decide` is the rule `verify` applies, apart from the API calls so the unit tests
(`tools/test_release_gate.py`) can hand it runs and markers.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ".github/workflows/release.yml"
LIVE_WORKFLOW = ".github/workflows/live-conformance.yml"

#: The job every publishing job needs, and the environment they all run in.
GATE = "live-gate"
ENVIRONMENT = "release"
#: What the gate job must run for its approval to mean anything.
VERIFY = "tools/release-gate.py verify"

#: The artifact a passing tag run of live-conformance.yml uploads, and its one file.
MARKER_ARTIFACT = "live-verified"
MARKER_FILE = f"{MARKER_ARTIFACT}.json"

_NOT_DRY = r"(?![^\n]*--dry-run)"
#: A step's `run` text that publishes, by label. Matched per logical line (a `\` continuation
#: joins the next).
PUBLISH_RUN: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("cargo publish", re.compile(r"\bcargo\s+publish\b" + _NOT_DRY)),
    ("npm publish", re.compile(r"\bnpm\s+publish\b" + _NOT_DRY)),
    ("twine upload", re.compile(r"\btwine\s+upload\b")),
    ("uv publish", re.compile(r"\buv\s+publish\b" + _NOT_DRY)),
    ("maturin publish", re.compile(r"\bmaturin\s+(?:publish|upload)\b")),
    ("draft=false", re.compile(r"\bdraft=false\b")),
)
#: `gh release create`, which publishes unless the same command drafts.
RELEASE_CREATE = re.compile(r"\bgh\s+release\s+create\b")
DRAFT_FLAG = re.compile(r"\s(?:--draft(?:=true)?|-d)(?=\s|$)")
#: A step's `uses` that publishes, by label: the action's name before its `@`.
PUBLISH_USES = {
    "pypa/gh-action-pypi-publish": "PyPI",
    "rust-lang/crates-io-auth-action": "crates.io",
}
#: The publishes every release makes, by label, and where each one goes: a detector that finds
#: none of one reads less than the file does.
SENTINELS = {
    "cargo publish": "crates.io",
    "npm publish": "npm",
    "pypa/gh-action-pypi-publish": "PyPI",
    "draft=false": "the GitHub release",
}

#: The status functions that run a job after one it needs failed.
OVERRIDES = re.compile(r"\b(?:always|failure|cancelled)\s*\(")


def logical_lines(text: str) -> list[str]:
    """`text` split into shell lines, a trailing `\\` joining the next."""
    return re.sub(r"\\\n", " ", text).splitlines()


def publishes(step: dict) -> list[str]:
    """The labels of what `step` publishes, empty when it publishes nothing."""
    found = []
    uses = step.get("uses")
    if isinstance(uses, str):
        found += [name for name in PUBLISH_USES if uses.split("@")[0] == name]
    run = step.get("run")
    if isinstance(run, str):
        for line in logical_lines(run):
            found += [label for label, pattern in PUBLISH_RUN if pattern.search(line)]
            if RELEASE_CREATE.search(line) and not DRAFT_FLAG.search(line):
                found.append("gh release create without --draft")
    return found


def needs_of(job: dict) -> list[str]:
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs or [])


def environment_of(job: dict) -> str | None:
    environment = job.get("environment")
    if isinstance(environment, dict):
        environment = environment.get("name")
    return environment if isinstance(environment, str) else None


def truthy(value: object) -> bool:
    """A `continue-on-error` that can be true: `true`, or an expression that might be."""
    return value not in (None, False, "false")


def graph(workflow: dict) -> tuple[list[str], dict[str, list[str]]]:
    """The problems in `workflow`'s job graph, and each publishing job with what it publishes."""
    jobs = workflow.get("jobs") if isinstance(workflow, dict) else None
    if not isinstance(jobs, dict) or not jobs:
        return ([f"{WORKFLOW} has no jobs, so there's nothing to hold to the gate"], {})
    problems: list[str] = []
    publishing: dict[str, list[str]] = {}
    for name, job in jobs.items():
        steps = (job.get("steps") or []) if isinstance(job, dict) else []
        labels = [
            label
            for step in steps
            if isinstance(step, dict)
            for label in publishes(step)
        ]
        if labels:
            publishing[name] = labels

    for name, labels in publishing.items():
        job = jobs[name]
        what = ", ".join(f"`{label}`" for label in dict.fromkeys(labels))
        if GATE not in needs_of(job):
            problems.append(
                f"job `{name}` publishes ({what}) and doesn't name `{GATE}` in its `needs`, "
                "so it can run before a live run passed on the tag's draft"
            )
        if environment_of(job) != ENVIRONMENT:
            problems.append(
                f"job `{name}` publishes ({what}) outside the `{ENVIRONMENT}` environment, "
                "which every trusted publisher names and a reviewer approves"
            )
        condition = job.get("if")
        if isinstance(condition, str) and OVERRIDES.search(condition):
            problems.append(
                f"job `{name}` publishes ({what}) with `if: {condition}`, which runs it after "
                f"a job it needs failed, `{GATE}` included"
            )

    found = {label for labels in publishing.values() for label in labels}
    for sentinel, where in SENTINELS.items():
        if sentinel not in found:
            problems.append(
                f"no job in {WORKFLOW} runs `{sentinel}` ({where}): either the detector "
                "stopped matching it or the publish is gone, and the rules above read less "
                "than the release does"
            )

    gate = jobs.get(GATE)
    if not isinstance(gate, dict):
        problems.append(
            f"{WORKFLOW} has no `{GATE}` job for the publishing jobs to wait on"
        )
        return problems, publishing
    verify = [
        step
        for step in gate.get("steps") or []
        if isinstance(step, dict) and VERIFY in str(step.get("run", ""))
    ]
    if not verify:
        problems.append(
            f"job `{GATE}` doesn't run `{VERIFY}`, so approving it proves nothing about a live run"
        )
    if truthy(gate.get("continue-on-error")) or any(
        truthy(step.get("continue-on-error")) for step in verify
    ):
        problems.append(
            f"job `{GATE}` sets `continue-on-error`, which counts a refused gate as passed"
        )
    return problems, publishing


def run_graph(root: Path) -> int:
    path = root / WORKFLOW
    try:
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        print(f"release gate: FAILED\n  can't read {path}: {error}")
        return 1
    problems, publishing = graph(workflow or {})
    if problems:
        print("release gate: FAILED")
        for problem in problems:
            print(f"  {problem}")
        return 1
    jobs = ", ".join(f"`{name}`" for name in publishing)
    print(
        f"release gate: ok, {len(publishing)} jobs publish ({jobs}), each needs `{GATE}` and "
        f"runs in the `{ENVIRONMENT}` environment"
    )
    return 0


def decide(
    runs: list[dict],
    marker_of: Callable[[dict], dict | str],
    tag: str,
    commit: str,
    sums: str,
) -> tuple[bool, list[str]]:
    """Whether one of `runs` proves a live pass on `tag`'s draft at `commit`, and a line for
    each run that doesn't. `marker_of` returns a run's marker, or why it has none."""
    lines: list[str] = []
    for run in sorted(
        runs, key=lambda run: str(run.get("created_at", "")), reverse=True
    ):
        label = f"run {run.get('id')} ({run.get('html_url', 'no URL')})"
        wrong = [
            f"{field} is {run.get(field)!r}, not {want!r}"
            for field, want in (
                ("path", LIVE_WORKFLOW),
                ("event", "workflow_dispatch"),
                ("head_branch", tag),
                ("head_sha", commit),
                ("status", "completed"),
                ("conclusion", "success"),
            )
            if run.get(field) != want
        ]
        if wrong:
            lines.append(f"{label}: {'; '.join(wrong)}")
            continue
        marker = marker_of(run)
        if isinstance(marker, str):
            lines.append(f"{label}: {marker}")
            continue
        wrong = [
            f"its marker's {field} is {marker.get(field)!r}, not {want!r}"
            for field, want in (("tag", tag), ("commit", commit))
            if marker.get(field) != want
        ]
        if marker.get("sha256sums") != sums:
            wrong.append(
                "its marker's SHA256SUMS isn't this run's, so it tested another draft of the tag"
            )
        if wrong:
            lines.append(f"{label}: {'; '.join(wrong)}")
            continue
        lines.append(f"{label}: passed on this draft")
        return True, lines
    return False, lines


def gh(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["gh", *args], capture_output=True, text=True)


def fetch_runs(repo: str, commit: str) -> list[dict]:
    """live-conformance.yml's dispatched runs on `commit`. The filters are the API's; `decide`
    checks each field again."""
    name = Path(LIVE_WORKFLOW).name
    listed = gh(
        "api",
        "--paginate",
        f"repos/{repo}/actions/workflows/{name}/runs"
        f"?head_sha={commit}&event=workflow_dispatch&per_page=100",
        "--jq",
        ".workflow_runs[]",
    )
    if listed.returncode != 0:
        raise SystemExit(
            f"release gate: FAILED\n  listing {name}'s runs: {listed.stderr.strip()}"
        )
    return [json.loads(line) for line in listed.stdout.splitlines() if line.strip()]


def marker_fetcher(repo: str, scratch: Path) -> Callable[[dict], dict | str]:
    def marker_of(run: dict) -> dict | str:
        dest = scratch / str(run.get("id"))
        got = gh(
            "run",
            "download",
            str(run.get("id")),
            "-R",
            repo,
            "-n",
            MARKER_ARTIFACT,
            "-D",
            str(dest),
        )
        if got.returncode != 0:
            return f"no `{MARKER_ARTIFACT}` artifact ({got.stderr.strip()})"
        try:
            marker = json.loads((dest / MARKER_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            return f"its `{MARKER_ARTIFACT}` artifact has no readable {MARKER_FILE}: {error}"
        return (
            marker if isinstance(marker, dict) else f"its {MARKER_FILE} isn't an object"
        )

    return marker_of


def run_verify(repo: str, tag: str, commit: str, sums_path: Path) -> int:
    sums = sums_path.read_text(encoding="utf-8")
    runs = fetch_runs(repo, commit)
    with tempfile.TemporaryDirectory() as scratch:
        passed, lines = decide(
            runs, marker_fetcher(repo, Path(scratch)), tag, commit, sums
        )
    for line in lines:
        print(f"  {line}")
    if passed:
        print(f"release gate: ok, a live run passed on {tag}'s draft at {commit}")
        return 0
    print(
        f"release gate: FAILED\n  no live-conformance.yml run passed on {tag}'s draft at "
        f"{commit}. Dispatch one on the tag (`gh workflow run live-conformance.yml --ref {tag}`), "
        "wait for it to pass, then re-run this job."
    )
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    graph_parser = sub.add_parser(
        "graph", help="hold release.yml's job graph to the gate"
    )
    graph_parser.add_argument(
        "--root", type=Path, default=ROOT, help="the repository root"
    )
    verify_parser = sub.add_parser(
        "verify", help="find a live run that passed on the draft"
    )
    verify_parser.add_argument("--tag", required=True)
    verify_parser.add_argument("--commit", required=True)
    verify_parser.add_argument(
        "--sums", type=Path, required=True, help="this run's SHA256SUMS"
    )
    verify_parser.add_argument(
        "--repo", default=os.environ.get("GITHUB_REPOSITORY", "")
    )
    args = parser.parse_args()
    if args.command == "graph":
        return run_graph(args.root)
    if not args.repo:
        parser.error("--repo is required outside GitHub Actions")
    return run_verify(args.repo, args.tag, args.commit, args.sums)


if __name__ == "__main__":
    sys.exit(main())
