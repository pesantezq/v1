from __future__ import annotations

import csv
import json
import logging
from pathlib import Path

from watchlist_scanner.models import WatchlistRow, WatchlistScanResult

logger = logging.getLogger("watchlist_scanner")


def _write_signals_json(output_dir: Path, scan_result: WatchlistScanResult) -> None:
    path = output_dir / "watchlist_signals.json"
    path.write_text(json.dumps(scan_result, indent=2, default=str), encoding="utf-8")
    logger.info("watchlist_signals.json written (%d results)", len(scan_result.get("results", [])))


def _write_portfolio_snapshot_json(output_dir: Path, portfolio_snapshot: dict) -> None:
    path = output_dir / "portfolio_snapshot.json"
    path.write_text(json.dumps(portfolio_snapshot, indent=2, default=str), encoding="utf-8")
    rows = len(portfolio_snapshot.get("rows", [])) if isinstance(portfolio_snapshot, dict) else 0
    logger.info("portfolio_snapshot.json written (%d rows)", rows)


def _write_portfolio_summary_md(output_dir: Path, portfolio_snapshot: dict) -> None:
    warnings = list(portfolio_snapshot.get("warnings") or []) if isinstance(portfolio_snapshot, dict) else []
    regime = dict(portfolio_snapshot.get("market_regime") or {}) if isinstance(portfolio_snapshot, dict) else {}
    lines = [
        "# Portfolio Construction View",
        "",
        f"Summary: **{portfolio_snapshot.get('summary_label', 'balanced')}**  ",
        f"{portfolio_snapshot.get('summary_line', 'Portfolio view unavailable.')}  ",
        f"Observe-only: **{'yes' if portfolio_snapshot.get('observe_only', True) else 'no'}**  ",
        f"Suggested allocation: **{float(portfolio_snapshot.get('total_suggested_allocation') or 0.0):.1%}**  ",
        f"Normalized allocation: **{float(portfolio_snapshot.get('total_normalized_allocation') or 0.0):.1%}**  ",
        f"Capped positions: **{int(portfolio_snapshot.get('capped_positions') or 0)}**  ",
        f"Sectors capped: **{', '.join(portfolio_snapshot.get('sectors_capped') or []) or 'none'}**  ",
        "",
        "## Market Regime View",
        "",
        f"- {regime.get('regime_summary_line', 'Market regime unavailable.')}",
        f"- Portfolio fit: {regime.get('regime_portfolio_fit', 'unknown')}",
        f"- Commentary: {regime.get('regime_portfolio_commentary', 'No regime commentary available.')}",
        "",
        "## Concentration Warnings",
        "",
    ]
    if warnings:
        lines.extend([f"- {warning}" for warning in warnings])
    else:
        lines.append("- No concentration warnings.")

    top_sector = portfolio_snapshot.get("top_sector") or {}
    lines += [
        "",
        "## Exposure Summary",
        "",
        f"- Top sector: {top_sector.get('name', 'Unknown')} ({float(top_sector.get('allocation_pct') or 0.0):.1%})",
        f"- Top 3 ticker concentration: {float(portfolio_snapshot.get('top_3_ticker_concentration_pct') or 0.0):.1%}",
        "",
        "## Allocation By Sector",
        "",
    ]
    allocation_by_sector = dict(portfolio_snapshot.get("allocation_by_sector") or {})
    if allocation_by_sector:
        for sector, allocation in sorted(allocation_by_sector.items(), key=lambda item: item[1], reverse=True):
            lines.append(f"- {sector}: {float(allocation or 0.0):.1%}")
    else:
        lines.append("- No actionable allocations.")

    lines += [
        "",
        "## Ticker Allocations",
        "",
        "| Ticker | Conviction | Suggested | Normalized | Cap Reason |",
        "|--------|------------|-----------|------------|------------|",
    ]
    rows = list(portfolio_snapshot.get("rows") or [])
    for row in rows:
        lines.append(
            f"| {row.get('ticker', '')} | {row.get('conviction_band', '')} | "
            f"{float(row.get('suggested_allocation') or 0.0):.1%} | "
            f"{float(row.get('normalized_allocation') or 0.0):.1%} | "
            f"{row.get('allocation_cap_reason', '') or '-'} |"
        )

    path = output_dir / "portfolio_summary.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("portfolio_summary.md written")


