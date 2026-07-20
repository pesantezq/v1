"""Tests for the read-only 'Today's Capital Plan' memo view model.

Covers the 20 acceptance cases for the daily-memo capital-plan redesign. All
tests use synthetic in-memory dicts (pure-function inputs) so they never depend
on live FMP/broker state or on-disk artifacts.

Guardrails asserted: the view model is read-only (no mutation of inputs),
observe-only, never fabricates capital, never counts sale proceeds as
deployable, and never silently substitutes $0 for a missing value.
"""

from __future__ import annotations

import copy

import pytest

from portfolio_automation import capital_plan_view as cpv


# ---------------------------------------------------------------------------
# fixtures / builders
# ---------------------------------------------------------------------------

def _funded_action(symbol, funded_capital, funding_source="cash_on_hand",
                   priority=0.55, tranche_type="standard", held_for_pullback=0.0):
    return {
        "symbol": symbol,
        "funded_capital": funded_capital,
        "funding_source": funding_source,
        "priority": priority,
        "conviction_band": "normal",
        "status": "FUNDED_STANDARD",
        "tranche_type": tranche_type,
        "pct_of_portfolio": 1.0,
        "pct_of_net_investable": 7.0,
        "held_for_pullback": held_for_pullback,
        "sector": None,
    }


def _action(symbol, decision="BUY", source="market", confidence=0.8,
            entry_move_pct=1.0, entry_extended=False, thesis=None,
            primary_risk=None, is_existing_holding=False):
    return {
        "symbol": symbol,
        "decision": decision,
        "source": source,
        "confidence": confidence,
        "entry_move_pct": entry_move_pct,
        "entry_extended": entry_extended,
        "primary_thesis": thesis or f"momentum: {entry_move_pct:+.2f}% today, RS: moderate (-14.0% vs high) | {symbol}.",
        "primary_risk": primary_risk,
        "is_existing_holding": is_existing_holding,
    }


def _deferred(symbol, decision="BUY", requested_capital=0.0,
              blocking_reason="DEFERRED_BY_WEEKLY_PACING", priority=0.55,
              thesis=None, entry_move_pct=1.5):
    return {
        "symbol": symbol,
        "decision": decision,
        "requested_capital": requested_capital,
        "blocking_reason": blocking_reason,
        "presentation_state": blocking_reason,
        "priority": priority,
        "primary_thesis": thesis or f"momentum: {entry_move_pct:+.2f}% today, RS: near 52wk high (-3.0%) | {symbol}.",
        "entry_move_pct": entry_move_pct,
    }


def _dp_row(symbol, decision="BUY", recommended_amount=None, reason="",
            inputs_used=None):
    return {
        "symbol": symbol,
        "decision": decision,
        "recommended_amount": recommended_amount,
        "reason": reason,
        "inputs_used": inputs_used or {},
    }


def _coherence(funded=None, deferred=None, actions=None, *, available=True,
               available_cash=3000.0, reserve=500.0, deployable_cash=2500.0,
               deployable_incoming=0.0, funded_capital=None,
               gross_sized=None, envelope=None):
    funded = funded or []
    if funded_capital is None:
        funded_capital = round(sum(_num(f["funded_capital"]) for f in funded), 2)
    if gross_sized is None:
        gross_sized = funded_capital
    return {
        "funding": {
            "available": available,
            "available_cash": available_cash,
            "cash_reserve_amount": reserve,
            "deployable_from_cash": deployable_cash,
            "deployable_from_incoming": deployable_incoming,
            "gross_recommended_sized": gross_sized,
            "funded_capital": funded_capital,
            "unfunded_capital": 0.0,
            "funded_count": len(funded),
            "blocked_count": len(deferred or []),
            "below_safety_floor": False,
            "funded_actions": funded,
            "blocked_actions": [],
            "monthly_envelope": envelope or {},
        },
        "actions": actions or [],
        "deferred_actions": deferred or [],
        "ranking": {"tie_break_rule": "priority desc → momentum desc → symbol asc"},
    }


def _cash_plan(cash_on_hand=3000.0, incoming=1000.0, reserve=500.0,
               deployable_cash=2500.0, weekly_pacing=None):
    return {
        "monthly_capital_envelope": {
            "cash_on_hand": cash_on_hand,
            "monthly_contribution_gross": incoming,
            "monthly_contribution_net_investable": incoming,
            "cash_reserve_target_amount": reserve,
            "deployable_cash": deployable_cash,
            "weekly_pacing": weekly_pacing or {"weekly_tranche": 250.0,
                                               "weekly_remaining": 100.0},
        },
        "cash_summary": {"cash_available": cash_on_hand},
    }


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# 1. Gross recommendation capital exceeds available capital
# ---------------------------------------------------------------------------

