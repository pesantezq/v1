"""Phase 12 tests — point-in-time institutional backtest.

Covers: next-session effectivity (not quarter-end, not filing day), forward
return anti-look-ahead (None when future missing), attribution by dimension,
consensus-vs-single + accumulation-vs-distribution splits, min-sample gate,
insufficient_data verdict with NO readiness verdict, walk-forward fold guard,
directional hit-rate.
"""

from __future__ import annotations

from datetime import date, timedelta

from portfolio_automation.institutional_intelligence import institutional_backtest as bt


def _sessions(start=date(2026, 1, 1), n=200):
    # weekday-ish sequential sessions (simplified — every day is a session)
    return [start + timedelta(days=i) for i in range(n)]


def _prices(sessions, symbol="AAA", start=100.0, drift=0.001):
    return {symbol: {s: start * ((1 + drift) ** i) for i, s in enumerate(sessions)}}


def test_next_session_strictly_after():
    ss = _sessions(n=5)
    assert bt.next_market_session(ss[0], ss) == ss[1]          # not the filing day
    assert bt.next_market_session(ss[4], ss) is None           # nothing after last


def test_forward_return_anti_lookahead():
    ss = _sessions(n=30)
    px = _prices(ss)
    # horizon within history -> value
    r = bt.forward_return("AAA", ss[1], 10, px, ss)
    assert r is not None and r > 0
    # horizon beyond history -> None (no peek)
    assert bt.forward_return("AAA", ss[25], 10, px, ss) is None


def test_signal_not_effective_before_next_session():
    ss = _sessions(n=60)
    px = _prices(ss)
    # filing available on ss[0]; effective = ss[1]. A single accumulation event.
    ev = bt.SignalEvent(symbol="AAA", filing_available=ss[0], direction=1, score=0.6)
    res = bt.backtest([ev], px, ss, horizon=10, min_samples=1)
    assert res.overall.n == 1                                   # tradable next session
    # If filing_available is the LAST session, no next session -> not counted.
    ev_last = bt.SignalEvent(symbol="AAA", filing_available=ss[-1], direction=1, score=0.6)
    assert bt.backtest([ev_last], px, ss, horizon=10, min_samples=1).overall.n == 0


def test_insufficient_data_no_readiness_verdict():
    ss = _sessions(n=60)
    px = _prices(ss)
    events = [bt.SignalEvent("AAA", ss[i], 1, 0.6) for i in range(5)]  # < 30
    res = bt.backtest(events, px, ss, horizon=10, min_samples=30)
    assert res.overall.insufficient_data is True
    assert res.readiness_verdict == "insufficient_data"
    assert "insufficient_samples_no_readiness_verdict" in res.warnings


def test_sufficient_samples_evaluated():
    ss = _sessions(n=200)
    px = _prices(ss)
    events = [bt.SignalEvent("AAA", ss[i], 1, 0.6, archetype="value",
                             is_consensus=True) for i in range(40)]
    res = bt.backtest(events, px, ss, horizon=10, min_samples=30)
    assert res.overall.sample_sufficient and res.readiness_verdict == "evaluated"
    # rising price + accumulation -> high directional hit rate
    assert res.overall.hit_rate is not None and res.overall.hit_rate > 0.9


def test_attribution_dimensions_present():
    ss = _sessions(n=200)
    px = _prices(ss)
    events = [bt.SignalEvent("AAA", ss[i], 1, 0.6, manager="m1", archetype="value",
                             event_type="new_position", fit_band="high",
                             freshness_band="fresh", crowding_band="low",
                             options_ambiguity_band="none", is_consensus=(i % 2 == 0))
              for i in range(40)]
    res = bt.backtest(events, px, ss, horizon=5, min_samples=30)
    for dim in ("manager", "archetype", "event_type", "fit_band", "freshness_band",
                "crowding_band", "options_ambiguity_band", "consensus_vs_single",
                "direction"):
        assert dim in res.by_dimension
    assert "consensus" in res.by_dimension["consensus_vs_single"]
    assert "single" in res.by_dimension["consensus_vs_single"]


def test_distribution_direction_bucket():
    ss = _sessions(n=200)
    px = _prices(ss, drift=-0.002)   # falling prices
    events = [bt.SignalEvent("AAA", ss[i], -1, 0.6) for i in range(40)]  # distribution
    res = bt.backtest(events, px, ss, horizon=10, min_samples=30)
    dist = res.by_dimension["direction"]["distribution"]
    assert dist.n == 40
    # falling price + distribution (dir=-1) -> directional correctness high
    assert dist.hit_rate is not None and dist.hit_rate > 0.9


def test_walk_forward_guard():
    ss = _sessions(n=200)
    px = _prices(ss)
    two = [bt.SignalEvent("AAA", ss[i], 1, 0.6) for i in range(2)]
    res = bt.backtest(two, px, ss, horizon=5, min_samples=1, walk_forward_folds=3)
    assert res.walk_forward_folds == 0
    assert "too_few_samples_for_walk_forward" in res.warnings
