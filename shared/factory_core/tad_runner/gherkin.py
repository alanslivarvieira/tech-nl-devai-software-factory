"""Gherkin view of the tests-as-data contract.

* ``generate_features`` renders CSV tables / YAML scenarios as ``.feature`` files (deterministic, so the
  manifest hash is stable and humans can read the contract as BDD).
* ``load_feature_cases`` executes any ``.feature`` written in the fixed step vocabulary below — generated
  or hand-written — through the same driver. Step definitions are factory-owned; agents cannot add any.

Vocabulary (values are JSON literals; ``(absent)`` in a table omits the field)::

    Given "<op>" was called [with:]          -- a call that must succeed
    When I call "<op>" [with:]                -- the call under test
    Then/And I query "<op>" [with:]           -- a follow-up call (state check)
    Then the output is <json> | the output is:  (doc string JSON)
    Then the output field "<dotted.path>" is <json>
    Then the call fails with error <json-string>
    Then the error contains <json-string>
    Then the call succeeds
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from gherkin.parser import Parser
from gherkin.pickles.compiler import Compiler

from .layout import SpecLayout
from .loader import SpecError, load_scenarios, load_table
from .model import MISSING, Case, Expect, Step
from .values import set_path

ABSENT = "(absent)"
GENERATED_SUFFIXES = (".table.feature", ".scenario.feature")


# ------------------------------------------------------------------------------------------- render
def _lit(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _cell(text: str) -> str:
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", "\\n")


def _flat(prefix: str, value: Any, out: dict[str, Any]) -> None:
    if isinstance(value, dict) and not value and not prefix:
        return
    if isinstance(value, dict) and value:
        for k in sorted(value):
            _flat(f"{prefix}.{k}" if prefix else k, value[k], out)
    else:
        out[prefix] = value


def _table(rows: list[list[str]], indent: str) -> list[str]:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return [indent + "| " + " | ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)) + " |" for r in rows]


def _call_lines(keyword: str, phrase: str, op: str, inp: dict[str, Any], indent: str) -> list[str]:
    flat: dict[str, Any] = {}
    _flat("", inp, flat)
    if not flat:
        return [f"{indent}{keyword} {phrase.format(op=op)}"]
    lines = [f"{indent}{keyword} {phrase.format(op=op)} with:"]
    lines += _table([[_cell(k), _cell(_lit(v))] for k, v in flat.items()], indent + "  ")
    return lines


def _assert_lines(e: Expect, indent: str, first_kw: str = "Then") -> list[str]:
    kw = [first_kw]

    def k() -> str:
        word = kw[0]
        kw[0] = "And"
        return word

    if e.error is not None:
        return [f"{indent}{k()} the call fails with error {_lit(e.error)}"]
    if e.error_contains is not None:
        return [f"{indent}{k()} the error contains {_lit(e.error_contains)}"]
    if e.output is not MISSING:
        text = _lit(e.output)
        if len(text) <= 80:
            return [f"{indent}{k()} the output is {text}"]
        body = json.dumps(e.output, ensure_ascii=False, sort_keys=True, indent=2).splitlines()
        return (
            [f"{indent}{k()} the output is:", f'{indent}  """json']
            + [f"{indent}  {b}" for b in body]
            + [f'{indent}  """']
        )
    if e.output_subset is not None:
        flat: dict[str, Any] = {}
        _flat("", e.output_subset, flat)
        return [f'{indent}{k()} the output field "{p}" is {_lit(v)}' for p, v in flat.items()]
    return [f"{indent}{k()} the call succeeds"]


def _shape(case: Case) -> tuple[Any, ...]:
    step = case.steps[0]
    e = step.expect or Expect(no_error=True)
    flat_in: dict[str, Any] = {}
    _flat("", step.input, flat_in)
    if e.error is not None:
        kind: tuple[Any, ...] = ("error",)
    elif e.error_contains is not None:
        kind = ("error_contains",)
    elif e.output is not MISSING:
        kind = ("output",)
    elif e.output_subset is not None:
        flat_out: dict[str, Any] = {}
        _flat("", e.output_subset, flat_out)
        kind = ("subset", tuple(sorted(flat_out)))
    else:
        kind = ("ok",)
    return (step.op, kind, case.baseline)


