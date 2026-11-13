"""Change sets from git or from a unified-diff patch (the gh-aw guard inspects agent patches)."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FileChange:
    path: str
    status: str  # A added, M modified, D deleted, R renamed (path = new path, old_path set)
    old_path: str | None = None
    added: list[tuple[int, str]] = field(default_factory=list)
    removed: list[tuple[int, str]] = field(default_factory=list)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, encoding="utf-8", errors="replace"
    ).stdout


_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def parse_patch(text: str) -> list[FileChange]:
    """Parse ``git diff``/``git format-patch`` output (multiple files)."""
    changes: list[FileChange] = []
    cur: FileChange | None = None
    old_ln = new_ln = 0
    in_hunk = False
    for line in text.splitlines():
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.+?) b/(.+)$", line)
            a, b = (m.group(1), m.group(2)) if m else ("", "")
            cur = FileChange(path=b, status="M", old_path=a if a != b else None)
            if a != b:
                cur.status = "R"
            changes.append(cur)
            in_hunk = False
            continue
        if cur is None:
            continue
        if not in_hunk:
            if line.startswith("new file mode"):
                cur.status = "A"
            elif line.startswith("deleted file mode"):
                cur.status = "D"
            elif line.startswith("rename from "):
                cur.old_path, cur.status = line[len("rename from ") :], "R"
            elif line.startswith("rename to "):
                cur.path = line[len("rename to ") :]
        if m := _HUNK.match(line):
            old_ln, new_ln = int(m.group(1)), int(m.group(2))
            in_hunk = True
        elif not in_hunk:
            continue
        elif line.startswith("+"):
            cur.added.append((new_ln, line[1:]))
            new_ln += 1
        elif line.startswith("-"):
            cur.removed.append((old_ln, line[1:]))
            old_ln += 1
        elif line.startswith(" "):
            old_ln += 1
            new_ln += 1
    return changes


def from_git(repo: Path, base: str, head: str = "HEAD", include_worktree: bool = False) -> list[FileChange]:
    """Changes between ``base`` and ``head`` (or the working tree incl. untracked files)."""
    if include_worktree:
        _git(repo, "add", "--intent-to-add", "--all")
        text = _git(repo, "diff", "-M", "-U0", "--no-color", base)
    else:
        text = _git(repo, "diff", "-M", "-U0", "--no-color", base, head)
    return parse_patch(text)
