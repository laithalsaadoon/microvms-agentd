# SPDX-License-Identifier: Apache-2.0
"""Negative twins for Dockerfile USER preservation."""

from harness.results import Results
from lanes.wrapped_user import wrapped_user_checks


def check_wrapped_user(results: Results) -> None:
    good = {
        "defaultUser": "4242:4343",
        "identity": "4242\n4343\n4343\n",
        "root": "0\n0\n",
        "mode": "0:444\n",
        "writeExit": 1,
    }
    positive = Results(probe=True)
    wrapped_user_checks(good, positive, "probe", 4242, 4343, "4242:4343")
    results.check(
        "wrapped-user checks accept correct observations", not positive.failed
    )
    for key in good:
        missing = {name: value for name, value in good.items() if name != key}
        negative = Results(probe=True)
        wrapped_user_checks(missing, negative, "probe", 4242, 4343, "4242:4343")
        results.check(
            f"wrapped-user checks reject missing {key}", bool(negative.failed)
        )
    for key, value in (
        ("defaultUser", "root"),
        ("identity", "0\n0\n0\n"),
        ("root", "4242\n4343\n"),
        ("mode", "0:666\n"),
        ("writeExit", 0),
        ("writeExit", False),
    ):
        negative = Results(probe=True)
        wrapped_user_checks(
            good | {key: value}, negative, "probe", 4242, 4343, "4242:4343"
        )
        results.check(f"wrapped-user checks reject wrong {key}", bool(negative.failed))
