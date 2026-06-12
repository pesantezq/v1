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
    names = {a.name for a in doc_audit.ANCHOR_REGISTRY}
    assert len(doc_audit.ANCHOR_REGISTRY) >= 3
    assert {"concentration_cap", "leverage_cap", "sector_cap"} <= names
    # ambiguous anchors intentionally dropped in the 2026-06-01 calibration
    assert {"pipeline_stage_count", "fmp_daily_budget", "ai_monthly_cap"}.isdisjoint(names)
    for a in doc_audit.ANCHOR_REGISTRY:
        assert a.pattern.count("(") >= 1
        assert a.fmt in {"int", "float2", "pct1", "usd0"}


# Shared fixtures for the calibrated structural-cap anchors. The doc mirrors the
# real ALLOCATION_POLICY.md shape: canonical "- `name = 0.NN`" bullet lines PLUS
# nearby "Pre-retune baseline ..." lines that hold different numbers and must
# never be matched/auto-fixed.
def _caps_src(root, conc=0.60, lev=0.25, sec=0.35):
    _write(root, "outputs/latest/retune_impact.json", json.dumps({
        "current_snapshot": {
            "structural_caps": {"concentration_cap": conc, "leverage_cap": lev},
            "allocation_engine": {"sector_cap": sec}}}))


def _alloc_doc(root, conc="0.60", lev="0.25", sec="0.35"):
    _write(root, "docs/ALLOCATION_POLICY.md",
           f"- `sector_cap = {sec}`\n"
           f"Pre-retune baseline: `max_position_cap = 0.08`, `sector_cap = 0.20`.\n"
           f"- `concentration_cap = {conc}` — max share of portfolio in a single position\n"
           f"- `leverage_cap = {lev}` — max share of portfolio in leveraged exposure\n"
           f"Pre-retune baseline (2026-05-18): `concentration_cap = 0.40`, `leverage_cap = 0.15`.\n")


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
    _caps_src(tmp_path, conc=0.65)         # source bumped to 0.65
    _alloc_doc(tmp_path, conc="0.60")       # doc bullet still says 0.60
    findings = doc_audit.find_drift(str(tmp_path))
    drift = [f for f in findings if f.anchor == "concentration_cap"]
    assert len(drift) == 1                  # exactly one — the baseline 0.40 line is NOT matched
    assert drift[0].current == "0.60" and drift[0].expected == "0.65"
    assert drift[0].auto_fixable is True
    assert drift[0].dimension == "drift"


def test_find_drift_silent_when_doc_matches_source(tmp_path):
    _caps_src(tmp_path)                      # 0.60 / 0.25 / 0.35
    _alloc_doc(tmp_path)                     # bullets match source
    assert doc_audit.find_drift(str(tmp_path)) == []


def test_find_drift_reports_only_when_source_unresolvable(tmp_path):
    _alloc_doc(tmp_path)                     # doc present, but no retune_impact.json source
    assert doc_audit.find_drift(str(tmp_path)) == []


def test_find_drift_never_matches_pre_retune_baseline_value(tmp_path):
    # Calibration safety: the bullet anchor (^- `name = ...`) must exclude the
    # "Pre-retune baseline ... concentration_cap = 0.40" line, so drift only ever
    # reports the current bullet value (0.60), never the historical 0.40.
    _caps_src(tmp_path, conc=0.65)
    _alloc_doc(tmp_path, conc="0.60")
    currents = [f.current for f in doc_audit.find_drift(str(tmp_path))
                if f.anchor == "concentration_cap"]
    assert currents == ["0.60"]              # never "0.40"


def test_find_coverage_gaps_flags_new_module_without_doc():
    changed = ["portfolio_automation/new_widget.py", "tests/test_new_widget.py"]
    existing_docs = {"docs/ARCHITECTURE.md"}
    gaps = doc_audit.find_coverage_gaps(changed, existing_docs)
    names = [g.detail for g in gaps]
    assert any("new_widget" in n for n in names)
    assert all(g.dimension == "coverage" and g.auto_fixable is False for g in gaps)


