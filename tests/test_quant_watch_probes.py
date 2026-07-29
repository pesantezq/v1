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
    # WS16: load_ledger now migrates every probe onto the expanded schema
    # (see test_load_ledger_migrates_probe_schema_tolerantly below) — the
    # historical field is preserved, new fields are backfilled.
    assert led["active"][0]["id"] == "x"
    assert led["archive"] == []


def test_load_ledger_migrates_probe_schema_tolerantly(tmp_path):
    # WS16: a bare-minimum legacy probe (as if written before the schema
    # expansion) must load with every new field backfilled to a safe default,
    # without losing the fields it already had.
    p = tmp_path / "ledger.json"
    p.write_text(json.dumps({"active": [
        {"id": "sector_drag:Foo", "detector": "sector_drag", "scope_key": "Foo",
         "created_at": "2026-06-01T00:00:00+00:00", "observations": [{"run": "a"}, {"run": "b"}]},
    ], "archive": [
        {"id": "manual:old", "detector": "manual", "scope_key": "old",
         "created_at": "2026-01-01T00:00:00+00:00", "resolved_at": "2026-02-01T00:00:00+00:00",
         "resolution": "recovered", "resolution_detail": "metric recovered",
         # ad hoc free-text owner — proves the historical shape had none enforced
         "owner": "regime-classifier owner (market_regime.py)"},
    ]}), encoding="utf-8")
    led = qwp.load_ledger(p)

    active = led["active"][0]
    assert active["concern_class"] == "sector_drag"          # derived from detector
    assert active["consecutive_observations"] == 2           # derived from observations trail
    assert active["remediation_status"] == "open"
    assert active["closure_evidence"] is None
    for k in ("evidence_artifact", "affected_component", "escalation_threshold",
              "regression_test_reference"):
        assert active[k] is None

    archived = led["archive"][0]
    # tolerant migration: the ad hoc free-text owner is preserved, not dropped
    assert archived["owner"]["identifier"] == "regime-classifier owner (market_regime.py)"
    assert archived["owner"]["note"] == "regime-classifier owner (market_regime.py)"
    assert archived["remediation_status"] == "closed"
    # closure_evidence is backfilled (honestly marked as such) from the
    # pre-existing resolution/resolution_detail fields, not fabricated
    assert archived["closure_evidence"]["backfilled"] is True
    assert archived["closure_evidence"]["note"] == "metric recovered"


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


# ── D3 sector_drag — current-fp cross-check (cross-gauge pooling guard) ──────

def _retune_sector_fixture(sector="Communication Services", mean_ret=1.97,
                           resolved_1d=26, current_fp="5687885c"):
    """retune_impact-shaped fixture carrying a current-fp sector_composition.
    by_tag (pattern_efficacy) pools across gauge eras; this is the per-fingerprint
    live-gauge slice used to veto a stale pooled 'loser'."""
    return {
        "current_fingerprint": current_fp,
        "outcome_attribution": {"by_fingerprint": {current_fp: {"sector_composition": {
            sector: {"resolved_1d": resolved_1d, "hit_rate_1d": 0.85,
                     "mean_return_1d": mean_ret},
        }}}},
    }


def test_sector_verdict_normalizes_names_and_flags_contradiction():
    # by_tag uses 'Communication_Services'; sector_composition uses 'Communication Services'
    r = _retune_sector_fixture()
    assert qwp._current_fp_sector_verdict(r, "Communication_Services") == "contradicts"


def test_sector_verdict_confirms_when_live_gauge_also_negative():
    r = _retune_sector_fixture(mean_ret=-0.5)
    assert qwp._current_fp_sector_verdict(r, "Communication_Services") == "confirms"


def test_sector_verdict_unknown_on_thin_or_missing_sample():
    assert qwp._current_fp_sector_verdict(None, "Communication_Services") == "unknown"
    thin = _retune_sector_fixture(resolved_1d=5)
    assert qwp._current_fp_sector_verdict(thin, "Communication_Services") == "unknown"
    absent = _retune_sector_fixture(sector="Energy")
    assert qwp._current_fp_sector_verdict(absent, "Communication_Services") == "unknown"