def _write_alerts_csv(output_dir: Path, alerts: list[WatchlistRow]) -> None:
    path = output_dir / "watchlist_alerts.csv"
    fields = [
        "operator_rank",
        "ticker", "signal_score", "confidence_weight", "effective_score", "data_quality",
        "alert_tier", "priority_score", "priority_explanation",
        "historical_performance_score", "signal_reliability",
        "conviction_score", "conviction_band", "sizing_recommendation",
        "target_allocation_band", "sizing_multiplier",
        "suggested_allocation", "normalized_allocation",
        "allocation_capped", "allocation_cap_reason",
        "trusted_signal_score",
        "alert_event_id", "surfaced_at", "baseline_price",
        "evaluation_window", "outcome_status", "outcome_pending",
        "confidence_score", "confidence_band",
        "cooldown_active", "cooldown_reason",
        "alert_priority", "alert_basis_summary", "alert_decision_reason",
        "filter_reason_code", "filter_reason",
        "alert_confirmation_summary",
        "confirmation_count", "evidence_count", "cooldown_applied_hours",
        "alert_quality_tier", "evidence_breadth", "evidence_categories",
        "portfolio_priority", "overlap_penalty", "diversification_bonus",
        "existing_position_relevance_bonus", "budget_fit",
        "exposure_context", "final_operator_rank_reason",
        "watchlist_source",
        "theme_news_score", "technical_score", "fundamental_context_score",
        "price", "price_change_pct", "price_change_5d",
        "avg_sentiment", "news_count",
        "volume_spike", "above_sma20", "above_sma50",
        "themes", "headline_examples",
        "sector", "market_cap", "pe_ratio", "profit_margin",
        "scan_time",
    ]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        if not alerts:
            logger.info("watchlist_alerts.csv written (0 alerts)")
            return
        for row in alerts:
            row_copy = dict(row)

            # Flatten score breakdown
            bd = row_copy.pop("score_breakdown", {}) or {}
            row_copy["theme_news_score"]          = bd.get("theme_news_score", "")
            row_copy["technical_score"]           = bd.get("technical_score", "")
            row_copy["fundamental_context_score"] = bd.get("fundamental_context_score", "")

            # Flatten fundamentals
            fund = row_copy.pop("fundamentals", {}) or {}
            row_copy["sector"]        = fund.get("sector", "")
            row_copy["market_cap"]    = fund.get("market_cap", "")
            row_copy["pe_ratio"]      = fund.get("pe_ratio", "")
            row_copy["profit_margin"] = fund.get("profit_margin", "")

            # 5-day change from technicals sub-object
            tech_sub = row_copy.pop("technicals", {}) or {}
            row_copy.setdefault("price_change_5d", tech_sub.get("price_change_5d", ""))

            # Drop remaining sub-objects
            row_copy.pop("news", None)

            # Stringify list fields
            row_copy["themes"]            = "; ".join(row_copy.get("themes") or [])
            row_copy["headline_examples"] = " | ".join(row_copy.get("headline_examples") or [])
            row_copy["evidence_categories"] = "; ".join(row_copy.get("evidence_categories") or [])

            w.writerow(row_copy)
    logger.info("watchlist_alerts.csv written (%d alerts)", len(alerts))


