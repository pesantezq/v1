"""
Simulation-ONLY bounded decision-engine context overlay.

Tests whether ETF-bundle context could improve selection quality — WITHOUT
touching production. This module:

  * imports nothing from decision_engine / scoring — the production engine is
    byte-for-byte unchanged and receives no new signal;
  * emits a bounded contextual signal (|modifier| <= 0.05, clamped at apply
    time, mirroring portfolio_sim.crowd_tactic.apply_sentiment_tilt);
  * NEVER creates an action (no BUY/SELL/HOLD/allocation), never overrides risk
    gates, and writes only to the SIMULATION namespace;
  * provides a baseline-vs-overlay A/B comparison over simulated rows.

feeds_decision_engine stays False. Exercised only in simulation/backtesting.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import OutputNamespace, safe_write_json
from portfolio_automation.weekly_etf_bundles.evaluation import spearman

logger = logging.getLogger("stockbot.weekly_etf_bundles.engine_overlay")

# Absolute cap on the context modifier. Mirrors the social-sentiment ±0.05 bound.
MAX_CONTEXT_MODIFIER = 0.05
_SOURCE = "weekly_etf_bundle_context"


def clamp_modifier(modifier: float, max_modifier: float = MAX_CONTEXT_MODIFIER) -> float:
    """Hard clamp to [-max_modifier, +max_modifier]. The bound is enforced here
    so no caller can exceed it regardless of the requested value."""
    return max(-abs(max_modifier), min(abs(max_modifier), float(modifier)))


def bundle_context_signal(
    symbol: str,
    analysis_payload: dict[str, Any],
    *,
    max_modifier: float = MAX_CONTEXT_MODIFIER,
) -> dict[str, Any]:
    """Bounded, simulation-only context signal for one symbol. Returns a zero
    modifier if the symbol is not a member of any enabled bundle."""
    symbol = symbol.upper()
    related: list[str] = []
    best_score: float | None = None
    best_breadth: float | None = None
    for b in analysis_payload.get("bundles", []):
        member_syms = {m.get("symbol") for m in b.get("members", [])}
        if symbol in member_syms:
            related.append(b.get("bundle_id"))
            bs = b.get("bundle_score")
            if bs is not None and (best_score is None or bs > best_score):
                best_score = bs
                best_breadth = b.get("pct_above_sma50")

    if best_score is None:
        return {
            "symbol": symbol, "related_etf_bundles": related,
            "bundle_context_score": None, "bundle_breadth": None,
            "context_modifier": 0.0, "maximum_allowed_modifier": abs(max_modifier),
            "source": _SOURCE, "simulation_only": True,
        }

    context_score = max(0.0, min(1.0, best_score / 100.0))
    breadth = best_breadth if best_breadth is not None else 0.5
    # Center at 0.5; scale by breadth so narrow leadership is damped; clamp.
    raw = (context_score - 0.5) * 2.0 * abs(max_modifier) * float(breadth)
    modifier = clamp_modifier(raw, max_modifier)
    return {
        "symbol": symbol,
        "related_etf_bundles": related,
        "bundle_context_score": round(context_score, 4),
        "bundle_breadth": round(float(breadth), 4),
        "context_modifier": round(modifier, 4),
        "maximum_allowed_modifier": abs(max_modifier),
        "source": _SOURCE,
        "simulation_only": True,
    }


def apply_context_modifier(
    baseline_score: float,
    modifier: float,
    *,
    max_modifier: float = MAX_CONTEXT_MODIFIER,
) -> float:
    """Apply the bounded modifier to a COPY of a baseline score (multiplicative,
    so the effect is at most ±max_modifier). Returns a NUMBER — never an action,
    allocation, or decision. Pure; does not touch any production state."""
    m = clamp_modifier(modifier, max_modifier)
    return float(baseline_score) * (1.0 + m)


def run_overlay_comparison(
    sim_rows: list[dict[str, Any]],
    analysis_payload: dict[str, Any],
    *,
    max_modifier: float = MAX_CONTEXT_MODIFIER,
    top_k: int = 5,
) -> dict[str, Any]:
    """Simulation-only A/B: rank the same sim rows by baseline_score vs by the
    ETF-context-adjusted score and compare top-k selection quality. Each row:
    {symbol, baseline_score, forward_return}. Answers 'does ETF context improve
    the ranking out of sample?' without ever touching the production engine."""
    rows = [r for r in sim_rows
            if r.get("baseline_score") is not None and r.get("forward_return") is not None]
    if not rows:
        return {"available": False, "reason": "no_sim_rows", "simulation_only": True,
                "feeds_decision_engine": False}

    enriched = []
    for r in rows:
        sig = bundle_context_signal(r["symbol"], analysis_payload, max_modifier=max_modifier)
        enriched.append({
            **r,
            "context_modifier": sig["context_modifier"],
            "overlay_score": apply_context_modifier(r["baseline_score"], sig["context_modifier"],
                                                    max_modifier=max_modifier),
        })

    def _topk_stats(score_key: str) -> dict[str, Any]:
        ranked = sorted(enriched, key=lambda r: (-r[score_key], r["symbol"]))[:top_k]
        rets = [r["forward_return"] for r in ranked]
        return {
            "avg_forward_return": round(sum(rets) / len(rets), 6) if rets else None,
            "hit_rate": round(sum(1 for x in rets if x > 0) / len(rets), 4) if rets else None,
            "selected": [r["symbol"] for r in ranked],
        }

    baseline = _topk_stats("baseline_score")
    overlay = _topk_stats("overlay_score")
    incr_return = (overlay["avg_forward_return"] - baseline["avg_forward_return"]
                   if (overlay["avg_forward_return"] is not None
                       and baseline["avg_forward_return"] is not None) else None)
    rank_corr = spearman([r["baseline_score"] for r in enriched],
                         [r["overlay_score"] for r in enriched])

    return {
        "available": True,
        "simulation_only": True,
        "feeds_decision_engine": False,
        "observe_only": True,
        "max_modifier": abs(max_modifier),
        "top_k": top_k,
        "n_rows": len(enriched),
        "baseline": baseline,
        "overlay": overlay,
        "incremental_avg_forward_return": incr_return,
        "baseline_overlay_rank_correlation": round(rank_corr, 4) if rank_corr is not None else None,
        "note": "Simulation-only ranking A/B. The context modifier is bounded to "
                "±max_modifier and never feeds the production decision engine. "
                "Full portfolio CAGR/Sharpe A/B requires the portfolio_sim engine "
                "(future extension); this compares selection quality only.",
    }


def write_overlay_comparison(comparison: dict[str, Any], *, root: str | Path) -> str:
    """Write to the SIMULATION namespace only (never production/latest)."""
    root_path = Path(root).resolve()
    doc = {**comparison, "generated_at": datetime.now(timezone.utc).isoformat(),
           "source": _SOURCE}
    path = safe_write_json(OutputNamespace.SIMULATION,
                           "weekly_etf_bundle_engine_overlay.json", doc,
                           base_dir=root_path / "outputs")
    return str(path)
