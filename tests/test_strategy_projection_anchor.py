"""run_portfolio_projection honours the active-strategy selection (re-anchor).

When a strategy is approved/active, the projection payload carries
``anchor_strategy_id`` (the operator's selection) and a ``selected_fan`` slot in
addition to the baseline ``anchor_fan``. With no selection, behaviour is
unchanged (``anchor_strategy_id`` is None).

Selection state is monkeypatched so the test is deterministic and does not write
to the real outputs/policy tree; prices come from the repo root.
"""
from __future__ import annotations

import portfolio_automation.portfolio_sim.run_portfolio_projection as proj


def test_projection_no_selection_anchor_is_none(monkeypatch):
    monkeypatch.setattr(proj, "load_active_selection", lambda root: {})
    r = proj.run_portfolio_projection(root=".", run_mode="weekly", write_files=False)
    assert r.get("status") in ("ok", "insufficient_data")
    assert r.get("anchor_strategy_id") is None
    # baseline fan slot remains
    assert "anchor_fan" in r


def test_projection_sets_anchor_strategy_id_when_selected(monkeypatch):
    monkeypatch.setattr(
        proj, "load_active_selection",
        lambda root: {"active_strategy_id": "long_term_compounding",
                      "status": "approved"},
    )
    r = proj.run_portfolio_projection(root=".", run_mode="weekly", write_files=False)
    assert r.get("anchor_strategy_id") == "long_term_compounding"
    # both fan slots present (baseline kept alongside selected)
    assert "anchor_fan" in r
    assert "selected_fan" in r


def test_projection_unresolvable_selection_anchor_is_none(monkeypatch):
    monkeypatch.setattr(
        proj, "load_active_selection",
        lambda root: {"active_strategy_id": "does_not_exist", "status": "approved"},
    )
    r = proj.run_portfolio_projection(root=".", run_mode="weekly", write_files=False)
    assert r.get("anchor_strategy_id") is None
