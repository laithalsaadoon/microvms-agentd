#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
# SPDX-License-Identifier: Apache-2.0
"""Hold the four surfaces to `verify/parity/capabilities.toml`, the capability table (#271).

Core is the one implementation, and the CLI, Python and TypeScript are thin surfaces over it.
Nothing used to say whether a name on one surface should exist on the others, so a gap couldn't
be told apart from a decision. The table has one row per capability, naming it on each surface
or exempting that surface with a reason. With an `issue`, an exemption is a tracked gap; without
one, it's a permanent decision.

Each surface is read by the tool that already owns it:

- core: `verify/parity/core-api.json`, the committed snapshot `mise run core-api` writes.
- CLI: `docs/manifest.json`, `data.commands[]`, each command's non-positional parameters as its
  flags.
- Python: `bindings/microvms-py/microvms.pyi`, through the site's `griffe_dump.py` and its lockfile.
- TypeScript: `bindings/microvms-js/index.d.ts`, through TypeDoc's JSON, run with npx at the site's
  pins of typedoc, typescript and @types/node (this script holds its pins to
  `site/package.json`). The declarations name `Buffer`, so `@types/node` has to resolve; npx
  puts it in its own cache, and a generated tsconfig that extends the site's names that
  directory as `typeRoots`. Every caller on one host shares npx's cache, so the locate that
  installs runs under a lock beside it, and a second caller waits for the first install
  instead of extracting over it (#347).

The check fails when:

(a) a public name belongs to no row: a module function or value (Python module attributes, TS
    variables, a TS namespace's functions), a method of a class a row or a `[[type]]` names,
    or a CLI command. A `[[type]]`'s accessors (Python properties and their TS twins, which
    napi-rs writes as methods) count as mapped, since the pairing below holds them; its other
    methods need a row like any class's. Classes themselves, TS interfaces, flags, positionals
    and global flags aren't in this rule, so a method of a class no row names isn't either;
    option-level parity is the follow-up issue. `[exempt_names]` lists a name no row should
    hold, with a reason.
(b) a name in a row doesn't exist on its surface: a core path, a CLI command or one of its flags,
    a Python or TypeScript name (`Class`, `Class.member`, or a module-level name).
(c) a row has no cell for a surface, or an exemption has an empty reason.
- an `issue` isn't `#<number>`, so a placeholder can't merge.
- a `[[type]]` member exists on one side only (names pair as `snake_case` equals `camelCase`)
  and its side's `exempt_members` doesn't name it, or an exempt member has a twin or is gone.
- a sentinel row doesn't resolve on all four surfaces. The table must mark `launch`, `health`,
  `kill` and `run-report`; a parser that returns nothing fails them, so an empty parse can't
  pass. A surface with no names at all is reported as well.
- `index.d.ts` carries `@hidden`, `@ignore` or `@private`: TypeDoc leaves such a declaration
  out of what it reports, so the check refuses to read around it.

A cell is a name, a list of names, or `{ exempt = "<reason>", issue = "#N" }`. A CLI name is
`<command>`, `<command> --<flag> ...`, or `@<group>` for a `[flag_groups]` entry, flags every
attached command shares.

    ./tools/check-parity.py                 # read the real surfaces and check the table
    ./tools/check-parity.py --json          # the same, as JSON with every exemption
    ./tools/check-parity.py --pyi F --dts F --manifest F --core-api F --table F

`--json` prints `{"ok", "problems", "exemptions"}`. Each exemption is `{"key", "reason",
"issue"}`, where `key` is `<capability>/<surface>`, `<Type>.<member>/<surface>` for an exempt
member, or `<name>/<surface>` for an exempt name, and `issue` is the number or null.

`--exemptions` prints `{"exemptions"}` alone, the same records, from the table without reading
any surface, and fails on an exemption it can't read. The ratchet's parity-gap category reads
it and counts the records with an issue. It doesn't read the surfaces because holding the table
to them is this check's job, and the ratchet runs in a CI job with no Node for TypeDoc.

    ./tools/check-parity.py --exemptions    # the table's exemptions, for the ratchet
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import npx_cache

ROOT = Path(__file__).resolve().parent.parent
TABLE = ROOT / "verify" / "parity" / "capabilities.toml"
CORE_API = ROOT / "verify" / "parity" / "core-api.json"
MANIFEST = ROOT / "docs" / "manifest.json"
PYI = ROOT / "bindings" / "microvms-py" / "microvms.pyi"
DTS = ROOT / "bindings" / "microvms-js" / "index.d.ts"
GRIFFE_DUMP = ROOT / "site" / "scripts" / "reference" / "griffe_dump.py"
TSCONFIG = ROOT / "site" / "scripts" / "reference" / "sdk" / "typedoc.tsconfig.json"
SITE_PACKAGE = ROOT / "site" / "package.json"

# The site renders the TypeScript reference with these, so the gate reads the declarations the
# way the reference does. `pin_problems` fails when the site moves one and this doesn't follow.
TYPEDOC_PINS = {"typedoc": "0.28.20", "typescript": "5.9.3", "@types/node": "26.4.0"}

SURFACES = ("core", "cli", "py", "ts")
LABELS = {"core": "core", "cli": "CLI", "py": "Python", "ts": "TypeScript"}
SENTINELS = ("launch", "health", "kill", "run-report")
CAPABILITY_KEYS = {"id", "sentinel", *SURFACES}
TYPE_KEYS = {"name", "exempt_members"}
TABLE_KEYS = {"capability", "type", "flag_groups", "exempt_names"}
ISSUE = re.compile(r"#[1-9][0-9]*")

# TypeDoc's ReflectionKind values for what the reader keeps.
TS_CLASS, TS_INTERFACE, TS_FUNCTION, TS_VARIABLE = 128, 256, 64, 32
TS_METHOD, TS_PROPERTY, TS_ACCESSOR = 2048, 1024, 262144
TS_MODULE, TS_NAMESPACE = 2, 4
HIDING_TAG = re.compile(r"(?<![\w.])@(?:hidden|ignore|private)\b")


class ParityError(Exception):
    """A surface couldn't be read at all."""


