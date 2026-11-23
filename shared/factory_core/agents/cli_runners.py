"""Headless adapters for the three supported coding-agent CLIs.

Flags reflect the CLIs' documented headless modes; override/extend per engine with the environment
variables ``FACTORY_<ENGINE>_BIN`` (executable) and ``FACTORY_<ENGINE>_ARGS`` (extra args, shlex-split).
Model and reasoning effort per agent role come from the factory config (``models:``) and are appended after
``FACTORY_<ENGINE>_ARGS``. Each adapter also extracts the token usage the CLI reports.
"""

from __future__ import annotations

import json
import os
import shlex
import time
import uuid
from pathlib import Path
from typing import Any

from .base import AgentRequest, AgentResult, AgentRunner, Executor, LocalExecutor, Usage, ensure_run_dir

READ_TOOLS = "Read,Glob,Grep,Skill"
WRITE_TOOLS = "Read,Glob,Grep,Edit,MultiEdit,Write,Bash,Skill"


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def usage_file(last_msg: Path) -> Path:
    return last_msg.with_name(last_msg.name.removesuffix(".last.txt") + ".usage.json")


class CliAgentRunner(AgentRunner):
    engine = "cli"
    default_bin = ""

    def __init__(self, executor: Executor | None = None) -> None:
        self.executor = executor or LocalExecutor()
        key = self.engine.upper()
        self.bin = os.environ.get(f"FACTORY_{key}_BIN", self.default_bin)
        self.extra = shlex.split(os.environ.get(f"FACTORY_{key}_ARGS", ""))

    def argv(self, req: AgentRequest, prompt_file: Path, last_msg: Path) -> tuple[list[str], str | None]:
        raise NotImplementedError

    def parse(self, stdout: str, last_msg: Path) -> str:
        return stdout

    def usage(self, stdout: str, last_msg: Path) -> list[Usage]:
        return []

    def _invoke(self, req: AgentRequest, prompt: str) -> AgentResult:
        run = ensure_run_dir(req.workdir)
        tag = f"{req.role}-{uuid.uuid4().hex[:8]}"
        prompt_file = run / f"{tag}.prompt.md"
        prompt_file.write_text(prompt, encoding="utf-8")
        last_msg = run / f"{tag}.last.txt"
        argv, stdin = self.argv(req, prompt_file, last_msg)
        start = time.monotonic()
        code, out, err = self.executor.run(argv, req.workdir, stdin, {"FACTORY_ROLE": req.role}, req.timeout_s)
        (run / f"{tag}.log").write_text(f"$ {' '.join(argv)}\n--- stdout\n{out}\n--- stderr\n{err}", encoding="utf-8")
        text = self.parse(out, last_msg)
        try:
            usage = self.usage(out, last_msg)
        except (OSError, ValueError, AttributeError, TypeError):  # usage is informative; never fail the agent on it
            usage = []
        return AgentResult(
            ok=code == 0,
            engine=self.engine,
            text=text,
            exit_code=code,
            duration_s=time.monotonic() - start,
            error="" if code == 0 else (err or out)[-2000:],
            usage=usage,
        )


class ClaudeCodeRunner(CliAgentRunner):
    """``claude -p`` (print mode). Deny rules give the agent immediate feedback on protected paths."""

    engine = "claude"
    default_bin = "claude"

    def argv(self, req: AgentRequest, prompt_file: Path, last_msg: Path) -> tuple[list[str], str | None]:
        argv = [
            self.bin,
            "-p",
            "--output-format",
            "json",
            "--permission-mode",
            "acceptEdits",
            "--allowedTools",
            READ_TOOLS if req.read_only else WRITE_TOOLS,
        ]
        deny = [f"{tool}({g})" for g in req.protected for tool in ("Edit", "Write", "MultiEdit")]
        deny.append("Bash(git push:*)")
        argv += ["--disallowedTools", ",".join(deny)]
        argv += self.extra
        if req.model:
            argv += ["--model", req.model]
        if req.effort:
            argv += ["--effort", req.effort]
        return argv, prompt_file.read_text(encoding="utf-8")

    def parse(self, stdout: str, last_msg: Path) -> str:
        try:
            return str(json.loads(stdout).get("result", stdout))
        except (json.JSONDecodeError, AttributeError):
            return stdout

    def usage(self, stdout: str, last_msg: Path) -> list[Usage]:
        """``--output-format json``: ``modelUsage`` per model (incl. subagents), else the aggregate ``usage``."""
        try:
            doc = json.loads(stdout)
        except json.JSONDecodeError:
            return []
        per_model = doc.get("modelUsage") or {}
        if per_model:
            return [
                Usage(
                    model=name,
                    input_tokens=_int(m.get("inputTokens")),
                    output_tokens=_int(m.get("outputTokens")),
                    cache_read_tokens=_int(m.get("cacheReadInputTokens")),
                    cache_write_tokens=_int(m.get("cacheCreationInputTokens")),
                    cost_usd=_float(m.get("costUSD")),
                )
                for name, m in per_model.items()
            ]
        u = doc.get("usage") or {}
        if not u:
            return []
        return [
            Usage(
                input_tokens=_int(u.get("input_tokens")),
                output_tokens=_int(u.get("output_tokens")),
                cache_read_tokens=_int(u.get("cache_read_input_tokens")),
                cache_write_tokens=_int(u.get("cache_creation_input_tokens")),
                cost_usd=_float(doc.get("total_cost_usd")),
            )
        ]


