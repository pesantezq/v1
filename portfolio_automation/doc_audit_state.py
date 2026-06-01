"""Committed, cross-workstation state for the doc auditor. Lives in .agent/ (a
tracked dir) so it travels via git; the last-audited SHA lets any workstation
derive 'what changed since last audit' from git diff."""
from __future__ import annotations

from pathlib import Path
import yaml

_DEFAULTS = {"last_audited_sha": None, "last_run_at": None,
             "apply_enabled": True, "fixes_last_run": 0}


def state_path(root: str) -> str:
    return str(Path(root) / ".agent" / "doc_audit_state.yaml")


def load_state(root: str) -> dict:
    p = Path(state_path(root))
    if not p.exists():
        return dict(_DEFAULTS)
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return dict(_DEFAULTS)
    return {**_DEFAULTS, **data}


def save_state(root: str, state: dict) -> None:
    p = Path(state_path(root))
    p.parent.mkdir(parents=True, exist_ok=True)
    merged = {**_DEFAULTS, **state}
    p.write_text(yaml.safe_dump(merged, sort_keys=True), encoding="utf-8")
