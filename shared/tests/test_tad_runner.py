"""Tests-as-data runner: parsing, comparison, Gherkin round-trip, execution, freeze check, properties."""

from __future__ import annotations

from pathlib import Path

import pytest

from factory_core import pipeline
from factory_core.tad_runner.compare import check
from factory_core.tad_runner.drivers import CallResult
from factory_core.tad_runner.gherkin import (
    parse_feature,
    render_scenario_feature,
    render_table_feature,
    write_features,
)
from factory_core.tad_runner.loader import SpecError, parse_scenarios, parse_table
from factory_core.tad_runner.model import MISSING, Expect
from factory_core.tad_runner.runner import freeze_check, run, validate
from factory_core.testing import GREETER_BUGGY, GREETER_GOOD

TABLE = """case,ac_id,baseline,in.name,in.age:int,in.vip:bool,out.greeting,error
plain,AC-1,existing,,,,"Hello, world!",
named,AC-2,,Joe,42,true,"Hello, Joe!",
blank,AC-3,," ",,,,Name must not be entirely whitespace
nullname,AC-3,,<null>,,,,boom
empty,AC-3,,<empty>,,,,boom
"""


# ------------------------------------------------------------------------------------------- parsing
def test_table_types_and_sentinels():
    cases = {c.name: c for c in parse_table(TABLE, suite="greet")}
    assert cases["plain"].steps[0].input == {} and cases["plain"].baseline == "existing"
    assert cases["named"].steps[0].input == {"name": "Joe", "age": 42, "vip": True}
    assert cases["named"].steps[0].expect.output_subset == {"greeting": "Hello, Joe!"}
    assert cases["blank"].steps[0].input == {"name": " "}, "whitespace in data cells is significant"
    assert cases["blank"].steps[0].expect.error == "Name must not be entirely whitespace"
    assert cases["nullname"].steps[0].input == {"name": None}
    assert cases["empty"].steps[0].input == {"name": ""}
    assert all(c.steps[0].op == "greet" for c in cases.values()), "op defaults to the file stem"


@pytest.mark.parametrize(
    "text,msg",
    [
        ("ac_id,in.x\nAC-1,1\n", "case"),
        ("case,ac_id,in.x:weird\na,AC-1,1\n", "bad type"),
        ("case,ac_id,in.x\na,AC-1,1\na,AC-1,2\n", "duplicate"),
        ("case,ac_id,out.y,error\na,AC-1,1,bad\n", "both an error and an output"),
        ("case,ac_id,in.n:int\na,AC-1,notanint\n", "column in.n"),
    ],
)
def test_table_rejects_malformed(text, msg):
    with pytest.raises(SpecError, match=msg):
        parse_table(text, suite="s")


def test_scenarios_require_assertions():
    with pytest.raises(SpecError, match="then"):
        parse_scenarios("cases:\n  - name: a\n    ac_id: AC-1\n    when: {op: x}\n", "s")
    with pytest.raises(SpecError, match="must state"):
        parse_scenarios("cases:\n  - name: a\n    ac_id: AC-1\n    when: {op: x}\n    then: {state: [{op: y}]}\n", "s")


def test_scenario_steps():
    text = """suite: todo
cases:
  - name: toggle
    ac_id: AC-1
    given: [{op: add, input: {title: milk}}]
    when: {op: toggle, input: {title: milk}}
    then:
      output_subset: {done: true}
      state: [{op: list, output: [{title: milk, done: true}]}]
"""
    (case,) = parse_scenarios(text, "x")
    assert [s.phase for s in case.steps] == ["given", "when", "then"]
    assert case.steps[0].expect.no_error
    assert case.steps[1].expect.output_subset == {"done": True}
    assert case.steps[2].expect.output == [{"title": "milk", "done": True}]


# ---------------------------------------------------------------------------------------- comparing
@pytest.mark.parametrize(
    "expect,result,ok",
    [
        (Expect(output=1), CallResult(output=True), False),  # bool is not int
        (Expect(output=True), CallResult(output=True), True),
        (Expect(output=1.0), CallResult(output=1), True),
        (Expect(output={"a": [1, 2]}), CallResult(output={"a": [1, 2]}), True),
        (Expect(output={"a": [1, 2]}), CallResult(output={"a": [2, 1]}), False),
        (Expect(output_subset={"a": {"b": 1}}), CallResult(output={"a": {"b": 1, "c": 2}}), True),
        (Expect(output_subset={"a": 1}), CallResult(output={}), False),
        (Expect(error="x"), CallResult(output=1), False),
        (Expect(error="x"), CallResult(error="x"), True),
        (Expect(error_contains="bad"), CallResult(error="very bad input"), True),
        (Expect(no_error=True), CallResult(error="boom"), False),
        (Expect(output_subset={"g": "a"}), CallResult(error="boom"), False),
    ],
)
def test_check(expect, result, ok):
    assert check(expect, result)[0] is ok


