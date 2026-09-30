#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml==6.0.3"]
# ///
# SPDX-License-Identifier: Apache-2.0
"""Run CI's Linux jobs here, the way the runner does (#315).

`mise run check` is fast, and CI still caught failures no local gate could see. PR #314's first
run failed seven `AdapterLintTests` on colored clippy output, because ci.yml sets
`CARGO_TERM_COLOR=always`; PR #313's `guards` job failed on a registry entry that needed a
merge base, because that job's checkout was shallow with no origin/main. Both passed every local
gate. This script runs a job's own `run:` steps in a checkout shaped like the job's, so a
difference of that kind shows up before a push.

`./tools/ci-local.py <task>` (`mise run ci:<task>`; `mise run ci:local` runs every task in
parallel, then `summary`):

1. Snapshots the worktree as a commit on HEAD, built through a scratch index, so uncommitted
   and untracked (not ignored) files are in it. No ref points at it, the caller's index isn't
   touched, and nothing is pushed. `--apply PATCH` applies a patch to the snapshot only.
2. For each workflow job the task runs (`ci/local.toml`), clones the snapshot the way the
   job's `actions/checkout` step does: `fetch-depth: 0` gets the full history plus
   origin/main and the tags; anything else gets one commit with no remote.
3. Runs the job's steps in order with the workflow's, job's and step's `env`, each `${{ }}`
   replaced by its value in `ci/local.toml`, and bash invoked as the runner invokes it (`bash -e`,
   or `bash --noprofile --norc -eo pipefail` for `shell: bash`). `RUNNER_TEMP`, `GITHUB_PATH`,
   `GITHUB_ENV` and `GITHUB_STEP_SUMMARY` work as they do there. The first failing step ends
   the job, and the job's `timeout-minutes` applies. A step that runs out of time gets what
   the runner sends: SIGINT, then SIGTERM 7.5 s later, then SIGKILL 2.5 s after that, so a
   step that cleans up on a signal (`check-guards-fire.py` removes its scratch worktrees and
   ends its own commands) gets to.
4. Keeps a failed job's clone, minus its target, in `<work-dir>/<task>/failed-<job>/`, where
   CI's `if: failure()` upload would keep its files (a fuzz crash's reproducer), since the next
   job's clone would delete it. A passing run of that job removes it.

The snapshot sits on HEAD. CI tests a pull request's merge with main (`refs/pull/N/merge`), so
rebase onto a fresh origin/main first, or CI tests a tree this didn't: a step or registry
entry main gained since the branch forked shows up there only.

The caller's environment is scrubbed of what CI doesn't have: git's hook pointers, an active
virtualenv, `CARGO_TARGET_DIR`, and every mise.toml `[env]` key the workflow doesn't set. Each
task has its own work directory, `<work-dir>/<task>`, so tasks run in parallel without waiting
on one build lock: the clone is `src/` and its `target/`, which is `CARGO_TARGET_DIR`, survives
the next run's fresh clone, so a warm run rebuilds only the workspace crates. That's a full
target per task, tens of GB in all. The work directory is `$CI_LOCAL_DIR`, else
`ci-local-<hash of the repo path>` under the system temp directory (`$TMPDIR`). A job's output
goes to `<work-dir>/<task>/log.txt`; the console gets one line per step and a failing step's
last lines.

The plan comes from `check-ci-parity.py`'s `plan()`, so this refuses to run while
`mise run ci:parity` would fail on step coverage. `summary` prints every task's last result and
what stays CI-only: the jobs and steps `ci/local.toml` skips, with their reasons, the actions
it doesn't run and why, the other matrix legs, and the workflows it doesn't name.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import runpy
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

PARITY = runpy.run_path(str(Path(__file__).with_name("check-ci-parity.py")))

# The pointers a git hook exports; inherited, they'd aim this script's git at the caller's index.
GIT_ENV_LEAKS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_CEILING_DIRECTORIES",
    "GIT_PREFIX",
)
# Set on a developer's machine and never on a runner.
NOT_ON_CI = ("VIRTUAL_ENV", "CARGO_TARGET_DIR", "CONDA_PREFIX")
# The snapshot's author, so the same tree on the same HEAD is the same commit.
IDENTITY = {
    "GIT_AUTHOR_NAME": "ci-local",
    "GIT_AUTHOR_EMAIL": "ci-local@localhost",
    "GIT_COMMITTER_NAME": "ci-local",
    "GIT_COMMITTER_EMAIL": "ci-local@localhost",
}
# Lets `git fetch` ask for the snapshot by id, since no ref names it.
UPLOAD_PACK = "git -c uploadpack.allowAnySHA1InWant=true upload-pack"
TAIL = 60
# What the runner does to a step past its job's timeout (actions/runner, ProcessInvoker.cs):
# SIGINT, 7.5 s, SIGTERM, 2.5 s, then a kill.
ESCALATION = ((signal.SIGINT, 7.5), (signal.SIGTERM, 2.5), (signal.SIGKILL, None))
# How long to wait for a step's output once it has exited or been killed. A process the step
# started in a session of its own can hold the pipe open past the step; its output after this
# is dropped rather than holding the job up.
DRAIN = 5


class Failed(Exception):
    """Setup failed before any step ran."""


def scrubbed_env() -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in GIT_ENV_LEAKS and k not in NOT_ON_CI
    }
    drop = set()
    if os.environ.get("VIRTUAL_ENV"):
        drop.add(str(Path(os.environ["VIRTUAL_ENV"]) / "bin"))
    if sys.prefix != sys.base_prefix:  # `uv run --script`'s own environment
        drop.add(str(Path(sys.prefix) / "bin"))
    env["PATH"] = os.pathsep.join(
        p for p in env.get("PATH", "").split(os.pathsep) if p not in drop
    )
    return env


def git(args: list[str], cwd: Path, env: dict[str, str] | None = None) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env if env is not None else scrubbed_env(),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise Failed(f"git {' '.join(args)} (in {cwd}): {proc.stderr.strip()}")
    return proc.stdout.strip()


def snapshot(root: Path, work: Path, patches: list[Path]) -> str:
    """A commit of the worktree on HEAD, through a scratch index. Its id; no ref names it."""
    env = scrubbed_env()
    index = work / "index"
    index.unlink(missing_ok=True)
    scratch = env | {"GIT_INDEX_FILE": str(index)}
    git(["read-tree", "HEAD"], root, scratch)
    git(["add", "-A"], root, scratch)
    for patch in patches:
        git(["apply", "--cached", str(patch.resolve())], root, scratch)
    tree = git(["write-tree"], root, scratch)
    index.unlink(missing_ok=True)
    date = git(["show", "-s", "--format=%ct +0000", "HEAD"], root, env)
    stamped = env | IDENTITY | {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    message = "ci:local snapshot" + "".join(f"\n\napplied {p.name}" for p in patches)
    return git(["commit-tree", tree, "-p", "HEAD", "-m", message], root, stamped)


def clone(root: Path, commit: str, dest: Path, full_history: bool) -> None:
    """`dest` holds `commit` checked out as the job's checkout would; `dest/target` is kept."""
    dest.mkdir(parents=True, exist_ok=True)
    for child in dest.iterdir():
        if child.name == "target" and child.is_dir() and not child.is_symlink():
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    source = Path(
        git(["rev-parse", "--path-format=absolute", "--git-common-dir"], root)
    )
    git(["-c", "init.defaultBranch=main", "init", "-q", "."], dest)
    fetch = ["fetch", "-q", "--no-tags", "--upload-pack", UPLOAD_PACK, source.as_uri()]
    if full_history:
        try:
            git(["rev-parse", "--verify", "-q", "refs/remotes/origin/main"], root)
        except Failed:
            raise Failed(
                "this job's checkout has the full history and origin/main, and this repo "
                "has no origin/main (git fetch origin)"
            ) from None
        fetch += [commit, "+refs/remotes/origin/main:refs/remotes/origin/main"]
        fetch += ["+refs/tags/*:refs/tags/*"]
    else:
        fetch += ["--depth", "1", commit]
    git(fetch, dest)
    git(["checkout", "-q", "--detach", commit], dest)


