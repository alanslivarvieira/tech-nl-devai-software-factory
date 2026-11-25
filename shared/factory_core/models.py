"""Typed models shared by both factories. JSON Schemas in ``schemas/`` are exported from these."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Engine = Literal["copilot", "claude", "codex"]
ENGINES: tuple[str, ...] = ("copilot", "claude", "codex")
AgentRole = Literal[
    "grill", "story_critic", "driver_writer", "planner", "builder", "reviewer", "auditor", "resolver", "finisher"
]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------------------------------
# Intent spec (the human-approved contract produced by the grill session)
# --------------------------------------------------------------------------------------------------


class AcceptanceCriterion(_Model):
    id: str = Field(pattern=r"^AC-\d+$")
    description: str


class OpSpec(_Model):
    """A DSL operation the tests use. The driver maps it onto the system's public interface."""

    name: str = Field(pattern=r"^[a-z_][a-z0-9_]*$")
    description: str = ""
    input: dict[str, str] = Field(default_factory=dict, description="field -> type/description")
    output: dict[str, str] = Field(default_factory=dict, description="field -> type/description")


class HttpRoute(_Model):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "POST"
    path: str


class InterfaceSpec(_Model):
    """How the factory-owned runner reaches the system under test.

    * ``jsonl`` – a driver process (``command``) speaking JSON lines on stdin/stdout (default).
    * ``http``  – call HTTP routes on ``base_url`` (optionally starting ``command`` first).
    * ``cli``   – run a command line per op (``cli[op]`` is an argv template with ``{field}``).
    """

    kind: Literal["jsonl", "http", "cli"] = "jsonl"
    command: list[str] | None = None
    base_url: str | None = None
    routes: dict[str, HttpRoute] = Field(default_factory=dict)
    cli: dict[str, list[str]] = Field(default_factory=dict)
    ops: list[OpSpec] = Field(min_length=1)
    timeout_s: float = 15.0
    entrypoint_hint: str = Field(
        default="", description="Where the production code must expose this behaviour (module/route/cmd)."
    )

    def op_names(self) -> set[str]:
        return {op.name for op in self.ops}


class Decision(_Model):
    question: str
    answer: str


class RepoRef(_Model):
    url: str
    base_branch: str = "main"


class IntentSpec(_Model):
    feature_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    title: str
    summary: str
    repo: RepoRef
    stack: str | None = None
    engine: Engine = "copilot"
    actors: list[str] = Field(default_factory=list)
    scope_in: list[str] = Field(default_factory=list)
    scope_out: list[str] = Field(default_factory=list)
    acceptance_criteria: list[AcceptanceCriterion] = Field(min_length=1)
    interface: InterfaceSpec
    nfrs: list[str] = Field(default_factory=list)
    decisions: list[Decision] = Field(default_factory=list)
    approved_by: str | None = None

    @field_validator("acceptance_criteria")
    @classmethod
    def _unique_ac(cls, v: list[AcceptanceCriterion]) -> list[AcceptanceCriterion]:
        ids = [ac.id for ac in v]
        if len(ids) != len(set(ids)):
            raise ValueError("acceptance criterion ids must be unique")
        return v

    def ac_ids(self) -> set[str]:
        return {ac.id for ac in self.acceptance_criteria}


class SpecDraft(_Model):
    """What the grill agent proposes; written to disk on approval (see ``factory_core.spec``)."""

    intent: IntentSpec
    examples: dict[str, str] = Field(default_factory=dict, description="file name (.csv) -> CSV text")
    scenarios: dict[str, str] = Field(default_factory=dict, description="file name (.yaml) -> YAML text")
    properties: str | None = Field(default=None, description="properties.yaml text")


class GrillOption(_Model):
    key: str = Field(description="Short letter the human can answer with, e.g. 'A'")
    label: str


class GrillQuestion(_Model):
    """One question of a grilling round (Matt Pocock's ``grilling`` skill): the human may answer `yes` to accept
    the recommendation, pick an option compactly (``1A, 2B``) or answer in free text."""

    title: str = ""
    body: str
    options: list[GrillOption] = Field(default_factory=list)
    recommendation: str = Field(default="", description="Your recommended answer and why")

    def render(self, number: int) -> str:
        """Markdown that keeps one option per line in Teams too (Teams joins plain lines; list items survive)."""
        head = f"**Q{number}** - **{self.title}**: {self.body}" if self.title else f"**Q{number}** - {self.body}"
        lines = [f"{number}. {head}"]
        lines += [f"   - **{o.key})** {o.label}" for o in self.options]
        if self.recommendation:
            lines.append(f"   - ➡️ Recommended: {self.recommendation}")
        return "\n".join(lines)


class GrillTurn(_Model):
    """Structured output of one grill-agent turn (one grilling round)."""

    status: Literal["questions", "draft"]
    questions: list[GrillQuestion] = Field(default_factory=list)
    engine_recommendation: Engine | None = None
    draft: SpecDraft | None = None
    notes: str = ""

    @field_validator("questions", mode="before")
    @classmethod
    def _plain_questions(cls, v: Any) -> Any:
        return [{"body": q} if isinstance(q, str) else q for q in v] if isinstance(v, list) else v

    @model_validator(mode="after")
    def _consistent(self) -> GrillTurn:
        if self.status == "questions" and not self.questions:
            raise ValueError("status=questions requires at least one question")
        if self.status == "draft" and self.draft is None:
            raise ValueError("status=draft requires a draft")
        return self


# --------------------------------------------------------------------------------------------------
# Task graph (graph engineering for the build phase)
# --------------------------------------------------------------------------------------------------


