# tests/test_regime_coverage.py
"""WS14 (.superpowers/audit/ws-04-05-14-18-health.md) — regime-concentration
must affect validity claims. Covers portfolio_automation.regime_coverage's
four explicit states and its distinctness from the pre-existing
semantic_liveness neutral-collapse guard (different failure mode)."""
import json

from portfolio_automation import regime_coverage as rc


def _regime(total, effective=None, avg_return=1.0, win_rate=0.55, share=None,
           rw_share=None, drawdown=1.0, uncertainty=5.0):
    return {
        "total_signals": total,
        "effective_signals": effective if effective is not None else total,
        "avg_return_pct": avg_return,
        "excess_return_pct": 0.1,
        "win_rate": win_rate,
        "drawdown_pct": drawdown,
        "hit_rate_uncertainty_pp": uncertainty,
        "share_of_evidence": share,
        "return_weighted_share": rw_share,
    }


def test_insufficient_data_when_absent():
    r = rc.assess_regime_coverage(None)
    assert r["states"] == [rc.REGIME_DATA_INSUFFICIENT]
    assert r["primary_state"] == rc.REGIME_DATA_INSUFFICIENT


def test_insufficient_data_below_min_resolved_total():
    perf = {"resolved_signals": 10, "by_regime": {"neutral": _regime(10, share=1.0)}}
    r = rc.assess_regime_coverage(perf)
    assert r["primary_state"] == rc.REGIME_DATA_INSUFFICIENT


def test_balanced_when_no_concentration_and_risk_off_proven():
    perf = {
        "resolved_signals": 200,
        "by_regime": {
            "neutral": _regime(80, share=0.40, rw_share=0.40),
            "risk_on": _regime(80, share=0.40, rw_share=0.40),
            "risk_off": _regime(40, share=0.20, rw_share=0.20),
        },
    }
    r = rc.assess_regime_coverage(perf)
    assert r["states"] == [rc.REGIME_COVERAGE_BALANCED]
    assert r["primary_state"] == rc.REGIME_COVERAGE_BALANCED


def test_concentrated_by_count_share():
    perf = {
        "resolved_signals": 1000,
        "by_regime": {
            "neutral": _regime(900, share=0.90, rw_share=0.85),
            "risk_off": _regime(100, share=0.10, rw_share=0.15),
        },
    }
    r = rc.assess_regime_coverage(perf)
    assert rc.REGIME_CONCENTRATED in r["states"]
    assert r["concentration"]["max_share_regime"] == "neutral"


def test_concentrated_by_return_weighted_share_even_if_count_share_moderate():
    perf = {
        "resolved_signals": 1000,
        "by_regime": {
            "neutral": _regime(500, share=0.5, rw_share=0.9),
            "risk_off": _regime(500, share=0.5, rw_share=0.1),
        },
    }
    r = rc.assess_regime_coverage(perf)
    assert rc.REGIME_CONCENTRATED in r["states"]


def test_risk_off_unproven_when_absent():
    perf = {
        "resolved_signals": 500,
        "by_regime": {"neutral": _regime(500, share=1.0, rw_share=1.0)},
    }
    r = rc.assess_regime_coverage(perf)
    assert rc.RISK_OFF_UNPROVEN in r["states"]
    assert r["risk_off"]["present"] is False


def test_risk_off_unproven_when_effective_below_min():
    perf = {
        "resolved_signals": 500,
        "by_regime": {
            "neutral": _regime(470, share=0.94, rw_share=0.94),
            "risk_off": _regime(30, effective=10, share=0.06, rw_share=0.06),
        },
    }
    r = rc.assess_regime_coverage(perf)
    assert rc.RISK_OFF_UNPROVEN in r["states"]
    assert r["risk_off"]["effective_signals"] == 10


