# tests/test_quant_watch_probes.py
import json
from pathlib import Path

from portfolio_automation import quant_watch_probes as qwp


def test_empty_ledger_shape():
    led = qwp._empty_ledger()
    assert led == {"schema_version": "1", "active": [], "archive": []}


def test_load_ledger_missing_returns_empty(tmp_path):
    led = qwp.load_ledger(tmp_path / "nope.json")
    assert led == qwp._empty_ledger()


def test_load_ledger_corrupt_resets_to_empty(tmp_path):
    p = tmp_path / "ledger.json"
    p.write_text("{not valid json", encoding="utf-8")
    led = qwp.load_ledger(p)
    assert led == qwp._empty_ledger()


def test_load_ledger_backfills_missing_keys(tmp_path):
    p = tmp_path / "ledger.json"
    p.write_text(json.dumps({"active": [{"id": "x"}]}), encoding="utf-8")
    led = qwp.load_ledger(p)
    assert led["schema_version"] == "1"
    assert led["active"] == [{"id": "x"}]
    assert led["archive"] == []


def test_select_prior_gauge_picks_latest_non_current_non_pretracker():
    by_fp = {
        "CUR": {"last_signal_time": "2026-06-08T09:00:00", "hit_rate_1d": 0.45},
        "OLDGAUGE": {"last_signal_time": "2026-05-29T09:00:00", "hit_rate_1d": 0.69},
        "OLDERGAUGE": {"last_signal_time": "2026-05-20T09:00:00", "hit_rate_1d": 0.55},
        "pre_tracker_unknown": {"last_signal_time": "2026-05-19T01:00:00", "hit_rate_1d": 0.40},
    }
    fp, entry = qwp._select_prior_gauge(by_fp, "CUR")
    assert fp == "OLDGAUGE"
    assert entry["hit_rate_1d"] == 0.69


def test_select_prior_gauge_none_when_only_current_and_pretracker():
    by_fp = {
        "CUR": {"last_signal_time": "2026-06-08T09:00:00"},
        "pre_tracker_unknown": {"last_signal_time": "2026-05-19T01:00:00"},
    }
    fp, entry = qwp._select_prior_gauge(by_fp, "CUR")
    assert fp is None and entry is None


def test_transition_builders_shape():
    probe = {"id": "d:scope"}
    now = "2026-06-08T09:00:00+00:00"
    a = qwp._active(probe, "still bad", now, {"run": "2026-06-08", "v": 1})
    assert a == {"id": "d:scope", "status": "active", "detail": "still bad",
                 "observation": {"run": "2026-06-08", "v": 1}}
    r = qwp._resolved(probe, "recovered", "delta +1pp", now)
    assert r["status"] == "resolved" and r["resolution"] == "recovered"
    assert r["resolved_at"] == now
    e = qwp._escalated(probe, "crossed gate", now)
    assert e["status"] == "escalated" and e["resolution"] == "escalated_to_red"


def test_age_days():
    assert qwp._age_days("2026-06-01T00:00:00+00:00", "2026-06-08T00:00:00+00:00") == 7
    assert qwp._age_days(None, "2026-06-08T00:00:00+00:00") == 0


# ── Task 4: D1 prior_gauge_underperformance ──────────────────────────────────

def _retune_fixture(cur_hr=0.4489, prior_hr=0.6894, pre_hr=0.4062,
                    resolved=176, mean_ret=-1.18, current_fp="d95e"):
    return {
        "current_fingerprint": current_fp,
        "outcome_attribution": {
            "pre_tracker_label": "pre_tracker_unknown",
            "by_fingerprint": {
                current_fp: {"resolved_1d": resolved, "hit_rate_1d": cur_hr,
                             "mean_return_1d": mean_ret,
                             "last_signal_time": "2026-06-08T09:00:00"},
                "f60e": {"resolved_1d": 264, "hit_rate_1d": prior_hr,
                         "last_signal_time": "2026-05-29T09:00:00"},
                "pre_tracker_unknown": {"resolved_1d": 352, "hit_rate_1d": pre_hr,
                                        "last_signal_time": "2026-05-19T01:00:00"},
            },
        },
    }


def test_d1_fires_on_prior_gauge_underperformance():
    probe = qwp.detect_prior_gauge_underperformance(
        _retune_fixture(), "2026-06-08T09:00:00+00:00", "test-run")
    assert probe is not None
    assert probe["id"] == "prior_gauge_underperformance:d95e"
    assert probe["detector"] == qwp.DETECTOR_PRIOR_GAUGE
    assert probe["scope_key"] == "d95e"
    assert probe["lens"] == "quant"
    assert "vs prior gauge" in probe["concern"]
    assert probe["trigger_snapshot"]["delta_vs_prior_pp"] == -24.1


