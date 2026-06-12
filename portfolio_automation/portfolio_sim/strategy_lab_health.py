"""
Strategy Lab health assessor — confirms the research lab ran and is producing
trustworthy output. Read-only; returns GREEN/AMBER/RED with reasons. Consumed by
the /strategy-lab-analysis skill and monthly-tool-analysis.

RED   = the lab is broken or surfacing untrustworthy results.
AMBER = degraded but non-fatal (disabled, stale, factor data missing, OOS-failing tactic).
GREEN = ran, populated, documented, no failing-OOS tactic surfaced.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SANDBOX = ("outputs", "sandbox")


def _load(root: Path, name: str) -> dict[str, Any] | None:
    try:
        return json.loads(root.joinpath(*_SANDBOX, name).read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def _age_hours(iso: str | None, now: datetime) -> float | None:
    if not iso:
        return None
    try:
        ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (now - ts).total_seconds() / 3600.0
    except Exception:
        return None


def assess_strategy_lab_health(root: str | Path = ".", now: datetime | None = None) -> dict[str, Any]:
    """Return {status, reasons[], signals{}} for the research strategy lab."""
    root = Path(root)
    now = now or datetime.now(timezone.utc)
    reasons: list[str] = []
    signals: dict[str, Any] = {}

    lb = _load(root, "strategy_leaderboard.json")
    cat = _load(root, "research_strategy_catalog.json")
    wf = _load(root, "walk_forward_results.json")
    factor = _load(root, "factor_exposure_report.json")

    if lb is None:
        return {"status": "AMBER", "reasons": ["leaderboard_absent (lab not yet run / disabled)"],
                "signals": {"present": False}}

    status_val = lb.get("status")
    signals["lab_status"] = status_val
    if status_val == "disabled":
        return {"status": "AMBER", "reasons": ["strategy_lab_disabled (inert steady state)"],
                "signals": {"present": True, "lab_status": "disabled"}}

    rows = lb.get("leaderboard") or []
    signals["tactic_count"] = len(rows)
    age = _age_hours(lb.get("created_at"), now)
    signals["age_hours"] = round(age, 1) if age is not None else None

    # RED conditions
    if status_val == "ok" and not rows:
        reasons.append("looks_fresh_but_empty: status ok but zero tactics scored")
    # AMBER conditions
    if status_val == "insufficient_data":
        reasons.append("insufficient_data: price panel/history too thin")
    if age is not None and age > 24 * 8:   # weekly cadence → stale past ~8 days
        reasons.append(f"stale: leaderboard {age/24:.1f}d old (weekly cadence)")

    # documentation coverage gate (Strategy Documentation Requirement)
    coverage = (cat or {}).get("coverage_complete")
    signals["coverage_complete"] = coverage
    if cat is not None and coverage is False:
        reasons.append(f"undocumented_tactics: {(cat or {}).get('undocumented')}")

    # walk-forward presence + any tactic failing OOS that is still surfaced
    signals["walk_forward_present"] = wf is not None
    failing_oos = [r["tactic_id"] for r in rows if r.get("still_works_oos") is False]
    signals["failing_oos"] = failing_oos
    if failing_oos:
        reasons.append(f"tactic(s) surfaced with still_works_oos=false: {failing_oos[:5]}")

    # factor data availability (AMBER only)
    signals["factor_data_available"] = bool((factor or {}).get("factor_data_available"))
    if factor is not None and not signals["factor_data_available"]:
        reasons.append("factor_data_unavailable (run scripts/fetch_factor_data.sh)")

    # classify
    red = any("looks_fresh_but_empty" in r for r in reasons)
    if red:
        status = "RED"
    elif reasons:
        status = "AMBER"
    else:
        status = "GREEN"
        reasons.append("lab healthy: ran, populated, documented, no failing-OOS tactic surfaced")
    # headline
    if rows:
        top = rows[0]
        signals["top_tactic"] = top.get("name")
        signals["top_score"] = top.get("strategy_score")
        signals["top_excess_vs_spy"] = top.get("mean_excess_vs_spy")
    return {"status": status, "reasons": reasons, "signals": signals}
