# tests/test_factor_liveness.py
"""Factor/filter liveness: can each documented scanner component influence results?

Field coverage (shipped 1c53aec0) answers "did the input arrive?". It cannot
answer "can this component change anything?". The live 2026-08-03 probe showed
99.6% primary coverage while a documented 15-point factor and a hard guard were
BOTH completely inert — `peRatio` resolved 0/503 because stable/key-metrics has no
peRatio/priceEarningsRatio, and the v3 fallback only fires when key-metrics
returns nothing at all.

A factor is NOT live merely because its source field exists. For a scoring factor
we require: input present, transformation executes, AND contribution varies across
the candidate set. For a hard filter we require: input present and the condition
actually evaluable (able to reject).

Measurement only — asserted below not to touch score, rank, or membership.
"""
from __future__ import annotations

from degraded_mode import FACTOR_STATUSES, assess_factor_liveness
from scanner.candidate_scanner import CandidateScanner, score_breakdown


def _m(symbol, rev=0.25, fcf=0.04, roe=0.2, pe=20.0):
    row = {"symbol": symbol}
    for key, val in (("revenueGrowth", rev), ("freeCashFlowYield", fcf),
                     ("roe", roe), ("peRatio", pe)):
        if val is not None:
            row[key] = val
    return row


def _q(price=100.0, dma=90.0):
    return {"price": price, "priceAvg200": dma}


def _inputs(n=10, **kw):
    """(eligible, metrics_by_symbol, quotes_by_symbol) for n synthetic symbols."""
    syms = [f"S{i}" for i in range(n)]
    metrics = {s: _m(s, **kw) for s in syms}
    quotes = {s: _q() for s in syms}
    return syms, metrics, quotes


def _assess(n=10, candidates=None, **kw):
    syms, metrics, quotes = _inputs(n, **kw)
    return assess_factor_liveness(
        eligible_symbols=syms, metrics_by_symbol=metrics, quotes_by_symbol=quotes,
        candidates=candidates if candidates is not None else [
            {"symbol": s, "score": 50.0} for s in syms],
        trend_filter_enabled=True, min_rev_growth=0.15,
    )


# --------------------------------------------------------------------------
# The headline case: PE inert
# --------------------------------------------------------------------------

def test_pe_absent_makes_both_score_factor_and_guard_inert():
    """The live condition. Two independent components, both dead."""
    r = _assess(n=20, pe=None)
    pe = r["factors"]["pe"]
    assert pe["input_coverage"] == 0.0
    assert pe["field_resolution"] == 0
    assert pe["score_nonzero_count"] == 0
    assert pe["score_variance"] == 0.0
    assert pe["status"] == "inert"
    guard = r["filters"]["pe_bubble_guard"]
    assert guard["evaluable"] == 0
    assert guard["rejections"] == 0
    assert guard["status"] == "inert"


def test_inert_pe_does_not_make_the_rollup_unsafe():
    """PE inert must be a DEGRADED observability finding, not a safety failure —
    treating it as unsafe would change production authority semantics."""
    r = _assess(n=20, pe=None)
    assert r["status"] == "degraded"
    assert r["suppresses_sleeve"] is False
    assert "pe" in r["inert_components"]


def test_pe_present_becomes_live_and_the_guard_can_reject():
    r = _assess(n=20, pe=12.0)
    assert r["factors"]["pe"]["status"] in ("live", "low_information")
    assert r["filters"]["pe_bubble_guard"]["evaluable"] == 20


def test_pe_guard_rejection_count_is_measured():
    syms, metrics, quotes = _inputs(4)
    metrics["S0"]["peRatio"] = 72.0      # rejects
    metrics["S1"]["peRatio"] = 60.0      # rejects
    metrics["S2"]["peRatio"] = 20.0
    metrics["S3"]["peRatio"] = 10.0
    r = assess_factor_liveness(eligible_symbols=syms, metrics_by_symbol=metrics,
                              quotes_by_symbol=quotes, candidates=[],
                              trend_filter_enabled=True, min_rev_growth=0.15)
    assert r["filters"]["pe_bubble_guard"]["rejections"] == 2
    assert r["filters"]["pe_bubble_guard"]["evaluable"] == 4


def test_negative_pe_is_counted_separately_and_never_as_a_guard_rejection():
    """A negative PE means loss-making, yet `pe > 50` passes it. That must be
    visible, not silently counted as 'guard OK'."""
    syms, metrics, quotes = _inputs(2)
    metrics["S0"]["peRatio"] = -615.0
    metrics["S1"]["peRatio"] = 20.0
    r = assess_factor_liveness(eligible_symbols=syms, metrics_by_symbol=metrics,
                              quotes_by_symbol=quotes, candidates=[],
                              trend_filter_enabled=True, min_rev_growth=0.15)
    g = r["filters"]["pe_bubble_guard"]
    assert g["rejections"] == 0
    assert g["negative_earnings_passing"] == 1