@dataclass
class ClassInfo:
    methods: set[str] = field(default_factory=set)
    # methods plus properties and accessors: what a `[[type]]` pairs
    members: set[str] = field(default_factory=set)


@dataclass
class Surface:
    # everything a cell may name
    names: set[str] = field(default_factory=set)
    # rule (a)'s module-level names
    functions: set[str] = field(default_factory=set)
    classes: dict[str, ClassInfo] = field(default_factory=dict)
    # CLI only: each command's flags
    flags: dict[str, set[str]] = field(default_factory=dict)


def dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def py_surface(dump: dict[str, Any]) -> Surface:
    """The stub as `griffe_dump.py` prints it. Dunders aren't surface: `__new__` is the
    constructor a row names as the class, and the rest are protocol methods."""
    surface = Surface()
    for item in dump.get("members") or []:
        name = item["name"]
        if dunder(name):
            continue
        surface.names.add(name)
        if item["kind"] == "class":
            info = ClassInfo()
            for member in item.get("members") or []:
                if dunder(member["name"]) or member["kind"] not in (
                    "function",
                    "attribute",
                ):
                    continue
                info.members.add(member["name"])
                if member["kind"] == "function":
                    info.methods.add(member["name"])
            surface.classes[name] = info
            surface.names.update(f"{name}.{member}" for member in info.members)
        elif item["kind"] in ("function", "attribute"):
            surface.functions.add(name)
    return surface


def ts_surface(project: dict[str, Any]) -> Surface:
    """The declarations as TypeDoc's JSON has them. Interfaces, their fields, enums and type
    aliases are names a cell may use, not rule (a) names. A namespace's members are named under
    it (`snapshots.snapshotVm`), since napi-rs writes `#[napi(namespace = ...)]` that way."""
    surface = Surface()
    read_ts_children(project.get("children") or [], "", surface)
    return surface


