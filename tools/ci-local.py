#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml==6.0.3"]
# ///
# SPDX-License-Identifier: Apache-2.0
"""Run CI's Linux jobs here, in clones shaped like their checkouts (#315).

Each CI job runs one `mise run ci:<job>`, and `mise run ci:<job>` runs the same task in this
worktree. What a worktree run can't show is a difference of checkout: CI clones the commit
fresh, shallow unless the job asks for its history, with no untracked files, no origin/main in
a shallow clone, and no target from an earlier build. PR #313's `guards` job failed on a
registry entry that needed a merge base, because that job's checkout was shallow; it passed
every local gate. This runs a job's `mise run` steps in a clone shaped like the job's.

`./tools/ci-local.py [JOB ...]` (`mise run ci:local`, or `mise run ci:local -- security drift`):

1. Snapshots the worktree as a commit on HEAD, built through a scratch index, so uncommitted
   and untracked (not ignored) files are in it. No ref points at it, the caller's index isn't
   touched, and nothing is pushed. `--apply PATCH` applies a patch to the snapshot only.
2. For each job, clones the snapshot the way its `actions/checkout` step does: `fetch-depth: 0`
   gets the full history plus origin/main and the tags; anything else gets one commit with no
   remote. The clone's `target/` survives the next run's fresh clone, so a warm run rebuilds
   only the workspace crates. That's a full target per job, tens of GB in all.
3. Runs the job's `mise run` steps in order, as the workflow writes them, for a pull request
   against main on the ubuntu leg, one shard of one (`EXPRESSIONS`), with the workflow's, the
   job's and the step's `env` and bash invoked as the runner invokes it (`bash -e`, or `bash
   --noprofile --norc -eo pipefail` for `shell: bash`). A step whose `if` is a push's doesn't
   run. The job's other steps set up the runner (the toolchain, the caches, mise itself), which
   the caller's mise stands in for. The first failing step ends the job, and the job's
   `timeout-minutes` applies. A step that runs out of time gets what the runner sends: SIGINT,
   then SIGTERM 7.5 s later, then SIGKILL 2.5 s after that, so a step that cleans up on a
   signal (`check-guards-fire.py` removes its scratch worktrees and ends its own commands) gets
   to.
4. Keeps a failed job's clone, minus its target, in `<work-dir>/<job>/failed/`, where CI's `if:
   failure()` upload would keep its files (a fuzz crash's reproducer), since the next run's
   clone would delete it. A passing run of that job removes it.

With no JOB it runs every job it can, all at once, then `summary`: each job's last result and
what stays CI-only (the jobs `SKIP` names with their reasons, the legs on other platforms, the
steps it doesn't run, the workflows it doesn't read). It reads the workflows and their matrix
legs with check-ci-parity.py's readers, so both see the same `mise run` steps.

The snapshot sits on HEAD. CI tests a pull request's merge with main (`refs/pull/N/merge`), so
rebase onto a fresh origin/main first, or CI tests a tree this didn't.

The caller's environment is scrubbed of what CI doesn't have: git's hook pointers, an active
virtualenv, `CARGO_TARGET_DIR`, mise's own variables but where it keeps its installs, since a
clone's `mise run` is a fresh one, and the caller's mise tools and shims on PATH. mise reads the clone's config alone, trusted as mise-action
trusts the checkout: no global config and none in a parent directory, which a runner has
neither of. The work directory is `$CI_LOCAL_DIR`, else `ci-local-<hash of the repo path>` under
the system temp directory (`$TMPDIR`). A job's output goes to `<work-dir>/<job>/log.txt`; the
console gets one line per step and a failing step's last lines.
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import hashlib
import json
import math
import os
import re
import runpy
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import NamedTuple

PARITY = runpy.run_path(str(Path(__file__).with_name("check-ci-parity.py")))

# What a pull request against main gives the `${{ }}`s of the steps this runs, on the ubuntu leg
# and as one shard of one, so `ci:guards` fires the whole pull request selection in one run. An
# expression missing here refuses the job by name rather than running it with the text in.
EXPRESSIONS = {
    "github.base_ref": "main",
    "github.event.pull_request.user.login": "contributor",
    "github.event_name == 'pull_request' && format('origin/{0}', github.base_ref) || 'HEAD^'": "origin/main",
    "github.event_name == 'pull_request' && format('origin/{0}', github.base_ref) || ''": "origin/main",
    "matrix.shard": "0",
    "strategy.job-total": "1",
}
# A step's `if` on a pull request.
ON_PULL_REQUEST = {
    "github.event_name == 'pull_request'": True,
    "github.event_name != 'pull_request'": False,
}
# The jobs that don't run here, and why.
SKIP = {
    (
        "ci.yml",
        "sbom",
    ): "its scanners read vulnerability databases that change daily, so a pass here says nothing about the next CI run; `mise run ci:sbom` runs its task",
    (
        "ci.yml",
        "mutants",
    ): "it runs on pull requests only, as a matrix of shards, and each mutant is a build; `mise run mutants` runs the same wrapper over the branch against origin/main, unsharded",
    (
        "ci.yml",
        "guards-cache",
    ): "it runs on a push to main only, to fill the caches the other jobs restore",
}
# A variable a job's step sets that names something only its runner has, and why it's dropped.
UNSET = {
    (
        "ci.yml",
        "build",
        "CARGO_TARGET_AARCH64_UNKNOWN_LINUX_MUSL_LINKER",
    ): "the job's apt step installs that gcc; without it, .cargo/config.toml's rust-lld links the same static target",
}
EXPRESSION = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")

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
# mise's variables a clone's run keeps: where the caller's mise keeps its installs, and its
# GitHub token for installing a tool the caller lacks.
MISE_KEPT = {"MISE_DATA_DIR", "MISE_CACHE_DIR", "MISE_STATE_DIR", "MISE_GITHUB_TOKEN"}
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


class Step(NamedTuple):
    label: str
    run: str
    shell: str | None
    env: dict[str, str]
    # Why it doesn't run on a pull request, or None when it does.
    skipped: str | None


class Job(NamedTuple):
    workflow: str
    name: str
    full_history: bool
    timeout_minutes: float | None
    env: dict[str, str]
    steps: list[Step]
    unset: list[str]


def resolve(text: str, leg: dict[str, str], where: str) -> str:
    """`text` with each `${{ }}` answered for a pull request on this leg."""

    def value(match: re.Match) -> str:
        expr = match.group(1)
        if expr in EXPRESSIONS:
            return EXPRESSIONS[expr]
        if expr.startswith("matrix.") and expr[len("matrix.") :] in leg:
            return leg[expr[len("matrix.") :]]
        raise Failed(
            f"{where} uses `${{{{ {expr} }}}}`, which tools/ci-local.py's EXPRESSIONS doesn't answer"
        )

    return EXPRESSION.sub(value, text)


def runs_on(body: dict, leg: dict[str, str]) -> str:
    text = str(body.get("runs-on", ""))
    return PARITY["MATRIX"].sub(lambda m: leg.get(m.group(1), ""), text)


def ubuntu_leg(body: dict) -> dict[str, str] | None:
    """The first leg of a job's matrix that runs on ubuntu, or None when none does. A job whose
    legs all run there (the `guards` shards) runs here once, as `EXPRESSIONS` answers it."""
    for leg in PARITY["legs"](body):
        if runs_on(body, leg).startswith("ubuntu"):
            return leg
    return None


def plan(root: Path) -> tuple[list[Job], list[str]]:
    """Every job this runs, in workflow order, and what stays CI-only."""
    jobs: list[Job] = []
    ci_only: list[str] = []
    for wf_path in PARITY["WORKFLOWS"]:
        wf = Path(wf_path).name
        try:
            data = PARITY["load_yaml"](root / wf_path, wf)
        except PARITY["Unreadable"] as error:
            raise Failed(str(error)) from None
        wf_env = {str(k): str(v) for k, v in (data.get("env") or {}).items()}
        for name, body in (data.get("jobs") or {}).items():
            if not isinstance(body, dict):
                continue
            where = f"{wf} job `{name}`"
            if (wf, name) in SKIP:
                ci_only.append(f"{where}: {SKIP[(wf, name)]}")
                continue
            leg = ubuntu_leg(body)
            if leg is None:
                ci_only.append(f"{where}: it runs on no ubuntu leg")
                continue
            for other in PARITY["legs"](body):
                if not runs_on(body, other).startswith("ubuntu"):
                    ci_only.append(
                        f"{where} on {runs_on(body, other)}: this runs its ubuntu leg"
                    )
            steps: list[Step] = []
            runner_steps: list[str] = []
            for step in body.get("steps") or []:
                if not isinstance(step, dict) or "run" not in step:
                    continue
                label = PARITY["step_label"](step)
                at = f"{where} step `{label}`"
                if not PARITY["MISE_RUN"].search(str(step["run"])):
                    runner_steps.append(f"`{label}`")
                    continue
                skipped = None
                if "if" in step:
                    condition = EXPRESSION.sub(
                        lambda m: m.group(1), str(step["if"])
                    ).strip()
                    if condition not in ON_PULL_REQUEST:
                        raise Failed(
                            f"{at} runs `if: {step['if']}`, which tools/ci-local.py doesn't "
                            "answer for a pull request"
                        )
                    if not ON_PULL_REQUEST[condition]:
                        skipped = "a push's step, not a pull request's"
                run = resolve(str(step["run"]), leg, at)
                steps.append(
                    Step(
                        # An unnamed step by its command as this leg runs it.
                        label=str(step.get("name") or run.strip().splitlines()[0]),
                        run=run,
                        shell=step.get("shell"),
                        env={
                            str(k): resolve(str(v), leg, f"{at} env {k}")
                            for k, v in (step.get("env") or {}).items()
                        },
                        skipped=skipped,
                    )
                )
            if not steps:
                ci_only.append(f"{where}: it runs no task")
                continue
            if runner_steps:
                noun, verb = (
                    ("step", "runs") if len(runner_steps) == 1 else ("steps", "run")
                )
                ci_only.append(
                    f"{where}: its {noun} {', '.join(runner_steps)} {verb} no task, and only "
                    "on the runner"
                )
            checkout = next(
                (
                    s
                    for s in body.get("steps") or []
                    if isinstance(s, dict)
                    and str(s.get("uses", "")).split("@")[0] == "actions/checkout"
                ),
                None,
            )
            if checkout is None:
                raise Failed(f"{where} has no actions/checkout step")
            depth = str((checkout.get("with") or {}).get("fetch-depth", "1"))
            timeout = body.get("timeout-minutes")
            # A budget this can't read as a positive number would run the job with no deadline,
            # where CI stops it: an expression, or `inf`, `nan` or 0.
            if timeout is not None and not (
                isinstance(timeout, (int, float))
                and not isinstance(timeout, bool)
                and math.isfinite(timeout)
                and timeout > 0
            ):
                raise Failed(
                    f"{where} sets timeout-minutes to {timeout!r}, which tools/ci-local.py "
                    "can't hold a job to"
                )
            env = {k: resolve(v, leg, f"{wf} env {k}") for k, v in wf_env.items()}
            env |= {
                str(k): resolve(str(v), leg, f"{where} env {k}")
                for k, v in (body.get("env") or {}).items()
            }
            unset = []
            for (u_wf, u_job, key), why in UNSET.items():
                if (u_wf, u_job) == (wf, name):
                    unset.append(key)
                    ci_only.append(f"{where}: `{key}` isn't set here: {why}")
            jobs.append(
                Job(
                    workflow=wf,
                    name=name,
                    full_history=depth == "0",
                    timeout_minutes=float(timeout)
                    if isinstance(timeout, (int, float))
                    else None,
                    env=env,
                    steps=steps,
                    unset=unset,
                )
            )
    return jobs, ci_only


def scrubbed_env() -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in GIT_ENV_LEAKS
        and k not in NOT_ON_CI
        and not (k.startswith(("MISE_", "__MISE_")) and k not in MISE_KEPT)
    }
    drop = set()
    if os.environ.get("VIRTUAL_ENV"):
        drop.add(str(Path(os.environ["VIRTUAL_ENV"]) / "bin"))
    if sys.prefix != sys.base_prefix:  # `uv run --script`'s own environment
        drop.add(str(Path(sys.prefix) / "bin"))
    # The caller's mise tools and shims: a clone's `mise run` puts its own tools on PATH, and a
    # runner has no others. A global tool's shim fails outright with no global config, as when
    # actionlint finds a shellcheck shim and runs it.
    data = os.environ.get("MISE_DATA_DIR") or str(
        Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "mise"
    )
    env["PATH"] = os.pathsep.join(
        p
        for p in env.get("PATH", "").split(os.pathsep)
        if p not in drop
        and p != os.path.join(data, "shims")
        and not p.startswith(os.path.join(data, "installs") + os.sep)
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
    work.mkdir(parents=True, exist_ok=True)
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


def stream(proc: subprocess.Popen, log, tail: collections.deque, verbose: bool) -> None:
    for line in proc.stdout:
        log.write(line)
        tail.append(line)
        if verbose:
            sys.stdout.write(line)
            sys.stdout.flush()


RUNNING: list[subprocess.Popen] = []
LOCK = threading.Lock()


def say(line: str) -> None:
    with LOCK:
        print(line, flush=True)


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
    for proc in list(RUNNING):
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    sys.exit(128 + signum)


def run_steps(job: Job, src: Path, work: Path, env: dict, log, verbose: bool) -> list:
    """Run a job's steps in `src`. One row per step; the first failure ends the job."""
    rows = []
    deadline = (
        time.monotonic() + job.timeout_minutes * 60 if job.timeout_minutes else None
    )
    runner = work / "runner"
    shutil.rmtree(runner, ignore_errors=True)
    (runner / "temp").mkdir(parents=True)
    env = env | job.env
    for key in job.unset:
        env.pop(key, None)
    env["RUNNER_TEMP"] = str(runner / "temp")
    # The clone's config alone, trusted as mise-action trusts the checkout: a runner has no
    # global mise config and no config in a parent directory, and a home directory's
    # `.config/mise/config.toml` is both.
    env["MISE_TRUSTED_CONFIG_PATHS"] = str(src)
    env["MISE_CEILING_PATHS"] = str(src.parent)
    env["MISE_GLOBAL_CONFIG_FILE"] = str(work / "no-global-config.toml")
    env["MISE_CONFIG_DIR"] = str(work / "no-config-dir")
    env["MISE_YES"] = "1"
    for index, step in enumerate(job.steps):
        head = f"ci:local {job.name} | {step.label}"
        if step.skipped:
            rows.append({"step": step.label, "status": "skipped", "note": step.skipped})
            say(f"{head} ... skipped ({step.skipped})")
            log.write(f"\n::: {step.label}: skipped ({step.skipped})\n")
            continue
        step_env = env | step.env
        for key in job.unset:
            step_env.pop(key, None)
        for name in ("GITHUB_ENV", "GITHUB_PATH", "GITHUB_OUTPUT"):
            path = runner / f"{name.lower()}-{index}"
            path.write_text("")
            step_env[name] = str(path)
        step_env["GITHUB_STEP_SUMMARY"] = str(work / "summary.md")
        script = runner / f"step-{index}.sh"
        script.write_text(step.run + "\n", encoding="utf-8")
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
            cwd=src,
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
            with LOCK:
                print(
                    f"{head} ... FAILED ({why}, {seconds} s). Its last lines:",
                    flush=True,
                )
                sys.stdout.write("".join(f"    {line}" for line in tail))
            break
        rows.append({"step": step.label, "status": "passed", "seconds": seconds})
        say(f"{head} ... ok ({seconds} s)")
    return rows


