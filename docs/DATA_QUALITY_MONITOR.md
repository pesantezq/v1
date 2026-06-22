# Data Quality Monitor

## Purpose

The Data Quality Monitor detects degraded, stale, missing, fallback, or
mixed-source market/fundamental/news data before it can silently affect
scoring, confidence, allocation, or recommendations.

It is a pure observability layer. It does not change any live scoring weights,
alert ranking, recommendation outcomes, allocation behavior, or conviction
logic. Its only effect is to write two operator artifacts after the watchlist
scanner completes each run.

---

## Observe-Only Behavior

The monitor is strictly additive:

- It reads existing pipeline records — it does not produce, modify, or block them.
- Exceptions within the monitor are caught as warnings and the pipeline continues.
- The `observe_only: true` field is hardcoded in all output artifacts.
- It does not gate, suppress, or override any decision.
- If no records are available (scanner disabled, dry run, no results), it writes
  an `available: false` artifact rather than failing.

---

## Artifacts

Written via `OutputNamespace.LATEST`:

| File | Path |
|------|------|
| JSON report | `outputs/latest/data_quality_report.json` |
| Markdown report | `outputs/latest/data_quality_report.md` |

### JSON Contract

```json
{
  "generated_at": "2025-01-01T12:00:00+00:00",
  "observe_only": true,
  "available": true,
  "total_symbols": 20,
  "healthy_symbols": 15,
  "info_symbols": 0,
  "warning_symbols": 4,
  "critical_symbols": 1,
  "missing_price_count": 1,
  "missing_fundamentals_count": 3,
  "missing_news_count": 5,
  "stale_price_count": 2,
  "fallback_count": 1,
  "cached_count": 2,
  "source_counts": {"fresh": 15, "partial": 3, "cached": 2},
  "summary_line": "DATA QUALITY DEGRADED: 1 critical issue(s), 4 warning(s) across 20 symbols",
  "issues": [ ... ],
  "symbols": [ ... ]
}
```

`issues` contains aggregate-level issues (excessive rates, system degraded mode).
`symbols` contains per-symbol reports with their own `issues` arrays.

---

## Severity Buckets

The per-symbol counts partition the symbol set along the severity ladder — each
symbol falls into exactly one bucket based on the most severe issue it carries:

| Field | Bucket | Definition |
|-------|--------|------------|
| `healthy_symbols` | healthy | No issues at all |
| `info_symbols` | info | Has issues, but **all** are `info`-severity — a notice, not a warning (e.g. an ETF/index with no single-issuer news) |
| `warning_symbols` | warning | At least one `warning`-severity issue and no `critical` issue |
| `critical_symbols` | critical | At least one `critical`-severity issue |

`info_symbols` was added on 2026-06-22. Before that, `warning_symbols` counted
*any* symbol with a non-critical issue, so `info`-severity `MISSING_NEWS` notices
(common for news-less ETFs like SPY/QQQ/XLK) were misreported as warnings. The
buckets now respect severity, so an info-only state reads e.g.
`"1/25 symbols healthy (24 with info notice(s))"` rather than
`"24 symbol(s) with warnings"`. This was a counting fix, not a change to news
collection — the news pipeline is unaffected.

The markdown report includes an `Info notices` row reflecting `info_symbols`.

---

## Issue Types

| Issue Type | Severity | Trigger |
|------------|----------|---------|
| `MISSING_PRICE` | critical | `price` is None or 0 |
| `STALE_PRICE` | warning | `quote_age_minutes` > threshold, or `data_quality='partial'` |
| `CACHE_ONLY` | warning | `data_quality='cached'` and price is not stale by age |
| `FALLBACK_USED` | warning | `data_mode='fallback'`, `fallback_used=True`, or `fallback_reason` present |
| `MISSING_FUNDAMENTALS` | warning | `fundamentals` is None or all-null dict |
| `MISSING_NEWS` | info | `news_count=0` or `news.headline_count=0` or both absent |
| `MIXED_SOURCE` | info | `data_mode='mixed'` (some live, some cached across sources) |
| `SOURCE_ERROR` | warning | `error` or `warning` field present in record |
| `UNKNOWN_SOURCE` | warning | Price present but no source field identifiable |
| `EXCESSIVE_FALLBACK_RATE` | warning | Aggregate: fallback rate > 30% of all symbols |
| `EXCESSIVE_MISSING_PRICE_RATE` | critical | Aggregate: missing price rate > 10% of all symbols |
| `DEGRADED_MODE` | warning | All symbols are operating on cached/fallback/stale data |
| `INSUFFICIENT_DATA` | info | No records provided to the monitor |

