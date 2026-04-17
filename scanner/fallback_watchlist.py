"""
Fallback Watchlist — resilience layer for when FMP is unavailable.

Returns a stable list of high-liquidity symbols so that the scanner never
becomes completely inoperative due to FMP API failures, plan-tier limits,
or missing watchlist state.

Activation contract
-------------------
The fallback is activated ONLY when:
  * FMP watchlist build fails (auth error, budget exceeded, circuit breaker)
  * No watchlist exists on disk
  * Scanner mode would otherwise skip due to missing watchlist

The FMP primary path is completely untouched by this module.  All that
changes on FMP failure is that a minimal, clearly-labelled watchlist is
saved so subsequent daily runs have something to work with.

Output schema matches CandidateScanner._build_row() so that
CandidateScanner.daily_refresh() can consume it without modification.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("portfolio_automation.scanner.fallback")

# ---------------------------------------------------------------------------
# Default symbols — high-liquidity S&P 500 mega-caps; no ETFs or leveraged
# products (those belong in the portfolio holdings, not the speculative sleeve).
# ---------------------------------------------------------------------------
_DEFAULT_SYMBOLS: List[str] = [
    "NVDA", "MSFT", "AMZN", "GOOGL", "META",
    "AAPL", "TSLA", "AMD",  "AVGO", "NFLX",
    "JPM",  "V",    "MA",   "XOM",  "LLY",
    "UNH",  "COST", "HD",   "PG",   "KO",
]

# ---------------------------------------------------------------------------
# Neutral candidate template
# Schema mirrors CandidateScanner._build_row() so daily_refresh() can
# update the price fields without blowing up on missing keys.
# ---------------------------------------------------------------------------
_NEUTRAL_ROW: Dict[str, Any] = {
    "score":        0.0,
    "sector":       "",
    "mkt_cap":      0.0,
    "rev_growth":   0.0,
    "fcf_yield":    0.0,
    "roe":          0.0,
    "pe":           0.0,
    "price":        0.0,
    "price_200dma": 0.0,
    "above_200dma": True,
    "reasons":      "fallback — no FMP data available",
    "theme_boost":  0,
    "theme_names":  "",
    "watchlist_source": "fallback",
}


class FallbackWatchlist:
    """
    Builds a minimal but functional watchlist from configurable default
    symbols when FMP is unavailable.

    This class has ZERO network dependencies.  It only reads from disk
    (cached theme candidates) and writes to the standard watchlist path.
    """

    def __init__(
        self,
        scanner_config: Dict[str, Any],
        watchlist_path: Optional[Path] = None,
    ) -> None:
        """
        Args:
            scanner_config:  The ``scanner`` sub-dict from config.json.
            watchlist_path:  Override for the watchlist file path.
                             Defaults to the same path used by CandidateScanner.
        """
        self._enabled: bool = bool(
            scanner_config.get("fallback_watchlist_enabled", True)
        )
        self._symbols: List[str] = list(
            scanner_config.get("fallback_watchlist_symbols") or _DEFAULT_SYMBOLS
        )
        self._max_size: int = int(
            scanner_config.get("fallback_watchlist_max_size", 20)
        )
        self._include_theme: bool = bool(
            scanner_config.get("fallback_watchlist_include_theme_candidates", True)
        )
        self._theme_limit: int = int(
            scanner_config.get("fallback_watchlist_theme_candidate_limit", 10)
        )
        # Use same path as CandidateScanner._WATCHLIST_PATH
        self._watchlist_path: Path = (
            watchlist_path or Path("data/fmp_cache/top100_watchlist.json")
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """True when fallback is enabled via config."""
        return self._enabled

    def build(
        self,
        theme_candidates_path: Optional[str] = None,
        existing_watchlist_symbols: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Build a fallback candidate list.

        Deduplication order (highest priority first):
          1. Theme engine watch_candidates (most topical, limited by theme_limit)
          2. Config-defined default symbols
          3. Previously known watchlist symbols (valid once → worth retaining)

        Args:
            theme_candidates_path:      Path to ``watch_candidates.json`` from
                                        the theme engine, or None to skip blending.
            existing_watchlist_symbols: Symbol list from the last-known watchlist
                                        (useful to preserve continuity).

        Returns:
            List of minimal candidate dicts capped at ``fallback_watchlist_max_size``.
        """
        base: List[str] = []

        # Step 1 — blend in theme candidates if configured
        theme_source_labels: set[str] = set()
        if self._include_theme and theme_candidates_path:
            theme_syms = self._load_theme_candidates(theme_candidates_path)
            if theme_syms:
                logger.info(
                    "FALLBACK WATCHLIST: blending %d theme candidates from %s",
                    len(theme_syms),
                    Path(theme_candidates_path).name,
                )
                base = theme_syms + [s for s in base if s not in set(theme_syms)]
                theme_source_labels = set(theme_syms)

        # Step 2 — add configured default symbols
        for sym in self._symbols:
            if sym.upper() not in {s.upper() for s in base}:
                base.append(sym.upper())

        # Step 3 — retain any previously known watchlist symbols
        if existing_watchlist_symbols:
            known = {s.upper() for s in base}
            for sym in existing_watchlist_symbols:
                if sym.upper() not in known:
                    base.append(sym.upper())
                    known.add(sym.upper())

        # Step 4 — deduplicate (preserve insertion order) + cap
        seen: set[str] = set()
        deduped: List[str] = []
        for sym in base:
            up = sym.upper()
            if up not in seen:
                seen.add(up)
                deduped.append(up)
        final = deduped[: self._max_size]

        now = datetime.now().isoformat()
        candidates: List[Dict[str, Any]] = []
        for sym in final:
            row = dict(_NEUTRAL_ROW)  # shallow copy of template
            row["symbol"] = sym
            row["scanned_at"] = now
            if sym in theme_source_labels:
                row["watchlist_source"] = "fallback+themes"
                row["reasons"] = "fallback+themes — no FMP data; theme-engine candidate"
            candidates.append(row)

        logger.info(
            "FALLBACK WATCHLIST: built %d/%d symbols "
            "(theme_blend=%s, max=%d)",
            len(candidates),
            len(final),
            self._include_theme,
            self._max_size,
        )
        return candidates

    def save(self, candidates: List[Dict[str, Any]]) -> None:
        """
        Persist fallback candidates to the standard watchlist path so that
        subsequent daily runs pick them up automatically.

        The saved JSON includes a ``watchlist_source`` field so it is
        trivially distinguishable from a real FMP-built watchlist.
        """
        self._watchlist_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": datetime.now().isoformat(),
            "watchlist_source": "fallback",
            "candidates": candidates,
        }
        self._watchlist_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info(
            "FALLBACK WATCHLIST: saved %d candidates → %s",
            len(candidates),
            self._watchlist_path,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_theme_candidates(self, path: str) -> List[str]:
        """
        Load ticker symbols from the theme engine's ``watch_candidates.json``.

        Returns [] on any error (network issues, missing file, bad JSON).
        """
        p = Path(path)
        if not p.exists():
            logger.debug(
                "FALLBACK WATCHLIST: no theme candidates file at %s — skipping blend",
                p,
            )
            return []
        try:
            raw = p.read_text(encoding="utf-8")
            data = json.loads(raw)
            # watch_candidates.json is either a bare list or {"candidates": [...]}
            items = data if isinstance(data, list) else data.get("candidates", [])
            symbols: List[str] = [
                c["symbol"].upper()
                for c in items
                if isinstance(c, dict) and c.get("symbol")
            ]
            limited = symbols[: self._theme_limit]
            logger.debug(
                "FALLBACK WATCHLIST: loaded %d theme candidates (limit=%d)",
                len(limited),
                self._theme_limit,
            )
            return limited
        except Exception as exc:
            logger.warning(
                "FALLBACK WATCHLIST: failed to load theme candidates from %s: %s",
                path,
                exc,
            )
            return []
