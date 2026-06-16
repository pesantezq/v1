"""Simulation Charts producer — normalized, human-readable backtest evidence.

Aggregates EXISTING sandbox simulation artifacts into a single normalized
artifact (``outputs/latest/simulation_charts.json``) that the Strategy Lab
dashboard renders as plain-English charts for a non-quant reader.

It reads (never writes) these upstream sandbox artifacts:
  - outputs/sandbox/strategy_comparison.json  (per-strategy return / risk / drawdown — daily)
  - outputs/sandbox/portfolio_backtest.json   (per-window cagr / excess-vs-SPY / contribution sensitivity — weekly)
  - outputs/sandbox/portfolio_projection.json (Monte-Carlo growth fan — weekly)

STRICT SANDBOX / OBSERVE-ONLY. This module produces *research context only*:
it never trades, never emits buy/sell/hold language, never writes or reads
``decision_plan.json``, and never changes any recommendation. The official
advisory source remains ``decision_plan.json`` alone. Charts with no upstream
source data degrade to an honest empty state with a stated reason — they are
never fabricated.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import OutputNamespace, safe_write_json

SCHEMA_VERSION = "1"
SOURCE = "simulation_charts"
SOURCE_FILES = [
    "strategy_comparison.json",
    "portfolio_backtest.json",
    "portfolio_projection.json",
]

# Chart prose is deliberately restricted to non-instructional language.
# (No buy/sell/hold/execute/trade/rebalance/approved/official-recommendation.)
_SAFETY = {
    "mode": "sandbox",
    "official_advisory_source": "decision_plan.json",
    "can_execute_trades": False,
}

# Time-ordered windows we surface for the consistency view (longest → most recent).
_CONSISTENCY_WINDOWS = ["trailing_5y", "trailing_3y", "trailing_1y", "ytd"]
_WINDOW_LABELS = {
    "trailing_5y": "5-yr",
    "trailing_3y": "3-yr",
    "trailing_1y": "1-yr",
    "ytd": "YTD",
}


def _now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).isoformat()


def _pct(v: Any, digits: int = 1) -> float | None:
    """Decimal fraction (0..1, may exceed 1 for cagr) → percent, rounded."""
    try:
        return round(float(v) * 100.0, digits)
    except (TypeError, ValueError):
        return None


def _f(v: Any, digits: int = 2) -> float | None:
    try:
        return round(float(v), digits)
    except (TypeError, ValueError):
        return None


def _empty_chart(title: str, help_text: str, reason: str, body_key: str = "series") -> dict[str, Any]:
    """An honest 'no data yet' chart — never fabricated."""
    return {
        "title": title,
        "help_text": help_text,
        "takeaway": "",
        "available": False,
        "missing_reason": reason,
        body_key: [],
    }


# ───────────────────────── per-chart builders (pure) ─────────────────────────

def _build_risk_return(comparison: list[dict]) -> dict[str, Any]:
    title, help_text = "Risk vs Return", "Higher up means stronger simulated return. Further right means more ups and downs along the way."
    points = []
    for s in comparison:
        ret = _pct(s.get("after_tax_return_estimate"))
        risk = _pct(s.get("expected_volatility"))
        if ret is None or risk is None:
            continue
        points.append({
            "label": s.get("name") or s.get("strategy_id") or "strategy",
            "return_pct": ret,
            "risk_pct": risk,
            "drawdown_pct": _pct(s.get("max_drawdown_estimate")),
        })
    if not points:
        return _empty_chart(title, help_text, "strategy_comparison.json has no comparable strategies yet.", "points")
    best = max(points, key=lambda p: p["return_pct"])
    return {
        "title": title, "help_text": help_text,
        "takeaway": (
            f"In this sandbox comparison, {best['label']} sits highest for simulated "
            f"return (~{best['return_pct']}%). Strategies further right took a bumpier path. "
            "Research context only — not advice."
        ),
        "available": True, "points": points,
    }


def _build_drawdown(comparison: list[dict]) -> dict[str, Any]:
    title, help_text = "How Deep the Losses Got", "How far each strategy fell from its previous high point in the simulation. Smaller bars are gentler rides."
    bars = []
    for s in comparison:
        depth = _pct(abs(s.get("max_drawdown_estimate"))) if s.get("max_drawdown_estimate") is not None else None
        if depth is None:
            continue
        bars.append({"label": s.get("name") or s.get("strategy_id") or "strategy", "value_pct": depth})
    if not bars:
        return _empty_chart(title, help_text, "strategy_comparison.json has no drawdown estimates yet.", "bars")
    bars.sort(key=lambda b: b["value_pct"])
    gentlest, deepest = bars[0], bars[-1]
    return {
        "title": title, "help_text": help_text,
        "takeaway": (
            f"{gentlest['label']} had the gentlest simulated dip (~{gentlest['value_pct']}%), "
            f"while {deepest['label']} fell the furthest (~{deepest['value_pct']}%). Sandbox simulation only."
        ),
        "available": True, "bars": bars,
    }


def _build_growth(projection: dict) -> dict[str, Any]:
    title = "Growth Over Time"
    help_text = "Shows how $10,000 might have grown over time in the simulation. The shaded band spans cautious-to-optimistic outcomes; the line is the typical (middle) path."
    if not isinstance(projection, dict) or projection.get("status") != "ok":
        return _empty_chart(title, help_text, "portfolio_projection.json is not available yet — run the simulation/backtest pipeline.")
    fan = projection.get("anchor_fan") or {}
    horizons = projection.get("horizons") or list(fan.keys())
    horizon = next((h for h in ("1y", "5y", "10y", "35y") if h in fan and fan[h]), (horizons[0] if horizons else None))
    seq = fan.get(horizon) if horizon else None
    if not seq:
        return _empty_chart(title, help_text, "portfolio_projection.json has no growth fan series yet.")
    base = 10000.0
    typical = [{"x": int(pt.get("month", i)), "y": round(base * float(pt.get("p50", 1.0)), 0)} for i, pt in enumerate(seq)]
    low = [{"x": int(pt.get("month", i)), "y": round(base * float(pt.get("p5", 1.0)), 0)} for i, pt in enumerate(seq)]
    high = [{"x": int(pt.get("month", i)), "y": round(base * float(pt.get("p95", 1.0)), 0)} for i, pt in enumerate(seq)]
    end_typical = typical[-1]["y"]
    return {
        "title": title, "help_text": help_text,
        "takeaway": (
            f"Over the {horizon} simulated horizon, a $10,000 starting amount lands near "
            f"${end_typical:,.0f} on the typical path (cautious ${low[-1]['y']:,.0f} to optimistic "
            f"${high[-1]['y']:,.0f}). Monte-Carlo projection, sandbox only — not a forecast or advice."
        ),
        "available": True,
        "horizon_label": horizon,
        "series": [
            {"label": "Typical (middle)", "role": "primary", "points": typical},
            {"label": "Cautious (low)", "role": "low", "points": low},
            {"label": "Optimistic (high)", "role": "high", "points": high},
        ],
    }


def _build_rolling(backtest: dict) -> dict[str, Any]:
    title = "Was Performance Consistent?"
    help_text = "How far each strategy ran ahead of (or behind) the SPY benchmark across different look-back windows. Above zero means ahead of SPY; staying above across windows means steadier."
    if not isinstance(backtest, dict) or backtest.get("status") != "ok":
        return _empty_chart(title, help_text, "portfolio_backtest.json is not available yet — run the simulation/backtest pipeline.")
    lb = backtest.get("leaderboard") or {}
    windows = [w for w in _CONSISTENCY_WINDOWS if lb.get(w)]
    if len(windows) < 2:
        return _empty_chart(title, help_text, "portfolio_backtest.json does not yet cover enough windows to compare consistency.")
    # rank tactics by the longest available window's cagr; surface the top few
    rank_window = windows[0]
    ranked = sorted(lb[rank_window], key=lambda r: (r.get("cagr") or -1), reverse=True)
    top_ids = [(r.get("tactic_id"), r.get("name")) for r in ranked[:5]]
    series = []
    for tid, name in top_ids:
        pts = []
        for w in windows:
            row = next((r for r in lb.get(w, []) if r.get("tactic_id") == tid), None)
            if row is None:
                continue
            ex = _pct(row.get("excess_vs_spy"))
            if ex is not None:
                pts.append({"x": _WINDOW_LABELS.get(w, w), "y": ex})
        if len(pts) >= 2:
            series.append({"label": name or tid, "points": pts})
    if not series:
        return _empty_chart(title, help_text, "portfolio_backtest.json has no excess-vs-SPY series to compare yet.")
    leader = series[0]
    always_ahead = all(p["y"] > 0 for p in leader["points"])
    return {
        "title": title, "help_text": help_text,
        "takeaway": (
            f"{leader['label']} stayed {'ahead of' if always_ahead else 'mixed versus'} SPY across the "
            f"{len(leader['points'])} simulated windows shown. Above the zero line = ahead of SPY. Sandbox research only."
        ),
        "available": True,
        "zero_line": True,
        "series": series,
    }


def _build_contribution(backtest: dict) -> dict[str, Any]:
    title = "How Contributions Change the Outcome"
    help_text = "Shows how adding a fixed amount every month changes the ending value in the simulation — the green slice is gain on top of what you put in."
    if not isinstance(backtest, dict) or backtest.get("status") != "ok":
        return _empty_chart(title, help_text, "portfolio_backtest.json is not available yet — run the simulation/backtest pipeline.", "bars")
    cs = backtest.get("contribution_sensitivity") or {}
    by_window = cs.get("by_window") or {}
    window = next((w for w in ["trailing_5y", "trailing_3y", "trailing_1y", "ytd"] if by_window.get(w)), next(iter(by_window), None))
    table = by_window.get(window) if window else None
    if not table:
        return _empty_chart(title, help_text, "portfolio_backtest.json has no contribution-sensitivity table yet.", "bars")
    bars = []
    for amount in sorted(table.keys(), key=lambda a: int(a) if str(a).isdigit() else 0):
        row = table[amount] or {}
        contributed = _f(row.get("total_contributed"), 0)
        final = _f(row.get("final_balance_dca"), 0)
        gain = _f(row.get("net_gain_dca"), 0)
        if final is None:
            continue
        bars.append({
            "label": f"${int(amount):,}/mo" if str(amount).isdigit() else str(amount),
            "contributed": contributed, "final_balance": final, "net_gain": gain,
        })
    if not bars:
        return _empty_chart(title, help_text, "portfolio_backtest.json contribution table is empty.", "bars")
    top = max(bars, key=lambda b: b["final_balance"])
    return {
        "title": title, "help_text": help_text,
        "takeaway": (
            f"In this {_WINDOW_LABELS.get(window, window)} simulation, adding {top['label']} ends near "
            f"${top['final_balance']:,.0f} (about ${top['net_gain']:,.0f} on top of contributions). "
            "Bigger monthly additions lift the ending value. Sandbox simulation — not advice."
        ),
        "available": True, "window_label": _WINDOW_LABELS.get(window, window), "bars": bars,
    }


def _build_allocation_drift() -> dict[str, Any]:
    # No upstream artifact tracks per-sleeve allocation over the simulation horizon.
    return _empty_chart(
        "How the Portfolio Shifted Over Time",
        "Shows how much of the strategy sat in each sleeve (e.g. growth, value, cash) over time.",
        "No simulation artifact tracks sleeve/sector allocation over time yet. "
        "This chart will populate once the backtest engine emits per-period composition.",
    )


def _build_summary(comparison: list[dict]) -> dict[str, Any]:
    def _card(strategy: str | None, **extra) -> dict[str, Any]:
        return {"strategy": strategy, **extra}

    if not comparison:
        none = "No strategy comparison data yet — run the simulation/backtest pipeline."
        return {
            "best_growth": _card(None, return_pct=None, plain_english=none),
            "best_risk_control": _card(None, max_drawdown_pct=None, plain_english=none),
            "best_balance": _card(None, score=None, plain_english=none),
            "biggest_pain_point": _card(None, max_drawdown_pct=None, plain_english=none),
        }

    def _name(s):
        return s.get("name") or s.get("strategy_id") or "strategy"

    by_return = max(comparison, key=lambda s: (s.get("after_tax_return_estimate") or -1))
    by_safe = min(comparison, key=lambda s: (s.get("max_drawdown_estimate") if s.get("max_drawdown_estimate") is not None else 9e9))
    by_balance = max(comparison, key=lambda s: (s.get("final_strategy_rank") or -1))
    by_pain = max(comparison, key=lambda s: (s.get("max_drawdown_estimate") or -1))

    return {
        "best_growth": _card(
            _name(by_return), return_pct=_pct(by_return.get("after_tax_return_estimate")),
            plain_english=f"{_name(by_return)} showed the strongest simulated growth (~{_pct(by_return.get('after_tax_return_estimate'))}%). Sandbox only — not advice."),
        "best_risk_control": _card(
            _name(by_safe), max_drawdown_pct=_pct(by_safe.get("max_drawdown_estimate")),
            plain_english=f"{_name(by_safe)} had the gentlest simulated dips (worst drop ~{_pct(by_safe.get('max_drawdown_estimate'))}%). Research context only."),
        "best_balance": _card(
            _name(by_balance), score=_f(by_balance.get("final_strategy_rank"), 3),
            plain_english=f"{_name(by_balance)} ranked best overall on the blended risk-vs-return score in this sandbox comparison."),
        "biggest_pain_point": _card(
            _name(by_pain), max_drawdown_pct=_pct(by_pain.get("max_drawdown_estimate")),
            plain_english=f"{_name(by_pain)} saw the deepest simulated drawdown (~{_pct(by_pain.get('max_drawdown_estimate'))}%) — the bumpiest ride here."),
    }


# ───────────────────────── public API ─────────────────────────

def build_simulation_charts(
    *,
    comparison: dict | None = None,
    backtest: dict | None = None,
    projection: dict | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Pure: normalize the three upstream artifacts into the chart contract.

    Never raises on shape problems — every chart degrades to an honest empty
    state with a stated reason. Always returns the full contract.
    """
    comparison = comparison if isinstance(comparison, dict) else {}
    backtest = backtest if isinstance(backtest, dict) else {}
    projection = projection if isinstance(projection, dict) else {}
    comp_rows = comparison.get("comparison") if isinstance(comparison.get("comparison"), list) else []

    present = []
    if comp_rows:
        present.append("strategy_comparison.json")
    if backtest.get("status") == "ok":
        present.append("portfolio_backtest.json")
    if projection.get("status") == "ok":
        present.append("portfolio_projection.json")

    return {
        "schema_version": SCHEMA_VERSION,
        "source": SOURCE,
        "generated_at": _now_iso(now),
        "observe_only": True,
        "sandbox_only": True,
        "no_trade": True,
        "disclaimer": "Sandbox simulation evidence for research only. Does not change decision_plan.json, "
                      "does not create trades, and is not official advice.",
        "source_files": SOURCE_FILES,
        "source_files_present": present,
        "safety": dict(_SAFETY),
        "summary": _build_summary(comp_rows),
        "charts": {
            "growth_over_time": _build_growth(projection),
            "drawdown": _build_drawdown(comp_rows),
            "risk_return": _build_risk_return(comp_rows),
            "rolling_outperformance": _build_rolling(backtest),
            "contribution_sensitivity": _build_contribution(backtest),
            "allocation_drift": _build_allocation_drift(),
        },
    }


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        return {}


