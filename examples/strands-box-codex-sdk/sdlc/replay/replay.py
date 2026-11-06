#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Replay hand-built Box event traces against box/policy.dw with the Dogwood CLI.

Usage: replay.py [--faults]   (DOGWOOD names the dogwood CLI, 1.1.0 or later)

Each case is a list of events; an event that asks for a decision carries the verdict it must
get. A trace models check.py's exit status: 3 when git can't run, 2 for a dirty tree, 1 for a
failing test, 0 for a pass. Exits 1 when any case gets a different verdict. With --faults, it also breaks the policy
once per entry in FAULTS and exits 1 unless every broken copy makes at least one case differ.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
POLICY = HERE.parent / "box/policy.dw"
SCHEMA = HERE / "schema"
CWD = "/home/agent/project"
PY = "/usr/bin/python3.12"
GIT = "/usr/bin/git"
MODEL = "bedrock-runtime.us-east-1.amazonaws.com"
SCOPE = 'callerPrincipal: Box::Agent::"agent", callerResource: Box::Resource::"box"'
SC = 'scope(principal: Box::Agent::"agent", resource: Box::Resource::"box")'


def q(s):
    return json.dumps(s)


def spawn_input(program, path, args, cwd):
    a = "".join(f", arg{i + 1}: {q(x)}" for i, x in enumerate(args[:2]))
    return (
        f"{{ command: {q(' '.join((program,) + tuple(args)))}, program: {q(program)}, "
        f"program_path: {q(path)}{a}, arg_count: {len(args)}, cwd: {q(cwd)} }}"
    )


def decide(t, action, inp, verdict):
    return (
        f"@{t} {SC} request_context(input: {inp}) Box::Action::{q(action)}::request("
        f'input: {inp}, {SCOPE}, requestId: "q{t}")',
        verdict,
    )


def spawn(t, verdict, program, path, args, status=None, cwd=CWD):
    """A shell:spawn decision; when permitted and given a status, also its response event."""
    inp = spawn_input(program, path, args, cwd)
    line, v = decide(t, "shell:spawn", inp, verdict)
    if verdict == "ALLOW" and status is not None:
        line += (
            f'\n@{t + 1} Box::Action::"shell:spawn"::response(input: {inp}, '
            f'output: {{ status: {status} }}, {SCOPE}, requestId: "q{t}")'
        )
    return line, v


def spawned(t, status, program, path, args, cwd=CWD):
    """A shell:spawn response with no request: history only, as if a later permit had let it run."""
    inp = spawn_input(program, path, args, cwd)
    return (
        f'@{t} Box::Action::"shell:spawn"::response(input: {inp}, output: {{ status: {status} }}, '
        f'{SCOPE}, requestId: "h{t}")',
        None,
    )


def check(t, status, verdict="ALLOW", args=("check.py",), cwd=CWD):
    return spawn(t, verdict, "python3.12", PY, args, status, cwd)


def git(t, verdict, *args, status=0):
    return spawn(t, verdict, "git", GIT, args, status)


def monty(t, status, args):
    inp = (
        f'{{ command: {q(" ".join(("python3",) + tuple(args)))}, program: "python3", '
        f"arg1: {q(args[0])}, arg_count: {len(args)}, cwd: {q(CWD)} }}"
    )
    return (
        f'@{t} Box::Action::"shell:exec"::response(input: {inp}, output: {{ status: {status} }}, '
        f'{SCOPE}, requestId: "e{t}")',
        None,
    )


OPS = {
    "fs:write": 'FsWriteOperation::"write_content"',
    "fs:move": 'FsMoveOperation::"rename"',
    "fs:delete": 'FsDeleteOperation::"remove_file"',
    "fs:read": 'FsReadOperation::"read_content"',
}


def fs(t, action, path, verdict):
    inp = f"{{ path: {q(path)}, operation: Box::{OPS[action]} }}"
    line, v = decide(t, action, inp, verdict)
    if verdict == "ALLOW" and action == "fs:write":
        line += (
            f"\n@{t + 1} Box::Action::{q(action)}::response(input: {inp}, output: "
            f'{{ result: Box::FsResponseResult::"completed" }}, {SCOPE}, requestId: "q{t}")'
        )
    return line, v


def write(t, path, verdict):
    return fs(t, "fs:write", "~/project/" + path, verdict)


def model(t, verdict):
    inp = (
        f'{{ host: {q(MODEL)}, port: 443, method: "POST", path: "/openai/v1/responses", '
        f"body_bytes: 100, intercepted: true }}"
    )
    return decide(t, "http:request", inp, verdict)


