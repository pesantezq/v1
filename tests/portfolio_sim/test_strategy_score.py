"""Tests for the master strategy score + expanded metrics."""
from __future__ import annotations

from portfolio_automation.portfolio_sim import metrics as m
from portfolio_automation.portfolio_sim.strategy_score import rank, score


def test_higher_excess_scores_higher():
    a = score({"excess_return_vs_spy": 0.10, "has_research": True, "overfit": 0.0})
    b = score({"excess_return_vs_spy": 0.02, "has_research": True, "overfit": 0.0})
    assert a["strategy_score"] > b["strategy_score"]


def test_overfit_penalizes():
    clean = score({"excess_return_vs_spy": 0.10, "overfit": 0.0, "has_research": True})
    overfit = score({"excess_return_vs_spy": 0.10, "overfit": 0.8, "has_research": True})
    assert overfit["strategy_score"] < clean["strategy_score"]


def test_overfit_unknown_flagged():
    s = score({"excess_return_vs_spy": 0.05})
    assert "overfit_unknown" in s["flags"]
    assert "no_academic_basis" in s["flags"]


def test_penalties_reduce_score():
    base = score({"excess_return_vs_spy": 0.10, "has_research": True, "overfit": 0.0})
    penalized = score({"excess_return_vs_spy": 0.10, "has_research": True, "overfit": 0.0,
                       "turnover": 1.0, "tax_drag": 1.0, "concentration": 1.0, "leverage": 1.0})
    assert penalized["strategy_score"] < base["strategy_score"]


def test_rank_orders_desc():
    out = rank([{"strategy_score": 1.0}, {"strategy_score": 3.0}, {"strategy_score": 2.0}])
    assert [s["strategy_score"] for s in out] == [3.0, 2.0, 1.0]


def test_metrics_time_underwater():
    assert m.time_underwater([100, 110, 105, 120]) > 0    # spent time below peak
    assert m.time_underwater([100, 110, 120]) == 0.0      # always at new highs


def test_worst_window_return():
    vals = [100, 90, 80, 120]
    assert m.worst_window_return(vals, 1) <= 0


def test_expected_shortfall_negative_tail():
    vals = [100, 90, 95, 80, 120, 100]
    assert m.expected_shortfall(vals, q=0.5) <= 0


def test_prob_beat():
    a = [100, 110, 120]   # +10%, +9%
    b = [100, 101, 102]   # +1%, +1%
    assert m.prob_beat(a, b) == 1.0
