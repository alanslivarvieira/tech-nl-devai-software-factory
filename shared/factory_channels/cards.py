"""Adaptive Cards (v1.5) for Teams. Buttons submit ``{"factory_action": ...}`` back to the bot.

Teams renders only a small Markdown subset inside cards (bold, italic, lists, links — no tables, headings or
code blocks, and single line breaks collapse), so structured content is built from native elements: ``Table``,
``FactSet`` and collapsible sections. The contract leads with what a reviewer needs (acceptance criteria and
plain-English scenarios); exact test data and technical details are one click away.
"""

from __future__ import annotations

import csv
import io
import itertools
import json
import re
from collections.abc import Iterator
from typing import Any

import yaml

from factory_core.models import IntentSpec
from factory_core.tad_runner.loader import parse_story

FENCE = re.compile(r"```(\w+) file=(\S+)\n(.*?)```", re.S)
GATE_ROW = re.compile(
    r"^\|\s*(?P<name>[^|]+?)\s*\|\s*\S+\s+(?P<status>pass|fail|error|skip)\s*\|\s*(?P<req>yes|no)\s*\|"
    r"\s*(?P<summary>.*?)\s*\|\s*$",
    re.M,
)
ITERATION = re.compile(r"iteration (?P<n>\d+) @ `?(?P<sha>[0-9a-f]{7,})")
ICON = {"pass": "✅", "fail": "❌", "error": "⛔", "skip": "⏭️"}
GATE_NAMES = {
    "integrity": "contract integrity",
    "unit": "unit tests",
    "contract": "contract examples",
    "acceptance": "acceptance scenarios",
    "property": "property tests",
    "mutation": "mutation testing",
    "diff_coverage": "coverage of changed lines",
    "typecheck": "type check",
    "playwright": "Playwright e2e",
}
EXPECT_VERBS = {
    "equals": "is",
    "starts_with": "starts with",
    "ends_with": "ends with",
    "contains": "contains",
    "matches": "matches the pattern",
    "gte": "is at least",
    "lte": "is at most",
    "type": "is of type",
    "length_lte": "has at most this many characters:",
}
NBSP = "\u00a0"
MAX_SCENARIOS = 15
MAX_ROWS = 15
MAX_CARD_BYTES = 26_000
SCHEMA = "http://adaptivecards.io/schemas/adaptive-card.json"


# ------------------------------------------------------------------------------------------- elements
def _safe(text: str) -> str:
    """Teams drops anything that looks like an HTML tag (``{removed: <count>}`` showed as ``{removed: }``); a
    zero-width space after ``<`` keeps the text visible and unchanged to the eye."""
    return re.sub(r"<(?=[A-Za-z/!?])", "<\u200b", text)


def _text(text: str, **kw: Any) -> dict[str, Any]:
    return {"type": "TextBlock", "text": _safe(text), "wrap": True, **kw}


def _heading(text: str) -> dict[str, Any]:
    return _text(text, weight="Bolder", size="Medium", color="Accent", spacing="ExtraLarge", separator=True)


def _bullets(items: list[str], **kw: Any) -> dict[str, Any]:
    return _text("\n".join(f"- {' '.join(i.split())}" for i in items), spacing="Small", **kw)


def _code(text: str, max_lines: int = 60) -> dict[str, Any]:
    """Monospace block: one paragraph per line (single newlines collapse) and a leading no-break space so no
    line is read as Markdown (YAML ``- item`` would otherwise become a bullet)."""
    lines = text.rstrip().splitlines() or [""]
    if len(lines) > max_lines:
        lines = [*lines[:max_lines], "…"]
    shown = [NBSP + NBSP * (len(ln) - len(ln.lstrip(" "))) + ln.lstrip(" ") for ln in lines]
    return _text("\n\n".join(shown), fontType="Monospace", size="Small", spacing="Small")


def _collapsible(
    title: str, items: list[dict[str, Any]], ids: Iterator[int], style: str | None = None
) -> list[dict[str, Any]]:
    cid = f"section-{next(ids)}"
    return [
        {
            "type": "Container",
            "spacing": "Small",
            "selectAction": {"type": "Action.ToggleVisibility", "targetElements": [cid]},
            "items": [_text(f"▸ {title}", color="Accent", size="Small", weight="Bolder")],
        },
        {"type": "Container", "id": cid, "isVisible": False, "style": style, "items": items},
    ]