def test_gross_exceeds_available_capital():
    funded = [_funded_action("AAA", 300.0)]
    deferred = [_deferred("BBB", requested_capital=3000.0,
                          blocking_reason="BLOCKED_BY_CASH")]
    coh = _coherence(funded=funded, deferred=deferred,
                     deployable_cash=300.0, deployable_incoming=0.0)
    view = cpv.build_capital_plan_view(coh, _cash_plan(deployable_cash=300.0),
                                       {"decisions": []})
    cs = view["capital_summary"]
    gross = cs["gross_recommended_capital"]["amount"]
    deployable = cs["deployable_capital"]["amount"]
    assert gross == pytest.approx(300.0 + 3000.0)
    assert gross > deployable
    # funded + deferred reconciles to gross
    assert view["reconciliation_status"] == "ok"


# ---------------------------------------------------------------------------
# 2. Portfolio rebalance (funded SCALE) prioritized over market opportunity
# ---------------------------------------------------------------------------

def test_rebalance_ranks_above_market_opportunity():
    funded = [
        _funded_action("MKT", 100.0, priority=0.55),   # BUY market opp
        _funded_action("HOLDCO", 100.0, priority=0.55),  # SCALE existing
    ]
    actions = [
        _action("MKT", decision="BUY", source="market", confidence=0.9),
        _action("HOLDCO", decision="SCALE", source="portfolio",
                is_existing_holding=True),
    ]
    coh = _coherence(funded=funded, actions=actions)
    view = cpv.build_capital_plan_view(coh, _cash_plan(), {"decisions": []})
    fa = view["funded_actions"]
    # The funded increase (SCALE) must rank above the new market starter.
    order = [a["symbol"] for a in fa]
    assert order.index("HOLDCO") < order.index("MKT")
    assert fa[0]["category"] == "funded_increase"


# ---------------------------------------------------------------------------
# 3. SCALE renders as investor-friendly "INCREASE"
# ---------------------------------------------------------------------------

def test_scale_renders_as_increase():
    assert cpv.investor_label("SCALE", funded=True, tranche_type="standard",
                              is_existing_holding=True) == "INCREASE"
    funded = [_funded_action("HOLDCO", 200.0)]
    actions = [_action("HOLDCO", decision="SCALE", is_existing_holding=True)]
    coh = _coherence(funded=funded, actions=actions)
    view = cpv.build_capital_plan_view(coh, _cash_plan(), {"decisions": []})
    md = "\n".join(cpv.render_capital_plan_md(view, markdown=True))
    assert "INCREASE HOLDCO" in md
    assert "SCALE HOLDCO" not in md  # the raw enum is not shown to the operator


# ---------------------------------------------------------------------------
# 4. Sell count cannot appear without detail (explicit missing-detail warning)
# ---------------------------------------------------------------------------

def test_sell_without_detail_shows_explicit_warning():
    dp = {"decisions": [
        _dp_row("drift_VFH_2026-07-20", decision="SELL", recommended_amount=None,
                reason="VFH underweight - rebalance needed."),
    ]}
    view = cpv.build_capital_plan_view(_coherence(), _cash_plan(), dp)
    sells = view["sell_actions"]
    assert len(sells) == 1
    assert sells[0]["symbol"] == "VFH"  # synthetic drift symbol cleaned
    assert sells[0]["detail_available"] is False
    md = "\n".join(cpv.render_capital_plan_md(view, markdown=True))
    assert "execution details" in md
    assert "No projected proceeds were included" in md


# ---------------------------------------------------------------------------
# 5. Funded actions show exact dollar amounts + funding sources
# ---------------------------------------------------------------------------

def test_funded_actions_show_amount_and_source():
    funded = [_funded_action("AAA", 123.45, funding_source="incoming_contributions")]
    coh = _coherence(funded=funded, actions=[_action("AAA")])
    view = cpv.build_capital_plan_view(coh, _cash_plan(), {"decisions": []})
    a = view["funded_actions"][0]
    assert a["funded_capital"]["amount"] == pytest.approx(123.45)
    assert a["funding_source"] == "Incoming contributions"
    md = "\n".join(cpv.render_capital_plan_md(view, markdown=True))
    assert "$123" in md
    assert "Incoming contributions" in md


