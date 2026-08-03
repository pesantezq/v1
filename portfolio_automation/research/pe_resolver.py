"""RESEARCH-ONLY canonical PE resolver.

**This must never feed the production scanner.** It exists so a Strategy Lab
champion/challenger experiment can ask "what if the intended PE component were
restored?" without changing production behaviour. `get_fundamentals_v3` is
deliberately left alone.

Why production PE is inert (measured 2026-08-03)
------------------------------------------------
`CandidateScanner` documents a PE>50 bubble guard and a 15-point PE attractiveness
factor, both reading ``metrics['peRatio']``. That key is never populated:
`get_fundamentals_v3` sources fundamentals from ``stable/key-metrics`` and looks
for ``peRatio`` / ``priceEarningsRatio``, and key-metrics returns **neither** — it
returns ``earningsYield`` instead. The v3 fallback that would have supplied
``peRatio`` only runs when key-metrics returns *nothing at all*, and it returns
plenty. So `peRatio` resolved 0/503 and both components were dead.

Source authority (validated live against the Starter plan)
---------------------------------------------------------
1. **DIRECT — ``stable/ratios`` → ``priceToEarningsRatio``.** Already an approved
   method (`get_ratios`, in `fmp_endpoint_compliance.STABLE_METHOD_MAP`), so **no
   new endpoint is introduced**. Note the field is ``priceToEarningsRatio``; the
   client docstring's ``priceEarningsRatio`` spelling does not exist in the live
   payload, which is part of why this was missed.
2. **DERIVED — ``1 / earningsYield``** from key-metrics, as a labelled fallback.
   ``earningsYield`` is a DECIMAL (AAPL 0.029). The reciprocal reconciles tightly
   for profitable names (AAPL 0.04%, NVDA 0.02%, XOM 0.00%, KO 0.13%) but **NOT
   universally**: BA diverged **15.07%** (87.20 direct vs 74.05 derived) and INTC
   7.12%, because the two use different earnings bases. Therefore derived is never
   presented as equivalent to direct.

Period: ``annual``, matching the basis `get_fundamentals_v3` already uses for
key-metrics and financial-growth. TTM diverges materially (NVDA 37.8 annual vs
31.5 TTM; PLTR 257.6 vs 130.9), so mixing bases would be a silent inconsistency.

Quality vocabulary
------------------
``direct`` · ``derived`` · ``negative_earnings`` · ``invalid`` · ``unavailable``.

``negative_earnings`` is a first-class state, not a number. A negative PE
**passes** a ``pe > 50`` guard while meaning loss-making — strictly worse than
expensive — so it must never be handed to the guard as a plain value.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

logger = logging.getLogger("portfolio_automation.research.pe_resolver")

# Both already approved in fmp_endpoint_compliance.STABLE_METHOD_MAP — a test
# pins this so the resolver cannot silently start needing a new endpoint.
REQUIRED_CLIENT_METHODS = ("get_ratios", "get_key_metrics")

DIRECT_FIELD = "priceToEarningsRatio"
# Accepted spellings, most-authoritative first. `priceEarningsRatio` is kept only
# because fmp_client's docstring claims it; it is absent from the live payload.
DIRECT_FIELD_CANDIDATES = (DIRECT_FIELD, "priceEarningsRatio", "peRatio")
DERIVED_FIELD = "earningsYield"

DEFAULT_PERIOD = "annual"

# Plausibility band for a resolved PE. Guards two distinct unit errors:
#   * an earningsYield delivered as a PERCENTAGE (2.9 meaning 2.9%) would derive
#     PE = 0.34 — far below the floor;
#   * a near-zero yield (1e-9) would derive PE = 1e9 — far above the ceiling.
MIN_PLAUSIBLE_PE = 0.5
MAX_PLAUSIBLE_PE = 10_000.0
# |earningsYield| below this cannot be inverted safely.
MIN_ABS_EARNINGS_YIELD = 1e-4


def _num(value: Any) -> float | None:
    """Strict numeric coercion. ``bool`` is rejected — it is never a ratio."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _result(symbol: str, *, pe: float | None, source: str, field: str | None,
            quality: str, as_of: str | None, reason: str = "",
            raw: float | None = None, period: str = DEFAULT_PERIOD) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "pe_ratio": pe,
        "source": source,
        "source_field": field,
        "period": period,
        "as_of": as_of,
        "quality": quality,
        "reason": reason,
        "raw_value": raw,
        "research_only": True,
    }