def read_env_file(path: Path) -> dict[str, str]:
    """`GITHUB_ENV`'s two forms: `KEY=VALUE` and `KEY<<DELIM` ... `DELIM`."""
    out: dict[str, str] = {}
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if "<<" in line and ("=" not in line or line.index("<<") < line.index("=")):
            key, delim = line.split("<<", 1)
            body = []
            i += 1
            while i < len(lines) and lines[i] != delim:
                body.append(lines[i])
                i += 1
            out[key] = "\n".join(body)
        elif "=" in line:
            key, value = line.split("=", 1)
            out[key] = value
        i += 1
    return out


def stream(proc: subprocess.Popen, log, tail: collections.deque, verbose: bool) -> None:
    for line in proc.stdout:
        log.write(line)
        tail.append(line)
        if verbose:
            sys.stdout.write(line)
            sys.stdout.flush()


RUNNING: list[subprocess.Popen] = []


def end_step(proc: subprocess.Popen) -> None:
    """Stop a step that ran out of time the way the runner does, one signal at a time."""
    for signum, wait in ESCALATION:
        try:
            os.killpg(proc.pid, signum)
        except ProcessLookupError:
            break
        try:
            proc.wait(timeout=wait)
            break
        except subprocess.TimeoutExpired:
            continue
    proc.wait()


