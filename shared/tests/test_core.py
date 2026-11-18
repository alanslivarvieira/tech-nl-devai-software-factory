"""Models, stack profiles, spec drafts, agent adapters and pymut."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from factory_core.agents import AgentRequest, ClaudeCodeRunner, CodexRunner, CopilotCliRunner, FakeAgentRunner
from factory_core.agents.base import extract_json, read_only
from factory_core.models import GateResult, GrillTurn, TaskGraph, VerificationReport
from factory_core.mutation.pymut import enumerate_mutants, mutated_source
from factory_core.spec import DraftError, draft_to_files, files_to_draft, parse_markdown, render_markdown
from factory_core.stack import all_profiles, detect_stack, glob_match
from factory_core.testing import greeter_draft, greeter_spec_files


# ----------------------------------------------------------------------------------------------- models
def test_task_graph_layers_and_cycles():
    g = TaskGraph(
        tasks=[
            {"id": "a", "title": "", "description": ""},
            {"id": "b", "title": "", "description": "", "depends_on": ["a"]},
            {"id": "c", "title": "", "description": "", "depends_on": ["a"]},
            {"id": "d", "title": "", "description": "", "depends_on": ["b", "c"]},
        ]
    )
    assert g.topological_layers() == [["a"], ["b", "c"], ["d"]]
    assert [t.id for t in g.ready({"a"})] == ["b", "c"]
    with pytest.raises(ValueError, match="cycle"):
        TaskGraph(
            tasks=[
                {"id": "a", "title": "", "description": "", "depends_on": ["b"]},
                {"id": "b", "title": "", "description": "", "depends_on": ["a"]},
            ]
        )
    with pytest.raises(ValueError, match="unknown"):
        TaskGraph(tasks=[{"id": "a", "title": "", "description": "", "depends_on": ["zz"]}])


def test_grill_turn_consistency():
    with pytest.raises(ValueError):
        GrillTurn(status="questions")
    with pytest.raises(ValueError):
        GrillTurn(status="draft")
    assert GrillTurn(status="questions", questions=["?"]).status == "questions"


def test_report_fingerprint_is_stable_and_specific():
    def rep(failures, it):
        return VerificationReport(
            feature_id="f",
            iteration=it,
            commit="c",
            gates=[
                GateResult(name="contract", status="fail", failures=failures),
                GateResult(name="lint", status="fail", required=False),
            ],
        )

    assert rep(["a", "b"], 1).fingerprint() == rep(["a", "b"], 2).fingerprint()
    assert rep(["a"], 1).fingerprint() != rep(["a", "b"], 1).fingerprint()
    assert not rep(["a"], 1).passed
    assert VerificationReport(
        feature_id="f", iteration=0, commit="c", gates=[GateResult(name="lint", status="fail", required=False)]
    ).passed


# ------------------------------------------------------------------------------------------------ stack
@pytest.mark.parametrize(
    "path,pattern,ok",
    [
        ("tests/test_a.py", "tests/**", True),
        ("pkg/tests/x.py", "**/tests/**", True),
        ("test_a.py", "**/test_*.py", True),
        ("src/a/test_b.py", "**/test_*.py", True),
        ("src/a.py", "tests/**", False),
        (".factory/specs/x/examples/a.csv", ".factory/specs/**", True),
        ("CODEOWNERS", "CODEOWNERS", True),
    ],
)
def test_glob_match(path, pattern, ok):
    assert glob_match(path, pattern) is ok


def test_profiles_load_and_detect(tmp_path: Path):
    profiles = all_profiles()
    assert {"python", "node", "java", "dotnet", "go"} <= set(profiles)
    for p in profiles.values():
        assert p.gate_commands("unit"), f"{p.name} needs a unit gate"
    (tmp_path / "package.json").write_text("{}")
    assert detect_stack(tmp_path) == "node"
    (tmp_path / "pom.xml").write_text("<project/>")
    assert detect_stack(tmp_path) == "java", "more specific profile wins"
    py = profiles["python"]
    assert py.is_test("tests/test_x.py") and py.is_src("src/greeter/__init__.py")
    assert not py.is_src(".factory/specs/x/driver/driver.py")


# ------------------------------------------------------------------------------------------- spec drafts
def test_markdown_roundtrip():
    files = greeter_spec_files()
    md = render_markdown(files)
    assert md.startswith("<!-- factory:draft -->")
    assert parse_markdown(md) == {k: v if v.endswith("\n") else v + "\n" for k, v in files.items()}
    draft = files_to_draft(parse_markdown(md))
    assert draft.intent.feature_id == "personalised-greeting"
    rendered_files = draft_to_files(draft)
    assert set(rendered_files) == set(files) | {"intent.md"}
    assert "<summary>Complete machine-readable specification</summary>" in rendered_files["intent.md"]


def test_draft_rejects_path_traversal():
    with pytest.raises(DraftError):
        files_to_draft({"intent.yaml": "x", "../../evil.py": "x"})
    with pytest.raises(DraftError):
        files_to_draft({"intent.yaml": "x", "driver/driver.py": "x"})


def test_critic_flags_uncovered_acceptance_criteria():
    from factory_core.pipeline import critique

    draft = greeter_draft()
    assert critique(draft) == []
    from factory_core.models import AcceptanceCriterion

    draft.intent.acceptance_criteria.append(AcceptanceCriterion(id="AC-9", description="untested"))
    draft = files_to_draft(draft_to_files(draft))
    assert any("AC-9" in p for p in critique(draft))


# ------------------------------------------------------------------------------------------------ agents
def test_extract_json_prefers_last_fence_then_largest_object():
    assert extract_json('blah ```json\n{"a": 1}\n``` and ```json\n{"b": 2}\n```') == {"b": 2}
    assert extract_json('noise {"x": {"y": 1}} trailing {"z": 2}') == {"x": {"y": 1}}
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_fake_runner_validates_schema_and_retries(tmp_path: Path):
    answers = [lambda r: "not json", lambda r: {"tasks": [{"id": "a", "title": "t", "description": "d"}]}]
    fake = FakeAgentRunner({"planner": answers})
    res = fake.run(AgentRequest(role="planner", prompt="plan", workdir=tmp_path, schema=TaskGraph))
    assert res.ok and isinstance(res.data, TaskGraph)
    assert len(fake.calls_for("planner")) == 2 and "REJECTED" in fake.calls_for("planner")[1].prompt


def test_cli_argv_builders(tmp_path: Path):
    req = AgentRequest(role="builder", prompt="p", workdir=tmp_path, protected=[".factory/specs/**"])
    pf = tmp_path / "p.md"
    pf.write_text("PROMPT")
    argv, stdin = ClaudeCodeRunner().argv(req, pf, tmp_path / "last")
    assert argv[:2] == ["claude", "-p"] and stdin == "PROMPT"
    assert "Edit(.factory/specs/**)" in argv[argv.index("--disallowedTools") + 1]
    argv, stdin = CodexRunner().argv(
        AgentRequest(role="auditor", prompt="", workdir=tmp_path, read_only=True), pf, tmp_path / "last"
    )
    assert argv[:2] == ["codex", "exec"] and "read-only" in argv and argv[-1] == "-"
    argv, stdin = CopilotCliRunner().argv(req, pf, tmp_path / "last")
    assert argv[0] == "copilot" and "p.md" in argv[2] and stdin is None


def test_read_only_context_locks_and_restores(tmp_path: Path):
    f = tmp_path / ".factory" / "specs" / "x" / "a.csv"
    f.parent.mkdir(parents=True)
    f.write_text("x")
    other = tmp_path / "src.py"
    other.write_text("y")
    with read_only(tmp_path, [".factory/specs/**"]) as locked:
        assert locked == [f]
        assert not os.access(f, os.W_OK) or not (f.stat().st_mode & stat.S_IWUSR)
        assert os.access(other, os.W_OK)
    assert f.stat().st_mode & stat.S_IWUSR


def test_cli_runner_end_to_end_with_stub_executable(tmp_path: Path, monkeypatch):
    """A stub 'claude' binary proves the adapter's process plumbing and JSON result parsing."""
    stub = tmp_path / "claude_stub.py"
    stub.write_text(
        "import sys, json\nprompt = sys.stdin.read()\n"
        "print(json.dumps({'result': 'echo:' + str(len(prompt)) + ' ```json\\n{\"findings\": []}\\n```'}))\n"
    )
    monkeypatch.setenv("FACTORY_CLAUDE_BIN", sys.executable)
    monkeypatch.setenv("FACTORY_CLAUDE_ARGS", "")
    runner = ClaudeCodeRunner()
    runner.argv = lambda req, pf, lm: ([sys.executable, str(stub)], pf.read_text())  # type: ignore[method-assign]
    from factory_core.models import AuditResult

    res = runner.run(AgentRequest(role="auditor", prompt="audit", workdir=tmp_path, schema=AuditResult))
    assert res.ok and res.data.findings == []
    assert (tmp_path / ".factory" / "run").is_dir()


