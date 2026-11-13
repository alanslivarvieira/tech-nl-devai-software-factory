"""Stack profiles: how to lint/build/test/mutate a target repo, independent of its language.

Commands are argv lists (no shell) with placeholders:
``{python}`` current interpreter, ``{out}`` report dir, ``{base_sha}``, ``{spec_dir}``, ``{src_root}``,
``{diff_coverage_threshold}``, ``{mutation_threshold}``, ``{max_mutants}``. A token that is exactly
``{changed_src...}`` / ``{changed_tests...}`` / ``{changed_files...}`` expands to many arguments.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .resources import STACK_PROFILES_DIR


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")

    argv: list[str]
    required: bool = True
    needs: list[str] = Field(default_factory=list, description="executables that must be on PATH")
    needs_modules: list[str] = Field(default_factory=list, description="python modules that must import")
    skip_if_no_changed_src: bool = False
    timeout_s: int | None = None


class Detect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    any_files: list[str] = Field(default_factory=list)
    priority: int = 100


class StackProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    detect: Detect
    src_globs: list[str]
    test_globs: list[str]
    exclude_globs: list[str] = Field(default_factory=lambda: [".factory/**", ".git/**"])
    guarded_files: list[str] = Field(default_factory=list, description="any change is a violation")
    guarded_line_patterns: list[str] = Field(
        default_factory=list, description="regex; touching matching lines anywhere is a violation"
    )
    skip_markers: list[str] = Field(default_factory=list)
    coverage_exclusions: list[str] = Field(default_factory=list)
    assertion_patterns: list[str] = Field(default_factory=list)
    setup: list[Command] = Field(default_factory=list)
    commands: dict[str, list[Command] | Command | None] = Field(default_factory=dict)
    driver_template: str | None = None
    toolchain_hint: str = ""

    # ---------------------------------------------------------------- classification helpers
    def _match(self, path: str, globs: list[str]) -> bool:
        return any(glob_match(path, g) for g in globs)

    def is_excluded(self, path: str) -> bool:
        return self._match(path, self.exclude_globs)

    def is_test(self, path: str) -> bool:
        return not self.is_excluded(path) and self._match(path, self.test_globs)

    def is_src(self, path: str) -> bool:
        return not self.is_excluded(path) and not self.is_test(path) and self._match(path, self.src_globs)

    def gate_commands(self, gate: str) -> list[Command] | None:
        """Commands for a gate (run in order, all must pass); ``None`` when the stack has no such gate."""
        value = self.commands.get(gate)
        if value is None:
            return None
        return value if isinstance(value, list) else [value]

    def driver_template_text(self) -> str | None:
        if not self.driver_template:
            return None
        return (STACK_PROFILES_DIR / self.driver_template).read_text(encoding="utf-8")


def glob_match(path: str, pattern: str) -> bool:
    """Glob with ``**`` spanning directories (and matching zero directories)."""
    path = path.replace("\\", "/")
    regex = _glob_to_regex(pattern)
    return re.fullmatch(regex, path) is not None


_GLOB_CACHE: dict[str, str] = {}


def _glob_to_regex(pattern: str) -> str:
    if pattern in _GLOB_CACHE:
        return _GLOB_CACHE[pattern]
    i, out = 0, []
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    _GLOB_CACHE[pattern] = "".join(out)
    return _GLOB_CACHE[pattern]


def _load(path: Path) -> StackProfile:
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return StackProfile.model_validate(data)


def all_profiles() -> dict[str, StackProfile]:
    return {p.stem: _load(p) for p in sorted(STACK_PROFILES_DIR.glob("*.yaml"))}


def load_profile(name: str) -> StackProfile:
    path = STACK_PROFILES_DIR / f"{name}.yaml"
    if not path.exists():
        raise KeyError(f"unknown stack profile {name!r}; known: {sorted(all_profiles())}")
    return _load(path)


def detect_stack(repo: Path) -> str | None:
    """Pick the matching profile with the lowest priority number (most specific first)."""
    candidates = []
    for name, prof in all_profiles().items():
        for pattern in prof.detect.any_files:
            if any(repo.glob(pattern)):
                candidates.append((prof.detect.priority, name))
                break
    return min(candidates)[1] if candidates else None
