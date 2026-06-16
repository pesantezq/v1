"""Tests for the Unified Crowd Intelligence bus (pure join + metrics)."""
from __future__ import annotations

from portfolio_automation.crowd_intelligence import unified_bus as ub
from portfolio_automation.crowd_intelligence.unified_schema import (
    SS_AVAILABLE,
    SS_PLAN_LOCKED,
    STATE_CAUTION_LOW_BREADTH,
    STATE_CONFIRMED_ATTENTION,
    STATE_DIVERGENT_ATTENTION,
    STATE_INSTITUTIONAL_ONLY,
    STATE_INSUFFICIENT_DATA,
    STATE_RETAIL_ONLY,
)

GEN = "2026-06-16T12:00:00+00:00"


def _social(ticker, velocity, breadth=1, confidence=0.35):
    return {
        "ticker": ticker,
        "mention_velocity": velocity,
        "source_breadth": breadth,
        "confidence": confidence,
        "active_sources": ["apewisdom"],
    }


def _fmp(records=20, freshness=0.95, confidence=0.6, scores=None):
    return {
        "symbol": "X",
        "source_records_count": records,
        "data_freshness": freshness,
        "confidence": confidence,
        "category_scores": scores or {"news": 0.0, "analyst": 0.0, "insider": 0.0,
                                      "congress": 0.0, "attention": 0.0, "social_sentiment": 0.0},
    }


# --- normalize_retail_attention -------------------------------------------------

def test_normalize_retail_attention_flat_is_zero():
    assert ub.normalize_retail_attention(1.0) == 0.0


def test_normalize_retail_attention_surge_saturates():
    assert ub.normalize_retail_attention(13.0) == 1.0
    assert ub.normalize_retail_attention(5.0) == 1.0


def test_normalize_retail_attention_midrange():
    # velocity 3.0 -> excess 2 / span 4 = 0.5
    assert ub.normalize_retail_attention(3.0) == 0.5


def test_normalize_retail_attention_handles_none_and_garbage():
    assert ub.normalize_retail_attention(None) == 0.0
    assert ub.normalize_retail_attention("nope") == 0.0


# --- join: both / A-only / B-only / empty --------------------------------------

