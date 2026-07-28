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
