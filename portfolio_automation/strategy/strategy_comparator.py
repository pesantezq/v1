"""Strategy comparator (spec §24.4). Orchestrates the multi-strategy engine.

Resolves the actual portfolio (via holdings_resolver §6), reads the opportunity
radar + shadow portfolios + broker positions, scores all 8 profiles with the
objective functions, and writes the strategy artifacts. Prefers sandbox/shadow
evidence over narrative (§24.8).

Owns ``strategy_comparison.json`` (``produced_by: strategy_comparator``, §23.13).
Advisory-only: approving/preferring a profile executes nothing; the blocked
actions are enforced and surfaced. Never writes ``decision_plan.json``.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import OutputNamespace, safe_write_json
from portfolio_automation.next_stage.contracts import (
    observe_only_envelope, BLOCKED_STRATEGY_ACTIONS,
)
from portfolio_automation.holdings_resolver import resolve_holdings
from portfolio_automation.strategy.profiles import SEED_PROFILES, build_strategy_profiles
from portfolio_automation.strategy.objective_functions import compute_strategy_metrics
from portfolio_automation.strategy.tax_scorecard import build_tax_scorecard, has_tax_lot_data

_APPROVE_ACTIONS = ["approve_strategy_for_review", "reject_strategy", "defer_strategy",
                    "request_deeper_strategy_analysis", "run_strategy_in_sandbox",
                    "compare_against_current_policy", "mark_as_preferred_profile"]
_RISK_KEYS = ("expected_risk_level", "expected_volatility", "max_drawdown_estimate",
              "concentration_risk", "leverage_exposure", "cash_drag")


def _load_json_safe(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except Exception:
        return None


def _build_context(root: Path, now: datetime) -> dict[str, Any]:
    res = resolve_holdings(root, now=now)
    cfg = _load_json_safe(root / "config.json") or {}
    cfg_holdings = (cfg.get("portfolio", {}) or {}).get("holdings", []) or []
    leveraged = {str(h.get("symbol", "")).upper() for h in cfg_holdings if h.get("is_leveraged")}

    # weights: broker market_value if available, else config target_weight, else shares
    weights: dict[str, float] = {}
    broker_cash_drag = None
    if res["holdings_source"] == "broker":
        mvs = {str(h["symbol"]).upper(): float(h.get("market_value") or 0) for h in res["holdings"]}
        total = sum(mvs.values()) + float(res.get("cash") or 0)
        if total > 0:
            weights = {s: round(mv / total, 4) for s, mv in mvs.items() if mv > 0}
            broker_cash_drag = round(float(res.get("cash") or 0) / total, 4)
    if not weights:
        tw = {str(h.get("symbol", "")).upper(): float(h.get("target_weight") or 0)
              for h in cfg_holdings if h.get("target_weight")}
        if sum(tw.values()) > 0:
            tot = sum(tw.values())
            weights = {k: round(v / tot, 4) for k, v in tw.items()}
        else:
            sh = {str(h.get("symbol", "")).upper(): float(h.get("shares") or 0) for h in cfg_holdings}
            tot = sum(sh.values())
            weights = {k: round(v / tot, 4) for k, v in sh.items() if v > 0} if tot > 0 else {}

    leveraged_exposure = sum(w for s, w in weights.items() if s in leveraged)
    radar = _load_json_safe(root / "outputs" / "sandbox" / "opportunity_radar.json") or {}
    positions = (_load_json_safe(root / "outputs" / "latest" / "schwab_positions.json")
                 if res["holdings_source"] == "broker" else None)
    dq = _load_json_safe(root / "outputs" / "latest" / "data_quality_report.json") or {}
    data_quality = 0.6
    if isinstance(dq, dict) and dq.get("available") is not False:
        total_sym = dq.get("total_symbols") or 0
        healthy = dq.get("healthy_symbols") or 0
        if total_sym:
            data_quality = round(min(1.0, healthy / total_sym), 4)

    return {
        "weights": weights,
        "cash_drag": broker_cash_drag,
        "leveraged_exposure": round(leveraged_exposure, 4),
        "radar_opportunities": radar.get("opportunities", []) or [],
        "has_tax_lot_data": has_tax_lot_data(positions),
        "data_quality_score": data_quality,
        "holdings_source": res["holdings_source"],
        "positions": positions,
    }


def build_comparison(root: Path, now: datetime) -> dict[str, Any]:
    ctx = _build_context(root, now)
    metrics = [compute_strategy_metrics(p, ctx) for p in SEED_PROFILES.values()]
    metrics.sort(key=lambda m: m["final_strategy_rank"], reverse=True)
    return {"context_source": ctx["holdings_source"], "metrics": metrics}


def write_strategy_artifacts(root: Path, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    now_iso = now.isoformat()
    base = root / "outputs"
    try:
        cmp = build_comparison(root, now)
        metrics = cmp["metrics"]
        # Mark the operator-approved active strategy (sandbox-only; observe-only).
        from portfolio_automation.strategy.strategy_selection import (
            load_active_selection, mark_operator_selected,
        )
        _active = load_active_selection(root).get("active_strategy_id")
        mark_operator_selected(metrics, _active)
        ctx_positions = (_load_json_safe(root / "outputs" / "latest" / "schwab_positions.json")
                         if cmp["context_source"] == "broker" else None)

        # strategy_profiles.json
        safe_write_json(OutputNamespace.SANDBOX, "strategy_profiles.json",
                        build_strategy_profiles(now_iso), base_dir=base)

        # strategy_comparison.json — OWNED here (§23.13), produced_by tag
        comparison = observe_only_envelope(now_iso, source="strategy_comparator")
        comparison["produced_by"] = "strategy_comparator"
        comparison["comparison"] = metrics
        comparison["context_source"] = cmp["context_source"]
        comparison["evidence_preference"] = "sandbox_backtest_over_narrative"
        safe_write_json(OutputNamespace.SANDBOX, "strategy_comparison.json", comparison, base_dir=base)

        # strategy_risk_scorecard.json — risk subset per strategy
        risk = observe_only_envelope(now_iso, source="strategy_comparator")
        risk["scorecards"] = [{"strategy_id": m["strategy_id"], "name": m["name"],
                               **{k: m[k] for k in _RISK_KEYS}} for m in metrics]
        safe_write_json(OutputNamespace.SANDBOX, "strategy_risk_scorecard.json", risk, base_dir=base)

        # strategy_shadow_results.json — link shadow portfolios when present
        shadow = _load_json_safe(base / "sandbox" / "shadow_portfolios.json") or {}
        sresults = observe_only_envelope(now_iso, source="strategy_comparator")
        sresults["results"] = [{"strategy_id": m["strategy_id"], "name": m["name"],
                                "final_strategy_rank": m["final_strategy_rank"],
                                "opportunity_capture_score": m["opportunity_capture_score"]}
                               for m in metrics]
        sresults["shadow_portfolios_present"] = bool(shadow.get("portfolios"))
        safe_write_json(OutputNamespace.SANDBOX, "strategy_shadow_results.json", sresults, base_dir=base)

        # strategy_tax_scorecard.json — degrades without tax-lot data
        import json as _json
        from pathlib import Path as _Path
        _tax_lots = {}
        try:
            _p = _Path(root) / "outputs" / "latest" / "schwab_tax_lots.json"
            if _p.exists():
                _tax_lots = (_json.loads(_p.read_text(encoding="utf-8")) or {}).get("by_symbol", {})
        except Exception:
            _tax_lots = {}
        safe_write_json(OutputNamespace.SANDBOX, "strategy_tax_scorecard.json",
                        build_tax_scorecard(now_iso, ctx_positions, tax_lots=_tax_lots), base_dir=base)

        # strategy_review_queue.json (LATEST) — operator review; executes nothing
        queue = observe_only_envelope(now_iso, source="strategy_comparator")
        queue["queue"] = [{
            "strategy_id": m["strategy_id"], "name": m["name"],
            "final_strategy_rank": m["final_strategy_rank"],
            "expected_objective_fit": m["expected_objective_fit"],
            "expected_risk_level": m["expected_risk_level"],
            "max_drawdown_estimate": m["max_drawdown_estimate"],
            "tax_efficiency": m["tax_efficiency"],
            "after_tax_degraded": m["after_tax_degraded"],
            "operator_selected": m.get("operator_selected", False),
            "allowed_actions": list(_APPROVE_ACTIONS),
            "blocked_actions": list(BLOCKED_STRATEGY_ACTIONS),
        } for m in metrics]
        safe_write_json(OutputNamespace.LATEST, "strategy_review_queue.json", queue, base_dir=base)

        return {"profiles": len(metrics), "context_source": cmp["context_source"],
                "top": metrics[0]["strategy_id"] if metrics else None, "degraded": False}
    except Exception as exc:
        for ns, fn, key in (
            (OutputNamespace.SANDBOX, "strategy_profiles.json", "profiles"),
            (OutputNamespace.SANDBOX, "strategy_comparison.json", "comparison"),
            (OutputNamespace.SANDBOX, "strategy_risk_scorecard.json", "scorecards"),
            (OutputNamespace.SANDBOX, "strategy_shadow_results.json", "results"),
            (OutputNamespace.SANDBOX, "strategy_tax_scorecard.json", "scorecards"),
            (OutputNamespace.LATEST, "strategy_review_queue.json", "queue"),
        ):
            deg = observe_only_envelope(now_iso, source="strategy_comparator",
                                        degraded_mode=True, degraded_reason=str(exc))
            if fn == "strategy_comparison.json":
                deg["produced_by"] = "strategy_comparator"
            deg[key] = []
            try:
                safe_write_json(ns, fn, deg, base_dir=base)
            except Exception:
                pass
        return {"profiles": 0, "degraded": True}
