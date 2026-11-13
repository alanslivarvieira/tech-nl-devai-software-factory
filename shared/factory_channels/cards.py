"""Adaptive Cards (v1.5) for Teams. Buttons submit ``{"factory_action": ...}`` back to the bot."""

from __future__ import annotations

import re
from typing import Any

FENCE = re.compile(r"```(\w+) file=(\S+)\n(.*?)```", re.S)


def _blocks(contract_markdown: str, limit: int = 6000) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for _lang, path, body in FENCE.findall(contract_markdown):
        items.append({"type": "TextBlock", "text": f"**{path}**", "wrap": True, "spacing": "Medium"})
        text = body if len(body) <= limit else body[:limit] + "\n…"
        items.append({"type": "TextBlock", "text": text, "wrap": True, "fontType": "Monospace", "size": "Small"})
    return items or [{"type": "TextBlock", "text": contract_markdown[:limit], "wrap": True}]


def approval_card(contract_markdown: str, summary: str, problems: list[str]) -> dict[str, Any]:
    body: list[dict[str, Any]] = [
        {"type": "TextBlock", "text": "📜 Draft contract — tests as data", "weight": "Bolder", "size": "Large"},
        {
            "type": "TextBlock",
            "text": summary
            or "Review the examples below. They become the frozen contract the "
            "coding agent must satisfy (it cannot edit them).",
            "wrap": True,
        },
    ]
    if problems:
        body.append(
            {
                "type": "TextBlock",
                "color": "Warning",
                "wrap": True,
                "text": "Spec critic notes:\n" + "\n".join(f"- {p}" for p in problems),
            }
        )
    body.append({"type": "Container", "style": "emphasis", "items": _blocks(contract_markdown)})
    body.append(
        {
            "type": "Input.ChoiceSet",
            "id": "engine",
            "label": "Coding agent",
            "value": "",
            "choices": [
                {"title": "(keep)", "value": ""},
                {"title": "GitHub Copilot CLI", "value": "copilot"},
                {"title": "Claude Code", "value": "claude"},
                {"title": "Codex", "value": "codex"},
            ],
        }
    )
    body.append({"type": "Input.Text", "id": "changes", "isMultiline": True, "placeholder": "What should change?"})
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.5",
        "body": body,
        "actions": [
            {
                "type": "Action.Submit",
                "title": "✅ Approve & build",
                "style": "positive",
                "data": {"factory_action": "approve"},
            },
            {"type": "Action.Submit", "title": "✏️ Request changes", "data": {"factory_action": "revise"}},
            {"type": "Action.Submit", "title": "🛑 Abort", "style": "destructive", "data": {"factory_action": "abort"}},
        ],
    }


def escalation_card(reason: str, report_markdown: str) -> dict[str, Any]:
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.5",
        "body": [
            {"type": "TextBlock", "text": "🚨 The factory needs your call", "weight": "Bolder", "size": "Large"},
            {"type": "TextBlock", "text": reason, "wrap": True},
            {
                "type": "TextBlock",
                "text": report_markdown[:4000],
                "wrap": True,
                "fontType": "Monospace",
                "size": "Small",
            },
            {
                "type": "Input.Text",
                "id": "changes",
                "isMultiline": True,
                "placeholder": "For 'amend': what should change in the contract?",
            },
        ],
        "actions": [
            {"type": "Action.Submit", "title": "🔁 Keep looping", "data": {"factory_action": "continue"}},
            {"type": "Action.Submit", "title": "✏️ Amend contract", "data": {"factory_action": "amend"}},
            {"type": "Action.Submit", "title": "🛑 Abort", "style": "destructive", "data": {"factory_action": "abort"}},
        ],
    }


def card_to_text(card: dict[str, Any]) -> str:
    """Terminal rendering of a card (used by the CLI chat)."""
    lines: list[str] = []

    def walk(items: list[dict[str, Any]]) -> None:
        for it in items:
            if it.get("type") == "TextBlock":
                lines.append(it["text"])
            elif it.get("type") == "Container":
                walk(it.get("items", []))

    walk(card.get("body", []))
    actions = " | ".join(f"/{a['data']['factory_action']}" for a in card.get("actions", []))
    return "\n".join(lines) + f"\n\n[{actions}]"
