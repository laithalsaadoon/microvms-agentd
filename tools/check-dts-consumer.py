#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
# SPDX-License-Identifier: Apache-2.0
"""Compile the TypeScript type probes against `bindings/microvms-js/index.d.ts`, as a caller would (#262).

`dts:check` proves the committed declarations match what `napi build` writes. It can't tell
whether what `napi build` writes lets a caller type the code the docs show: `index.d.ts` once
declared `ExecStream` as an empty class, byte-identical to the generator's output, and `tsc`
rejected the `for await (const event of handle.stream())` loop that `stream()`'s own doc
recommends (TS2504). The runtime tests are `.mjs`, so nothing type-checked them.

Each `*.ts` file under `--probes` imports `@theagenticguy/microvms` by its package name, the
way a caller does. A generated tsconfig maps that name to `--dts` through `compilerOptions.paths`
and lists only the probes in `files`, so the declarations enter the program only through that
import, and the file `--dts` names is the one checked. `skipLibCheck` is off, unlike the
docs site's TypeDoc config, so the declaration file itself is checked too.

TypeScript and `@types/node` run through npx at the versions `site/package.json` pins, the pins
`tools/check-parity.py` holds to the same file, so the version lives in one place. npx installs
them into npm's cache, which every caller on the host shares, so the locate that installs runs
under the lock `tools/npx_cache.py` holds beside it, the one check-parity.py's TypeDoc install
takes (#348).

POSIX only, like `check-parity.py`'s TypeDoc run: npx runs `command -v tsc` in a POSIX shell, and
the `--listFiles` parse reads absolute paths as starting with `/`. CI runs it on ubuntu.

The check fails when:

- there's no probe under `--probes`, or a file there isn't one: a probe is a `*.ts` file directly
  under the directory, and anything else (a `.mts`, a subdirectory) would be skipped with no
  word, so it's refused instead;
- a probe has no `@ts-expect-error` directive, a comment that starts with it, which is the only
  form `tsc` honors. The directive is each probe's control: a declaration that types a value as
  `any` makes every use of it pass, and the directive then goes unused and `tsc` reports TS2578,
  so "it compiles" can't mean "it checked nothing". A mention in prose isn't a control;
- a probe says `@ts-nocheck` anywhere, directive or prose. The pragma drops every diagnostic in
  the file, the control's TS2578 included, so one line would turn the gate off. Refusing any
  mention is stricter than `tsc`'s rule on purpose: the false red it can cost names itself;
- npx locates a `tsc` outside npm's `<cache>/_npx/`: `command -v` found another one on PATH,
  which it does when an install left none in npx's tree (#347's racing callers printed mise's
  tsc), so the pinned compiler isn't the one that would run;
- `tsc` exits non-zero (its diagnostics print as they are);
- `tsc --listFiles` doesn't list the declarations and every probe, so a probe that silently
  resolved the package name somewhere else, or a listing this script can't read, fails by name.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import npx_cache

ROOT = Path(__file__).resolve().parent.parent
DTS = ROOT / "bindings" / "microvms-js" / "index.d.ts"
PROBES = ROOT / "bindings" / "microvms-js" / "__test__" / "types"
JS_PACKAGE = ROOT / "bindings" / "microvms-js" / "package.json"
SITE_PACKAGE = ROOT / "site" / "package.json"
PINNED = ("typescript", "@types/node")
CONTROL = "@ts-expect-error"
# tsc's own rule: a `//`, `///`, `/*` or `/**` comment whose text starts with the directive
DIRECTIVE = re.compile(r"^\s*(?:///?|/\*+)\s*@ts-expect-error\b", re.MULTILINE)
NOCHECK = "@ts-nocheck"


class ConsumerError(Exception):
    """An input couldn't be read, or the check failed."""


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ConsumerError(f"{path} doesn't exist") from error


def pins(package: Path = SITE_PACKAGE) -> dict[str, str]:
    site = read_json(package).get("devDependencies") or {}
    missing = [name for name in PINNED if not site.get(name)]
    if missing:
        raise ConsumerError(
            f"{package} pins no {' or '.join(missing)} in devDependencies"
        )
    return {name: site[name] for name in PINNED}


def package_name(package: Path = JS_PACKAGE) -> str:
    name = read_json(package).get("name")
    if not name:
        raise ConsumerError(
            f"{package} has no `name`, so a probe has no package to import"
        )
    return name


def probes(directory: Path) -> list[Path]:
    found = sorted(path.resolve() for path in directory.glob("*.ts"))
    stray = sorted(
        path.resolve()
        for path in directory.rglob("*")
        if path.resolve() not in found and not path.is_dir()
    )
    if stray:
        raise ConsumerError(
            "; ".join(
                f"{path} isn't compiled: a probe is a *.ts file directly under {directory}"
                for path in stray
            )
        )
    if not found:
        raise ConsumerError(f"no type probes under {directory}")
    problems = []
    for path in found:
        text = path.read_text(encoding="utf-8")
        if NOCHECK in text:
            problems.append(f"{path} turns type-checking off ({NOCHECK})")
        if not DIRECTIVE.search(text):
            problems.append(f"{path} has no {CONTROL} control")
    if problems:
        raise ConsumerError("; ".join(problems))
    return found


