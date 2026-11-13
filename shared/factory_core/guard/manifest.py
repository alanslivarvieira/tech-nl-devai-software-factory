"""Hash-lock of the approved contract.

The manifest lists the sha256 of every file in the spec directory and a ``root_hash`` over all of them.
The root hash is ALSO stored outside the agent's reach (LangGraph checkpoint DB / gh-aw issue body) and that
external copy is the trust anchor: an agent rewriting both a CSV and the manifest file is still caught.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import __version__
from ..tad_runner.layout import MANIFEST

IGNORED_PARTS = {"__pycache__", ".pytest_cache", "node_modules"}
IGNORED_SUFFIXES = {".pyc", ".pyo"}


def _files(spec_dir: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(spec_dir.rglob("*")):
        if not p.is_file() or p.name == MANIFEST:
            continue
        rel = p.relative_to(spec_dir)
        if IGNORED_PARTS & set(rel.parts) or p.suffix in IGNORED_SUFFIXES:
            continue
        # Normalise line endings so Windows checkouts (core.autocrlf) hash identically.
        data = p.read_bytes().replace(b"\r\n", b"\n")
        out[rel.as_posix()] = hashlib.sha256(data).hexdigest()
    return out


def root_hash(files: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def compute(spec_dir: Path) -> dict[str, Any]:
    files = _files(spec_dir)
    return {"files": files, "root_hash": root_hash(files)}


def lock(spec_dir: Path, feature_id: str, approver: str) -> dict[str, Any]:
    m = compute(spec_dir)
    manifest = {
        "feature_id": feature_id,
        "root_hash": m["root_hash"],
        "files": m["files"],
        "approved_by": approver,
        "approved_at": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat(),
        "runner_version": __version__,
    }
    (spec_dir / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


@dataclass
class VerifyResult:
    ok: bool
    actual_root_hash: str
    expected_root_hash: str | None
    modified: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.ok:
            return f"contract intact (root {self.actual_root_hash[:12]})"
        parts = self.problems[:]
        for label, items in (("modified", self.modified), ("added", self.added), ("removed", self.removed)):
            if items:
                parts.append(f"{label}: {', '.join(items)}")
        return "CONTRACT TAMPERED — " + "; ".join(parts)


def verify(spec_dir: Path, expected_root_hash: str | None = None) -> VerifyResult:
    """Compare the spec dir with its manifest and (authoritatively) with the externally stored hash."""
    actual = compute(spec_dir)
    res = VerifyResult(ok=True, actual_root_hash=actual["root_hash"], expected_root_hash=expected_root_hash)
    mpath = spec_dir / MANIFEST
    recorded: dict[str, str] = {}
    if mpath.exists():
        try:
            data = json.loads(mpath.read_text(encoding="utf-8"))
            recorded = data.get("files", {})
            if data.get("root_hash") != root_hash(recorded):
                res.problems.append("manifest file is internally inconsistent")
        except json.JSONDecodeError:
            res.problems.append("manifest file is not valid JSON")
    else:
        res.problems.append("manifest.lock.json is missing")
    res.modified = sorted(k for k in actual["files"].keys() & recorded.keys() if actual["files"][k] != recorded[k])
    res.added = sorted(actual["files"].keys() - recorded.keys())
    res.removed = sorted(recorded.keys() - actual["files"].keys())
    if expected_root_hash and actual["root_hash"] != expected_root_hash:
        res.problems.append(
            f"root hash {actual['root_hash'][:12]} != approved {expected_root_hash[:12]} (external trust anchor)"
        )
    res.ok = not (res.problems or res.modified or res.added or res.removed)
    return res
