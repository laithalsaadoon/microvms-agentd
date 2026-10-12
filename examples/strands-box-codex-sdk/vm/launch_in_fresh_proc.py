# SPDX-License-Identifier: Apache-2.0
"""Run a command as uid 1000 in a private mount namespace whose /proc is a fresh procfs.

Strands Box's Linux backend mounts a fresh procfs inside the user namespace it creates for the
agent, and the kernel refuses that mount (EPERM, `mount_too_revealing`) while every procfs
mount the namespace can see has something mounted over part of it. A Lambda MicroVM guest
always has one: without identity repair the platform masks /proc/kcore, /proc/keys, and
/proc/timer_list and leaves the guest without CAP_SYS_ADMIN, and with identity repair agentd
bind-mounts its own boot_id over /proc/sys/kernel/random/boot_id.

So the image is built with `--repair-identity`, which requests additionalOsCapabilities
["ALL"], and this launcher, run as root, gives the work a mount namespace of its own with a
fresh procfs at /proc. The VM's global mounts, agentd's boot_id among them, stay as they are.
It then drops to uid 1000 for good and execs the command, so Box and everything it starts run
unprivileged.

Usage: launch_in_fresh_proc.py PROGRAM [ARG ...]
"""

import ctypes
import os
import sys

MS_REC = 0x4000
MS_PRIVATE = 0x40000
AGENT_UID = AGENT_GID = 1000

libc = ctypes.CDLL(None, use_errno=True)


def must(rc: int, what: str) -> None:
    if rc != 0:
        sys.exit(f"launch_in_fresh_proc: {what}: {os.strerror(ctypes.get_errno())}")


os.unshare(os.CLONE_NEWNS)
# Keep this namespace's mounts from propagating back to the VM's.
must(libc.mount(b"none", b"/", None, MS_REC | MS_PRIVATE, None), "make / private")
must(libc.mount(b"proc", b"/proc", b"proc", 0, None), "mount a fresh procfs at /proc")
os.setgroups([])
os.setgid(AGENT_GID)
os.setuid(AGENT_UID)
os.chdir("/home/agent")
env = {"HOME": "/home/agent", "PATH": "/opt/guest/.venv/bin:/usr/bin:/bin"}
os.execve(sys.argv[1], sys.argv[1:], env)
