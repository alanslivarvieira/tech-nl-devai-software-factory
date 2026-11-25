"""Reusable fixtures for tests, demos and CI: the python-greeter sample and a scripted fake agent.

``FACTORY_FAKE_AGENT=greeter`` makes the LangGraph app use :func:`greeter_fake_agent` instead of a real
CLI, so the whole pipeline (grill → freeze → plan → build ⇄ verify → PR) can be demoed offline.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from .agents import AgentRequest, FakeAgentRunner
from .models import SpecDraft
from .spec import files_to_draft

REPO_ROOT = Path(__file__).resolve().parents[2]
# Installed as a package (e.g. in the Docker image) the source-relative path doesn't exist; point here instead.
EXAMPLES_DIR = Path(os.environ.get("FACTORY_EXAMPLES_DIR") or REPO_ROOT / "examples")
GREETER_TARGET = EXAMPLES_DIR / "targets" / "python-greeter"
GREETER_SPEC = EXAMPLES_DIR / "specs" / "personalised-greeting"

GREETER_GOOD = '''"""Tiny sample service used to demo and end-to-end test the software factory."""


def greet(name: str | None = None) -> str:
    if name is None:
        return "Hello, world!"
    cleaned = name.strip()
    if not cleaned:
        raise ValueError("Name must not be entirely whitespace")
    return f"Hello, {cleaned}!"
'''

# First attempt of the scripted builder: forgets to trim and to reject blank names.
GREETER_BUGGY = '''"""Tiny sample service used to demo and end-to-end test the software factory."""


def greet(name: str | None = None) -> str:
    if name is None:
        return "Hello, world!"
    return f"Hello, {name}!"
'''

GREETER_UNIT_TESTS = """import pytest

from greeter import greet


def test_personalised():
    assert greet("Joe") == "Hello, Joe!"


def test_trims_whitespace():
    assert greet("  Ann  ") == "Hello, Ann!"


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_names_rejected(blank):
    with pytest.raises(ValueError, match="Name must not be entirely whitespace"):
        greet(blank)
"""


def greeter_spec_files() -> dict[str, str]:
    files = {}
    for p in GREETER_SPEC.rglob("*"):
        rel = p.relative_to(GREETER_SPEC)
        if p.is_file() and rel.parts[0] not in ("driver", "features"):
            files[rel.as_posix()] = p.read_text(encoding="utf-8")
    return files


def greeter_draft(repo_url: str | None = None) -> SpecDraft:
    draft = files_to_draft(greeter_spec_files())
    if repo_url:
        draft.intent.repo.url = repo_url
    return draft


def greeter_driver() -> str:
    return (GREETER_SPEC / "driver" / "driver.py").read_text(encoding="utf-8")


def make_greeter_repo(dest: Path) -> Path:
    from .pipeline import commit_all, git

    shutil.copytree(GREETER_TARGET, dest)
    git(dest, "init", "-q", "-b", "main")
    commit_all(dest, "initial greeter")
    return dest


def _is_task(req: AgentRequest, task_id: str) -> bool:
    return f"(`{task_id}`)" in req.prompt


def _write(req: AgentRequest, rel: str, text: str) -> None:
    path = req.workdir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def greeter_fake_agent(buggy_first: bool = True) -> FakeAgentRunner:
    """Scripted agent: asks one question, drafts the canned contract, writes the driver, plans two tasks,
    builds (buggy first, so the verify→fix loop is exercised), and audits with no findings."""

    def grill_questions(req: AgentRequest) -> dict[str, Any]:
        return {
            "status": "questions",
            "questions": [
                {
                    "title": "Trimming",
                    "body": "Should surrounding whitespace in the name be trimmed?",
                    "options": [{"key": "A", "label": "yes"}, {"key": "B", "label": "no"}],
                    "recommendation": "A — users rarely mean leading/trailing spaces",
                },
                {
                    "title": "Blank names",
                    "body": "What should happen for a blank name — reject with an error or fall back to 'world'?",
                    "recommendation": "reject with `name must not be blank`",
                },
                "Which coding agent should build it: copilot, claude or codex?",
            ],
        }

    def grill_draft(req: AgentRequest) -> dict[str, Any]:
        url = req.meta.get("repo_url")
        return {
            "status": "draft",
            "draft": greeter_draft(url).model_dump(),
            "notes": "Personalised greeting with trimming and blank-name rejection.",
            "engine_recommendation": "claude",
        }

    def driver(req: AgentRequest) -> str:
        _write(req, ".factory/specs/personalised-greeting/driver/driver.py", greeter_driver())
        return "DRIVER WRITTEN"

    def plan(req: AgentRequest) -> dict[str, Any]:
        return {
            "tasks": [
                {
                    "id": "impl",
                    "title": "Personalised greet()",
                    "description": "Add optional name with trimming and blank-name validation to greeter.greet",
                    "files": ["src/greeter/__init__.py"],
                    "ac_ids": ["AC-1", "AC-2", "AC-3"],
                    "plan": "- [ ] Step 1: write the failing test for `greet(' Ada ')`\n"
                    "- [ ] Step 2: run `pytest -q` — Expected: FAIL\n"
                    "- [ ] Step 3: implement, run again — Expected: PASS",
                },
                {
                    "id": "tests",
                    "title": "Unit tests",
                    "description": "Unit tests for the new behaviour",
                    "files": ["tests/test_personalised.py"],
                    "ac_ids": ["AC-2", "AC-3"],
                    "depends_on": [],
                },
            ]
        }

    def build_buggy(req: AgentRequest) -> str:
        if _is_task(req, "tests"):
            _write(req, "tests/test_personalised.py", GREETER_UNIT_TESTS.split("\n\n@pytest")[0] + "\n")
        else:
            _write(req, "src/greeter/__init__.py", GREETER_BUGGY)
        return "implemented"

    def build_good(req: AgentRequest) -> str:
        if _is_task(req, "tests"):
            _write(req, "tests/test_personalised.py", GREETER_UNIT_TESTS)
        else:
            _write(req, "src/greeter/__init__.py", GREETER_GOOD)
            if "Previous verification failed" in req.prompt:
                _write(req, "tests/test_personalised.py", GREETER_UNIT_TESTS)
        return "implemented"

    builds = [build_buggy, build_buggy, build_good] if buggy_first else [build_good]
    return FakeAgentRunner(
        {
            "grill": [grill_questions, grill_draft],
            "story_critic": lambda req: {"findings": []},
            "driver_writer": driver,
            "planner": plan,
            "builder": builds,
            "auditor": lambda req: {"findings": []},
            "reviewer": lambda req: {"spec_compliant": True, "findings": [], "summary": "Approved"},
            "finisher": lambda req: {
                "tests_passed": True,
                "test_command": "pytest -q",
                "test_summary": "all tests pass",
                "pr_title": "feat: personalised greeting",
                "pr_summary": "Adds an optional, trimmed name to `greet()`; blank names are rejected.",
            },
            "resolver": lambda req: "RESOLVED",
        }
    )
