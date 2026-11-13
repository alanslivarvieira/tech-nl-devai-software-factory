"""``tad`` command line: validate | run | gherkin | freeze-check."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .gherkin import write_features
from .layout import SpecLayout
from .report import to_text, write_reports
from .runner import ALL_KINDS, freeze_check, load_intent, run, validate


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="tad", description="Tests-as-data contract runner")
    sub = ap.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate", help="check structure, AC coverage and Gherkin sync")
    v.add_argument("spec_dir", type=Path)

    r = sub.add_parser("run", help="execute the contract against a repo")
    r.add_argument("spec_dir", type=Path)
    r.add_argument("--repo", type=Path, default=Path("."))
    r.add_argument("--kinds", default=",".join(ALL_KINDS))
    r.add_argument("--json", type=Path)
    r.add_argument("--junit", type=Path)

    g = sub.add_parser("gherkin", help="(re)generate features/*.feature from the data files")
    g.add_argument("spec_dir", type=Path)

    f = sub.add_parser("freeze-check", help="new cases must fail and existing cases pass on untouched code")
    f.add_argument("spec_dir", type=Path)
    f.add_argument("--repo", type=Path, default=Path("."))

    a = ap.parse_args(argv)
    if a.cmd == "validate":
        problems = validate(SpecLayout(a.spec_dir))
        for p in problems:
            print(f"✗ {p}")
        print("contract is valid" if not problems else f"{len(problems)} problem(s)")
        return 1 if problems else 0
    if a.cmd == "gherkin":
        layout = SpecLayout(a.spec_dir)
        for p in write_features(layout, load_intent(layout).title):
            print(f"wrote {p}")
        return 0
    if a.cmd == "freeze-check":
        problems = freeze_check(a.spec_dir, a.repo)
        for p in problems:
            print(f"✗ {p}")
        print("freeze check passed" if not problems else f"{len(problems)} problem(s)")
        return 1 if problems else 0
    report = run(a.spec_dir, a.repo, [k.strip() for k in a.kinds.split(",") if k.strip()])
    write_reports(report, a.json, a.junit)
    print(to_text(report))
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
