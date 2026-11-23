# Tests as data in the factory

The human ↔ AI contract is a set of **data files**, executed by a **runner the agent cannot edit**. All
assertions live in the runner; the per-feature **driver** only translates DSL operations into calls on the
system's public interface (*Constrained Tests*: an agent cannot write a test without an assertion, because
it does not write tests in code at all).

```
.factory/specs/<feature-id>/
  intent.md             generated, human-readable view of the complete intent specification
  intent.yaml            problem, scope, acceptance criteria (AC-n), interface + ops, NFRs, grill decisions
  examples/<op>.csv      one row per case (tabular, Go-style)
  scenarios/<name>.yaml  multi-step / stateful cases (approved-scenario style)
  properties.yaml        invariants for property-based testing
  features/*.feature     Gherkin GENERATED from the data (human view + executed by the runner)
  driver/                assertion-free adapter (JSON-lines protocol)
  manifest.lock.json     sha256 of every file + root hash; root hash also stored outside the repo
```

## CSV tables

```csv
case,ac_id,baseline,in.name,out.greeting,error
default greeting,AC-1,existing,,"Hello, world!",
personalised greeting,AC-2,,Joe,"Hello, Joe!",
name is trimmed,AC-2,,"  Ann  ","Hello, Ann!",
whitespace-only name rejected,AC-3,," ",,Name must not be entirely whitespace
empty name rejected,AC-3,,<empty>,,Name must not be entirely whitespace
```

- Reserved columns: `case`, `ac_id`, `op` (defaults to the file name), `error`, `error_contains`, `baseline`.
- `in.<field>[:type]` / `out.<field>[:type]` with types `str` (default), `int`, `float`, `bool`, `json`;
  dotted names build nested objects; a bare `out[:type]` column compares the whole output exactly.
- Empty cell = field omitted, `<null>` = null, `<empty>` = empty string. Whitespace in data cells is kept.
- `baseline=existing` marks regression guards that must already pass before the change.

## YAML scenarios

```yaml
suite: todo
cases:
  - name: unchecking a completed todo reactivates it
    ac_id: AC-4
    given: [{op: add, input: {title: buy milk}}, {op: toggle, input: {title: buy milk}}]
    when: {op: toggle, input: {title: buy milk}}
    then:
      output_subset: {done: false}
      state:
        - {op: list, output: [{title: buy milk, done: false}]}
```

`then` is mandatory and must assert something: `output` (exact), `output_subset`, `error`, `error_contains`
or `no_error`, plus optional `state` queries.

## Properties

```yaml
properties:
  - name: any letters-only name is echoed in the greeting
    ac_id: AC-2
    op: greet
    input: {name: {type: string, alphabet: letters, min_size: 1, max_size: 20}}
    expect:
      - {path: greeting, starts_with: "Hello, "}
      - {path: greeting, contains_input: name}
```

Hypothesis generates inputs, the driver executes them — property-based testing for **any** language.

## Gherkin

`tad gherkin <spec>` renders each data file as a `.feature` (Scenario Outlines with `@AC-n` tagged Examples).
The runner also executes `.feature` files written in the fixed step vocabulary, so humans can add BDD
scenarios by hand. `tad validate` fails if a generated feature drifts from its data file.

The vocabulary reads as plain sentences: operations are unquoted, inputs are inline (`with title "buy milk"`),
follow-up state checks are a single line, and lists of objects become tables:

```gherkin
  @AC-2
  Scenario: toggling marks a todo as done
    Given I called add with title "buy milk"
    And I called add with title "walk the dog"
    When I call toggle with title "buy milk"
    Then it returns title "buy milk" and done true
    And list returns items:
      | title        | done  |
      | buy milk     | true  |
      | walk the dog | false |
    And remaining returns count 1
```

| Step | Meaning |
|---|---|
| `Given I called <op> [with <fields> \| with:]` | setup call that must succeed |
| `When I call <op> [with <fields> \| with:]` | the call under test |
| `Then it returns <json> \| <fields> \| :` | exact output (inline, JSON doc string or table of rows) |
| `Then it returns <field>:` | exact output `{field: [rows…]}` |
| `Then the result has <fields> \| :` / `Then the <field> is <value>` | only these fields must match |
| `Then it fails with <message>` / `… with an error containing <text>` | exact / partial error |
| `Then it succeeds` | no error |
| `And <op> returns … \| has … \| fails with … \| succeeds` | follow-up call (state check) |
| `And I query <op> with …` then `And it …` | follow-up call that needs input |

`<fields>` is `a 1, b "x" and c true` (JSON values; the last value may be bare text). Table cells hold JSON
or bare text (`buy milk`, `true`, `"  padded  "`); `(absent)` omits the field. The older phrasing
(`I call "<op>"`, `the output is …`, `the call fails with error …`) is still accepted.

## The driver protocol

```
→ {"id": 1, "op": "greet", "input": {"name": "Joe"}}
← {"id": 1, "output": {"greeting": "Hello, Joe!"}}      or   {"id": 1, "error": "Name must not be …"}
```

Plus `__ping__` and `__reset__`. Interface kinds: `jsonl` (driver process, any language — templates for Python
and Node), `http` (routes on a base URL) and `cli` (argv templates).

## Protection — what an agent cannot do

| Attempt | Caught by |
|---|---|
| edit/delete a CSV, YAML, feature or the driver | read-only during build · guard `protected-path` · manifest vs external root hash |
| re-lock the manifest after editing | external trust anchor (approved root hash is not in the repo) |
| add `skip`/`xfail`/`@Disabled`/`it.skip` | guard `skip-marker` |
| delete or move existing tests | guard `deleted-test` |
| lower coverage/mutation thresholds | thresholds live in the factory; guard `threshold-tamper` |
| `# pragma: no cover` / `istanbul ignore` | guard `coverage-exclusion` |
| weaken assertions in existing tests | guard warning → test auditor |
| tests that assert nothing / mock everything | mutation testing (pymut / Stryker / PIT) + test auditor |
| special-case the examples | property-based tests + mutation testing + auditor |

## CLI

```bash
uv run tad validate .factory/specs/<id>
```

```bash
uv run tad run .factory/specs/<id> --repo . --junit out/tad.xml
```

```bash
uv run tad freeze-check .factory/specs/<id> --repo .
```

```bash
uv run factory-guard verify --spec-dir .factory/specs/<id> --expected-hash <approved root hash>
```

```bash
uv run factory-gates run --repo . --spec-dir .factory/specs/<id> --base <freeze sha> --expected-hash <hash>
```
