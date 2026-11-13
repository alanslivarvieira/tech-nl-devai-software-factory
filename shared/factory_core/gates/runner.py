"""Deterministic verification gates, driven by a stack profile.

Gate order (all gates always run so the builder gets the full picture in one iteration):
integrity → setup → lint → typecheck → build → unit → diff_coverage → contract → acceptance → property
→ mutation → regression → security

``base_sha`` is the *freeze commit* (the commit that added the locked contract), so every diff measured
here is exactly the agent's production change.
"""

from __future__ import annotations

import glob as globmod
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..guard import check_changes, from_git, verify
from ..models import FactoryConfig, GateResult
from ..stack import Command, StackProfile
from ..tad_runner.report import to_text
from ..tad_runner.runner import run as tad_run

GATE_ORDER = [
    "integrity",
    "setup",
    "lint",
    "typecheck",
    "build",
    "unit",
    "diff_coverage",
    "contract",
    "acceptance",
    "property",
    "mutation",
    "regression",
    "security",
]
SECURITY = [
    Command(
        argv=["semgrep", "scan", "--error", "--quiet", "--config", "p/default", "{changed_files...}"],
        required=False,
        needs=["semgrep"],
    ),
    Command(
        argv=["gitleaks", "detect", "--no-banner", "--redact", "--log-opts={base_sha}..HEAD"],
        required=False,
        needs=["gitleaks"],
    ),
]
TAIL = 6000


@dataclass
class GateContext:
    repo: Path
    spec_dir: Path
    base_sha: str
    profile: StackProfile
    config: FactoryConfig
    expected_hash: str | None
    out_dir: Path

    def __post_init__(self) -> None:
        self.repo = self.repo.resolve()
        self.spec_dir = self.spec_dir if self.spec_dir.is_absolute() else (self.repo / self.spec_dir)
        self.out_dir = self.out_dir.resolve()
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._changed: list[str] | None = None

    @property
    def spec_rel(self) -> str:
        return self.spec_dir.resolve().relative_to(self.repo).as_posix()

    def changed_files(self) -> list[str]:
        if self._changed is None:
            out = subprocess.run(
                ["git", "diff", "--name-only", "--diff-filter=d", self.base_sha, "HEAD"],
                cwd=self.repo,
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            self._changed = [line.strip() for line in out.splitlines() if line.strip()]
        return self._changed

    def changed_src(self) -> list[str]:
        return [f for f in self.changed_files() if self.profile.is_src(f)]

    def changed_tests(self) -> list[str]:
        return [f for f in self.changed_files() if self.profile.is_test(f)]

    def src_root(self) -> str:
        return "src" if (self.repo / "src").is_dir() else "."

    def expand(self, argv: list[str]) -> list[str]:
        scalars = {
            "{python}": sys.executable,
            "{out}": self.out_dir.as_posix(),
            "{base_sha}": self.base_sha,
            "{spec_dir}": self.spec_rel,
            "{src_root}": self.src_root(),
            "{diff_coverage_threshold}": str(self.config.diff_coverage_threshold),
            "{mutation_threshold}": str(self.config.mutation_threshold),
            "{max_mutants}": str(self.config.max_mutants),
            "{changed_src_csv}": ",".join(self.changed_src()),
        }
        lists = {
            "{changed_src...}": self.changed_src(),
            "{changed_tests...}": self.changed_tests(),
            "{changed_files...}": self.changed_files(),
            "{pip_install...}": pip_install(),
        }
        out: list[str] = []
        for tok in argv:
            if tok in lists:
                out.extend(lists[tok])
                continue
            for k, v in scalars.items():
                tok = tok.replace(k, v)
            if tok.startswith("glob:"):
                hits = sorted(
                    globmod.glob(str(self.repo / tok[5:]) if not os.path.isabs(tok[5:]) else tok[5:], recursive=True)
                )
                out.extend(hits or [tok[5:]])
                continue
            out.append(tok)
        if out and not os.path.isabs(out[0]):
            out[0] = shutil.which(out[0]) or out[0]
        return out


def pip_install() -> list[str]:
    """``python -m pip install`` when pip exists in this interpreter, else ``uv pip install``."""
    if importlib.util.find_spec("pip") is not None:
        return [sys.executable, "-m", "pip", "install"]
    if shutil.which("uv"):
        return [shutil.which("uv") or "uv", "pip", "install", "--python", sys.executable]
    return [sys.executable, "-m", "pip", "install"]


def _env(ctx: GateContext) -> dict[str, str]:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    if (ctx.repo / "src").is_dir():
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(ctx.repo / "src"), env.get("PYTHONPATH")]))
    return env


