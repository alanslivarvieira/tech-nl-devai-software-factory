"""Create a factory issue from a repository_dispatch / workflow_dispatch event and start the grill."""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from ghaw_lib import ENGINES, PHASES, dispatch, gh, request_body

LABELS = ["factory", *(f"phase:{p}" for p in PHASES), *(f"engine:{e}" for e in ENGINES), "iter:0"]


def main() -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as fh:
        event = json.load(fh)
    data = event.get("client_payload") or event.get("inputs") or {}
    engine = data.get("engine") or os.environ.get("FACTORY_DEFAULT_ENGINE", "copilot")
    if engine not in ENGINES:
        raise SystemExit(f"unknown engine {engine}")
    for label in LABELS:
        gh("label", "create", label, "--repo", repo, "--force", "--color", "5319e7")
    title = "[factory] " + str(data["request"])[:70]
    body = request_body(data["repo"], data.get("base") or "main", data["request"], data.get("requester") or "unknown")
    url = gh(
        "issue",
        "create",
        "--repo",
        repo,
        "--title",
        title,
        "--body-file",
        "-",
        "--label",
        "factory",
        "--label",
        "phase:grill",
        "--label",
        f"engine:{engine}",
        "--label",
        "iter:0",
        input=body,
    ).strip()
    number = int(url.rstrip("/").rsplit("/", 1)[1])
    dispatch(repo, f"factory-grill-{engine}.lock.yml", issue=number)
    print(f"created {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
