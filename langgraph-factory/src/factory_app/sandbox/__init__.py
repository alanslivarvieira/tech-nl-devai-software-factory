"""Sandboxes in which coding-agent CLIs run.

* ``local``  — run on this host/container (the docker-compose ``app`` container is itself a sandbox).
* ``docker`` — one throw-away container per agent call; the contract directory is bind-mounted READ-ONLY,
  so even a misbehaving agent physically cannot edit the tests.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from factory_core.agents import Executor, LocalExecutor
from factory_core.tad_runner.layout import SPECS_ROOT

WORK = "/work"


class DockerExecutor:
    def __init__(
        self,
        image: str,
        env_passthrough: tuple[str, ...] = (
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "GITHUB_TOKEN",
            "COPILOT_GITHUB_TOKEN",
            "GH_TOKEN",
        ),
    ) -> None:
        self.image = image
        self.env_passthrough = env_passthrough

    def command(self, argv: list[str], cwd: Path, env: dict[str, str]) -> list[str]:
        host = str(cwd.resolve())
        mapped = [a.replace(host, WORK).replace("\\", "/") if host in a else a for a in argv]
        cmd = ["docker", "run", "--rm", "-i", "-v", f"{host}:{WORK}", "-w", WORK]
        specs = cwd / SPECS_ROOT
        if specs.is_dir():
            cmd += ["-v", f"{specs.resolve()}:{WORK}/{SPECS_ROOT}:ro"]
        for key in self.env_passthrough:
            if os.environ.get(key):
                cmd += ["-e", key]
        for k, v in env.items():
            cmd += ["-e", f"{k}={v}"]
        return cmd + [self.image] + mapped

    def run(
        self, argv: list[str], cwd: Path, stdin: str | None, env: dict[str, str], timeout: int
    ) -> tuple[int, str, str]:
        try:
            p = subprocess.run(
                self.command(argv, cwd, env),
                input=stdin,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return 124, "", f"sandboxed agent timed out after {timeout}s"
        return p.returncode, p.stdout, p.stderr


def make_executor(kind: str, image: str) -> Executor:
    return DockerExecutor(image) if kind == "docker" else LocalExecutor()