def test_real_production_state_is_concentrated_and_risk_off_unproven():
    # Mirrors the audit's confirmed live shape (2211/2238 neutral = 98.8%;
    # risk_off effectively unproven). Do not tune thresholds to soften this.
    perf = {
        "resolved_signals": 2238,
        "by_regime": {
            "high_volatility": _regime(27, share=0.012, rw_share=0.04, avg_return=0.807, win_rate=0.63),
            "neutral": _regime(2211, effective=925, share=0.988, rw_share=1.0, avg_return=0.226, win_rate=0.519),
        },
    }
    r = rc.assess_regime_coverage(perf)
    assert set(r["states"]) == {rc.REGIME_CONCENTRATED, rc.RISK_OFF_UNPROVEN}


def test_both_states_can_apply_simultaneously():
    perf = {
        "resolved_signals": 2265,
        "by_regime": {
            "high_volatility": _regime(27, share=0.0119, rw_share=0.0468),
            "neutral": _regime(2211, effective=925, share=0.9762, rw_share=1.0722),
            "risk_off": _regime(27, effective=27, share=0.0119, rw_share=-0.1189),
        },
    }
    r = rc.assess_regime_coverage(perf)
    assert set(r["states"]) == {rc.REGIME_CONCENTRATED, rc.RISK_OFF_UNPROVEN}
    assert r["primary_state"] == rc.RISK_OFF_UNPROVEN  # priority order


def test_run_regime_coverage_writes_status_artifact(tmp_path):
    regime_dir = tmp_path / "outputs" / "regime"
    regime_dir.mkdir(parents=True)
    (regime_dir / "regime_performance.json").write_text(json.dumps({
        "resolved_signals": 200,
        "by_regime": {
            "neutral": _regime(80, share=0.40, rw_share=0.40),
            "risk_on": _regime(80, share=0.40, rw_share=0.40),
            "risk_off": _regime(40, share=0.20, rw_share=0.20),
        },
    }), encoding="utf-8")
    status = rc.run_regime_coverage(root=tmp_path, now_iso="2026-07-28T09:00:00+00:00")
    assert status["primary_state"] == rc.REGIME_COVERAGE_BALANCED
    written = json.loads((tmp_path / "outputs" / "latest" / "regime_coverage_status.json").read_text())
    assert written["primary_state"] == rc.REGIME_COVERAGE_BALANCED


def test_run_regime_coverage_degrades_gracefully_when_artifact_missing(tmp_path):
    status = rc.run_regime_coverage(root=tmp_path, now_iso="2026-07-28T09:00:00+00:00", write_files=False)
    assert status["primary_state"] == rc.REGIME_DATA_INSUFFICIENT


# ---------------------------------------------------------------------------
# B4 correction (docs/reliability-program/2026-07-28-final-report.md addendum,
# "Sent back for correction"). Two defects that survive independent of which
# artifact generation is on disk:
#   1. a MISSING share_of_evidence was coerced to 0.0 by `float(... or 0.0)`,
#      so REGIME_CONCENTRATED structurally could not fire and the window read
#      as balanced — imputing BEST-CASE from missing data, the exact practice
#      the program's "never impute a missing component" decision forbids;
#   2. risk_off absent from resolved evidence was reported as "never
#      observed", which is false when the label was observed but has not
#      matured at the primary window (live 2026-07-28: 108 risk_off rows from
#      2026-07-25..27, all regime_data_quality=full, 54 resolved at 1d, ZERO
#      at the 3d primary window).
# ---------------------------------------------------------------------------


def _stale_regime(total, avg_return=1.0, win_rate=0.55):
    """Pre-WS14 artifact shape: real counts, but NONE of the derived fields
    (effective_signals / share_of_evidence / return_weighted_share). This is
    what `outputs/regime/regime_performance.json` actually contained on
    2026-07-28 — the producer emits them, the on-disk artifact predated it."""
    return {
        "total_signals": total,
        "win_rate": win_rate,
        "avg_return_pct": avg_return,
        "avg_signal_score": 0.32,
        "avg_conviction_score": 0.55,
    }


