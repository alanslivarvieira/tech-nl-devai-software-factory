"""Runs feature threads in the background and exposes their state (shared by the API and the CLI)."""

from __future__ import annotations

import re
import threading
import uuid
from collections import defaultdict
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from factory_core.agents import AgentRunner

from .config import Settings
from .graph import build_graph
from .services import Services, normalize_repo_url

RECURSION_LIMIT = 10_000  # the build⇄verify loop is intentionally unbounded; the stall breaker asks a human


def make_checkpointer(settings: Settings) -> BaseCheckpointSaver:
    if not settings.database_url:
        return InMemorySaver()
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg_pool import ConnectionPool

    pool = ConnectionPool(
        conninfo=settings.database_url, max_size=10, open=True, kwargs={"autocommit": True, "prepare_threshold": 0}
    )
    saver = PostgresSaver(pool)  # type: ignore[arg-type]
    saver.setup()
    return saver


def feature_key(request: str) -> str:
    words = re.findall(r"[a-z0-9]+", request.lower())[:4]
    return "-".join(words + [uuid.uuid4().hex[:6]])


def _is_abort(answer: dict[str, Any] | str) -> bool:
    if isinstance(answer, str):
        return answer.strip().lower() in ("/abort", "abort")
    return str(answer.get("action") or "").lower() == "abort" or _is_abort(str(answer.get("text") or ""))


class FactoryRuntime:
    def __init__(
        self,
        settings: Settings | None = None,
        agent: AgentRunner | None = None,
        checkpointer: BaseCheckpointSaver | None = None,
        workers: int = 4,
    ) -> None:
        self.settings = settings or Settings()
        self.services = Services.from_settings(self.settings, agent)
        self.graph = build_graph(self.services, checkpointer or make_checkpointer(self.settings))
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="factory")
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._futures: dict[str, Future[Any]] = {}
        self._errors: dict[str, str] = {}

    @staticmethod
    def config(key: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": key}, "recursion_limit": RECURSION_LIMIT}

    def _run(self, key: str, payload: Any) -> None:
        with self._locks[key]:
            try:
                self._errors.pop(key, None)
                self.graph.invoke(payload, self.config(key))
            except Exception as exc:
                self._errors[key] = f"{type(exc).__name__}: {exc}"
                self.services.events.emit(key, "error", f"factory error: {exc}")

    def _submit(self, key: str, payload: Any, wait: bool) -> None:
        fut = self._pool.submit(self._run, key, payload)
        self._futures[key] = fut
        if wait:
            fut.result()

    def start(
        self,
        *,
        request: str,
        repo_url: str,
        base_branch: str = "main",
        engine: str | None = None,
        requester: str = "human",
        notify_url: str | None = None,
        key: str | None = None,
        wait: bool = False,
    ) -> str:
        key = key or feature_key(request)
        if notify_url:
            self.services.events.per_feature_url[key] = notify_url
        state = {
            "feature_key": key,
            "request": request,
            "repo_url": normalize_repo_url(repo_url),
            "base_branch": base_branch,
            "requester": requester,
            "engine": engine or self.settings.factory.default_engine,
            "engine_locked": bool(engine),
        }
        self._submit(key, state, wait)
        return key

    def reply(self, key: str, answer: dict[str, Any] | str, wait: bool = False) -> None:
        if self.busy(key):
            raise RuntimeError("the factory is still working on this feature; wait for its next question")
        snap = self.graph.get_state(self.config(key))
        waiting = any(getattr(t, "interrupts", ()) for t in snap.tasks)
        if snap.next and not waiting:
            if _is_abort(answer):
                self._errors.pop(key, None)
                self.services.events.emit(key, "aborted", "🛑 Aborted.", terminal=True)
                return
            # The last run stopped mid-step (an error, or the app restarted): any other message re-runs that step
            # from its checkpoint instead of being lost.
            self.services.events.emit(key, "retry", f"Retrying {', '.join(snap.next)}")
            self._submit(key, None, wait)
            return
        self._submit(key, Command(resume=answer), wait)

    def busy(self, key: str) -> bool:
        fut = self._futures.get(key)
        return bool(fut and not fut.done())

    def status(self, key: str) -> dict[str, Any]:
        snap = self.graph.get_state(self.config(key))
        values = dict(snap.values or {})
        interrupts = [i.value for t in snap.tasks for i in getattr(t, "interrupts", ())]
        return {
            "feature_key": key,
            "exists": bool(values),
            "busy": self.busy(key),
            "status": values.get("status"),
            "engine": values.get("engine"),
            "stack": values.get("stack"),
            "iteration": values.get("iteration"),
            "waiting_for": interrupts[0] if interrupts and not self.busy(key) else None,
            # stopped mid-step (an error or an app restart); a message re-runs that step, see reply()
            "stopped": bool(snap.next) and not interrupts and not self.busy(key),
            "pr_url": values.get("pr_url"),
            "manifest_hash": values.get("manifest_hash"),
            "last_report": values.get("last_report"),
            "error": self._errors.get(key),
            "next": list(snap.next),
        }

    def wait(self, key: str) -> None:
        fut = self._futures.get(key)
        if fut:
            fut.result()
