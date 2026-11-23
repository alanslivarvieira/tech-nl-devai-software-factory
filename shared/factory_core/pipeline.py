"""Phase logic shared by both factories (LangGraph nodes and gh-aw workflow scripts call these)."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .agents.base import RUN_DIR
from .guard import lock
from .models import IntentSpec, ReviewResult, SpecDraft, Task, TaskGraph, VerificationReport
from .reports import failure_brief, report_markdown
from .resources import render_prompt
from .spec import intent_markdown, intent_yaml, write_draft
from .stack import StackProfile
from .tad_runner.layout import SPECS_ROOT, SpecLayout
from .tad_runner.runner import freeze_check, validate

BOT_AUTHOR = ["-c", "user.name=AI Software Factory", "-c", "user.email=factory@users.noreply.github.com"]
DRIVER_FILES = {"python": "driver.py", "node": "driver.mjs"}
DRIVER_COMMANDS = {
    "python": ["{python}", "{spec_dir}/driver/driver.py"],
    "node": ["node", "{spec_dir}/driver/driver.mjs"],
}

JSON_GRILL_CONTRACT = """## Output
Return `status: "questions"` with `questions` (this round's frontier, unnumbered: `title`, `body`, lettered
`options` when there are alternatives, and your `recommendation`), or `status: "draft"` with `draft` = {intent,
examples: {"<op>.csv": "<csv text>"}, scenarios: {"<name>.yaml": "<yaml text>"}, properties: "<yaml text>"
| null}. Put a one-paragraph summary for the human in `notes`."""

MARKDOWN_GRILL_CONTRACT = """## Output
Post exactly ONE comment. Either:
- this round's questions, formatted as the `grilling` skill says (`❓ **Q1** - **<title>**: <body>` with
  lettered options when there are alternatives, then `➡️ <your recommended answer>`, questions separated by
  `---`), ending with: "Reply `yes` to accept my recommendations, answer like `1A, 2B`, or in your own
  words. Say `/engine copilot|claude|codex` to pick the coding agent.", or
- a draft that starts with the line `<!-- factory:draft -->`, a short summary for the human, then one fenced
  block per file with a `file=` attribute, e.g. ```` ```yaml file=intent.yaml ```` and
  ```` ```csv file=examples/greet.csv ````, ending with: "Reply `/approve` to freeze this contract or
  comment what to change." """


