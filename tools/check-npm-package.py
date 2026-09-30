#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
# SPDX-License-Identifier: Apache-2.0
"""Lint the npm package as it would publish: publint and attw over the tarball `npm pack` makes.

check-npm-loader.py holds the generated loader to the package's name, and check-dts-consumer.py
holds the declarations to what a caller types. Neither reads the fields a consumer's resolver
reads from `package.json` (`main`, `types`, `exports`, `files`) against the files the tarball
holds. publint lints those fields against the tarball, and @arethetypeswrong/cli (attw) resolves
the package the way TypeScript does under each module resolution (node10, node16 from CommonJS
and from ESM, bundler) and reports where the types and the JavaScript part ways. Each passes a
broken entry the other fails, measured on 0.10.0's package on 2026-09-30: publint passes an
`exports` whose one condition is `import`, which attw fails from CommonJS, and attw passes a
`types` naming a file the tarball doesn't hold, since TypeScript falls back to the `index.d.ts`
beside `main`, which publint fails.

It needs a built addon (`bindings:js`, or `mise run dts`): `napi build` writes `index.js` and the
`.node` file, which are gitignored.

1. Reads both tools' versions from the package's `devDependencies`, where Dependabot's npm
   entry bumps them. Each must be an exact version: a range would lint with whatever npx
   resolves on the day.
2. Packs the package (`npm pack --json`) into a scratch directory.
3. Installs each tool through npx under the lock `tools/npx_cache.py` holds beside npm's cache
   (#347), and refuses a binary that isn't in npx's tree: `command -v` finds another one on PATH
   when an install left none there (#348).
4. `publint run --strict` over the tarball: an error or a warning fails. A suggestion doesn't.
5. `attw --format json` over the tarball: a problem fails, and so does a report attw passes
   that says nothing. attw exits 0 on a package with no type declarations at all ("This package
   does not contain types"), and on an `exports` with no root entrypoint, since it then has no
   `.` to resolve. So the report must hold types (the floor), and the root entrypoint resolved
   to a declaration file under each of the four resolutions (the sentinel).

Every finding of both tools prints before it exits, so a package that breaks both says so once.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import npx_cache

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "bindings" / "microvms-js"
# Each tool's package and the binary npx puts on PATH.
TOOLS = {"publint": "publint", "@arethetypeswrong/cli": "attw"}
EXACT = re.compile(r"^\d+\.\d+\.\d+$")
# attw's resolutions, each of which must take the root entrypoint to declarations.
RESOLUTIONS = ("node10", "node16-cjs", "node16-esm", "bundler")


class PackageError(Exception):
    """An input couldn't be read, or a tool couldn't run."""


def clean_env() -> dict[str, str]:
    # uv's `--script` environment isn't the caller's, and npx has no use for it
    return {key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"}


def pins(package: Path) -> dict[str, str]:
    try:
        manifest = json.loads((package / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise PackageError(f"{package / 'package.json'}: {error}") from None
    declared = manifest.get("devDependencies") or {}
    out, problems = {}, []
    for name in TOOLS:
        version = declared.get(name)
        if not isinstance(version, str) or not EXACT.match(version):
            problems.append(
                f"{package / 'package.json'} pins {name} at {version!r}, not an exact version "
                "in devDependencies"
            )
        else:
            out[name] = version
    if problems:
        raise PackageError("; ".join(problems))
    return out


def pack(package: Path, dest: Path) -> Path:
    """The tarball `npm pack` writes for `package` into `dest`."""
    done = subprocess.run(
        ["npm", "pack", "--json", "--pack-destination", str(dest)],
        cwd=package,
        capture_output=True,
        text=True,
        env=clean_env(),
        check=False,
    )
    if done.returncode != 0:
        raise PackageError(f"npm pack exited {done.returncode}:\n{done.stderr}")
    try:
        filename = json.loads(done.stdout)[0]["filename"]
    except (ValueError, LookupError, TypeError):
        raise PackageError(f"npm pack printed no tarball:\n{done.stdout}") from None
    tarball = dest / filename
    if not tarball.is_file():
        raise PackageError(f"npm pack named {filename}, and {dest} doesn't hold it")
    return tarball


def locate(name: str, version: str) -> Path:
    """The tool's binary, installed through npx under the npx cache's lock."""
    binary = TOOLS[name]
    with npx_cache.held("npm-package", PackageError) as cache:
        try:
            done = subprocess.run(
                ["npx", "-y", "-p", f"{name}@{version}", "-c", f"command -v {binary}"],
                capture_output=True,
                text=True,
                env=clean_env(),
                cwd=ROOT,
                check=False,
            )
        except FileNotFoundError:
            raise PackageError(
                "npx isn't on PATH (`mise install` provides node)"
            ) from None
    where = done.stdout.strip()
    if done.returncode != 0 or not where:
        raise PackageError(
            f"npx locating {binary} exited {done.returncode}:\n{done.stdout}{done.stderr}"
        )
    found = Path(where)
    npx_dir = cache / "_npx"
    if not found.resolve().is_relative_to(npx_dir.resolve()):
        raise PackageError(
            f"npx located {binary} at {found}, outside npm's npx cache {npx_dir}: `command -v`"
            f" found another {binary} on PATH, not the {name}@{version} npx installs. An install"
            " that lost a race leaves a directory under it with no node_modules/.bin entry;"
            " delete that one and rerun."
        )
    return found


def publint(binary: Path, tarball: Path) -> list[str]:
    done = subprocess.run(
        [str(binary), "run", "--strict", str(tarball)],
        capture_output=True,
        text=True,
        env=clean_env(),
        check=False,
    )
    if done.returncode == 0:
        return []
    return [f"publint exited {done.returncode}:\n{done.stdout}{done.stderr}".rstrip()]


def attw(binary: Path, tarball: Path) -> list[str]:
    done = subprocess.run(
        [str(binary), str(tarball), "--format", "json"],
        capture_output=True,
        text=True,
        env=clean_env(),
        check=False,
    )
    try:
        report = json.loads(done.stdout)
        analysis = report["analysis"]
    except (ValueError, LookupError, TypeError):
        return [
            f"attw exited {done.returncode} with no report:\n{done.stdout}{done.stderr}"
        ]
    if not analysis.get("types"):
        return [
            "attw: the package holds no type declarations, and attw reports that as a pass: "
            "`types` or `files` in package.json lost `index.d.ts`"
        ]
    problems = [
        f"attw: {problem.get('kind')} at `{problem.get('entrypoint')}` under "
        f"{problem.get('resolutionKind')}"
        for found in (report.get("problems") or {}).values()
        for problem in found
    ]
    if done.returncode != 0 and not problems:
        problems.append(f"attw exited {done.returncode} and reported no problem")
    root = (analysis.get("entrypoints") or {}).get(".")
    if root is None:
        problems.append(
            "attw: the root entrypoint `.` isn't in the report, so an import of the package "
            "itself went unchecked (an `exports` without `.`)"
        )
    else:
        resolutions = root.get("resolutions") or {}
        for kind in RESOLUTIONS:
            resolved = (resolutions.get(kind) or {}).get("resolution") or {}
            if not str(resolved.get("fileName", "")).endswith(".d.ts"):
                problems.append(
                    f"attw: `.` under {kind} resolves to {resolved.get('fileName')!r}, not "
                    "to declarations"
                )
    return problems


def check(package: Path) -> tuple[list[str], str]:
    versions = pins(package)
    binaries = {name: locate(name, version) for name, version in versions.items()}
    with tempfile.TemporaryDirectory(prefix="npm-package-") as scratch:
        tarball = pack(package, Path(scratch))
        problems = publint(binaries["publint"], tarball)
        problems += attw(binaries["@arethetypeswrong/cli"], tarball)
    tools = ", ".join(f"{name} {version}" for name, version in versions.items())
    return problems, f"{tarball.name} passes {tools}"


def main() -> int:
    package = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else PACKAGE
    try:
        problems, summary = check(package)
    except PackageError as error:
        print(f"npm-package: {error}", file=sys.stderr)
        return 1
    if problems:
        print("npm-package: FAIL", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"npm-package: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
