# gh-aw factory (GitHub Agentic Workflows + Actions)

The "basic" factory: everything runs in GitHub. A **hub repository** hosts the workflows; each feature request is
an **issue** in the hub; the PR is opened in the **target** repository.

## Workflows

| File | Kind | Role |
|---|---|---|
| `factory-intake.yml` | Actions | `repository_dispatch: factory-request` / manual → creates the issue, dispatches the grill |
| `factory-orchestrator.yml` | Actions (generated) | deterministic state machine (`scripts/orchestrator.py`) on comments + workflow completions |
| `factory-step.yml` | Actions | deterministic steps with the target **write** token: `prepare-contract`, `apply-patch` (guard), `freeze`, `verify`, `open-pr` |
| `factory-{grill,driver,plan,build,audit}-{copilot,claude,codex}.md` | gh-aw (generated) | agent phases; read-only tokens; patches or one safe-output comment |

## Setup

1. Create a hub repository and copy into it: `gh-aw-factory/` and `shared/` (keep the paths), plus
   `gh-aw-factory/.github/workflows/*` → `.github/workflows/`.
2. Install the gh-aw CLI and compile the agentic workflows:
   ```bash
   gh extension install github/gh-aw
   ```
   ```bash
   python gh-aw-factory/scripts/gen_workflows.py && gh aw compile
   ```
3. Create a **GitHub App** with *Contents: read & write*, *Pull requests: read & write*, *Issues: read & write*,
   *Actions: read & write*; install it on the hub and the target repositories. Hub settings:
   - variable `FACTORY_APP_ID`, secret `FACTORY_APP_PRIVATE_KEY`
   - engine secrets as required by gh-aw: `COPILOT_GITHUB_TOKEN`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`
   - optional variables: `FACTORY_DEFAULT_ENGINE`, `FACTORY_STALL_LIMIT` (3), `FACTORY_MAX_ITERATIONS` (0 = loop
     until green), `FACTORY_RELAY_LOGINS` (the Teams bot's service account), `FACTORY_BOT_LOGINS`
4. In each target repository add `templates/target-repo/.github/workflows/factory-guard.yml` as a **required
   status check** and the `CODEOWNERS` rule for `/.factory/specs/` (set `FACTORY_HUB_REPO` variable there).
5. Thresholds: `gh-aw-factory/factory.yaml` (in the hub — agents cannot change it).

## Try it

```bash
gh workflow run factory-intake.yml -f repo=my-org/my-service -f request="Add a personalised greeting" -f engine=claude
```

Then talk in the created issue (or through Teams / `factory-chat --backend gh-aw`): answer the questions,
`/approve` the draft, and watch the labels move `phase:contract → driver → freeze → plan → build → verify →
audit → pr → done`. On escalation reply `/continue`, `/amend <change>` or `/abort`.

## Notes and assumptions

- gh-aw frontmatter (`engine`, `tools`, `safe-outputs.add-comment.target: "*"`, `steps`, `post-steps`,
  `network`) follows the gh-aw documentation at the time of writing; `gh aw compile --strict` is the source of
  truth — fix any reported keys there.
- The orchestrator dispatches with the App token so that chained `workflow_run` events fire.
- Unit tests for the orchestrator, issue helpers, workflow generation and prompt rendering: `gh-aw-factory/tests`.
