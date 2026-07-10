"""Tests for portfolio_automation/artifact_registry.py (Tasks 1 & 2)."""
import json as _json
import os
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


def _mini_registry():
    return {"schema_version": 1, "daily_run_status_tracked": [],
            "artifacts": {
                "sot.json": {"path": "outputs/latest/sot.json", "label": "sot",
                    "lens": "decision_core", "role": "source_of_truth", "required": True,
                    "cadence": "daily", "producer": "p", "consumers": ["daily-tool-analysis"],
                    "severity_if_missing": "critical", "consumer_status": "consumed"},
                "probe.json": {"path": "outputs/latest/probe.json", "label": "probe",
                    "lens": "quant_learning", "role": "probe", "required": True,
                    "cadence": "daily", "producer": "p", "consumers": ["UNATTRIBUTED"],
                    "severity_if_missing": "warning", "consumer_status": "consumed"},
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
    # probe.json has consumer_status "consumed" with a non-empty consumers list →
    # classified (not unjustified debt), even though the consumer is the UNATTRIBUTED sentinel
    assert "probe.json" not in st["unjustified_debt"]


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
    assert st["overall_status"] == "green"  # severity_if_missing=info → no status escalation


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
    assert st["overall_status"] == "amber"


def test_daily_run_status_tracks_same_artifacts_via_registry():
    import json as J
    from pathlib import Path
    from portfolio_automation import daily_run_status as d
    golden = J.loads(Path("tests/fixtures/daily_run_status_golden.json").read_text())
    rows = d.scan_expected_artifacts(Path("."))
    got = [{"path": r["path"], "label": r["label"], "required": r["required"]} for r in rows]
    assert got == golden


# ---------------------------------------------------------------------------
# Task 1: consumer_status schema validation
# ---------------------------------------------------------------------------


def _row(**over):
    base = {"path": "outputs/latest/x.json", "label": "x", "lens": "developer",
            "role": "telemetry", "required": False, "cadence": "daily",
            "producer": "p", "consumers": ["daily-tool-analysis"],
            "severity_if_missing": "info", "consumer_status": "consumed"}
    base.update(over)
    return base


def test_schema_errors_flags_missing_consumer_status():
    reg = {"artifacts": {"a.json": _row(consumer_status=None)}, "daily_run_status_tracked": []}
    del reg["artifacts"]["a.json"]["consumer_status"]
    errs = ar.schema_errors(reg)
    assert any("consumer_status" in e for e in errs)


def test_schema_errors_flags_bad_consumer_status():
    reg = {"artifacts": {"a.json": _row(consumer_status="nope")}, "daily_run_status_tracked": []}
    assert any("consumer_status" in e for e in ar.schema_errors(reg))


def test_schema_errors_flags_consumed_with_empty_consumers():
    reg = {"artifacts": {"a.json": _row(consumer_status="consumed", consumers=[])},
           "daily_run_status_tracked": []}
    assert any("consumed" in e and "consumers" in e for e in ar.schema_errors(reg))


def test_schema_errors_allows_diagnostic_only_with_empty_consumers():
    reg = {"artifacts": {"a.json": _row(consumer_status="diagnostic_only", consumers=[])},
           "daily_run_status_tracked": []}
    assert ar.schema_errors(reg) == []


def test_critical_severity_iff_source_of_truth():
    # The daily-tool-analysis governance gate derives "source_of_truth degraded"
    # from overall_status==red, which is only valid while critical severity is
    # used by, and only by, source_of_truth rows. Guard that biconditional.
    reg = ar.load_registry()
    arts = reg["artifacts"]
    source_of_truth = {k for k, r in arts.items() if r.get("role") == "source_of_truth"}
    critical = {k for k, r in arts.items() if r.get("severity_if_missing") == "critical"}
    assert source_of_truth == critical, (
        f"critical⟺source_of_truth broken: "
        f"sot-not-critical={source_of_truth - critical}, "
        f"critical-not-sot={critical - source_of_truth}")


# ---------------------------------------------------------------------------
# Task 2: 100% classified, UNATTRIBUTED removed, consumed rows non-empty
# ---------------------------------------------------------------------------


def test_every_row_has_valid_consumer_status():
    reg = ar.load_registry()
    bad = {k: r.get("consumer_status") for k, r in reg["artifacts"].items()
           if r.get("consumer_status") not in ar.CONSUMER_STATUSES}
    assert bad == {}, f"rows missing/invalid consumer_status: {bad}"


def test_no_legacy_sentinel_in_consumers():
    reg = ar.load_registry()
    leftover = [k for k, r in reg["artifacts"].items()
                if "UNATTRIBUTED" in (r.get("consumers") or [])]
    assert leftover == [], f"UNATTRIBUTED sentinel still present: {leftover}"


def test_consumed_rows_have_real_consumers():
    reg = ar.load_registry()
    bad = [k for k, r in reg["artifacts"].items()
           if r.get("consumer_status") == "consumed"
           and not (isinstance(r.get("consumers"), list) and r.get("consumers"))]
    assert bad == [], f"consumed rows with empty consumers: {bad}"


# ---------------------------------------------------------------------------
# Task 3: validate_registry debt fields
# ---------------------------------------------------------------------------


def _debt_registry():
    def r(status, consumers, sev="info"):
        return {"path": f"outputs/latest/{status}.json", "label": status,
                "lens": "developer", "role": "telemetry", "required": False,
                "cadence": "daily", "producer": "p", "consumers": consumers,
                "severity_if_missing": sev, "consumer_status": status}
    return {"daily_run_status_tracked": [], "artifacts": {
        "consumed.json": r("consumed", ["daily-tool-analysis"]),
        "diagnostic_only.json": r("diagnostic_only", []),
        "archive_only.json": r("archive_only", []),
        "deprecated_candidate.json": r("deprecated_candidate", []),
    }}


def test_validate_reports_debt_fields(tmp_path):
    # make all four present + fresh so presence rules don't interfere
    import json as J
    for name in ["consumed", "diagnostic_only", "archive_only", "deprecated_candidate"]:
        p = tmp_path / "outputs/latest" / f"{name}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(J.dumps({"x": 1}), encoding="utf-8")
    st = ar.validate_registry(_debt_registry(), tmp_path, ar.datetime.now(ar.timezone.utc))
    assert st["classified"] == 4
    assert st["counts"]["total"] == 4
    assert set(st["unjustified_debt"]) == {"deprecated_candidate.json"}
    assert st["justified_no_consumer"] == 2  # diagnostic_only + archive_only
    assert st["by_consumer_status"] == {"consumed": 1, "diagnostic_only": 1,
                                        "archive_only": 1, "deprecated_candidate": 1}
    assert st["debt_target_met"] is False  # one deprecated_candidate


def test_validate_debt_does_not_change_overall_status(tmp_path):
    import json as J
    for name in ["consumed", "diagnostic_only", "archive_only", "deprecated_candidate"]:
        p = tmp_path / "outputs/latest" / f"{name}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(J.dumps({"x": 1}), encoding="utf-8")
    st = ar.validate_registry(_debt_registry(), tmp_path, ar.datetime.now(ar.timezone.utc))
    # all present+fresh, only info severity → debt must NOT make it red/amber
    assert st["overall_status"] == "green"


def test_validate_consumed_empty_is_unjustified(tmp_path):
    reg = _debt_registry()
    reg["artifacts"]["consumed.json"]["consumers"] = []  # invariant violation at runtime
    st = ar.validate_registry(reg, tmp_path, ar.datetime.now(ar.timezone.utc))
    assert "consumed.json" in st["unjustified_debt"]


# ---------------------------------------------------------------------------
# Task 4: Live debt sanity + invariant on the shipped registry
# ---------------------------------------------------------------------------


def test_shipped_registry_meets_debt_target():
    # The shipped registry must be 100% classified with zero unjustified debt.
    from pathlib import Path
    st = ar.run_artifact_registry(root=".", write_files=False)
    assert st["classified"] == st["counts"]["total"], "not every row is classified"
    assert st["unjustified_debt"] == [], f"unjustified debt present: {st['unjustified_debt']}"
    assert st["debt_target_met"] is True


# ---------------------------------------------------------------------------
# Task 5: Validator immutability guard
# ---------------------------------------------------------------------------


def test_run_does_not_mutate_the_registry_contract(tmp_path):
    import hashlib
    from pathlib import Path
    reg_path = Path(ar.DEFAULT_REGISTRY_PATH)
    before = hashlib.sha256(reg_path.read_bytes()).hexdigest()
    ar.run_artifact_registry(root=tmp_path, write_files=True)  # full run incl. status write
    after = hashlib.sha256(reg_path.read_bytes()).hexdigest()
    assert before == after, "run_artifact_registry must never modify artifact_registry.yaml"


# ---------------------------------------------------------------------------
# Task 7: Proof-wire — correlation_risk_advisor + pattern_efficacy_weekly
# ---------------------------------------------------------------------------

import re as _re
from pathlib import Path as _Path


def test_proof_wired_artifacts_are_referenced_by_their_consumer():
    reg = ar.load_registry()
    for art in ("pattern_efficacy_weekly.json", "correlation_risk_advisor.json"):
        row = reg["artifacts"][art]
        assert row["consumer_status"] == "consumed", \
            f"{art} should be consumed but is {row['consumer_status']!r}"
        assert row["consumers"], f"{art} consumed but no consumers listed"
        # every listed skill/agent consumer file must actually reference the artifact
        for c in row["consumers"]:
            hits = list(_Path(".claude").rglob(f"{c}.md"))
            assert hits, f"consumer file {c}.md not found for {art}"
            assert any(art in h.read_text(encoding="utf-8") for h in hits), \
                f"{c}.md does not reference {art}"


# ---------------------------------------------------------------------------
# Broker-sync (Schwab read-only) registry coverage + daily-health pairing
# (added 2026-06-09 — Track B: register the 5 broker artifacts and wire the
#  broker_sync_status health check into daily-tool-analysis.)
# ---------------------------------------------------------------------------

_BROKER_ARTIFACTS = {
    "broker_sync_status.json": ("developer", "telemetry"),
    "schwab_portfolio_snapshot.json": ("risk_action", "advisor"),
    "schwab_positions.json": ("risk_action", "advisor"),
    "portfolio_reconciliation.json": ("risk_action", "advisor"),
    "portfolio_config_update_proposal.json": ("risk_action", "advisor"),
}


def test_broker_artifacts_registered_and_schema_valid():
    reg = ar.load_registry()
    arts = reg["artifacts"]
    for key, (lens, role) in _BROKER_ARTIFACTS.items():
        assert key in arts, f"broker artifact not registered: {key}"
        row = arts[key]
        assert row["lens"] == lens
        assert row["role"] == role
        # broker_sync_status is refreshed by the daily cron stage (always-producible);
        # the 4 advisor artifacts only populate post-auth and stay on_demand so they
        # never manufacture false-stale flags while unconfigured (2026-06-12 activation).
        expected_cadence = "daily" if key == "broker_sync_status.json" else "on_demand"
        assert row["cadence"] == expected_cadence
        assert row["required"] is False
        assert row["severity_if_missing"] == "info"  # absence must never escalate
        assert ar._row_schema_ok(row), f"schema invalid row: {key}"
    # the whole shipped registry still has zero schema errors after the additions
    assert ar.schema_errors(reg) == []


def test_broker_sync_status_paired_with_daily_health_check():
    # CLAUDE.md Analysis+Health Coverage Requirement: a shipped producer must have
    # at least one consumer at the appropriate cadence — here the daily skill.
    reg = ar.load_registry()
    row = reg["artifacts"]["broker_sync_status.json"]
    assert row["consumer_status"] == "consumed"
    assert "daily-tool-analysis" in row["consumers"], \
        "broker_sync_status must be consumed by the daily-tool-analysis health check"


def test_broker_artifacts_never_unjustified_debt(tmp_path):
    # Healthy state: broker_sync_status present → all 5 rows classified 'consumed',
    # zero unjustified debt, zero schema-invalid.
    import datetime
    reg = ar.load_registry()
    (tmp_path / "outputs" / "latest").mkdir(parents=True)
    (tmp_path / "outputs" / "latest" / "broker_sync_status.json").write_text(
        _json.dumps({"overall_status": "unconfigured", "observe_only": True}))
    now = datetime.datetime.now(datetime.timezone.utc)
    st = ar.validate_registry(reg, tmp_path, now)
    for key in _BROKER_ARTIFACTS:
        assert key not in st["unjustified_debt"], f"{key} flagged as unjustified debt"
        assert key not in st["schema_invalid"], f"{key} flagged schema-invalid"


def test_broker_post_sync_absence_is_info_not_escalating(tmp_path):
    # Degraded/uncredentialed state: the 4 post-sync artifacts are absent until a
    # live --sync/--reconcile runs. Their absence must read info-missing only and
    # NOT push governance to AMBER/RED. Isolate the broker rows so the assertion is
    # purely about them.
    import datetime
    reg_full = ar.load_registry()
    broker_only = {
        "artifacts": {k: reg_full["artifacts"][k] for k in _BROKER_ARTIFACTS},
        "daily_run_status_tracked": [],
    }
    (tmp_path / "outputs" / "latest").mkdir(parents=True)
    # only the always-producible status artifact exists
    (tmp_path / "outputs" / "latest" / "broker_sync_status.json").write_text(
        _json.dumps({"overall_status": "unconfigured"}))
    now = datetime.datetime.now(datetime.timezone.utc)
    st = ar.validate_registry(broker_only, tmp_path, now)
    assert st["overall_status"] == ar.GREEN          # info-missing never escalates
    assert st["counts"]["missing"] == 4              # the 4 post-sync artifacts
    assert st["counts"]["missing_required"] == 0


# ---------------------------------------------------------------------------
# Next-stage lane (Phases 1-15) — daily-tool-analysis per-phase dispatch wiring
# (added 2026-06-10: the activated next-stage producers are now consumed by the
#  daily check's Step 4 line 6h; guard the consumer wiring + skill reference so
#  it can't silently revert to producer-without-consumer debt.)
# ---------------------------------------------------------------------------

# artifact -> the daily skill must consume it and reference its filename.
_NEXT_STAGE_DAILY_CONSUMED = (
    "opportunity_radar.json",
    "opportunity_approval_queue.json",
    "strategy_comparison.json",
    "shadow_opportunity_tracking.json",
    "broker_aware_portfolio.json",
    "system_improvement_ideas.json",
)


def test_next_stage_artifacts_consumed_by_daily_tool_analysis():
    reg = ar.load_registry()
    for art in _NEXT_STAGE_DAILY_CONSUMED:
        row = reg["artifacts"][art]
        assert row["consumer_status"] == "consumed", \
            f"{art} should be consumed by the daily check but is {row['consumer_status']!r}"
        assert "daily-tool-analysis" in row["consumers"], \
            f"{art} must list daily-tool-analysis as a consumer"
        # absence of a next-stage advisory must never escalate governance
        assert row["severity_if_missing"] == "info", \
            f"{art} severity_if_missing must be info (observe-only side-panel)"


def test_next_stage_dispatch_lines_present_in_daily_skill():
    skill = _Path(".claude/commands/daily-tool-analysis.md")
    assert skill.exists(), "daily-tool-analysis skill file missing"
    text = skill.read_text(encoding="utf-8")
    # the per-phase heartbeat line (Step 4 item 6h) and its silent-zero dispatch
    assert "6h." in text and "Next-stage lane" in text, \
        "Step 4 line 6h (Next-stage lane heartbeat) missing from daily skill"
    assert "next_stage_radar_candidates" in text, \
        "next-stage radar silent-zero signal missing from daily skill"
    # every artifact the registry says daily-tool-analysis consumes must be named
    for art in _NEXT_STAGE_DAILY_CONSUMED:
        assert art in text, f"daily skill does not reference consumed artifact {art}"


# ---------------------------------------------------------------------------
# SQG program (simulation / quant-feedback / governance loop) registry coverage
# (added 2026-07-01 — register the 8 SQG producer artifacts; proposal_evidence
#  ships as pure helpers with no standalone artifact so it is intentionally not
#  registered. Cadence per docs/SQG_CADENCE_INTEGRATION.md.)
# ---------------------------------------------------------------------------

# key -> (lens, role, cadence, consumer_status)
_SQG_ARTIFACTS = {
    "run_manifest.json":              ("meta_governance", "telemetry", "daily",     "consumed"),
    "daily_input_snapshot.json":      ("meta_governance", "telemetry", "daily",     "consumed"),
    "decision_context_log.jsonl":     ("quant_learning",  "telemetry", "daily",     "consumed"),
    "quant_feedback.json":            ("quant_learning",  "probe",     "daily",     "consumed"),
    "semantic_liveness_status.json":  ("meta_governance", "probe",     "daily",     "consumed"),
    "scenario_risk.json":             ("risk_action",     "advisor",   "daily",     "consumed"),
    "experiment_registry.json":       ("quant_learning",  "telemetry", "on_demand", "consumed"),
    "strategy_mandates.json":         ("quant_learning",  "advisor",   "weekly",    "consumed"),
}

# SQG artifacts whose consumer is a cadence-analysis skill (.claude/commands/*.md).
# Each listed skill file must actually reference the artifact (no phantom wiring).
_SQG_SKILL_CONSUMED = {
    "quant_feedback.json":       ["monthly-tool-analysis", "yearly-tool-analysis"],
    "experiment_registry.json":  ["monthly-tool-analysis", "yearly-tool-analysis"],
    "strategy_mandates.json":    ["monthly-tool-analysis", "yearly-tool-analysis"],
}

# The SQG artifacts whose real, current consumer is the daily_run_status module
# (it reads/surfaces each — see portfolio_automation/daily_run_status.py).
_SQG_DAILY_RUN_STATUS_CONSUMED = {
    "run_manifest.json", "daily_input_snapshot.json", "quant_feedback.json",
    "semantic_liveness_status.json", "scenario_risk.json",
}


def test_sqg_artifacts_registered_and_schema_valid():
    reg = ar.load_registry()
    arts = reg["artifacts"]
    for key, (lens, role, cadence, cs) in _SQG_ARTIFACTS.items():
        assert key in arts, f"SQG artifact not registered: {key}"
        row = arts[key]
        assert row["lens"] == lens, f"{key}: lens {row['lens']!r} != {lens!r}"
        assert row["role"] == role, f"{key}: role {row['role']!r} != {role!r}"
        assert row["cadence"] == cadence, f"{key}: cadence {row['cadence']!r} != {cadence!r}"
        assert row["consumer_status"] == cs, f"{key}: consumer_status {row['consumer_status']!r} != {cs!r}"
        # observe-only additive artifacts: never required, absence never escalates
        assert row["required"] is False, f"{key}: must not be required"
        assert row["severity_if_missing"] == "info", f"{key}: severity must be info"
        assert ar._row_schema_ok(row), f"schema invalid row: {key}"
    # whole shipped registry still schema-clean after the additions
    assert ar.schema_errors(reg) == []


def test_proposal_evidence_not_registered_as_artifact():
    # proposal_evidence.py is pure helpers (evidence cards embedded in proposals);
    # it writes no standalone artifact, so registering one would manufacture a
    # permanently-missing row. Guard against a well-meaning future addition.
    reg = ar.load_registry()
    assert "proposal_evidence.json" not in reg["artifacts"]


def test_sqg_daily_consumers_actually_read_the_artifact():
    # Every SQG row that claims daily_run_status as a consumer must be genuinely
    # referenced by portfolio_automation/daily_run_status.py — no phantom links.
    reg = ar.load_registry()
    drs = _Path("portfolio_automation/daily_run_status.py").read_text(encoding="utf-8")
    for key in _SQG_DAILY_RUN_STATUS_CONSUMED:
        row = reg["artifacts"][key]
        assert row["consumer_status"] == "consumed", f"{key} should be consumed"
        assert "daily_run_status" in row["consumers"], \
            f"{key} must list daily_run_status as a consumer"
        # daily_run_status references the artifact by filename or by its stem
        stem = key.rsplit(".", 1)[0]
        assert key in drs or stem in drs, \
            f"daily_run_status.py does not reference consumed artifact {key}"


def test_sqg_skill_consumers_reference_the_artifact():
    # The monthly/yearly analysis skills that the registry lists as consumers of
    # the SQG research artifacts must actually name each artifact (Analysis+Health
    # Coverage Requirement — every artifact needs a real consumer at its cadence).
    reg = ar.load_registry()
    for key, skills in _SQG_SKILL_CONSUMED.items():
        row = reg["artifacts"][key]
        assert row["consumer_status"] == "consumed", f"{key} should be consumed"
        for skill in skills:
            assert skill in row["consumers"], f"{key} must list {skill} as a consumer"
            md = _Path(f".claude/commands/{skill}.md")
            assert md.exists(), f"skill file missing: {md}"
            assert key in md.read_text(encoding="utf-8"), \
                f"{skill}.md does not reference consumed artifact {key}"


def test_decision_context_log_consumed_by_quant_feedback():
    reg = ar.load_registry()
    row = reg["artifacts"]["decision_context_log.jsonl"]
    assert row["consumers"] == ["quant_feedback"]
    qf = _Path("portfolio_automation/quant_feedback.py").read_text(encoding="utf-8")
    assert "decision_context_log" in qf, \
        "quant_feedback.py must read decision_context_log.jsonl to justify the consumer link"


def test_sqg_registration_keeps_registry_green_and_debt_free():
    # Healthy live corpus: adding the 8 rows must not introduce a required-miss,
    # must keep 100% classification, and must not create unjustified debt.
    st = ar.run_artifact_registry(root=".", write_files=False)
    assert st["overall_status"] == "green"
    assert st["counts"]["missing_required"] == 0
    assert st["classified"] == st["counts"]["total"]
    assert st["debt_target_met"] is True


def test_sqg_missing_on_demand_ledger_does_not_escalate(tmp_path):
    # Degraded fixture: an on_demand SQG ledger (experiment_registry) is absent.
    # It must be reported missing but, at severity=info, must NOT escalate the
    # overall status past green — the steady state before the first experiment.
    reg = {"artifacts": {
        "experiment_registry.json": {
            "path": "outputs/sandbox/experiment_registry.json",
            "lens": "quant_learning", "role": "telemetry", "required": False,
            "cadence": "on_demand", "producer": "experiment_registry",
            "consumers": [], "severity_if_missing": "info",
            "consumer_status": "diagnostic_only",
        },
    }}
    st = ar.validate_registry(reg, tmp_path, ar.datetime.now(ar.timezone.utc))
    assert "experiment_registry.json" in st["missing"]
    assert st["counts"]["missing_required"] == 0
    assert st["overall_status"] == "green"           # info-missing never escalates
    assert st["justified_no_consumer"] == 1           # diagnostic_only is justified
    assert st["unjustified_debt"] == []


def test_sqg_daily_artifact_goes_stale_when_pipeline_stops(tmp_path):
    # Degraded fixture: a daily SQG artifact exists but the cron stopped refreshing
    # it. Past the 30h daily window it must flag stale (so the wiring probe / daily
    # check can surface a silently-stopped Stage). At severity=info it stays green.
    root = tmp_path
    p = root / "outputs" / "latest" / "quant_feedback.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"evidence_status": "ok"}', encoding="utf-8")
    old = os.stat(p).st_mtime - 40 * 3600  # 40h ago > 30h daily window
    os.utime(p, (old, old))
    reg = {"artifacts": {
        "quant_feedback.json": {
            "path": "outputs/latest/quant_feedback.json",
            "lens": "quant_learning", "role": "probe", "required": False,
            "cadence": "daily", "producer": "quant_feedback",
            "consumers": ["daily_run_status"], "severity_if_missing": "info",
            "consumer_status": "consumed",
        },
    }}
    st = ar.validate_registry(reg, root, ar.datetime.now(ar.timezone.utc))
    assert any(s["artifact"] == "quant_feedback.json" for s in st["stale"])
    assert st["overall_status"] == "green"


# ---------------------------------------------------------------------------
# idle_ok — append-only event logs are idle (info) not stale (warning) on quiet
# days, while genuine producer breaks are still caught. (2026-07-10)
# ---------------------------------------------------------------------------


def _event_log_row(idle_ok=True, cadence="daily", role="telemetry", sev="warning"):
    row = {"path": "outputs/policy/evt.jsonl", "label": "evt",
           "lens": "developer", "role": role, "required": False,
           "cadence": cadence, "producer": "p", "consumers": [],
           "severity_if_missing": sev, "consumer_status": "diagnostic_only"}
    if idle_ok:
        row["idle_ok"] = True
    return row


def _write_old(p, obj, age_hours):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_json.dumps(obj), encoding="utf-8")
    old = ar.datetime.now(ar.timezone.utc).timestamp() - age_hours * 3600
    os.utime(p, (old, old))


def test_is_idle_ok_helper_scoping():
    # opts in only when idle_ok truthy AND role is not source_of_truth
    assert ar.is_idle_ok(_event_log_row(idle_ok=True)) is True
    assert ar.is_idle_ok(_event_log_row(idle_ok=False)) is False
    sot = _event_log_row(idle_ok=True, role="source_of_truth")
    assert ar.is_idle_ok(sot) is False, "source_of_truth must never be idle_ok"


def test_idle_event_log_classified_idle_not_stale(tmp_path):
    # Criterion 1: an idle append-only event log (stale by mtime) is info/idle,
    # not stale/warning, and does NOT escalate governance.
    reg = {"daily_run_status_tracked": [],
           "artifacts": {"evt.jsonl": _event_log_row(idle_ok=True)}}
    _write_old(tmp_path / "outputs/policy/evt.jsonl", [{"e": 1}], age_hours=200)
    st = ar.validate_registry(reg, tmp_path, ar.datetime.now(ar.timezone.utc))
    assert st["overall_status"] == "green"           # not escalated
    assert "evt.jsonl" not in [s["artifact"] for s in st["stale"]]
    assert "evt.jsonl" in [i["artifact"] for i in st["idle"]]
    assert st["idle"][0]["idle_ok"] is True
    assert st["counts"]["idle"] == 1
    assert st["counts"]["stale"] == 0
    assert st["counts"]["present"] == 1              # idle counts as present, not a problem


def test_non_idle_event_log_still_stale(tmp_path):
    # A stale row WITHOUT idle_ok (a genuine producer break) still surfaces as stale
    # and escalates to amber at warning severity — the real-break path is preserved.
    reg = {"daily_run_status_tracked": [],
           "artifacts": {"evt.jsonl": _event_log_row(idle_ok=False, sev="warning")}}
    reg["artifacts"]["evt.jsonl"]["consumers"] = ["daily-tool-analysis"]
    reg["artifacts"]["evt.jsonl"]["consumer_status"] = "consumed"
    _write_old(tmp_path / "outputs/policy/evt.jsonl", [{"e": 1}], age_hours=200)
    st = ar.validate_registry(reg, tmp_path, ar.datetime.now(ar.timezone.utc))
    assert "evt.jsonl" in [s["artifact"] for s in st["stale"]]
    assert st["idle"] == []
    assert st["overall_status"] == "amber"


def test_source_of_truth_staleness_unchanged_even_if_idle_ok(tmp_path):
    # Criterion 3: source_of_truth staleness behaviour is unchanged — the idle_ok
    # flag cannot downgrade a stale source_of_truth (critical → red).
    row = _event_log_row(idle_ok=True, role="source_of_truth", sev="critical")
    row["path"] = "outputs/latest/sot.json"
    row["consumers"] = ["daily-tool-analysis"]
    row["consumer_status"] = "consumed"
    reg = {"daily_run_status_tracked": [], "artifacts": {"sot.json": row}}
    _write_old(tmp_path / "outputs/latest/sot.json", {"x": 1}, age_hours=200)
    st = ar.validate_registry(reg, tmp_path, ar.datetime.now(ar.timezone.utc))
    assert "sot.json" in [s["artifact"] for s in st["stale"]]
    assert st["idle"] == []
    assert st["overall_status"] == "red"


def test_status_schema_backward_compatible(tmp_path):
    # Criterion: additive only — every legacy key still present; idle is a new key.
    st = ar.run_artifact_registry(root=tmp_path, write_files=False)
    for key in ("generated_at", "observe_only", "schema_version", "source",
                "overall_status", "counts", "missing", "stale", "invalid_json",
                "schema_invalid", "classified", "unjustified_debt",
                "justified_no_consumer", "by_consumer_status", "debt_target_met",
                "severity", "by_lens", "operator_message"):
        assert key in st, f"legacy status key dropped: {key}"
    assert "idle" in st and isinstance(st["idle"], list)
    assert "idle" in st["counts"]


def test_shipped_event_logs_have_idle_ok():
    # The daily-cadence append-only event logs that used to false-positive stale
    # must carry idle_ok; source_of_truth rows must never carry it.
    reg = ar.load_registry()
    arts = reg["artifacts"]
    for key in ("system_improvement_history.jsonl", "user_action_log.jsonl",
                "decision_context_log.jsonl", "pattern_events.jsonl",
                "opportunity_events.jsonl", "outcome_events.jsonl"):
        assert arts[key].get("idle_ok") is True, f"{key} should be idle_ok"
    for key, row in arts.items():
        if row.get("role") == "source_of_truth":
            assert not row.get("idle_ok"), f"source_of_truth {key} must not be idle_ok"