def test_d3_suppressed_when_current_fp_contradicts_pooled_loser():
    # pooled by_tag says Communication_Services is a loser, but the live gauge shows
    # it positive-mean at adequate n → do NOT register (cross-gauge pooling artifact).
    eff = _efficacy_fixture(sector="sector:Communication_Services", n=62, vs_baseline=-12.28)
    retune = _retune_sector_fixture()
    probes = qwp.detect_sector_drag(eff, "2026-07-10T09:00:00+00:00", "r", retune=retune)
    assert probes == []


def test_d3_still_fires_when_current_fp_confirms_or_unknown():
    eff = _efficacy_fixture(sector="sector:Communication_Services", n=62, vs_baseline=-12.28)
    # confirms (live gauge also negative) → fire
    confirms = qwp.detect_sector_drag(
        eff, "2026-07-10T09:00:00+00:00", "r",
        retune=_retune_sector_fixture(mean_ret=-0.5))
    assert len(confirms) == 1
    # unknown (no retune) → fall back to pooled signal, still fire (backward compatible)
    unknown = qwp.detect_sector_drag(eff, "2026-07-10T09:00:00+00:00", "r")
    assert len(unknown) == 1


def test_d3_eval_resolves_on_current_fp_contradiction():
    # existing probe (pooled still loser), but current-fp contradicts → auto-retire.
    eff = _efficacy_fixture(sector="sector:Communication_Services", n=62, vs_baseline=-12.28)
    probe = qwp.detect_sector_drag(eff, "2026-07-06T09:00:00+00:00", "r")[0]
    t = qwp._eval_sector_drag(probe, _retune_sector_fixture(), eff, "5687885c",
                              "2026-07-10T09:00:00+00:00")
    assert t["status"] == "resolved" and t["resolution"] == "current_fp_contradicts"


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


def test_d2_eval_stays_active_past_60_days_when_still_firing():
    # WS16 fix: age alone must NEVER close a concern. 100 days later, the
    # underlying condition (still negative, but below the RED severity/n/
    # consecutive gates) is unchanged -> the probe must stay active, not
    # silently auto-resolve via a TTL.
    probe = qwp.detect_negative_mean_return_persistence(
        _retune_fixture(mean_ret=-1.18), "2026-06-08T09:00:00+00:00", "r")
    t = qwp._eval_neg_return(probe, _retune_fixture(mean_ret=-1.0), None, "d95e",
                             "2026-09-16T09:00:00+00:00")
    assert t["status"] == "active"
    assert "ttl" not in t["detail"].lower()


def test_d2_eval_escalates_on_persistence_plus_severe_impact_not_age():
    # Persistence (>= NEG_RETURN_RED_MIN_CONSECUTIVE consecutive observations)
    # + impact (mean_return_1d <= NEG_RETURN_RED_PCT) + sample size escalates —
    # explicitly NOT gated on age (created_at is "today").
    probe = qwp.detect_negative_mean_return_persistence(
        _retune_fixture(mean_ret=-1.5, resolved=70), "2026-06-08T09:00:00+00:00", "r")
    probe["consecutive_observations"] = 2  # this eval would be the 3rd
    t = qwp._eval_neg_return(probe, _retune_fixture(mean_ret=-1.5, resolved=70), None, "d95e",
                             "2026-06-09T09:00:00+00:00")
    assert t["status"] == "escalated"
    assert "persistent severe negative return" in t["detail"]


def test_d3_eval_stays_active_past_60_days_when_still_firing():
    # WS16 fix: same as D2 — age alone must never close a concern.
    probe = qwp.detect_sector_drag(_efficacy_fixture(), "2026-06-08T09:00:00+00:00", "r")[0]
    t = qwp._eval_sector_drag(probe, None, _efficacy_fixture(), "d95e",
                              "2026-09-16T09:00:00+00:00")
    assert t["status"] == "active"
    assert "ttl" not in t["detail"].lower()


def test_d3_eval_escalates_on_persistence_plus_severe_impact_not_age():
    eff = _efficacy_fixture(sector="sector:Consumer_Cyclical", n=70, vs_baseline=-20.0)
    probe = qwp.detect_sector_drag(eff, "2026-06-08T09:00:00+00:00", "r")[0]
    probe["consecutive_observations"] = 2  # this eval would be the 3rd
    t = qwp._eval_sector_drag(probe, None, eff, "d95e", "2026-06-09T09:00:00+00:00")
    assert t["status"] == "escalated"
    assert "persistent severe sector drag" in t["detail"]


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


