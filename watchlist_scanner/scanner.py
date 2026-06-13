"""
Watchlist Scanner — main scan orchestrator.

For each ticker in the watchlist:
  1. Fetch / load cached daily OHLCV  → compute SMA20, SMA50, volume spike,
     1-day price change, 5-day price change
  2. Fetch / load cached OVERVIEW     → sector, market cap, PE, margins
  3. Fetch / load cached news         → classify themes, compute avg sentiment
  4. Compute composite signal_score (3-component model)
  5. Emit alert if score ≥ threshold OR price_change ≥ alert_pct OR volume_spike

Scoring weights:
  theme_news_score        0.45  — headlines, sentiment, theme intensity
  technical_score         0.30  — momentum, SMAs, volume spike
  fundamental_ctx_score   0.25  — sector, size, margins, PE

Budget strategy:
  - News: ONE call covers the full watchlist (batch tickers param).
  - OVERVIEW: 1 call per symbol; 7-day cache → ~3 calls/day at steady state.
  - Daily OHLCV: 1 call per symbol; 24-h cache → 0 calls after first run.
"""

from __future__ import annotations

import logging
from datetime import datetime, date
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from watchlist_scanner.alert_filter import should_emit_alert
from watchlist_scanner.alert_ranking import apply_priority_score
from watchlist_scanner.approved_config_loader import load_approved_weights
from watchlist_scanner.cache_manager import CacheManager
from watchlist_scanner import theme_engine as te
from watchlist_scanner.fundamentals_engine import (
    parse_fmp_profile,
    parse_fmp_fundamentals_bundle,
    fundamental_context_score,
    derive_fundamentals_score,
    derive_valuation_score,
)

try:
    from fmp_client import FMPClient as _FMPClientType
    from fmp_client import CallBudgetExceeded as _FMPBudgetExceeded
except ImportError:  # pragma: no cover — fmp_client is always present in this repo
    _FMPClientType = None  # type: ignore[assignment,misc]
    _FMPBudgetExceeded = Exception  # type: ignore[assignment,misc]
from watchlist_scanner.confidence import compute_confidence
from watchlist_scanner.config import (
    DEFAULT_WATCHLIST,
    PRICE_CHANGE_ALERT_PCT,
    VOLUME_SPIKE_FACTOR,
    THEME_SCORE_THRESHOLD,
    MIN_SIGNAL_SCORE,
    CONFIDENCE_HIGH_THRESHOLD,
    CONFIDENCE_MEDIUM_THRESHOLD,
    CONFIDENCE_MIN_THRESHOLD,
    MEDIUM_CONF_MIN_SIGNAL,
    EXCEPTIONAL_SIGNAL_SCORE,
    FMP_QUOTE_TTL_MINUTES,
    FMP_NEWS_TTL_MINUTES,
    FMP_HISTORICAL_TTL_HOURS,
    FMP_PROFILE_TTL_DAYS,
    FMP_FUNDAMENTALS_TTL_DAYS,
)
from watchlist_scanner.models import AlertDecision, WatchlistRow, WatchlistScanResult
from watchlist_scanner.theme_alignment import load_theme_opportunities, load_theme_signals, enrich_row_with_theme
from watchlist_scanner.portfolio_fit import load_portfolio_snapshot, enrich_row_with_portfolio_fit

logger = logging.getLogger("watchlist_scanner.scanner")

# Neutral fundamental score when OVERVIEW data is unavailable.
# Derived from fundamental_context_score() with all-neutral inputs:
#   sector=unknown(0.45) × 0.30 + size=neutral(0.30) × 0.30
#   + quality=neutral(0.30) × 0.25 + pe=neutral(0.30) × 0.15 = 0.345
_NEUTRAL_FUND_SCORE: float = 0.345


# ---------------------------------------------------------------------------
# Technical indicator helpers (pure pandas — no API calls)
# ---------------------------------------------------------------------------

def _compute_technicals(df: pd.DataFrame, spike_factor: float = VOLUME_SPIKE_FACTOR) -> dict[str, Any]:
    """
    Compute SMA20, SMA50, volume spike, 1-day and 5-day price changes
    from a daily OHLCV DataFrame.

    Expects df indexed by date (newest first) with columns:
        close, adj_close, volume.

    Returns dict with keys:
        price, price_change_1d, price_change_5d,
        sma20, sma50, above_sma20, above_sma50,
        volume_today, volume_avg20, volume_spike,
        data_days
    """
    if df is None or len(df) < 2:
        return {}

    # Work on ascending order for rolling computations
    df_asc = df.sort_index()

    close = df_asc["adj_close"] if "adj_close" in df_asc.columns else df_asc["close"]
    volume = df_asc["volume"]

    price_today = float(close.iloc[-1])
    price_prev  = float(close.iloc[-2])
    price_change_1d = (price_today - price_prev) / price_prev * 100 if price_prev else 0.0

    # 5-day change (requires at least 6 rows)
    price_change_5d: float | None = None
    if len(df_asc) >= 6:
        price_5d_ago = float(close.iloc[-6])
        if price_5d_ago:
            price_change_5d = round((price_today - price_5d_ago) / price_5d_ago * 100, 2)

    sma20 = float(close.rolling(20).mean().iloc[-1]) if len(df_asc) >= 20 else None
    sma50 = float(close.rolling(50).mean().iloc[-1]) if len(df_asc) >= 50 else None

    vol_today  = float(volume.iloc[-1])
    vol_avg20  = float(volume.rolling(20).mean().iloc[-1]) if len(df_asc) >= 20 else None
    volume_spike = bool(vol_avg20 and vol_today > vol_avg20 * spike_factor)

    return {
        "price":           price_today,
        "price_change_1d": round(price_change_1d, 2),
        "price_change_5d": price_change_5d,
        "sma20":           round(sma20, 2) if sma20 is not None else None,
        "sma50":           round(sma50, 2) if sma50 is not None else None,
        "above_sma20":     bool(sma20 and price_today > sma20),
        "above_sma50":     bool(sma50 and price_today > sma50),
        "volume_today":    int(vol_today),
        "volume_avg20":    int(vol_avg20) if vol_avg20 is not None else None,
        "volume_spike":    volume_spike,
        "data_days":       len(df_asc),
    }


# ---------------------------------------------------------------------------
# Sentiment helpers
# ---------------------------------------------------------------------------

def _compute_avg_sentiment(articles: list[dict]) -> float:
    """Compute average overall_sentiment_score from news articles."""
    scores = []
    for art in articles:
        try:
            s = float(art.get("overall_sentiment_score") or 0)
            scores.append(s)
        except (TypeError, ValueError):
            pass
    return round(sum(scores) / len(scores), 4) if scores else 0.0


# ---------------------------------------------------------------------------
# Composite signal score (3-component model)
# ---------------------------------------------------------------------------

def _multi_timeframe_gate(tech: dict[str, Any]) -> tuple[float, bool, bool, bool]:
    """
    P4.3 — Multi-timeframe trend gate for the technical score.

    Reads optional weekly + monthly trend booleans from `tech`. When both
    keys are present and non-None, returns:
        both bullish     → 1.00
        exactly one      → 0.85
        both bearish     → 0.70

    When either key is missing or None, returns 1.00 (legacy parity). The
    gate never increases technical_score — it can only penalise
    cross-timeframe disagreement.

    Returns (gate, weekly_known, monthly_known, has_any).
    """
    weekly = tech.get("weekly_trend_bullish") if tech else None
    monthly = tech.get("monthly_trend_bullish") if tech else None
    weekly_known = weekly is not None
    monthly_known = monthly is not None
    if not (weekly_known and monthly_known):
        return 1.00, weekly_known, monthly_known, (weekly_known or monthly_known)
    if weekly and monthly:
        return 1.00, True, True, True
    if weekly or monthly:
        return 0.85, True, True, True
    return 0.70, True, True, True