def _table(rows: list[list[str]]) -> dict[str, Any]:
    return {
        "type": "Table",
        "spacing": "Small",
        "firstRowAsHeader": True,
        "showGridLines": True,
        "gridStyle": "accent",
        "columns": [{"width": 1} for _ in rows[0]],
        "rows": [
            {
                "type": "TableRow",
                "cells": [
                    {"type": "TableCell", "items": [_text(c or " ", size="Small", weight="Bolder" if i == 0 else None)]}
                    for c in row
                ],
            }
            for i, row in enumerate(rows)
        ],
    }


def _card(body: list[dict[str, Any]], actions: list[dict[str, Any]]) -> dict[str, Any]:
    budget = MAX_CARD_BYTES - _size(actions) - 200  # 200: the card envelope ($schema, version, keys)
    clean = _fit(_strip_none(body), budget)
    return {"type": "AdaptiveCard", "$schema": SCHEMA, "version": "1.5", "body": clean, "actions": actions}


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode())


def _fit(body: list[dict[str, Any]], budget: int) -> list[dict[str, Any]]:
    """Teams rejects cards over ~28 KB: drop scenarios from the end (noting how many) until the card fits."""
    dropped = 0
    while _size(body) > budget - 300:  # 300: room for the note below
        last = max((i for i, el in enumerate(body) if str(el.get("id", "")).startswith("scenario-")), default=None)
        if last is None:
            break
        del body[last]
        dropped += 1
    if dropped:
        note = _text(f"…{dropped} more scenario(s) are not shown here (Teams card size limit); they are in the "
                     "contract files.", isSubtle=True, size="Small")
        after = max((i for i, el in enumerate(body) if str(el.get("id", "")).startswith("scenario-")), default=None)
        body.insert(after + 1 if after is not None else len(body), note)
    return body


def _strip_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_strip_none(v) for v in value]
    return value


# ------------------------------------------------------------------------------------------- wording
def _human(name: str) -> str:
    return name.replace("_", " ").strip()


LEAD_WORDS = {"the", "a", "an", "they", "he", "she", "it", "we", "you", "someone", "nobody", "every", "all", "no",
              "there", "this", "that", "each", "both", "only", "one", "two", "three", "exactly"}


def _sentence_case_after(keyword: str, sentence: str) -> str:
    """``**When** the user …``: lower-case a leading common word ("The", "A", "They"), never a name."""
    first = sentence.split(" ", 1)[0]
    if first.lower() in LEAD_WORDS and first[:1].isupper():
        sentence = sentence[0].lower() + sentence[1:]
    return f"**{keyword}** {sentence}"


def _value(v: Any) -> str:
    if v == [] or v == {}:
        return "none"
    if isinstance(v, list):
        return ", ".join(_value(x) for x in v)
    if isinstance(v, dict):
        return "; ".join(f"{_human(k)}: {_value(x)}" for k, x in v.items())
    if v is None:
        return "null"
    if v == "":
        return "(empty text)"
    return str(v).lower() if isinstance(v, bool) else str(v)


def _call(step: dict[str, Any]) -> str:
    inp = step.get("input") or {}
    return _human(str(step.get("op", "?"))) + (f" ({_value(inp)})" if inp else "")


def _expectation(d: dict[str, Any]) -> str:
    if "error" in d:
        return f"fails with “{d['error']}”"
    if "error_contains" in d:
        return f"fails with an error containing “{d['error_contains']}”"
    if "output" in d:
        return f"returns {_value(d['output'])}"
    if "output_subset" in d:
        return f"returns at least {_value(d['output_subset'])}"
    return "succeeds"


# ------------------------------------------------------------------------------------------- intent
def _contract_files(contract_markdown: str) -> dict[str, str]:
    return {path: body for _lang, path, body in FENCE.findall(contract_markdown)}


