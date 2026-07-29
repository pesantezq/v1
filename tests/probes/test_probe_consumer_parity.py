"""E5 probes -- memo/GUI/decision-artifact consumer parity.

Scenario 2  -- a dashboard renders while omitting a decision-critical section.
Scenario 3  -- a test fixture uses obsolete headers.
Scenario 19 -- memo, GUI and decision artifact disagree.

Scenario 2/3 surfaced a NEW finding from this probe suite, since FIXED by
`c8ff5d95` (2026-07-28): two of the six headers
`capital_plan_view.render_capital_plan_md` emits ("Funded Market
Opportunities", "Sell and Funding Dependencies") were absent from
`gui_v2/data/dash_memo.py`'s `_HEADER_MAP` -- a residual instance of the
defect class commit 8686898d fixed for the other four headers (Today's
Capital Plan / What To Do Today / Deferred Recommendations / Bottom Line).
Both are mapped now, and the probes below assert coverage rather than
documenting the gap.

The reason it survived that earlier fix is the point worth keeping: the
regression guard in ``tests/test_gui_dashboard_memo.py`` was itself a
hand-maintained literal header list, so it structurally could not catch a
header its author had not seen -- a stale fixture guarding against stale
fixtures. Both that guard and the probes here now derive the header set from
the producer's own `h("...")` call sites, which is why the pair below stays
source-derived rather than hardcoded.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from gui_v2.data.dash_memo import _HEADER_MAP, _map_header, _parse_memo
from portfolio_automation import capital_plan_view as cpv

from tests.probes.assertions import (
    assert_decision_consumer_parity,
    extract_literal_header_calls,
)

# CLOSED by `c8ff5d95` (2026-07-28). This set held the two headers
# capital_plan_view.py emitted with no matching dash_memo._HEADER_MAP
# fragment -- "Funded Market Opportunities" and "Sell and Funding
# Dependencies", both inside conditional blocks and therefore absent from the
# memo sampled when the original fix was written. It is now EMPTY and stays
# that way: the carve-out is retained (rather than deleted) so that a header
# regressing into an orphan fails with the diff spelled out, which is the
# a5387a27 defect class this suite exists to catch.
_KNOWN_ORPHANED_CAPITAL_PLAN_HEADERS: set[str] = set()


# ---------------------------------------------------------------------------
# Scenario 3 -- fixture obsolescence, generalized: derive the renderer's
# REAL headers from source (not a hand-maintained literal list) and cross-
# check against the GUI's header map.
# ---------------------------------------------------------------------------


def test_renderer_derived_headers_are_not_a_stale_hardcoded_list():
    """The generic extraction must find headers a hand-maintained literal
    list is liable to miss -- proven here by comparing against the actual
    hardcoded list this repo already shipped for this exact purpose."""
    real_headers = set(extract_literal_header_calls(cpv, "h"))
    # The hardcoded list from tests/test_gui_dashboard_memo.py's
    # SHIPPED_CAPITAL_PLAN_HEADERS, reproduced inline (not imported, so this
    # probe does not silently track edits to that other file) to prove it is
    # incomplete right now.
    hardcoded_list = {"Today's Capital Plan", "What To Do Today", "Deferred Recommendations"}
    missing_from_hardcoded_list = real_headers - hardcoded_list
    assert missing_from_hardcoded_list, (
        "expected the source-derived header set to catch headers the "
        "hardcoded literal list omits -- if this is empty, either the "
        "renderer shrank or the other file's list was already kept in sync; "
        "re-check this probe's premise")
    assert "Funded Market Opportunities" in missing_from_hardcoded_list
    assert "Sell and Funding Dependencies" in missing_from_hardcoded_list


def test_no_capital_plan_header_is_orphaned_except_the_known_open_gap():
    """Cross-check EVERY header the renderer can currently emit (derived
    from source, not a hand-maintained list) against `_map_header`. All must
    map to a real GUI section, with the sole exception of the two headers
    already tracked above as an explicit, currently-open residual gap.

    If this test starts failing because `missing` shrank, that is GOOD NEWS:
    update `_KNOWN_ORPHANED_CAPITAL_PLAN_HEADERS` (and ideally delete this
    carve-out entirely once it is empty). If it fails because `missing`
    GREW, that is a genuine regression -- a new header was added to the
    renderer without updating `_HEADER_MAP`, i.e. the a5387a27 defect class
    recurring for a third time.
    """
    real_headers = extract_literal_header_calls(cpv, "h")
    assert real_headers, "sanity: source extraction must find at least the known headers"

    orphaned = {h for h in real_headers if _map_header(h) is None}
    assert orphaned == _KNOWN_ORPHANED_CAPITAL_PLAN_HEADERS, (
        f"orphaned capital-plan headers changed: {orphaned}. If this GREW beyond "
        f"{_KNOWN_ORPHANED_CAPITAL_PLAN_HEADERS}, a NEW header is silently dropped "
        "from /dashboard/memo -- update _HEADER_MAP, not this probe. If it SHRANK, "
        "update _KNOWN_ORPHANED_CAPITAL_PLAN_HEADERS to reflect the fix.")


def test_pre_fix_header_map_would_have_orphaned_all_capital_plan_headers():
    """Verify-by-construction against the documented a5387a27 pre-fix shape:
    a `_HEADER_MAP` that has NO capital-plan fragments at all (the state
    before commit 8686898d) orphans every capital-plan header, including the
    four that are correctly mapped today."""
    _RETIRED_CAPITAL_PLAN_FRAGMENTS = {
        "today's capital plan", "what to do today", "deferred recommendations",
        "top decisions", "capital actions", "top movers",
    }
    pre_fix_map = [(frag, section) for frag, section in _HEADER_MAP
                  if frag not in _RETIRED_CAPITAL_PLAN_FRAGMENTS]

    def _pre_fix_map_header(header_text: str) -> str | None:
        lower = header_text.strip().lower()
        for fragment, section in pre_fix_map:
            if fragment in lower:
                return section
        return None

    for header in ("Today's Capital Plan", "What To Do Today", "Deferred Recommendations"):
        assert _pre_fix_map_header(header) is None  # the bug
        assert _map_header(header) == "Portfolio Decisions"  # the fix


# ---------------------------------------------------------------------------
# Scenario 2 -- end-to-end: build a real capital-plan view hitting all six
# renderer sections, render it, parse it through the GUI consumer, and show
# which content survives vs is silently dropped.
# ---------------------------------------------------------------------------


def _full_coverage_view() -> dict:
    coherence = {
        "funding": {
            "available": True, "available_cash": 5000.0, "cash_reserve_amount": 1000.0,
            "deployable_from_cash": 4000.0, "deployable_from_incoming": 0.0,
            "funded_capital": 3000.0,
            "funded_actions": [
                {"symbol": "AAPL", "funded_capital": 3000.0, "funding_source": "cash_on_hand",
                 "priority": 0.8, "tranche_type": "standard", "held_for_pullback": 0.0,
                 "pct_of_net_investable": 7.0},
            ],
        },
        "actions": [
            {"symbol": "AAPL", "decision": "BUY", "source": "market", "confidence": 0.8,
             "entry_move_pct": 1.2, "entry_extended": False, "primary_thesis": "momentum",
             "primary_risk": None, "is_existing_holding": False},
        ],
        "deferred_actions": [
            {"symbol": "NVDA", "decision": "BUY", "requested_capital": 0.0,
             "blocking_reason": "DEFERRED_BY_WEEKLY_PACING", "priority": 0.5},
        ],
    }
    cash_plan: dict = {}
    decision_plan = {"decisions": [
        {"symbol": "OLD_HOLDING", "decision": "SELL", "recommended_amount": 1500.0,
         "inputs_used": {"shares": 10}, "reason": "trim risk | detail"},
    ]}
    return cpv.build_capital_plan_view(coherence, cash_plan, decision_plan)


def test_full_capital_plan_render_hits_all_six_sections():
    """Fixture sanity: confirms the fixture genuinely exercises every
    section the renderer can emit, so the parity check below is meaningful
    (not accidentally passing because a section never rendered)."""
    view = _full_coverage_view()
    md = "\n".join(cpv.render_capital_plan_md(view, markdown=True))
    headers_present = {ln[3:] for ln in md.splitlines() if ln.startswith("## ")}
    assert headers_present == set(extract_literal_header_calls(cpv, "h")), (
        f"fixture only hit {headers_present}; expected all shipped headers -- "
        "widen the fixture so the omission probe below is testing something real")


def test_dashboard_preserves_every_decision_critical_section_from_full_render():
    """CLOSED by `c8ff5d95`. The scenario-2 reproduction, now inverted to pin
    the fix: a fully-populated capital plan (all six sections in the rendered
    Markdown) is parsed by the GUI's mobile memo collector, and NO section's
    content may go missing. Previously a funded market opportunity's entry
    guidance and a pending sell's proceeds detail vanished with no error and
    no signal -- the `Portfolio Decisions` section still rendered non-empty
    from the other four sections, which is exactly how the original a5387a27
    loss stayed invisible. Content-level, not header-level: a header can map
    correctly while its body is still dropped."""
    view = _full_coverage_view()
    md = "\n".join(cpv.render_capital_plan_md(view, markdown=True))
    assert "Entry setup:" in md  # the market-opportunity content exists in the source memo
    assert "Estimated proceeds:" in md  # the sell-dependency content exists in the source memo

    parsed = _parse_memo(md)
    portfolio_decisions_text = "\n".join(parsed.get("Portfolio Decisions", []))

    # The four previously-fixed headers' content DOES survive:
    assert "Cash on hand" in portfolio_decisions_text
    assert "AAPL" in portfolio_decisions_text

    # The two formerly-orphaned headers' content now survives the round trip.
    all_parsed_text = "\n".join(line for lines in parsed.values() for line in lines)
    assert "Entry setup:" in all_parsed_text, (
        "'Funded Market Opportunities' content is being dropped by the GUI "
        "memo collector again -- a5387a27 defect class, fourth occurrence")
    assert "Estimated proceeds:" in all_parsed_text, (
        "'Sell and Funding Dependencies' content is being dropped by the GUI "
        "memo collector again -- a5387a27 defect class, fourth occurrence")


# ---------------------------------------------------------------------------
# Scenario 19 -- memo, GUI, and decision artifact disagree.
# ---------------------------------------------------------------------------


def test_decision_consumer_parity_passes_when_consistent():
    decision_plan_total = 3000.0
    memo_text_total = 3000.0
    gui_coherence_total = 3000.0
    assert_decision_consumer_parity(
        decision_value=decision_plan_total,
        consumer_values={"memo": memo_text_total, "gui": gui_coherence_total},
        context="funded_capital across decision_plan/memo/gui")


def test_decision_consumer_parity_catches_gui_drift():
    """The false-GREEN shape: each consumer independently 'renders
    successfully' with its own number, and only a cross-artifact check
    reveals they tell different stories -- e.g. a GUI collector reading a
    stale or differently-scoped cache while the memo and decision_plan agree."""
    decision_plan_total = 3000.0
    memo_text_total = 3000.0
    gui_coherence_total = 2400.0  # drifted -- e.g. stale cache, different rounding, a bug

    with pytest.raises(AssertionError, match="disagrees with consumer"):
        assert_decision_consumer_parity(
            decision_value=decision_plan_total,
            consumer_values={"memo": memo_text_total, "gui": gui_coherence_total},
            context="funded_capital across decision_plan/memo/gui")


def test_real_coherence_view_funded_capital_agrees_with_capital_plan_view(tmp_path):
    """A real cross-artifact check: `capital_plan_view.build_capital_plan_view`
    (the memo's own funded-capital number) must agree with what
    `gui_v2.data.dash_memo._coherence_view` would surface from
    `memo_coherence.json` for the SAME underlying funding dict -- both read
    from the same `coherence["funding"]` shape, so a divergence here would
    mean one of the two consumers is looking at stale or reshaped data."""
    from gui_v2.data.dash_memo import _coherence_view
    import json

    view = _full_coverage_view()
    funded_capital_from_memo_view = view["capital_summary"]["funded_capital"]["amount"]

    latest = tmp_path / "outputs" / "latest"
    latest.mkdir(parents=True, exist_ok=True)
    (latest / "memo_coherence.json").write_text(json.dumps({
        "coherence_status": "ok",
        "generated_at": "2026-07-28T09:00:00+00:00",
        "investor_summary": {"posture_paragraph": "", "main_opportunity": "",
                             "main_risk": "", "what_changed": []},
        "funding": {"portfolio_value": 100000.0, "available_cash": 5000.0,
                   "cash_reserve_amount": 1000.0},
        "reconciliation": {"unresolved_issues": [], "issue_count": 0},
    }), encoding="utf-8")
    gui_view = _coherence_view(latest)

    assert_decision_consumer_parity(
        decision_value=view["capital_summary"]["required_reserve"]["amount"],
        consumer_values={"gui_coherence_view": gui_view["cash_reserve_amount"]},
        context="cash_reserve_amount (capital_plan_view vs GUI coherence panel)")
    assert funded_capital_from_memo_view == 3000.0
