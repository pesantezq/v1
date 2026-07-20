"""Phase 6 tests — position-change engine.

Covers: new, increase, reduction, exit, unchanged tolerance, split-like change,
missing previous filing (comparison_unavailable), options separated from shares,
identity_unresolved, no infinite % on new/exit, weight/rank/top10, turnover.
"""

from __future__ import annotations

from datetime import date

from portfolio_automation.institutional_intelligence import position_changes as pc
from portfolio_automation.institutional_intelligence.schemas import (
    PUT_CALL_CALL,
    PUT_CALL_NONE,
    PUT_CALL_PUT,
    ParsedHolding,
)
from portfolio_automation.institutional_intelligence.security_identity import (
    SecurityIdentity,
)


def _h(cusip, value, shares, put_call=PUT_CALL_NONE, issuer="ISS"):
    return ParsedHolding(issuer_name=issuer, class_title="COM", cusip=cusip,
                         value=value, shares_or_principal=shares,
                         share_principal_type="SH", put_call=put_call)


def _id(symbol, cusip, resolved=True, reason=None):
    return SecurityIdentity(cusip=cusip, figi=None, symbol=symbol,
                            method="cusip_exact" if resolved else "unresolved",
                            resolved=resolved, reason=reason)


def _pair(symbol, cusip, value, shares, put_call=PUT_CALL_NONE, resolved=True):
    return (_h(cusip, value, shares, put_call), _id(symbol, cusip, resolved))


def _events(cs):
    return {c.symbol or c.cusip: c.event for c in cs.changes}


def test_new_position_no_infinite_pct():
    cur = [_pair("AAA", "111", 1000, 500)]
    prev = [_pair("BBB", "222", 1000, 500)]
    cs = pc.compute_position_changes(cur, prev)
    aaa = next(c for c in cs.changes if c.symbol == "AAA")
    assert aaa.event == pc.EV_NEW
    assert aaa.shares_pct_change is None    # never infinite


def test_increase_reduce_unchanged():
    prev = [_pair("AAA", "111", 1000, 1000)]
    # +30% -> increased
    inc = pc.compute_position_changes([_pair("AAA", "111", 1300, 1300)], prev)
    assert _events(inc)["AAA"] == pc.EV_INCREASED
    # -30% -> reduced
    red = pc.compute_position_changes([_pair("AAA", "111", 700, 700)], prev)
    assert _events(red)["AAA"] == pc.EV_REDUCED
    # +0.5% -> unchanged (within tolerance)
    unch = pc.compute_position_changes([_pair("AAA", "111", 1005, 1005)], prev)
    assert _events(unch)["AAA"] == pc.EV_UNCHANGED


def test_exit():
    prev = [_pair("AAA", "111", 1000, 1000)]
    cs = pc.compute_position_changes([], prev)
    aaa = next(c for c in cs.changes if c.symbol == "AAA")
    assert aaa.event == pc.EV_EXITED and aaa.shares_pct_change is None


def test_comparison_unavailable_on_first_filing():
    cs = pc.compute_position_changes([_pair("AAA", "111", 1000, 500)], None)
    assert not cs.comparison_available
    assert cs.changes[0].event == pc.EV_COMPARISON_UNAVAILABLE   # not a flood of "new"


def test_split_like_not_treated_as_increase():
    prev = [_pair("AAA", "111", 1000, 1000)]
    # 2:1 split: shares double, value ~unchanged -> unchanged + possible_split
    cs = pc.compute_position_changes([_pair("AAA", "111", 1000, 2000)], prev)
    aaa = next(c for c in cs.changes if c.symbol == "AAA")
    assert aaa.event == pc.EV_UNCHANGED and aaa.possible_split
    assert "possible_split" in aaa.warnings


def test_real_increase_not_flagged_split():
    prev = [_pair("AAA", "111", 1000, 1000)]
    # shares +40%, value +40% -> genuine increase, not a split
    cs = pc.compute_position_changes([_pair("AAA", "111", 1400, 1400)], prev)
    aaa = next(c for c in cs.changes if c.symbol == "AAA")
    assert aaa.event == pc.EV_INCREASED and not aaa.possible_split


def test_options_separated_from_shares():
    # Same issuer, ordinary shares increase but a NEW put appears — distinct events.
    prev = [_pair("AAA", "111", 1000, 1000)]
    cur = [_pair("AAA", "111", 1500, 1500),
           _pair("AAA", "111", 200, 10, put_call=PUT_CALL_PUT)]
    cs = pc.compute_position_changes(cur, prev)
    events = [(c.put_call, c.event) for c in cs.changes]
    assert (PUT_CALL_NONE, pc.EV_INCREASED) in events
    assert (PUT_CALL_PUT, pc.EV_NEW_PUT) in events


def test_call_events():
    prev = [_pair("AAA", "111", 100, 10, put_call=PUT_CALL_CALL)]
    inc = pc.compute_position_changes([_pair("AAA", "111", 200, 20, put_call=PUT_CALL_CALL)], prev)
    assert inc.changes[0].event == pc.EV_INCREASED_CALL
    ext = pc.compute_position_changes([], prev)
    assert ext.changes[0].event == pc.EV_EXITED_CALL


def test_identity_unresolved_event():
    cur = [(_h("999", 500, 100), _id(None, "999", resolved=False, reason="no_mapping"))]
    cs = pc.compute_position_changes(cur, [])
    assert cs.changes[0].event == pc.EV_IDENTITY_UNRESOLVED
    assert cs.changes[0].symbol is None


def test_weight_rank_top10():
    prev = [_pair("SMALL", "111", 100, 100)]
    cur = [_pair("BIG", "222", 9000, 9000), _pair("SMALL", "111", 1000, 1000)]
    cs = pc.compute_position_changes(cur, prev)
    big = next(c for c in cs.changes if c.symbol == "BIG")
    assert big.curr_rank == 1 and big.top10_entry     # new #1
    assert 0.89 < big.curr_weight < 0.91              # 9000/10000
    small = next(c for c in cs.changes if c.symbol == "SMALL")
    assert small.weight_delta is not None             # both weights present


def test_portfolio_turnover_bounded():
    prev = [_pair("AAA", "111", 1000, 1000)]
    cur = [_pair("AAA", "111", 500, 500), _pair("BBB", "222", 500, 500)]
    cs = pc.compute_position_changes(cur, prev)
    assert cs.portfolio_turnover is not None
    assert 0.0 <= cs.portfolio_turnover <= 1.0


def test_filing_age_computed():
    cs = pc.compute_position_changes([_pair("AAA", "111", 1000, 500)], [],
                                     as_of=date(2026, 6, 8), current_filed_at=date(2026, 5, 15))
    assert cs.changes[0].filing_age_days == 24
