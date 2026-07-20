"""Phase 11 tests — institutional tilt primitive + Strategy Lab tactic/variants.

Covers: weights always sum to 1, no negative weights (long-only), sleeve cap,
per-symbol caps, distribution trim cap, no-signal->anchor unchanged, stale/weak
signal->no tilt, diagnostic variant isolated, variants use identical inputs,
feeds_decision_engine=false, PIT no-look-ahead signal selection, crowding-aware
dampening, contrarian flip, single-famous-manager does NOT get added.
"""

from __future__ import annotations

import math

from portfolio_automation.portfolio_sim import institutional_tilt as it

_CORE = {"AAA": 0.5, "BBB": 0.3, "CCC": 0.2}


def _sig(score, conf=0.7, eff=2.0, state="moderate_accumulation", crowding=0.2, fit=1.0):
    return {"consensus_score": score, "consensus_confidence": conf,
            "effective_independent_managers": eff, "consensus_state": state,
            "crowding_score": crowding, "strategy_fit": fit}


def _sum1(w):
    assert abs(sum(w.values()) - 1.0) < 1e-9
    assert all(v >= 0 for v in w.values())


def test_weights_sum_to_one_and_long_only():
    w = it.apply_institutional_tilt(_CORE, {"DDD": _sig(0.8)})
    _sum1(w)


def test_no_signal_anchor_unchanged():
    w = it.apply_institutional_tilt(_CORE, {})
    assert w == {k: round(v, 12) for k, v in it._normalize(_CORE).items()} or \
        all(abs(w[k] - v) < 1e-9 for k, v in it._normalize(_CORE).items())


def test_weak_confidence_no_tilt():
    # confidence below min -> no tilt (not added on one/weak manager)
    w = it.apply_institutional_tilt(_CORE, {"DDD": _sig(0.9, conf=0.4)})
    assert "DDD" not in w or w.get("DDD", 0) == 0


def test_low_effective_managers_no_tilt():
    w = it.apply_institutional_tilt(_CORE, {"DDD": _sig(0.9, eff=1.0)})  # < 1.5
    assert w.get("DDD", 0) == 0


def test_single_famous_manager_not_added():
    # one manager, high score but effective independent < min -> excluded
    w = it.apply_institutional_tilt(_CORE, {"NVDA": _sig(1.0, conf=0.9, eff=1.0)})
    assert "NVDA" not in w or w.get("NVDA", 0) == 0


def test_new_position_capped():
    caps = it.InstitutionalCaps(max_new_position=0.02)
    w = it.apply_institutional_tilt(_CORE, {"DDD": _sig(1.0, conf=1.0)}, caps)
    # DDD is new; its weight after normalization must be small (<= ~cap-ish).
    assert 0 < w["DDD"] < 0.03


def test_sleeve_cap_respected():
    caps = it.InstitutionalCaps(max_total_sleeve=0.04, max_new_position=0.02)
    many = {f"S{i}": _sig(1.0, conf=1.0) for i in range(10)}
    w = it.apply_institutional_tilt(_CORE, many, caps)
    added = sum(w[k] for k in many if k in w)
    # total added sleeve (pre-normalization ~0.04) stays bounded post-normalize.
    _sum1(w)
    assert added < 0.06


def test_distribution_trims_not_shorts():
    core = {"AAA": 0.5, "BBB": 0.5}
    w = it.apply_institutional_tilt(core, {"AAA": _sig(-0.9, state="strong_distribution")})
    _sum1(w)
    assert w["AAA"] >= 0                       # long-only, never negative
    assert w["AAA"] < it._normalize(core)["AAA"]  # trimmed


def test_crowding_aware_dampens_crowded_add():
    plain = it.apply_institutional_tilt(_CORE, {"DDD": _sig(0.9, state="crowded_accumulation",
                                                            crowding=0.8)})
    aware = it.apply_institutional_tilt(_CORE, {"DDD": _sig(0.9, state="crowded_accumulation",
                                                            crowding=0.8)}, crowding_aware=True)
    assert aware.get("DDD", 0) <= plain.get("DDD", 0)


def test_contrarian_flips_crowded_to_trim():
    core = {"AAA": 0.5, "DDD": 0.5}
    w = it.apply_institutional_tilt(core, {"DDD": _sig(0.9, state="crowded_accumulation",
                                                       crowding=0.8)}, contrarian=True)
    assert w["DDD"] < it._normalize(core)["DDD"]   # crowded accumulation -> caution trim


def test_strategy_fit_scales_tilt():
    hi = it.apply_institutional_tilt(_CORE, {"DDD": _sig(0.9, fit=1.0)}, use_strategy_fit=True)
    lo = it.apply_institutional_tilt(_CORE, {"DDD": _sig(0.9, fit=0.2)}, use_strategy_fit=True)
    assert hi["DDD"] > lo["DDD"]


# --- tactic (PIT) --------------------------------------------------------

def test_tactic_no_lookahead():
    signals_by_date = {
        "2026-05-15": {"DDD": _sig(0.9)},
        "2026-06-01": {"DDD": _sig(-0.9, state="strong_distribution")},
    }
    t = it.InstitutionalTactic(_CORE, signals_by_date=signals_by_date)
    # On 2026-05-20 only the 05-15 snapshot is visible.
    w_early = t.target_weights_asof("2026-05-20")
    assert w_early.get("DDD", 0) > 0
    # Before any snapshot -> anchor unchanged.
    w_before = t.target_weights_asof("2026-05-01")
    assert "DDD" not in w_before or w_before.get("DDD", 0) == 0
    _sum1(w_early)


def test_tactic_feeds_decision_engine_false():
    t = it.InstitutionalTactic(_CORE, signals_by_date={})
    assert t.metadata["feeds_decision_engine"] is False
    assert t.metadata["sandbox_only"] is True


def test_variants_identical_inputs_and_isolated():
    sbd = {"2026-05-15": {"DDD": _sig(0.9), "EEE": _sig(-0.8, state="strong_distribution")}}
    variants = it.institutional_variants(_CORE, sbd)
    ids = {v.tactic_id for v in variants}
    assert ids == {"institutional_single_manager", "institutional_consensus",
                   "institutional_consensus_strategy_fit",
                   "institutional_consensus_crowding_aware",
                   "institutional_contrarian_crowding_diagnostic"}
    # All evaluate over identical inputs and produce valid weight vectors.
    for v in variants:
        w = v.target_weights_asof("2026-05-20")
        _sum1(w)
    # The single-manager variant is flagged diagnostic.
    diag = next(v for v in variants if v.tactic_id == "institutional_single_manager")
    assert diag.metadata["single_manager_diagnostic"] is True


def test_no_nan_inf():
    for score in (0.0, 1.0, -1.0, 0.5):
        w = it.apply_institutional_tilt(_CORE, {"DDD": _sig(score)})
        assert all(math.isfinite(v) for v in w.values())