def connect(t, host, verdict, ip=None):
    extra = f", ip4: {q(ip)}" if ip else ""
    return decide(t, "net:connect", f"{{ host: {q(host)}{extra}, port: 443 }}", verdict)


M = 60


def RED(t):
    return check(t, 1)


def PUSH(t, v):
    return git(t, v, "push", "origin")


CASES = {
    # push_after_green_check
    "push: refused with no check run": [PUSH(10, "DENY")],
    "push: refused after a failing check": [RED(10), PUSH(20, "DENY")],
    "push: refused after a dirty-tree check (exit 2)": [check(10, 2), PUSH(20, "DENY")],
    "push: allowed after a passing check": [check(10, 0), PUSH(20, "ALLOW")],
    "push: a commit after the pass closes it, a new pass reopens it": [
        check(10, 0),
        git(20, "ALLOW", "commit", "-m", "x"),
        PUSH(30, "DENY"),
        check(40, 0),
        PUSH(50, "ALLOW"),
    ],
    "push: a failed commit attempt also closes it": [
        check(10, 0),
        git(20, "ALLOW", "commit", "-m", "x", status=1),
        PUSH(30, "DENY"),
    ],
    "push: a file write after the pass leaves it open (HEAD is unchanged)": [
        RED(5),
        check(10, 0),
        write(20, "notes.txt", "ALLOW"),
        PUSH(30, "ALLOW"),
    ],
    "push: Monty `python3 check.py` exit 0 does not open it": [
        monty(10, 0, ("check.py",)),
        PUSH(20, "DENY"),
    ],
    "push: a passing run of another file does not count": [
        spawn(10, "DENY", "python3.12", PY, ("fake_check.py",), 0),
        PUSH(20, "DENY"),
    ],
    "push: check.py with extra arguments is refused and does not count": [
        check(10, 0, verdict="DENY", args=("check.py", "--skip")),
        PUSH(20, "DENY"),
    ],
    "push: a passing check.py under another interpreter does not count": [
        spawned(10, 0, "python3.9", "/usr/bin/python3.9", ("check.py",)),
        PUSH(20, "DENY"),
    ],
    "push: a passing check.py from another directory does not count": [
        spawned(10, 0, "python3.12", PY, ("check.py",), cwd="/home/agent/codex-tmp"),
        PUSH(20, "DENY"),
    ],
    "push: a pass older than 30 minutes is stale": [
        check(10, 0),
        PUSH(10 + 31 * M, "DENY"),
    ],
    "push: a pass 29 minutes ago still counts": [
        check(10, 0),
        PUSH(10 + 29 * M, "ALLOW"),
    ],
    # push_shape
    "shape: `git push` with no remote refused": [check(10, 0), git(20, "DENY", "push")],
    "shape: `git push origin main --force` refused": [
        check(10, 0),
        git(20, "DENY", "push", "origin", "main", "--force"),
    ],
    "shape: `git push origin HEAD~1:refs/heads/x` refused": [
        check(10, 0),
        git(20, "DENY", "push", "origin", "HEAD~1:refs/heads/x"),
    ],
    "shape: `git push other` refused": [check(10, 0), git(20, "DENY", "push", "other")],
    # git_commands
    "git: add, commit, status, diff, log, show permitted": [
        git(10, "ALLOW", "add", "calc.py"),
        git(20, "ALLOW", "commit", "-m", "x"),
        git(30, "ALLOW", "status"),
        git(40, "ALLOW", "diff"),
        git(50, "ALLOW", "log"),
        git(60, "ALLOW", "show"),
    ],
    "git: checkout refused": [git(10, "DENY", "checkout", "--", "calc.py")],
    "git: stash refused": [git(10, "DENY", "stash")],
    "git: reset refused": [git(10, "DENY", "reset", "--hard", "HEAD~1")],
    "git: config refused": [git(10, "DENY", "config", "core.hooksPath", "x")],
    "git: `git -c` refused": [git(10, "DENY", "-c", "core.hooksPath=x", "commit")],
    "git: from another directory refused": [
        spawn(10, "DENY", "git", GIT, ("status",), cwd="/home/agent/codex-tmp")
    ],
    "host: another host program refused": [
        spawn(10, "DENY", "uname", "/usr/bin/uname", ("-a",))
    ],
    # git_dir_is_gits
    "gitdir: pre-push hook write refused, even after a red check": [
        RED(10),
        write(20, ".git/hooks/pre-push", "DENY"),
    ],
    "gitdir: .git/config write refused": [RED(10), write(20, ".git/config", "DENY")],
    "gitdir: deleting .git/index refused": [
        RED(10),
        fs(20, "fs:delete", "~/project/.git/index", "DENY"),
    ],
    # red_before_green
    "red: a code write before any check refused": [write(10, "calc.py", "DENY")],
    "red: a code write after only a dirty-tree check refused": [
        check(10, 2),
        write(20, "calc.py", "DENY"),
    ],
    "red: a code write after only a passing check refused": [
        check(10, 0),
        write(20, "calc.py", "DENY"),
    ],
    "red: a code write after only a check that could not run (exit 3) refused": [
        check(10, 3),
        write(20, "calc.py", "DENY"),
    ],
    "red: a code write after a failing check allowed": [
        RED(10),
        write(20, "calc.py", "ALLOW"),
    ],
    "red: /dev/null stays writable before any check": [
        fs(10, "fs:write", "/dev/null", "ALLOW")
    ],
    # agent_state
    "state: the agent's state directory stays readable and writable before any check": [
        fs(10, "fs:write", "~/agent-state/tmp/claude-ab12-cwd", "ALLOW"),
        fs(
            20,
            "fs:read",
            "~/agent-state/.claude/shell-snapshots/snapshot-zsh-1.sh",
            "ALLOW",
        ),
    ],
    "state: a path beside the state directory stays refused": [
        fs(10, "fs:write", "~/agent-state-other/x", "DENY"),
        fs(20, "fs:write", "~/origin.git/refs/heads/x", "DENY"),
    ],
    # checks_are_frozen
    "frozen: check.py write refused after a red check": [
        RED(10),
        write(20, "check.py", "DENY"),
    ],
    "frozen: test_calc.py write refused after a red check": [
        RED(10),
        write(20, "test_calc.py", "DENY"),
    ],
    "frozen: tests/ write refused after a red check": [
        RED(10),
        write(20, "tests/test_more.py", "DENY"),
    ],
    "frozen: a move onto check.py refused": [
        RED(10),
        fs(20, "fs:move", "~/project/check.py", "DENY"),
    ],
    # the rules kept from the parent example
    "env: reading .env refused": [fs(10, "fs:read", "~/project/.env", "DENY")],
    "env: reading calc.py allowed": [fs(10, "fs:read", "~/project/calc.py", "ALLOW")],
    "delete: deleting calc.py refused": [
        RED(10),
        fs(20, "fs:delete", "~/project/calc.py", "DENY"),
    ],
    "metadata: connect by name refused": [connect(10, "169.254.169.254", "DENY")],
    "metadata: connect to a link-local address refused": [
        connect(10, "evil.example", "DENY", ip="169.254.10.10")
    ],
    "model: connect to bedrock-runtime allowed": [connect(10, MODEL, "ALLOW")],
    "budget: the 400th request in an hour is allowed and the 401st refused": [
        model(10 + i, "ALLOW") for i in range(400)
    ]
    + [model(500, "DENY")],
    "stall: 9 failing checks, writes still allowed": [
        check(10 + 2 * i, 1) for i in range(9)
    ]
    + [write(100, "calc.py", "ALLOW")],
    "stall: 10 failing checks in an hour, project writes refused": [
        check(10 + 2 * i, 1) for i in range(10)
    ]
    + [write(100, "calc.py", "DENY")],
    "stall: 10 dirty-tree checks count too": [RED(5)]
    + [check(10 + 2 * i, 2) for i in range(10)]
    + [write(100, "calc.py", "DENY")],
    "stall: failures older than an hour age out": [
        check(10 + 2 * i, 1) for i in range(10)
    ]
    + [write(10 + 61 * M + 40, "calc.py", "ALLOW")],
    # whole runs
    "run: honest order fix, add, commit, check, push": [
        RED(10),
        write(20, "calc.py", "ALLOW"),
        git(30, "ALLOW", "add", "calc.py"),
        git(40, "ALLOW", "commit", "-m", "fix add"),
        check(50, 0),
        PUSH(60, "ALLOW"),
    ],
    "run: a check before the commit sees a dirty tree, so the commit needs another check": [
        RED(10),
        write(20, "calc.py", "ALLOW"),
        check(30, 2),
        git(40, "ALLOW", "add", "calc.py"),
        git(50, "ALLOW", "commit", "-m", "fix"),
        PUSH(60, "DENY"),
        check(70, 0),
        PUSH(80, "ALLOW"),
    ],
    "run: stage a bad file, edit to good, the check sees a dirty tree, push refused": [
        RED(10),
        write(20, "calc.py", "ALLOW"),
        git(30, "ALLOW", "add", "calc.py"),
        write(40, "calc.py", "ALLOW"),
        check(50, 2),
        git(60, "ALLOW", "commit", "-m", "x"),
        check(70, 2),
        PUSH(80, "DENY"),
    ],
}

