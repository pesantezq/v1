"""
Data Quality Monitor — observe-only layer for detecting degraded, stale,
missing, fallback, or mixed-source market data before it silently affects
scoring, confidence, or recommendations.

This module is additive and non-blocking. It does not change scoring weights,
alert ranking, recommendation outcomes, or allocation behavior. It produces
structured artifacts for operator review and future GUI consumption.

Artifacts written (via OutputNamespace.LATEST):
  outputs/latest/data_quality_report.json
  outputs/latest/data_quality_report.md
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("stockbot.portfolio_automation.data_quality_monitor")

# ---------------------------------------------------------------------------
# Issue type constants
# ---------------------------------------------------------------------------

ISSUE_MISSING_PRICE = "MISSING_PRICE"
ISSUE_STALE_PRICE = "STALE_PRICE"
ISSUE_CACHE_ONLY = "CACHE_ONLY"
ISSUE_FALLBACK_USED = "FALLBACK_USED"
ISSUE_MISSING_FUNDAMENTALS = "MISSING_FUNDAMENTALS"
ISSUE_MISSING_NEWS = "MISSING_NEWS"
ISSUE_MIXED_SOURCE = "MIXED_SOURCE"
ISSUE_SOURCE_ERROR = "SOURCE_ERROR"
ISSUE_UNKNOWN_SOURCE = "UNKNOWN_SOURCE"
ISSUE_EXCESSIVE_FALLBACK_RATE = "EXCESSIVE_FALLBACK_RATE"
ISSUE_EXCESSIVE_MISSING_PRICE_RATE = "EXCESSIVE_MISSING_PRICE_RATE"
ISSUE_DEGRADED_MODE = "DEGRADED_MODE"
ISSUE_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"

SEVERITY_CRITICAL = "critical"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"

_KNOWN_ISSUE_TYPES: frozenset[str] = frozenset({
    ISSUE_MISSING_PRICE,
    ISSUE_STALE_PRICE,
    ISSUE_CACHE_ONLY,
    ISSUE_FALLBACK_USED,
    ISSUE_MISSING_FUNDAMENTALS,
    ISSUE_MISSING_NEWS,
    ISSUE_MIXED_SOURCE,
    ISSUE_SOURCE_ERROR,
    ISSUE_UNKNOWN_SOURCE,
    ISSUE_EXCESSIVE_FALLBACK_RATE,
    ISSUE_EXCESSIVE_MISSING_PRICE_RATE,
    ISSUE_DEGRADED_MODE,
    ISSUE_INSUFFICIENT_DATA,
})

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class DataQualityConfig:
    stale_quote_minutes: int = 1440             # 24h — flag as stale if quote is older
    max_fallback_rate_warning: float = 0.30     # warn if >30% of symbols used fallback
    max_missing_price_rate_critical: float = 0.10  # critical if >10% of symbols missing price


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class DataQualityIssue:
    issue_type: str
    severity: str
    symbol: str | None = None
    source: str | None = None
    message: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class DataQualitySymbolReport:
    symbol: str
    price_source: str | None = None
    fundamentals_source: str | None = None
    news_source: str | None = None
    price_status: str | None = None
    fundamentals_status: str | None = None
    news_status: str | None = None
    fallback_used: bool = False
    cached: bool = False
    stale: bool = False
    missing_price: bool = False
    missing_fundamentals: bool = False
    missing_news: bool = False
    issues: list[DataQualityIssue] = field(default_factory=list)


@dataclass
class DataQualitySummary:
    generated_at: str
    observe_only: bool = True
    available: bool = True
    total_symbols: int = 0
    healthy_symbols: int = 0
    warning_symbols: int = 0
    critical_symbols: int = 0
    missing_price_count: int = 0
    missing_fundamentals_count: int = 0
    missing_news_count: int = 0
    stale_price_count: int = 0
    fallback_count: int = 0
    cached_count: int = 0
    source_counts: dict = field(default_factory=dict)
    issues: list[DataQualityIssue] = field(default_factory=list)
    symbols: list[DataQualitySymbolReport] = field(default_factory=list)
    summary_line: str = ""


# ---------------------------------------------------------------------------
# Symbol-level evaluation
# ---------------------------------------------------------------------------


def _safe_str(val: Any) -> str | None:
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


def _safe_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _evaluate_symbol(record: dict, config: DataQualityConfig) -> DataQualitySymbolReport:
    """Evaluate data quality for a single symbol record."""
    symbol = _safe_str(record.get("ticker") or record.get("symbol")) or "UNKNOWN"

    # ── Price ────────────────────────────────────────────────────────────────
    price_raw = record.get("price")
    missing_price = price_raw is None or (_safe_float(price_raw) or 0.0) == 0.0

    # ── Data quality / freshness ─────────────────────────────────────────────
    data_quality = _safe_str(
        record.get("data_quality") or record.get("price_status")
    )
    data_mode = _safe_str(
        record.get("data_mode") or record.get("source")
    )

    # ── Staleness (age-based when available, quality-based otherwise) ────────
    stale = False
    quote_age = _safe_float(record.get("quote_age_minutes"))
    if quote_age is not None and quote_age > config.stale_quote_minutes:
        stale = True
    elif data_quality == "partial":
        # partial = live + cached mix, some portion is stale
        stale = True

    # ── Cache-only (separate from stale) ────────────────────────────────────
    cached = data_quality == "cached"

    # ── Fallback ─────────────────────────────────────────────────────────────
    fallback_used = bool(
        record.get("fallback_used")
        or data_mode == "fallback"
        or record.get("fallback_reason") is not None
        or record.get("data_fallback_triggered")
    )

    # ── Fundamentals ─────────────────────────────────────────────────────────
    fundamentals = record.get("fundamentals")
    if fundamentals is None:
        missing_fundamentals = True
    elif isinstance(fundamentals, dict):
        missing_fundamentals = not any(
            v is not None and v != "" and v != 0
            for v in fundamentals.values()
        )
    else:
        missing_fundamentals = False

    # ── News ─────────────────────────────────────────────────────────────────
    news = record.get("news")
    news_count_raw = record.get("news_count")
    news_count = _safe_float(news_count_raw)
    if news is None and news_count is None:
        missing_news = True
    elif isinstance(news, dict):
        missing_news = (news.get("headline_count", 0) or 0) == 0
    elif news_count is not None:
        missing_news = news_count == 0
    else:
        missing_news = False

    # ── Sources ──────────────────────────────────────────────────────────────
    price_source = _safe_str(
        record.get("price_data_source") or data_quality or data_mode
    )
    fundamentals_source = _safe_str(record.get("fundamentals_source"))
    news_source = _safe_str(record.get("news_source"))

    # ── Issue construction ───────────────────────────────────────────────────
    issues: list[DataQualityIssue] = []

    if missing_price:
        issues.append(DataQualityIssue(
            issue_type=ISSUE_MISSING_PRICE,
            severity=SEVERITY_CRITICAL,
            symbol=symbol,
            source=price_source,
            message=f"{symbol}: price data missing",
            metadata={"price": price_raw},
        ))
    else:
        if stale:
            issues.append(DataQualityIssue(
                issue_type=ISSUE_STALE_PRICE,
                severity=SEVERITY_WARNING,
                symbol=symbol,
                source=price_source,
                message=f"{symbol}: price data is stale (data_quality={data_quality!r})",
                metadata={"data_quality": data_quality, "quote_age_minutes": quote_age},
            ))
        elif cached:
            issues.append(DataQualityIssue(
                issue_type=ISSUE_CACHE_ONLY,
                severity=SEVERITY_WARNING,
                symbol=symbol,
                source=price_source,
                message=f"{symbol}: price sourced from cache only",
                metadata={"data_quality": data_quality},
            ))
        elif price_source is None and not missing_price:
            issues.append(DataQualityIssue(
                issue_type=ISSUE_UNKNOWN_SOURCE,
                severity=SEVERITY_WARNING,
                symbol=symbol,
                source=None,
                message=f"{symbol}: price data source is unknown",
                metadata={},
            ))

    if fallback_used:
        issues.append(DataQualityIssue(
            issue_type=ISSUE_FALLBACK_USED,
            severity=SEVERITY_WARNING,
            symbol=symbol,
            source=price_source,
            message=f"{symbol}: fallback data source used",
            metadata={
                "data_mode": data_mode,
                "fallback_reason": record.get("fallback_reason"),
            },
        ))

    if data_mode == "mixed":
        issues.append(DataQualityIssue(
            issue_type=ISSUE_MIXED_SOURCE,
            severity=SEVERITY_INFO,
            symbol=symbol,
            source=price_source,
            message=f"{symbol}: mixed data sources (live + cached)",
            metadata={"data_mode": data_mode},
        ))

    if missing_fundamentals:
        issues.append(DataQualityIssue(
            issue_type=ISSUE_MISSING_FUNDAMENTALS,
            severity=SEVERITY_WARNING,
            symbol=symbol,
            source=fundamentals_source,
            message=f"{symbol}: fundamentals data missing",
            metadata={},
        ))

    if missing_news:
        issues.append(DataQualityIssue(
            issue_type=ISSUE_MISSING_NEWS,
            severity=SEVERITY_INFO,
            symbol=symbol,
            source=news_source,
            message=f"{symbol}: news data missing or empty",
            metadata={"news_count": news_count_raw},
        ))

    error = record.get("error") or record.get("warning")
    if error:
        issues.append(DataQualityIssue(
            issue_type=ISSUE_SOURCE_ERROR,
            severity=SEVERITY_WARNING,
            symbol=symbol,
            source=price_source,
            message=f"{symbol}: data source error — {error}",
            metadata={"error": str(error)},
        ))

    return DataQualitySymbolReport(
        symbol=symbol,
        price_source=price_source,
        fundamentals_source=fundamentals_source,
        news_source=news_source,
        price_status=data_quality,
        fundamentals_status="missing" if missing_fundamentals else "present",
        news_status="missing" if missing_news else "present",
        fallback_used=fallback_used,
        cached=cached,
        stale=stale,
        missing_price=missing_price,
        missing_fundamentals=missing_fundamentals,
        missing_news=missing_news,
        issues=issues,
    )


# ---------------------------------------------------------------------------
# Aggregate evaluation
# ---------------------------------------------------------------------------


def _compute_summary_line(summary: DataQualitySummary) -> str:
    if not summary.available:
        return "No symbols evaluated — insufficient data"
    if summary.total_symbols == 0:
        return "No symbols evaluated"
    if summary.critical_symbols > 0:
        return (
            f"DATA QUALITY DEGRADED: {summary.critical_symbols} critical issue(s), "
            f"{summary.warning_symbols} warning(s) across {summary.total_symbols} symbols"
        )
    if summary.warning_symbols > 0:
        return (
            f"{summary.warning_symbols} symbol(s) with warnings "
            f"({summary.healthy_symbols}/{summary.total_symbols} healthy)"
        )
    return f"All {summary.total_symbols} symbols healthy"


def evaluate_data_quality(
    records: list[dict],
    generated_at: str | None = None,
    config: DataQualityConfig | None = None,
) -> DataQualitySummary:
    """
    Evaluate data quality for a list of symbol-level records.

    Tolerates missing keys and unknown fields. Records may come from
    watchlist_signals.json results, market_opportunities, or any compatible
    dict list. Returns a DataQualitySummary regardless of input shape.

    This function is observe-only: it does not raise, does not modify records,
    and does not affect scoring or recommendation behavior.
    """
    cfg = config or DataQualityConfig()
    ts = generated_at or datetime.now(timezone.utc).isoformat()

    if not records:
        summary = DataQualitySummary(
            generated_at=ts,
            observe_only=True,
            available=False,
            summary_line="No symbols evaluated — insufficient data",
        )
        summary.issues.append(DataQualityIssue(
            issue_type=ISSUE_INSUFFICIENT_DATA,
            severity=SEVERITY_INFO,
            message="No records provided to evaluate_data_quality.",
        ))
        return summary

    symbol_reports: list[DataQualitySymbolReport] = []
    source_counts: dict[str, int] = {}

    for record in records:
        if not isinstance(record, dict):
            continue
        try:
            report = _evaluate_symbol(record, cfg)
        except Exception as exc:
            sym = str(record.get("ticker") or record.get("symbol") or "UNKNOWN")
            logger.warning("data_quality_monitor: error evaluating %s (skipped): %s", sym, exc)
            continue
        symbol_reports.append(report)

        # Tally source counts
        src = report.price_source or "unknown"
        source_counts[src] = source_counts.get(src, 0) + 1

    total = len(symbol_reports)

    healthy = sum(1 for r in symbol_reports if not r.issues)
    critical = sum(
        1 for r in symbol_reports
        if any(i.severity == SEVERITY_CRITICAL for i in r.issues)
    )
    warning = sum(
        1 for r in symbol_reports
        if (not any(i.severity == SEVERITY_CRITICAL for i in r.issues))
        and r.issues
    )

    missing_price_count = sum(1 for r in symbol_reports if r.missing_price)
    missing_fund_count = sum(1 for r in symbol_reports if r.missing_fundamentals)
    missing_news_count = sum(1 for r in symbol_reports if r.missing_news)
    stale_count = sum(1 for r in symbol_reports if r.stale)
    fallback_count = sum(1 for r in symbol_reports if r.fallback_used)
    cached_count = sum(1 for r in symbol_reports if r.cached)

    # ── Aggregate issues ─────────────────────────────────────────────────────
    agg_issues: list[DataQualityIssue] = []

    if total > 0:
        fallback_rate = fallback_count / total
        if fallback_rate > cfg.max_fallback_rate_warning:
            agg_issues.append(DataQualityIssue(
                issue_type=ISSUE_EXCESSIVE_FALLBACK_RATE,
                severity=SEVERITY_WARNING,
                message=(
                    f"Fallback rate {fallback_rate:.0%} exceeds warning threshold "
                    f"{cfg.max_fallback_rate_warning:.0%} "
                    f"({fallback_count}/{total} symbols)"
                ),
                metadata={
                    "fallback_count": fallback_count,
                    "total": total,
                    "rate": round(fallback_rate, 4),
                    "threshold": cfg.max_fallback_rate_warning,
                },
            ))

        missing_price_rate = missing_price_count / total
        if missing_price_rate > cfg.max_missing_price_rate_critical:
            agg_issues.append(DataQualityIssue(
                issue_type=ISSUE_EXCESSIVE_MISSING_PRICE_RATE,
                severity=SEVERITY_CRITICAL,
                message=(
                    f"Missing price rate {missing_price_rate:.0%} exceeds critical threshold "
                    f"{cfg.max_missing_price_rate_critical:.0%} "
                    f"({missing_price_count}/{total} symbols)"
                ),
                metadata={
                    "missing_price_count": missing_price_count,
                    "total": total,
                    "rate": round(missing_price_rate, 4),
                    "threshold": cfg.max_missing_price_rate_critical,
                },
            ))

        # Check for system-level degraded mode via data_mode field
        degraded_modes = [
            r for r in symbol_reports
            if any(i.issue_type == ISSUE_FALLBACK_USED for i in r.issues)
               or r.stale or r.cached
        ]
        if len(degraded_modes) == total and total > 0:
            agg_issues.append(DataQualityIssue(
                issue_type=ISSUE_DEGRADED_MODE,
                severity=SEVERITY_WARNING,
                message=f"All {total} symbols are operating on degraded data (cached/fallback/stale)",
                metadata={"total": total},
            ))

    summary = DataQualitySummary(
        generated_at=ts,
        observe_only=True,
        available=True,
        total_symbols=total,
        healthy_symbols=healthy,
        warning_symbols=warning,
        critical_symbols=critical,
        missing_price_count=missing_price_count,
        missing_fundamentals_count=missing_fund_count,
        missing_news_count=missing_news_count,
        stale_price_count=stale_count,
        fallback_count=fallback_count,
        cached_count=cached_count,
        source_counts=source_counts,
        issues=agg_issues,
        symbols=symbol_reports,
    )
    summary.summary_line = _compute_summary_line(summary)
    return summary


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _issue_to_dict(issue: DataQualityIssue) -> dict:
    return {
        "issue_type": issue.issue_type,
        "severity": issue.severity,
        "symbol": issue.symbol,
        "source": issue.source,
        "message": issue.message,
        "metadata": issue.metadata,
    }


def _symbol_report_to_dict(report: DataQualitySymbolReport) -> dict:
    return {
        "symbol": report.symbol,
        "price_source": report.price_source,
        "fundamentals_source": report.fundamentals_source,
        "news_source": report.news_source,
        "price_status": report.price_status,
        "fundamentals_status": report.fundamentals_status,
        "news_status": report.news_status,
        "fallback_used": report.fallback_used,
        "cached": report.cached,
        "stale": report.stale,
        "missing_price": report.missing_price,
        "missing_fundamentals": report.missing_fundamentals,
        "missing_news": report.missing_news,
        "issue_count": len(report.issues),
        "issues": [_issue_to_dict(i) for i in report.issues],
    }


def summary_to_dict(summary: DataQualitySummary) -> dict:
    """Convert a DataQualitySummary to a JSON-serializable dict."""
    return {
        "generated_at": summary.generated_at,
        "observe_only": summary.observe_only,
        "available": summary.available,
        "total_symbols": summary.total_symbols,
        "healthy_symbols": summary.healthy_symbols,
        "warning_symbols": summary.warning_symbols,
        "critical_symbols": summary.critical_symbols,
        "missing_price_count": summary.missing_price_count,
        "missing_fundamentals_count": summary.missing_fundamentals_count,
        "missing_news_count": summary.missing_news_count,
        "stale_price_count": summary.stale_price_count,
        "fallback_count": summary.fallback_count,
        "cached_count": summary.cached_count,
        "source_counts": summary.source_counts,
        "summary_line": summary.summary_line,
        "issues": [_issue_to_dict(i) for i in summary.issues],
        "symbols": [_symbol_report_to_dict(r) for r in summary.symbols],
    }


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------


def build_data_quality_markdown(summary: DataQualitySummary) -> str:
    lines: list[str] = []
    lines.append("# Data Quality Report")
    lines.append("")
    lines.append(f"**Generated:** {summary.generated_at}  ")
    lines.append("**Mode:** observe-only")
    lines.append("")
    lines.append("---")
    lines.append("")

    lines.append("## Summary")
    lines.append("")
    lines.append(f"**{summary.summary_line}**")
    lines.append("")
    lines.append("| Metric | Count |")
    lines.append("|--------|-------|")
    lines.append(f"| Total symbols | {summary.total_symbols} |")
    lines.append(f"| Healthy | {summary.healthy_symbols} |")
    lines.append(f"| Warning | {summary.warning_symbols} |")
    lines.append(f"| Critical | {summary.critical_symbols} |")
    lines.append(f"| Missing price | {summary.missing_price_count} |")
    lines.append(f"| Missing fundamentals | {summary.missing_fundamentals_count} |")
    lines.append(f"| Missing news | {summary.missing_news_count} |")
    lines.append(f"| Stale prices | {summary.stale_price_count} |")
    lines.append(f"| Fallback used | {summary.fallback_count} |")
    lines.append(f"| Cache-only | {summary.cached_count} |")
    lines.append("")

    if summary.source_counts:
        lines.append("### Source Counts")
        lines.append("")
        lines.append("| Source | Symbols |")
        lines.append("|--------|---------|")
        for src, cnt in sorted(summary.source_counts.items()):
            lines.append(f"| {src} | {cnt} |")
        lines.append("")

    # Aggregate issues
    all_issues = summary.issues + [
        i for r in summary.symbols for i in r.issues
    ]
    if all_issues:
        lines.append(f"## Issues ({len(all_issues)})")
        lines.append("")
        lines.append("| Symbol | Type | Severity | Message |")
        lines.append("|--------|------|----------|---------|")
        for issue in all_issues[:30]:  # cap at 30 rows in markdown
            sym = issue.symbol or "—"
            lines.append(
                f"| {sym} | {issue.issue_type} | {issue.severity} | {issue.message} |"
            )
        if len(all_issues) > 30:
            lines.append(f"| … | … | … | {len(all_issues) - 30} more issues not shown |")
        lines.append("")

    # Per-symbol table
    if summary.symbols:
        lines.append(f"## Per-Symbol Table ({summary.total_symbols})")
        lines.append("")
        lines.append("| Symbol | Price Status | Fund. | News | Fallback | Cached | Stale | Issues |")
        lines.append("|--------|--------------|-------|------|----------|--------|-------|--------|")
        for r in summary.symbols:
            fund_status = "ok" if not r.missing_fundamentals else "missing"
            news_status = "ok" if not r.missing_news else "missing"
            price_status = "MISSING" if r.missing_price else (r.price_status or "ok")
            lines.append(
                f"| {r.symbol} | {price_status} | {fund_status} | {news_status} "
                f"| {'yes' if r.fallback_used else 'no'} "
                f"| {'yes' if r.cached else 'no'} "
                f"| {'yes' if r.stale else 'no'} "
                f"| {len(r.issues)} |"
            )
        lines.append("")

    lines.append("---")
    lines.append("*Data Quality Monitor is observe-only. "
                 "This report does not affect scoring, allocation, or recommendations.*")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Artifact writer
# ---------------------------------------------------------------------------


def write_data_quality_report(
    summary: DataQualitySummary,
    base_dir: str = "outputs",
) -> tuple[Path, Path]:
    """
    Write data_quality_report.json and data_quality_report.md to
    OutputNamespace.LATEST (outputs/latest/).

    Returns (json_path, md_path).
    Raises if the write fails; callers should wrap in try/except.
    """
    from portfolio_automation.data_governance import OutputNamespace, safe_write_json, safe_write_text

    payload = summary_to_dict(summary)
    json_path = safe_write_json(
        OutputNamespace.LATEST,
        "data_quality_report.json",
        payload,
        base_dir=base_dir,
    )
    md_path = safe_write_text(
        OutputNamespace.LATEST,
        "data_quality_report.md",
        build_data_quality_markdown(summary),
        base_dir=base_dir,
    )
    logger.info(
        "DATA QUALITY: report written — %s symbols, %d issues (%s)",
        summary.total_symbols,
        len(summary.issues) + sum(len(r.issues) for r in summary.symbols),
        summary.summary_line,
    )
    return json_path, md_path
