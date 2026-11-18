# AI Software Factory

Chat with an agent in **Microsoft Teams**, ask for a feature in any repository, and get back a **pull request**
whose behaviour is pinned down by an **executable, human-approved contract** that the coding agent is not
allowed to touch.

```
Teams / CLI ──► GRILL ──► CONTRACT (tests as data) ──► FREEZE ──► PLAN (task graph) ──► BUILD ⇄ VERIFY ──► PR
               (human ⇄ agent)   CSV · YAML · Gherkin       hash-lock   (graph engineering)   clean context, loop until green
```

Two implementations of the same factory live side by side:

| | [`gh-aw-factory/`](gh-aw-factory/README.md) | [`langgraph-factory/`](langgraph-factory/README.md) |
|---|---|---|
| Runtime | GitHub Agentic Workflows + plain GitHub Actions | Python app on LangGraph (FastAPI, Postgres checkpoints) |
| State | a GitHub Issue (labels, comments, hidden anchors in the body) | LangGraph checkpointer (`thread_id` = feature) |
| Human-in-the-loop | issue comments (`/approve`, `/continue`, …) relayed from Teams | `interrupt()` / `Command(resume=…)` via HTTP API |
| Build graph | task DAG executed in topological order by one agent run | task DAG fanned out with `Send` to parallel workers in git worktrees, merged by an integrator |
| Clean verify context | separate workflow run on a fresh runner | separate subgraph with its own state type + fresh clone |
| Deploy | hub repo + GitHub App | `docker compose up` (local first) |

Everything that must behave identically is in [`shared/`](shared): the tests-as-data runner, the anti-cheat
guard, the verification gates, mutation testing, prompts, coding-agent adapters and the chat channels.

## Why "tests as data"

Following [Matteo Vaccari, *Tests as data* (martinfowler.com, 2026)](https://martinfowler.com/articles/tests-as-data.html):
when AI writes the code, we become its customers, so **customer tests are the contract** — and moving that
contract into **data files** (CSV tables, YAML scenarios) executed by a **factory-owned runner** makes it easy
to read, review and protect from "make the tests pass by changing the tests". See
[docs/tests-as-data.md](docs/tests-as-data.md).

## The pipeline

1. **Grill** — the agent interviews the requester (≤5 sharp questions per turn: edge cases, exact error
   messages, interface, existing behaviour, which coding agent: *Copilot CLI, Claude Code or Codex*). A
   deterministic **spec critic** checks every acceptance criterion has examples before the human sees a draft.
2. **Contract** — `intent.yaml` + `examples/*.csv` + `scenarios/*.yaml` + `properties.yaml`; Gherkin
   `features/*.feature` are generated from the data; a thin, **assertion-free driver** maps DSL ops to the
   system's public interface.
3. **Freeze** — *freeze check* proves the tests are not vacuous (new cases must fail on today's code,
   `existing` cases must pass), then a sha256 **manifest** is locked and its root hash stored **outside the
   agent's reach** (LangGraph DB / issue body).
4. **Plan** — a schema-validated **task graph** (DAG).
5. **Build** — fresh agent sessions implement tasks; the contract is read-only (OS bit / `:ro` mount /
   CLI deny rules) and every change goes through the **guard**.
6. **Verify (clean context)** — fresh clone, fresh session, deterministic gates:
   `integrity → setup → frontend_dependencies → lint → typecheck → build → unit → playwright →
   diff_coverage → contract (CSV/YAML) → acceptance (Gherkin) → property (Hypothesis) → mutation → regression →
   security`, then a **test auditor**
   agent that has never seen the build conversation.
7. **Loop** — any failure goes back to a *fresh* builder with a bounded failure brief. It loops until green;
   a **stall breaker** (same failure fingerprint *N* times) asks the human: continue / amend the contract /
   abort. Amending is the *only* legitimate way tests change, and it requires re-approval and re-locking.
8. **PR** — the body renders the contract tables, gate results, iterations and manifest hash.

## Quick start (no LLM, no Teams, no cloud)

```bash
uv sync --all-packages
```

```bash
uv run factory-run --fake greeter --repo examples/targets/python-greeter --request "Add a personalised greeting"
```

The scripted demo agent asks questions, drafts the contract, writes the driver, builds a *buggy* first version
(verification fails), fixes it in a fresh session, and writes the PR body to the workspace. Answer the
questions, then type `/approve`.

Run the whole test suite (unit, adversarial, Gherkin, property, mutation and end-to-end graph runs):

```bash
uv run pytest
```

## Talking to the factory

All front-ends share one chat core ([`shared/factory_channels`](shared/factory_channels)); see
[docs/channels.md](docs/channels.md) for every option to test **without Teams**:

| Option | Needs |
|---|---|
| `factory-run` (in-process) | nothing |
| `factory-chat` (terminal) against the LangGraph API or gh-aw issues | API running / GitHub token |
| Microsoft 365 **Agents Playground** or Bot Framework Emulator against the real bot | Node / Emulator, no tenant |
| plain HTTP / LangGraph Studio / GitHub issue comments | curl / browser |
| Microsoft Teams via Azure Bot Service | Azure Bot registration + Teams app package |

## Repository layout

```
shared/factory_core/      tad_runner · guard · gates · mutation (pymut) · agents · prompts · stack_profiles · reports
shared/factory_channels/  chat core · LangGraph & gh-aw backends · Teams bot (M365 Agents SDK) · terminal client
langgraph-factory/        LangGraph graph, API, runtime, sandbox, docker-compose
gh-aw-factory/            agentic workflow generator, orchestrator, deterministic steps, target-repo guard
examples/                 sample target repo + canned contract used by demos and e2e tests
docs/                     architecture, tests as data, channels
```
