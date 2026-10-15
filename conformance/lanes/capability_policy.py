# SPDX-License-Identifier: Apache-2.0
"""AGENTD-26: the real startup policy, retained daemon authority, and child limits."""

from __future__ import annotations

import base64
import io
import json
import re
import tarfile
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from harness.cli import Cli, attach_args
from harness.constants import REPO
from harness.image import CONFORMANCE_USER, conformance_dockerfile, resolve_base_ref
from harness.results import Results

from lanes.isolated_image import isolated_image, launch_image
from lanes.platform_measurements import DROPPED, STATUS_FIELDS

DROP_MASK = sum(1 << cap for cap in DROPPED)
REQUEST_SECONDS = 60
FIFO_PATH = "/tmp/microvm-policy-reader"
BLOCKING_BYTES = b"policy-blocking-reader\n"
COMPAT_OUTPUT = "4242:4242\nchanged\nuploaded-appended\n4242:4242\n4242:4242\n"


def policy_dockerfile() -> str:
    recipe = conformance_dockerfile(resolve_base_ref())
    additions = [
        "RUN dnf install -y python3 && dnf clean all",
        "ENV AGENTD_DROP_EXEC_CAPABILITIES=true",
        f"ENV AGENTD_DEFAULT_USER={CONFORMANCE_USER}",
    ]
    for name, destination in (
        ("platform_probe.py", "/platform_probe.py"),
        ("capability_policy_probe.py", "/capability-policy-probe.py"),
    ):
        encoded = base64.b64encode(
            (REPO / "conformance/drivers" / name).read_bytes()
        ).decode("ascii")
        additions.append(f"RUN printf '%s' '{encoded}' | base64 -d > {destination}")
    hook = base64.b64encode(
        b"#!/bin/sh\nexec python3 /capability-policy-probe.py hook\n"
    ).decode("ascii")
    additions.extend(
        [
            "RUN mkdir -p /etc/agentd/hooks.d "
            f"&& printf '%s' '{hook}' | base64 -d > /etc/agentd/hooks.d/run "
            "&& chmod 0755 /etc/agentd/hooks.d/run",
            'CMD ["python3", "/capability-policy-probe.py", "bootstrap"]',
        ]
    )
    return recipe.replace('CMD ["/agentd"]', "\n".join(additions))


def capability_status(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == STATUS_FIELDS
        and all(
            isinstance(item, str)
            and re.fullmatch(
                r"[0-9a-fA-F]{16}" if name.startswith("Cap") else r"[0-9]+", item
            )
            is not None
            for name, item in value.items()
        )
    )


def matches_policy(value: Any, before: dict, *, child: bool = False) -> bool:
    if not capability_status(value):
        return False
    expected = int(before["CapBnd"], 16) & ~DROP_MASK
    return (
        int(value["CapBnd"], 16) == expected
        and int(value["CapInh"], 16) == 0
        and int(value["CapAmb"], 16) == 0
        and all(
            int(value[name], 16) == (expected if child else int(before[name], 16))
            for name in ("CapPrm", "CapEff")
        )
        and value["NoNewPrivs"] == before["NoNewPrivs"]
        and value["Seccomp"] == before["Seccomp"]
    )


def check_capability_policy(report: dict, repair: bool, results: Results) -> None:
    prefix = f"{'repair' if repair else 'default'} image"
    baseline = report.get("baseline")
    before = baseline.get("before") if isinstance(baseline, dict) else None
    seeded = baseline.get("seeded") if isinstance(baseline, dict) else None
    valid = (
        capability_status(before)
        and capability_status(seeded)
        and all(seeded[name] == before[name] for name in ("CapPrm", "CapEff", "CapBnd"))
        and int(seeded["CapInh"], 16) & 1 == 1
        and int(seeded["CapAmb"], 16) & 1 == 1
        and (
            not repair
            or all(
                int(before[name], 16) & DROP_MASK == DROP_MASK
                for name in ("CapPrm", "CapEff", "CapBnd")
            )
        )
    )
    results.check(
        f"AGENTD-26 {prefix} seeds nonempty inheritance without changing authority",
        valid,
    )
    results.check(
        f"AGENTD-26 {prefix} daemon drops only selected bounding bits and keeps P/E",
        valid and matches_policy(report.get("daemon"), before),
    )
    pid = report.get("daemonPid")
    threads = report.get("threads")
    # The daemon uses a current-thread runtime. The held FIFO read requires its
    # blocking pool, so this observation checks more than the main thread.
    tids = (
        [row.get("tid") for row in threads if isinstance(row, dict)]
        if isinstance(threads, list)
        else []
    )
    results.check(
        f"AGENTD-26 {prefix} runtime and blocking threads inherit the policy",
        valid
        and report.get("readerConnected") is True
        and report.get("blockingTransfer") is True
        and type(pid) is int
        and pid > 0
        and isinstance(threads, list)
        and len(threads) > 1
        and len(tids) == len(threads)
        and all(type(tid) is int and tid > 0 for tid in tids)
        and len(set(tids)) == len(tids)
        and pid in tids
        and all(matches_policy(row.get("status"), before) for row in threads),
    )
    for label, key in (("root exec", "rootExec"), ("root lifecycle hook", "hook")):
        child = report.get(key)
        results.check(
            f"AGENTD-26 {prefix} {label} inherits reduced capabilities",
            valid
            and isinstance(child, dict)
            and type(child.get("uid")) is int
            and child["uid"] == 0
            and matches_policy(child.get("status"), before, child=True),
        )
    results.check(
        f"AGENTD-26 {prefix} default-user upload and incremental sync remain writable",
        type(report.get("initialSyncExit")) is int
        and report["initialSyncExit"] == 0
        and type(report.get("compatExit")) is int
        and report["compatExit"] == 0
        and report.get("compatOutput") == COMPAT_OUTPUT,
    )
    steps = report.get("identitySteps")
    required = {"machine-id", "hostname", "boot-id"}
    results.check(
        f"AGENTD-26 {prefix} identity repair retains its privileged operations",
        report.get("repair") is repair
        and isinstance(steps, list)
        and (
            all(
                any(
                    isinstance(step, dict)
                    and step.get("name") == name
                    and step.get("outcome") == "repaired"
                    for step in steps
                )
                for name in required
            )
            if repair
            else steps == []
        ),
    )


