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


# ── Task 6: D3 sector_drag ───────────────────────────────────────────────────

def _efficacy_fixture(sector="sector:Consumer_Cyclical", sig="loser", n=42,
                      vs_baseline=-37.67):
    return {"by_tag": {
        sector: {"significance": sig, "n_samples": n, "vs_baseline_pp": vs_baseline,
                 "hit_rate_1d": 0.07},
        "sector:Technology": {"significance": "winner", "n_samples": 77,
                              "vs_baseline_pp": 6.21, "hit_rate_1d": 0.51},
    }}


def test_d3_fires_on_sector_loser_at_min_n():
    probes = qwp.detect_sector_drag(_efficacy_fixture(), "2026-06-08T09:00:00+00:00", "r")
    assert len(probes) == 1
    assert probes[0]["id"] == "sector_drag:Consumer_Cyclical"
    assert probes[0]["scope_key"] == "Consumer_Cyclical"
    assert probes[0]["trigger_snapshot"]["vs_baseline_pp"] == -37.67


def test_d3_quiet_when_loser_below_min_n():
    probes = qwp.detect_sector_drag(_efficacy_fixture(n=12), "2026-06-08T09:00:00+00:00", "r")
    assert probes == []


def test_d3_quiet_when_no_loser():
    probes = qwp.detect_sector_drag(_efficacy_fixture(sig="neutral"),
                                    "2026-06-08T09:00:00+00:00", "r")
    assert probes == []


def test_d3_eval_resolves_when_no_longer_loser():
    probe = qwp.detect_sector_drag(_efficacy_fixture(), "2026-06-08T09:00:00+00:00", "r")[0]
    t = qwp._eval_sector_drag(probe, None, _efficacy_fixture(sig="neutral"), "d95e",
                              "2026-06-20T09:00:00+00:00")
    assert t["status"] == "resolved" and t["resolution"] == "recovered"


def test_d3_eval_resolves_when_tag_absent():
    probe = qwp.detect_sector_drag(_efficacy_fixture(), "2026-06-08T09:00:00+00:00", "r")[0]
    t = qwp._eval_sector_drag(probe, None, {"by_tag": {}}, "d95e",
                              "2026-06-20T09:00:00+00:00")
    assert t["status"] == "resolved"


def test_d3_eval_stays_active_when_still_loser():
    probe = qwp.detect_sector_drag(_efficacy_fixture(), "2026-06-08T09:00:00+00:00", "r")[0]
    t = qwp._eval_sector_drag(probe, None, _efficacy_fixture(), "d95e",
                              "2026-06-09T09:00:00+00:00")
    assert t["status"] == "active"


def test_d1_eval_does_not_escalate_on_outperformance_vs_pretracker():
    # cur=0.55 vs pre_hr=0.40 → delta_pre +15pp (outperformance); must NOT escalate
    probe = qwp.detect_prior_gauge_underperformance(
        _retune_fixture(), "2026-06-08T09:00:00+00:00", "r")
    t = qwp._eval_prior_gauge(probe, _retune_fixture(cur_hr=0.55, pre_hr=0.40),
                              None, "d95e", "2026-06-20T09:00:00+00:00")
    assert t["status"] != "escalated"


def test_d2_eval_resolves_on_scope_change():
    probe = qwp.detect_negative_mean_return_persistence(
        _retune_fixture(mean_ret=-1.18), "2026-06-08T09:00:00+00:00", "r")
    t = qwp._eval_neg_return(probe, _retune_fixture(current_fp="NEWFP"),
                             None, "NEWFP", "2026-06-20T09:00:00+00:00")
    assert t["status"] == "resolved" and t["resolution"] == "scope_changed"


def test_d2_eval_active_when_mean_return_absent():
    probe = qwp.detect_negative_mean_return_persistence(
        _retune_fixture(mean_ret=-1.18), "2026-06-08T09:00:00+00:00", "r")
    # artifact regenerated with mean_return_1d missing → stay active, no crash
    fix = _retune_fixture()
    fix["outcome_attribution"]["by_fingerprint"]["d95e"]["mean_return_1d"] = None
    t = qwp._eval_neg_return(probe, fix, None, "d95e", "2026-06-20T09:00:00+00:00")
    assert t["status"] == "active"


def test_d2_eval_ttl_expires():
    probe = qwp.detect_negative_mean_return_persistence(
        _retune_fixture(mean_ret=-1.18), "2026-06-08T09:00:00+00:00", "r")
    # 100 days later, still negative → TTL (60d) expires it
    t = qwp._eval_neg_return(probe, _retune_fixture(mean_ret=-1.0), None, "d95e",
                             "2026-09-16T09:00:00+00:00")
    assert t["status"] == "resolved" and t["resolution"] == "ttl_expired"


def test_d3_eval_ttl_expires():
    probe = qwp.detect_sector_drag(_efficacy_fixture(), "2026-06-08T09:00:00+00:00", "r")[0]
    # 100 days later, still loser → TTL expires
    t = qwp._eval_sector_drag(probe, None, _efficacy_fixture(), "d95e",
                              "2026-09-16T09:00:00+00:00")
    assert t["status"] == "resolved" and t["resolution"] == "ttl_expired"


def test_d3_eval_tag_absent_is_scope_changed():
    probe = qwp.detect_sector_drag(_efficacy_fixture(), "2026-06-08T09:00:00+00:00", "r")[0]
    t = qwp._eval_sector_drag(probe, None, {"by_tag": {}}, "d95e",
                              "2026-06-20T09:00:00+00:00")
    assert t["status"] == "resolved" and t["resolution"] == "scope_changed"


# ── Task 7: detect() / evaluate() aggregators ────────────────────────────────

def test_detect_aggregates_and_dedupes_active():
    retune = _retune_fixture()
    efficacy = _efficacy_fixture()
    ledger = qwp._empty_ledger()
    new1 = qwp.detect(retune, efficacy, ledger, "2026-06-08T09:00:00+00:00", "r")
    ids = {p["id"] for p in new1}
    assert "prior_gauge_underperformance:d95e" in ids
    assert "negative_mean_return_persistence:d95e" in ids
    assert "sector_drag:Consumer_Cyclical" in ids
    # now mark them active; re-running detect yields no duplicates
    ledger["active"] = new1
    new2 = qwp.detect(retune, efficacy, ledger, "2026-06-09T09:00:00+00:00", "r")
    assert new2 == []


def test_evaluate_dispatches_per_detector_and_manual_stays_active():
    retune = _retune_fixture()
    efficacy = _efficacy_fixture()
    ledger = qwp._empty_ledger()
    ledger["active"] = qwp.detect(retune, efficacy, ledger, "2026-06-08T09:00:00+00:00", "r")
    ledger["active"].append({"id": "manual:foo", "detector": "manual",
                             "scope_key": "foo", "created_at": "2026-06-08T09:00:00+00:00"})
    transitions = qwp.evaluate(retune, efficacy, "d95e", ledger, "2026-06-09T09:00:00+00:00")
    by_id = {t["id"]: t for t in transitions}
    assert by_id["prior_gauge_underperformance:d95e"]["status"] == "active"
    assert by_id["manual:foo"]["status"] == "active"  # never auto-resolved
