# SPDX-License-Identifier: Apache-2.0
"""Opt-in measurements for capability hardening (#599).

This section records observations rather than presuming whether AWS permits an
operation. It runs only with --only platform_measurements, before either feature
is selected.
The snapshot wrapper drops before execing agentd to measure inheritance; that
exec removes effective/permitted capabilities too. Its repair outcome cannot
rule out an in-process drop that preserves the daemon's effective capabilities.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any

from harness.cli import Cli, attach_args
from harness.constants import REPO
from harness.image import conformance_dockerfile, resolve_base_ref
from harness.results import Results

from lanes.isolated_image import isolated_image, launch_image

STATUS_FIELDS = {
    "CapInh",
    "CapPrm",
    "CapEff",
    "CapBnd",
    "CapAmb",
    "NoNewPrivs",
    "Seccomp",
}
DROPPED = (16, 17, 19, 21, 22, 38, 39)


def measurement_dockerfile(drop_before_daemon: bool) -> str:
    source = (REPO / "conformance/drivers/platform_probe.py").read_bytes()
    encoded = base64.b64encode(source).decode("ascii")
    recipe = conformance_dockerfile(resolve_base_ref())
    recipe = recipe.replace(
        'CMD ["/agentd"]',
        "RUN dnf install -y python3 iproute && dnf clean all\n"
        f"RUN printf '%s' '{encoded}' | base64 -d > /platform-probe.py\n"
        + (
            'CMD ["python3", "/platform-probe.py", "bootstrap", "--drop"]'
            if drop_before_daemon
            else 'CMD ["python3", "/platform-probe.py", "bootstrap"]'
        ),
    )
    return recipe


def check_measurement(report: dict[str, Any], label: str, results: Results) -> None:
    """Require every requested observation; permit measured refusals as observations."""

    def observed_status(value: Any) -> bool:
        return (
            isinstance(value, dict)
            and set(value) == STATUS_FIELDS
            and all(
                isinstance(item, str)
                and re.fullmatch(
                    r"[0-9a-fA-F]+" if key.startswith("Cap") else r"[0-9]+", item
                )
                is not None
                for key, item in value.items()
            )
        )

    def observed_errnos(value: Any, keys: set[str]) -> bool:
        return (
            isinstance(value, dict)
            and set(value) == keys
            and all(type(item) is int and item >= 0 for item in value.values())
        )

    capabilities = {str(cap) for cap in DROPPED}
    build = report.get("build")
    if not isinstance(build, dict):
        build = {}
    complete = all(
        observed_status(report.get(name))
        for name in ("daemon", "execBefore", "execAfter")
    )
    complete = complete and all(
        observed_status(build.get(name)) for name in ("before", "after")
    )
    complete = complete and observed_errnos(report.get("dropErrnos"), capabilities)
    complete = complete and observed_errnos(report.get("memOpenErrnos"), {"0", "4242"})
    build_drops = build.get("dropErrnos")
    complete = complete and (
        observed_errnos(build_drops, capabilities)
        or (label != "snapshot-drop" and build_drops == {})
    )
    ptrace = report.get("ptraceScope")
    complete = (
        complete
        and isinstance(ptrace, str)
        and (ptrace == "absent" or re.fullmatch(r"[0-9]+", ptrace) is not None)
    )
    for name in ("blackholeExit", "metadataConnectBefore"):
        complete = complete and type(report.get(name)) is int and report[name] >= 0
    if report.get("blackholeExit") == 0:
        after = report.get("metadataConnectAfter")
        complete = complete and type(after) is int and after >= 0
    results.check(
        f"platform measurement {label} has every requested observation", complete
    )


def drive_platform_measurements(
    cli: Cli, binary: Path, aws: Any, results: Results
) -> None:
    """Own three image variants; a refusal is evidence, never an invented capability."""
    for label, repair, drop in (
        ("default", False, False),
        ("repair", True, False),
        ("snapshot-drop", True, True),
    ):
        with isolated_image(
            cli,
            binary,
            aws,
            results,
            f"measure-{label}",
            measurement_dockerfile(drop),
            repair=repair,
        ) as (built, cleanup):
            launched = launch_image(cli, built, cleanup)
            measured = cli.call(
                "exec",
                "python3 /platform-probe.py measure",
                "--user",
                "0",
                *attach_args(cli, launched),
            )
            results.eq(
                f"platform measurement {label} probe exited zero",
                measured.data.get("exitCode"),
                0,
            )
            report = json.loads(measured.data.get("stdout") or "{}")
            check_measurement(report, label, results)
            print(
                f"MEASUREMENT {label} {json.dumps(report, sort_keys=True)}", flush=True
            )
