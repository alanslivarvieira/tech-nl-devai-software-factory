"""Report writers: JUnit XML (for CI UIs), JSON (for the factory), console text (for agents/humans)."""

from __future__ import annotations

import json
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from .runner import TadReport


def to_junit(report: TadReport, name: str = "tests-as-data") -> str:
    c = report.counts()
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<testsuite name={quoteattr(name)} tests="{len(report.results)}" failures="{c["fail"]}" '
        f'errors="{c["error"] + len(report.problems)}">',
    ]
    for p in report.problems:
        lines.append(
            f'  <testcase classname="contract" name={quoteattr("structure")}><error message={quoteattr(p)}/></testcase>'
        )
    for r in report.results:
        lines.append(
            f"  <testcase classname={quoteattr(f'{r.kind}.{r.suite}')} name={quoteattr(r.name)} "
            f'time="{r.duration_s:.3f}">'
        )
        if r.status != "pass":
            tag = "failure" if r.status == "fail" else "error"
            lines.append(f"    <{tag} message={quoteattr(r.message)}>{escape(r.diff)}</{tag}>")
        lines.append("  </testcase>")
    lines.append("</testsuite>")
    return "\n".join(lines) + "\n"


def to_text(report: TadReport, max_failures: int = 30) -> str:
    c = report.counts()
    out = [
        f"tests-as-data: {c['pass']} passed, {c['fail']} failed, {c['error']} errors"
        + (f", {len(report.problems)} structural problems" if report.problems else "")
    ]
    for p in report.problems:
        out.append(f"  ! {p}")
    shown = 0
    for r in report.results:
        if r.status == "pass":
            continue
        shown += 1
        if shown > max_failures:
            out.append("  ... more failures omitted")
            break
        out.append(f"  ✗ [{r.ac_id}] {r.case_id} — {r.message}")
        if r.diff:
            out.extend("      " + line for line in r.diff.splitlines())
    return "\n".join(out)


def write_reports(report: TadReport, json_path: Path | None, junit_path: Path | None) -> None:
    if json_path:
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(report.as_dict(), indent=2, default=str), encoding="utf-8")
    if junit_path:
        junit_path.parent.mkdir(parents=True, exist_ok=True)
        junit_path.write_text(to_junit(report), encoding="utf-8")
