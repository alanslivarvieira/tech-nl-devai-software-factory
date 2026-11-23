"""Coding-agent adapters.

The factory never talks to a model API directly: it drives the user's chosen coding-agent CLI in headless
mode (GitHub Copilot CLI, Claude Code, Codex). Every call is a *fresh session* — no resume — so each phase
(grill turn, build task, test audit) starts from a clean context window containing only what we pass in.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import subprocess
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ValidationError

from ..skills import deploy_skills, skill_globs
from ..stack import glob_match

Role = Literal["grill", "driver_writer", "planner", "builder", "reviewer", "auditor", "resolver", "finisher"]
RUN_DIR = ".factory/run"


@dataclass
class AgentRequest:
    role: Role
    prompt: str
    workdir: Path
    protected: list[str] = field(default_factory=list)  # globs relative to workdir, made read-only
    read_only: bool = False
    timeout_s: int = 1800
    schema: type[BaseModel] | None = None  # when set, the answer must be JSON valid for this model
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentResult:
    ok: bool
    engine: str
    text: str = ""
    data: Any = None
    exit_code: int = 0
    duration_s: float = 0.0
    error: str = ""


class Executor(Protocol):
    def run(
        self, argv: list[str], cwd: Path, stdin: str | None, env: dict[str, str], timeout: int
    ) -> tuple[int, str, str]: ...


class LocalExecutor:
    def run(
        self, argv: list[str], cwd: Path, stdin: str | None, env: dict[str, str], timeout: int
    ) -> tuple[int, str, str]:
        try:
            p = subprocess.run(
                argv,
                cwd=cwd,
                input=stdin,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env={**os.environ, **env},
            )
        except FileNotFoundError as exc:
            return 127, "", f"agent CLI not found: {exc}"
        except subprocess.TimeoutExpired:
            return 124, "", f"agent timed out after {timeout}s"
        return p.returncode, p.stdout, p.stderr


# ---------------------------------------------------------------------------------------- helpers
def extract_json(text: str) -> Any:
    """Last fenced ```json block, else the longest JSON object found in the text."""
    fences = [m for m in _fence_iter(text)]
    for body in reversed(fences):
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            continue
    dec = json.JSONDecoder()
    best: tuple[int, Any] | None = None
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, end = dec.raw_decode(text, i)
        except json.JSONDecodeError:
            continue
        span = end - i
        if best is None or span >= best[0]:
            best = (span, obj)
    if best is None:
        raise ValueError("no JSON object found in agent output")
    return best[1]


def _fence_iter(text: str) -> Iterator[str]:
    start = 0
    while True:
        i = text.find("```json", start)
        if i < 0:
            return
        j = text.find("```", i + 7)
        if j < 0:
            return
        yield text[i + 7 : j].strip()
        start = j + 3


@contextlib.contextmanager
def read_only(workdir: Path, globs: list[str]) -> Iterator[list[Path]]:
    """Best-effort OS-level protection for local runs (docker sandboxes mount these paths :ro).
    The guard + manifest are the real enforcement; this gives the agent immediate feedback."""
    locked: list[Path] = []
    if globs:
        for p in workdir.rglob("*"):
            if ".git" in p.parts or not p.is_file():
                continue
            rel = p.relative_to(workdir).as_posix()
            if any(glob_match(rel, g) for g in globs):
                p.chmod(p.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
                locked.append(p)
    try:
        yield locked
    finally:
        for p in locked:
            if p.exists():
                p.chmod(p.stat().st_mode | stat.S_IWUSR)


def ensure_run_dir(workdir: Path) -> Path:
    run = workdir / RUN_DIR
    run.mkdir(parents=True, exist_ok=True)
    exclude = workdir / ".git" / "info" / "exclude"
    if exclude.parent.is_dir():
        current = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if RUN_DIR not in current:
            exclude.write_text(current + f"\n{RUN_DIR}/\n", encoding="utf-8")
    return run


JSON_SUFFIX = """

---
OUTPUT FORMAT (mandatory): finish your answer with ONE fenced ```json block that validates against this
JSON Schema. Nothing after the block.
```json
{schema}
```
"""


class AgentRunner(ABC):
    engine: str = "abstract"

    @abstractmethod
    def _invoke(self, req: AgentRequest, prompt: str) -> AgentResult: ...

    def run(self, req: AgentRequest) -> AgentResult:
        req.protected = list(dict.fromkeys([*req.protected, *skill_globs(deploy_skills(req.workdir))]))
        prompt = req.prompt
        if req.schema is not None:
            prompt += JSON_SUFFIX.format(schema=json.dumps(req.schema.model_json_schema(), indent=1)[:12000])
        with read_only(req.workdir, req.protected):
            res = self._invoke(req, prompt)
            if req.schema is not None and res.ok:
                res = self._validate(req, res)
                if not res.ok and "schema" in res.error:
                    retry = prompt + f"\n\nYOUR PREVIOUS ANSWER WAS REJECTED: {res.error}\nAnswer again, JSON only."
                    res = self._validate(req, self._invoke(req, retry))
        return res

    @staticmethod
    def _validate(req: AgentRequest, res: AgentResult) -> AgentResult:
        if not res.ok:
            return res
        assert req.schema is not None
        try:
            raw = res.data if isinstance(res.data, dict) else extract_json(res.text)
            res.data = req.schema.model_validate(raw)
        except (ValueError, ValidationError) as exc:
            res.ok, res.error = False, f"schema validation failed: {str(exc)[:1500]}"
        return res