# One edit per rule property. Each broken copy must make at least one case differ.
GREEN = 'input.cwd: "/home/agent/project", output.status: 0 }\n};'
FAULTS = {
    "push gate has no commit reset": (
        '!Box::Action::"shell:spawn"::response{ input.program_path: "/usr/bin/git", input.arg1: "commit" }\n    since within 30m\n  ',
        "formerly within 30m ",
    ),
    "push gate reads any program": (
        'Box::Action::"shell:spawn"::response{ input.program_path: "/usr/bin/python3.12", input.arg1: "check.py", input.arg_count: 1, input.cwd: "/home/agent/project", output.status: 0 }',
        'Box::Action::"shell:spawn"::response{ input.arg1: "check.py", input.arg_count: 1, input.cwd: "/home/agent/project", output.status: 0 }',
    ),
    "push gate reads any directory": (GREEN, "output.status: 0 }\n};"),
    "push gate window 1h": ("since within 30m", "since within 1h"),
    "git permits checkout": (
        '"show", "add", "commit", "push"]',
        '"show", "add", "commit", "push", "checkout"]',
    ),
    "no .git rule": (
        'when { context.input.path == "~/project/.git" || context.input.path like "~/project/.git/*" };',
        "when { false };",
    ),
    "red rule accepts any status": (
        'input.cwd: "/home/agent/project", output.status: 1 }',
        'input.cwd: "/home/agent/project" }',
    ),
    "no push shape": (
        "unless { context.input.arg_count == 2",
        "unless { true || context.input.arg_count == 2",
    ),
    "check.py not frozen": ('  context.input.path == "~/project/check.py" ||\n', ""),
    "state rule reaches beside its directory": (
        'like "~/agent-state/*"',
        'like "~/agent-state*"',
    ),
    "stall threshold 11": ("&& fails >= 10", "&& fails >= 11"),
    "budget cap 401": ("&& calls > 400", "&& calls > 401"),
}


