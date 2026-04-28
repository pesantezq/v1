"""
Universal Scanner
=================
Lightweight shallow scan across the broad market universe.

Consumes pre-loaded FMP batch quote dicts — NO direct API calls.
Follows the same data-contract pattern as scanner/candidate_scanner.py:
all HTTP work stays in FMPClient; this module only processes the dicts.

Fields extracted per symbol
----------------------------
  price             — latest price (USD)
  pct_change_1d     — daily % change  (changesPercentage from FMP)
  volume            — latest session volume
  avg_volume        — average daily volume  (avgVolume from FMP)
  rel_volume        — volume / avg_volume  (None if avg_volume missing/zero)
  market_cap        — market capitalisation (USD)
  price_200dma      — 200-day moving average  (priceAvg200)
  pct_from_200dma   — (price − 200dma) / 200dma × 100
  price_50dma       — 50-day moving average  (priceAvg50)
  day_high          — intraday high
  day_low           — intraday low
  day_range_pct     — (day_high − day_low) / price × 100  (volatility proxy)
  year_high         — 52-week high
  year_low          — 52-week low
  pct_from_year_high — (price − year_high) / year_high × 100  (RS proxy; ≤ 0)
  timestamp         — quote timestamp string from FMP

Graceful degradation: every field that cannot be computed is set to None.
The scanner never raises; it logs warnings for symbols with unusual data.

Config key: ``universal_scanner``
  min_price       — skip symbols priced below this (default: 5.0)
  min_market_cap  — skip symbols with market cap below this in USD (default: 1e9)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger("portfolio_automation.universal_scanner")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ScanResult:
    """Shallow market scan result for a single symbol."""

    symbol: str

    # Price / return
    price: Optional[float] = None
    pct_change_1d: Optional[float] = None

    # Volume
    volume: Optional[int] = None
    avg_volume: Optional[int] = None
    rel_volume: Optional[float] = None

    # Size
    market_cap: Optional[float] = None

    # Moving averages
    price_200dma: Optional[float] = None
    pct_from_200dma: Optional[float] = None
    price_50dma: Optional[float] = None

    # Intraday range
    day_high: Optional[float] = None
    day_low: Optional[float] = None
    day_range_pct: Optional[float] = None

    # 52-week range
    year_high: Optional[float] = None
    year_low: Optional[float] = None
    pct_from_year_high: Optional[float] = None

    # Meta
    timestamp: Optional[str] = None
    price_data_source: Optional[str] = None

    # Theme confirmation score (0.0–1.0).
    # Populated externally (e.g. via theme_signals arg to scan()) or by
    # event_detection.compute_theme_support().  None means "not assessed".
    theme_support: Optional[float] = None

    # Derived flags (set in __post_init__)
    has_price: bool = field(default=False, init=False)
    has_volume: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self.has_price = self.price is not None and self.price > 0
        self.has_volume = self.volume is not None and self.volume > 0

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "price": self.price,
            "pct_change_1d": self.pct_change_1d,
            "volume": self.volume,
            "avg_volume": self.avg_volume,
            "rel_volume": self.rel_volume,
            "market_cap": self.market_cap,
            "price_200dma": self.price_200dma,
            "pct_from_200dma": self.pct_from_200dma,
            "price_50dma": self.price_50dma,
            "day_high": self.day_high,
            "day_low": self.day_low,
            "day_range_pct": self.day_range_pct,
            "year_high": self.year_high,
            "year_low": self.year_low,
            "pct_from_year_high": self.pct_from_year_high,
            "timestamp": self.timestamp,
            "price_data_source": self.price_data_source,
            "theme_support": self.theme_support,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(d: dict, key: str) -> Optional[float]:
    v = d.get(key)
    if v is None:
        return None
    try:
        f = float(v)
        # Reject NaN / Inf
        if f != f or f == float("inf") or f == float("-inf"):
            return None
        return f
    except (TypeError, ValueError):
        return None


def _safe_int(d: dict, key: str) -> Optional[int]:
    v = d.get(key)
    if v is None:
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

class UniversalScanner:
    """
    Converts FMP batch quote dicts into ScanResult objects.

    Usage::

        scanner = UniversalScanner(config.get("universal_scanner", {}))
        results = scanner.scan(batch_quotes, symbols=universe_symbols)
    """

    def __init__(self, config: Optional[dict] = None) -> None:
        cfg = config or {}
        self.min_price = _config_float(cfg, "min_price", 5.0, minimum=0.0)
        self.min_market_cap = _config_float(cfg, "min_market_cap", 1e9, minimum=0.0)

    def scan(
        self,
        batch_quotes: Dict[str, Dict],
        symbols: Optional[List[str]] = None,
        theme_signals: Optional[Dict[str, float]] = None,
    ) -> List[ScanResult]:
        """
        Convert batch_quotes to ScanResult list.

        Args:
            batch_quotes:   ``{symbol: quote_dict}`` from
                            ``FMPClient.get_batch_quotes()``.
                            Quote dict keys include: price, changesPercentage,
                            volume, avgVolume, marketCap, priceAvg200,
                            priceAvg50, dayHigh, dayLow, yearHigh, yearLow,
                            timestamp.
            symbols:        Optional explicit list of symbols to process.
                            If None, all keys in batch_quotes are scanned.
            theme_signals:  Optional ``{symbol: score}`` dict (0.0–1.0) with
                            pre-computed theme-support values (e.g. from
                            sector-ETF moves).  Values are stored directly on
                            the matching ScanResult.theme_support field.

        Returns:
            List of ScanResult, one per symbol that passes quality gates.
            Symbols with no quote data emit a bare ScanResult with only
            the symbol field set (has_price=False).
        """
        requested_symbols = _normalise_symbols(symbols or [])
        target_set: Optional[set] = set(requested_symbols) if requested_symbols else None
        results: List[ScanResult] = []
        seen: set = set()

        for sym, q in batch_quotes.items():
            if not isinstance(q, dict):
                continue
            normalized_sym = _normalize_symbol(sym)
            if target_set is not None and normalized_sym not in target_set:
                continue
            sr = self._parse_quote(normalized_sym, q)
            if sr is not None:
                if theme_signals:
                    score = theme_signals.get(normalized_sym)
                    if score is not None:
                        try:
                            sr.theme_support = max(0.0, min(1.0, float(score)))
                        except (TypeError, ValueError):
                            pass
                results.append(sr)
                seen.add(normalized_sym)

        # Emit bare results for requested symbols absent from batch_quotes
        if requested_symbols:
            for sym in requested_symbols:
                if sym not in seen:
                    logger.debug("No quote data for %s — emitting bare ScanResult", sym)
                    results.append(ScanResult(symbol=sym))

        n_with_price = sum(1 for r in results if r.has_price)
        logger.info(
            "UniversalScanner: %d symbols requested, %d results, %d with price",
            len(symbols) if symbols else len(batch_quotes),
            len(results),
            n_with_price,
        )
        return results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _parse_quote(self, sym: str, q: dict) -> Optional["ScanResult"]:
        """
        Parse one FMP quote dict into a ScanResult.

        Returns None for symbols that fail basic quality gates (price
        missing/zero, below min_price, or market cap below threshold).
        """
        price = _safe_float(q, "price")

        if price is None or price <= 0:
            logger.debug("Skipping %s: missing or zero price", sym)
            return None
        if price < self.min_price:
            logger.debug(
                "Skipping %s: price %.2f < min_price %.2f", sym, price, self.min_price
            )
            return None

        mkt_cap = _safe_float(q, "marketCap")
        if mkt_cap is not None and mkt_cap > 0 and mkt_cap < self.min_market_cap:
            logger.debug(
                "Skipping %s: marketCap %.0f < min_market_cap %.0f",
                sym, mkt_cap, self.min_market_cap,
            )
            return None

        pct_change = _safe_float(q, "changesPercentage")
        volume = _safe_int(q, "volume")
        avg_volume = _safe_int(q, "avgVolume")

        rel_volume: Optional[float] = None
        if volume is not None and avg_volume is not None and avg_volume > 0:
            rel_volume = round(volume / avg_volume, 3)

        p200 = _safe_float(q, "priceAvg200")
        pct_from_200: Optional[float] = None
        if p200 is not None and p200 > 0:
            pct_from_200 = round((price - p200) / p200 * 100, 2)

        p50 = _safe_float(q, "priceAvg50")

        day_high = _safe_float(q, "dayHigh")
        day_low = _safe_float(q, "dayLow")
        day_range_pct: Optional[float] = None
        if day_high is not None and day_low is not None and price > 0:
            day_range_pct = round(max(0.0, day_high - day_low) / price * 100, 2)

        year_high = _safe_float(q, "yearHigh")
        year_low = _safe_float(q, "yearLow")
        pct_from_yh: Optional[float] = None
        if year_high is not None and year_high > 0:
            pct_from_yh = round((price - year_high) / year_high * 100, 2)

        ts = q.get("timestamp")
        timestamp = str(ts) if ts is not None else None

        return ScanResult(
            symbol=sym,
            price=price,
            pct_change_1d=pct_change,
            volume=volume,
            avg_volume=avg_volume,
            rel_volume=rel_volume,
            market_cap=mkt_cap,
            price_200dma=p200,
            pct_from_200dma=pct_from_200,
            price_50dma=p50,
            day_high=day_high,
            day_low=day_low,
            day_range_pct=day_range_pct,
            year_high=year_high,
            year_low=year_low,
            pct_from_year_high=pct_from_yh,
            timestamp=timestamp,
            price_data_source="fmp",
        )


def _config_float(
    cfg: dict,
    key: str,
    default: float,
    *,
    minimum: float | None = None,
) -> float:
    value = _safe_float(cfg, key)
    if value is None:
        value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


def _normalize_symbol(value: object) -> str:
    return str(value or "").strip().upper()


def _normalise_symbols(symbols: List[str]) -> List[str]:
    result: List[str] = []
    seen: set[str] = set()
    for symbol in symbols:
        normalized = _normalize_symbol(symbol)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result
