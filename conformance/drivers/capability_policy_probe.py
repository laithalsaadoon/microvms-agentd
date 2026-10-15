# SPDX-License-Identifier: Apache-2.0
"""Observe the actual startup policy; never read process environments or memory."""

from __future__ import annotations

import ctypes
import errno
import json
import os
import sys
import time
from pathlib import Path

from platform_probe import status

BASELINE = Path("/tmp/microvm-policy-baseline.json")
HOOK = Path("/tmp/microvm-policy-hook.json")


def seed_inheritance() -> None:
    """Make I/A clearing observable without changing P/E or the bounding set."""

    class Header(ctypes.Structure):
        _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]

    class Data(ctypes.Structure):
        _fields_ = [
            ("effective", ctypes.c_uint32),
            ("permitted", ctypes.c_uint32),
            ("inheritable", ctypes.c_uint32),
        ]

    libc = ctypes.CDLL(None, use_errno=True)
    header = Header(0x20080522, 0)  # Linux capability ABI v3, this process.
    data = (Data * 2)()
    if libc.capget(ctypes.byref(header), ctypes.byref(data)) != 0:
        raise OSError(ctypes.get_errno(), "capget")
    data[0].inheritable |= 1  # CAP_CHOWN is harmless here and present in both images.
    if libc.capset(ctypes.byref(header), ctypes.byref(data)) != 0:
        raise OSError(ctypes.get_errno(), "seed inheritable CAP_CHOWN")
    if libc.prctl(47, 2, 0, 0, 0) != 0:  # PR_CAP_AMBIENT, PR_CAP_AMBIENT_RAISE.
        raise OSError(ctypes.get_errno(), "seed ambient CAP_CHOWN")


def observe() -> dict:
    baseline = json.loads(BASELINE.read_text())
    pid = baseline.get("daemonPid")
    if type(pid) is not int or pid <= 0:
        raise ValueError("invalid daemon PID in bootstrap baseline")
    # Exec preserves the bootstrap PID. Discovering it via /proc/*/exe would
    # require ptrace authority that the daemon keeps and this child must lose.
    tasks = sorted(Path(f"/proc/{pid}/task").iterdir(), key=lambda p: int(p.name))
    return {
        "baseline": baseline,
        "daemonPid": pid,
        "daemon": status(pid),
        "threads": [
            {"tid": int(task.name), "status": status(f"{pid}/task/{task.name}")}
            for task in tasks
        ],
        "rootExec": {"uid": os.getuid(), "status": status("self")},
        "hook": json.loads(HOOK.read_text()),
    }


def observe_with_reader(path: Path) -> dict:
    deadline = time.monotonic() + 30
    while True:
        try:
            writer = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
            break
        except OSError as error:
            if error.errno != errno.ENXIO or time.monotonic() >= deadline:
                raise
            time.sleep(0.05)
    # The daemon has opened this FIFO in its blocking pool. Keep its read alive
    # throughout observation; no network round trip can expire the idle worker.
    try:
        report = observe()
        report["readerConnected"] = True
        return report
    finally:
        try:
            os.write(writer, b"policy-blocking-reader\n")
        finally:
            os.close(writer)


def main() -> None:
    mode = sys.argv[1]
    if mode == "bootstrap":
        before = status("self")
        seed_inheritance()
        BASELINE.write_text(
            json.dumps(
                {"daemonPid": os.getpid(), "before": before, "seeded": status("self")}
            )
        )
        os.execv("/agentd", ["/agentd"])
    elif mode == "hook":
        HOOK.write_text(json.dumps({"uid": os.getuid(), "status": status("self")}))
    elif mode == "fifo":
        os.mkfifo(Path(sys.argv[2]), mode=0o600)
    elif mode == "observe":
        print(json.dumps(observe_with_reader(Path(sys.argv[2]))))
    else:
        raise ValueError("unknown mode")


if __name__ == "__main__":
    main()