# ── Task 8: update_ledger() ───────────────────────────────────────────────────

def test_update_ledger_adds_new_and_archives_resolved():
    now = "2026-06-09T09:00:00+00:00"
    ledger = {"schema_version": "1", "active": [
        {"id": "a", "detector": "d", "created_at": "2026-06-01T09:00:00+00:00",
         "observations": []},
        {"id": "b", "detector": "d", "created_at": "2026-06-01T09:00:00+00:00",
         "observations": []},
    ], "archive": []}
    new_probes = [{"id": "c", "detector": "d", "created_at": now, "observations": []}]
    transitions = [
        qwp._active({"id": "a"}, "still bad", now, {"run": "2026-06-09", "v": 1}),
        qwp._resolved({"id": "b"}, "recovered", "ok now", now),
    ]
    out = qwp.update_ledger(ledger, new_probes, transitions, now)
    active_ids = {p["id"] for p in out["active"]}
    archive_ids = {p["id"] for p in out["archive"]}
    assert active_ids == {"a", "c"}          # b archived, c added
    assert archive_ids == {"b"}
    arch_b = out["archive"][0]
    assert arch_b["resolution"] == "recovered"
    assert arch_b["resolved_at"] == now
    assert arch_b["lifetime_days"] == 8
    # a got its observation appended + last_evaluated_at bumped
    a = next(p for p in out["active"] if p["id"] == "a")
    assert a["observations"][-1] == {"run": "2026-06-09", "v": 1}
    assert a["last_evaluated_at"] == now


def test_update_ledger_escalated_goes_to_archive_with_reason():
    now = "2026-06-09T09:00:00+00:00"
    ledger = {"schema_version": "1", "active": [
        {"id": "a", "detector": "d", "created_at": "2026-06-01T09:00:00+00:00",
         "observations": []}], "archive": []}
    transitions = [qwp._escalated({"id": "a"}, "crossed gate", now)]
    out = qwp.update_ledger(ledger, [], transitions, now)
    assert out["active"] == []
    assert out["archive"][0]["resolution"] == "escalated_to_red"


def test_update_ledger_caps_observations_and_archive():
    now = "2026-06-09T09:00:00+00:00"
    probe = {"id": "a", "detector": "d", "created_at": "2026-06-01T09:00:00+00:00",
             "observations": [{"run": f"d{i}"} for i in range(qwp.MAX_OBSERVATIONS)]}
    ledger = {"schema_version": "1", "active": [probe],
              "archive": [{"id": f"old{i}"} for i in range(qwp.MAX_ARCHIVE)]}
    transitions = [qwp._active({"id": "a"}, "x", now, {"run": "new"})]
    out = qwp.update_ledger(ledger, [], transitions, now)
    a = out["active"][0]
    assert len(a["observations"]) == qwp.MAX_OBSERVATIONS  # capped
    assert a["observations"][-1] == {"run": "new"}
    assert len(out["archive"]) == qwp.MAX_ARCHIVE          # capped (FIFO)


def test_update_ledger_does_not_mutate_input():
    now = "2026-06-09T09:00:00+00:00"
    ledger = {"schema_version": "1", "active": [
        {"id": "a", "detector": "d", "created_at": now, "observations": []}],
        "archive": []}
    transitions = [qwp._resolved({"id": "a"}, "recovered", "x", now)]
    qwp.update_ledger(ledger, [], transitions, now)
    assert ledger["active"][0]["id"] == "a"  # original untouched
    assert ledger["archive"] == []


# ── Task 9: overall_status() + render_status() + ledger_liveness ─────────────

def test_overall_status_mapping():
    assert qwp.overall_status({"active": []}, []) == qwp.GREEN
    assert qwp.overall_status({"active": [{"id": "a"}]}, []) == qwp.AMBER
    esc = [{"id": "a", "status": "escalated"}]
    assert qwp.overall_status({"active": []}, esc) == qwp.RED


