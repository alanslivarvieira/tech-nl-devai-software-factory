"""Graph state. The verify subgraph has its OWN state type: nothing from build conversations reaches it."""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, TypedDict

Status = Literal[
    "grilling",
    "awaiting_approval",
    "freezing",
    "planning",
    "building",
    "verifying",
    "escalated",
    "pr_opened",
    "aborted",
    "failed",
]


def merge_results(left: list[dict[str, Any]] | None, right: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Reducer for parallel worker results; ``None`` resets (start of a new build round)."""
    if right is None:
        return []
    return (left or []) + right


class FactoryState(TypedDict, total=False):
    # intake
    feature_key: str
    request: str
    requester: str
    repo_url: str
    base_branch: str
    engine: str
    engine_locked: bool
    # recon
    workdir: str
    stack: str
    repo_map: str
    # grill
    transcript: Annotated[list[dict[str, str]], operator.add]
    pending: dict[str, Any] | None
    draft: dict[str, Any] | None
    critic_findings: list[str]
    critic_rounds: int
    approved_by: str | None
    # freeze
    feature_id: str
    spec_rel: str
    freeze_sha: str
    manifest_hash: str
    freeze_attempts: int
    # plan / build
    task_graph: dict[str, Any]
    completed: list[str]
    task_results: Annotated[list[dict[str, Any]], merge_results]
    iteration: int
    # verify loop
    last_report: dict[str, Any] | None
    fingerprints: list[str]
    escalation: dict[str, Any] | None
    # finish (superpowers finishing-a-development-branch)
    finish: dict[str, Any] | None
    # outcome
    status: Status
    pr_url: str | None
    error: str | None


class TaskState(TypedDict, total=False):
    """One task of the build (superpowers subagent-driven-development): implement → review → fix loop.
    Fresh agent sessions only see the task, its brief, the contract and the previous review findings."""

    task: dict[str, Any]
    worktree: str
    base_sha: str
    feature_key: str
    feature_id: str
    engine: str
    stack: str
    last_report: dict[str, Any] | None
    protected: list[str]
    review_round: int
    review: dict[str, Any] | None
    fix: bool
    ok: bool
    summary: str
    sha: str


class VerifyState(TypedDict, total=False):
    """Inputs are the minimum needed to verify a commit from scratch; no transcript, no build logs."""

    feature_key: str  # only used to attribute agent usage/events, never to read the build conversation
    feature_id: str
    source_repo: str
    commit: str
    freeze_sha: str
    manifest_hash: str
    spec_rel: str
    stack: str
    engine: str
    iteration: int
    verify_dir: str
    report: dict[str, Any]
