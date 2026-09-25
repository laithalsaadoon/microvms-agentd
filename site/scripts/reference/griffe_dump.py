#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["griffelib==2.3.0"]
# ///
# SPDX-License-Identifier: Apache-2.0
"""Read `microvms-py/microvms.pyi` with Griffe and print the public surface as JSON.

The site's Python reference (`site/scripts/reference/sdk/python.mjs`) is rendered from this
output, so the parsing is Griffe's rather than a second hand-written reader of the stub.
Griffe is the engine under mkdocstrings; it reads a `.pyi` with the standard `ast` module
and renders every annotation and default back to source text, which is all a reference page
needs.

`griffe2md`, Griffe's own Markdown renderer, was measured and declined: it drops parameter
annotations from signatures (`parse(name)` for `parse(name: str) -> Region`), links `str` and
`bool` to in-page anchors that do not exist, and writes dotted heading ids that do not match
the ones Starlight slugs, so the links validator rejects every cross-reference it emits.

The stub is loaded with `griffe.visit` on the file rather than `griffe.load` on a module name,
because the stub has no importable module beside it in the repository: `load` searches for
`microvms.py` or a package and finds neither.

Usage, from the repository root or anywhere:

    uv run --script site/scripts/reference/griffe_dump.py microvms-py/microvms.pyi

The JSON is printed to stdout. Nothing is written.
"""

from __future__ import annotations

import json
import sys
from importlib.metadata import version
from pathlib import Path

import griffe


def text(value: object) -> str | None:
    """An annotation or default as the source spells it, or `None` when there is none."""
    return None if value is None else str(value)


def docstring(obj: griffe.Object) -> str:
    """The docstring's raw text, dedented by Griffe, or the empty string."""
    return "" if obj.docstring is None else obj.docstring.value


def parameters(function: griffe.Function) -> list[dict[str, str | None]]:
    """Each parameter with its kind, so the renderer can place `/` and `*` the way Python does."""
    return [
        {
            "name": parameter.name,
            "kind": parameter.kind.value if parameter.kind is not None else None,
            "annotation": text(parameter.annotation),
            "default": text(parameter.default),
        }
        for parameter in function.parameters
    ]


def function_record(function: griffe.Function) -> dict[str, object]:
    return {
        "kind": "function",
        "name": function.name,
        "docstring": docstring(function),
        "decorators": sorted(str(decorator.value) for decorator in function.decorators),
        "labels": sorted(function.labels),
        "parameters": parameters(function),
        "returns": text(function.returns),
        "special": function.is_special,
        "lineno": function.lineno,
    }


def attribute_record(attribute: griffe.Attribute) -> dict[str, object]:
    return {
        "kind": "attribute",
        "name": attribute.name,
        "docstring": docstring(attribute),
        "labels": sorted(attribute.labels),
        "annotation": text(attribute.annotation),
        "value": text(attribute.value),
        "lineno": attribute.lineno,
    }


def member_record(member: griffe.Object | griffe.Alias) -> dict[str, object] | None:
    """A member the reference documents, or `None` for an import or a private name."""
    if member.is_alias or member.is_imported:
        return None
    if member.is_private and not member.is_special:
        return None
    if member.is_function:
        return function_record(member)
    if member.is_attribute:
        return attribute_record(member)
    if member.is_class:
        return class_record(member)
    return None


def class_record(cls: griffe.Class) -> dict[str, object]:
    members = [member_record(member) for member in cls.members.values()]
    return {
        "kind": "class",
        "name": cls.name,
        "docstring": docstring(cls),
        "bases": [str(base) for base in cls.bases],
        "decorators": sorted(str(decorator.value) for decorator in cls.decorators),
        "members": [member for member in members if member is not None],
        "lineno": cls.lineno,
    }


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        sys.stderr.write("usage: griffe_dump.py <path/to/module.pyi>\n")
        return 2
    path = Path(argv[0])
    module = griffe.visit(
        path.stem, filepath=path, code=path.read_text(encoding="utf-8")
    )
    members = [member_record(member) for member in module.members.values()]
    json.dump(
        {
            "griffe": version("griffelib"),
            "module": module.name,
            "docstring": docstring(module),
            "members": [member for member in members if member is not None],
        },
        sys.stdout,
        indent=1,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