def test_render_status_shape():
    now = "2026-06-09T09:00:00+00:00"
    ledger = {"schema_version": "1", "active": [
        {"id": "prior_gauge_underperformance:d95e", "detector": "prior_gauge_underperformance",
         "concern": "bad", "severity": "amber", "created_at": "2026-06-08T09:00:00+00:00",
         "observations": [{"run": "2026-06-09", "delta_vs_prior_pp": -24.1}]}],
        "archive": []}
    new_probes = [{"id": "prior_gauge_underperformance:d95e"}]
    transitions = [{"id": "x", "status": "resolved", "resolution": "recovered"},
                   {"id": "y", "status": "escalated", "resolution": "escalated_to_red"}]
    status = qwp.render_status(ledger, new_probes, transitions, now)
    assert status["observe_only"] is True
    assert status["source"] == "quant_watch_probes"
    assert status["overall_status"] == "red"   # an escalation this run
    assert status["active_count"] == 1
    assert status["registered_today"] == ["prior_gauge_underperformance:d95e"]
    assert status["resolved_today"] == [{"id": "x", "resolution": "recovered"}]
    assert status["escalated_today"] == [{"id": "y", "resolution": "escalated_to_red"}]
    assert status["ledger_liveness"]["status"] == "ok"
    assert status["active"][0]["age_days"] == 1


def test_update_ledger_observation_is_deepcopied():
    now = "2026-06-09T09:00:00+00:00"
    ledger = {"schema_version": "1", "active": [
        {"id": "a", "detector": "d", "created_at": "2026-06-01T09:00:00+00:00",
         "observations": []}], "archive": []}
    obs = {"run": "2026-06-09", "v": 1}
    transitions = [qwp._active({"id": "a"}, "x", now, obs)]
    out = qwp.update_ledger(ledger, [], transitions, now)
    obs["v"] = 999  # mutate the source AFTER update_ledger
    assert out["active"][0]["observations"][-1]["v"] == 1  # ledger unaffected


def test_update_ledger_archive_fifo_keeps_newest():
    now = "2026-06-09T09:00:00+00:00"
    ledger = {"schema_version": "1", "active": [],
              "archive": [{"id": f"old{i}"} for i in range(qwp.MAX_ARCHIVE)]}
    # archive a fresh probe → oldest evicted, newest kept
    led2 = {"schema_version": "1", "active": [
        {"id": "fresh", "detector": "d", "created_at": now, "observations": []}],
        "archive": ledger["archive"]}
    transitions = [qwp._resolved({"id": "fresh"}, "recovered", "x", now)]
    out = qwp.update_ledger(led2, [], transitions, now)
    archive_ids = [a["id"] for a in out["archive"]]
    assert len(out["archive"]) == qwp.MAX_ARCHIVE
    assert "old0" not in archive_ids        # oldest evicted
    assert "fresh" in archive_ids           # newest kept


def test_evaluate_error_path_keeps_probe_active(monkeypatch):
    retune = _retune_fixture()
    ledger = qwp._empty_ledger()
    ledger["active"] = [qwp.detect_prior_gauge_underperformance(
        retune, "2026-06-08T09:00:00+00:00", "r")]
    def boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setitem(qwp._EVALUATORS, qwp.DETECTOR_PRIOR_GAUGE, boom)
    transitions = qwp.evaluate(retune, {}, "d95e", ledger, "2026-06-09T09:00:00+00:00")
    assert len(transitions) == 1
    assert transitions[0]["status"] == "active"
    assert "eval error" in transitions[0]["detail"]


# ── Task 10: run_quant_watch() orchestrator ───────────────────────────────────

def test_run_quant_watch_end_to_end(tmp_path):
    # arrange artifacts under a fake root
    root = tmp_path
    (root / "outputs" / "latest").mkdir(parents=True)
    (root / "data").mkdir()
    (root / "outputs" / "latest" / "retune_impact.json").write_text(
        json.dumps(_retune_fixture()), encoding="utf-8")
    (root / "outputs" / "latest" / "pattern_efficacy_monthly.json").write_text(
        json.dumps(_efficacy_fixture()), encoding="utf-8")

    result = qwp.run_quant_watch(root=root, now_iso="2026-06-08T09:00:00+00:00",
                                 created_run="test-run", write_files=True)

    assert result["overall_status"] == "amber"
    assert result["active_count"] == 3
    # ledger written
    led = json.loads((root / "data" / "quant_watch_ledger.json").read_text())
    assert len(led["active"]) == 3
    # status artifact written
    status = json.loads(
        (root / "outputs" / "latest" / "quant_watch_status.json").read_text())
    assert status["observe_only"] is True
    assert status["active_count"] == 3
    assert status["schema_version"] == "1"

    # second run same inputs → idempotent (no new probes, still 3 active)
    result2 = qwp.run_quant_watch(root=root, now_iso="2026-06-09T09:00:00+00:00",
                                  created_run="test-run", write_files=True)
    assert result2["registered_today"] == []
    assert result2["active_count"] == 3


