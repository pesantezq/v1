"""
Theme Mapper — maps detected theme names to S&P 500 tickers using a static catalog.

Fuzzy matching uses normalized string comparison against theme names and synonyms.
Direct company/ticker mentions from the LLM are also filtered against the S&P 500
symbol set (if provided).
"""

from __future__ import annotations

import difflib
import json
import logging
import re
import string
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _normalize(text: str) -> str:
    """Lowercase, remove punctuation except spaces, collapse whitespace."""
    text = text.lower()
    text = text.translate(str.maketrans(string.punctuation, " " * len(string.punctuation)))
    return re.sub(r"\s+", " ", text).strip()


class ThemeMapper:
    """Map detected themes to S&P 500 ticker lists.

    Args:
        catalog_path:  Path to data/themes_catalog.json.
        sp500_symbols: Optional set/list of valid S&P 500 ticker symbols.
                       Used to filter direct_mentions. If None, all mentions pass.
    """

    def __init__(
        self,
        catalog_path: str = "data/themes_catalog.json",
        sp500_symbols: list[str] | set[str] | None = None,
    ) -> None:
        self.catalog = self._load_catalog(catalog_path)
        self.sp500: set[str] = set(sp500_symbols) if sp500_symbols else set()
        # Pre-normalise catalog keys and synonyms for fast lookup
        self._index = self._build_index()

    # ── Public API ────────────────────────────────────────────────────────────

    def map_themes(
        self, detected: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Enrich detected themes and produce watch_candidates.

        Returns:
            enriched_themes:   detected themes with added 'tickers' field.
            watch_candidates:  flat list of {ticker, sources, themes, confidence,
                                              rationale, timestamp}.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        enriched: list[dict[str, Any]] = []
        # ticker → aggregated metadata
        ticker_meta: dict[str, dict[str, Any]] = {}

        for theme in detected:
            name = theme.get("name", "")
            confidence = float(theme.get("confidence", 0.5))
            rationale = theme.get("rationale", "")

            # Match to catalog
            catalog_key = self._match_catalog(name)
            tickers: list[str] = []
            if catalog_key:
                tickers = list(self.catalog[catalog_key].get("tickers", []))

            # Collect direct mentions that are valid S&P 500 tickers
            direct_tickers: list[str] = []
            for mention in theme.get("direct_mentions", []):
                sym = mention.upper().strip()
                if self.sp500 and sym not in self.sp500:
                    continue
                if sym and sym not in direct_tickers:
                    direct_tickers.append(sym)

            theme_copy = dict(theme)
            theme_copy["catalog_match"] = catalog_key
            theme_copy["tickers"] = tickers
            enriched.append(theme_copy)

            # Merge into ticker_meta
            all_tickers_for_theme = set(tickers) | set(direct_tickers)
            for ticker in all_tickers_for_theme:
                source = []
                if ticker in tickers:
                    source.append("theme")
                if ticker in direct_tickers:
                    source.append("direct")
                if ticker not in ticker_meta:
                    ticker_meta[ticker] = {
                        "ticker": ticker,
                        "sources": [],
                        "themes": [],
                        "confidence": 0.0,
                        "rationale": "",
                        "timestamp": now_iso,
                    }
                meta = ticker_meta[ticker]
                for s in source:
                    if s not in meta["sources"]:
                        meta["sources"].append(s)
                if name not in meta["themes"]:
                    meta["themes"].append(name)
                # Take max confidence across themes
                meta["confidence"] = max(meta["confidence"], confidence)
                if not meta["rationale"]:
                    meta["rationale"] = rationale

        watch_candidates = list(ticker_meta.values())
        logger.info(
            "ThemeMapper: %d themes → %d watch candidates",
            len(enriched),
            len(watch_candidates),
        )
        return enriched, watch_candidates

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _load_catalog(self, path: str) -> dict[str, Any]:
        p = Path(path)
        if not p.exists():
            logger.warning("themes_catalog.json not found at %s", path)
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error("Failed to load themes catalog: %s", exc)
            return {}

    def _build_index(self) -> dict[str, str]:
        """Build {normalized_term → catalog_key} for fast lookup."""
        index: dict[str, str] = {}
        for key, entry in self.catalog.items():
            index[_normalize(key)] = key
            for syn in entry.get("synonyms", []):
                index[_normalize(syn)] = key
        return index

    def _match_catalog(self, name: str) -> str | None:
        """Return catalog key for the best match, or None."""
        norm = _normalize(name)
        if not norm:
            return None

        # 1. Exact match
        if norm in self._index:
            return self._index[norm]

        # 2. Substring: catalog term is substring of detected name or vice versa
        for term, key in self._index.items():
            if term in norm or norm in term:
                return key

        # 3. Fuzzy match (difflib)
        return self._fuzzy_match(norm)

    def _fuzzy_match(self, norm: str) -> str | None:
        """Return catalog key for the best fuzzy match above threshold, or None."""
        best_key: str | None = None
        best_score = 0.0
        for term, key in self._index.items():
            score = difflib.SequenceMatcher(None, norm, term).ratio()
            if score > best_score:
                best_score = score
                best_key = key
        return best_key if best_score >= 0.72 else None
