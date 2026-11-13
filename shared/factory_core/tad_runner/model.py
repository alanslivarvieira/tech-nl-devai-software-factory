from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

CaseKind = Literal["table", "scenario", "gherkin", "property"]
Baseline = Literal["new", "existing"]


class _Missing:
    def __repr__(self) -> str:
        return "MISSING"


MISSING: Any = _Missing()


@dataclass
class Expect:
    """What a step must produce. Exactly one family is used per step."""

    output: Any = MISSING  # exact deep equality
    output_subset: dict[str, Any] | None = None  # listed (dotted) fields must equal
    error: str | None = None  # exact error message
    error_contains: str | None = None
    no_error: bool = False  # setup steps: only require success

    def describe(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.output is not MISSING:
            d["output"] = self.output
        if self.output_subset is not None:
            d["output_subset"] = self.output_subset
        if self.error is not None:
            d["error"] = self.error
        if self.error_contains is not None:
            d["error_contains"] = self.error_contains
        if self.no_error and not d:
            d["no_error"] = True
        return d


@dataclass
class Step:
    op: str
    input: dict[str, Any] = field(default_factory=dict)
    expect: Expect | None = None
    phase: Literal["given", "when", "then"] = "when"


@dataclass
class Case:
    kind: CaseKind
    suite: str
    name: str
    ac_id: str
    steps: list[Step]
    baseline: Baseline = "new"
    source: str = ""

    @property
    def id(self) -> str:
        return f"{self.kind}:{self.suite}:{self.name}"


@dataclass
class CaseResult:
    case_id: str
    kind: CaseKind
    suite: str
    name: str
    ac_id: str
    baseline: Baseline
    status: Literal["pass", "fail", "error"]
    message: str = ""
    diff: str = ""
    duration_s: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()
