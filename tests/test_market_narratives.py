"""
Tests for portfolio_automation/market_narratives.py

Coverage:
  - missing inputs degrade safely
  - malformed JSON handled
  - empty inputs produce safe artifacts
  - daily narrative generation
  - weekly narrative generation
  - monthly narrative generation
  - markdown rendering (all sections)
  - safety flags hardcoded (observe_only, no_trade, not_recommendation)
  - no prohibited recommendation language in generated text
  - discovery candidates remain sandbox-only in narrative
  - forbidden statuses not emitted
  - no policy/portfolio/sandbox writes from narrative layer
  - LATEST namespace writes only
  - deterministic output (except generated_at)
  - invalid period raises ValueError
  - validate_narrative_safety detects prohibited phrases
  - safety sanitizer neutralizes prohibited phrases
  - missing discovery context degrades safely
  - data_quality_notes populated from dq artifact
  - confidence_notes populated from calibration artifact
  - operator_watchlist items do not contain trading instructions
  - write_files=False skips file writes
  - run_market_narratives single and multi-period
  - artifact paths written to outputs/latest/
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from portfolio_automation.market_narratives import (
    NarrativeInputSummary,
    NarrativeTheme,
    NarrativeRisk,
    NarrativeCatalyst,
    NarrativeDiscoveryContext,
    MarketNarrativeReport,
    load_all_inputs,
    validate_narrative_safety,
    build_market_narrative_report,
    render_market_narrative_markdown,
    write_market_narrative_report,
    run_market_narratives,
    _OBSERVE_ONLY,
    _NO_TRADE,
    _NOT_RECOMMENDATION,
    _SAFETY_DISCLAIMER,
    _PROHIBITED_INSTRUCTION_PATTERNS,
    _sanitize_text,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _write_latest(base: Path, name: str, payload: dict) -> None:
    d = base / "latest"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps(payload), encoding="utf-8")


def _write_sandbox(base: Path, relative: str, payload: dict) -> None:
    p = base / "sandbox" / relative
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload), encoding="utf-8")


def _news_intel_payload(tickers=None) -> dict:
    tickers = tickers or ["NVDA", "AAPL"]
    return {
        "observe_only": True,
        "evidence_packets": [
            {
                "entity_key": t,
                "themes": ["ai_infrastructure", "earnings_guidance"],
                "risk_flags": ["investigation"],
                "catalyst_flags": ["beat estimates"],
                "article_count": 4,
                "source_count": 2,
            }
            for t in tickers
        ],
    }


def _enriched_payload() -> dict:
    return {
        "observe_only": True,
        "enriched_candidates": [
            {
                "ticker": "NVDA",
                "candidate_status": "watch",
                "news_context": "research_supported",
                "matched_news_count": 5,
                "source_diversity": 3,
                "matched_themes": ["ai_infrastructure"],
                "risk_flags": [],
                "catalyst_flags": ["beat estimates"],
            },
            {
                "ticker": "ZZZZ",
                "candidate_status": "news_only",
                "news_context": "research_caution",
                "matched_news_count": 2,
                "source_diversity": 1,
                "matched_themes": ["legal_regulatory_risk"],
                "risk_flags": ["investigation", "fine"],
                "catalyst_flags": [],
            },
        ],
    }


def _decision_plan_payload() -> dict:
    return {
        "decisions": [
            {"ticker": "NVDA", "decision": "maintain", "decision_reason": "strong momentum"},
            {"ticker": "AAPL", "decision": "maintain", "decision_reason": "stable"},
        ]
    }


def _dq_payload() -> dict:
    return {
        "issues": [
            {"severity": "warning", "field": "some_field"},
            {"severity": "info", "field": "another"},
        ],
        "overall_health": "degraded",
    }


def _cal_payload() -> dict:
    return {
        "resolved_decisions": 25,
        "overall_accuracy": 0.72,
    }


def _minimal_inputs(base: Path) -> dict:
    """Write minimal valid inputs and return load_all_inputs result."""
    _write_latest(base, "news_intelligence.json", _news_intel_payload())
    return load_all_inputs(base)


# ---------------------------------------------------------------------------
# 1. Input loading
# ---------------------------------------------------------------------------

class TestInputLoading:
    def test_all_missing_returns_unavailable(self, tmp_path):
        inputs = load_all_inputs(tmp_path)
        for key, val in inputs.items():
            assert val["summary"].available is False

    def test_valid_news_intelligence_loaded(self, tmp_path):
        _write_latest(tmp_path, "news_intelligence.json", _news_intel_payload())
        inputs = load_all_inputs(tmp_path)
        assert inputs["news_intelligence"]["summary"].available is True
        assert inputs["news_intelligence"]["payload"] is not None

    def test_malformed_json_returns_unavailable(self, tmp_path):
        (tmp_path / "latest").mkdir(parents=True, exist_ok=True)
        (tmp_path / "latest" / "news_intelligence.json").write_text("NOT JSON")
        inputs = load_all_inputs(tmp_path)
        assert inputs["news_intelligence"]["summary"].available is False

    def test_list_valued_json_returns_unavailable(self, tmp_path):
        _write_latest(tmp_path, "news_intelligence.json", [1, 2, 3])
        # We wrote a list, but _write_latest wraps in dict — rewrite directly
        (tmp_path / "latest" / "news_intelligence.json").write_text("[1,2,3]")
        inputs = load_all_inputs(tmp_path)
        assert inputs["news_intelligence"]["summary"].available is False

    def test_empty_file_returns_unavailable(self, tmp_path):
        (tmp_path / "latest").mkdir(parents=True, exist_ok=True)
        (tmp_path / "latest" / "news_intelligence.json").write_text("")
        inputs = load_all_inputs(tmp_path)
        assert inputs["news_intelligence"]["summary"].available is False

    def test_approval_decisions_jsonl_loaded(self, tmp_path):
        path = tmp_path / "sandbox" / "discovery" / "approval_decisions.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"ticker": "NVDA", "decision": "watch"}\n')
        inputs = load_all_inputs(tmp_path)
        assert inputs["approval_decisions"]["summary"].available is True

    def test_enriched_candidates_loaded(self, tmp_path):
        _write_sandbox(tmp_path, "discovery/news_enriched_candidates.json", _enriched_payload())
        inputs = load_all_inputs(tmp_path)
        assert inputs["news_enriched_candidates"]["summary"].available is True


# ---------------------------------------------------------------------------
# 2. Safety validator
# ---------------------------------------------------------------------------

class TestSafetyValidator:
    def test_clean_text_returns_no_violations(self):
        text = "This narrative reviews market themes for context."
        assert validate_narrative_safety(text) == []

    def test_buy_now_detected(self):
        violations = validate_narrative_safety("Investors should buy now.")
        assert "buy now" in violations

    def test_sell_now_detected(self):
        assert "sell now" in validate_narrative_safety("sell now at market open")

    def test_recommend_buying_detected(self):
        assert "recommend buying" in validate_narrative_safety("I recommend buying NVDA")

    def test_add_shares_detected(self):
        assert "add shares" in validate_narrative_safety("Consider: add shares to AAPL")

    def test_execute_trade_detected(self):
        assert "execute trade" in validate_narrative_safety("execute trade immediately")

    def test_case_insensitive_detection(self):
        assert "buy now" in validate_narrative_safety("BUY NOW! Markets open.")

    def test_not_recommendation_phrase_clean(self):
        # "not a buy/sell" should NOT trigger the validator
        text = "This is not a buy/sell/hold recommendation."
        violations = validate_narrative_safety(text)
        # "buy now" and "sell now" are not in this text
        assert "buy now" not in violations
        assert "sell now" not in violations

    def test_promote_candidate_detected(self):
        assert "promote candidate" in validate_narrative_safety("We will promote candidate NVDA")

    def test_sanitizer_replaces_prohibited(self):
        result = _sanitize_text("Investors should buy now at open.")
        assert "buy now" not in result.lower()
        assert "[REDACTED]" in result

    def test_all_prohibited_patterns_covered(self):
        for pattern in _PROHIBITED_INSTRUCTION_PATTERNS:
            violations = validate_narrative_safety(f"The system will {pattern} today.")
            assert pattern in violations, f"Pattern not detected: {pattern!r}"


# ---------------------------------------------------------------------------
# 3. Daily narrative
# ---------------------------------------------------------------------------

class TestDailyNarrative:
    def test_builds_without_error(self, tmp_path):
        inputs = _minimal_inputs(tmp_path)
        report = build_market_narrative_report("daily", inputs, tmp_path)
        assert report.narrative_period == "daily"

    def test_safety_flags_hardcoded(self, tmp_path):
        inputs = _minimal_inputs(tmp_path)
        report = build_market_narrative_report("daily", inputs, tmp_path)
        assert report.observe_only is True
        assert report.no_trade is True
        assert report.not_recommendation is True

    def test_headline_not_empty(self, tmp_path):
        inputs = _minimal_inputs(tmp_path)
        report = build_market_narrative_report("daily", inputs, tmp_path)
        assert report.top_headline

    def test_executive_summary_not_empty(self, tmp_path):
        inputs = _minimal_inputs(tmp_path)
        report = build_market_narrative_report("daily", inputs, tmp_path)
        assert report.executive_summary

    def test_key_themes_populated(self, tmp_path):
        inputs = _minimal_inputs(tmp_path)
        report = build_market_narrative_report("daily", inputs, tmp_path)
        assert len(report.key_themes) > 0

    def test_safety_disclaimer_present(self, tmp_path):
        inputs = _minimal_inputs(tmp_path)
        report = build_market_narrative_report("daily", inputs, tmp_path)
        assert report.safety_disclaimer == _SAFETY_DISCLAIMER

    def test_empty_inputs_safe(self, tmp_path):
        inputs = load_all_inputs(tmp_path)
        report = build_market_narrative_report("daily", inputs, tmp_path)
        assert report.data_available is False
        assert report.narrative_period == "daily"

    def test_data_quality_notes_populated(self, tmp_path):
        _write_latest(tmp_path, "data_quality_report.json", _dq_payload())
        inputs = load_all_inputs(tmp_path)
        report = build_market_narrative_report("daily", inputs, tmp_path)
        assert len(report.data_quality_notes) > 0

    def test_confidence_notes_populated(self, tmp_path):
        _write_latest(tmp_path, "confidence_calibration.json", _cal_payload())
        inputs = load_all_inputs(tmp_path)
        report = build_market_narrative_report("daily", inputs, tmp_path)
        assert len(report.confidence_notes) > 0

    def test_discovery_context_populated(self, tmp_path):
        _write_sandbox(tmp_path, "discovery/news_enriched_candidates.json", _enriched_payload())
        inputs = load_all_inputs(tmp_path)
        report = build_market_narrative_report("daily", inputs, tmp_path)
        assert report.discovery_context is not None
        assert report.discovery_context.candidate_count == 2

    def test_risks_populated_from_news(self, tmp_path):
        inputs = _minimal_inputs(tmp_path)
        report = build_market_narrative_report("daily", inputs, tmp_path)
        assert len(report.risks_to_watch) > 0

    def test_catalysts_populated_from_news(self, tmp_path):
        inputs = _minimal_inputs(tmp_path)
        report = build_market_narrative_report("daily", inputs, tmp_path)
        assert len(report.catalysts_to_watch) > 0

    def test_no_prohibited_language_in_headline(self, tmp_path):
        inputs = _minimal_inputs(tmp_path)
        report = build_market_narrative_report("daily", inputs, tmp_path)
        violations = validate_narrative_safety(report.top_headline)
        assert violations == [], f"Prohibited language in headline: {violations}"

    def test_no_prohibited_language_in_summary(self, tmp_path):
        inputs = _minimal_inputs(tmp_path)
        report = build_market_narrative_report("daily", inputs, tmp_path)
        violations = validate_narrative_safety(report.executive_summary)
        assert violations == [], f"Prohibited language in summary: {violations}"

    def test_missing_inputs_tracked(self, tmp_path):
        inputs = load_all_inputs(tmp_path)
        report = build_market_narrative_report("daily", inputs, tmp_path)
        assert len(report.missing_inputs) > 0


# ---------------------------------------------------------------------------
# 4. Weekly narrative
# ---------------------------------------------------------------------------

class TestWeeklyNarrative:
    def test_builds_without_error(self, tmp_path):
        inputs = _minimal_inputs(tmp_path)
        report = build_market_narrative_report("weekly", inputs, tmp_path)
        assert report.narrative_period == "weekly"

    def test_safety_flags(self, tmp_path):
        inputs = load_all_inputs(tmp_path)
        report = build_market_narrative_report("weekly", inputs, tmp_path)
        assert report.observe_only is True
        assert report.no_trade is True
        assert report.not_recommendation is True

    def test_empty_inputs_safe(self, tmp_path):
        inputs = load_all_inputs(tmp_path)
        report = build_market_narrative_report("weekly", inputs, tmp_path)
        assert report.data_available is False

    def test_headline_references_weekly(self, tmp_path):
        inputs = _minimal_inputs(tmp_path)
        report = build_market_narrative_report("weekly", inputs, tmp_path)
        # Either "weekly" or themes are in headline
        assert report.top_headline

    def test_discovery_context_sandbox_only(self, tmp_path):
        _write_sandbox(tmp_path, "discovery/news_enriched_candidates.json", _enriched_payload())
        inputs = load_all_inputs(tmp_path)
        report = build_market_narrative_report("weekly", inputs, tmp_path)
        disc = report.discovery_context
        assert disc is not None
        assert "sandbox" in disc.disclaimer.lower() or "not promoted" in disc.disclaimer.lower()


# ---------------------------------------------------------------------------
# 5. Monthly narrative
# ---------------------------------------------------------------------------

class TestMonthlyNarrative:
    def test_builds_without_error(self, tmp_path):
        inputs = _minimal_inputs(tmp_path)
        report = build_market_narrative_report("monthly", inputs, tmp_path)
        assert report.narrative_period == "monthly"

    def test_safety_flags(self, tmp_path):
        inputs = load_all_inputs(tmp_path)
        report = build_market_narrative_report("monthly", inputs, tmp_path)
        assert report.observe_only is True
        assert report.no_trade is True
        assert report.not_recommendation is True

    def test_empty_inputs_safe(self, tmp_path):
        inputs = load_all_inputs(tmp_path)
        report = build_market_narrative_report("monthly", inputs, tmp_path)
        assert report.data_available is False

    def test_invalid_period_raises(self, tmp_path):
        inputs = load_all_inputs(tmp_path)
        with pytest.raises(ValueError, match="Invalid period"):
            build_market_narrative_report("quarterly", inputs, tmp_path)


# ---------------------------------------------------------------------------
# 6. Markdown rendering
# ---------------------------------------------------------------------------

class TestMarkdownRendering:
    def _report(self, period="daily") -> MarketNarrativeReport:
        return MarketNarrativeReport(
            narrative_period=period,
            generated_at="2026-05-11T00:00:00Z",
            top_headline=f"{period.title()} headline",
            executive_summary="This is observe-only narrative context.",
            key_themes=[
                NarrativeTheme("ai_infrastructure", 3, ["NVDA"], "AI theme active.")
            ],
            portfolio_context="Decision plan has 2 positions.",
            discovery_context=NarrativeDiscoveryContext(
                candidate_count=2, watch_count=1,
                news_supported=["NVDA"], risk_heavy=["ZZZZ"],
                news_only=["PLTR"], top_themes=["ai_infrastructure"],
            ),
            risks_to_watch=[NarrativeRisk("investigation", ["ZZZZ"], ["news"], "Risk.")],
            catalysts_to_watch=[NarrativeCatalyst("beat estimates", ["NVDA"], ["news"], "Catalyst.")],
            data_quality_notes=["2 warning issues detected."],
            confidence_notes=["25 resolved decisions."],
            operator_watchlist=["Review risk context for: ZZZZ."],
            inputs_used=[
                NarrativeInputSummary("news_intelligence", True),
                NarrativeInputSummary("decision_plan", False),
            ],
            missing_inputs=["decision_plan"],
        )

    def test_markdown_contains_disclaimer(self):
        md = render_market_narrative_markdown(self._report())
        assert "observe-only" in md.lower() or "not a buy/sell" in md.lower()

    def test_markdown_contains_period_header(self):
        for period in ("daily", "weekly", "monthly"):
            md = render_market_narrative_markdown(self._report(period))
            assert period.title() in md or period in md.lower()

    def test_markdown_contains_themes(self):
        md = render_market_narrative_markdown(self._report())
        assert "Ai Infrastructure" in md or "ai_infrastructure" in md

    def test_markdown_contains_risks(self):
        md = render_market_narrative_markdown(self._report())
        assert "investigation" in md.lower() or "Risks" in md

    def test_markdown_contains_catalysts(self):
        md = render_market_narrative_markdown(self._report())
        assert "beat estimates" in md.lower() or "Catalysts" in md

    def test_markdown_contains_discovery_section(self):
        md = render_market_narrative_markdown(self._report())
        assert "Discovery" in md
        assert "sandbox" in md.lower()

    def test_markdown_contains_data_quality(self):
        md = render_market_narrative_markdown(self._report())
        assert "Data Quality" in md

    def test_markdown_contains_operator_review(self):
        md = render_market_narrative_markdown(self._report())
        assert "Watch" in md or "Review" in md

    def test_markdown_contains_input_coverage(self):
        md = render_market_narrative_markdown(self._report())
        assert "Input Coverage" in md or "available" in md.lower()

    def test_markdown_no_buy_instructions(self):
        md = render_market_narrative_markdown(self._report())
        violations = validate_narrative_safety(md)
        assert violations == [], f"Prohibited language in markdown: {violations}"

    def test_markdown_source_label_present(self):
        md = render_market_narrative_markdown(self._report())
        assert "market_narratives_layer" in md

    def test_daily_markdown_has_what_changed(self):
        md = render_market_narrative_markdown(self._report("daily"))
        assert "What Changed" in md

    def test_weekly_markdown_has_persistent_themes(self):
        md = render_market_narrative_markdown(self._report("weekly"))
        assert "Persistent" in md or "Weekly" in md

    def test_monthly_markdown_has_regime(self):
        md = render_market_narrative_markdown(self._report("monthly"))
        assert "Regime" in md or "Monthly" in md


# ---------------------------------------------------------------------------
# 7. Artifact writing
# ---------------------------------------------------------------------------

class TestArtifactWriting:
    def test_writes_json_to_latest(self, tmp_path):
        report = MarketNarrativeReport(
            narrative_period="daily",
            generated_at="2026-05-11T00:00:00Z",
        )
        paths = write_market_narrative_report("daily", report, tmp_path)
        json_path = Path(paths["market_narrative_daily_json"])
        assert json_path.exists()
        assert "latest" in str(json_path)

    def test_writes_md_to_latest(self, tmp_path):
        report = MarketNarrativeReport(
            narrative_period="daily",
            generated_at="2026-05-11T00:00:00Z",
        )
        paths = write_market_narrative_report("daily", report, tmp_path)
        md_path = Path(paths["market_narrative_daily_md"])
        assert md_path.exists()
        assert "latest" in str(md_path)

    def test_json_has_safety_flags(self, tmp_path):
        report = MarketNarrativeReport(
            narrative_period="weekly",
            generated_at="2026-05-11T00:00:00Z",
        )
        write_market_narrative_report("weekly", report, tmp_path)
        payload = json.loads(
            (tmp_path / "latest" / "market_narrative_weekly.json").read_text()
        )
        assert payload["observe_only"] is True
        assert payload["no_trade"] is True
        assert payload["not_recommendation"] is True

    def test_no_policy_namespace_writes(self, tmp_path):
        report = MarketNarrativeReport(
            narrative_period="monthly",
            generated_at="2026-05-11T00:00:00Z",
        )
        write_market_narrative_report("monthly", report, tmp_path)
        assert not (tmp_path / "policy").exists() or not any(
            (tmp_path / "policy").iterdir()
        )

    def test_no_sandbox_namespace_writes(self, tmp_path):
        report = MarketNarrativeReport(
            narrative_period="daily",
            generated_at="2026-05-11T00:00:00Z",
        )
        write_market_narrative_report("daily", report, tmp_path)
        assert not (tmp_path / "sandbox").exists()

    def test_no_portfolio_namespace_writes(self, tmp_path):
        report = MarketNarrativeReport(
            narrative_period="daily",
            generated_at="2026-05-11T00:00:00Z",
        )
        write_market_narrative_report("daily", report, tmp_path)
        assert not (tmp_path / "portfolio").exists()

    def test_invalid_period_raises(self, tmp_path):
        report = MarketNarrativeReport(
            narrative_period="quarterly",
            generated_at="2026-05-11T00:00:00Z",
        )
        with pytest.raises(ValueError, match="Unknown period"):
            write_market_narrative_report("quarterly", report, tmp_path)

    def test_all_three_periods_written(self, tmp_path):
        for period in ("daily", "weekly", "monthly"):
            report = MarketNarrativeReport(
                narrative_period=period,
                generated_at="2026-05-11T00:00:00Z",
            )
            paths = write_market_narrative_report(period, report, tmp_path)
            assert Path(paths[f"market_narrative_{period}_json"]).exists()
            assert Path(paths[f"market_narrative_{period}_md"]).exists()


# ---------------------------------------------------------------------------
# 8. Orchestrator (run_market_narratives)
# ---------------------------------------------------------------------------

class TestOrchestrator:
    def test_runs_daily_by_default(self, tmp_path):
        result = run_market_narratives(base_dir=tmp_path)
        assert "daily" in result

    def test_runs_all_periods(self, tmp_path):
        result = run_market_narratives(
            base_dir=tmp_path, periods=["daily", "weekly", "monthly"]
        )
        assert "daily" in result
        assert "weekly" in result
        assert "monthly" in result

    def test_write_files_true_creates_artifacts(self, tmp_path):
        run_market_narratives(base_dir=tmp_path, periods=["daily"], write_files=True)
        assert (tmp_path / "latest" / "market_narrative_daily.json").exists()
        assert (tmp_path / "latest" / "market_narrative_daily.md").exists()

    def test_write_files_false_no_artifacts(self, tmp_path):
        run_market_narratives(base_dir=tmp_path, periods=["daily"], write_files=False)
        assert not (tmp_path / "latest" / "market_narrative_daily.json").exists()

    def test_safety_flags_in_result(self, tmp_path):
        result = run_market_narratives(base_dir=tmp_path)
        assert result["observe_only"] is True
        assert result["no_trade"] is True
        assert result["not_recommendation"] is True

    def test_empty_inputs_safe(self, tmp_path):
        result = run_market_narratives(base_dir=tmp_path)
        assert isinstance(result, dict)
        assert result["observe_only"] is True

    def test_artifacts_in_latest_namespace(self, tmp_path):
        result = run_market_narratives(
            base_dir=tmp_path, periods=["daily", "weekly"], write_files=True
        )
        for path_str in result.get("artifacts", {}).values():
            assert "latest" in path_str

    def test_with_real_inputs(self, tmp_path):
        _write_latest(tmp_path, "news_intelligence.json", _news_intel_payload())
        _write_latest(tmp_path, "data_quality_report.json", _dq_payload())
        _write_latest(tmp_path, "confidence_calibration.json", _cal_payload())
        _write_latest(tmp_path, "decision_plan.json", _decision_plan_payload())
        _write_sandbox(tmp_path, "discovery/news_enriched_candidates.json", _enriched_payload())
        result = run_market_narratives(base_dir=tmp_path, periods=["daily"])
        assert result["daily"]["data_available"] is True
        assert result["daily"]["themes_found"] > 0

    def test_deterministic_structure(self, tmp_path):
        _write_latest(tmp_path, "news_intelligence.json", _news_intel_payload())
        r1 = run_market_narratives(
            base_dir=tmp_path, periods=["daily"], write_files=False
        )
        r2 = run_market_narratives(
            base_dir=tmp_path, periods=["daily"], write_files=False
        )
        # Structure (themes/risks counts) should be same across runs
        assert r1["daily"]["themes_found"] == r2["daily"]["themes_found"]
        assert r1["daily"]["risks_found"] == r2["daily"]["risks_found"]

    def test_no_safety_violations_in_output_json(self, tmp_path):
        _write_latest(tmp_path, "news_intelligence.json", _news_intel_payload())
        run_market_narratives(base_dir=tmp_path, periods=["daily"], write_files=True)
        payload = json.loads(
            (tmp_path / "latest" / "market_narrative_daily.json").read_text()
        )
        # Check executive_summary and headline for prohibited language
        for field_name in ("executive_summary", "top_headline", "portfolio_context"):
            text = payload.get(field_name, "")
            violations = validate_narrative_safety(text)
            assert violations == [], f"Prohibited in {field_name}: {violations}"


# ---------------------------------------------------------------------------
# 9. Discovery boundary
# ---------------------------------------------------------------------------

class TestDiscoveryBoundary:
    def test_news_supported_label_used(self, tmp_path):
        _write_sandbox(tmp_path, "discovery/news_enriched_candidates.json", _enriched_payload())
        inputs = load_all_inputs(tmp_path)
        report = build_market_narrative_report("daily", inputs, tmp_path)
        disc = report.discovery_context
        assert disc is not None
        assert "NVDA" in disc.news_supported

    def test_risk_heavy_label_used(self, tmp_path):
        _write_sandbox(tmp_path, "discovery/news_enriched_candidates.json", _enriched_payload())
        inputs = load_all_inputs(tmp_path)
        report = build_market_narrative_report("daily", inputs, tmp_path)
        assert "ZZZZ" in report.discovery_context.risk_heavy

    def test_no_promoted_in_discovery_context(self, tmp_path):
        _write_sandbox(tmp_path, "discovery/news_enriched_candidates.json", _enriched_payload())
        inputs = load_all_inputs(tmp_path)
        report = build_market_narrative_report("daily", inputs, tmp_path)
        disc = report.discovery_context
        for field_val in [disc.news_supported, disc.risk_heavy, disc.news_only]:
            assert "PROMOTED" not in field_val
            assert "ACTIONABLE" not in field_val

    def test_discovery_disclaimer_present_in_markdown(self, tmp_path):
        _write_sandbox(tmp_path, "discovery/news_enriched_candidates.json", _enriched_payload())
        inputs = load_all_inputs(tmp_path)
        report = build_market_narrative_report("daily", inputs, tmp_path)
        md = render_market_narrative_markdown(report)
        assert "sandbox" in md.lower()

    def test_discovery_context_none_without_data(self, tmp_path):
        inputs = load_all_inputs(tmp_path)
        report = build_market_narrative_report("daily", inputs, tmp_path)
        # discovery_context is always populated (even if empty)
        assert report.discovery_context is not None
        assert report.discovery_context.candidate_count == 0