# ---------------------------------------------------------------------------
# 6. Deferred actions include deterministic reasons
# ---------------------------------------------------------------------------

def test_deferred_reasons_deterministic():
    deferred = [
        _deferred("AAA", blocking_reason="DEFERRED_BY_WEEKLY_PACING"),
        _deferred("BBB", blocking_reason="DEFERRED_BY_MONTHLY_BUDGET"),
    ]
    coh = _coherence(deferred=deferred)
    view = cpv.build_capital_plan_view(coh, _cash_plan(), {"decisions": []})
    da = {d["symbol"]: d for d in view["deferred_actions"]}
    assert "week" in da["AAA"]["reason_plain"].lower()
    assert "month" in da["BBB"]["reason_plain"].lower()
    assert da["AAA"]["would_fund_when"]
    assert da["BBB"]["would_fund_when"]


# ---------------------------------------------------------------------------
# 7. Raw momentum/RS become understandable entry guidance
# ---------------------------------------------------------------------------

def test_entry_setup_translations():
    # near 52wk high + up today -> elevated entry risk
    r1 = {"entry_move_pct": 1.5, "primary_thesis": "momentum: +1.5% today, RS: near 52wk high (-2.0%)"}
    assert "elevated" in cpv.entry_setup(r1)["guidance"].lower()
    # near high + down today -> weakness may provide entry
    r2 = {"entry_move_pct": -1.2, "primary_thesis": "momentum: -1.2% today, RS: near 52wk high (-3.2%)"}
    assert "weakness" in cpv.entry_setup(r2)["guidance"].lower()
    # moderate RS + up sharply -> chase risk
    r3 = {"entry_move_pct": 3.5, "primary_thesis": "momentum: +3.5% today, RS: moderate (-12.0% vs high)"}
    assert "chase" in cpv.entry_setup(r3)["guidance"].lower()
    # well below high -> recovery candidate
    r4 = {"entry_move_pct": 0.1, "primary_thesis": "momentum: +0.1% today, RS: weak (-30.0% vs high)"}
    assert "recovery" in cpv.entry_setup(r4)["guidance"].lower()
    # preserves raw details
    assert "Distance from 52-week high" in (cpv.entry_setup(r2)["details"] or "")
    # no data -> honest unavailable, not an invented read
    assert cpv.entry_setup({"primary_thesis": ""})["available"] is False


# ---------------------------------------------------------------------------
# 8. Identical priority still produces stable ordering
# ---------------------------------------------------------------------------

def test_identical_priority_stable_order():
    funded = [
        _funded_action("ZZZ", 100.0, priority=0.55),
        _funded_action("AAA", 100.0, priority=0.55),
        _funded_action("MMM", 100.0, priority=0.55),
    ]
    actions = [_action(s, decision="BUY", confidence=0.9)
               for s in ("ZZZ", "AAA", "MMM")]
    coh = _coherence(funded=funded, actions=actions)
    v1 = cpv.build_capital_plan_view(coh, _cash_plan(), {"decisions": []})
    v2 = cpv.build_capital_plan_view(coh, _cash_plan(), {"decisions": []})
    order1 = [a["symbol"] for a in v1["funded_actions"]]
    order2 = [a["symbol"] for a in v2["funded_actions"]]
    assert order1 == order2 == ["AAA", "MMM", "ZZZ"]  # alphabetical tie-break


# ---------------------------------------------------------------------------
# 9. Missing capital fields do not silently become zero
# ---------------------------------------------------------------------------

def test_missing_reserve_not_zeroed():
    coh = _coherence(funded=[_funded_action("AAA", 100.0)])
    coh["funding"]["cash_reserve_amount"] = None
    cash = _cash_plan()
    cash["monthly_capital_envelope"]["cash_reserve_target_amount"] = None
    view = cpv.build_capital_plan_view(coh, cash, {"decisions": []})
    reserve = view["capital_summary"]["required_reserve"]
    assert reserve["amount"] is None
    assert reserve["state"] == "missing"
    md = "\n".join(cpv.render_capital_plan_md(view, markdown=True))
    assert "Required cash reserve: unavailable" in md
    assert "Required cash reserve: $0" not in md


