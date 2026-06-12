"""
Forward shadow-tracking for the crowd-signal tactic.

Each run snapshots the current useful-state sleeve as a paper position into
`social_signal_history.json`; resolution joins forward prices at 1/5/20/60d to
realized returns. This is the *real* (honest) evaluation track — distinct from
the labeled proxy backtest. Reuses the Crowd Radar history ledger + the
sample-gated social_signal_backtest. Never raises.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from portfolio_automation.portfolio_sim.crowd_tactic import USEFUL_STATES

logger = logging.getLogger("stockbot.portfolio_sim.crowd_forward_track")

_HISTORY_REL = ("outputs", "sandbox", "discovery", "social_signal_history.json")
_OFFSETS = {"1D": 1, "5D": 5, "20D": 20, "60D": 60}


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def _records(root: Path) -> list[dict[str, Any]]:
    doc = _load(root.joinpath(*_HISTORY_REL))
    if isinstance(doc, dict):
        return list(doc.get("records") or [])
    return list(doc or [])


def snapshot_sleeve(root: str | Path, signal_date: str,
                    crowd_states: list[dict[str, Any]], panel) -> list[dict[str, Any]]:
    """
    Append a paper-position record per useful-state name for `signal_date`.
    Idempotent per (ticker, signal_date). Returns the records appended.
    """
    root = Path(root)
    existing = _records(root)
    seen = {(r.get("ticker"), r.get("signal_date")) for r in existing}
    appended: list[dict[str, Any]] = []
    for s in crowd_states:
        if s.get("crowd_state") not in USEFUL_STATES:
            continue
        t = s["ticker"]
        if (t, signal_date) in seen:
            continue
        entry = panel.close(t, signal_date) if panel is not None else None
        if not entry:
            continue
        appended.append({
            "ticker": t, "crowd_state": s["crowd_state"], "signal_date": signal_date,
            "entry_price": entry, "crowd_research_priority_score": s.get("crowd_research_priority_score", 0.0),
            "raw_returns": {}, "returns": {}, "resolved": False,
        })
    return appended


def resolve_records(records: list[dict[str, Any]], panel,
                    benchmark: str = "SPY") -> list[dict[str, Any]]:
    """
    For each unresolved record, compute forward raw returns + excess vs benchmark
    at each offset whose date is available. Look-ahead-safe (forward by offset).
    """
    dates = panel.dates
    didx = {d: i for i, d in enumerate(dates)}
    for rec in records:
        sd = rec.get("signal_date")
        if sd not in didx or rec.get("entry_price") in (None, 0):
            continue
        i0 = didx[sd]
        entry = float(rec["entry_price"])
        bench0 = panel.close(benchmark, sd)
        any_resolved = False
        for label, off in _OFFSETS.items():
            j = i0 + off
            if j >= len(dates):
                continue
            d2 = dates[j]
            p2 = panel.close(rec["ticker"], d2)
            if not p2:
                continue
            r = p2 / entry - 1.0
            rec["raw_returns"][label] = round(r, 6)
            bench2 = panel.close(benchmark, d2)
            if bench0 and bench2 and bench0 > 0:
                rec.setdefault("returns", {})[label] = {
                    "vs_spy": round(r - (bench2 / bench0 - 1.0), 6)}
            any_resolved = True
        rec["resolved"] = any_resolved or rec.get("resolved", False)
    return records