def _compute_signal_score(
    tech: dict[str, Any],
    theme_scores: dict[str, float],
    articles: list[dict],
    fund_score: float,
    price_change_alert_pct: float = PRICE_CHANGE_ALERT_PCT,
) -> tuple[float, dict[str, float]]:
    """
    Composite signal score in [0, 1] with score breakdown.

    Weights:
        theme_news_score        0.45
        technical_score         0.30
        fundamental_ctx_score   0.25

    theme_news_score sub-components:
        max theme strength   0.60 of 0.45
        avg sentiment        0.25 of 0.45  (positive only)
        news volume          0.15 of 0.45  (10+ headlines = full)

    technical_score sub-components:
        1d price momentum    0.40 of 0.30  (normalised to 5%)
        5d price momentum    0.10 of 0.30  (normalised to 10%)
        volume spike         0.25 of 0.30
        SMA position         0.25 of 0.30  (SMA20=0.125, SMA50=0.125)

    P4.3: technical_score is then multiplied by the multi-timeframe gate
    (1.00 default, 0.85 partial disagreement, 0.70 both bearish). Legacy
    callers that omit weekly/monthly inputs see no behavioral change.

    Returns (total_score, breakdown_dict).
    """
    # ── 1. Theme / News score ────────────────────────────────────────────────
    max_theme = max(theme_scores.values()) if theme_scores else 0.0
    avg_sent  = _compute_avg_sentiment(articles)
    hl_count  = len(articles)

    theme_component    = max_theme                          * 0.60
    sentiment_component = max(0.0, avg_sent)               * 0.25   # positive only
    volume_component   = min(1.0, hl_count / 10.0)         * 0.15   # 10 headlines = full
    theme_news_score   = min(1.0, theme_component + sentiment_component + volume_component)

    # ── 2. Technical score ───────────────────────────────────────────────────
    if not tech:
        technical_score = 0.0
    else:
        pc1 = abs(tech.get("price_change_1d") or 0.0)
        pc5 = abs(tech.get("price_change_5d") or 0.0)

        momentum_1d  = min(1.0, pc1 / 5.0)   * 0.40   # ≥5% → full
        momentum_5d  = min(1.0, pc5 / 10.0)  * 0.10   # ≥10% 5d → full
        vol_score    = 0.25 if tech.get("volume_spike") else 0.0
        sma_score    = (0.125 if tech.get("above_sma20") else 0.0) \
                     + (0.125 if tech.get("above_sma50") else 0.0)

        technical_score = min(1.0, momentum_1d + momentum_5d + vol_score + sma_score)

    # ── P4.3: multi-timeframe gate on technical_score ────────────────────────
    mtf_gate, weekly_known, monthly_known, mtf_any = _multi_timeframe_gate(tech or {})
    technical_score = round(technical_score * mtf_gate, 4)

    # ── 3. Fundamental context score ─────────────────────────────────────────
    # fund_score already in [0, 1] from fundamentals_engine.fundamental_context_score()

    total = (
        theme_news_score * 0.45
        + technical_score * 0.30
        + fund_score      * 0.25
    )

    breakdown = {
        "theme_news_score":          round(theme_news_score, 4),
        "technical_score":           round(technical_score, 4),
        "fundamental_context_score": round(fund_score, 4),
        "mtf_gate":                  round(mtf_gate, 4),
    }
    # Only surface the input booleans when caller actually provided them —
    # legacy artifacts keep their existing shape.
    if weekly_known:
        breakdown["weekly_trend_bullish"] = bool((tech or {}).get("weekly_trend_bullish"))
    if monthly_known:
        breakdown["monthly_trend_bullish"] = bool((tech or {}).get("monthly_trend_bullish"))

    return round(min(1.0, total), 4), breakdown


# ---------------------------------------------------------------------------
# FMP quote → technicals bridge
# ---------------------------------------------------------------------------

def _technicals_from_fmp_quote(
    quote: dict[str, Any],
    spike_factor: float = VOLUME_SPIKE_FACTOR,
) -> dict[str, Any]:
    """
    Derive technical indicators from an FMP batch-quote row.

    Returns a subset of what _compute_technicals() returns.  Missing values:
      - sma20 / above_sma20: not available from a single-day FMP quote
      - price_change_5d:     not available from a single-day quote
      - data_days:           always 1 (single observation)

    above_sma20 defaults to False (conservative) when unavailable.
    """
    if not quote:
        return {}
    try:
        price = float(quote.get("price") or 0)
        if not price:
            return {}
        change_pct = float(quote.get("changesPercentage") or 0)
        vol_today  = int(float(quote.get("volume") or 0))
        avg_vol    = int(float(quote.get("avgVolume") or 0))
        sma50_raw  = float(quote.get("priceAvg50") or 0)
        sma50      = sma50_raw if sma50_raw else None
        vol_spike  = bool(avg_vol and vol_today > avg_vol * spike_factor)
        return {
            "price":           round(price, 4),
            "price_change_1d": round(change_pct, 2),
            "price_change_5d": None,
            "sma20":           None,
            "sma50":           round(sma50, 2) if sma50 else None,
            "above_sma20":     False,
            "above_sma50":     bool(sma50 and price > sma50),
            "volume_today":    vol_today,
            "volume_avg20":    avg_vol or None,
            "volume_spike":    vol_spike,
            "data_days":       1,
        }
    except (TypeError, ValueError):
        return {}


# ---------------------------------------------------------------------------
# FMP historical price data → OHLCV DataFrame bridge
# ---------------------------------------------------------------------------

def _df_from_fmp_historical(rows: list[dict]) -> Optional[pd.DataFrame]:
    """
    Convert FMP historical-price-full rows to an OHLCV DataFrame compatible
    with _compute_technicals().

    FMP returns rows newest-first; this function sorts to ascending order
    so rolling windows in _compute_technicals() are correctly computed.

    Expected input fields per row: date, open, high, low, close, adjClose, volume.
    """
    if not rows:
        return None
    result = []
    for row in rows:
        try:
            close = float(row.get("close", 0) or 0)
            result.append({
                "date":      pd.to_datetime(row["date"]),
                "open":      float(row.get("open", 0) or 0),
                "high":      float(row.get("high", 0) or 0),
                "low":       float(row.get("low", 0) or 0),
                "close":     close,
                "adj_close": float(row.get("adjClose") or close),
                "volume":    float(row.get("volume", 0) or 0),
            })
        except (KeyError, ValueError, TypeError):
            continue
    if not result:
        return None
    df = pd.DataFrame(result).set_index("date")
    return df.sort_index()   # ascending so rolling windows work


# ---------------------------------------------------------------------------
# Main scanner class
# ---------------------------------------------------------------------------

