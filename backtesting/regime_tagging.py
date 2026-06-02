"""
Regime conditioning for the POC backtest  (additive | advisory-only | observe-only)

Pattern-Improvement Loop — Step 3. Answers "do these patterns hold in drawdowns
vs. normal regimes?" by classifying the market regime *as of each signal's entry
date* from that symbol's own price series, then letting the harness break every
efficacy metric down by regime bucket.

Read-only reuse: regime labels come from the production
``market_regime.detect_market_regime`` classifier, fed single-name proxies derived
from the trailing price window (price-vs-SMA as a 0/1 breadth, trailing average and
mean-absolute daily return as price-change / volatility). This keeps the regime
vocabulary identical to live and avoids forking a second classifier.

Observe-only: no protected scoring/decision/allocation logic is touched and no
artifacts are written. Insufficient inputs return 'unknown' rather than guessing.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from market_regime import detect_market_regime

_MIN_BARS = 30        # need a meaningful trailing window before classifying
_TREND_WINDOW = 50    # SMA50-equivalent lookback
_SHORT_WINDOW = 20    # SMA20 / return / volatility lookback
_UNKNOWN = "unknown"


def _parse_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def _as_date(key: Any) -> date | None:
    return key if isinstance(key, date) else _parse_date(key)


def tag_signal_regime(signal: dict, price_series: dict) -> str:
    """Classify the regime as of the signal's entry date from *price_series*
    (a ``{date|iso_str: close}`` map). Returns a regime_label from the production
    vocabulary ('risk_on' | 'risk_off' | 'high_volatility' | 'neutral'); returns
    'unknown' when the entry date is missing or the trailing window is too short.
    """
    entry = _parse_date(signal.get("entry_date") or signal.get("scan_time") or signal.get("signal_date"))
    if entry is None or not price_series:
        return _UNKNOWN

    trailing = sorted(
        ((d, float(c)) for k, c in price_series.items()
         if (d := _as_date(k)) is not None and d <= entry),
        key=lambda pair: pair[0],
    )
    if len(trailing) < _MIN_BARS:
        return _UNKNOWN

    closes = [c for _, c in trailing]
    last = closes[-1]
    sma_trend = sum(closes[-_TREND_WINDOW:]) / len(closes[-_TREND_WINDOW:])
    sma_short = sum(closes[-_SHORT_WINDOW:]) / len(closes[-_SHORT_WINDOW:])

    window = closes[-(_SHORT_WINDOW + 1):]
    rets = [(window[i] / window[i - 1] - 1.0) * 100.0 for i in range(1, len(window)) if window[i - 1]]
    if not rets:
        return _UNKNOWN
    avg_change = sum(rets) / len(rets)
    volatility = sum(abs(r) for r in rets) / len(rets)

    regime = detect_market_regime(regime_inputs={
        "breadth_sma50": 1.0 if last >= sma_trend else 0.0,
        "breadth_sma20": 1.0 if last >= sma_short else 0.0,
        "avg_price_change_pct": round(avg_change, 4),
        "volatility_proxy": round(volatility, 4),
        "index_trend_state": "up" if last >= sma_trend else "down",
    })
    return str(regime.get("regime_label") or _UNKNOWN)


def per_regime_breakdown(results: list[dict], bt: Any, forward_days: int) -> list[dict]:
    """Group evaluated results by the regime in force at each signal's entry date
    (classified from that symbol's cached price map) and summarize efficacy.
    Long-only hit rate, matching the existing per-pattern convention. Additive and
    defensive: any per-row failure buckets that row as 'unknown' rather than
    raising (observe-only, non-blocking)."""
    ret_key = f"return_{forward_days}d"
    groups: dict[str, list[float]] = {}
    for r in results:
        ret = r.get(ret_key)
        if ret is None:
            continue
        try:
            price_map = bt._get_price_map(str(r.get("ticker", "")).upper())
            regime = tag_signal_regime(
                {"entry_date": r.get("entry_date") or r.get("signal_date")}, price_map or {}
            )
        except Exception:
            regime = _UNKNOWN
        groups.setdefault(regime, []).append(ret)

    out = []
    for regime, rets in sorted(groups.items()):
        wins = [x for x in rets if x > 0]
        out.append({
            "regime": regime,
            "count": len(rets),
            "hit_rate": round(len(wins) / len(rets) * 100.0, 2) if rets else 0.0,
            "avg_return": round(sum(rets) / len(rets), 4) if rets else 0.0,
        })
    return out
