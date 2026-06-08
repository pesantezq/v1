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


def test_required_artifacts_matches_legacy_expected():
    # The exact tuples daily_run_status historically used (order matters).
    legacy = [
        ("outputs/latest/decision_plan.json", "decision plan", True),
        ("outputs/latest/decision_plan.md", "decision plan (md)", True),
        ("outputs/latest/system_decision_summary.json", "system decision summary", True),
        ("outputs/latest/daily_memo.md", "daily memo (md)", True),
        ("outputs/latest/daily_memo.txt", "daily memo (txt)", True),
        ("outputs/latest/news_intelligence.json", "news intelligence", True),
        ("outputs/latest/risk_delta.json", "risk delta panel", True),
        ("outputs/portfolio/portfolio_snapshot.json", "portfolio snapshot", True),
        ("outputs/performance/approved_ranking_config.json", "approved ranking config", False),
        ("outputs/performance/approved_allocation_policy.json", "approved allocation policy", False),
        ("outputs/latest/theme_opportunities.json", "theme opportunities", False),
    ]
    assert ar.required_artifacts() == legacy


def test_max_age_hours_by_cadence():
    assert ar.max_age_hours({"cadence": "daily"}) == 30
    assert ar.max_age_hours({"cadence": "weekly"}) == 192
    assert ar.max_age_hours({"cadence": "monthly"}) == 768
    assert ar.max_age_hours({"cadence": "weekend"}) == 100
    assert ar.max_age_hours({"cadence": "yearly"}) == 9000
    assert ar.max_age_hours({"cadence": "on_demand"}) is None
    # override wins
    assert ar.max_age_hours({"cadence": "daily", "staleness_hours_override": 50}) == 50


def test_is_stale_respects_cadence():
    # weekly artifact 40h old is NOT stale; daily artifact 51h old IS stale
    assert ar.is_stale({"cadence": "weekly"}, age_hours=40) is False
    assert ar.is_stale({"cadence": "daily"}, age_hours=51) is True
    assert ar.is_stale({"cadence": "on_demand"}, age_hours=10_000) is False


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


# ---------------------------------------------------------------------------
# Task 5: validate_registry — classification + severity rollup
# ---------------------------------------------------------------------------

import json as _json


def _mini_registry():
    return {"schema_version": 1, "daily_run_status_tracked": [],
            "artifacts": {
                "sot.json": {"path": "outputs/latest/sot.json", "label": "sot",
                    "lens": "decision_core", "role": "source_of_truth", "required": True,
                    "cadence": "daily", "producer": "p", "consumers": ["daily-tool-analysis"],
                    "severity_if_missing": "critical"},
                "probe.json": {"path": "outputs/latest/probe.json", "label": "probe",
                    "lens": "quant_learning", "role": "probe", "required": True,
                    "cadence": "daily", "producer": "p", "consumers": ["UNATTRIBUTED"],
                    "severity_if_missing": "warning"},
            }}


def _write(p, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_json.dumps(obj), encoding="utf-8")


def test_validate_red_when_critical_missing(tmp_path):
    reg = _mini_registry()
    # only the probe exists, fresh; sot (critical) missing
    _write(tmp_path / "outputs/latest/probe.json", {"x": 1})
    now = ar.datetime.now(ar.timezone.utc)
    st = ar.validate_registry(reg, tmp_path, now)
    assert st["overall_status"] == "red"
    assert "sot.json" in st["missing"]
    assert "probe.json" in st["unattributed"]


def test_validate_green_when_all_present_fresh(tmp_path):
    reg = _mini_registry()
    reg["artifacts"]["probe.json"]["consumers"] = ["daily-tool-analysis"]  # attributed
    _write(tmp_path / "outputs/latest/sot.json", {"x": 1})
    _write(tmp_path / "outputs/latest/probe.json", {"x": 1})
    now = ar.datetime.now(ar.timezone.utc)
    st = ar.validate_registry(reg, tmp_path, now)
    assert st["overall_status"] == "green"
    assert st["counts"]["present"] == 2


def test_validate_amber_when_warning_stale(tmp_path):
    reg = _mini_registry()
    reg["artifacts"]["probe.json"]["consumers"] = ["daily-tool-analysis"]
    _write(tmp_path / "outputs/latest/sot.json", {"x": 1})
    pf = tmp_path / "outputs/latest/probe.json"
    _write(pf, {"x": 1})
    import os
    old = (ar.datetime.now(ar.timezone.utc).timestamp()) - 60 * 3600  # 60h old
    os.utime(pf, (old, old))
    now = ar.datetime.now(ar.timezone.utc)
    st = ar.validate_registry(reg, tmp_path, now)
    assert st["overall_status"] == "amber"
    assert any(s["artifact"] == "probe.json" for s in st["stale"])


def test_validate_invalid_json_listed(tmp_path):
    reg = _mini_registry()
    reg["artifacts"]["probe.json"]["consumers"] = ["daily-tool-analysis"]
    reg["artifacts"]["probe.json"]["severity_if_missing"] = "info"
    _write(tmp_path / "outputs/latest/sot.json", {"x": 1})
    bad = tmp_path / "outputs/latest/probe.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("{not json", encoding="utf-8")
    now = ar.datetime.now(ar.timezone.utc)
    st = ar.validate_registry(reg, tmp_path, now)
    assert "probe.json" in st["invalid_json"]


def test_validate_flags_schema_invalid_row(tmp_path):
    reg = _mini_registry()
    reg["artifacts"]["bad.json"] = {"path": "outputs/latest/bad.json", "lens": "nope",
        "role": "probe", "required": False, "cadence": "daily", "producer": "p",
        "consumers": ["x"], "severity_if_missing": "info"}
    now = ar.datetime.now(ar.timezone.utc)
    st = ar.validate_registry(reg, tmp_path, now)
    assert "bad.json" in st["schema_invalid"]


# ---------------------------------------------------------------------------
# Task 6: run_artifact_registry orchestrator + status write
# ---------------------------------------------------------------------------


def test_run_writes_status_and_never_raises(tmp_path):
    # point the orchestrator at the SHIPPED registry but a tmp artifacts root
    st = ar.run_artifact_registry(root=tmp_path, write_files=True)
    assert st["observe_only"] is True
    assert st["source"] == "artifact_registry"
    # most artifacts absent under tmp root → status produced, no raise
    out = tmp_path / "outputs/latest/artifact_registry_status.json"
    assert out.exists()
    written = _json.loads(out.read_text())
    assert written["overall_status"] in ("green", "amber", "red")


def test_run_degrades_on_bad_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(ar, "load_registry", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    st = ar.run_artifact_registry(root=tmp_path, write_files=False)
    assert st["observe_only"] is True
    assert st["overall_status"] == "green"  # degraded-but-valid