def test_find_coverage_gaps_silent_when_doc_exists():
    changed = ["portfolio_automation/new_widget.py"]
    existing_docs = {"docs/new_widget.md"}
    assert doc_audit.find_coverage_gaps(changed, existing_docs) == []


def test_find_coverage_gaps_ignores_non_source_changes():
    changed = ["README.md", "outputs/latest/foo.json"]
    assert doc_audit.find_coverage_gaps(changed, set()) == []


def test_find_coverage_gaps_silent_when_module_cited_in_grouped_doc():
    # No docs/<stem>.md, but the module's full path is documented in a
    # subsystem/grouped doc (the repo's convention for the next-stage lane).
    changed = ["portfolio_automation/universe_scanner.py"]
    existing_docs = {"docs/NEXT_STAGE_IMPLEMENTATION.md"}
    documented = {"portfolio_automation/universe_scanner.py"}
    assert doc_audit.find_coverage_gaps(changed, existing_docs, documented) == []


def test_find_coverage_gaps_silent_when_module_cited_by_basename():
    # Subsystem docs frequently cite modules by backticked basename
    # (e.g. `profiles.py`); that counts as coverage too.
    changed = ["portfolio_automation/strategy/profiles.py"]
    documented = {"profiles.py"}
    assert doc_audit.find_coverage_gaps(changed, set(), documented) == []


def test_find_coverage_gaps_flags_module_absent_from_all_docs():
    # Neither a per-module doc nor any citation anywhere -> still a real gap.
    changed = ["portfolio_automation/pipeline_wiring_probe.py"]
    gaps = doc_audit.find_coverage_gaps(changed, set(), documented_modules=set())
    assert any("pipeline_wiring_probe" in g.detail for g in gaps)


def test_collect_documented_modules_extracts_cited_paths(tmp_path):
    _write(tmp_path, "docs/NEXT_STAGE_IMPLEMENTATION.md",
           "| 5 | `universe_scanner.py` | radar |\n"
           "| 9 | `portfolio_automation/brokers/base.py` | client |\n")
    mods = doc_audit.collect_documented_modules(str(tmp_path))
    assert "universe_scanner.py" in mods                       # backticked basename
    assert "portfolio_automation/brokers/base.py" in mods       # full path form
    assert "base.py" in mods                                    # basename of a full path


def test_find_dead_refs_flags_missing_python_file(tmp_path):
    _write(tmp_path, "docs/ARCHITECTURE.md",
           "See `portfolio_automation/ghost_module.py` for details.\n")
    findings = doc_audit.find_dead_refs(str(tmp_path))
    assert any("ghost_module.py" in f.detail for f in findings)
    assert all(f.dimension == "dead_ref" and f.auto_fixable is False for f in findings)


def test_find_dead_refs_silent_for_existing_file(tmp_path):
    _write(tmp_path, "portfolio_automation/real_module.py", "x = 1\n")
    _write(tmp_path, "docs/ARCHITECTURE.md",
           "See `portfolio_automation/real_module.py`.\n")
    assert doc_audit.find_dead_refs(str(tmp_path)) == []


def test_find_cross_doc_inconsistency_flags_disagreement(tmp_path):
    # Inject a synthetic 2-doc anchor (decoupled from the live registry).
    _write(tmp_path, "docs/A.md", "- `widget_cap = 0.10`\n")
    _write(tmp_path, "docs/B.md", "- `widget_cap = 0.20`\n")
    test_anchor = doc_audit.Anchor(
        name="widget_cap", source_artifact="outputs/latest/x.json",
        source_json_path="a", doc_globs=("docs/A.md", "docs/B.md"),
        pattern=r"^- `widget_cap = (\d+\.\d+)`", fmt="float2")
    findings = doc_audit.find_cross_doc_inconsistency(str(tmp_path), registry=[test_anchor])
    assert any(f.anchor == "widget_cap" for f in findings)
    assert all(f.dimension == "consistency" for f in findings)