def read_ts_children(
    items: list[dict[str, Any]], prefix: str, surface: Surface
) -> None:
    for item in items:
        name = prefix + item["name"]
        surface.names.add(name)
        if item["kind"] == TS_CLASS:
            info = ClassInfo()
            for member in item.get("children") or []:
                if member["kind"] not in (TS_METHOD, TS_PROPERTY, TS_ACCESSOR):
                    continue
                info.members.add(member["name"])
                if member["kind"] == TS_METHOD:
                    info.methods.add(member["name"])
            surface.classes[name] = info
            surface.names.update(f"{name}.{member}" for member in info.members)
        elif item["kind"] == TS_INTERFACE:
            # a record type: its fields are names a cell may use (`Image.buildLogGroup`)
            surface.names.update(
                f"{name}.{member['name']}" for member in item.get("children") or []
            )
        elif item["kind"] in (TS_FUNCTION, TS_VARIABLE):
            surface.functions.add(name)
        elif item["kind"] in (TS_NAMESPACE, TS_MODULE):
            read_ts_children(item.get("children") or [], f"{name}.", surface)


def hidden_declarations(text: str) -> list[str]:
    """Lines of the declarations carrying a tag TypeDoc drops a declaration for.

    TypeDoc leaves out a declaration whose JSDoc says `@hidden`, `@ignore` or `@private`
    (measured on 0.28.20; `@internal` and `@protected` stay). napi-rs copies a Rust doc comment
    into the JSDoc, so such a tag would take a callable name off the surface this check reads,
    and a TS-only function would pass as if it weren't there. The check refuses the file instead.
    """
    return [
        f"line {number}: {match.group(0)}"
        for number, line in enumerate(text.splitlines(), 1)
        for match in HIDING_TAG.finditer(line)
    ]


def cli_surface(manifest: dict[str, Any]) -> Surface:
    """`microvm manifest`'s envelope. Positionals are arguments, not flags."""
    surface = Surface()
    for command in (manifest.get("data") or {}).get("commands") or []:
        surface.names.add(command["name"])
        surface.flags[command["name"]] = {
            parameter["name"]
            for parameter in command.get("parameters") or []
            if not parameter.get("positional")
        }
    return surface


def core_surface(snapshot: dict[str, Any]) -> Surface:
    return Surface(names=set(snapshot.get("paths") or {}))


def parse_table(text: str) -> dict[str, Any]:
    return tomllib.loads(text)


def names_of(cell: Any) -> list[str] | None:
    """The names a cell holds, or `None` for an exemption or a malformed cell."""
    if isinstance(cell, str):
        return [cell]
    if isinstance(cell, list) and cell and all(isinstance(name, str) for name in cell):
        return cell
    return None


def exemption_problems(where: str, cell: Any) -> list[str]:
    """What's wrong with an exemption: `{ exempt = "<reason>" }`, plus an optional issue."""
    if isinstance(cell, str):
        cell = {"exempt": cell}
    if not isinstance(cell, dict):
        return [f"{where}: a cell is a name, a list of names, or {{ exempt = ... }}"]
    problems = [
        f"{where}: unknown key {key}" for key in sorted(set(cell) - {"exempt", "issue"})
    ]
    reason = cell.get("exempt")
    if not isinstance(reason, str) or not reason.strip():
        problems.append(f"{where}: the exemption has an empty reason")
    if "issue" in cell and not (
        isinstance(cell["issue"], str) and ISSUE.fullmatch(cell["issue"])
    ):
        problems.append(f"{where}: issue {cell['issue']!r} isn't of the form #<number>")
    return problems


def resolves(
    surface_key: str, name: str, surface: Surface, groups: dict[str, Any]
) -> str | None:
    """Why `name` doesn't exist on the surface, or `None` when it does."""
    if surface_key != "cli":
        return None if name in surface.names else f"{name} isn't on the surface"
    if name.startswith("@"):
        return None if name[1:] in groups else f"{name} names no flag group"
    command, *flags = name.split()
    if command not in surface.flags or any(
        not flag.startswith("--") or flag[2:] not in surface.flags[command]
        for flag in flags
    ):
        return f"{name} isn't on the surface"
    return None


def normalize(name: str) -> str:
    return name.replace("_", "").lower()


