"""Tests for portfolio_automation/artifact_registry.py (Tasks 1 & 2)."""
from pathlib import Path

from portfolio_automation import artifact_registry as ar


def test_load_registry_missing_returns_empty(tmp_path):
    assert ar.load_registry(tmp_path / "nope.yaml") == {}


def test_load_registry_corrupt_returns_empty(tmp_path):
    p = tmp_path / "r.yaml"
    p.write_text(":\n  - [unbalanced", encoding="utf-8")
    assert ar.load_registry(p) == {}


def test_load_registry_parses_minimal(tmp_path):
    p = tmp_path / "r.yaml"
    p.write_text(
        "schema_version: 1\n"
        "daily_run_status_tracked: [a.json]\n"
        "artifacts:\n"
        "  a.json:\n"
        "    path: outputs/latest/a.json\n"
        "    label: A\n"
        "    lens: developer\n"
        "    role: telemetry\n"
        "    required: true\n"
        "    cadence: daily\n"
        "    producer: prod_a\n"
        "    consumers: [daily-tool-analysis]\n"
        "    severity_if_missing: warning\n",
        encoding="utf-8",
    )
    reg = ar.load_registry(p)
    assert reg["schema_version"] == 1
    assert reg["daily_run_status_tracked"] == ["a.json"]
    assert reg["artifacts"]["a.json"]["lens"] == "developer"


def test_shipped_registry_schema_valid():
    reg = ar.load_registry()  # the real artifact_registry.yaml
    assert reg, "registry failed to load"
    arts = reg["artifacts"]
    # every tracked key exists in artifacts
    for key in reg["daily_run_status_tracked"]:
        assert key in arts, f"tracked key missing from artifacts: {key}"
    # every row has the 7 required fields with in-enum values
    bad = ar.schema_errors(reg)
    assert bad == [], f"schema errors: {bad}"
    # coverage: all 46 outputs/latest json names cataloged
    expected_latest = {
        "ai_budget_summary", "ai_decision_validation", "alpha_attribution_report",
        "cash_deployment_plan", "confidence_calibration", "correlation_risk_advisor",
        "daily_run_status", "data_quality_report", "decision_explanations", "decision_plan",
        "decisions_due_for_resolution", "decision_triage", "discovery_pulse_status",
        "doc_audit_status", "earnings_gate", "exit_advisor", "fmp_budget_status",
        "gate_retune_suggestions", "historical_backfill_status", "kelly_sizing_advisor",
        "market_narrative_daily", "market_narrative_monthly", "market_narrative_weekly",
        "market_opportunities", "memo_delivery_status", "news_evidence_layer",
        "news_intelligence", "pattern_efficacy_monthly", "pattern_efficacy_weekly",
        "pattern_efficacy_yearly", "pipeline_run_status", "quant_watch_status",
        "retune_impact", "risk_delta", "scraped_intel_comparison", "scraped_intel_run_summary",
        "system_decision_summary", "tax_harvest_advisor", "theme_engine_llm_metadata",
        "theme_signals", "top100_daily", "top100_monthly", "top100_weekly", "vol_regime_advisor",
        "watch_candidates", "watchlist_signals",
    }
    cataloged = {k[:-5] for k in arts if k.endswith(".json")}
    missing = expected_latest - cataloged
    assert missing == set(), f"uncataloged outputs/latest artifacts: {missing}"
