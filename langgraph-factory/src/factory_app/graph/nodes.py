"""Graph nodes. Each node is small and talks to the outside world only through ``Services``."""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

from langgraph.types import Send, interrupt

from factory_core import pipeline
from factory_core.agents import AgentRequest
from factory_core.gates import GateContext, run_gates
from factory_core.models import AuditResult, GrillTurn, SpecDraft, Task, TaskGraph, VerificationReport
from factory_core.recon import repo_map
from factory_core.reports import chat_summary, failure_brief, pr_body, report_markdown
from factory_core.resources import render_prompt
from factory_core.spec import draft_to_files, render_markdown
from factory_core.stack import detect_stack, load_profile
from factory_core.tad_runner.layout import SpecLayout
from factory_core.tad_runner.runner import load_intent

from ..services import Services, clone
from ..state import FactoryState, VerifyState

ALWAYS_PROTECTED = [".factory/specs/**", ".github/workflows/**", "CODEOWNERS", ".github/CODEOWNERS"]
ENGINE_CMD = re.compile(r"/engine\s+(copilot|claude|codex)\b", re.I)
APPROVE = {"/approve", "approve", "approved", "lgtm", "ship it"}


def _report(state: FactoryState) -> VerificationReport | None:
    raw = state.get("last_report")
    return VerificationReport.model_validate(raw) if raw else None


