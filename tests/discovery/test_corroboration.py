"""Tests for portfolio_automation.discovery.corroboration."""
import math
import pytest

from portfolio_automation.discovery.corroboration import (
    CORROBORATION_MET_THRESHOLD,
    CorroborationResult,
    compute_corroboration,
)
from portfolio_automation.discovery.candidate_promotion_engine import (
    CandidateStatus,
    evaluate_candidates,
    score_candidate,
)
from portfolio_automation.discovery.discovery_reports import (
    write_discovery_reports,
    _candidate_to_dict,
)
from portfolio_automation.discovery.discovery_memory import DiscoveryMemory
from portfolio_automation.discovery.event_classifier import EventType, ClassificationResult
from portfolio_automation.discovery.news_ticker_discovery import DiscoveredTicker, TickerEvidence


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TS = "2026-05-01T00:00:00+00:00"


def _compute(**kwargs):
    defaults = dict(
        unique_source_count=2,
        mention_count=3,
        event_confidence=0.5,
        risk_flag=False,
        seen_runs=0,
    )
    defaults.update(kwargs)
    return compute_corroboration(**defaults)


def _make_discovered(ticker, mention_count=3, sources=None):
    srcs = sources or ["source_a", "source_b"]
    evidence = [
        TickerEvidence(
            record_index=0,
            source=srcs[0],
            published_at=_TS,
            extraction_method="cashtag",
            context=f"${ticker} context",
        )
    ]
    return DiscoveredTicker(
        ticker=ticker,
        mention_count=mention_count,
        unique_sources=list(set(srcs)),
        evidence=evidence,
    )


def _make_cls(
    event_type=EventType.EARNINGS,
    confidence=0.7,
    risk_flag=False,
):
    return ClassificationResult(
        event_type=event_type,
        confidence=confidence,
        matched_keywords=["earnings"],
        risk_flag=risk_flag,
    )


def _make_candidate_dc(ticker, status=CandidateStatus.DISCOVERED, score=1.0):
    from portfolio_automation.discovery.candidate_promotion_engine import DiscoveryCandidate
    return DiscoveryCandidate(
        ticker=ticker,
        status=status,
        score=score,
        mention_count=2,
        unique_source_count=2,
        event_type=EventType.EARNINGS,
        event_confidence=0.7,
        risk_flag=False,
        rejection_reason=None,
        corroboration_score=0.42,
        corroboration_level="weak",
        first_seen=_TS,
        last_seen=_TS,
    )


# ---------------------------------------------------------------------------
# 1. Source diversity component
# ---------------------------------------------------------------------------

class TestSourceDiversityComponent:
    def test_zero_sources_zero_component(self):
        r = _compute(unique_source_count=0)
        assert r.source_diversity_component == 0.0

    def test_one_source_quarter_of_max(self):
        r = _compute(unique_source_count=1)
        assert abs(r.source_diversity_component - 0.35 * 0.25) < 1e-3

    def test_four_sources_max_component(self):
        r = _compute(unique_source_count=4)
        assert abs(r.source_diversity_component - 0.35) < 1e-4

    def test_source_component_capped_beyond_four(self):
        r4 = _compute(unique_source_count=4)
        r10 = _compute(unique_source_count=10)
        assert r4.source_diversity_component == r10.source_diversity_component


# ---------------------------------------------------------------------------
# 2. Mention component
# ---------------------------------------------------------------------------

class TestMentionComponent:
    def test_zero_mentions_zero_component(self):
        r = _compute(mention_count=0)
        assert r.mention_component == 0.0

    def test_one_mention_nonzero(self):
        r = _compute(mention_count=1)
        expected = (math.log2(2) / 3.0) * 0.20
        assert abs(r.mention_component - expected) < 1e-4

    def test_seven_mentions_near_max(self):
        # log2(8) = 3.0 → saturates cap
        r = _compute(mention_count=7)
        assert abs(r.mention_component - 0.20) < 1e-4

    def test_mention_component_capped(self):
        r7 = _compute(mention_count=7)
        r100 = _compute(mention_count=100)
        assert r7.mention_component == r100.mention_component


# ---------------------------------------------------------------------------
# 3. Event strength component
# ---------------------------------------------------------------------------

