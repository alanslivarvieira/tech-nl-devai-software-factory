"""Report rendering (Markdown for PRs, chat and fresh agent sessions)."""

from .render import chat_summary, contract_markdown, failure_brief, gates_table, pr_body, report_markdown

__all__ = ["chat_summary", "contract_markdown", "failure_brief", "gates_table", "pr_body", "report_markdown"]
