"""Phase 6 — opportunity scoring (distinct from protected portfolio scores)."""
from __future__ import annotations

import portfolio_automation.opportunity_scoring as ops
from portfolio_automation.next_stage.contracts import OpportunityStatus as S


def _strong(**over):
    base = dict(
        candidate="NVDA", candidate_type="public_ticker", access_route="etf",
        catalyst_strength=0.8, price_volume_confirmation=0.8, fundamental_support=0.8,
        market_regime_fit=0.7, portfolio_diversification_value=0.7,
        access_investability=0.9, risk_adjusted_timing=0.7, boom_potential=0.6,
        evidence_quality=0.8, liquidity_quality=0.9, data_quality=0.8,
    )
    base.update(over)
    return base


def test_deterministic_same_input_same_output():
    a = ops.score_opportunity(_strong())
    b = ops.score_opportunity(_strong())
    assert a.to_dict() == b.to_dict()


def test_strong_evidence_and_investability_can_qualify():
    sc = ops.score_opportunity(_strong())
    assert sc.final_status in (S.QUALIFIED.value, S.APPROVED_WATCHLIST_REVIEW.value)
    assert sc.observe_only is True


def test_high_boom_alone_cannot_promote():
    # Max boom, but weak evidence + weak investability → must NOT qualify.
    sc = ops.score_opportunity(dict(
        candidate="MEME", candidate_type="public_ticker", access_route="watch_only",
        boom_potential=1.0, catalyst_strength=1.0,
        evidence_quality=0.1, data_quality=0.1, price_volume_confirmation=0.1,
        access_investability=0.2, liquidity_quality=0.2,
        single_headline_penalty=0.6, hype_penalty=0.5,
    ))
    assert sc.boom_score >= 0.7  # boom is genuinely high
    assert sc.final_status not in (S.QUALIFIED.value, S.APPROVED_WATCHLIST_REVIEW.value)


def test_low_investability_caps_at_access_limited():
    sc = ops.score_opportunity(_strong(access_investability=0.1, liquidity_quality=0.1))
    assert sc.final_status == S.ACCESS_LIMITED.value


def test_private_company_never_tradeable():
    sc = ops.score_opportunity(dict(
        candidate="SpaceX", candidate_type="private_ipo", access_route="watch_only",
        catalyst_strength=0.9, boom_potential=0.9, evidence_quality=0.8,
        access_investability=0.9, liquidity_quality=0.9, data_quality=0.8))
    assert sc.final_status == S.PRIVATE_WATCH_ONLY.value


def test_private_with_public_proxy_route_is_access_limited_not_qualified():
    sc = ops.score_opportunity(dict(
        candidate="SpaceX-proxy", candidate_type="private_ipo", access_route="proxy",
        access_investability=0.8, liquidity_quality=0.8, evidence_quality=0.7,
        data_quality=0.7, price_volume_confirmation=0.6, catalyst_strength=0.7))
    # investable only via proxy — never a direct QUALIFIED buy
    assert sc.final_status == S.ACCESS_LIMITED.value


def test_hype_dominant_weak_evidence_is_noise():
    sc = ops.score_opportunity(dict(
        candidate="HYPE", candidate_type="public_ticker", access_route="etf",
        hype_penalty=0.9, crowded_trade_penalty=0.8, boom_potential=0.9,
        evidence_quality=0.1, data_quality=0.1, access_investability=0.6,
        liquidity_quality=0.6))
    assert sc.final_status == S.HYPE_NOISE.value


def test_single_headline_requires_corroboration():
    sc = ops.score_opportunity(dict(
        candidate="ONE", candidate_type="public_ticker", access_route="etf",
        single_headline_penalty=0.7, evidence_quality=0.2, data_quality=0.2,
        access_investability=0.6, liquidity_quality=0.6, catalyst_strength=0.7,
        boom_potential=0.6))
    assert sc.final_status not in (S.QUALIFIED.value, S.APPROVED_WATCHLIST_REVIEW.value)


def test_penalties_are_surfaced():
    sc = ops.score_opportunity(_strong(hype_penalty=0.3, crowded_trade_penalty=0.2,
                                       single_headline_penalty=0.1, portfolio_overlap_penalty=0.4))
    d = sc.to_dict()
    assert d["hype_penalty"] == 0.3 and d["crowded_trade_penalty"] == 0.2
    assert d["single_headline_penalty"] == 0.1 and d["portfolio_overlap_penalty"] == 0.4


def test_does_not_use_protected_score_names():
    d = ops.score_opportunity(_strong()).to_dict()
    for protected in ("signal_score", "confidence_score", "effective_score",
                      "conviction_score", "final_rank_score", "recommendation_score"):
        assert protected not in d, f"opportunity score must not reuse {protected}"
