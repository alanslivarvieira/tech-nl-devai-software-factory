"""Channel-agnostic chat logic shared by the Teams bot and the terminal client.

FactoryChat.handle(conversation, user, text=..., action=...) -> [Reply]   (user input)
FactoryChat.updates(conversation) -> [Reply]                              (factory → user, polled)
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .backend import ConversationBackend, FactoryEvent
from .cards import approval_card, escalation_card

HELP = """I'm the **AI software factory**. Tell me what to build and where:

- `build <what you want> in <owner/repo>[@branch]`
- `/new <owner/repo or path>[@branch] <what you want>`

While we talk: answer my questions, `/engine copilot|claude|codex` to pick the coding agent, `/approve` to freeze
the contract, `/status`, `/abort`. When I escalate: `/continue`, `/amend <change>`, `/abort`."""

BUILD_RE = re.compile(
    r"^(?:build|create|add|implement)\s+(?P<request>.+?)\s+in\s+(?P<repo>[\w./:@~\\-]+)\s*$", re.I | re.S
)
NEW_RE = re.compile(r"^/new\s+(?P<repo>\S+)\s+(?P<request>.+)$", re.I | re.S)
ENGINE_RE = re.compile(r"/engine\s+(copilot|claude|codex)\b", re.I)


@dataclass
class Reply:
    text: str = ""
    card: dict[str, Any] | None = None


@dataclass
class Conversation:
    feature: str | None = None
    cursor: Any = 0
    done: bool = False
    repo: str = ""
    history: list[str] = field(default_factory=list)


class FactoryChat:
    def __init__(self, backend: ConversationBackend, store_path: Path | None = None) -> None:
        self.backend = backend
        self.store_path = store_path
        self._lock = threading.Lock()
        self.conversations: dict[str, Conversation] = {}
        if store_path and store_path.exists():
            raw = json.loads(store_path.read_text(encoding="utf-8"))
            self.conversations = {k: Conversation(**v) for k, v in raw.items()}

    def _save(self) -> None:
        if self.store_path:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            self.store_path.write_text(
                json.dumps({k: v.__dict__ for k, v in self.conversations.items()}, indent=1), encoding="utf-8"
            )

    def conv(self, cid: str) -> Conversation:
        return self.conversations.setdefault(cid, Conversation())

    def active(self) -> list[str]:
        return [cid for cid, c in self.conversations.items() if c.feature and not c.done]

    # ----------------------------------------------------------------------------------------- input
    def handle(
        self, cid: str, user: str, text: str | None = None, action: str | None = None, engine: str | None = None
    ) -> list[Reply]:
        with self._lock:
            c = self.conv(cid)
            text = (text or "").strip()
            low = text.lower()
            if low in ("/help", "help", "hi", "hello") and not action:
                return [Reply(HELP)]
            if low == "/reset":
                self.conversations[cid] = Conversation()
                self._save()
                return [Reply("Forgot the current feature. Tell me what to build next.")]
            if not c.feature or c.done:
                return self._start(cid, user, text)
            if low == "/status":
                return [Reply(f"Working on feature `{c.feature}` in `{c.repo}`.")]
            engine = engine or None
            if m := ENGINE_RE.search(text):
                engine = m.group(1).lower()
                text = ENGINE_RE.sub("", text).strip()
            if not action and text.startswith("/"):
                word, _, rest = text[1:].partition(" ")
                if word.lower() in ("approve", "abort", "continue", "amend"):
                    action, text = word.lower(), rest
            try:
                self.backend.send(c.feature, text=text or None, action=action, user=user, engine=engine)
            except RuntimeError as exc:
                return [Reply(str(exc))]
            ack = {
                "approve": "✅ Approved — freezing the contract and starting the build. I'll keep you posted.",
                "abort": "🛑 Aborting.",
                "continue": "🔁 Continuing the build/verify loop.",
                "amend": "✏️ Back to the grill with your change.",
            }.get(action or "", "👍 Got it — thinking…")
            if engine:
                ack = f"🛠️ Coding agent: **{engine}**. " + ack
            return [Reply(ack)]

    def _start(self, cid: str, user: str, text: str) -> list[Reply]:
        m = NEW_RE.match(text) or BUILD_RE.match(text)
        if not m:
            return [Reply(HELP)]
        repo, _, base = m.group("repo").partition("@")
        request = m.group("request").strip()
        engine = None
        if em := ENGINE_RE.search(request):
            engine = em.group(1).lower()
            request = ENGINE_RE.sub("", request).strip()
        key = self.backend.start(request=request, repo=repo, base=base or "main", requester=user, engine=engine)
        self.conversations[cid] = Conversation(feature=key, repo=repo)
        self._save()
        return [
            Reply(
                f"🏭 On it! Feature `{key}` for `{repo}`. First I'll grill you until we have an executable "
                "contract (tests as data). Questions coming up…"
            )
        ]

    # ---------------------------------------------------------------------------------------- output
    def updates(self, cid: str) -> list[Reply]:
        with self._lock:
            c = self.conv(cid)
            if not c.feature or c.done:
                return []
            events, c.cursor = self.backend.poll(c.feature, c.cursor)
            replies = [r for e in events for r in self.render(e)]
            if any(e.final for e in events):
                c.done = True
            self._save()
            return replies

    @staticmethod
    def render(e: FactoryEvent) -> list[Reply]:
        if e.kind == "questions":
            qs = e.data.get("questions") or [e.message]
            notes = e.data.get("notes")
            return [Reply("🔥 **Grill time**\n\n" + "\n".join(qs) + (f"\n\n_{notes}_" if notes else ""))]
        if e.kind == "approval_needed":
            return [
                Reply(
                    card=approval_card(
                        e.data.get("contract_markdown", ""),
                        e.data.get("summary") or e.message,
                        e.data.get("problems") or [],
                    )
                )
            ]
        if e.kind == "escalation":
            return [Reply(card=escalation_card(e.data.get("reason") or e.message, e.data.get("report_markdown", "")))]
        if e.kind == "pr_opened":
            return [Reply(f"🎉 **Done!** Pull request ready for review: {e.data.get('pr_url') or e.message}")]
        if e.kind == "error":
            return [Reply(f"⚠️ {e.message}")]
        return [Reply(f"⏳ {e.message}")]