def check(table: dict[str, Any], surfaces: dict[str, Surface]) -> list[str]:
    problems = [f"table: unknown key {key}" for key in sorted(set(table) - TABLE_KEYS)]
    groups = table.get("flag_groups") or {}
    rows = table.get("capability") or []
    types = {row.get("name"): row for row in table.get("type") or []}
    exempt_names = table.get("exempt_names") or {}

    # An empty surface is reported once, with its sentinels; every other row naming it would
    # say the same thing again and bury the cause.
    empty = {key for key, surface in surfaces.items() if not surface.names}
    problems += [
        f"{key}: the parser returned no members" for key in SURFACES if key in empty
    ]

    known = set().union(*surfaces["cli"].flags.values())
    for group, flags in groups.items():
        for flag in flags:
            if "cli" not in empty and flag not in known:
                problems.append(f"flag_groups: {group}: {flag} is no command's flag")

    # rule (a) needs what the rows map; collect it while checking (b) and (c)
    mapped: dict[str, set[str]] = {key: set() for key in SURFACES}
    seen: set[str] = set()
    for row in rows:
        row_id = row.get("id", "<no id>")
        if row_id in seen:
            problems.append(f"{row_id}: the id is used by more than one row")
        seen.add(row_id)
        problems += [
            f"{row_id}: unknown key {key}" for key in sorted(set(row) - CAPABILITY_KEYS)
        ]
        sentinel = row.get("sentinel") is True
        for key in SURFACES:
            where = f"{row_id}: {key}"
            if key not in row:
                problems.append(f"{where}: no cell; name the surface or exempt it")
                continue
            names = names_of(row[key])
            if names is None:
                problems += exemption_problems(where, row[key])
                if sentinel:
                    problems.append(
                        f"sentinel {row_id}: {key}: a sentinel can't be exempt"
                    )
                continue
            for name in names:
                mapped[key].add(name.split()[0] if key == "cli" else name)
                if key in empty and not sentinel:
                    continue
                why = resolves(key, name, surfaces[key], groups)
                if why:
                    problems.append(
                        f"sentinel {row_id}: {key}: {why}"
                        if sentinel
                        else f"{where}: {why}"
                    )

    for sentinel in SENTINELS:
        if not any(
            row.get("id") == sentinel and row.get("sentinel") is True for row in rows
        ):
            problems.append(f"the table marks no sentinel row {sentinel}")
    if not rows:
        # every public name would follow, each belonging to no row
        return [*problems, "table: no [[capability]] rows"]

    for name, row in types.items():
        if not empty & {"py", "ts"}:
            problems += type_problems(name, row, surfaces)

    exempt: dict[str, set[str]] = {}
    for key, entries in exempt_names.items():
        if key not in SURFACES or not isinstance(entries, dict):
            problems.append(f"exempt_names: {key} isn't a surface's table of names")
            continue
        exempt[key] = set(entries)
        for name, cell in entries.items():
            where = f"exempt_names: {key}: {name}"
            problems += exemption_problems(where, cell)
            if name in mapped[key]:
                problems.append(
                    f"exempt_names: {key}: {name} is exempt and also in a row"
                )
            elif key not in empty and resolves(key, name, surfaces[key], groups):
                problems.append(f"{where} is exempt but isn't on the surface")

    for key in ("py", "ts"):
        surface = surfaces[key]
        covered = mapped[key] | exempt.get(key, set())
        for name in sorted(surface.functions - covered):
            problems.append(f"{key}: {name} belongs to no row")
        named = {name.split(".")[0] for name in mapped[key]} | set(types)
        for cls in sorted(named & set(surface.classes)):
            for method in sorted(held_methods(cls, key, surfaces, types)):
                if f"{cls}.{method}" not in covered:
                    problems.append(f"{key}: {cls}.{method} belongs to no row")
    cli = surfaces["cli"]
    for command in sorted(cli.names - mapped["cli"] - exempt.get("cli", set())):
        problems.append(f"cli: {command} belongs to no row")
    return problems


def held_methods(
    cls: str, key: str, surfaces: dict[str, Surface], types: dict[str, Any]
) -> set[str]:
    """The methods of `cls` on surface `key` that rule (a) holds to a row.

    Every method, except on the TypeScript side of a `[[type]]`: napi-rs writes a Rust getter
    as a method, so TypeDoc can't tell `agentToken()` from `snapshot()`. There a method is held
    only when its Python twin is a method too; a twin that's a Python property marks an
    accessor, which the pairing holds. A TS method with no twin fails the pairing anyway.
    """
    methods = surfaces[key].classes[cls].methods
    if key != "ts" or cls not in types:
        return methods
    twin = surfaces["py"].classes.get(cls)
    python_methods = {normalize(name) for name in twin.methods} if twin else set()
    return {name for name in methods if normalize(name) in python_methods}


