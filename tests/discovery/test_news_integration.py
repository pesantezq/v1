"""
Tests for portfolio_automation/discovery/news_integration.py

Coverage:
  - missing inputs degrade safely
  - malformed JSON handled gracefully
  - normal enrichment produces correct structure
  - ticker matching by entity_key
  - ticker matching by related_tickers
  - theme matching carries through
  - risk flags aggregated correctly
  - catalyst flags aggregated correctly
  - source diversity calculated
  - news_only tickers added from evidence-only packets
  - forbidden statuses never emitted
  - no official namespace writes
  - run-mode write blocking (DAILY/MANUAL_UPDATE/WEEKLY_REVIEW cannot write sandbox)
  - dry_run suppresses file writes
  - markdown summary contains sandbox-only disclaimer
  - deterministic repeated output
  - observe_only / no_trade / not_recommendation hardcoded
  - news context classification (research_supported, research_caution, research_neutral, no_news)
  - enrichment preserves original candidate fields
  - news-only ticker never gets forbidden status
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from portfolio_automation.discovery.news_integration import (
    load_news_intelligence,
    load_news_candidate_evidence,
    load_emerging_candidates,
    load_rejected_candidates,
    match_evidence_to_candidates,
    enrich_candidates,
    build_integration_summary,
    write_news_integration_artifacts,
    run_discovery_news_integration,
    _OBSERVE_ONLY,
    _NO_TRADE,
    _NOT_RECOMMENDATION,
    _DISCOVERY_ONLY,
    _SOURCE_LABEL,
    _DISCLAIMER,
    _FORBIDDEN_STATUSES,
    _classify_news_context,
)
from portfolio_automation.run_mode_governance import RunMode, RunModeViolation


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _make_evidence_packet(
    ticker="NVDA",
    article_count=5,
    source_count=3,
    themes=None,
    risk_flags=None,
    catalyst_flags=None,
    evidence_lane="sandbox_discovery_research",
    article_refs=None,
) -> dict:
    return {
        "entity_key": ticker,
        "entity_type": "ticker",
        "related_tickers": [ticker],
        "article_count": article_count,
        "source_count": source_count,
        "latest_published_at": "2026-05-10T09:00:00Z",
        "themes": themes or ["ai_infrastructure", "semiconductors"],
        "risk_flags": risk_flags or [],
        "catalyst_flags": catalyst_flags or ["beat estimates", "raised guidance"],
        "sentiment_hint": "positive",
        "article_refs": article_refs or [
            {"title": f"{ticker} earnings beat", "url": "", "published_at": "", "source": "Reuters"}
        ],
        "summary_bullets": [f"{ticker} earnings beat"],
        "evidence_lane": evidence_lane,
        "observe_only": True,
        "no_trade": True,
        "not_recommendation": True,
    }


def _make_candidate(ticker="NVDA", status="watch", score=0.8) -> dict:
    return {
        "ticker": ticker,
        "status": status,
        "score": score,
        "mention_count": 3,
        "unique_source_count": 2,
        "event_type": "earnings",
        "event_confidence": 0.9,
        "risk_flag": False,
        "rejection_reason": None,
        "discovery_only": True,
        "sandbox_only": True,
        "corroboration_required": True,
        "corroboration_met": False,
        "corroboration_score": 0.3,
        "corroboration_level": "partial",
        "corroboration_sources": [],
        "first_seen": "2026-05-01",
        "last_seen": "2026-05-10",
        "evidence_snippets": [],
    }


def _write_news_intelligence(base: Path, packets: list[dict]) -> None:
    (base / "latest").mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": "2026-05-10T09:00:00Z",
        "observe_only": True,
        "no_trade": True,
        "not_recommendation": True,
        "source": "fmp_news_intelligence_layer",
        "evidence_packets": packets,
    }
    (base / "latest" / "news_intelligence.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def _write_sandbox_json(base: Path, relative: str, payload: dict) -> None:
    path = base / "sandbox" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_emerging_candidates(base: Path, candidates: list[dict]) -> None:
    _write_sandbox_json(base, "discovery/emerging_candidates.json", {
        "generated_at": "2026-05-10T09:00:00Z",
        "observe_only": True,
        "candidates": candidates,
    })


def _write_rejected_candidates(base: Path, candidates: list[dict]) -> None:
    _write_sandbox_json(base, "discovery/rejected_candidates.json", {
        "generated_at": "2026-05-10T09:00:00Z",
        "observe_only": True,
        "candidates": candidates,
    })


# ---------------------------------------------------------------------------
# 1. Input loading — missing / malformed
# ---------------------------------------------------------------------------

class TestInputLoading:
    def test_missing_news_intelligence_returns_empty(self, tmp_path):
        result = load_news_intelligence(tmp_path)
        assert result["available"] is False
        assert result["evidence_packets"] == []

    def test_malformed_news_intelligence_returns_empty(self, tmp_path):
        (tmp_path / "latest").mkdir(parents=True, exist_ok=True)
        (tmp_path / "latest" / "news_intelligence.json").write_text("not json")
        result = load_news_intelligence(tmp_path)
        assert result["available"] is False

    def test_valid_news_intelligence_loaded(self, tmp_path):
        _write_news_intelligence(tmp_path, [_make_evidence_packet()])
        result = load_news_intelligence(tmp_path)
        assert result["available"] is True
        assert len(result["evidence_packets"]) == 1

    def test_missing_news_candidate_evidence_returns_empty(self, tmp_path):
        result = load_news_candidate_evidence(tmp_path)
        assert result["available"] is False
        assert result["evidence_packets"] == []

    def test_malformed_news_candidate_evidence_returns_empty(self, tmp_path):
        _write_sandbox_json(tmp_path, "discovery/news_candidate_evidence.json", [])
        result = load_news_candidate_evidence(tmp_path)
        assert result["available"] is False

    def test_missing_emerging_candidates_returns_empty_list(self, tmp_path):
        result = load_emerging_candidates(tmp_path)
        assert result == []

    def test_malformed_emerging_candidates_returns_empty_list(self, tmp_path):
        _write_sandbox_json(tmp_path, "discovery/emerging_candidates.json", {"candidates": "bad"})
        result = load_emerging_candidates(tmp_path)
        assert result == []

    def test_valid_emerging_candidates_loaded(self, tmp_path):
        _write_emerging_candidates(tmp_path, [_make_candidate()])
        result = load_emerging_candidates(tmp_path)
        assert len(result) == 1
        assert result[0]["ticker"] == "NVDA"

    def test_missing_rejected_candidates_returns_empty_list(self, tmp_path):
        result = load_rejected_candidates(tmp_path)
        assert result == []

    def test_empty_candidates_list(self, tmp_path):
        _write_emerging_candidates(tmp_path, [])
        result = load_emerging_candidates(tmp_path)
        assert result == []

    def test_non_dict_candidates_filtered(self, tmp_path):
        _write_sandbox_json(tmp_path, "discovery/emerging_candidates.json", {
            "candidates": [_make_candidate(), "bad", None, 42]
        })
        result = load_emerging_candidates(tmp_path)
        assert len(result) == 1

    def test_empty_json_file_returns_empty(self, tmp_path):
        (tmp_path / "latest").mkdir(parents=True, exist_ok=True)
        (tmp_path / "latest" / "news_intelligence.json").write_text("")
        result = load_news_intelligence(tmp_path)
        assert result["available"] is False


# ---------------------------------------------------------------------------
# 2. Evidence matching
# ---------------------------------------------------------------------------

class TestEvidenceMatching:
    def test_matches_by_entity_key(self):
        packets = [_make_evidence_packet("NVDA")]
        candidates = [_make_candidate("NVDA")]
        matched = match_evidence_to_candidates(packets, candidates)
        assert "NVDA" in matched
        assert len(matched["NVDA"]) == 1

    def test_no_match_for_unknown_ticker(self):
        packets = [_make_evidence_packet("NVDA")]
        candidates = [_make_candidate("AAPL")]
        matched = match_evidence_to_candidates(packets, candidates)
        assert matched.get("AAPL", []) == []

    def test_matches_by_related_tickers(self):
        packet = _make_evidence_packet("NVDA")
        packet["related_tickers"] = ["NVDA", "AMD"]
        candidates = [_make_candidate("AMD")]
        matched = match_evidence_to_candidates([packet], candidates)
        assert len(matched.get("AMD", [])) == 1

    def test_case_insensitive_matching(self):
        packets = [_make_evidence_packet("nvda")]
        candidates = [_make_candidate("NVDA")]
        matched = match_evidence_to_candidates(packets, candidates)
        assert len(matched.get("NVDA", [])) >= 1

    def test_empty_packets_returns_empty_matches(self):
        candidates = [_make_candidate("NVDA")]
        matched = match_evidence_to_candidates([], candidates)
        assert matched.get("NVDA", []) == []

    def test_empty_candidates_returns_empty_dict(self):
        packets = [_make_evidence_packet("NVDA")]
        matched = match_evidence_to_candidates(packets, [])
        assert matched == {}

    def test_multiple_packets_same_ticker(self):
        packets = [_make_evidence_packet("NVDA"), _make_evidence_packet("NVDA")]
        candidates = [_make_candidate("NVDA")]
        matched = match_evidence_to_candidates(packets, candidates)
        assert len(matched["NVDA"]) == 2

    def test_non_dict_packets_skipped(self):
        candidates = [_make_candidate("NVDA")]
        matched = match_evidence_to_candidates(["bad", None], candidates)
        assert matched.get("NVDA", []) == []

    def test_non_dict_candidates_skipped(self):
        packets = [_make_evidence_packet("NVDA")]
        matched = match_evidence_to_candidates(packets, ["bad", None])
        assert matched == {}


# ---------------------------------------------------------------------------
# 3. Enrichment
# ---------------------------------------------------------------------------

class TestEnrichment:
    def test_basic_enrichment(self):
        candidates = [_make_candidate("NVDA")]
        packets = [_make_evidence_packet("NVDA")]
        matched = match_evidence_to_candidates(packets, candidates)
        enriched = enrich_candidates(candidates, matched, packets)
        assert len(enriched) >= 1
        nvda = next(e for e in enriched if e["ticker"] == "NVDA")
        assert nvda["matched_news_count"] > 0

    def test_observe_only_hardcoded(self):
        candidates = [_make_candidate("NVDA")]
        packets = [_make_evidence_packet("NVDA")]
        matched = match_evidence_to_candidates(packets, candidates)
        enriched = enrich_candidates(candidates, matched, packets)
        for e in enriched:
            assert e["observe_only"] is True

    def test_no_trade_hardcoded(self):
        candidates = [_make_candidate("NVDA")]
        matched = {}
        enriched = enrich_candidates(candidates, matched, [])
        for e in enriched:
            assert e["no_trade"] is True

    def test_not_recommendation_hardcoded(self):
        candidates = [_make_candidate("NVDA")]
        enriched = enrich_candidates(candidates, {}, [])
        for e in enriched:
            assert e["not_recommendation"] is True

    def test_discovery_only_hardcoded(self):
        candidates = [_make_candidate("NVDA")]
        enriched = enrich_candidates(candidates, {}, [])
        for e in enriched:
            assert e["discovery_only"] is True

    def test_no_forbidden_statuses(self):
        candidates = [_make_candidate("NVDA", status="watch")]
        enriched = enrich_candidates(candidates, {}, [])
        for e in enriched:
            cs = e.get("candidate_status", "").upper()
            assert cs not in {s.upper() for s in _FORBIDDEN_STATUSES}

    def test_forbidden_status_in_input_replaced(self):
        bad_cand = _make_candidate("NVDA", status="promoted")
        enriched = enrich_candidates([bad_cand], {}, [])
        for e in enriched:
            if e["ticker"] == "NVDA":
                assert e["candidate_status"].upper() not in {
                    "PROMOTED", "VALIDATED", "ACTIONABLE", "BUY", "SELL"
                }

    def test_risk_flags_aggregated(self):
        packets = [_make_evidence_packet("NVDA", risk_flags=["investigation", "fine"])]
        candidates = [_make_candidate("NVDA")]
        matched = match_evidence_to_candidates(packets, candidates)
        enriched = enrich_candidates(candidates, matched, packets)
        nvda = next(e for e in enriched if e["ticker"] == "NVDA")
        assert "investigation" in nvda["risk_flags"] or "fine" in nvda["risk_flags"]

    def test_catalyst_flags_aggregated(self):
        packets = [_make_evidence_packet("NVDA", catalyst_flags=["beat estimates"])]
        candidates = [_make_candidate("NVDA")]
        matched = match_evidence_to_candidates(packets, candidates)
        enriched = enrich_candidates(candidates, matched, packets)
        nvda = next(e for e in enriched if e["ticker"] == "NVDA")
        assert "beat estimates" in nvda["catalyst_flags"]

    def test_themes_aggregated(self):
        packets = [_make_evidence_packet("NVDA", themes=["ai_infrastructure", "semiconductors"])]
        candidates = [_make_candidate("NVDA")]
        matched = match_evidence_to_candidates(packets, candidates)
        enriched = enrich_candidates(candidates, matched, packets)
        nvda = next(e for e in enriched if e["ticker"] == "NVDA")
        assert "ai_infrastructure" in nvda["matched_themes"]

    def test_source_diversity_summed(self):
        packets = [_make_evidence_packet("NVDA", source_count=3)]
        candidates = [_make_candidate("NVDA")]
        matched = match_evidence_to_candidates(packets, candidates)
        enriched = enrich_candidates(candidates, matched, packets)
        nvda = next(e for e in enriched if e["ticker"] == "NVDA")
        assert nvda["source_diversity"] == 3

    def test_headlines_collected(self):
        packets = [_make_evidence_packet("NVDA", article_refs=[
            {"title": "NVDA beats Q1", "url": "", "published_at": "", "source": "Reuters"},
        ])]
        candidates = [_make_candidate("NVDA")]
        matched = match_evidence_to_candidates(packets, candidates)
        enriched = enrich_candidates(candidates, matched, packets)
        nvda = next(e for e in enriched if e["ticker"] == "NVDA")
        assert len(nvda["latest_news_headlines"]) > 0

    def test_no_news_candidate_enriched_safely(self):
        candidates = [_make_candidate("AAPL")]
        enriched = enrich_candidates(candidates, {}, [])
        aapl = next(e for e in enriched if e["ticker"] == "AAPL")
        assert aapl["matched_news_count"] == 0
        assert aapl["news_context"] == "no_news"

    def test_original_fields_preserved(self):
        candidates = [_make_candidate("NVDA", score=0.75)]
        enriched = enrich_candidates(candidates, {}, [])
        nvda = next(e for e in enriched if e["ticker"] == "NVDA")
        assert nvda["original_score"] == 0.75

    def test_news_only_ticker_added(self):
        # Packet for a ticker not in any candidate
        packets = [_make_evidence_packet("PLTR", evidence_lane="sandbox_discovery_research")]
        candidates = [_make_candidate("NVDA")]
        matched = match_evidence_to_candidates(packets, candidates)
        enriched = enrich_candidates(candidates, matched, packets)
        tickers = {e["ticker"] for e in enriched}
        assert "PLTR" in tickers

    def test_news_only_ticker_status(self):
        packets = [_make_evidence_packet("PLTR", evidence_lane="sandbox_discovery_research")]
        enriched = enrich_candidates([], {}, packets)
        pltr = next((e for e in enriched if e["ticker"] == "PLTR"), None)
        if pltr:
            assert pltr["candidate_status"] == "news_only"
            assert pltr["candidate_status"].upper() not in {
                "PROMOTED", "VALIDATED", "ACTIONABLE", "BUY", "SELL"
            }

    def test_official_monitoring_packet_not_added_as_news_only(self):
        # Packets with official_monitoring lane should not be added as news-only
        packets = [_make_evidence_packet("NVDA", evidence_lane="official_monitoring")]
        enriched = enrich_candidates([], {}, packets)
        # Should be empty since we only include sandbox lane
        assert all(e["ticker"] != "NVDA" for e in enriched)

    def test_no_duplicate_tickers(self):
        packets = [
            _make_evidence_packet("NVDA", evidence_lane="sandbox_discovery_research"),
            _make_evidence_packet("NVDA", evidence_lane="sandbox_discovery_research"),
        ]
        candidates = [_make_candidate("NVDA")]
        matched = match_evidence_to_candidates(packets, candidates)
        enriched = enrich_candidates(candidates, matched, packets)
        nvda_entries = [e for e in enriched if e["ticker"] == "NVDA"]
        assert len(nvda_entries) == 1


# ---------------------------------------------------------------------------
# 4. News context classification
# ---------------------------------------------------------------------------

class TestNewsContextClassification:
    def test_no_news_returns_no_news(self):
        assert _classify_news_context([], [], 0) == "no_news"

    def test_more_catalyst_than_risk_returns_supported(self):
        result = _classify_news_context([], ["beat estimates", "raised guidance"], 2)
        assert result == "research_supported"

    def test_more_risk_than_catalyst_returns_caution(self):
        result = _classify_news_context(
            ["investigation", "fine", "penalty"], [], 3
        )
        assert result == "research_caution"

    def test_equal_risk_and_catalyst_returns_neutral(self):
        result = _classify_news_context(["lawsuit"], ["beat estimates"], 2)
        assert result == "research_neutral"

    def test_one_risk_flag_does_not_trigger_caution(self):
        # Need at least 2 risk flags for "research_caution"
        result = _classify_news_context(["lawsuit"], [], 1)
        assert result != "research_caution"

    def test_no_forbidden_context_values(self):
        for risk, cat, count in [
            ([], [], 0),
            ([], ["beat estimates"], 1),
            (["lawsuit", "fine"], [], 2),
            (["lawsuit"], ["beat estimates"], 2),
        ]:
            result = _classify_news_context(risk, cat, count)
            assert result.upper() not in {"PROMOTED", "VALIDATED", "ACTIONABLE", "BUY", "SELL"}


# ---------------------------------------------------------------------------
# 5. Summary markdown
# ---------------------------------------------------------------------------

class TestSummaryMarkdown:
    def test_disclaimer_in_summary(self):
        md = build_integration_summary([], "discovery", "2026-05-10T00:00:00Z")
        assert "sandbox" in md.lower()
        assert "not a buy/sell" in md.lower() or "not_recommendation" in md.lower()

    def test_summary_contains_generated_at(self):
        md = build_integration_summary([], "discovery", "2026-05-10T00:00:00Z")
        assert "2026-05-10" in md

    def test_research_supported_section_present(self):
        enriched = [{
            "ticker": "NVDA", "candidate_status": "watch",
            "news_context": "research_supported",
            "matched_news_count": 5, "source_diversity": 3,
            "matched_themes": ["ai_infrastructure"], "catalyst_flags": ["beat estimates"],
            "risk_flags": [], "latest_news_headlines": ["NVDA beats Q1"],
            "observe_only": True, "no_trade": True, "not_recommendation": True,
        }]
        md = build_integration_summary(enriched, "discovery", "2026-05-10T00:00:00Z")
        assert "News-Supported" in md
        assert "NVDA" in md

    def test_research_caution_section_present(self):
        enriched = [{
            "ticker": "ZZZZ", "candidate_status": "watch",
            "news_context": "research_caution",
            "matched_news_count": 3, "source_diversity": 2,
            "matched_themes": [], "catalyst_flags": [],
            "risk_flags": ["lawsuit", "fine"], "latest_news_headlines": [],
            "observe_only": True, "no_trade": True, "not_recommendation": True,
        }]
        md = build_integration_summary(enriched, "discovery", "2026-05-10T00:00:00Z")
        assert "Risk-Heavy" in md or "Caution" in md

    def test_news_only_section_present(self):
        enriched = [{
            "ticker": "PLTR", "candidate_status": "news_only",
            "news_context": "research_neutral",
            "matched_news_count": 2, "source_diversity": 1,
            "matched_themes": ["mna"], "catalyst_flags": [],
            "risk_flags": [], "latest_news_headlines": [],
            "observe_only": True, "no_trade": True, "not_recommendation": True,
        }]
        md = build_integration_summary(enriched, "discovery", "2026-05-10T00:00:00Z")
        assert "News-Only" in md or "Corroboration" in md
        assert "PLTR" in md

    def test_no_forbidden_words_in_summary(self):
        enriched = [
            {"ticker": "NVDA", "candidate_status": "watch", "news_context": "research_supported",
             "matched_news_count": 3, "source_diversity": 2, "matched_themes": [],
             "catalyst_flags": [], "risk_flags": [], "latest_news_headlines": [],
             "observe_only": True, "no_trade": True, "not_recommendation": True},
        ]
        md = build_integration_summary(enriched, "discovery", "2026-05-10")
        # These phrases should not appear as recommendations
        assert "BUY" not in md
        assert "SELL" not in md
        assert "PROMOTED" not in md
        assert "ACTIONABLE" not in md


# ---------------------------------------------------------------------------
# 6. Artifact writing and governance
# ---------------------------------------------------------------------------

class TestArtifactWriting:
    def test_writes_to_sandbox_namespace(self, tmp_path):
        enriched = [{"ticker": "NVDA", "candidate_status": "watch",
                     "observe_only": True, "no_trade": True, "not_recommendation": True}]
        summary_md = "# Summary\n\nDisclamer: sandbox only."
        artifacts = write_news_integration_artifacts(
            base_dir=tmp_path,
            enriched=enriched,
            summary_md=summary_md,
            run_mode=RunMode.DISCOVERY,
            run_id="test",
        )
        json_path = Path(artifacts["news_enriched_candidates_json"])
        assert json_path.exists()
        assert "sandbox" in str(json_path)

    def test_writes_markdown_to_sandbox(self, tmp_path):
        artifacts = write_news_integration_artifacts(
            base_dir=tmp_path,
            enriched=[],
            summary_md="# Test\n\nSandbox only.",
            run_mode=RunMode.DISCOVERY,
            run_id="test",
        )
        md_path = Path(artifacts["news_integration_summary_md"])
        assert md_path.exists()
        assert "sandbox" in str(md_path)

    def test_json_has_safety_flags(self, tmp_path):
        write_news_integration_artifacts(
            base_dir=tmp_path, enriched=[], summary_md="x",
            run_mode=RunMode.DISCOVERY, run_id="test",
        )
        path = tmp_path / "sandbox" / "discovery" / "news_enriched_candidates.json"
        payload = json.loads(path.read_text())
        assert payload["observe_only"] is True
        assert payload["no_trade"] is True
        assert payload["not_recommendation"] is True
        assert payload["discovery_only"] is True

    def test_daily_mode_cannot_write_sandbox(self, tmp_path):
        with pytest.raises(RunModeViolation):
            write_news_integration_artifacts(
                base_dir=tmp_path, enriched=[], summary_md="x",
                run_mode=RunMode.DAILY, run_id="test",
            )

    def test_manual_update_mode_cannot_write_sandbox(self, tmp_path):
        with pytest.raises(RunModeViolation):
            write_news_integration_artifacts(
                base_dir=tmp_path, enriched=[], summary_md="x",
                run_mode=RunMode.MANUAL_UPDATE, run_id="test",
            )

    def test_weekly_review_mode_cannot_write_sandbox(self, tmp_path):
        with pytest.raises(RunModeViolation):
            write_news_integration_artifacts(
                base_dir=tmp_path, enriched=[], summary_md="x",
                run_mode=RunMode.WEEKLY_REVIEW, run_id="test",
            )

    def test_backtest_mode_can_write_sandbox(self, tmp_path):
        artifacts = write_news_integration_artifacts(
            base_dir=tmp_path, enriched=[], summary_md="x",
            run_mode=RunMode.BACKTEST, run_id="test",
        )
        assert Path(artifacts["news_enriched_candidates_json"]).exists()

    def test_no_official_namespace_writes(self, tmp_path):
        write_news_integration_artifacts(
            base_dir=tmp_path, enriched=[], summary_md="x",
            run_mode=RunMode.DISCOVERY, run_id="test",
        )
        # Verify nothing written to latest/policy/portfolio
        assert not (tmp_path / "latest" / "news_enriched_candidates.json").exists()
        assert not (tmp_path / "policy" / "news_enriched_candidates.json").exists()
        assert not (tmp_path / "portfolio" / "news_enriched_candidates.json").exists()


# ---------------------------------------------------------------------------
# 7. Orchestrator (run_discovery_news_integration)
# ---------------------------------------------------------------------------

class TestOrchestrator:
    def test_empty_inputs_safe(self, tmp_path):
        result = run_discovery_news_integration(
            base_dir=tmp_path,
            run_mode="discovery",
        )
        assert result.get("observe_only") is True
        assert result.get("no_trade") is True
        assert result.get("not_recommendation") is True

    def test_writes_sandbox_files_in_discovery_mode(self, tmp_path):
        _write_news_intelligence(tmp_path, [_make_evidence_packet()])
        _write_emerging_candidates(tmp_path, [_make_candidate()])
        result = run_discovery_news_integration(base_dir=tmp_path, run_mode="discovery")
        assert result.get("artifacts")
        json_path = Path(result["artifacts"]["news_enriched_candidates_json"])
        assert json_path.exists()

    def test_dry_run_no_files_written(self, tmp_path):
        _write_news_intelligence(tmp_path, [_make_evidence_packet()])
        _write_emerging_candidates(tmp_path, [_make_candidate()])
        result = run_discovery_news_integration(
            base_dir=tmp_path, run_mode="discovery", dry_run=True
        )
        assert result["dry_run"] is True
        assert not (tmp_path / "sandbox" / "discovery" / "news_enriched_candidates.json").exists()

    def test_daily_mode_acts_as_dry_run(self, tmp_path):
        _write_news_intelligence(tmp_path, [_make_evidence_packet()])
        result = run_discovery_news_integration(base_dir=tmp_path, run_mode="daily")
        # Should not raise; dry_run=True because daily cannot write sandbox
        assert result["dry_run"] is True
        assert not (tmp_path / "sandbox" / "discovery" / "news_enriched_candidates.json").exists()

    def test_invalid_run_mode_returns_error(self, tmp_path):
        result = run_discovery_news_integration(base_dir=tmp_path, run_mode="not_a_mode")
        assert "error" in result

    def test_enriched_count_correct(self, tmp_path):
        _write_news_intelligence(tmp_path, [_make_evidence_packet("NVDA")])
        _write_emerging_candidates(tmp_path, [_make_candidate("NVDA"), _make_candidate("AAPL")])
        result = run_discovery_news_integration(base_dir=tmp_path, run_mode="discovery")
        assert result["candidate_count"] == 2

    def test_with_news_count_correct(self, tmp_path):
        _write_news_intelligence(tmp_path, [_make_evidence_packet("NVDA")])
        _write_emerging_candidates(tmp_path, [_make_candidate("NVDA"), _make_candidate("AAPL")])
        result = run_discovery_news_integration(base_dir=tmp_path, run_mode="discovery")
        # NVDA has evidence, AAPL does not
        assert result["with_news_count"] >= 1

    def test_result_includes_artifact_paths(self, tmp_path):
        result = run_discovery_news_integration(base_dir=tmp_path, run_mode="discovery")
        assert "artifacts" in result
        assert isinstance(result["artifacts"], dict)

    def test_backtest_mode_writes_artifacts(self, tmp_path):
        result = run_discovery_news_integration(base_dir=tmp_path, run_mode="backtest")
        assert not result["dry_run"]
        assert (tmp_path / "sandbox" / "discovery" / "news_enriched_candidates.json").exists()

    def test_rejected_candidates_also_enriched(self, tmp_path):
        _write_news_intelligence(tmp_path, [_make_evidence_packet("ZZZZ")])
        _write_rejected_candidates(tmp_path, [_make_candidate("ZZZZ", status="rejected")])
        result = run_discovery_news_integration(base_dir=tmp_path, run_mode="discovery")
        assert result["candidate_count"] >= 1

    def test_malformed_inputs_degrade_safely(self, tmp_path):
        (tmp_path / "latest").mkdir(parents=True, exist_ok=True)
        (tmp_path / "latest" / "news_intelligence.json").write_text("NOT JSON")
        result = run_discovery_news_integration(base_dir=tmp_path, run_mode="discovery")
        assert isinstance(result, dict)
        assert result.get("observe_only") is True

    def test_deterministic_output(self, tmp_path):
        _write_news_intelligence(tmp_path, [_make_evidence_packet("NVDA")])
        _write_emerging_candidates(tmp_path, [_make_candidate("NVDA")])
        r1 = run_discovery_news_integration(
            base_dir=tmp_path, run_mode="discovery", run_id="test-run", dry_run=True
        )
        r2 = run_discovery_news_integration(
            base_dir=tmp_path, run_mode="discovery", run_id="test-run", dry_run=True
        )
        assert r1["enriched_count"] == r2["enriched_count"]
        assert r1["with_news_count"] == r2["with_news_count"]

    def test_no_forbidden_statuses_in_output_json(self, tmp_path):
        _write_news_intelligence(tmp_path, [_make_evidence_packet("NVDA")])
        _write_emerging_candidates(tmp_path, [_make_candidate("NVDA")])
        run_discovery_news_integration(base_dir=tmp_path, run_mode="discovery")
        payload = json.loads(
            (tmp_path / "sandbox" / "discovery" / "news_enriched_candidates.json").read_text()
        )
        payload_str = json.dumps(payload)
        for forbidden in ["PROMOTED", "VALIDATED", "ACTIONABLE"]:
            # These should not appear as status values
            assert f'"candidate_status": "{forbidden}"' not in payload_str
            assert f'"candidate_status": "{forbidden.lower()}"' not in payload_str
