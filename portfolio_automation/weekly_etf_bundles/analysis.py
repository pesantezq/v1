"""
Point-in-time ETF metrics for the weekly bundle watchlist.

All functions are PURE and operate on an injected PricePanel
(portfolio_automation.portfolio_sim.prices.PricePanel). They only ever read
prices dated on-or-before the as-of date, so there is no future-data leakage:
`compute_etf_metrics(panel, sym, bm, as_of)` cannot see a single close after
`as_of`, and the frozen prediction and its later outcome evaluation draw from
the identical price source (raw close), so a ranking never drifts against its
own evaluation.

Returns are PRICE returns (raw close, ex-distributions), consistent with the
portfolio_sim suite. Missing metrics are reported as None with a warning — they
are NEVER coerced to zero.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from typing import Any

from portfolio_automation import weekly_etf_bundles as _pkg
from portfolio_automation.weekly_etf_bundles.scoring import ScoringParams, score_bundle, score_etf

# Trading-day constants (approximate; the repo has no exchange-calendar dep and
# operates on the union of dates that actually exist in the price data).
_TDAYS_YEAR = 252
_TDAYS_52W = 252
_VOL_WINDOW_TDAYS = 63          # ~3 months of daily returns for realized vol
_MA_PERIODS = (20, 50, 200)


def _to_date(d: str) -> date:
    return date.fromisoformat(d[:10])


def last_on_or_before(dates: list[str], target: str) -> str | None:
    """Last calendar date in the sorted (asc) `dates` that is <= target.
    ISO date strings compare lexicographically == chronologically."""
    hi = None
    for d in dates:
        if d <= target:
            hi = d
        else:
            break
    return hi


def _series_upto(panel: Any, symbol: str, as_of: str) -> list[tuple[str, float]]:
    """(date, close) pairs for `symbol` with date <= as_of, ascending."""
    return [(d, c) for (d, c) in panel.series(symbol) if d <= as_of]


def _price_on_or_before(panel: Any, symbol: str, target: str) -> tuple[str, float] | None:
    ser = _series_upto(panel, symbol, target)
    return ser[-1] if ser else None


def _window_return(panel: Any, symbol: str, as_of: str, weeks: int) -> float | None:
    """Simple return over `weeks` calendar weeks ending at the on-or-before
    close for as_of. None if either endpoint is unavailable."""
    end = _price_on_or_before(panel, symbol, as_of)
    if end is None:
        return None
    target_start = (_to_date(as_of) - timedelta(weeks=weeks)).isoformat()
    start = _price_on_or_before(panel, symbol, target_start)
    if start is None or start[1] <= 0:
        return None
    return end[1] / start[1] - 1.0


def _sma(series: list[tuple[str, float]], period: int) -> float | None:
    if len(series) < period:
        return None
    window = [c for _, c in series[-period:]]
    return sum(window) / period


def _realized_vol(series: list[tuple[str, float]], window: int = _VOL_WINDOW_TDAYS) -> float | None:
    """Annualized realized volatility of daily simple returns over the trailing
    `window` closes."""
    closes = [c for _, c in series[-(window + 1):]]
    if len(closes) < 5:
        return None
    rets = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes)) if closes[i - 1] > 0]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(_TDAYS_YEAR)


def _max_drawdown(series: list[tuple[str, float]], window: int = _TDAYS_52W) -> float | None:
    """Maximum peak-to-trough drawdown over the trailing `window` closes.
    Returned as a non-positive fraction (e.g. -0.23)."""
    closes = [c for _, c in series[-window:]]
    if len(closes) < 2:
        return None
    peak = closes[0]
    mdd = 0.0
    for c in closes:
        if c > peak:
            peak = c
        if peak > 0:
            mdd = min(mdd, c / peak - 1.0)
    return mdd


def _ma_ordering(ma20: float | None, ma50: float | None, ma200: float | None) -> str:
    if None in (ma20, ma50, ma200):
        return "insufficient_data"
    if ma20 > ma50 > ma200:
        return "bullish"          # 20 > 50 > 200
    if ma20 < ma50 < ma200:
        return "bearish"          # 20 < 50 < 200
    return "mixed"


def compute_etf_metrics(
    panel: Any,
    symbol: str,
    benchmark: str,
    as_of: str,
    *,
    minimum_history_days: int = 200,
) -> dict[str, Any]:
    """Point-in-time metric bundle for one ETF as of `as_of`. Pure; reads only
    closes dated <= as_of. Every metric that cannot be computed is None with a
    warning appended — never zero."""
    symbol = symbol.upper()
    benchmark = benchmark.upper()
    warnings: list[str] = []

    series = _series_upto(panel, symbol, as_of)
    if not series:
        return {
            "symbol": symbol, "benchmark": benchmark, "as_of": as_of,
            "available": False, "reason": "no_price_history",
            "history_days": 0, "warnings": ["no_price_history"],
        }

    as_of_used, price = series[-1]
    history_days = len(series)
    insufficient_history = history_days < minimum_history_days
    if insufficient_history:
        warnings.append(f"insufficient_history:{history_days}<{minimum_history_days}")

    # Freshness: gap between requested as_of and the last close actually used.
    freshness_days = (_to_date(as_of) - _to_date(as_of_used)).days
    if freshness_days > 7:
        warnings.append(f"stale_price:{freshness_days}d")

    # Window returns (ETF + benchmark → excess).
    win = {"1w": 1, "4w": 4, "12w": 12, "26w": 26}
    etf_ret = {k: _window_return(panel, symbol, as_of, w) for k, w in win.items()}
    bm_ret = {k: _window_return(panel, benchmark, as_of, w) for k, w in win.items()}
    excess = {
        k: (etf_ret[k] - bm_ret[k]) if (etf_ret[k] is not None and bm_ret[k] is not None) else None
        for k in win
    }
    if any(bm_ret[k] is None for k in win):
        warnings.append(f"benchmark_history_incomplete:{benchmark}")

    # Moving averages + trend structure.
    ma = {p: _sma(series, p) for p in _MA_PERIODS}
    vs_ma = {
        p: ((price / ma[p] - 1.0) if (ma[p] and ma[p] > 0) else None) for p in _MA_PERIODS
    }
    above_ma = {p: (price > ma[p]) if ma[p] else None for p in _MA_PERIODS}
    ordering = _ma_ordering(ma[20], ma[50], ma[200])

    # 52-week high distance (<= 0).
    yr_ago = (_to_date(as_of) - timedelta(days=365)).isoformat()
    trailing_year = [c for (d, c) in series if d >= yr_ago]
    if trailing_year:
        high_52w = max(trailing_year)
        dist_52w_high = (price / high_52w - 1.0) if high_52w > 0 else None
    else:
        dist_52w_high = None
        warnings.append("no_52w_window")

    realized_vol = _realized_vol(series)
    max_dd = _max_drawdown(series)
    # Volatility-adjusted return: 12w return annualized-ish over realized vol.
    if etf_ret["12w"] is not None and realized_vol and realized_vol > 0:
        vol_adj_return = (etf_ret["12w"] * (_TDAYS_YEAR / (12 * 5))) / realized_vol
    else:
        vol_adj_return = None

    # Data coverage over the trailing year vs an idealized ~252 trading days.
    trailing_year_count = len(trailing_year)
    data_coverage = min(1.0, trailing_year_count / _TDAYS_52W) if trailing_year_count else 0.0

    return {
        "symbol": symbol,
        "benchmark": benchmark,
        "as_of": as_of,
        "as_of_used": as_of_used,
        "available": True,
        "price": round(price, 4),
        "history_days": history_days,
        "insufficient_history": insufficient_history,
        "data_coverage": round(data_coverage, 4),
        "data_freshness_days": freshness_days,
        "return_1w": etf_ret["1w"],
        "return_4w": etf_ret["4w"],
        "return_12w": etf_ret["12w"],
        "return_26w": etf_ret["26w"],
        "benchmark_return_1w": bm_ret["1w"],
        "benchmark_return_4w": bm_ret["4w"],
        "benchmark_return_12w": bm_ret["12w"],
        "benchmark_return_26w": bm_ret["26w"],
        "excess_return_1w": excess["1w"],
        "excess_return_4w": excess["4w"],
        "excess_return_12w": excess["12w"],
        "excess_return_26w": excess["26w"],
        "sma20": ma[20], "sma50": ma[50], "sma200": ma[200],
        "vs_sma20": vs_ma[20], "vs_sma50": vs_ma[50], "vs_sma200": vs_ma[200],
        "above_sma20": above_ma[20], "above_sma50": above_ma[50], "above_sma200": above_ma[200],
        "ma_ordering": ordering,
        "distance_from_52w_high": dist_52w_high,
        "realized_volatility": realized_vol,
        "max_drawdown": max_dd,
        "volatility_adjusted_return": vol_adj_return,
        "warnings": warnings,
    }


def build_weekly_analysis(
    config: Any,
    panel: Any,
    *,
    as_of: str,
    generated_at: str | None = None,
    params: ScoringParams | None = None,
    prior_bundle_scores: dict[str, float] | None = None,
    strategy_id: str | None = None,
    model_version: str | None = None,
) -> dict[str, Any]:
    """Pure: compose the full weekly analysis payload (the `latest.json` shape)
    from a validated config and a loaded PricePanel, as of `as_of`. Writes no
    files. `market_data_date` is the last real trading date <= as_of and is
    distinct from `generated_at` (the email/build timestamp)."""
    params = params or ScoringParams()
    prior_bundle_scores = prior_bundle_scores or {}
    ts = generated_at or datetime.now(timezone.utc).isoformat()
    market_data_date = last_on_or_before(panel.dates, as_of) if panel.dates else None
    min_hist = int(config.defaults.get("minimum_history_days", 200))
    min_cov = float(config.defaults.get("minimum_bundle_coverage", 0.80))

    base = {
        "generated_at": ts,
        "market_data_date": market_data_date,
        "as_of_requested": as_of,
        "observe_only": _pkg.POSTURE["observe_only"],
        "simulation_active": _pkg.POSTURE["simulation_active"],
        "production_gated": _pkg.POSTURE["production_gated"],
        "feeds_decision_engine": _pkg.POSTURE["feeds_decision_engine"],
        "schema_version": _pkg.SCHEMA_VERSION,
        "source": _pkg.SOURCE_LABEL,
        "strategy_id": strategy_id or _pkg.STRATEGY_ID,
        "model_version": model_version or _pkg.MODEL_VERSION,
        "config_version": config.content_hash,
        "disclaimer": _pkg.DISCLAIMER,
    }

    if market_data_date is None:
        return {**base, "status": "insufficient_data", "reason": "no_market_data",
                "bundles": [], "ranking_global": [], "coverage": 0.0,
                "bundle_count": 0, "etf_count": 0}

    # Per-ETF metrics + scores, deduped across bundles (compute once per symbol).
    metrics_cache: dict[str, dict[str, Any]] = {}
    score_cache: dict[str, dict[str, Any]] = {}

    def metrics_for(symbol: str, benchmark: str) -> dict[str, Any]:
        key = symbol.upper()
        if key not in metrics_cache:
            metrics_cache[key] = compute_etf_metrics(
                panel, key, benchmark, market_data_date, minimum_history_days=min_hist
            )
            score_cache[key] = score_etf(metrics_cache[key], params)
        return metrics_cache[key]

    bundles_out: list[dict[str, Any]] = []
    all_scored: list[dict[str, Any]] = []

    for bundle in config.enabled_bundles:
        member_metrics: dict[str, dict[str, Any]] = {}
        member_scores: dict[str, dict[str, Any]] = {}
        members_payload: list[dict[str, Any]] = []
        # Ensure benchmark metrics exist even if not a member.
        metrics_for(bundle.benchmark, bundle.benchmark)
        member_map = {m.symbol: m for m in bundle.members}
        for sym in bundle.symbols:
            m = metrics_for(sym, bundle.benchmark)
            s = score_cache[sym]
            member_metrics[sym] = m
            member_scores[sym] = s
            members_payload.append({
                "symbol": sym,
                "role": member_map[sym].role,
                "watch_score": s.get("watch_score"),
                "label": s.get("label"),
                "components": s.get("components", {}),
                "metrics": m,
            })

        summary = score_bundle(
            bundle, member_metrics, member_scores,
            minimum_bundle_coverage=min_cov,
            prior_bundle_score=prior_bundle_scores.get(bundle.id),
        )

        # Rank members within the bundle by watch score.
        ranked = sorted(
            [p for p in members_payload if p["watch_score"] is not None],
            key=lambda p: p["watch_score"], reverse=True,
        )
        for i, p in enumerate(ranked, start=1):
            p["rank_in_bundle"] = i
        summary["members"] = members_payload
        bundles_out.append(summary)

        for p in members_payload:
            if p["watch_score"] is not None:
                all_scored.append({
                    "symbol": p["symbol"], "bundle_id": bundle.id,
                    "benchmark": bundle.benchmark,
                    "watch_score": p["watch_score"], "label": p["label"],
                    "rank_in_bundle": p.get("rank_in_bundle"),
                    "components": p["components"],
                    "expected_direction": "outperform" if p["watch_score"] >= 65 else "neutral",
                })

    # Global ranking across every scored ETF (ties broken by symbol for determinism).
    all_scored.sort(key=lambda r: (-r["watch_score"], r["symbol"]))
    for i, r in enumerate(all_scored, start=1):
        r["rank_global"] = i

    etf_count = len({s for b in config.enabled_bundles for s in b.symbols})
    scored_count = len(all_scored)
    coverage = round(scored_count / etf_count, 4) if etf_count else 0.0

    # Bubble up any degraded-symbol warnings.
    stale_symbols = sorted(k for k, m in metrics_cache.items()
                           if any(w.startswith("stale_price") for w in m.get("warnings", [])))
    failed_symbols = sorted(k for k, m in metrics_cache.items() if not m.get("available"))

    return {
        **base,
        "status": "ok" if scored_count else "insufficient_data",
        "bundle_count": len(bundles_out),
        "etf_count": etf_count,
        "coverage": coverage,
        "stale_symbols": stale_symbols,
        "failed_symbols": failed_symbols,
        "panel_missing_symbols": list(getattr(panel, "missing", []) or []),
        "bundles": bundles_out,
        "ranking_global": all_scored,
    }