def type_problems(
    name: str, row: dict[str, Any], surfaces: dict[str, Surface]
) -> list[str]:
    """A `[[type]]`'s members pair across Python and TypeScript by normalized name."""
    where = f"type {name}"
    problems = [f"{where}: unknown key {key}" for key in sorted(set(row) - TYPE_KEYS)]
    sides = {key: surfaces[key].classes.get(name) for key in ("py", "ts")}
    for key, info in sides.items():
        if info is None:
            problems.append(f"{where}: {key}: no class {name}")
    if None in sides.values():
        return problems
    exempt = row.get("exempt_members") or {}
    for key in sorted(set(exempt) - {"py", "ts"}):
        problems.append(f"{where}: exempt_members.{key} isn't py or ts")
    for key, other in (("py", "ts"), ("ts", "py")):
        mine = sides[key].members
        theirs = {normalize(member) for member in sides[other].members}
        side_exempt = exempt.get(key) or {}
        for member in sorted(mine):
            if normalize(member) not in theirs and member not in side_exempt:
                problems.append(
                    f"{where}: {key}: {member} has no {LABELS[other]} twin and no exemption"
                )
        for member, cell in side_exempt.items():
            problems += exemption_problems(f"{where}: {key}: {member}", cell)
            if member not in mine:
                problems.append(
                    f"{where}: {key}: {member} is exempt but isn't on the surface"
                )
            elif normalize(member) in theirs:
                problems.append(
                    f"{where}: {key}: {member} is exempt but has a {LABELS[other]} twin"
                )
    return problems


def record(key: str, cell: Any) -> dict[str, Any]:
    if isinstance(cell, str):
        cell = {"exempt": cell}
    issue = cell.get("issue")
    number = (
        int(issue[1:]) if isinstance(issue, str) and ISSUE.fullmatch(issue) else None
    )
    return {"key": key, "reason": cell.get("exempt"), "issue": number}


def exemption_cells(table: dict[str, Any]) -> list[tuple[str, Any]]:
    """Every exemption in the table as its record key and its cell, in the table's order."""
    cells = []
    for row in table.get("capability") or []:
        for key in SURFACES:
            if key in row and names_of(row[key]) is None:
                cells.append((f"{row.get('id')}/{key}", row[key]))
    for row in table.get("type") or []:
        for key, members in (row.get("exempt_members") or {}).items():
            for member, cell in members.items():
                cells.append((f"{row.get('name')}.{member}/{key}", cell))
    for key, entries in (table.get("exempt_names") or {}).items():
        for name, cell in entries.items():
            cells.append((f"{name}/{key}", cell))
    return cells


def exemptions(table: dict[str, Any]) -> list[dict[str, Any]]:
    """Every exemption in the table, one record each, in the table's order."""
    return [record(key, cell) for key, cell in exemption_cells(table)]


def pin_problems(package: Path = SITE_PACKAGE) -> list[str]:
    site = json.loads(package.read_text(encoding="utf-8")).get("devDependencies") or {}
    return [
        f"pins: {name} is {version} here and {site.get(name)} in site/package.json"
        for name, version in TYPEDOC_PINS.items()
        if site.get(name) != version
    ]


def clean_env() -> dict[str, str]:
    # uv otherwise reports, and in some subcommands uses, whatever venv the caller has active
    return {key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"}


def run(argv: list[str], what: str) -> str:
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, env=clean_env(), cwd=ROOT, check=False
        )
    except FileNotFoundError as error:
        raise ParityError(
            f"{what}: {argv[0]} isn't on PATH (`mise install` provides it)"
        ) from error
    if done.returncode != 0:
        raise ParityError(
            f"{what} exited {done.returncode}:\n{done.stdout}{done.stderr}"
        )
    return done.stdout


def run_griffe(pyi: Path) -> dict[str, Any]:
    out = run(
        ["uv", "run", "--quiet", "--locked", "--script", str(GRIFFE_DUMP), str(pyi)],
        f"griffe_dump.py over {pyi}",
    )
    return json.loads(out)


