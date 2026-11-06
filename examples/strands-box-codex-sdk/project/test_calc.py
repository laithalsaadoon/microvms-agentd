# SPDX-License-Identifier: Apache-2.0
"""The project's tests. The policy freezes this file: Codex runs it and never changes it.

Run it with `python3.12 test_calc.py`. It exits 0 when every case passes and 1 when one fails,
and the policy's history rules read that exit status.
"""

import sys

from calc import add

failures = 0
for a, b, want in [(2, 3, 5), (-1, 1, 0), (0, 0, 0)]:
    got = add(a, b)
    if got != want:
        print(f"FAIL: add({a}, {b}) returned {got!r}, expected {want}")
        failures += 1
if failures:
    sys.exit(1)
print("ok")
