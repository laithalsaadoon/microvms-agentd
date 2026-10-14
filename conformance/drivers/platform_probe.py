# SPDX-License-Identifier: Apache-2.0
"""Guest-only platform measurements. Read status fields, never process environments."""

from __future__ import annotations

import ctypes
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

CAPABILITIES = (16, 17, 19, 21, 22, 38, 39)
STATUS_FIELDS = {
    "CapInh",
    "CapPrm",
    "CapEff",
    "CapBnd",
    "CapAmb",
    "NoNewPrivs",
    "Seccomp",
}
BUILD_REPORT = Path("/tmp/microvm-cap-build.json")


def status(pid: int | str) -> dict[str, str]:
    return {
        key: value.strip()
        for line in Path(f"/proc/{pid}/status").read_text().splitlines()
        for key, _, value in [line.partition(":")]
        if key in STATUS_FIELDS
    }


def drop() -> dict[str, int]:
    libc = ctypes.CDLL(None, use_errno=True)
    results = {}
    for cap in CAPABILITIES:
        ctypes.set_errno(0)
        result = libc.prctl(24, cap, 0, 0, 0)  # PR_CAPBSET_DROP
        results[str(cap)] = 0 if result == 0 else ctypes.get_errno()
    return results


def daemon_pid() -> int:
    for path in Path("/proc").iterdir():
        if path.name.isdigit():
            try:
                if (path / "exe").readlink() == Path("/agentd"):
                    return int(path.name)
            except (FileNotFoundError, PermissionError):
                pass
    raise RuntimeError("no agentd process")


def metadata_connect() -> int:
    # Only a TCP connect; no HTTP request, token, or credential is read.
    with socket.socket() as stream:
        stream.settimeout(2)
        return stream.connect_ex(("169.254.169.254", 80))


def measure() -> dict:
    daemon = daemon_pid()
    before = status("self")
    drops = drop()
    after = status("self")
    try:
        ptrace_scope = Path("/proc/sys/kernel/yama/ptrace_scope").read_text().strip()
    except FileNotFoundError:
        ptrace_scope = "absent"
    mem = {}
    for uid in (0, 4242):
        try:
            os.seteuid(uid)
            try:
                # Opening alone measures access; do not inspect daemon memory.
                descriptor = os.open(f"/proc/{daemon}/mem", os.O_RDONLY)
                os.close(descriptor)
                mem[str(uid)] = 0
            except OSError as error:
                mem[str(uid)] = error.errno
        finally:
            os.seteuid(0)
    initial_connect = metadata_connect()
    installed = subprocess.run(
        ["/sbin/ip", "route", "replace", "blackhole", "169.254.169.254/32"],
        capture_output=True,
        check=False,
    )
    blocked_connect = metadata_connect() if installed.returncode == 0 else None
    if installed.returncode == 0:
        subprocess.run(
            ["/sbin/ip", "route", "del", "blackhole", "169.254.169.254/32"],
            capture_output=True,
            check=True,
        )
    return {
        "daemon": status(daemon),
        "execBefore": before,
        "execAfter": after,
        "dropErrnos": drops,
        "ptraceScope": ptrace_scope,
        "memOpenErrnos": mem,
        "metadataConnectBefore": initial_connect,
        "blackholeExit": installed.returncode,
        "metadataConnectAfter": blocked_connect,
        "build": json.loads(BUILD_REPORT.read_text()),
    }


def main() -> None:
    if sys.argv[1] == "bootstrap":
        before = status("self")
        drops = drop() if "--drop" in sys.argv else {}
        BUILD_REPORT.write_text(
            json.dumps(
                {
                    "before": before,
                    "after": status("self"),
                    "dropErrnos": drops,
                }
            )
        )
        os.execv("/agentd", ["/agentd"])
    elif sys.argv[1] == "measure":
        print(json.dumps(measure()))
    else:
        raise ValueError("unknown mode")


if __name__ == "__main__":
    main()
