"""GitHub-side helpers for the gh-aw factory: issue state, markers, gh CLI wrappers.

State model (one GitHub Issue per feature, in the hub repo):
* labels   ``factory``, ``phase:<grill|contract|driver|freeze|plan|build|verify|audit|pr|done|escalated|aborted>``,
             ``engine:<copilot|claude|codex>``, ``iter:<n>``
* body     the original request (```yaml factory-request```) and a hidden, factory-only state block
             ``<!-- factory:state {...} -->`` with the trust anchors (manifest root hash, freeze sha). Agents have
             no safe-output that can edit the body, so the anchors are out of their reach.
* comments drafts (``<!-- factory:draft -->``), plans, verification reports and audits, each with a marker.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any

import yaml

PHASES = (
    "grill",
    "contract",
    "driver",
    "freeze",
    "plan",
    "build",
    "verify",
    "audit",
    "pr",
    "done",
    "escalated",
    "aborted",
)
ENGINES = ("copilot", "claude", "codex")
HUMAN_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
STATE_RE = re.compile(r"<!-- factory:state (\{.*?\}) -->", re.S)
REQUEST_RE = re.compile(r"```yaml factory-request\n(.*?)```", re.S)
MARKER_RE = re.compile(r"<!-- factory:(?P<kind>[a-z]+)(?P<attrs>[^>]*)-->")
JSON_BLOCK_RE = re.compile(r"```json\n(.*?)```", re.S)


@dataclass
class Comment:
    author: str
    association: str
    body: str
    is_bot: bool = False

    def marker(self) -> tuple[str, dict[str, str]] | None:
        m = MARKER_RE.search(self.body)
        if not m:
            return None
        attrs = dict(re.findall(r"(\w+)=(\S+)", m.group("attrs")))
        return m.group("kind"), attrs

    def json_block(self) -> Any:
        m = JSON_BLOCK_RE.findall(self.body)
        return json.loads(m[-1]) if m else None


@dataclass
class Issue:
    number: int
    body: str
    labels: list[str]
    comments: list[Comment] = field(default_factory=list)
    state: str = "OPEN"

    # ---------------------------------------------------------------------------------------- labels
    def label_value(self, prefix: str, default: str | None = None) -> str | None:
        for lbl in self.labels:
            if lbl.startswith(prefix + ":"):
                return lbl.split(":", 1)[1]
        return default

    @property
    def phase(self) -> str:
        return self.label_value("phase", "grill") or "grill"

    @property
    def engine(self) -> str:
        return self.label_value("engine", os.environ.get("FACTORY_DEFAULT_ENGINE", "copilot")) or "copilot"

    @property
    def iteration(self) -> int:
        return int(self.label_value("iter", "0") or 0)

    # ----------------------------------------------------------------------------------------- body
    @property
    def request(self) -> dict[str, Any]:
        m = REQUEST_RE.search(self.body)
        return yaml.safe_load(m.group(1)) if m else {}

    @property
    def anchors(self) -> dict[str, Any]:
        m = STATE_RE.search(self.body)
        return json.loads(m.group(1)) if m else {}

    def with_anchors(self, **updates: Any) -> str:
        state = {**self.anchors, **updates}
        block = f"<!-- factory:state {json.dumps(state, sort_keys=True)} -->"
        return (
            STATE_RE.sub(block, self.body) if STATE_RE.search(self.body) else self.body.rstrip() + "\n\n" + block + "\n"
        )

    # ------------------------------------------------------------------------------------- comments
    def latest(self, kind: str, bot_only: bool = True) -> Comment | None:
        for c in reversed(self.comments):
            mk = c.marker()
            if mk and mk[0] == kind and (c.is_bot or not bot_only):
                return c
        return None

    def all_markers(self, kind: str) -> list[dict[str, str]]:
        out = []
        for c in self.comments:
            mk = c.marker()
            if mk and mk[0] == kind and c.is_bot:
                out.append(mk[1])
        return out

    def transcript(self) -> list[dict[str, str]]:
        """Human/factory conversation for the grill agent (drafts are summarised, not repeated)."""
        out = [{"role": "human", "text": str(self.request.get("request", ""))}]
        for c in self.comments:
            mk = c.marker()
            if mk and mk[0] != "draft":
                continue
            text = "[draft contract presented]" if mk else c.body.strip()
            out.append({"role": "factory" if c.is_bot else "human", "text": text})
        return out


# ------------------------------------------------------------------------------------------- gh CLI
def gh(*args: str, input: str | None = None, token: str | None = None) -> str:
    env = {**os.environ, "GH_TOKEN": token} if token else None
    p = subprocess.run(["gh", *args], capture_output=True, text=True, input=input, encoding="utf-8", env=env)
    if p.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {p.stderr.strip()}")
    return p.stdout


def load_issue(repo: str, number: int) -> Issue:
    data = json.loads(gh("issue", "view", str(number), "--repo", repo, "--json", "number,body,labels,comments,state"))
    comments = [
        Comment(
            author=(c.get("author") or {}).get("login", ""),
            association=c.get("authorAssociation", ""),
            body=c.get("body", ""),
            is_bot=_is_bot((c.get("author") or {}).get("login", "")),
        )
        for c in data.get("comments", [])
    ]
    return Issue(
        number=data["number"],
        body=data.get("body") or "",
        labels=[lbl["name"] for lbl in data["labels"]],
        comments=comments,
        state=data.get("state", "OPEN"),
    )


def _is_bot(login: str) -> bool:
    bots = {b.strip() for b in os.environ.get("FACTORY_BOT_LOGINS", "github-actions,github-actions[bot]").split(",")}
    return login in bots or login.endswith("[bot]")


def comment(repo: str, number: int, body: str) -> None:
    gh("issue", "comment", str(number), "--repo", repo, "--body-file", "-", input=body)


def set_body(repo: str, number: int, body: str) -> None:
    gh("issue", "edit", str(number), "--repo", repo, "--body-file", "-", input=body)


def relabel(repo: str, issue: Issue, **values: str | int) -> None:
    add, remove = [], []
    for prefix, value in values.items():
        remove += [lbl for lbl in issue.labels if lbl.startswith(prefix + ":")]
        add.append(f"{prefix}:{value}")
    args = ["issue", "edit", str(issue.number), "--repo", repo]
    for lbl in add:
        args += ["--add-label", lbl]
    for lbl in set(remove) - set(add):
        args += ["--remove-label", lbl]
    gh(*args)


def request_body(repo: str, base: str, request: str, requester: str) -> str:
    """Issue body for a new factory request (the Teams bot's gh-aw backend produces the same format)."""
    spec = yaml.safe_dump(
        {"repo": repo, "base": base, "request": request, "requester": requester}, sort_keys=False, allow_unicode=True
    )
    return (
        f"**Feature request** from {requester} for `{repo}` (base `{base}`)\n\n> {request}\n\n"
        f"```yaml factory-request\n{spec}```\n\n"
        "The factory will grill you in the comments. Commands: `/engine copilot|claude|codex`, `/approve`, "
        "`/continue`, `/amend <change>`, `/abort`.\n\n<!-- factory:state {} -->\n"
    )


def dispatch(repo: str, workflow: str, **inputs: str | int) -> None:
    args = ["workflow", "run", workflow, "--repo", repo]
    for k, v in inputs.items():
        args += ["-f", f"{k}={v}"]
    gh(*args)
