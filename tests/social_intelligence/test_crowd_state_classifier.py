"""Tests that the classifier produces each required crowd state + governance."""
from __future__ import annotations

from portfolio_automation.social_intelligence.base import CrowdState, NextStep
from portfolio_automation.social_intelligence.crowd_state_classifier import (
    ClassifierThresholds,
    classify_all,
    classify_ticker,
)
from portfolio_automation.social_intelligence.feature_aggregation import TickerFeatures

T = ClassifierThresholds()


def _classify(**kw):
    f = TickerFeatures(ticker=kw.pop("ticker", "TST"), **kw)
    return classify_ticker(f, T)


def test_dormant_noise():
    r = _classify(mention_count=1, evidence_score=0.0, meme_language_score=0.0)
    assert r["crowd_state"] == CrowdState.DORMANT_NOISE.value
    assert r["recommended_next_step"] == NextStep.IGNORE.value


def test_contrarian_neglect():
    # Quiet crowd but external support present.
    r = _classify(mention_count=1, evidence_score=0.6, external_news_match=True)
    assert r["crowd_state"] == CrowdState.CONTRARIAN_NEGLECT.value
    assert r["crowd_research_priority_score"] > 0


def test_reflexive_squeeze_risk():
    r = _classify(mention_count=50, mention_velocity_zscore=3.0,
                  options_or_short_interest_context=2.5, meme_language_score=0.5)
    assert r["crowd_state"] == CrowdState.REFLEXIVE_SQUEEZE_RISK.value
    assert r["recommended_next_step"] == NextStep.FLAG_AS_HYPE_RISK.value
    assert "elevated_short_interest_or_options" in r["risk_flags"]


def test_known_news_echo():
    r = _classify(mention_count=20, mention_velocity_zscore=1.2,
                  external_news_match=True, price_move_before_social_spike=5.0)
    assert r["crowd_state"] == CrowdState.KNOWN_NEWS_ECHO.value
    assert r["recommended_next_step"] == NextStep.MONITOR.value


def test_hype_acceleration():
    r = _classify(mention_count=40, mention_velocity_zscore=3.0,
                  meme_language_score=0.8, evidence_score=0.05)
    assert r["crowd_state"] == CrowdState.HYPE_ACCELERATION.value
    assert r["recommended_next_step"] == NextStep.FLAG_AS_HYPE_RISK.value
    # Hype must be SUPPRESSED in research priority (negative).
    assert r["crowd_research_priority_score"] <= 0


def test_crowd_exhaustion():
    r = _classify(mention_count=30, mention_velocity_zscore=1.5,
                  sentiment_dispersion=0.6, meme_language_score=0.5, evidence_score=0.1)
    assert r["crowd_state"] == CrowdState.CROWD_EXHAUSTION.value
    assert "fragmenting_sentiment" in r["risk_flags"]


def test_crowd_validation():
    r = _classify(mention_count=15, unique_author_count=8, evidence_score=0.7,
                  meme_language_score=0.05, external_news_match=True)
    assert r["crowd_state"] == CrowdState.CROWD_VALIDATION.value
    assert "independent_authors_converging" in r["risk_flags"]
    assert r["crowd_research_priority_score"] > 0


def test_emerging_dd():
    r = _classify(mention_count=6, mention_velocity_zscore=1.3, unique_author_count=2,
                  dd_density=0.6, evidence_score=0.36, meme_language_score=0.0)
    assert r["crowd_state"] == CrowdState.EMERGING_DD.value


def test_research_priority_is_capped():
    r = _classify(mention_count=200, mention_velocity_zscore=50.0,
                  evidence_score=1.0, unique_author_count=100,
                  external_news_match=True)
    assert r["crowd_research_priority_score"] <= 10.0


def test_next_step_never_a_trade_verb():
    forbidden = {"buy", "sell", "hold", "rebalance", "trim", "scale", "promote"}
    # Sweep a grid of feature combos and assert no trade verb ever appears.
    for mc in (0, 5, 50):
        for mv in (0.0, 1.5, 3.0):
            for meme in (0.0, 0.8):
                for ev in (0.0, 0.7):
                    r = _classify(mention_count=mc, mention_velocity_zscore=mv,
                                  meme_language_score=meme, evidence_score=ev,
                                  unique_author_count=5)
                    assert r["recommended_next_step"] not in forbidden
                    assert r["recommended_next_step"] in {s.value for s in NextStep}


def test_classify_all_sorted_by_priority():
    feats = [
        TickerFeatures(ticker="LOW", mention_count=40, mention_velocity_zscore=3.0,
                       meme_language_score=0.9, evidence_score=0.0),
        TickerFeatures(ticker="HIGH", mention_count=15, unique_author_count=8,
                       evidence_score=0.8, external_news_match=True),
    ]
    out = classify_all(feats, T)
    assert out[0]["crowd_research_priority_score"] >= out[1]["crowd_research_priority_score"]


def test_score_components_present_for_explainability():
    r = _classify(mention_count=10, evidence_score=0.5)
    assert "score_components" in r and isinstance(r["score_components"], dict)
    assert "velocity_z" in r["score_components"]
