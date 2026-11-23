"""Agent skills deployed into every coding-agent working directory.

The skills (obra/superpowers + Matt Pocock's grill-me/grilling) are declared in ``factory-skills/apm.yml``
and installed with APM, which pins them in ``apm.lock.yaml`` and writes them to ``.agents/skills`` (Codex,
Copilot CLI) and ``.claude/skills`` (Claude Code). Before each agent call the factory copies those skill
folders into the agent's working directory, git-excludes them (so they are never committed to the target
repo) and protects them from edits. APM-generated hooks are deliberately NOT deployed: the factory drives
every phase itself, so the superpowers session-start bootstrap is not wanted.

    python -m factory_core.skills deploy <dir>     # gh-aw: make the skills visible to the agent step
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from .resources import ROOT

SKILL_ROOTS = (".agents/skills", ".claude/skills")
MARKER = ".factory-skill"


def skills_source() -> Path | None:
    """``FACTORY_SKILLS_DIR`` or the ``factory-skills/`` folder of a source checkout."""
    env = os.environ.get("FACTORY_SKILLS_DIR")
    if env:
        return Path(env) if Path(env).is_dir() else None
    for parent in ROOT.parents:
        candidate = parent / "factory-skills"
        if (candidate / "apm.yml").is_file():
            return candidate
    return None


def _digest(folder: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(folder.rglob("*")):
        if p.is_file() and p.name != MARKER:
            h.update(p.relative_to(folder).as_posix().encode())
            h.update(p.read_bytes())
    return h.hexdigest()


def _rmtree(path: Path) -> None:
    def force(func, p, _exc):  # type: ignore[no-untyped-def]
        os.chmod(p, stat.S_IWRITE)
        func(p)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=force)
    else:
        shutil.rmtree(path, onerror=force)


def git_exclude(workdir: Path, patterns: list[str]) -> None:
    """Append ``patterns`` to the repository's ``info/exclude`` (the common one, so worktrees share it)."""
    dotgit = workdir / ".git"
    if dotgit.is_dir():
        common = dotgit
    elif dotgit.is_file():
        out = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"], cwd=workdir, capture_output=True, text=True, check=False
        )
        if out.returncode != 0:
            return
        common = (workdir / out.stdout.strip()).resolve()
    else:
        return
    exclude = common / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    current = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    missing = [p for p in patterns if p not in current.splitlines()]
    if missing:
        exclude.write_text(current.rstrip("\n") + "\n" + "\n".join(missing) + "\n", encoding="utf-8")


def deploy_skills(workdir: Path, source: Path | None = None) -> list[str]:
    """Copy the factory skills into ``workdir``; returns the deployed skill folders (relative paths).

    Idempotent. A skill folder the target repository already ships (no factory marker) is left untouched.
    """
    source = source or skills_source()
    if source is None:
        return []
    deployed: list[str] = []
    for root in SKILL_ROOTS:
        src_root = source / root
        if not src_root.is_dir():
            continue
        for skill in sorted(p for p in src_root.iterdir() if (p / "SKILL.md").is_file()):
            dest = workdir / root / skill.name
            if dest.exists():
                if not (dest / MARKER).exists():
                    continue
                if _digest(dest) == _digest(skill):
                    deployed.append(f"{root}/{skill.name}")
                    continue
                _rmtree(dest)
            shutil.copytree(skill, dest)
            (dest / MARKER).write_text("deployed by the AI software factory; do not edit\n", encoding="utf-8")
            deployed.append(f"{root}/{skill.name}")
    git_exclude(workdir, [f"/{rel}/" for rel in deployed])
    return deployed


def skill_globs(deployed: list[str]) -> list[str]:
    return [f"{rel}/**" for rel in deployed]


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or args[0] != "deploy":
        print("usage: python -m factory_core.skills deploy <dir>", file=sys.stderr)
        return 2
    deployed = deploy_skills(Path(args[1]))
    if not deployed:
        print("no factory skills found (set FACTORY_SKILLS_DIR)", file=sys.stderr)
        return 1
    print("\n".join(deployed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
