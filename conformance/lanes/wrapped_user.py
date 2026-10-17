# SPDX-License-Identifier: Apache-2.0
"""Builder-resolved Dockerfile USER defaults, with a root daemon (#626)."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from harness.cli import Cli, attach_args
from harness.image import conformance_dockerfile, resolve_base_ref
from harness.results import Results

from lanes.isolated_image import isolated_image, launch_image


def wrapped_user_checks(
    report: dict[str, Any],
    results: Results,
    label: str,
    uid: int,
    gid: int,
    default: str,
) -> None:
    results.eq(
        f"AGENTD-22 wrapped {label} discovers the effective image default",
        report.get("defaultUser"),
        default,
    )
    results.eq(
        f"AGENTD-22 wrapped {label} chooses the workload uid and primary gid",
        report.get("identity"),
        f"{uid}\n{gid}\n{gid}\n",
    )
    results.eq(
        f"AGENTD-23 wrapped {label} permits an explicit root request",
        report.get("root"),
        "0\n0\n",
    )
    results.eq(
        f"IMAGE-2 wrapped {label} seals the image-user record as root",
        report.get("mode"),
        "0:444\n",
    )
    results.check(
        f"IMAGE-2 wrapped {label} refuses workload changes to the captured user",
        type(report.get("writeExit")) is int and report["writeExit"] != 0,
    )


def drive_wrapped_user(cli: Cli, binary: Path, aws: Any, results: Results) -> None:
    base = conformance_dockerfile(resolve_base_ref()).replace(
        f"FROM {resolve_base_ref()}\n", f"FROM {resolve_base_ref()} AS userbase\n", 1
    )
    variants = (
        (
            "final-variable",
            base
            + "USER 5151:5252\nFROM userbase\nARG selected=conformance\nUSER ${selected}\n",
            4242,
            4242,
        ),
        (
            "inherited-numeric",
            base
            + "RUN sed -i '/^root:/d' /etc/passwd\nUSER 5151:5252\nFROM userbase\n",
            5151,
            5252,
        ),
        (
            "explicit-image",
            base
            + "USER 5151:5252\nFROM userbase\nENV AGENTD_DEFAULT_USER=conformance\n",
            4242,
            4242,
        ),
    )
    for label, task, uid, gid in variants:
        with tempfile.TemporaryDirectory() as tmp:
            recipe = Path(tmp) / "Dockerfile"
            recipe.write_text(task)
            wrapped = cli.call(
                "dockerfile", "--wrap", str(recipe), "--preserve-user"
            ).data["stanza"]
        with isolated_image(cli, binary, aws, results, f"wrap-{label}", wrapped) as (
            built,
            cleanup,
        ):
            launched = launch_image(cli, built, cleanup)
            attach = attach_args(cli, launched)
            report = {
                "defaultUser": cli.call("discovery", *attach).data.get("defaultUser"),
                "identity": cli.call("exec", "id -u; id -g; id -G", *attach).data.get(
                    "stdout"
                ),
                "root": cli.call(
                    "exec", "id -u; id -g", "--user", "0", "--group", "0", *attach
                ).data.get("stdout"),
                "mode": cli.call(
                    "exec",
                    "stat -c '%u:%a' /agentd-image-user.json",
                    "--user",
                    "0",
                    *attach,
                ).data.get("stdout"),
                "writeExit": cli.call(
                    "exec", "printf tamper >> /agentd-image-user.json", *attach
                ).data.get("exitCode"),
            }
            recorded = cli.call(
                "exec", "cat /agentd-image-user.json", "--user", "0", *attach
            ).data.get("stdout")
            captured = json.loads(recorded) if isinstance(recorded, str) else None
            expected_capture = (
                {"uid": 4242, "gid": 4242}
                if label == "final-variable"
                else {"uid": 5151, "gid": 5252}
            )
            results.eq(
                f"IMAGE-2 wrapped {label} records the builder-resolved identity",
                captured,
                expected_capture,
            )
            default = "conformance" if label == "explicit-image" else f"{uid}:{gid}"
            wrapped_user_checks(report, results, label, uid, gid, default)
