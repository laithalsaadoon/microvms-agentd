#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml==6.0.3"]
# ///
# SPDX-License-Identifier: Apache-2.0
"""Hold release builds to fresh dependencies, pinned tools.

Runs with release:check and the release guard. Every expected artifact job must exist;
all release steps are checked so adding a job does not exempt its tools or checkout.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ".github/workflows/release.yml"
PACKAGE = "bindings/microvms-js/package.json"
LOCK = "bindings/microvms-js/package-lock.json"
ARTIFACT_JOBS = {"wheels", "sdist", "addons", "cli", "draft"}
EXACT = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?")
TAG_PATTERN = r"^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$"


def check(workflow: dict, package: dict, lock: dict) -> list[str]:
    """Return every policy violation in the parsed release inputs."""
    jobs = workflow.get("jobs")
    if not isinstance(jobs, dict) or not jobs:
        return [
            "release workflow has no jobs"  # pragma: no mutate (diagnostic wording does not change the verdict)
        ]
    problems = []
    for name in sorted(ARTIFACT_JOBS - jobs.keys()):
        problems.append(
            f"release workflow is missing artifact job {name}"  # pragma: no mutate (diagnostic wording does not change the verdict)
        )
    seen = set()
    for job_name, job in jobs.items():
        steps = job.get("steps", [])
        for step in steps:
            where = f"{job_name}: {step.get('name', step.get('uses', 'step'))}"  # pragma: no mutate (diagnostic context does not change the verdict)
            action = str(step.get("uses")).split("@")[0]
            options = step.get("with", {})
            run = str(step.get("run"))
            if "github.ref_name" in run:
                problems.append(
                    f"{where}: pass the tag through an environment variable"  # pragma: no mutate (diagnostic wording does not change the verdict)
                )
            if "cache" in action.lower():
                problems.append(
                    f"{where}: release jobs cannot restore caches"  # pragma: no mutate (diagnostic wording does not change the verdict)
                )
            if (
                action == "actions/checkout"
                and options.get("persist-credentials") is not False
            ):
                problems.append(
                    f"{where}: checkout must disable credential persistence"  # pragma: no mutate (diagnostic wording does not change the verdict)
                )
            if action == "actions/setup-node" and (
                options.get("package-manager-cache") is not False
                or options.get("cache")
            ):
                problems.append(
                    f"{where}: Node dependency caches must be disabled"  # pragma: no mutate (diagnostic wording does not change the verdict)
                )
            if action == "actions/setup-node" and not EXACT.fullmatch(
                str(options.get("node-version"))
            ):
                problems.append(
                    f"{where}: Node needs an exact version"  # pragma: no mutate (diagnostic wording does not change the verdict)
                )
            if (
                action == "astral-sh/setup-uv"
                and options.get("enable-cache") is not False
            ):
                problems.append(
                    f"{where}: uv caches must be disabled"  # pragma: no mutate (diagnostic wording does not change the verdict)
                )
            if action == "astral-sh/setup-uv" and not EXACT.fullmatch(
                str(options.get("version"))
            ):
                problems.append(
                    f"{where}: uv needs an exact version"  # pragma: no mutate (diagnostic wording does not change the verdict)
                )
            if "uvx maturin" in run:
                seen.add("maturin")
                version = re.search(r"uvx maturin@([^\s]+)", run)
                if not version or not EXACT.fullmatch(version[1]):
                    problems.append(
                        f"{where}: maturin needs an exact version"  # pragma: no mutate (diagnostic wording does not change the verdict)
                    )
            if action == "PyO3/maturin-action":
                problems.append(
                    f"{where}: install only the pinned release tools explicitly"  # pragma: no mutate (diagnostic wording does not change the verdict)
                )
            if re.search(r"\bnpx\b", run):
                problems.append(
                    f"{where}: use the locked local Node tools"  # pragma: no mutate (diagnostic wording does not change the verdict)
                )
            for line in run.splitlines():
                if "cargo install" in line:
                    seen.add("cargo install")
                    version = re.search(r"--version(?:=|\s+)([^\s]+)", line)
                    if (
                        not version
                        or not EXACT.fullmatch(version[1])
                        or "--locked" not in line
                    ):
                        problems.append(
                            f"{where}: cargo install needs an exact version and --locked"  # pragma: no mutate (diagnostic wording does not change the verdict)
                        )
            if "npm exec --offline -- napi build" in run:
                seen.add("napi")
                earlier = steps[: steps.index(step)]
                if not any(
                    s.get("run") == "npm ci --ignore-scripts"
                    and s.get("working-directory") == "bindings/microvms-js"
                    for s in earlier
                ):
                    problems.append(
                        f"{where}: install the locked Node tools before building"  # pragma: no mutate (diagnostic wording does not change the verdict)
                    )
    for tool in sorted({"maturin", "cargo install", "napi"} - seen):
        problems.append(
            f"release workflow has no {tool} tool step"  # pragma: no mutate (diagnostic wording does not change the verdict)
        )
    guard = jobs.get("guard", {}).get("steps", [])
    first = guard[0] if guard else {}
    if (
        TAG_PATTERN not in first.get("run", "")
        or first.get("env", {}).get("TAG") != "${{ github.ref_name }}"
    ):
        problems.append("guard must validate the release tag before any other step")
    dependencies = package.get("devDependencies", {})
    version = dependencies.get("@napi-rs/cli", "")
    if not EXACT.fullmatch(version):
        problems.append("the Node generator must have an exact version")
    packages = lock.get("packages", {})
    if (
        lock.get("lockfileVersion") != 3
        or packages.get("", {}).get("devDependencies") != dependencies
    ):
        problems.append(
            "the Node lock must match the package's development dependencies"  # pragma: no mutate (diagnostic wording does not change the verdict)
        )
    if not packages.get("node_modules/@napi-rs/cli"):
        problems.append("the Node lock must contain the generator")
    for name, entry in packages.items():
        if name and not (
            entry.get("integrity")
            and entry.get("resolved", "").startswith("https://registry.npmjs.org/")
        ):
            problems.append(
                f"the Node lock needs registry integrity for {name}"  # pragma: no mutate (diagnostic wording does not change the verdict)
            )
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        inputs = []
        for path, parse in (
            (WORKFLOW, yaml.safe_load),
            (PACKAGE, json.loads),
            (LOCK, json.loads),
        ):
            text = (args.root / path).read_text()
            if not text.strip():
                raise ValueError(
                    f"{path} is empty"  # pragma: no mutate (diagnostic wording does not change the verdict)
                )
            try:
                inputs.append(parse(text))
            except (ValueError, yaml.YAMLError) as error:
                raise ValueError(
                    f"{path}: {error}"  # pragma: no mutate (diagnostic wording does not change the verdict)
                ) from error
        if any(not isinstance(value, dict) for value in inputs):
            raise ValueError(
                "release inputs must be mappings"  # pragma: no mutate (diagnostic wording does not change the verdict)
            )
        workflow, package, lock = inputs
        problems = check(workflow, package, lock)
    except (OSError, ValueError, yaml.YAMLError) as error:
        problems = [f"cannot read release build inputs: {error}"]
    if problems:
        print(
            "release builds: FAILED\n  " + "\n  ".join(problems)
        )  # pragma: no mutate (rendering diagnostics does not change the verdict)
        return 1
    print(
        "release builds: fresh dependencies and pinned tools"
    )  # pragma: no mutate (rendering diagnostics does not change the verdict)
    return 0


if __name__ == "__main__":
    sys.exit(main())