def git(repo: Path, *args: str, check: bool = True) -> str:
    p = subprocess.run(
        ["git", *BOT_AUTHOR, *args], cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {p.stderr.strip()}")
    return p.stdout.strip()


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD")


def commit_all(repo: Path, message: str, *, require_factory_branch: bool = False) -> str:
    if require_factory_branch:
        branch = git(repo, "branch", "--show-current")
        if not branch.startswith(("factory/", "factory-task/")):
            raise RuntimeError(f"refusing factory commit on non-feature branch {branch!r}")
    git(repo, "add", "-A")
    if git(repo, "status", "--porcelain"):
        git(repo, "commit", "-q", "-m", message)
    return head(repo)


def spec_rel(feature_id: str) -> str:
    return f"{SPECS_ROOT}/{feature_id}"


def driver_command(stack: str | None) -> str:
    cmd = DRIVER_COMMANDS.get(stack or "")
    return json.dumps(cmd) if cmd else '["<interpreter>", "{spec_dir}/driver/<driver file>"]'


# --------------------------------------------------------------------------------------------- grill
def grill_prompt(
    *,
    request: str,
    repo_url: str,
    base_branch: str,
    stack: str | None,
    repo_map: str,
    transcript: list[dict[str, str]],
    critic_findings: list[str],
    default_engine: str,
    feature_id_hint: str,
    markdown: bool = False,
) -> str:
    convo = "\n".join(f"**{t['role']}**: {t['text']}" for t in transcript) or "(no answers yet — first turn)"
    return render_prompt(
        "grill",
        request=request,
        repo_url=repo_url,
        base_branch=base_branch,
        stack=stack or "unknown",
        repo_map=repo_map,
        transcript=convo,
        critic_findings="\n".join(f"- {c}" for c in critic_findings) or "(none)",
        default_engine=default_engine,
        feature_id_hint=feature_id_hint,
        driver_command=driver_command(stack),
        output_contract=MARKDOWN_GRILL_CONTRACT if markdown else JSON_GRILL_CONTRACT,
    )


def critique(draft: SpecDraft) -> list[str]:
    """Deterministic spec critic: write the draft to a scratch dir and run the contract validator."""
    with tempfile.TemporaryDirectory() as tmp:
        try:
            layout = write_draft(draft, Path(tmp) / "spec")
        except Exception as exc:
            return [f"draft cannot be written: {exc}"]
        return validate(layout)


# -------------------------------------------------------------------------------------------- freeze
@dataclass
class FreezeResult:
    ok: bool
    problems: list[str] = field(default_factory=list)
    root_hash: str = ""
    freeze_sha: str = ""


def write_contract(repo: Path, draft: SpecDraft, profile: StackProfile | None) -> SpecLayout:
    layout = write_draft(draft, repo / spec_rel(draft.intent.feature_id))
    layout.driver.mkdir(parents=True, exist_ok=True)
    template = profile.driver_template_text() if profile else None
    target = layout.driver / DRIVER_FILES.get(profile.name if profile else "", "README.md")
    if template and not target.exists():
        target.write_text(template, encoding="utf-8", newline="\n")
    return layout


def driver_writer_prompt(layout: SpecLayout, repo: Path, profile: StackProfile | None) -> str:
    intent_text = layout.intent.read_text(encoding="utf-8")
    name = DRIVER_FILES.get(profile.name if profile else "", "<choose a file name>")
    return render_prompt(
        "driver_writer",
        spec_dir=layout.root.relative_to(repo).as_posix(),
        driver_path=(layout.driver / name).relative_to(repo).as_posix(),
        intent_yaml=intent_text,
        driver_template=(profile.driver_template_text() if profile else None) or "(no template for this stack)",
    )


def freeze(repo: Path, layout: SpecLayout, approver: str) -> FreezeResult:
    """Validate, prove the tests are not vacuous, hash-lock and commit the contract."""
    problems = freeze_check(layout.root, repo)
    if problems:
        return FreezeResult(ok=False, problems=problems)
    intent = IntentSpec.model_validate(_yaml(layout.intent))
    if intent.approved_by != approver:
        intent.approved_by = approver
        layout.intent.write_text(intent_yaml(intent), encoding="utf-8", newline="\n")
        layout.intent_markdown.write_text(intent_markdown(intent), encoding="utf-8", newline="\n")
    manifest = lock(layout.root, intent.feature_id, approver)
    sha = commit_all(
        repo,
        f"factory: freeze contract {intent.feature_id}\n\nApproved-by: {approver}\n"
        f"Contract-Root-Hash: {manifest['root_hash']}",
        require_factory_branch=True,
    )
    return FreezeResult(ok=True, root_hash=manifest["root_hash"], freeze_sha=sha)


def _yaml(path: Path) -> object:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ------------------------------------------------------------------------------------- build/verify
def tad_command(spec: str) -> str:
    return f"python -m factory_core.tad_runner.cli run {spec} --repo ."


def report_file(task_id: str) -> str:
    return f"{RUN_DIR}/{task_id}-report.md"


def planner_prompt(repo: Path, spec: str, stack: str, repo_map: str) -> str:
    return render_prompt(
        "planner",
        spec_dir=spec,
        intent_yaml=(repo / spec / "intent.yaml").read_text(encoding="utf-8"),
        stack=stack,
        repo_map=repo_map,
        tad_command=tad_command(spec),
    )


def plan_markdown(intent: IntentSpec, graph: TaskGraph) -> str:
    """The writing-plans document for the run, assembled from the planner's task graph."""
    layers = " → ".join("[" + ", ".join(layer) + "]" for layer in graph.topological_layers())
    lines = [
        f"# {intent.title} Implementation Plan",
        "",
        "> Executed by the AI software factory as superpowers:subagent-driven-development "
        "(fresh implementer per task, task review after each, whole-branch review at the end).",
        "",
        f"**Goal:** {intent.summary}",
        "",
        f"**Spec:** `{spec_rel(intent.feature_id)}` (approved, hash-locked tests-as-data contract)",
        "",
        f"**Execution order:** {layers}",
        "",
        "## Global Constraints",
        "",
        *[f"- {ac.id}: {ac.description}" for ac in intent.acceptance_criteria],
        *[f"- NFR: {n}" for n in intent.nfrs],
    ]
    for n, t in enumerate(graph.tasks, start=1):
        lines += [
            "",
            f"### Task {n}: {t.title} (`{t.id}`)",
            "",
            t.description,
            "",
            f"Depends on: {', '.join(t.depends_on) or '—'} · Files: {', '.join(t.files) or '—'} · "
            f"Serves: {', '.join(t.ac_ids) or 'all'}",
            "",
            t.plan or "_(no detailed steps; implement the description test-first)_",
        ]
    return "\n".join(lines) + "\n"


def builder_prompt(
    task: Task,
    feature_id: str,
    stack: str,
    report: VerificationReport | None,
    review: ReviewResult | None = None,
) -> str:
    brief = failure_brief(report) if report is not None and not report.passed else ""
    return render_prompt(
        "builder",
        task_title=task.title,
        task_id=task.id,
        task_description=task.description,
        task_files=", ".join(task.files) or "(you decide)",
        task_acs=", ".join(task.ac_ids) or "all",
        task_plan=task.plan or "(no detailed brief — implement the description test-first)",
        spec_dir=spec_rel(feature_id),
        tad_command=tad_command(spec_rel(feature_id)),
        failure_brief=("## Previous verification failed — fix this first\n" + brief + "\n") if brief else "",
        review_findings=review_findings_markdown(review) if review is not None else "",
        report_file=report_file(task.id),
        stack=stack,
    )


def review_findings_markdown(review: ReviewResult) -> str:
    items = "\n".join(
        f"- **{f.severity}** ({f.kind}) {f.file or ''}: {f.detail}"
        + (f"\n  Suggested fix: {f.suggested_fix}" if f.suggested_fix else "")
        for f in review.findings
        if f.severity in ("critical", "important")
    )
    return (
        "## The task review found issues — fix them (After Review Findings)\n"
        "Your previous work on this task is already committed in this worktree. Fix every finding below "
        "test-first, re-run the covering tests, and append a fix report to your report file.\n"
        f"{items}\n" + (f"\nReviewer summary: {review.summary}\n" if review.summary else "")
    )


def review_package(repo: Path, base: str, limit: int = 60000) -> str:
    """Commit list + stat + full diff with context (the superpowers review package) for one task."""
    exclude = f":(exclude){SPECS_ROOT}"
    log = git(repo, "log", "--oneline", f"{base}..HEAD")
    stat = git(repo, "diff", "--stat", base, "HEAD", "--", ".", exclude)
    diff = git(repo, "diff", "--no-color", "-U10", base, "HEAD", "--", ".", exclude)
    if len(diff) > limit:
        diff = diff[:limit] + "\n…(diff truncated)"
    return f"# Commits\n{log}\n\n# Stat\n{stat}\n\n# Diff\n{diff}\n"


def reviewer_prompt(
    repo: Path, task: Task, feature_id: str, base: str, implementer_summary: str, review_round: int
) -> str:
    diff_file = f"{RUN_DIR}/{task.id}-review-{review_round}.diff"
    (repo / RUN_DIR).mkdir(parents=True, exist_ok=True)
    (repo / diff_file).write_text(review_package(repo, base), encoding="utf-8")
    spec = spec_rel(feature_id)
    return render_prompt(
        "reviewer",
        task_id=task.id,
        task_title=task.title,
        task_description=task.description,
        task_acs=", ".join(task.ac_ids) or "all",
        task_plan=task.plan or "(no detailed brief)",
        spec_dir=spec,
        intent_yaml=(repo / spec / "intent.yaml").read_text(encoding="utf-8"),
        report_file=report_file(task.id),
        implementer_summary=implementer_summary[-1500:] or "(none)",
        base_sha=base,
        head_sha=head(repo),
        diff_file=diff_file,
    )


def finisher_prompt(repo: Path, spec: str, report: VerificationReport, branch: str, base_branch: str) -> str:
    return render_prompt(
        "finisher",
        branch=branch,
        base_branch=base_branch,
        intent_yaml=(repo / spec / "intent.yaml").read_text(encoding="utf-8"),
        report=report_markdown(report),
        commit=report.commit,
    )


def diff_text(repo: Path, base: str, limit: int = 30000) -> str:
    text = git(repo, "diff", "--no-color", base, "HEAD", "--", ".", f":(exclude){SPECS_ROOT}")
    return text if len(text) <= limit else text[:limit] + "\n…(diff truncated)"


def auditor_prompt(repo: Path, layout: SpecLayout, report: VerificationReport, base: str, out_dir: Path) -> str:
    survivors = "(none)"
    mj = out_dir / "mutation.json"
    if mj.exists():
        data = json.loads(mj.read_text(encoding="utf-8"))
        survivors = (
            "\n".join(
                f"- {s['file']}:{s['line']} {s['operator']}: `{s['original']}` → `{s['mutated']}`"
                for s in data.get("survivors", [])
            )
            or "(none)"
        )
    return render_prompt(
        "auditor",
        spec_dir=layout.root.relative_to(repo).as_posix(),
        intent_yaml=layout.intent.read_text(encoding="utf-8"),
        gates=report_markdown(report),
        survivors=survivors,
        diff=diff_text(repo, base),
    )


def fresh_clone(source: Path | str, dest: Path, sha: str) -> Path:
    """Clean checkout for the verify phase: nothing from the builder's working directory leaks in."""
    if dest.exists():
        rmtree(dest)
    subprocess.run(["git", "clone", "-q", "--no-local", str(source), str(dest)], check=True, capture_output=True)
    git(dest, "checkout", "-q", sha)
    return dest


def rmtree(path: Path) -> None:
    """rmtree that also removes read-only files (git objects on Windows, locked spec files)."""
    import os
    import stat
    import sys

    def force(func, p, _exc):  # type: ignore[no-untyped-def]
        os.chmod(p, stat.S_IWRITE)
        func(p)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=force)
    else:
        shutil.rmtree(path, onerror=force)
