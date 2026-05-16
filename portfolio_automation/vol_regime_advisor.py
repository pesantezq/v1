"""
Volatility Regime Advisor — observe-only market-regime risk label.

Computes the 20-day realized volatility of a benchmark proxy (default: SPY)
and produces a regime label plus a *suggested* aggregate sizing multiplier.

Important: the multiplier is purely advisory. Live sizing is NOT modified
by this layer. The number exists so operators (and future calibrated
sizing layers) can see what an adaptive sizing rule would suggest.

Regime labels:
  - calm        : annualised σ < 12%   → suggested multiplier 1.10
  - normal      : 12% ≤ σ < 18%        → suggested multiplier 1.00
  - elevated    : 18% ≤ σ < 28%        → suggested multiplier 0.75
  - risk_off    : 28% ≤ σ < 45%        → suggested multiplier 0.50
  - crisis      : σ ≥ 45%              → suggested multiplier 0.25

Inputs (read-only):
  - An FMP client (optional). When None, advisor reports
    status="insufficient_data".

Outputs (LATEST namespace):
  - outputs/latest/vol_regime_advisor.json
  - outputs/latest/vol_regime_advisor.md

Hard guarantees:
  - observe_only=True hardcoded.
  - Never modifies signal_score / conviction_score / allocation outputs.
  - Never raises into the pipeline (caller wraps in try/except).
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import (
    OutputNamespace,
    safe_write_json,
    safe_write_text,
)

logger = logging.getLogger("stockbot.portfolio_automation.vol_regime_advisor")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_BENCHMARK = "SPY"
_REALIZED_VOL_WINDOW_DAYS = 20
_TRADING_DAYS_PER_YEAR = 252
_MIN_OBSERVATIONS = 15

# Regime thresholds expressed as annualised σ (decimal, 0.18 = 18%).
_REGIME_THRESHOLDS = [
    ("calm",     0.00, 0.12, 1.10),
    ("normal",   0.12, 0.18, 1.00),
    ("elevated", 0.18, 0.28, 0.75),
    ("risk_off", 0.28, 0.45, 0.50),
    ("crisis",   0.45, math.inf, 0.25),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _log_returns_from_fmp(rows: list[dict[str, Any]], window: int) -> list[float]:
    """
    Extract log returns from a newest-first FMP historical-prices list.

    Only the most recent *window* daily returns are returned.
    """
    closes: list[float] = []
    for r in rows[: window + 1]:
        if not isinstance(r, dict):
            continue
        c = _safe_float(r.get("adjClose")) or _safe_float(r.get("close"))
        if c is not None and c > 0:
            closes.append(c)
    if len(closes) < 2:
        return []
    ascending = list(reversed(closes))
    rets: list[float] = []
    for prev, cur in zip(ascending[:-1], ascending[1:]):
        if prev > 0 and cur > 0:
            rets.append(math.log(cur / prev))
    return rets


def realized_vol_annualised(returns: list[float]) -> float | None:
    """Population stdev of log returns, annualised."""
    n = len(returns)
    if n < _MIN_OBSERVATIONS:
        return None
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / n
    if var <= 0:
        return 0.0
    return math.sqrt(var) * math.sqrt(_TRADING_DAYS_PER_YEAR)


def classify_regime(sigma_annual: float | None) -> dict[str, Any]:
    """
    Map annualised σ to a regime label + advisory sizing multiplier.

    Returns {"regime": str, "sizing_multiplier": float, "sigma_lower": float,
             "sigma_upper": float | None}.
    """
    if sigma_annual is None:
        return {
            "regime": "unknown",
            "sizing_multiplier": 1.00,
            "sigma_lower": None,
            "sigma_upper": None,
        }
    for label, lo, hi, mult in _REGIME_THRESHOLDS:
        if lo <= sigma_annual < hi:
            return {
                "regime": label,
                "sizing_multiplier": mult,
                "sigma_lower": lo,
                "sigma_upper": hi if math.isfinite(hi) else None,
            }
    # Defensive — should never hit because crisis spans to ∞
    return {
        "regime": "crisis",
        "sizing_multiplier": 0.25,
        "sigma_lower": 0.45,
        "sigma_upper": None,
    }


# ---------------------------------------------------------------------------
# Plan envelope
# ---------------------------------------------------------------------------


def build_plan(
    *,
    benchmark: str,
    sigma_annual: float | None,
    observations: int,
    status: str,
    notes: list[str],
) -> dict[str, Any]:
    regime = classify_regime(sigma_annual)
    sigma_str = f"{sigma_annual:.1%}" if sigma_annual is not None else "n/a"
    summary_line = (
        f"Vol regime: {regime['regime']} "
        f"(stdev_annual={sigma_str}), "
        f"suggested sizing x{regime['sizing_multiplier']:.2f}"
    )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "observe_only": True,
        "schema_version": "1",
        "status": status,
        "benchmark": benchmark,
        "window_days": _REALIZED_VOL_WINDOW_DAYS,
        "observations": observations,
        "sigma_annual": (
            round(sigma_annual, 4) if sigma_annual is not None else None
        ),
        "regime": regime["regime"],
        "sizing_multiplier_suggested": regime["sizing_multiplier"],
        "regime_bounds": {
            "lower_sigma": regime["sigma_lower"],
            "upper_sigma": regime["sigma_upper"],
        },
        "summary_line": summary_line,
        "notes": list(notes),
        "advisory_disclaimer": (
            "sizing_multiplier_suggested is an advisory observation. Live "
            "allocations are NOT modified by this layer."
        ),
    }


def _render_markdown(plan: dict[str, Any]) -> str:
    sigma = plan.get("sigma_annual")
    notes_lines = [f"- {n}" for n in (plan.get("notes") or [])] or ["(none)"]
    return "\n".join([
        "# Volatility Regime Advisor",
        "",
        f"_Generated: {plan.get('generated_at')}_",
        "",
        "Observe-only. Live allocations are NOT modified by this layer.",
        "",
        plan.get("summary_line", ""),
        "",
        f"- Benchmark: **{plan.get('benchmark')}**",
        f"- Window: {plan.get('window_days')} trading days "
        f"({plan.get('observations')} observations)",
        f"- Stdev annualised: {f'{sigma:.1%}' if sigma is not None else 'n/a'}",
        f"- Regime: **{plan.get('regime')}**",
        f"- Suggested aggregate sizing multiplier: **×{plan.get('sizing_multiplier_suggested')}**",
        "",
        "## Notes",
        *notes_lines,
        "",
    ])


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------


def run_vol_regime_advisor(
    repo_root: Path | str,
    *,
    fmp_client: Any | None = None,
    benchmark: str = _DEFAULT_BENCHMARK,
    base_dir: Path | str = "outputs",
) -> dict[str, Any]:
    base_dir = Path(base_dir)
    notes: list[str] = []

    if fmp_client is None:
        plan = build_plan(
            benchmark=benchmark,
            sigma_annual=None,
            observations=0,
            status="insufficient_data",
            notes=["fmp_client unavailable; benchmark history not loaded"],
        )
        _write_artifacts(plan, base_dir)
        return plan

    rows: list[dict[str, Any]] = []
    try:
        rows = fmp_client.get_historical_prices(benchmark, years=1, ttl_days=1) or []
    except Exception as exc:
        logger.debug(
            "vol_regime_advisor: FMP fetch failed for %s (non-fatal): %s",
            benchmark, exc,
        )

    returns = _log_returns_from_fmp(rows, _REALIZED_VOL_WINDOW_DAYS)
    sigma = realized_vol_annualised(returns)

    if sigma is None:
        notes.append(
            f"fewer than {_MIN_OBSERVATIONS} usable observations for {benchmark}"
        )
        status = "insufficient_data"
    else:
        status = "ok"

    plan = build_plan(
        benchmark=benchmark,
        sigma_annual=sigma,
        observations=len(returns),
        status=status,
        notes=notes,
    )
    _write_artifacts(plan, base_dir)
    return plan


def _write_artifacts(plan: dict[str, Any], base_dir: Path) -> None:
    try:
        safe_write_json(
            OutputNamespace.LATEST, "vol_regime_advisor.json", plan, base_dir=base_dir,
        )
        safe_write_text(
            OutputNamespace.LATEST, "vol_regime_advisor.md",
            _render_markdown(plan), base_dir=base_dir,
        )
    except Exception as exc:
        logger.warning(
            "vol_regime_advisor: failed to write artifacts (non-fatal): %s", exc
        )