def _intent(files: dict[str, str]) -> IntentSpec | None:
    try:
        return IntentSpec.model_validate(yaml.safe_load(files["intent.yaml"]))
    except Exception:
        return None


def _intent_section(intent: IntentSpec, ids: Iterator[int]) -> list[dict[str, Any]]:
    body = [
        _text(intent.title, weight="Bolder", size="Large", spacing="Medium"),
        _text(intent.summary, spacing="Small"),
        _text("**What it must do**", spacing="Medium"),
        _bullets([f"**{ac.id}** — {ac.description}" for ac in intent.acceptance_criteria]),
    ]
    scope: list[dict[str, Any]] = []
    if intent.scope_in:
        scope += [_text("**In scope**"), _bullets(intent.scope_in)]
    if intent.scope_out:
        scope += [_text("**Not in scope**"), _bullets(intent.scope_out)]
    if intent.decisions:
        scope += [_text("**Your answers**"), _bullets([f"{d.question} → {d.answer}" for d in intent.decisions])]
    if scope:
        body += _collapsible("Scope and your answers", scope, ids)
    ops = [
        f"**{_human(op.name)}**"
        + (f" ({', '.join(op.input)})" if op.input else "")
        + (f" → {', '.join(op.output)}" if op.output else "")
        + (f" — {op.description}" if op.description else "")
        for op in intent.interface.ops
    ]
    facts = [
        {"title": "Repository", "value": f"{intent.repo.url} ({intent.repo.base_branch})"},
        {"title": "Stack", "value": intent.stack or "unknown"},
        {"title": "Coding agent", "value": intent.engine},
        {"title": "Test interface", "value": intent.interface.kind},
    ]
    tech: list[dict[str, Any]] = [
        {"type": "FactSet", "facts": facts},
        _text("**Operations the tests use**"),
        _bullets(ops),
    ]
    if intent.interface.entrypoint_hint:
        tech += [_text("**Where the code goes**"), _text(intent.interface.entrypoint_hint, spacing="Small")]
    if intent.nfrs:
        tech += [_text("**Constraints**"), _bullets(intent.nfrs)]
    body += _collapsible("Technical details", tech, ids)
    return body


def _intent_markdown_section(intent_markdown: str) -> list[dict[str, Any]]:
    """Fallback when the draft carries no parsable intent.yaml (e.g. hand-written drafts)."""
    text = intent_markdown.split("<details>", 1)[0]
    text = re.sub(r"^#{1,6} (.+)$", r"**\1**", text, flags=re.M)
    return [_text(text.strip()[:6000])] if text.strip() else []


# ------------------------------------------------------------------------------------------- tests as data
def _scenario_cards(files: dict[str, str], ids: Iterator[int]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    shown = 0
    for path, text in sorted(files.items()):
        if not path.startswith("scenarios/"):
            continue
        try:
            cases = (yaml.safe_load(text) or {}).get("cases") or []
        except Exception:
            out += [_text(path, isSubtle=True, size="Small"), _code(text)]
            continue
        for case in cases:
            shown += 1
            if shown > MAX_SCENARIOS:
                continue
            out.append({"type": "Container", "id": f"scenario-{shown}", "style": "emphasis", "spacing": "Medium",
                        "items": _scenario(case, ids)})
    if shown > MAX_SCENARIOS:
        out.append(_text(f"…and {shown - MAX_SCENARIOS} more scenarios in the contract files.", isSubtle=True))
    return out


def _scenario(case: Any, ids: Iterator[int]) -> list[dict[str, Any]]:
    """One scenario; a malformed one degrades on its own instead of turning the whole file into raw YAML."""
    if not isinstance(case, dict):
        return [_code(yaml.safe_dump(case, allow_unicode=True))]
    name, ac = case.get("name", "?"), case.get("ac_id")
    items = [_text(f"**Scenario: {name}**" + (f" ({ac})" if ac else ""), spacing="None")]
    try:
        steps = _steps(case)
    except Exception:
        data = {k: v for k, v in case.items() if k not in ("name", "ac_id", "story")}
        steps = [_code(yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100))]
    story = None
    if case.get("story"):
        try:
            story = parse_story(case["story"], "story")
        except Exception:
            items.append(_text("⚠️ The plain-English version could not be read; these are the exact checks.",
                               color="Warning", size="Small", spacing="Small"))
    if story:
        return items + _story(story) + _collapsible("Show exactly what is tested", steps, ids, style="default")
    return items + steps


