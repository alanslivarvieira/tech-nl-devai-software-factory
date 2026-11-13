"""Language-agnostic property-based tests: Hypothesis generates inputs, the driver executes them.

``properties.yaml``::

    properties:
      - name: greeting always ends with "!"
        ac_id: AC-2
        op: greet
        examples: 100                       # optional, default 50
        input:
          name: {type: string, alphabet: letters, min_size: 1, max_size: 20}
        allow_errors: ["Name must not be entirely whitespace"]   # optional: errors that are acceptable
        expect:                             # every predicate must hold for every generated input
          - {path: greeting, ends_with: "!"}
          - {path: greeting, contains_input: name}

Generators: string (alphabet: letters|digits|alnum|printable|ascii, min_size, max_size), integer (min, max),
float (min, max), boolean, one_of (values: [...]), list (of: <gen>, min_size, max_size), const (value).
Predicates (on ``path`` of the output; omit ``path`` for the whole output): equals, not_equals, starts_with,
ends_with, contains, contains_input (an input field's value appears), matches (regex), gte, lte,
length_lte, length_gte, type (string|integer|number|boolean|list|object|null), no_error (bool).
"""

from __future__ import annotations

import re
import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from .drivers import Driver, DriverError
from .loader import SpecError
from .model import MISSING
from .values import get_path

ALPHABETS = {
    "letters": string.ascii_letters,
    "digits": string.digits,
    "alnum": string.ascii_letters + string.digits,
    "ascii": string.printable,
    "printable": None,  # any unicode text except surrogates
}
PREDICATES = {
    "equals",
    "not_equals",
    "starts_with",
    "ends_with",
    "contains",
    "contains_input",
    "matches",
    "gte",
    "lte",
    "length_lte",
    "length_gte",
    "type",
    "no_error",
}
TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "list": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
    "null": lambda v: v is None,
}


@dataclass
class PropertySpec:
    name: str
    ac_id: str
    op: str
    input: dict[str, Any]
    expect: list[dict[str, Any]]
    allow_errors: list[str] = field(default_factory=list)
    examples: int = 50
    baseline: str = "new"


def strategy(gen: Any, where: str) -> st.SearchStrategy[Any]:
    if not isinstance(gen, dict) or "type" not in gen:
        raise SpecError(f"{where}: generator needs a 'type'")
    t = gen["type"]
    if t == "string":
        alpha = gen.get("alphabet", "printable")
        if alpha not in ALPHABETS:
            raise SpecError(f"{where}: unknown alphabet {alpha!r}")
        chars = ALPHABETS[alpha]
        alphabet = st.sampled_from(chars) if chars else st.characters(blacklist_categories=("Cs",))
        return st.text(alphabet=alphabet, min_size=gen.get("min_size", 0), max_size=gen.get("max_size", 30))
    if t == "integer":
        return st.integers(min_value=gen.get("min"), max_value=gen.get("max"))
    if t == "float":
        return st.floats(min_value=gen.get("min"), max_value=gen.get("max"), allow_nan=False, allow_infinity=False)
    if t == "boolean":
        return st.booleans()
    if t == "one_of":
        values = gen.get("values")
        if not values:
            raise SpecError(f"{where}: one_of needs values")
        return st.sampled_from(values)
    if t == "list":
        return st.lists(
            strategy(gen.get("of"), where + ".of"), min_size=gen.get("min_size", 0), max_size=gen.get("max_size", 10)
        )
    if t == "const":
        return st.just(gen.get("value"))
    raise SpecError(f"{where}: unknown generator type {t!r}")


def load_properties(path: Path) -> list[PropertySpec]:
    if not path.exists():
        return []
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out = []
    for i, p in enumerate(doc.get("properties") or []):
        where = f"properties.yaml#{i}"
        for key in ("name", "ac_id", "op", "expect"):
            if key not in p:
                raise SpecError(f"{where}: missing {key!r}")
        if not isinstance(p["expect"], list) or not p["expect"]:
            raise SpecError(f"{where}: 'expect' must be a non-empty list of predicates")
        for pred in p["expect"]:
            keys = set(pred) - {"path"}
            if len(keys) != 1 or not keys <= PREDICATES:
                raise SpecError(f"{where}: each predicate needs exactly one of {sorted(PREDICATES)}")
        for fname, gen in (p.get("input") or {}).items():
            strategy(gen, f"{where}.input.{fname}")  # validate eagerly
        out.append(
            PropertySpec(
                name=p["name"],
                ac_id=p["ac_id"],
                op=p["op"],
                input=p.get("input") or {},
                expect=p["expect"],
                allow_errors=list(p.get("allow_errors") or []),
                examples=int(p.get("examples", 50)),
                baseline=p.get("baseline", "new"),
            )
        )
    return out


def _holds(pred: dict[str, Any], output: Any, inp: dict[str, Any]) -> tuple[bool, str]:
    path = pred.get("path")
    value = get_path(output, path) if path else output
    ((name, arg),) = ((k, v) for k, v in pred.items() if k != "path")
    label = f"{path or '<output>'} {name} {arg!r}"
    if name == "no_error":
        return True, label
    if value is MISSING:
        return False, f"{label}: field missing"
    try:
        ok = {
            "equals": lambda: value == arg,
            "not_equals": lambda: value != arg,
            "starts_with": lambda: isinstance(value, str) and value.startswith(arg),
            "ends_with": lambda: isinstance(value, str) and value.endswith(arg),
            "contains": lambda: arg in value,
            "contains_input": lambda: str(get_path(inp, arg)) in str(value),
            "matches": lambda: isinstance(value, str) and re.search(arg, value) is not None,
            "gte": lambda: value >= arg,
            "lte": lambda: value <= arg,
            "length_lte": lambda: len(value) <= arg,
            "length_gte": lambda: len(value) >= arg,
            "type": lambda: TYPE_CHECKS[arg](value),
        }[name]()
    except (TypeError, KeyError):
        ok = False
    return bool(ok), f"{label}: got {value!r}"


def run_property(prop: PropertySpec, driver: Driver) -> tuple[bool, str]:
    """Return (ok, message). The message carries the shrunk counterexample on failure."""
    strategies = {k: strategy(v, prop.name) for k, v in prop.input.items()}
    failure: dict[str, Any] = {}

    @settings(
        max_examples=prop.examples,
        deadline=None,
        database=None,
        derandomize=True,
        suppress_health_check=list(HealthCheck),
        print_blob=False,
    )
    @given(st.fixed_dictionaries(strategies))
    def check(inp: dict[str, Any]) -> None:
        driver.reset()
        res = driver.call(prop.op, inp)
        if not res.ok:
            if res.error in prop.allow_errors:
                return
            failure.update(input=inp, reason=f"unexpected error: {res.error}")
            raise AssertionError(failure["reason"])
        for pred in prop.expect:
            ok, msg = _holds(pred, res.output, inp)
            if not ok:
                failure.update(input=inp, reason=msg)
                raise AssertionError(msg)

    try:
        check()
    except AssertionError:
        return False, f"falsified by input {failure.get('input')!r}: {failure.get('reason')}"
    except DriverError as exc:
        return False, f"driver error: {exc}"
    return True, ""
