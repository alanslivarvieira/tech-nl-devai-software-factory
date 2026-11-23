"""Channels: chat core, both backends, and the Teams bot over the real Bot Framework protocol (no Teams)."""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from factory_channels.backend import FactoryEvent, GhAwBackend, LangGraphBackend, request_body
from factory_channels.cards import card_to_text
from factory_channels.chat import HELP, FactoryChat


class FakeBackend:
    def __init__(self) -> None:
        self.started: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []
        self.queue: list[FactoryEvent] = []

    def start(self, **kw: Any) -> str:
        self.started.append(kw)
        return "feat-1"

    def send(self, key: str, **kw: Any) -> None:
        self.sent.append({"key": key, **kw})

    def poll(self, key: str, cursor: Any) -> tuple[list[FactoryEvent], Any]:
        events, self.queue = self.queue, []
        return events, (cursor or 0) + len(events)


# ------------------------------------------------------------------------------------------- chat core
def test_chat_parses_requests_and_commands(tmp_path):
    be = FakeBackend()
    chat = FactoryChat(be, store_path=tmp_path / "store.json")
    assert chat.handle("c1", "alan", text="hello")[0].text == HELP
    assert chat.handle("c1", "alan", text="gibberish")[0].text == HELP
    r = chat.handle("c1", "alan", text="build a personalised greeting /engine claude in acme/shop@develop")
    assert "feat-1" in r[0].text
    assert be.started[0] == {
        "request": "a personalised greeting",
        "repo": "acme/shop",
        "base": "develop",
        "requester": "alan",
        "engine": "claude",
    }
    chat.handle("c1", "alan", text="/engine codex trim it")
    chat.handle("c1", "alan", text="/approve")
    chat.handle("c1", "alan", text="please add more", action="revise", engine="copilot")
    assert be.sent[0] == {"key": "feat-1", "text": "trim it", "action": None, "user": "alan", "engine": "codex"}
    assert be.sent[1]["action"] == "approve"
    assert be.sent[2] == {
        "key": "feat-1",
        "text": "please add more",
        "action": "revise",
        "user": "alan",
        "engine": "copilot",
    }
    # persisted across restarts
    assert FactoryChat(be, store_path=tmp_path / "store.json").conv("c1").feature == "feat-1"


def test_structured_grill_round_tells_the_human_how_to_answer():
    plain = FactoryChat.render(FactoryEvent("questions", "q", {"questions": ["1. trim?"]}))[0].text
    rnd = FactoryChat.render(
        FactoryEvent("questions", "q", {"questions": ["1. a", "2. b"], "question_items": [{}, {}]})
    )[0].text
    assert "Reply `yes`" not in plain
    assert "1. a\n\n2. b" in rnd and "Reply `yes` to accept my recommendations" in rnd


def test_chat_renders_events_and_finishes():
    be = FakeBackend()
    chat = FactoryChat(be)
    chat.handle("c", "u", text="/new acme/shop add greeting")
    be.queue = [
        FactoryEvent("questions", "q", {"questions": ["1. trim?"]}),
        FactoryEvent(
            "approval_needed",
            "draft",
            {
                "contract_markdown": "```csv file=examples/a.csv\nx\n```",
                "intent_markdown": "# Intent\n\n## Scope\n\n- Add greetings\n\n<details>hidden</details>",
            },
        ),
        FactoryEvent("escalation", "stuck", {"reason": "same failures"}),
        FactoryEvent("pr_opened", "done", {"pr_url": "https://github.com/acme/shop/pull/1"}),
    ]
    replies = chat.updates("c")
    assert "1. trim?" in replies[0].text
    assert (
        replies[1].card
        and "examples/a.csv" in card_to_text(replies[1].card)
        and "Add greetings" in card_to_text(replies[1].card)
        and "hidden" not in card_to_text(replies[1].card)
        and "/approve" in card_to_text(replies[1].card)
    )
    assert replies[2].card and "/continue" in card_to_text(replies[2].card)
    assert "pull/1" in replies[3].text
    assert chat.active() == []


