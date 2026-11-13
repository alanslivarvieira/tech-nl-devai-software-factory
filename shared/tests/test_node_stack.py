"""Node stack: the Node driver template, stateful YAML scenarios and the factory-owned gates on node-todo."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from factory_core import pipeline
from factory_core.gates import GateContext, run_gates
from factory_core.models import FactoryConfig
from factory_core.spec import files_to_draft
from factory_core.stack import detect_stack, load_profile
from factory_core.tad_runner.runner import freeze_check, run
from factory_core.testing import REPO_ROOT

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

TARGET = REPO_ROOT / "examples" / "targets" / "node-todo"
SPEC = REPO_ROOT / "examples" / "specs" / "toggle-todos"

TOGGLE_IMPL = """
export function toggle(title) {
  const todo = todos.find((t) => t.title === title);
  if (!todo) throw new Error(`No todo titled "${title}"`);
  todo.done = !todo.done;
  return { ...todo };
}

export function remaining() {
  return todos.filter((t) => !t.done).length;
}
"""


@pytest.fixture
def node_repo(tmp_path: Path):
    repo = tmp_path / "node-todo"
    shutil.copytree(TARGET, repo)
    pipeline.git(repo, "init", "-q", "-b", "main")
    pipeline.commit_all(repo, "initial")
    files = {
        p.relative_to(SPEC).as_posix(): p.read_text(encoding="utf-8")
        for p in SPEC.rglob("*")
        if p.is_file() and p.relative_to(SPEC).parts[0] not in ("driver", "features")
    }
    assert detect_stack(repo) == "node"
    layout = pipeline.write_contract(repo, files_to_draft(files), load_profile("node"))
    (layout.driver / "driver.mjs").write_text((SPEC / "driver" / "driver.mjs").read_text(encoding="utf-8"))
    return repo, layout


def test_node_contract_freezes_then_passes_after_implementation(node_repo, tmp_path):
    repo, layout = node_repo
    assert freeze_check(layout.root, repo) == [], "existing add cases pass, new toggle scenarios fail"
    fr = pipeline.freeze(repo, layout, "tester")
    assert fr.ok

    before = run(layout.root, repo, ["scenario"])
    assert {r.status for r in before.results} == {"error", "fail"} or not before.passed

    todo = repo / "src" / "todo.js"
    todo.write_text(todo.read_text(encoding="utf-8") + TOGGLE_IMPL, encoding="utf-8")
    pipeline.commit_all(repo, "toggle")
    report = run(layout.root, repo)
    assert report.passed, report.as_dict()
    assert {r.kind for r in report.results} == {"table", "scenario", "gherkin"}

    # The factory-owned gates (contract, Gherkin, integrity) work for Node without any npm tooling.
    ctx = GateContext(
        repo=repo,
        spec_dir=layout.root,
        base_sha=fr.freeze_sha,
        profile=load_profile("node"),
        config=FactoryConfig(run_setup=False),
        expected_hash=fr.root_hash,
        out_dir=tmp_path / "out",
    )
    gates = {g.name: g for g in run_gates(ctx, only=["integrity", "contract", "acceptance"])}
    assert all(g.status == "pass" for g in gates.values()), {n: g.details for n, g in gates.items()}


def test_node_scenarios_keep_state_within_a_case_and_reset_between_cases(node_repo):
    repo, layout = node_repo
    todo = repo / "src" / "todo.js"
    todo.write_text(todo.read_text(encoding="utf-8") + TOGGLE_IMPL, encoding="utf-8")
    report = run(layout.root, repo, ["scenario"])
    by_name = {r.name: r for r in report.results}
    # "remaining counts only open todos" adds a, b, c — if state leaked from earlier cases the count would differ.
    assert by_name["remaining counts only open todos"].status == "pass", by_name
    assert by_name["toggling marks a todo as done"].status == "pass", "state is kept across given/when/then"
