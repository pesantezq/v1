"""Phase 11 — risk + scenario comparison (observe-only).

Deterministic stress scenarios applied to portfolio weights, with pre/post-action
risk and per-position marginal contribution. These are **illustrations, not
forecasts** (`is_forecast=False`). ETF look-through is NOT fabricated — when
constituent/covariance data is unavailable we map by a coarse asset class and
say so (`etf_lookthrough_available=False`); deeper scenario simulation stays in
the weekly pipeline.

Observe-only: reads weights, computes shocked P&L. Never mutates production,
scores, or holdings.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import OutputNamespace, safe_write_json
from portfolio_automation.next_stage.contracts import observe_only_envelope

# Coarse asset classes a shock can target.
_BROAD = "broad_equity"
_GROWTH = "nasdaq_growth"
_SEMI = "semiconductors"
_GOLD = "gold"
_INTL = "international"
_FIN = "financials"

# Deterministic per-asset-class shock (% return) for each named scenario.
SCENARIOS: dict[str, dict[str, Any]] = {
    "broad_market_decline": {"desc": "Broad equity -10%",
        "shocks": {_BROAD: -10.0, _GROWTH: -12.0, _SEMI: -14.0, _FIN: -11.0, _INTL: -9.0, _GOLD: 1.0}},
    "nasdaq_growth_decline": {"desc": "Nasdaq/growth -15%",
        "shocks": {_GROWTH: -15.0, _SEMI: -16.0, _BROAD: -7.0, _FIN: -5.0, _INTL: -6.0, _GOLD: 1.0}},
    "semiconductor_drawdown": {"desc": "Semis -20%",
        "shocks": {_SEMI: -20.0, _GROWTH: -8.0, _BROAD: -4.0, _FIN: -2.0, _INTL: -3.0, _GOLD: 0.0}},
    "volatility_spike": {"desc": "Vol spike, risk-off",
        "shocks": {_BROAD: -8.0, _GROWTH: -11.0, _SEMI: -13.0, _FIN: -9.0, _INTL: -8.0, _GOLD: 3.0}},
    "rate_shock": {"desc": "Rates +100bp",
        "shocks": {_GROWTH: -9.0, _SEMI: -10.0, _BROAD: -5.0, _FIN: 2.0, _INTL: -4.0, _GOLD: -4.0}},
    "gold_decline": {"desc": "Gold -10%",
        "shocks": {_GOLD: -10.0, _BROAD: -0.5, _GROWTH: -0.5, _SEMI: -0.5, _FIN: -0.5, _INTL: -0.5}},
    "liquidity_shock": {"desc": "Liquidity crunch, everything down",
        "shocks": {_BROAD: -12.0, _GROWTH: -14.0, _SEMI: -16.0, _FIN: -13.0, _INTL: -13.0, _GOLD: -2.0}},
}

# Coarse symbol -> asset-class map (best-effort; degrade to broad equity).
_CLASS_MAP = {
    "GLD": _GOLD, "IAU": _GOLD,
    "QQQ": _GROWTH, "QLD": _GROWTH, "TQQQ": _GROWTH, "CHAT": _GROWTH, "NASA": _GROWTH,
    "SMH": _SEMI, "SOXX": _SEMI, "NVDA": _SEMI, "AMD": _SEMI, "AVGO": _SEMI,
    "ASML": _SEMI, "LRCX": _SEMI, "KLAC": _SEMI,
    "VFH": _FIN, "XLF": _FIN,
    "VXUS": _INTL, "EFA": _INTL, "VEA": _INTL,
}

__all__ = ["SCENARIOS", "classify_symbol", "apply_scenario", "pre_post_action_risk",
           "marginal_contribution", "build_scenario_risk"]


def classify_symbol(symbol: str) -> str:
    return _CLASS_MAP.get(str(symbol).upper(), _BROAD)


def _shock_for(symbol: str, scenario: str) -> float:
    shocks = SCENARIOS[scenario]["shocks"]
    return float(shocks.get(classify_symbol(symbol), shocks.get(_BROAD, 0.0)))


def apply_scenario(weights: dict[str, float], scenario: str) -> dict[str, Any]:
    """Portfolio % return under a named scenario (deterministic). Pure."""
    by_position = {sym: round(float(w) * _shock_for(sym, scenario), 6)
                   for sym, w in weights.items()}
    return {
        "scenario": scenario,
        "description": SCENARIOS[scenario]["desc"],
        "portfolio_return_pct": round(sum(by_position.values()), 6),
        "by_position": by_position,
    }


def marginal_contribution(weights: dict[str, float], scenario: str) -> dict[str, float]:
    """Each position's contribution to the scenario P&L (sums to the total)."""
    return apply_scenario(weights, scenario)["by_position"]


def pre_post_action_risk(
    before: dict[str, float], after: dict[str, float], scenario: str,
) -> dict[str, Any]:
    """Scenario P&L before vs after a simulated action + the action's marginal
    effect (post - pre) overall and per position."""
    pre = apply_scenario(before, scenario)
    post = apply_scenario(after, scenario)
    syms = set(before) | set(after)
    marginal = {s: round(post["by_position"].get(s, 0.0) - pre["by_position"].get(s, 0.0), 6)
                for s in syms}
    return {
        "scenario": scenario,
        "pre_return_pct": pre["portfolio_return_pct"],
        "post_return_pct": post["portfolio_return_pct"],
        "delta_pct": round(post["portfolio_return_pct"] - pre["portfolio_return_pct"], 6),
        "marginal_contribution": marginal,
    }


def _load_weights(root: Path) -> dict[str, float]:
    """Best-effort current weights from risk_delta concentration positions."""
    import json
    p = root / "outputs" / "latest" / "risk_delta.json"
    try:
        if p.exists():
            doc = json.loads(p.read_text(encoding="utf-8"))
            positions = (doc.get("concentration") or {}).get("positions") or []
            return {str(x["symbol"]).upper(): float(x.get("weight", 0.0))
                    for x in positions if x.get("symbol")}
    except Exception:
        pass
    return {}


def build_scenario_risk(root: Path | str, *, now: str | None = None) -> dict[str, Any]:
    """Run all scenarios on the current portfolio weights. Never raises;
    degrades honestly when holdings are unavailable."""
    root = Path(root)
    now = now or datetime.now(timezone.utc).isoformat()
    weights = _load_weights(root)
    results = {name: apply_scenario(weights, name) for name in SCENARIOS} if weights \
        else {name: {"scenario": name, "description": SCENARIOS[name]["desc"],
                     "portfolio_return_pct": None, "by_position": {}} for name in SCENARIOS}
    worst = None
    if weights:
        worst = min(results.values(), key=lambda r: r["portfolio_return_pct"])["scenario"]
    payload = dict(observe_only_envelope(now))
    payload.update({
        "source": "scenario_risk",
        "schema_version": "1",
        "is_forecast": False,                 # illustrations, not predictions
        "etf_lookthrough_available": False,   # no constituent data — not fabricated
        "degraded": not weights,
        "n_positions": len(weights),
        "scenarios": results,
        "worst_case_scenario": worst,
        "disclaimer": (
            "Observe-only deterministic stress illustrations (NOT forecasts). "
            "Coarse asset-class mapping; ETF look-through not modeled. Reads "
            "weights only; never mutates portfolio/scores/decisions."
        ),
    })
    try:
        safe_write_json(OutputNamespace.LATEST, "scenario_risk.json", payload,
                        base_dir=str(root / "outputs"))
    except Exception:
        pass
    return payload