def run_job(root: Path, work_dir: Path, job: Job, commit: str, verbose: bool) -> dict:
    work = work_dir / job.name
    work.mkdir(parents=True, exist_ok=True)
    (work / "result.json").unlink(missing_ok=True)
    (work / "summary.md").unlink(missing_ok=True)
    kept = work / "failed"
    shutil.rmtree(kept, ignore_errors=True)
    started = time.time()
    depth = "full history and origin/main" if job.full_history else "depth 1, no remote"
    with (work / "log.txt").open("w", encoding="utf-8", errors="replace") as log:
        log.write(f"{job.workflow} {job.name} on snapshot {commit} of {root}\n")
        try:
            clone(root, commit, work / "src", job.full_history)
        except Failed as error:
            rows = [{"step": "checkout", "status": "failed", "note": str(error)}]
            say(f"ci:local {job.name}: FAILED: {error}")
        else:
            say(f"ci:local {job.name}: clone of {commit[:12]} ({depth})")
            rows = run_steps(job, work / "src", work, scrubbed_env(), log, verbose)
    failed = any(r["status"] == "failed" for r in rows)
    record = {
        "workflow": job.workflow,
        "job": job.name,
        "commit": commit,
        "root": str(root),
        "seconds": round(time.time() - started),
        "status": "failed" if failed else "passed",
        "steps": rows,
    }
    if failed and (work / "src").is_dir():
        keep_failed(work / "src", kept)
        record["kept"] = str(kept)
        say(f"ci:local {job.name}: its tree is kept in {kept}")
    (work / "result.json").write_text(json.dumps(record, indent=2) + "\n")
    verdict = "FAILED" if failed else "passed"
    say(
        f"ci:local {job.name}: {verdict} in {record['seconds']} s on snapshot "
        f"{commit[:12]}. Log: {work / 'log.txt'}"
    )
    return record