def stop_children(signum, _frame) -> None:
    for proc in RUNNING:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    sys.exit(128 + signum)


def run_job(
    job, src: Path, work: Path, tools: Path, env: dict, log, verbose: bool
) -> dict:
    """Run one job's steps in `src`. Its result: status and one row per step."""
    rows = []
    status = "passed"
    deadline = (
        time.monotonic() + job.timeout_minutes * 60 if job.timeout_minutes else None
    )
    runner = work / "runner"
    shutil.rmtree(runner, ignore_errors=True)
    (runner / "temp").mkdir(parents=True)
    env = env | dict(job.env)
    env["CARGO_TARGET_DIR"] = str(src / "target")
    env["RUNNER_TEMP"] = str(runner / "temp")
    env["PATH"] = os.pathsep.join([str(tools / "bin"), env.get("PATH", "")])
    summary = work / "summary.md"
    for index, step in enumerate(job.steps):
        head = f"ci:{job.task} {job.workflow} {job.name} | {step.label}"
        if step.run is None or step.condition == "false":
            why = step.note if step.run is None else "its `if` is false on this leg"
            rows.append({"step": step.label, "status": "skipped", "note": why})
            print(f"{head} ... skipped ({why})", flush=True)
            log.write(f"\n::: {step.label}: skipped ({why})\n")
            continue
        files = {
            name: runner / f"{name.lower()}-{index}"
            for name in ("GITHUB_PATH", "GITHUB_ENV", "GITHUB_STEP_SUMMARY")
        }
        for path in files.values():
            path.write_text("")
        script = runner / f"step-{index}.sh"
        script.write_text(step.run + "\n", encoding="utf-8")
        step_env = env | {
            k: v.replace("{tools}", str(tools)).replace("{work}", str(work))
            for k, v in step.env.items()
        }
        step_env |= {name: str(path) for name, path in files.items()}
        for key in step.unset:
            step_env.pop(key, None)
        shell = (
            ["bash", "--noprofile", "--norc", "-eo", "pipefail"]
            if step.shell == "bash"
            else ["bash", "-e"]
        )
        log.write(f"\n::: {step.label}\n")
        log.flush()
        tail: collections.deque = collections.deque(maxlen=TAIL)
        start = time.monotonic()
        proc = subprocess.Popen(
            [*shell, str(script)],
            cwd=src / step.cwd if step.cwd else src,
            env=step_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            start_new_session=True,
        )
        RUNNING.append(proc)
        reader = threading.Thread(
            target=stream, args=(proc, log, tail, verbose), daemon=True
        )
        reader.start()
        timed_out = False
        try:
            proc.wait(
                timeout=None
                if deadline is None
                else max(deadline - time.monotonic(), 0)
            )
        except subprocess.TimeoutExpired:
            timed_out = True
            end_step(proc)
        reader.join(timeout=DRAIN)
        if reader.is_alive():
            log.write(
                f"\n::: {step.label}: a process it started still holds its output open; "
                "the rest of its output isn't in this log\n"
            )
        RUNNING.remove(proc)
        seconds = round(time.monotonic() - start)
        for line in files["GITHUB_PATH"].read_text().splitlines():
            if line.strip():
                env["PATH"] = os.pathsep.join([line.strip(), env["PATH"]])
        env |= read_env_file(files["GITHUB_ENV"])
        with summary.open("a", encoding="utf-8") as out:
            out.write(files["GITHUB_STEP_SUMMARY"].read_text())
        if timed_out or proc.returncode != 0:
            why = (
                f"the job's timeout-minutes ({job.timeout_minutes:g}) ran out"
                if timed_out
                else f"exit {proc.returncode}"
            )
            rows.append(
                {
                    "step": step.label,
                    "status": "failed",
                    "seconds": seconds,
                    "note": why,
                }
            )
            print(
                f"{head} ... FAILED ({why}, {seconds} s). Its last lines:", flush=True
            )
            sys.stdout.write("".join(f"    {line}" for line in tail))
            status = "failed"
            break
        rows.append(
            {
                "step": step.label,
                "status": "passed",
                "seconds": seconds,
                "note": step.note,
            }
        )
        print(f"{head} ... ok ({seconds} s)", flush=True)
    return {"workflow": job.workflow, "job": job.name, "status": status, "steps": rows}


