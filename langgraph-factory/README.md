# LangGraph factory

The robust factory: a Python service built on **LangGraph** with Postgres checkpoints, an HTTP API every
channel talks to, parallel builder workers in git worktrees, and a clean-context verification subgraph.

## Run

Offline demo (scripted agent, no LLM):

```bash
uv run factory-run --fake greeter --repo examples/targets/python-greeter --request "Add a personalised greeting"
```

Real coding agent on a real repo (needs `copilot`, `claude` or `codex` installed and authenticated):

```bash
uv run factory-run --repo https://github.com/my-org/my-service.git --request "Add rate limiting to /orders" --engine claude
```

Local git repository (e.g. a sibling `todo-app` checkout). The feature branch `factory/<key>` is
pushed back into that repository and the PR body is written to `workspaces/<feature>/PR.md`:

```bash
uv run factory-run --repo ../todo-app --request "Add an optional due date to todos" --engine copilot
```

```bash
git -C ../todo-app diff main...factory/<key>
```

A plain folder without `.git` is imported as a fresh repository; nothing is written back to it.

Service (API on :8080, Postgres, optional Teams bot):

```bash
docker compose up --build
```

```bash
docker compose --profile teams up --build
```

Set `FACTORY_FAKE_AGENT=greeter` for the offline demo inside compose, `GITHUB_TOKEN` to push branches and open
real PRs (otherwise the PR body is written to `workspaces/<feature>/PR.md`), and `FACTORY_SANDBOX=docker`
(+ mount the docker socket) for one container per agent call with the contract mounted read-only.

## API

| Method | Path | |
|---|---|---|
| POST | `/features` | `{request, repo, base_branch?, engine?, requester?, notify_url?}` → `{feature_key}` |
| POST | `/features/{key}/messages` | `{text?, action?: approve\|revise\|abort\|continue\|amend, engine?, user?}` |
| GET | `/features/{key}` | status, `waiting_for` (questions / approval / escalation payload), last report, PR URL |
| GET | `/features/{key}/events?after=N&wait=S` | event log (long-poll) |
| GET | `/features/{key}/stream` | Server-Sent Events |

## Configuration

| Env | Default | |
|---|---|---|
| `DATABASE_URL` | — | Postgres checkpointer (in-memory when unset) |
| `FACTORY_WORKSPACES` | `./workspaces` | clones, worktrees, verify checkouts |
| `FACTORY_CONFIG` | — | YAML thresholds, see `factory.example.yaml` |
| `FACTORY_SANDBOX` / `FACTORY_SANDBOX_IMAGE` | `local` / `factory-sandbox:latest` | where agent CLIs run |
| `FACTORY_<ENGINE>_BIN` / `FACTORY_<ENGINE>_ARGS` | — | override CLI binary / extra flags (e.g. `--model`) |
| `FACTORY_SKILLS_DIR` | `../factory-skills` (source checkout) | APM-installed agent skills copied into every agent workdir |
| `FACTORY_NOTIFY_URL` | — | webhook for every event |
| `GITHUB_TOKEN` | — | push + open PRs on github.com |

## Code map

See [docs/nodes.md](docs/nodes.md) for a per-node walkthrough, the graph diagram and where each node's prompt lives.

`src/factory_app/graph/nodes.py` (all nodes), `graph/build.py` (wiring), `state.py` (FactoryState /
TaskState / VerifyState), `runtime.py` (threads + checkpointer), `api/server.py`, `cli.py`, `sandbox/`, `services.py`.
Tests: `tests/test_graph.py` (happy path with a failing first iteration, clean-context assertions, cheating
builder caught by the guard, stall breaker + continue/abort, task review → fix loop, finish escalation, API
round trip).
