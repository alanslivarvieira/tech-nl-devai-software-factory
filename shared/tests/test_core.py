"""Models, stack profiles, spec drafts, agent adapters and pymut."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from factory_core.agents import (
    AgentRequest,
    AgentResult,
    ClaudeCodeRunner,
    CodexRunner,
    CopilotCliRunner,
    FakeAgentRunner,
)
from factory_core.agents.base import extract_json, read_only
from factory_core.models import GateResult, GrillTurn, TaskGraph, VerificationReport
from factory_core.mutation.pymut import enumerate_mutants, mutated_source
from factory_core.skills import deploy_skills
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


def test_grill_questions_are_structured_rounds_with_recommendations():
    turn = GrillTurn(
        status="questions",
        questions=[
            "Trim the name?",
            {
                "title": "Blank names",
                "body": "Reject or fall back?",
                "options": [{"key": "A", "label": "reject"}, {"key": "B", "label": "fall back"}],
                "recommendation": "A, with `name must not be blank`",
            },
        ],
    )
    assert turn.questions[0].body == "Trim the name?" and turn.questions[0].render(1) == "1. **Q1** - Trim the name?"
    assert turn.questions[1].render(2).splitlines() == [
        "2. **Q2** - **Blank names**: Reject or fall back?",
        "   A) reject",
        "   B) fall back",
        "   ➡️ Recommended: A, with `name must not be blank`",
    ]


# ----------------------------------------------------------------------------------------------- skills
def _git(repo: Path, *args: str) -> str:
    ident = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
    return subprocess.run(["git", *ident, *args], cwd=repo, check=True, capture_output=True, text=True).stdout


def test_deploy_skills_is_excluded_idempotent_and_keeps_repo_owned_skills(tmp_path, monkeypatch):
    src = tmp_path / "factory-skills"
    for root in (".agents/skills", ".claude/skills"):
        for name in ("grilling", "test-driven-development"):
            (src / root / name).mkdir(parents=True)
            (src / root / name / "SKILL.md").write_text(f"---\nname: {name}\n---\nv1\n", encoding="utf-8")
    repo = tmp_path / "repo"
    (repo / ".claude/skills/grilling").mkdir(parents=True)
    (repo / ".claude/skills/grilling/SKILL.md").write_text("the repo's own skill", encoding="utf-8")
    _git(tmp_path, "init", "-q", "-b", "main", str(repo))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")

    deployed = deploy_skills(repo, src)
    assert ".claude/skills/grilling" not in deployed
    assert (repo / ".claude/skills/grilling/SKILL.md").read_text(encoding="utf-8") == "the repo's own skill"
    assert set(deployed) == {
        ".agents/skills/grilling",
        ".agents/skills/test-driven-development",
        ".claude/skills/test-driven-development",
    }
    assert _git(repo, "status", "--porcelain") == "", "deployed skills are never committed"
    assert deploy_skills(repo, src) == deployed

    (src / ".agents/skills/grilling/SKILL.md").write_text("v2", encoding="utf-8")
    deploy_skills(repo, src)
    assert (repo / ".agents/skills/grilling/SKILL.md").read_text(encoding="utf-8") == "v2"

    _git(repo, "worktree", "add", "-q", "-b", "factory-task/x", str(tmp_path / "wt"), "HEAD")
    deploy_skills(tmp_path / "wt", src)
    assert _git(tmp_path / "wt", "status", "--porcelain") == ""

    monkeypatch.setenv("FACTORY_SKILLS_DIR", str(src))
    agent = FakeAgentRunner({"builder": lambda req: "done"})
    agent.run(AgentRequest(role="builder", prompt="p", workdir=repo, protected=[".factory/specs/**"]))
    assert agent.calls[0].protected == [".factory/specs/**", *(f"{d}/**" for d in deployed)]


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


def test_cli_argv_passes_model_and_effort_per_engine(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FACTORY_COPILOT_ARGS", "--model from-env")
    req = AgentRequest(role="grill", prompt="p", workdir=tmp_path, model="m1", effort="low")
    pf = tmp_path / "p.md"
    pf.write_text("PROMPT")
    last = tmp_path / "x.last.txt"
    claude, _ = ClaudeCodeRunner().argv(req, pf, last)
    assert claude[-4:] == ["--model", "m1", "--effort", "low"]
    codex, _ = CodexRunner().argv(req, pf, last)
    assert "--json" in codex and codex[-5:] == ["--model", "m1", "-c", 'model_reasoning_effort="low"', "-"]
    copilot, _ = CopilotCliRunner().argv(req, pf, last)
    assert copilot[-4:] == ["--model", "m1", "--reasoning-effort", "low"]  # role config wins over env args
    assert copilot[copilot.index("--usage-output-file") + 1] == str(tmp_path / "x.usage.json")
    plain, _ = ClaudeCodeRunner().argv(AgentRequest(role="grill", prompt="p", workdir=tmp_path), pf, last)
    assert "--model" not in plain and "--effort" not in plain


def test_cli_usage_parsers(tmp_path: Path):
    last = tmp_path / "x.last.txt"
    claude = ClaudeCodeRunner().usage(
        json.dumps(
            {
                "result": "ok",
                "total_cost_usd": 0.02,
                "modelUsage": {
                    "claude-sonnet-5-5": {
                        "inputTokens": 100,
                        "outputTokens": 20,
                        "cacheReadInputTokens": 5,
                        "cacheCreationInputTokens": 7,
                        "costUSD": 0.015,
                    },
                    "claude-haiku-5-5": {"inputTokens": 10, "outputTokens": 2, "costUSD": 0.005},
                },
            }
        ),
        last,
    )
    assert [(u.model, u.input_tokens, u.cache_write_tokens, u.cost_usd) for u in claude] == [
        ("claude-sonnet-5-5", 100, 7, 0.015),
        ("claude-haiku-5-5", 10, 0, 0.005),
    ]
    codex_out = "\n".join(
        [
            '{"type":"thread.started","thread_id":"t"}',
            '{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":40,"output_tokens":10,'
            '"reasoning_output_tokens":3}}',
            '{"type":"item.completed","item":{"id":"i","type":"agent_message","text":"final answer"}}',
            '{"type":"turn.completed","usage":{"input_tokens":50,"cached_input_tokens":0,"output_tokens":5}}',
        ]
    )
    [codex] = CodexRunner().usage(codex_out, last)
    assert (codex.model, codex.input_tokens, codex.cache_read_tokens, codex.output_tokens, codex.reasoning_tokens) == (
        None,
        150,
        40,
        15,
        3,
    )
    assert CodexRunner().parse(codex_out, last) == "final answer"  # no --output-last-message file
    assert CopilotCliRunner().usage("", last) == []  # no usage file written (e.g. CLI crashed)
    (tmp_path / "x.usage.json").write_text(
        json.dumps(
            {
                "totalPremiumRequestCost": 0.33,
                "modelMetrics": {
                    "claude-haiku-4.5": {
                        "requests": {"count": 1, "cost": 0.33},
                        "usage": {
                            "inputTokens": 19392,
                            "outputTokens": 42,
                            "cacheReadTokens": 0,
                            "cacheWriteTokens": 19382,
                            "reasoningTokens": 35,
                        },
                        "totalNanoAiu": 2444750000,
                    }
                },
            }
        )
    )
    [cop] = CopilotCliRunner().usage("", last)
    assert (cop.model, cop.input_tokens, cop.reasoning_tokens, cop.premium_requests, cop.ai_credits) == (
        "claude-haiku-4.5",
        19392,
        35,
        0.33,
        2.44475,
    )


def test_rejected_model_falls_back_to_cli_default_and_is_recorded(tmp_path: Path):
    class PickyRunner(FakeAgentRunner):
        def _invoke(self, req, prompt):
            if req.model:
                return AgentResult(ok=False, engine="fake", error=f'Error: Model "{req.model}" is not available.')
            return super()._invoke(req, prompt)

    runner = PickyRunner({"planner": lambda r: {"tasks": [{"id": "a", "title": "t", "description": "d"}]}})
    req = AgentRequest(role="planner", prompt="plan", workdir=tmp_path, schema=TaskGraph, model="nope", effort="max")
    res = runner.run(req)
    assert res.ok and isinstance(res.data, TaskGraph)
    assert [(s.model, s.ok, s.fallback) for s in res.sessions] == [("nope", False, False), (None, True, True)]
    # an unrelated failure is not retried
    broken = FakeAgentRunner({})
    res = broken.run(AgentRequest(role="builder", prompt="p", workdir=tmp_path, model="m"))
    assert not res.ok and len(res.sessions) == 1


def test_usage_report_totals_per_role_and_model(tmp_path: Path):
    from factory_core.agents import AgentSession, Usage
    from factory_core.reports.usage import UsageLog, build_report, write_report

    log = UsageLog()
    log.record(
        "f",
        [
            AgentSession(
                "grill", "claude", "haiku", "low", True, 1.0, [Usage("claude-haiku-5-5", 100, 10, cost_usd=0.01)]
            ),
            AgentSession(
                "builder",
                "claude",
                None,
                None,
                True,
                2.0,
                [Usage("claude-sonnet-5-5", 1000, 100, cost_usd=0.2), Usage("claude-haiku-5-5", 50, 5)],
            ),
        ],
    )
    log.record("f", [AgentSession("builder", "codex", "gpt-x", "high", False, 3.0, [Usage(None, 7, 1)])], task="t1")
    report = build_report(log.rows("f"), {"feature_key": "f"})
    assert report["totals"]["sessions"] == 3 and report["totals"]["input_tokens"] == 1157
    assert report["totals"]["cost_usd"] == 0.21
    assert report["by_role"]["builder"]["sessions"] == 2 and report["by_role"]["builder"]["output_tokens"] == 106
    assert report["by_model"]["claude-haiku-5-5"]["input_tokens"] == 150
    assert report["by_model"]["gpt-x (configured)"]["input_tokens"] == 7
    assert report["sessions"][2]["task"] == "t1" and report["sessions"][2]["cost_usd"] is None
    json_path, md_path = write_report(tmp_path / "out" / "usage.json", report)
    assert json_path.name == "usage.json" and md_path.name == "usage.md"
    assert json.loads(json_path.read_text())["totals"]["sessions"] == 3
    md = md_path.read_text(encoding="utf-8")
    assert "## By model" in md and "| builder |" in md and "gpt-x (configured)" in md


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
