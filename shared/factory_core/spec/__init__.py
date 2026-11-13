"""Turning grill drafts into on-disk contracts, in both directions.

The LangGraph grill returns a structured ``SpecDraft``; the gh-aw grill posts a Markdown comment whose
fenced blocks carry ``file=<path>`` attributes. Both converge on the same ``{path: text}`` map.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from ..models import IntentSpec, SpecDraft
from ..tad_runner.gherkin import write_features
from ..tad_runner.layout import EXAMPLES, INTENT, PROPERTIES, SCENARIOS, SpecLayout

DRAFT_MARKER = "<!-- factory:draft -->"
_FENCE = re.compile(r"^```[a-zA-Z0-9_-]*\s+file=(?P<path>[^\s`]+)\s*\n(?P<body>.*?)^```\s*$", re.M | re.S)
_SAFE_PATH = re.compile(r"^(intent\.yaml|properties\.yaml|examples/[\w.-]+\.csv|scenarios/[\w.-]+\.ya?ml)$")


class DraftError(ValueError):
    pass


def intent_yaml(intent: IntentSpec) -> str:
    return yaml.safe_dump(intent.model_dump(exclude_none=True), sort_keys=False, allow_unicode=True, width=110)


def draft_to_files(draft: SpecDraft) -> dict[str, str]:
    files = {INTENT: intent_yaml(draft.intent)}
    files.update({f"{EXAMPLES}/{name}": text for name, text in draft.examples.items()})
    files.update({f"{SCENARIOS}/{name}": text for name, text in draft.scenarios.items()})
    if draft.properties:
        files[PROPERTIES] = draft.properties
    for path in files:
        if not _SAFE_PATH.match(path):
            raise DraftError(f"draft file path not allowed: {path!r}")
    return files


def files_to_draft(files: dict[str, str]) -> SpecDraft:
    for path in files:
        if not _SAFE_PATH.match(path):
            raise DraftError(f"draft file path not allowed: {path!r}")
    if INTENT not in files:
        raise DraftError("draft has no intent.yaml")
    try:
        intent = IntentSpec.model_validate(yaml.safe_load(files[INTENT]))
    except Exception as exc:
        raise DraftError(f"intent.yaml invalid: {exc}") from exc
    return SpecDraft(
        intent=intent,
        examples={p.split("/", 1)[1]: t for p, t in files.items() if p.startswith(EXAMPLES + "/")},
        scenarios={p.split("/", 1)[1]: t for p, t in files.items() if p.startswith(SCENARIOS + "/")},
        properties=files.get(PROPERTIES),
    )


def parse_markdown(markdown: str) -> dict[str, str]:
    files = {m.group("path"): m.group("body") for m in _FENCE.finditer(markdown)}
    if not files:
        raise DraftError("no ```<lang> file=<path> blocks found in the draft")
    return files


def render_markdown(files: dict[str, str]) -> str:
    lang = {".yaml": "yaml", ".yml": "yaml", ".csv": "csv"}
    parts = [DRAFT_MARKER]
    for path in sorted(files, key=lambda p: (p != INTENT, p)):
        body = files[path] if files[path].endswith("\n") else files[path] + "\n"
        parts.append(f"```{lang.get(Path(path).suffix, 'text')} file={path}\n{body}```")
    return "\n\n".join(parts) + "\n"


def write_draft(draft: SpecDraft, spec_dir: Path) -> SpecLayout:
    """Write data files and regenerate Gherkin. The driver is added afterwards by the driver-writer."""
    layout = SpecLayout(spec_dir)
    spec_dir.mkdir(parents=True, exist_ok=True)
    for rel, text in draft_to_files(draft).items():
        path = spec_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8", newline="\n")
    write_features(layout, draft.intent.title)
    return layout