class Nodes:
    def __init__(self, services: Services) -> None:
        self.s = services

    # ------------------------------------------------------------------------------------- helpers
    def _emit(self, state: FactoryState | VerifyState | dict[str, Any], kind: str, message: str, **data: Any) -> None:
        key = state.get("feature_key") or state.get("feature_id") or "unknown"
        self.s.events.emit(str(key), kind, message, **data)

    def _profile(self, state: FactoryState):  # type: ignore[no-untyped-def]
        return load_profile(state["stack"]) if state.get("stack") else None

    def _protected(self, state: FactoryState) -> list[str]:
        prof = self._profile(state)
        return ALWAYS_PROTECTED + (prof.guarded_files if prof else [])

    # ---------------------------------------------------------------------------------- intake/recon
    def intake(self, state: FactoryState) -> dict[str, Any]:
        engine = state.get("engine") or self.s.settings.factory.default_engine
        self._emit(state, "intake", f"Request received for {state['repo_url']}: {state['request']}")
        return {
            "engine": engine,
            "engine_locked": bool(state.get("engine_locked")),
            "status": "grilling",
            "transcript": [{"role": "human", "text": state["request"]}],
            "critic_rounds": 0,
            "critic_findings": [],
            "iteration": 0,
            "fingerprints": [],
            "freeze_attempts": 0,
        }

    def recon(self, state: FactoryState) -> dict[str, Any]:
        ws = self.s.workspace(state["feature_key"])
        workdir = ws / "repo"
        if not (workdir / ".git").exists():
            clone(state["repo_url"], workdir, state.get("base_branch") or "main")
            pipeline.git(workdir, "checkout", "-q", "-b", f"factory/{state['feature_key']}")
        stack = detect_stack(workdir) or "unknown"
        self._emit(state, "recon", f"Cloned repo; detected stack `{stack}`")
        return {"workdir": str(workdir), "stack": stack, "repo_map": repo_map(workdir)}

    # ---------------------------------------------------------------------------------------- grill
    def grill_turn(self, state: FactoryState) -> dict[str, Any]:
        prompt = pipeline.grill_prompt(
            request=state["request"],
            repo_url=state["repo_url"],
            base_branch=state.get("base_branch", "main"),
            stack=state.get("stack"),
            repo_map=state.get("repo_map", ""),
            transcript=state.get("transcript", []),
            critic_findings=state.get("critic_findings", []),
            default_engine=state["engine"],
            feature_id_hint=state["feature_key"],
        )
        res = self.s.agent(state["engine"]).run(
            AgentRequest(
                role="grill",
                prompt=prompt,
                workdir=Path(state["workdir"]),
                read_only=True,
                schema=GrillTurn,
                meta={"repo_url": state["repo_url"]},
            )
        )
        if not res.ok:
            pending = {
                "type": "questions",
                "questions": [f"The grill agent failed ({res.error[:300]}). Reply anything to retry."],
            }
            self._emit(state, "error", "Grill agent failed", error=res.error[:2000])
            return {"pending": pending, "draft": None}
        turn: GrillTurn = res.data
        if turn.status == "questions":
            text = "\n".join(turn.questions)
            self._emit(state, "questions", text, questions=turn.questions, notes=turn.notes)
            return {
                "pending": {"type": "questions", "questions": turn.questions, "notes": turn.notes},
                "draft": None,
                "critic_findings": [],
                "status": "grilling",
                "transcript": [{"role": "factory", "text": text}],
            }
        assert turn.draft is not None
        problems = pipeline.critique(turn.draft)
        rounds = state.get("critic_rounds", 0)
        if problems and rounds < 2:
            self._emit(
                state, "critic", f"Spec critic found {len(problems)} issue(s); revising draft", problems=problems
            )
            return {"pending": None, "draft": None, "critic_findings": problems, "critic_rounds": rounds + 1}
        markdown = render_markdown(draft_to_files(turn.draft))
        pending = {
            "type": "approval",
            "summary": turn.notes,
            "contract_markdown": markdown,
            "problems": problems,
            "engine": turn.draft.intent.engine,
            "engine_recommendation": turn.engine_recommendation,
        }
        self._emit(state, "approval_needed", turn.notes or "Draft contract ready for review", **pending)
        return {
            "pending": pending,
            "draft": turn.draft.model_dump(),
            "critic_findings": [],
            "critic_rounds": 0,
            "status": "awaiting_approval",
            "transcript": [{"role": "factory", "text": f"[draft contract presented]\n{turn.notes}"}],
        }

    def ask_human(self, state: FactoryState) -> dict[str, Any]:
        answer = interrupt(state.get("pending") or {"type": "questions", "questions": ["Anything else?"]})
        if isinstance(answer, str):
            answer = {"text": answer}
        text = str(answer.get("text") or "").strip()
        action = str(answer.get("action") or "").lower() or None
        user = str(answer.get("user") or state.get("requester") or "human")
        update: dict[str, Any] = {"pending": None}
        engine = answer.get("engine") or (m.group(1).lower() if (m := ENGINE_CMD.search(text)) else None)
        if engine:
            update.update(engine=engine, engine_locked=True)
        if action == "abort" or text.lower() in ("/abort", "abort"):
            update.update(status="aborted")
            return update
        is_approval = action == "approve" or text.lower() in APPROVE
        if is_approval and (state.get("pending") or {}).get("type") == "approval" and state.get("draft"):
            update.update(
                approved_by=user, status="freezing", transcript=[{"role": "human", "text": f"/approve ({user})"}]
            )
            return update
        update.update(transcript=[{"role": "human", "text": text or "(no answer)"}], draft=None, status="grilling")
        return update

    # --------------------------------------------------------------------------------------- freeze
    def freeze(self, state: FactoryState) -> dict[str, Any]:
        workdir = Path(state["workdir"])
        draft = SpecDraft.model_validate(state["draft"])
        engine = state["engine"]
        if state.get("engine_locked"):
            draft.intent.engine = engine  # type: ignore[assignment]
        else:
            engine = draft.intent.engine
        draft.intent.approved_by = state.get("approved_by")
        profile = self._profile(state)
        spec_dir = workdir / pipeline.spec_rel(draft.intent.feature_id)
        if spec_dir.exists():
            pipeline.rmtree(spec_dir)
        layout = pipeline.write_contract(workdir, draft, profile)
        problems: list[str] = []
        result = None
        for _attempt in range(3):
            prompt = pipeline.driver_writer_prompt(layout, workdir, profile)
            if problems:
                prompt += "\n\n## Your previous driver failed the freeze check\n" + "\n".join(
                    f"- {p}" for p in problems
                )
            protected = [
                f"{pipeline.spec_rel(draft.intent.feature_id)}/{p}"
                for p in ("intent.yaml", "examples/**", "scenarios/**", "properties.yaml", "features/**")
            ]
            self.s.agent(engine).run(
                AgentRequest(role="driver_writer", prompt=prompt, workdir=workdir, protected=protected)
            )
            result = pipeline.freeze(workdir, layout, state.get("approved_by") or "human")
            if result.ok:
                break
            problems = result.problems
        if not result or not result.ok:
            msg = "The contract could not be frozen:\n" + "\n".join(f"- {p}" for p in problems[:15])
            self._emit(state, "freeze_failed", msg, problems=problems)
            return {
                "status": "grilling",
                "draft": None,
                "approved_by": None,
                "pending": {"type": "questions", "questions": [msg, "How should the contract change?"]},
                "transcript": [{"role": "factory", "text": msg}],
                "freeze_attempts": state.get("freeze_attempts", 0) + 1,
            }
        self._emit(
            state,
            "frozen",
            f"Contract frozen at {result.freeze_sha[:10]} (root {result.root_hash[:12]})",
            manifest_hash=result.root_hash,
            freeze_sha=result.freeze_sha,
        )
        return {
            "feature_id": draft.intent.feature_id,
            "spec_rel": pipeline.spec_rel(draft.intent.feature_id),
            "freeze_sha": result.freeze_sha,
            "manifest_hash": result.root_hash,
            "engine": engine,
            "status": "planning",
            "pending": None,
        }

    def route_after_freeze(self, state: FactoryState) -> str:
        return "plan" if state.get("status") == "planning" else "ask_human"

    # ----------------------------------------------------------------------------------------- plan
    def plan(self, state: FactoryState) -> dict[str, Any]:
        workdir = Path(state["workdir"])
        intent = load_intent(SpecLayout(workdir / state["spec_rel"]))
        prompt = render_prompt(
            "planner",
            spec_dir=state["spec_rel"],
            intent_yaml=(workdir / state["spec_rel"] / "intent.yaml").read_text(encoding="utf-8"),
            stack=state["stack"],
            repo_map=state.get("repo_map", ""),
        )
        res = self.s.agent(state["engine"]).run(
            AgentRequest(role="planner", prompt=prompt, workdir=workdir, read_only=True, schema=TaskGraph)
        )
        if res.ok:
            graph: TaskGraph = res.data
        else:
            graph = TaskGraph(
                tasks=[
                    Task(id="feature", title=intent.title, description=intent.summary, ac_ids=sorted(intent.ac_ids()))
                ]
            )
        layers = graph.topological_layers()
        self._emit(
            state,
            "planned",
            f"Task graph: {' → '.join('[' + ', '.join(layer) + ']' for layer in layers)}",
            task_graph=graph.model_dump(),
        )
        return {"task_graph": graph.model_dump(), "completed": [], "iteration": 1, "status": "building"}

    # ---------------------------------------------------------------------------------------- build
    def schedule(self, state: FactoryState) -> dict[str, Any]:
        return {"task_results": None}

    def route_schedule(self, state: FactoryState) -> list[Send] | str:
        graph = TaskGraph.model_validate(state["task_graph"])
        ready = graph.ready(set(state.get("completed", [])))
        if not ready:
            return "verify"
        return [
            Send(
                "worker",
                {
                    "task": t.model_dump(),
                    "workdir": state["workdir"],
                    "feature_key": state["feature_key"],
                    "feature_id": state["feature_id"],
                    "engine": state["engine"],
                    "stack": state["stack"],
                    "iteration": state.get("iteration", 1),
                    "last_report": state.get("last_report"),
                    "protected": self._protected(state),
                },
            )
            for t in ready
        ]

    def worker(self, payload: dict[str, Any]) -> dict[str, Any]:
        task = Task.model_validate(payload["task"])
        workdir = Path(payload["workdir"])
        tag = f"{task.id}-{payload['iteration']}-{uuid.uuid4().hex[:4]}"
        branch = f"factory-task/{payload['feature_key']}/{tag}"
        wt = workdir.parent / "worktrees" / tag
        pipeline.git(workdir, "worktree", "add", "-q", "-b", branch, str(wt), "HEAD")
        report = VerificationReport.model_validate(payload["last_report"]) if payload.get("last_report") else None
        prompt = pipeline.builder_prompt(task, payload["feature_id"], payload["stack"], report)
        self.s.events.emit(payload["feature_key"], "building", f"Task `{task.id}`: {task.title}")
        res = self.s.agent(payload["engine"]).run(
            AgentRequest(role="builder", prompt=prompt, workdir=wt, protected=payload["protected"])
        )
        sha = pipeline.commit_all(wt, f"factory: {task.title}\n\nTask: {task.id}")
        return {
            "task_results": [
                {
                    "task_id": task.id,
                    "branch": branch,
                    "worktree": str(wt),
                    "ok": res.ok,
                    "summary": (res.text or res.error)[-500:],
                    "sha": sha,
                }
            ]
        }

    def integrate(self, state: FactoryState) -> dict[str, Any]:
        workdir = Path(state["workdir"])
        results = sorted(state.get("task_results") or [], key=lambda r: r["task_id"])
        for r in results:
            out = pipeline.git(
                workdir, "merge", "--no-ff", "-q", "-m", f"factory: merge task {r['task_id']}", r["branch"], check=False
            )
            conflicts = pipeline.git(workdir, "diff", "--name-only", "--diff-filter=U", check=False)
            if conflicts:
                self._emit(state, "conflict", f"Resolving merge conflicts for {r['task_id']}: {conflicts}")
                prompt = render_prompt(
                    "resolver",
                    conflicts=conflicts,
                    tasks="\n".join(f"- {x['task_id']}: {x['summary'][:200]}" for x in results),
                )
                self.s.agent(state["engine"]).run(
                    AgentRequest(role="resolver", prompt=prompt, workdir=workdir, protected=self._protected(state))
                )
                pipeline.git(workdir, "add", "-A")
                pipeline.git(workdir, "commit", "-q", "--no-edit", check=False)
            elif "fatal" in out.lower():
                self._emit(state, "error", f"merge of {r['task_id']} failed: {out[:300]}")
            pipeline.git(workdir, "worktree", "remove", "--force", r["worktree"], check=False)
            pipeline.git(workdir, "branch", "-D", r["branch"], check=False)
        done = sorted(set(state.get("completed", [])) | {r["task_id"] for r in results})
        return {"completed": done}

    # --------------------------------------------------------------------------------------- verify
    def verify(self, state: FactoryState) -> dict[str, Any]:
        """Hand the verify subgraph ONLY what it needs — a commit and the trust anchors."""
        ws = self.s.workspace(state["feature_key"])
        vs: VerifyState = {
            "feature_id": state["feature_id"],
            "source_repo": state["workdir"],
            "commit": pipeline.head(Path(state["workdir"])),
            "freeze_sha": state["freeze_sha"],
            "manifest_hash": state["manifest_hash"],
            "spec_rel": state["spec_rel"],
            "stack": state["stack"],
            "engine": state["engine"],
            "iteration": state.get("iteration", 1),
            "verify_dir": str(ws / "verify" / f"iter-{state.get('iteration', 1)}"),
        }
        self._emit(
            state, "verifying", f"Iteration {vs['iteration']}: verifying {vs['commit'][:10]} in a clean checkout"
        )
        out = self.verify_graph.invoke(vs)  # type: ignore[attr-defined]
        report = VerificationReport.model_validate(out["report"])
        self._emit(
            state, "verified", chat_summary(report), passed=report.passed, report_markdown=report_markdown(report)
        )
        return {"last_report": report.model_dump(), "status": "verifying"}

    # verify subgraph nodes (VerifyState only)
    def v_checkout(self, vs: VerifyState) -> dict[str, Any]:
        pipeline.fresh_clone(vs["source_repo"], Path(vs["verify_dir"]) / "repo", vs["commit"])
        return {}

    def v_gates(self, vs: VerifyState) -> dict[str, Any]:
        repo = Path(vs["verify_dir"]) / "repo"
        ctx = GateContext(
            repo=repo,
            spec_dir=repo / vs["spec_rel"],
            base_sha=vs["freeze_sha"],
            profile=load_profile(vs["stack"]),
            config=self.s.settings.factory,
            expected_hash=vs["manifest_hash"],
            out_dir=Path(vs["verify_dir"]) / "out",
        )
        report = VerificationReport(
            feature_id=vs["feature_id"], iteration=vs["iteration"], commit=vs["commit"], gates=run_gates(ctx)
        )
        return {"report": report.model_dump()}

    def v_audit(self, vs: VerifyState) -> dict[str, Any]:
        report = VerificationReport.model_validate(vs["report"])
        if not report.passed:
            return {}  # deterministic failures already give the builder everything it needs
        repo = Path(vs["verify_dir"]) / "repo"
        prompt = pipeline.auditor_prompt(
            repo, SpecLayout(repo / vs["spec_rel"]), report, vs["freeze_sha"], Path(vs["verify_dir"]) / "out"
        )
        res = self.s.agent(vs["engine"]).run(
            AgentRequest(role="auditor", prompt=prompt, workdir=repo, read_only=True, schema=AuditResult)
        )
        if res.ok:
            report.audit = res.data.findings
        return {"report": report.model_dump()}

    # --------------------------------------------------------------------------------------- triage
    def triage(self, state: FactoryState) -> dict[str, Any]:
        report = _report(state)
        assert report is not None
        if report.passed:
            return {"escalation": None}
        cfg = self.s.settings.factory
        fps = list(state.get("fingerprints", [])) + [report.fingerprint()]
        contradiction = [f for f in report.audit if f.severity == "blocking" and f.category == "spec_contradiction"]
        stalled = fps.count(fps[-1]) >= cfg.stall_limit
        capped = cfg.max_iterations and state.get("iteration", 1) >= cfg.max_iterations
        if contradiction or stalled or capped:
            reason = (
                "the auditor believes the contract is contradictory: " + contradiction[0].detail
                if contradiction
                else f"the same failures repeated {cfg.stall_limit} times"
                if stalled
                else f"iteration cap {cfg.max_iterations} reached"
            )
            esc = {
                "type": "escalation",
                "reason": reason,
                "report_markdown": report_markdown(report),
                "options": ["continue", "amend", "abort"],
            }
            self._emit(
                state, "escalation", f"Need your call: {reason}. Reply continue / amend <change> / abort.", **esc
            )
            return {"fingerprints": fps, "escalation": esc, "status": "escalated", "pending": esc}
        return {"fingerprints": fps, **self._fix_round(state, report)}

    def _fix_round(self, state: FactoryState, report: VerificationReport) -> dict[str, Any]:
        it = state.get("iteration", 1) + 1
        failing = ", ".join(g.name for g in report.gates if g.blocking) or "audit findings"
        fix = Task(
            id=f"fix-{it}",
            title=f"Fix verification failures ({failing})",
            description=failure_brief(report, max_chars=3000),
        )
        self._emit(state, "fixing", f"Iteration {it}: sending failures back to a fresh builder ({failing})")
        return {
            "task_graph": TaskGraph(tasks=[fix]).model_dump(),
            "completed": [],
            "iteration": it,
            "status": "building",
            "escalation": None,
        }

    def route_triage(self, state: FactoryState) -> str:
        report = _report(state)
        if report and report.passed:
            return "pr"
        return "escalate" if state.get("escalation") else "schedule"

    def escalate(self, state: FactoryState) -> dict[str, Any]:
        answer = interrupt(state.get("escalation") or {})
        if isinstance(answer, str):
            answer = {"text": answer}
        text = str(answer.get("text") or "").strip()
        action = str(answer.get("action") or text.split(" ", 1)[0]).lower().lstrip("/")
        if action == "abort":
            return {"status": "aborted", "escalation": None, "pending": None}
        if action == "amend":
            change = text.split(" ", 1)[1] if " " in text else text
            return {
                "status": "grilling",
                "escalation": None,
                "pending": None,
                "draft": None,
                "approved_by": None,
                "transcript": [{"role": "human", "text": f"Amend the approved contract: {change}"}],
            }
        report = _report(state)
        assert report is not None
        return {"fingerprints": [], "pending": None, **self._fix_round(state, report)}

    def route_escalate(self, state: FactoryState) -> str:
        return {"aborted": "end", "grilling": "grill_turn"}.get(state.get("status", ""), "schedule")

    # ------------------------------------------------------------------------------------------- pr
    def pr(self, state: FactoryState) -> dict[str, Any]:
        workdir = Path(state["workdir"])
        layout = SpecLayout(workdir / state["spec_rel"])
        intent = load_intent(layout)
        report = _report(state)
        assert report is not None
        body = pr_body(
            intent,
            state["spec_rel"],
            layout.root,
            report,
            state.get("iteration", 1),
            state["manifest_hash"],
            state["engine"],
        )
        url = self.s.githost.open_pr(
            workdir,
            state["repo_url"],
            f"factory/{state['feature_key']}",
            state.get("base_branch", "main"),
            f"feat: {intent.title}",
            body,
        )
        self._emit(state, "pr_opened", f"PR ready for review: {url}", pr_url=url)
        return {"pr_url": url, "status": "pr_opened"}