class TestEventStrengthComponent:
    def test_zero_confidence_zero_component(self):
        r = _compute(event_confidence=0.0)
        assert r.event_strength_component == 0.0

    def test_full_confidence_max_component(self):
        r = _compute(event_confidence=1.0)
        assert abs(r.event_strength_component - 0.25) < 1e-4

    def test_half_confidence_half_component(self):
        r = _compute(event_confidence=0.5)
        assert abs(r.event_strength_component - 0.125) < 1e-4

    def test_confidence_clamped_above_one(self):
        r1 = _compute(event_confidence=1.0)
        r2 = _compute(event_confidence=2.0)
        assert r1.event_strength_component == r2.event_strength_component

    def test_confidence_clamped_below_zero(self):
        r = _compute(event_confidence=-0.5)
        assert r.event_strength_component == 0.0


# ---------------------------------------------------------------------------
# 4. Persistence component
# ---------------------------------------------------------------------------

class TestPersistenceComponent:
    def test_zero_runs_zero_component(self):
        r = _compute(seen_runs=0)
        assert r.persistence_component == 0.0

    def test_three_runs_max_component(self):
        r = _compute(seen_runs=3)
        assert abs(r.persistence_component - 0.20) < 1e-4

    def test_one_run_partial(self):
        r = _compute(seen_runs=1)
        assert 0.0 < r.persistence_component < 0.20

    def test_persistence_capped_beyond_three(self):
        r3 = _compute(seen_runs=3)
        r10 = _compute(seen_runs=10)
        assert r3.persistence_component == r10.persistence_component


# ---------------------------------------------------------------------------
# 5. Risk penalty
# ---------------------------------------------------------------------------

class TestRiskPenalty:
    def test_no_risk_flag_zero_penalty(self):
        r = _compute(risk_flag=False)
        assert r.risk_penalty_applied == 0.0

    def test_risk_flag_penalty_applied(self):
        r = _compute(risk_flag=True)
        assert r.risk_penalty_applied == 0.20

    def test_risk_flag_lowers_score(self):
        no_risk = _compute(risk_flag=False)
        with_risk = _compute(risk_flag=True)
        assert with_risk.score < no_risk.score

    def test_score_clamped_to_zero_with_severe_risk(self):
        # Only 1 source, 0 confidence, risk_flag — score should be 0.0
        r = _compute(
            unique_source_count=0,
            mention_count=0,
            event_confidence=0.0,
            risk_flag=True,
            seen_runs=0,
        )
        assert r.score == 0.0


# ---------------------------------------------------------------------------
# 6. Score clamping
# ---------------------------------------------------------------------------

class TestScoreClamping:
    def test_score_never_below_zero(self):
        r = _compute(
            unique_source_count=0, mention_count=0,
            event_confidence=0.0, risk_flag=True, seen_runs=0
        )
        assert r.score >= 0.0

    def test_score_never_above_one(self):
        r = _compute(
            unique_source_count=100, mention_count=1000,
            event_confidence=1.0, risk_flag=False, seen_runs=100
        )
        assert r.score <= 1.0


# ---------------------------------------------------------------------------
# 7. Level assignment
# ---------------------------------------------------------------------------

class TestCorroborationLevel:
    def test_level_none_low_score(self):
        r = _compute(
            unique_source_count=0, mention_count=0,
            event_confidence=0.1, risk_flag=False, seen_runs=0
        )
        assert r.level == "none"

    def test_level_weak_range(self):
        # 1 source + 1 mention + 0.3 confidence = ~0.088 + 0.067 + 0.075 = 0.23 → "none"
        # Need to engineer ~0.35–0.49 to hit "weak"
        # 2 sources + 2 mentions + 0.0 confidence = 0.175 + 0.106 + 0 = 0.281 → "none"
        # Let's use: 2 sources (0.175) + 3 mentions (0.133) + 0.1 confidence (0.025) = 0.333 → "weak"
        r = _compute(unique_source_count=2, mention_count=3, event_confidence=0.1, seen_runs=0)
        assert r.level == "weak"

    def test_level_moderate_range(self):
        # 3 sources (0.2625) + 3 mentions (0.133) + 0.4 confidence (0.10) = 0.4955 → "weak"
        # 3 sources (0.2625) + 5 mentions (0.172) + 0.3 confidence (0.075) = 0.5095 → "moderate"
        r = _compute(unique_source_count=3, mention_count=5, event_confidence=0.3, seen_runs=0)
        assert r.level == "moderate"

    def test_level_strong_at_threshold(self):
        # 4 sources (0.35) + 7 mentions (0.20) + 0.5 confidence (0.125) = 0.675 → "strong"
        r = _compute(unique_source_count=4, mention_count=7, event_confidence=0.5, seen_runs=0)
        assert r.level == "strong"
        assert r.corroboration_met is True

    def test_level_strong_with_persistence(self):
        # 3 sources + 5 mentions + 0.75 confidence + 2 runs
        r = _compute(unique_source_count=3, mention_count=5, event_confidence=0.75, seen_runs=2)
        assert r.level == "strong"