def observe_policy(cli: Cli, launched: Any, directory: Path, repair: bool) -> dict:
    attach = attach_args(cli, launched)
    project = directory / "project"
    project.mkdir()
    (project / "source.txt").write_text("initial")
    (project / "deleted.txt").write_text("remove me")
    cli.call("sync", str(project), *attach, timeout=REQUEST_SECONDS)
    initial = cli.call(
        "exec",
        'test -f /workspace/deleted.txt && test "$(cat /workspace/source.txt)" = initial',
        *attach,
        timeout=REQUEST_SECONDS,
    )
    (project / "source.txt").write_text("changed")
    (project / "deleted.txt").unlink()
    cli.call("sync", str(project), *attach, timeout=REQUEST_SECONDS)
    archive = directory / "upload.tar"
    with tarfile.open(archive, "w") as packed:
        info = tarfile.TarInfo("payload.txt")
        payload = b"uploaded"
        info.size = len(payload)
        info.mode = 0o644
        packed.addfile(info, io.BytesIO(payload))
    cli.call(
        "cp",
        str(archive),
        "vm:/workspace/upload",
        "--tar",
        *attach,
        timeout=REQUEST_SECONDS,
    )
    compatible = cli.call(
        "exec",
        "set -e; test ! -e /workspace/deleted.txt; "
        'printf \'%s:%s\\n\' "$(id -u)" "$(id -g)"; '
        "cat /workspace/source.txt; printf '\\n'; "
        "printf -- '-appended' >> /workspace/upload/payload.txt; "
        "cat /workspace/upload/payload.txt; printf '\\n'; "
        "stat -c '%u:%g' /workspace/source.txt /workspace/upload/payload.txt",
        *attach,
        timeout=REQUEST_SECONDS,
    )
    steps = cli.call("health", *attach, timeout=REQUEST_SECONDS).data.get(
        "identitySteps"
    )
    cli.call(
        "exec",
        f"python3 /capability-policy-probe.py fifo {FIFO_PATH}",
        "--user",
        "0",
        *attach,
        timeout=REQUEST_SECONDS,
    )
    downloaded = directory / "blocking-reader.txt"
    # A bounded public file read starts before the probe connects its FIFO
    # writer. ThreadPoolExecutor joins even on failure; Cli.call has its own
    # process timeout, then isolated_image independently cleans the VM.
    with ThreadPoolExecutor(max_workers=1) as worker:
        transfer = worker.submit(
            cli.call,
            "cp",
            f"vm:{FIFO_PATH}",
            str(downloaded),
            *attach,
            timeout=REQUEST_SECONDS,
        )
        observed = cli.call(
            "exec",
            f"python3 /capability-policy-probe.py observe {FIFO_PATH}",
            "--user",
            "0",
            *attach,
            timeout=REQUEST_SECONDS,
        )
        transfer.result(timeout=REQUEST_SECONDS + 5)
    if type(observed.data.get("exitCode")) is not int or observed.data["exitCode"] != 0:
        raise ValueError("capability policy probe failed")
    report = json.loads(observed.data.get("stdout") or "{}")
    report.update(
        initialSyncExit=initial.data.get("exitCode"),
        compatExit=compatible.data.get("exitCode"),
        compatOutput=compatible.data.get("stdout"),
        identitySteps=steps,
        blockingTransfer=downloaded.read_bytes() == BLOCKING_BYTES,
        repair=repair,
    )
    return report


def drive_capability_policy(cli: Cli, binary: Path, aws: Any, results: Results) -> None:
    """AGENTD-26 through default and repaired images, with independent cleanup."""
    for repair in (False, True):
        with (
            isolated_image(
                cli,
                binary,
                aws,
                results,
                f"capability-policy-{'repair' if repair else 'default'}",
                policy_dockerfile(),
                repair=repair,
            ) as (built, cleanup),
            tempfile.TemporaryDirectory() as directory,
        ):
            launched = launch_image(cli, built, cleanup)
            report = observe_policy(cli, launched, Path(directory), repair)
            print(
                f"CAPABILITY_POLICY {'repair' if repair else 'default'} {json.dumps(report, sort_keys=True)}",
                flush=True,
            )
            check_capability_policy(report, repair, results)
