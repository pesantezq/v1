"""Phase 7 tests — options interpretation + manager-symbol scoring.

Options: shares->common_equity_long, put->put_exposure (NOT bearish),
call->call_exposure (NOT bullish), sector_beta_hedge_possible inference,
directional_contribution ALWAYS 0. Scoring: bounded, tiny-position penalty,
top-10/rank conviction, freshness decay, strategy fit, options ambiguity,
cloneability, price-move penalty, no-options-directional, no NaN/inf, every
component persisted.
"""

from __future__ import annotations

import math

from portfolio_automation.institutional_intelligence import manager_scoring as ms
from portfolio_automation.institutional_intelligence import options_interpretation as oi
from portfolio_automation.institutional_intelligence import position_changes as pc
from portfolio_automation.institutional_intelligence.schemas import (
    PUT_CALL_CALL,
    PUT_CALL_NONE,
    PUT_CALL_PUT,
)


def _change(event, *, pct=None, weight=0.05, weight_delta=0.01, rank=5,
            top10=True, age=20, put_call=PUT_CALL_NONE, resolved=True):
    return pc.PositionChange(
        symbol="AAA", cusip="111", put_call=put_call, event=event,
        shares_delta=100.0, shares_pct_change=pct, value_delta=1000.0,
        prev_weight=weight - weight_delta, curr_weight=weight, weight_delta=weight_delta,
        curr_rank=rank, prev_rank=rank + 1, top10_entry=top10, filing_age_days=age,
        identity_resolved=resolved)


_EQUITY = oi.classify_option_context(PUT_CALL_NONE)


# --- options interpretation ---------------------------------------------

def test_shares_are_common_equity_long():
    r = oi.classify_option_context(PUT_CALL_NONE)
    assert r.taxonomy == oi.OPT_COMMON_EQUITY_LONG
    assert r.directional_contribution == 0.0 and r.interpretability_penalty == 0.0


def test_put_not_bearish():
    r = oi.classify_option_context(PUT_CALL_PUT)
    assert r.taxonomy == oi.OPT_PUT_EXPOSURE
    assert r.directional_contribution == 0.0     # never bearish
    assert "not interpreted as bearish" in r.note.lower()


def test_call_not_bullish():
    r = oi.classify_option_context(PUT_CALL_CALL)
    assert r.taxonomy == oi.OPT_CALL_EXPOSURE
    assert r.directional_contribution == 0.0     # never bullish


def test_sector_beta_hedge_is_inference():
    r = oi.classify_option_context(PUT_CALL_PUT, has_concentrated_longs=True,
                                   underlier_is_broad_market=True)
    assert r.taxonomy == oi.OPT_SECTOR_BETA_HEDGE_POSSIBLE
    assert r.is_inference is True and r.directional_contribution == 0.0
    assert "inference" in r.note.lower()


def test_high_complexity_manager_option_is_complex():
    r = oi.classify_option_context(PUT_CALL_CALL, manager_options_complexity="high")
    assert r.taxonomy == oi.OPT_COMPLEX_OR_UNKNOWN
    assert r.interpretability_penalty >= oi._PENALTY_COMPLEX


# --- direction ------------------------------------------------------------

def test_direction_defaults():
    assert ms.direction_score(pc.EV_NEW, None) == ms.DIRECTION_NEW
    assert ms.direction_score(pc.EV_EXITED, None) == ms.DIRECTION_EXIT
    assert ms.direction_score(pc.EV_INCREASED, 0.30) == ms.DIRECTION_INCREASE_LARGE
    assert ms.direction_score(pc.EV_INCREASED, 0.10) == ms.DIRECTION_INCREASE_SMALL
    assert ms.direction_score(pc.EV_REDUCED, -0.30) == ms.DIRECTION_REDUCE_LARGE
    assert ms.direction_score(pc.EV_REDUCED, -0.10) == ms.DIRECTION_REDUCE_SMALL
    assert ms.direction_score(pc.EV_UNCHANGED, 0.0) == 0.0


