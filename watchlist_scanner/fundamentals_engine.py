"""
Fundamentals engine — parses Alpha Vantage OVERVIEW responses.

Extracts key company data and computes a fundamental_context_score in [0, 1]
that captures sector relevance, size/liquidity, and quality signals.

No API calls are made here — this module only parses and scores
pre-fetched OVERVIEW payloads.
"""

from __future__ import annotations

import logging
import math
from typing import Any

logger = logging.getLogger("watchlist_scanner.fundamentals")

# ── Sector relevance weights ────────────────────────────────────────────────
# Proxy for alignment with growth / high-momentum investing themes.
_SECTOR_SCORES: dict[str, float] = {
    "Technology":               1.00,
    "Communication Services":   0.85,
    "Consumer Discretionary":   0.70,
    "Health Care":              0.65,
    "Industrials":              0.60,
    "Energy":                   0.55,
    "Financials":               0.50,
    "Consumer Staples":         0.40,
    "Materials":                0.35,
    "Real Estate":              0.30,
    "Utilities":                0.25,
}
_DEFAULT_SECTOR_SCORE: float = 0.45   # Unknown / N/A sector


# ── Helpers ─────────────────────────────────────────────────────────────────

def _safe_float(value: Any, default: float | None = None) -> float | None:
    """Convert to float; return default on failure or non-finite result."""
    try:
        v = float(value)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def _safe_str(value: Any) -> str | None:
    s = str(value).strip() if value is not None else ""
    return s if s and s.lower() != "none" else None


# ── Parser ───────────────────────────────────────────────────────────────────

def parse_overview(raw: dict[str, Any]) -> dict[str, Any]:
    """
    Extract key fields from an Alpha Vantage OVERVIEW response.

    Returns a clean fundamentals dict.  Missing / invalid fields are None.
    Returns an empty dict when `raw` is falsy.
    """
    if not raw:
        return {}

    def _f(key: str) -> float | None:
        val = raw.get(key)
        if val in (None, "None", "", "-", "N/A"):
            return None
        return _safe_float(val)

    def _s(key: str) -> str | None:
        return _safe_str(raw.get(key, ""))

    return {
        "symbol":               _s("Symbol"),
        "name":                 _s("Name"),
        "sector":               _s("Sector"),
        "industry":             _s("Industry"),
        "description":          (_s("Description") or "")[:200],
        "market_cap":           _f("MarketCapitalization"),
        "pe_ratio":             _f("PERatio"),
        "forward_pe":           _f("ForwardPE"),
        "profit_margin":        _f("ProfitMargin"),
        "revenue_ttm":          _f("RevenueTTM"),
        "gross_profit_ttm":     _f("GrossProfitTTM"),
        "beta":                 _f("Beta"),
        "analyst_target_price": _f("AnalystTargetPrice"),
        "dividend_yield":       _f("DividendYield"),
        "eps":                  _f("EPS"),
        "book_value":           _f("BookValue"),
        "52w_high":             _f("52WeekHigh"),
        "52w_low":              _f("52WeekLow"),
        "50dma":                _f("50DayMovingAverage"),
        "200dma":               _f("200DayMovingAverage"),
        # Growth metrics available in AV OVERVIEW
        "revenue_growth":       _f("QuarterlyRevenueGrowthYOY"),
        "earnings_growth":      _f("QuarterlyEarningsGrowthYOY"),
        # Not available in AV free-tier OVERVIEW
        "debt_ratio":           None,
    }


# ── Scorer ───────────────────────────────────────────────────────────────────

def fundamental_context_score(fundamentals: dict[str, Any]) -> float:
    """
    Compute a normalised score in [0, 1] from company fundamentals.

    Components (weights sum to 1.0):
        sector_score    0.30  — sector relevance to growth / tech themes
        size_score      0.30  — log10(market_cap) normalised; bigger = more liquid
        quality_score   0.25  — profit margin quality
        pe_score        0.15  — PE attractiveness (moderate PE wins)

    Returns 0.0 for an empty fundamentals dict.
    """
    if not fundamentals:
        return 0.0

    # ── 1. Sector relevance ──────────────────────────────────────────────────
    sector = fundamentals.get("sector") or ""
    sector_score = _SECTOR_SCORES.get(sector, _DEFAULT_SECTOR_SCORE)

    # ── 2. Size / liquidity proxy ────────────────────────────────────────────
    # log10 normalised: 1B (9) → 0.0 ; 100B (11) → 0.57 ; 3T (12.5) → 1.0
    mktcap = fundamentals.get("market_cap")
    if mktcap and mktcap > 0:
        log_mc = math.log10(mktcap)
        size_score = min(1.0, max(0.0, (log_mc - 9.0) / 3.5))
    else:
        size_score = 0.30   # neutral

    # ── 3. Profit margin quality ─────────────────────────────────────────────
    pm = fundamentals.get("profit_margin")
    if pm is not None:
        # 0% → 0.0 ; 20% → 0.5 ; 40%+ → 1.0 ; negative allowed (0)
        quality_score = min(1.0, max(0.0, pm / 0.40))
    else:
        quality_score = 0.30   # neutral

    # ── 4. PE attractiveness ─────────────────────────────────────────────────
    pe = fundamentals.get("pe_ratio")
    if pe is not None and pe > 0:
        if pe < 10:
            pe_score = 0.40    # cheap, but possibly distressed
        elif pe <= 35:
            pe_score = 1.00    # sweet-spot
        elif pe <= 50:
            pe_score = 0.60    # growth premium, acceptable
        elif pe <= 80:
            pe_score = 0.30    # expensive
        else:
            pe_score = 0.10    # very expensive
    else:
        pe_score = 0.30        # neutral (no PE data / ETF)

    total = (
        sector_score  * 0.30
        + size_score  * 0.30
        + quality_score * 0.25
        + pe_score    * 0.15
    )
    return round(min(1.0, total), 4)


