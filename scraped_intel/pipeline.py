"""
Scraped Intelligence — pipeline orchestrator.

Entry point: run_scraped_intel()

This function is the single integration point for the rest of the codebase.
It:
  1. Builds the configured adapter set.
  2. Fetches records for every symbol (best-effort, one adapter at a time).
  3. Persists new records to the store (deduped by record_id).
  4. Computes SoftSignals via features.py + provenance.py.
  5. Persists soft signals to the store.
  6. Optionally exports to CSV (training data).
  7. Returns dict[symbol → IntelBundle] for the caller to attach to scan rows.

Separation guarantee
--------------------
run_scraped_intel() never modifies any scan result row directly.
The caller (watchlist_scanner/__main__.py) attaches the bundle as
result["scraped_intel"] = bundle.to_dict() — a completely new key.

All trusted hard-data fields (signal_score, confidence_score, price,
fundamentals, technicals, news) are untouched.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

from scraped_intel.base import SourceAdapter
from scraped_intel.features import compute_soft_signals
from scraped_intel.models import IntelBundle, ScrapedRecord
from scraped_intel.provenance import compute_scraped_confidence
from scraped_intel.store import ScrapedIntelStore

logger = logging.getLogger("scraped_intel.pipeline")


# ---------------------------------------------------------------------------
# Adapter registry
# ---------------------------------------------------------------------------

def _build_adapters(config: dict, known_themes: list[str]) -> list[SourceAdapter]:
    """
    Instantiate adapters listed in config["adapters"].

    Adapter names: "sec_filings", "rss_news"
    Unknown names are logged and skipped (no crash).
    """
    enabled = config.get("adapters") or ["sec_filings", "rss_news"]
    cache_dir = config.get("cache_dir", "data/scraped_cache")
    adapters: list[SourceAdapter] = []

    for name in enabled:
        try:
            if name == "sec_filings":
                from scraped_intel.adapters.sec_filings import SECFilingsAdapter
                adapters.append(SECFilingsAdapter(cache_dir=cache_dir))

            elif name == "rss_news":
                from scraped_intel.adapters.rss_news import RSSNewsAdapter
                feeds = config.get("rss_feeds") or []
                adapters.append(
                    RSSNewsAdapter(
                        feeds=feeds,
                        cache_dir=cache_dir,
                        known_themes=known_themes,
                    )
                )
            else:
                logger.warning("scraped_intel: unknown adapter '%s' — skipped", name)
        except Exception as exc:
            logger.warning("scraped_intel: failed to build adapter '%s': %s", name, exc)

    return adapters


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_scraped_intel(
    symbols: list[str],
    config: dict,
    known_themes: Optional[list[str]] = None,
    dry_run: bool = False,
) -> dict[str, IntelBundle]:
    """
    Run the full scraped intelligence pipeline for a list of symbols.

    Args:
        symbols:       Ticker symbols to gather evidence for.
        config:        ``scraped_intel`` sub-dict from config.json.
        known_themes:  Theme names from the theme engine for alignment scoring.
        dry_run:       If True, skip adapter fetching (use stored records only).

    Returns:
        dict mapping symbol.upper() → IntelBundle.
        Empty dict if the pipeline is disabled or encounters a fatal error.
    """
    if not config.get("enabled", False):
        return {}

    themes = known_themes or []
    lookback_days = int(config.get("lookback_days", 30))
    db_path = config.get("db_path", "data/portfolio.db")
    as_of = date.today().isoformat()
    since_date = (date.today() - timedelta(days=lookback_days)).isoformat()

    store = ScrapedIntelStore(db_path=db_path)

    # Build adapters (only when not dry-running)
    adapters: list[SourceAdapter] = []
    if not dry_run:
        adapters = _build_adapters(config, themes)

    bundles: dict[str, IntelBundle] = {}

    for symbol in symbols:
        sym = symbol.upper()
        bundle = IntelBundle(symbol=sym, as_of_date=as_of)

        # ── Step 1: Fetch new records from adapters ─────────────────────
        new_count = 0
        for adapter in adapters:
            try:
                records = adapter.fetch(sym, lookback_days=lookback_days)
                saved = store.save_records(records)
                new_count += saved
            except Exception as exc:
                logger.warning(
                    "scraped_intel: adapter %s failed for %s: %s",
                    adapter.source_type, sym, exc,
                )

        # ── Step 2: Load all stored records for feature computation ─────
        all_records = store.load_records(sym, since_date=since_date)
        bundle.records = all_records

        # ── Step 3: Compute features ────────────────────────────────────
        scraped_conf = compute_scraped_confidence(all_records)
        signals = compute_soft_signals(
            symbol=sym,
            records=all_records,
            as_of_date=as_of,
            known_themes=themes,
            scraped_confidence=scraped_conf,
        )
        bundle.signals = signals

        # ── Step 4: Persist soft signals ────────────────────────────────
        try:
            store.save_soft_signals(signals)
        except Exception as exc:
            logger.warning("scraped_intel: failed to save signals for %s: %s", sym, exc)

        bundles[sym] = bundle

        if all_records or new_count:
            logger.debug(
                "scraped_intel: %s — %d records (%d new), conf=%.2f",
                sym, len(all_records), new_count, scraped_conf,
            )

    # ── Step 5: Optional training export ────────────────────────────────
    # Export is triggered separately via export_training_rows() in __main__.py
    # so that scan result rows are fully available.

    logger.info(
        "scraped_intel: pipeline complete — %d symbols, %d adapters",
        len(symbols), len(adapters),
    )
    return bundles
