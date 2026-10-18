# SPDX-License-Identifier: Apache-2.0
"""A probe failure still cleans a known VM that the listing fails to return."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness.results import Results
from lanes.ensure_image import ensure_image_cleanup
from lanes.isolated_image import isolated_image, launch_image

from selftest.ensure_image import FakeEnsureImageAws


class HiddenVmAws(FakeEnsureImageAws):
    def __init__(self) -> None:
        super().__init__()
        self.direct_reads: list[str] = []

    def get_paginator(self, operation: str):
        if operation == "list_microvms":
            return SimpleNamespace(paginate=lambda **_: [{"items": []}])
        return super().get_paginator(operation)

    def get_microvm(self, *, microvmIdentifier: str) -> dict:
        self.direct_reads.append(microvmIdentifier)
        return super().get_microvm(microvmIdentifier=microvmIdentifier)


def check_isolated_known_vm(results: Results) -> None:
    aws = HiddenVmAws()

    class Cli:
        region = "us-east-1"

        def call(self, *args: str, **_: object) -> SimpleNamespace:
            if args[0] == "build":
                name = args[args.index("--name") + 1] + "-0123456789ab"
                arn = f"arn:aws:lambda:us-east-1:000000000000:microvm-image:{name}"
                aws.images[arn] = {"imageArn": arn, "name": name, "state": "CREATED"}
                return SimpleNamespace(data={"imageIdentifier": arn})
            if args[0] == "run":
                aws.vms["vm-known"] = {
                    "microvmId": "vm-known",
                    "imageArn": args[args.index("--image") + 1],
                    "state": "RUNNING",
                }
                return SimpleNamespace(data={"microvmId": "vm-known"})
            raise AssertionError(args[0])

    probe = Results(probe=True)
    with (
        patch.dict("os.environ", {"MICROVM_BUCKET": "bucket"}),
        patch(
            "lanes.isolated_image.ensure_image_cleanup",
            side_effect=lambda *args: ensure_image_cleanup(*args, sleep=lambda _: None),
        ),
    ):
        try:
            with isolated_image(
                Cli(), Path("agentd"), aws, probe, "test", "FROM scratch"
            ) as (built, cleanup):
                launch_image(Cli(), built, cleanup)
                raise RuntimeError("the probe failed after launch")
        except RuntimeError:
            pass
    results.check(
        "isolated cleanup reads and terminates a known VM omitted by listing after a probe raises",
        bool(aws.direct_reads)
        and set(aws.direct_reads) == {"vm-known"}
        and aws.vms["vm-known"]["state"] == "TERMINATED"
        and not aws.images
        and not probe.failed,
    )