def test_missing_share_of_evidence_is_not_imputed_to_zero():
    perf = {
        "resolved_signals": 2238,
        "by_regime": {
            "high_volatility": _stale_regime(27),
            "neutral": _stale_regime(2211),
        },
    }
    r = rc.assess_regime_coverage(perf)
    assert rc.REGIME_COVERAGE_BALANCED not in r["states"]
    assert r["primary_state"] == rc.REGIME_DATA_INSUFFICIENT
    assert r["assessable"] is False
    assert r["insufficiency_kind"] == "missing_derived_fields"


def test_missing_share_of_evidence_reason_names_the_missing_field():
    perf = {"resolved_signals": 2238, "by_regime": {"neutral": _stale_regime(2211)}}
    r = rc.assess_regime_coverage(perf)
    assert any("share_of_evidence" in reason for reason in r["reasons"])


def test_missing_share_of_evidence_names_no_concentration_leader():
    # The old code took max() over an all-zeros dict, which named the FIRST
    # key — reporting high_volatility (n=27) as the max-share regime at 0.0%
    # while neutral actually held 98.8%. Naming any leader from missing data
    # is worse than naming none.
    perf = {
        "resolved_signals": 2238,
        "by_regime": {
            "high_volatility": _stale_regime(27),
            "neutral": _stale_regime(2211),
        },
    }
    r = rc.assess_regime_coverage(perf)
    assert r["concentration"].get("max_share_regime") is None
    assert r["concentration"].get("max_share") is None


def test_too_few_resolved_is_a_different_insufficiency_than_missing_fields():
    # These two absences must not be conflated: one has no evidence at all,
    # the other has 2238 signals of evidence that cannot be READ. Only the
    # latter should cost a validity downgrade downstream.
    perf = {"resolved_signals": 10, "by_regime": {"neutral": _regime(10, share=1.0)}}
    r = rc.assess_regime_coverage(perf)
    assert r["primary_state"] == rc.REGIME_DATA_INSUFFICIENT
    assert r["insufficiency_kind"] == "too_few_resolved"


def test_complete_artifact_is_assessable():
    perf = {
        "resolved_signals": 1000,
        "by_regime": {
            "neutral": _regime(900, share=0.90, rw_share=0.85),
            "risk_off": _regime(100, share=0.10, rw_share=0.15),
        },
    }
    r = rc.assess_regime_coverage(perf)
    assert r["assessable"] is True
    assert r["insufficiency_kind"] is None


def _census(observed, primary_window_days=3):
    return {"primary_window_days": primary_window_days, "observed": observed}


def test_risk_off_observed_but_unresolved_is_reported_as_immature():
    perf = {
        "resolved_signals": 2238,
        "by_regime": {
            "high_volatility": _regime(27, share=0.0121, rw_share=0.0418),
            "neutral": _regime(2211, effective=925, share=0.9879, rw_share=0.9582),
        },
        "regime_census": _census({
            "neutral": {"observed": 2238, "resolved": 2211},
            "risk_off": {"observed": 108, "resolved": 0},
            "high_volatility": {"observed": 27, "resolved": 27},
        }),
    }
    r = rc.assess_regime_coverage(perf)
    assert rc.RISK_OFF_UNPROVEN in r["states"]
    risk_off_reasons = [x for x in r["reasons"] if "risk_off" in x]
    assert risk_off_reasons, "expected a risk_off reason"
    assert not any("never observed" in x for x in risk_off_reasons)
    assert any("108" in x for x in risk_off_reasons)
    assert r["risk_off"]["observed"] == 108
    assert r["risk_off"]["resolved_at_primary_window"] == 0
    assert r["risk_off"]["absence_kind"] == "immature"