def run_simulation_charts(root: str | Path = ".", *, write_files: bool = True, now: datetime | None = None) -> dict[str, Any]:
    """Read upstream sandbox artifacts, build the chart contract, write it to
    outputs/latest/simulation_charts.json. Non-fatal: returns a degraded dict
    on any unhandled error and never raises into the pipeline."""
    try:
        root = Path(root)
        sb = root / "outputs" / "sandbox"
        payload = build_simulation_charts(
            comparison=_load(sb / "strategy_comparison.json"),
            backtest=_load(sb / "portfolio_backtest.json"),
            projection=_load(sb / "portfolio_projection.json"),
            now=now,
        )
        if write_files:
            safe_write_json(OutputNamespace.LATEST, "simulation_charts.json", payload, base_dir=str(root / "outputs"))
        return payload
    except Exception as exc:  # never sink the pipeline
        return {
            "schema_version": SCHEMA_VERSION, "source": SOURCE, "status": "error",
            "observe_only": True, "sandbox_only": True, "error": str(exc),
            "generated_at": _now_iso(now), "safety": dict(_SAFETY),
        }


if __name__ == "__main__":  # manual run
    import sys
    r = run_simulation_charts(sys.argv[1] if len(sys.argv) > 1 else ".")
    print("simulation_charts:", "error" if r.get("status") == "error" else "ok",
          "| sources present:", r.get("source_files_present"))
