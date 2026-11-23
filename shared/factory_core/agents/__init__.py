"""Coding-agent adapters (Copilot CLI, Claude Code, Codex) plus a scripted fake."""

from .base import (
    AgentRequest,
    AgentResult,
    AgentRunner,
    AgentSession,
    Executor,
    LocalExecutor,
    Usage,
    extract_json,
    read_only,
)
from .cli_runners import RUNNERS, ClaudeCodeRunner, CodexRunner, CopilotCliRunner, make_runner
from .fake import FakeAgentRunner

__all__ = [
    "RUNNERS",
    "AgentRequest",
    "AgentResult",
    "AgentRunner",
    "AgentSession",
    "ClaudeCodeRunner",
    "CodexRunner",
    "CopilotCliRunner",
    "Executor",
    "FakeAgentRunner",
    "LocalExecutor",
    "Usage",
    "extract_json",
    "make_runner",
    "read_only",
]
