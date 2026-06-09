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
    shadow = _load(sb / "shadow_portfolios.json") or {}
    ideas = _load(latest / "system_improvement_ideas.json") or {}
    imp_q = _load(latest / "system_improvement_action_queue.json") or {}
    opp_q = _load(latest / "operator_action_queue.json") or {}
    broker = _load(portfolio / "broker_aware_portfolio.json") or {}
    cards_review = _load(sb / "market_opportunity_review_cards.json") or {}

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
        "shadow_portfolios": sp,
        "improvement_ideas": (ideas.get("ideas", []) or [])[:8],
        "improvement_queue": imp_q.get("queue", []) if isinstance(imp_q, dict) else [],
        "opportunity_queue": opp_q.get("queue", []) if isinstance(opp_q, dict) else [],
        "market_review_cards": (cards_review.get("cards", []) or [])[:12],
        "broker_aware": broker,
    }