def test_options_events_have_zero_direction():
    for ev in (pc.EV_NEW_CALL, pc.EV_NEW_PUT, pc.EV_INCREASED_CALL, pc.EV_EXITED_PUT):
        assert ms.direction_score(ev, 0.5) == 0.0     # options never directional


# --- scoring bounds + components -----------------------------------------

def _score(change, **kw):
    base = dict(manager_quality_prior=0.7, cloneability=0.7, option_ctx=_EQUITY)
    base.update(kw)
    return ms.score_manager_symbol(change, **base)


def test_score_bounded_and_finite():
    for ev, pct in [(pc.EV_NEW, None), (pc.EV_EXITED, None),
                    (pc.EV_INCREASED, 0.5), (pc.EV_REDUCED, -0.5)]:
        s = _score(_change(ev, pct=pct))
        assert -1.0 <= s.final_score <= 1.0
        assert math.isfinite(s.final_score)
        for comp in (s.conviction_score, s.manager_quality_score, s.cloneability_score,
                     s.freshness_score, s.strategy_fit_score, s.persistence_score,
                     s.options_interpretability_score, s.data_quality_score):
            assert 0.0 <= comp <= 1.0 and math.isfinite(comp)


def test_new_position_positive_exit_negative():
    assert _score(_change(pc.EV_NEW)).final_score > 0
    assert _score(_change(pc.EV_EXITED)).final_score < 0


def test_tiny_position_penalized():
    big = _score(_change(pc.EV_NEW, weight=0.05))
    tiny = _score(_change(pc.EV_NEW, weight=0.001))
    assert "tiny_position" in tiny.penalties
    assert tiny.final_score < big.final_score


def test_top10_and_rank_conviction():
    high = _score(_change(pc.EV_NEW, rank=1, top10=True))
    low = _score(_change(pc.EV_NEW, rank=40, top10=False, weight=0.01))
    assert high.conviction_score > low.conviction_score


def test_freshness_decay():
    assert ms.freshness_score(10) == 1.0
    assert ms.freshness_score(300) == ms.FRESHNESS_FLOOR
    mid = ms.freshness_score(90)
    assert ms.FRESHNESS_FLOOR < mid < 1.0
    fresh = _score(_change(pc.EV_NEW, age=10))
    stale = _score(_change(pc.EV_NEW, age=150))
    assert fresh.final_score > stale.final_score


def test_strategy_fit():
    fit = ms.strategy_fit_score(["semiconductors"], ["semiconductors", "ai_compute"])
    miss = ms.strategy_fit_score(["utilities"], ["semiconductors"])
    neutral = ms.strategy_fit_score([], ["semiconductors"])
    assert fit > neutral > miss


def test_cloneability_scales_score():
    hi = _score(_change(pc.EV_NEW), cloneability=0.9)
    lo = _score(_change(pc.EV_NEW), cloneability=0.2)
    assert hi.final_score > lo.final_score


def test_options_ambiguity_lowers_interpretability():
    put_ctx = oi.classify_option_context(PUT_CALL_PUT)
    s = _score(_change(pc.EV_NEW_PUT, put_call=PUT_CALL_PUT), option_ctx=put_ctx)
    assert s.options_interpretability_score < 1.0
    # And a put event contributes no direction -> final 0.
    assert s.final_score == 0.0


def test_price_move_penalty():
    s = _score(_change(pc.EV_NEW), price_move_since_filing=0.35)
    assert "price_move" in s.penalties
    assert "large_price_move_since_filing" in s.warnings


def test_unresolved_identity_low_data_quality():
    s = _score(_change(pc.EV_NEW, resolved=False))
    assert s.data_quality_score <= 0.3


def test_amendment_penalty():
    s = _score(_change(pc.EV_NEW), is_amendment=True)
    assert "amendment" in s.penalties


def test_every_component_persisted():
    s = _score(_change(pc.EV_INCREASED, pct=0.3))
    # None of the nine components is None; penalties is a dict.
    assert all(v is not None for v in (
        s.direction_score, s.conviction_score, s.manager_quality_score,
        s.cloneability_score, s.freshness_score, s.strategy_fit_score,
        s.persistence_score, s.options_interpretability_score, s.data_quality_score))
    assert isinstance(s.penalties, dict)
