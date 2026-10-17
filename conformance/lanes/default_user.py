# SPDX-License-Identifier: Apache-2.0
"""AGENTD-22, AGENTD-23, and AGENTD-24 on an image with writable uploads (#604)."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import httpx
from harness.cli import Cli, attach_args
from harness.constants import BASELINE_MEMORY_MIB, SERVICE
from harness.daemon import Daemon
from harness.image import (
    CONFORMANCE_HOME,
    CONFORMANCE_UID,
    CONFORMANCE_USER,
    conformance_dockerfile,
    resolve_base_ref,
)
from harness.results import Results

from lanes.isolated_image import isolated_image

SYNC_WRITE = (
    'set -e; test "$(id -u)" = 4242; test "$(id -g)" = 4242; '
    "printf updated > source.txt; mkdir result; printf created > result/output.txt; "
    "printf sync-write-ok"
)


def default_user_checks(report: dict[str, Any], results: Results) -> None:
    """Verdicts are separate from I/O so missing or wrong observations have negative twins."""
    results.check(
        "AGENTD-22 authenticated discovery reports the configured image user",
        report.get("discoveryAvailable") is True
        and report.get("discoveryUser") == CONFORMANCE_USER,
    )
    sync_exit = report.get("syncExit")
    root_exit = report.get("missingRootExit")
    results.check(
        "AGENTD-22 run with a synced tree writes as the image's default user (#604)",
        type(sync_exit) is int
        and sync_exit == 0
        and report.get("syncStdout") == "sync-write-ok"
        and report.get("files") == "updated\ncreated\n4242:4242\n4242:4242\n",
    )
    results.eq(
        "AGENTD-22 an omitted user takes the image user's identity",
        report.get("defaultIdentity"),
        f"{CONFORMANCE_UID}\n{CONFORMANCE_UID}\n{CONFORMANCE_HOME}\n{CONFORMANCE_USER}\n",
    )
    results.eq(
        "AGENTD-23 an explicit root user overrides the image default",
        report.get("explicitRoot"),
        "0\n0\n",
    )
    results.eq(
        "AGENTD-22 each request resolves the current passwd row",
        report.get("changedIdentity"),
        "4343\n4344\n",
    )
    refused = report.get("refusal") or {}
    results.check(
        "AGENTD-24 a missing default is refused without a child",
        report.get("refusalStatus") == 400
        and refused.get("error") == "unknown_user"
        and "AGENTD_DEFAULT_USER" in str(refused.get("detail", ""))
        and CONFORMANCE_USER in str(refused.get("detail", ""))
        and report.get("execStatus") == 404
        and report.get("markerAbsent") is True,
    )
    results.check(
        "AGENTD-23 explicit root still works when the default has no passwd row",
        type(root_exit) is int and root_exit == 0,
    )


def drive_default_user(cli: Cli, binary: Path, aws: Any, results: Results) -> None:
    """AGENTD-22, AGENTD-23, AGENTD-24 through run, exec, and the raw start route.

    The shared image stays unchanged because its root-default behavior is another
    contract. Altering passwd after launch proves resolution happens per request.
    """
    recipe = (
        conformance_dockerfile(resolve_base_ref())
        + f"ENV AGENTD_DEFAULT_USER={CONFORMANCE_USER}\n"
    )
    with (
        isolated_image(cli, binary, aws, results, "default-user", recipe) as (
            built,
            cleanup,
        ),
        tempfile.TemporaryDirectory() as temp,
    ):
        project = Path(temp)
        (project / "source.txt").write_text("before")
        launched = cli.call(
            "run",
            str(project),
            "--image",
            str(built.data["imageIdentifier"]),
            "--exec",
            SYNC_WRITE,
            "--keep",
            "--memory",
            str(BASELINE_MEMORY_MIB),
            "--region",
            cli.region,
            "--max-idle-sec",
            "600",
            "--suspended-sec",
            "600",
            "--max-duration-sec",
            "900",
        )
        cleanup["vm"] = {"id": launched.data["microvmId"]}
        attach = attach_args(cli, launched)
        report = {
            "syncExit": launched.data.get("execExitCode"),
            "syncStdout": launched.data.get("stdout"),
        }
        discovery = cli.call("discovery", *attach).data
        report["discoveryAvailable"] = discovery.get("available")
        report["discoveryUser"] = discovery.get("defaultUser")
        files = cli.call(
            "exec",
            "cat /workspace/source.txt; printf '\\n'; cat /workspace/result/output.txt; printf '\\n'; stat -c '%u:%g' /workspace/source.txt /workspace/result/output.txt",
            "--user",
            "0",
            *attach,
        )
        report["files"] = files.data.get("stdout")
        report["defaultIdentity"] = cli.call(
            "exec", 'id -u; id -g; printf "%s\\n" "$HOME" "$USER"', *attach
        ).data.get("stdout")
        report["explicitRoot"] = cli.call(
            "exec", "id -u; id -g", "--user", "0", *attach
        ).data.get("stdout")
        cli.call(
            "exec",
            "cp /etc/passwd /tmp/default-user-passwd; sed -i '/^conformance:/s/:4242:4242:/:4343:4344:/' /etc/passwd",
            "--user",
            "0",
            *attach,
        )
        report["changedIdentity"] = cli.call("exec", "id -u; id -g", *attach).data.get(
            "stdout"
        )
        cli.call(
            "exec", "sed -i '/^conformance:/d' /etc/passwd", "--user", "0", *attach
        )
        daemon = Daemon(
            endpoint=str(launched.data["endpoint"]),
            agent_token=str(launched.data["agentToken"]),
            microvm_id=str(launched.data["microvmId"]),
            microvm_client=aws.client(SERVICE),
        )
        try:
            with httpx.Client(timeout=60) as client:
                refused = client.post(
                    f"{daemon.endpoint}/v1/exec/start",
                    headers=daemon.headers(daemon.agent_token),
                    json={
                        "exec_id": "missing-default",
                        "command": ["touch /tmp/default-user-unexpected-child"],
                        "shell": True,
                    },
                )
            report["refusalStatus"] = refused.status_code
            report["refusal"] = refused.json()
            report["execStatus"] = daemon.status(
                "GET", "/v1/exec/missing-default", token=daemon.agent_token
            )
            absent = cli.call(
                "exec",
                "test ! -e /tmp/default-user-unexpected-child",
                "--user",
                "0",
                *attach,
            )
            report["markerAbsent"] = absent.data.get("exitCode") == 0
            report["missingRootExit"] = absent.data.get("exitCode")
        finally:
            daemon.close()
        default_user_checks(report, results)
