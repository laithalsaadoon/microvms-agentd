# SPDX-License-Identifier: Apache-2.0
"""The project's one check. The policy freezes this file and reads its exit status.

Run it with `python3.12 check.py`. It exits 3 when it can't read the git tree, and 2 when the
tree has any change or untracked file, so a pass always describes HEAD. Otherwise it runs the
tests (tests/ with unittest, or test_calc.py) and exits 1 when one fails and 0 when every one
passes. It writes nothing, so it runs on a read-only project. A failure to run is never a 1,
because the policy reads a 1 as a test that ran and failed.
"""

import os
import subprocess
import sys

env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
try:
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        capture_output=True,
        text=True,
        env=env,
    )
except OSError as error:
    print(f"check: git could not run: {error}")
    sys.exit(3)
if status.returncode != 0:
    print(f"check: git status failed: {status.stderr.strip()}")
    sys.exit(3)
if status.stdout.strip():
    print("check: commit every change first; the tree differs from HEAD:")
    print(status.stdout, end="")
    sys.exit(2)

# A project with a tests/ package runs it with unittest; the calculator runs its one test file.
if os.path.isdir("tests"):
    command = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."]
else:
    command = [sys.executable, "test_calc.py"]
tests = subprocess.run(command, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
sys.exit(0 if tests.returncode == 0 else 1)