# --------------------------------------------------------------------------
# Healthy factors
# --------------------------------------------------------------------------

def test_revenue_growth_live_when_varied():
    syms = [f"S{i}" for i in range(10)]
    metrics = {s: _m(s, rev=0.16 + i * 0.02) for i, s in enumerate(syms)}
    r = assess_factor_liveness(eligible_symbols=syms, metrics_by_symbol=metrics,
                              quotes_by_symbol={s: _q() for s in syms},
                              candidates=[], trend_filter_enabled=True,
                              min_rev_growth=0.15)
    rg = r["factors"]["revenue_growth"]
    assert rg["status"] == "live"
    assert rg["input_coverage"] == 1.0
    assert rg["score_variance"] > 0
    assert rg["score_nonzero_count"] == 10


def test_constant_contribution_is_low_information_not_live():
    """Every symbol scoring the same points means the factor cannot discriminate,
    even though its input is fully present."""
    r = _assess(n=12, rev=0.25)          # identical rev growth for all
    rg = r["factors"]["revenue_growth"]
    assert rg["input_coverage"] == 1.0
    assert rg["score_variance"] == 0.0
    assert rg["status"] == "low_information"
    assert rg["status"] != "live"


def test_fcf_and_roe_and_trend_are_assessed():
    r = _assess(n=8)
    for name in ("fcf_yield", "roe", "trend"):
        assert name in r["factors"], name
    assert r["filters"]["fcf_negative_guard"]["evaluable"] == 8
    assert r["filters"]["trend_200dma"]["evaluable"] == 8


def test_trend_not_applicable_when_filter_disabled():
    syms, metrics, quotes = _inputs(5)
    r = assess_factor_liveness(eligible_symbols=syms, metrics_by_symbol=metrics,
                              quotes_by_symbol=quotes, candidates=[],
                              trend_filter_enabled=False, min_rev_growth=0.15)
    assert r["filters"]["trend_200dma"]["status"] == "not_applicable"


def test_missing_quotes_make_trend_inert_not_healthy():
    syms, metrics, _ = _inputs(5)
    r = assess_factor_liveness(eligible_symbols=syms, metrics_by_symbol=metrics,
                              quotes_by_symbol={}, candidates=[],
                              trend_filter_enabled=True, min_rev_growth=0.15)
    assert r["filters"]["trend_200dma"]["status"] == "inert"
    assert r["factors"]["trend"]["status"] == "inert"


# --------------------------------------------------------------------------
# Fail-closed / contract
# --------------------------------------------------------------------------

def test_no_eligible_universe_is_unknown_not_live():
    r = assess_factor_liveness(eligible_symbols=[], metrics_by_symbol={},
                               quotes_by_symbol={}, candidates=[],
                               trend_filter_enabled=True, min_rev_growth=0.15)
    assert r["status"] == "unknown"
    for f in r["factors"].values():
        assert f["status"] == "unknown"


def test_every_status_is_from_the_declared_vocabulary():
    r = _assess(n=6, pe=None)
    for block in (r["factors"], r["filters"]):
        for name, payload in block.items():
            assert payload["status"] in FACTOR_STATUSES, (name, payload["status"])


def test_payload_is_json_serializable_and_deterministic():
    import json
    a = _assess(n=9)
    b = _assess(n=9)
    assert a == b
    json.dumps(a)
    assert a["observe_only"] is True


# --------------------------------------------------------------------------
# Production-invariance: measuring must not change scoring
# --------------------------------------------------------------------------

def test_score_breakdown_reconciles_exactly_to_production_score():
    """score_breakdown mirrors _score's formulas. If either drifts, this fails —
    which is the drift protection that lets liveness measure contributions
    without refactoring production scoring."""
    sc = CandidateScanner(min_mkt_cap=5e9, min_rev_growth=0.15,
                          trend_filter_200dma=True, top_k=100)
    cases = [
        ({}, {}),
        (_m("A"), _q()),
        (_m("B", rev=0.5, fcf=0.09, roe=0.9, pe=8.0), _q(200.0, 100.0)),
        (_m("C", rev=None, fcf=None, roe=None, pe=None), _q(50.0, 90.0)),
        (_m("D", rev=0.15, fcf=-0.02, roe=0.0, pe=-10.0), _q(0.0, 0.0)),
        (_m("E", rev=0.39, fcf=0.049, roe=0.299, pe=50.0), _q(90.0, 90.0)),
    ]
    for metrics, quote in cases:
        expected = sc._score({}, metrics, quote)
        bd = score_breakdown(metrics, quote, min_rev_growth=0.15)
        assert round(min(100.0, sum(bd.values())), 9) == round(expected, 9), (metrics, bd)