class WatchlistScanner:
    """
    Orchestrates daily OHLCV + OVERVIEW + news fetching, technical computation,
    fundamental scoring, theme classification, and alert generation.

    Args:
        watchlist:              Ticker symbols to scan.
        cache:                  Shared CacheManager instance.
        price_change_alert_pct: Min |price_change| % to flag.
        volume_spike_factor:    Multiplier over 20-day avg volume.
        theme_score_threshold:  Min theme score to include in output.
        min_signal_score:       Min composite score to emit as alert.
    """

    def __init__(
        self,
        watchlist: list[str],
        cache: CacheManager,
        price_change_alert_pct: float = PRICE_CHANGE_ALERT_PCT,
        volume_spike_factor: float = VOLUME_SPIKE_FACTOR,
        theme_score_threshold: float = THEME_SCORE_THRESHOLD,
        min_signal_score: float = MIN_SIGNAL_SCORE,
        confidence_high_threshold: float = CONFIDENCE_HIGH_THRESHOLD,
        confidence_medium_threshold: float = CONFIDENCE_MEDIUM_THRESHOLD,
        confidence_min_threshold: float = CONFIDENCE_MIN_THRESHOLD,
        medium_conf_min_signal: float = MEDIUM_CONF_MIN_SIGNAL,
        exceptional_signal_score: float = EXCEPTIONAL_SIGNAL_SCORE,
        signals_config: dict[str, Any] | None = None,
        ranking_config: dict[str, Any] | None = None,
        root: Path | str | None = None,
        fmp_client: Optional[Any] = None,
        data_sources: dict[str, Any] | None = None,
    ) -> None:
        self.watchlist = watchlist
        self._cache = cache
        self._price_alert_pct = price_change_alert_pct
        self._spike_factor = volume_spike_factor
        self._theme_threshold = theme_score_threshold
        self._min_signal = min_signal_score
        self._conf_high = confidence_high_threshold
        self._conf_medium = confidence_medium_threshold
        self._conf_min = confidence_min_threshold
        self._medium_conf_min_signal = medium_conf_min_signal
        self._exceptional_signal = exceptional_signal_score
        self._signals_config = dict(signals_config or {})
        self._ranking_config = dict(ranking_config or {})
        # Root is used to locate theme_opportunities.json; defaults to repo root.
        self._root: Path = Path(root) if root is not None else Path(__file__).resolve().parents[1]
        # FMP primary-data configuration
        self._fmp = fmp_client
        _ds = dict(data_sources or {})
        self._fmp_enabled = fmp_client is not None and _ds.get("fmp_enabled", True)

    # ── Public entry point ─────────────────────────────────────────────────

    def run(self, dry_run: bool = False) -> WatchlistScanResult:
        """
        Execute a full scan of the watchlist.

        Returns:
            {
              "run_date": "YYYY-MM-DD",
              "generated_at": "<ISO>",
              "calls_used": int,
              "results": [<signal_dict>, ...],    # all symbols scanned
              "alerts": [<signal_dict>, ...],      # signals that meet alert threshold
            }
        """
        logger.info(
            "WatchlistScanner: starting scan of %d symbols (FMP-primary)",
            len(self.watchlist),
        )

        # ── Step 0: Load approved ranking weights (optional, non-blocking) ────
        # Reads approved_ranking_config.json if present and valid. Falls back to
        # hardcoded defaults silently when absent; logs a warning when the file
        # exists but fails validation.
        _approved_path = self._root / "outputs" / "performance" / "approved_ranking_config.json"
        _approved_weights_config = load_approved_weights(_approved_path)
        if _approved_weights_config and _approved_weights_config.get("_valid"):
            logger.info(
                "Approved ranking weights active: candidate=%s, approved_at=%s",
                _approved_weights_config.get("recommended_candidate"),
                _approved_weights_config.get("approved_at"),
            )
        elif _approved_weights_config:
            logger.warning(
                "Approved ranking config found but invalid (%s) — using default weights",
                _approved_weights_config.get("reason", "unknown reason"),
            )

        # ── Step 0.5: Pre-fetch FMP batch data ───────────────────────────────
        # Runs whenever FMP is enabled — even when dry_run=True (AV budget
        # exhausted).  FMP is an independent provider; AV budget exhaustion
        # must not prevent FMP data from being loaded.
        #
        # Pre-fetches (all use FMP's internal cache + budget guards):
        #   quotes      — stable/quote, TTL = FMP_QUOTE_TTL_MINUTES (default 15 min)
        #   profiles    — stable/profile per symbol, TTL = FMP_PROFILE_TTL_DAYS (7 days)
        #   historical  — stable/historical-price-eod/full per symbol, TTL = 1 day
        #   ratios      — stable/ratios per symbol, TTL = FMP_FUNDAMENTALS_TTL_DAYS (30 days)
        _fmp_quotes:    dict[str, dict] = {}
        _fmp_profiles:  dict[str, dict] = {}
        _fmp_historical: dict[str, list] = {}
        _fmp_ratios:    dict[str, dict] = {}

        if self._fmp_enabled:
            _q_ttl = int(self._signals_config.get("fmp_quote_ttl_minutes", FMP_QUOTE_TTL_MINUTES))
            _p_ttl = int(self._signals_config.get("fmp_profile_ttl_days", FMP_PROFILE_TTL_DAYS))
            _f_ttl = int(self._signals_config.get("fmp_fundamentals_ttl_days", FMP_FUNDAMENTALS_TTL_DAYS))

            # Quotes
            try:
                raw_q = self._fmp.get_batch_quotes(self.watchlist, ttl_hours=_q_ttl / 60)
                _fmp_quotes = raw_q or {}
            except Exception as exc:
                logger.warning("FMP batch-quotes pre-fetch failed (non-fatal): %s", exc)

            # Profiles — stable/profile per symbol (replaces v3 comma-separated batch)
            try:
                profiles = self._fmp.get_batch_profiles(self.watchlist, ttl_days=_p_ttl)
                _fmp_profiles = {
                    p["symbol"]: p
                    for p in (profiles or [])
                    if isinstance(p, dict) and p.get("symbol")
                }
            except Exception as exc:
                logger.warning("FMP batch-profiles pre-fetch failed (non-fatal): %s", exc)

            # Historical prices — for SMA20, 5d change (1-day TTL; mostly cached)
            for _sym in self.watchlist:
                try:
                    hist = self._fmp.get_historical_prices(_sym, years=1, ttl_days=1)
                    if hist:
                        _fmp_historical[_sym] = hist
                except Exception as exc:
                    logger.debug("FMP historical for %s failed (non-fatal): %s", _sym, exc)

            # Ratios — for profit_margin, dividend_yield (30-day TTL; monthly refresh)
            for _sym in self.watchlist:
                try:
                    rat = self._fmp.get_ratios(_sym, ttl_days=_f_ttl)
                    if rat:
                        _fmp_ratios[_sym] = rat
                except Exception as exc:
                    logger.debug("FMP ratios for %s failed (non-fatal): %s", _sym, exc)

        logger.info(
            "WatchlistScanner: FMP primary enabled=%s — %d quotes, %d profiles, "
            "%d historical, %d ratios loaded for %d watchlist symbols",
            self._fmp_enabled, len(_fmp_quotes), len(_fmp_profiles),
            len(_fmp_historical), len(_fmp_ratios), len(self.watchlist),
        )

        # ── Step 0.6: Fresh-data rotation ─────────────────────────────────────
        # Randomly invalidates the OVERVIEW (7-day TTL) cache for a configurable
        # fraction of symbols so every run guarantees at least some fresh
        # fundamentals, keeping confidence scores from bottoming out on cache-only
        # runs.  fresh_scan_fraction=0.25 → ~25% of symbols get a forced refresh.
        import random as _random
        _fresh_scan_fraction = float(self._signals_config.get("fresh_scan_fraction", 0.25))
        _fresh_symbols: set[str] = set()
        if _fresh_scan_fraction > 0 and not dry_run:
            n_fresh = max(1, round(len(self.watchlist) * _fresh_scan_fraction))
            _fresh_symbols = set(_random.sample(self.watchlist, min(n_fresh, len(self.watchlist))))
            for _sym in _fresh_symbols:
                self._cache.delete(f"overview_{_sym}")
            logger.info(
                "Fresh rotation: invalidated OVERVIEW cache for %d/%d symbol(s) — %s",
                len(_fresh_symbols), len(self.watchlist),
                ", ".join(sorted(_fresh_symbols)[:5]) + ("..." if len(_fresh_symbols) > 5 else ""),
            )

        # ── Step 1: Fetch news for the entire watchlist ───────────────────────
        # FMP stock_news is the sole news provider. Runs even in dry_run (the
        # FMP client manages its own cache + budget).
        _news_ttl = int(self._signals_config.get("fmp_news_ttl_minutes", FMP_NEWS_TTL_MINUTES))
        news_source: str = "none"
        articles: list[dict] = []

        if self._fmp_enabled and self._fmp is not None:
            try:
                fmp_arts = self._fmp.get_stock_news(
                    self.watchlist, limit=50, ttl_hours=_news_ttl / 60
                )
                if fmp_arts:
                    articles = fmp_arts
                    news_source = "fmp"
                    logger.info("FMP news: %d articles", len(articles))
            except Exception as fmp_exc:
                logger.warning("FMP news fetch failed: %s", fmp_exc)

        # ── Step 2: Build per-ticker news lookup ─────────────────────────────
        ticker_articles: dict[str, list[dict]] = {sym: [] for sym in self.watchlist}
        for art in articles:
            for ts in art.get("ticker_sentiment", []):
                sym = ts.get("ticker", "").upper()
                if sym in ticker_articles:
                    ticker_articles[sym].append(art)

        # ── Step 3: Fundamentals (FMP profile + ratios bundle) ────────────────
        # FMP is the sole fundamentals source for all watchlist symbols. Symbols
        # FMP cannot supply a profile for are marked "missing" (and treated as
        # neutral-fundamentals downstream, not penalised).
        #
        # overview_source:          "fresh" | "missing"
        # _fundamentals_source_map: "fmp"   | "missing"
        fundamentals_map:         dict[str, dict] = {}
        overview_source:          dict[str, str]  = {}
        _fundamentals_source_map: dict[str, str]  = {}

        if self._fmp_enabled:
            for _sym in self.watchlist:
                _prof = _fmp_profiles.get(_sym)
                if _prof:
                    fundamentals_map[_sym] = parse_fmp_fundamentals_bundle(
                        _prof, _fmp_quotes.get(_sym), _fmp_ratios.get(_sym)
                    )
                    overview_source[_sym]          = "fresh"
                    _fundamentals_source_map[_sym] = "fmp"
                    logger.debug(
                        "Fundamentals FMP for %s (sector=%s, profit_margin=%s)",
                        _sym,
                        fundamentals_map[_sym].get("sector"),
                        fundamentals_map[_sym].get("profit_margin"),
                    )

        # Symbols without an FMP profile → neutral / missing (no other provider).
        for _sym in self.watchlist:
            if _sym not in _fundamentals_source_map:
                overview_source[_sym]          = "missing"
                _fundamentals_source_map[_sym] = "missing"

        _n_fmp_fund  = sum(1 for v in _fundamentals_source_map.values() if v == "fmp")
        _n_miss_fund = sum(1 for v in _fundamentals_source_map.values() if v == "missing")
        logger.info(
            "Fundamentals: %d FMP, %d missing", _n_fmp_fund, _n_miss_fund,
        )

        # ── Step 4: Scan each symbol ──────────────────────────────────────────
        results: list[WatchlistRow] = []
        for symbol in self.watchlist:
            try:
                result = self._scan_symbol(
                    symbol,
                    ticker_articles.get(symbol, []),
                    fundamentals_map.get(symbol, {}),
                    ov_source=overview_source.get(symbol, "fresh"),
                    dry_run=dry_run,
                    fmp_quote=_fmp_quotes.get(symbol),
                    fundamentals_source=_fundamentals_source_map.get(symbol, "missing"),
                    news_source=news_source,
                    fmp_historical=_fmp_historical.get(symbol),
                )
                if result:
                    results.append(result)
            except _FMPBudgetExceeded:
                # FMP call budget exhausted mid-scan — stop gracefully.
                logger.warning("FMP budget exhausted mid-scan — stopping after %d symbols", len(results))
                break
            except Exception as exc:
                logger.warning("Error scanning %s: %s", symbol, exc)

        # data_freshness mirrors data_quality for backward compat.
        for _r in results:
            _r["data_freshness"] = "fresh" if _r.get("data_quality") == "fresh" else "cached"

        # Log historical data routing summary
        _n_fmp_hist = sum(1 for r in results if r.get("historical_source") == "fmp")
        _n_tot      = len(results)
        logger.info(
            "FMP historical used for %d/%d symbols", _n_fmp_hist, _n_tot,
        )

        # ── Step 4b: Soft theme alignment enrichment (additive, non-blocking) ──
        # Loads outputs/latest/theme_opportunities.json produced by theme_discovery.
        # Also loads outputs/latest/theme_signals.json (LLM engine) for theme_strength_score.
        # Adds theme_* explainability fields, augmented_signal_score, and theme boost
        # to every row.  When theme artifacts are absent or empty, rows receive safe
        # defaults (theme_alignment_score=0, theme_strength_score=0, no boost).
        _theme_opps = load_theme_opportunities(self._root)
        _lm_themes = load_theme_signals(self._root)
        if _theme_opps:
            logger.info(
                "Theme alignment: %d theme(s) loaded — enriching %d result(s)",
                len(_theme_opps), len(results),
            )
        if _lm_themes:
            logger.info("Theme strength: %d LLM theme(s) loaded", len(_lm_themes))
        for r in results:
            enrich_row_with_theme(r, _theme_opps, lm_themes=_lm_themes)

        # ── Step 4c: Portfolio fit enrichment (additive, non-blocking) ──
        # Loads outputs/portfolio/portfolio_snapshot.json to evaluate how well
        # each symbol fits the current portfolio (sector, diversification, cash).
        # Adds portfolio_fit_score, portfolio_fit_label, portfolio_fit_reason to every row.
        # When the snapshot is absent or empty, rows receive neutral defaults (score=0.5).
        _portfolio_snapshot = load_portfolio_snapshot(self._root)
        if _portfolio_snapshot:
            logger.info("Portfolio fit: snapshot loaded — enriching %d result(s)", len(results))
        for r in results:
            enrich_row_with_portfolio_fit(r, _portfolio_snapshot)

        # ── Step 5: Enrich results with alert_priority + trusted_signal_score ──
        # alert_priority: "high" | "normal" | "watch" | None (suppressed)
        # trusted_signal_score: confidence-adjusted rank for ordering alerts
        _old_eligible_count = 0
        for r in results:
            # Count what would have alerted under the old signal-only rules
            if (
                abs(r.get("price_change_pct") or 0) >= self._price_alert_pct
                or r.get("volume_spike")
                or float(r.get("signal_score") or 0) >= self._min_signal
                or float(r.get("avg_sentiment") or 0) >= 0.25
            ):
                _old_eligible_count += 1

            decision = self._evaluate_alert_decision(r)
            r["routed_alert_priority"] = decision["priority"]
            r["alert_priority"] = decision["priority"]
            r["alert_basis"] = decision["basis"]
            r["alert_basis_summary"] = decision["basis_summary"]
            r["alert_decision_reason"] = decision["reason"]
            r["alert_decision_code"] = decision["code"]
            r["alert_confirmation_signals"] = decision.get("confirmation_signals", [])
            r["alert_confirmation_summary"] = decision.get("confirmation_summary", "none")
            r["confirmation_count"] = len(r["alert_confirmation_signals"])
            r["evidence_categories"] = decision.get("evidence_categories", [])
            r["evidence_breadth"] = decision.get("evidence_breadth", 0)
            r["alert_quality_tier"] = decision.get("alert_quality_tier", "none")
            conf = float(r.get("confidence_score") or 0.0)
            r["trusted_signal_score"] = round(
                float(r.get("signal_score") or 0.0) * (0.7 + 0.3 * conf), 4
            )
            r["trusted_augmented_signal_score"] = round(
                float(r.get("augmented_signal_score") or r.get("signal_score") or 0.0)
                * (0.7 + 0.3 * conf),
                4,
            )
            filter_decision = should_emit_alert(r, self._signals_config)
            r["filter_allowed"] = bool(filter_decision["allowed"])
            r["filter_reason"] = filter_decision["reason"]
            r["filter_reason_code"] = filter_decision["reason_code"]
            r["filtered_reason"] = "" if r["filter_allowed"] else filter_decision["reason_code"]
            r["alert_tier"] = filter_decision.get("tier")
            r["cooldown_applied_hours"] = filter_decision.get("cooldown_applied_hours")
            r["evidence_count"] = int(filter_decision.get("evidence_count", r.get("evidence_breadth", 0)))
            if not r["filter_allowed"]:
                r["alert_priority"] = None
            apply_priority_score(r, self._ranking_config, approved_weights_config=_approved_weights_config)

        # Identify alerts and sort by priority_score (primary), trusted_signal_score
        # (secondary), then theme_alignment_score as a soft tiebreaker.
        # Theme never overrides alert gating — it only affects ordering among
        # already-eligible alerts.
        alerts = [r for r in results if r["alert_priority"] is not None and r.get("filter_allowed", True)]
        alerts.sort(
            key=lambda x: (
                x.get("priority_score", 0.0),
                x.get("trusted_signal_score", 0.0),
                x.get("final_rank_score", 0.0),
            ),
            reverse=True,
        )

        n_watch_level = sum(1 for r in alerts if r.get("alert_priority") == "watch")
        n_conf_suppressed = max(0, _old_eligible_count - len(alerts))
        if n_conf_suppressed:
            logger.info(
                "Confidence filter: %d signal(s) suppressed (low confidence, not exceptional)",
                n_conf_suppressed,
            )

        # ── Top-N fallback: surface best-ranked results when no alerts pass ────
        _fallback_used = False

        # ── Step 6: Build scan quality summary ───────────────────────────────
        quality_counts: dict[str, int] = {"fresh": 0, "cached": 0, "partial": 0}
        for r in results:
            q = r.get("data_quality", "fresh")
            quality_counts[q] = quality_counts.get(q, 0) + 1

        _n_live   = quality_counts["fresh"] + quality_counts["partial"]
        _n_cached = quality_counts["cached"]
        scan_status = "ok" if _n_cached == 0 else (
            "cache_only" if _n_live == 0 else "degraded"
        )
        scan_summary = {
            "scan_status":             scan_status,
            "symbols_fresh":           quality_counts["fresh"],
            "symbols_cached":          _n_cached,
            "symbols_partial":         quality_counts["partial"],
            "symbols_budget_skipped":  0,  # merged into partial/cached in FMP-primary model
            "alerts_watch_level":      n_watch_level,
            "signals_conf_suppressed": n_conf_suppressed,
            "fallback_alerts_used":    _fallback_used,
            "fallback_alert_count":    0,
            "fallback_trigger_stage":  None,
        }
        if scan_status == "cache_only":
            logger.warning(
                "Scan quality: 0 fresh, %d cached — status=cache_only", _n_cached
            )
        elif scan_status == "degraded":
            logger.warning(
                "Scan quality: %d fresh, %d partial (live price), %d cached",
                quality_counts["fresh"], quality_counts["partial"], _n_cached,
            )
        else:
            logger.info("Scan quality: %d fresh", quality_counts["fresh"])

        logger.info(
            "WatchlistScanner done: %d results, %d alerts "
            "(%d fresh, %d partial, %d cached; %d calls used; status=%s)",
            len(results), len(alerts),
            quality_counts["fresh"], quality_counts["partial"], _n_cached,
            self._cache.calls_today, scan_status,
        )

        return {
            "run_date":     date.today().isoformat(),
            "generated_at": datetime.now().isoformat(),
            "calls_used":   self._cache.calls_today,
            "scan_summary": scan_summary,
            "results":      results,
            "alerts":       alerts,
        }

    # ── Per-symbol logic ───────────────────────────────────────────────────

    def _scan_symbol(
        self,
        symbol: str,
        articles: list[dict],
        fundamentals: dict[str, Any],
        ov_source: str = "fresh",
        dry_run: bool = False,
        fmp_quote: dict[str, Any] | None = None,
        fundamentals_source: str = "missing",
        news_source: str = "none",
        fmp_historical: list[dict] | None = None,
    ) -> Optional[WatchlistRow]:
        """Fetch OHLCV + compute technicals + classify themes + score for one symbol."""

        # --- Daily OHLCV ---------------------------------------------------------
        # Provider order (FMP-primary):
        #   1. FMP quote        → price, 1d change, sma50, volume (live)
        #   2. FMP historical   → SMA20, 5d change (enriches FMP quote or standalone)
        # When FMP supplies neither, technicals stay empty (price_data_source="missing").
        tech: dict[str, Any] = {}
        price_data_source:  str = "missing"
        historical_source:  str = "missing"

        # ── 1. FMP quote: current price, 1d change, sma50, volume ─────────────
        if fmp_quote and self._fmp_enabled:
            _fmp_tech = _technicals_from_fmp_quote(fmp_quote, self._spike_factor)
            if _fmp_tech:
                tech = _fmp_tech
                price_data_source = "fmp"

        # ── 2. FMP historical: SMA20, 5d change, volume_avg20 ─────────────────
        # Merges with FMP quote technicals when quote is primary; provides full
        # technicals standalone when no quote is available.
        if fmp_historical and self._fmp_enabled:
            _hist_df = _df_from_fmp_historical(fmp_historical)
            if _hist_df is not None:
                _hist_tech = _compute_technicals(_hist_df, self._spike_factor)
                if _hist_tech:
                    if price_data_source == "fmp":
                        # Enrich FMP quote with time-series indicators it cannot provide
                        for _k in ("price_change_5d", "sma20", "volume_avg20", "data_days"):
                            if _hist_tech.get(_k) is not None:
                                tech[_k] = _hist_tech[_k]
                        # Recompute above_sma20 using FMP quote price (not historical close)
                        _sma20 = tech.get("sma20")
                        _price = tech.get("price")
                        if _sma20 and _price:
                            tech["above_sma20"] = bool(_price > _sma20)
                        # Recompute volume_spike with historical avg20
                        if tech.get("volume_today") and tech.get("volume_avg20"):
                            tech["volume_spike"] = bool(
                                tech["volume_today"] > tech["volume_avg20"] * self._spike_factor
                            )
                    else:
                        # No quote: historical gives us full technicals including price
                        tech = _hist_tech
                        price_data_source = "fmp"
                    historical_source = "fmp"
                    logger.debug(
                        "Technical indicators computed from FMP for %s "
                        "(sma20=%s, price_change_5d=%s, vol_avg20=%s)",
                        symbol, tech.get("sma20"), tech.get("price_change_5d"),
                        tech.get("volume_avg20"),
                    )

        # --- Technical data completeness label --------------------------------
        _t_price   = tech.get("price") is not None
        _t_sma20   = tech.get("sma20") is not None
        _t_sma50   = tech.get("sma50") is not None
        _t_5d      = tech.get("price_change_5d") is not None
        _t_volavg  = tech.get("volume_avg20") is not None
        if _t_price and _t_sma20 and _t_sma50 and _t_5d and _t_volavg:
            technical_data_completeness: str = "full"
        elif _t_price and (_t_sma20 or _t_5d):
            technical_data_completeness = "partial"
        elif _t_price:
            technical_data_completeness = "price_only"
        else:
            technical_data_completeness = "missing"

        # --- Theme classification from ticker-specific articles ---------------
        headlines = [
            (art.get("title", "") + " " + art.get("summary", ""))
            for art in articles
            if art.get("title")
        ]
        theme_scores   = te.classify_headlines(headlines)
        active_themes  = te.top_themes(theme_scores, min_score=self._theme_threshold)
        headline_examples = te.extract_headline_examples(articles, active_themes, max_per_theme=2)
        avg_sentiment  = _compute_avg_sentiment(articles)

        # --- Fundamental context score ----------------------------------------
        # Use neutral score when fundamentals are missing (no FMP profile) to
        # avoid penalising the symbol for absent data rather than poor fundamentals.
        if ov_source == "missing":
            fund_score = _NEUTRAL_FUND_SCORE
        else:
            fund_score = fundamental_context_score(fundamentals)

        # --- Composite signal score -------------------------------------------
        signal_score, breakdown = _compute_signal_score(
            tech, theme_scores, articles, fund_score, self._price_alert_pct,
        )

        # --- Data quality label -----------------------------------------------
        # fresh:   live FMP price + live FMP fundamentals
        # partial: live FMP price, fundamentals missing
        # cached:  no live price data (missing)
        _has_live_price = price_data_source == "fmp"
        _has_live_fund  = ov_source == "fresh" or fundamentals_source == "fmp"
        if _has_live_price and _has_live_fund:
            data_quality = "fresh"
        elif _has_live_price:
            data_quality = "partial"
        else:
            data_quality = "cached"

        # --- Confidence scoring -----------------------------------------------
        # Measures trustworthiness of this result's data provenance/completeness.
        # Distinct from signal_score (which measures investment attractiveness).
        ov_cache_age = self._cache.get_age_seconds(f"overview_{symbol}")
        confidence_score, confidence_band, confidence_reasons = compute_confidence(
            data_quality=data_quality,
            ov_source=ov_source,
            tech=tech,
            fundamentals=fundamentals,
            articles=articles,
            cache_age_seconds=ov_cache_age,
        )

        return {
            "ticker":       symbol,
            "scan_time":    datetime.now().isoformat(),
            "data_quality": data_quality,
            "confidence_score":   confidence_score,
            "confidence_band":    confidence_band,
            "confidence_reasons": confidence_reasons,

            # ── Backward-compat top-level fields (used by CSV/MD writers) ──
            "price":            tech.get("price"),
            "price_change_pct": tech.get("price_change_1d"),    # 1-day change
            "above_sma20":      tech.get("above_sma20"),
            "above_sma50":      tech.get("above_sma50"),
            "volume_spike":     tech.get("volume_spike", False),
            "themes":           active_themes,
            "headline_examples": headline_examples,
            "signal_score":     signal_score,
            # legacy flat fields
            "sma20":            tech.get("sma20"),
            "sma50":            tech.get("sma50"),
            "volume_today":     tech.get("volume_today"),
            "volume_avg20":     tech.get("volume_avg20"),
            "theme_scores":     {k: round(v, 3) for k, v in theme_scores.items() if v > 0},
            "news_count":       len(articles),
            "avg_sentiment":    avg_sentiment,

            # ── Structured sub-objects ────────────────────────────────────────
            "fundamentals":     fundamentals,
            "news": {
                "headline_count": len(articles),
                "avg_sentiment":  avg_sentiment,
                "themes":         active_themes,
                "theme_scores":   {k: round(v, 3) for k, v in theme_scores.items() if v > 0},
                "top_headlines":  headline_examples,
            },
            "technicals": {
                "price":           tech.get("price"),
                "price_change_1d": tech.get("price_change_1d"),
                "price_change_5d": tech.get("price_change_5d"),
                "sma20":           tech.get("sma20"),
                "sma50":           tech.get("sma50"),
                "above_sma20":     tech.get("above_sma20"),
                "above_sma50":     tech.get("above_sma50"),
                "volume_today":    tech.get("volume_today"),
                "volume_avg20":    tech.get("volume_avg20"),
                "volume_spike":    tech.get("volume_spike", False),
                "data_days":       tech.get("data_days"),
            },
            "score_breakdown": breakdown,

            # ── FMP-enhanced fundamentals fields ──────────────────────────────
            # fundamentals_score and valuation_score are 0-100 derived values.
            # revenue_growth / earnings_growth / debt_ratio are None when the
            # data source (AV free-tier or FMP free-tier profile) doesn't
            # supply them — callers must handle None safely.
            "fundamentals_score":  derive_fundamentals_score(fundamentals),
            "valuation_score":     derive_valuation_score(fundamentals),
            "revenue_growth":      fundamentals.get("revenue_growth"),
            "earnings_growth":     fundamentals.get("earnings_growth"),
            "debt_ratio":          fundamentals.get("debt_ratio"),

            # ── Data-source provenance ────────────────────────────────────────
            "price_data_source":           price_data_source,
            "quote_source":                price_data_source,        # alias
            "historical_source":           historical_source,
            "technical_data_completeness": technical_data_completeness,
            "fundamentals_source": fundamentals_source,
            "ratios_source":       "fmp" if fundamentals_source == "fmp" else "missing",
            "news_source":         news_source,
            "technical_source":    (
                "fmp_quote+historical" if price_data_source == "fmp" and historical_source == "fmp"
                else "fmp_quote"        if price_data_source == "fmp"
                else price_data_source
            ),
            "provider_health":     (
                "fmp_primary" if price_data_source == "fmp" and fundamentals_source == "fmp"
                else "fmp_partial" if price_data_source == "fmp" or fundamentals_source == "fmp"
                else "missing"
            ),
            "fallback_used":       False,
            "fallback_reason":     "",
        }

    def _is_alert(self, result: WatchlistRow) -> bool:
        """Return True if this result warrants any alert (any non-None priority)."""
        return self._classify_alert_priority(result) is not None

    def _observable_confirmation(
        self,
        result: WatchlistRow,
    ) -> tuple[list[str], float]:
        """
        Return supporting confirmation signals for observable alerts.

        This keeps signal_score and confidence_score separate, while giving the
        alert router a way to ask whether a price/volume trigger is backed by
        other evidence before promoting it.

        "trusted score" is included as routing context, but it is not treated
        as an independent structural confirmation by itself.
        """
        signal_score = float(result.get("signal_score") or 0.0)
        confidence_score = float(result.get("confidence_score") or 0.0)
        avg_sentiment = float(result.get("avg_sentiment") or 0.0)
        score_breakdown = result.get("score_breakdown") or {}
        technical_score = float(score_breakdown.get("technical_score") or 0.0)
        theme_news_score = float(score_breakdown.get("theme_news_score") or 0.0)
        trusted_signal_score = round(signal_score * (0.7 + 0.3 * confidence_score), 4)

        confirmations: list[str] = []
        if trusted_signal_score >= 0.35:
            confirmations.append("trusted score")
        if technical_score >= 0.55:
            confirmations.append("technical strength")
        if theme_news_score >= 0.35 or avg_sentiment >= 0.25:
            confirmations.append("news/theme support")
        if bool(result.get("above_sma20")) and bool(result.get("above_sma50")):
            confirmations.append("trend alignment")

        return confirmations, trusted_signal_score

    def _assess_promotion_quality(self, result: WatchlistRow) -> AlertDecision:
        """
        Assess independent evidence breadth without changing signal_score/confidence_score.

        This is an additive routing layer for operator-grade promotion quality.
        It keeps raw components inspectable while answering a different question:
        how broad is the evidence behind a promotion decision?

        Important: the confidence-adjusted/trusted score is useful routing context,
        but it is derived from signal_score + confidence_score and is therefore not
        counted as an independent evidence category. That avoids double-counting
        theme-heavy or otherwise one-factor signals as "confirmed".
        """
        score_breakdown = result.get("score_breakdown") or {}
        technical_score = float(score_breakdown.get("technical_score") or 0.0)
        theme_news_score = float(score_breakdown.get("theme_news_score") or 0.0)
        fundamental_score = float(score_breakdown.get("fundamental_context_score") or 0.0)
        avg_sentiment = float(result.get("avg_sentiment") or 0.0)
        news_count = int(result.get("news_count") or 0)
        data_quality = str(result.get("data_quality") or "fresh")
        signal_score = float(result.get("signal_score") or 0.0)
        confidence_score = float(result.get("confidence_score") or 0.0)
        trusted_signal_score = round(signal_score * (0.7 + 0.3 * confidence_score), 4)

        categories: list[str] = []
        if technical_score >= 0.45 or (bool(result.get("above_sma20")) and bool(result.get("above_sma50"))):
            categories.append("technical")
        if theme_news_score >= 0.35 or (news_count >= 2 and avg_sentiment >= 0.15):
            categories.append("news_theme")
        if fundamental_score >= 0.55:
            categories.append("fundamentals")

        breadth = len(categories)
        quality_tier = "none"
        if breadth >= 3:
            quality_tier = "broad"
        elif breadth == 2:
            quality_tier = "confirmed"
        elif breadth == 1:
            quality_tier = "thin"

        if data_quality == "partial":
            if quality_tier == "broad":
                quality_tier = "confirmed"
            elif quality_tier == "confirmed":
                quality_tier = "thin"
            elif quality_tier == "thin":
                quality_tier = "none"
        elif data_quality == "cached":
            if quality_tier == "broad":
                quality_tier = "confirmed"
            elif quality_tier == "confirmed":
                quality_tier = "thin"
            elif quality_tier == "thin":
                quality_tier = "none"

        return {
            "evidence_categories": categories,
            "evidence_breadth": breadth,
            "alert_quality_tier": quality_tier,
            "trusted_signal_score": trusted_signal_score,
            "composite_support": trusted_signal_score >= 0.45,
        }

    def _evaluate_alert_decision(self, result: WatchlistRow) -> AlertDecision:
        """
        Return alert routing metadata for one result.

        The output is designed to stay operator-readable in JSON/summary output
        while also giving tests a stable, machine-checkable decision code.
        """
        price_change = abs(result.get("price_change_pct") or 0.0)
        volume_spike = bool(result.get("volume_spike"))
        signal_score = float(result.get("signal_score") or 0.0)
        avg_sentiment = float(result.get("avg_sentiment") or 0.0)
        confidence_score = float(result.get("confidence_score") or 0.0)

        basis: list[str] = []
        if price_change >= self._price_alert_pct:
            basis.append("price_move")
        if volume_spike:
            basis.append("volume_spike")
        if signal_score >= self._min_signal:
            basis.append("signal_score")
        if avg_sentiment >= 0.25:
            basis.append("sentiment")

        if not basis:
            return {
                "priority": None,
                "basis": [],
                "basis_summary": "none",
                "reason": "suppressed: below observable and signal thresholds",
                "code": "below_threshold",
                "confirmation_signals": [],
                "confirmation_summary": "none",
            }

        observable = "price_move" in basis or "volume_spike" in basis
        confirmation_signals, trusted_signal_score = self._observable_confirmation(result)
        confirmation_summary = ", ".join(confirmation_signals) if confirmation_signals else "none"
        structural_confirmation_signals = [
            signal for signal in confirmation_signals if signal != "trusted score"
        ]
        structural_confirmation_summary = (
            ", ".join(structural_confirmation_signals)
            if structural_confirmation_signals
            else "none"
        )
        trusted_only_confirmation = bool(confirmation_signals) and not structural_confirmation_signals
        strong_observable_move = price_change >= max(self._price_alert_pct * 2, 6.0)
        quality = self._assess_promotion_quality(result)
        evidence_categories = quality["evidence_categories"]
        evidence_breadth = quality["evidence_breadth"]
        alert_quality_tier = quality["alert_quality_tier"]

        if confidence_score >= self._conf_high:
            if observable:
                if structural_confirmation_signals:
                    return {
                        "priority": "high",
                        "basis": basis,
                        "basis_summary": ", ".join(basis),
                        "reason": (
                            "high confidence observable trigger confirmed by "
                            f"{structural_confirmation_summary}"
                        ),
                        "code": "high_conf_observable_confirmed",
                        "confirmation_signals": confirmation_signals,
                        "confirmation_summary": confirmation_summary,
                        "evidence_categories": evidence_categories,
                        "evidence_breadth": evidence_breadth,
                        "alert_quality_tier": alert_quality_tier,
                    }
                if strong_observable_move:
                    reason = (
                        "high confidence observable trigger kept because the move is large, "
                        "but confirmation is limited"
                    )
                    if trusted_only_confirmation:
                        reason = (
                            "high confidence observable trigger kept because the move is large, "
                            "but trusted score alone was not enough for full promotion"
                        )
                    return {
                        "priority": "normal",
                        "basis": basis,
                        "basis_summary": ", ".join(basis),
                        "reason": reason,
                        "code": "high_conf_observable_large_move",
                        "confirmation_signals": confirmation_signals,
                        "confirmation_summary": confirmation_summary,
                        "evidence_categories": evidence_categories,
                        "evidence_breadth": evidence_breadth,
                        "alert_quality_tier": alert_quality_tier,
                    }
                reason = (
                    "observable trigger lacked confirmation from trusted score, trend, "
                    "technicals, or news; demoted to watch"
                )
                if trusted_only_confirmation:
                    reason = (
                        "observable trigger had trusted score support, but lacked independent "
                        "confirmation from trend, technicals, or news; demoted to watch"
                    )
                return {
                    "priority": "watch",
                    "basis": basis,
                    "basis_summary": ", ".join(basis),
                    "reason": reason,
                    "code": "high_conf_observable_unconfirmed",
                    "confirmation_signals": confirmation_signals,
                    "confirmation_summary": confirmation_summary,
                    "evidence_categories": evidence_categories,
                    "evidence_breadth": evidence_breadth,
                    "alert_quality_tier": alert_quality_tier,
                }
            if signal_score >= 0.70:
                if alert_quality_tier == "broad":
                    return {
                        "priority": "high",
                        "basis": basis,
                        "basis_summary": ", ".join(basis),
                        "reason": "high confidence plus strong signal with broad evidence agreement",
                        "code": "high_conf_strong_signal_broad",
                        "confirmation_signals": evidence_categories,
                        "confirmation_summary": ", ".join(evidence_categories) or "none",
                        "evidence_categories": evidence_categories,
                        "evidence_breadth": evidence_breadth,
                        "alert_quality_tier": alert_quality_tier,
                    }
                if alert_quality_tier == "confirmed":
                    return {
                        "priority": "normal",
                        "basis": basis,
                        "basis_summary": ", ".join(basis),
                        "reason": "high confidence strong signal confirmed by multiple evidence categories",
                        "code": "high_conf_strong_signal_confirmed",
                        "confirmation_signals": evidence_categories,
                        "confirmation_summary": ", ".join(evidence_categories) or "none",
                        "evidence_categories": evidence_categories,
                        "evidence_breadth": evidence_breadth,
                        "alert_quality_tier": alert_quality_tier,
                    }
                if alert_quality_tier == "thin":
                    return {
                        "priority": "watch",
                        "basis": basis,
                        "basis_summary": ", ".join(basis),
                        "reason": "strong signal is real, but evidence breadth is thin; kept at watch",
                        "code": "high_conf_strong_signal_thin",
                        "confirmation_signals": evidence_categories,
                        "confirmation_summary": ", ".join(evidence_categories) or "none",
                        "evidence_categories": evidence_categories,
                        "evidence_breadth": evidence_breadth,
                        "alert_quality_tier": alert_quality_tier,
                    }
                return {
                    "priority": None,
                    "basis": basis,
                    "basis_summary": ", ".join(basis),
                    "reason": "suppressed: strong signal lacked enough independent evidence categories",
                    "code": "high_conf_strong_signal_unconfirmed",
                    "confirmation_signals": [],
                    "confirmation_summary": "none",
                    "evidence_categories": evidence_categories,
                    "evidence_breadth": evidence_breadth,
                    "alert_quality_tier": alert_quality_tier,
                }
            if alert_quality_tier in {"broad", "confirmed"}:
                return {
                    "priority": "normal",
                    "basis": basis,
                    "basis_summary": ", ".join(basis),
                    "reason": "high confidence meets threshold with confirmed evidence breadth",
                    "code": "high_conf_standard_signal_confirmed",
                    "confirmation_signals": evidence_categories,
                    "confirmation_summary": ", ".join(evidence_categories) or "none",
                    "evidence_categories": evidence_categories,
                    "evidence_breadth": evidence_breadth,
                    "alert_quality_tier": alert_quality_tier,
                }
            if alert_quality_tier == "thin":
                return {
                    "priority": "watch",
                    "basis": basis,
                    "basis_summary": ", ".join(basis),
                    "reason": "high confidence signal passed threshold, but evidence breadth is thin",
                    "code": "high_conf_standard_signal_thin",
                    "confirmation_signals": evidence_categories,
                    "confirmation_summary": ", ".join(evidence_categories) or "none",
                    "evidence_categories": evidence_categories,
                    "evidence_breadth": evidence_breadth,
                    "alert_quality_tier": alert_quality_tier,
                }
            return {
                "priority": None,
                "basis": basis,
                "basis_summary": ", ".join(basis),
                "reason": "suppressed: signal crossed threshold but evidence breadth is insufficient",
                "code": "high_conf_standard_signal_unconfirmed",
                "confirmation_signals": [],
                "confirmation_summary": "none",
                "evidence_categories": evidence_categories,
                "evidence_breadth": evidence_breadth,
                "alert_quality_tier": alert_quality_tier,
            }

        if confidence_score >= self._conf_medium:
            if observable:
                if structural_confirmation_signals:
                    return {
                        "priority": "normal",
                        "basis": basis,
                        "basis_summary": ", ".join(basis),
                        "reason": (
                            "medium confidence observable trigger confirmed by "
                            f"{structural_confirmation_summary}"
                        ),
                        "code": "medium_conf_observable_confirmed",
                        "confirmation_signals": confirmation_signals,
                        "confirmation_summary": confirmation_summary,
                        "evidence_categories": evidence_categories,
                        "evidence_breadth": evidence_breadth,
                        "alert_quality_tier": alert_quality_tier,
                    }
                if strong_observable_move:
                    reason = (
                        "medium confidence observable trigger kept as watch because the move "
                        "is large, but confirmation is limited"
                    )
                    if trusted_only_confirmation:
                        reason = (
                            "medium confidence observable trigger kept as watch because the move "
                            "is large, but trusted score alone was not enough for promotion"
                        )
                    return {
                        "priority": "watch",
                        "basis": basis,
                        "basis_summary": ", ".join(basis),
                        "reason": reason,
                        "code": "medium_conf_observable_large_move",
                        "confirmation_signals": confirmation_signals,
                        "confirmation_summary": confirmation_summary,
                        "evidence_categories": evidence_categories,
                        "evidence_breadth": evidence_breadth,
                        "alert_quality_tier": alert_quality_tier,
                    }
                reason = "suppressed: medium confidence observable trigger lacked confirmation"
                if trusted_only_confirmation:
                    reason = (
                        "suppressed: medium confidence observable trigger had trusted score "
                        "support, but no independent confirmation"
                    )
                return {
                    "priority": None,
                    "basis": basis,
                    "basis_summary": ", ".join(basis),
                    "reason": reason,
                    "code": "medium_conf_observable_unconfirmed",
                    "confirmation_signals": confirmation_signals,
                    "confirmation_summary": confirmation_summary,
                    "evidence_categories": evidence_categories,
                    "evidence_breadth": evidence_breadth,
                    "alert_quality_tier": alert_quality_tier,
                }
            if signal_score >= self._medium_conf_min_signal:
                if alert_quality_tier in {"broad", "confirmed"}:
                    return {
                        "priority": "watch",
                        "basis": basis,
                        "basis_summary": ", ".join(basis),
                        "reason": "medium confidence signal cleared the higher bar with confirmed evidence breadth",
                        "code": "medium_conf_signal_confirmed",
                        "confirmation_signals": evidence_categories,
                        "confirmation_summary": ", ".join(evidence_categories) or "none",
                        "evidence_categories": evidence_categories,
                        "evidence_breadth": evidence_breadth,
                        "alert_quality_tier": alert_quality_tier,
                    }
                return {
                    "priority": None,
                    "basis": basis,
                    "basis_summary": ", ".join(basis),
                    "reason": "suppressed: medium confidence signal lacked enough evidence breadth",
                    "code": "medium_conf_signal_thin",
                    "confirmation_signals": evidence_categories,
                    "confirmation_summary": ", ".join(evidence_categories) or "none",
                    "evidence_categories": evidence_categories,
                    "evidence_breadth": evidence_breadth,
                    "alert_quality_tier": alert_quality_tier,
                }
            return {
                "priority": None,
                "basis": basis,
                "basis_summary": ", ".join(basis),
                "reason": "suppressed: medium confidence signal did not clear the higher bar",
                "code": "medium_conf_suppressed",
                "confirmation_signals": [],
                "confirmation_summary": "none",
                "evidence_categories": evidence_categories,
                "evidence_breadth": evidence_breadth,
                "alert_quality_tier": alert_quality_tier,
            }

        if observable:
            if structural_confirmation_signals:
                return {
                    "priority": "watch",
                    "basis": basis,
                    "basis_summary": ", ".join(basis),
                    "reason": (
                        "low confidence observable trigger preserved as watch because it is "
                        f"confirmed by {structural_confirmation_summary}"
                    ),
                    "code": "low_conf_observable_confirmed",
                    "confirmation_signals": confirmation_signals,
                    "confirmation_summary": confirmation_summary,
                    "evidence_categories": evidence_categories,
                    "evidence_breadth": evidence_breadth,
                    "alert_quality_tier": alert_quality_tier,
                }
            if strong_observable_move:
                reason = "low confidence large observable move preserved as watch despite limited confirmation"
                if trusted_only_confirmation:
                    reason = (
                        "low confidence large observable move preserved as watch, but trusted "
                        "score alone did not qualify as independent confirmation"
                    )
                return {
                    "priority": "watch",
                    "basis": basis,
                    "basis_summary": ", ".join(basis),
                    "reason": reason,
                    "code": "low_conf_observable_large_move",
                    "confirmation_signals": confirmation_signals,
                    "confirmation_summary": confirmation_summary,
                    "evidence_categories": evidence_categories,
                    "evidence_breadth": evidence_breadth,
                    "alert_quality_tier": alert_quality_tier,
                }
            reason = "suppressed: low confidence observable trigger lacked confirmation"
            if trusted_only_confirmation:
                reason = (
                    "suppressed: low confidence observable trigger had trusted score support, "
                    "but no independent confirmation"
                )
            return {
                "priority": None,
                "basis": basis,
                "basis_summary": ", ".join(basis),
                "reason": reason,
                "code": "low_conf_observable_unconfirmed",
                "confirmation_signals": confirmation_signals,
                "confirmation_summary": confirmation_summary,
                "evidence_categories": evidence_categories,
                "evidence_breadth": evidence_breadth,
                "alert_quality_tier": alert_quality_tier,
            }
        if confidence_score >= self._conf_min and signal_score >= self._exceptional_signal:
            if alert_quality_tier in {"broad", "confirmed"}:
                return {
                    "priority": "watch",
                    "basis": basis,
                    "basis_summary": ", ".join(basis),
                    "reason": "low confidence but exceptional signal preserved because evidence breadth is confirmed",
                    "code": "low_conf_exceptional_signal_confirmed",
                    "confirmation_signals": evidence_categories,
                    "confirmation_summary": ", ".join(evidence_categories) or "none",
                    "evidence_categories": evidence_categories,
                    "evidence_breadth": evidence_breadth,
                    "alert_quality_tier": alert_quality_tier,
                }
            return {
                "priority": None,
                "basis": basis,
                "basis_summary": ", ".join(basis),
                "reason": "suppressed: exceptional signal lacked enough breadth to offset low confidence",
                "code": "low_conf_exceptional_signal_thin",
                "confirmation_signals": evidence_categories,
                "confirmation_summary": ", ".join(evidence_categories) or "none",
                "evidence_categories": evidence_categories,
                "evidence_breadth": evidence_breadth,
                "alert_quality_tier": alert_quality_tier,
            }
        return {
            "priority": None,
            "basis": basis,
            "basis_summary": ", ".join(basis),
            "reason": "suppressed: low confidence without an exceptional or observable trigger",
            "code": "low_conf_suppressed",
            "confirmation_signals": [],
            "confirmation_summary": "none",
            "evidence_categories": evidence_categories,
            "evidence_breadth": evidence_breadth,
            "alert_quality_tier": alert_quality_tier,
        }

    def _classify_alert_priority(self, result: WatchlistRow) -> Optional[str]:
        """
        Return the alert priority level, or None if the alert should be suppressed.

        Priority levels
        ---------------
        "high"   — high confidence + strong signal or observable trigger
        "normal" — high confidence + meets standard threshold, OR medium confidence
                   + observable trigger
        "watch"  — medium confidence + meets higher signal bar, OR low confidence +
                   exceptional signal, OR low confidence + observable trigger
        None     — suppressed: signal too weak, or confidence too low without exception

        Confidence bands (config-driven defaults)
        -----------------------------------------
        high   >= 0.75  → full alert eligibility
        medium  0.60–0.74 → higher signal bar (MEDIUM_CONF_MIN_SIGNAL = 0.60)
        low    < 0.60  → only exceptional (EXCEPTIONAL_SIGNAL_SCORE = 0.85) or observable trigger
        """
        return self._evaluate_alert_decision(result)["priority"]