def render_table_feature(cases: list[Case], title: str, source: str) -> str:
    lines = [
        f"# GENERATED from {source} by the factory tests-as-data runner. Edit the data file, not this one.",
        f"Feature: {title}",
        "",
    ]
    groups: dict[tuple[Any, ...], list[Case]] = defaultdict(list)
    for c in cases:
        groups[_shape(c)].append(c)
    for (op, kind, baseline), group in groups.items():
        in_cols = sorted({k for c in group for k in _flat_keys(c.steps[0].input)})
        tags = "  @existing" if baseline == "existing" else ""
        if tags:
            lines.append(tags)
        label = {"error": "rejects", "error_contains": "rejects", "ok": "accepts"}.get(kind[0], "returns")
        lines.append(f"  Scenario Outline: {op} {label} — <case>")
        if in_cols:
            lines.append(f'    When I call "{op}" with:')
            lines += _table([[_cell(c), f"<in.{c}>"] for c in in_cols], "      ")
        else:
            lines.append(f'    When I call "{op}"')
        if kind[0] == "error":
            lines.append("    Then the call fails with error <error>")
            out_cols = ["error"]
        elif kind[0] == "error_contains":
            lines.append("    Then the error contains <error_contains>")
            out_cols = ["error_contains"]
        elif kind[0] == "output":
            lines.append("    Then the output is <output>")
            out_cols = ["output"]
        elif kind[0] == "subset":
            out_cols = [f"out.{p}" for p in kind[1]]
            for i, p in enumerate(kind[1]):
                lines.append(f'    {"Then" if i == 0 else "And"} the output field "{p}" is <out.{p}>')
        else:
            lines.append("    Then the call succeeds")
            out_cols = []
        by_ac: dict[str, list[Case]] = defaultdict(list)
        for c in group:
            by_ac[c.ac_id].append(c)
        for ac in sorted(by_ac):
            lines += ["", f"    @{ac}", "    Examples:"]
            header = ["case"] + [f"in.{c}" for c in in_cols] + out_cols
            rows = [header]
            for c in by_ac[ac]:
                rows.append(
                    [_cell(c.name)]
                    + [_cell(_example_in(c, col)) for col in in_cols]
                    + [_cell(_example_out(c, col)) for col in out_cols]
                )
            lines += _table(rows, "      ")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _flat_keys(inp: dict[str, Any]) -> list[str]:
    flat: dict[str, Any] = {}
    _flat("", inp, flat)
    return list(flat)


def _example_in(c: Case, col: str) -> str:
    flat: dict[str, Any] = {}
    _flat("", c.steps[0].input, flat)
    return _lit(flat[col]) if col in flat else ABSENT


def _example_out(c: Case, col: str) -> str:
    e = c.steps[0].expect or Expect()
    if col == "error":
        return _lit(e.error)
    if col == "error_contains":
        return _lit(e.error_contains)
    if col == "output":
        return _lit(e.output)
    flat: dict[str, Any] = {}
    _flat("", e.output_subset or {}, flat)
    return _lit(flat[col[4:]])


def render_scenario_feature(cases: list[Case], title: str, source: str) -> str:
    lines = [
        f"# GENERATED from {source} by the factory tests-as-data runner. Edit the data file, not this one.",
        f"Feature: {title}",
        "",
    ]
    for c in cases:
        tags = [f"@{c.ac_id}"] + (["@existing"] if c.baseline == "existing" else [])
        lines += ["  " + " ".join(tags), f"  Scenario: {c.name}"]
        for s in c.steps:
            if s.phase == "given":
                lines += _call_lines("Given", '"{op}" was called', s.op, s.input, "    ")
            elif s.phase == "when":
                lines += _call_lines("When", 'I call "{op}"', s.op, s.input, "    ")
                if s.expect and s.expect.describe() != {"no_error": True}:
                    lines += _assert_lines(s.expect, "    ")
            else:
                lines += _call_lines("And", 'I query "{op}"', s.op, s.input, "    ")
                lines += _assert_lines(s.expect or Expect(no_error=True), "    ", first_kw="And")
        if (
            not any(s.phase == "then" for s in c.steps)
            and c.steps[-1].expect
            and c.steps[-1].expect.describe() == {"no_error": True}
        ):
            lines.append("    Then the call succeeds")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def generate_features(layout: SpecLayout, title: str) -> dict[str, str]:
    """Return {relative feature path: content} for every data file (does not write)."""
    out: dict[str, str] = {}
    if layout.examples.is_dir():
        for p in sorted(layout.examples.glob("*.csv")):
            out[f"{p.stem}.table.feature"] = render_table_feature(load_table(p), title, f"examples/{p.name}")
    if layout.scenarios.is_dir():
        for p in sorted(list(layout.scenarios.glob("*.yaml")) + list(layout.scenarios.glob("*.yml"))):
            out[f"{p.stem}.scenario.feature"] = render_scenario_feature(load_scenarios(p), title, f"scenarios/{p.name}")
    return out


def write_features(layout: SpecLayout, title: str) -> list[Path]:
    layout.features.mkdir(parents=True, exist_ok=True)
    for old in layout.features.glob("*.feature"):
        if old.name.endswith(GENERATED_SUFFIXES):
            old.unlink()
    written = []
    for name, text in generate_features(layout, title).items():
        path = layout.features / name
        path.write_text(text, encoding="utf-8", newline="\n")
        written.append(path)
    return written