def dogwood() -> str:
    path = os.environ.get("DOGWOOD")
    if not path or not Path(path).is_file():
        sys.exit("set DOGWOOD to the dogwood CLI (1.1.0 or later)")
    return path


def run(dw, policy: Path, steps):
    trace = "\n".join(s for s, _ in steps) + "\n"
    want = [v for _, v in steps if v]
    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
        f.write(trace)
    try:
        p = subprocess.run(
            [
                dw,
                "replay",
                str(policy),
                "--policy-schema",
                str(SCHEMA / "actions.cedarschema"),
                "--event-schema",
                str(SCHEMA / "events.dwschema"),
                "--trace",
                f.name,
            ],
            capture_output=True,
            text=True,
        )
    finally:
        os.unlink(f.name)
    got = [
        w
        for line in p.stdout.splitlines()
        for w in ("ALLOW", "DENY")
        if f"): {w}" in line
    ]
    return p.returncode == 0 and got == want, got, want, p


def run_all(dw, policy: Path, quiet=False) -> int:
    bad = 0
    for name, steps in CASES.items():
        ok, got, want, p = run(dw, policy, steps)
        bad += not ok
        if not quiet:
            print(
                f"{'ok ' if ok else 'BAD'} {name}"
                + (
                    ""
                    if ok
                    else f"  got={got[:6]} want={want[:6]} {p.stderr.strip()[:300]}"
                )
            )
    return bad


def main():
    dw = dogwood()
    v = subprocess.run(
        [
            dw,
            "validate",
            str(POLICY),
            "--policy-schema",
            str(SCHEMA / "actions.cedarschema"),
            "--event-schema",
            str(SCHEMA / "events.dwschema"),
        ],
        capture_output=True,
        text=True,
    )
    print(v.stdout.strip() or v.stderr.strip())
    if v.returncode != 0:
        return 1
    bad = run_all(dw, POLICY)
    print(f"{bad} of {len(CASES)} cases differ")
    if bad or "--faults" not in sys.argv:
        return 1 if bad else 0
    text = POLICY.read_text()
    missed = []
    for name, (old, new) in FAULTS.items():
        if old not in text:
            missed.append(f"{name}: edit target not found")
            continue
        with tempfile.NamedTemporaryFile(
            "w", suffix=".dw", dir=POLICY.parent, delete=False
        ) as f:
            f.write(text.replace(old, new, 1))
        try:
            caught = run_all(dw, Path(f.name), quiet=True)
        finally:
            os.unlink(f.name)
        print(
            f"{'caught' if caught else 'MISSED'} fault: {name} ({caught} cases differ)"
        )
        if not caught:
            missed.append(name)
    print(f"{len(FAULTS) - len(missed)} of {len(FAULTS)} faults caught")
    return 1 if missed else 0


if __name__ == "__main__":
    sys.exit(main())