def test_liveness_assessment_does_not_mutate_its_inputs():
    syms, metrics, quotes = _inputs(6)
    before = (repr(metrics), repr(quotes))
    cands = [{"symbol": s, "score": 40.0} for s in syms]
    cands_before = [dict(c) for c in cands]
    assess_factor_liveness(eligible_symbols=syms, metrics_by_symbol=metrics,
                           quotes_by_symbol=quotes, candidates=cands,
                           trend_filter_enabled=True, min_rev_growth=0.15)
    assert (repr(metrics), repr(quotes)) == before
    assert cands == cands_before


def test_full_scan_output_is_identical_with_and_without_liveness_computed():
    """The acceptance test for 'diagnostics change nothing': same membership,
    same scores, same order."""
    sc = CandidateScanner(min_mkt_cap=5e9, min_rev_growth=0.15,
                          trend_filter_200dma=True, top_k=100)
    syms = [f"S{i}" for i in range(15)]
    profiles = [{"symbol": s, "mktCap": 1e10, "sector": "Tech"} for s in syms]
    metrics = [_m(s, rev=0.16 + i * 0.01) for i, s in enumerate(syms)]
    quotes = {s: _q() for s in syms}

    before, _ = sc.full_scan(syms, profiles, metrics, quotes)
    snapshot = [(c["symbol"], c["score"]) for c in before]

    assess_factor_liveness(
        eligible_symbols=syms,
        metrics_by_symbol={m["symbol"]: m for m in metrics},
        quotes_by_symbol=quotes, candidates=before,
        trend_filter_enabled=True, min_rev_growth=0.15)

    after, _ = sc.full_scan(syms, profiles, metrics, quotes)
    assert [(c["symbol"], c["score"]) for c in after] == snapshot


# ---------------------------------------------------------------------------
# "We did not look" must not render as "we looked and it is dead" (2026-08-07)
# ---------------------------------------------------------------------------
# main.py:1454 read `bulk_metrics`, which is bound only in the monthly and
# weekly branches. Every DAILY run therefore raised
#   UnboundLocalError: cannot access local variable 'bulk_metrics'
# (logs/daily_safe_2026-08-06.log:675), swallowed to factor_liveness = None --
# so the whole feature was silently dark daily and the canary reported
# factor_liveness_absent.
#
# The naive repair -- passing {} -- is worse than the bug: with no metrics the
# assessor returns status "degraded" and NINE inert components, which would
# raise a false alarm every single day and bury the genuinely-inert PE factor
# that experiment pe_restoration_full_2026_08 is tracking. That is the same
# defect class this batch exists to remove: a verdict derived from absent data.

class TestFactorLivenessNotAssessable:
    def test_none_metrics_reads_not_assessable_not_degraded(self):
        r = assess_factor_liveness(
            eligible_symbols=["AAPL", "MSFT"], metrics_by_symbol=None,
            quotes_by_symbol={}, candidates=[])
        assert r["status"] == "not_assessable"
        assert r["inert_components"] == [], (
            "absent metrics must not be reported as inert components")
        assert "metrics_not_fetched_this_cadence" in r["reasons"]

    def test_empty_dict_still_means_looked_and_found_nothing(self):
        """{} is a real (if degenerate) observation; None is not."""
        r = assess_factor_liveness(
            eligible_symbols=["AAPL", "MSFT"], metrics_by_symbol={},
            quotes_by_symbol={}, candidates=[])
        assert r["status"] != "not_assessable"

    def test_not_assessable_never_suppresses_the_sleeve(self):
        r = assess_factor_liveness(
            eligible_symbols=["AAPL"], metrics_by_symbol=None,
            quotes_by_symbol={}, candidates=[])
        assert r["suppresses_sleeve"] is False
        assert r["observe_only"] is True

    def test_not_assessable_keeps_the_component_keys(self):
        """Consumers index into factors/filters unconditionally."""
        r = assess_factor_liveness(
            eligible_symbols=["AAPL"], metrics_by_symbol=None,
            quotes_by_symbol={}, candidates=[])
        assert set(r["factors"]) >= {"revenue_growth", "pe", "trend"}
        assert set(r["filters"]) >= {"pe_bubble_guard", "trend_200dma"}
        assert all(v["status"] == "not_assessable" for v in r["factors"].values())