def test_run_quant_watch_degrades_when_artifacts_missing(tmp_path):
    (tmp_path / "data").mkdir()
    result = qwp.run_quant_watch(root=tmp_path, now_iso="2026-06-08T09:00:00+00:00",
                                 created_run="r", write_files=False)
    assert result["overall_status"] == "green"
    assert result["active_count"] == 0
    assert result["schema_version"] == "1"


# ── WS16: closure-evidence-gated resolution (never age-based) ────────────────

def test_update_ledger_populates_closure_evidence_on_resolve():
    now = "2026-06-09T09:00:00+00:00"
    ledger = {"schema_version": "1", "active": [
        {"id": "a", "detector": "d", "created_at": "2026-06-01T09:00:00+00:00",
         "observations": [], "evidence_artifact": "outputs/latest/retune_impact.json"}],
        "archive": []}
    transitions = [qwp._resolved({"id": "a"}, "recovered", "delta +3pp", now)]
    out = qwp.update_ledger(ledger, [], transitions, now)
    closed = out["archive"][0]
    assert closed["remediation_status"] == "closed"
    assert closed["closure_evidence"]["note"] == "delta +3pp"
    assert closed["closure_evidence"]["artifact"] == "outputs/latest/retune_impact.json"
    assert closed["closure_evidence"]["backfilled"] is False


def test_update_ledger_preserves_pre_attached_closure_evidence():
    # A human called record_closure() before the detector confirmed recovery —
    # update_ledger must not overwrite that evidence when it later archives.
    now = "2026-06-09T09:00:00+00:00"
    pre_attached = {"artifact": "manual", "snapshot": None, "note": "operator verified fix",
                    "closed_by": "operator", "closed_at": "2026-06-08T00:00:00+00:00",
                    "backfilled": False}
    ledger = {"schema_version": "1", "active": [
        {"id": "a", "detector": "d", "created_at": "2026-06-01T09:00:00+00:00",
         "observations": [], "closure_evidence": pre_attached}], "archive": []}
    transitions = [qwp._resolved({"id": "a"}, "recovered", "delta +3pp", now)]
    out = qwp.update_ledger(ledger, [], transitions, now)
    assert out["archive"][0]["closure_evidence"] == pre_attached


def test_update_ledger_increments_consecutive_observations_on_active():
    now = "2026-06-09T09:00:00+00:00"
    ledger = {"schema_version": "1", "active": [
        {"id": "a", "detector": "d", "created_at": "2026-06-01T09:00:00+00:00",
         "observations": [], "consecutive_observations": 2}], "archive": []}
    transitions = [qwp._active({"id": "a"}, "still bad", now, {"run": "x"})]
    out = qwp.update_ledger(ledger, [], transitions, now)
    assert out["active"][0]["consecutive_observations"] == 3


def test_render_status_flags_stale_unresolved_without_resolving():
    now = "2026-09-16T09:00:00+00:00"  # >100 days after created_at below
    ledger = {"schema_version": "1", "active": [
        {"id": "a", "detector": "sector_drag", "concern": "still bad",
         "severity": "amber", "created_at": "2026-06-01T09:00:00+00:00",
         "last_evaluated_at": now, "observations": [{"run": "2026-09-16"}]}],
        "archive": []}
    status = qwp.render_status(ledger, [], [], now)
    entry = status["active"][0]
    assert entry["stale_unresolved"] is True
    # still active — the flag informs, it never removes the concern
    assert status["active_count"] == 1


# ── WS16: overall_status honors a directly-registered RED severity ──────────

def test_overall_status_red_on_registered_severity_without_transition():
    ledger = {"active": [{"id": "a", "severity": qwp.RED}]}
    assert qwp.overall_status(ledger, []) == qwp.RED