def features_in_sync(layout: SpecLayout, title: str) -> list[str]:
    """Names of generated feature files that are missing or differ from their data source."""
    problems = []
    expected = generate_features(layout, title)
    for name, text in expected.items():
        path = layout.features / name
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            problems.append(name)
    if layout.features.is_dir():
        for p in layout.features.glob("*.feature"):
            if p.name.endswith(GENERATED_SUFFIXES) and p.name not in expected:
                problems.append(p.name)
    return sorted(problems)


# ------------------------------------------------------------------------------------------ execute
_CALL = re.compile(r'^(?:"(?P<g>[^"]+)" was called|I call "(?P<w>[^"]+)"|I query "(?P<q>[^"]+)")(?P<with> with:)?$')
_OUT = re.compile(r"^the output is (?P<json>.+)$")
_OUT_DOC = re.compile(r"^the output is:$")
_FIELD = re.compile(r'^the output field "(?P<path>[^"]+)" is (?P<json>.+)$')
_ERR = re.compile(r"^the call fails with error (?P<json>.+)$")
_ERR_IN = re.compile(r"^the error contains (?P<json>.+)$")
_OK = re.compile(r"^the call succeeds$")


def _json(text: str, where: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise SpecError(f"{where}: value is not a JSON literal: {text!r}") from exc


def _pickle_to_case(pickle: dict[str, Any], suite: str, source: str) -> Case:
    where = f"{source}:{pickle['name']}"
    steps: list[Step] = []
    asserted: set[int] = set()
    for st in pickle["steps"]:
        text = st["text"].strip()
        arg = st.get("argument") or {}
        m = _CALL.match(text)
        if m:
            op = m.group("g") or m.group("w") or m.group("q")
            phase = "given" if m.group("g") else "when" if m.group("w") else "then"
            inp: dict[str, Any] = {}
            if m.group("with"):
                rows = (arg.get("dataTable") or {}).get("rows") or []
                if not rows:
                    raise SpecError(f"{where}: '{text}' needs a data table")
                for row in rows:
                    cells = [c["value"] for c in row["cells"]]
                    if len(cells) != 2:
                        raise SpecError(f"{where}: input tables have exactly two columns (field | json)")
                    if cells[1] != ABSENT:
                        set_path(inp, cells[0], _json(cells[1], where))
            steps.append(Step(op=op, input=inp, expect=Expect(no_error=True), phase=phase))  # type: ignore[arg-type]
            continue
        if not steps:
            raise SpecError(f"{where}: assertion '{text}' before any call")
        last = steps[-1]
        if last.phase == "given":
            raise SpecError(f"{where}: assertions must follow a When/query call, not a Given")
        e = last.expect if id(last) in asserted and last.expect else Expect()
        asserted.add(id(last))
        if mm := _OUT_DOC.match(text):
            doc = (arg.get("docString") or {}).get("content")
            if doc is None:
                raise SpecError(f"{where}: 'the output is:' needs a doc string")
            e.output = _json(doc, where)
        elif mm := _OUT.match(text):
            e.output = _json(mm.group("json"), where)
        elif mm := _FIELD.match(text):
            subset = e.output_subset or {}
            set_path(subset, mm.group("path"), _json(mm.group("json"), where))
            e.output_subset = subset
        elif mm := _ERR.match(text):
            e.error = _json(mm.group("json"), where)
        elif mm := _ERR_IN.match(text):
            e.error_contains = _json(mm.group("json"), where)
        elif _OK.match(text):
            e.no_error = True
        else:
            raise SpecError(f"{where}: step not in the factory vocabulary: '{text}'")
        last.expect = e
    if not asserted:
        raise SpecError(f"{where}: scenario has no assertion (Then step) — not allowed")
    tags = [t["name"].lstrip("@") for t in pickle.get("tags", [])]
    ac = next((t for t in tags if re.fullmatch(r"AC-\d+", t)), "")
    return Case(
        kind="gherkin",
        suite=suite,
        name=pickle["name"],
        ac_id=ac,
        steps=steps,
        baseline="existing" if "existing" in tags else "new",
        source=where,
    )


def parse_feature(text: str, suite: str, source: str) -> list[Case]:
    try:
        doc = Parser().parse(text)
    except Exception as exc:
        raise SpecError(f"{source}: {exc}") from exc
    doc["uri"] = source
    return [_pickle_to_case(p, suite, source) for p in Compiler().compile(doc)]


def load_feature_cases(layout: SpecLayout) -> list[Case]:
    cases: list[Case] = []
    if layout.features.is_dir():
        for p in sorted(layout.features.glob("*.feature")):
            cases.extend(parse_feature(p.read_text(encoding="utf-8"), p.name.split(".")[0], p.name))
    return cases