# ---------------------------------------------------------------------------
# 8. corroboration_met
# ---------------------------------------------------------------------------

class TestCorroborationMet:
    def test_met_false_below_threshold(self):
        r = _compute(unique_source_count=1, mention_count=1, event_confidence=0.2, seen_runs=0)
        assert r.corroboration_met is False

    def test_met_true_above_threshold(self):
        r = _compute(unique_source_count=4, mention_count=7, event_confidence=1.0, seen_runs=0)
        assert r.corroboration_met is True

    def test_met_aligns_with_strong_level(self):
        r = _compute(unique_source_count=4, mention_count=7, event_confidence=0.5, seen_runs=0)
        assert r.corroboration_met == (r.level == "strong")

    def test_threshold_constant_is_0_65(self):
        assert CORROBORATION_MET_THRESHOLD == 0.65


# ---------------------------------------------------------------------------
# 9. Source names
# ---------------------------------------------------------------------------

class TestSourceNames:
    def test_source_names_populated(self):
        r = compute_corroboration(
            unique_source_count=2,
            mention_count=2,
            event_confidence=0.5,
            risk_flag=False,
            source_names=["reuters", "bloomberg"],
        )
        assert "reuters" in r.corroboration_sources
        assert "bloomberg" in r.corroboration_sources

    def test_source_names_none_gives_empty_list(self):
        r = compute_corroboration(
            unique_source_count=2,
            mention_count=2,
            event_confidence=0.5,
            risk_flag=False,
            source_names=None,
        )
        assert r.corroboration_sources == []

    def test_source_names_empty_gives_empty_list(self):
        r = compute_corroboration(
            unique_source_count=0,
            mention_count=0,
            event_confidence=0.0,
            risk_flag=False,
            source_names=[],
        )
        assert r.corroboration_sources == []


# ---------------------------------------------------------------------------
# 10. Integration: score_candidate corroboration fields
# ---------------------------------------------------------------------------

class TestScoreCandidateCorroborationIntegration:
    def test_candidate_has_corroboration_score(self):
        d = _make_discovered("NVDA")
        cand = score_candidate(d, _make_cls())
        assert isinstance(cand.corroboration_score, float)
        assert 0.0 <= cand.corroboration_score <= 1.0

    def test_candidate_has_valid_corroboration_level(self):
        d = _make_discovered("NVDA")
        cand = score_candidate(d, _make_cls())
        assert cand.corroboration_level in ("none", "weak", "moderate", "strong")

    def test_strong_corroboration_enables_watch(self):
        # 4 sources, 7 mentions, 0.9 confidence, 3 prior runs → strong, corroboration_met=True
        d = _make_discovered("NVDA", mention_count=7, sources=["a", "b", "c", "d"])
        cls = _make_cls(confidence=0.9)
        cand = score_candidate(d, cls, watch_threshold=2.0, seen_runs=3)
        assert cand.corroboration_met is True
        assert cand.status == CandidateStatus.WATCH

    def test_insufficient_corroboration_prevents_watch(self):
        # Even with a high base score, no persistence → corroboration_met=False → DISCOVERED
        d = _make_discovered("NVDA", mention_count=5, sources=["a", "b", "c"])
        cls = _make_cls(confidence=0.75)
        cand = score_candidate(d, cls, watch_threshold=2.0, seen_runs=0)
        assert cand.corroboration_met is False
        assert cand.status == CandidateStatus.DISCOVERED

    def test_risk_flag_lowers_corroboration_score(self):
        d = _make_discovered("NVDA", mention_count=5, sources=["a", "b", "c"])
        no_risk = score_candidate(d, _make_cls(confidence=0.7, risk_flag=False))
        with_risk = score_candidate(d, _make_cls(confidence=0.7, risk_flag=True))
        assert with_risk.corroboration_score < no_risk.corroboration_score

    def test_persistence_increases_corroboration(self):
        d = _make_discovered("NVDA", mention_count=3, sources=["a", "b"])
        low = score_candidate(d, _make_cls(), seen_runs=0)
        high = score_candidate(d, _make_cls(), seen_runs=3)
        assert high.corroboration_score > low.corroboration_score


# ---------------------------------------------------------------------------
# 11. Artifact serialization
# ---------------------------------------------------------------------------