def test_overall_status_amber_when_no_red_severity():
    ledger = {"active": [{"id": "a", "severity": qwp.AMBER}]}
    assert qwp.overall_status(ledger, []) == qwp.AMBER


# ── WS16: register_manual_concern() / record_closure() ───────────────────────

def test_register_manual_concern_writes_full_schema(tmp_path):
    (tmp_path / "data").mkdir()
    result = qwp.register_manual_concern(
        root=tmp_path, concern="discovery_watchlist_adds permanently zero (WS13)",
        concern_class=qwp.CONCERN_CLASS_INERT_EXPERIMENT, scope_key="discovery_adds",
        evidence_artifact="outputs/promotion_review/daily_governance_status.json",
        affected_component="portfolio_automation/sim_governance/daily_governance_run.py",
        owner="portfolio-learning-loop-health", now_iso="2026-07-28T09:00:00+00:00")
    assert result["status"] == "registered"
    led = qwp.load_ledger(tmp_path / "data" / "quant_watch_ledger.json")
    probe = led["active"][0]
    assert probe["id"] == f"{qwp.DETECTOR_MANUAL}:discovery_adds"
    assert probe["concern_class"] == qwp.CONCERN_CLASS_INERT_EXPERIMENT
    assert probe["severity"] == qwp.AMBER  # not trust-boundary -> capped at AMBER
    assert probe["owner"]["identifier"] == "portfolio-learning-loop-health"
    assert probe["remediation_status"] == "open"


def test_register_manual_concern_is_idempotent_by_id(tmp_path):
    (tmp_path / "data").mkdir()
    qwp.register_manual_concern(root=tmp_path, concern="x", scope_key="dup",
                                now_iso="2026-07-28T09:00:00+00:00")
    result2 = qwp.register_manual_concern(root=tmp_path, concern="x", scope_key="dup",
                                          now_iso="2026-07-28T09:00:00+00:00")
    assert result2["status"] == "already_active"
    led = qwp.load_ledger(tmp_path / "data" / "quant_watch_ledger.json")
    assert len(led["active"]) == 1


def test_register_manual_concern_trust_boundary_gets_red_severity(tmp_path):
    (tmp_path / "data").mkdir()
    result = qwp.register_manual_concern(
        root=tmp_path, concern="approval timestamp leaked across environments",
        concern_class="timestamp_leakage", scope_key="ts_leak", severity=qwp.RED,
        now_iso="2026-07-28T09:00:00+00:00")
    probe = result["probe"]
    assert probe["severity"] == qwp.RED
    assert probe["trust_boundary"] is True
    # a single confirmed occurrence is enough — overall_status must read RED
    # even though no `evaluate()` transition has run yet.
    led = qwp.load_ledger(tmp_path / "data" / "quant_watch_ledger.json")
    assert qwp.overall_status(led, []) == qwp.RED


def test_register_manual_concern_non_trust_boundary_cannot_self_escalate_to_red(tmp_path):
    (tmp_path / "data").mkdir()
    result = qwp.register_manual_concern(
        root=tmp_path, concern="not actually a trust boundary issue",
        concern_class=qwp.CONCERN_CLASS_STATISTICAL_INSUFFICIENCY, severity=qwp.RED,
        now_iso="2026-07-28T09:00:00+00:00")
    assert result["probe"]["severity"] == qwp.AMBER


def test_record_closure_retires_manual_concern_immediately(tmp_path):
    (tmp_path / "data").mkdir()
    qwp.register_manual_concern(root=tmp_path, concern="x", scope_key="foo",
                                now_iso="2026-07-01T00:00:00+00:00")
    result = qwp.record_closure(
        tmp_path, f"{qwp.DETECTOR_MANUAL}:foo",
        evidence_artifact="outputs/latest/some_fix_verification.json",
        note="fix shipped and verified", closed_by="pesantez",
        regression_test_reference="tests/test_some_fix.py::test_regression",
        now_iso="2026-07-28T00:00:00+00:00")
    assert result["status"] == "closed"
    led = qwp.load_ledger(tmp_path / "data" / "quant_watch_ledger.json")
    assert led["active"] == []
    archived = led["archive"][0]
    assert archived["resolution"] == "closed_with_evidence"
    assert archived["closure_evidence"]["closed_by"] == "pesantez"
    assert archived["regression_test_reference"] == "tests/test_some_fix.py::test_regression"