def test_join_both_lanes_present():
    rows = ub.build_unified_rows(
        social_records=[_social("AAPL", 5.0, breadth=1, confidence=0.4)],
        fmp_by_symbol={"AAPL": _fmp(records=20, freshness=1.0, confidence=0.7)},
        enabled_categories=["analyst", "attention", "congress", "insider", "news"],
        disabled_categories=["social_sentiment"],
        generated_at=GEN,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.source_lanes_present == {"social_intelligence": True, "crowd_intelligence": True}
    assert row.retail_attention_score == 1.0
    assert row.fmp_attention_score == 1.0
    assert row.crowd_state == STATE_CONFIRMED_ATTENTION
    assert not [w for w in row.warnings if w.endswith("_only")]


def test_join_lane_a_only_kept_with_warning():
    rows = ub.build_unified_rows(
        social_records=[_social("GME", 8.0)],
        fmp_by_symbol={},
        enabled_categories=[], disabled_categories=[], generated_at=GEN,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.source_lanes_present["social_intelligence"] is True
    assert row.source_lanes_present["crowd_intelligence"] is False
    assert "lane_a_only" in row.warnings
    assert row.fmp_attention_score is None
    assert row.crowd_state in (STATE_RETAIL_ONLY, STATE_CAUTION_LOW_BREADTH)


def test_join_lane_b_only_kept_with_warning():
    rows = ub.build_unified_rows(
        social_records=[],
        fmp_by_symbol={"XOM": _fmp(records=20, freshness=1.0)},
        enabled_categories=["news"], disabled_categories=["social_sentiment"], generated_at=GEN,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.source_lanes_present["crowd_intelligence"] is True
    assert "lane_b_only" in row.warnings
    assert row.retail_attention_score is None
    assert row.crowd_state == STATE_INSTITUTIONAL_ONLY


def test_join_both_empty():
    rows = ub.build_unified_rows(
        social_records=[], fmp_by_symbol={},
        enabled_categories=[], disabled_categories=[], generated_at=GEN,
    )
    assert rows == []


# --- cross-source metrics ------------------------------------------------------

def test_confirmation_high_when_both_strong():
    row = ub.build_unified_row(
        "T", generated_at=GEN,
        social=_social("T", 5.0, breadth=1), fmp=_fmp(records=20, freshness=1.0),
        enabled_categories=["news"], disabled_categories=["social_sentiment"],
    )
    # both ~1.0, breadth_total=2 -> confirmation ~1.0, divergence ~0
    assert row.cross_source_confirmation_score >= 0.9
    assert row.cross_source_divergence_score <= 0.1


def test_divergence_high_when_one_side_strong_other_weak():
    # retail strong, fmp present but near-zero context
    row = ub.build_unified_row(
        "T", generated_at=GEN,
        social=_social("T", 5.0, breadth=1),
        fmp=_fmp(records=1, freshness=0.1),  # activation ~0.005
        enabled_categories=["news"], disabled_categories=["social_sentiment"],
    )
    assert row.cross_source_divergence_score >= 0.5
    assert row.crowd_state == STATE_DIVERGENT_ATTENTION


def test_confirmation_and_divergence_mutually_exclusive():
    row = ub.build_unified_row(
        "T", generated_at=GEN,
        social=_social("T", 5.0), fmp=_fmp(records=20, freshness=1.0),
        enabled_categories=["news"], disabled_categories=["social_sentiment"],
    )
    # high confirmation implies low divergence
    assert not (row.cross_source_confirmation_score >= 0.5
                and row.cross_source_divergence_score >= 0.5)


def test_delta_retail_minus_fmp():
    row = ub.build_unified_row(
        "T", generated_at=GEN,
        social=_social("T", 3.0),  # retail 0.5
        fmp=_fmp(records=20, freshness=1.0),  # fmp 1.0
        enabled_categories=["news"], disabled_categories=["social_sentiment"],
    )
    assert row.retail_vs_fmp_attention_delta == round(0.5 - 1.0, 4)


def test_directional_scores_passthrough_signed():
    row = ub.build_unified_row(
        "T", generated_at=GEN, social=None,
        fmp=_fmp(scores={"news": 0.8, "analyst": -0.4, "insider": 0.0,
                         "congress": 0.2, "attention": 0.0, "social_sentiment": 0.0}),
        enabled_categories=["news"], disabled_categories=["social_sentiment"],
    )
    assert row.news_score == 0.8
    assert row.analyst_score == -0.4
    assert row.congress_score == 0.2


# --- PLAN_LOCKED social sentiment ----------------------------------------------

def test_social_sentiment_null_when_plan_locked():
    row = ub.build_unified_row(
        "T", generated_at=GEN, social=None, fmp=_fmp(),
        enabled_categories=["news", "analyst"], disabled_categories=["social_sentiment"],
    )
    assert row.social_sentiment_status == SS_PLAN_LOCKED
    assert row.social_sentiment_score is None  # null, never 0.0


def test_social_sentiment_available_status():
    row = ub.build_unified_row(
        "T", generated_at=GEN, social=None, fmp=_fmp(),
        enabled_categories=["news", "social_sentiment"], disabled_categories=[],
    )
    assert row.social_sentiment_status == SS_AVAILABLE


# --- crowd_state classification ------------------------------------------------

def test_state_retail_only():
    row = ub.build_unified_row(
        "T", generated_at=GEN, social=_social("T", 5.0, breadth=1), fmp=None,
        enabled_categories=[], disabled_categories=[],
    )
    assert row.crowd_state == STATE_RETAIL_ONLY


def test_state_institutional_only():
    row = ub.build_unified_row(
        "T", generated_at=GEN, social=None, fmp=_fmp(records=20, freshness=1.0),
        enabled_categories=["news"], disabled_categories=["social_sentiment"],
    )
    assert row.crowd_state == STATE_INSTITUTIONAL_ONLY


def test_state_insufficient_data_when_both_quiet():
    row = ub.build_unified_row(
        "T", generated_at=GEN,
        social=_social("T", 1.0),            # flat -> retail 0
        fmp=_fmp(records=0, freshness=0.0),  # activation 0
        enabled_categories=["news"], disabled_categories=["social_sentiment"],
    )
    assert row.crowd_state == STATE_INSUFFICIENT_DATA


def test_state_divergent_requires_both_lanes():
    # single-lane high retail must NOT be labeled divergent (it's retail_only)
    row = ub.build_unified_row(
        "T", generated_at=GEN, social=_social("T", 8.0), fmp=None,
        enabled_categories=[], disabled_categories=[],
    )
    assert row.cross_source_divergence_score >= 0.5   # score is one-sided-high
    assert row.crowd_state != STATE_DIVERGENT_ATTENTION  # but state is not divergent


# --- staleness -----------------------------------------------------------------

def test_stale_lane_lowers_scores_and_warns():
    fresh = ub.build_unified_row(
        "T", generated_at=GEN, social=_social("T", 5.0), fmp=_fmp(records=20, freshness=1.0),
        enabled_categories=["news"], disabled_categories=["social_sentiment"],
    )
    stale = ub.build_unified_row(
        "T", generated_at=GEN, social=_social("T", 5.0), fmp=_fmp(records=20, freshness=1.0),
        enabled_categories=["news"], disabled_categories=["social_sentiment"],
        fmp_stale=True,
    )
    assert "fmp_lane_stale" in stale.warnings
    assert stale.crowd_confidence < fresh.crowd_confidence
    assert stale.fmp_attention_score < fresh.fmp_attention_score


def test_rows_sorted_by_confidence_desc():
    rows = ub.build_unified_rows(
        social_records=[_social("LOW", 1.2), _social("HIGH", 5.0)],
        fmp_by_symbol={"HIGH": _fmp(records=20, freshness=1.0)},
        enabled_categories=["news"], disabled_categories=["social_sentiment"], generated_at=GEN,
    )
    confs = [r.crowd_confidence for r in rows]
    assert confs == sorted(confs, reverse=True)
