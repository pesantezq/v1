"""Tests for portfolio_automation.discovery.candidate_promotion_engine."""
import pytest
from datetime import datetime, timezone

from portfolio_automation.discovery.news_ticker_discovery import (
    DiscoveredTicker,
    TickerEvidence,
)
from portfolio_automation.discovery.event_classifier import (
    ClassificationResult,
    EventType,
)
from portfolio_automation.discovery.candidate_promotion_engine import (
    CandidateStatus,
    DiscoveryCandidate,
    evaluate_candidates,
    score_candidate,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TS = "2026-05-01T00:00:00+00:00"


def _make_discovered(
    ticker: str,
    mention_count: int = 1,
    sources: list[str] | None = None,
    record_indices: list[int] | None = None,
) -> DiscoveredTicker:
    srcs = sources or ["source_a"]
    indices = record_indices or [0]
    evidence = [
        TickerEvidence(
            record_index=i,
            source=s,
            published_at=_TS,
            extraction_method="cashtag",
            context=f"${ticker} context",
        )
        for i, s in zip(indices, srcs * len(indices))
    ]
    return DiscoveredTicker(
        ticker=ticker,
        mention_count=mention_count,
        unique_sources=list(set(srcs)),
        evidence=evidence,
    )


def _make_cls(
    event_type: EventType = EventType.EARNINGS,
    confidence: float = 0.5,
    risk_flag: bool = False,
) -> ClassificationResult:
    return ClassificationResult(
        event_type=event_type,
        confidence=confidence,
        matched_keywords=["earnings"],
        risk_flag=risk_flag,
    )


# ---------------------------------------------------------------------------
# 1. No forbidden statuses
# ---------------------------------------------------------------------------

class TestForbiddenStatuses:
    def test_no_actionable_status(self):
        for attr in ("ACTIONABLE", "PROMOTED", "VALIDATED", "BUY", "SELL"):
            assert not hasattr(CandidateStatus, attr), (
                f"CandidateStatus must not have {attr}"
            )

    def test_only_allowed_statuses_exist(self):
        allowed = {"discovered", "watch", "rejected"}
        actual = {s.value for s in CandidateStatus}
        assert actual == allowed

    def test_candidates_never_have_forbidden_status(self):
        discovered = _make_discovered("NVDA", mention_count=5, sources=["a", "b", "c"])
        cls = _make_cls(confidence=0.9)
        candidates = evaluate_candidates([discovered], [cls])
        for c in candidates:
            assert c.status.value in ("discovered", "watch", "rejected")


# ---------------------------------------------------------------------------
# 2. Corroboration flags always set
# ---------------------------------------------------------------------------

class TestCorroborationFlags:
    def test_corroboration_required_true(self):
        d = _make_discovered("NVDA")
        cls = _make_cls()
        cand = score_candidate(d, cls)
        assert cand.corroboration_required is True

    def test_corroboration_met_false(self):
        d = _make_discovered("NVDA")
        cls = _make_cls()
        cand = score_candidate(d, cls)
        assert cand.corroboration_met is False

    def test_corroboration_sources_empty(self):
        d = _make_discovered("NVDA")
        cls = _make_cls()
        cand = score_candidate(d, cls)
        assert cand.corroboration_sources == []

    def test_all_candidates_have_corroboration_required(self):
        tickers = [_make_discovered("NVDA"), _make_discovered("AAPL")]
        clss = [_make_cls(), _make_cls()]
        candidates = evaluate_candidates(tickers, clss)
        assert all(c.corroboration_required is True for c in candidates)


# ---------------------------------------------------------------------------
# 3. Governance flags
# ---------------------------------------------------------------------------

class TestGovernanceFlags:
    def test_discovery_only_true(self):
        d = _make_discovered("NVDA")
        cand = score_candidate(d, _make_cls())
        assert cand.discovery_only is True

    def test_sandbox_only_true(self):
        d = _make_discovered("NVDA")
        cand = score_candidate(d, _make_cls())
        assert cand.sandbox_only is True


# ---------------------------------------------------------------------------
# 4. Mention count impact on score
# ---------------------------------------------------------------------------

class TestMentionCountImpact:
    def test_more_mentions_higher_score(self):
        d1 = _make_discovered("NVDA", mention_count=1)
        d3 = _make_discovered("AAPL", mention_count=5)
        cls = _make_cls(confidence=0.0, event_type=EventType.UNKNOWN)
        c1 = score_candidate(d1, cls)
        c3 = score_candidate(d3, cls)
        assert c3.score > c1.score

    def test_single_mention_still_scores_positive(self):
        d = _make_discovered("NVDA", mention_count=1)
        cand = score_candidate(d, _make_cls(confidence=0.5))
        assert cand.score > 0.0


# ---------------------------------------------------------------------------
# 5. Unique source count impact
# ---------------------------------------------------------------------------

class TestUniqueSourceCountImpact:
    def test_more_sources_higher_score(self):
        d1 = _make_discovered("NVDA", mention_count=1, sources=["src_a"])
        d3 = _make_discovered("AAPL", mention_count=1, sources=["src_a", "src_b", "src_c"])
        cls = _make_cls(confidence=0.0, event_type=EventType.UNKNOWN)
        c1 = score_candidate(d1, cls)
        c3 = score_candidate(d3, cls)
        assert c3.score > c1.score


# ---------------------------------------------------------------------------
# 6. Risk penalty
# ---------------------------------------------------------------------------

class TestRiskPenalty:
    def test_risk_flag_lowers_score(self):
        d = _make_discovered("NVDA", mention_count=3)
        no_risk = score_candidate(d, _make_cls(risk_flag=False, confidence=0.5))
        with_risk = score_candidate(d, _make_cls(risk_flag=True, confidence=0.5))
        assert with_risk.score < no_risk.score

    def test_risk_flag_low_confidence_triggers_rejected(self):
        d = _make_discovered("NVDA", mention_count=1)
        # risk_flag=True, confidence < reject_risk_below (0.3)
        cls = _make_cls(risk_flag=True, confidence=0.1, event_type=EventType.LEGAL_RISK)
        cand = score_candidate(d, cls, reject_risk_below=0.3)
        assert cand.status == CandidateStatus.REJECTED
        assert cand.rejection_reason is not None


# ---------------------------------------------------------------------------
# 7. Status assignment
# ---------------------------------------------------------------------------

class TestStatusAssignment:
    def test_watch_status_high_score(self):
        d = _make_discovered("NVDA", mention_count=5, sources=["a", "b", "c"])
        cls = _make_cls(confidence=0.75, event_type=EventType.EARNINGS)
        cand = score_candidate(d, cls, watch_threshold=2.0)
        assert cand.status == CandidateStatus.WATCH

    def test_discovered_status_low_score(self):
        d = _make_discovered("NVDA", mention_count=1, sources=["a"])
        cls = _make_cls(confidence=0.0, event_type=EventType.UNKNOWN)
        cand = score_candidate(d, cls, watch_threshold=5.0)
        assert cand.status == CandidateStatus.DISCOVERED

    def test_rejected_status_risk_low_confidence(self):
        d = _make_discovered("NVDA", mention_count=1)
        cls = _make_cls(risk_flag=True, confidence=0.1, event_type=EventType.LEGAL_RISK)
        cand = score_candidate(d, cls, reject_risk_below=0.3)
        assert cand.status == CandidateStatus.REJECTED

    def test_rejection_reason_populated_when_rejected(self):
        d = _make_discovered("NVDA")
        cls = _make_cls(risk_flag=True, confidence=0.1, event_type=EventType.LEGAL_RISK)
        cand = score_candidate(d, cls, reject_risk_below=0.3)
        assert cand.rejection_reason is not None
        assert len(cand.rejection_reason) > 0

    def test_rejection_reason_none_when_not_rejected(self):
        d = _make_discovered("NVDA", mention_count=1)
        cls = _make_cls(confidence=0.5)
        cand = score_candidate(d, cls, watch_threshold=5.0)
        assert cand.status != CandidateStatus.REJECTED
        assert cand.rejection_reason is None


# ---------------------------------------------------------------------------
# 8. evaluate_candidates
# ---------------------------------------------------------------------------

class TestEvaluateCandidates:
    def test_empty_input_returns_empty(self):
        result = evaluate_candidates([], [])
        assert result == []

    def test_returns_list_of_discovery_candidates(self):
        d = _make_discovered("NVDA")
        cls = _make_cls()
        result = evaluate_candidates([d], [cls])
        assert all(isinstance(c, DiscoveryCandidate) for c in result)

    def test_sorting_watch_before_discovered_before_rejected(self):
        # Assign distinct record_indices so each ticker maps to a different classification.
        d_watch = _make_discovered("NVDA", mention_count=5, sources=["a", "b", "c"], record_indices=[0])
        d_disc = _make_discovered("AAPL", mention_count=1, record_indices=[1])
        d_rej = _make_discovered("BADCO", mention_count=1, record_indices=[2])
        clss = [
            _make_cls(confidence=0.9, event_type=EventType.EARNINGS),      # → WATCH
            _make_cls(confidence=0.0, event_type=EventType.UNKNOWN),       # → DISCOVERED
            _make_cls(risk_flag=True, confidence=0.1, event_type=EventType.LEGAL_RISK),  # → REJECTED
        ]
        result = evaluate_candidates([d_watch, d_disc, d_rej], clss)
        statuses = [c.status for c in result]
        watch_idx = next((i for i, s in enumerate(statuses) if s == CandidateStatus.WATCH), -1)
        disc_idx = next((i for i, s in enumerate(statuses) if s == CandidateStatus.DISCOVERED), -1)
        rej_idx = next((i for i, s in enumerate(statuses) if s == CandidateStatus.REJECTED), -1)
        assert watch_idx != -1, "Expected a WATCH candidate"
        assert disc_idx != -1, "Expected a DISCOVERED candidate"
        assert rej_idx != -1, "Expected a REJECTED candidate"
        assert watch_idx < disc_idx
        assert disc_idx < rej_idx

    def test_mismatched_classification_count_handled(self):
        # Fewer classifications than tickers — should not crash
        d1 = _make_discovered("NVDA")
        d2 = _make_discovered("AAPL")
        result = evaluate_candidates([d1, d2], [_make_cls()])
        assert len(result) == 2

    def test_no_candidates_have_forbidden_status(self):
        tickers = [_make_discovered("NVDA", mention_count=5, sources=["a", "b", "c"])]
        clss = [_make_cls(confidence=0.9)]
        result = evaluate_candidates(tickers, clss)
        for c in result:
            assert c.status in (
                CandidateStatus.WATCH,
                CandidateStatus.DISCOVERED,
                CandidateStatus.REJECTED,
            )

    def test_mention_count_carried_through(self):
        d = _make_discovered("NVDA", mention_count=7)
        result = evaluate_candidates([d], [_make_cls()])
        assert result[0].mention_count == 7

    def test_unique_source_count_carried_through(self):
        d = _make_discovered("NVDA", sources=["a", "b", "c"])
        result = evaluate_candidates([d], [_make_cls()])
        assert result[0].unique_source_count == 3
