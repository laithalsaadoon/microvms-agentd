# SPDX-License-Identifier: Apache-2.0
"""Copy the example's policy/configuration for either hosted live-suite region."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


def validate_region(region: str) -> None:
    if region not in ("us-east-1", "us-west-2"):
        raise ValueError("the Box example supports us-east-1 and us-west-2")


def stage_box(region: str, source: Path, destination: Path) -> None:
    validate_region(region)
    shutil.copytree(source, destination)
    # Change only the copied endpoint configuration. The checked-in east-region
    # template remains unchanged, including after a failed or interrupted run.
    for relative in ("box.toml", "policy.dw", "codex-home/config.toml"):
        path = destination / relative
        text = path.read_text()
        path.write_text(text.replace("us-east-1", region))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("region")
    parser.add_argument("source", type=Path, nargs="?")
    parser.add_argument("destination", type=Path, nargs="?")
    args = parser.parse_args()
    try:
        validate_region(args.region)
    except ValueError as error:
        parser.error(str(error))
    if (args.source is None) != (args.destination is None):
        parser.error("source and destination must be supplied together")
    if args.source is not None:
        stage_box(args.region, args.source, args.destination)


if __name__ == "__main__":
    main()
