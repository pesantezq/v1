"""
Tests for portfolio_automation/news_evidence_layer.py
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from portfolio_automation.news_evidence_layer import (
    NewsEvidenceInputSummary,
    TickerNewsEvidence,
    DecisionNewsContext,
    NewsRiskEvidence,
    NewsCatalystEvidence,
    NewsEvidenceLayerReport,
    UnsafeNewsEvidenceArtifactError,
    load_all_inputs,
    build_news_evidence_layer_report,
    render_news_evidence_markdown,
    write_news_evidence_layer_report,
    run_news_evidence_layer,
    validate_news_evidence_safety,
    sanitize_news_evidence_text,
    sanitize_label,
    sanitize_nested_news_evidence_payload,
    _SAFETY_DISCLAIMER,
    _PROHIBITED_INSTRUCTION_PATTERNS,
    _INFLUENCE_CAP,
    _STRENGTH_NONE,
    _STRENGTH_WEAK,
    _STRENGTH_MODERATE,
    _STRENGTH_STRONG,
    _EFFECT_INFORMATIONAL,
    _EFFECT_RISK,
    _EFFECT_CATALYST,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_latest(base: Path, name: str, payload: dict) -> None:
    d = base / "latest"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps(payload), encoding="utf-8")


def _write_sandbox(base: Path, relative: str, payload: dict) -> None:
    p = base / "sandbox" / relative
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload), encoding="utf-8")


def _news_intel(tickers=None) -> dict:
    tickers = tickers or ["NVDA", "AAPL"]
    return {
        "observe_only": True,
        "evidence_packets": [
            {
                "entity_key": t,
                "related_tickers": [t],
                "themes": ["ai_infrastructure", "earnings_guidance"],
                "risk_flags": ["investigation"],
                "catalyst_flags": ["beat estimates"],
                "article_count": 5,
                "source_count": 3,
            }
            for t in tickers
        ],
    }


def _decision_plan(tickers=None) -> dict:
    tickers = tickers or ["NVDA", "AAPL"]
    return {
        "decisions": [
            {
                "ticker": t,
                "decision": "maintain",
                "decision_reason": f"{t} stable momentum",
            }
            for t in tickers
        ]
    }


def _enriched() -> dict:
    return {
        "observe_only": True,
        "enriched_candidates": [
            {
                "ticker": "ZZZZ",
                "candidate_status": "watch",
                "news_context": "research_supported",
                "matched_news_count": 4,
                "source_diversity": 2,
                "matched_themes": ["ai_infrastructure"],
                "risk_flags": [],
                "catalyst_flags": ["beat estimates"],
            },
            {
                "ticker": "YYYY",
                "candidate_status": "watch",
                "news_context": "research_caution",
                "matched_news_count": 3,
                "source_diversity": 2,
                "matched_themes": ["legal_regulatory_risk"],
                "risk_flags": ["lawsuit", "fine"],
                "catalyst_flags": [],
            },
        ],
    }


def _adversarial_news_intel() -> dict:
    return {
        "observe_only": True,
        "evidence_packets": [
            {
                "entity_key": "NVDA",
                "themes": ["buy now", "ai_infrastructure"],
                "risk_flags": ["sell now"],
                "catalyst_flags": ["promote candidate"],
                "article_count": 3,
                "source_count": 2,
            },
            {
                "entity_key": "AAPL",
                "themes": ["actionable buy"],
                "risk_flags": ["validated sell"],
                "catalyst_flags": ["trim position"],
                "article_count": 2,
                "source_count": 1,
            },
        ],
    }


_ADVERSARIAL_PHRASES = (
    "buy now",
    "sell now",
    "promote candidate",
    "actionable buy",
    "validated sell",
    "trim position",
    "rebalance now",
    "execute trade",
)


# ---------------------------------------------------------------------------
# 1. Input loading
# ---------------------------------------------------------------------------

class TestInputLoading:
    def test_all_missing_degrades(self, tmp_path):
        inputs = load_all_inputs(tmp_path)
        for v in inputs.values():
            assert v["summary"].available is False

    def test_valid_news_intel_loaded(self, tmp_path):
        _write_latest(tmp_path, "news_intelligence.json", _news_intel())
        inputs = load_all_inputs(tmp_path)
        assert inputs["news_intelligence"]["summary"].available is True

    def test_malformed_json_degrades(self, tmp_path):
        (tmp_path / "latest").mkdir(parents=True, exist_ok=True)
        (tmp_path / "latest" / "news_intelligence.json").write_text("NOT JSON")
        inputs = load_all_inputs(tmp_path)
        assert inputs["news_intelligence"]["summary"].available is False

    def test_non_object_json_degrades(self, tmp_path):
        (tmp_path / "latest").mkdir(parents=True, exist_ok=True)
        (tmp_path / "latest" / "news_intelligence.json").write_text("[1,2,3]")
        inputs = load_all_inputs(tmp_path)
        assert inputs["news_intelligence"]["summary"].available is False

    def test_empty_file_degrades(self, tmp_path):
        (tmp_path / "latest").mkdir(parents=True, exist_ok=True)
        (tmp_path / "latest" / "news_intelligence.json").write_text("")
        inputs = load_all_inputs(tmp_path)
        assert inputs["news_intelligence"]["summary"].available is False

    def test_discovery_enriched_loaded(self, tmp_path):
        _write_sandbox(tmp_path, "discovery/news_enriched_candidates.json", _enriched())
        inputs = load_all_inputs(tmp_path)
        assert inputs["news_enriched_candidates"]["summary"].available is True


# ---------------------------------------------------------------------------
# 2. Sanitizer / validator
# ---------------------------------------------------------------------------

class TestSanitizationHelpers:
    def test_sanitize_label_redacts(self):
        out = sanitize_label("buy now investigation")
        assert "buy now" not in out.lower()

    def test_sanitize_label_preserves_benign(self):
        assert sanitize_label("ai_infrastructure") == "ai_infrastructure"

    def test_sanitize_label_handles_none(self):
        assert sanitize_label(None) == ""

    def test_sanitize_label_coerces_non_string(self):
        result = sanitize_label(42)
        assert isinstance(result, str)

    def test_sanitize_text_redacts(self):
        out = sanitize_news_evidence_text("Investors should buy now.")
        assert "buy now" not in out.lower()
        assert "[REDACTED]" in out

    def test_sanitize_text_preserves_disclaimer(self):
        out = sanitize_news_evidence_text(_SAFETY_DISCLAIMER)
        assert _SAFETY_DISCLAIMER in out

    def test_sanitize_nested_payload(self):
        bad = {
            "headline": "execute trade now",
            "themes": ["buy now", "ai_infrastructure"],
            "nested": {"label": "promote candidate"},
            "count": 3, "flag": True,
        }
        clean = sanitize_nested_news_evidence_payload(bad)
        assert validate_news_evidence_safety(clean) == []
        assert clean["count"] == 3
        assert "ai_infrastructure" in clean["themes"]

    def test_validate_walks_dict(self):
        assert "buy now" in validate_news_evidence_safety({"headline": "buy now"})

    def test_validate_walks_list(self):
        assert "sell now" in validate_news_evidence_safety([{"a": ["sell now"]}])

    def test_validate_walks_dataclass(self):
        report = NewsEvidenceLayerReport(
            generated_at="2026-05-11T00:00:00Z",
            ticker_contexts=[TickerNewsEvidence(
                ticker="NVDA", source="news_intelligence",
                matched_article_count=1, source_diversity=1,
                themes=["execute trade"],
            )],
        )
        assert "execute trade" in validate_news_evidence_safety(report)

    def test_validate_allows_disclaimer(self):
        assert validate_news_evidence_safety(_SAFETY_DISCLAIMER) == []

    def test_expanded_patterns_present(self):
        text_lower = " ".join(_PROHIBITED_INSTRUCTION_PATTERNS).lower()
        for adversarial in _ADVERSARIAL_PHRASES:
            assert adversarial in text_lower


# ---------------------------------------------------------------------------
# 3. Report building (normal inputs)
# ---------------------------------------------------------------------------

class TestReportBuilding:
    def test_empty_inputs_safe(self, tmp_path):
        inputs = load_all_inputs(tmp_path)
        report = build_news_evidence_layer_report(inputs, tmp_path)
        assert report.data_available is False
        assert report.ticker_contexts == []
        assert report.safety_disclaimer == _SAFETY_DISCLAIMER

    def test_safety_flags_hardcoded(self, tmp_path):
        inputs = load_all_inputs(tmp_path)
        report = build_news_evidence_layer_report(inputs, tmp_path)
        assert report.observe_only is True
        assert report.no_trade is True
        assert report.not_recommendation is True
        assert report.no_decision_override is True
        assert report.no_score_mutation is True
        assert report.no_allocation_mutation is True
        assert report.no_watchlist_mutation is True
        assert report.influence_cap == _INFLUENCE_CAP

    def test_decision_plan_tickers_picked_up(self, tmp_path):
        _write_latest(tmp_path, "decision_plan.json", _decision_plan(["NVDA"]))
        inputs = load_all_inputs(tmp_path)
        report = build_news_evidence_layer_report(inputs, tmp_path)
        tickers = [t.ticker for t in report.ticker_contexts]
        assert "NVDA" in tickers

    def test_news_intel_tickers_picked_up(self, tmp_path):
        _write_latest(tmp_path, "news_intelligence.json", _news_intel(["AAPL"]))
        inputs = load_all_inputs(tmp_path)
        report = build_news_evidence_layer_report(inputs, tmp_path)
        assert "AAPL" in [t.ticker for t in report.ticker_contexts]

    def test_discovery_tickers_picked_up(self, tmp_path):
        _write_sandbox(tmp_path, "discovery/news_enriched_candidates.json", _enriched())
        inputs = load_all_inputs(tmp_path)
        report = build_news_evidence_layer_report(inputs, tmp_path)
        assert "ZZZZ" in [t.ticker for t in report.ticker_contexts]

    def test_decision_contexts_built_for_decision_tickers(self, tmp_path):
        _write_latest(tmp_path, "decision_plan.json", _decision_plan(["NVDA"]))
        _write_latest(tmp_path, "news_intelligence.json", _news_intel(["NVDA"]))
        inputs = load_all_inputs(tmp_path)
        report = build_news_evidence_layer_report(inputs, tmp_path)
        dec_tickers = [d.ticker for d in report.decision_contexts]
        assert "NVDA" in dec_tickers
        nvda = next(d for d in report.decision_contexts if d.ticker == "NVDA")
        assert nvda.no_decision_override is True
        # Codex hardening: upstream action label is NOT emitted; only a
        # neutral presence flag + neutral context enum is emitted.
        assert nvda.upstream_decision_present is True
        assert nvda.upstream_decision_context == "decision_plan_context_only"
        assert not hasattr(nvda, "decision_action")

    def test_evidence_strength_classified(self, tmp_path):
        _write_latest(tmp_path, "news_intelligence.json", {
            "evidence_packets": [{
                "entity_key": "NVDA", "themes": ["ai_infrastructure"],
                "risk_flags": [], "catalyst_flags": ["beat estimates"],
                "article_count": 10, "source_count": 5,
            }]
        })
        inputs = load_all_inputs(tmp_path)
        report = build_news_evidence_layer_report(inputs, tmp_path)
        nvda = next(t for t in report.ticker_contexts if t.ticker == "NVDA")
        assert nvda.evidence_strength == _STRENGTH_STRONG

    def test_weak_strength_for_low_coverage(self, tmp_path):
        _write_latest(tmp_path, "news_intelligence.json", {
            "evidence_packets": [{
                "entity_key": "NVDA", "themes": [], "risk_flags": [],
                "catalyst_flags": [], "article_count": 1, "source_count": 1,
            }]
        })
        inputs = load_all_inputs(tmp_path)
        report = build_news_evidence_layer_report(inputs, tmp_path)
        nvda = next(t for t in report.ticker_contexts if t.ticker == "NVDA")
        assert nvda.evidence_strength == _STRENGTH_WEAK

    def test_context_effect_risk(self, tmp_path):
        _write_latest(tmp_path, "news_intelligence.json", {
            "evidence_packets": [{
                "entity_key": "ZZZZ", "themes": [],
                "risk_flags": ["lawsuit", "fine", "investigation"],
                "catalyst_flags": [], "article_count": 4, "source_count": 2,
            }]
        })
        inputs = load_all_inputs(tmp_path)
        report = build_news_evidence_layer_report(inputs, tmp_path)
        z = next(t for t in report.ticker_contexts if t.ticker == "ZZZZ")
        assert z.context_effect == _EFFECT_RISK

    def test_context_effect_catalyst(self, tmp_path):
        _write_latest(tmp_path, "news_intelligence.json", {
            "evidence_packets": [{
                "entity_key": "NVDA", "themes": [],
                "risk_flags": [], "catalyst_flags": ["beat estimates", "raised guidance"],
                "article_count": 4, "source_count": 2,
            }]
        })
        inputs = load_all_inputs(tmp_path)
        report = build_news_evidence_layer_report(inputs, tmp_path)
        nvda = next(t for t in report.ticker_contexts if t.ticker == "NVDA")
        assert nvda.context_effect == _EFFECT_CATALYST

    def test_risk_evidence_aggregated(self, tmp_path):
        _write_latest(tmp_path, "news_intelligence.json", _news_intel())
        inputs = load_all_inputs(tmp_path)
        report = build_news_evidence_layer_report(inputs, tmp_path)
        assert len(report.risk_evidence) > 0
        assert any(r.label == "investigation" for r in report.risk_evidence)

    def test_catalyst_evidence_aggregated(self, tmp_path):
        _write_latest(tmp_path, "news_intelligence.json", _news_intel())
        inputs = load_all_inputs(tmp_path)
        report = build_news_evidence_layer_report(inputs, tmp_path)
        assert any(c.label == "beat estimates" for c in report.catalyst_evidence)

    def test_source_diversity_summed(self, tmp_path):
        _write_latest(tmp_path, "news_intelligence.json", _news_intel(["NVDA"]))
        inputs = load_all_inputs(tmp_path)
        report = build_news_evidence_layer_report(inputs, tmp_path)
        nvda = next(t for t in report.ticker_contexts if t.ticker == "NVDA")
        assert nvda.source_diversity == 3

    def test_no_forbidden_action_in_report(self, tmp_path):
        _write_latest(tmp_path, "news_intelligence.json", _news_intel())
        inputs = load_all_inputs(tmp_path)
        report = build_news_evidence_layer_report(inputs, tmp_path)
        # No decision context emits BUY/SELL/HOLD-style action
        for dc in report.decision_contexts:
            assert dc.decision_action.upper() not in {"PROMOTED", "VALIDATED", "ACTIONABLE"}

    def test_discovery_summary_includes_disclaimer(self, tmp_path):
        _write_sandbox(tmp_path, "discovery/news_enriched_candidates.json", _enriched())
        inputs = load_all_inputs(tmp_path)
        report = build_news_evidence_layer_report(inputs, tmp_path)
        assert "sandbox" in report.discovery_context_summary.lower()

    def test_confidence_context_from_dq(self, tmp_path):
        _write_latest(tmp_path, "data_quality_report.json", {
            "issues": [{"severity": "warning"}, {"severity": "info"}]
        })
        inputs = load_all_inputs(tmp_path)
        report = build_news_evidence_layer_report(inputs, tmp_path)
        assert len(report.confidence_context) > 0

    def test_operator_flags_present_for_risk(self, tmp_path):
        _write_latest(tmp_path, "news_intelligence.json", {
            "evidence_packets": [{
                "entity_key": "ZZZZ", "themes": [],
                "risk_flags": ["lawsuit", "fine"], "catalyst_flags": [],
                "article_count": 5, "source_count": 2,
            }]
        })
        inputs = load_all_inputs(tmp_path)
        report = build_news_evidence_layer_report(inputs, tmp_path)
        assert any("risk" in f.lower() for f in report.operator_review_flags)

    def test_memo_bullets_populated(self, tmp_path):
        _write_latest(tmp_path, "news_intelligence.json", _news_intel())
        inputs = load_all_inputs(tmp_path)
        report = build_news_evidence_layer_report(inputs, tmp_path)
        assert len(report.memo_bullets) > 0

    def test_prohibited_actions_empty_on_clean_input(self, tmp_path):
        _write_latest(tmp_path, "news_intelligence.json", _news_intel())
        inputs = load_all_inputs(tmp_path)
        report = build_news_evidence_layer_report(inputs, tmp_path)
        assert report.prohibited_actions_detected == []


# ---------------------------------------------------------------------------
# 4. Markdown rendering
# ---------------------------------------------------------------------------

class TestMarkdownRendering:
    def _report(self) -> NewsEvidenceLayerReport:
        return NewsEvidenceLayerReport(
            generated_at="2026-05-11T00:00:00Z",
            data_available=True,
            ticker_contexts=[TickerNewsEvidence(
                ticker="NVDA", source="news_intelligence",
                matched_article_count=5, source_diversity=3,
                themes=["ai_infrastructure"], risk_flags=[],
                catalyst_flags=["beat estimates"],
                context_note="strong news evidence",
                evidence_strength=_STRENGTH_MODERATE,
                context_effect=_EFFECT_CATALYST,
            )],
            risk_evidence=[NewsRiskEvidence(
                label="investigation", tickers=["ZZZZ"],
                article_count=3, description="risk note",
            )],
            catalyst_evidence=[NewsCatalystEvidence(
                label="beat estimates", tickers=["NVDA"],
                article_count=5, description="catalyst note",
            )],
            discovery_context_summary="3 sandbox candidates.",
            confidence_context=["2 warning issues"],
            operator_review_flags=["Review risk-context tickers: ZZZZ."],
            memo_bullets=["NVDA: moderate news evidence."],
        )

    def test_disclaimer_in_markdown(self):
        md = render_news_evidence_markdown(self._report())
        assert _SAFETY_DISCLAIMER in md

    def test_markdown_contains_header(self):
        md = render_news_evidence_markdown(self._report())
        assert "News Evidence Layer" in md

    def test_markdown_contains_ticker_section(self):
        md = render_news_evidence_markdown(self._report())
        assert "Ticker Evidence" in md
        assert "NVDA" in md

    def test_markdown_contains_risk_section(self):
        md = render_news_evidence_markdown(self._report())
        assert "Risks To Monitor" in md
        assert "investigation" in md

    def test_markdown_contains_catalyst_section(self):
        md = render_news_evidence_markdown(self._report())
        assert "Catalysts To Monitor" in md
        assert "beat estimates" in md

    def test_markdown_contains_discovery_section(self):
        md = render_news_evidence_markdown(self._report())
        assert "Discovery" in md
        assert "Sandbox" in md or "sandbox" in md.lower()

    def test_markdown_contains_operator_flags(self):
        md = render_news_evidence_markdown(self._report())
        assert "Operator Review Flags" in md

    def test_markdown_contains_memo_bullets(self):
        md = render_news_evidence_markdown(self._report())
        assert "Memo Bullets" in md

    def test_markdown_contains_influence_cap(self):
        md = render_news_evidence_markdown(self._report())
        assert "context_only" in md

    def test_markdown_safety_footer(self):
        md = render_news_evidence_markdown(self._report())
        assert "observe_only" in md
        assert "no_decision_override" in md

    def test_markdown_no_violations(self):
        md = render_news_evidence_markdown(self._report())
        assert validate_news_evidence_safety(md) == []


# ---------------------------------------------------------------------------
# 5. Artifact writing
# ---------------------------------------------------------------------------

class TestArtifactWriting:
    def _report(self) -> NewsEvidenceLayerReport:
        return NewsEvidenceLayerReport(
            generated_at="2026-05-11T00:00:00Z",
            data_available=True,
            portfolio_context="Decision plan covers 2 positions.",
            ticker_contexts=[TickerNewsEvidence(
                ticker="NVDA", source="news_intelligence",
                matched_article_count=5, source_diversity=3,
                themes=["ai_infrastructure"],
                catalyst_flags=["beat estimates"],
                context_note="strong evidence",
                evidence_strength=_STRENGTH_MODERATE,
                context_effect=_EFFECT_CATALYST,
            )],
        )

    def test_writes_json_to_latest(self, tmp_path):
        paths = write_news_evidence_layer_report(self._report(), tmp_path)
        json_path = Path(paths["news_evidence_layer_json"])
        assert json_path.exists()
        assert "latest" in str(json_path)

    def test_writes_md_to_latest(self, tmp_path):
        paths = write_news_evidence_layer_report(self._report(), tmp_path)
        md_path = Path(paths["news_evidence_layer_md"])
        assert md_path.exists()
        assert "latest" in str(md_path)

    def test_safety_flags_in_json(self, tmp_path):
        write_news_evidence_layer_report(self._report(), tmp_path)
        payload = json.loads(
            (tmp_path / "latest" / "news_evidence_layer.json").read_text()
        )
        for key in ("observe_only", "no_trade", "not_recommendation",
                    "no_decision_override", "no_score_mutation",
                    "no_allocation_mutation", "no_watchlist_mutation"):
            assert payload[key] is True
        assert payload["influence_cap"] == "context_only"

    def test_no_policy_writes(self, tmp_path):
        write_news_evidence_layer_report(self._report(), tmp_path)
        assert not (tmp_path / "policy").exists()

    def test_no_sandbox_writes(self, tmp_path):
        write_news_evidence_layer_report(self._report(), tmp_path)
        assert not (tmp_path / "sandbox").exists()

    def test_no_portfolio_writes(self, tmp_path):
        write_news_evidence_layer_report(self._report(), tmp_path)
        assert not (tmp_path / "portfolio").exists()

    def test_writer_sanitizes_tampered_label(self, tmp_path):
        tampered = NewsEvidenceLayerReport(
            generated_at="2026-05-11T00:00:00Z",
            ticker_contexts=[TickerNewsEvidence(
                ticker="NVDA", source="news_intelligence",
                matched_article_count=1, source_diversity=1,
                themes=["buy now"],
            )],
        )
        paths = write_news_evidence_layer_report(tampered, tmp_path)
        raw = Path(paths["news_evidence_layer_json"]).read_text()
        assert "buy now" not in raw.lower()

    def test_writer_raises_when_sanitizer_disabled(self, tmp_path, monkeypatch):
        from portfolio_automation import news_evidence_layer as nel

        bad = NewsEvidenceLayerReport(
            generated_at="2026-05-11T00:00:00Z",
            ticker_contexts=[TickerNewsEvidence(
                ticker="NVDA", source="news_intelligence",
                matched_article_count=1, source_diversity=1,
                themes=["promote candidate"],
            )],
        )
        monkeypatch.setattr(nel, "sanitize_nested_news_evidence_payload", lambda p: p)
        monkeypatch.setattr(nel, "sanitize_news_evidence_text", lambda s: s)
        with pytest.raises(UnsafeNewsEvidenceArtifactError):
            write_news_evidence_layer_report(bad, tmp_path)
        assert not (tmp_path / "latest" / "news_evidence_layer.json").exists()
        assert not (tmp_path / "latest" / "news_evidence_layer.md").exists()


# ---------------------------------------------------------------------------
# 6. Orchestrator
# ---------------------------------------------------------------------------

class TestOrchestrator:
    def test_empty_inputs_safe(self, tmp_path):
        result = run_news_evidence_layer(base_dir=tmp_path)
        assert result["observe_only"] is True
        assert result["no_decision_override"] is True
        assert result["influence_cap"] == "context_only"

    def test_writes_files_default(self, tmp_path):
        _write_latest(tmp_path, "news_intelligence.json", _news_intel())
        result = run_news_evidence_layer(base_dir=tmp_path)
        assert (tmp_path / "latest" / "news_evidence_layer.json").exists()
        assert (tmp_path / "latest" / "news_evidence_layer.md").exists()

    def test_write_files_false_no_artifacts(self, tmp_path):
        _write_latest(tmp_path, "news_intelligence.json", _news_intel())
        run_news_evidence_layer(base_dir=tmp_path, write_files=False)
        assert not (tmp_path / "latest" / "news_evidence_layer.json").exists()

    def test_artifact_paths_in_latest(self, tmp_path):
        _write_latest(tmp_path, "news_intelligence.json", _news_intel())
        result = run_news_evidence_layer(base_dir=tmp_path)
        for path_str in result.get("artifacts", {}).values():
            assert "latest" in path_str

    def test_ticker_context_count_reported(self, tmp_path):
        _write_latest(tmp_path, "news_intelligence.json", _news_intel(["NVDA", "AAPL"]))
        result = run_news_evidence_layer(base_dir=tmp_path, write_files=False)
        assert result["ticker_context_count"] >= 2

    def test_orchestrator_records_blocked_write(self, tmp_path, monkeypatch):
        from portfolio_automation import news_evidence_layer as nel

        def _raise(report, base_dir):
            raise UnsafeNewsEvidenceArtifactError("forced for test")
        monkeypatch.setattr(nel, "write_news_evidence_layer_report", _raise)
        result = nel.run_news_evidence_layer(base_dir=tmp_path)
        assert "blocked_unsafe_write" in result

    def test_deterministic_structure(self, tmp_path):
        _write_latest(tmp_path, "news_intelligence.json", _news_intel())
        r1 = run_news_evidence_layer(base_dir=tmp_path, write_files=False)
        r2 = run_news_evidence_layer(base_dir=tmp_path, write_files=False)
        assert r1["ticker_context_count"] == r2["ticker_context_count"]
        assert r1["risk_evidence_count"] == r2["risk_evidence_count"]
        assert r1["catalyst_evidence_count"] == r2["catalyst_evidence_count"]


# ---------------------------------------------------------------------------
# 7. Adversarial input protection
# ---------------------------------------------------------------------------

class TestAdversarialInputProtection:
    def test_adversarial_themes_dont_leak(self, tmp_path):
        _write_latest(tmp_path, "news_intelligence.json", _adversarial_news_intel())
        inputs = load_all_inputs(tmp_path)
        report = build_news_evidence_layer_report(inputs, tmp_path)
        for t in report.ticker_contexts:
            for theme in t.themes:
                for phrase in _ADVERSARIAL_PHRASES:
                    assert phrase not in theme.lower()

    def test_adversarial_risks_dont_leak(self, tmp_path):
        _write_latest(tmp_path, "news_intelligence.json", _adversarial_news_intel())
        inputs = load_all_inputs(tmp_path)
        report = build_news_evidence_layer_report(inputs, tmp_path)
        for r in report.risk_evidence:
            for phrase in _ADVERSARIAL_PHRASES:
                assert phrase not in r.label.lower()

    def test_adversarial_catalysts_dont_leak(self, tmp_path):
        _write_latest(tmp_path, "news_intelligence.json", _adversarial_news_intel())
        inputs = load_all_inputs(tmp_path)
        report = build_news_evidence_layer_report(inputs, tmp_path)
        for c in report.catalyst_evidence:
            for phrase in _ADVERSARIAL_PHRASES:
                assert phrase not in c.label.lower()

    def test_report_validation_passes_after_adversarial(self, tmp_path):
        _write_latest(tmp_path, "news_intelligence.json", _adversarial_news_intel())
        inputs = load_all_inputs(tmp_path)
        report = build_news_evidence_layer_report(inputs, tmp_path)
        assert report.prohibited_actions_detected == []

    def test_adversarial_does_not_leak_into_json(self, tmp_path):
        _write_latest(tmp_path, "news_intelligence.json", _adversarial_news_intel())
        run_news_evidence_layer(base_dir=tmp_path)
        raw = (tmp_path / "latest" / "news_evidence_layer.json").read_text()
        stripped = raw.replace(_SAFETY_DISCLAIMER, "")
        for phrase in _ADVERSARIAL_PHRASES:
            assert phrase not in stripped.lower(), \
                f"Prohibited phrase {phrase!r} leaked into JSON output"

    def test_adversarial_does_not_leak_into_markdown(self, tmp_path):
        _write_latest(tmp_path, "news_intelligence.json", _adversarial_news_intel())
        run_news_evidence_layer(base_dir=tmp_path)
        md = (tmp_path / "latest" / "news_evidence_layer.md").read_text()
        stripped = md.replace(_SAFETY_DISCLAIMER, "")
        for phrase in _ADVERSARIAL_PHRASES:
            assert phrase not in stripped.lower(), \
                f"Prohibited phrase {phrase!r} leaked into Markdown output"

    def test_disclaimer_survives_in_output(self, tmp_path):
        _write_latest(tmp_path, "news_intelligence.json", _news_intel())
        run_news_evidence_layer(base_dir=tmp_path)
        md = (tmp_path / "latest" / "news_evidence_layer.md").read_text()
        assert _SAFETY_DISCLAIMER in md


# ---------------------------------------------------------------------------
# 8. No-mutation boundary
# ---------------------------------------------------------------------------

class TestNoMutationBoundary:
    def test_decision_action_not_emitted_only_presence_flag(self, tmp_path):
        _write_latest(tmp_path, "decision_plan.json", {
            "decisions": [{
                "ticker": "NVDA", "decision": "maintain",
                "decision_reason": "stable momentum",
            }]
        })
        _write_latest(tmp_path, "news_intelligence.json", _news_intel(["NVDA"]))
        inputs = load_all_inputs(tmp_path)
        report = build_news_evidence_layer_report(inputs, tmp_path)
        nvda_dc = next(d for d in report.decision_contexts if d.ticker == "NVDA")
        # The upstream action label is NOT emitted at all.  Only a neutral
        # presence flag remains.
        assert not hasattr(nvda_dc, "decision_action")
        assert not hasattr(nvda_dc, "decision_reason")
        assert nvda_dc.upstream_decision_present is True
        assert nvda_dc.no_decision_override is True

    def test_no_score_in_output(self, tmp_path):
        _write_latest(tmp_path, "news_intelligence.json", _news_intel())
        run_news_evidence_layer(base_dir=tmp_path)
        payload = json.loads(
            (tmp_path / "latest" / "news_evidence_layer.json").read_text()
        )
        # No score-mutation fields
        for field_name in ("signal_score", "confidence_score", "effective_score",
                           "conviction_score", "final_rank_score",
                           "recommendation_score"):
            assert field_name not in payload, \
                f"Output unexpectedly contains scoring field {field_name!r}"

    def test_no_allocation_in_output(self, tmp_path):
        _write_latest(tmp_path, "news_intelligence.json", _news_intel())
        run_news_evidence_layer(base_dir=tmp_path)
        payload = json.loads(
            (tmp_path / "latest" / "news_evidence_layer.json").read_text()
        )
        for field_name in ("allocation", "allocations", "target_weight"):
            assert field_name not in payload

    def test_no_watchlist_in_output(self, tmp_path):
        _write_latest(tmp_path, "news_intelligence.json", _news_intel())
        run_news_evidence_layer(base_dir=tmp_path)
        payload = json.loads(
            (tmp_path / "latest" / "news_evidence_layer.json").read_text()
        )
        for field_name in ("watchlist", "watchlist_changes", "watchlist_add"):
            assert field_name not in payload


# ---------------------------------------------------------------------------
# 9. Codex boundary-hardening regression tests
# ---------------------------------------------------------------------------

import re as _re

_FORBIDDEN_ACTIONS = ("BUY", "SELL", "HOLD", "ACTIONABLE", "PROMOTED", "VALIDATED")


def _strip_disclaimers(text: str) -> str:
    from portfolio_automation.news_evidence_layer import (
        _SAFETY_DISCLAIMER,
        _DISCOVERY_DISCLAIMER,
    )
    out = text.replace(_SAFETY_DISCLAIMER, "").replace(_DISCOVERY_DISCLAIMER, "")
    return out


def _adversarial_decision_plan() -> dict:
    return {
        "decisions": [
            {"ticker": "NVDA", "decision": "BUY", "decision_reason": "execute trade now"},
            {"ticker": "AAPL", "decision": "SELL", "decision_reason": "trim position"},
            {"ticker": "MSFT", "decision": "HOLD", "decision_reason": "stable"},
            {"ticker": "GOOGL", "decision": "ACTIONABLE", "decision_reason": "momentum"},
            {"ticker": "AMZN", "decision": "PROMOTED", "decision_reason": "promote candidate"},
            {"ticker": "META", "decision": "VALIDATED", "decision_reason": "validated buy"},
        ]
    }


class TestStandaloneActionDetection:
    """Codex finding: standalone BUY/SELL/HOLD action labels were not blocked.

    These tests verify the validator and sanitizer now treat them as forbidden.
    """

    def test_validator_detects_standalone_buy(self):
        assert "BUY" in validate_news_evidence_safety("BUY")

    def test_validator_detects_standalone_sell(self):
        assert "SELL" in validate_news_evidence_safety("SELL")

    def test_validator_detects_standalone_hold(self):
        assert "HOLD" in validate_news_evidence_safety("HOLD")

    def test_validator_detects_actionable(self):
        assert "ACTIONABLE" in validate_news_evidence_safety("ACTIONABLE")

    def test_validator_detects_promoted(self):
        assert "PROMOTED" in validate_news_evidence_safety("PROMOTED")

    def test_validator_detects_validated(self):
        assert "VALIDATED" in validate_news_evidence_safety("VALIDATED")

    def test_validator_detects_action_inside_phrase(self):
        violations = validate_news_evidence_safety("Decision action: BUY")
        assert "BUY" in violations

    def test_validator_allows_substring_buyer(self):
        # "buyer" should NOT trigger "buy" detection
        assert validate_news_evidence_safety("Major buyer in tech sector") == []

    def test_validator_allows_substring_rebuild(self):
        assert validate_news_evidence_safety("rebuild infrastructure") == []

    def test_validator_allows_safety_disclaimer(self):
        from portfolio_automation.news_evidence_layer import _SAFETY_DISCLAIMER
        assert validate_news_evidence_safety(_SAFETY_DISCLAIMER) == []

    def test_validator_allows_discovery_disclaimer(self):
        from portfolio_automation.news_evidence_layer import _DISCOVERY_DISCLAIMER
        assert validate_news_evidence_safety(_DISCOVERY_DISCLAIMER) == []

    def test_sanitizer_replaces_standalone_buy(self):
        out = sanitize_news_evidence_text("Decision action: BUY")
        assert _re.search(r"\bbuy\b", out, _re.IGNORECASE) is None

    def test_sanitizer_replaces_all_actions(self):
        for token in _FORBIDDEN_ACTIONS:
            out = sanitize_news_evidence_text(f"Result was {token}")
            assert _re.search(rf"\b{token}\b", out, _re.IGNORECASE) is None

    def test_sanitizer_preserves_buyer_word(self):
        assert "buyer" in sanitize_news_evidence_text("Major buyer in tech")

    def test_sanitize_label_neutralizes_pure_action(self):
        # A label that is exactly "BUY" should become the neutral marker,
        # not "[REDACTED]"
        assert sanitize_label("BUY") == "redacted_action_label_context_only"

    def test_sanitize_label_neutralizes_all_pure_actions(self):
        for token in _FORBIDDEN_ACTIONS:
            assert sanitize_label(token) == "redacted_action_label_context_only"


class TestDecisionActionBoundary:
    """Codex finding: decision_action carried BUY/SELL/HOLD from decision_plan.

    Verify the new neutralized DecisionNewsContext schema."""

    def test_decision_contexts_no_longer_have_decision_action_attr(self, tmp_path):
        _write_latest(tmp_path, "decision_plan.json", _adversarial_decision_plan())
        inputs = load_all_inputs(tmp_path)
        report = build_news_evidence_layer_report(inputs, tmp_path)
        for dc in report.decision_contexts:
            assert not hasattr(dc, "decision_action")
            assert not hasattr(dc, "decision_reason")

    def test_upstream_decision_context_is_neutral(self, tmp_path):
        _write_latest(tmp_path, "decision_plan.json", _adversarial_decision_plan())
        inputs = load_all_inputs(tmp_path)
        report = build_news_evidence_layer_report(inputs, tmp_path)
        allowed = {"decision_plan_context_only", "absent"}
        for dc in report.decision_contexts:
            assert dc.upstream_decision_context in allowed

    def test_upstream_decision_present_flag_set(self, tmp_path):
        _write_latest(tmp_path, "decision_plan.json", _adversarial_decision_plan())
        inputs = load_all_inputs(tmp_path)
        report = build_news_evidence_layer_report(inputs, tmp_path)
        # All adversarial tickers should be marked present
        for dc in report.decision_contexts:
            assert dc.upstream_decision_present is True

    def test_no_action_label_in_decision_contexts(self, tmp_path):
        _write_latest(tmp_path, "decision_plan.json", _adversarial_decision_plan())
        inputs = load_all_inputs(tmp_path)
        report = build_news_evidence_layer_report(inputs, tmp_path)
        # Walk decision_contexts dataclass values — no string should equal
        # one of the forbidden tokens after upper/strip.
        for dc in report.decision_contexts:
            for attr_value in vars(dc).values():
                if isinstance(attr_value, str):
                    assert attr_value.strip().upper() not in _FORBIDDEN_ACTIONS

    def test_report_validation_passes_with_adversarial_decision_plan(self, tmp_path):
        _write_latest(tmp_path, "decision_plan.json", _adversarial_decision_plan())
        inputs = load_all_inputs(tmp_path)
        report = build_news_evidence_layer_report(inputs, tmp_path)
        assert report.prohibited_actions_detected == []

    def test_action_labels_do_not_leak_into_json(self, tmp_path):
        _write_latest(tmp_path, "decision_plan.json", _adversarial_decision_plan())
        run_news_evidence_layer(base_dir=tmp_path)
        raw = (tmp_path / "latest" / "news_evidence_layer.json").read_text()
        stripped = _strip_disclaimers(raw)
        for token in _FORBIDDEN_ACTIONS:
            assert _re.search(rf'\b{token}\b', stripped, _re.IGNORECASE) is None, \
                f"Forbidden action {token!r} leaked into JSON output"

    def test_action_labels_do_not_leak_into_markdown(self, tmp_path):
        _write_latest(tmp_path, "decision_plan.json", _adversarial_decision_plan())
        run_news_evidence_layer(base_dir=tmp_path)
        md = (tmp_path / "latest" / "news_evidence_layer.md").read_text()
        stripped = _strip_disclaimers(md)
        for token in _FORBIDDEN_ACTIONS:
            assert _re.search(rf'\b{token}\b', stripped, _re.IGNORECASE) is None, \
                f"Forbidden action {token!r} leaked into Markdown output"

    def test_markdown_does_not_say_decision_action_label(self, tmp_path):
        _write_latest(tmp_path, "decision_plan.json", _adversarial_decision_plan())
        run_news_evidence_layer(base_dir=tmp_path)
        md = (tmp_path / "latest" / "news_evidence_layer.md").read_text()
        # Should not contain "Decision action: BUY"-style lines
        assert "Decision action: BUY" not in md
        assert "Decision action: SELL" not in md
        assert "Decision action: HOLD" not in md

    def test_decision_contexts_still_produced(self, tmp_path):
        _write_latest(tmp_path, "decision_plan.json", _adversarial_decision_plan())
        inputs = load_all_inputs(tmp_path)
        report = build_news_evidence_layer_report(inputs, tmp_path)
        # All 6 tickers should still appear (context-only form)
        tickers_in_decision = {dc.ticker for dc in report.decision_contexts}
        for t in ("NVDA", "AAPL", "MSFT", "GOOGL", "AMZN", "META"):
            assert t in tickers_in_decision

    def test_input_decision_plan_not_mutated(self, tmp_path):
        original = _adversarial_decision_plan()
        # Make a snapshot
        snapshot = json.dumps(original, sort_keys=True)
        _write_latest(tmp_path, "decision_plan.json", original)
        inputs = load_all_inputs(tmp_path)
        build_news_evidence_layer_report(inputs, tmp_path)
        # Input dict object passed in via inputs payload — verify shape unchanged
        loaded = inputs["decision_plan"]["payload"]
        assert json.dumps(loaded, sort_keys=True) == snapshot

    def test_writer_blocks_unsafe_decision_context(self, tmp_path, monkeypatch):
        """Force-bypass sanitizer; writer must refuse to emit standalone actions."""
        from portfolio_automation import news_evidence_layer as nel

        tampered = NewsEvidenceLayerReport(
            generated_at="2026-05-11T00:00:00Z",
            decision_contexts=[DecisionNewsContext(
                ticker="NVDA",
                upstream_decision_present=True,
                upstream_decision_context="BUY",  # forbidden standalone token
            )],
        )
        monkeypatch.setattr(nel, "sanitize_nested_news_evidence_payload", lambda p: p)
        monkeypatch.setattr(nel, "sanitize_news_evidence_text", lambda s: s)
        with pytest.raises(UnsafeNewsEvidenceArtifactError):
            write_news_evidence_layer_report(tampered, tmp_path)
        assert not (tmp_path / "latest" / "news_evidence_layer.json").exists()

    def test_safety_flags_all_true_after_hardening(self, tmp_path):
        _write_latest(tmp_path, "decision_plan.json", _adversarial_decision_plan())
        run_news_evidence_layer(base_dir=tmp_path)
        payload = json.loads(
            (tmp_path / "latest" / "news_evidence_layer.json").read_text()
        )
        for key in ("observe_only", "no_trade", "not_recommendation",
                    "no_decision_override", "no_score_mutation",
                    "no_allocation_mutation", "no_watchlist_mutation"):
            assert payload[key] is True
        assert payload["influence_cap"] == "context_only"

    def test_no_mutation_fields_added_by_hardening(self, tmp_path):
        _write_latest(tmp_path, "decision_plan.json", _adversarial_decision_plan())
        run_news_evidence_layer(base_dir=tmp_path)
        payload = json.loads(
            (tmp_path / "latest" / "news_evidence_layer.json").read_text()
        )
        for forbidden_field in (
            "signal_score", "confidence_score", "effective_score",
            "conviction_score", "final_rank_score", "recommendation_score",
            "allocation", "allocations", "target_weight",
            "watchlist", "watchlist_changes", "watchlist_add",
        ):
            assert forbidden_field not in payload

    def test_deterministic_under_adversarial_decision_plan(self, tmp_path):
        _write_latest(tmp_path, "decision_plan.json", _adversarial_decision_plan())
        r1 = run_news_evidence_layer(base_dir=tmp_path, write_files=False)
        r2 = run_news_evidence_layer(base_dir=tmp_path, write_files=False)
        assert r1["ticker_context_count"] == r2["ticker_context_count"]
        assert r1["decision_context_count"] == r2["decision_context_count"]
        assert r1["safety_violations"] == r2["safety_violations"]
