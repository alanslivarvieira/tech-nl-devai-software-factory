"""Locations of packaged, non-code resources (prompts, stack profiles, schemas, driver templates)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROMPTS_DIR = ROOT / "prompts"
STACK_PROFILES_DIR = ROOT / "stack_profiles"
DRIVER_TEMPLATES_DIR = STACK_PROFILES_DIR / "drivers"
SCHEMAS_DIR = ROOT / "schemas"

_VAR = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def render(template: str, **values: object) -> str:
    """Render ``{{name}}`` placeholders. Unknown placeholders raise, so prompts never ship half-filled."""

    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise KeyError(f"missing prompt variable: {key}")
        return str(values[key])

    return _VAR.sub(repl, template)


def render_prompt(name: str, **values: object) -> str:
    return render(load_prompt(name), **values)
