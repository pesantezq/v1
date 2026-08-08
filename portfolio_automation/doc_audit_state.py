"""Committed, cross-workstation state for the doc auditor. Lives in .agent/ (a
tracked dir) so it travels via git; the last-audited SHA lets any workstation
derive 'what changed since last audit' from git diff."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import yaml

_DEFAULTS = {"last_audited_sha": None, "last_run_at": None,
             "apply_enabled": True, "fixes_last_run": 0,
             # Coverage gaps still unresolved from earlier audits. Carried across
             # runs because find_coverage_gaps only sees the current git range,
             # so an advancing last_audited_sha would otherwise erase them.
             "open_coverage_gaps": [],
             # doc path -> ISO timestamp the gap was FIRST seen open. Lets the
             # daily check escalate a gap nobody ever closed; a carried gap is
             # otherwise indistinguishable on day 1 and day 90.
             "coverage_gap_first_seen": {}}


def state_path(root: str) -> str:
    return str(Path(root) / ".agent" / "doc_audit_state.yaml")


def load_state(root: str) -> dict:
    # deepcopy, not dict(): _DEFAULTS holds a list and a dict, and `{**_DEFAULTS}`
    # copies only the outer mapping — callers would share (and could mutate) the
    # module-level containers, poisoning every later load in the process.
    p = Path(state_path(root))
    if not p.exists():
        return deepcopy(_DEFAULTS)
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return deepcopy(_DEFAULTS)
    return {**deepcopy(_DEFAULTS), **data}


def save_state(root: str, state: dict) -> None:
    p = Path(state_path(root))
    p.parent.mkdir(parents=True, exist_ok=True)
    merged = {**deepcopy(_DEFAULTS), **state}
    p.write_text(yaml.safe_dump(merged, sort_keys=True), encoding="utf-8")
