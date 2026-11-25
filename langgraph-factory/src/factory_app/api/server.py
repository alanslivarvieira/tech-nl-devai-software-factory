"""HTTP API used by every channel (Teams bot, CLI chat, curl, a web page).

POST /features                    {request, repo, base_branch?, engine?, requester?, notify_url?}
POST /features/{key}/messages     {text?, action?: approve|revise|abort|continue|amend, engine?, user?}
GET  /features/{key}              status + what the factory is waiting for
GET  /features/{key}/events?after=N[&wait=25]   long-poll event log
GET  /features/{key}/stream       Server-Sent Events
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..runtime import FactoryRuntime

FINISHED = {"pr_opened", "aborted"}


class FeatureRequest(BaseModel):
    request: str
    repo: str
    base_branch: str = "main"
    engine: str | None = None
    requester: str = "human"
    notify_url: str | None = None


class Message(BaseModel):
    text: str | None = None
    action: str | None = None
    engine: str | None = None
    user: str | None = None


def create_app(runtime: FactoryRuntime | None = None) -> FastAPI:
    rt = runtime or FactoryRuntime()
    app = FastAPI(title="AI Software Factory (LangGraph)", version="0.1.0")
    app.state.runtime = rt

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "fake_agent": rt.settings.fake_agent, "sandbox": rt.settings.sandbox}

    @app.post("/features", status_code=202)
    def create(req: FeatureRequest) -> dict[str, Any]:
        key = rt.start(
            request=req.request,
            repo_url=req.repo,
            base_branch=req.base_branch,
            engine=req.engine,
            requester=req.requester,
            notify_url=req.notify_url,
        )
        return {"feature_key": key}

    @app.post("/features/{key}/messages", status_code=202)
    def message(key: str, msg: Message) -> dict[str, Any]:
        st = rt.status(key)
        if not st["exists"]:
            raise HTTPException(404, "unknown feature")
        if st["status"] in FINISHED:
            raise HTTPException(410, f"this feature has already finished ({st['status']})")
        if st["busy"] or not (st["waiting_for"] or st["stopped"]):
            raise HTTPException(409, "the factory is working; it will ask when it needs you")
        rt.reply(key, msg.model_dump(exclude_none=True))
        return {"accepted": True}

    @app.get("/features/{key}")
    def get(key: str) -> dict[str, Any]:
        st = rt.status(key)
        if not st["exists"] and not st["busy"]:
            raise HTTPException(404, "unknown feature")
        return st

    @app.get("/features/{key}/events")
    def events(key: str, after: int = 0, wait: float = 0) -> list[dict[str, Any]]:
        if wait:
            return rt.services.events.wait(key, after, timeout=min(wait, 30))
        return rt.services.events.since(key, after)

    @app.get("/features/{key}/stream")
    def stream(key: str) -> StreamingResponse:
        def gen() -> Iterator[str]:
            seq = 0
            while True:
                for e in rt.services.events.wait(key, seq, timeout=15):
                    seq = e["seq"]
                    yield f"id: {seq}\nevent: {e['kind']}\ndata: {json.dumps(e)}\n\n"
                yield ": keep-alive\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app


def main() -> None:
    import uvicorn

    uvicorn.run(create_app(), host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8080")))


if __name__ == "__main__":
    main()