def keep_failed(src: Path, kept: Path) -> None:
    """Move a failed job's clone to `kept`, and its target back, so the next clone is warm."""
    src.rename(kept)
    src.mkdir()
    target = kept / "target"
    if target.is_dir() and not target.is_symlink():
        target.rename(src / "target")


def summary(root: Path, work_dir: Path) -> int:
    jobs, ci_only = plan(root)
    commits = set()
    bad = 0
    print("ci:local results:")
    for job in jobs:
        path = work_dir / job.name / "result.json"
        if not path.exists():
            print(f"  {job.name}: no result (not run, or stopped before it finished)")
            bad += 1
            continue
        record = json.loads(path.read_text())
        commits.add(record["commit"])
        print(
            f"  {job.name}: {record['status']} in {record['seconds']} s on "
            f"{record['commit'][:12]} ({work_dir / job.name / 'log.txt'})"
        )
        bad += record["status"] != "passed"
    if len(commits) > 1:
        print(
            "  The jobs ran on different snapshots; rerun `mise run ci:local` for one."
        )
        bad += 1
    print("CI-only, not run here:")
    for line in ci_only:
        print(f"  - {line}")
    read = {Path(w).name for w in PARITY["WORKFLOWS"]}
    rest = sorted(
        p.name for p in (root / ".github/workflows").glob("*.yml") if p.name not in read
    )
    if rest:
        print(f"  - the workflows this doesn't read: {', '.join(rest)}")
    return 1 if bad else 0


