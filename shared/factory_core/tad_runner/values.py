"""Typed cell values for CSV example tables.

Header syntax: ``in.<field>[:type]``, ``out.<field>[:type]``, plus the reserved columns
``case``, ``ac_id``, ``op``, ``error``, ``error_contains``, ``baseline``.
Types: ``str`` (default), ``int``, ``float``, ``bool``, ``json``.
Cell sentinels: empty cell = field omitted, ``<null>`` = JSON null, ``<empty>`` = empty string.
Dotted field names (``out.user.name``) build nested objects.
"""

from __future__ import annotations

import json
from typing import Any

from .model import MISSING

NULL, EMPTY = "<null>", "<empty>"
TYPES = ("str", "int", "float", "bool", "json")


def parse_header(column: str) -> tuple[str, str, str]:
    """``in.age:int`` -> (``in``, ``age``, ``int``)."""
    side, _, rest = column.partition(".")
    name, _, typ = rest.partition(":")
    typ = typ or "str"
    if side not in ("in", "out") or not name:
        raise ValueError(f"bad column {column!r}: expected in.<field>[:type] or out.<field>[:type]")
    if typ not in TYPES:
        raise ValueError(f"bad type {typ!r} in column {column!r}; use one of {TYPES}")
    return side, name, typ


def parse_cell(raw: str | None, typ: str) -> Any:
    if raw is None or raw == "":
        return MISSING
    if raw == NULL:
        return None
    if raw == EMPTY:
        return ""
    if typ == "str":
        return raw
    if typ == "int":
        return int(raw)
    if typ == "float":
        return float(raw)
    if typ == "bool":
        low = raw.strip().lower()
        if low in ("true", "yes", "1"):
            return True
        if low in ("false", "no", "0"):
            return False
        raise ValueError(f"not a bool: {raw!r}")
    return json.loads(raw)


def set_path(target: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = target
    for p in parts[:-1]:
        node = node.setdefault(p, {})
    node[parts[-1]] = value


def get_path(source: Any, dotted: str) -> Any:
    node = source
    for p in dotted.split("."):
        if isinstance(node, dict) and p in node:
            node = node[p]
        elif isinstance(node, list) and p.isdigit() and int(p) < len(node):
            node = node[int(p)]
        else:
            return MISSING
    return node