def npx() -> list[str]:
    # The three packages are pinned exactly, but npx resolves their own dependencies without
    # the site's lockfile. Reading site/node_modules instead would make `check` depend on
    # `docs:install`. `dts:check` runs `@napi-rs/cli` through npx the same way.
    argv = ["npx", "-y"]
    for name, version in TYPEDOC_PINS.items():
        argv += ["-p", f"{name}@{version}"]
    return argv


def npx_lock() -> contextlib.AbstractContextManager[Path]:
    # `npx_cache.py` has the lock and its reasons (#347). check-dts-consumer.py's tsc install
    # takes the same one.
    return npx_cache.held("parity", ParityError)


def run_typedoc(dts: Path, scratch: Path) -> dict[str, Any]:
    hidden = hidden_declarations(dts.read_text(encoding="utf-8"))
    if hidden:
        raise ParityError(
            f"{dts} has JSDoc tags TypeDoc drops a declaration for, so this check can't see"
            f" what they hide: {'; '.join(hidden)}"
        )
    # the locate installs; once it returns the tree is whole, so TypeDoc runs stay parallel
    with npx_lock():
        where = run(
            [*npx(), "-c", "command -v typedoc"], "npx locating typedoc"
        ).strip()
    # <npx cache>/node_modules/.bin/typedoc, so @types sits beside .bin
    type_roots = Path(where).parent.parent / "@types"
    tsconfig = scratch / "tsconfig.json"
    tsconfig.write_text(
        json.dumps(
            {
                "extends": str(TSCONFIG),
                "compilerOptions": {"typeRoots": [str(type_roots)]},
                "files": [str(dts.resolve())],
            }
        ),
        encoding="utf-8",
    )
    out = scratch / "typedoc.json"
    run(
        [
            *npx(),
            "typedoc",
            "--json",
            str(out),
            "--tsconfig",
            str(tsconfig),
            "--entryPoints",
            str(dts.resolve()),
        ],
        f"typedoc over {dts}",
    )
    return json.loads(out.read_text(encoding="utf-8"))


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ParityError(f"{path} doesn't exist") from error


def print_exemptions(table: dict[str, Any]) -> int:
    # The ratchet's parity-gap collector reads this. It refuses a cell it can't read rather than
    # printing a record for it: an issue that isn't `#N` would print as null, and the ratchet
    # would count a tracked gap as a decision.
    problems = [
        problem
        for key, cell in exemption_cells(table)
        for problem in exemption_problems(": ".join(key.rsplit("/", 1)), cell)
    ]
    if problems:
        print("parity: the table's exemptions can't be read:")
        for problem in problems:
            print(f"  {problem}")
        return 1
    json.dump({"exemptions": exemptions(table)}, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--table", type=Path, default=TABLE)
    parser.add_argument("--core-api", type=Path, default=CORE_API)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--pyi", type=Path, default=PYI)
    parser.add_argument("--dts", type=Path, default=DTS)
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    parser.add_argument(
        "--exemptions",
        action="store_true",
        help="print the table's exemption records as JSON, reading no surface",
    )
    args = parser.parse_args()
    try:
        try:
            table = parse_table(args.table.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as error:
            raise ParityError(f"{args.table}: {error}") from error
        if args.exemptions:
            return print_exemptions(table)
        with tempfile.TemporaryDirectory(prefix="parity-") as scratch:
            surfaces = {
                "core": core_surface(read_json(args.core_api)),
                "cli": cli_surface(read_json(args.manifest)),
                "py": py_surface(run_griffe(args.pyi)),
                "ts": ts_surface(run_typedoc(args.dts, Path(scratch))),
            }
    except (ParityError, OSError, json.JSONDecodeError) as error:
        print(f"parity: {error}")
        return 1
    problems = pin_problems() + check(table, surfaces)
    if args.json:
        json.dump(
            {"ok": not problems, "problems": problems, "exemptions": exemptions(table)},
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
    elif problems:
        shown = args.table.resolve()
        shown = shown.relative_to(ROOT) if shown.is_relative_to(ROOT) else shown
        print(f"parity: {shown} doesn't match the surfaces:")
        for problem in problems:
            print(f"  {problem}")
    else:
        print(
            "parity: every row resolves on its surfaces, and every name rule (a) holds has a row"
        )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
