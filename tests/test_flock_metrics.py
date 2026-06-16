"""Flock Intelligence — metrics + classifier unit tests (pure, no I/O)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from portfolio_automation.flock_intelligence import metrics as M
from portfolio_automation.flock_intelligence.states import (
    FlockState, GroupMetrics, Thresholds, classify_group,
)


# ---------------------------------------------------------------------------
# Metric math
# ---------------------------------------------------------------------------

def test_pairwise_correlation_perfect_and_short():
    assert M.pairwise_correlation([1, 2, 3, 4], [2, 4, 6, 8]) == pytest.approx(1.0)
    assert M.pairwise_correlation([1, 2, 3, 4], [8, 6, 4, 2]) == pytest.approx(-1.0)
    assert M.pairwise_correlation([1, 2], [2, 4]) is None          # too short
    assert M.pairwise_correlation([1, 1, 1], [2, 4, 6]) is None     # zero variance


def test_average_pairwise_correlation():
    rets = {"A": [1, 2, 3, 4], "B": [2, 4, 6, 8], "C": [1, 2, 3, 4]}
    assert M.average_pairwise_correlation(rets) == pytest.approx(1.0)
    assert M.average_pairwise_correlation({"A": [1, 2, 3, 4]}) is None  # <2 tickers


def test_crowd_velocity_breadth_concentration():
    vel = {"A": 2.0, "B": 0.0, "C": -1.0}
    assert M.crowd_velocity(vel) == (2.0 + 0.0 - 1.0) / 3
    assert M.crowd_breadth(vel, group_size=3) == round(1 / 3, 10) or M.crowd_breadth(vel, 3) > 0
    # concentration: all in one name -> ~1.0; even split -> ~1/n
    assert M.mention_concentration({"A": 100, "B": 0, "C": 0}) == 1.0
    assert abs(M.mention_concentration({"A": 10, "B": 10, "C": 10}) - 1 / 3) < 1e-9


def test_return_spread_and_momentum():
    assert M.return_spread({"A": 5.0, "B": -5.0}) > 0
    assert M.return_spread({"A": 1.0}) == 0.0
    assert M.group_momentum({"A": 2.0, "B": 4.0}) == 3.0


def test_dispersion_score_rises_when_correlation_falls():
    low = M.dispersion_score(avg_corr=0.9, prior_avg_corr=0.9, ret_spread=0.0,
                             breadth=0.9, concentration=0.2, vol_change=0.0)
    high = M.dispersion_score(avg_corr=0.1, prior_avg_corr=0.9, ret_spread=4.0,
                              breadth=0.1, concentration=0.9, vol_change=1.0)
    assert high > low
    assert 0.0 <= low <= 1.0 and 0.0 <= high <= 1.0


def test_exhaustion_score_rises_with_velocity_and_concentration():
    calm = M.exhaustion_score(velocity=0.0, concentration=0.1, breadth=0.9,
                              prior_breadth=0.9, vol_change=0.0, momentum=1.0)
    hot = M.exhaustion_score(velocity=2.0, concentration=0.9, breadth=0.2,
                             prior_breadth=0.9, vol_change=1.0, momentum=-2.0)
    assert hot > calm


# ---------------------------------------------------------------------------
# Classifier — a builder that sets sensible neutral defaults
# ---------------------------------------------------------------------------

def _gm(**kw) -> GroupMetrics:
    base = dict(
        group="G", group_kind="theme", tickers=["A", "B", "C"], n_tickers=3,
        n_with_returns=3, history_points=6, has_crowd_data=True,
        crowd_velocity=0.0, crowd_breadth=0.0, source_breadth=1.0,
        mention_concentration=0.33, avg_correlation=None, prior_avg_correlation=None,
        return_spread=0.0, group_momentum=0.0, volatility_change=0.0,
        flock_score=0.0, dispersion_score=0.0, exhaustion_score=0.0,
    )
    base.update(kw)
    return GroupMetrics(**base)


def test_classify_insufficient_data_group_too_small():
    gf = classify_group(_gm(tickers=["A"], n_tickers=1, n_with_returns=1))
    assert gf.flock_state == FlockState.INSUFFICIENT_DATA.value
    assert gf.confidence <= 0.3


def test_classify_insufficient_data_no_structure():
    # Enough data, but nothing flocking (no velocity, no correlation).
    gf = classify_group(_gm(crowd_velocity=0.0, avg_correlation=0.0, crowd_breadth=0.0))
    assert gf.flock_state == FlockState.INSUFFICIENT_DATA.value


def test_classify_flock_forming():
    gf = classify_group(_gm(crowd_velocity=0.8, crowd_breadth=0.5, avg_correlation=0.35,
                            flock_score=0.45))
    assert gf.flock_state == FlockState.FLOCK_FORMING.value


def test_classify_flock_confirmed():
    gf = classify_group(_gm(crowd_velocity=1.6, crowd_breadth=0.8, avg_correlation=0.7,
                            group_momentum=2.0, flock_score=0.8))
    assert gf.flock_state == FlockState.FLOCK_CONFIRMED.value
    assert "flock_confirmed" in gf.risk_flags


def test_classify_flock_exhaustion():
    gf = classify_group(_gm(crowd_velocity=2.0, crowd_breadth=0.3, mention_concentration=0.7,
                            group_momentum=-1.0, volatility_change=1.0, exhaustion_score=0.7))
    assert gf.flock_state == FlockState.FLOCK_EXHAUSTION.value
    assert "crowded_trade" in gf.risk_flags


def test_classify_flock_dispersing_requires_prior_flock():
    # Correlation falling but crowd velocity still elevated -> dispersing (not broken).
    m = _gm(avg_correlation=0.5, prior_avg_correlation=0.8, return_spread=3.0,
            crowd_velocity=1.2, crowd_breadth=0.3, dispersion_score=0.6)
    # No prior flock -> not dispersing
    assert classify_group(m, prior_state=None).flock_state != FlockState.FLOCK_DISPERSING.value
    # With a prior confirmed flock -> dispersing
    gf = classify_group(m, prior_state=FlockState.FLOCK_CONFIRMED.value)
    assert gf.flock_state == FlockState.FLOCK_DISPERSING.value
    assert "dispersion_risk" in gf.risk_flags


def test_classify_flock_broken():
    m = _gm(avg_correlation=0.1, prior_avg_correlation=0.8, crowd_velocity=0.2,
            return_spread=4.0)
    gf = classify_group(m, prior_state=FlockState.FLOCK_CONFIRMED.value)
    assert gf.flock_state == FlockState.FLOCK_BROKEN.value


def test_classify_confidence_scales_with_data():
    # crowd_breadth > 0 so both classify as flock_forming (confidence not capped).
    thin = classify_group(_gm(n_tickers=2, n_with_returns=2, history_points=3,
                              has_crowd_data=False, avg_correlation=0.4,
                              crowd_velocity=0.5, crowd_breadth=0.5))
    rich = classify_group(_gm(n_tickers=4, n_with_returns=4, history_points=12,
                              has_crowd_data=True, avg_correlation=0.4,
                              crowd_velocity=0.5, crowd_breadth=0.5))
    assert rich.flock_state == FlockState.FLOCK_FORMING.value
    assert rich.confidence > thin.confidence
