"""Phase 9 tests — institutional_context_only -> market_context_only rename.

Asserts the backward-compatible compat-alias contract: new canonical state
value, deprecated alias equality, classifier emits the new value, and the writer
status emits BOTH top-keys (same data) plus the state_counts mirror — so every
existing consumer keeps working.
"""

from __future__ import annotations

from portfolio_automation.crowd_intelligence import unified_schema as us
from portfolio_automation.crowd_intelligence import unified_writer as uw
from portfolio_automation.crowd_intelligence.unified_schema import UnifiedCrowdRow


def test_new_state_value_and_alias():
    assert us.STATE_MARKET_CONTEXT_ONLY == "market_context_only"
    # Deprecated alias resolves to the SAME value (constant-based code/tests keep working).
    assert us.STATE_INSTITUTIONAL_ONLY == us.STATE_MARKET_CONTEXT_ONLY
    assert us.STATE_MARKET_CONTEXT_ONLY in us.CROWD_STATES
    assert us.SCHEMA_VERSION == "2"


def test_classifier_emits_new_value():
    from portfolio_automation.crowd_intelligence import unified_bus as ub
    # f high, r low -> market context only.
    state = ub._classify(r=0.0, f=0.8, confirmation=0.0, divergence=0.8,
                         breadth_total=1, breadth_fmp=1,
                         social_present=False, fmp_present=True)
    assert state == us.STATE_MARKET_CONTEXT_ONLY == "market_context_only"


def _row(ticker, state):
    return UnifiedCrowdRow(ticker=ticker, generated_at="2026-07-20T00:00:00Z",
                           crowd_state=state, crowd_confidence=0.5)


def test_writer_emits_both_top_keys_same_data():
    rows = [_row("AAA", us.STATE_MARKET_CONTEXT_ONLY),
            _row("BBB", us.STATE_MARKET_CONTEXT_ONLY),
            _row("CCC", us.STATE_CONFIRMED_ATTENTION)]
    status = uw.build_status(rows, generated_at="2026-07-20T00:00:00Z", social_available=False,
                             fmp_available=True, enabled_categories=["news"],
                             disabled_categories=[], warnings=[])
    # New key present.
    assert "top_market_context_only" in status
    # Deprecated alias key still present (compat) with identical data.
    assert "top_institutional_context_only" in status
    assert (status["top_market_context_only"]
            == status["top_institutional_context_only"])
    tickers = {t["ticker"] for t in status["top_market_context_only"]}
    assert tickers == {"AAA", "BBB"}


def test_state_counts_mirror():
    rows = [_row("AAA", us.STATE_MARKET_CONTEXT_ONLY)]
    status = uw.build_status(rows, generated_at="2026-07-20T00:00:00Z", social_available=False,
                             fmp_available=True, enabled_categories=["news"],
                             disabled_categories=[], warnings=[])
    sc = status["state_counts"]
    assert sc["market_context_only"] == 1
    # Deprecated literal key mirrors the same count (consumers indexing the old
    # key keep working); not additive.
    assert sc["institutional_context_only"] == 1