def resolve_pe(client: Any, symbol: str, *, as_of: str | None = None,
               period: str = DEFAULT_PERIOD, ttl_days: int = 30) -> dict[str, Any]:
    """Resolve one symbol's PE with explicit provenance and quality.

    Never raises, never returns 0.0 as a stand-in for missing, and never hands a
    negative PE back as a usable number.
    """
    if client is None:
        return _result(symbol, pe=None, source="none", field=None,
                       quality="unavailable", as_of=as_of,
                       reason="no client supplied", period=period)

    # 1. Direct.
    ratios: Any = None
    try:
        ratios = client.get_ratios(symbol, period=period, limit=1, ttl_days=ttl_days)
    except Exception as exc:
        logger.debug("pe_resolver: get_ratios(%s) failed: %s", symbol, exc)
    if isinstance(ratios, list):
        ratios = ratios[0] if ratios and isinstance(ratios[0], dict) else None
    if isinstance(ratios, dict):
        for field in DIRECT_FIELD_CANDIDATES:
            raw = _num(ratios.get(field))
            if raw is None:
                continue
            if raw <= 0:
                return _result(symbol, pe=None, source="stable_ratios", field=field,
                               quality="negative_earnings", as_of=as_of, raw=raw,
                               period=period,
                               reason="non-positive PE implies negative/zero earnings; "
                                      "a `pe > 50` guard would PASS it")
            if not (MIN_PLAUSIBLE_PE <= raw <= MAX_PLAUSIBLE_PE):
                return _result(symbol, pe=None, source="stable_ratios", field=field,
                               quality="invalid", as_of=as_of, raw=raw, period=period,
                               reason=f"implausible PE {raw!r} outside "
                                      f"[{MIN_PLAUSIBLE_PE}, {MAX_PLAUSIBLE_PE}]")
            return _result(symbol, pe=raw, source="stable_ratios", field=field,
                           quality="direct", as_of=as_of, raw=raw, period=period)

    # 2. Derived — labelled, never equivalent to direct.
    km: Any = None
    try:
        km = client.get_key_metrics(symbol, period=period, limit=1, ttl_days=ttl_days)
    except Exception as exc:
        logger.debug("pe_resolver: get_key_metrics(%s) failed: %s", symbol, exc)
    if isinstance(km, list):
        km = km[0] if km and isinstance(km[0], dict) else None
    if isinstance(km, dict):
        ey = _num(km.get(DERIVED_FIELD))
        if ey is not None:
            if ey < 0:
                return _result(symbol, pe=None, source="derived_earnings_yield",
                               field=DERIVED_FIELD, quality="negative_earnings",
                               as_of=as_of, raw=ey, period=period,
                               reason="negative earnings yield implies negative earnings")
            if abs(ey) < MIN_ABS_EARNINGS_YIELD:
                return _result(symbol, pe=None, source="derived_earnings_yield",
                               field=DERIVED_FIELD, quality="invalid", as_of=as_of,
                               raw=ey, period=period,
                               reason="earnings yield at/near zero — not invertible")
            derived = 1.0 / ey
            if not (MIN_PLAUSIBLE_PE <= derived <= MAX_PLAUSIBLE_PE):
                return _result(symbol, pe=None, source="derived_earnings_yield",
                               field=DERIVED_FIELD, quality="invalid", as_of=as_of,
                               raw=ey, period=period,
                               reason=f"implausible derived PE {derived:.4f} — likely a "
                                      "percentage-vs-decimal unit error in earningsYield")
            return _result(symbol, pe=round(derived, 6), source="derived_earnings_yield",
                           field=DERIVED_FIELD, quality="derived", as_of=as_of,
                           raw=ey, period=period,
                           reason="reciprocal of earningsYield; diverges from the direct "
                                  "source by up to ~15% on observed names")

    return _result(symbol, pe=None, source="none", field=None, quality="unavailable",
                   as_of=as_of, period=period,
                   reason="neither stable/ratios PE nor earningsYield available")


def resolve_pe_batch(client: Any, symbols: Iterable[str], *, as_of: str | None = None,
                     period: str = DEFAULT_PERIOD, ttl_days: int = 30) -> dict[str, Any]:
    """Resolve many symbols and summarise coverage by quality.

    ``coverage`` counts only USABLE PEs (direct + derived) over the eligible set.
    negative_earnings/invalid/unavailable are all explicitly not usable — they are
    not folded into coverage, so coverage can never be inflated by rows that
    merely returned something.
    """
    syms = [str(s) for s in (symbols or []) if s]
    by_symbol: dict[str, Any] = {}
    counts = {"direct": 0, "derived": 0, "negative_earnings": 0,
              "invalid": 0, "unavailable": 0}
    for symbol in syms:
        res = resolve_pe(client, symbol, as_of=as_of, period=period, ttl_days=ttl_days)
        by_symbol[symbol] = res
        counts[res["quality"]] = counts.get(res["quality"], 0) + 1

    usable = counts["direct"] + counts["derived"]
    return {
        "research_only": True,
        "feeds_production_scanner": False,
        "as_of": as_of,
        "period": period,
        "by_symbol": by_symbol,
        "summary": {
            "eligible": len(syms),
            **counts,
            "usable": usable,
            "coverage": (round(usable / len(syms), 4) if syms else None),
        },
    }