def _run_commands(ctx: GateContext, name: str, cmds: list[Command]) -> GateResult:
    start = time.monotonic()
    required = any(c.required for c in cmds)
    logs: list[str] = []
    for cmd in cmds:
        missing = [n for n in cmd.needs if shutil.which(n) is None]
        missing += [m for m in cmd.needs_modules if importlib.util.find_spec(m) is None]
        if missing:
            status = "error" if cmd.required else "skip"
            return GateResult(
                name=name,
                status=status,
                required=required,
                summary=f"tool(s) not available: {', '.join(missing)}",
                duration_s=time.monotonic() - start,
            )
        if cmd.skip_if_no_changed_src and not ctx.changed_src():
            return GateResult(
                name=name,
                status="skip",
                required=required,
                summary="no changed source files",
                duration_s=time.monotonic() - start,
            )
        argv = ctx.expand(cmd.argv)
        logs.append("$ " + " ".join(argv))
        try:
            proc = subprocess.run(
                argv,
                cwd=ctx.repo,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=cmd.timeout_s or ctx.config.gate_timeout_s,
                env=_env(ctx),
            )
        except subprocess.TimeoutExpired:
            return GateResult(
                name=name,
                status="fail",
                required=cmd.required,
                summary="timed out",
                details="\n".join(logs)[-TAIL:],
                duration_s=time.monotonic() - start,
                failures=["timeout"],
            )
        except OSError as exc:
            return GateResult(
                name=name,
                status="error",
                required=cmd.required,
                summary=f"cannot run: {exc}",
                duration_s=time.monotonic() - start,
            )
        logs.append((proc.stdout + proc.stderr).strip())
        if proc.returncode != 0:
            return GateResult(
                name=name,
                status="fail",
                required=cmd.required,
                summary=f"exit code {proc.returncode}",
                details="\n".join(logs)[-TAIL:],
                duration_s=time.monotonic() - start,
                failures=_failures(ctx, name),
            )
    return GateResult(
        name=name,
        status="pass",
        required=required,
        summary="ok",
        details="\n".join(logs)[-TAIL:],
        duration_s=time.monotonic() - start,
    )


def _failures(ctx: GateContext, gate: str) -> list[str]:
    """Stable ids of failing items, used for the stall fingerprint."""
    try:
        if gate == "unit" and (ctx.out_dir / "unit.xml").exists():
            root = ET.parse(ctx.out_dir / "unit.xml").getroot()
            return sorted(
                f"{tc.get('classname')}::{tc.get('name')}"
                for tc in root.iter("testcase")
                if tc.find("failure") is not None or tc.find("error") is not None
            ) or ["*"]
        if gate == "mutation" and (ctx.out_dir / "mutation.json").exists():
            data = json.loads((ctx.out_dir / "mutation.json").read_text(encoding="utf-8"))
            return sorted(f"{s['file']}:{s['line']}:{s['operator']}" for s in data.get("survivors", [])) or ["score"]
    except (ET.ParseError, json.JSONDecodeError, KeyError):
        pass
    return ["*"]


# --------------------------------------------------------------------------------------------- gates
def gate_integrity(ctx: GateContext) -> GateResult:
    start = time.monotonic()
    res = verify(ctx.spec_dir, ctx.expected_hash)
    guard = check_changes(from_git(ctx.repo, ctx.base_sha, "HEAD"), ctx.profile)
    ok = res.ok and guard.ok
    details = res.summary() + "\n" + guard.summary()
    if guard.warnings:
        details += "\n" + "\n".join(f"  ⚠ [{w.rule}] {w.path} — {w.detail}" for w in guard.warnings)
    failures = ([] if res.ok else ["contract-tampered"]) + [f"{v.rule}:{v.path}" for v in guard.violations]
    return GateResult(
        name="integrity",
        status="pass" if ok else "fail",
        summary="contract and guard rules intact" if ok else "anti-cheat violation",
        details=details,
        failures=failures,
        duration_s=time.monotonic() - start,
    )


def _tad_gate(name: str, kinds: list[str]) -> Callable[[GateContext], GateResult]:
    def gate(ctx: GateContext) -> GateResult:
        start = time.monotonic()
        report = tad_run(ctx.spec_dir, ctx.repo, kinds)
        (ctx.out_dir / f"tad-{name}.json").write_text(
            json.dumps(report.as_dict(), indent=2, default=str), encoding="utf-8"
        )
        if not report.results and not report.problems:
            return GateResult(
                name=name, status="skip", summary=f"no {'/'.join(kinds)} cases", duration_s=time.monotonic() - start
            )
        c = report.counts()
        return GateResult(
            name=name,
            status="pass" if report.passed else "fail",
            summary=f"{c['pass']} passed, {c['fail']} failed, {c['error']} errors",
            details=to_text(report)[-TAIL:],
            failures=report.failing_ids() + report.problems[:5],
            duration_s=time.monotonic() - start,
        )

    return gate


def run_gates(ctx: GateContext, only: list[str] | None = None) -> list[GateResult]:
    results: list[GateResult] = []
    for name in GATE_ORDER:
        if only and name not in only:
            continue
        if name == "integrity":
            results.append(gate_integrity(ctx))
        elif name == "setup":
            if ctx.config.run_setup and ctx.profile.setup:
                results.append(_run_commands(ctx, "setup", ctx.profile.setup))
        elif name == "contract":
            results.append(_tad_gate("contract", ["table", "scenario"])(ctx))
        elif name == "acceptance":
            results.append(_tad_gate("acceptance", ["gherkin"])(ctx))
        elif name == "property":
            results.append(_tad_gate("property", ["property"])(ctx))
        elif name == "security":
            for i, cmd in enumerate(SECURITY):
                if shutil.which(cmd.needs[0]):
                    results.append(_run_commands(ctx, f"security:{cmd.needs[0]}", [cmd]))
                elif i == 0:
                    results.append(
                        GateResult(
                            name="security", status="skip", required=False, summary="semgrep/gitleaks not installed"
                        )
                    )
        else:
            cmds = ctx.profile.gate_commands(name)
            if cmds:
                results.append(_run_commands(ctx, name, cmds))
    return results
