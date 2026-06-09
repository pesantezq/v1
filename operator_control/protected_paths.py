"""Pure classifier for repo paths a worker must never modify.

Used by worker_runner as a deterministic post-run guard: if a worker's diff
touches any protected path, the run is quarantined regardless of what the
worker claimed. No I/O — just string classification.
"""
from __future__ import annotations

_PROTECTED_EXACT = {
    "decision_engine.py", "portfolio_decision_engine.py", "scoring.py",
    "config.json", "requirements.txt", "config/signal_registry.yaml",
}
_PROTECTED_BASENAMES = {
    "decision_engine.py", "portfolio_decision_engine.py", "scoring.py",
    "config.json", "requirements.txt",
}
_PROTECTED_DIR_PREFIXES = (
    ".claude/", "deploy/", "portfolio_automation/brokers/",
)


def is_protected(path: str) -> bool:
    norm = str(path).replace("\\", "/")
    if norm.startswith("./"):
        norm = norm[2:]
    if norm in _PROTECTED_EXACT:
        return True
    base = norm.rsplit("/", 1)[-1]
    if base in _PROTECTED_BASENAMES:
        return True
    if any(norm.startswith(p) for p in _PROTECTED_DIR_PREFIXES):
        return True
    if base.startswith(".env"):
        return True
    if base.endswith(".service"):
        return True
    return False


def violating_paths(paths) -> list[str]:
    return [p for p in paths if is_protected(p)]


__all__ = ["is_protected", "violating_paths"]