# ---------------------------------------------------------------------------
# 10. Sell proceeds are excluded until marked available
# ---------------------------------------------------------------------------

def test_sell_proceeds_excluded_from_funding():
    dp = {"decisions": [
        _dp_row("XYZ", decision="SELL", recommended_amount=240.0,
                reason="Reduce XYZ exposure."),
    ]}
    funded = [_funded_action("AAA", 100.0)]
    view = cpv.build_capital_plan_view(_coherence(funded=funded), _cash_plan(), dp)
    sell = view["sell_actions"][0]
    assert sell["proceeds_available"] is False
    assert sell["dependent_funded_symbols"] == []
    # funded capital is unchanged by the sell proceeds
    assert view["capital_summary"]["funded_capital"]["amount"] == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# 11. Funded, deferred, gross totals reconcile (sized deferred)
# ---------------------------------------------------------------------------

def test_totals_reconcile_when_sized():
    funded = [_funded_action("AAA", 200.0)]
    deferred = [_deferred("BBB", requested_capital=300.0,
                          blocking_reason="BLOCKED_BY_CASH")]
    view = cpv.build_capital_plan_view(_coherence(funded=funded, deferred=deferred),
                                       _cash_plan(), {"decisions": []})
    cs = view["capital_summary"]
    assert cs["funded_capital"]["amount"] == pytest.approx(200.0)
    assert cs["deferred_capital"]["amount"] == pytest.approx(300.0)
    assert cs["gross_recommended_capital"]["amount"] == pytest.approx(500.0)
    assert view["reconciliation_status"] == "ok"
    assert view["funding_warnings"] == []


# ---------------------------------------------------------------------------
# 12. A reconciliation mismatch produces a visible advisory
# ---------------------------------------------------------------------------

def test_reconciliation_mismatch_warns():
    # funded_capital says 200 but the funded action amounts sum to 100.
    funded = [_funded_action("AAA", 100.0)]
    coh = _coherence(funded=funded, funded_capital=200.0)
    view = cpv.build_capital_plan_view(coh, _cash_plan(), {"decisions": []})
    assert view["reconciliation_status"] == "mismatch"
    assert view["funding_warnings"]
    md = "\n".join(cpv.render_capital_plan_md(view, markdown=True))
    assert "⚠" in md
    assert "warning" in md.lower() and "do not sum" in md.lower()


# ---------------------------------------------------------------------------
# 13. No action / score / symbol / amount is mutated by memo generation
# ---------------------------------------------------------------------------

def test_no_mutation_of_inputs():
    coh = _coherence(funded=[_funded_action("AAA", 100.0)],
                     deferred=[_deferred("BBB")],
                     actions=[_action("AAA", decision="SCALE")])
    cash = _cash_plan()
    dp = {"decisions": [_dp_row("XYZ", decision="SELL", recommended_amount=None)]}
    coh_before = copy.deepcopy(coh)
    cash_before = copy.deepcopy(cash)
    dp_before = copy.deepcopy(dp)
    cpv.build_capital_plan_view(coh, cash, dp)
    assert coh == coh_before
    assert cash == cash_before
    assert dp == dp_before


# ---------------------------------------------------------------------------
# 14. Governance semantics unchanged (observe-only, no production/sim mutation)
# ---------------------------------------------------------------------------

def test_observe_only_and_no_trade():
    view = cpv.build_capital_plan_view(_coherence(funded=[_funded_action("AAA", 10.0)]),
                                       _cash_plan(), {"decisions": []})
    assert view["observe_only"] is True
    assert view["no_trade"] is True
    # The machine-readable decision enum is never rewritten on the input rows.
    assert "approval" not in view
    assert "production" not in view


# ---------------------------------------------------------------------------
# 15. Rendering is deterministic and idempotent
# ---------------------------------------------------------------------------

def test_deterministic_idempotent_render():
    coh = _coherence(funded=[_funded_action("AAA", 100.0)],
                     deferred=[_deferred("BBB")], actions=[_action("AAA")])
    v1 = cpv.build_capital_plan_view(coh, _cash_plan(), {"decisions": []})
    v2 = cpv.build_capital_plan_view(coh, _cash_plan(), {"decisions": []})
    # Ignore the timestamp field.
    v1.pop("generated_at"); v2.pop("generated_at")
    assert v1 == v2
    assert cpv.render_capital_plan_md(v1) == cpv.render_capital_plan_md(v2)