def clean_env() -> dict[str, str]:
    # uv otherwise reports, and in some subcommands uses, whatever venv the caller has active
    return {key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"}


def locate_tsc(versions: dict[str, str]) -> Path:
    argv = ["npx", "-y"]
    for name, version in versions.items():
        argv += ["-p", f"{name}@{version}"]
    # the locate installs; once it returns the tree is whole, so tsc runs unlocked
    with npx_cache.held("dts-consumer", ConsumerError) as cache:
        try:
            done = subprocess.run(
                [*argv, "-c", "command -v tsc"],
                capture_output=True,
                text=True,
                env=clean_env(),
                cwd=ROOT,
                check=False,
            )
        except FileNotFoundError as error:
            raise ConsumerError(
                "npx isn't on PATH (`mise install` provides node)"
            ) from error
    where = done.stdout.strip()
    if done.returncode != 0 or not where:
        raise ConsumerError(
            f"npx locating tsc exited {done.returncode}:\n{done.stdout}{done.stderr}"
        )
    tsc = Path(where)
    # npx puts the set's .bin first on PATH, and `command -v` falls through to any other tsc
    # when that tree has none. Measured for #347: racing callers exited 0 printing mise's
    # global tsc. That compiler isn't the pinned one, and the typeRoots derived from its path
    # would name another tree, so it fails here by name rather than in tsc.
    npx_dir = cache / "_npx"
    if not tsc.resolve().is_relative_to(npx_dir.resolve()):
        raise ConsumerError(
            f"npx located tsc at {tsc}, outside npm's npx cache {npx_dir}: `command -v`"
            f" found another tsc on PATH, not the typescript@{versions['typescript']} npx"
            " installs. An install that lost a race leaves a directory under it with no"
            " node_modules/.bin/tsc; delete that one and rerun."
        )
    return tsc


def listed(output: str) -> set[Path]:
    """The files `tsc --listFiles` printed: one absolute path per line."""
    return {
        Path(line.strip())
        for line in output.splitlines()
        if line.strip().startswith("/") and line.strip().endswith(".ts")
    }


def check(dts: Path, directory: Path) -> str:
    versions = pins()
    name = package_name()
    found = probes(directory)
    tsc = locate_tsc(versions)
    # <npx cache>/node_modules/.bin/tsc, so @types sits beside .bin
    type_roots = tsc.parent.parent / "@types"
    with tempfile.TemporaryDirectory(prefix="dts-consumer-") as scratch:
        tsconfig = Path(scratch) / "tsconfig.json"
        tsconfig.write_text(
            json.dumps(
                {
                    "compilerOptions": {
                        "strict": True,
                        "noEmit": True,
                        "target": "ES2022",
                        "lib": ["ES2022"],
                        "module": "NodeNext",
                        "types": ["node"],
                        "typeRoots": [str(type_roots)],
                        "skipLibCheck": False,
                        "paths": {name: [str(dts)]},
                    },
                    "files": [str(path) for path in found],
                }
            ),
            encoding="utf-8",
        )
        # cwd at the root, so diagnostics print as `bindings/microvms-js/__test__/types/<probe>.ts(l,c)`
        done = subprocess.run(
            [str(tsc), "-p", str(tsconfig), "--listFiles"],
            capture_output=True,
            text=True,
            env=clean_env(),
            cwd=ROOT,
            check=False,
        )
    output = done.stdout + done.stderr
    read = listed(output)
    problems = []
    if done.returncode != 0:
        diagnostics = [
            line for line in output.splitlines() if Path(line.strip()) not in read
        ]
        problems.append(
            "\n".join(diagnostics)
            + f"\ntsc {versions['typescript']} exited {done.returncode}: the TypeScript"
            " declarations don't type-check a consumer"
        )
    problems += [
        f"tsc didn't read {path}" for path in [dts, *found] if path not in read
    ]
    if problems:
        raise ConsumerError("\n".join(problems))
    shown = ", ".join(
        str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)
        for path in found
    )
    return f"{shown} type-check against {dts.name} with tsc {versions['typescript']}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dts", type=Path, default=DTS)
    parser.add_argument("--probes", type=Path, default=PROBES)
    args = parser.parse_args()
    try:
        summary = check(args.dts.resolve(), args.probes)
    except (ConsumerError, OSError, json.JSONDecodeError) as error:
        print(f"dts-consumer: {error}")
        return 1
    print(f"dts-consumer: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