def test_risk_off_absent_from_census_is_reported_as_never_observed():
    perf = {
        "resolved_signals": 2238,
        "by_regime": {"neutral": _regime(2238, share=1.0, rw_share=1.0)},
        "regime_census": _census({"neutral": {"observed": 2238, "resolved": 2238}}),
    }
    r = rc.assess_regime_coverage(perf)
    assert rc.RISK_OFF_UNPROVEN in r["states"]
    assert r["risk_off"]["absence_kind"] == "never_observed"
    assert any("never observed" in x for x in r["reasons"])


def test_risk_off_absent_without_census_does_not_claim_never_observed():
    # Legacy artifact with no census: the honest statement is that this
    # artifact cannot distinguish the two absences — not an assertion of one.
    perf = {
        "resolved_signals": 2238,
        "by_regime": {"neutral": _regime(2238, share=1.0, rw_share=1.0)},
    }
    r = rc.assess_regime_coverage(perf)
    assert rc.RISK_OFF_UNPROVEN in r["states"]
    assert r["risk_off"]["absence_kind"] == "indeterminate"
    assert not any("never observed" in x for x in r["reasons"])


def test_live_2026_07_28_stale_artifact_shape_fails_closed():
    # Regression lock on the exact on-disk artifact that produced the
    # false-signal verdict: RISK_OFF_UNPROVEN ("never observed") + a 0.0
    # max_share naming high_volatility, over a 98.8%-neutral window.
    perf = {
        "generated_at": "2026-07-28T09:03:30.748137",
        "primary_window_days": 3,
        "resolved_signals": 2238,
        "by_regime": {
            "high_volatility": _stale_regime(27, avg_return=0.807, win_rate=0.63),
            "neutral": _stale_regime(2211, avg_return=0.226, win_rate=0.519),
        },
    }
    r = rc.assess_regime_coverage(perf)
    assert r["states"] == [rc.REGIME_DATA_INSUFFICIENT]
    assert r["insufficiency_kind"] == "missing_derived_fields"
    assert not any("never observed" in x for x in r["reasons"])


def test_reconstructed_live_state_from_current_producer_is_concentrated():
    # What the SAME window yields once the artifact is regenerated by the
    # current producer (verified against live rows: neutral share 0.9879,
    # return-weighted 0.9582). Concentration must fire here.
    perf = {
        "resolved_signals": 2238,
        "by_regime": {
            "high_volatility": _regime(27, effective=27, share=0.0121, rw_share=0.0418),
            "neutral": _regime(2211, effective=925, share=0.9879, rw_share=0.9582),
        },
        "regime_census": _census({
            "neutral": {"observed": 2238, "resolved": 2211},
            "risk_off": {"observed": 108, "resolved": 0},
            "high_volatility": {"observed": 27, "resolved": 27},
        }),
    }
    r = rc.assess_regime_coverage(perf)
    assert set(r["states"]) == {rc.REGIME_CONCENTRATED, rc.RISK_OFF_UNPROVEN}
    assert r["concentration"]["max_share_regime"] == "neutral"
    assert r["assessable"] is True


def test_never_fabricates_states_beyond_the_declared_four():
    perf = {
        "resolved_signals": 2265,
        "by_regime": {
            "high_volatility": _regime(27, share=0.0119, rw_share=0.0468),
            "neutral": _regime(2211, effective=925, share=0.9762, rw_share=1.0722),
            "risk_off": _regime(27, effective=27, share=0.0119, rw_share=-0.1189),
        },
    }
    r = rc.assess_regime_coverage(perf)
    allowed = {rc.REGIME_COVERAGE_BALANCED, rc.REGIME_CONCENTRATED,
              rc.RISK_OFF_UNPROVEN, rc.REGIME_DATA_INSUFFICIENT}
    assert set(r["states"]) <= allowed