def parse_fmp_profile(
    profile: dict[str, Any],
    quote: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Map an FMP v3/profile response (+ optional batch-quote row) to the same
    schema as parse_overview() so the rest of the pipeline needs no changes.

    Fields that are not available on the FMP free-tier profile endpoint are
    returned as None:
        forward_pe, profit_margin, revenue_ttm, gross_profit_ttm,
        analyst_target_price, dividend_yield, book_value

    The optional quote dict (from FMPClient.get_batch_quotes) fills in PE,
    SMAs, 52-week range, and EPS which are not present on the profile endpoint.
    """
    if not profile:
        return {}
    q = quote or {}

    def _pf(*keys: str) -> float | None:
        for k in keys:
            v = _safe_float(profile.get(k))
            if v is not None:
                return v
        return None

    def _qf(*keys: str) -> float | None:
        for k in keys:
            v = _safe_float(q.get(k))
            if v is not None:
                return v
        return None

    return {
        "symbol":               _safe_str(profile.get("symbol")),
        "name":                 _safe_str(profile.get("companyName")),
        "sector":               _safe_str(profile.get("sector")),
        "industry":             _safe_str(profile.get("industry")),
        "description":          (profile.get("description") or "")[:200],
        "market_cap":           _pf("mktCap") or _qf("marketCap"),
        "pe_ratio":             _qf("pe") or _pf("pe"),
        "forward_pe":           None,
        "profit_margin":        None,
        "revenue_ttm":          None,
        "gross_profit_ttm":     None,
        "beta":                 _pf("beta"),
        "analyst_target_price": None,
        "dividend_yield":       None,
        "eps":                  _qf("eps"),
        "book_value":           None,
        "52w_high":             _qf("yearHigh"),
        "52w_low":              _qf("yearLow"),
        "50dma":                _qf("priceAvg50"),
        "200dma":               _qf("priceAvg200"),
        # Not available on FMP free-tier profile/quote endpoints
        "revenue_growth":       None,
        "earnings_growth":      None,
        "debt_ratio":           None,
    }


def derive_fundamentals_score(fundamentals: dict[str, Any]) -> float:
    """
    Normalized fundamentals quality score in [0, 100].

    Re-scales fundamental_context_score() (which returns [0, 1]) to the
    0-100 range used in signal output and allocation caps.
    Returns 50.0 (neutral) for an empty or missing fundamentals dict.
    """
    if not fundamentals:
        return 50.0
    return round(fundamental_context_score(fundamentals) * 100, 1)


def derive_valuation_score(fundamentals: dict[str, Any]) -> float:
    """
    PE-based valuation attractiveness score in [0, 100].

    Scores based on PE attractiveness (higher = more attractive):
      None / ≤0   → 50.0  neutral (no PE data)
      <10         → 40.0  cheap but possibly distressed
      10–20       → 90.0  very attractive growth+value zone
      20–35       → 100.0 sweet-spot
      35–50       → 60.0  growth premium, acceptable
      50–80       → 30.0  expensive
      >80         → 10.0  very expensive
    """
    pe = fundamentals.get("pe_ratio") if fundamentals else None
    if pe is None or not isinstance(pe, (int, float)) or pe <= 0:
        return 50.0
    if pe < 10:
        return 40.0
    if pe <= 20:
        return 90.0
    if pe <= 35:
        return 100.0
    if pe <= 50:
        return 60.0
    if pe <= 80:
        return 30.0
    return 10.0


def format_market_cap(mktcap: float | None) -> str:
    """Human-readable market-cap string (e.g. '$1.23T', '$456.7B', '$12.3M')."""
    if not mktcap:
        return "N/A"
    if mktcap >= 1e12:
        return f"${mktcap / 1e12:.2f}T"
    if mktcap >= 1e9:
        return f"${mktcap / 1e9:.1f}B"
    if mktcap >= 1e6:
        return f"${mktcap / 1e6:.1f}M"
    return f"${mktcap:,.0f}"
