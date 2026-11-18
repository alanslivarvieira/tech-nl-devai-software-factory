from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from factory_core import pipeline
from factory_core.stack import load_profile
from factory_core.testing import greeter_draft, greeter_driver, make_greeter_repo


@pytest.fixture
def greeter_repo(tmp_path: Path) -> Path:
    repo = make_greeter_repo(tmp_path / "repo")
    pipeline.git(repo, "checkout", "-q", "-b", "factory/test")
    return repo


@pytest.fixture
def frozen_greeter(greeter_repo: Path):
    """Greeter repo with the approved contract written, driver in place, frozen and committed."""
    layout = pipeline.write_contract(greeter_repo, greeter_draft(), load_profile("python"))
    (layout.driver / "driver.py").write_text(greeter_driver(), encoding="utf-8")
    result = pipeline.freeze(greeter_repo, layout, approver="tester")
    assert result.ok, result.problems
    return greeter_repo, layout, result


def copy_spec(src: Path, dst: Path) -> Path:
    shutil.copytree(src, dst)
    return dst