def test_run_doc_audit_assembles_status_dict(tmp_path):
    _caps_src(tmp_path, conc=0.65)          # source bumped
    _alloc_doc(tmp_path, conc="0.60")        # doc stale -> concentration_cap drift
    result = doc_audit.run_doc_audit(
        str(tmp_path), last_audited_sha="abc123",
        changed_files=["portfolio_automation/new_widget.py"],
        existing_doc_paths={"docs/ALLOCATION_POLICY.md"},
    )
    assert result["observe_only"] is True
    assert result["source"] == "doc_audit"
    assert result["last_audited_sha"] == "abc123"
    assert result["overall_status"] in {"drift", "coverage_gap", "ok_with_warnings"}
    assert any(f["anchor"] == "concentration_cap" for f in result["findings"])
    assert any(f["auto_fixable"] for f in result["auto_fix_candidates"])


def test_run_doc_audit_degrades_gracefully_on_empty_repo(tmp_path):
    result = doc_audit.run_doc_audit(str(tmp_path), last_audited_sha=None,
                                     changed_files=[], existing_doc_paths=set())
    assert result["observe_only"] is True
    assert result["overall_status"] == "ok"
    assert result["findings"] == []


def test_write_doc_audit_status_emits_json_and_md(tmp_path):
    result = doc_audit.run_doc_audit(str(tmp_path), last_audited_sha=None,
                                     changed_files=[], existing_doc_paths=set())
    json_path = doc_audit.write_doc_audit_status(result, str(tmp_path))
    assert Path(json_path).exists()
    loaded = json.loads(Path(json_path).read_text())
    assert loaded["source"] == "doc_audit"
    assert (Path(json_path).parent / "doc_audit_status.md").exists()


def test_run_doc_audit_coverage_gap_status_reachable(tmp_path):
    # a coverage gap alone -> overall_status == "coverage_gap"
    result = doc_audit.run_doc_audit(
        str(tmp_path), last_audited_sha=None,
        changed_files=["portfolio_automation/orphan_mod.py"],
        existing_doc_paths=set())
    assert result["overall_status"] == "coverage_gap"
    assert any(f["dimension"] == "coverage" for f in result["coverage_gaps"])


def test_apply_auto_fix_substitutes_only_the_captured_value(tmp_path):
    _caps_src(tmp_path, conc=0.65)
    doc = _write(tmp_path, "docs/ALLOCATION_POLICY.md",
                 "- `concentration_cap = 0.60` — max share of portfolio in a single position\n"
                 "Pre-retune baseline (2026-05-18): `concentration_cap = 0.40`.\n")
    drift = [f for f in doc_audit.find_drift(str(tmp_path)) if f.anchor == "concentration_cap"]
    assert len(drift) == 1
    changed = doc_audit.apply_auto_fix(drift[0], str(tmp_path))
    assert changed is True
    text = doc.read_text()
    assert "- `concentration_cap = 0.65`" in text                                   # current bullet fixed
    assert "Pre-retune baseline (2026-05-18): `concentration_cap = 0.40`." in text   # baseline untouched


def test_apply_auto_fix_refuses_non_auto_fixable():
    f = doc_audit.Finding(dimension="dead_ref", severity="med",
                          doc="docs/X.md", detail="x", auto_fixable=False)
    assert doc_audit.apply_auto_fix(f, ".") is False


def test_apply_auto_fix_refuses_path_escape(tmp_path):
    # A doc path escaping root must be refused even when the target file exists.
    outside = tmp_path.parent / "doc_audit_escape_target.md"
    outside.write_text("Runs 17 pipeline stages.\n", encoding="utf-8")
    try:
        f = doc_audit.Finding(
            dimension="drift", severity="med", doc=f"../{outside.name}",
            detail="x", auto_fixable=True, anchor="concentration_cap",
            current="0.60", expected="0.65", line=1)
        assert doc_audit.apply_auto_fix(f, str(tmp_path)) is False
        assert outside.read_text() == "Runs 17 pipeline stages.\n"  # untouched
    finally:
        outside.unlink(missing_ok=True)
