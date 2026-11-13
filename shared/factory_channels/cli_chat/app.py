"""``factory-chat``: talk to the factory from a terminal — the same conversation the Teams bot offers.

factory-chat --backend langgraph --url http://localhost:8080
factory-chat --backend gh-aw --hub my-org/factory-hub        (token from GH_TOKEN / GITHUB_TOKEN)
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import threading
import time

from ..backend import make_backend
from ..cards import card_to_text
from ..chat import HELP, FactoryChat, Reply

CID = "terminal"


def show(replies: list[Reply]) -> None:
    for r in replies:
        print()
        print(card_to_text(r.card) if r.card else r.text)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="factory-chat")
    ap.add_argument("--backend", choices=["langgraph", "gh-aw"], default="langgraph")
    ap.add_argument("--url", default=os.environ.get("FACTORY_API_URL", "http://localhost:8080"))
    ap.add_argument("--hub", default=os.environ.get("FACTORY_HUB_REPO"))
    ap.add_argument("--interval", type=float, default=2.0)
    a = ap.parse_args(argv)
    backend = make_backend(
        a.backend, url=a.url, hub=a.hub, token=os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    )
    chat = FactoryChat(backend)
    user = os.environ.get("FACTORY_USER") or getpass.getuser()
    stop = threading.Event()

    def pump() -> None:
        while not stop.is_set():
            try:
                replies = chat.updates(CID)
                if replies:
                    show(replies)
                    print("\nyou> ", end="", flush=True)
            except Exception as exc:
                print(f"\n(poll failed: {exc})")
            time.sleep(a.interval)

    threading.Thread(target=pump, daemon=True).start()
    print(HELP)
    try:
        while True:
            line = input("\nyou> ").strip()
            if line in ("/quit", "/exit"):
                break
            if line:
                show(chat.handle(CID, user, text=line))
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
