"""
Tests for backtesting/calibration_proposer.py — observe-only / proposes-only
calibration-correction proposer (sub-project D1).

Fully offline and deterministic. Asserts: an inverted-calibration fixture yields
inverted=True with a monotone (non-decreasing) suggested map and apply_gate
oos_unconfirmed while the OOS window is immature; a well-calibrated fixture
yields inverted=False and no correction; thin bands (n < min_band_n) are excluded;
degraded/empty input never raises.

Observe-only: reads a results dict and proposes a review artifact; touches no
protected scoring logic, applies nothing.
"""

from __future__ import annotations

from backtesting.calibration_proposer import propose_calibration_correction


def _results(*, slope, buckets, folds_possible=False):
    return {
        "calibration": {"buckets": buckets, "calibration_slope": slope,
                        "well_calibrated": slope is not None and slope >= 0},
        "oos_window": {"folds_possible": folds_possible},
    }


_INVERTED_BUCKETS = [
    {"label": "0-20", "count": 0, "hit_rate": 0.0, "avg_return": 0.0},
    {"label": "20-40", "count": 0, "hit_rate": 0.0, "avg_return": 0.0},
    {"label": "40-60", "count": 30, "hit_rate": 83.33, "avg_return": 8.49},
    {"label": "60-80", "count": 40, "hit_rate": 57.14, "avg_return": 0.95},
    {"label": "80-100", "count": 754, "hit_rate": 60.64, "avg_return": 3.88},
]


def test_inverted_calibration_flagged_with_monotone_map():
    out = propose_calibration_correction(_results(slope=-11.345, buckets=_INVERTED_BUCKETS))
    assert out["observe_only"] is True
    assert out["proposed_only"] is True
    assert out["status"] == "ok"
    assert out["inverted"] is True
    # Only the three bands with count >= 20 survive.
    assert [b["band"] for b in out["bands"]] == ["40-60", "60-80", "80-100"]
    suggested = [b["suggested_calibrated_conf"] for b in out["bands"]]
    assert suggested == sorted(suggested)  # isotonic: non-decreasing
    assert out["apply_gate"] == "oos_unconfirmed"  # immature window


def test_apply_gate_ready_when_window_mature():
    out = propose_calibration_correction(
        _results(slope=-2.0, buckets=_INVERTED_BUCKETS, folds_possible=True))
    assert out["apply_gate"] == "ready"


def test_well_calibrated_no_correction():
    good = [
        {"label": "40-60", "count": 30, "hit_rate": 50.0, "avg_return": 0.1},
        {"label": "60-80", "count": 40, "hit_rate": 60.0, "avg_return": 0.5},
        {"label": "80-100", "count": 100, "hit_rate": 75.0, "avg_return": 1.2},
    ]
    out = propose_calibration_correction(_results(slope=0.30, buckets=good))
    assert out["inverted"] is False


def test_thin_bands_excluded():
    thin = [
        {"label": "40-60", "count": 5, "hit_rate": 80.0, "avg_return": 8.0},
        {"label": "60-80", "count": 8, "hit_rate": 50.0, "avg_return": 0.5},
        {"label": "80-100", "count": 12, "hit_rate": 60.0, "avg_return": 3.0},
    ]
    out = propose_calibration_correction(_results(slope=-3.0, buckets=thin), min_band_n=20)
    assert out["status"] == "insufficient"
    assert out["bands"] == []


def test_empty_or_degraded_input_never_raises():
    assert propose_calibration_correction({})["status"] in ("insufficient", "degraded")
    assert propose_calibration_correction({"calibration": {}})["status"] == "insufficient"
    # malformed buckets must not raise
    bad = {"calibration": {"buckets": "nope", "calibration_slope": -1}}
    assert propose_calibration_correction(bad)["status"] in ("insufficient", "degraded")


# ---------------------------------------------------------------------------
# apply_gate must not read "ready" on zero evidence (found 2026-08-03).
#
# apply_gate was derived from OOS maturity ALONE, and both insufficient-evidence
# early returns passed that value straight through. Once the walk-forward window
# matured (2026-08-01, oos_window.folds_possible -> true) the live artifact began
# reporting apply_gate="ready" with bands=[], calibration_slope=0.0,
# well_calibrated=null and n_samples=null — a verdict derived from ABSENT data.
# Nothing consumes apply_gate today, which is the only reason it was inert rather
# than harmful. Same defect family as the doc-audit range-scoped coverage gap and
# retune_auto_apply's missing evidence gate.
#
# NOTE the band schema: the proposer reads `label`, `count`, `hit_rate` (percent).
# ---------------------------------------------------------------------------
def _band(label, count, hit_rate_pct):
    return {"label": label, "count": count, "hit_rate": hit_rate_pct}


def test_apply_gate_not_ready_when_there_are_no_bands():
    r = propose_calibration_correction({
        "calibration": {"buckets": [], "calibration_slope": 0.0},
        "oos_window": {"folds_possible": True},
    })
    assert r["apply_gate"] == "insufficient_evidence", r
    assert r["bands"] == []


def test_apply_gate_not_ready_when_buckets_missing_entirely():
    r = propose_calibration_correction({
        "calibration": {"calibration_slope": 0.0},
        "oos_window": {"folds_possible": True},
    })
    assert r["apply_gate"] == "insufficient_evidence"


def test_apply_gate_not_ready_when_only_one_band_has_sample():
    r = propose_calibration_correction({
        "calibration": {"calibration_slope": -3.0, "buckets": [
            _band("80-100", 50, 40.0),
            _band("40-60", 2, 90.0),   # below min_band_n -> unusable
        ]},
        "oos_window": {"folds_possible": True},
    }, min_band_n=20)
    assert r["apply_gate"] == "insufficient_evidence"


def test_oos_immaturity_still_blocks_even_with_real_bands():
    r = propose_calibration_correction({
        "calibration": {"calibration_slope": -3.0, "buckets": [
            _band("80-100", 50, 40.0), _band("40-60", 50, 70.0),
        ]},
        "oos_window": {"folds_possible": False},
    })
    assert r["status"] == "ok", r
    assert r["apply_gate"] == "oos_unconfirmed"


def test_ready_still_reachable_with_mature_window_and_real_bands():
    r = propose_calibration_correction({
        "calibration": {"calibration_slope": -3.0, "buckets": [
            _band("80-100", 50, 40.0), _band("40-60", 50, 70.0),
        ]},
        "oos_window": {"folds_possible": True},
    })
    assert r["status"] == "ok", r
    assert r["apply_gate"] == "ready"
    assert len(r["bands"]) == 2
