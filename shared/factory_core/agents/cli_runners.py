"""Headless adapters for the three supported coding-agent CLIs.

Flags reflect the CLIs' documented headless modes; override/extend per engine with the environment
variables ``FACTORY_<ENGINE>_BIN`` (executable) and ``FACTORY_<ENGINE>_ARGS`` (extra args, shlex-split),
e.g. ``FACTORY_CLAUDE_ARGS="--model claude-sonnet-5-5"``.
"""

from __future__ import annotations

import json
import os
import shlex
import time
import uuid
from pathlib import Path

from .base import AgentRequest, AgentResult, AgentRunner, Executor, LocalExecutor, ensure_run_dir

READ_TOOLS = "Read,Glob,Grep"
WRITE_TOOLS = "Read,Glob,Grep,Edit,MultiEdit,Write,Bash"


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
        return AgentResult(
            ok=code == 0,
            engine=self.engine,
            text=text,
            exit_code=code,
            duration_s=time.monotonic() - start,
            error="" if code == 0 else (err or out)[-2000:],
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
        return argv + self.extra, prompt_file.read_text(encoding="utf-8")

    def parse(self, stdout: str, last_msg: Path) -> str:
        try:
            return str(json.loads(stdout).get("result", stdout))
        except (json.JSONDecodeError, AttributeError):
            return stdout


class CodexRunner(CliAgentRunner):
    """``codex exec`` (non-interactive). Workspace-write sandbox; read-only for grill/plan/audit roles."""

    engine = "codex"
    default_bin = "codex"

    def argv(self, req: AgentRequest, prompt_file: Path, last_msg: Path) -> tuple[list[str], str | None]:
        sandbox = "read-only" if req.read_only else "workspace-write"
        argv = (
            [self.bin, "exec", "--skip-git-repo-check", "--sandbox", sandbox, "--output-last-message", str(last_msg)]
            + self.extra
            + ["-"]
        )
        return argv, prompt_file.read_text(encoding="utf-8")

    def parse(self, stdout: str, last_msg: Path) -> str:
        return last_msg.read_text(encoding="utf-8") if last_msg.exists() else stdout


class CopilotCliRunner(CliAgentRunner):
    """GitHub Copilot CLI ``copilot -p``. The prompt is passed by file to avoid command-line limits."""

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
        ]
        if req.read_only:
            argv += ["--deny-tool", "write"]
        return argv + self.extra, None


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
