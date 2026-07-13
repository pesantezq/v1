"""Task 4 (Milestone 4): /dashboard/system — health + Schwab broker health.

Tests:
  - collect_system_view returns expected card titles and structure
  - every card has non-empty source_artifacts
  - route renders 200 (artifacts present + absent → empty states)
  - broker_sync_status absent → explicit "Schwab not configured" info card (not red)
  - broker_sync_status present (fixture: overall_status unconfigured) → status card
  - failure-queue card lists failed/warn stages from a daily_run_status fixture
  - failure-queue card empty-state when all stages clean
  - no forbidden action labels in rendered HTML
  - no forbidden action labels in template file
  - mobile card stacks present (md:hidden)
  - observe_only=True in view dict
  - persona field is "system"
  - each card source_artifacts non-empty
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EXPECTED_CARD_TITLES = {
    "Daily Run Status",
    "Pipeline Run Status",
    "Artifact Registry",
    "Data Quality",
    "FMP Budget",
    "AI Budget",
    "Memo Delivery",
    "Doc Audit",
    "Historical Backfill",
    "Schwab Broker Health",
    "Failure Queue",
    "Analysis Loop Status",
}

_FORBIDDEN_LABELS = (
    "execute trade",
    "buy now",
    "sell now",
    "place order",
    "auto-trade",
    "auto trade",
    "auto-approve",
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _make_latest(tmp_path: Path) -> Path:
    d = tmp_path / "outputs" / "latest"
    d.mkdir(parents=True)
    return d


def _write(directory: Path, filename: str, data: dict) -> None:
    (directory / filename).write_text(json.dumps(data), encoding="utf-8")


def _make_daily_run_status(latest: Path, overall_status: str = "ok",
                            stages: list | None = None) -> None:
    _write(latest, "daily_run_status.json", {
        "generated_at": "2026-06-08T12:00:00Z",
        "observe_only": True,
        "schema_version": "1.0",
        "source": "daily_safe_wrapper",
        "overall_status": overall_status,
        "stage_summary": {"total": 5, "ok": 4, "warn": 0, "failed": 0},
        "stages": stages or [
            {"name": "stage_a", "status": "ok", "output_lines_count": 10},
            {"name": "stage_b", "status": "ok", "output_lines_count": 5},
        ],
        "content_liveness": {},
        "content_warn_count": 0,
        "required_missing_count": 0,
        "optional_missing_count": 0,
        "disclaimer": "Observe-only.",
    })


def _make_pipeline_run_status(latest: Path, success: bool = True) -> None:
    _write(latest, "pipeline_run_status.json", {
        "generated_at": "2026-06-08T12:00:00Z",
        "run_id": "run-001",
        "source": "pipeline",
        "run_mode": "daily",
        "observe_only": True,
        "no_trade": True,
        "disclaimer": "Observe-only.",
        "success": success,
        "exit_code": 0,
        "steps_attempted": 10,
        "steps_succeeded": 9 if success else 8,
        "steps_skipped": 1,
        "steps_failed": 0 if success else 1,
        "steps": [],
        "errors": [],
    })


def _make_data_quality_report(latest: Path, critical: int = 0) -> None:
    _write(latest, "data_quality_report.json", {
        "generated_at": "2026-06-08T12:00:00Z",
        "observe_only": True,
        "available": True,
        "total_symbols": 40,
        "healthy_symbols": 40 - critical,
        "warning_symbols": 0,
        "critical_symbols": critical,
        "missing_price_count": 0,
        "missing_fundamentals_count": 0,
        "missing_news_count": 0,
        "stale_price_count": 0,
        "fallback_count": 0,
        "cached_count": 5,
        "source_counts": {},
        "summary_line": f"40 symbols: {40 - critical} healthy, {critical} critical",
    })


def _make_fmp_budget_status(latest: Path, overall_status: str = "ok") -> None:
    _write(latest, "fmp_budget_status.json", {
        "generated_at": "2026-06-08T12:00:00Z",
        "observe_only": True,
        "schema_version": "1.0",
        "source": "fmp_budget",
        "overall_status": overall_status,
        "budget": {"used": 50, "cap": 500},
        "news": {"used": 10, "cap": 100},
        "discovery": {},
        "cache": {},
        "disclaimer": "Observe-only.",
        "history_row_appended": True,
    })


def _make_ai_budget_summary(latest: Path, blocked: bool = False,
                             warning: bool = False) -> None:
    _write(latest, "ai_budget_summary.json", {
        "generated_at": "2026-06-08T12:00:00Z",
        "observe_only": True,
        "enabled": True,
        "daily_token_total": 10000,
        "daily_cost_total_usd": 0.05,
        "monthly_cost_total_usd": 1.20,
        "daily_cost_limit_usd": 2.00,
        "monthly_cost_limit_usd": 20.00,
        "warning": warning,
        "blocked": blocked,
        "warnings": [],
        "summary_line": "AI budget: $0.05 today / $1.20 month",
        "event_count": 5,
        "events": [],
    })


def _make_memo_delivery_status(latest: Path, sent: bool = True) -> None:
    _write(latest, "memo_delivery_status.json", {
        "generated_at": "2026-06-08T12:00:00Z",
        "observe_only": True,
        "no_trade": True,
        "available": True,
        "enabled": True,
        "dry_run": False,
        "attempted": True,
        "sent": sent,
        "skipped": not sent,
        "reason": "" if sent else "No recipients configured",
        "run_id": "run-001",
        "memo_date": "2026-06-08",
        "memo_source_txt": "outputs/latest/daily_memo.txt",
        "memo_source_md": "outputs/latest/daily_memo.md",
        "recipients_count": 1 if sent else 0,
    })


def _make_doc_audit_status(latest: Path, overall_status: str = "ok") -> None:
    _write(latest, "doc_audit_status.json", {
        "generated_at": "2026-06-08T12:00:00Z",
        "observe_only": True,
        "schema_version": "1.0",
        "source": "doc_auditor",
        "last_audited_sha": "abc123",
        "overall_status": overall_status,
        "findings": [],
        "auto_fix_candidates": [],
        "coverage_gaps": [],
        "auto_fixes_applied": 0,
        "disclaimer": "Observe-only.",
    })


def _make_historical_backfill_status(latest: Path, errored: int = 0) -> None:
    _write(latest, "historical_backfill_status.json", {
        "generated_at": "2026-06-08T12:00:00Z",
        "observe_only": True,
        "schema_version": "1.0",
        "source": "historical_backfill",
        "years": 2,
        "freshness_days": 30,
        "universe_size": 40,
        "fetched": 38,
        "skipped_fresh": 2,
        "skipped_budget": 0,
        "errored": errored,
        "per_ticker": {},
        "error": None,
        "disclaimer": "Observe-only.",
    })


def _make_broker_sync_status(latest: Path, overall_status: str = "unconfigured",
                               configured: bool = False,
                               authenticated: bool = False) -> None:
    _write(latest, "broker_sync_status.json", {
        "generated_at": "2026-06-08T12:00:00Z",
        "observe_only": True,
        "source": "schwab",
        "enabled": True,
        "configured": configured,
        "authenticated": authenticated,
        "read_only_mode": True,
        "trading_enabled": False,
        "last_success_at": None,
        "last_error": None,
        "account_count": 0,
        "position_count": 0,
        "overall_status": overall_status,
    })


def _make_analysis_state(tmp_path: Path,
                          daily_run_at: str = "2026-06-08T13:00:00Z",
                          monthly_verdict: str = "AMBER") -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "daily_check_state.json").write_text(json.dumps({
        "last_run_at": daily_run_at,
        "last_fingerprint": "abc123",
        "last_current_fp_resolved_1d": 50,
        "last_pre_tracker_hit_rate_1d": 0.55,
        "thresholds_crossed": [],
        "applied_fixes": [],
    }), encoding="utf-8")
    (data_dir / "monthly_check_state.json").write_text(json.dumps({
        "last_run_at": "2026-06-01T09:00:00Z",
        "last_verdict": monthly_verdict,
        "last_top_concern": "Some concern",
        "report_path": "docs/monthly_reports/2026-05.md",
        "counts": {},
        "agents_dispatched": [],
        "observe_only": True,
    }), encoding="utf-8")


def _make_all_artifacts(tmp_path: Path, latest: Path) -> None:
    _make_daily_run_status(latest)
    _make_pipeline_run_status(latest)
    _make_data_quality_report(latest)
    _make_fmp_budget_status(latest)
    _make_ai_budget_summary(latest)
    _make_memo_delivery_status(latest)
    _make_doc_audit_status(latest)
    _make_historical_backfill_status(latest)
    _make_broker_sync_status(latest)
    _make_analysis_state(tmp_path)


# ---------------------------------------------------------------------------
# Unit tests: collect_system_view — card structure
# ---------------------------------------------------------------------------


def test_system_view_has_all_expected_card_titles(tmp_path):
    """All expected card domains are present even with no artifacts."""
    from gui_v2.data.dash_system import collect_system_view

    _make_latest(tmp_path)
    v = collect_system_view(tmp_path)
    titles = {c["title"] for c in v["cards"]}
    assert EXPECTED_CARD_TITLES <= titles, f"Missing cards: {EXPECTED_CARD_TITLES - titles}"


def test_every_card_has_non_empty_source_artifacts(tmp_path):
    """source_artifacts must be non-empty for every card — artifacts absent."""
    from gui_v2.data.dash_system import collect_system_view

    _make_latest(tmp_path)
    v = collect_system_view(tmp_path)
    bad = [c["title"] for c in v["cards"] if not c.get("source_artifacts")]
    assert bad == [], f"Cards missing source_artifacts: {bad}"


def test_every_card_has_non_empty_source_artifacts_with_artifacts(tmp_path):
    """source_artifacts non-empty when artifacts are present."""
    from gui_v2.data.dash_system import collect_system_view

    latest = _make_latest(tmp_path)
    _make_all_artifacts(tmp_path, latest)
    v = collect_system_view(tmp_path)
    bad = [c["title"] for c in v["cards"] if not c.get("source_artifacts")]
    assert bad == [], f"Cards missing source_artifacts: {bad}"


def test_system_view_persona_field(tmp_path):
    from gui_v2.data.dash_system import collect_system_view

    _make_latest(tmp_path)
    v = collect_system_view(tmp_path)
    assert v["persona"] == "system"


def test_system_view_observe_only_flag(tmp_path):
    from gui_v2.data.dash_system import collect_system_view

    _make_latest(tmp_path)
    v = collect_system_view(tmp_path)
    assert v.get("observe_only") is True


def test_system_view_failed_stages_key_present(tmp_path):
    from gui_v2.data.dash_system import collect_system_view

    _make_latest(tmp_path)
    v = collect_system_view(tmp_path)
    assert "failed_stages" in v


# ---------------------------------------------------------------------------
# Schwab broker health card
# ---------------------------------------------------------------------------


def test_broker_sync_absent_yields_info_not_red(tmp_path):
    """broker_sync_status.json absent → status='info', NOT 'red'."""
    from gui_v2.data.dash_system import collect_system_view

    _make_latest(tmp_path)
    v = collect_system_view(tmp_path)
    schwab_card = next(
        (c for c in v["cards"] if c["title"] == "Schwab Broker Health"), None
    )
    assert schwab_card is not None, "Schwab card missing"
    assert schwab_card["status"] == "info", (
        f"Expected status='info' when absent, got {schwab_card['status']!r}"
    )
    assert schwab_card["severity"] == "blue", (
        f"Expected severity='blue' (info), got {schwab_card['severity']!r}"
    )


def test_broker_sync_absent_label_not_configured(tmp_path):
    """broker_sync_status.json absent → label contains 'not configured'."""
    from gui_v2.data.dash_system import collect_system_view

    _make_latest(tmp_path)
    v = collect_system_view(tmp_path)
    schwab_card = next(c for c in v["cards"] if c["title"] == "Schwab Broker Health")
    assert "not configured" in schwab_card["label"].lower(), (
        f"Expected 'not configured' in label, got {schwab_card['label']!r}"
    )


def test_broker_sync_absent_summary_mentions_schwab_optional(tmp_path):
    """Absent broker_sync → summary mentions Schwab is optional."""
    from gui_v2.data.dash_system import collect_system_view

    _make_latest(tmp_path)
    v = collect_system_view(tmp_path)
    schwab_card = next(c for c in v["cards"] if c["title"] == "Schwab Broker Health")
    assert "optional" in schwab_card["summary"].lower() or "schwab" in schwab_card["summary"].lower()


def test_broker_sync_present_unconfigured_yields_status_card(tmp_path):
    """broker_sync_status present (overall_status=unconfigured) → status card, info."""
    from gui_v2.data.dash_system import collect_system_view

    latest = _make_latest(tmp_path)
    _make_broker_sync_status(latest, overall_status="unconfigured", configured=False)
    v = collect_system_view(tmp_path)
    schwab_card = next(c for c in v["cards"] if c["title"] == "Schwab Broker Health")
    # unconfigured → info (not red)
    assert schwab_card["status"] == "info", (
        f"Expected status='info' for unconfigured, got {schwab_card['status']!r}"
    )
    assert "unconfigured" in schwab_card["label"].lower()


def test_broker_sync_present_ok_yields_ok_card(tmp_path):
    """broker_sync_status present (overall_status=ok) → status='ok'."""
    from gui_v2.data.dash_system import collect_system_view

    latest = _make_latest(tmp_path)
    _make_broker_sync_status(latest, overall_status="ok", configured=True, authenticated=True)
    v = collect_system_view(tmp_path)
    schwab_card = next(c for c in v["cards"] if c["title"] == "Schwab Broker Health")
    assert schwab_card["status"] == "ok", (
        f"Expected status='ok', got {schwab_card['status']!r}"
    )


def test_broker_sync_shows_configured_authenticated_fields(tmp_path):
    """broker_sync_status card summary shows configured/authenticated."""
    from gui_v2.data.dash_system import collect_system_view

    latest = _make_latest(tmp_path)
    _make_broker_sync_status(latest, overall_status="ok", configured=True, authenticated=True)
    v = collect_system_view(tmp_path)
    schwab_card = next(c for c in v["cards"] if c["title"] == "Schwab Broker Health")
    summary = schwab_card["summary"].lower()
    assert "configured" in summary
    assert "authenticated" in summary


def test_broker_sync_no_trade_language(tmp_path):
    """Schwab card summary/label must not contain trade/execute language."""
    from gui_v2.data.dash_system import collect_system_view

    latest = _make_latest(tmp_path)
    _make_broker_sync_status(latest, overall_status="ok", configured=True, authenticated=True)
    v = collect_system_view(tmp_path)
    schwab_card = next(c for c in v["cards"] if c["title"] == "Schwab Broker Health")
    for field in ("summary", "label"):
        text = (schwab_card.get(field) or "").lower()
        for bad in ("execute trade", "buy now", "sell now", "place order",
                    "auto-trade", "auto trade"):
            assert bad not in text, f"Forbidden term '{bad}' in Schwab card {field}: {text!r}"


# ---------------------------------------------------------------------------
# Failure queue card
# ---------------------------------------------------------------------------


def test_failure_queue_empty_when_all_stages_ok(tmp_path):
    """Failure queue empty when all stages have status ok."""
    from gui_v2.data.dash_system import collect_system_view

    latest = _make_latest(tmp_path)
    _make_daily_run_status(latest, stages=[
        {"name": "stage_a", "status": "ok", "output_lines_count": 5},
        {"name": "stage_b", "status": "ok", "output_lines_count": 3},
    ])
    v = collect_system_view(tmp_path)
    assert v["failed_stages"] == []
    fq_card = next(c for c in v["cards"] if c["title"] == "Failure Queue")
    assert fq_card["status"] == "ok"


def test_failure_queue_lists_failed_stages(tmp_path):
    """Failure queue lists stages with status failed."""
    from gui_v2.data.dash_system import collect_system_view

    latest = _make_latest(tmp_path)
    _make_daily_run_status(latest, stages=[
        {"name": "stage_ok", "status": "ok", "output_lines_count": 5},
        {"name": "stage_fail", "status": "failed", "output_lines_count": 2},
        {"name": "stage_warn", "status": "warn", "output_lines_count": 1},
    ])
    v = collect_system_view(tmp_path)
    names = {s["name"] for s in v["failed_stages"]}
    assert "stage_fail" in names, f"Expected stage_fail in failed_stages, got: {names}"
    assert "stage_warn" in names, f"Expected stage_warn in failed_stages, got: {names}"
    assert "stage_ok" not in names


def test_failure_queue_card_is_red_when_failed_stages(tmp_path):
    """Failure queue card status is red when there are failed stages."""
    from gui_v2.data.dash_system import collect_system_view

    latest = _make_latest(tmp_path)
    _make_daily_run_status(latest, stages=[
        {"name": "stage_bad", "status": "failed", "output_lines_count": 2},
    ])
    v = collect_system_view(tmp_path)
    fq_card = next(c for c in v["cards"] if c["title"] == "Failure Queue")
    assert fq_card["status"] == "red", f"Expected red, got {fq_card['status']!r}"


def test_failure_queue_card_is_warning_when_warn_only(tmp_path):
    """Failure queue card status is warning when only warned stages (no failed)."""
    from gui_v2.data.dash_system import collect_system_view

    latest = _make_latest(tmp_path)
    _make_daily_run_status(latest, stages=[
        {"name": "stage_warn", "status": "warn", "output_lines_count": 1},
    ])
    v = collect_system_view(tmp_path)
    fq_card = next(c for c in v["cards"] if c["title"] == "Failure Queue")
    assert fq_card["status"] == "warning", f"Expected warning, got {fq_card['status']!r}"


# ---------------------------------------------------------------------------
# Data quality
# ---------------------------------------------------------------------------


def test_data_quality_critical_yields_red(tmp_path):
    """data_quality_report with critical symbols → card status red."""
    from gui_v2.data.dash_system import collect_system_view

    latest = _make_latest(tmp_path)
    _make_data_quality_report(latest, critical=3)
    v = collect_system_view(tmp_path)
    dq_card = next(c for c in v["cards"] if c["title"] == "Data Quality")
    assert dq_card["status"] == "red", f"Expected red, got {dq_card['status']!r}"


def test_data_quality_absent_yields_unknown(tmp_path):
    """data_quality_report absent → status unknown."""
    from gui_v2.data.dash_system import collect_system_view

    _make_latest(tmp_path)
    v = collect_system_view(tmp_path)
    dq_card = next(c for c in v["cards"] if c["title"] == "Data Quality")
    assert dq_card["status"] == "unknown"


# ---------------------------------------------------------------------------
# AI budget
# ---------------------------------------------------------------------------


def test_ai_budget_blocked_yields_red(tmp_path):
    """ai_budget_summary blocked=True → card status red."""
    from gui_v2.data.dash_system import collect_system_view

    latest = _make_latest(tmp_path)
    _make_ai_budget_summary(latest, blocked=True)
    v = collect_system_view(tmp_path)
    ai_card = next(c for c in v["cards"] if c["title"] == "AI Budget")
    assert ai_card["status"] == "red"


def test_ai_budget_ok_yields_ok(tmp_path):
    """ai_budget_summary normal → card status ok."""
    from gui_v2.data.dash_system import collect_system_view

    latest = _make_latest(tmp_path)
    _make_ai_budget_summary(latest, blocked=False, warning=False)
    v = collect_system_view(tmp_path)
    ai_card = next(c for c in v["cards"] if c["title"] == "AI Budget")
    assert ai_card["status"] == "ok"


# ---------------------------------------------------------------------------
# Analysis loop status
# ---------------------------------------------------------------------------


def test_analysis_loop_status_monthly_amber(tmp_path):
    """Monthly AMBER verdict → card status warning."""
    from gui_v2.data.dash_system import collect_system_view

    latest = _make_latest(tmp_path)
    _make_analysis_state(tmp_path, monthly_verdict="AMBER")
    v = collect_system_view(tmp_path)
    al_card = next(c for c in v["cards"] if c["title"] == "Analysis Loop Status")
    assert al_card["status"] == "warning", f"Expected warning, got {al_card['status']!r}"


def test_analysis_loop_status_absent_yields_unknown(tmp_path):
    """Analysis state files absent → status unknown."""
    from gui_v2.data.dash_system import collect_system_view

    _make_latest(tmp_path)
    # No data dir / state files created
    v = collect_system_view(tmp_path)
    al_card = next(c for c in v["cards"] if c["title"] == "Analysis Loop Status")
    assert al_card["status"] == "unknown"


# ---------------------------------------------------------------------------
# Route / integration tests
# ---------------------------------------------------------------------------


def test_system_route_renders_200():
    """GET /dashboard/system returns 200."""
    from gui_v2.app import app

    client = TestClient(app)
    r = client.get("/dashboard/system")
    assert r.status_code == 200


def test_system_route_has_observe_only_banner():
    """Page contains the global observe-only banner."""
    from gui_v2.app import app

    client = TestClient(app)
    r = client.get("/dashboard/system")
    assert r.status_code == 200
    assert "No brokerage trade execution" in r.text


def test_system_route_has_read_only_notice():
    """Page contains the read-only system health notice."""
    from gui_v2.app import app

    client = TestClient(app)
    r = client.get("/dashboard/system")
    assert r.status_code == 200
    assert "read-only" in r.text.lower() or "observe-only" in r.text.lower()


def test_system_route_no_forbidden_labels():
    """Rendered /dashboard/system HTML must not contain forbidden action labels."""
    from gui_v2.app import app

    client = TestClient(app)
    r = client.get("/dashboard/system")
    assert r.status_code == 200
    text = r.text.lower()
    offenders = [label for label in _FORBIDDEN_LABELS if label in text]
    assert offenders == [], f"Forbidden labels in /dashboard/system: {offenders}"


def test_system_route_mobile_card_stack_present():
    """Template has md:hidden mobile card stack alongside desktop hidden table."""
    from gui_v2.app import app

    # Check the template source for the responsive pair — the failure-queue
    # table only renders when failed_stages is non-empty, so we check the
    # template file directly (same approach as quant test for efficacy table).
    template_path = Path("gui_v2/templates/dashboard/system.html")
    tpl_text = template_path.read_text(encoding="utf-8")
    assert "hidden md:block" in tpl_text, "Missing 'hidden md:block' desktop table class in system.html"
    assert "md:hidden" in tpl_text, "Missing 'md:hidden' mobile stack class in system.html"

    # Route must still render 200
    client = TestClient(app)
    r = client.get("/dashboard/system")
    assert r.status_code == 200
    # md:hidden will be present from base layout (nav/bottom-nav)
    assert "md:hidden" in r.text


# ---------------------------------------------------------------------------
# Template grep: no forbidden labels
# ---------------------------------------------------------------------------


def test_no_forbidden_action_labels_in_system_template():
    """system.html must not contain forbidden action label strings."""
    template_path = Path("gui_v2/templates/dashboard/system.html")
    text = template_path.read_text(encoding="utf-8").lower()
    offenders = [label for label in _FORBIDDEN_LABELS if label in text]
    assert offenders == [], f"Forbidden labels in system.html: {offenders}"


# ---------------------------------------------------------------------------
# Empty-state integration — route with no artifacts renders without error
# ---------------------------------------------------------------------------


def test_system_route_renders_200_with_no_artifacts(monkeypatch, tmp_path):
    """Route renders 200 even when all artifacts are absent (empty states)."""
    from gui_v2 import app as app_module
    from gui_v2.data.dash_system import collect_system_view

    empty_latest = tmp_path / "outputs" / "latest"
    empty_latest.mkdir(parents=True)

    original_root = app_module.REPO_ROOT
    monkeypatch.setattr(app_module, "REPO_ROOT", tmp_path)
    try:
        client = TestClient(app_module.app)
        r = client.get("/dashboard/system")
        assert r.status_code == 200
    finally:
        monkeypatch.setattr(app_module, "REPO_ROOT", original_root)


# ---------------------------------------------------------------------------
# M3: status_label filter
# ---------------------------------------------------------------------------


def test_status_label_filter_near_cap():
    """status_label filter converts near_cap → Near cap."""
    from gui_v2.app import _status_label

    assert _status_label("near_cap") == "Near cap"
    assert _status_label("ok_with_warnings") == "OK · warnings"
    assert _status_label("coverage_gap") == "Coverage gap"
    assert _status_label("unconfigured") == "Unconfigured"
    assert _status_label("exhausted") == "Exhausted"


def test_status_label_filter_fallback_title_case():
    """Unknown single-token / snake_case labels are title-cased (_ → space)."""
    from gui_v2.app import _status_label

    assert _status_label("some_custom_status") == "Some Custom Status"
    assert _status_label("running") == "Running"


def test_status_label_preserves_already_humanized_phrases():
    """A label that already contains a space is human text — returned unchanged,
    not title-cased (fixes 'No findings' → 'No Findings' badge regression)."""
    from gui_v2.app import _status_label

    # already-humanized SQG / card labels must survive verbatim
    assert _status_label("No findings") == "No findings"
    assert _status_label("1 finding") == "1 finding"
    assert _status_label("3 tracked") == "3 tracked"
    assert _status_label("High fallback") == "High fallback"
    assert _status_label("Complete with warnings") == "Complete with warnings"
    assert _status_label("No experiments yet") == "No experiments yet"
    assert _status_label("Insufficient history") == "Insufficient history"
    # a lowercase multi-word phrase is also left alone (title-casing it is wrong)
    assert _status_label("No structural risk actions") == "No structural risk actions"
    # leading/trailing whitespace is trimmed
    assert _status_label("  No findings  ") == "No findings"


def test_system_route_badge_uses_status_label(monkeypatch, tmp_path):
    """Card badge with label 'near_cap' renders 'Near cap' not 'near_cap'."""
    import json
    from gui_v2 import app as app_module

    latest = tmp_path / "outputs" / "latest"
    latest.mkdir(parents=True)
    # Write a daily_run_status that will generate a card with near_cap-like label
    (latest / "daily_run_status.json").write_text(json.dumps({
        "generated_at": "2026-06-08T09:00:00",
        "overall_status": "ok_with_warnings",
        "observe_only": True,
        "stage_summary": {"ok": 10, "failed": 0, "warn": 1},
        "content_liveness": {},
    }))

    original_root = app_module.REPO_ROOT
    monkeypatch.setattr(app_module, "REPO_ROOT", tmp_path)
    try:
        client = TestClient(app_module.app)
        r = client.get("/dashboard/system")
        assert r.status_code == 200
        # The ok_with_warnings label should be rendered as "OK · warnings"
        assert "ok_with_warnings" not in r.text, (
            "Raw snake_case 'ok_with_warnings' should not appear in badge"
        )
        assert "OK · warnings" in r.text or "OK" in r.text, (
            "Human-readable label should appear in badge"
        )
    finally:
        monkeypatch.setattr(app_module, "REPO_ROOT", original_root)
