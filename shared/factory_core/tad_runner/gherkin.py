"""Gherkin view of the tests-as-data contract.

* ``generate_features`` renders CSV tables / YAML scenarios as ``.feature`` files (deterministic, so the
  manifest hash is stable and humans can read the contract as BDD).
* ``load_feature_cases`` executes any ``.feature`` written in the fixed step vocabulary below — generated
  or hand-written — through the same driver. Step definitions are factory-owned; agents cannot add any.

Vocabulary. ``<op>`` is an interface operation. ``<fields>`` reads like ``title "buy milk"`` or
``a 1, b 2 and c 3`` (JSON values; the last one may be bare text). Table cells hold JSON or bare text;
``(absent)`` omits the field::

    Given I called <op> [with <fields> | with:]              -- a call that must succeed
    When I call <op> [with <fields> | with:]                 -- the call under test
    Then it returns <json> | <fields>                        -- exact output
    Then it returns:              (JSON doc string, or a table of rows)
    Then it returns <field>:      (table of rows)
    Then the result has <fields> | the result has:           -- only these output fields must match
    Then the <dotted.field> is <value>
    Then it fails with <message>
    Then it fails with an error containing <text>
    Then it succeeds
    And <op> returns … | has … | fails with … | succeeds     -- follow-up call without input (state check)
    And I query <op> [with <fields> | with:]                 -- follow-up call with input; assert with "it …"

The original phrasing (``"<op>" was called``, ``I call "<op>"``, ``I query "<op>"``, ``the output is …``,
``the output field "<path>" is …``, ``the call fails with error …``, ``the error contains …``,
``the call succeeds``) is still accepted.
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
INLINE_MAX = 90
CONTAINING = "an error containing "
_NAME = re.compile(r"^[A-Za-z_][\w.-]*$")
_FIELD_NAME = re.compile(r"(?P<key>[A-Za-z_][\w.-]*) ")
_SEP = re.compile(r"\s*,\s*|\s+and\s+")
_DECODER = json.JSONDecoder()


# ------------------------------------------------------------------------------------------- values
def _lit(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return MISSING


def _bare(text: str) -> Any:
    """Cell / trailing value: JSON if it parses, otherwise the text itself."""
    value = _loads(text)
    return text if value is MISSING else value


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _soft(value: Any) -> str:
    """Render a cell / trailing value; strings lose their quotes when that cannot change their meaning."""
    if (
        isinstance(value, str)
        and value
        and value == value.strip()
        and "\n" not in value
        and value != ABSENT
        and not value.startswith('"')
        and _loads(value) is MISSING
        and _parse_fields(f"x {value}") == {"x": value}
    ):
        return value
    return _lit(value)


def _message(text: str) -> str:
    if text and text == text.strip() and "\n" not in text and not text.startswith(('"', CONTAINING)):
        return text
    return _lit(text)


def _unmessage(text: str) -> str:
    if text.startswith('"') and isinstance(value := _loads(text), str):
        return value
    return text


def _parse_fields(text: str) -> dict[str, Any] | None:
    """``a 1, b "x" and c true`` -> {a: 1, b: "x", c: true}; None when the text is not a field list."""
    out: dict[str, Any] = {}
    pos = 0
    while m := _FIELD_NAME.match(text, pos):
        pos = m.end()
        try:
            value, end = _DECODER.raw_decode(text, pos)
        except ValueError:
            end = -1
        if end == len(text):
            set_path(out, m["key"], value)
            return out
        sep = _SEP.match(text, end) if end >= 0 else None
        if sep is None:
            raw = text[pos:]
            if raw != ABSENT:
                set_path(out, m["key"], _bare(raw))
            return out
        set_path(out, m["key"], value)
        pos = sep.end()
    return None


# ------------------------------------------------------------------------------------------- render
def _cell(text: str) -> str:
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", "\\n")


def _flat(prefix: str, value: Any, out: dict[str, Any]) -> None:
    if isinstance(value, dict) and not value and not prefix:
        return
    if isinstance(value, dict) and value:
        for k in value:
            _flat(f"{prefix}.{k}" if prefix else k, value[k], out)
    else:
        out[prefix] = value


def _flatten(value: dict[str, Any]) -> dict[str, Any]:
    flat: dict[str, Any] = {}
    _flat("", value, flat)
    return flat


def _table(rows: list[list[str]], indent: str) -> list[str]:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return [indent + "| " + " | ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)) + " |" for r in rows]


def _inline(flat: dict[str, Any]) -> str | None:
    if not flat or not all(_NAME.match(k) and _is_scalar(v) for k, v in flat.items()):
        return None
    parts = [f"{k} {_lit(v)}" for k, v in flat.items()]
    text = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    return text if len(text) <= INLINE_MAX else None


def _fields_table(flat: dict[str, Any], indent: str) -> list[str]:
    return _table([[_cell(k), _cell(_soft(v))] for k, v in flat.items()], indent)


def _records(value: Any) -> list[list[str]] | None:
    """A list of flat objects as a table with a header row, or None."""
    if not isinstance(value, list) or not value or not all(isinstance(r, dict) for r in value):
        return None
    flats = [_flatten(r) for r in value]
    cols: list[str] = []
    for f in flats:
        for k, v in f.items():
            if not _is_scalar(v):
                return None
            if k not in cols:
                cols.append(k)
    if not cols:
        return None
    return [[_cell(c) for c in cols]] + [[_cell(_soft(f[c])) if c in f else ABSENT for c in cols] for f in flats]


class _Keyword:
    """``Given``/``Then`` the first time, ``And`` afterwards."""

    def __init__(self, first: str) -> None:
        self.first, self.used = first, False

    def __call__(self) -> str:
        word = "And" if self.used else self.first
        self.used = True
        return word


def _call_lines(keyword: str, verb: str, op: str, inp: dict[str, Any], indent: str) -> list[str]:
    flat = _flatten(inp)
    head = f"{indent}{keyword} I {verb} {op}"
    if not flat:
        return [head]
    if (text := _inline(flat)) is not None:
        return [f"{head} with {text}"]
    return [f"{head} with:"] + _fields_table(flat, indent + "  ")


def _returns_lines(value: Any, head: str, indent: str) -> list[str]:
    if isinstance(value, dict) and (text := _inline(_flatten(value))) is not None:
        return [f"{head} {text}"]
    if (rows := _records(value)) is not None:
        return [f"{head}:"] + _table(rows, indent + "  ")
    if isinstance(value, dict) and len(value) == 1:
        ((key, inner),) = value.items()
        if _NAME.match(key) and "." not in key and (rows := _records(inner)) is not None:
            return [f"{head} {key}:"] + _table(rows, indent + "  ")
    text = _lit(value)
    if len(text) <= 80:
        return [f"{head} {text}"]
    body = json.dumps(value, ensure_ascii=False, indent=2).splitlines()
    return [f"{head}:", f'{indent}  """json'] + [f"{indent}  {b}" for b in body] + [f'{indent}  """']


def _assert_lines(e: Expect, op: str | None, kw: _Keyword, indent: str) -> list[str]:
    """Assertion on the last call (``it …``) or, with ``op``, a follow-up call and its assertion in one step."""
    it = op or "it"
    if e.error is not None:
        return [f"{indent}{kw()} {it} fails with {_message(e.error)}"]
    if e.error_contains is not None:
        return [f"{indent}{kw()} {it} fails with {CONTAINING}{_message(e.error_contains)}"]
    if e.output is not MISSING:
        return _returns_lines(e.output, f"{indent}{kw()} {it} returns", indent)
    if e.output_subset is not None:
        has = f"{op} has" if op else "the result has"
        flat = _flatten(e.output_subset)
        if (text := _inline(flat)) is not None:
            return [f"{indent}{kw()} {has} {text}"]
        return [f"{indent}{kw()} {has}:"] + _fields_table(flat, indent + "  ")
    return [f"{indent}{kw()} {it} succeeds"]


def _shape(case: Case) -> tuple[Any, ...]:
    step = case.steps[0]
    e = step.expect or Expect(no_error=True)
    if e.error is not None:
        kind: tuple[Any, ...] = ("error",)
    elif e.error_contains is not None:
        kind = ("error_contains",)
    elif e.output is not MISSING:
        kind = ("output",)
    elif e.output_subset is not None:
        kind = ("subset", tuple(_flatten(e.output_subset)))
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
        in_cols = list(dict.fromkeys(k for c in group for k in _flatten(c.steps[0].input)))
        if kind[0] == "subset":
            out_cols = list(kind[1])
        else:
            out_cols = {"error": ["error"], "error_contains": ["error"], "output": ["output"]}.get(kind[0], [])
        # Column headers double as placeholders; prefix them only when an input and an output share a name.
        clash = len({"case", *in_cols, *out_cols}) != 1 + len(in_cols) + len(out_cols)
        in_names = [f"in.{c}" if clash else c for c in in_cols]
        out_names = [f"out.{c}" if clash and kind[0] == "subset" else c for c in out_cols]

        if baseline == "existing":
            lines.append("  @existing")
        lines.append("  Scenario Outline: <case>")
        if len(in_cols) == 1 and _NAME.match(in_cols[0]):
            lines.append(f"    When I call {op} with {in_cols[0]} <{in_names[0]}>")
        elif in_cols:
            lines.append(f"    When I call {op} with:")
            lines += _table([[_cell(c), f"<{n}>"] for c, n in zip(in_cols, in_names, strict=True)], "      ")
        else:
            lines.append(f"    When I call {op}")
        if kind[0] == "error":
            lines.append(f"    Then it fails with <{out_names[0]}>")
        elif kind[0] == "error_contains":
            lines.append(f"    Then it fails with {CONTAINING}<{out_names[0]}>")
        elif kind[0] == "output":
            lines.append(f"    Then it returns <{out_names[0]}>")
        elif kind[0] == "subset" and all(_NAME.match(p) and p != "output" for p in out_cols):
            for i, (p, n) in enumerate(zip(out_cols, out_names, strict=True)):
                lines.append(f"    {'Then' if i == 0 else 'And'} the {p} is <{n}>")
        elif kind[0] == "subset":
            lines.append("    Then the result has:")
            lines += _table([[_cell(p), f"<{n}>"] for p, n in zip(out_cols, out_names, strict=True)], "      ")
        else:
            lines.append("    Then it succeeds")

        by_ac: dict[str, list[Case]] = defaultdict(list)
        for c in group:
            by_ac[c.ac_id].append(c)
        for ac in sorted(by_ac):
            lines += ["", f"    @{ac}", "    Examples:"]
            rows = [["case", *in_names, *out_names]]
            for c in by_ac[ac]:
                rows.append(
                    [_cell(c.name)]
                    + [_cell(_example_in(c, col)) for col in in_cols]
                    + [_cell(_example_out(c, kind[0], col)) for col in out_cols]
                )
            lines += _table(rows, "      ")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _example_in(c: Case, col: str) -> str:
    flat = _flatten(c.steps[0].input)
    return _soft(flat[col]) if col in flat else ABSENT


def _example_out(c: Case, kind: str, col: str) -> str:
    e = c.steps[0].expect or Expect()
    if kind == "error":
        return _message(e.error or "")
    if kind == "error_contains":
        return _message(e.error_contains or "")
    if kind == "output":
        return _lit(e.output)
    return _soft(_flatten(e.output_subset or {})[col])


def render_scenario_feature(cases: list[Case], title: str, source: str) -> str:
    lines = [
        f"# GENERATED from {source} by the factory tests-as-data runner. Edit the data file, not this one.",
        f"Feature: {title}",
        "",
    ]
    ind = "    "
    for c in cases:
        tags = [f"@{c.ac_id}"] + (["@existing"] if c.baseline == "existing" else [])
        lines += ["  " + " ".join(tags), f"  Scenario: {c.name}"]
        given, then = _Keyword("Given"), _Keyword("Then")
        for s in c.steps:
            e = s.expect or Expect(no_error=True)
            if s.phase == "given":
                lines += _call_lines(given(), "called", s.op, s.input, ind)
            elif s.phase == "when":
                lines += _call_lines("When", "call", s.op, s.input, ind)
                if e.describe() != {"no_error": True}:
                    lines += _assert_lines(e, None, then, ind)
            elif _flatten(s.input) or s.op == "it":
                lines += _call_lines(then(), "query", s.op, s.input, ind)
                lines += _assert_lines(e, None, then, ind)
            else:
                lines += _assert_lines(e, s.op, then, ind)
        if not then.used:
            lines.append(f"{ind}Then it succeeds")
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
_OP = r"[a-z_][a-z0-9_]*"
_CALL = re.compile(rf"^I (?P<verb>called|call|query) (?P<op>{_OP})(?: with (?P<fields>.+)|(?P<table> with:))?$")
_PHASE = {"called": "given", "call": "when", "query": "then"}
_FOLLOW_UP = re.compile(rf"^(?P<op>{_OP}) (?P<rest>(?:returns|has)(?: .+|:)|fails with .+|succeeds)$")
_RETURNS_KEY = re.compile(r"^it returns (?P<key>[A-Za-z_][\w.-]*):$")
_RETURNS = re.compile(r"^it returns (?P<value>.+)$")
_HAS = re.compile(r"^the result has (?P<fields>.+)$")
_FAILS_CONTAINING = re.compile(rf"^it fails with {CONTAINING}(?P<msg>.+)$")
_FAILS = re.compile(r"^it fails with (?P<msg>.+)$")
_IS = re.compile(r"^the (?P<path>[A-Za-z_][\w.-]*) is (?P<value>.+)$")

_LEGACY_CALL = re.compile(
    r'^(?:"(?P<g>[^"]+)" was called|I call "(?P<w>[^"]+)"|I query "(?P<q>[^"]+)")(?P<table> with:)?$'
)
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


def _table_rows(arg: dict[str, Any]) -> list[list[str]] | None:
    rows = (arg.get("dataTable") or {}).get("rows")
    return [[c["value"] for c in r["cells"]] for r in rows] if rows else None


def _read_fields(rows: list[list[str]], where: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for cells in rows:
        if len(cells) != 2:
            raise SpecError(f"{where}: field tables have exactly two columns (field | value)")
        if cells[1] != ABSENT:
            set_path(out, cells[0], _bare(cells[1]))
    return out


def _read_records(rows: list[list[str]]) -> list[dict[str, Any]]:
    header, *body = rows
    records = []
    for cells in body:
        rec: dict[str, Any] = {}
        for k, v in zip(header, cells, strict=True):
            if v != ABSENT:
                set_path(rec, k, _bare(v))
        records.append(rec)
    return records


def _fields(text: str, where: str) -> dict[str, Any]:
    parsed = _parse_fields(text)
    if parsed is None:
        raise SpecError(f"{where}: cannot read fields from {text!r} (expected e.g. `name \"Joe\" and age 3`)")
    return parsed


def _call_step(text: str, arg: dict[str, Any], where: str) -> Step | None:
    if m := _LEGACY_CALL.match(text):
        op = m["g"] or m["w"] or m["q"]
        phase = "given" if m["g"] else "when" if m["w"] else "then"
        inline = None
    elif m := _CALL.match(text):
        op, phase, inline = m["op"], _PHASE[m["verb"]], m["fields"]
    else:
        return None
    inp: dict[str, Any] = {}
    if m["table"]:
        rows = _table_rows(arg)
        if not rows:
            raise SpecError(f"{where}: '{text}' needs a data table")
        inp = _read_fields(rows, where)
    elif inline:
        inp = _fields(inline, where)
    return Step(op=op, input=inp, expect=Expect(no_error=True), phase=phase)  # type: ignore[arg-type]


def _merge(subset: dict[str, Any] | None, fields: dict[str, Any]) -> dict[str, Any]:
    out = subset or {}
    for path, value in _flatten(fields).items():
        set_path(out, path, value)
    return out


def _apply_assertion(text: str, arg: dict[str, Any], e: Expect, where: str) -> None:
    doc = (arg.get("docString") or {}).get("content")
    rows = _table_rows(arg)
    if _OUT_DOC.match(text):
        if doc is None:
            raise SpecError(f"{where}: 'the output is:' needs a doc string")
        e.output = _json(doc, where)
    elif mm := _OUT.match(text):
        e.output = _json(mm["json"], where)
    elif mm := _FIELD.match(text):
        e.output_subset = _merge(e.output_subset, {mm["path"]: _json(mm["json"], where)})
    elif mm := _ERR.match(text):
        e.error = _json(mm["json"], where)
    elif mm := _ERR_IN.match(text):
        e.error_contains = _json(mm["json"], where)
    elif _OK.match(text) or text == "it succeeds":
        e.no_error = True
    elif text == "it returns:":
        if doc is not None:
            e.output = _json(doc, where)
        elif rows:
            e.output = _read_records(rows)
        else:
            raise SpecError(f"{where}: 'it returns:' needs a JSON doc string or a table")
    elif rows and (mm := _RETURNS_KEY.match(text)):
        out: dict[str, Any] = {}
        set_path(out, mm["key"], _read_records(rows))
        e.output = out
    elif mm := _RETURNS.match(text):
        value = _loads(mm["value"])
        e.output = _fields(mm["value"], where) if value is MISSING else value
    elif text == "the result has:":
        if not rows:
            raise SpecError(f"{where}: 'the result has:' needs a table")
        e.output_subset = _merge(e.output_subset, _read_fields(rows, where))
    elif mm := _HAS.match(text):
        e.output_subset = _merge(e.output_subset, _fields(mm["fields"], where))
    elif mm := _FAILS_CONTAINING.match(text):
        e.error_contains = _unmessage(mm["msg"])
    elif mm := _FAILS.match(text):
        e.error = _unmessage(mm["msg"])
    elif mm := _IS.match(text):
        e.output_subset = _merge(e.output_subset, {mm["path"]: _bare(mm["value"])})
    else:
        raise SpecError(f"{where}: step not in the factory vocabulary: '{text}'")


def _pickle_to_case(pickle: dict[str, Any], suite: str, source: str) -> Case:
    where = f"{source}:{pickle['name']}"
    steps: list[Step] = []
    asserted: set[int] = set()
    for st in pickle["steps"]:
        text = st["text"].strip()
        arg = st.get("argument") or {}
        if call := _call_step(text, arg, where):
            steps.append(call)
            continue
        if (m := _FOLLOW_UP.match(text)) and m["op"] != "it":
            steps.append(Step(op=m["op"], input={}, expect=Expect(no_error=True), phase="then"))
            text = ("the result " if m["rest"].startswith("has") else "it ") + m["rest"]
        if not steps:
            raise SpecError(f"{where}: assertion '{text}' before any call")
        last = steps[-1]
        if last.phase == "given":
            raise SpecError(f"{where}: assertions must follow a When/query call, not a Given")
        e = last.expect if id(last) in asserted and last.expect else Expect()
        asserted.add(id(last))
        _apply_assertion(text, arg, e, where)
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
