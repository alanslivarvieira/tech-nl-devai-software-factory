"""Execute the tests-as-data contract and validate its structure."""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..models import IntentSpec
from .compare import check
from .drivers import DriverError, JsonlDriver, open_driver
from .gherkin import features_in_sync, load_feature_cases
from .layout import SpecLayout
from .loader import SpecError, load_example_cases
from .model import Case, CaseResult
from .properties import PropertySpec, load_properties, run_property

ALL_KINDS = ("table", "scenario", "gherkin", "property")


def load_intent(layout: SpecLayout) -> IntentSpec:
    if not layout.intent.exists():
        raise SpecError(f"missing {layout.intent}")
    return IntentSpec.model_validate(yaml.safe_load(layout.intent.read_text(encoding="utf-8")))


@dataclass
class TadReport:
    results: list[CaseResult] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # structural problems (validation, driver)

    @property
    def passed(self) -> bool:
        return not self.problems and all(r.status == "pass" for r in self.results)

    def counts(self) -> dict[str, int]:
        c = {"pass": 0, "fail": 0, "error": 0}
        for r in self.results:
            c[r.status] += 1
        return c

    def failing_ids(self) -> list[str]:
        return [r.case_id for r in self.results if r.status != "pass"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "counts": self.counts(),
            "problems": self.problems,
            "results": [r.as_dict() for r in self.results],
        }


# ------------------------------------------------------------------------------------------ validate
def validate(layout: SpecLayout) -> list[str]:
    """Structural checks done before freeze and at every verification (returns problems)."""
    problems: list[str] = []
    try:
        intent = load_intent(layout)
    except Exception as exc:
        return [f"intent.yaml invalid: {exc}"]
    try:
        cases = load_example_cases(layout)
        cases += load_feature_cases(layout)
        props = load_properties(layout.properties)
    except SpecError as exc:
        return [str(exc)]
    if not cases and not props:
        problems.append("no test cases: add examples/*.csv, scenarios/*.yaml or properties.yaml")
    known_ops = intent.interface.op_names()
    ac_ids = intent.ac_ids()
    covered: set[str] = set()
    for c in cases:
        if c.ac_id not in ac_ids:
            problems.append(f"{c.source}: ac_id {c.ac_id!r} is not an acceptance criterion in intent.yaml")
        covered.add(c.ac_id)
        for s in c.steps:
            if s.op not in known_ops:
                problems.append(f"{c.source}: op {s.op!r} is not declared in intent.interface.ops")
    for p in props:
        if p.ac_id not in ac_ids:
            problems.append(f"properties.yaml:{p.name}: unknown ac_id {p.ac_id!r}")
        if p.op not in known_ops:
            problems.append(f"properties.yaml:{p.name}: op {p.op!r} is not declared")
        covered.add(p.ac_id)
    for ac in sorted(ac_ids - covered):
        problems.append(f"{ac} has no example, scenario or property — every acceptance criterion needs one")
    for name in features_in_sync(layout, intent.title):
        problems.append(f"features/{name} is out of sync with its data file (regenerate with `tad gherkin`)")
    if intent.interface.kind == "jsonl" and not intent.interface.command:
        problems.append("interface.kind=jsonl needs interface.command (the driver launch command)")
    return sorted(set(problems))


# ------------------------------------------------------------------------------------------------ run
def _run_case(case: Case, driver: Any) -> CaseResult:
    start = time.monotonic()
    base = dict(
        case_id=case.id, kind=case.kind, suite=case.suite, name=case.name, ac_id=case.ac_id, baseline=case.baseline
    )
    try:
        driver.reset()
        for i, step in enumerate(case.steps):
            res = driver.call(step.op, step.input)
            if step.expect is None:
                continue
            ok, msg, diff = check(step.expect, res)
            if not ok:
                return CaseResult(
                    **base,
                    status="fail",
                    message=f"step {i + 1} ({step.op}): {msg}",
                    diff=diff,
                    duration_s=time.monotonic() - start,
                )
    except DriverError as exc:
        return CaseResult(**base, status="error", message=f"driver error: {exc}", duration_s=time.monotonic() - start)
    return CaseResult(**base, status="pass", duration_s=time.monotonic() - start)


def _prop_result(p: PropertySpec, driver: Any) -> CaseResult:
    start = time.monotonic()
    ok, msg = run_property(p, driver)
    return CaseResult(
        case_id=f"property:{p.op}:{p.name}",
        kind="property",
        suite=p.op,
        name=p.name,
        ac_id=p.ac_id,
        baseline=p.baseline,
        status="pass" if ok else "fail",  # type: ignore[arg-type]
        message=msg,
        duration_s=time.monotonic() - start,
    )


def run(spec_dir: Path, repo: Path, kinds: Iterable[str] = ALL_KINDS) -> TadReport:
    layout = SpecLayout(spec_dir)
    report = TadReport()
    report.problems = validate(layout)
    if any(p.startswith("intent.yaml invalid") for p in report.problems):
        return report
    intent = load_intent(layout)
    kinds = set(kinds)
    cases: list[Case] = []
    try:
        if kinds & {"table", "scenario"}:
            cases += [c for c in load_example_cases(layout) if c.kind in kinds]
        if "gherkin" in kinds:
            cases += load_feature_cases(layout)
        props = load_properties(layout.properties) if "property" in kinds else []
    except SpecError as exc:
        report.problems.append(str(exc))
        return report
    spec_rel = spec_dir.resolve().relative_to(repo.resolve()).as_posix() if _inside(spec_dir, repo) else str(spec_dir)
    try:
        driver = open_driver(intent.interface, repo, spec_rel)
    except DriverError as exc:
        report.problems.append(f"driver failed to start: {exc}")
        return report
    try:
        for case in cases:
            report.results.append(_run_case(case, driver))
        for p in props:
            report.results.append(_prop_result(p, driver))
    finally:
        driver.close()
    return report


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


# ------------------------------------------------------------------------------------- freeze check
def freeze_check(spec_dir: Path, repo: Path) -> list[str]:
    """Before any production change: the driver must answer, 'existing' cases must pass and 'new'
    cases must NOT pass (otherwise the test is vacuous or the feature already exists)."""
    layout = SpecLayout(spec_dir)
    problems = validate(layout)
    if problems:
        return problems
    intent = load_intent(layout)
    if intent.interface.kind == "jsonl":
        spec_rel = spec_dir.resolve().relative_to(repo.resolve()).as_posix()
        from .drivers import expand_argv

        drv = JsonlDriver(expand_argv(intent.interface.command or [], spec_rel), repo, intent.interface.timeout_s)
        try:
            if not drv.ping():
                return [f"driver does not answer __ping__: {drv.stderr_tail()}"]
        finally:
            drv.close()
    report = run(spec_dir, repo)
    problems = list(report.problems)
    for r in report.results:
        if r.baseline == "existing" and r.status != "pass":
            problems.append(f"{r.case_id}: marked 'existing' but fails on the untouched code: {r.message}")
        if r.baseline == "new" and r.status == "pass":
            problems.append(
                f"{r.case_id}: passes before any change — vacuous test or behaviour already exists "
                "(mark it baseline=existing if intended)"
            )
    return problems
