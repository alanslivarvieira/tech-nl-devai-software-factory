"""``factory-spec``: draft <-> files conversions and JSON Schema export.

factory-spec from-markdown --input comment.md --spec-dir DIR     (gh-aw freeze)
factory-spec to-markdown   --spec-dir DIR                        (show a contract in a comment)
factory-spec export-schemas [--out DIR]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..models import GrillTurn, IntentSpec, SpecDraft, TaskGraph, VerificationReport
from ..resources import SCHEMAS_DIR
from . import files_to_draft, parse_markdown, render_markdown, write_draft


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="factory-spec")
    sub = ap.add_subparsers(dest="cmd", required=True)
    fm = sub.add_parser("from-markdown")
    fm.add_argument("--input", type=Path, required=True)
    fm.add_argument("--spec-dir", type=Path, required=True)
    tm = sub.add_parser("to-markdown")
    tm.add_argument("--spec-dir", type=Path, required=True)
    ex = sub.add_parser("export-schemas")
    ex.add_argument("--out", type=Path, default=SCHEMAS_DIR)
    a = ap.parse_args(argv)

    if a.cmd == "from-markdown":
        draft = files_to_draft(parse_markdown(a.input.read_text(encoding="utf-8")))
        write_draft(draft, a.spec_dir)
        print(draft.intent.feature_id)
        return 0
    if a.cmd == "to-markdown":
        files = {
            p.relative_to(a.spec_dir).as_posix(): p.read_text(encoding="utf-8")
            for p in sorted(a.spec_dir.rglob("*"))
            if p.is_file()
            and p.relative_to(a.spec_dir).parts[0]
            in ("intent.md", "intent.yaml", "examples", "scenarios", "properties.yaml")
        }
        print(render_markdown(files))
        return 0
    a.out.mkdir(parents=True, exist_ok=True)
    for name, model in {
        "intent-spec": IntentSpec,
        "spec-draft": SpecDraft,
        "grill-turn": GrillTurn,
        "task-graph": TaskGraph,
        "verification-report": VerificationReport,
    }.items():
        (a.out / f"{name}.schema.json").write_text(
            json.dumps(model.model_json_schema(), indent=2) + "\n", encoding="utf-8"
        )
        print(f"wrote {a.out / f'{name}.schema.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
