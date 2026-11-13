"""Repository reconnaissance: a compact map an agent can read in one glance."""

from __future__ import annotations

import subprocess
from pathlib import Path

SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    "dist",
    "build",
    "target",
    "bin",
    "obj",
    ".factory",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "coverage",
}


def list_files(repo: Path, limit: int = 300) -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files"], cwd=repo, capture_output=True, text=True, check=True).stdout
        files = [f for f in out.splitlines() if not (set(Path(f).parts) & SKIP_DIRS)]
    except (subprocess.CalledProcessError, FileNotFoundError):
        files = [
            p.relative_to(repo).as_posix()
            for p in repo.rglob("*")
            if p.is_file() and not (set(p.relative_to(repo).parts) & SKIP_DIRS)
        ]
    return sorted(files)[:limit]


def repo_map(repo: Path, limit: int = 300, readme_chars: int = 1500) -> str:
    files = list_files(repo, limit)
    lines = ["files:"] + [f"  {f}" for f in files]
    for name in ("README.md", "README.rst", "README.txt", "README"):
        p = repo / name
        if p.exists():
            lines += ["", f"{name} (head):", p.read_text(encoding="utf-8", errors="replace")[:readme_chars]]
            break
    return "\n".join(lines)