# ------------------------------------------------------------------------------------------- gh-aw
def test_ghaw_backend_relays_through_issue_comments():
    calls: list[tuple[str, str, Any]] = []
    comments = [
        {"id": 1, "user": {"login": "relay-bot", "type": "User"}, "body": "my own relayed message"},
        {"id": 2, "user": {"login": "github-actions[bot]", "type": "Bot"}, "body": "1. Trim names?\n2. Engine?"},
        {
            "id": 3,
            "user": {"login": "github-actions[bot]", "type": "Bot"},
            "body": "<!-- factory:draft -->\nsummary\n```csv file=examples/a.csv\nx\n```",
        },
        {
            "id": 4,
            "user": {"login": "github-actions[bot]", "type": "Bot"},
            "body": "<!-- factory:verify iteration=1 passed=false fingerprint=ab -->\n"
            "report<details>big json</details>",
        },
        {
            "id": 5,
            "user": {"login": "github-actions[bot]", "type": "Bot"},
            "body": "🎉 Pull request ready for review: https://github.com/acme/shop/pull/9",
        },
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else None
        calls.append((req.method, req.url.path, body))
        if req.url.path == "/user":
            return httpx.Response(200, json={"login": "relay-bot"})
        if req.method == "POST" and req.url.path.endswith("/issues"):
            return httpx.Response(201, json={"number": 42})
        if req.method == "GET" and req.url.path.endswith("/comments"):
            return httpx.Response(200, json=comments)
        return httpx.Response(204)

    be = GhAwBackend(
        "acme/hub", "t", client=httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    )
    assert be.start(request="add greeting", repo="acme/shop", base="main", requester="alan", engine="claude") == "42"
    issue_call = calls[0]
    assert issue_call[2]["labels"] == ["factory", "phase:grill", "engine:claude", "iter:0"]
    assert issue_call[2]["body"] == request_body("acme/shop", "main", "add greeting", "alan")
    assert calls[1][1] == "/repos/acme/hub/actions/workflows/factory-grill-claude.lock.yml/dispatches"
    be.send("42", action="approve", user="alan")
    be.send("42", text="reject names > 50", action="amend")
    be.send("42", text="ok", engine="codex")
    sent = [c[2]["body"] for c in calls if c[1].endswith("/comments") and c[0] == "POST"]
    assert sent[0].startswith("/approve") and "relayed from Teams for alan" in sent[0]
    assert sent[1] == "/amend reject names > 50" and sent[2] == "/engine codex\nok"
    events, cursor = be.poll("42", 0)
    assert [e.kind for e in events] == ["questions", "approval_needed", "progress", "pr_opened"]
    assert "big json" not in events[2].message and events[3].data["pr_url"].endswith("/pull/9")
    assert cursor == 5 and be.poll("42", cursor)[0] == []


def test_request_body_matches_gh_aw_scripts():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "gh-aw-factory" / "scripts"))
    from ghaw_lib import request_body as gh_body

    assert gh_body("a/b", "main", "x", "y") == request_body("a/b", "main", "x", "y")


