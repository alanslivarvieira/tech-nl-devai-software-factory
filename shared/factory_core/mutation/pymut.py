"""pymut — a small, factory-owned AST mutation tester for Python.

Factory-owned on purpose: the target repo cannot configure it away (no config file is read), and it kills
mutants with BOTH the repo's unit tests and the tests-as-data contract (``--tad-spec``).

    python -m factory_core.mutation.pymut --files src/a.py [--diff-base SHA] [--tad-spec DIR]
        [--threshold 70] [--max-mutants 150] [--json out.json] -- python -m pytest -q -x
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import copy
import json
import random
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path

BINOP_SWAP = {
    ast.Add: ast.Sub,
    ast.Sub: ast.Add,
    ast.Mult: ast.FloorDiv,
    ast.Div: ast.Mult,
    ast.FloorDiv: ast.Mult,
    ast.Mod: ast.Mult,
}
CMP_SWAP = {
    ast.Lt: ast.LtE,
    ast.LtE: ast.Lt,
    ast.Gt: ast.GtE,
    ast.GtE: ast.Gt,
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
}


@dataclass
class Mutant:
    file: str
    index: int
    line: int
    col: int
    operator: str
    original: str
    mutated: str
    status: str = "pending"  # killed | survived | timeout


class _Walker(ast.NodeTransformer):
    """Enumerates mutation sites in a deterministic order; mutates the ``target``-th one if given."""

    def __init__(self, target: int = -1, lines: set[int] | None = None, skip_lines: set[int] | None = None):
        self.target = target
        self.lines = lines
        self.skip_lines = skip_lines or set()
        self.count = 0
        self.sites: list[tuple[int, int, str, str, str]] = []
        self.applied: tuple[int, int, str, str, str] | None = None

    def _eligible(self, node: ast.AST) -> bool:
        ln = getattr(node, "lineno", None)
        if ln is None or ln in self.skip_lines:
            return False
        return self.lines is None or ln in self.lines

    def _site(self, node: ast.AST, op: str, before: ast.AST, after: ast.AST) -> bool:
        """Register a site; return True when this site is the one to apply."""
        info = (node.lineno, node.col_offset, op, _src(before), _src(after))  # type: ignore[attr-defined]
        self.sites.append(info)
        hit = self.count == self.target
        if hit:
            self.applied = info
        self.count += 1
        return hit

    # -- operators -------------------------------------------------------------------------------
    def visit_BinOp(self, node: ast.BinOp) -> ast.AST:
        self.generic_visit(node)
        swap = BINOP_SWAP.get(type(node.op))
        if swap and self._eligible(node):
            new = copy.copy(node)
            new.op = swap()
            if self._site(node, "binop", node, new):
                return new
        return node

    def visit_Compare(self, node: ast.Compare) -> ast.AST:
        self.generic_visit(node)
        if self._eligible(node):
            for i, op in enumerate(node.ops):
                swap = CMP_SWAP.get(type(op))
                if not swap:
                    continue
                new = copy.copy(node)
                new.ops = list(node.ops)
                new.ops[i] = swap()
                if self._site(node, "compare", node, new):
                    return new
        return node

    def visit_BoolOp(self, node: ast.BoolOp) -> ast.AST:
        self.generic_visit(node)
        if self._eligible(node):
            new = copy.copy(node)
            new.op = ast.Or() if isinstance(node.op, ast.And) else ast.And()
            if self._site(node, "boolop", node, new):
                return new
        return node

    def visit_UnaryOp(self, node: ast.UnaryOp) -> ast.AST:
        self.generic_visit(node)
        if isinstance(node.op, ast.Not) and self._eligible(node):
            if self._site(node, "remove-not", node, node.operand):
                return node.operand
        return node

    def visit_If(self, node: ast.If) -> ast.AST:
        self.generic_visit(node)
        if self._eligible(node):
            neg = ast.UnaryOp(op=ast.Not(), operand=node.test)
            if self._site(node, "negate-if", node.test, neg):
                new = copy.copy(node)
                new.test = neg
                return new
        return node

    def visit_Return(self, node: ast.Return) -> ast.AST:
        self.generic_visit(node)
        if (
            node.value is not None
            and not (isinstance(node.value, ast.Constant) and node.value.value is None)
            and self._eligible(node)
        ):
            new = ast.Return(value=ast.Constant(value=None))
            if self._site(node, "return-none", node, new):
                return ast.copy_location(new, node)
        return node

    def visit_Expr(self, node: ast.Expr) -> ast.AST:
        # Docstrings / bare string statements are not behaviour; skip their constants.
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return node
        self.generic_visit(node)
        return node

    def visit_JoinedStr(self, node: ast.JoinedStr) -> ast.AST:
        # Mutate formatted values' expressions but not the literal fragments of f-strings.
        for v in node.values:
            if isinstance(v, ast.FormattedValue):
                v.value = self.visit(v.value)
        return node

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        if not self._eligible(node):
            return node
        v = node.value
        if isinstance(v, bool):
            new_v: object = not v
        elif isinstance(v, int):
            new_v = v + 1
        elif isinstance(v, str):
            new_v = "" if v else "mutant"
        else:
            return node
        new = ast.Constant(value=new_v)
        if self._site(node, "constant", node, new):
            return ast.copy_location(new, node)
        return node


def _src(node: ast.AST) -> str:
    try:
        return ast.unparse(node)[:80]
    except Exception:
        return type(node).__name__


def _skip_lines(text: str) -> set[int]:
    return {i for i, line in enumerate(text.splitlines(), start=1) if "pragma: no mutate" in line}


def changed_lines(file: str, base: str) -> set[int]:
    out = subprocess.run(
        ["git", "diff", "-U0", base, "--", file], capture_output=True, text=True, encoding="utf-8", errors="replace"
    ).stdout
    lines: set[int] = set()
    for ln in out.splitlines():
        if ln.startswith("@@"):
            new = ln.split("+", 1)[1].split(" ", 1)[0]
            start, _, count = new.partition(",")
            n = int(count) if count else 1
            lines.update(range(int(start), int(start) + n))
    return lines


def enumerate_mutants(path: Path, lines: set[int] | None) -> list[Mutant]:
    text = path.read_text(encoding="utf-8")
    walker = _Walker(lines=lines, skip_lines=_skip_lines(text))
    walker.visit(ast.parse(text))
    return [Mutant(str(path), i, *site) for i, site in enumerate(walker.sites)]


def mutated_source(path: Path, mutant: Mutant, lines: set[int] | None) -> str:
    text = path.read_text(encoding="utf-8")
    walker = _Walker(target=mutant.index, lines=lines, skip_lines=_skip_lines(text))
    tree = ast.fix_missing_locations(walker.visit(ast.parse(text)))
    return ast.unparse(tree) + "\n"


BACKUP_SUFFIX = ".pymut-orig"


def _backup(path: Path) -> Path:
    return path.with_name(path.name + BACKUP_SUFFIX)


@contextlib.contextmanager
def mutated(path: Path, source: str) -> Iterator[None]:
    """Swap in a mutant; the original is restored byte-for-byte even on errors or Ctrl-C. A byte-exact backup
    is written first, so a hard kill (which skips ``finally``) is repaired by :func:`recover` on the next run."""
    backup = _backup(path)
    original = path.read_bytes()
    backup.write_bytes(original)
    try:
        path.write_text(source, encoding="utf-8")
        yield
    finally:
        path.write_bytes(original)
        backup.unlink(missing_ok=True)


def recover(root: Path) -> list[Path]:
    """Restore any source left mutated by a killed run."""
    restored = []
    for backup in root.rglob(f"*.py{BACKUP_SUFFIX}"):
        target = backup.with_name(backup.name[: -len(BACKUP_SUFFIX)])
        target.write_bytes(backup.read_bytes())
        backup.unlink()
        restored.append(target)
    return restored


def _interrupt(signum: int, _frame: object) -> None:
    raise KeyboardInterrupt(f"signal {signum}")  # unwinds through ``mutated`` → original restored


def _run(cmds: list[list[str]], timeout: float) -> str:
    for cmd in cmds:
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return "timeout"
        if proc.returncode != 0:
            return "killed"
    return "survived"


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    test_cmd: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, test_cmd = argv[:i], argv[i + 1 :]
    ap = argparse.ArgumentParser(prog="pymut")
    ap.add_argument("--files", nargs="*", default=[])
    ap.add_argument("--diff-base")
    ap.add_argument("--tad-spec", type=Path)
    ap.add_argument("--threshold", type=float, default=70)
    ap.add_argument("--max-mutants", type=int, default=150)
    ap.add_argument("--json", type=Path)
    ap.add_argument("--seed", type=int, default=1234)
    a = ap.parse_args(argv)

    cmds: list[list[str]] = []
    if test_cmd:
        cmds.append(test_cmd)
    if a.tad_spec:
        cmds.append([sys.executable, "-m", "factory_core.tad_runner.cli", "run", str(a.tad_spec), "--repo", "."])
    if not cmds:
        print("pymut: give a test command after '--' and/or --tad-spec", file=sys.stderr)
        return 2

    files = [Path(f) for f in a.files if f.endswith(".py") and Path(f).exists()]
    for restored in recover(Path(".")):
        print(f"pymut: restored {restored} left mutated by an interrupted run")
    signal.signal(signal.SIGTERM, _interrupt)
    started = time.monotonic()
    baseline = _run(cmds, timeout=1800)
    if baseline != "survived":
        print(f"pymut: test suite does not pass on unmutated code ({baseline}); cannot measure mutation score")
        return 2
    per_run_timeout = max(10.0, (time.monotonic() - started) * 3 + 5)

    mutants: list[tuple[Mutant, set[int] | None]] = []
    for f in files:
        lines = changed_lines(str(f), a.diff_base) if a.diff_base else None
        mutants += [(m, lines) for m in enumerate_mutants(f, lines)]
    if len(mutants) > a.max_mutants:
        random.Random(a.seed).shuffle(mutants)
        mutants = sorted(mutants[: a.max_mutants], key=lambda t: (t[0].file, t[0].index))

    for m, lines in mutants:
        path = Path(m.file)
        with mutated(path, mutated_source(path, m, lines)):
            m.status = _run(cmds, per_run_timeout)

    killed = sum(1 for m, _ in mutants if m.status in ("killed", "timeout"))
    total = len(mutants)
    score = 100.0 if total == 0 else round(100.0 * killed / total, 1)
    survivors = [asdict(m) for m, _ in mutants if m.status == "survived"]
    result = {
        "score": score,
        "threshold": a.threshold,
        "total": total,
        "killed": killed,
        "survived": len(survivors),
        "survivors": survivors,
    }
    if a.json:
        a.json.parent.mkdir(parents=True, exist_ok=True)
        a.json.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"pymut: mutation score {score}% ({killed}/{total} killed), threshold {a.threshold}%")
    for s in survivors[:20]:
        print(f"  survived {s['file']}:{s['line']} [{s['operator']}] {s['original']!r} -> {s['mutated']!r}")
    return 0 if score >= a.threshold else 1


if __name__ == "__main__":
    sys.exit(main())
