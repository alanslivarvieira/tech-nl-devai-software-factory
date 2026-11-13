"""Adversarial tests: every way an agent could cheat must be caught by the guard or the manifest."""

from __future__ import annotations

from pathlib import Path

import pytest

from factory_core import pipeline
from factory_core.guard import check_changes, from_git, lock, parse_patch, verify
from factory_core.stack import load_profile
from factory_core.testing import GREETER_GOOD

PY = load_profile("python")


def _edit(repo: Path, rel: str, text: str | None) -> None:
    path = repo / rel
    if text is None:
        path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.chmod(0o644)
    path.write_text(text, encoding="utf-8")


def _guard(repo: Path, base: str):
    pipeline.commit_all(repo, "agent change")
    return check_changes(from_git(repo, base, "HEAD"), PY)


SPEC = ".factory/specs/personalised-greeting"

CHEATS = {
    "edit expected value in CSV": (
        f"{SPEC}/examples/greet.csv",
        lambda t: t.replace("Hello, Ann!", "Hello,   Ann  !"),
        "protected-path",
    ),
    "weaken the driver": (
        f"{SPEC}/driver/driver.py",
        lambda t: t.replace('greeter.greet(inp["name"])', '"x"'),
        "protected-path",
    ),
    "delete a gherkin feature": (f"{SPEC}/features/greet.table.feature", None, "protected-path"),
    "add skip marker": (
        "tests/test_greeter.py",
        lambda t: "import pytest\n\n\n@pytest.mark.skip(reason='flaky')\n" + t.split("\n", 2)[2],
        "skip-marker",
    ),
    "delete existing test": ("tests/test_greeter.py", None, "deleted-test"),
    "lower coverage threshold": (
        "pyproject.toml",
        lambda t: t + "\n[tool.coverage.report]\nfail_under = 0\n",
        "threshold-tamper",
    ),
    "exclude code from coverage": (
        "src/greeter/__init__.py",
        lambda t: t.replace("def greet", "def greet  # pragma: no cover\ndef _x():\n    pass\n\n\ndef greet"),
        "coverage-exclusion",
    ),
    "edit CI workflow": (".github/workflows/ci.yml", lambda t: "on: push\n", "protected-path"),
}


@pytest.mark.parametrize("name", list(CHEATS))
def test_guard_blocks_cheating(frozen_greeter, name):
    repo, layout, fr = frozen_greeter
    rel, change, rule = CHEATS[name]
    path = repo / rel
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    _edit(repo, rel, change(original) if change else None)
    rep = _guard(repo, fr.freeze_sha)
    assert not rep.ok, f"{name} was not detected"
    assert rule in {v.rule for v in rep.violations}, rep.summary()


def test_guard_allows_legitimate_change(frozen_greeter):
    repo, _, fr = frozen_greeter
    _edit(repo, "src/greeter/__init__.py", GREETER_GOOD)
    _edit(
        repo,
        "tests/test_extra.py",
        "from greeter import greet\n\n\ndef test_x():\n    assert greet('A') == 'Hello, A!'\n",
    )
    rep = _guard(repo, fr.freeze_sha)
    assert rep.ok, rep.summary()


def test_guard_warns_on_weakened_assertions(frozen_greeter):
    repo, _, fr = frozen_greeter
    _edit(repo, "tests/test_greeter.py", "from greeter import greet\n\n\ndef test_default_greeting():\n    greet()\n")
    rep = _guard(repo, fr.freeze_sha)
    assert rep.ok and any(w.rule == "weakened-test" for w in rep.warnings)


def test_patch_mode_matches_git_mode(frozen_greeter):
    repo, _, fr = frozen_greeter
    _edit(repo, f"{SPEC}/examples/greet.csv", "case,ac_id\n")
    pipeline.commit_all(repo, "tamper")
    patch = pipeline.git(repo, "diff", fr.freeze_sha, "HEAD")
    rep = check_changes(parse_patch(patch), PY)
    assert not rep.ok and rep.violations[0].rule == "protected-path"


def test_allow_reopens_protected_paths_for_freeze(frozen_greeter):
    repo, _, fr = frozen_greeter
    _edit(repo, f"{SPEC}/examples/more.csv", "case,ac_id\n")
    pipeline.commit_all(repo, "spec amendment")
    rep = check_changes(from_git(repo, fr.freeze_sha), PY, allow_paths=[f"{SPEC}/**"])
    assert rep.ok


# ------------------------------------------------------------------------------------------ manifest
def test_manifest_detects_modified_added_removed(tmp_path: Path):
    spec = tmp_path / "spec"
    (spec / "examples").mkdir(parents=True)
    (spec / "examples" / "a.csv").write_text("x\n", encoding="utf-8")
    (spec / "intent.yaml").write_text("y\n", encoding="utf-8")
    m = lock(spec, "f", "me")
    assert verify(spec, m["root_hash"]).ok
    (spec / "examples" / "a.csv").write_text("changed\n", encoding="utf-8")
    (spec / "examples" / "b.csv").write_text("new\n", encoding="utf-8")
    (spec / "intent.yaml").unlink()
    res = verify(spec, m["root_hash"])
    assert not res.ok
    assert res.modified == ["examples/a.csv"] and res.added == ["examples/b.csv"] and res.removed == ["intent.yaml"]


def test_rewriting_manifest_is_caught_by_external_anchor(tmp_path: Path):
    spec = tmp_path / "spec"
    spec.mkdir()
    (spec / "a.csv").write_text("x\n", encoding="utf-8")
    approved = lock(spec, "f", "me")["root_hash"]
    (spec / "a.csv").write_text("weakened\n", encoding="utf-8")
    lock(spec, "f", "agent")  # the agent re-locks to make the in-repo manifest consistent again
    assert verify(spec).ok, "in-repo manifest alone can be forged"
    res = verify(spec, approved)
    assert not res.ok and "external trust anchor" in res.summary()


def test_manifest_is_line_ending_agnostic(tmp_path: Path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "x.csv").write_bytes(b"h\nv\n")
    (b / "x.csv").write_bytes(b"h\r\nv\r\n")
    assert lock(a, "f", "me")["root_hash"] == lock(b, "f", "me")["root_hash"]


def test_manifest_ignores_caches_and_reports_clearly(tmp_path: Path):
    spec = tmp_path / "spec"
    (spec / "driver").mkdir(parents=True)
    (spec / "driver" / "driver.py").write_text("x\n", encoding="utf-8")
    m = lock(spec, "f", "me")
    assert m["approved_by"] == "me" and m["approved_at"] and m["feature_id"] == "f"
    # Byte-code and dependency caches produced by running the driver must not count as tampering.
    (spec / "driver" / "__pycache__").mkdir()
    (spec / "driver" / "__pycache__" / "driver.cpython-312.pyc").write_bytes(b"\0")
    (spec / "driver" / "stray.pyc").write_bytes(b"\0")
    (spec / "node_modules" / "pkg").mkdir(parents=True)
    (spec / "node_modules" / "pkg" / "index.js").write_text("x", encoding="utf-8")
    assert verify(spec, m["root_hash"]).ok
    (spec / "driver" / "extra.py").write_text("y\n", encoding="utf-8")
    res = verify(spec, m["root_hash"])
    assert res.summary().startswith("CONTRACT TAMPERED — ") and "added: driver/extra.py" in res.summary()
