# SPDX-License-Identifier: Apache-2.0
"""Collect and hold every public input and type, including non-callable declarations.

The capability table maps operations. This inventory maps their input contracts and the
types that carry them. The reviewed manifest records each surface's shape as well as its
cross-surface contract: an optional wrapper, positional argument, and required Rust input
can implement one contract without having identical syntax. A new or changed declaration
requires an explicit manifest decision; generating observations never enrolls them.
"""

from __future__ import annotations

from typing import Any


class InventoryError(ValueError):
    """A declaration form is not understood, so no complete inventory can be claimed."""


Inventory = dict[str, dict[str, Any]]


def put(out: Inventory, category: str, name: str, shape: dict) -> None:
    key = f"{category}:{name}"
    if key in out and out[key] != shape:
        raise InventoryError(f"conflicting declarations for {key}")
    out[key] = shape


def core(snapshot: dict) -> Inventory:
    out: Inventory = {}
    declarations = snapshot.get("declarations")
    if not isinstance(declarations, dict) or not declarations:
        raise InventoryError("core snapshot has no declarations; regenerate core-api")
    for name, declaration in declarations.items():
        if "parameters" in declaration:
            put(out, "callable", name, declaration)
            for parameter, shape in declaration["parameters"].items():
                put(
                    out,
                    "option",
                    f"{name}({parameter})",
                    {
                        **shape,
                        "kind": "argument",
                        "origin": f"{declaration.get('origin')}({parameter})",
                    },
                )
        elif declaration.get("kind") in ("constant", "static", "assoc_const"):
            put(out, "value", name, declaration)
        else:
            put(out, "type", name, declaration)
            for field, type_ in declaration.get("fields", {}).items():
                put(
                    out,
                    "option",
                    f"{name}.{field}",
                    {
                        "type": type_,
                        "kind": "field",
                        "origin": f"{declaration.get('origin')}.{field}",
                    },
                )
    return out


def cli(manifest: dict) -> Inventory:
    out: Inventory = {}
    data = manifest.get("data", {})

    def parameter(owner, item, position=None):
        expected = {
            "name",
            "type",
            "required",
            "positional",
            "choices",
            "default",
            "help",
            "index",
            "short",
            "aliases",
            "shortAliases",
            "action",
            "arity",
            "valueDelimiter",
            "valueTerminator",
        }
        if (
            set(item) - expected
            or not {"name", "type", "required", "positional"} <= item.keys()
        ):
            raise InventoryError(f"unknown CLI parameter shape for {owner}: {item}")
        name = (
            f"{owner} <{item['name']}>"
            if item["positional"]
            else f"{owner} --{item['name']}"
        )
        put(
            out,
            "option",
            name,
            {
                "kind": "positional" if item["positional"] else "flag",
                "type": item["type"],
                "required": item["required"],
                "choices": item.get("choices"),
                "position": position,
                **{
                    key: item.get(key)
                    for key in (
                        "index",
                        "short",
                        "aliases",
                        "shortAliases",
                        "action",
                        "arity",
                        "valueDelimiter",
                        "valueTerminator",
                    )
                },
            },
        )

    for command in data.get("commands", []):
        position = 0
        for item in command.get("parameters", []):
            if item["positional"]:
                position += 1
            parameter(command["name"], item, position if item["positional"] else None)
    for item in data.get("globalFlags", []):
        parameter("*", item)
    return out