def test_record_closure_on_detector_probe_does_not_resolve_alone(tmp_path):
    # Evidence recorded, but the concern stays active until the OWNING
    # detector also confirms non-firing on the next evaluate() cycle — this
    # is the "AND" in "detector no longer fires AND a closure record exists".
    (tmp_path / "data").mkdir()
    ledger = {"schema_version": "1", "active": [
        {"id": "sector_drag:Foo", "detector": "sector_drag", "scope_key": "Foo",
         "created_at": "2026-07-01T00:00:00+00:00", "observations": []}], "archive": []}
    qwp.write_ledger(tmp_path / "data" / "quant_watch_ledger.json", ledger)
    result = qwp.record_closure(tmp_path, "sector_drag:Foo", note="fix shipped",
                                closed_by="pesantez", now_iso="2026-07-28T00:00:00+00:00")
    assert result["status"] == "evidence_recorded_awaiting_detector_confirmation"
    led = qwp.load_ledger(tmp_path / "data" / "quant_watch_ledger.json")
    assert len(led["active"]) == 1  # still active
    assert led["active"][0]["closure_evidence"]["note"] == "fix shipped"
    assert led["active"][0]["remediation_status"] == "in_progress"


def test_record_closure_not_found(tmp_path):
    (tmp_path / "data").mkdir()
    qwp.write_ledger(tmp_path / "data" / "quant_watch_ledger.json", qwp._empty_ledger())
    result = qwp.record_closure(tmp_path, "nope:nope", now_iso="2026-07-28T00:00:00+00:00")
    assert result["status"] == "not_found"


# ── WS16: the two REAL live-ledger concerns must keep loading unchanged ─────

def test_live_ledger_shaped_probes_keep_loading(tmp_path):
    """Mirrors the two real open concerns in data/quant_watch_ledger.json
    (crypto-miner sector drag; regime coverage gap) WITHOUT copying real
    ledger content — a manual probe with an ad hoc owner string (as
    `manual:regime_classifier_neutral_collapse` carries in production) plus
    an active sector_drag probe, both loading tolerantly under the new
    schema and CLAUDE.md's "never resolve/delete/alter the two live
    concerns" constraint (this test proves the mechanism, not the data)."""
    p = tmp_path / "ledger.json"
    p.write_text(json.dumps({
        "schema_version": "1",
        "active": [
            {"id": "sector_drag:crypto_miners", "detector": "sector_drag",
             "lens": "quant", "scope_key": "crypto_miners",
             "created_at": "2026-07-15T00:00:00+00:00", "created_run": "quant-watch-analysis",
             "severity": "amber", "concern": "crypto-miner sector drag",
             "trigger_snapshot": {"vs_baseline_pp": -13.1, "n_samples": 40},
             "resolve_hint": "sector no longer flagged loser",
             "last_evaluated_at": "2026-07-28T00:00:00+00:00",
             "observations": [{"run": "2026-07-28", "vs_baseline_pp": -13.1}]},
            {"id": "manual:regime_coverage_gap_5687885c", "detector": "manual",
             "lens": "quant", "scope_key": "regime_coverage_gap_5687885c",
             "created_at": "2026-07-27T00:00:00+00:00", "created_run": "quant-watch-analysis",
             "severity": "amber", "concern": "regime coverage gap",
             "trigger_snapshot": {}, "resolve_hint": "operator clears",
             "last_evaluated_at": "2026-07-28T00:00:00+00:00", "observations": [],
             "owner": "regime-classifier owner (market_regime.py)"},
        ],
        "archive": [],
    }), encoding="utf-8")

    led = qwp.load_ledger(p)
    assert len(led["active"]) == 2
    ids = {pr["id"] for pr in led["active"]}
    assert ids == {"sector_drag:crypto_miners", "manual:regime_coverage_gap_5687885c"}
    manual = next(pr for pr in led["active"] if pr["detector"] == "manual")
    assert manual["owner"]["identifier"] == "regime-classifier owner (market_regime.py)"
    assert manual["remediation_status"] == "open"  # still active, not silently closed
