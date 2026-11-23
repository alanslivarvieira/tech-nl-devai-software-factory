"""Token usage report: one row per agent session (fresh CLI invocation), with totals per role and per model.

Numbers are what the coding-agent CLI reports; nothing is estimated except by the fake agent. Cost columns are
only filled when the CLI provides them (Claude Code: USD estimate; Copilot: premium requests / AI credits).
"""

from __future__ import annotations

import datetime as dt
import json
import threading
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..agents.base import AgentSession

TOKENS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")
COSTS = ("cost_usd", "premium_requests", "ai_credits")
CLI_DEFAULT = "(cli default)"


def _sum_optional(values: Iterable[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return round(sum(present), 6) if present else None


def _model_label(reported: str | None, requested: str | None) -> str:
    if reported:
        return reported
    return f"{requested} (configured)" if requested else CLI_DEFAULT


class UsageLog:
    """Thread-safe, per-feature collection of agent sessions (parallel build workers record concurrently)."""

    def __init__(self) -> None:
        self._rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._lock = threading.Lock()

    def record(self, feature_key: str, sessions: Iterable[AgentSession], task: str | None = None) -> None:
        with self._lock:
            rows = self._rows[feature_key]
            for s in sessions:
                usage = [asdict(u) for u in s.usage]
                reported = [u["model"] for u in usage if u["model"]]
                rows.append(
                    {
                        "seq": len(rows) + 1,
                        "at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
                        "role": s.role,
                        "task": task,
                        "engine": s.engine,
                        "model_requested": s.model,
                        "effort": s.effort,
                        "model": ", ".join(reported) if reported else _model_label(None, s.model),
                        **{k: sum(int(u[k]) for u in usage) for k in TOKENS},
                        **{k: _sum_optional(u[k] for u in usage) for k in COSTS},
                        "duration_s": round(s.duration_s, 2),
                        "ok": s.ok,
                        "fallback": s.fallback,
                        "usage_reported": bool(usage),
                        "error": s.error,
                        "usage": usage,
                    }
                )

    def rows(self, feature_key: str) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._rows.get(feature_key, [])]


def _totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "sessions": len(rows),
        **{k: sum(r[k] for r in rows) for k in TOKENS},
        **{k: _sum_optional(r[k] for r in rows) for k in COSTS},
        "duration_s": round(sum(r.get("duration_s", 0.0) for r in rows), 2),
    }


def build_report(rows: list[dict[str, Any]], meta: dict[str, Any] | None = None) -> dict[str, Any]:
    by_role: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_role[r["role"]].append(r)
    # per model: split each session by the models it reports (a Claude session may use several)
    model_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        entries = r["usage"] or [{k: 0 for k in TOKENS} | {k: None for k in COSTS} | {"model": None}]
        for u in entries:
            model_rows[_model_label(u["model"], r["model_requested"])].append(
                {**{k: u[k] for k in (*TOKENS, *COSTS)}, "duration_s": 0.0}
            )
    by_model = {}
    for name, entries in model_rows.items():
        t = _totals(entries)
        t.pop("duration_s")
        t["sessions"] = len(entries)
        by_model[name] = t
    return {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "meta": meta or {},
        "totals": _totals(rows),
        "by_role": {role: _totals(rs) for role, rs in by_role.items()},
        "by_model": by_model,
        "sessions": rows,
    }


def _n(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "–"
    return f"{value:,}" if isinstance(value, int) else f"{value:,.{digits}f}"


def _table(header: list[str], lines: list[list[str]]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(cell.replace("|", "\\|") for cell in line) + " |" for line in lines]
    return "\n".join(out)


def _totals_cells(t: dict[str, Any]) -> list[str]:
    return [
        _n(t["sessions"]),
        _n(t["input_tokens"]),
        _n(t["output_tokens"]),
        _n(t["cache_read_tokens"]),
        _n(t["cache_write_tokens"]),
        _n(t["reasoning_tokens"]),
        _n(t["cost_usd"], 4),
        _n(t["premium_requests"]),
        _n(t["ai_credits"]),
    ]


TOTAL_HEADER = [
    "sessions",
    "input",
    "output",
    "cache read",
    "cache write",
    "reasoning",
    "USD",
    "premium req",
    "AI credits",
]


def render_markdown(report: dict[str, Any]) -> str:
    meta = report.get("meta") or {}
    parts = [f"# Factory usage report — {meta.get('feature_key', '')}".rstrip(" —"), ""]
    if meta:
        parts += [_table(["field", "value"], [[k, str(v)] for k, v in meta.items()]), ""]
    parts += [f"Generated {report['generated_at']}.", "", "## Totals", ""]
    parts += [
        _table(
            TOTAL_HEADER + ["duration s"], [[*_totals_cells(report["totals"]), _n(report["totals"]["duration_s"], 1)]]
        )
    ]
    parts += ["", "## By role", ""]
    parts += [
        _table(
            ["role", *TOTAL_HEADER, "duration s"],
            [[role, *_totals_cells(t), _n(t["duration_s"], 1)] for role, t in report["by_role"].items()],
        )
    ]
    parts += ["", "## By model", ""]
    parts += [_table(["model", *TOTAL_HEADER], [[m, *_totals_cells(t)] for m, t in report["by_model"].items()])]
    parts += ["", "## Sessions", ""]
    parts += [
        _table(
            [
                "#",
                "role",
                "task",
                "engine",
                "model",
                "effort",
                "input",
                "output",
                "cache read",
                "reasoning",
                "USD",
                "premium req",
                "s",
                "status",
            ],
            [
                [
                    str(r["seq"]),
                    r["role"],
                    r["task"] or "",
                    r["engine"],
                    r["model"],
                    r["effort"] or "",
                    _n(r["input_tokens"]),
                    _n(r["output_tokens"]),
                    _n(r["cache_read_tokens"]),
                    _n(r["reasoning_tokens"]),
                    _n(r["cost_usd"], 4),
                    _n(r["premium_requests"]),
                    _n(r["duration_s"], 1),
                    ("ok" if r["ok"] else "failed")
                    + (" · fallback to CLI default" if r["fallback"] else "")
                    + ("" if r["usage_reported"] else " · no usage reported"),
                ]
                for r in report["sessions"]
            ],
        )
    ]
    parts += [
        "",
        "Notes: tokens are as reported by each CLI (Copilot's *input* includes cache reads/writes). "
        "Codex does not report the model it ran, so its rows show the configured model. "
        "`–` = not reported by the CLI.",
        "",
    ]
    return "\n".join(parts)


def write_report(base: Path, report: dict[str, Any]) -> tuple[Path, Path]:
    """Write ``<base>.json`` and ``<base>.md`` (a ``.json``/``.md`` suffix on ``base`` is ignored)."""
    if base.suffix.lower() in (".json", ".md"):
        base = base.with_suffix("")
    base.parent.mkdir(parents=True, exist_ok=True)
    json_path, md_path = base.with_name(base.name + ".json"), base.with_name(base.name + ".md")
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8", newline="\n")
    return json_path, md_path