class Task(_Model):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    title: str
    description: str
    depends_on: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    ac_ids: list[str] = Field(default_factory=list)
    plan: str = Field(
        default="",
        description="Task brief in superpowers writing-plans format (markdown): files, interfaces and "
        "bite-sized RED/GREEN steps with exact commands and expected output",
    )


class TaskGraph(_Model):
    tasks: list[Task] = Field(min_length=1)

    @model_validator(mode="after")
    def _is_dag(self) -> TaskGraph:
        ids = [t.id for t in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("task ids must be unique")
        known = set(ids)
        for t in self.tasks:
            missing = set(t.depends_on) - known
            if missing:
                raise ValueError(f"task {t.id} depends on unknown tasks {sorted(missing)}")
        self.topological_layers()  # raises on cycles
        return self

    def topological_layers(self) -> list[list[str]]:
        """Kahn layering: each layer can run in parallel."""
        deps = {t.id: set(t.depends_on) for t in self.tasks}
        done: set[str] = set()
        layers: list[list[str]] = []
        while len(done) < len(deps):
            layer = sorted(tid for tid, d in deps.items() if tid not in done and d <= done)
            if not layer:
                raise ValueError("task graph has a cycle")
            layers.append(layer)
            done.update(layer)
        return layers

    def ready(self, completed: set[str]) -> list[Task]:
        return [t for t in self.tasks if t.id not in completed and set(t.depends_on) <= completed]

    def get(self, task_id: str) -> Task:
        return next(t for t in self.tasks if t.id == task_id)


# --------------------------------------------------------------------------------------------------
# Verification reports
# --------------------------------------------------------------------------------------------------

GateStatus = Literal["pass", "fail", "skip", "error"]


class GateResult(_Model):
    name: str
    status: GateStatus
    required: bool = True
    summary: str = ""
    details: str = ""
    duration_s: float = 0.0
    failures: list[str] = Field(default_factory=list, description="stable ids of failing items")

    @property
    def blocking(self) -> bool:
        return self.required and self.status in ("fail", "error")


class AuditFinding(_Model):
    severity: Literal["blocking", "warning"]
    category: Literal[
        "weak_assertion", "mock_heavy", "ac_gap", "surviving_mutant", "spec_contradiction", "code_quality", "other"
    ]
    detail: str
    file: str | None = None
    suggested_fix: str = ""


class VerificationReport(_Model):
    feature_id: str
    iteration: int
    commit: str
    gates: list[GateResult]
    audit: list[AuditFinding] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not any(g.blocking for g in self.gates) and not any(f.severity == "blocking" for f in self.audit)

    def failing_ids(self) -> list[str]:
        ids: list[str] = []
        for g in self.gates:
            if g.blocking:
                ids.extend(f"{g.name}:{f}" for f in (g.failures or ["*"]))
        ids.extend(f"audit:{f.category}:{f.file or ''}" for f in self.audit if f.severity == "blocking")
        return sorted(ids)

    def fingerprint(self) -> str:
        """Stable hash of *what* fails; identical fingerprints across iterations signal a stall."""
        return hashlib.sha256(json.dumps(self.failing_ids()).encode()).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        d = self.model_dump()
        d["passed"] = self.passed
        d["fingerprint"] = self.fingerprint()
        return d


class ModelChoice(_Model):
    """Model + reasoning effort for one agent role. ``None`` = no flag is passed, the CLI default applies.
    Values are passed through verbatim; a value the CLI rejects falls back to the CLI default (with a warning)."""

    model: str | None = None
    effort: str | None = None


class FactoryConfig(_Model):
    """Thresholds live in the factory, never in the target repo, so agents cannot lower them."""

    diff_coverage_threshold: int = 80
    mutation_threshold: int = 70
    max_mutants: int = 150
    stall_limit: int = 3
    max_iterations: int = 0  # 0 = unbounded ("always loop"); the stall breaker still asks a human
    review_rounds: int = 2  # per-task review → fix passes (superpowers task review); 0 disables the task review
    gate_timeout_s: int = 900
    default_engine: Engine = "copilot"
    run_setup: bool = True
    # engine -> agent role -> model/effort, e.g. {"claude": {"grill": {"model": "haiku", "effort": "low"}}}
    models: dict[Engine, dict[AgentRole, ModelChoice]] = Field(default_factory=dict)

    def choice_for(self, engine: str, role: str) -> ModelChoice:
        return self.models.get(engine, {}).get(role) or ModelChoice()  # type: ignore[call-overload]


class AuditResult(_Model):
    findings: list[AuditFinding] = Field(default_factory=list)


class StoryFinding(_Model):
    scenario: str = Field(description="name of the scenario case")
    problem: str = Field(description="what the story says that the data does not check, or the other way round")


class StoryCheck(_Model):
    """Story critic: does each scenario's plain-English story say exactly what its test data checks?"""

    findings: list[StoryFinding] = Field(default_factory=list)


class ReviewFinding(_Model):
    severity: Literal["critical", "important", "minor"]
    kind: Literal["spec_compliance", "code_quality", "testing"] = "code_quality"
    detail: str
    file: str | None = Field(default=None, description="path[:line]")
    suggested_fix: str = ""


class ReviewResult(_Model):
    """Per-task review (superpowers subagent-driven-development task reviewer: spec compliance + quality)."""

    spec_compliant: bool = True
    findings: list[ReviewFinding] = Field(default_factory=list)
    summary: str = ""

    @property
    def needs_fixes(self) -> bool:
        return any(f.severity in ("critical", "important") for f in self.findings)


class FinishResult(_Model):
    """Outcome of superpowers finishing-a-development-branch with the choice fixed to 'push and create a PR'."""

    tests_passed: bool
    test_command: str = ""
    test_summary: str = ""
    pr_title: str = ""
    pr_summary: str = Field(default="", description="Markdown PR description written for reviewers")