def load_plan(root: Path) -> list:
    problems: list[str] = []
    try:
        local = PARITY["load_toml"](root / PARITY["LOCAL"], PARITY["LOCAL"])
    except PARITY["Unreadable"] as error:
        raise Failed(str(error)) from None
    jobs = PARITY["plan"](root, local, problems)
    if problems:
        raise Failed(
            "ci/local.toml doesn't cover the workflows (`mise run ci:parity` says the same):\n"
            + "\n".join(f"  - {p}" for p in problems)
        )
    return jobs


def mise_only_keys(root: Path, jobs: list) -> set[str]:
    """mise.toml `[env]` keys no planned job's workflow sets: a CI runner doesn't have them.

    Read through the loader `ci:parity` reads the tasks with, so a config it refuses stops the
    run rather than leaving those keys in the job's environment.
    """
    try:
        mise = PARITY["load_mise"](root, root / PARITY["MISE"])
    except PARITY["Unreadable"] as error:
        raise Failed(str(error)) from None
    ci_keys = {key for job in jobs for key in job.env}
    return {str(k) for k in (mise.data.get("env") or {})} - ci_keys


def run_task(
    root: Path, work_dir: Path, task: str, patches: list[Path], verbose: bool
) -> int:
    jobs = load_plan(root)
    selected = [job for job in jobs if job.task == task]
    if not selected:
        names = sorted({job.task for job in jobs})
        raise Failed(f"no job runs as `{task}`; the tasks are {', '.join(names)}")
    work = work_dir / task
    work.mkdir(parents=True, exist_ok=True)
    (work / "result.json").unlink(missing_ok=True)
    (work / "summary.md").unlink(missing_ok=True)
    tools = work_dir / "tools"
    commit = snapshot(root, work, patches)
    env = scrubbed_env()
    for key in mise_only_keys(root, jobs):
        env.pop(key, None)
    started = time.time()
    results = []
    with (work / "log.txt").open("w", encoding="utf-8", errors="replace") as log:
        log.write(f"ci:{task} on snapshot {commit} of {root}\n")
        for job in selected:
            kept = work / f"failed-{job.name}"
            shutil.rmtree(kept, ignore_errors=True)
            clone(root, commit, work / "src", job.full_history)
            depth = (
                "full history and origin/main"
                if job.full_history
                else "depth 1, no remote"
            )
            print(
                f"ci:{task} {job.workflow} {job.name}: clone of {commit[:12]} ({depth})",
                flush=True,
            )
            results.append(run_job(job, work / "src", work, tools, env, log, verbose))
            log.flush()
            if results[-1]["status"] != "passed":
                keep_failed(work / "src", kept)
                results[-1]["kept"] = str(kept)
                print(f"ci:{task} {job.name}: its tree is kept in {kept}", flush=True)
    failed = [r for r in results if r["status"] != "passed"]
    record = {
        "task": task,
        "commit": commit,
        "root": str(root),
        "patches": [str(p) for p in patches],
        "started": started,
        "seconds": round(time.time() - started),
        "status": "failed" if failed else "passed",
        "jobs": results,
    }
    (work / "result.json").write_text(json.dumps(record, indent=2) + "\n")
    verdict = "FAILED" if failed else "passed"
    print(
        f"ci:{task}: {verdict} in {record['seconds']} s on snapshot {commit[:12]}. "
        f"Log: {work / 'log.txt'}",
        flush=True,
    )
    return 1 if failed else 0


