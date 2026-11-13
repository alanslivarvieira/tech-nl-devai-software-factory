"""Deterministic orchestrator for the gh-aw factory.

Triggered by human comments on factory issues and by completion of any factory workflow. ``decide`` is a
pure function (unit-tested); ``main`` loads the event + issue and executes the resulting actions with gh.

Flow:  grill ⇄ human → /approve → step:prepare-contract → driver agent → step:apply-patch(driver)
       → step:freeze → plan agent → build agent → step:apply-patch(build) → step:verify (fresh runner)
       → audit agent (fresh runner) → step:open-pr          … any failure → build again (iter+1)
       … same failure fingerprint `stall_limit` times → escalate (/continue | /amend … | /abort)
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))
from ghaw_lib import ENGINES, HUMAN_ASSOCIATIONS, Issue

STEP = "factory-step.yml"
ENGINE_CMD = re.compile(r"^/engine\s+(\w+)", re.M)


def agentic(phase: str, engine: str) -> str:
    return f"factory-{phase}-{engine}.lock.yml"


@dataclass
class Event:
    kind: str  # "comment" | "run"
    text: str = ""
    author: str = ""
    association: str = ""
    is_bot: bool = False
    workflow: str = ""  # e.g. "factory-build" (engine suffix stripped) or "factory-step"
    step: str = ""
    conclusion: str = ""
    run_id: str = ""


@dataclass
class Plan:
    actions: list[dict[str, Any]] = field(default_factory=list)

    def dispatch(self, workflow: str, **inputs: Any) -> Plan:
        self.actions.append({"do": "dispatch", "workflow": workflow, "inputs": {k: str(v) for k, v in inputs.items()}})
        return self

    def label(self, **values: Any) -> Plan:
        self.actions.append({"do": "label", "values": {k: str(v) for k, v in values.items()}})
        return self

    def comment(self, body: str) -> Plan:
        self.actions.append({"do": "comment", "body": body})
        return self

    def close(self) -> Plan:
        self.actions.append({"do": "close"})
        return self


def _stalled(issue: Issue, stall_limit: int) -> bool:
    fps = [m.get("fingerprint") for m in issue.all_markers("verify") if m.get("passed") != "true"]
    return bool(fps) and fps.count(fps[-1]) >= stall_limit


def _next_build(issue: Issue, plan: Plan, stall_limit: int, max_iterations: int, reason: str) -> Plan:
    if _stalled(issue, stall_limit) or (max_iterations and issue.iteration >= max_iterations):
        why = (
            f"the same failures repeated {stall_limit} times"
            if _stalled(issue, stall_limit)
            else f"iteration cap {max_iterations} reached"
        )
        return plan.label(phase="escalated").comment(
            f"🚨 **Escalation** — {why} ({reason}).\n\nReply `/continue` to keep looping, "
            "`/amend <what to change in the contract>` to go back to the grill, or `/abort`."
        )
    return plan.label(phase="build", iter=issue.iteration + 1).dispatch(
        agentic("build", issue.engine), issue=issue.number, fix="true"
    )


def decide(event: Event, issue: Issue, stall_limit: int = 3, max_iterations: int = 0) -> Plan:
    plan = Plan()
    n = issue.number
    if issue.phase in ("done", "aborted") or issue.state != "OPEN":
        return plan

    # ------------------------------------------------------------------------------ human comments
    if event.kind == "comment":
        if event.is_bot or event.association not in HUMAN_ASSOCIATIONS:
            return plan  # only repo collaborators steer the factory; agents' comments never do
        text = event.text.strip()
        if m := ENGINE_CMD.search(text):
            engine = m.group(1).lower()
            if engine not in ENGINES:
                return plan.comment(f"Unknown engine `{engine}`; choose one of {', '.join(ENGINES)}.")
            plan.label(engine=engine).comment(f"🛠️ Coding agent set to **{engine}**.")
            issue.labels = [lbl for lbl in issue.labels if not lbl.startswith("engine:")] + [f"engine:{engine}"]
            text = ENGINE_CMD.sub("", text).strip()
            if not text:
                return plan
        if text.startswith("/abort"):
            return plan.label(phase="aborted").comment("🛑 Aborted.").close()
        if issue.phase == "grill":
            if text.startswith("/approve"):
                if issue.latest("draft") is None:
                    return plan.comment("There is no draft contract to approve yet.")
                return (
                    plan.label(phase="contract")
                    .comment(
                        f"✅ Contract approved by @{event.author}. Freezing it (driver → freeze-check → hash-lock)…"
                    )
                    .dispatch(STEP, step="prepare-contract", issue=n, approver=event.author)
                )
            return plan.dispatch(agentic("grill", issue.engine), issue=n)
        if issue.phase == "escalated":
            if text.startswith("/continue"):
                return plan.label(phase="build", iter=issue.iteration + 1).dispatch(
                    agentic("build", issue.engine), issue=n, fix="true"
                )
            if text.startswith("/amend"):
                return plan.label(phase="grill").dispatch(agentic("grill", issue.engine), issue=n)
        return plan

    # ------------------------------------------------------------------------- workflow completions
    if event.conclusion == "cancelled":
        return plan
    ok = event.conclusion == "success"
    wf, step = event.workflow, event.step
    if wf == "factory-grill" and not ok:
        return plan.comment("⚠️ The grill agent failed. Reply to this issue to retry.")
    if wf == "factory-step" and step == "prepare-contract":
        return (
            plan.label(phase="driver").dispatch(agentic("driver", issue.engine), issue=n)
            if ok
            else plan.label(phase="grill").comment("⚠️ The approved draft could not be written; please revise it.")
        )
    if wf == "factory-driver":
        return (
            plan.dispatch(STEP, step="apply-patch", issue=n, run_id=event.run_id, mode="driver")
            if ok
            else plan.comment("⚠️ The driver agent failed. Reply `/approve` again to retry.").label(phase="grill")
        )
    if wf == "factory-step" and step == "apply-patch":
        mode = "build" if issue.phase == "build" else "driver"
        if mode == "driver":
            return (
                plan.label(phase="freeze").dispatch(STEP, step="freeze", issue=n)
                if ok
                else plan.label(phase="grill").comment("⚠️ The driver patch was rejected by the guard. Reply to revise.")
            )
        if ok:
            return plan.label(phase="verify").dispatch(STEP, step="verify", issue=n)
        return _next_build(issue, plan, stall_limit, max_iterations, "guard rejected the patch")
    if wf == "factory-step" and step == "freeze":
        return (
            plan.label(phase="plan").dispatch(agentic("plan", issue.engine), issue=n)
            if ok
            else plan.label(phase="grill").dispatch(agentic("grill", issue.engine), issue=n)
        )
    if wf == "factory-plan":
        return plan.label(phase="build", iter=1).dispatch(agentic("build", issue.engine), issue=n)
    if wf == "factory-build":
        return (
            plan.dispatch(STEP, step="apply-patch", issue=n, run_id=event.run_id, mode="build")
            if ok
            else _next_build(issue, plan, stall_limit, max_iterations, "build agent failed")
        )
    if wf == "factory-step" and step == "verify":
        if ok:
            return plan.label(phase="audit").dispatch(agentic("audit", issue.engine), issue=n)
        return _next_build(issue, plan, stall_limit, max_iterations, "verification failed")
    if wf == "factory-audit":
        audit = issue.latest("audit")
        findings = (audit.json_block() or {}).get("findings", []) if audit else []
        blocking = [f for f in findings if f.get("severity") == "blocking"]
        if any(f.get("category") == "spec_contradiction" for f in blocking):
            return plan.label(phase="escalated").comment(
                "🚨 The auditor thinks the contract contradicts itself. `/amend <change>` or `/continue`."
            )
        if blocking:
            return _next_build(issue, plan, stall_limit, max_iterations, "audit found blocking issues")
        return plan.label(phase="pr").dispatch(STEP, step="open-pr", issue=n)
    if wf == "factory-step" and step == "open-pr" and ok:
        return plan.label(phase="done")
    return plan


# ------------------------------------------------------------------------------------------------ main
TITLE = re.compile(r"factory (?:step (?P<step>[a-z-]+) )?(?:\w+ )?#(?P<issue>\d+)")


def event_from_github(payload: dict[str, Any], event_name: str) -> tuple[Event, int] | None:
    if event_name == "issue_comment":
        c = payload["comment"]
        login = c["user"]["login"]
        # The Teams bot relays humans' messages under a service identity listed in FACTORY_RELAY_LOGINS.
        relays = {r.strip() for r in os.environ.get("FACTORY_RELAY_LOGINS", "").split(",") if r.strip()}
        if login in relays:
            return Event(kind="comment", text=c["body"], author=login, association="MEMBER"), payload["issue"]["number"]
        return Event(
            kind="comment",
            text=c["body"],
            author=login,
            association=c.get("author_association", ""),
            is_bot=c["user"].get("type") == "Bot" or login.endswith("[bot]"),
        ), payload["issue"]["number"]
    if event_name == "workflow_run":
        run = payload["workflow_run"]
        m = TITLE.search(run.get("display_title", ""))
        if not m:
            return None
        name = re.sub(r"-(copilot|claude|codex)$", "", run["name"])
        return Event(
            kind="run",
            workflow=name,
            step=m.group("step") or "",
            conclusion=run.get("conclusion") or "",
            run_id=str(run["id"]),
        ), int(m.group("issue"))
    return None


def main() -> int:
    from ghaw_lib import comment, dispatch, gh, load_issue, relabel

    repo = os.environ["GITHUB_REPOSITORY"]
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as fh:
        payload = json.load(fh)
    parsed = event_from_github(payload, os.environ["GITHUB_EVENT_NAME"])
    if not parsed:
        print("not a factory event")
        return 0
    event, number = parsed
    issue = load_issue(repo, number)
    if "factory" not in issue.labels:
        print("issue is not a factory issue")
        return 0
    plan = decide(
        event,
        issue,
        int(os.environ.get("FACTORY_STALL_LIMIT", "3")),
        int(os.environ.get("FACTORY_MAX_ITERATIONS", "0")),
    )
    for a in plan.actions:
        print(json.dumps(a))
        if a["do"] == "label":
            relabel(repo, issue, **a["values"])
            issue = load_issue(repo, number)
        elif a["do"] == "comment":
            comment(repo, number, a["body"])
        elif a["do"] == "dispatch":
            dispatch(repo, a["workflow"], **a["inputs"])
        elif a["do"] == "close":
            gh("issue", "close", str(number), "--repo", repo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
