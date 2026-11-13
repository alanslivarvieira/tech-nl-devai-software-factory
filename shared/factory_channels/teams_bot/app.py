"""Microsoft Teams bot (Microsoft 365 Agents SDK) — a thin adapter around ``FactoryChat``.

Run locally:   factory-teams-bot        (listens on :3978/api/messages)
Test locally WITHOUT Teams:  Microsoft 365 Agents Playground  →  agentsplayground -e http://localhost:3978/api/messages
                             or Bot Framework Emulator          →  http://localhost:3978/api/messages
Env:
  FACTORY_BACKEND=langgraph|gh-aw, FACTORY_API_URL (langgraph), FACTORY_HUB_REPO + GH_TOKEN (gh-aw)
  CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID / CLIENTSECRET / TENANTID   (Azure Bot; omit for local)
  FACTORY_POLL_SECONDS (default 3), FACTORY_BOT_STORE (conversation map, default .factory-bot.json)
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from aiohttp import web
from microsoft_agents.activity import (
    Activity,
    ActivityTypes,
    Attachment,
    ConversationReference,
    load_configuration_from_env,
)
from microsoft_agents.authentication.msal import MsalConnectionManager
from microsoft_agents.hosting.aiohttp import CloudAdapter, jwt_authorization_middleware, start_agent_process
from microsoft_agents.hosting.core import (
    AgentApplication,
    Authorization,
    ClaimsIdentity,
    MemoryStorage,
    TurnContext,
    TurnState,
)

from ..backend import make_backend
from ..chat import HELP, FactoryChat, Reply

log = logging.getLogger("factory.teams")
ADAPTIVE = "application/vnd.microsoft.card.adaptive"


def to_activity(reply: Reply) -> Activity | str:
    if reply.card:
        return Activity(type=ActivityTypes.message, attachments=[Attachment(content_type=ADAPTIVE, content=reply.card)])
    return reply.text


CLIENT_ID_ENV = "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID"
ANON_ENV = "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__ANONYMOUS_ALLOWED"


def sdk_config() -> dict[str, Any]:
    """Azure Bot credentials when configured; otherwise local anonymous mode (Agents Playground / Emulator)."""
    env = dict(os.environ)
    if not env.get(CLIENT_ID_ENV):
        log.warning(
            "No Azure Bot credentials (%s) — running in LOCAL ANONYMOUS mode for Agents Playground / "
            "Bot Framework Emulator. Never expose this endpoint publicly.",
            CLIENT_ID_ENV,
        )
        env.setdefault(ANON_ENV, "true")
    return load_configuration_from_env(env)


class FactoryBot:
    def __init__(self) -> None:
        config = sdk_config()
        self.storage = MemoryStorage()
        self.connections = MsalConnectionManager(**config)
        self.adapter = CloudAdapter(connection_manager=self.connections)
        self.app = AgentApplication[TurnState](
            storage=self.storage,
            adapter=self.adapter,
            authorization=Authorization(self.storage, self.connections, **config),
            **config,
        )
        self.chat = FactoryChat(
            make_backend(
                os.environ.get("FACTORY_BACKEND", "langgraph"),
                url=os.environ.get("FACTORY_API_URL", "http://localhost:8080"),
                hub=os.environ.get("FACTORY_HUB_REPO"),
                token=os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"),
            ),
            store_path=Path(os.environ.get("FACTORY_BOT_STORE", ".factory-bot.json")),
        )
        self.references: dict[str, ConversationReference] = {}
        self.app_id = self.connections.get_default_connection_configuration().CLIENT_ID or ""
        self._register()

    def _register(self) -> None:
        @self.app.conversation_update("membersAdded")
        async def welcome(context: TurnContext, _state: TurnState) -> None:
            await context.send_activity(HELP)

        @self.app.activity("message")
        async def on_message(context: TurnContext, _state: TurnState) -> None:
            act = context.activity
            cid = act.conversation.id
            self.references[cid] = act.get_conversation_reference()
            user = (act.from_property.name if act.from_property else None) or "teams-user"
            value: dict[str, Any] = act.value if isinstance(act.value, dict) else {}
            await context.send_activity(Activity(type=ActivityTypes.typing))
            replies = await asyncio.to_thread(
                self.chat.handle,
                cid,
                user,
                text=value.get("changes") or act.text,
                action=value.get("factory_action"),
                engine=value.get("engine") or None,
            )
            for r in replies:
                await context.send_activity(to_activity(r))

    async def proactive(self, ref: ConversationReference, callback: Any) -> None:
        activity = ref.get_continuation_activity()
        if self.app_id:
            await self.adapter.continue_conversation(self.app_id, activity, callback)
        else:  # local anonymous mode (Agents Playground / Emulator): no token exchange
            await self.adapter.continue_conversation_with_claims(ClaimsIdentity({}, "anonymous"), activity, callback)

    async def pump(self, interval: float) -> None:
        """Proactively push factory events (questions, approval cards, progress, PR link) into Teams."""
        while True:
            for cid in self.chat.active():
                ref = self.references.get(cid)
                if ref is None:
                    continue
                try:
                    replies = await asyncio.to_thread(self.chat.updates, cid)
                except Exception as exc:
                    log.warning("poll %s failed: %s", cid, exc)
                    continue
                if replies:

                    async def send(ctx: TurnContext, rs: list[Reply] = replies) -> None:
                        for r in rs:
                            await ctx.send_activity(to_activity(r))

                    try:
                        await self.proactive(ref, send)
                    except Exception as exc:
                        log.warning("proactive send to %s failed: %s", cid, exc)
            await asyncio.sleep(interval)


def create_web_app(bot: FactoryBot | None = None) -> web.Application:
    bot = bot or FactoryBot()

    async def messages(req: web.Request) -> web.StreamResponse:
        return await start_agent_process(req, bot.app, bot.adapter)  # type: ignore[return-value]

    async def health(_req: web.Request) -> web.Response:
        return web.json_response({"ok": True, "active": bot.chat.active()})

    async def start_pump(app: web.Application) -> None:
        app["pump"] = asyncio.create_task(bot.pump(float(os.environ.get("FACTORY_POLL_SECONDS", "3"))))

    app = web.Application(middlewares=[jwt_authorization_middleware])
    app.router.add_post("/api/messages", messages)
    app.router.add_get("/health", health)
    app["agent_configuration"] = bot.connections.get_default_connection_configuration()
    app["agent_app"], app["adapter"] = bot.app, bot.adapter
    app.on_startup.append(start_pump)
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    web.run_app(create_web_app(), host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "3978")))


if __name__ == "__main__":
    main()