class TestArtifactSerialization:
    def test_candidate_to_dict_has_corroboration_score(self):
        cand = _make_candidate_dc("NVDA")
        d = _candidate_to_dict(cand)
        assert "corroboration_score" in d
        assert isinstance(d["corroboration_score"], float)

    def test_candidate_to_dict_has_corroboration_level(self):
        cand = _make_candidate_dc("NVDA")
        d = _candidate_to_dict(cand)
        assert "corroboration_level" in d
        assert d["corroboration_level"] in ("none", "weak", "moderate", "strong")

    def test_candidate_to_dict_has_corroboration_met(self):
        cand = _make_candidate_dc("NVDA")
        d = _candidate_to_dict(cand)
        assert "corroboration_met" in d

    def test_emerging_json_has_corroboration_fields(self, tmp_path):
        import json
        d = _make_discovered("NVDA", mention_count=7, sources=["a", "b", "c", "d"])
        cls = _make_cls(confidence=0.9)
        cand = score_candidate(d, cls, watch_threshold=2.0, seen_runs=3)
        mem = DiscoveryMemory()
        mem.update([cand])
        written = write_discovery_reports(
            [cand], mem, run_mode="discovery", base_dir=str(tmp_path)
        )
        data = json.loads(written["emerging_candidates"].read_text())
        first = data["candidates"][0]
        assert "corroboration_score" in first
        assert "corroboration_level" in first
        assert "corroboration_met" in first


# ---------------------------------------------------------------------------
# 12. Memo contains corroboration info for WATCH candidates
# ---------------------------------------------------------------------------

class TestMemoCorroborationContent:
    def test_memo_shows_corroboration_level_for_watch(self, tmp_path):
        d = _make_discovered("NVDA", mention_count=7, sources=["a", "b", "c", "d"])
        cls = _make_cls(confidence=0.9)
        cand = score_candidate(d, cls, watch_threshold=2.0, seen_runs=3)
        mem = DiscoveryMemory()
        mem.update([cand])
        written = write_discovery_reports(
            [cand], mem, run_mode="discovery", base_dir=str(tmp_path)
        )
        md = written["discovery_memo_section"].read_text()
        assert "corroboration:" in md.lower() or "strong" in md.lower()

    def test_memo_has_corroboration_summary_section(self, tmp_path):
        d = _make_discovered("NVDA")
        cls = _make_cls()
        cand = score_candidate(d, cls)
        mem = DiscoveryMemory()
        mem.update([cand])
        written = write_discovery_reports(
            [cand], mem, run_mode="discovery", base_dir=str(tmp_path)
        )
        md = written["discovery_memo_section"].read_text()
        assert "Corroboration Summary" in md


# ---------------------------------------------------------------------------
# 13. Safety regression: no WATCH without corroboration_met
# ---------------------------------------------------------------------------

class TestWatchRequiresCorroboration:
    def test_no_watch_without_corroboration_met(self):
        # High score but no persistence → corroboration_met=False → all DISCOVERED
        d = _make_discovered("NVDA", mention_count=5, sources=["a", "b", "c"])
        cls = _make_cls(confidence=0.75)
        result = evaluate_candidates([d], [cls], watch_threshold=2.0, persistence_data=None)
        assert all(c.status != CandidateStatus.WATCH for c in result)

    def test_all_watch_candidates_have_corroboration_met(self):
        d1 = _make_discovered("NVDA", mention_count=7, sources=["a", "b", "c", "d"])
        d2 = _make_discovered("AAPL", mention_count=3, sources=["a", "b"])
        clss = [_make_cls(confidence=0.9), _make_cls(confidence=0.5)]
        result = evaluate_candidates(
            [d1, d2], clss,
            watch_threshold=2.0,
            persistence_data={"NVDA": 3, "AAPL": 0},
        )
        watch_candidates = [c for c in result if c.status == CandidateStatus.WATCH]
        assert all(c.corroboration_met is True for c in watch_candidates)

    def test_forbidden_statuses_never_produced_with_corroboration(self):
        d = _make_discovered("NVDA", mention_count=7, sources=["a", "b", "c", "d"])
        cls = _make_cls(confidence=0.9)
        result = evaluate_candidates(
            [d], [cls],
            watch_threshold=2.0,
            persistence_data={"NVDA": 3},
        )
        for c in result:
            assert c.status.value in ("watch", "discovered", "rejected")

    def test_persistence_data_unknown_ticker_defaults_zero(self):
        d = _make_discovered("UNKN", mention_count=5, sources=["a", "b", "c"])
        cls = _make_cls(confidence=0.75)
        # UNKN not in persistence_data → seen_runs=0 → no persistence bonus
        result = evaluate_candidates(
            [d], [cls],
            watch_threshold=2.0,
            persistence_data={"OTHER": 5},
        )
        assert result[0].status == CandidateStatus.DISCOVERED
