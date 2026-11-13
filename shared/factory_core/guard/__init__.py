"""Anti-cheat guard: contract hash-lock and agent-change rules."""

from .changes import FileChange, from_git, parse_patch
from .diffcheck import GuardReport, check_changes
from .manifest import VerifyResult, compute, lock, verify

__all__ = [
    "FileChange",
    "GuardReport",
    "VerifyResult",
    "check_changes",
    "compute",
    "from_git",
    "lock",
    "parse_patch",
    "verify",
]