def python(dump: dict) -> Inventory:
    out: Inventory = {}

    def parameters(item):
        return [
            {
                "name": parameter["name"],
                "kind": parameter.get("kind"),
                "type": parameter.get("annotation"),
                "required": parameter.get("default") is None,
            }
            for parameter in item.get("parameters", [])
        ]

    def function(owner, item):
        put(
            out,
            "callable",
            owner,
            {
                "returns": item.get("returns"),
                "decorators": item.get("decorators", []),
                "labels": item.get("labels", []),
                "overloads": [
                    {
                        "parameters": parameters(overload),
                        "returns": overload.get("returns"),
                    }
                    for overload in item.get("overloads", [])
                ],
                "parameters": parameters(item),
            },
        )
        signatures = [item, *item.get("overloads", [])]
        for index, signature in enumerate(signatures):
            suffix = f"#{index + 1}" if len(signatures) > 1 else ""
            for position, parameter in enumerate(signature.get("parameters", [])):
                if parameter["name"] in ("self", "cls"):
                    continue
                kind = parameter.get("kind")
                if kind not in (
                    "positional-only",
                    "positional or keyword",
                    "keyword-only",
                    "variadic positional",
                    "variadic keyword",
                ):
                    raise InventoryError(
                        f"unknown Python parameter kind {kind!r}: {owner}"
                    )
                put(
                    out,
                    "option",
                    f"{owner}{suffix}({parameter['name']})",
                    {
                        "kind": kind,
                        "type": parameter.get("annotation"),
                        "position": position,
                        "required": parameter.get("default") is None
                        and not kind.startswith("variadic"),
                    },
                )

    def members(items, prefix=""):
        for item in items:
            name = prefix + item["name"]
            kind = item["kind"]
            if kind == "class":
                children = item.get("members", [])
                put(
                    out,
                    "type",
                    name,
                    {
                        "kind": "class",
                        "bases": item.get("bases", []),
                        "decorators": item.get("decorators", []),
                        "members": {
                            child["name"]: {
                                "kind": child["kind"],
                                "type": child.get("annotation"),
                                "labels": child.get("labels", []),
                            }
                            for child in children
                        },
                    },
                )
                members(children, name + ".")
            elif kind == "function":
                function(name, item)
            elif kind == "attribute":
                annotation = item.get("annotation") or ""
                if "TypeAlias" in annotation:
                    put(out, "type", name, {"kind": "alias", "type": item.get("value")})
                elif prefix:
                    put(
                        out,
                        "option",
                        name,
                        {
                            "kind": "field",
                            "type": annotation,
                            "labels": item.get("labels", []),
                        },
                    )
                elif not prefix and not item["name"].startswith("__"):
                    put(
                        out,
                        "value",
                        name,
                        {"type": annotation, "value": item.get("value")},
                    )
            else:
                raise InventoryError(
                    f"unknown Python declaration kind {kind!r}: {name}"
                )

    members(dump.get("members", []))
    for alias in dump.get("aliases", []):
        put(out, "type", alias["name"], {"kind": "alias", "type": alias["target"]})
    return out


# TypeDoc 0.28's public ReflectionKind values. Fail on a new form instead of leaving it out.
TS_KINDS = {
    2: "module",
    4: "namespace",
    8: "enum",
    16: "enum member",
    32: "variable",
    64: "function",
    128: "class",
    256: "interface",
    512: "constructor",
    1024: "property",
    2048: "method",
    262144: "accessor",
    2097152: "alias",
}


def typescript(project: dict) -> Inventory:
    out: Inventory = {}
    references = {}

    def index(items, prefix=""):
        for item in items:
            name = prefix + item["name"]
            references[item["id"]] = name
            index(item.get("children", []), name + ".")

    index(project.get("children", []))

    def stable(value):
        if isinstance(value, list):
            return [stable(child) for child in value]
        if isinstance(value, dict):
            # References already carry their written name; IDs, source links and comments
            # change independently of the input contract.
            result = {
                key: stable(child)
                for key, child in value.items()
                if key
                not in ("id", "target", "comment", "sources", "groups", "categories")
            }
            if "target" in value:
                target = value["target"]
                if isinstance(target, int):
                    if target not in references:
                        raise InventoryError(
                            f"unresolved TypeScript reference {target}"
                        )
                    result["target"] = references[target]
                else:
                    result["target"] = stable(target)
            return result
        return value

    def signatures(owner, item):
        signatures_ = item.get("signatures", []) or [
            item[key] for key in ("getSignature", "setSignature") if key in item
        ]
        put(
            out,
            "callable",
            owner,
            {"signatures": stable(signatures_), "flags": stable(item.get("flags", {}))},
        )
        for index, signature in enumerate(signatures_):
            suffix = f"#{index + 1}" if len(signatures_) > 1 else ""
            for position, parameter in enumerate(signature.get("parameters", [])):
                put(
                    out,
                    "option",
                    f"{owner}{suffix}({parameter['name']})",
                    {
                        "kind": "rest"
                        if parameter.get("flags", {}).get("isRest")
                        else "argument",
                        "type": stable(parameter.get("type")),
                        "required": not parameter.get("flags", {}).get(
                            "isOptional", False
                        ),
                        "position": position,
                    },
                )

    def children(items, prefix=""):
        for item in items:
            name = prefix + item["name"]
            kind = TS_KINDS.get(item["kind"])
            if kind is None:
                raise InventoryError(
                    f"unknown TypeScript declaration kind {item['kind']}: {name}"
                )
            if kind in ("module", "namespace"):
                children(item.get("children", []), name + ".")
            elif kind in ("class", "interface", "enum", "alias"):
                put(
                    out,
                    "type",
                    name,
                    {
                        "kind": kind,
                        "type": stable(item.get("type")),
                        "parameters": stable(item.get("typeParameters", [])),
                        "extends": stable(item.get("extendedTypes", [])),
                        "implements": stable(item.get("implementedTypes", [])),
                        "flags": stable(item.get("flags", {})),
                        "members": {
                            member["name"]: {
                                "kind": TS_KINDS.get(member["kind"], member["kind"]),
                                "type": stable(member.get("type")),
                                "optional": member.get("flags", {}).get(
                                    "isOptional", False
                                ),
                                "value": member.get("defaultValue"),
                                "flags": stable(member.get("flags", {})),
                            }
                            for member in item.get("children", [])
                        },
                    },
                )
                children(item.get("children", []), name + ".")
                if "signatures" in item:
                    signatures(name, item)
            elif kind in ("function", "method", "constructor", "accessor"):
                signatures(prefix.rstrip(".") if kind == "constructor" else name, item)
            elif kind == "property":
                put(
                    out,
                    "option",
                    name,
                    {
                        "kind": "field",
                        "type": stable(item.get("type")),
                        "required": not item.get("flags", {}).get("isOptional", False),
                        "flags": stable(item.get("flags", {})),
                    },
                )
            elif kind == "variable":
                put(out, "value", name, {"type": stable(item.get("type"))})
                if "signatures" in item:
                    signatures(name, item)
            # Accessors and enum members are output members, already in their public type.

    children(project.get("children", []))
    for name, declarations in project.get("globalDeclarations", {}).items():
        put(out, "type", name, {"kind": "ambient", "declarations": declarations})
    return out


