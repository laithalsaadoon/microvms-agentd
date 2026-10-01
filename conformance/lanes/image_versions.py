# SPDX-License-Identifier: Apache-2.0
"""An image's versions and builds through the CLI (#264): `image-versions`, `image-builds`, and
`image-set-status`'s round trip, INACTIVE, a pinned launch refused, and ACTIVE again.

Run against the suite's own image from `drive_lifecycle_by_id`. The retire is the one write
here, and every exit path restores the version before anything is asserted, as
`crates/microvms-core/tests/live_versions.rs` does for core's call: an INACTIVE version starts
and stops nothing, and the later sections launch from this image.
"""

from __future__ import annotations

import secrets
from typing import Any

from harness.constants import BASELINE_MEMORY_MIB
from harness.envelope import Envelope, KindError
from harness.results import Results, section_failure_detail

IMAGE_VERSIONS_LISTED = (
    "image-versions lists the suite image's one version as ACTIVE (#264)"
)
IMAGE_BUILDS_READ = (
    "image-builds lists the version's builds, and --build-id reads one (#264)"
)
IMAGE_RETIRED = "image-set-status INACTIVE reads the version back INACTIVE (#264)"
IMAGE_PINNED_LAUNCH_REFUSED = "a launch pinned to an INACTIVE version is refused (#264)"
IMAGE_RESTORED = "image-set-status ACTIVE restores the version (#264)"
IMAGE_RESTORE_READ_BACK = (
    "image-versions reads the restored version back as ACTIVE (#264)"
)


def call(cli: Any, *args: str, timeout: float = 300.0) -> Envelope | KindError:
    """One CLI call, its failure envelope returned rather than raised, so a round trip can
    restore what it changed before it reports."""
    try:
        return cli.call(*args, "--region", cli.region, timeout=timeout)
    except KindError as exc:
        return exc


def versions_of(reply: Envelope | KindError) -> list[dict[str, Any]]:
    """The `versions` of an `image-versions` reply, or nothing when it failed."""
    if isinstance(reply, KindError):
        return []
    return [item for item in reply.data.get("versions") or [] if isinstance(item, dict)]


def drive_image_versions(cli: Any, image_arn: str, results: Results) -> None:
    """The CLI's image-version commands against the suite's image.

    The version round-tripped is the image's only one, and only when it is `ACTIVE`: a
    listing that says otherwise is its own failed check and flips nothing, since a version this
    section can't identify is one it mustn't retire.
    """
    listed = call(cli, "image-versions", image_arn)
    versions = versions_of(listed)
    active = [item for item in versions if item.get("status") == "ACTIVE"]
    version = str(active[0].get("imageVersion") or "") if len(versions) == 1 else ""
    results.check(
        IMAGE_VERSIONS_LISTED,
        bool(version) and len(active) == 1,
        section_failure_detail(listed)
        if isinstance(listed, KindError)
        else f"versions={[(v.get('imageVersion'), v.get('status')) for v in versions]!r}",
    )
    if not version:
        return

    builds = call(cli, "image-builds", image_arn, version)
    listed_builds = (
        [] if isinstance(builds, KindError) else list(builds.data.get("builds") or [])
    )
    build_id = str((listed_builds[0] if listed_builds else {}).get("buildId") or "")
    one = call(cli, "image-builds", image_arn, version, "--build-id", build_id)
    read = [] if isinstance(one, KindError) else list(one.data.get("builds") or [])
    results.check(
        IMAGE_BUILDS_READ,
        bool(build_id) and len(read) == 1 and read[0].get("buildId") == build_id,
        f"builds={[(b.get('buildId'), b.get('buildState')) for b in listed_builds]!r} "
        f"read={[b.get('buildId') for b in read]!r}",
    )

    retired: Envelope | KindError | None = None
    pinned: Envelope | KindError | None = None
    try:
        retired = call(cli, "image-set-status", image_arn, version, "INACTIVE")
        # Only against a version the service read back INACTIVE, since against an ACTIVE one
        # it launches. A real launch: an incomplete one isn't refused locally
        # (live_versions.rs measured that), so the exec is `true` and `run` tears down
        # whatever it launched.
        if isinstance(retired, Envelope) and retired.data.get("status") == "INACTIVE":
            pinned = call(
                cli,
                "run",
                "--image",
                image_arn,
                "--image-version",
                version,
                "--name",
                f"microvm-cli-conformance-pinned-{secrets.token_hex(4)}",
                "--memory",
                str(BASELINE_MEMORY_MIB),
                "--exec",
                "true",
                "--max-duration-sec",
                "300",
                timeout=15 * 60,
            )
    finally:
        restored = call(cli, "image-set-status", image_arn, version, "ACTIVE")
    back = call(cli, "image-versions", image_arn)

    results.check(
        IMAGE_RETIRED,
        isinstance(retired, Envelope) and retired.data.get("status") == "INACTIVE",
        section_failure_detail(retired)
        if isinstance(retired, KindError)
        else f"status={None if retired is None else retired.data.get('status')!r}",
    )
    results.check(
        IMAGE_PINNED_LAUNCH_REFUSED,
        isinstance(pinned, KindError),
        section_failure_detail(pinned)
        if isinstance(pinned, KindError)
        else "not attempted: the retire didn't take"
        if pinned is None
        else f"launched={pinned.data.get('microvmId')!r}",
    )
    results.check(
        IMAGE_RESTORED,
        isinstance(restored, Envelope) and restored.data.get("status") == "ACTIVE",
        section_failure_detail(restored)
        if isinstance(restored, KindError)
        else f"status={restored.data.get('status')!r}",
    )
    statuses = {
        str(item.get("imageVersion")): item.get("status") for item in versions_of(back)
    }
    results.check(
        IMAGE_RESTORE_READ_BACK,
        statuses.get(version) == "ACTIVE",
        f"statuses={statuses!r}",
    )
