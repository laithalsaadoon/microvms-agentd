# SPDX-License-Identifier: Apache-2.0
"""An image owned by one section, with cleanup independent of the CLI's ledger."""

from __future__ import annotations

import contextlib
import os
import secrets
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from harness.cli import Cli
from harness.constants import BASELINE_MEMORY_MIB
from harness.envelope import Envelope
from harness.results import Results

from lanes.ensure_image import ensure_image_cleanup


class CleanupResults(Results):
    def __init__(self, target: Results, label: str) -> None:
        super().__init__()
        self.target = target
        self.label = label

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        return self.target.check(f"{self.label}: {name}", ok, detail)


@contextlib.contextmanager
def isolated_image(
    cli: Cli,
    binary: Path,
    aws: Any,
    results: Results,
    label: str,
    dockerfile: str,
    *,
    repair: bool = False,
) -> Iterator[tuple[Envelope, dict[str, Any]]]:
    prefix = f"microvm-cli-{label}-{secrets.token_hex(4)}"
    report: dict[str, Any] = {}
    with tempfile.TemporaryDirectory() as tmp:
        recipe = Path(tmp) / "Dockerfile"
        recipe.write_text(dockerfile)
        try:
            built = cli.call(
                "build",
                str(binary),
                "--dockerfile",
                str(recipe),
                "--name",
                prefix,
                "--reuse",
                "--s3-key-prefix",
                prefix,
                "--memory",
                str(BASELINE_MEMORY_MIB),
                "--region",
                cli.region,
                *(["--repair-identity"] if repair else []),
                timeout=50 * 60,
            )
            report["arn"] = built.data["imageIdentifier"]
            yield built, report
        finally:
            ensure_image_cleanup(
                report,
                prefix,
                os.environ["MICROVM_BUCKET"],
                prefix,
                aws,
                CleanupResults(results, label),
            )


def launch_image(cli: Cli, built: Envelope, cleanup: dict[str, Any]) -> Envelope:
    launched = cli.call(
        "run",
        "--image",
        str(built.data["imageIdentifier"]),
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
    return launched