def problems(
    manifest: dict, observed: dict[str, Inventory], capabilities: set[str]
) -> list[str]:
    """Require an exact shape and one reviewed cross-surface decision for every item."""
    errors = []
    surfaces = ("core", "cli", "py", "ts")
    if manifest.get("schema") != 1:
        return ["inventory: unsupported or missing schema"]
    if set(manifest) != {"schema", "shapes", "contracts", "exemptions"}:
        errors.append("inventory: unknown or missing top-level keys")
    shapes = manifest.get("shapes", {})
    decisions = manifest.get("exemptions", {})
    used_decisions = set()
    claimed = {surface: set() for surface in surfaces}
    seen = set()
    if not manifest.get("contracts"):
        errors.append("inventory: no contract rows")
    for row in manifest.get("contracts", []):
        identifier = row.get("id")
        where = f"inventory {identifier}"
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            errors.append(f"{where}: missing or repeated id")
        seen.add(identifier)
        category = row.get("kind")
        if category not in ("type", "option", "callable", "value"):
            errors.append(f"{where}: unknown kind")
        if set(row) - {"id", "kind", "capability", *surfaces}:
            errors.append(f"{where}: unknown row keys")
        if "capability" in row and row["capability"] not in capabilities:
            errors.append(f"{where}: unknown capability {row['capability']}")
        named = 0
        for surface in surfaces:
            cell = row.get(surface)
            if isinstance(cell, dict) and set(cell) == {"exempt"}:
                decision = cell["exempt"]
                reason = decisions.get(decision)
                if not isinstance(reason, str) or not reason.strip() or "#" in reason:
                    errors.append(f"{where}: {surface}: invalid exemption {decision}")
                used_decisions.add(decision)
                continue
            if (
                not isinstance(cell, list)
                or not cell
                or not all(isinstance(name, str) for name in cell)
            ):
                errors.append(f"{where}: {surface}: name declarations or an exemption")
                continue
            named += 1
            for name in cell:
                if not name.startswith(f"{category}:"):
                    errors.append(f"{where}: {surface}: {name} has the wrong kind")
                if name in claimed[surface]:
                    errors.append(
                        f"{where}: {surface}: {name} belongs to multiple rows"
                    )
                claimed[surface].add(name)
        if not named:
            errors.append(f"{where}: no surface implements the contract")
    for surface in surfaces:
        actual = observed.get(surface, {})
        expected = shapes.get(surface, {})
        if not actual:
            errors.append(f"inventory {surface}: collector returned no declarations")
        if not expected:
            errors.append(f"inventory {surface}: no expected declarations")
        for name in sorted(actual.keys() - expected.keys()):
            errors.append(f"inventory {surface}: {name} has no recorded shape")
        for name in sorted(expected.keys() - actual.keys()):
            errors.append(f"inventory {surface}: {name} is recorded but absent")
        for name in sorted(actual.keys() & expected.keys()):
            if actual[name] != expected[name]:
                errors.append(f"inventory {surface}: {name} changed shape")
        for name in sorted(actual.keys() - claimed[surface]):
            errors.append(f"inventory {surface}: {name} has no contract row")
        for name in sorted(claimed[surface] - actual.keys()):
            errors.append(f"inventory {surface}: {name} is mapped but absent")
    for decision in sorted(set(decisions) - used_decisions):
        errors.append(f"inventory: unused exemption {decision}")
    return errors
