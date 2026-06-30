"""Phase 9 — strategy mandates + champion/challenger framing (observe-only).

Turns the 8 materialized strategy profiles into well-defined research
contestants: each carries a structured **mandate** (objective, benchmark, hard
risk/turnover/leverage/concentration budgets, holding period, the regime it is
expected to win/lose in, and explicit promotion + rollback criteria). The daily
leaderboard is research context and is scored multi-factor — never CAGR/Sharpe
alone — with insufficient OOS evidence blocking promotion eligibility.

Roles: champion = the production baseline, control = production with overlays
disabled, challengers = the materialized profiles + overlays.

Observe-only: defines + scores research contestants; never mutates production,
scores, or weights. Promotion is human-gated (Phase 10).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import OutputNamespace, safe_write_json
from portfolio_automation.next_stage.contracts import observe_only_envelope

_MANDATE_FIELDS = ("objective", "benchmark", "permitted_inputs", "risk_budget",
                   "turnover_budget", "leverage_limit", "concentration_limit",
                   "holding_period", "success_regime", "failure_regime",
                   "promotion_criteria", "rollback_criteria", "role")

_MIN_OOS_SAMPLE = 30

__all__ = [
    "MANDATES", "mandate_complete", "mandate_missing_fields", "assign_roles",
    "leaderboard_score", "promotion_eligible", "build_strategy_mandates",
]


def _m(objective, benchmark, permitted, *, risk_budget, turnover_budget,
       leverage_limit, concentration_limit, hold, win, lose,
       role="challenger") -> dict[str, Any]:
    return {
        "objective": objective, "benchmark": benchmark,
        "permitted_inputs": list(permitted), "risk_budget": risk_budget,
        "turnover_budget": turnover_budget, "leverage_limit": leverage_limit,
        "concentration_limit": concentration_limit, "holding_period": hold,
        "success_regime": win, "failure_regime": lose,
        "promotion_criteria": (
            f"OOS excess vs {benchmark} > 0 over >= {_MIN_OOS_SAMPLE} resolved "
            "obs, consistent across sub-periods, drawdown within risk budget, "
            "regime-stable, cost-adjusted positive — and a human approval."),
        "rollback_criteria": (
            "OOS excess turns negative, drawdown breaches risk budget, regime "
            "instability, or evidence goes stale -> degrade/retire."),
        "role": role,
    }


# Structured mandate per materialized profile (hard budgets, regime fit).
MANDATES: dict[str, dict[str, Any]] = {
    "aggressive_growth": _m(
        "Maximize long-run growth", "QQQ", ["momentum", "growth", "crowd"],
        risk_budget=0.30, turnover_budget=0.60, leverage_limit=0.25, concentration_limit=0.60,
        hold="weeks-months", win="bull/expansion", lose="sharp drawdown / risk-off"),
    "short_term_tactical": _m(
        "Capture short-term tactical moves", "SPY", ["momentum", "crowd", "news"],
        risk_budget=0.25, turnover_budget=0.90, leverage_limit=0.25, concentration_limit=0.40,
        hold="days-weeks", win="trending/high-dispersion", lose="choppy/mean-reverting"),
    "long_term_compounding": _m(
        "Compound quality at low turnover", "SPY", ["quality", "fundamentals"],
        risk_budget=0.20, turnover_budget=0.15, leverage_limit=0.0, concentration_limit=0.40,
        hold="quarters-years", win="steady bull", lose="prolonged value rotation"),
    "tax_aware": _m(
        "Maximize after-tax return", "SPY", ["fundamentals", "tax_lots"],
        risk_budget=0.20, turnover_budget=0.20, leverage_limit=0.0, concentration_limit=0.40,
        hold="quarters-years", win="steady bull (defer gains)", lose="forced turnover"),
    "defensive_capital_preservation": _m(
        "Preserve capital, limit drawdown", "SPY", ["regime", "vol", "quality"],
        risk_budget=0.12, turnover_budget=0.30, leverage_limit=0.0, concentration_limit=0.35,
        hold="months", win="risk-off / high-vol", lose="strong bull (under-participates)"),
    "income_dividend": _m(
        "Generate income with stability", "SPY", ["dividend", "quality"],
        risk_budget=0.15, turnover_budget=0.20, leverage_limit=0.0, concentration_limit=0.35,
        hold="quarters-years", win="rate-stable / value", lose="rate shock / growth melt-up"),
    "balanced_core_satellite": _m(
        "Balanced core + tactical satellites", "SPY", ["core", "momentum", "crowd"],
        risk_budget=0.20, turnover_budget=0.40, leverage_limit=0.10, concentration_limit=0.45,
        hold="months", win="most regimes (diversified)", lose="correlated crash"),
    "boom_bucket": _m(
        "Small capped sleeve for asymmetric upside", "QQQ", ["crowd", "momentum", "theme"],
        risk_budget=0.10, turnover_budget=0.80, leverage_limit=0.0, concentration_limit=0.20,
        hold="days-weeks", win="speculative risk-on", lose="hype unwind / squeeze exhaustion"),
}


def mandate_missing_fields(m: dict[str, Any]) -> list[str]:
    return [f for f in _MANDATE_FIELDS if f not in m or m.get(f) in (None, "")]


def mandate_complete(m: dict[str, Any]) -> bool:
    return not mandate_missing_fields(m)


def assign_roles(profile_ids: list[str] | None = None) -> dict[str, Any]:
    """Champion = production baseline; control = overlays-off; challengers =
    the materialized profiles."""
    ids = profile_ids if profile_ids is not None else list(MANDATES)
    return {"champion": "production_baseline", "control": "overlays_off",
            "challengers": list(ids)}


def leaderboard_score(metrics: dict[str, Any]) -> float:
    """Multi-factor research score in [0,1] — NOT CAGR/Sharpe alone. Rewards OOS
    excess + consistency + regime stability; penalizes drawdown + turnover."""
    oos = float(metrics.get("oos_excess", 0.0))
    dd = float(metrics.get("max_drawdown", 0.0))
    consistency = float(metrics.get("consistency", 0.0))
    regime_stability = float(metrics.get("regime_stability", 0.0))
    turnover = float(metrics.get("turnover", 0.0))
    raw = (0.30 * max(0.0, min(oos * 5, 1.0))      # OOS excess (scaled)
           + 0.25 * consistency
           + 0.20 * regime_stability
           - 0.15 * min(dd, 1.0)
           - 0.10 * min(turnover, 1.0))
    return round(max(0.0, min(1.0, raw + 0.3)), 4)


def promotion_eligible(metrics: dict[str, Any]) -> bool:
    """Insufficient OOS evidence blocks promotion eligibility regardless of
    score (no promoting on a lucky short window)."""
    return int(metrics.get("oos_sample", 0)) >= _MIN_OOS_SAMPLE


def build_strategy_mandates(
    root: Path | str, *, now: str | None = None, profile_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Attach mandates to the known profiles; flag any profile lacking one.
    Observe-only artifact. Never raises."""
    root = Path(root)
    now = now or datetime.now(timezone.utc).isoformat()
    ids = profile_ids if profile_ids is not None else list(MANDATES)
    unmandated = [p for p in ids if p not in MANDATES or not mandate_complete(MANDATES[p])]
    payload = dict(observe_only_envelope(now))
    payload.update({
        "source": "strategy_mandate",
        "schema_version": "1",
        "roles": assign_roles(ids),
        "mandates": {p: MANDATES[p] for p in ids if p in MANDATES},
        "unmandated": unmandated,
        "coverage_complete": len(unmandated) == 0,
        "disclaimer": (
            "Observe-only strategy mandates + leaderboard framing. Research "
            "contestants only; never production instructions; promotion human-gated."
        ),
    })
    try:
        safe_write_json(OutputNamespace.SANDBOX, "strategy_mandates.json", payload,
                        base_dir=str(root / "outputs"))
    except Exception:
        pass
    return payload
