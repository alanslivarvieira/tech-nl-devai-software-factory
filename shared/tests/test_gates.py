"""Gate runner end-to-end on the greeter sample (real pytest, coverage, diff-cover, pymut, tad)."""

from __future__ import annotations

import pytest

from factory_core import pipeline
from factory_core.gates import GateContext, run_gates
from factory_core.gates import runner as gate_runner
from factory_core.models import FactoryConfig, GateResult, VerificationReport
from factory_core.reports import failure_brief, pr_body, report_markdown
from factory_core.stack import load_profile
from factory_core.tad_runner.runner import load_intent
from factory_core.testing import GREETER_BUGGY, GREETER_GOOD, GREETER_UNIT_TESTS

pytestmark = pytest.mark.e2e


def _node_gate_context(repo, out_dir):
    return GateContext(
        repo=repo,
        spec_dir=repo / ".factory/specs/web",
        base_sha="base",
        profile=load_profile("node"),
        config=FactoryConfig(),
        expected_hash=None,
        out_dir=out_dir,
    )


def test_frontend_dependency_gate_finds_missing_postcss_plugin(tmp_path):
    repo = tmp_path / "web"
    repo.mkdir()
    (repo / "package.json").write_text('{"dependencies":{"next":"16","react-dom":"19"}}', encoding="utf-8")
    (repo / "postcss.config.mjs").write_text(
        'export default { plugins: { "@tailwindcss/postcss": {} } }', encoding="utf-8"
    )

    result = gate_runner.gate_frontend_dependencies(_node_gate_context(repo, tmp_path / "out"))

    assert result.status == "fail"
    assert "@tailwindcss/postcss" in result.details
    assert "package.json" in result.details


def test_playwright_gate_requires_config_script_and_spec(tmp_path):
    repo = tmp_path / "web"
    repo.mkdir()
    (repo / "package.json").write_text('{"dependencies":{"next":"16","react-dom":"19"}}', encoding="utf-8")

    result = gate_runner.gate_playwright(_node_gate_context(repo, tmp_path / "out"))

    assert result.status == "fail" and result.required
    assert "@playwright/test" in result.details
    assert "playwright.config.ts" in result.details
    assert "test:e2e" in result.details
    assert "Playwright spec" in result.details


def test_backend_node_skips_frontend_gates(tmp_path):
    repo = tmp_path / "api"
    repo.mkdir()
    (repo / "package.json").write_text('{"dependencies":{"express":"5"}}', encoding="utf-8")
    ctx = _node_gate_context(repo, tmp_path / "out")

    results = run_gates(ctx, only=["frontend_dependencies", "playwright"])

    assert [(result.name, result.status) for result in results] == [
        ("frontend_dependencies", "skip"),
        ("playwright", "skip"),
    ]


def test_playwright_gate_installs_browser_then_runs_e2e_script(tmp_path, monkeypatch):
    repo = tmp_path / "web"
    (repo / "tests" / "e2e").mkdir(parents=True)
    (repo / "package.json").write_text(
        '{"dependencies":{"next":"16"},"devDependencies":{"@playwright/test":"1.50"},'
        '"scripts":{"test:e2e":"playwright test"}}',
        encoding="utf-8",
    )
    (repo / "playwright.config.ts").write_text("export default {}", encoding="utf-8")
    (repo / "tests" / "e2e" / "home.spec.ts").write_text("import { test } from '@playwright/test';", encoding="utf-8")
    calls = []

    def run_commands(ctx, name, commands):
        calls.extend(command.argv for command in commands)
        return GateResult(name=name, status="pass", summary="mocked")

    monkeypatch.setattr(gate_runner, "_run_commands", run_commands)
    result = gate_runner.gate_playwright(_node_gate_context(repo, tmp_path / "out"))

    assert result.status == "pass"
    assert calls == [
        ["npx", "--no-install", "playwright", "install", "chromium"],
        ["npm", "run", "test:e2e"],
    ]


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
