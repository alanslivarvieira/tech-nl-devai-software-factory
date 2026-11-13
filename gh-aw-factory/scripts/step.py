"""Deterministic (non-agent) steps of the gh-aw factory, run by ``factory-step.yml``.

step.py target-info      --issue N                          -> GITHUB_OUTPUT: owner, name, base, branch
step.py prepare-contract --issue N --approver LOGIN         (target checked out at base in ./target)
step.py apply-patch      --issue N --mode driver|build --patch agent.patch
step.py freeze           --issue N
step.py verify           --issue N [--out verify-out]       (fresh runner, branch head in ./target)
step.py open-pr          --issue N
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from ghaw_lib import comment, gh, load_issue, set_body

from factory_core import pipeline
from factory_core.gates import GateContext, run_gates
from factory_core.gates.cli import load_config
from factory_core.guard import check_changes, parse_patch
from factory_core.guard.diffcheck import Finding
from factory_core.models import VerificationReport
from factory_core.reports import pr_body, report_markdown
from factory_core.spec import files_to_draft, parse_markdown
from factory_core.stack import detect_stack, glob_match, load_profile
from factory_core.tad_runner.layout import SpecLayout
from factory_core.tad_runner.runner import freeze_check, load_intent

TARGET = Path("target")


def branch_for(n: int) -> str:
    return f"factory/issue-{n}"


def push(target: Path, branch: str) -> None:
    pipeline.git(target, "push", "origin", f"HEAD:refs/heads/{branch}", "--force-with-lease")


def output(**values: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    lines = "".join(f"{k}={v}\n" for k, v in values.items())
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(lines)
    print(lines, end="")


def verify_comment(report: VerificationReport, title: str = "") -> str:
    data = report.model_dump()
    for g in data["gates"]:
        g["details"] = g["details"][-2500:]
    return (
        f"<!-- factory:verify iteration={report.iteration} passed={str(report.passed).lower()} "
        f"fingerprint={report.fingerprint()} -->\n{title}{report_markdown(report)}\n"
        f"<details><summary>machine-readable report</summary>\n\n```json\n{json.dumps(data)}\n```\n</details>\n"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("step")
    ap.add_argument("--issue", type=int, required=True)
    ap.add_argument("--approver", default="")
    ap.add_argument("--mode", default="build")
    ap.add_argument("--patch", type=Path)
    ap.add_argument("--out", type=Path, default=Path("verify-out"))
    a = ap.parse_args()
    repo = os.environ["GITHUB_REPOSITORY"]
    issue = load_issue(repo, a.issue)
    req, anchors, n = issue.request, issue.anchors, issue.number

    if a.step == "target-info":
        owner, name = str(req["repo"]).split("/", 1)
        output(
            owner=owner,
            name=name,
            base=str(req.get("base", "main")),
            branch=branch_for(n),
            branch_exists=str(bool(anchors.get("spec_rel"))).lower(),
        )
        return 0

    if a.step == "prepare-contract":
        draft_comment = issue.latest("draft")
        assert draft_comment is not None
        draft = files_to_draft(parse_markdown(draft_comment.body))
        draft.intent.engine = issue.engine  # type: ignore[assignment]
        draft.intent.approved_by = a.approver
        stack = detect_stack(TARGET)
        pipeline.git(TARGET, "checkout", "-q", "-B", branch_for(n))
        spec_dir = TARGET / pipeline.spec_rel(draft.intent.feature_id)
        if spec_dir.exists():
            pipeline.rmtree(spec_dir)
        pipeline.write_contract(TARGET, draft, load_profile(stack) if stack else None)
        pipeline.commit_all(TARGET, f"factory: approved contract draft (#{n})")
        push(TARGET, branch_for(n))
        set_body(
            repo,
            n,
            issue.with_anchors(
                feature_id=draft.intent.feature_id,
                approver=a.approver,
                spec_rel=pipeline.spec_rel(draft.intent.feature_id),
                stack=stack,
            ),
        )
        return 0

    profile = load_profile(anchors["stack"]) if anchors.get("stack") else None

    if a.step == "apply-patch":
        assert a.patch is not None
        text = a.patch.read_text(encoding="utf-8") if a.patch.exists() else ""
        changes = parse_patch(text)
        driver_glob = f"{anchors['spec_rel']}/driver/**"
        rep = check_changes(changes, profile, allow_paths=[driver_glob] if a.mode == "driver" else [])
        if a.mode == "driver":
            rep.violations += [
                Finding("driver-scope", ch.path, "the driver writer may only touch the driver")
                for ch in changes
                if not glob_match(ch.path, driver_glob)
            ]
        if not changes:
            comment(repo, n, f"⚠️ The {a.mode} agent produced no changes.")
            return 1
        if not rep.ok:
            fp = (
                "guard-"
                + hashlib.sha256(json.dumps(sorted(f"{v.rule}:{v.path}" for v in rep.violations)).encode()).hexdigest()[
                    :12
                ]
            )
            comment(
                repo,
                n,
                f"<!-- factory:verify iteration={issue.iteration} passed=false fingerprint={fp} -->\n"
                f"🛡️ **Guard rejected the {a.mode} patch**\n```\n{rep.summary()}\n```",
            )
            return 1
        pipeline.git(TARGET, "apply", "--index", "--whitespace=nowarn", str(a.patch.resolve()))
        pipeline.commit_all(TARGET, f"factory: {a.mode} changes (#{n}, iteration {issue.iteration})")
        push(TARGET, branch_for(n))
        return 0

    layout = SpecLayout(TARGET / anchors["spec_rel"])
    if a.step == "freeze":
        problems = freeze_check(layout.root, TARGET)
        if problems:
            comment(
                repo,
                n,
                "<!-- factory:freezefailed -->\n❌ **Freeze check failed** — back to the grill.\n\n"
                + "\n".join(f"- {p}" for p in problems[:20])
                + f"\n\n```json\n{json.dumps({'problems': problems})}\n```",
            )
            return 1
        res = pipeline.freeze(TARGET, layout, anchors.get("approver") or "human")
        push(TARGET, branch_for(n))
        set_body(repo, n, issue.with_anchors(manifest_hash=res.root_hash, freeze_sha=res.freeze_sha))
        comment(repo, n, f"🔒 Contract frozen at `{res.freeze_sha[:10]}` (root `{res.root_hash[:16]}`). Planning…")
        return 0

    if a.step == "verify":
        ctx = GateContext(
            repo=TARGET,
            spec_dir=layout.root,
            base_sha=anchors["freeze_sha"],
            profile=profile,  # type: ignore[arg-type]
            config=load_config(Path(os.environ.get("FACTORY_CONFIG", "factory.yaml"))),
            expected_hash=anchors["manifest_hash"],
            out_dir=a.out,
        )
        report = VerificationReport(
            feature_id=anchors["feature_id"],
            iteration=issue.iteration,
            commit=pipeline.head(TARGET),
            gates=run_gates(ctx),
        )
        comment(repo, n, verify_comment(report))
        Path(a.out / "report.json").write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
        return 0 if report.passed else 1

    if a.step == "open-pr":
        c = issue.latest("verify")
        report = VerificationReport.model_validate(c.json_block())  # type: ignore[union-attr]
        intent = load_intent(layout)
        body = (
            pr_body(
                intent,
                anchors["spec_rel"],
                layout.root,
                report,
                issue.iteration,
                anchors["manifest_hash"],
                issue.engine,
            )
            + f"\n\nCloses factory request {os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{repo}/issues/{n}\n"
        )
        url = gh(
            "pr",
            "create",
            "--repo",
            str(req["repo"]),
            "--head",
            branch_for(n),
            "--base",
            str(req.get("base", "main")),
            "--title",
            f"feat: {intent.title}",
            "--body-file",
            "-",
            input=body,
            token=os.environ.get("FACTORY_TARGET_TOKEN"),
        ).strip()
        comment(repo, n, f"🎉 Pull request ready for review: {url}")
        return 0
    raise SystemExit(f"unknown step {a.step}")


if __name__ == "__main__":
    sys.exit(main())