def keep_failed(src: Path, kept: Path) -> None:
    """Move a failed job's clone to `kept`, and its target back, so the next clone is warm."""
    src.rename(kept)
    src.mkdir()
    target = kept / "target"
    if target.is_dir() and not target.is_symlink():
        target.rename(src / "target")


def ci_only(root: Path, jobs: list) -> list[str]:
    """What `ci:local` doesn't run, each with its reason."""
    local = PARITY["load_toml"](root / PARITY["LOCAL"], PARITY["LOCAL"])
    lines = []
    named = local.get("workflows") or []
    for wf, specs in (local.get("job") or {}).items():
        for job, spec in specs.items():
            if "skip" in spec:
                lines.append(f"{wf} job `{job}`: {spec['skip']}")
    for job in jobs:
        for step in job.steps:
            if step.run is None:
                lines.append(
                    f"{job.workflow} job `{job.name}` step `{step.label}`: {step.note}"
                )
    leg = (local.get("expressions") or {}).get("matrix.os")
    for wf in named:
        data = PARITY["load_yaml"](root / PARITY["WORKFLOWS"] / wf, wf)
        for name, body in (data.get("jobs") or {}).items():
            oses = ((body.get("strategy") or {}).get("matrix") or {}).get("os") or []
            others = [os_ for os_ in oses if os_ != leg]
            if others:
                lines.append(
                    f"{wf} job `{name}` on {', '.join(others)}: ci:local runs the {leg} leg"
                )
    rest = sorted(
        p.name
        for p in (root / PARITY["WORKFLOWS"]).glob("*.yml")
        if p.name not in named
    )
    if rest:
        lines.append(f"the workflows ci/local.toml doesn't name: {', '.join(rest)}")
    for action, why in sorted((local.get("actions") or {}).items()):
        lines.append(f"`{action}` steps: {why}")
    return lines


def summary(root: Path, work_dir: Path) -> int:
    jobs = load_plan(root)
    tasks = sorted({job.task for job in jobs})
    commits = set()
    bad = 0
    print("ci:local results:")
    for task in tasks:
        path = work_dir / task / "result.json"
        if not path.exists():
            print(f"  ci:{task}: no result (not run, or stopped before it finished)")
            bad += 1
            continue
        record = json.loads(path.read_text())
        commits.add(record["commit"])
        print(
            f"  ci:{task}: {record['status']} in {record['seconds']} s on "
            f"{record['commit'][:12]} ({work_dir / task / 'log.txt'})"
        )
        bad += record["status"] != "passed"
    if len(commits) > 1:
        print(
            "  The tasks ran on different snapshots; rerun `mise run ci:local` for one."
        )
        bad += 1
    print("CI-only, not run here:")
    for line in ci_only(root, jobs):
        print(f"  - {line}")
    return 1 if bad else 0


def default_work_dir(root: Path) -> Path:
    if os.environ.get("CI_LOCAL_DIR"):
        return Path(os.environ["CI_LOCAL_DIR"])
    tag = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:10]
    return Path(tempfile.gettempdir()) / f"ci-local-{tag}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "task", help="a task from ci/local.toml (rust, security, ...), or `summary`"
    )
    parser.add_argument("--root", help="the repository (default: this script's)")
    parser.add_argument("--work-dir", help="where clones and targets live")
    parser.add_argument(
        "--apply",
        action="append",
        default=[],
        metavar="PATCH",
        help="apply a patch to the snapshot, not the worktree (repeatable)",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="stream every step's output"
    )
    args = parser.parse_args(argv)
    root = (
        Path(args.root) if args.root else Path(__file__).resolve().parents[1]
    ).resolve()
    work_dir = Path(args.work_dir) if args.work_dir else default_work_dir(root)
    try:
        if args.task == "summary":
            return summary(root, work_dir)
        return run_task(
            root, work_dir, args.task, [Path(p) for p in args.apply], args.verbose
        )
    except Failed as error:
        print(f"ci:{args.task}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, stop_children)
    signal.signal(signal.SIGINT, stop_children)
    sys.exit(main())