# ---------------------------------------------------------------------------
# The return-weighted concentration arm was UNBOUNDED (found 2026-08-03).
#
# `return_weighted_share` is a signed attribution ratio (a regime's return
# contribution / the NET total), documented in performance_feedback.py:625-634 as
# "not a bounded probability; do not treat it as one". regime_coverage then did
# exactly that: `abs(max_rw_share) >= concentration_share_threshold` compares an
# unbounded quantity against a threshold that is by construction <= 1.0. With
# mixed-sign contributions that cancel, a well-BALANCED book produces a large
# ratio and fires REGIME_CONCENTRATED for reasons unrelated to concentration.
#
# Live 2026-08-03: neutral 2238x(+0.223) = +499.07, risk_off 108x(-1.240) = -133.92,
# high_volatility 27x(+0.807) = +21.79, net +386.94 -> 499.07/386.94 = 1.2898,
# rendered "129.0%". The count arm (94.31%) fired independently, so the verdict was
# right by luck.
# ---------------------------------------------------------------------------
def _by_regime(rows):
    """rows: {label: (total_signals, avg_return_pct)} -> by_regime with shares."""
    total_n = sum(n for n, _ in rows.values())
    contribs = {r: n * ret for r, (n, ret) in rows.items()}
    net = sum(contribs.values())
    out = {}
    for r, (n, ret) in rows.items():
        out[r] = {
            "total_signals": n, "effective_signals": n,
            "avg_return_pct": ret, "win_rate": 0.5,
            "share_of_evidence": (n / total_n) if total_n else 0.0,
            "return_weighted_share": (contribs[r] / net) if net else None,
        }
    return out


def test_balanced_book_with_cancelling_returns_is_not_concentrated(tmp_path):
    """No regime near the count threshold, but contributions nearly cancel."""
    # 3 regimes at ~1/3 count share each; +10, -9, +1 net = +2 -> ratios blow up
    by_regime = _by_regime({"neutral": (100, 10.0), "risk_off": (100, -9.0),
                            "high_volatility": (100, 1.0)})
    rw = {r: m["return_weighted_share"] for r, m in by_regime.items()}
    assert max(abs(v) for v in rw.values()) > 1.0, "fixture must have an unbounded ratio"

    r = rc.assess_regime_coverage(
        {"resolved_signals": 300, "by_regime": by_regime})
    assert "REGIME_CONCENTRATED" not in r["states"], (
        "a balanced book must not read as concentrated just because its regime "
        f"return contributions cancel (rw ratios={rw})")


def test_genuine_count_concentration_still_fires(tmp_path):
    by_regime = _by_regime({"neutral": (940, 0.22), "risk_off": (60, -1.2)})
    r = rc.assess_regime_coverage(
        {"resolved_signals": 1000, "by_regime": by_regime})
    assert "REGIME_CONCENTRATED" in r["states"]


def test_return_weighted_abs_share_is_bounded_and_exposed(tmp_path):
    """A bounded companion metric the trigger can safely use."""
    by_regime = _by_regime({"neutral": (100, 10.0), "risk_off": (100, -9.0),
                            "high_volatility": (100, 1.0)})
    r = rc.assess_regime_coverage(
        {"resolved_signals": 300, "by_regime": by_regime})
    conc = r["concentration"]
    v = conc["return_weighted_abs_share"]
    assert 0.0 <= v <= 1.0, f"abs-normalised share must be bounded, got {v}"
    # signed ratio still published for backward compatibility
    assert abs(conc["return_weighted_max_share"]) > 1.0


def test_return_weighted_concentration_can_still_fire_when_genuinely_lopsided(tmp_path):
    """Same-sign contributions, one regime dominating the return -> concentrated."""
    by_regime = _by_regime({"neutral": (100, 10.0), "risk_off": (100, 0.2),
                            "high_volatility": (100, 0.1)})
    r = rc.assess_regime_coverage(
        {"resolved_signals": 300, "by_regime": by_regime})
    conc = r["concentration"]
    assert conc["return_weighted_abs_share"] >= 0.8
    assert "REGIME_CONCENTRATED" in r["states"]
