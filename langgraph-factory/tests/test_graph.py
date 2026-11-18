"""LangGraph factory: full graph runs with a scripted agent (no CLI, no network)."""

from __future__ import annotations

import shutil
import subprocess
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


def drive(rt: FactoryRuntime, answers: list[str | dict], repo_url: str = str(GREETER_TARGET)) -> str:
    key = rt.start(request="Add a personalised greeting", repo_url=repo_url, wait=True)
    for a in answers:
        st = rt.status(key)
        assert st["waiting_for"], f"factory not waiting (status={st['status']}, error={st['error']})"
        rt.reply(key, a if isinstance(a, dict) else {"text": a, "user": "alan"}, wait=True)
    return key


def test_cli_shows_rotating_work_verbs(monkeypatch):
    from io import StringIO

    from factory_app import cli

    class InteractiveOutput(StringIO):
        def isatty(self):
            return True

    class RuntimeStub:
        waited = False

        def busy(self, key):
            return clock.elapsed < 5 * len(cli.WORK_VERBS)

        def wait(self, key):
            self.waited = True

    output = InteractiveOutput()
    rt = RuntimeStub()
    clock = type("Clock", (), {"elapsed": 0.0})()
    monkeypatch.setattr(cli.sys, "stdout", output)
    monkeypatch.setattr(cli.time, "monotonic", lambda: clock.elapsed)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: setattr(clock, "elapsed", clock.elapsed + seconds))
    cli.wait_for_run(rt, "feature", lambda _: None)
    assert len(cli.WORK_VERBS) == 187
    assert all(verb in output.getvalue() for verb in cli.WORK_VERBS)
    assert all(f"{cli.WORK_VERBS[0]}{dots}" in output.getvalue() for dots in (".", "..", "..."))
    assert clock.elapsed == 5 * len(cli.WORK_VERBS)
    assert rt.waited


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
    key = drive(rt, ["1A, 2B /engine claude", "/approve"])
    assert rt.status(key)["engine"] == "claude"
    grill = agent.calls_for("grill")
    assert len(grill) == 2 and "/engine claude" not in grill[0].prompt
    assert "/engine claude" in grill[1].prompt, "the transcript carries the human's answers"
    assert "1A, 2B" in grill[1].prompt


def test_intent_markdown_is_available_before_approval(tmp_path):
    rt = runtime(tmp_path, greeter_fake_agent(buggy_first=False))
    key = rt.start(request="Add a personalised greeting", repo_url=str(GREETER_TARGET), wait=True)
    rt.reply(key, {"text": "1A, 2B"}, wait=True)

    pending = rt.status(key)["waiting_for"]
    assert pending["type"] == "approval"
    intent_path = Path(pending["intent_path"])
    assert intent_path.is_file()
    assert "## Acceptance Criteria" in intent_path.read_text(encoding="utf-8")
    assert "Not yet approved" in pending["intent_markdown"]

    rt.reply(key, {"text": "abort"}, wait=True)


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
    assert st["waiting_for"]["questions"][0].startswith("1. ")
    assert client.post(f"/features/{key}/messages", json={"text": "fine"}).status_code == 202
    st = wait_for(lambda s: (s.get("waiting_for") or {}).get("type") == "approval")
    assert "examples/greet.csv" in st["waiting_for"]["contract_markdown"]
    client.post(f"/features/{key}/messages", json={"action": "approve", "user": "alan"})
    st = wait_for(lambda s: s.get("status") == "pr_opened" and not s["busy"])
    events = client.get(f"/features/{key}/events").json()
    assert events[-1]["kind"] == "pr_opened"
    assert client.post("/features/nope/messages", json={"text": "x"}).status_code == 404


def test_local_git_repo_gets_the_feature_branch_pushed_back(tmp_path):
    src = tmp_path / "greeter"
    shutil.copytree(GREETER_TARGET, src, ignore=shutil.ignore_patterns(".venv", "__pycache__"))
    ident = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], [*ident, "commit", "-q", "-m", "init"]):
        subprocess.run(["git", *args], cwd=src, check=True, capture_output=True)

    rt = runtime(tmp_path, greeter_fake_agent(buggy_first=False))
    key = drive(rt, ["ok", "/approve"], repo_url=str(src))
    st = rt.status(key)
    assert st["status"] == "pr_opened", st

    branch = f"factory/{key}"
    out = subprocess.run(["git", "branch", "--list", branch], cwd=src, check=True, capture_output=True, text=True)
    assert branch in out.stdout
    workdir = rt.services.workspace(key) / "repo"
    current = subprocess.run(
        ["git", "branch", "--show-current"], cwd=workdir, check=True, capture_output=True, text=True
    ).stdout.strip()
    assert current == branch
    changed = subprocess.run(
        ["git", "diff", "--name-only", f"main...{branch}"], cwd=src, check=True, capture_output=True, text=True
    ).stdout
    assert "src/greeter/__init__.py" in changed
    assert "Pushed to" in (rt.services.workspace(key) / "PR.md").read_text(encoding="utf-8")


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
