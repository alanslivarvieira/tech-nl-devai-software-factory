"""Enforce the factory's mutation threshold on reports from stack-native tools.

    python -m factory_core.mutation.score --stryker mutation.json --threshold 70
    python -m factory_core.mutation.score --pit target/pit-reports/mutations.xml --threshold 70
    python -m factory_core.mutation.score --regex 'score is ([0-9.]+)' --text-file out.txt --threshold 70

Paths may be ``glob:<pattern>``; the first match is used.
"""

from __future__ import annotations

import argparse
import glob as globmod
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

KILLED = {"Killed", "Timeout", "RuntimeError", "CompileError"}
COUNTED = KILLED | {"Survived", "NoCoverage"}


def _resolve(path: str) -> Path:
    if path.startswith("glob:"):
        hits = sorted(globmod.glob(path[5:], recursive=True))
        if not hits:
            raise FileNotFoundError(path)
        return Path(hits[0])
    return Path(path)


def stryker_score(path: Path) -> tuple[float, int, list[str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    killed = total = 0
    survivors: list[str] = []
    for fname, f in (data.get("files") or {}).items():
        for m in f.get("mutants", []):
            status = m.get("status")
            if status not in COUNTED:
                continue
            total += 1
            if status in KILLED:
                killed += 1
            else:
                loc = (m.get("location") or {}).get("start", {})
                survivors.append(f"{fname}:{loc.get('line')} {m.get('mutatorName')} -> {m.get('replacement')}")
    return (100.0 if total == 0 else 100.0 * killed / total), total, survivors


def pit_score(path: Path) -> tuple[float, int, list[str]]:
    root = ET.parse(path).getroot()
    muts = root.findall("mutation")
    killed = sum(1 for m in muts if m.get("detected") == "true")
    survivors = [
        f"{m.findtext('sourceFile')}:{m.findtext('lineNumber')} {m.findtext('mutator')}"
        for m in muts
        if m.get("detected") != "true"
    ]
    return (100.0 if not muts else 100.0 * killed / len(muts)), len(muts), survivors


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mutation-score")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--stryker")
    g.add_argument("--pit")
    g.add_argument("--regex")
    ap.add_argument("--text-file")
    ap.add_argument("--threshold", type=float, required=True)
    a = ap.parse_args(argv)
    if a.stryker:
        score, total, survivors = stryker_score(_resolve(a.stryker))
    elif a.pit:
        score, total, survivors = pit_score(_resolve(a.pit))
    else:
        text = _resolve(a.text_file).read_text(encoding="utf-8") if a.text_file else sys.stdin.read()
        m = re.search(a.regex, text)
        if not m:
            print("mutation-score: score not found in output")
            return 2
        score = float(m.group(1))
        score = score * 100 if score <= 1.0 else score
        total, survivors = -1, []
    print(f"mutation score {score:.1f}% over {total} mutants, threshold {a.threshold}%")
    for s in survivors[:20]:
        print(f"  survived {s}")
    return 0 if score >= a.threshold else 1


if __name__ == "__main__":
    sys.exit(main())
