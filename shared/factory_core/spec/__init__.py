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
from ..tad_runner.layout import EXAMPLES, INTENT, INTENT_MARKDOWN, PROPERTIES, SCENARIOS, SpecLayout

DRAFT_MARKER = "<!-- factory:draft -->"
_FENCE = re.compile(r"^```[a-zA-Z0-9_-]*\s+file=(?P<path>[^\s`]+)\s*\n(?P<body>.*?)^```\s*$", re.M | re.S)
_SAFE_PATH = re.compile(
    r"^(intent\.yaml|intent\.md|properties\.yaml|examples/[\w.-]+\.csv|scenarios/[\w.-]+\.ya?ml)$"
)


class DraftError(ValueError):
    pass


def intent_yaml(intent: IntentSpec) -> str:
    return yaml.safe_dump(intent.model_dump(exclude_none=True), sort_keys=False, allow_unicode=True, width=110)


def intent_markdown(intent: IntentSpec) -> str:
    interface = intent.interface
    operations = []
    for op in interface.ops:
        detail = f"- `{op.name}`: {op.description or 'No description'}"
        if op.input:
            detail += "\n  - Input: " + ", ".join(f"`{key}` ({value})" for key, value in op.input.items())
        if op.output:
            detail += "\n  - Output: " + ", ".join(f"`{key}` ({value})" for key, value in op.output.items())
        operations.append(detail)
    decisions = [f"- **{item.question}** {item.answer}" for item in intent.decisions]
    interface_details = [f"- Kind: `{interface.kind}`"]
    if interface.entrypoint_hint:
        interface_details.append(f"- Entry point: {interface.entrypoint_hint}")
    if interface.command:
        interface_details.append(f"- Command: `{ ' '.join(interface.command) }`")
    if interface.base_url:
        interface_details.append(f"- Base URL: `{interface.base_url}`")
    return (
        f"# {intent.title}\n\n{intent.summary}\n\n"
        "## Overview\n\n"
        f"- Feature ID: `{intent.feature_id}`\n"
        f"- Repository: `{intent.repo.url}` (base `{intent.repo.base_branch}`)\n"
        f"- Stack: {intent.stack or 'Unknown'}\n"
        f"- Coding agent: {intent.engine}\n"
        f"- Approved by: {intent.approved_by or 'Not yet approved'}\n\n"
        "## Actors\n\n"
        f"{_markdown_items(intent.actors)}\n\n"
        "## Scope\n\n### Included\n\n"
        f"{_markdown_items(intent.scope_in)}\n\n"
        "### Excluded\n\n"
        f"{_markdown_items(intent.scope_out)}\n\n"
        "## Acceptance Criteria\n\n"
        f"{_markdown_items([f'**{ac.id}**: {ac.description}' for ac in intent.acceptance_criteria])}\n\n"
        "## Public Interface\n\n"
        f"{chr(10).join(interface_details)}\n\n"
        f"{_markdown_items(operations)}\n\n"
        "## Non-functional Requirements\n\n"
        f"{_markdown_items(intent.nfrs)}\n\n"
        "## Decisions\n\n"
        f"{_markdown_items(decisions)}\n\n"
        "<details>\n<summary>Complete machine-readable specification</summary>\n\n~~~yaml\n"
        f"{intent_yaml(intent)}~~~\n\n</details>\n"
    )


def _markdown_items(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items) if items else "- None specified"


def draft_to_files(draft: SpecDraft) -> dict[str, str]:
    files = {INTENT: intent_yaml(draft.intent), INTENT_MARKDOWN: intent_markdown(draft.intent)}
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
    lang = {".yaml": "yaml", ".yml": "yaml", ".csv": "csv", ".md": "markdown"}
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
