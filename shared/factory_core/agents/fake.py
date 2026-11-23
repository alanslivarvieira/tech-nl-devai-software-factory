"""Deterministic stand-in for a coding agent: scripted per role. Used by tests, demos and CI."""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .base import AgentRequest, AgentResult, AgentRunner, Usage

Handler = Callable[[AgentRequest], Any]


@dataclass
class Call:
    role: str
    prompt: str
    workdir: str
    protected: list[str]


class FakeAgentRunner(AgentRunner):
    """``script`` maps a role to a handler or to a list of handlers consumed in order (the last one repeats).

    A handler receives the request (it may edit files in ``req.workdir``) and returns either text, a dict
    (structured answer) or ``None``.
    """

    engine = "fake"

    def __init__(self, script: dict[str, Handler | list[Handler]]) -> None:
        self.script = script
        self.calls: list[Call] = []
        self._pos: dict[str, int] = defaultdict(int)
        self._lock = threading.Lock()

    def _invoke(self, req: AgentRequest, prompt: str) -> AgentResult:
        with self._lock:
            self.calls.append(Call(req.role, prompt, str(req.workdir), list(req.protected)))
            entry = self.script.get(req.role)
            if entry is None:
                return AgentResult(ok=False, engine=self.engine, error=f"fake agent has no script for role {req.role}")
            if isinstance(entry, list):
                i = min(self._pos[req.role], len(entry) - 1)
                self._pos[req.role] += 1
                handler = entry[i]
            else:
                handler = entry
        out = handler(req)
        text = "" if isinstance(out, dict) else str(out or "done")
        usage = [self._synthetic_usage(req, prompt, json.dumps(out, default=str) if isinstance(out, dict) else text)]
        if isinstance(out, dict):
            return AgentResult(ok=True, engine=self.engine, data=out, text="", usage=usage)
        return AgentResult(ok=True, engine=self.engine, text=text, usage=usage)

    @staticmethod
    def _synthetic_usage(req: AgentRequest, prompt: str, answer: str) -> Usage:
        """Deterministic stand-in for CLI token reporting (~4 characters per token)."""
        return Usage(
            model=req.model or "fake-model",
            input_tokens=len(prompt) // 4 + 1,
            output_tokens=len(answer) // 4 + 1,
        )

    def calls_for(self, role: str) -> list[Call]:
        return [c for c in self.calls if c.role == role]
