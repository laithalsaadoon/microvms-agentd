# SPDX-License-Identifier: Apache-2.0
"""The image-version section against a fake CLI whose service can mislead it."""

from __future__ import annotations

from typing import Any

from harness.envelope import Envelope, KindError
from harness.results import Results
from lanes.image_versions import (
    IMAGE_BUILDS_READ,
    IMAGE_PINNED_LAUNCH_REFUSED,
    IMAGE_RESTORE_READ_BACK,
    IMAGE_RESTORED,
    IMAGE_RETIRED,
    IMAGE_VERSIONS_LISTED,
    drive_image_versions,
)

ARN = "arn:aws:lambda:us-east-1:123456789012:microvm-image:suite"


def refusal(message: str) -> KindError:
    return KindError(
        Envelope(
            status="error",
            api_version="1",
            type="",
            data={},
            code="ERR_PLATFORM",
            exit_code=9,
            error=message,
        )
    )


def ok(kind: str, data: dict[str, Any]) -> Envelope:
    return Envelope(status="ok", api_version="1", type=kind, data=data)


class FakeImageCli:
    """The `microvm` calls the section makes, answered from one image's versions.

    Each flag is a way the service can mislead the section: `advisory` launches a version
    that reads INACTIVE, `stuck` refuses every status change (the CLI's answer to a readback
    that didn't take), `restore_fails` refuses the change back to ACTIVE alone, and `builds`
    is what the build listing answers.
    """

    region = "us-east-1"

    def __init__(
        self,
        *,
        versions: tuple[str, ...] = ("1.0",),
        advisory: bool = False,
        stuck: bool = False,
        restore_fails: bool = False,
        builds: tuple[str, ...] = ("build-1", "build-2"),
    ) -> None:
        self.statuses = {version: "ACTIVE" for version in versions}
        self.advisory = advisory
        self.stuck = stuck
        self.restore_fails = restore_fails
        self.builds = builds
        self.commands: list[tuple[str, ...]] = []
        self.launched: list[str] = []

    def call(self, *args: str, timeout: float = 0.0) -> Envelope:
        self.commands.append(args)
        command, rest = args[0], [arg for arg in args[1:] if arg != "--region"]
        rest = [arg for arg in rest if arg != self.region]
        if command == "image-versions":
            return ok(
                "microvm.image.versions",
                {
                    "imageArn": ARN,
                    "versions": [
                        {"imageVersion": v, "state": "SUCCESSFUL", "status": s}
                        for v, s in self.statuses.items()
                    ],
                },
            )
        if command == "image-builds":
            listed = [{"buildId": b, "buildState": "SUCCESSFUL"} for b in self.builds]
            if "--build-id" in rest:
                wanted = rest[rest.index("--build-id") + 1]
                listed = [build for build in listed if build["buildId"] == wanted]
                if not listed:
                    raise refusal(f"no build {wanted!r}")
            return ok("microvm.image.builds", {"imageArn": ARN, "builds": listed})
        if command == "image-set-status":
            _, version, status = rest[:3]
            if self.stuck or (self.restore_fails and status == "ACTIVE"):
                raise refusal("read back a status other than the one asked for")
            self.statuses[version] = status
            return ok(
                "microvm.image.status", {"imageVersion": version, "status": status}
            )
        if command == "run":
            version = rest[rest.index("--image-version") + 1]
            if self.statuses.get(version) == "INACTIVE" and not self.advisory:
                raise refusal("No active version found for MicroVM image")
            self.launched.append(version)
            return ok("microvm.run", {"microvmId": "mvm-pinned"})
        raise AssertionError(f"the fake CLI has no answer for {args!r}")


def check_image_versions_section(results: Results) -> None:
    """The section's checks fail exactly where each service misleads it, and every path
    that retired the version restores it."""
    every = [
        IMAGE_VERSIONS_LISTED,
        IMAGE_BUILDS_READ,
        IMAGE_RETIRED,
        IMAGE_PINNED_LAUNCH_REFUSED,
        IMAGE_RESTORED,
        IMAGE_RESTORE_READ_BACK,
    ]
    scenarios: dict[str, tuple[FakeImageCli, list[str], list[str]]] = {
        "a service that enforces the retire": (FakeImageCli(), [], every),
        "a service that launches an INACTIVE version": (
            FakeImageCli(advisory=True),
            [IMAGE_PINNED_LAUNCH_REFUSED],
            every,
        ),
        "a retire that doesn't take": (
            FakeImageCli(stuck=True),
            [IMAGE_RETIRED, IMAGE_PINNED_LAUNCH_REFUSED, IMAGE_RESTORED],
            every,
        ),
        "a restore that fails": (
            FakeImageCli(restore_fails=True),
            [IMAGE_RESTORED, IMAGE_RESTORE_READ_BACK],
            every,
        ),
        "a version with no builds listed": (
            FakeImageCli(builds=()),
            [IMAGE_BUILDS_READ],
            every,
        ),
        "an image with two versions": (
            FakeImageCli(versions=("1.0", "2.0")),
            [IMAGE_VERSIONS_LISTED],
            [IMAGE_VERSIONS_LISTED],
        ),
    }
    wrong: list[str] = []
    for scenario, (cli, expected, ran_expected) in scenarios.items():
        probe = Results(probe=True)
        try:
            drive_image_versions(cli, ARN, probe)
        except Exception as exc:  # noqa: BLE001 - a raise is the finding
            wrong.append(f"{scenario}: raised {type(exc).__name__}: {exc}")
            continue
        failed = sorted(name for name, _ in probe.failed)
        ran = sorted({*probe.passed, *failed})
        retires = [c for c in cli.commands if c[0] == "image-set-status"]
        restored = not retires or retires[-1][3] == "ACTIVE"
        launched_active = [v for v in cli.launched if not cli.advisory]
        if (
            failed != sorted(expected)
            or ran != sorted(ran_expected)
            or not restored
            or launched_active
        ):
            wrong.append(
                f"{scenario}: failed {failed!r}, ran {ran!r}, last status change "
                f"{retires[-1][1:4] if retires else None!r}, launched {cli.launched!r}"
            )
    results.check(
        "the image-version section fails exactly where each service misleads it",
        not wrong,
        "; ".join(wrong),
    )
