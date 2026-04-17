"""
Outcome-linked analytics for scraped intelligence comparison snapshots.

Flow
----
1. Snapshots are written by run_comparison() when ``comparison_outcome_tracking``
   is enabled in config.json.  One row is stored per (symbol, as_of_date); pending
   outcome slots for 1d / 5d / 20d windows are created automatically.

2. evaluate_pending_comparison_outcomes() resolves pending windows using cached
   TIME_SERIES_DAILY price data — the same cache used by the watchlist scanner
   so no extra API calls are needed.

3. run_outcome_analysis() loads all resolved rows, groups them into four bucket
   dimensions (signal_delta, confidence_delta, top soft feature, source_count),
   computes per-bucket statistics for each return window, and writes a JSON +
   Markdown analysis report.

Off by default
--------------
Enable with ``scraped_intel.comparison_outcome_tracking: true`` in config.json.
All functions are additive and never touch production WatchlistRow fields.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("scraped_intel.outcome_analysis")

# ---------------------------------------------------------------------------
# Bucket helpers (pure functions)
# ---------------------------------------------------------------------------

_SIGNAL_BUCKET_THRESHOLDS = [
    (0.0,   "none"),
    (0.02,  "small(0-2%)"),
    (0.06,  "medium(2-6%)"),
    (float("inf"), "large(6%+)"),
]

_SOURCE_BUCKET_THRESHOLDS = [
    (0,  "0"),
    (1,  "1"),
    (3,  "2-3"),
    (99, "4+"),
]


def _signal_delta_bucket(delta: float) -> str:
    """Map a signal_delta value to a named bucket."""
    if delta <= 0.0:
        return "none"
    for threshold, label in _SIGNAL_BUCKET_THRESHOLDS[1:]:
        if delta <= threshold:
            return label
    return "large(6%+)"


def _confidence_delta_bucket(delta: float) -> str:
    """Map a confidence_delta value to a named bucket (same thresholds as signal)."""
    return _signal_delta_bucket(delta)


def _top_feature_bucket(top_features: list) -> str:
    """Return the name of the highest-contributing soft feature, or 'none'."""
    if not top_features:
        return "none"
    try:
        return str(top_features[0].get("feature", "none"))
    except (IndexError, AttributeError, TypeError):
        return "none"


def _source_count_bucket(source_count: int) -> str:
    """Map source_count to a named bucket."""
    n = int(source_count or 0)
    if n == 0:
        return "0"
    if n == 1:
        return "1"
    if n <= 3:
        return "2-3"
    return "4+"


# ---------------------------------------------------------------------------
# Price lookup (mirrors watchlist_scanner/outcome_evaluator.py pattern)
# ---------------------------------------------------------------------------

def _load_close_from_cache(
    cache_dir: Path,
    symbol: str,
    target_date: date,
    as_of_date: date,
) -> Optional[tuple[date, float]]:
    """
    Return (trade_date, close_price) for the first trading day on or after
    ``target_date`` and on or before ``as_of_date``, using cached daily JSON.

    Returns None if the cache file is missing or no eligible date is found.
    """
    cache_file = cache_dir / f"daily_{symbol.upper()}.json"
    if not cache_file.exists():
        return None

    try:
        raw = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    ts = raw.get("Time Series (Daily)", {})
    if not ts:
        return None

    candidates: list[tuple[date, float]] = []
    for day_str, payload in ts.items():
        try:
            day = date.fromisoformat(day_str)
            if day < target_date or day > as_of_date:
                continue
            close = float(payload.get("4. close", 0) or 0)
            if close <= 0:
                continue
            candidates.append((day, close))
        except (TypeError, ValueError):
            continue

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0])
    return candidates[0]


def _label_return(return_pct: float) -> str:
    """Convert return_pct to a three-way outcome label."""
    if return_pct >= 1.0:
        return "positive"
    if return_pct <= -1.0:
        return "negative"
    return "flat"


# ---------------------------------------------------------------------------
# Pending outcome evaluator
# ---------------------------------------------------------------------------

def evaluate_pending_comparison_outcomes(
    db_path: str | Path = "data/portfolio.db",
    cache_dir: str | Path = "data/watchlist_cache",
    *,
    as_of: datetime | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    """
    Resolve pending comparison outcome windows using cached price data.

    For each pending (snapshot_id, window_days) pair, looks up the first
    available close price that falls on or after ``as_of_date + window_days``
    calendar days.  Uses only the cached TIME_SERIES_DAILY JSON files —
    no live API calls are made.

    Args:
        db_path:   Path to portfolio.db.
        cache_dir: Directory containing ``daily_<SYMBOL>.json`` files.
        as_of:     Treat this datetime as "now" (default: datetime.now()).
        limit:     Maximum pending rows to evaluate per call.

    Returns:
        Summary dict: resolved_count, skipped_count, errors.
    """
    from scraped_intel.store import ScrapedIntelStore

    now = as_of or datetime.now()
    today = now.date()
    _cache = Path(cache_dir)
    store = ScrapedIntelStore(db_path=db_path)

    pending = store.get_pending_comparison_outcomes(limit=limit)
    resolved_count = 0
    skipped_count = 0
    errors: list[str] = []

    for row in pending:
        try:
            symbol: str = row["symbol"]
            as_of_date_str: str = row["as_of_date"]
            window: int = int(row["window_days"])
            outcome_id: int = int(row["id"])

            surfaced = date.fromisoformat(as_of_date_str)
            target = surfaced + timedelta(days=window)

            if target > today:
                # Not enough calendar time has passed yet.
                skipped_count += 1
                continue

            # Find the close on the surfaced date (baseline price).
            baseline_result = _load_close_from_cache(
                _cache, symbol, surfaced, surfaced + timedelta(days=4)
            )
            if baseline_result is None:
                skipped_count += 1
                continue
            _, baseline_price = baseline_result

            # Find the close on or after target_date.
            outcome_result = _load_close_from_cache(
                _cache, symbol, target, today
            )
            if outcome_result is None:
                skipped_count += 1
                continue
            _, outcome_price = outcome_result

            return_pct = ((outcome_price - baseline_price) / baseline_price) * 100.0
            label = _label_return(return_pct)

            store.resolve_comparison_outcome(
                outcome_id,
                baseline_price=baseline_price,
                outcome_price=outcome_price,
                return_pct=round(return_pct, 4),
                outcome_label=label,
            )
            resolved_count += 1

        except Exception as exc:
            errors.append(f"{row.get('symbol', '?')}/{row.get('window_days', '?')}: {exc}")

    logger.info(
        "evaluate_pending_comparison_outcomes: resolved=%d skipped=%d errors=%d",
        resolved_count, skipped_count, len(errors),
    )
    return {
        "resolved_count": resolved_count,
        "skipped_count": skipped_count,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Bucket aggregation (pure functions — accept list[dict] inputs)
# ---------------------------------------------------------------------------

def _bucket_stats(rows: list[dict], bucket_fn, bucket_field: str) -> dict[str, dict]:
    """
    Group ``rows`` by ``bucket_fn(row[bucket_field])`` and compute statistics.

    Each bucket entry contains:
        count          — total rows
        resolved_count — rows with a numeric return_pct
        avg_return     — mean return_pct (None if no resolved rows)
        win_rate       — fraction with return_pct > 0 (None if no resolved rows)
        positive       — count of return_pct > 0
        negative       — count of return_pct < 0
        flat           — count of return_pct == 0 or in (-1, +1)
    """
    buckets: dict[str, dict] = {}

    for r in rows:
        raw = r.get(bucket_field)
        if isinstance(raw, list):
            # top_features list → delegate to _top_feature_bucket
            label = _top_feature_bucket(raw)
        else:
            try:
                label = bucket_fn(raw)
            except Exception:
                label = "unknown"

        if label not in buckets:
            buckets[label] = {
                "count": 0,
                "resolved_count": 0,
                "_returns": [],
            }
        buckets[label]["count"] += 1
        ret = r.get("return_pct")
        if ret is not None:
            buckets[label]["resolved_count"] += 1
            buckets[label]["_returns"].append(float(ret))

    # Compute derived stats and remove scratch fields.
    for label, b in buckets.items():
        rets = b.pop("_returns", [])
        if rets:
            b["avg_return"] = round(sum(rets) / len(rets), 4)
            b["win_rate"] = round(sum(1 for x in rets if x > 0) / len(rets), 4)
            b["positive"] = sum(1 for x in rets if x > 0)
            b["negative"] = sum(1 for x in rets if x < 0)
            b["flat"] = sum(1 for x in rets if x == 0)
        else:
            b["avg_return"] = None
            b["win_rate"] = None
            b["positive"] = 0
            b["negative"] = 0
            b["flat"] = 0

    return buckets


def compute_bucket_analysis(
    resolved_rows: list[dict],
) -> dict[str, Any]:
    """
    Compute bucket statistics across four dimensions.

    ``resolved_rows`` must be the joined list returned by
    ``ScrapedIntelStore.get_resolved_comparison_outcomes()``.

    Returns a nested dict with keys:
        by_signal_delta      — bucket → stats
        by_confidence_delta  — bucket → stats
        by_top_feature       — bucket → stats
        by_source_count      — bucket → stats
        totals               — overall counts
    """
    if not resolved_rows:
        return {
            "by_signal_delta":     {},
            "by_confidence_delta": {},
            "by_top_feature":      {},
            "by_source_count":     {},
            "totals": {"count": 0, "resolved_count": 0},
        }

    by_sig   = _bucket_stats(resolved_rows, _signal_delta_bucket,     "signal_delta")
    by_conf  = _bucket_stats(resolved_rows, _confidence_delta_bucket,  "confidence_delta")
    by_feat  = _bucket_stats(resolved_rows, _top_feature_bucket,       "top_features")
    by_src   = _bucket_stats(resolved_rows, _source_count_bucket,      "source_count")

    resolved_count = sum(1 for r in resolved_rows if r.get("return_pct") is not None)

    return {
        "by_signal_delta":     by_sig,
        "by_confidence_delta": by_conf,
        "by_top_feature":      by_feat,
        "by_source_count":     by_src,
        "totals": {
            "count":          len(resolved_rows),
            "resolved_count": resolved_count,
        },
    }


# ---------------------------------------------------------------------------
# Full analysis builder (per-window)
# ---------------------------------------------------------------------------

def build_analysis_report(
    store: "ScrapedIntelStore",  # type: ignore[name-defined]
    windows: list[int] | None = None,
    since_date: str | None = None,
) -> dict[str, Any]:
    """
    Load resolved outcomes from the store and produce a full analysis report.

    The report is keyed by window_days so callers can inspect each horizon
    independently.  Overall totals across all windows are included at the top
    level.

    Args:
        store:      An open ScrapedIntelStore instance.
        windows:    Return-window days to include (default [1, 5, 20]).
        since_date: Restrict to outcomes with as_of_date >= since_date.

    Returns:
        {
            "generated_at": ...,
            "since_date": ...,
            "windows": [1, 5, 20],
            "by_window": {
                1:  { "analysis": {...}, "row_count": N },
                5:  { "analysis": {...}, "row_count": N },
                20: { "analysis": {...}, "row_count": N },
            },
            "overall_totals": { "count": N, "resolved_count": N },
        }
    """
    _windows = windows if windows is not None else [1, 5, 20]
    by_window: dict[int, dict] = {}
    total_count = 0
    total_resolved = 0

    for w in _windows:
        rows = store.get_resolved_comparison_outcomes(
            since_date=since_date, window_days=w, limit=5000
        )
        analysis = compute_bucket_analysis(rows)
        by_window[w] = {
            "analysis":  analysis,
            "row_count": len(rows),
        }
        total_count   += analysis["totals"]["count"]
        total_resolved += analysis["totals"]["resolved_count"]

    return {
        "generated_at":  datetime.now().isoformat(),
        "since_date":    since_date,
        "windows":       _windows,
        "by_window":     by_window,
        "overall_totals": {
            "count":          total_count,
            "resolved_count": total_resolved,
        },
    }


# ---------------------------------------------------------------------------
# Report writers
# ---------------------------------------------------------------------------

def write_outcome_analysis_json(
    report: dict[str, Any],
    output_dir: Path,
) -> Path:
    """Write the analysis report as JSON to ``output_dir``."""
    # JSON doesn't allow int keys — convert window keys to strings.
    serialisable = dict(report)
    serialisable["by_window"] = {
        str(k): v for k, v in report.get("by_window", {}).items()
    }
    path = output_dir / "scraped_intel_outcome_analysis.json"
    path.write_text(json.dumps(serialisable, indent=2), encoding="utf-8")
    logger.info(
        "scraped_intel_outcome_analysis.json written (%d total rows, %d resolved)",
        report.get("overall_totals", {}).get("count", 0),
        report.get("overall_totals", {}).get("resolved_count", 0),
    )
    return path


def _md_bucket_table(
    bucket_data: dict[str, dict],
    dimension_label: str,
) -> list[str]:
    """Render one bucket dimension as a markdown table."""
    lines = [
        f"### {dimension_label}",
        "",
        "| Bucket | Count | Resolved | Avg Ret % | Win % | Positive | Negative | Flat |",
        "|--------|------:|--------:|---------:|------:|---------:|---------:|-----:|",
    ]
    for bucket, stats in sorted(bucket_data.items()):
        avg = f"{stats['avg_return']:.2f}" if stats["avg_return"] is not None else "—"
        win = f"{stats['win_rate']*100:.1f}%" if stats["win_rate"] is not None else "—"
        lines.append(
            f"| `{bucket}` "
            f"| {stats['count']} "
            f"| {stats['resolved_count']} "
            f"| {avg} "
            f"| {win} "
            f"| {stats['positive']} "
            f"| {stats['negative']} "
            f"| {stats['flat']} |"
        )
    return lines


def write_outcome_analysis_md(
    report: dict[str, Any],
    output_dir: Path,
) -> Path:
    """Write a human-readable markdown analysis report to ``output_dir``."""
    lines: list[str] = [
        "# Scraped Intelligence — Outcome Analysis",
        "",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}  ",
    ]
    since = report.get("since_date")
    if since:
        lines.append(f"Since: {since}  ")
    windows = report.get("windows", [])
    lines += [
        f"Windows: {', '.join(f'{w}d' for w in windows)}  ",
        "",
        f"> **{report['overall_totals']['resolved_count']}** resolved outcomes "
        f"across {report['overall_totals']['count']} total rows.",
        "",
    ]

    by_window = report.get("by_window", {})
    for w in windows:
        wdata = by_window.get(w) or by_window.get(str(w), {})
        analysis = wdata.get("analysis", {})
        row_count = wdata.get("row_count", 0)
        totals = analysis.get("totals", {})

        lines += [
            f"## {w}-Day Return Window",
            "",
            f"Rows: {row_count}  |  Resolved: {totals.get('resolved_count', 0)}",
            "",
        ]

        for bucket_key, label in [
            ("by_signal_delta",     "By Signal Delta Bucket"),
            ("by_confidence_delta", "By Confidence Delta Bucket"),
            ("by_top_feature",      "By Top Contributing Soft Feature"),
            ("by_source_count",     "By Source Count Bucket"),
        ]:
            bdata = analysis.get(bucket_key, {})
            if bdata:
                lines += _md_bucket_table(bdata, label)
                lines.append("")

    lines.append(
        "_Outcome tracking is in shadow mode.  "
        "No production WatchlistRow fields were modified._"
    )

    path = output_dir / "scraped_intel_outcome_analysis.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("scraped_intel_outcome_analysis.md written")
    return path


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------

def run_outcome_analysis(
    db_path: str | Path = "data/portfolio.db",
    output_dir: str | Path = "outputs/latest",
    config: Optional[dict] = None,
    *,
    evaluate_first: bool = True,
    cache_dir: str | Path = "data/watchlist_cache",
) -> dict[str, Any]:
    """
    Full outcome analysis pipeline: optionally evaluate pending windows →
    load resolved data → bucket analysis → write reports.

    Args:
        db_path:        Path to portfolio.db.
        output_dir:     Directory to write output files into.
        config:         ``scraped_intel`` config sub-dict.  Reads keys:
                        ``comparison_outcome_windows`` (default [1, 5, 20]),
                        ``comparison_since_date`` (optional date filter).
        evaluate_first: When True, evaluate_pending_comparison_outcomes() is
                        called before building the report.
        cache_dir:      Watchlist price-cache directory (for evaluation step).

    Returns:
        The analysis report dict (same structure as build_analysis_report()).
        Also writes ``scraped_intel_outcome_analysis.json`` and
        ``scraped_intel_outcome_analysis.md`` to ``output_dir``.
    """
    from scraped_intel.store import ScrapedIntelStore

    cfg = config or {}
    _windows = list(cfg.get("comparison_outcome_windows", [1, 5, 20]))
    _since   = cfg.get("comparison_since_date")
    _db      = Path(db_path)
    _out     = Path(output_dir)
    _out.mkdir(parents=True, exist_ok=True)

    store = ScrapedIntelStore(db_path=_db)

    # Optional evaluation step
    eval_summary: dict[str, Any] = {}
    if evaluate_first:
        try:
            eval_summary = evaluate_pending_comparison_outcomes(
                db_path=_db,
                cache_dir=cache_dir,
            )
        except Exception as exc:
            logger.warning("evaluate_pending_comparison_outcomes: non-fatal — %s", exc)
            eval_summary = {"error": str(exc)}

    report = build_analysis_report(store, windows=_windows, since_date=_since)
    report["eval_summary"] = eval_summary

    write_outcome_analysis_json(report, _out)
    write_outcome_analysis_md(report, _out)

    logger.info(
        "run_outcome_analysis complete: %d total rows, %d resolved, windows=%s",
        report["overall_totals"]["count"],
        report["overall_totals"]["resolved_count"],
        _windows,
    )
    return report