# ---------------------------------------------------------------------------
# 16. Empty funded-action case
# ---------------------------------------------------------------------------

def test_empty_funded_actions():
    deferred = [_deferred("AAA")]
    view = cpv.build_capital_plan_view(_coherence(deferred=deferred, funded_capital=0.0),
                                       _cash_plan(), {"decisions": []})
    assert view["funded_actions"] == []
    md = "\n".join(cpv.render_capital_plan_md(view, markdown=True))
    assert "No actions are funded" in md
    assert "No capital is funded" in view["bottom_line"] or "funded" in view["bottom_line"]


# ---------------------------------------------------------------------------
# 17. No incoming contribution case
# ---------------------------------------------------------------------------

def test_no_incoming_contribution():
    cash = _cash_plan(incoming=0.0)
    view = cpv.build_capital_plan_view(_coherence(funded=[_funded_action("AAA", 10.0)]),
                                       cash, {"decisions": []})
    inc = view["capital_summary"]["incoming_contributions"]
    assert inc["amount"] == pytest.approx(0.0)
    assert inc["state"] == "confirmed"
    assert "no incoming" in (inc["note"] or "").lower()


# ---------------------------------------------------------------------------
# 18. Cash below reserve case
# ---------------------------------------------------------------------------

def test_cash_below_reserve():
    coh = _coherence(funded=[], deployable_cash=0.0, deployable_incoming=0.0,
                     funded_capital=0.0)
    view = cpv.build_capital_plan_view(coh, _cash_plan(deployable_cash=0.0),
                                       {"decisions": []})
    dep = view["capital_summary"]["deployable_capital"]
    assert (dep["amount"] or 0.0) <= 0.0
    assert "no deployable cash above reserve" in (dep["note"] or "")


# ---------------------------------------------------------------------------
# 19. All recommendations fully funded case
# ---------------------------------------------------------------------------

def test_all_fully_funded():
    funded = [_funded_action("AAA", 100.0), _funded_action("BBB", 200.0)]
    view = cpv.build_capital_plan_view(_coherence(funded=funded), _cash_plan(),
                                       {"decisions": []})
    cs = view["capital_summary"]
    assert cs["deferred_count"] == 0
    assert cs["deferred_capital"]["amount"] == pytest.approx(0.0)
    assert cs["deferred_capital"]["state"] == "confirmed"
    assert view["reconciliation_status"] == "ok"


# ---------------------------------------------------------------------------
# 20. More deferred actions than the display limit
# ---------------------------------------------------------------------------

def test_deferred_overflow_summarized():
    deferred = [_deferred(f"S{i:02d}",
                          blocking_reason="DEFERRED_BY_MONTHLY_BUDGET")
                for i in range(8)]
    coh = _coherence(deferred=deferred)
    view = cpv.build_capital_plan_view(coh, _cash_plan(),
                                       {"decisions": []},
                                       config={"max_deferred_displayed": 5})
    md = "\n".join(cpv.render_capital_plan_md(view, markdown=True))
    assert "3 additional action(s) deferred" in md
    # exactly 5 individual deferred lines rendered
    individual = [l for l in md.splitlines()
                  if l.startswith("- S") and "—" in l]
    assert len(individual) == 5


# ---------------------------------------------------------------------------
# Bonus: degraded funding -> honest, non-fabricated output
# ---------------------------------------------------------------------------

def test_funding_unavailable_degrades_honestly():
    coh = {"funding": {"available": False}, "actions": [], "deferred_actions": []}
    view = cpv.build_capital_plan_view(coh, {}, {"decisions": []})
    assert view["available"] is False
    assert view["reconciliation_status"] == "degraded"
    md = "\n".join(cpv.render_capital_plan_md(view, markdown=True))
    assert "unavailable" in md.lower()


def test_investor_labels():
    assert cpv.investor_label("BUY", funded=True, tranche_type="standard",
                              is_existing_holding=False) == "FULL BUY"
    assert cpv.investor_label("BUY", funded=True, tranche_type="starter_extended",
                              is_existing_holding=False) == "STARTER BUY"
    assert cpv.investor_label("BUY", funded=False, tranche_type=None,
                              is_existing_holding=False) == "DEFER"
    assert cpv.investor_label("WAIT", funded=False, tranche_type=None,
                              is_existing_holding=False) == "WATCH"
    assert cpv.investor_label("SELL", funded=False, tranche_type=None,
                              is_existing_holding=True) == "REDUCE"
