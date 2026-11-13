"""Load test cases from tests-as-data files (CSV tables and YAML scenarios)."""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any

import yaml

from .layout import SpecLayout
from .model import MISSING, Case, Expect, Step
from .values import parse_cell, parse_header, set_path

RESERVED = {"case", "ac_id", "op", "error", "error_contains", "baseline"}
EXPECT_KEYS = {"output", "output_subset", "error", "error_contains", "no_error"}


class SpecError(ValueError):
    """Malformed tests-as-data file."""


# --------------------------------------------------------------------------------------------- CSV
def load_table(path: Path) -> list[Case]:
    return parse_table(path.read_text(encoding="utf-8"), suite=path.stem, source=str(path.name))


def parse_table(text: str, suite: str, source: str = "") -> list[Case]:
    reader = csv.DictReader(io.StringIO(text), skipinitialspace=True)
    if not reader.fieldnames:
        raise SpecError(f"{source}: empty table")
    columns = [c.strip() for c in reader.fieldnames]
    if "case" not in columns or "ac_id" not in columns:
        raise SpecError(f"{source}: tables need 'case' and 'ac_id' columns")
    typed: dict[str, tuple[str, str, str]] = {}
    for col in columns:
        if col in RESERVED:
            continue
        if col == "out" or col.startswith("out:"):
            typed[col] = ("out", "", col.partition(":")[2] or "str")
            continue
        try:
            typed[col] = parse_header(col)
        except ValueError as exc:
            raise SpecError(f"{source}: {exc}") from exc

    cases: list[Case] = []
    seen: set[str] = set()
    for lineno, raw_row in enumerate(reader, start=2):
        # Data cells keep their whitespace (it can be meaningful); reserved columns are trimmed.
        row = {
            k.strip(): (v.strip() if k.strip() in RESERVED and isinstance(v, str) else v)
            for k, v in raw_row.items()
            if k
        }
        name = row.get("case") or ""
        if not name:
            raise SpecError(f"{source}:{lineno}: missing case name")
        if name in seen:
            raise SpecError(f"{source}:{lineno}: duplicate case name {name!r}")
        seen.add(name)
        op = row.get("op") or suite
        inp: dict[str, Any] = {}
        out: dict[str, Any] = {}
        whole_out: Any = MISSING
        for col, (side, field, typ) in typed.items():
            try:
                value = parse_cell(row.get(col), typ)
            except ValueError as exc:
                raise SpecError(f"{source}:{lineno}: column {col}: {exc}") from exc
            if value is MISSING:
                continue
            if side == "in":
                set_path(inp, field, value)
            elif field == "":
                whole_out = value
            else:
                set_path(out, field, value)
        expect = Expect()
        if row.get("error"):
            expect.error = row["error"]
        elif row.get("error_contains"):
            expect.error_contains = row["error_contains"]
        elif whole_out is not MISSING:
            expect.output = whole_out
        elif out:
            expect.output_subset = out
        else:
            expect.no_error = True
        if (expect.error or expect.error_contains) and (out or whole_out is not MISSING):
            raise SpecError(f"{source}:{lineno}: a row cannot expect both an error and an output")
        baseline = row.get("baseline") or "new"
        if baseline not in ("new", "existing"):
            raise SpecError(f"{source}:{lineno}: baseline must be 'new' or 'existing'")
        cases.append(
            Case(
                kind="table",
                suite=suite,
                name=name,
                ac_id=row.get("ac_id") or "",
                steps=[Step(op=op, input=inp, expect=expect)],
                baseline=baseline,  # type: ignore[arg-type]
                source=f"{source}:{lineno}",
            )
        )
    return cases


# ------------------------------------------------------------------------------------ YAML scenarios
def _expect_from(d: dict[str, Any], where: str) -> Expect:
    unknown = set(d) - EXPECT_KEYS - {"op", "input", "state"}
    if unknown:
        raise SpecError(f"{where}: unknown expectation keys {sorted(unknown)}")
    e = Expect(
        output=d.get("output", MISSING),
        output_subset=d.get("output_subset"),
        error=d.get("error"),
        error_contains=d.get("error_contains"),
        no_error=bool(d.get("no_error", False)),
    )
    if not e.describe():
        raise SpecError(f"{where}: expectation must state output, output_subset, error, error_contains or no_error")
    return e


def _step(d: Any, where: str, phase: str) -> Step:
    if not isinstance(d, dict) or "op" not in d:
        raise SpecError(f"{where}: each step needs an 'op'")
    inp = d.get("input") or {}
    if not isinstance(inp, dict):
        raise SpecError(f"{where}: 'input' must be a mapping")
    return Step(op=str(d["op"]), input=inp, phase=phase)  # type: ignore[arg-type]


def load_scenarios(path: Path) -> list[Case]:
    return parse_scenarios(path.read_text(encoding="utf-8"), default_suite=path.stem, source=path.name)


def parse_scenarios(text: str, default_suite: str, source: str = "") -> list[Case]:
    doc = yaml.safe_load(text) or {}
    suite = str(doc.get("suite") or default_suite)
    raw_cases = doc.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise SpecError(f"{source}: needs a non-empty 'cases' list")
    cases: list[Case] = []
    names: set[str] = set()
    for i, rc in enumerate(raw_cases):
        where = f"{source}#cases[{i}]"
        name = rc.get("name")
        if not name or name in names:
            raise SpecError(f"{where}: missing or duplicate name")
        names.add(name)
        steps: list[Step] = []
        for g in rc.get("given") or []:
            s = _step(g, where + ".given", "given")
            s.expect = Expect(no_error=True)
            steps.append(s)
        when = rc.get("when")
        whens = when if isinstance(when, list) else [when] if when else []
        if not whens:
            raise SpecError(f"{where}: needs a 'when'")
        for w in whens:
            s = _step(w, where + ".when", "when")
            s.expect = Expect(no_error=True)
            steps.append(s)
        then = rc.get("then")
        if not isinstance(then, dict):
            raise SpecError(f"{where}: needs a 'then' mapping (the assertion is mandatory)")
        main_keys = {k: v for k, v in then.items() if k != "state"}
        if main_keys:
            steps[-1].expect = _expect_from(main_keys, where + ".then")
        for j, q in enumerate(then.get("state") or []):
            s = _step(q, f"{where}.then.state[{j}]", "then")
            s.expect = _expect_from(q, f"{where}.then.state[{j}]")
            steps.append(s)
        if not main_keys and not then.get("state"):
            raise SpecError(f"{where}: 'then' must assert something")
        baseline = rc.get("baseline", "new")
        if baseline not in ("new", "existing"):
            raise SpecError(f"{where}: baseline must be 'new' or 'existing'")
        cases.append(
            Case(
                kind="scenario",
                suite=suite,
                name=str(name),
                ac_id=str(rc.get("ac_id") or ""),
                steps=steps,
                baseline=baseline,
                source=where,
            )
        )
    return cases


# --------------------------------------------------------------------------------------------- all
def load_example_cases(layout: SpecLayout) -> list[Case]:
    cases: list[Case] = []
    if layout.examples.is_dir():
        for p in sorted(layout.examples.glob("*.csv")):
            cases.extend(load_table(p))
    if layout.scenarios.is_dir():
        for p in sorted(list(layout.scenarios.glob("*.yaml")) + list(layout.scenarios.glob("*.yml"))):
            cases.extend(load_scenarios(p))
    return cases
