"""Pre-agent step for gh-aw workflows: render the phase prompt into /tmp/gh-aw/factory/prompt.md.

Each agentic workflow run is a fresh runner + a fresh agent session; this script decides exactly what
context that session receives (nothing else from previous phases leaks in).

    python gh-aw-factory/scripts/context.py --phase build --issue 12 --target target [--fix true]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from ghaw_lib import load_issue

from factory_core import pipeline
from factory_core.models import Task, TaskGraph, VerificationReport
from factory_core.recon import repo_map
from factory_core.stack import detect_stack, load_profile
from factory_core.tad_runner.layout import SpecLayout

COMMENT_RULE = (
    "\n\n---\nPost your answer as exactly ONE comment on issue #{n} of {repo} using the add-comment safe "
    "output. Start the comment with the line `{marker}`."
)
INLINE_EXECUTION = (
    "\n\n---\nIn this workflow there is one build session for the whole plan, so you are both controller and "
    "implementer (this overrides the one-task and no-final-review rules above): execute the tasks in order "
    "with the `executing-plans` skill (inline execution, no "
    "subagents, no worktrees), `test-driven-development` for every step, and before finishing do the "
    "`requesting-code-review` self-review of the whole change against `code-reviewer.md` and fix "
    "Critical/Important findings test-first. The factory's audit phase is the independent whole-branch review."
)


def latest_report(issue) -> VerificationReport | None:  # type: ignore[no-untyped-def]
    c = issue.latest("verify")
    data = c.json_block() if c else None
    return VerificationReport.model_validate(data) if data else None


def render(phase: str, issue, target: Path, repo: str, fix: bool) -> str:  # type: ignore[no-untyped-def]
    n = issue.number
    stack = detect_stack(target)
    profile = load_profile(stack) if stack else None
    anchors = issue.anchors
    if phase == "grill":
        req = issue.request
        prompt = pipeline.grill_prompt(
            request=str(req.get("request", "")),
            repo_url=str(req.get("repo", "")),
            base_branch=str(req.get("base", "main")),
            stack=stack,
            repo_map=repo_map(target),
            transcript=issue.transcript(),
            critic_findings=_critic(issue),
            default_engine=issue.engine,
            feature_id_hint=f"issue-{n}",
            markdown=True,
        )
        return prompt + COMMENT_RULE.format(n=n, repo=repo, marker="(questions: no marker; drafts: [factory:draft])")
    layout = SpecLayout(target / anchors["spec_rel"])
    if phase == "driver":
        return (
            pipeline.driver_writer_prompt(layout, target, profile)
            + "\n\nThe repository is checked out in `./target`; write the driver there."
        )
    if phase == "plan":
        prompt = pipeline.planner_prompt(target, anchors["spec_rel"], stack or "unknown", repo_map(target))
        return (
            prompt
            + COMMENT_RULE.format(n=n, repo=repo, marker="[factory:plan]")
            + ' Then give the task graph as one ```json block: {"tasks": [...]} (each task with its `plan`).'
        )
    if phase == "build":
        plan_comment = issue.latest("plan")
        graph = TaskGraph.model_validate(plan_comment.json_block()) if plan_comment else None
        order = [t for layer in graph.topological_layers() for t in layer] if graph else []
        description = (
            "\n".join(
                f"{i + 1}. **{graph.get(t).title}** — {graph.get(t).description}"  # type: ignore[union-attr]
                for i, t in enumerate(order)
            )
            or "Implement the approved contract."
        )
        brief = "\n\n".join(
            f"#### Task {i + 1}: {graph.get(t).title} (`{t}`)\n{graph.get(t).plan or '(no detailed steps)'}"  # type: ignore[union-attr]
            for i, t in enumerate(order)
        )
        task = Task(
            id=f"build-{issue.iteration}",
            title="Implement the plan (tasks in order)",
            description=description,
            ac_ids=[],
            plan=brief,
        )
        report = latest_report(issue) if fix else None
        return (
            pipeline.builder_prompt(task, anchors["feature_id"], stack or "unknown", report)
            + "\n\nThe repository is checked out in `./target`; make all changes there."
            + INLINE_EXECUTION
        )
    if phase == "audit":
        report = latest_report(issue)
        assert report is not None, "audit needs a verification report"
        prompt = pipeline.auditor_prompt(target, layout, report, anchors["freeze_sha"], Path("/nonexistent"))
        return (
            prompt
            + COMMENT_RULE.format(n=n, repo=repo, marker="[factory:audit]")
            + ' Then give the findings as one ```json block: {"findings": [...]} (empty list if none).'
        )
    raise SystemExit(f"unknown phase {phase}")


def _critic(issue) -> list[str]:  # type: ignore[no-untyped-def]
    c = issue.latest("freezefailed")
    return (c.json_block() or {}).get("problems", []) if c else []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True, choices=["grill", "driver", "plan", "build", "audit"])
    ap.add_argument("--issue", type=int, required=True)
    ap.add_argument("--target", type=Path, default=Path("target"))
    ap.add_argument("--fix", default="false")
    ap.add_argument("--out", type=Path, default=Path("/tmp/gh-aw/factory"))
    a = ap.parse_args()
    repo = os.environ["GITHUB_REPOSITORY"]
    issue = load_issue(repo, a.issue)
    a.out.mkdir(parents=True, exist_ok=True)
    text = render(a.phase, issue, a.target, repo, a.fix == "true")
    (a.out / "prompt.md").write_text(text, encoding="utf-8")
    print(f"wrote {a.out / 'prompt.md'} ({len(text)} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