class CodexRunner(CliAgentRunner):
    """``codex exec --json`` (non-interactive). Workspace-write sandbox; read-only for grill/plan/audit roles.
    Codex does not report which model ran, so usage rows carry the configured model (or none)."""

    engine = "codex"
    default_bin = "codex"

    def argv(self, req: AgentRequest, prompt_file: Path, last_msg: Path) -> tuple[list[str], str | None]:
        sandbox = "read-only" if req.read_only else "workspace-write"
        argv = [
            self.bin,
            "exec",
            "--skip-git-repo-check",
            "--json",
            "--sandbox",
            sandbox,
            "--output-last-message",
            str(last_msg),
            *self.extra,
        ]
        if req.model:
            argv += ["--model", req.model]
        if req.effort:
            argv += ["-c", f'model_reasoning_effort="{req.effort}"']
        return argv + ["-"], prompt_file.read_text(encoding="utf-8")

    @staticmethod
    def _events(stdout: str) -> list[dict[str, Any]]:
        events = []
        for line in stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return events

    def parse(self, stdout: str, last_msg: Path) -> str:
        if last_msg.exists():
            return last_msg.read_text(encoding="utf-8")
        messages = [
            e["item"].get("text", "")
            for e in self._events(stdout)
            if e.get("type") == "item.completed" and (e.get("item") or {}).get("type") == "agent_message"
        ]
        return messages[-1] if messages else stdout

    def usage(self, stdout: str, last_msg: Path) -> list[Usage]:
        turns = [e.get("usage") or {} for e in self._events(stdout) if e.get("type") == "turn.completed"]
        if not turns:
            return []
        return [
            Usage(
                input_tokens=sum(_int(u.get("input_tokens")) for u in turns),
                output_tokens=sum(_int(u.get("output_tokens")) for u in turns),
                cache_read_tokens=sum(_int(u.get("cached_input_tokens")) for u in turns),
                reasoning_tokens=sum(_int(u.get("reasoning_output_tokens")) for u in turns),
            )
        ]


class CopilotCliRunner(CliAgentRunner):
    """GitHub Copilot CLI ``copilot -p``. The prompt is passed by file to avoid command-line limits; usage
    statistics are written by ``--usage-output-file`` next to it."""

    engine = "copilot"
    default_bin = "copilot"

    def argv(self, req: AgentRequest, prompt_file: Path, last_msg: Path) -> tuple[list[str], str | None]:
        rel = prompt_file.relative_to(req.workdir).as_posix()
        argv = [
            self.bin,
            "-p",
            f"Read the file {rel} and follow its instructions exactly.",
            "--no-color",
            "--allow-all-tools",
            "--deny-tool",
            "shell(git push)",
            "--usage-output-file",
            str(usage_file(last_msg)),
        ]
        if req.read_only:
            argv += ["--deny-tool", "write"]
        argv += self.extra
        if req.model:
            argv += ["--model", req.model]
        if req.effort:
            argv += ["--reasoning-effort", req.effort]
        return argv, None

    def usage(self, stdout: str, last_msg: Path) -> list[Usage]:
        path = usage_file(last_msg)
        if not path.exists():
            return []
        doc = json.loads(path.read_text(encoding="utf-8"))
        out = []
        for name, m in (doc.get("modelMetrics") or {}).items():
            u = m.get("usage") or {}
            nano = m.get("totalNanoAiu")
            out.append(
                Usage(
                    model=name,
                    input_tokens=_int(u.get("inputTokens")),
                    output_tokens=_int(u.get("outputTokens")),
                    cache_read_tokens=_int(u.get("cacheReadTokens")),
                    cache_write_tokens=_int(u.get("cacheWriteTokens")),
                    reasoning_tokens=_int(u.get("reasoningTokens")),
                    premium_requests=_float((m.get("requests") or {}).get("cost")),
                    ai_credits=None if nano is None else _int(nano) / 1e9,
                )
            )
        return out


RUNNERS: dict[str, type[CliAgentRunner]] = {
    "claude": ClaudeCodeRunner,
    "codex": CodexRunner,
    "copilot": CopilotCliRunner,
}


def make_runner(engine: str, executor: Executor | None = None) -> CliAgentRunner:
    try:
        return RUNNERS[engine](executor)
    except KeyError:
        raise ValueError(f"unknown engine {engine!r}; choose one of {sorted(RUNNERS)}") from None
