"""``factory-gates``: run the deterministic verification gates and write a VerificationReport.

    factory-gates run --repo . --spec-dir .factory/specs/<id> --base <freeze-sha> \
        [--expected-hash H] [--stack python] [--out .factory/run/verify] [--report report.json] \
        [--iteration N] [--only unit,contract] [--no-setup] [--config factory.yaml]
    factory-gates detect --repo .
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml

from ..models import FactoryConfig, VerificationReport
from ..reports.render import report_markdown
from ..stack import detect_stack, load_profile
from ..tad_runner.layout import SpecLayout
from ..tad_runner.runner import load_intent
from .runner import GateContext, run_gates


def load_config(path: Path | None) -> FactoryConfig:
    if path and path.exists():
        return FactoryConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    return FactoryConfig()


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="factory-gates")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("detect")
    d.add_argument("--repo", type=Path, default=Path("."))
    r = sub.add_parser("run")
    r.add_argument("--repo", type=Path, default=Path("."))
    r.add_argument("--spec-dir", type=Path, required=True)
    r.add_argument("--base", required=True, help="freeze commit sha")
    r.add_argument("--expected-hash")
    r.add_argument("--stack")
    r.add_argument("--out", type=Path, default=Path(".factory/run/verify"))
    r.add_argument("--report", type=Path)
    r.add_argument("--markdown", type=Path)
    r.add_argument("--iteration", type=int, default=0)
    r.add_argument("--only")
    r.add_argument("--no-setup", action="store_true")
    r.add_argument("--config", type=Path)
    a = ap.parse_args(argv)

    if a.cmd == "detect":
        print(detect_stack(a.repo) or "unknown")
        return 0
    stack = a.stack or detect_stack(a.repo)
    if not stack:
        print("cannot detect the target stack; pass --stack")
        return 2
    config = load_config(a.config)
    if a.no_setup:
        config.run_setup = False
    ctx = GateContext(
        repo=a.repo,
        spec_dir=a.spec_dir,
        base_sha=a.base,
        profile=load_profile(stack),
        config=config,
        expected_hash=a.expected_hash,
        out_dir=a.out,
    )
    gates = run_gates(ctx, a.only.split(",") if a.only else None)
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=a.repo, capture_output=True, text=True).stdout.strip()
    intent = load_intent(SpecLayout(ctx.spec_dir))
    report = VerificationReport(feature_id=intent.feature_id, iteration=a.iteration, commit=commit, gates=gates)
    if a.report:
        a.report.parent.mkdir(parents=True, exist_ok=True)
        a.report.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    md = report_markdown(report)
    if a.markdown:
        a.markdown.write_text(md, encoding="utf-8")
    print(md)
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
