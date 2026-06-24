"""Strategy Lab / next-stage dashboard loader (Phase 2 + 12-13, spec §12/§24.6).

Read-only loader that surfaces the next-stage artifacts (opportunity radar,
strategy comparison + review queue, shadow portfolios, system-improvement ideas,
approval queues, broker-aware side-panel, market-opportunity review cards). Every
read is defensive — missing/malformed artifacts render a "not yet produced" card
rather than failing (Phase 2 tolerate-absent requirement).

Observe-only. Surfaces approve/blocked actions and generated prompts for COPY only
(launch center) — it executes nothing.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from gui_v2.data.shared import card
from gui_v2.data.dash_simulation_charts import collect_simulation_charts_view


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except Exception:
        return None


def collect_strategy_lab_view(root: Path) -> dict[str, Any]:
    sb = root / "outputs" / "sandbox"
    latest = root / "outputs" / "latest"
    portfolio = root / "outputs" / "portfolio"

    radar = _load(sb / "opportunity_radar.json") or {}
    comparison = _load(sb / "strategy_comparison.json") or {}
    review_q = _load(latest / "strategy_review_queue.json") or {}
    active_sel = _load(root / "outputs" / "policy" / "active_strategy_selection.json") or {}
    shadow = _load(sb / "shadow_portfolios.json") or {}
    ideas = _load(latest / "system_improvement_ideas.json") or {}
    imp_q = _load(latest / "system_improvement_action_queue.json") or {}
    opp_q = _load(latest / "operator_action_queue.json") or {}
    broker = _load(portfolio / "broker_aware_portfolio.json") or {}
    cards_review = _load(sb / "market_opportunity_review_cards.json") or {}
    backtest = _load(sb / "portfolio_backtest.json") or {}
    projection = _load(sb / "portfolio_projection.json") or {}
    strategy_lab = _load(sb / "strategy_leaderboard.json") or {}

    def _n(d, key):  # safe count
        v = d.get(key)
        return len(v) if isinstance(v, list) else (v if isinstance(v, int) else 0)

    cards = []
    # Opportunity radar
    opp_count = _n(radar, "opportunities")
    cards.append(card("Opportunity Radar",
                      status="info" if opp_count else "unknown",
                      label=f"{opp_count} candidates" if opp_count else "not yet produced",
                      summary="Scored opportunities across the universe (sandbox, observe-only).",
                      source_artifacts=["opportunity_radar.json"],
                      updated_at=radar.get("generated_at")))
    # Strategy comparison
    metrics = comparison.get("comparison", []) if isinstance(comparison, dict) else []
    top = metrics[0]["name"] if metrics else None
    cards.append(card("Strategy Lab",
                      status="info" if metrics else "unknown",
                      label=(f"top: {top}" if top else "not yet produced"),
                      summary="Multi-strategy objective comparison (advisory, sandbox).",
                      source_artifacts=["strategy_comparison.json"],
                      updated_at=comparison.get("generated_at")))
    # Shadow portfolios
    sp = shadow.get("portfolios", {}) if isinstance(shadow, dict) else {}
    cards.append(card("Shadow Portfolios",
                      status="info" if sp else "unknown",
                      label=f"{len(sp)} models" if sp else "not yet produced",
                      summary="Simulated shadow portfolios (no real positions).",
                      source_artifacts=["shadow_portfolios.json"],
                      updated_at=shadow.get("generated_at")))
    # System improvement
    ic = _n(ideas, "ideas")
    cards.append(card("System Improvement",
                      status="info" if ic else "unknown",
                      label=f"{ic} ideas" if ic else "none today",
                      summary="Daily engineering/ops improvement backlog (not market advice).",
                      source_artifacts=["system_improvement_ideas.json"],
                      updated_at=ideas.get("generated_at")))
    # Approval queues
    opp_open = _n(opp_q, "queue")
    imp_open = _n(imp_q, "queue")
    cards.append(card("Approval Queues",
                      status="warning" if (opp_open + imp_open) else "ok",
                      label=f"{opp_open} opp · {imp_open} sys" if (opp_open + imp_open) else "all clear",
                      summary="Artifact-based review items — approving executes nothing.",
                      source_artifacts=["operator_action_queue.json",
                                        "system_improvement_action_queue.json"]))
    # Broker-aware side-panel
    bsrc = broker.get("holdings_source") if isinstance(broker, dict) else None
    cards.append(card("Broker-Aware Portfolio",
                      status="info" if bsrc else "unknown",
                      label=(f"source: {bsrc}" if bsrc else "not produced"),
                      summary="Actual-vs-config holdings (read-only side-panel; never feeds decisions).",
                      source_artifacts=["broker_aware_portfolio.json"],
                      updated_at=broker.get("generated_at")))

    return {
        "persona": "strategy_lab",
        "observe_only": True,
        "cards": cards,
        "radar": (radar.get("opportunities", []) or [])[:12],
        "strategies": metrics,
        "strategy_review_queue": review_q.get("queue", []) if isinstance(review_q, dict) else [],
        "active_strategy_id": active_sel.get("active_strategy_id"),
        "shadow_portfolios": sp,
        "improvement_ideas": (ideas.get("ideas", []) or [])[:8],
        "improvement_queue": imp_q.get("queue", []) if isinstance(imp_q, dict) else [],
        "opportunity_queue": opp_q.get("queue", []) if isinstance(opp_q, dict) else [],
        "market_review_cards": (cards_review.get("cards", []) or [])[:12],
        "broker_aware": broker,
        "backtest": _backtest_view(backtest),
        "projection": _projection_view(projection),
        "strategy_lab": _strategy_lab_view(strategy_lab),
        "simulation_charts": collect_simulation_charts_view(root),
    }


_CROWD_TACTIC_IDS = {"crowd_signal_only", "crowd_signal_plus_sentiment"}
_ANCHOR_TACTIC_IDS = {"shadow_actual_baseline"}


def _slim_row(r: dict | None) -> dict | None:
    if not r:
        return None
    return {
        "name": r.get("name"), "tactic_id": r.get("tactic_id"),
        "strategy_score": r.get("strategy_score"),
        "mean_excess_vs_spy": r.get("mean_excess_vs_spy"),
        "prob_beat_spy": r.get("prob_beat_spy"),
        "worst_max_drawdown": r.get("worst_max_drawdown"),
        "approximate": r.get("approximate", False),
        "by_window": r.get("by_window", []),
    }


def _strategy_lab_view(doc: dict) -> dict[str, Any]:
    """Compact Research Strategy Lab leaderboard view (observe-only)."""
    if not isinstance(doc, dict) or doc.get("status") != "ok":
        return {"available": False, "status": (doc or {}).get("status", "absent")}
    all_rows = doc.get("leaderboard") or []
    rows = []
    for r in all_rows[:15]:
        rows.append({
            "name": r.get("name"), "tactic_id": r.get("tactic_id"),
            "strategy_score": r.get("strategy_score"),
            "mean_excess_vs_spy": r.get("mean_excess_vs_spy"),
            "prob_beat_spy": r.get("prob_beat_spy"),
            "worst_max_drawdown": r.get("worst_max_drawdown"),
            "academic_basis": r.get("academic_basis", ""),
            "still_works_oos": r.get("still_works_oos"),
            "approximate": r.get("approximate", False),
            "is_crowd": r.get("tactic_id") in _CROWD_TACTIC_IDS,
        })
    # Crowd comparison: anchor vs crowd_only vs crowd+sentiment vs diagnostic
    anchor = next((r for r in all_rows if r.get("tactic_id") in _ANCHOR_TACTIC_IDS), None)
    crowd_variants = [r for r in all_rows if r.get("tactic_id") in _CROWD_TACTIC_IDS]
    diag_raw = doc.get("sentiment_diagnostic")
    crowd_comparison = {
        "available": bool(crowd_variants),
        "anchor": _slim_row(anchor),
        "variants": [_slim_row(r) for r in crowd_variants],
        "sentiment_diagnostic": _slim_row(diag_raw),
    }
    return {
        "available": True,
        "objective": doc.get("objective"),
        "tactic_count": doc.get("tactic_count"),
        "rows": rows,
        "crowd_comparison": crowd_comparison,
        "created_at": doc.get("created_at"),
    }


def _backtest_view(doc: dict) -> dict[str, Any]:
    """Compact Strategy Lab view of the portfolio backtest (observe-only)."""
    if not isinstance(doc, dict) or doc.get("status") not in ("ok",):
        return {"available": False, "status": (doc or {}).get("status", "absent")}
    lb = doc.get("leaderboard") or {}
    # Prefer a multi-year window for the headline leaderboard.
    headline_key = next((k for k in ("trailing_3y", "trailing_5y", "trailing_1y", "ytd")
                         if k in lb and lb[k]), next(iter(lb), None))
    return {
        "available": True,
        "objective": doc.get("objective"),
        "primary_benchmark": doc.get("primary_benchmark"),
        "headline_window": headline_key,
        "leaderboard": (lb.get(headline_key) or [])[:8] if headline_key else [],
        "windows": doc.get("windows", []),
        "contribution_sensitivity": doc.get("contribution_sensitivity", {}),
        "created_at": doc.get("created_at"),
    }


def _projection_view(doc: dict) -> dict[str, Any]:
    """Compact Strategy Lab view of the Monte-Carlo projection (observe-only)."""
    if not isinstance(doc, dict) or doc.get("status") not in ("ok",):
        return {"available": False, "status": (doc or {}).get("status", "absent")}
    return {
        "available": True,
        "rows": (doc.get("rows") or [])[:12],
        "horizons": doc.get("horizons", []),
        "seed": doc.get("seed"),
        "created_at": doc.get("created_at"),
    }
