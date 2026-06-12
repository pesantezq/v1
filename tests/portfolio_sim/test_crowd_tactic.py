"""Tests for the crowd-signal tactic: sleeve, overlay, proxy mapper."""
from __future__ import annotations

from portfolio_automation.portfolio_sim.crowd_tactic import (
    build_crowd_sleeve,
    proxy_pseudo_state,
)

CORE = {"QQQ": 0.6, "GLD": 0.4}


def _state(ticker, st, prio=1.0):
    return {"ticker": ticker, "crowd_state": st, "crowd_research_priority_score": prio}


def test_sleeve_caps_respected():
    states = [_state("NVDA", "emerging_dd", 5), _state("AMD", "crowd_validation", 3),
              _state("PLTR", "contrarian_neglect", 2)]
    w, flags, sleeve = build_crowd_sleeve(CORE, states, sleeve_total=0.15, per_idea=0.05)
    assert abs(sum(w.values()) - 1.0) < 1e-9
    sleeve_sum = sum(w[t] for t in sleeve)
    assert sleeve_sum <= 0.15 + 1e-9
    for t in sleeve:
        assert w[t] <= 0.05 + 1e-9


def test_priority_weighting_orders_sleeve():
    states = [_state("HI", "emerging_dd", 10), _state("LO", "emerging_dd", 1)]
    w, _, sleeve = build_crowd_sleeve(CORE, states, priority_weighted=True)
    assert w["HI"] >= w["LO"]


def test_caution_excluded_and_core_trimmed():
    # QQQ (a core holding) is in caution → ×0.8 trim + flag; GME caution never enters sleeve
    states = [_state("QQQ", "hype_acceleration"), _state("GME", "reflexive_squeeze_risk"),
              _state("NVDA", "emerging_dd", 4)]
    w, flags, sleeve = build_crowd_sleeve(CORE, states)
    assert "QQQ" in flags
    assert "GME" not in sleeve and "GME" not in w
    assert "NVDA" in sleeve
    # QQQ trimmed relative to GLD vs the untrimmed 0.6/0.4 split
    assert w["QQQ"] / (w["QQQ"] + w["GLD"]) < 0.6


def test_no_useful_states_is_core_only():
    w, flags, sleeve = build_crowd_sleeve(CORE, [])
    assert sleeve == []
    assert abs(sum(w.values()) - 1.0) < 1e-9


def test_proxy_pseudo_state_mapping():
    assert proxy_pseudo_state(volume_z=4.0, momentum=0.10) == "hype_acceleration"
    assert proxy_pseudo_state(volume_z=2.0, momentum=-0.05) == "crowd_exhaustion"
    assert proxy_pseudo_state(volume_z=1.2, momentum=0.03) == "emerging_dd"
    assert proxy_pseudo_state(volume_z=0.2, momentum=0.0) == "dormant_noise"
