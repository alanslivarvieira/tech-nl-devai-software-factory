"""LangGraph factory: full graph runs with a scripted agent (no CLI, no network)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from factory_app.api.server import create_app
from factory_app.config import Settings
from factory_app.runtime import FactoryRuntime
from factory_app.sandbox import DockerExecutor
from factory_app.services import GitHost
from factory_core.agents import AgentRequest
from factory_core.models import FactoryConfig
from factory_core.testing import GREETER_TARGET, greeter_fake_agent

pytestmark = pytest.mark.e2e


def runtime(tmp_path: Path, agent, **cfg) -> FactoryRuntime:
    settings = Settings(
        workspaces=tmp_path / "ws", fake_agent=None, factory=FactoryConfig(run_setup=False, max_mutants=40, **cfg)
    )
    return FactoryRuntime(settings, agent=agent)


def drive(rt: FactoryRuntime, answers: list[str | dict]) -> str:
    key = rt.start(request="Add a personalised greeting", repo_url=str(GREETER_TARGET), wait=True)
    for a in answers:
        st = rt.status(key)
        assert st["waiting_for"], f"factory not waiting (status={st['status']}, error={st['error']})"
        rt.reply(key, a if isinstance(a, dict) else {"text": a, "user": "alan"}, wait=True)
    return key


def test_happy_path_loops_until_green_and_opens_pr(tmp_path):
    agent = greeter_fake_agent(buggy_first=True)
    rt = runtime(tmp_path, agent)
    key = drive(rt, ["trim yes, reject blanks /engine codex", "/approve"])
    st = rt.status(key)
    assert st["status"] == "pr_opened", st
    assert st["iteration"] == 2 and st["engine"] == "codex"
    assert st["pr_url"].startswith("file:") and Path(st["pr_url"].replace("file:///", "")).name == "PR.md"
    kinds = [e["kind"] for e in rt.services.events.since(key)]
    assert kinds.index("questions") < kinds.index("approval_needed") < kinds.index("frozen") < kinds.index("planned")
    assert kinds.count("verified") == 2 and "fixing" in kinds and kinds[-1] == "pr_opened"

    # The builder never got write access to the contract, and its fix round got the failure brief.
    builder_calls = agent.calls_for("builder")
    assert all(".factory/specs/**" in c.protected for c in builder_calls)
    assert "Previous verification failed" in builder_calls[-1].prompt
    assert "name is trimmed" in builder_calls[-1].prompt

    # Clean context: the auditor ran in a fresh clone and saw none of the build conversation.
    (audit,) = agent.calls_for("auditor")
    assert "verify" in Path(audit.workdir).parts
    assert audit.workdir not in {c.workdir for c in builder_calls}
    assert "Previous verification failed" not in audit.prompt and "Grill Master" not in audit.prompt


def test_grill_is_read_only_and_engine_choice_sticks(tmp_path):
    agent = greeter_fake_agent(buggy_first=False)
    rt = runtime(tmp_path, agent)
    key = drive(rt, ["/engine claude", "/approve"])
    assert rt.status(key)["engine"] == "claude"
    grill = agent.calls_for("grill")
    assert len(grill) == 2 and "/engine claude" not in grill[0].prompt
    assert "/engine claude" in grill[1].prompt, "the transcript carries the human's answers"


def test_cheating_builder_is_caught_and_escalated(tmp_path):
    def cheat(req: AgentRequest) -> str:
        csv = Path(req.workdir) / ".factory/specs/personalised-greeting/examples/greet.csv"
        csv.chmod(0o644)  # defeats the local read-only bit — the guard must still catch it
        csv.write_text("case,ac_id\n", encoding="utf-8")
        return "made the tests pass"

    agent = greeter_fake_agent(buggy_first=False)
    agent.script["builder"] = cheat
    rt = runtime(tmp_path, agent, stall_limit=2)
    key = drive(rt, ["ok", "/approve"])
    st = rt.status(key)
    assert st["status"] == "escalated" and st["waiting_for"]["type"] == "escalation"
    integrity = next(g for g in st["last_report"]["gates"] if g["name"] == "integrity")
    assert integrity["status"] == "fail" and "protected-path" in integrity["details"]
    rt.reply(key, {"text": "abort"}, wait=True)
    assert rt.status(key)["status"] == "aborted"


def test_stall_breaker_then_continue(tmp_path):
    agent = greeter_fake_agent(buggy_first=False)
    from factory_core.testing import GREETER_BUGGY

    def always_buggy(req: AgentRequest) -> str:
        (Path(req.workdir) / "src/greeter/__init__.py").write_text(GREETER_BUGGY, encoding="utf-8")
        return "done"

    agent.script["builder"] = [always_buggy, always_buggy, always_buggy]
    from factory_core.testing import GREETER_GOOD, GREETER_UNIT_TESTS

    def good(req: AgentRequest) -> str:
        (Path(req.workdir) / "src/greeter/__init__.py").write_text(GREETER_GOOD, encoding="utf-8")
        (Path(req.workdir) / "tests/test_personalised.py").write_text(GREETER_UNIT_TESTS, encoding="utf-8")
        return "done"

    agent.script["builder"].append(good)
    rt = runtime(tmp_path, agent, stall_limit=2)
    key = drive(rt, ["ok", "/approve"])
    st = rt.status(key)
    assert st["status"] == "escalated" and "repeated 2 times" in st["waiting_for"]["reason"]
    rt.reply(key, {"action": "continue"}, wait=True)
    assert rt.status(key)["status"] == "pr_opened"


def test_api_round_trip(tmp_path):
    rt = runtime(tmp_path, greeter_fake_agent(buggy_first=False))
    client = TestClient(create_app(rt))
    assert client.get("/health").json()["ok"]
    key = client.post("/features", json={"request": "personalised greeting", "repo": str(GREETER_TARGET)}).json()[
        "feature_key"
    ]

    def wait_for(predicate, timeout=300):
        end = time.time() + timeout
        while time.time() < end:
            st = client.get(f"/features/{key}").json()
            if predicate(st):
                return st
            time.sleep(0.2)
        raise AssertionError(f"timeout; last status {st}")

    st = wait_for(lambda s: s.get("waiting_for"))
    assert st["waiting_for"]["type"] == "questions"
    assert client.post(f"/features/{key}/messages", json={"text": "fine"}).status_code == 202
    st = wait_for(lambda s: (s.get("waiting_for") or {}).get("type") == "approval")
    assert "examples/greet.csv" in st["waiting_for"]["contract_markdown"]
    client.post(f"/features/{key}/messages", json={"action": "approve", "user": "alan"})
    st = wait_for(lambda s: s.get("status") == "pr_opened" and not s["busy"])
    events = client.get(f"/features/{key}/events").json()
    assert events[-1]["kind"] == "pr_opened"
    assert client.post("/features/nope/messages", json={"text": "x"}).status_code == 404


def test_docker_executor_mounts_contract_read_only(tmp_path):
    (tmp_path / ".factory/specs").mkdir(parents=True)
    cmd = DockerExecutor("img").command(["claude", "-p", str(tmp_path / "x.md")], tmp_path, {"FACTORY_ROLE": "builder"})
    joined = " ".join(cmd)
    assert f"{tmp_path.resolve()}:/work" in joined and ".factory/specs:ro" in joined
    assert cmd[-3:] == ["claude", "-p", "/work/x.md"]


@pytest.mark.parametrize(
    "url,slug",
    [
        ("https://github.com/acme/shop.git", "acme/shop"),
        ("git@github.com:acme/shop.git", "acme/shop"),
        ("https://github.com/acme/shop", "acme/shop"),
        ("/local/path", None),
    ],
)
def test_github_slug(url, slug):
    assert GitHost.github_slug(url) == slug
