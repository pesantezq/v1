"""
S&P 500 Candidate Scanner

Applies hard filters and a multi-factor score to rank S&P 500 stocks
for inclusion in the speculative sleeve.

Scoring (0–100 total):
  Revenue growth    0–30 pts  (15 % → 0, 40 %+ → 30, linear)
  FCF yield         0–25 pts  (5 % yield → 25 pts)
  ROE               0–20 pts  (30 % ROE → 20 pts)
  PE attractiveness 0–15 pts  (≤15 → 15, ≤25 → 12, ≤35 → 8, ≤50 → 3, >50 → 0)
  Trend (>200 DMA)  0–10 pts  (binary)

Hard filters (any failing stock goes to candidates_debug.csv):
  mktCap            >= min_mkt_cap       (config default 5 B)
  revenueGrowth     >= min_rev_growth    (config default 15 %)
  peRatio           <= 50                (bubble guard)
  freeCashFlowYield >= 0                 (positive free cash flow required)
  price > priceAvg200                    (trend filter, if enabled)

API contract: this module makes ZERO direct API calls.
All data is consumed from pre-loaded dicts passed by main.py.

Run-mode entry points:
  full_scan()      — monthly: score every S&P 500 symbol from bulk data
  weekly_refresh() — weekly:  rescore Top-k using fresh metrics + quotes
  daily_refresh()  — daily:   update price/trend fields only (no metrics)
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger('portfolio_automation.scanner')

_WATCHLIST_PATH = Path('data/fmp_cache/top100_watchlist.json')


class CandidateScanner:
    """
    Filter and score S&P 500 candidates from pre-loaded bulk FMP data.

    The scanner never makes its own HTTP requests; it receives bulk data
    dicts from main.py so that all API-call accounting stays in FMPClient.
    """

    def __init__(
        self,
        min_mkt_cap: float = 5e9,
        min_rev_growth: float = 0.15,
        trend_filter_200dma: bool = True,
        top_k: int = 100,
        watchlist_path: Optional[Path] = None,
    ) -> None:
        self.min_mkt_cap = min_mkt_cap
        self.min_rev_growth = min_rev_growth
        self.trend_filter_200dma = trend_filter_200dma
        self.top_k = top_k
        self._watchlist_path = watchlist_path or _WATCHLIST_PATH

    # ------------------------------------------------------------------
    # Public run-mode entry points
    # ------------------------------------------------------------------

    def full_scan(
        self,
        sp500_symbols: List[str],
        bulk_profiles: List[Dict],
        bulk_metrics: List[Dict],
        batch_quotes: Dict[str, Dict],
    ) -> Tuple[List[Dict], List[Dict]]:
        """
        Monthly: filter and score every symbol in sp500_symbols.

        Args:
            sp500_symbols: Current S&P 500 tickers (from FMPClient).
            bulk_profiles: List of profile dicts (mktCap, sector, …).
            bulk_metrics:  List of key-metrics dicts (revenueGrowth, roe, …).
            batch_quotes:  {symbol: quote_dict} (price, priceAvg200, …).

        Returns:
            candidates: Top-k scored candidates, sorted by score descending.
            debug_rows: One row per S&P 500 symbol with pass/fail detail.
        """
        profiles = {r['symbol']: r for r in bulk_profiles if r.get('symbol')}
        metrics = {r['symbol']: r for r in bulk_metrics if r.get('symbol')}

        candidates: List[Dict] = []
        debug_rows: List[Dict] = []

        for symbol in sp500_symbols:
            profile = profiles.get(symbol, {})
            m = metrics.get(symbol, {})
            q = batch_quotes.get(symbol, {})

            passes, failures = self._passes_hard_filters(symbol, profile, m, q)
            score = self._score(profile, m, q)
            if passes:
                candidates.append(self._build_row(symbol, profile, m, q, score))
            debug_rows.append({
                'symbol': symbol,
                'passed': passes,
                'failed_filters': '; '.join(failures) if failures else '',
                'score': round(score, 1),
            })

        candidates.sort(key=lambda r: r['score'], reverse=True)
        return candidates[:self.top_k], debug_rows

    def weekly_refresh(
        self,
        watchlist: List[Dict],
        bulk_metrics: List[Dict],
        batch_quotes: Dict[str, Dict],
    ) -> Tuple[List[Dict], List[Dict]]:
        """
        Weekly: rescore the existing Top-k watchlist with fresh metrics + quotes.
        Re-applies all hard filters; drops any symbol that now fails.
        """
        metrics = {r['symbol']: r for r in bulk_metrics if r.get('symbol')}
        updated: List[Dict] = []
        debug_rows: List[Dict] = []

        for row in watchlist:
            symbol = row['symbol']
            m = metrics.get(symbol, {})
            q = batch_quotes.get(symbol, {})
            # Reconstruct a minimal profile from stored fields
            profile = {'mktCap': row.get('mkt_cap', 0), 'sector': row.get('sector', '')}

            passes, failures = self._passes_hard_filters(symbol, profile, m, q)
            score = self._score(profile, m, q)
            if passes:
                updated.append(self._build_row(symbol, profile, m, q, score))
            debug_rows.append({
                'symbol': symbol,
                'passed': passes,
                'failed_filters': '; '.join(failures) if failures else '',
                'score': round(score, 1),
            })

        updated.sort(key=lambda r: r['score'], reverse=True)
        return updated[:self.top_k], debug_rows

    def daily_refresh(
        self,
        watchlist: List[Dict],
        batch_quotes: Dict[str, Dict],
    ) -> Tuple[List[Dict], List[Dict]]:
        """
        Daily: update price-based fields only (no fundamentals refresh).
        Re-evaluates the trend filter (price vs 200 DMA) and adjusts
        score ±10 pts if trend status changed.

        Returns (updated_watchlist, []) — no debug rows for daily mode.
        """
        updated: List[Dict] = []

        for row in watchlist:
            symbol = row['symbol']
            q = batch_quotes.get(symbol, {})

            price = float(q.get('price', 0) or row.get('price', 0) or 0)
            price_200 = float(q.get('priceAvg200', 0) or row.get('price_200dma', 0) or 0)
            above_200 = (price > price_200) if price_200 > 0 else row.get('above_200dma', True)

            # Adjust score by trend change (±10 pts)
            base_score = float(row.get('score', 0))
            was_above = row.get('above_200dma', True)
            if above_200 and not was_above:
                base_score = min(100.0, base_score + 10)
            elif not above_200 and was_above:
                base_score = max(0.0, base_score - 10)

            updated.append({
                **row,
                'price': price,
                'price_200dma': price_200,
                'above_200dma': above_200,
                'score': round(base_score, 1),
                'scanned_at': datetime.now().isoformat(),
            })

        updated.sort(key=lambda r: r['score'], reverse=True)
        return updated, []

    # ------------------------------------------------------------------
    # Watchlist persistence
    # ------------------------------------------------------------------

    def save_watchlist(self, candidates: List[Dict]) -> None:
        """Persist Top-k candidates to disk for daily/weekly reuse."""
        self._watchlist_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            'updated_at': datetime.now().isoformat(),
            'candidates': candidates,
        }
        self._watchlist_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding='utf-8',
        )
        logger.info(
            f"Watchlist saved: {len(candidates)} candidates → {self._watchlist_path}"
        )

    def load_watchlist(self) -> List[Dict]:
        """Load persisted Top-k candidates, or [] if not found / unreadable."""
        if not self._watchlist_path.exists():
            return []
        try:
            d = json.loads(self._watchlist_path.read_text(encoding='utf-8'))
            return d.get('candidates', [])
        except Exception as e:
            logger.warning(f"Failed to load watchlist: {e}")
            return []

    # ------------------------------------------------------------------
    # Filter and score (public for unit testing)
    # ------------------------------------------------------------------

    def _passes_hard_filters(
        self,
        symbol: str,
        profile: Dict,
        metrics: Dict,
        quote: Dict,
    ) -> Tuple[bool, List[str]]:
        """
        Returns (passes: bool, failures: List[str]).
        failures is empty when the stock passes all filters.
        """
        failures: List[str] = []

        # Market cap
        mkt_cap = float(profile.get('mktCap', 0) or quote.get('marketCap', 0) or 0)
        if mkt_cap < self.min_mkt_cap:
            failures.append(
                f"mkt_cap={mkt_cap / 1e9:.1f}B < {self.min_mkt_cap / 1e9:.0f}B"
            )

        # Revenue growth
        rev_growth = metrics.get('revenueGrowth')
        if rev_growth is None:
            failures.append("rev_growth=N/A")
        elif float(rev_growth) < self.min_rev_growth:
            failures.append(
                f"rev_growth={float(rev_growth):.1%} < {self.min_rev_growth:.0%}"
            )

        # PE bubble guard
        pe = metrics.get('peRatio')
        if pe is not None and float(pe) > 50:
            failures.append(f"pe={float(pe):.1f} > 50")

        # Positive FCF
        fcf_yield = metrics.get('freeCashFlowYield')
        if fcf_yield is not None and float(fcf_yield) < 0:
            failures.append(f"fcf_yield={float(fcf_yield):.1%} < 0")

        # Trend: price above 200-day MA
        if self.trend_filter_200dma:
            price = float(quote.get('price', 0) or 0)
            price_200 = float(quote.get('priceAvg200', 0) or 0)
            if price_200 > 0 and price < price_200:
                failures.append(
                    f"price={price:.2f} < 200dma={price_200:.2f}"
                )

        return len(failures) == 0, failures

    def _score(
        self,
        profile: Dict,
        metrics: Dict,
        quote: Dict,
    ) -> float:
        """Compute a 0–100 composite score for a single candidate."""
        score = 0.0

        # Revenue growth (0–30 pts): linear from min_rev_growth to 40 %
        rev_growth = float(metrics.get('revenueGrowth', 0) or 0)
        if rev_growth >= self.min_rev_growth and (0.40 - self.min_rev_growth) > 0:
            rev_pts = 30.0 * (rev_growth - self.min_rev_growth) / (0.40 - self.min_rev_growth)
            score += min(30.0, max(0.0, rev_pts))

        # FCF yield (0–25 pts): 5 % → 25 pts
        fcf_yield = float(metrics.get('freeCashFlowYield', 0) or 0)
        score += min(25.0, max(0.0, fcf_yield * 500.0))

        # ROE (0–20 pts): 30 % → 20 pts
        roe = float(metrics.get('roe', 0) or 0)
        score += min(20.0, max(0.0, roe * 66.7))

        # PE attractiveness (0–15 pts)
        pe = float(metrics.get('peRatio', 100) or 100)
        if pe <= 0:
            pe_pts = 0.0   # Negative earnings
        elif pe <= 15:
            pe_pts = 15.0
        elif pe <= 25:
            pe_pts = 12.0
        elif pe <= 35:
            pe_pts = 8.0
        elif pe <= 50:
            pe_pts = 3.0
        else:
            pe_pts = 0.0
        score += pe_pts

        # Trend (0–10 pts): above 200 DMA
        price = float(quote.get('price', 0) or 0)
        price_200 = float(quote.get('priceAvg200', 0) or 0)
        if price_200 > 0 and price > price_200:
            score += 10.0

        return min(100.0, score)

    def _build_row(
        self,
        symbol: str,
        profile: Dict,
        metrics: Dict,
        quote: Dict,
        score: float,
    ) -> Dict:
        """Build a canonical candidate dict from raw FMP fields."""
        price = float(quote.get('price', 0) or profile.get('price', 0) or 0)
        price_200 = float(quote.get('priceAvg200', 0) or 0)
        rev_growth = float(metrics.get('revenueGrowth', 0) or 0)
        fcf_yield = float(metrics.get('freeCashFlowYield', 0) or 0)
        roe = float(metrics.get('roe', 0) or 0)
        pe = float(metrics.get('peRatio', 0) or 0)
        mkt_cap = float(profile.get('mktCap', 0) or quote.get('marketCap', 0) or 0)

        # Build human-readable reason snippets
        reasons = []
        if rev_growth >= 0.30:
            reasons.append(f"RevGrowth {rev_growth:.0%}")
        if fcf_yield >= 0.03:
            reasons.append(f"FCF {fcf_yield:.1%}")
        if roe >= 0.20:
            reasons.append(f"ROE {roe:.0%}")
        if price_200 > 0 and price > price_200:
            reasons.append("Above 200 DMA")

        return {
            'symbol': symbol,
            'score': round(score, 1),
            'sector': profile.get('sector', ''),
            'mkt_cap': mkt_cap,
            'rev_growth': rev_growth,
            'fcf_yield': fcf_yield,
            'roe': roe,
            'pe': pe,
            'price': price,
            'price_200dma': price_200,
            'above_200dma': price_200 > 0 and price > price_200,
            'reasons': '; '.join(reasons),
            'scanned_at': datetime.now().isoformat(),
            # Theme boost fields — initialised to zero; populated by apply_theme_boosts()
            'theme_boost': 0,
            'theme_names': '',
        }


# ---------------------------------------------------------------------------
# Module-level helper — no scanner instance required
# ---------------------------------------------------------------------------

def apply_theme_boosts(
    candidates: List[Dict[str, Any]],
    theme_signals_path: str,
    config: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Apply theme confidence boosts to scanner candidates.

    Reads ``theme_signals.json`` written by the theme engine and adds a
    small, capped score boost to any candidate whose ticker appears in an
    active theme.

    Boost rules:
    - Only applied if theme confidence >= ``min_confidence`` (default 0.6).
    - Boost = ``min(max_theme_boost_points, round(confidence * max_boost))``.
    - Final score is capped at 100.
    - Adds ``theme_boost`` (int) and ``theme_names`` (str) fields to every
      candidate dict regardless — zero/empty if no matching theme.

    The function is idempotent and safe to call even when ``theme_signals.json``
    does not exist; candidates are returned unchanged with 0-boost fields.

    Args:
        candidates:          Candidate dicts from full_scan/weekly_refresh/daily_refresh.
        theme_signals_path:  Path to ``outputs/latest/theme_signals.json``.
        config:              ``theme_engine`` config dict (or any dict-like).

    Returns:
        Same list with theme_boost and theme_names populated; sorted by score desc.
    """
    max_boost: int = int(config.get("max_theme_boost_points", 10))
    min_conf: float = float(config.get("min_confidence", 0.6))

    # Ensure every candidate has the fields even if we return early
    for c in candidates:
        c.setdefault("theme_boost", 0)
        c.setdefault("theme_names", "")

    signals_path = Path(theme_signals_path)
    if not signals_path.exists():
        logger.debug("apply_theme_boosts: no theme_signals.json found, skipping boost")
        return candidates

    try:
        payload = json.loads(signals_path.read_text(encoding="utf-8"))
        themes = payload.get("themes", [])
    except Exception as exc:
        logger.warning("apply_theme_boosts: failed to read theme_signals.json: %s", exc)
        return candidates

    # Build ticker → {confidence, theme_names} map (max confidence across themes)
    ticker_map: Dict[str, Dict[str, Any]] = {}
    for theme in themes:
        confidence = float(theme.get("confidence", 0.0))
        if confidence < min_conf:
            continue
        theme_name = theme.get("name", "")
        for ticker in theme.get("tickers", []):
            if ticker not in ticker_map:
                ticker_map[ticker] = {"confidence": 0.0, "names": []}
            entry = ticker_map[ticker]
            entry["confidence"] = max(entry["confidence"], confidence)
            if theme_name and theme_name not in entry["names"]:
                entry["names"].append(theme_name)

    # Apply boosts
    for candidate in candidates:
        sym = candidate.get("symbol", "")
        if sym in ticker_map:
            entry = ticker_map[sym]
            boost = min(max_boost, round(entry["confidence"] * max_boost))
            candidate["theme_boost"] = boost
            candidate["theme_names"] = "; ".join(entry["names"])
            candidate["score"] = round(min(100.0, candidate["score"] + boost), 1)

    candidates.sort(key=lambda r: r["score"], reverse=True)
    boosted_count = sum(1 for c in candidates if c["theme_boost"] > 0)
    logger.info(
        "apply_theme_boosts: boosted %d/%d candidates (max_boost=%d, min_conf=%.2f)",
        boosted_count, len(candidates), max_boost, min_conf,
    )
    return candidates
