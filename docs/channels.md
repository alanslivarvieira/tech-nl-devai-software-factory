# Channels — talking to the factory (with and without Teams)

One chat core (`shared/factory_channels/chat.py`) gives every front-end the same conversation:

```
you:      build a personalised greeting in acme/shop@main
factory:  🔥 Grill time — 1. Trim whitespace? 2. Blank names: reject or default? 3. Which coding agent?
you:      trim yes, reject blanks with an error. /engine claude
factory:  📜 Draft contract (card with the CSV tables)   [✅ Approve & build] [✏️ Request changes] [🛑 Abort]
you:      ✅
factory:  ⏳ Contract frozen … ⏳ Task graph [impl, tests] … ❌ Iteration 1: failing contract … ✅ Iteration 2
factory:  🎉 Pull request ready for review: https://github.com/acme/shop/pull/42
```

Commands: `build <what> in <owner/repo>[@branch]`, `/new <repo> <what>`, `/engine copilot|claude|codex`,
`/approve`, `/status`, `/abort`, and on escalation `/continue`, `/amend <change>`.

Backends: **LangGraph** (`FACTORY_BACKEND=langgraph`, `FACTORY_API_URL`) or **gh-aw**
(`FACTORY_BACKEND=gh-aw`, `FACTORY_HUB_REPO`, `GH_TOKEN` of a relay service account listed in the hub's
`FACTORY_RELAY_LOGINS` variable).

## Option 1 — in-process, nothing else running

```bash
uv run factory-run --fake greeter --repo examples/targets/python-greeter --request "Add a personalised greeting"
```

Drop `--fake` to use a real coding agent CLI (`copilot`, `claude` or `codex` must be installed and logged in).

## Option 2 — terminal chat against the API

```bash
cd langgraph-factory && FACTORY_FAKE_AGENT=greeter docker compose up --build
```

```bash
uv run factory-chat --backend langgraph --url http://localhost:8080
```

Against the gh-aw factory instead:

```bash
uv run factory-chat --backend gh-aw --hub my-org/factory-hub
```

## Option 3 — the real Teams bot, without Teams

Start the bot (no Azure credentials → local anonymous mode):

```bash
uv run factory-teams-bot
```

**Microsoft 365 Agents Playground** (renders Teams UI incl. Adaptive Cards, no tenant needed):

```bash
npx @microsoft/m365agentsplayground -e http://localhost:3978/api/messages -c msteams
```

Or **Bot Framework Emulator** → *Open Bot* → `http://localhost:3978/api/messages` (leave App ID empty).

The automated test `shared/tests/test_channels.py::test_teams_bot_speaks_bot_framework_protocol` does the
same thing in CI: it posts Bot Framework activities to `/api/messages` and captures the bot's replies (including
proactive pushes and card button submits) on a fake connector service.

## Option 4 — raw HTTP / Studio / issues

```bash
curl -X POST localhost:8080/features -H 'content-type: application/json' -d '{"request":"Add a personalised greeting","repo":"/opt/factory/examples/targets/python-greeter"}'
```

```bash
curl localhost:8080/features/<feature_key>
```

```bash
curl -X POST localhost:8080/features/<feature_key>/messages -H 'content-type: application/json' -d '{"action":"approve","user":"me"}'
```

Live events: `GET /features/<key>/stream` (Server-Sent Events). For gh-aw, simply comment on the factory
issue in the hub repository.

## Option 5 — Microsoft Teams (production)

1. Create an **Azure Bot** resource (single-tenant app registration); messaging endpoint
   `https://<host>/api/messages` (use a Dev Tunnel for a local bot).
2. Set `CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID / CLIENTSECRET / TENANTID` for the bot.
3. Edit `shared/factory_channels/teams_bot/appPackage/manifest.json` (`${{TEAMS_APP_ID}}`, `${{BOT_ID}}`),
   zip it with the two icons and upload it in Teams (or via the Teams Developer Portal).
4. With docker-compose: `docker compose --profile teams up --build` runs the bot next to the factory.

The bot pushes factory events proactively (questions, approval cards, escalations, the PR link) into the
conversation that started the feature.