def test_check_produces_readable_diff():
    ok, msg, diff = check(
        Expect(output_subset={"greeting": "Hello, Ann!"}), CallResult(output={"greeting": "Hello,   Ann  !"})
    )
    assert not ok and "-greeting: Hello, Ann!" in diff and "+greeting: Hello,   Ann  !" in diff


# ------------------------------------------------------------------------------------------- gherkin
def test_gherkin_roundtrip_preserves_semantics():
    cases = parse_table(TABLE, suite="greet")
    feature = render_table_feature(cases, "Greeting", "examples/greet.csv")
    back = {c.name.split("— ")[-1]: c for c in parse_feature(feature, "greet", "greet.feature")}
    for c in cases:
        g = back[c.name]
        assert g.ac_id == c.ac_id and g.baseline == c.baseline
        assert g.steps[0].op == c.steps[0].op and g.steps[0].input == c.steps[0].input
        assert g.steps[0].expect.describe() == c.steps[0].expect.describe()


def test_gherkin_rejects_unknown_vocabulary_and_missing_assertions():
    bad_step = 'Feature: f\n  @AC-1\n  Scenario: s\n    When I call "greet"\n    Then it looks fine\n'
    with pytest.raises(SpecError, match="vocabulary"):
        parse_feature(bad_step, "f", "f.feature")
    no_then = 'Feature: f\n  @AC-1\n  Scenario: s\n    When I call "greet"\n'
    with pytest.raises(SpecError, match="no assertion"):
        parse_feature(no_then, "f", "f.feature")


SCENARIOS = """suite: todo
cases:
  - name: toggling marks a todo as done
    ac_id: AC-2
    given:
      - {op: add, input: {title: buy milk}}
      - {op: add, input: {title: walk the dog}}
    when: {op: toggle, input: {title: buy milk}}
    then:
      output: {title: buy milk, done: true}
      state:
        - op: list
          output: {items: [{title: buy milk, done: true}, {title: walk the dog, done: false}]}
        - {op: remaining, output: {count: 1}}
  - name: toggling twice reactivates the todo
    ac_id: AC-2
    given: [{op: add, input: {title: buy milk, tags: [home, 1]}}]
    when: {op: toggle, input: {title: buy milk}}
    then:
      output_subset: {done: false, meta: {by: "  me  "}}
      state:
        - {op: find, input: {title: buy milk}, output: [{title: buy milk}, {}]}
  - name: unknown todo is rejected
    ac_id: AC-4
    when: {op: toggle, input: {title: '42'}}
    then:
      error: 'No todo titled "42"'
      state: [{op: remaining, error_contains: "an error containing x"}]
  - name: clearing succeeds
    ac_id: AC-5
    when: {op: clear}
    then: {no_error: true}
"""


def test_gherkin_scenarios_read_fluently():
    feature = render_scenario_feature(parse_scenarios(SCENARIOS, "todo"), "Todos", "scenarios/todo.yaml")
    assert (
        "  Scenario: toggling marks a todo as done\n"
        '    Given I called add with title "buy milk"\n'
        '    And I called add with title "walk the dog"\n'
        '    When I call toggle with title "buy milk"\n'
        '    Then it returns title "buy milk" and done true\n'
        "    And list returns items:\n"
        "      | title        | done  |\n"
        "      | buy milk     | true  |\n"
        "      | walk the dog | false |\n"
        "    And remaining returns count 1\n"
    ) in feature
    assert '    When I call clear\n    Then it succeeds\n' in feature


def test_gherkin_scenarios_roundtrip_preserves_semantics():
    cases = parse_scenarios(SCENARIOS, "todo")
    back = {c.name: c for c in parse_feature(render_scenario_feature(cases, "T", "s.yaml"), "todo", "s.feature")}
    for c in cases:
        g = back[c.name]
        assert g.ac_id == c.ac_id
        assert [(s.op, s.input, s.phase) for s in g.steps] == [(s.op, s.input, s.phase) for s in c.steps]
        assert [s.expect.describe() for s in g.steps] == [s.expect.describe() for s in c.steps]


