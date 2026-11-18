"""gh-aw factory: orchestrator state machine, issue helpers, workflow generation and prompt rendering."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import context  # noqa: E402
import gen_workflows  # noqa: E402
from ghaw_lib import Comment, Issue, request_body  # noqa: E402
from orchestrator import Event, decide, event_from_github  # noqa: E402

from factory_core.spec import render_markdown  # noqa: E402
from factory_core.testing import greeter_spec_files  # noqa: E402

BOT = "github-actions[bot]"


def issue(phase="grill", engine="copilot", it=0, comments=(), body=None) -> Issue:
    return Issue(
        number=7,
        body=body or request_body("acme/shop", "main", "add greeting", "alan"),
        labels=["factory", f"phase:{phase}", f"engine:{engine}", f"iter:{it}"],
        comments=list(comments),
    )


def human(text) -> Event:
    return Event(kind="comment", text=text, author="alan", association="MEMBER")


def run(workflow, ok=True, step="", run_id="99") -> Event:
    return Event(kind="run", workflow=workflow, step=step, conclusion="success" if ok else "failure", run_id=run_id)


def draft_comment() -> Comment:
    return Comment(author=BOT, association="NONE", body=render_markdown(greeter_spec_files()), is_bot=True)


def verify_comment(fp: str, passed=False) -> Comment:
    return Comment(
        author=BOT,
        association="NONE",
        is_bot=True,
        body=f"<!-- factory:verify iteration=1 passed={str(passed).lower()} fingerprint={fp} -->\nreport",
    )


def kinds(plan):
    return [(a["do"], a.get("workflow") or a.get("values") or "") for a in plan.actions]


# ----------------------------------------------------------------------------------------- grilling
def test_human_answer_triggers_next_grill_turn_with_chosen_engine():
    plan = decide(human("trim names please"), issue(engine="claude"))
    assert kinds(plan) == [("dispatch", "factory-grill-claude.lock.yml")]


def test_engine_command_relabels_and_continues():
    plan = decide(human("/engine codex\nalso reject blanks"), issue())
    assert plan.actions[0] == {"do": "label", "values": {"engine": "codex"}}
    assert plan.actions[-1]["workflow"] == "factory-grill-codex.lock.yml"
    assert kinds(decide(human("/engine gpt"), issue()))[0][0] == "comment"


def test_only_collaborators_steer_and_bots_are_ignored():
    assert decide(Event(kind="comment", text="/approve", author="rando", association="NONE"), issue()).actions == []
    assert (
        decide(
            Event(kind="comment", text="/approve", author=BOT, association="MEMBER", is_bot=True),
            issue(comments=[draft_comment()]),
        ).actions
        == []
    )


def test_approve_requires_a_factory_draft():
    assert kinds(decide(human("/approve"), issue()))[0][0] == "comment"
    forged = Comment(author="alan", association="MEMBER", body=render_markdown(greeter_spec_files()))
    assert kinds(decide(human("/approve"), issue(comments=[forged])))[0][0] == "comment", "human drafts don't count"
    plan = decide(human("/approve"), issue(comments=[draft_comment()]))
    assert plan.actions[0] == {"do": "label", "values": {"phase": "contract"}}
    assert plan.actions[-1] == {
        "do": "dispatch",
        "workflow": "factory-step.yml",
        "inputs": {"step": "prepare-contract", "issue": "7", "approver": "alan"},
    }


# ---------------------------------------------------------------------------------- pipeline chain
@pytest.mark.parametrize(
    "phase,event,expected",
    [
        ("contract", run("factory-step", step="prepare-contract"), ("dispatch", "factory-driver-copilot.lock.yml")),
        ("driver", run("factory-driver"), ("dispatch", "factory-step.yml")),
        ("driver", run("factory-step", step="apply-patch"), ("dispatch", "factory-step.yml")),
        ("freeze", run("factory-step", step="freeze"), ("dispatch", "factory-plan-copilot.lock.yml")),
        ("freeze", run("factory-step", ok=False, step="freeze"), ("dispatch", "factory-grill-copilot.lock.yml")),
        ("plan", run("factory-plan"), ("dispatch", "factory-build-copilot.lock.yml")),
        ("build", run("factory-build"), ("dispatch", "factory-step.yml")),
        ("build", run("factory-step", step="apply-patch"), ("dispatch", "factory-step.yml")),
        ("verify", run("factory-step", step="verify"), ("dispatch", "factory-audit-copilot.lock.yml")),
        ("verify", run("factory-step", ok=False, step="verify"), ("dispatch", "factory-build-copilot.lock.yml")),
        ("audit", run("factory-audit"), ("dispatch", "factory-step.yml")),
    ],
)
def test_chain(phase, event, expected):
    assert kinds(decide(event, issue(phase=phase, it=1)))[-1] == expected


def test_apply_patch_inputs_carry_the_agent_run():
    plan = decide(run("factory-build", run_id="4242"), issue(phase="build", it=1))
    assert plan.actions[-1]["inputs"] == {"step": "apply-patch", "issue": "7", "run_id": "4242", "mode": "build"}


def test_failed_verification_loops_with_incremented_iteration():
    plan = decide(
        run("factory-step", ok=False, step="verify"), issue(phase="verify", it=2, comments=[verify_comment("a")])
    )
    assert plan.actions[0] == {"do": "label", "values": {"phase": "build", "iter": "3"}}
    assert plan.actions[1]["inputs"]["fix"] == "true"


def test_stall_breaker_escalates():
    comments = [verify_comment("same"), verify_comment("other"), verify_comment("same"), verify_comment("same")]
    plan = decide(run("factory-step", ok=False, step="verify"), issue(phase="verify", it=4, comments=comments))
    assert plan.actions[0] == {"do": "label", "values": {"phase": "escalated"}}
    assert "Escalation" in plan.actions[1]["body"]
    plan = decide(
        run("factory-step", ok=False, step="verify"),
        issue(phase="verify", it=2, comments=[verify_comment("x")]),
        max_iterations=2,
    )
    assert plan.actions[0]["values"] == {"phase": "escalated"}


def test_audit_findings_route():
    def audit(findings):
        return Comment(
            author=BOT,
            association="NONE",
            is_bot=True,
            body="<!-- factory:audit -->\n```json\n" + json.dumps({"findings": findings}) + "\n```",
        )

    blocking = [{"severity": "blocking", "category": "weak_assertion", "detail": "x"}]
    contradiction = [{"severity": "blocking", "category": "spec_contradiction", "detail": "x"}]
    warn = [{"severity": "warning", "category": "other", "detail": "x"}]
    assert (
        kinds(decide(run("factory-audit"), issue("audit", it=1, comments=[audit(blocking)])))[-1][1]
        == "factory-build-copilot.lock.yml"
    )
    assert kinds(decide(run("factory-audit"), issue("audit", it=1, comments=[audit(contradiction)])))[0] == (
        "label",
        {"phase": "escalated"},
    )
    assert kinds(decide(run("factory-audit"), issue("audit", it=1, comments=[audit(warn)])))[-1] == (
        "dispatch",
        "factory-step.yml",
    )


@pytest.mark.parametrize(
    "text,expected",
    [
        ("/continue", ("dispatch", "factory-build-copilot.lock.yml")),
        ("/amend reject names longer than 50 chars", ("dispatch", "factory-grill-copilot.lock.yml")),
        ("/abort", ("close", "")),
    ],
)
def test_escalation_commands(text, expected):
    assert kinds(decide(human(text), issue(phase="escalated", it=3)))[-1] == expected


def test_finished_issues_are_inert():
    assert decide(human("/approve"), issue(phase="done")).actions == []
    assert decide(run("factory-step", step="open-pr"), issue(phase="pr")).actions == [
        {"do": "label", "values": {"phase": "done"}}
    ]


def test_event_parsing_from_github_payloads():
    ev, n = event_from_github(
        {
            "workflow_run": {
                "name": "factory-build-claude",
                "display_title": "factory build #12",
                "conclusion": "success",
                "id": 5,
            }
        },
        "workflow_run",
    )
    assert (ev.workflow, ev.step, n, ev.run_id) == ("factory-build", "", 12, "5")
    ev, n = event_from_github(
        {
            "workflow_run": {
                "name": "factory-step",
                "display_title": "factory step verify #3",
                "conclusion": "failure",
                "id": 6,
            }
        },
        "workflow_run",
    )
    assert (ev.workflow, ev.step, n, ev.conclusion) == ("factory-step", "verify", 3, "failure")
    ev, n = event_from_github(
        {
            "comment": {"body": "hi", "user": {"login": "x", "type": "User"}, "author_association": "OWNER"},
            "issue": {"number": 9},
        },
        "issue_comment",
    )
    assert ev.kind == "comment" and n == 9 and not ev.is_bot
    assert (
        event_from_github({"workflow_run": {"name": "ci", "display_title": "Build main", "id": 1}}, "workflow_run")
        is None
    )


# ------------------------------------------------------------------------------------------ helpers
def test_issue_body_anchors_and_transcript():
    i = issue(comments=[Comment("alan", "MEMBER", "trim please"), draft_comment()])
    assert i.request == {"repo": "acme/shop", "base": "main", "request": "add greeting", "requester": "alan"}
    i.body = i.with_anchors(manifest_hash="abc", freeze_sha="123")
    i.body = i.with_anchors(stack="python")
    assert i.anchors == {"manifest_hash": "abc", "freeze_sha": "123", "stack": "python"}
    assert [t["role"] for t in i.transcript()] == ["human", "human", "factory"]
    assert i.transcript()[-1]["text"] == "[draft contract presented]"


def test_generated_workflows_are_consistent():
    names = gen_workflows.workflow_names()
    assert len(names) == 1 + 5 * 3
    for phase, cfg in gen_workflows.PHASES.items():
        text = gen_workflows.render(phase, "codex")
        front = yaml.safe_load(text.split("---")[1])
        assert front["engine"] == "codex" and front["name"] == f"factory-{phase}-codex"
        assert ("edit" in front["tools"]) is cfg["writes"]
        assert ("post-steps" in front) is cfg["writes"], "writing phases export a patch for the guard"
        assert front["permissions"]["contents"] == "read", "agents never hold write tokens"
        assert "safe-outputs" in front if cfg["comment"] else True
        assert "persist-credentials: false" in text


# -------------------------------------------------------------------------------------- prompts
def test_context_renders_grill_and_build_prompts(tmp_path):
    from factory_core import pipeline
    from factory_core.stack import load_profile
    from factory_core.testing import greeter_draft, greeter_driver, make_greeter_repo

    target = make_greeter_repo(tmp_path / "target")
    pipeline.git(target, "checkout", "-q", "-b", "factory/test")
    grill = context.render(
        "grill", issue(comments=[Comment("alan", "MEMBER", "trim please")]), target, "acme/hub", False
    )
    assert "Grill Master" in grill and "trim please" in grill and "<!-- factory:draft -->" in grill

    layout = pipeline.write_contract(target, greeter_draft(), load_profile("python"))
    (layout.driver / "driver.py").write_text(greeter_driver(), encoding="utf-8")
    res = pipeline.freeze(target, layout, "alan")
    assert "approved_by: alan" in layout.intent_markdown.read_text(encoding="utf-8")
    plan = Comment(
        BOT,
        "NONE",
        '<!-- factory:plan -->\n```json\n{"tasks": [{"id": "a", "title": "Impl", '
        '"description": "do it"}, {"id": "b", "title": "Tests", "description": "test it", '
        '"depends_on": ["a"]}]}\n```',
        is_bot=True,
    )
    i = issue(phase="build", it=1, comments=[plan])
    i.body = i.with_anchors(
        spec_rel=pipeline.spec_rel("personalised-greeting"),
        feature_id="personalised-greeting",
        freeze_sha=res.freeze_sha,
        manifest_hash=res.root_hash,
    )
    build = context.render("build", i, target, "acme/hub", False)
    assert "1. **Impl**" in build and "2. **Tests**" in build and ".factory/specs/" in build
    assert "Previous verification failed" not in build
