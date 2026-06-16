"""
Production overlay loaders (spec §8 watchlist, §9 advisory).

These are the *production* side of the two-lane model. They take a baseline
(what production would do on its own) and apply ONLY the approved-proposal
overlay artifacts written by production_application. They are pure transforms at
the INPUT boundary — they never call or modify decision_engine / scoring logic
(protected semantics), exactly like the broker-overlay pattern.

What they ignore, by construction:
  * raw simulation artifacts (they read only the approved overlay files)
  * pending proposals (never written into the overlay)
  * rejected proposals (never written into the overlay)
  * simulation-only items

Each loader is gated by a config flag (default OFF) so that turning on live
production effect is the final, explicit human step. When the flag is off the
loader is a no-op and returns the baseline unchanged.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from portfolio_automation.data_governance import OutputNamespace, get_output_path
from portfolio_automation.sim_governance import schemas as S
from portfolio_automation.sim_governance.production_application import (
    ADVISORY_OVERLAY,
    WATCHLIST_OVERLAY,
)

logger = logging.getLogger("stockbot.sim_governance.production_overlays")


def _load_overlay(filename: str, base_dir: str) -> dict:
    path = Path(get_output_path(OutputNamespace.LATEST, filename, base_dir=base_dir))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("feeds_production"):
            return data
    except Exception:
        pass
    return {"ops": [], "applied_proposal_ids": []}


# ---------------------------------------------------------------------------
# Watchlist (spec §8)
# ---------------------------------------------------------------------------


def apply_approved_watchlist(baseline_watchlist: list[str], overlay: dict) -> dict:
    """Apply approved watchlist ops (add/remove/rank/tag) to a baseline.

    Returns {"watchlist": [...], "ranks": {sym: rank}, "tags": {sym: [..]},
    "applied_proposal_ids": [...]}. Pure; deterministic.
    """
    watchlist = [str(t).upper() for t in (baseline_watchlist or [])]
    ranks: dict[str, int] = {}
    tags: dict[str, list[str]] = {}
    applied: list[str] = []

    for op in overlay.get("ops", []) or []:
        change = op.get("change", {}) or {}
        ptype = op.get("proposal_type")
        sym = str(change.get("symbol", "")).upper()
        if not sym:
            continue
        if ptype in (S.PROPOSAL_WATCHLIST_ADD, S.PROPOSAL_DISCOVERY_PROMOTION):
            if sym not in watchlist:
                watchlist.append(sym)
            if change.get("tags"):
                tags[sym] = list(change["tags"])
            if change.get("rank") is not None:
                ranks[sym] = int(change["rank"])
        elif ptype == S.PROPOSAL_WATCHLIST_REMOVE:
            if sym in watchlist:
                watchlist.remove(sym)
        elif ptype == S.PROPOSAL_WATCHLIST_RANK:
            if change.get("rank") is not None:
                ranks[sym] = int(change["rank"])
        elif ptype == S.PROPOSAL_WATCHLIST_TAG:
            tags[sym] = list(change.get("tags", []))
        applied.append(op.get("proposal_id"))

    return {
        "watchlist": watchlist,
        "ranks": ranks,
        "tags": tags,
        "applied_proposal_ids": [a for a in applied if a],
    }


def load_production_watchlist(
    baseline_watchlist: list[str],
    *,
    base_dir: str,
    enabled: bool,
) -> dict:
    """Production watchlist = baseline + approved overlay (when enabled).

    When ``enabled`` is False this is a no-op returning the baseline unchanged,
    so production behavior does not change until the operator flips the flag.
    """
    if not enabled:
        return {"watchlist": [str(t).upper() for t in (baseline_watchlist or [])],
                "ranks": {}, "tags": {}, "applied_proposal_ids": [], "overlay_enabled": False}
    overlay = _load_overlay(WATCHLIST_OVERLAY, base_dir)
    result = apply_approved_watchlist(baseline_watchlist, overlay)
    result["overlay_enabled"] = True
    return result


# ---------------------------------------------------------------------------
# Advisory (spec §9) — input-boundary overlay; decision_engine untouched.
# ---------------------------------------------------------------------------


def apply_approved_advisory(baseline_advisory: list[dict], overlay: dict) -> dict:
    """Apply approved advisory ops (context/ranking/strategy) to a baseline.

    Operates only on advisory *context/annotation* fields and ranking hints —
    never on signal_score / confidence / scoring fields. Returns
    {"advisory": [...], "applied_proposal_ids": [...]}.
    """
    by_symbol: dict[str, dict] = {}
    order: list[str] = []
    for pick in baseline_advisory or []:
        sym = str(pick.get("symbol", "")).upper()
        if not sym:
            continue
        by_symbol[sym] = dict(pick)
        order.append(sym)

    applied: list[str] = []
    for op in overlay.get("ops", []) or []:
        change = op.get("change", {}) or {}
        ptype = op.get("proposal_type")
        sym = str(change.get("symbol", "")).upper()
        if not sym:
            continue
        rec = by_symbol.setdefault(sym, {"symbol": sym})
        if sym not in order:
            order.append(sym)
        if ptype in (S.PROPOSAL_CROWD_CONTEXT, S.PROPOSAL_ADVISORY_CONTEXT):
            rec["overlay_context"] = change.get("crowd_context") or change.get("context")
        elif ptype == S.PROPOSAL_ADVISORY_RANKING:
            rec["overlay_rank_hint"] = change.get("rank")
        elif ptype == S.PROPOSAL_ADVISORY_STRATEGY:
            rec["overlay_strategy"] = change.get("strategy")
        rec["overlay_proposal_id"] = op.get("proposal_id")
        applied.append(op.get("proposal_id"))

    return {
        "advisory": [by_symbol[s] for s in order],
        "applied_proposal_ids": [a for a in applied if a],
    }


def load_production_advisory(
    baseline_advisory: list[dict],
    *,
    base_dir: str,
    enabled: bool,
) -> dict:
    """Production advisory = baseline + approved overlay (when enabled)."""
    if not enabled:
        return {"advisory": list(baseline_advisory or []),
                "applied_proposal_ids": [], "overlay_enabled": False}
    overlay = _load_overlay(ADVISORY_OVERLAY, base_dir)
    result = apply_approved_advisory(baseline_advisory, overlay)
    result["overlay_enabled"] = True
    return result