---

## Severity Definitions

| Severity | Meaning |
|----------|---------|
| `critical` | A condition that materially compromises data reliability — scoring and recommendations produced under this condition may be unreliable. Requires operator review. |
| `warning` | A condition that reduces data freshness or introduces source uncertainty. The system can still function but results should be interpreted with caution. |
| `info` | A non-blocking data gap (e.g., missing news) that does not materially affect scoring but is worth tracking over time. |

---

## Configurable Thresholds

```python
@dataclass
class DataQualityConfig:
    stale_quote_minutes: int = 1440              # 24h — flag quote as stale
    max_fallback_rate_warning: float = 0.30      # warn if >30% of symbols used fallback
    max_missing_price_rate_critical: float = 0.10  # critical if >10% missing price
```

Pass a custom `DataQualityConfig` to `evaluate_data_quality()` to override defaults.

---

## Module API

```python
from portfolio_automation.data_quality_monitor import (
    evaluate_data_quality,
    write_data_quality_report,
    DataQualityConfig,
    DataQualitySummary,
    DataQualitySymbolReport,
    DataQualityIssue,
    summary_to_dict,
    build_data_quality_markdown,
)

# Evaluate from any list of symbol-level dicts
summary = evaluate_data_quality(records, config=DataQualityConfig())

# Write artifacts (non-blocking; wrap in try/except in production)
json_path, md_path = write_data_quality_report(summary, base_dir="outputs")
```

`evaluate_data_quality()` accepts any list of dicts. Unknown or missing fields
are tolerated. Field name variants (`ticker` vs `symbol`,
`data_quality` vs `price_status`) are handled transparently.

---

## Pipeline Integration

The monitor runs after the watchlist scanner completes (Section 4f of the daily
pipeline in `main.py`). It reads `_ws_result.get('results', [])` — the
full enriched symbol list — and writes the two artifacts if not in dry-run mode.

Integration is wrapped in `try/except` so any monitor failure is logged as a
warning and the pipeline continues.

```python
# After scanner completes in main.py:
try:
    from portfolio_automation.data_quality_monitor import (
        evaluate_data_quality, write_data_quality_report,
    )
    _dq_records = [r for r in _ws_result.get('results', []) if isinstance(r, dict)]
    _dq_summary = evaluate_data_quality(_dq_records)
    if not dry_run:
        write_data_quality_report(_dq_summary)
    logger.info("DATA QUALITY: %s", _dq_summary.summary_line)
except Exception as _dq_err:
    logger.warning("DATA QUALITY MONITOR: non-fatal error — %s", _dq_err)
```

---

## Future: Scoring and Confidence Consumption

In a future phase, the confidence layer or decision engine may read
`data_quality_report.json` to:

- Apply a confidence penalty when `critical_symbols > 0`
- Suppress recommendations when `missing_price_count / total_symbols > threshold`
- Annotate decisions with `data_quality_degraded: true` when `degraded_mode` is present
- Feed the GUI Decision Center's System/Data Health card

**Rule:** The monitor reports quality but does not override decisions yet.
Any behavioral change based on monitor output requires an explicit Phase 0 step
and must go through the standard observe-only → validate → wire-in lifecycle.

---

## Module Location

```
portfolio_automation/data_quality_monitor.py
```

Tests:

```bash
python -m pytest -q tests/test_data_quality_monitor.py
```
