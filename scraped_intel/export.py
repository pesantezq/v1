"""
Scraped Intelligence — training/backtest CSV export.

Produces a flat CSV row per (symbol, as_of_date) joining:
  hard_* columns  — trusted market/fundamental features (from scan result rows)
  soft_*  columns — scraped-derived features (from SoftSignals)
  meta_*  columns — labels/metadata for supervised learning

Column naming convention (enforced, never mixed)
-------------------------------------------------
hard_signal_score, hard_confidence_score, hard_price, ...
soft_headline_count_7d, soft_theme_alignment_score, ...
meta_as_of_date, meta_symbol, meta_outcome_status, ...

The prefix convention is the contamination guard: any downstream model or
analyst can filter columns by prefix to use only the feature class they trust.

Usage
-----
    from scraped_intel.export import export_training_rows
    export_training_rows(
        scan_results=result["results"],
        bundles=bundles,             # dict[symbol → IntelBundle]
        export_dir="data/training_export",
    )

Output file: data/training_export/YYYY-MM-DD_intel_export.csv
Rows are appended if the file for today already exists (idempotent on re-run
because records are deduped by symbol+as_of_date in the final write).
"""

from __future__ import annotations

import csv
import logging
from datetime import date
from pathlib import Path
from typing import Any, Optional

from scraped_intel.models import IntelBundle

logger = logging.getLogger("scraped_intel.export")

# Ordered column list for reproducible schema
_HARD_COLS: list[str] = [
    "hard_signal_score",
    "hard_trusted_signal_score",
    "hard_confidence_score",
    "hard_confidence_band",
    "hard_data_quality",
    "hard_alert_priority",
    "hard_price",
    "hard_price_change_1d",
    "hard_price_change_5d",
    "hard_above_sma20",
    "hard_above_sma50",
    "hard_volume_spike",
    "hard_avg_sentiment",         # AV news sentiment (hard-data tier)
    "hard_news_count",
    "hard_theme_news_score",
    "hard_technical_score",
    "hard_fundamental_ctx_score",
    "hard_sector",
    "hard_market_cap",
    "hard_pe_ratio",
    "hard_profit_margin",
    "hard_watchlist_source",
]

_SOFT_COLS: list[str] = [
    "soft_headline_count_7d",
    "soft_headline_count_30d",
    "soft_source_count",
    "soft_avg_sentiment",         # scraped sentiment (soft-data tier)
    "soft_theme_alignment_score",
    "soft_mention_acceleration",
    "soft_recency_score",
    "soft_scraped_confidence",
    "soft_records_count",
]

_META_COLS: list[str] = [
    "meta_symbol",
    "meta_as_of_date",
    "meta_outcome_status",        # pending / positive / negative / flat
    "meta_outcome_return_pct",    # actual return if resolved
    "meta_notification_status",   # alerted / cooldown_suppressed / not_alerting
    "meta_portfolio_priority",
    "meta_operator_rank",
]

_ALL_COLS = _META_COLS + _HARD_COLS + _SOFT_COLS


def _extract_hard(row: dict[str, Any]) -> dict[str, Any]:
    """Pull hard-data fields from a scan result row, with safe defaults."""
    bd    = row.get("score_breakdown") or {}
    tech  = row.get("technicals") or {}
    fund  = row.get("fundamentals") or {}
    news  = row.get("news") or {}

    return {
        "hard_signal_score":         row.get("signal_score"),
        "hard_trusted_signal_score": row.get("trusted_signal_score"),
        "hard_confidence_score":     row.get("confidence_score"),
        "hard_confidence_band":      row.get("confidence_band"),
        "hard_data_quality":         row.get("data_quality"),
        "hard_alert_priority":       row.get("alert_priority"),
        "hard_price":                row.get("price") or tech.get("price"),
        "hard_price_change_1d":      row.get("price_change_pct") or tech.get("price_change_1d"),
        "hard_price_change_5d":      tech.get("price_change_5d"),
        "hard_above_sma20":          row.get("above_sma20"),
        "hard_above_sma50":          row.get("above_sma50"),
        "hard_volume_spike":         row.get("volume_spike"),
        "hard_avg_sentiment":        row.get("avg_sentiment") or news.get("avg_sentiment"),
        "hard_news_count":           row.get("news_count") or news.get("headline_count"),
        "hard_theme_news_score":     bd.get("theme_news_score"),
        "hard_technical_score":      bd.get("technical_score"),
        "hard_fundamental_ctx_score": bd.get("fundamental_context_score"),
        "hard_sector":               fund.get("sector"),
        "hard_market_cap":           fund.get("market_cap"),
        "hard_pe_ratio":             fund.get("pe_ratio"),
        "hard_profit_margin":        fund.get("profit_margin"),
        "hard_watchlist_source":     row.get("watchlist_source"),
    }