def test_d1_quiet_when_within_resolve_band():
    # current 0.68 vs prior 0.69 → delta -1pp, above the -10 fire gate
    probe = qwp.detect_prior_gauge_underperformance(
        _retune_fixture(cur_hr=0.68), "2026-06-08T09:00:00+00:00", "r")
    assert probe is None


def test_d1_quiet_when_daily_red_would_own_it():
    # delta vs pre_tracker is large (|0.30-0.55|=25pp >= 10) → daily RED owns it
    probe = qwp.detect_prior_gauge_underperformance(
        _retune_fixture(cur_hr=0.30, pre_hr=0.55), "2026-06-08T09:00:00+00:00", "r")
    assert probe is None


def test_d1_quiet_below_min_sample():
    probe = qwp.detect_prior_gauge_underperformance(
        _retune_fixture(resolved=10), "2026-06-08T09:00:00+00:00", "r")
    assert probe is None


def test_d1_eval_resolves_on_scope_change():
    probe = qwp.detect_prior_gauge_underperformance(
        _retune_fixture(), "2026-06-08T09:00:00+00:00", "r")
    # current fingerprint is now something else
    t = qwp._eval_prior_gauge(probe, _retune_fixture(current_fp="NEWFP"),
                              None, "NEWFP", "2026-06-20T09:00:00+00:00")
    assert t["status"] == "resolved" and t["resolution"] == "scope_changed"


def test_d1_eval_resolves_on_recovery():
    probe = qwp.detect_prior_gauge_underperformance(
        _retune_fixture(), "2026-06-08T09:00:00+00:00", "r")
    recovered = _retune_fixture(cur_hr=0.68)  # delta vs prior -1pp >= -2
    t = qwp._eval_prior_gauge(probe, recovered, None, "d95e",
                              "2026-06-20T09:00:00+00:00")
    assert t["status"] == "resolved" and t["resolution"] == "recovered"


def test_d1_eval_escalates_when_crosses_daily_red_gate():
    probe = qwp.detect_prior_gauge_underperformance(
        _retune_fixture(), "2026-06-08T09:00:00+00:00", "r")
    worse = _retune_fixture(cur_hr=0.30, pre_hr=0.55)  # |delta vs pre|=25pp
    t = qwp._eval_prior_gauge(probe, worse, None, "d95e",
                              "2026-06-20T09:00:00+00:00")
    assert t["status"] == "escalated"


def test_d1_eval_stays_active_when_still_bad():
    probe = qwp.detect_prior_gauge_underperformance(
        _retune_fixture(), "2026-06-08T09:00:00+00:00", "r")
    t = qwp._eval_prior_gauge(probe, _retune_fixture(), None, "d95e",
                              "2026-06-09T09:00:00+00:00")
    assert t["status"] == "active"
    assert t["observation"]["delta_vs_prior_pp"] == -24.1


# ── Task 5: D2 negative_mean_return_persistence ──────────────────────────────

def test_d2_fires_on_negative_mean_return():
    probe = qwp.detect_negative_mean_return_persistence(
        _retune_fixture(mean_ret=-1.18), "2026-06-08T09:00:00+00:00", "r")
    assert probe is not None
    assert probe["id"] == "negative_mean_return_persistence:d95e"
    assert probe["trigger_snapshot"]["mean_return_1d"] == -1.18


def test_d2_quiet_when_positive():
    probe = qwp.detect_negative_mean_return_persistence(
        _retune_fixture(mean_ret=0.5), "2026-06-08T09:00:00+00:00", "r")
    assert probe is None


def test_d2_quiet_below_min_sample():
    probe = qwp.detect_negative_mean_return_persistence(
        _retune_fixture(mean_ret=-1.0, resolved=5), "2026-06-08T09:00:00+00:00", "r")
    assert probe is None


def test_d2_eval_resolves_when_return_recovers():
    probe = qwp.detect_negative_mean_return_persistence(
        _retune_fixture(mean_ret=-1.18), "2026-06-08T09:00:00+00:00", "r")
    t = qwp._eval_neg_return(probe, _retune_fixture(mean_ret=0.2), None, "d95e",
                             "2026-06-20T09:00:00+00:00")
    assert t["status"] == "resolved" and t["resolution"] == "recovered"


def test_d2_eval_stays_active_when_still_negative():
    probe = qwp.detect_negative_mean_return_persistence(
        _retune_fixture(mean_ret=-1.18), "2026-06-08T09:00:00+00:00", "r")
    t = qwp._eval_neg_return(probe, _retune_fixture(mean_ret=-0.9), None, "d95e",
                             "2026-06-09T09:00:00+00:00")
    assert t["status"] == "active"
    assert t["observation"]["mean_return_1d"] == -0.9
