import json
from pathlib import Path
import pytest
from portfolio_automation import doc_audit


def _write(root: Path, rel: str, content: str):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def test_resolve_source_reads_nested_json_value(tmp_path):
    _write(tmp_path, "outputs/latest/daily_run_status.json",
           json.dumps({"stage_summary": {"total": 24}}))
    anchor = doc_audit.Anchor(
        name="pipeline_stage_count",
        source_artifact="outputs/latest/daily_run_status.json",
        source_json_path="stage_summary.total",
        doc_globs=("docs/PIPELINE_RUNBOOK.md",),
        pattern=r"(\d+)\s+pipeline stages",
        fmt="int",
    )
    assert doc_audit.resolve_source(anchor, str(tmp_path)) == "24"


def test_resolve_source_returns_none_when_artifact_missing(tmp_path):
    anchor = doc_audit.Anchor(
        name="x", source_artifact="outputs/latest/missing.json",
        source_json_path="a.b", doc_globs=("docs/X.md",), pattern=r"(\d+)",
    )
    assert doc_audit.resolve_source(anchor, str(tmp_path)) is None


def test_registry_is_non_empty_and_well_formed():
    assert len(doc_audit.ANCHOR_REGISTRY) >= 6
    for a in doc_audit.ANCHOR_REGISTRY:
        assert a.pattern.count("(") >= 1
        assert a.fmt in {"int", "float2", "pct1", "usd0"}


def test_resolve_source_pct1_formats_as_integer_percent(tmp_path):
    _write(tmp_path, "outputs/latest/retune_impact.json",
           json.dumps({"current_snapshot": {"structural_caps": {"concentration_cap": 0.6}}}))
    anchor = doc_audit.Anchor(
        name="concentration_cap",
        source_artifact="outputs/latest/retune_impact.json",
        source_json_path="current_snapshot.structural_caps.concentration_cap",
        doc_globs=("docs/ALLOCATION_POLICY.md",),
        pattern=r"concentration cap[^\d]*(\d+)\s*%",
        fmt="pct1",
    )
    assert doc_audit.resolve_source(anchor, str(tmp_path)) == "60"


def test_resolve_source_usd0_formats_without_decimals(tmp_path):
    _write(tmp_path, "outputs/latest/ai_budget_summary.json",
           json.dumps({"monthly_cost_limit_usd": 20.0}))
    anchor = doc_audit.Anchor(
        name="ai_monthly_cap",
        source_artifact="outputs/latest/ai_budget_summary.json",
        source_json_path="monthly_cost_limit_usd",
        doc_globs=("docs/AI_BUDGET.md",),
        pattern=r"monthly[^\$]*\$(\d+)",
        fmt="usd0",
    )
    assert doc_audit.resolve_source(anchor, str(tmp_path)) == "20"


def test_find_drift_flags_mismatch_and_marks_auto_fixable(tmp_path):
    _write(tmp_path, "outputs/latest/daily_run_status.json",
           json.dumps({"stage_summary": {"total": 24}}))
    _write(tmp_path, "docs/PIPELINE_RUNBOOK.md",
           "The wrapper runs 17 pipeline stages end to end.\n")
    findings = doc_audit.find_drift(str(tmp_path))
    drift = [f for f in findings if f.anchor == "pipeline_stage_count"]
    assert len(drift) == 1
    assert drift[0].current == "17" and drift[0].expected == "24"
    assert drift[0].auto_fixable is True
    assert drift[0].dimension == "drift"


def test_find_drift_silent_when_doc_matches_source(tmp_path):
    _write(tmp_path, "outputs/latest/daily_run_status.json",
           json.dumps({"stage_summary": {"total": 24}}))
    _write(tmp_path, "docs/PIPELINE_RUNBOOK.md", "Runs 24 pipeline stages.\n")
    assert doc_audit.find_drift(str(tmp_path)) == []


def test_find_drift_reports_only_when_source_unresolvable(tmp_path):
    _write(tmp_path, "docs/PIPELINE_RUNBOOK.md", "Runs 17 pipeline stages.\n")
    assert doc_audit.find_drift(str(tmp_path)) == []
