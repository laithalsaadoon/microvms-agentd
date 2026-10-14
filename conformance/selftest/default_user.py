# SPDX-License-Identifier: Apache-2.0
"""Negative twins of every default-user live assertion, without AWS."""

from __future__ import annotations

import copy

from harness.results import Results
from lanes.default_user import default_user_checks


def check_default_user(results: Results) -> None:
    good = {
        "syncExit": 0,
        "syncStdout": "sync-write-ok",
        "files": "updated\ncreated\n4242:4242\n4242:4242\n",
        "defaultIdentity": "4242\n4242\n/home/conformance\nconformance\n",
        "explicitRoot": "0\n0\n",
        "changedIdentity": "4343\n4344\n",
        "refusalStatus": 400,
        "refusal": {
            "error": "unknown_user",
            "detail": "AGENTD_DEFAULT_USER conformance has no passwd row",
        },
        "execStatus": 404,
        "markerAbsent": True,
        "missingRootExit": 0,
    }
    positive = Results(probe=True)
    default_user_checks(good, positive)
    results.check(
        "default-user checks accept complete successful observations",
        not positive.failed,
    )
    for key in good:
        missing = copy.deepcopy(good)
        del missing[key]
        negative = Results(probe=True)
        default_user_checks(missing, negative)
        results.check(
            f"default-user checks reject missing {key}", bool(negative.failed)
        )
    for key, value in (
        ("files", "updated\ncreated\n0:0\n0:0\n"),
        ("defaultIdentity", "0\n0\n/root\nroot\n"),
        ("explicitRoot", "4242\n4242\n"),
        ("changedIdentity", "4242\n4242\n"),
        ("refusal", {"error": "unknown_user", "detail": "other"}),
        ("markerAbsent", False),
        ("missingRootExit", 1),
    ):
        broken = copy.deepcopy(good)
        broken[key] = value
        negative = Results(probe=True)
        default_user_checks(broken, negative)
        results.check(f"default-user checks reject wrong {key}", bool(negative.failed))