def test_gherkin_table_reads_fluently():
    feature = render_table_feature(parse_table(TABLE, suite="greet"), "Greeting", "examples/greet.csv")
    assert "    Then it fails with <error>\n" in feature
    assert '| blank    | " "  | Name must not be entirely whitespace |' in feature
    single = "case,ac_id,in.name,out.greeting\nnamed,AC-2,Joe,\"Hello, Joe!\"\n"
    feature = render_table_feature(parse_table(single, suite="greet"), "Greeting", "examples/greet.csv")
    assert "    When I call greet with name <name>\n    Then the greeting is <greeting>\n" in feature
    assert "| named | Joe  | Hello, Joe! |" in feature


def test_gherkin_hand_written_steps():
    text = """Feature: f
  @AC-1
  Scenario: s
    Given I called add with:
      | title | buy milk |
      | due   | (absent) |
    When I call find with title "buy milk", limit 2 and open true
    Then the result has count 1
    And the first.title is buy milk
    And it fails with an error containing nothing
"""
    (case,) = parse_feature(text, "f", "f.feature")
    assert case.steps[0].input == {"title": "buy milk"}
    assert case.steps[1].input == {"title": "buy milk", "limit": 2, "open": True}
    assert case.steps[1].expect.output_subset == {"count": 1, "first": {"title": "buy milk"}}
    assert case.steps[1].expect.error_contains == "nothing"


# ----------------------------------------------------------------------------------------- execution
def _implement(repo: Path, code: str) -> None:
    (repo / "src" / "greeter" / "__init__.py").write_text(code, encoding="utf-8")


def test_validate_and_run_on_greeter(frozen_greeter):
    repo, layout, _ = frozen_greeter
    assert validate(layout) == []
    _implement(repo, GREETER_GOOD)
    report = run(layout.root, repo)
    assert report.passed, report.as_dict()
    kinds = {r.kind for r in report.results}
    assert kinds == {"table", "gherkin", "property"}


def test_run_catches_buggy_implementation(frozen_greeter):
    repo, layout, _ = frozen_greeter
    _implement(repo, GREETER_BUGGY)
    report = run(layout.root, repo)
    assert not report.passed
    failing = {r.name for r in report.results if r.status != "pass"}
    assert {"name is trimmed", "whitespace-only name rejected", "empty name rejected"} <= failing
    trimmed = next(r for r in report.results if r.name == "name is trimmed")
    assert "Hello, Ann!" in trimmed.diff


def test_property_falsification(frozen_greeter):
    repo, layout, _ = frozen_greeter
    _implement(repo, GREETER_GOOD.replace('f"Hello, {cleaned}!"', 'f"Hello, {cleaned[:5]}!"'))
    report = run(layout.root, repo, ["property"])
    (prop,) = report.results
    assert prop.status == "fail" and "falsified by input" in prop.message


def test_freeze_check_rejects_vacuous_and_broken_baselines(greeter_repo):
    from factory_core.stack import load_profile
    from factory_core.testing import greeter_draft, greeter_driver

    layout = pipeline.write_contract(greeter_repo, greeter_draft(), load_profile("python"))
    (layout.driver / "driver.py").write_text(greeter_driver(), encoding="utf-8")
    assert freeze_check(layout.root, greeter_repo) == []
    # A "new" case that already passes is vacuous.
    csv = layout.examples / "greet.csv"
    csv.write_text(
        csv.read_text(encoding="utf-8").replace("default greeting,AC-1,existing", "default greeting,AC-1,"),
        encoding="utf-8",
    )
    write_features(layout, "Personalised greeting")
    problems = freeze_check(layout.root, greeter_repo)
    assert any("vacuous" in p for p in problems)


def test_validate_requires_ac_coverage_and_sync(frozen_greeter):
    repo, layout, _ = frozen_greeter
    (layout.features / "greet.table.feature").write_text("Feature: tampered\n", encoding="utf-8")
    problems = validate(layout)
    assert any("out of sync" in p for p in problems)


def test_missing_value_sentinel_repr():
    assert repr(MISSING) == "MISSING"


def test_error_contains_mismatch_fails():
    ok, msg, diff = check(Expect(error_contains="whitespace"), CallResult(error="Name too long"))
    assert not ok and "does not contain" in msg and "Name too long" in diff


def test_diff_is_independent_of_key_order():
    from factory_core.tad_runner.compare import text_diff

    assert text_diff({"b": 1, "a": 2}, {"a": 2, "b": 1}) == ""
