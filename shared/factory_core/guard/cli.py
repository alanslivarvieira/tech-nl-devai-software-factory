"""``factory-guard`` command line.

factory-guard lock   --spec-dir DIR --feature-id ID --approver NAME      -> prints root hash
factory-guard verify --spec-dir DIR [--expected-hash H]
factory-guard diff   --repo R --base SHA [--head REF] [--stack python] [--allow GLOB ...]
factory-guard patch  --patch FILE [--stack python] [--allow GLOB ...]   (gh-aw: before applying)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..stack import detect_stack, load_profile
from .changes import from_git, parse_patch
from .diffcheck import check_changes
from .manifest import lock, verify


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="factory-guard")
    sub = ap.add_subparsers(dest="cmd", required=True)
    lk = sub.add_parser("lock")
    lk.add_argument("--spec-dir", type=Path, required=True)
    lk.add_argument("--feature-id", required=True)
    lk.add_argument("--approver", required=True)
    vf = sub.add_parser("verify")
    vf.add_argument("--spec-dir", type=Path, required=True)
    vf.add_argument("--expected-hash")
    for name in ("diff", "patch"):
        p = sub.add_parser(name)
        p.add_argument("--stack")
        p.add_argument("--allow", action="append", default=[])
        p.add_argument("--json", type=Path)
        if name == "diff":
            p.add_argument("--repo", type=Path, default=Path("."))
            p.add_argument("--base", required=True)
            p.add_argument("--head", default="HEAD")
        else:
            p.add_argument("--patch", type=Path, required=True)
            p.add_argument("--repo", type=Path, default=Path("."))
    a = ap.parse_args(argv)

    if a.cmd == "lock":
        m = lock(a.spec_dir, a.feature_id, a.approver)
        print(m["root_hash"])
        return 0
    if a.cmd == "verify":
        res = verify(a.spec_dir, a.expected_hash)
        print(res.summary())
        return 0 if res.ok else 1
    stack = a.stack or detect_stack(a.repo)
    profile = load_profile(stack) if stack else None
    changes = from_git(a.repo, a.base, a.head) if a.cmd == "diff" else parse_patch(a.patch.read_text(encoding="utf-8"))
    rep = check_changes(changes, profile, a.allow)
    if a.json:
        a.json.write_text(json.dumps(rep.as_dict(), indent=2), encoding="utf-8")
    print(rep.summary())
    for w in rep.warnings:
        print(f"  ⚠ [{w.rule}] {w.path} — {w.detail}")
    return 0 if rep.ok else 1


if __name__ == "__main__":
    sys.exit(main())
