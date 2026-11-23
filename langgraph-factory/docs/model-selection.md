# Model selection per node

Every agent-backed node runs a fresh coding-agent CLI session with a **role** (see
[nodes.md](nodes.md#where-the-instructions-for-each-node-live)). You can pick the model and the reasoning
effort for each role and engine, so cheap work runs on cheap models and the few high-stakes steps get
the strong ones.

## Configure it

Add a `models:` block to the factory YAML (`FACTORY_CONFIG`, see [factory.example.yaml](../factory.example.yaml)).
It is keyed by **engine**, then **role**, because model names differ per CLI:

```yaml
models:
  claude:
    grill:   {model: sonnet, effort: medium}
    planner: {model: opus, effort: high}
    builder: {model: sonnet, effort: high}
    finisher: {model: haiku, effort: low}
  codex:
    builder: {model: gpt-6.2-sol, effort: high}
```

Rules:

- **Roles:** `grill`, `driver_writer`, `planner`, `builder`, `reviewer`, `resolver`, `auditor`, `finisher`.
- **Missing role or field:** no flag is passed, and the CLI's own default model/effort is used.
- **Values are passed through as-is.** They become `--model` / `--effort` (Claude Code), `--model` /
  `--reasoning-effort` (Copilot CLI), or `--model` / `-c model_reasoning_effort=…` (Codex).
- **Rejected model or effort:** if the CLI rejects the value (typo, no access, or a model that does not
  support effort), the factory logs a warning. It emits a `warning` event and reruns that step once with the
  CLI default. The usage report marks the row `fallback to CLI default`.
- **The YAML wins over `FACTORY_<ENGINE>_ARGS`.** Its flags are appended after the extra args from the
  environment.
- **The engine can still change during the run** (`/engine claude`). The block for the engine in use applies.

## Recommendations

Tiers, not just names: model catalogs change often, so the table gives the tier you need and an example
name per engine as of this writing. Check the current names with `copilot` → `/model`, `claude --model`,
or the [Codex models page](https://developers.openai.com/codex/models).

| Tier | Copilot CLI | Claude Code | Codex |
|---|---|---|---|
| **Low** (fast, cheap) | `claude-haiku-4.5` (no effort setting) | `haiku` | `gpt-6-luna` |
| **Mid** (default workhorse) | `claude-sonnet-4.6` or `gpt-5.6-sol` | `sonnet` | `gpt-6.2-sol` |
| **High** (deep reasoning) | `claude-opus-4.6` | `opus` | `gpt-6.2-astra` |

### Per role

| Role (node) | Calls per feature | What it needs | Recommended | Effort |
|---|---|---|---|---|
| `grill` (`grill_turn`) | one per interview round (2–5) | Finds edge cases and drafts the contract. A weak contract means a wrong feature, but outputs are short. | **Mid** | medium |
| `driver_writer` (`freeze`) | 1–3 (freeze retries) | Thin glue code filled in from a stack template. The freeze check catches mistakes. | **Low → Mid** | low |
| `planner` (`plan`) | 1 | Breaks the feature into a task graph and writes the per-task briefs. Every builder depends on it, and it's cheap because it runs once. | **High** | high |
| `builder` (`implement`) | tasks × (1 + review fixes) + fix rounds; **most of the tokens** | Writes the real code with TDD. Code quality drives the number of verify iterations. | **Mid** (High for large or legacy repos) | high |
| `reviewer` (`review`) | tasks × review rounds | Read-only check of spec compliance and code quality. A *different* model family than the builder catches more. | **Mid** | medium |
| `resolver` (`integrate`) | only on merge conflicts (rare) | Resolves conflicts between parallel task branches. | **Mid** | medium |
| `auditor` (`audit`) | once per green iteration | The last independent reviewer before the PR, checked against the frozen contract. A miss ships. | **High** | high |
| `finisher` (`finish`) | 1 | Runs the test suite and writes the PR title and summary. | **Low** | low |

### Ready-to-copy profiles

**Balanced** (recommended starting point):

```yaml
models:
  copilot:
    grill:         {model: claude-sonnet-4.6, effort: medium}
    driver_writer: {model: claude-haiku-4.5}
    planner:       {model: claude-opus-4.6, effort: high}
    builder:       {model: claude-sonnet-4.6, effort: high}
    reviewer:      {model: gpt-5.6-sol, effort: medium}
    resolver:      {model: claude-sonnet-4.6, effort: medium}
    auditor:       {model: claude-opus-4.6, effort: high}
    finisher:      {model: claude-haiku-4.5}
  claude:
    grill:         {model: sonnet, effort: medium}
    driver_writer: {model: haiku, effort: low}
    planner:       {model: opus, effort: high}
    builder:       {model: sonnet, effort: high}
    reviewer:      {model: sonnet, effort: medium}
    resolver:      {model: sonnet, effort: medium}
    auditor:       {model: opus, effort: high}
    finisher:      {model: haiku, effort: low}
  codex:
    grill:         {model: gpt-6.2-sol, effort: medium}
    driver_writer: {model: gpt-6-luna, effort: low}
    planner:       {model: gpt-6.2-astra, effort: high}
    builder:       {model: gpt-6.2-sol, effort: high}
    reviewer:      {model: gpt-6.2-sol, effort: medium}
    resolver:      {model: gpt-6.2-sol, effort: medium}
    auditor:       {model: gpt-6.2-astra, effort: high}
    finisher:      {model: gpt-6-luna, effort: low}
```

**Budget:** keep `planner` and `auditor` on Mid with high effort, and move `grill`, `driver_writer`,
`reviewer` and `finisher` to Low. Keep `builder` on Mid: a cheaper builder usually costs *more*, because
every failed verification adds another build, review and verify round.

**Maximum quality** (large or legacy codebases): move `builder` and `grill` to High. Keep the rest as
in Balanced.

### Why these choices

- **Spend where errors compound.** The planner's task graph and the auditor's verdict shape everything
  downstream, and each runs about once per feature. A High model there costs little in total.
- **The builder dominates the bill.** Check the usage report's *By role* table. If `builder` has many sessions,
  the fix is usually a better plan or a stronger builder, not a cheaper one.
- **Mechanical steps don't need reasoning.** The driver is filled in from a template and checked by the freeze
  check. The finisher runs tests and writes a summary.
- **Use a different reviewer model.** A reviewer from another model family than the builder doesn't share
  its blind spots.
- **Effort costs output tokens.** `high`/`xhigh` mostly add reasoning tokens. Use them on planning, building
  and auditing, and leave the rest at the default or `low`.
- **Not every model takes an effort setting.** Copilot rejects `--reasoning-effort` for `claude-haiku-4.5`. In
  that case the factory falls back to the CLI default and flags the row, so leave `effort` out for such models.

## Measure it: `--usage-report`

```bash
uv run factory-run --repo ../my-service --request "..." --engine claude --usage-report
```

When the run ends (also on abort, error or Ctrl+C), the factory writes `usage-report.json` and
`usage-report.md` to the feature workspace (`<workspaces>/<feature>/`). Use `--usage-report path/to/name` to
choose the location (`.json`/`.md` are added). The report has:

- **Totals:** sessions, input, output, cache-read, cache-write and reasoning tokens, plus cost.
- **By role** and **by model:** the same columns.
- **Sessions:** one row per CLI session with role, task, engine, model, effort, tokens, duration and status.
  Schema retries and fallbacks appear as extra rows.

What each CLI reports:

| Engine | Model reported | Tokens | Cost |
|---|---|---|---|
| Copilot CLI | yes (`--usage-output-file`) | input (includes cache), output, cache read/write, reasoning | premium requests, AI credits |
| Claude Code | yes (`modelUsage` in `--output-format json`) | input, output, cache read/write | USD estimate |
| Codex | no: the row shows the configured model as `<model> (configured)` | input, cached input, output, reasoning (`--json`) | — |

Compare a few runs of the same kind of feature with different `models:` profiles. Then keep the cheapest
profile that doesn't increase `iterations` or audit findings.
