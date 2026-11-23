# Factory skills

The agent skills the factory nodes follow, imported and pinned with [APM](https://github.com/microsoft/apm):

| Skill | Source | Used by |
|---|---|---|
| `grill-me` → `grilling` | [mattpocock/skills](https://github.com/mattpocock/skills) ([docs](https://www.aihero.dev/skills-grill-me)) | `grill_turn` |
| `writing-plans` | [obra/superpowers](https://github.com/obra/superpowers) step 3 | `plan` |
| `subagent-driven-development`, `executing-plans` | superpowers step 4 | the graph is the SDD controller; `worker` is its implementer seat (gh-aw build: `executing-plans` inline) |
| `test-driven-development` | superpowers step 5 | `worker` (builder) |
| `requesting-code-review` | superpowers step 6 | per-task `review` (task-reviewer template), `audit` (whole-branch `code-reviewer.md`) |
| `finishing-a-development-branch` | superpowers step 7 | `finish` (choice fixed to *push and create a PR*) |
| `verification-before-completion`, `systematic-debugging` | superpowers (required by `executing-plans`) | `worker` |

## How they reach the agents

`apm install` (run in this folder) resolves [apm.yml](apm.yml), pins every source to a commit in
[apm.lock.yaml](apm.lock.yaml) and writes the skills to `.agents/skills/` (Codex, Copilot CLI) and
`.claude/skills/` (Claude Code). Before every agent call,
[`factory_core/skills.py`](../shared/factory_core/skills.py) copies those skill folders into the agent's
working directory (recon checkout, task worktrees, verify clones). It adds them to `.git/info/exclude` so they
are never committed to the target repo, and it adds them to the call's protected paths. A skill folder that
the target repository already ships is left alone.

APM also generates superpowers' `SessionStart` hooks (`.claude/hooks`, `.codex/`, `.github/hooks`). The
factory does **not** deploy them: every node tells the agent which skill to use, and the superpowers bootstrap
(brainstorming etc.) would fight the factory's own flow.

- Local runs find this folder automatically from a source checkout; elsewhere set `FACTORY_SKILLS_DIR`
  (the sandbox image does: `/opt/factory/factory-skills`).
- gh-aw workflows run `python -m factory_core.skills deploy .` with `FACTORY_SKILLS_DIR=factory-skills`.

## Updating

```bash
cd factory-skills
apm update          # or: apm install --update — re-resolves `ref: main` to the latest commits
apm audit           # check the deployed files against the lockfile
```

Commit `apm.yml`, `apm.lock.yaml` and the deployed `.agents/skills/` and `.claude/skills/` folders, and
review a skill update the way you would review code: the prompts depend on the skills' wording (seat
templates such as `implementer-prompt.md`, `task-reviewer-prompt.md`, `code-reviewer.md`).
