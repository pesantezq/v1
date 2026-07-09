"""System / Developer cockpit — answers "is the system healthy enough to trust today?"

Composes normalized `shared.card(...)` cards from pipeline health, data-quality,
budget, delivery, audit, backfill, Schwab broker-health, and analysis-loop status
artifacts. REUSES `gui_v2.data.health.collect_health_view` and
`gui_v2.data.operations.collect_operations_view` as data sources where helpful.

SAFETY:
  - No trade/execute/buy/sell/hold language anywhere in this module.
  - broker_sync_status absent → explicit "Schwab not configured" card with
    status="info" (NOT red — Schwab is optional).
  - All cards set source_artifacts.
  - observe_only=True hardcoded.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from gui_v2.data.shared import card, _read_json

# ---------------------------------------------------------------------------
# Status-mapping helpers
# ---------------------------------------------------------------------------

_STATUS_MAP: dict[str, str] = {
    "ok": "ok",
    "green": "ok",
    "healthy": "ok",
    "success": "ok",
    "ok_with_warnings": "warning",
    "partial": "warning",
    "warn": "warning",
    "warning": "warning",
    "amber": "warning",
    "degraded": "warning",
    "failed": "red",
    "error": "red",
    "red": "red",
    "unconfigured": "info",
    "not_configured": "info",
    "info": "info",
    "unknown": "unknown",
}


def _map_status(raw: str | None) -> str:
    return _STATUS_MAP.get((raw or "unknown").lower().strip(), "unknown")


def _yesno(val: object) -> str:
    if val is True:
        return "yes"
    if val is False:
        return "no"
    return str(val) if val is not None else "—"


# ---------------------------------------------------------------------------
# Individual card builders
# ---------------------------------------------------------------------------

def _card_daily_run(latest: Path) -> dict:
    """Daily run status — overall pipeline health."""
    drs = _read_json(latest / "daily_run_status.json") or {}
    if not drs:
        return card(
            "Daily Run Status",
            status="unknown",
            label="unavailable",
            summary="daily_run_status.json absent — run the daily pipeline",
            source_artifacts=["daily_run_status.json"],
        )

    overall = drs.get("overall_status") or "unknown"
    stage_summary = drs.get("stage_summary") or {}
    total = (stage_summary.get("total") or 0) if isinstance(stage_summary, dict) else 0
    ok_count = (stage_summary.get("ok") or 0) if isinstance(stage_summary, dict) else 0
    warn_count = (stage_summary.get("warn") or 0) if isinstance(stage_summary, dict) else 0
    failed_count = (stage_summary.get("failed") or 0) if isinstance(stage_summary, dict) else 0
    content_warn_count = drs.get("content_warn_count") or 0

    parts = [f"{ok_count}/{total} stages OK"]
    if warn_count:
        parts.append(f"{warn_count} warn")
    if failed_count:
        parts.append(f"{failed_count} failed")
    if content_warn_count:
        parts.append(f"{content_warn_count} content warnings")

    return card(
        "Daily Run Status",
        status=_map_status(overall),
        label=overall,
        summary="; ".join(parts),
        source_artifacts=["daily_run_status.json"],
        updated_at=drs.get("generated_at"),
    )


def _card_pipeline_run(latest: Path) -> dict:
    """Pipeline run status — step-level detail."""
    prs = _read_json(latest / "pipeline_run_status.json") or {}
    if not prs:
        return card(
            "Pipeline Run Status",
            status="unknown",
            label="unavailable",
            summary="pipeline_run_status.json absent — run the daily pipeline",
            source_artifacts=["pipeline_run_status.json"],
        )

    success = prs.get("success")
    steps_attempted = prs.get("steps_attempted") or 0
    steps_succeeded = prs.get("steps_succeeded") or 0
    steps_failed = prs.get("steps_failed") or 0
    steps_skipped = prs.get("steps_skipped") or 0
    run_mode = prs.get("run_mode") or "unknown"

    if success is True:
        c_status = "ok"
        label = "success"
    elif success is False:
        c_status = "red"
        label = "failed"
    else:
        c_status = "unknown"
        label = "unknown"

    parts = [f"{steps_succeeded}/{steps_attempted} steps succeeded"]
    if steps_failed:
        parts.append(f"{steps_failed} failed")
    if steps_skipped:
        parts.append(f"{steps_skipped} skipped")
    parts.append(f"mode: {run_mode}")

    return card(
        "Pipeline Run Status",
        status=c_status,
        label=label,
        summary="; ".join(parts),
        source_artifacts=["pipeline_run_status.json"],
        updated_at=prs.get("generated_at"),
    )


def _card_artifact_registry(latest: Path) -> dict:
    """Artifact registry status — from pending branch; absent is normal."""
    art = _read_json(latest / "artifact_registry_status.json")
    if art is None:
        return card(
            "Artifact Registry",
            status="info",
            label="not yet available",
            summary="artifact_registry_status.json absent — feature from pending branch; this is normal",
            source_artifacts=["artifact_registry_status.json"],
        )

    overall = art.get("overall_status") or "unknown"
    registered = art.get("registered_count") or 0
    missing = art.get("missing_count") or 0
    stale = art.get("stale_count") or 0

    parts = [f"{registered} registered"]
    if missing:
        parts.append(f"{missing} missing")
    if stale:
        parts.append(f"{stale} stale")

    return card(
        "Artifact Registry",
        status=_map_status(overall),
        label=overall,
        summary="; ".join(parts),
        source_artifacts=["artifact_registry_status.json"],
        updated_at=art.get("generated_at"),
    )


def _card_data_quality(latest: Path) -> dict:
    """Data quality report — symbol-level health."""
    dq = _read_json(latest / "data_quality_report.json") or {}
    if not dq:
        return card(
            "Data Quality",
            status="unknown",
            label="unavailable",
            summary="data_quality_report.json absent — run the daily pipeline",
            source_artifacts=["data_quality_report.json"],
        )

    total = dq.get("total_symbols") or 0
    healthy = dq.get("healthy_symbols") or 0
    warning_syms = dq.get("warning_symbols") or 0
    critical = dq.get("critical_symbols") or 0
    summary_line = dq.get("summary_line") or ""

    if critical > 0:
        c_status = "red"
        label = f"{critical} critical"
    elif warning_syms > 0:
        c_status = "warning"
        label = f"{warning_syms} warnings"
    else:
        c_status = "ok"
        label = f"{healthy}/{total} healthy"

    summary = summary_line or f"{healthy}/{total} symbols healthy; {warning_syms} warn; {critical} critical"

    return card(
        "Data Quality",
        status=c_status,
        label=label,
        summary=summary,
        source_artifacts=["data_quality_report.json"],
        updated_at=dq.get("generated_at"),
    )


def _card_fmp_budget(latest: Path) -> dict:
    """FMP API budget status."""
    fmp = _read_json(latest / "fmp_budget_status.json") or {}
    if not fmp:
        return card(
            "FMP Budget",
            status="unknown",
            label="unavailable",
            summary="fmp_budget_status.json absent — run the daily pipeline",
            source_artifacts=["fmp_budget_status.json"],
        )

    overall = fmp.get("overall_status") or "unknown"
    budget_obj = fmp.get("budget") or {}
    used = (budget_obj.get("used") or 0) if isinstance(budget_obj, dict) else 0
    cap = (budget_obj.get("cap") or 0) if isinstance(budget_obj, dict) else 0
    pct = (used / cap * 100) if cap else 0

    parts: list[str] = []
    if cap:
        parts.append(f"{used}/{cap} calls ({pct:.0f}%)")
    else:
        parts.append(f"{used} calls")
    news_obj = fmp.get("news") or {}
    if isinstance(news_obj, dict) and news_obj.get("used") is not None:
        parts.append(f"news: {news_obj.get('used')}/{news_obj.get('cap')}")

    return card(
        "FMP Budget",
        status=_map_status(overall),
        label=overall,
        summary="; ".join(parts) or "FMP budget data available",
        source_artifacts=["fmp_budget_status.json"],
        updated_at=fmp.get("generated_at"),
    )


def _card_ai_budget(latest: Path) -> dict:
    """AI / LLM budget summary."""
    ai = _read_json(latest / "ai_budget_summary.json") or {}
    if not ai:
        return card(
            "AI Budget",
            status="unknown",
            label="unavailable",
            summary="ai_budget_summary.json absent — run the daily pipeline",
            source_artifacts=["ai_budget_summary.json"],
        )

    blocked = ai.get("blocked") or False
    warning = ai.get("warning") or False
    daily_cost = ai.get("daily_cost_total_usd") or 0
    monthly_cost = ai.get("monthly_cost_total_usd") or 0
    daily_limit = ai.get("daily_cost_limit_usd") or 0
    monthly_limit = ai.get("monthly_cost_limit_usd") or 0
    summary_line = ai.get("summary_line") or ""

    if blocked:
        c_status = "red"
        label = "blocked"
    elif warning:
        c_status = "warning"
        label = "near limit"
    else:
        c_status = "ok"
        label = "within budget"

    parts: list[str] = []
    if summary_line:
        parts.append(summary_line)
    else:
        if daily_limit:
            parts.append(f"Daily: ${daily_cost:.4f}/${daily_limit:.2f}")
        if monthly_limit:
            parts.append(f"Monthly: ${monthly_cost:.4f}/${monthly_limit:.2f}")

    return card(
        "AI Budget",
        status=c_status,
        label=label,
        summary="; ".join(parts) or "AI budget data available",
        source_artifacts=["ai_budget_summary.json"],
        updated_at=ai.get("generated_at"),
    )


def _card_memo_delivery(latest: Path) -> dict:
    """Memo delivery status — email delivery health."""
    memo = _read_json(latest / "memo_delivery_status.json") or {}
    if not memo:
        return card(
            "Memo Delivery",
            status="unknown",
            label="unavailable",
            summary="memo_delivery_status.json absent — run the daily pipeline",
            source_artifacts=["memo_delivery_status.json"],
        )

    enabled = memo.get("enabled") or False
    sent = memo.get("sent") or False
    dry_run = memo.get("dry_run") or False
    skipped = memo.get("skipped") or False
    reason = memo.get("reason") or ""
    recipients_count = memo.get("recipients_count") or 0

    if not enabled:
        c_status = "info"
        label = "disabled"
        summary = "Memo delivery not enabled"
    elif dry_run:
        c_status = "info"
        label = "dry-run"
        summary = f"Dry-run mode; {recipients_count} recipient(s)"
    elif sent:
        c_status = "ok"
        label = "sent"
        summary = f"Sent to {recipients_count} recipient(s)"
    elif skipped:
        c_status = "warning"
        label = "skipped"
        summary = reason or "Skipped"
    else:
        c_status = "warning"
        label = "not sent"
        summary = reason or "Not sent"

    return card(
        "Memo Delivery",
        status=c_status,
        label=label,
        summary=summary,
        source_artifacts=["memo_delivery_status.json"],
        updated_at=memo.get("generated_at"),
    )


def _card_doc_audit(latest: Path) -> dict:
    """Documentation audit status."""
    doc = _read_json(latest / "doc_audit_status.json") or {}
    if not doc:
        return card(
            "Doc Audit",
            status="unknown",
            label="unavailable",
            summary="doc_audit_status.json absent — run /doc-audit",
            source_artifacts=["doc_audit_status.json"],
        )

    overall = doc.get("overall_status") or "unknown"
    findings = doc.get("findings") or []
    auto_fix_candidates = doc.get("auto_fix_candidates") or []
    coverage_gaps = doc.get("coverage_gaps") or []
    auto_fixes_applied = doc.get("auto_fixes_applied") or 0

    n_findings = len(findings) if isinstance(findings, list) else 0
    n_fix_candidates = len(auto_fix_candidates) if isinstance(auto_fix_candidates, list) else 0
    n_gaps = len(coverage_gaps) if isinstance(coverage_gaps, list) else 0

    parts = []
    if n_findings:
        parts.append(f"{n_findings} findings")
    if n_fix_candidates:
        parts.append(f"{n_fix_candidates} auto-fix candidates")
    if n_gaps:
        parts.append(f"{n_gaps} coverage gaps")
    if auto_fixes_applied:
        parts.append(f"{auto_fixes_applied} fixes applied")

    return card(
        "Doc Audit",
        status=_map_status(overall),
        label=overall,
        summary="; ".join(parts) or "Doc audit data available",
        source_artifacts=["doc_audit_status.json"],
        updated_at=doc.get("generated_at"),
    )


def _card_historical_backfill(latest: Path) -> dict:
    """Historical backfill status."""
    hb = _read_json(latest / "historical_backfill_status.json") or {}
    if not hb:
        return card(
            "Historical Backfill",
            status="unknown",
            label="unavailable",
            summary="historical_backfill_status.json absent — run backfill pipeline",
            source_artifacts=["historical_backfill_status.json"],
        )

    fetched = hb.get("fetched") or 0
    skipped_fresh = hb.get("skipped_fresh") or 0
    skipped_budget = hb.get("skipped_budget") or 0
    errored = hb.get("errored") or 0
    freshness_days = hb.get("freshness_days") or 0
    universe_size = hb.get("universe_size") or 0

    if errored > 0:
        c_status = "warning"
        label = f"{errored} errors"
    elif skipped_budget > 0:
        c_status = "warning"
        label = "budget limited"
    else:
        c_status = "ok"
        label = "complete"

    parts = [f"{fetched} fetched, {skipped_fresh} fresh-skipped"]
    if skipped_budget:
        parts.append(f"{skipped_budget} budget-skipped")
    if errored:
        parts.append(f"{errored} errored")
    if freshness_days:
        parts.append(f"freshness: {freshness_days}d")
    if universe_size:
        parts.append(f"universe: {universe_size}")

    return card(
        "Historical Backfill",
        status=c_status,
        label=label,
        summary="; ".join(parts),
        source_artifacts=["historical_backfill_status.json"],
        updated_at=hb.get("generated_at"),
    )


def _card_broker_schwab(latest: Path) -> dict:
    """Schwab broker health — optional; absent is NORMAL (status=info, not red)."""
    bss = _read_json(latest / "broker_sync_status.json")

    # Explicit empty state when absent — info, not red.
    if bss is None:
        return card(
            "Schwab Broker Health",
            status="info",
            label="not configured",
            summary="Schwab not configured — broker_sync_status.json absent. Schwab is optional.",
            source_artifacts=["broker_sync_status.json"],
        )

    overall = bss.get("overall_status") or "unknown"
    configured = bss.get("configured") or False
    authenticated = bss.get("authenticated") or False
    read_only_mode = bss.get("read_only_mode")
    enabled = bss.get("enabled") or False

    # Map overall_status to card status
    # "unconfigured" → info (not red — this is expected/optional)
    if overall.lower() in ("unconfigured", "not_configured"):
        c_status = "info"
    else:
        c_status = _map_status(overall)

    parts: list[str] = [f"overall: {overall}"]
    parts.append(f"configured: {_yesno(configured)}")
    parts.append(f"authenticated: {_yesno(authenticated)}")
    if read_only_mode is not None:
        parts.append(f"read-only: {_yesno(read_only_mode)}")
    # NOTE: No trade/execute language; read_only_mode is a connection property.

    label = overall if overall else "unknown"

    return card(
        "Schwab Broker Health",
        status=c_status,
        label=label,
        summary="; ".join(parts),
        source_artifacts=["broker_sync_status.json"],
        updated_at=bss.get("generated_at"),
    )


def _card_failure_queue(latest: Path) -> tuple[dict, list[dict[str, Any]]]:
    """Failure queue — stages with status failed/warn from daily_run_status."""
    drs = _read_json(latest / "daily_run_status.json") or {}
    stages = drs.get("stages") or []

    failed_stages: list[dict[str, Any]] = []
    if isinstance(stages, list):
        for s in stages:
            if isinstance(s, dict) and s.get("status") in ("failed", "warn"):
                failed_stages.append({
                    "name": s.get("name") or "unknown",
                    "status": s.get("status") or "unknown",
                    "output_lines_count": s.get("output_lines_count") or 0,
                })

    if not drs:
        summary = "daily_run_status.json absent"
        c_status = "unknown"
        label = "unavailable"
    elif not failed_stages:
        summary = "No failed or warned stages"
        c_status = "ok"
        label = "clean"
    else:
        n_failed = sum(1 for s in failed_stages if s["status"] == "failed")
        n_warn = sum(1 for s in failed_stages if s["status"] == "warn")
        parts = []
        if n_failed:
            parts.append(f"{n_failed} failed")
        if n_warn:
            parts.append(f"{n_warn} warned")
        summary = "; ".join(parts) + f" — {', '.join(s['name'] for s in failed_stages[:5])}"
        c_status = "red" if n_failed else "warning"
        label = f"{n_failed} failed, {n_warn} warn"

    c = card(
        "Failure Queue",
        status=c_status,
        label=label,
        summary=summary,
        source_artifacts=["daily_run_status.json"],
        updated_at=drs.get("generated_at") if drs else None,
    )
    return c, failed_stages


def _card_analysis_status(root: Path) -> dict:
    """Daily / monthly / yearly analysis loop status from data/*_check_state.json."""
    data_dir = root / "data"

    daily = _read_json(data_dir / "daily_check_state.json") or {}
    monthly = _read_json(data_dir / "monthly_check_state.json") or {}

    parts: list[str] = []

    daily_run_at = daily.get("last_run_at")
    if daily_run_at:
        parts.append(f"Daily analysis: {daily_run_at[:10]}")
    else:
        parts.append("Daily analysis: never run")

    monthly_run_at = monthly.get("last_run_at")
    monthly_verdict = monthly.get("last_verdict")
    if monthly_run_at:
        verdict_str = f" ({monthly_verdict})" if monthly_verdict else ""
        parts.append(f"Monthly: {monthly_run_at[:10]}{verdict_str}")
    else:
        parts.append("Monthly: never run")

    if daily or monthly:
        c_status = "info"
        label = "observed"
        if monthly_verdict and monthly_verdict.upper() in ("RED", "FAIL", "FAILED"):
            c_status = "red"
            label = f"monthly {monthly_verdict}"
        elif monthly_verdict and monthly_verdict.upper() in ("AMBER", "WARN", "WARNING"):
            c_status = "warning"
            label = f"monthly {monthly_verdict}"
    else:
        c_status = "unknown"
        label = "no data"
        parts = ["Analysis state files absent — run daily/monthly analysis tools"]

    return card(
        "Analysis Loop Status",
        status=c_status,
        label=label,
        summary="; ".join(parts),
        source_artifacts=["data/daily_check_state.json", "data/monthly_check_state.json"],
        updated_at=daily_run_at or monthly_run_at,
    )


# ---------------------------------------------------------------------------
# Public collector
# ---------------------------------------------------------------------------

def _card_pipeline_wiring(latest: Path) -> dict | None:
    """Producer→consumer wiring audit (dev/ops lens). Absent → omitted."""
    pw = _read_json(latest / "pipeline_wiring_status.json")
    if not pw:
        return None
    s = pw.get("summary") or {}
    total = s.get("total_audited") or 0
    healthy = s.get("healthy") or 0
    unwired = s.get("unwired") or 0
    idle = s.get("event_log_idle") or 0
    not_aud = s.get("not_audited") or 0
    overall = pw.get("overall_status") or "unknown"
    return card(
        "Pipeline Wiring",
        status=_map_status(overall),
        label=overall,
        summary=(f"{healthy}/{total} healthy · {unwired} unwired · "
                 f"{idle} event-log idle · {not_aud} not audited"),
        source_artifacts=["pipeline_wiring_status.json"],
        updated_at=pw.get("generated_at"),
    )


def _card_discovery_pulse(latest: Path) -> dict | None:
    """Universe-discovery funnel: budget caps/usage + tier counts. Absent → omitted.

    Covers theme_signals / watch_candidates via the pulse's tier_a counts (listed
    as source artifacts). Observe-only telemetry.
    """
    dp = _read_json(latest / "discovery_pulse_status.json")
    if not dp:
        return None
    caps = dp.get("caps") or {}
    usage = dp.get("usage") or {}
    tier_a = dp.get("tier_a") or {}
    if dp.get("skipped"):
        status = "warning"
        summary = f"pulse skipped — {dp.get('skip_reason') or 'no reason given'}"
    else:
        status = "ok"
        parts: list[str] = []
        themes_n = tier_a.get("themes_count")
        watch_n = tier_a.get("watch_candidates_count")
        fmp_used, fmp_max = usage.get("fmp_calls_month"), caps.get("fmp_calls_max")
        oa_used, oa_max = usage.get("openai_cost_usd_month"), caps.get("openai_cost_usd_max")
        if themes_n is not None:
            parts.append(f"{themes_n} themes")
        if watch_n is not None:
            parts.append(f"{watch_n} watch candidates")
        if fmp_used is not None and fmp_max is not None:
            parts.append(f"FMP {fmp_used}/{fmp_max}")
        if oa_used is not None and oa_max is not None:
            parts.append(f"OpenAI ${oa_used:.0f}/${oa_max:.0f}")
        summary = " · ".join(parts) or "discovery pulse active"
    return card(
        "Discovery Pulse",
        status=status,
        label=dp.get("month") or "observe only",
        summary=summary,
        source_artifacts=["discovery_pulse_status.json", "theme_signals.json",
                          "watch_candidates.json"],
        updated_at=dp.get("generated_at"),
    )


def collect_system_view(root: Path) -> dict[str, Any]:
    """
    Persona collector for /dashboard/system.

    Returns::

        {
          "cards": [ <card dicts> ],
          "persona": "system",
          "failed_stages": [ <stage dicts> ],   # for the failure-queue table
          "observe_only": True,
        }
    """
    root = Path(root)
    latest = root / "outputs" / "latest"
    cards: list[dict[str, Any]] = []

    # 1. Daily run status
    cards.append(_card_daily_run(latest))

    # 2. Pipeline run status
    cards.append(_card_pipeline_run(latest))

    # 3. Artifact registry (from pending branch — absent is normal → empty state)
    cards.append(_card_artifact_registry(latest))

    # 4. Data quality
    cards.append(_card_data_quality(latest))

    # 5. FMP budget
    cards.append(_card_fmp_budget(latest))

    # 6. AI budget
    cards.append(_card_ai_budget(latest))

    # 7. Memo delivery
    cards.append(_card_memo_delivery(latest))

    # 8. Doc audit
    cards.append(_card_doc_audit(latest))

    # 9. Historical backfill
    cards.append(_card_historical_backfill(latest))

    # 10. Schwab broker health (optional — absent → info empty state, NOT red)
    cards.append(_card_broker_schwab(latest))

    # 11. Failure queue (card + rows)
    failure_queue_card, failed_stages = _card_failure_queue(latest)
    cards.append(failure_queue_card)

    # 12. Analysis loop status (daily/monthly/yearly)
    cards.append(_card_analysis_status(root))

    # Dev/ops surfaces — appended only when their artifact exists (absent = omit,
    # not a red/empty card, since these are optional telemetry).
    _wiring = _card_pipeline_wiring(latest)
    if _wiring:
        cards.append(_wiring)
    _pulse = _card_discovery_pulse(latest)
    if _pulse:
        cards.append(_pulse)

    return {
        "cards": cards,
        "persona": "system",
        "failed_stages": failed_stages,
        "observe_only": True,
    }