def _write_summary_md(output_dir: Path, scan_result: WatchlistScanResult) -> None:
    from watchlist_scanner.fundamentals_engine import format_market_cap

    results  = scan_result.get("results", [])
    alerts   = scan_result.get("alerts", [])
    run_date = scan_result.get("run_date", "")
    calls    = scan_result.get("calls_used", 0)

    scan_summary = scan_result.get("scan_summary", {})
    scan_status  = scan_summary.get("scan_status", "ok")
    data_mode    = scan_result.get("data_mode", scan_summary.get("data_mode", "live"))
    degraded_mode = scan_result.get("degraded_mode", scan_summary.get("degraded_mode", False))
    degraded_reason = scan_result.get("degraded_reason", scan_summary.get("degraded_reason"))
    n_fresh      = scan_summary.get("symbols_fresh", len(results))
    n_cached     = scan_summary.get("symbols_cached", 0)
    n_partial    = scan_summary.get("symbols_partial", 0)
    n_skipped    = scan_summary.get("symbols_budget_skipped", 0)
    n_cooldown_suppressed = scan_summary.get("alerts_cooldown_suppressed", 0)
    n_action_suppressed = scan_summary.get("alerts_action_suppressed", 0)
    n_signals_suppressed = scan_summary.get("signals_suppressed", 0)
    n_perf_tracked = scan_summary.get("performance_tracked_signals", 0)
    n_perf_resolved = scan_summary.get("performance_resolved_signals", 0)
    conviction_summary_line = scan_summary.get("conviction_summary_line", "")
    portfolio_summary_line = scan_summary.get("portfolio_construction_summary_line", "")
    regime_summary_line = scan_summary.get("market_regime_summary_line", "")

    # Confidence band counts across all results
    n_conf_high   = sum(1 for r in results if r.get("confidence_band") == "high")
    n_conf_medium = sum(1 for r in results if r.get("confidence_band") == "medium")
    n_conf_low    = sum(1 for r in results if r.get("confidence_band") == "low")

    status_note = ""
    if scan_status == "degraded":
        status_note = (
            f"  \n> **Note:** Scan partially degraded due to API budget limits — "
            f"{n_partial} partial, {n_skipped} budget_skipped, {n_cached} cached fallback."
        )
    elif scan_status == "cache_only":
        status_note = "  \n> **Note:** All results from cache — API budget exhausted."

    lines = [
        f"# Watchlist Scanner — {run_date}",
        "",
        f"Generated: {scan_result.get('generated_at', '')}  ",
        f"API calls used today: **{calls}**  ",
        f"Symbols scanned: **{len(results)}**  ",
        f"Alerts triggered: **{len(alerts)}**  ",
        f"Cooldown-suppressed: **{n_cooldown_suppressed}**  ",
        f"Action-suppressed: **{n_action_suppressed}**  ",
        f"Signals suppressed: **{n_signals_suppressed}**  ",
        f"Performance feedback: **{n_perf_tracked}** tracked | **{n_perf_resolved}** resolved  ",
        f"Data mode: **{data_mode}**  ",
        f"Degraded mode: **{'yes' if degraded_mode else 'no'}**"
        + (f" (`{degraded_reason}`)  " if degraded_reason else "  "),
        f"Data quality: **{n_fresh}** fresh | **{n_partial}** partial | "
        f"**{n_skipped}** budget_skipped | **{n_cached}** cached  ",
        f"Confidence: **{n_conf_high}** high | **{n_conf_medium}** medium | **{n_conf_low}** low",
        conviction_summary_line + ("  " if conviction_summary_line else ""),
        portfolio_summary_line + ("  " if portfolio_summary_line else ""),
        regime_summary_line + ("  " if regime_summary_line else ""),
        status_note,
        "",
    ]

    if alerts:
        lines += ["## Alerts", ""]
        for a in alerts:
            sym   = a["ticker"]
            score = a.get("signal_score", 0)
            pct   = a.get("price_change_pct")
            spike = "  vol-spike" if a.get("volume_spike") else ""
            themes = ", ".join(a.get("themes") or []) or "—"
            pct_str = f"{pct:+.2f}%" if pct is not None else "N/A"

            bd    = a.get("score_breakdown") or {}
            fund  = a.get("fundamentals") or {}
            tech  = a.get("technicals") or {}
            news  = a.get("news") or {}

            # Fundamental context line
            sector   = fund.get("sector") or "N/A"
            mktcap   = format_market_cap(fund.get("market_cap"))
            pe       = fund.get("pe_ratio")
            pe_str   = f"PE {pe:.1f}" if pe else "PE N/A"

            # News line
            hl_count = news.get("headline_count") or a.get("news_count", 0)
            avg_sent = news.get("avg_sentiment") or a.get("avg_sentiment") or 0.0
            sent_str = f"{avg_sent:+.3f}" if avg_sent else "0.000"

            # 5-day change
            pc5 = tech.get("price_change_5d") or a.get("price_change_5d")
            pc5_str = f" | 5d: {pc5:+.2f}%" if pc5 is not None else ""

            dq = a.get("data_quality", "fresh")
            dq_badge = f"  [{dq}]" if dq != "fresh" else ""
            conf_score = a.get("confidence_score")
            conf_band  = a.get("confidence_band", "")
            conf_str   = f"  conf={conf_score:.2f} [{conf_band}]" if conf_score is not None else ""
            src_badge  = "  [extended]" if a.get("watchlist_source") == "extended_theme" else ""
            operator_rank = a.get("operator_rank", 0)
            alert_tier = a.get("alert_tier") or "none"
            priority_score = float(a.get("priority_score") or 0.0)
            priority_explanation = a.get("priority_explanation") or "n/a"
            lines.append(
                f"### #{operator_rank} {sym}  score={score:.2f}  priority={priority_score:.2f}  "
                f"tier={alert_tier}  {pct_str}{spike}{dq_badge}{conf_str}{src_badge}"
            )
            alert_priority = a.get("alert_priority") or "suppressed"
            alert_basis = a.get("alert_basis_summary") or "none"
            alert_reason = a.get("alert_decision_reason") or "decision reason unavailable"
            filter_reason = a.get("filter_reason") or "allowed"
            confirmation = a.get("alert_confirmation_summary") or "none"
            confirmation_count = a.get("confirmation_count", len(a.get("alert_confirmation_signals") or []))
            evidence_count = a.get("evidence_count", a.get("evidence_breadth", 0))
            quality_tier = a.get("alert_quality_tier") or "none"
            evidence_breadth = a.get("evidence_breadth", 0)
            evidence_categories = ", ".join(a.get("evidence_categories") or []) or "none"
            portfolio_priority = float(a.get("portfolio_priority") or 0.0)
            exposure_context = a.get("exposure_context") or "none"
            budget_fit = a.get("budget_fit") or "unknown"
            rank_reason = a.get("final_operator_rank_reason") or "portfolio-neutral"
            outcome_status = a.get("outcome_status") or "pending"
            event_id = a.get("alert_event_id") or "n/a"
            baseline_price = a.get("baseline_price")
            baseline_str = f"${baseline_price:.2f}" if isinstance(baseline_price, (int, float)) else "N/A"
            evaluation_window = a.get("evaluation_window") or "1d,3d,5d,10d"
            lines.append(f"**Alerting:** {alert_priority} via {alert_basis} | {alert_reason}  ")
            lines.append(f"**Filter:** {filter_reason}  ")
            lines.append(f"**Confirmation:** {confirmation}  ")
            lines.append(
                f"**Promotion quality:** {quality_tier} | breadth {evidence_breadth} | "
                f"confirmations {confirmation_count} | evidence count {evidence_count} | categories: {evidence_categories}  "
            )
            lines.append(f"**Ranking:** {priority_score:.2f} | {priority_explanation}  ")
            lines.append(
                f"**Conviction:** {a.get('conviction_band', 'observe')} "
                f"({float(a.get('conviction_score') or 0.0):.2f}) | "
                f"sizing {a.get('target_allocation_band', 'n/a')} | "
                f"{a.get('sizing_reason', 'n/a')}  "
            )
            lines.append(
                f"**Portfolio fit:** priority {portfolio_priority:+.2f} | {exposure_context} | "
                f"reason: {rank_reason} | budget {budget_fit}  "
            )
            lines.append(
                f"**Outcome tracking:** event {event_id} | {outcome_status} | "
                f"baseline {baseline_str} | windows {evaluation_window}  "
            )
            lines.append(f"**Fundamentals:** {sector} | {mktcap} | {pe_str}  ")
            lines.append(
                f"**News:** {hl_count} headlines | avg sentiment {sent_str} | "
                f"themes: {themes}  "
            )
            lines.append(
                f"**Technicals:** "
                f"SMA20={'yes' if tech.get('above_sma20') else 'no'} | "
                f"SMA50={'yes' if tech.get('above_sma50') else 'no'}"
                f"{pc5_str}  "
            )
            lines.append(
                f"**Score breakdown:** "
                f"news {bd.get('theme_news_score', 0):.2f} × 0.45 | "
                f"tech {bd.get('technical_score', 0):.2f} × 0.30 | "
                f"fund {bd.get('fundamental_context_score', 0):.2f} × 0.25  "
            )
            for h in (a.get("headline_examples") or [])[:2]:
                lines.append(f"> {h}")
            lines.append("")

    # All Signals table
    lines += ["## All Signals", ""]
    lines.append(
        "| Rank | Ticker | Score | Conviction | Size | Priority | Tier | Alert | Filtered | Quality | Conf | Source | Price | 1d% | 5d% | SMA20 | SMA50 | Vol | Sentiment | Sector | Themes |"
    )
    lines.append(
        "|------|--------|-------|------------|------|----------|------|-------|----------|---------|------|--------|-------|-----|-----|-------|-------|-----|-----------|--------|--------|"
    )
    for r in results:
        sym   = r["ticker"]
        operator_rank = r.get("operator_rank", "")
        score = r.get("signal_score", 0)
        priority_score = float(r.get("priority_score") or 0.0)
        alert_tier = r.get("alert_tier") or "none"
        price = r.get("price")
        pct   = r.get("price_change_pct")
        dq    = r.get("data_quality", "fresh")

        tech  = r.get("technicals") or {}
        pc5   = tech.get("price_change_5d")
        s20   = "yes" if r.get("above_sma20") else "no"
        s50   = "yes" if r.get("above_sma50") else "no"
        vspike = "yes" if r.get("volume_spike") else ""

        fund   = r.get("fundamentals") or {}
        sector = (fund.get("sector") or "—")[:18]

        avg_sent = r.get("avg_sentiment") or 0.0
        themes   = ", ".join((r.get("themes") or [])[:2]) or "—"

        price_str = f"${price:.2f}" if price else "N/A"
        pct_str   = f"{pct:+.2f}%" if pct is not None else "N/A"
        pc5_str   = f"{pc5:+.2f}%" if pc5 is not None else "N/A"
        sent_str  = f"{avg_sent:+.3f}"

        conf_score = r.get("confidence_score")
        conf_band  = r.get("confidence_band", "")
        conf_str   = f"{conf_score:.2f} [{conf_band}]" if conf_score is not None else "N/A"
        conviction_band = r.get("conviction_band", "observe")
        size_band = r.get("target_allocation_band", "n/a")
        src        = "ext" if r.get("watchlist_source") == "extended_theme" else "static"
        alert_priority = r.get("alert_priority") or "suppressed"
        filtered_reason = r.get("filtered_reason") or "—"
        if r.get("notification_status") == "cooldown_suppressed":
            alert_priority = "cooldown"

        lines.append(
            f"| {operator_rank} | {sym} | {score:.2f} | {conviction_band} | {size_band} | {priority_score:.2f} | {alert_tier} | {alert_priority} | {filtered_reason} | {dq} | {conf_str} | {src} | {price_str} | {pct_str} | {pc5_str} "
            f"| {s20} | {s50} | {vspike} | {sent_str} | {sector} | {themes} |"
        )

    path = output_dir / "watchlist_summary.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("watchlist_summary.md written")