def default_work_dir(root: Path) -> Path:
    if os.environ.get("CI_LOCAL_DIR"):
        return Path(os.environ["CI_LOCAL_DIR"])
    tag = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:10]
    return Path(tempfile.gettempdir()) / f"ci-local-{tag}"


def run(
    root: Path, work_dir: Path, names: list[str], patches: list[Path], verbose: bool
) -> int:
    jobs, _ = plan(root)
    if names:
        known = {job.name: job for job in jobs}
        missing = [n for n in names if n not in known]
        if missing:
            raise Failed(
                f"no job here runs as {', '.join(f'`{n}`' for n in missing)}; the jobs are "
                f"{', '.join(sorted(known))}"
            )
        jobs = [known[n] for n in names]
    commit = snapshot(root, work_dir, patches)
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs) or 1) as pool:
        records = list(
            pool.map(lambda job: run_job(root, work_dir, job, commit, verbose), jobs)
        )
    if not names:
        return summary(root, work_dir)
    return 1 if any(r["status"] != "passed" for r in records) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "jobs",
        nargs="*",
        metavar="JOB",
        help="the jobs to run (rust, security, ...), or `summary`; every job without one",
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
        if args.jobs == ["summary"]:
            return summary(root, work_dir)
        return run(
            root, work_dir, args.jobs, [Path(p) for p in args.apply], args.verbose
        )
    except Failed as error:
        print(f"ci:local: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, stop_children)
    signal.signal(signal.SIGINT, stop_children)
    sys.exit(main())