def _story(story: dict[str, list[str]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    given, when, then = story["given"], story["when"], story["then"]
    if len(given) == 1:
        items.append(_text(_sentence_case_after("Given", given[0]), spacing="Small"))
    elif given:
        items += [_text("**Given**", spacing="Small"), _bullets(given)]
    items.append(_text(_sentence_case_after("When", " ".join(when)), spacing="Small"))
    if len(then) == 1:
        items.append(_text(_sentence_case_after("Then", then[0]), spacing="Small"))
    else:
        items += [_text("**Then**", spacing="Small"), _bullets(then)]
    return items


def _steps(case: dict[str, Any]) -> list[dict[str, Any]]:
    """No story (older contracts): spell the data out step by step."""
    items: list[dict[str, Any]] = []
    given = [_call(g) for g in case.get("given") or []]
    if given:
        items += [_text("**Given**", spacing="Small"), _bullets(given)]
    whens = case.get("when") if isinstance(case.get("when"), list) else [case.get("when") or {}]
    items.append(_text("**When** " + " and then ".join(_call(w) for w in whens), spacing="Small"))
    then = case.get("then") or {}
    checks = []
    main = {k: v for k, v in then.items() if k != "state"}
    if main:
        checks.append("it " + _expectation(main))
    checks += [f"{_call(q)} {_expectation(q)}" for q in then.get("state") or []]
    items += [_text("**Then**", spacing="Small"), _bullets(checks)]
    return items


def _example_tables(files: dict[str, str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path, text in sorted(files.items()):
        if not path.startswith("examples/"):
            continue
        rows = [r for r in csv.reader(io.StringIO(text), skipinitialspace=True) if r]
        out.append(_text(f"**{_human(path.rsplit('/', 1)[-1].removesuffix('.csv'))}** · {path}", spacing="Medium"))
        if not rows:
            continue
        head, body = rows[0], rows[1:]
        keep = [i for i, h in enumerate(head) if h in ("case", "ac_id") or any(i < len(r) and r[i] for r in body)]
        table = [[_column(head[i]) for i in keep]]
        table += [[_cell(r[i] if i < len(r) else "") for i in keep] for r in body[:MAX_ROWS]]
        out.append(_table(table))
        if len(body) > MAX_ROWS:
            out.append(_text(f"…and {len(body) - MAX_ROWS} more rows.", isSubtle=True, size="Small"))
    return out


def _column(header: str) -> str:
    name = header.split(":", 1)[0]
    if name.startswith("in."):
        return _human(name[3:])
    if name.startswith("out."):
        return "→ " + _human(name[4:])
    if name == "out":
        return "→ result"
    fixed = {"case": "Case", "ac_id": "AC", "op": "Operation", "error": "→ error", "baseline": "Works today?"}
    return fixed.get(name, "→ error contains" if name == "error_contains" else _human(name))


def _cell(value: str) -> str:
    fixed = {"<empty>": "(empty text)", "<null>": "null", "[]": "none", "{}": "none", "existing": "yes"}
    return fixed.get(value, value)


def _property_cards(files: dict[str, str]) -> list[dict[str, Any]]:
    text = files.get("properties.yaml")
    if not text:
        return []
    try:
        props = (yaml.safe_load(text) or {}).get("properties") or []
    except Exception:
        return [_code(text)]
    out: list[dict[str, Any]] = []
    for p in props:
        fields = [f"{_human(k)} ({_value(v) if not isinstance(v, dict) else _input_kind(v)})"
                  for k, v in (p.get("input") or {}).items()]
        op = _human(str(p.get("op", "")))
        when = f"For any {', '.join(fields)} passed to {op}" if fields else f"Whenever {op} runs"
        checks = [_property_check(e) for e in p.get("expect") or []]
        if p.get("allow_errors"):
            checks.append("or it fails with one of: " + ", ".join(f"“{e}”" for e in p["allow_errors"]))
        items = [
            _text(f"**{p.get('name', 'property')}**" + (f" ({p['ac_id']})" if p.get("ac_id") else ""), spacing="None"),
            _text(f"{when}:", spacing="Small"),
            _bullets(checks),
        ]
        out.append({"type": "Container", "style": "emphasis", "spacing": "Medium", "items": items})
    return out


def _input_kind(spec: dict[str, Any]) -> str:
    kind = str(spec.get("type", "value"))
    extra = [f"{_human(k)} {v}" for k, v in spec.items() if k != "type"]
    return kind + (f", {', '.join(extra)}" if extra else "")


def _property_check(e: dict[str, Any]) -> str:
    path = _human(str(e.get("path", "result")))
    for key, verb in EXPECT_VERBS.items():
        if key in e:
            return f"{path} {verb} {_value(e[key])}"
    if "contains_input" in e:
        return f"{path} contains the given {_human(str(e['contains_input']))}"
    return f"{path} {_value({k: v for k, v in e.items() if k != 'path'})}"


# ------------------------------------------------------------------------------------------- cards
def approval_card(
    contract_markdown: str, summary: str, problems: list[str], intent_preview: str = ""
) -> dict[str, Any]:
    ids = itertools.count(1)
    files = _contract_files(contract_markdown)
    intent = _intent(files)
    body: list[dict[str, Any]] = [
        _text("📜 Review the contract", weight="Bolder", size="Large"),
        _text(
            "Approve it and it is frozen: the coding agent must make these checks pass and cannot change them.",
            isSubtle=True,
            spacing="None",
        ),
    ]
    if summary:
        body.append(_text(summary, spacing="Medium"))
    if intent:
        body += _intent_section(intent, ids)
    elif intent_preview:
        body += _intent_markdown_section(intent_preview)
    if problems:
        body.append(
            {
                "type": "Container",
                "style": "warning",
                "spacing": "Medium",
                "items": [_text("⚠️ **Check these before approving**"), _bullets(problems)],
            }
        )
    sections = [
        ("Scenarios", _scenario_cards(files, ids)),
        ("Examples", _example_tables(files)),
        ("Rules that must always hold", _property_cards(files)),
    ]
    for title, items in sections:
        if items:
            body += [_heading(title), *items]
    if not files:
        body += [_heading("Contract"), _code(contract_markdown)]
    body += [
        {
            "type": "Input.ChoiceSet",
            "id": "engine",
            "label": "Coding agent",
            "placeholder": f"Keep {intent.engine}" if intent else "Keep the current agent",
            "spacing": "Large",
            "separator": True,
            "choices": [
                {"title": "GitHub Copilot CLI", "value": "copilot"},
                {"title": "Claude Code", "value": "claude"},
                {"title": "Codex", "value": "codex"},
            ],
        },
        {
            "type": "Input.Text",
            "id": "changes",
            "isMultiline": True,
            "placeholder": "What should change? (then press ✏️ Request changes)",
        },
    ]
    return _card(
        body,
        [
            {
                "type": "Action.Submit",
                "title": "✅ Approve & build",
                "style": "positive",
                "data": {"factory_action": "approve"},
            },
            {"type": "Action.Submit", "title": "✏️ Request changes", "data": {"factory_action": "revise"}},
            {"type": "Action.Submit", "title": "🛑 Abort", "style": "destructive", "data": {"factory_action": "abort"}},
        ],
    )


def _gates(data: dict[str, Any], report_markdown: str) -> list[dict[str, Any]]:
    if data.get("gates"):
        return list(data["gates"])
    return [
        {"name": m["name"], "status": m["status"], "required": m["req"] == "yes", "summary": m["summary"]}
        for m in GATE_ROW.finditer(report_markdown)
        if m["name"] != "Gate"
    ]


def escalation_card(reason: str, report_markdown: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    data = data or {}
    ids = itertools.count(1)
    gates = _gates(data, report_markdown)
    body: list[dict[str, Any]] = [
        _text("🚨 The factory needs your call", weight="Bolder", size="Large"),
        _text(reason[:1].upper() + reason[1:], spacing="Small"),
    ]
    m = ITERATION.search(report_markdown)
    iteration, commit = data.get("iteration") or (m and m["n"]), data.get("commit") or (m and m["sha"])
    if iteration:
        body.append(_text(f"Attempt {iteration}" + (f" · commit {str(commit)[:7]}" if commit else ""), isSubtle=True,
                          spacing="None"))
    if gates:
        failing = [g for g in gates if g["status"] in ("fail", "error") and g.get("required", True)]
        passing = [g for g in gates if g["status"] == "pass"]
        skipped = [g for g in gates if g["status"] == "skip" or (g["status"] in ("fail", "error")
                                                                  and not g.get("required", True))]
        if failing:
            lines = []
            for g in failing:
                line = f"{ICON[g['status']]} **{_gate(g['name'])}**: {g.get('summary') or g['status']}"
                if g.get("failures"):
                    # runner ids look like "scenario:<suite>:<case name>"; the case name is what a human knows
                    line += " — " + ", ".join(str(f).split(":", 2)[-1] for f in g["failures"][:5])
                lines.append(line)
            body += [_text("**What is failing**", spacing="Medium"), _bullets(lines)]
        if data.get("audit"):
            body += [_text("**Test auditor**", spacing="Medium"), _bullets([str(a) for a in data["audit"]])]
        if passing:
            body.append(_text("✅ **Passing:** " + ", ".join(_gate(g["name"]) for g in passing), spacing="Medium",
                              size="Small"))
        if skipped:
            body.append(_text("⏭️ **Not checked:** " + "; ".join(
                f"{_gate(g['name'])} ({g.get('summary') or g['status']})" for g in skipped), spacing="Small",
                size="Small"))
        facts = [
            {"title": _gate(g["name"]), "value": _safe(f"{ICON.get(g['status'], '')} {g.get('summary') or g['status']}"
             + ("" if g.get("required", True) else " (optional)"))}
            for g in gates
        ]
        body += _collapsible("All checks", [{"type": "FactSet", "facts": facts}], ids)
    elif report_markdown:
        body.append(_code(report_markdown, max_lines=40))
    body += [
        _text("**What now?**", spacing="Medium"),
        _bullets([
            "🔁 **Keep looping** — let a fresh builder try again",
            "✏️ **Amend contract** — the tests are wrong or too strict; describe the change below",
            "🛑 **Abort** — stop this feature",
        ]),
        {
            "type": "Input.Text",
            "id": "changes",
            "isMultiline": True,
            "placeholder": "For ✏️ Amend: what should change in the contract?",
        },
    ]
    return _card(
        body,
        [
            {"type": "Action.Submit", "title": "🔁 Keep looping", "data": {"factory_action": "continue"}},
            {"type": "Action.Submit", "title": "✏️ Amend contract", "data": {"factory_action": "amend"}},
            {"type": "Action.Submit", "title": "🛑 Abort", "style": "destructive", "data": {"factory_action": "abort"}},
        ],
    )


def _gate(name: str) -> str:
    return GATE_NAMES.get(name, _human(name))


# ------------------------------------------------------------------------------------------- terminal
def card_to_text(card: dict[str, Any]) -> str:
    """Terminal rendering of a card (used by the CLI chat); collapsed sections are left out."""
    lines: list[str] = []

    def walk(items: list[dict[str, Any]]) -> None:
        for it in items:
            kind = it.get("type")
            if it.get("isVisible") is False:
                continue
            if kind == "TextBlock":
                lines.append(it["text"].replace(NBSP, " ").replace("\u200b", ""))
            elif kind == "Container":
                walk(it.get("items", []))
            elif kind == "FactSet":
                lines.extend(f"{f['title']}: {f['value']}" for f in it.get("facts", []))
            elif kind == "Table":
                for row in it.get("rows", []):
                    lines.append(" | ".join(c["items"][0]["text"] for c in row["cells"]))

    walk(card.get("body", []))
    actions = " | ".join(f"/{a['data']['factory_action']}" for a in card.get("actions", []))
    return "\n".join(lines) + f"\n\n[{actions}]"
