"""Services injected into the graph: agents, sandbox, event log/notifications and the git host."""

from __future__ import annotations

import datetime as dt
import logging
import re
import subprocess
import threading
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from factory_core.agents import AgentRequest, AgentResult, AgentRunner, make_runner
from factory_core.pipeline import git
from factory_core.reports.usage import UsageLog

from .config import Settings
from .sandbox import make_executor

log = logging.getLogger("factory")


class EventLog:
    """Per-feature event stream (API polling/SSE) with optional webhook fan-out (e.g. the Teams bot)."""

    def __init__(self, notify_url: str | None = None) -> None:
        self._events: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._cond = threading.Condition()
        self.notify_url = notify_url
        self.per_feature_url: dict[str, str] = {}

    def emit(self, feature_key: str, kind: str, message: str, **data: Any) -> None:
        event = {
            "seq": 0,
            "at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            "feature": feature_key,
            "kind": kind,
            "message": message,
            "data": data,
        }
        with self._cond:
            event["seq"] = len(self._events[feature_key]) + 1
            self._events[feature_key].append(event)
            self._cond.notify_all()
        log.info("[%s] %s: %s", feature_key, kind, message)
        url = self.per_feature_url.get(feature_key) or self.notify_url
        if url:
            try:
                httpx.post(url, json=event, timeout=5)
            except httpx.HTTPError as exc:  # notification must never break the factory
                log.warning("notify %s failed: %s", url, exc)

    def _from(self, feature_key: str, seq: int) -> int:
        # The log is in memory: after an app restart a client's cursor can be ahead of it; replay from the start
        # rather than waiting forever for numbers the new log has not reached.
        return 0 if seq > len(self._events[feature_key]) else seq

    def since(self, feature_key: str, seq: int = 0) -> list[dict[str, Any]]:
        with self._cond:
            seq = self._from(feature_key, seq)
            return [e for e in self._events[feature_key] if e["seq"] > seq]

    def wait(self, feature_key: str, seq: int, timeout: float) -> list[dict[str, Any]]:
        with self._cond:
            seq = self._from(feature_key, seq)
            self._cond.wait_for(lambda: len(self._events[feature_key]) > seq, timeout=timeout)
            return [e for e in self._events[feature_key] if e["seq"] > seq]


class GitHost:
    """Push the feature branch and open a PR; for non-GitHub targets write a local PR.md (and push the branch
    back into the source repo when it is a local git repository)."""

    def __init__(self, token: str | None, api: str) -> None:
        self.token, self.api = token, api

    @staticmethod
    def github_slug(url: str) -> str | None:
        m = re.search(r"github\.com[:/]([^/]+)/([^/.]+?)(?:\.git)?/?$", url)
        return f"{m.group(1)}/{m.group(2)}" if m else None

    def open_pr(self, workdir: Path, repo_url: str, branch: str, base: str, title: str, body: str) -> str:
        slug = self.github_slug(repo_url)
        if not (slug and self.token):
            header = f"# {title}\n\nbranch `{branch}` → `{base}`"
            local = local_git_repo(repo_url)
            if local is not None:
                git(workdir, "push", "-q", str(local), f"HEAD:refs/heads/{branch}")
                header += f"\n\nPushed to `{local}`. Review with `git -C \"{local}\" diff {base}...{branch}`."
            path = workdir.parent / "PR.md"
            path.write_text(f"{header}\n\n{body}", encoding="utf-8")
            return path.as_uri()
        git(workdir, "push", "-u", "origin", f"HEAD:refs/heads/{branch}")
        resp = httpx.post(
            f"{self.api}/repos/{slug}/pulls",
            timeout=30,
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json"},
            json={"title": title, "head": branch, "base": base, "body": body, "draft": False},
        )
        resp.raise_for_status()
        return str(resp.json()["html_url"])


@dataclass
class Services:
    settings: Settings
    events: EventLog
    githost: GitHost
    agent_factory: Callable[[str], AgentRunner]
    _agents: dict[str, AgentRunner] = field(default_factory=dict)
    usage: UsageLog = field(default_factory=UsageLog)

    def agent(self, engine: str) -> AgentRunner:
        if engine not in self._agents:
            self._agents[engine] = self.agent_factory(engine)
        return self._agents[engine]

    def run_agent(self, feature_key: str, engine: str, req: AgentRequest, task: str | None = None) -> AgentResult:
        """Run one agent step with the model/effort configured for ``engine`` × ``req.role`` and record usage."""
        choice = self.settings.factory.choice_for(engine, req.role)
        req.model = req.model or choice.model
        req.effort = req.effort or choice.effort
        res = self.agent(engine).run(req)
        self.usage.record(feature_key, res.sessions, task=task)
        for s in res.sessions:
            if s.fallback:
                self.events.emit(
                    feature_key,
                    "warning",
                    f"{engine} rejected model={choice.model} effort={choice.effort} for `{req.role}`; "
                    "used the CLI default model instead",
                )
        return res

    def workspace(self, feature_key: str) -> Path:
        path = self.settings.workspaces / feature_key
        path.mkdir(parents=True, exist_ok=True)
        return path

    @classmethod
    def from_settings(cls, settings: Settings, agent: AgentRunner | None = None) -> Services:
        if agent is not None:
            factory: Callable[[str], AgentRunner] = lambda _engine: agent
        elif settings.fake_agent:
            from factory_core.testing import greeter_fake_agent

            fake = greeter_fake_agent()
            factory = lambda _engine: fake
        else:
            executor = make_executor(settings.sandbox, settings.sandbox_image)
            factory = lambda engine: make_runner(engine, executor)
        return cls(
            settings=settings,
            events=EventLog(settings.notify_url),
            githost=GitHost(settings.github_token, settings.github_api),
            agent_factory=factory,
        )


GITHUB_SHORTHAND = re.compile(r"^[\w.-]+/[\w.-]+$")


def normalize_repo_url(repo_url: str) -> str:
    """Make local paths absolute so later steps (clone, verify, PR) do not depend on the working directory,
    and expand the chat shorthand ``owner/repo`` to its github.com clone URL."""
    src = Path(repo_url)
    if src.exists():
        return str(src.resolve())
    if GITHUB_SHORTHAND.match(repo_url):
        return f"https://github.com/{repo_url.removesuffix('.git')}.git"
    return repo_url


def local_git_repo(repo_url: str) -> Path | None:
    src = Path(repo_url)
    return src.resolve() if (src / ".git").exists() else None


def clone(repo_url: str, dest: Path, base_branch: str) -> None:
    """Clone a git URL / local repo. A plain local folder (no .git) is imported as a fresh repository."""
    src = Path(repo_url)
    if src.exists() and not (src / ".git").exists():
        import shutil

        shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".venv", "node_modules", "__pycache__"))
        git(dest, "init", "-q", "-b", base_branch)
        git(dest, "add", "-A")
        git(dest, "commit", "-q", "-m", "import target folder")
        return
    url = normalize_repo_url(repo_url)
    subprocess.run(["git", "clone", "-q", "--branch", base_branch, url, str(dest)], check=True, capture_output=True)
