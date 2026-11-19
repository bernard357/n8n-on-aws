"""Turn the assistant's answers into a minimal, commented YAML file."""

from __future__ import annotations

import copy
from collections.abc import Iterator
from typing import Any

import yaml

from ..config import resolve

SCHEMA_URL = (
    "https://raw.githubusercontent.com/bernard357/n8n-on-aws/main/"
    "schema/n8n-on-aws.v1.json"
)
KEEP = {"apiVersion", "name", "profile"}
TOP_ORDER = [
    "apiVersion",
    "name",
    "profile",
    "environment",
    "aws",
    "n8n",
    "topology",
    "runtime",
    "network",
    "ingress",
    "auth",
    "database",
    "queue",
    "storage",
    "observability",
    "backup",
    "finops",
]


def _leaves(
    data: dict[str, Any], prefix: tuple[str, ...] = ()
) -> Iterator[tuple[str, ...]]:
    for key, value in data.items():
        path = (*prefix, key)
        if isinstance(value, dict) and value:
            yield from _leaves(value, path)
        else:
            yield path


def _without(data: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any]:
    trimmed = copy.deepcopy(data)
    node = trimmed
    parents = []
    for key in path[:-1]:
        parents.append((node, key))
        node = node[key]
    del node[path[-1]]
    for parent, key in reversed(parents):  # drop mappings left empty
        if parent[key] == {}:
            del parent[key]
    return trimmed


def _signature(data: dict[str, Any]) -> tuple[Any, frozenset[str]] | None:
    result = resolve(data)
    if result.deployment is None:
        return None
    errors = frozenset(f.code + f.path for f in result.findings if f.level == "error")
    return result.deployment.model_dump(mode="json"), errors


def prune(data: dict[str, Any]) -> dict[str, Any]:
    """Drop every key the profile would set to the same value anyway."""
    reference = _signature(data)
    current = copy.deepcopy(data)
    changed = True
    while changed:
        changed = False
        for path in list(_leaves(current)):
            if path[0] in KEEP:
                continue
            candidate = _without(current, path)
            if _signature(candidate) == reference:
                current = candidate
                changed = True
    return current


def _scalar(value: Any) -> str:
    text = yaml.safe_dump([value], default_flow_style=True, width=1000).strip()
    return text[1:-1]


def render(
    data: dict[str, Any],
    comments: dict[str, str],
    header: list[str],
    plain: bool = False,
) -> str:
    """YAML with a header and one comment per written key (unless ``plain``)."""
    lines: list[str] = []
    if not plain:
        lines.append(f"# yaml-language-server: $schema={SCHEMA_URL}")
        lines.extend(f"# {line}".rstrip() for line in header)

    def walk(node: dict[str, Any], indent: int, prefix: str) -> None:
        pad = "  " * indent
        for key, value in node.items():
            path = f"{prefix}{key}"
            note = "" if plain else comments.get(path, "")
            if isinstance(value, dict):
                if note:
                    lines.append(f"{pad}# {note}")
                lines.append(f"{pad}{key}:")
                walk(value, indent + 1, path + ".")
                continue
            if isinstance(value, list):
                text = yaml.safe_dump(value, default_flow_style=True, width=1000)
                rendered = text.strip()
            else:
                rendered = _scalar(value)
            suffix = f"  # {note}" if note else ""
            lines.append(f"{pad}{key}: {rendered}{suffix}")

    order = {key: index for index, key in enumerate(TOP_ORDER)}
    top = dict(sorted(data.items(), key=lambda kv: order.get(kv[0], len(order))))
    walk(top, 0, "")
    return "\n".join(lines) + "\n"
