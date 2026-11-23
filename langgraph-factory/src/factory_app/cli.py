"""``factory-run``: drive the whole factory in-process from a terminal — no server, no Teams.

    factory-run --repo ../my-service --request "add a personalised greeting"
    factory-run --fake greeter --repo examples/targets/python-greeter --request "personalised greeting" \
        --answers "trim yes, reject blanks, /engine claude" --answers /approve
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from itertools import cycle
from pathlib import Path
from typing import Any

from .config import Settings
from .runtime import FactoryRuntime

WORK_VERBS = """Accomplishing
Actioning
Actualizing
Architecting
Baking
Beaming
Beboppin'
Befuddling
Billowing
Blanching
Bloviating
Boogieing
Boondoggling
Booping
Bootstrapping
Brewing
Bunning
Burrowing
Calculating
Canoodling
Caramelizing
Cascading
Catapulting
Cerebrating
Channeling
Channelling
Choreographing
Churning
Clauding
Coalescing
Cogitating
Combobulating
Composing
Computing
Concocting
Considering
Contemplating
Cooking
Crafting
Creating
Crunching
Crystallizing
Cultivating
Deciphering
Deliberating
Determining
Dilly-dallying
Discombobulating
Doing
Doodling
Drizzling
Ebbing
Effecting
Elucidating
Embellishing
Enchanting
Envisioning
Evaporating
Fermenting
Fiddle-faddling
Finagling
Flambéing
Flibbertigibbeting
Flowing
Flummoxing
Fluttering
Forging
Forming
Frolicking
Frosting
Gallivanting
Galloping
Garnishing
Generating
Gesticulating
Germinating
Gitifying
Grooving
Gusting
Harmonizing
Hashing
Hatching
Herding
Honking
Hullaballooing
Hyperspacing
Ideating
Imagining
Improvising
Incubating
Inferring
Infusing
Ionizing
Jitterbugging
Julienning
Kneading
Leavening
Levitating
Lollygagging
Manifesting
Marinating
Meandering
Metamorphosing
Misting
Moonwalking
Moseying
Mulling
Mustering
Musing
Nebulizing
Nesting
Newspapering
Noodling
Nucleating
Orbiting
Orchestrating
Osmosing
Perambulating
Percolating
Perusing
Philosophising
Photosynthesizing
Pollinating
Pondering
Pontificating
Pouncing
Precipitating
Prestidigitating
Processing
Proofing
Propagating
Puttering
Puzzling
Quantumizing
Razzle-dazzling
Razzmatazzing
Recombobulating
Reticulating
Roosting
Ruminating
Sautéing
Scampering
Schlepping
Scurrying
Seasoning
Shenaniganing
Shimmying
Simmering
Skedaddling
Sketching
Slithering
Smooshing
Sock-hopping
Spelunking
Spinning
Sprouting
Stewing
Sublimating
Swirling
Swooping
Symbioting
Synthesizing
Tempering
Thinking
Thundering
Tinkering
Tomfoolering
Topsy-turvying
Transfiguring
Transmuting
Twisting
Undulating
Unfurling
Unravelling
Vibing
Waddling
Wandering
Warping
Whatchamacalliting
Whirlpooling
Whirring
Whisking
Wibbling
Working
Wrangling
Zesting
Zigzagging""".splitlines()


def show(waiting: dict[str, Any]) -> None:
    kind = waiting.get("type")
    print()
    if kind == "questions":
        print("🔥 The factory asks:")
        for q in waiting.get("questions", []):
            print("   " + q.replace("\n", "\n   ") + "\n")
        print("Reply `yes` to accept my recommendations, answer like `1A, 2B`, or in your own words.")
    elif kind == "approval":
        print("📝 Intent preview:")
        if waiting.get("intent_path"):
            print(f"Saved to: {waiting['intent_path']}")
        print(waiting.get("intent_markdown", ""))
        print("📜 Draft contract (tests as data):\n")
        print(waiting.get("contract_markdown", ""))
        if waiting.get("summary"):
            print(f"Summary: {waiting['summary']}")
        for p in waiting.get("problems") or []:
            print(f"   ⚠ critic: {p}")
        print("Reply /approve to freeze it, or say what to change. (/engine copilot|claude|codex to pick the agent)")
    elif kind == "escalation":
        print(f"🚨 Escalation: {waiting.get('reason')}\n{waiting.get('report_markdown', '')}")
        print("Reply: continue | amend <what to change> | abort")
    else:
        print(waiting)


def wait_for_run(rt: FactoryRuntime, key: str, drain: Any) -> None:
    if not sys.stdout.isatty():
        rt.wait(key)
        return
    verbs = cycle(WORK_VERBS)
    width = 24
    while rt.busy(key):
        print("\r" + " " * width + "\r", end="")
        drain(key)
        verb = next(verbs)
        deadline = time.monotonic() + 5
        dots = 0
        while rt.busy(key):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            dots = dots % 3 + 1
            print(f"\r{verb}{'.' * dots}".ljust(width), end="", flush=True)
            time.sleep(min(0.4, remaining))
    print("\r" + " " * width + "\r", end="", flush=True)
    rt.wait(key)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="factory-run")
    ap.add_argument("--repo", required=True, help="git URL or local path of the target repository")
    ap.add_argument("--request", required=True)
    ap.add_argument("--base", default="main")
    ap.add_argument("--engine", choices=["copilot", "claude", "codex"])
    ap.add_argument("--fake", choices=["greeter"], help="use the scripted demo agent instead of a real CLI")
    ap.add_argument("--workspaces", type=Path)
    ap.add_argument("--answers", action="append", default=[], help="scripted replies, consumed in order")
    a = ap.parse_args(argv)

    if a.fake:
        os.environ["FACTORY_FAKE_AGENT"] = a.fake
    if a.workspaces:
        os.environ["FACTORY_WORKSPACES"] = str(a.workspaces)
    elif "FACTORY_WORKSPACES" not in os.environ:
        os.environ["FACTORY_WORKSPACES"] = tempfile.mkdtemp(prefix="factory-")
    rt = FactoryRuntime(Settings())
    seen = 0

    def drain(key: str) -> None:
        nonlocal seen
        for e in rt.services.events.since(key, seen):
            seen = e["seq"]
            if e["kind"] not in ("questions", "approval_needed", "escalation"):
                print(f"[{e['kind']}] {e['message']}")

    key = rt.start(request=a.request, repo_url=a.repo, base_branch=a.base, engine=a.engine)
    wait_for_run(rt, key, drain)
    answers = list(a.answers)
    while True:
        drain(key)
        st = rt.status(key)
        if st["error"]:
            print(f"✗ {st['error']}")
            return 1
        if not st["waiting_for"]:
            break
        show(st["waiting_for"])
        reply = answers.pop(0) if answers else input("\nyou> ")
        if answers or a.answers:
            print(f"\nyou> {reply}")
        rt.reply(key, {"text": reply})
        wait_for_run(rt, key, drain)
    st = rt.status(key)
    print(f"\nstatus: {st['status']}  iterations: {st['iteration']}  PR: {st['pr_url']}")
    print(f"workspace: {rt.services.workspace(key)}")
    return 0 if st["status"] == "pr_opened" else 1


if __name__ == "__main__":
    sys.exit(main())
