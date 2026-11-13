#!/usr/bin/env python3
"""Tests-as-data DRIVER (JSON-lines protocol) — generated at freeze time, then hash-locked.

The factory-owned runner (``tad``) sends one JSON request per line on stdin:
    {"id": 1, "op": "<op name>", "input": {...}}
and expects one JSON response per line on stdout:
    {"id": 1, "output": <any JSON>}            on success
    {"id": 1, "error": "<message>"}            when the system rejects the input

Rules for this file (enforced by review + the guard):
  * NO assertions and NO expected values here — the runner owns every comparison.
  * Only translate DSL ops into calls on the system's PUBLIC interface (see intent.yaml -> interface).
  * Import production code lazily inside the op functions so the driver starts before the feature exists.
  * Return plain JSON-serialisable data; map domain errors to ``raise ValueError(message)``.
"""

from __future__ import annotations

import importlib
import json
import sys

sys.path[:0] = ["src", "."]
_REAL_STDOUT = sys.stdout


def _reset(_inp: dict) -> None:
    """Return the system to a clean state between test cases (reload modules holding state)."""
    for name in list(sys.modules):
        if name.split(".")[0] in RESET_MODULES:
            importlib.reload(sys.modules[name])


RESET_MODULES: set[str] = set()  # e.g. {"todo"} when the module keeps in-memory state

# ---- op table: DSL op name -> callable(input: dict) -> JSON-serialisable output -------------------
OPS: dict = {
    # "greet": lambda inp: {"greeting": importlib.import_module("greeter").greet(inp.get("name"))},
}
# ---------------------------------------------------------------------------------------------------


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line)
        rid, op, inp = req.get("id"), req.get("op"), req.get("input") or {}
        sys.stdout = sys.stderr  # production prints must never corrupt the protocol
        try:
            if op == "__ping__":
                resp = {"id": rid, "output": "pong"}
            elif op == "__reset__":
                _reset(inp)
                resp = {"id": rid, "output": None}
            elif op in OPS:
                resp = {"id": rid, "output": OPS[op](inp)}
            else:
                resp = {"id": rid, "error": f"unknown op: {op}", "error_type": "UnknownOp"}
        except Exception as exc:
            resp = {"id": rid, "error": str(exc), "error_type": type(exc).__name__}
        finally:
            sys.stdout = _REAL_STDOUT
        _REAL_STDOUT.write(json.dumps(resp, default=str) + "\n")
        _REAL_STDOUT.flush()


if __name__ == "__main__":
    main()