def _extract_soft(bundle: Optional[IntelBundle]) -> dict[str, Any]:
    """Pull soft-signal fields from an IntelBundle, with safe defaults."""
    if bundle is None or bundle.signals is None:
        return {col: None for col in _SOFT_COLS}
    s = bundle.signals
    return {
        "soft_headline_count_7d":     s.headline_count_7d,
        "soft_headline_count_30d":    s.headline_count_30d,
        "soft_source_count":          s.source_count,
        "soft_avg_sentiment":         s.avg_sentiment,
        "soft_theme_alignment_score": s.theme_alignment_score,
        "soft_mention_acceleration":  s.mention_acceleration,
        "soft_recency_score":         s.recency_score,
        "soft_scraped_confidence":    s.scraped_confidence,
        "soft_records_count":         len(bundle.records),
    }


def _extract_meta(row: dict[str, Any], as_of_date: str) -> dict[str, Any]:
    return {
        "meta_symbol":             row.get("ticker"),
        "meta_as_of_date":         as_of_date,
        "meta_outcome_status":     row.get("outcome_status"),
        "meta_outcome_return_pct": row.get("outcome_return_pct"),
        "meta_notification_status": row.get("notification_status"),
        "meta_portfolio_priority": row.get("portfolio_priority"),
        "meta_operator_rank":      row.get("operator_rank"),
    }


def export_training_rows(
    scan_results: list[dict[str, Any]],
    bundles: dict[str, IntelBundle],
    export_dir: str | Path = "data/training_export",
    as_of_date: Optional[str] = None,
) -> Path:
    """
    Write a training-ready CSV for today's scan.

    Each row = one symbol.
    Columns are prefixed hard_ / soft_ / meta_ for origin clarity.

    Returns the path to the written file.
    """
    today = as_of_date or date.today().isoformat()
    out_dir = Path(export_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{today}_intel_export.csv"

    rows_by_sym: dict[str, dict[str, Any]] = {}
    for row in scan_results:
        sym = (row.get("ticker") or "").upper()
        if not sym:
            continue
        bundle = bundles.get(sym) or bundles.get(sym.upper())
        combined = {
            **_extract_meta(row, today),
            **_extract_hard(row),
            **_extract_soft(bundle),
        }
        rows_by_sym[sym] = combined   # last write wins if duplicate symbol

    if not rows_by_sym:
        logger.info("export_training_rows: no rows to write")
        return out_path

    # Merge with any existing rows for today (idempotent re-runs)
    existing: dict[str, dict[str, Any]] = {}
    if out_path.exists():
        try:
            with open(out_path, newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for existing_row in reader:
                    s = existing_row.get("meta_symbol", "").upper()
                    if s:
                        existing[s] = existing_row
        except Exception as exc:
            logger.warning("export: could not read existing file: %s", exc)

    existing.update(rows_by_sym)   # new data wins
    final_rows = list(existing.values())

    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=_ALL_COLS, extrasaction="ignore")
        writer.writeheader()
        for r in sorted(final_rows, key=lambda x: x.get("meta_symbol") or ""):
            writer.writerow(r)

    logger.info(
        "export_training_rows: wrote %d rows → %s", len(final_rows), out_path
    )
    return out_path
