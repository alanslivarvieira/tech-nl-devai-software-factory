"""Anti-cheat rules applied to every change an agent makes.

Violations block the change; warnings are surfaced to the test-auditor and the PR reviewer.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from ..stack import StackProfile, glob_match
from ..tad_runner.layout import SPECS_ROOT
from .changes import FileChange

ALWAYS_PROTECTED = [
    f"{SPECS_ROOT}/**",
    ".github/workflows/**",
    "CODEOWNERS",
    ".github/CODEOWNERS",
    ".factory/config/**",
]


@dataclass
class Finding:
    rule: str
    path: str
    detail: str
    line: int | None = None


@dataclass
class GuardReport:
    violations: list[Finding] = field(default_factory=list)
    warnings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "violations": [asdict(v) for v in self.violations],
            "warnings": [asdict(w) for w in self.warnings],
        }

    def summary(self) -> str:
        if self.ok:
            return "guard: no violations" + (f" ({len(self.warnings)} warnings)" if self.warnings else "")
        lines = [f"guard: {len(self.violations)} violation(s)"]
        lines += [f"  ✗ [{v.rule}] {v.path}{f':{v.line}' if v.line else ''} — {v.detail}" for v in self.violations]
        return "\n".join(lines)


def check_changes(
    changes: list[FileChange],
    profile: StackProfile | None,
    allow_paths: list[str] | None = None,
) -> GuardReport:
    """``allow_paths`` re-opens protected globs for phases that legitimately write them
    (e.g. the freeze step writing ``.factory/specs/<id>/**``)."""
    rep = GuardReport()
    allow = allow_paths or []
    protected = ALWAYS_PROTECTED + (profile.guarded_files if profile else [])
    skip_res = [re.compile(p) for p in (profile.skip_markers if profile else [])]
    cov_res = [re.compile(p) for p in (profile.coverage_exclusions if profile else [])]
    line_res = [re.compile(p) for p in (profile.guarded_line_patterns if profile else [])]
    assert_res = [re.compile(p) for p in (profile.assertion_patterns if profile else [])]

    for ch in changes:
        paths = [ch.path] + ([ch.old_path] if ch.old_path else [])
        for path in paths:
            if any(glob_match(path, a) for a in allow):
                continue
            hit = next((g for g in protected if glob_match(path, g)), None)
            if hit:
                rep.violations.append(Finding("protected-path", path, f"matches protected pattern {hit!r}"))
                break
        is_test = bool(profile and profile.is_test(ch.path))
        was_test = bool(profile and ch.old_path and profile.is_test(ch.old_path))
        if ch.status == "D" and (is_test or was_test):
            rep.violations.append(Finding("deleted-test", ch.path, "existing tests must not be deleted"))
        if ch.status == "R" and was_test and not is_test:
            rep.violations.append(Finding("deleted-test", ch.old_path or ch.path, "test moved out of the test tree"))
        for ln, text in ch.added:
            if is_test and any(r.search(text) for r in skip_res):
                rep.violations.append(Finding("skip-marker", ch.path, f"adds a skip/xfail marker: {text.strip()}", ln))
            if any(r.search(text) for r in cov_res):
                rep.violations.append(
                    Finding("coverage-exclusion", ch.path, f"adds a coverage/mutation exclusion: {text.strip()}", ln)
                )
        if not any(glob_match(ch.path, a) for a in allow):
            for ln, text in ch.added + ch.removed:
                if any(r.search(text) for r in line_res):
                    rep.violations.append(
                        Finding(
                            "threshold-tamper", ch.path, f"touches a guarded test/coverage setting: {text.strip()}", ln
                        )
                    )
                    break
        if is_test and ch.status in ("M", "R") and assert_res:
            removed_asserts = sum(1 for _, t in ch.removed if any(r.search(t) for r in assert_res))
            added_asserts = sum(1 for _, t in ch.added if any(r.search(t) for r in assert_res))
            if removed_asserts > added_asserts:
                rep.warnings.append(
                    Finding(
                        "weakened-test", ch.path, f"removes {removed_asserts} assertion line(s), adds {added_asserts}"
                    )
                )
    return rep