# ----------------------------------------------------------------------------------- LangGraph API
@pytest.mark.e2e
def test_chat_over_langgraph_api_end_to_end(tmp_path):
    from fastapi.testclient import TestClient

    from factory_app.api.server import create_app
    from factory_app.config import Settings
    from factory_app.runtime import FactoryRuntime
    from factory_core.models import FactoryConfig
    from factory_core.testing import GREETER_TARGET, greeter_fake_agent

    rt = FactoryRuntime(
        Settings(workspaces=tmp_path / "ws", fake_agent=None, factory=FactoryConfig(run_setup=False, max_mutants=30)),
        agent=greeter_fake_agent(buggy_first=False),
    )
    chat = FactoryChat(LangGraphBackend("http://test", client=TestClient(create_app(rt))))

    def until(kind_check, timeout=300):
        seen = []
        end = time.time() + timeout
        while time.time() < end:
            seen += chat.updates("t")
            if kind_check(seen):
                return seen
            time.sleep(0.2)
        raise AssertionError([r.text or "card" for r in seen])

    chat.handle("t", "alan", text=f"build a personalised greeting in {GREETER_TARGET}")
    until(lambda rs: any("Grill time" in r.text for r in rs))
    assert "Got it" in chat.handle("t", "alan", text="trim yes, reject blanks")[0].text
    until(lambda rs: any(r.card for r in rs))
    chat.handle("t", "alan", action="approve")
    replies = until(lambda rs: any("Pull request ready" in r.text or "Done!" in r.text for r in rs))
    assert any("frozen" in r.text.lower() for r in replies)
    assert chat.active() == []


# ------------------------------------------------------------------------------ Teams bot, no Teams
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_teams_bot_speaks_bot_framework_protocol(tmp_path, monkeypatch):
    """Posts a real Bot Framework activity to /api/messages (as Agents Playground / Teams would) and captures
    the bot's replies on a fake connector service."""
    from aiohttp import web

    monkeypatch.setenv("FACTORY_BOT_STORE", str(tmp_path / "store.json"))
    monkeypatch.delenv("CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID", raising=False)
    from factory_channels.teams_bot.app import FactoryBot, create_web_app

    async def scenario() -> list[dict[str, Any]]:
        captured: list[dict[str, Any]] = []

        async def connector(req: web.Request) -> web.Response:
            captured.append(await req.json())
            return web.json_response({"id": f"r{len(captured)}"})

        conn_app = web.Application()
        conn_app.router.add_post("/v3/conversations/{cid}/activities", connector)
        conn_app.router.add_post("/v3/conversations/{cid}/activities/{aid}", connector)
        cport, bport = _free_port(), _free_port()
        runners = []
        for app, port in ((conn_app, cport), (None, bport)):
            if app is None:
                bot = FactoryBot()
                fake = FakeBackend()
                bot.chat = FactoryChat(fake)
                app = create_web_app(bot)
            runner = web.AppRunner(app)
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", port).start()
            runners.append(runner)
        activity = {
            "type": "message",
            "id": "m1",
            "channelId": "msteams",
            "serviceUrl": f"http://127.0.0.1:{cport}",
            "from": {"id": "u1", "name": "Alan"},
            "recipient": {"id": "bot", "name": "factory"},
            "conversation": {"id": "conv-1"},
            "text": "build a personalised greeting in acme/shop",
        }
        async with httpx.AsyncClient() as client:
            r = await client.post(f"http://127.0.0.1:{bport}/api/messages", json=activity, timeout=30)
            assert r.status_code in (200, 201, 202), r.text
            # The factory asks a question later; the pump pushes it proactively.
            fake.queue.append(FactoryEvent("questions", "q", {"questions": ["1. Trim names?"]}))
            for _ in range(50):
                if any("Trim names" in (a.get("text") or "") for a in captured):
                    break
                await asyncio.sleep(0.2)
            # Card button submit
            card_submit = {**activity, "id": "m2", "text": None, "value": {"factory_action": "approve"}}
            await client.post(f"http://127.0.0.1:{bport}/api/messages", json=card_submit, timeout=30)
        for runner in runners:
            await runner.cleanup()
        assert fake.started[0]["repo"] == "acme/shop" and fake.started[0]["requester"] == "Alan"
        assert fake.sent[-1]["action"] == "approve"
        return captured

    monkeypatch.setenv("FACTORY_POLL_SECONDS", "0.2")
    captured = asyncio.run(scenario())
    texts = [a.get("text") or "" for a in captured]
    assert any("On it!" in t for t in texts), texts
    assert any("Trim names" in t for t in texts), "proactive message from the factory reached the chat"
    assert any("Approved" in t for t in texts)
