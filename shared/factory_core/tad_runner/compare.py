"""Comparisons and approval-style diffs. Every assertion of the contract lives here."""

from __future__ import annotations

import difflib
from typing import Any

import yaml

from .drivers import CallResult
from .model import MISSING, Expect
from .values import get_path


def _dump(value: Any) -> str:
    return yaml.safe_dump(value, sort_keys=True, allow_unicode=True, default_flow_style=False).rstrip()


def text_diff(expected: Any, actual: Any) -> str:
    exp, act = _dump(expected).splitlines(), _dump(actual).splitlines()
    return "\n".join(difflib.unified_diff(exp, act, "expected", "actual", lineterm=""))


def _equal(a: Any, b: Any) -> bool:
    # bool is an int subclass in Python; the contract treats them as different types.
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_equal(x, y) for x, y in zip(a, b, strict=True))
    return type(a) is type(b) and a == b


def _flatten(prefix: str, value: Any, out: dict[str, Any]) -> None:
    if isinstance(value, dict) and not value and not prefix:
        return
    if isinstance(value, dict) and value:
        for k, v in value.items():
            _flatten(f"{prefix}.{k}" if prefix else str(k), v, out)
    else:
        out[prefix] = value


def check(expect: Expect, result: CallResult) -> tuple[bool, str, str]:
    """Return (ok, message, diff)."""
    expected_view = expect.describe()
    if expect.error is not None or expect.error_contains is not None:
        if result.ok:
            return False, "expected an error but the call succeeded", text_diff(expected_view, result.view())
        if expect.error is not None and result.error != expect.error:
            return False, "wrong error message", text_diff({"error": expect.error}, {"error": result.error})
        if expect.error_contains is not None and expect.error_contains not in (result.error or ""):
            return (
                False,
                "error message does not contain the expected text",
                text_diff({"error_contains": expect.error_contains}, {"error": result.error}),
            )
        return True, "", ""
    if not result.ok:
        return False, f"unexpected error: {result.error}", text_diff(expected_view, result.view())
    if expect.output is not MISSING:
        if not _equal(expect.output, result.output):
            return False, "output differs", text_diff(expect.output, result.output)
        return True, "", ""
    if expect.output_subset is not None:
        flat: dict[str, Any] = {}
        _flatten("", expect.output_subset, flat)
        actual_view: dict[str, Any] = {}
        ok = True
        for path, want in flat.items():
            got = get_path(result.output, path)
            actual_view[path] = "<missing>" if got is MISSING else got
            if got is MISSING or not _equal(want, got):
                ok = False
        if not ok:
            return False, "output fields differ", text_diff(flat, actual_view)
        return True, "", ""
    return True, "", ""  # no_error only
