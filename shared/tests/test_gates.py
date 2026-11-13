"""Gate runner end-to-end on the greeter sample (real pytest, coverage, diff-cover, pymut, tad)."""

from __future__ import annotations

import pytest

from factory_core import pipeline
from factory_core.gates import GateContext, run_gates
from factory_core.models import FactoryConfig, VerificationReport
from factory_core.reports import failure_brief, pr_body, report_markdown
from factory_core.stack import load_profile
from factory_core.tad_runner.runner import load_intent
from factory_core.testing import GREETER_BUGGY, GREETER_GOOD, GREETER_UNIT_TESTS

pytestmark = pytest.mark.e2e


def _verify(repo, layout, fr, tmp_path, it=1) -> VerificationReport:
    ctx = GateContext(
        repo=repo,
        spec_dir=layout.root,
        base_sha=fr.freeze_sha,
        profile=load_profile("python"),
        config=FactoryConfig(),
        expected_hash=fr.root_hash,
        out_dir=tmp_path / f"out{it}",
    )
    return VerificationReport(
        feature_id="personalised-greeting", iteration=it, commit=pipeline.head(repo), gates=run_gates(ctx)
    )


def test_good_change_passes_every_gate(frozen_greeter, tmp_path):
    repo, layout, fr = frozen_greeter
    (repo / "src/greeter/__init__.py").write_text(GREETER_GOOD, encoding="utf-8")
    (repo / "tests/test_personalised.py").write_text(GREETER_UNIT_TESTS, encoding="utf-8")
    pipeline.commit_all(repo, "feature")
    report = _verify(repo, layout, fr, tmp_path)
    by = {g.name: g for g in report.gates}
    assert report.passed, report_markdown(report) + "\n".join(g.details for g in report.gates if g.blocking)
    for gate in ("integrity", "build", "unit", "diff_coverage", "contract", "acceptance", "property", "mutation"):
        assert by[gate].status == "pass", gate
    body = pr_body(
        load_intent(layout), pipeline.spec_rel("personalised-greeting"), layout.root, report, 2, fr.root_hash, "claude"
    )
    assert "| default greeting |" in body and "AC-3" in body


def test_buggy_change_fails_with_actionable_brief(frozen_greeter, tmp_path):
    repo, layout, fr = frozen_greeter
    (repo / "src/greeter/__init__.py").write_text(GREETER_BUGGY, encoding="utf-8")
    pipeline.commit_all(repo, "feature")
    report = _verify(repo, layout, fr, tmp_path)
    by = {g.name: g for g in report.gates}
    assert not report.passed
    assert by["contract"].status == "fail" and by["acceptance"].status == "fail"
    assert by["integrity"].status == "pass"
    brief = failure_brief(report)
    assert "name is trimmed" in brief and "must not modify anything under .factory/specs/" in brief


def test_tampering_fails_integrity_even_if_tests_pass(frozen_greeter, tmp_path):
    repo, layout, fr = frozen_greeter
    (repo / "src/greeter/__init__.py").write_text(GREETER_BUGGY, encoding="utf-8")
    csv = layout.examples / "greet.csv"
    csv.chmod(0o644)
    # The cheat: rewrite the expectations to match the buggy code and regenerate everything.
    csv.write_text(csv.read_text(encoding="utf-8").replace('"Hello, Ann!"', '"Hello,   Ann  !"'), encoding="utf-8")
    pipeline.commit_all(repo, "make tests pass")
    report = _verify(repo, layout, fr, tmp_path)
    integrity = next(g for g in report.gates if g.name == "integrity")
    assert integrity.status == "fail" and "TAMPERED" in integrity.details
