"""
Correlation Risk Advisor — observe-only hidden-concentration surface.

The sector_cap rule catches obvious single-sector overload, but it cannot
see two facts:

  1. QQQ + QLD + tech individual names are all NDX-correlated even though
     QLD is "leveraged" and QQQ is "broad index".
  2. A 50% allocation across 5 holdings that all move with the same factor
     is not 5 independent bets — it's closer to 1.

This module computes a 90-day daily-return correlation matrix for current
holdings and reports:

  - the effective number of independent bets (`1 / sum_ij w_i w_j corr_ij`)
  - any holding pair with |corr| > 0.85 AND combined weight > 25%
  - overall concentration warning when effective_bets < 4

Inputs (read-only):
  - config.json portfolio.holdings (symbols + target_weight)
  - FMP historical prices via the provided FMP client (cached)

Outputs (LATEST namespace):
  - outputs/latest/correlation_risk_advisor.json
  - outputs/latest/correlation_risk_advisor.md

Hard guarantees:
  - observe_only=True hardcoded.
  - No mutation of any score or decision.
  - Degrades to status="insufficient_data" when fewer than 2 holdings have
    usable price history.
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

logger = logging.getLogger("stockbot.portfolio_automation.correlation_risk_advisor")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LOOKBACK_DAYS = 90
_MIN_OBSERVATIONS = 30
_HIGH_CORR_THRESHOLD = 0.85
_PAIR_COMBINED_WEIGHT_FLAG = 0.25
_LOW_EFFECTIVE_BETS_FLAG = 4.0

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        result = float(v)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _safe_str(v: Any) -> str:
    return str(v or "").strip()


def _load_json_safe(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return {}


# ---------------------------------------------------------------------------
# Math — keep it dependency-light. statistics module would be enough but
# computing it inline avoids any surprise with NaN inputs.
# ---------------------------------------------------------------------------


def _daily_log_returns(closes: list[float]) -> list[float]:
    """Return ascending-time list of log returns from a closes list.

    The input is assumed newest-first (FMP convention). We sort to ascending
    before differencing so the i-th return is between close[i] and close[i+1].
    """
    if len(closes) < 2:
        return []
    ascending = list(reversed(closes))
    rets: list[float] = []
    for prev, cur in zip(ascending[:-1], ascending[1:]):
        if prev > 0 and cur > 0:
            rets.append(math.log(cur / prev))
    return rets


def _correlation(a: list[float], b: list[float]) -> float | None:
    """Sample Pearson correlation; returns None when undefined."""
    n = min(len(a), len(b))
    if n < _MIN_OBSERVATIONS:
        return None
    a = a[-n:]
    b = b[-n:]
    mean_a = sum(a) / n
    mean_b = sum(b) / n
    cov = sum((ai - mean_a) * (bi - mean_b) for ai, bi in zip(a, b)) / n
    var_a = sum((ai - mean_a) ** 2 for ai in a) / n
    var_b = sum((bi - mean_b) ** 2 for bi in b) / n
    denom = math.sqrt(var_a * var_b)
    if denom <= 0:
        return None
    return max(-1.0, min(1.0, cov / denom))


def effective_independent_bets(
    weights: dict[str, float],
    corr: dict[tuple[str, str], float],
) -> float:
    """
    Return `1 / (w^T C w)` — the conventional effective-N proxy.

    For uncorrelated equal-weight holdings of count N this returns ~N. Missing
    correlations are treated as 0.0 (deliberately optimistic — caller should
    note the limitation in the artifact).
    """
    syms = list(weights.keys())
    if not syms:
        return 0.0
    total = 0.0
    for i, s_i in enumerate(syms):
        for j, s_j in enumerate(syms):
            w_i = weights[s_i]
            w_j = weights[s_j]
            if i == j:
                c = 1.0
            else:
                key = (s_i, s_j) if (s_i, s_j) in corr else (s_j, s_i)
                c = corr.get(key, 0.0) if isinstance(corr.get(key, 0.0), (int, float)) else 0.0
            total += w_i * w_j * c
    if total <= 0:
        return 0.0
    return 1.0 / total


# ---------------------------------------------------------------------------
# Plan building
# ---------------------------------------------------------------------------


def build_pair_flags(
    weights: dict[str, float],
    corr: dict[tuple[str, str], float],
) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []
    syms = list(weights.keys())
    for i in range(len(syms)):
        for j in range(i + 1, len(syms)):
            s_i, s_j = syms[i], syms[j]
            key = (s_i, s_j) if (s_i, s_j) in corr else (s_j, s_i)
            c = corr.get(key)
            if c is None:
                continue
            combined_w = weights[s_i] + weights[s_j]
            if abs(c) >= _HIGH_CORR_THRESHOLD and combined_w >= _PAIR_COMBINED_WEIGHT_FLAG:
                flags.append({
                    "pair": [s_i, s_j],
                    "correlation": round(c, 3),
                    "combined_weight": round(combined_w, 4),
                    "flag": "high_correlation_concentration",
                })
    flags.sort(key=lambda r: -r["combined_weight"])
    return flags


def build_plan(
    *,
    weights: dict[str, float],
    corr: dict[tuple[str, str], float],
    coverage: dict[str, int],
    status: str,
    notes: list[str],
) -> dict[str, Any]:
    effective_bets = round(effective_independent_bets(weights, corr), 3)
    pair_flags = build_pair_flags(weights, corr)
    overall_flags: list[str] = []
    if effective_bets > 0 and effective_bets < _LOW_EFFECTIVE_BETS_FLAG:
        overall_flags.append("low_effective_independent_bets")

    # Render the matrix as a JSON-friendly nested dict.
    matrix: dict[str, dict[str, float]] = {}
    syms = list(weights.keys())
    for s_i in syms:
        matrix[s_i] = {}
        for s_j in syms:
            if s_i == s_j:
                matrix[s_i][s_j] = 1.0
            else:
                key = (s_i, s_j) if (s_i, s_j) in corr else (s_j, s_i)
                c = corr.get(key)
                matrix[s_i][s_j] = round(c, 3) if c is not None else None

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "observe_only": True,
        "schema_version": "1",
        "status": status,
        "lookback_days": _LOOKBACK_DAYS,
        "weights": {k: round(v, 4) for k, v in weights.items()},
        "coverage_by_symbol": coverage,
        "effective_independent_bets": effective_bets,
        "high_correlation_pairs": pair_flags,
        "overall_flags": overall_flags,
        "matrix": matrix,
        "summary_line": (
            f"Correlation advisor: effective bets={effective_bets}, "
            f"{len(pair_flags)} high-correlation pair(s) flagged"
        ),
        "notes": list(notes),
    }


def _render_markdown(plan: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# Correlation Risk Advisor")
    lines.append("")
    lines.append(f"_Generated: {plan.get('generated_at')}_")
    lines.append("")
    lines.append("Observe-only. No trades are executed.")
    lines.append("")
    lines.append(plan.get("summary_line", ""))
    lines.append("")
    if plan.get("overall_flags"):
        lines.append(f"**Overall flags:** {', '.join(plan['overall_flags'])}")
        lines.append("")
    if plan.get("high_correlation_pairs"):
        lines.append("## High-correlation concentration pairs")
        lines.append("")
        lines.append("| Pair | Correlation | Combined weight |")
        lines.append("|---|---|---|")
        for f in plan["high_correlation_pairs"]:
            lines.append("| {p} | {c:.2f} | {w:.1%} |".format(
                p=" / ".join(f["pair"]),
                c=f["correlation"],
                w=f["combined_weight"],
            ))
        lines.append("")
    if plan.get("notes"):
        lines.append("## Notes")
        for n in plan["notes"]:
            lines.append(f"- {n}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------


def _holdings_with_weights(repo_root: Path) -> dict[str, float]:
    cfg = _load_json_safe(repo_root / "config.json")
    portfolio = cfg.get("portfolio") or {}
    weights: dict[str, float] = {}
    for h in portfolio.get("holdings") or []:
        if not isinstance(h, dict):
            continue
        symbol = _safe_str(h.get("symbol")).upper()
        target = _safe_float(h.get("target_weight"))
        shares = _safe_float(h.get("shares"))
        # Skip planned-but-not-bought rows (shares=0)
        if not symbol or shares is None or shares <= 0:
            continue
        if target is None or target <= 0:
            continue
        weights[symbol] = target
    # Normalize so weights sum to 1.0 across active holdings only.
    total = sum(weights.values())
    if total > 0:
        weights = {k: v / total for k, v in weights.items()}
    return weights


def _extract_closes_from_fmp(rows: list[dict[str, Any]], lookback: int) -> list[float]:
    closes: list[float] = []
    for r in rows[:lookback]:
        if not isinstance(r, dict):
            continue
        c = _safe_float(r.get("adjClose")) or _safe_float(r.get("close"))
        if c is not None and c > 0:
            closes.append(c)
    return closes


def run_correlation_risk_advisor(
    repo_root: Path | str,
    *,
    fmp_client: Any | None = None,
    base_dir: Path | str = "outputs",
    lookback_days: int = _LOOKBACK_DAYS,
) -> dict[str, Any]:
    repo_root = Path(repo_root)
    base_dir = Path(base_dir)

    weights = _holdings_with_weights(repo_root)
    notes: list[str] = []

    if not weights:
        plan = build_plan(
            weights={}, corr={}, coverage={},
            status="insufficient_data",
            notes=["no active holdings in config.json"],
        )
        _write_artifacts(plan, base_dir)
        return plan

    if fmp_client is None:
        plan = build_plan(
            weights=weights, corr={}, coverage={s: 0 for s in weights},
            status="insufficient_data",
            notes=["fmp_client unavailable; correlation matrix not computed"],
        )
        _write_artifacts(plan, base_dir)
        return plan

    # Fetch price history per symbol; collect returns.
    returns: dict[str, list[float]] = {}
    coverage: dict[str, int] = {}
    for symbol in weights:
        try:
            hist = fmp_client.get_historical_prices(
                symbol, years=max(1, lookback_days // 252 + 1), ttl_days=1
            )
        except Exception as exc:
            logger.debug(
                "correlation_advisor: fetch failed for %s (non-fatal): %s",
                symbol, exc,
            )
            hist = []
        closes = _extract_closes_from_fmp(hist or [], lookback_days)
        rets = _daily_log_returns(closes)
        returns[symbol] = rets
        coverage[symbol] = len(rets)

    # Build correlation pair map
    syms = list(weights.keys())
    corr: dict[tuple[str, str], float] = {}
    for i in range(len(syms)):
        for j in range(i + 1, len(syms)):
            c = _correlation(returns[syms[i]], returns[syms[j]])
            if c is not None:
                corr[(syms[i], syms[j])] = round(c, 4)

    usable = sum(1 for r in returns.values() if len(r) >= _MIN_OBSERVATIONS)
    if usable < 2:
        status = "insufficient_data"
        notes.append(
            f"fewer than 2 symbols have ≥{_MIN_OBSERVATIONS} return observations"
        )
    else:
        status = "ok"

    plan = build_plan(
        weights=weights, corr=corr, coverage=coverage,
        status=status, notes=notes,
    )
    _write_artifacts(plan, base_dir)
    return plan


def _write_artifacts(plan: dict[str, Any], base_dir: Path) -> None:
    try:
        safe_write_json(
            OutputNamespace.LATEST,
            "correlation_risk_advisor.json",
            plan,
            base_dir=base_dir,
        )
        safe_write_text(
            OutputNamespace.LATEST,
            "correlation_risk_advisor.md",
            _render_markdown(plan),
            base_dir=base_dir,
        )
    except Exception as exc:
        logger.warning(
            "correlation_advisor: failed to write artifacts (non-fatal): %s", exc
        )