# ------------------------------------------------------------------------------------------------- pymut
SRC = '''def price(qty, unit, vip):
    """doc"""
    total = qty * unit
    if vip and qty > 10:
        total = total - 5
    return total
'''


def test_pymut_enumerates_and_applies(tmp_path: Path):
    f = tmp_path / "m.py"
    f.write_text(SRC)
    muts = enumerate_mutants(f, None)
    ops = {m.operator for m in muts}
    assert {"binop", "compare", "boolop", "negate-if", "return-none", "constant"} <= ops
    assert not any("doc" in m.original for m in muts), "docstrings are not mutated"
    m = next(m for m in muts if m.operator == "compare")
    assert "qty >= 10" in mutated_source(f, m, None)
    only_line_4 = enumerate_mutants(f, {4})
    assert only_line_4 and all(m.line == 4 for m in only_line_4)


@pytest.mark.parametrize(
    "assertions,expect_pass",
    [
        ("assert price(1, 2, False) is not None", False),  # weak test → survivors
        (
            "assert price(3, 2, False) == 6\n    assert price(11, 1, True) == 6\n    assert price(10, 1, True) == 10\n"
            "    assert price(11, 1, False) == 11\n    assert price(12, 1, True) == 7",
            True,
        ),
    ],
)
def test_pymut_score_reflects_test_strength(tmp_path: Path, assertions, expect_pass):
    (tmp_path / "m.py").write_text(SRC)
    (tmp_path / "test_m.py").write_text(f"from m import price\n\n\ndef test_price():\n    {assertions}\n")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "factory_core.mutation.pymut",
            "--threshold",
            "80",
            "--json",
            "out.json",
            "--files",
            "m.py",
            "--",
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-x",
            "-p",
            "no:cacheprovider",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    data = json.loads((tmp_path / "out.json").read_text())
    assert (proc.returncode == 0) is expect_pass, proc.stdout
    assert data["total"] > 5
    if not expect_pass:
        assert data["survivors"]


def test_pymut_restores_sources_even_after_a_hard_kill(tmp_path: Path):
    from factory_core.mutation.pymut import BACKUP_SUFFIX, mutated, recover

    f = tmp_path / "m.py"
    original = b"def f():\r\n    return 1  # keep me\r\n"
    f.write_bytes(original)
    with pytest.raises(KeyboardInterrupt), mutated(f, "def f():\n    return 2\n"):
        assert "return 2" in f.read_text()
        raise KeyboardInterrupt
    assert f.read_bytes() == original, "byte-exact restore (comments, CRLF) after an interrupt"
    # Simulate a hard kill: mutant on disk + backup left behind; the next run repairs it.
    (tmp_path / ("m.py" + BACKUP_SUFFIX)).write_bytes(original)
    f.write_text("def f():\n    return 2\n")
    assert recover(tmp_path) == [f] and f.read_bytes() == original
    assert not list(tmp_path.glob("*" + BACKUP_SUFFIX))
