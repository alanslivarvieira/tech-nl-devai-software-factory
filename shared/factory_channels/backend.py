"""Backends a chat channel can talk to. Both expose the same conversation semantics:

start a feature → receive events (questions / approval_needed / escalation / progress / pr_opened / error)
→ send answers or actions (approve, abort, continue, amend, engine).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
import yaml

ACTIONS = {"approve", "abort", "continue", "amend", "revise"}


@dataclass
class FactoryEvent:
    kind: str  # questions | approval_needed | escalation | progress | pr_opened | error
    message: str
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def final(self) -> bool:
        return self.kind == "pr_opened" or (self.kind == "progress" and self.data.get("terminal", False))


class ConversationBackend(Protocol):
    def start(self, *, request: str, repo: str, base: str, requester: str, engine: str | None = None) -> str: ...
    def send(
        self,
        key: str,
        *,
        text: str | None = None,
        action: str | None = None,
        user: str | None = None,
        engine: str | None = None,
    ) -> None: ...
    def poll(self, key: str, cursor: Any) -> tuple[list[FactoryEvent], Any]: ...


# ------------------------------------------------------------------------------------------ LangGraph
class LangGraphBackend:
    PASS = {"questions", "approval_needed", "escalation", "pr_opened", "error"}

    def __init__(self, base_url: str, client: httpx.Client | None = None) -> None:
        self.client = client or httpx.Client(base_url=base_url.rstrip("/"), timeout=60)

    def start(self, *, request: str, repo: str, base: str, requester: str, engine: str | None = None) -> str:
        r = self.client.post(
            "/features",
            json={"request": request, "repo": repo, "base_branch": base, "requester": requester, "engine": engine},
        )
        r.raise_for_status()
        return str(r.json()["feature_key"])

    def send(
        self,
        key: str,
        *,
        text: str | None = None,
        action: str | None = None,
        user: str | None = None,
        engine: str | None = None,
    ) -> None:
        payload = {k: v for k, v in {"text": text, "action": action, "user": user, "engine": engine}.items() if v}
        r = self.client.post(f"/features/{key}/messages", json=payload)
        if r.status_code == 409:
            raise RuntimeError("The factory is still working — I'll ping you when it needs you.")
        r.raise_for_status()

    def poll(self, key: str, cursor: Any) -> tuple[list[FactoryEvent], Any]:
        r = self.client.get(f"/features/{key}/events", params={"after": int(cursor or 0)})
        r.raise_for_status()
        events = []
        for e in r.json():
            cursor = e["seq"]
            kind = e["kind"] if e["kind"] in self.PASS else "progress"
            events.append(FactoryEvent(kind=kind, message=e["message"], data=dict(e.get("data") or {})))
        return events, cursor


# ---------------------------------------------------------------------------------------------- gh-aw
def request_body(repo: str, base: str, request: str, requester: str) -> str:
    """Must match gh-aw-factory/scripts/ghaw_lib.request_body (tested)."""
    spec = yaml.safe_dump(
        {"repo": repo, "base": base, "request": request, "requester": requester}, sort_keys=False, allow_unicode=True
    )
    return (
        f"**Feature request** from {requester} for `{repo}` (base `{base}`)\n\n> {request}\n\n"
        f"```yaml factory-request\n{spec}```\n\n"
        "The factory will grill you in the comments. Commands: `/engine copilot|claude|codex`, `/approve`, "
        "`/continue`, `/amend <change>`, `/abort`.\n\n<!-- factory:state {} -->\n"
    )


class GhAwBackend:
    """Relays the chat into a factory issue of the hub repo. ``token`` belongs to a service account listed in
    the hub's FACTORY_RELAY_LOGINS (the orchestrator then treats relayed messages as a collaborator's)."""

    MARKER = re.compile(r"<!-- factory:(\w+)")
    PR = re.compile(r"https://\S+/pull/\d+")

    def __init__(
        self,
        hub_repo: str,
        token: str,
        api: str = "https://api.github.com",
        ref: str = "main",
        default_engine: str = "copilot",
        client: httpx.Client | None = None,
    ) -> None:
        self.hub, self.ref, self.default_engine = hub_repo, ref, default_engine
        self.client = client or httpx.Client(
            base_url=api,
            timeout=30,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        )
        self._me: str | None = None

    def _login(self) -> str:
        if self._me is None:
            self._me = str(self.client.get("/user").json().get("login", ""))
        return self._me

    def start(self, *, request: str, repo: str, base: str, requester: str, engine: str | None = None) -> str:
        engine = engine or self.default_engine
        r = self.client.post(
            f"/repos/{self.hub}/issues",
            json={
                "title": f"[factory] {request[:70]}",
                "body": request_body(repo, base, request, requester),
                "labels": ["factory", "phase:grill", f"engine:{engine}", "iter:0"],
            },
        )
        r.raise_for_status()
        number = r.json()["number"]
        self.client.post(
            f"/repos/{self.hub}/actions/workflows/factory-grill-{engine}.lock.yml/dispatches",
            json={"ref": self.ref, "inputs": {"issue": str(number)}},
        ).raise_for_status()
        return str(number)

    def send(
        self,
        key: str,
        *,
        text: str | None = None,
        action: str | None = None,
        user: str | None = None,
        engine: str | None = None,
    ) -> None:
        body = (text or "").strip()
        if action in ("approve", "abort", "continue"):
            body = f"/{action}"
        elif action in ("amend", "revise"):
            body = f"/amend {body}" if action == "amend" else body
        if engine:
            body = f"/engine {engine}\n{body}".strip()
        if user:
            body += f"\n\n<sub>relayed from Teams for {user}</sub>"
        self.client.post(f"/repos/{self.hub}/issues/{key}/comments", json={"body": body}).raise_for_status()

    def poll(self, key: str, cursor: Any) -> tuple[list[FactoryEvent], Any]:
        r = self.client.get(f"/repos/{self.hub}/issues/{key}/comments", params={"per_page": 100})
        r.raise_for_status()
        events: list[FactoryEvent] = []
        last = int(cursor or 0)
        me = self._login()
        for c in r.json():
            if c["id"] <= last:
                continue
            last = c["id"]
            login = c["user"]["login"]
            if login == me or c["user"].get("type") != "Bot":
                continue  # our own relayed messages and other humans
            events.append(self.to_event(c["body"]))
        return events, last

    @classmethod
    def to_event(cls, body: str) -> FactoryEvent:
        m = cls.MARKER.search(body)
        kind = m.group(1) if m else None
        clean = re.sub(r"<details>.*?</details>", "", body, flags=re.S)
        clean = re.sub(r"<!--.*?-->", "", clean, flags=re.S).strip()
        if kind == "draft":
            return FactoryEvent("approval_needed", "Draft contract ready for review", {"contract_markdown": body})
        if "Escalation" in body or "contradicts itself" in body:
            return FactoryEvent("escalation", clean, {"reason": clean})
        if pr := cls.PR.search(body):
            if "Pull request ready" in body:
                return FactoryEvent("pr_opened", clean, {"pr_url": pr.group(0)})
        if kind in ("verify", "plan", "audit", "freezefailed") or body.startswith(("🔒", "✅", "🛠️", "🛡️", "⚠️")):
            return FactoryEvent("progress", clean, {"terminal": "Aborted" in body})
        if body.startswith("🛑"):
            return FactoryEvent("progress", clean, {"terminal": True})
        return FactoryEvent("questions", clean, {"questions": [clean]})


def make_backend(kind: str, **kw: Any) -> ConversationBackend:
    if kind == "langgraph":
        return LangGraphBackend(kw["url"])
    if kind == "gh-aw":
        return GhAwBackend(
            kw["hub"], kw["token"], ref=kw.get("ref", "main"), default_engine=kw.get("engine") or "copilot"
        )
    raise ValueError(f"unknown backend {kind!r}")
