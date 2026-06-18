"""Tests for the pipeline wiring probe (stale-producer root-cause layer).

The probe crosses two signals per registry producer:
  - freshness (authoritative): is the artifact within its cadence window?
  - static caller-grep (classifier): which cadence's cron script names the producer?

and classifies each stale producer as unwired / cadence_mismatch / silently_skipped.
The pure `classify_producers` function is exercised directly with fixtures; the
`run_pipeline_wiring_probe` orchestrator is smoke-tested against the real repo.
"""

from pathlib import Path

import pytest

from portfolio_automation.pipeline_wiring_probe import (
    classify_producers,
    run_pipeline_wiring_probe,
    CADENCE_WINDOW_HOURS,
)


# ── Fixtures: a tiny synthetic registry + script corpus ──────────────────────

def _registry():
    return {
        "narratives.json": {"producer": "market_narratives", "cadence": "daily", "role": "narrative"},
        "evidence.json": {"producer": "news_evidence_layer", "cadence": "daily", "role": "advisor"},
        "retune.json": {"producer": "retune_suggestions", "cadence": "weekly", "role": "advisor"},
        "scraped.json": {"producer": "scraped_intel", "cadence": "daily", "role": "advisor"},
        "ondemand.json": {"producer": "schwab_sync", "cadence": "on_demand", "role": "advisor"},
        "noproducer.json": {"cadence": "daily", "role": "telemetry"},
    }


def _scripts(*, daily="", weekly="", monthly="", core=""):
    return {"daily": daily, "weekly": weekly, "monthly": monthly, "core": core}


# ── classify_producers: the four verdicts ────────────────────────────────────

def test_all_fresh_is_green():
    reg = _registry()
    ages = {k: 1.0 for k in reg}  # everything just produced
    out = classify_producers(reg, _scripts(daily="market_narratives news_evidence_layer scraped_intel",
                                           weekly="retune_suggestions"),
                             ages)
    assert out["overall_status"] == "green"
    assert out["summary"]["unwired"] == 0
    assert out["summary"]["cadence_mismatch"] == 0
    assert out["summary"]["silently_skipped"] == 0
    # on_demand + no-producer rows are not audited
    statuses = {p["artifact"]: p["status"] for p in out["producers"]}
    assert statuses["ondemand.json"] == "not_audited"
    assert "noproducer.json" not in statuses


def test_unwired_when_stale_and_no_caller():
    reg = _registry()
    ages = {k: 1.0 for k in reg}
    ages["narratives.json"] = 9999.0  # very stale
    # narratives token appears in NO script
    out = classify_producers(reg, _scripts(daily="news_evidence_layer scraped_intel",
                                           weekly="retune_suggestions"),
                             ages)
    v = {p["artifact"]: p for p in out["producers"]}
    assert v["narratives.json"]["status"] == "unwired"
    assert out["overall_status"] == "amber"
    assert out["summary"]["unwired"] == 1


def test_cadence_mismatch_when_caller_is_wrong_cadence():
    reg = _registry()
    ages = {k: 1.0 for k in reg}
    ages["retune.json"] = 9999.0  # stale
    # retune declared weekly, but token only present in the DAILY script
    out = classify_producers(reg, _scripts(daily="retune_suggestions market_narratives",
                                           weekly=""),
                             ages)
    v = {p["artifact"]: p for p in out["producers"]}
    assert v["retune.json"]["status"] == "cadence_mismatch"
    assert "daily" in v["retune.json"]["caller_cadences"]
    assert out["summary"]["cadence_mismatch"] == 1


def test_silently_skipped_when_wired_but_stale():
    reg = _registry()
    ages = {k: 1.0 for k in reg}
    ages["scraped.json"] = 9999.0  # stale despite being wired daily
    out = classify_producers(reg, _scripts(daily="scraped_intel market_narratives news_evidence_layer",
                                           weekly="retune_suggestions"),
                             ages)
    v = {p["artifact"]: p for p in out["producers"]}
    assert v["scraped.json"]["status"] == "silently_skipped"
    assert out["summary"]["silently_skipped"] == 1


def test_config_gate_off_is_disabled_not_skipped():
    reg = _registry()
    ages = {k: 1.0 for k in reg}
    ages["scraped.json"] = 9999.0
    out = classify_producers(reg, _scripts(daily="scraped_intel"),
                             ages, config_gates={"scraped.json": False})
    v = {p["artifact"]: p for p in out["producers"]}
    # an intentionally-disabled producer's staleness is expected, not a fault
    assert v["scraped.json"]["status"] == "disabled"
    assert out["summary"]["silently_skipped"] == 0


def test_crowd_mention_history_registered_in_config_gates():
    """The crowd-radar ledger must be registered so a disabled Crowd Radar
    reclassifies its absent ledger unwired->disabled (non-AMBER)."""
    from portfolio_automation.pipeline_wiring_probe import _CONFIG_GATES
    assert "crowd_mention_history.json" in _CONFIG_GATES
    path_keys, default = _CONFIG_GATES["crowd_mention_history.json"]
    assert path_keys == ("crowd_radar", "enabled")
    assert default is False


def test_crowd_mention_history_gated_off_is_disabled_not_unwired():
    """A .json (not .jsonl) telemetry ledger that is missing because its layer
    is gated off must read `disabled`, not `unwired` — the 2026-06-13 false-positive."""
    reg = {"crowd_mention_history.json": {"producer": "public_knowledge_velocity_layer",
                                          "cadence": "daily", "role": "telemetry"}}
    ages = {"crowd_mention_history.json": None}  # absent on disk (gated off)
    out = classify_producers(reg, _scripts(daily="public_knowledge_velocity"),
                             ages, config_gates={"crowd_mention_history.json": False})
    v = {p["artifact"]: p for p in out["producers"]}
    assert v["crowd_mention_history.json"]["status"] == "disabled"
    assert out["summary"]["unwired"] == 0
    assert out["overall_status"] == "green"


def test_fresh_but_empty_content_flag():
    reg = _registry()
    ages = {k: 1.0 for k in reg}
    out = classify_producers(reg, _scripts(daily="market_narratives news_evidence_layer scraped_intel",
                                           weekly="retune_suggestions"),
                             ages, content_flags={"narratives.json": False})
    v = {p["artifact"]: p for p in out["producers"]}
    assert v["narratives.json"]["status"] == "fresh_but_empty"
    assert out["overall_status"] == "amber"


def test_event_log_jsonl_is_idle_not_unwired():
    """Append-only telemetry .jsonl logs that are missing are idle, not a fault."""
    reg = {"pattern_events.jsonl": {"producer": "event_store", "cadence": "daily",
                                    "role": "telemetry"}}
    out = classify_producers(reg, _scripts(), {"pattern_events.jsonl": None})
    v = {p["artifact"]: p for p in out["producers"]}
    assert v["pattern_events.jsonl"]["status"] == "event_log_idle"
    assert out["overall_status"] == "green"  # idle logs don't trip amber
    assert out["summary"]["unwired"] == 0


def test_core_corpus_counts_as_daily():
    """A producer invoked only via main.py / orchestrator modules (the 'core'
    corpus) is wired for the daily cadence, not unwired."""
    reg = {"scraped.json": {"producer": "scraped_intel", "cadence": "daily", "role": "advisor"}}
    ages = {"scraped.json": 9999.0}
    # token only in 'core' (e.g. watchlist_scanner.__main__), not the wrapper
    out = classify_producers(reg, _scripts(core="run_comparison scraped_intel"), ages)
    v = {p["artifact"]: p for p in out["producers"]}
    assert v["scraped.json"]["status"] == "silently_skipped"  # wired (core==daily) but stale


# ── content predicate: narrative non-degeneracy (2026-06-18 triage) ──────────

def test_narr_predicate_accepts_body_without_key_themes():
    """A weekly/monthly narrative with key_themes==[] but a real body (headline +
    summary, or a populated discovery_context) is NOT fresh_but_empty."""
    from portfolio_automation.pipeline_wiring_probe import _content_predicates

    narr = _content_predicates()["market_narrative_weekly.json"]
    # Healthy thin-theme narrative: empty key_themes, full body.
    assert narr({"key_themes": [], "data_available": True,
                 "top_headline": "X", "executive_summary": "Y"}) is True
    # Populated discovery_context alone also counts.
    assert narr({"key_themes": [], "discovery_context": {"candidate_count": 4}}) is True
    # Genuinely empty narrative still flags.
    assert narr({"key_themes": [], "data_available": False,
                 "discovery_context": {"candidate_count": 0}}) is False
    # Original key_themes path still works.
    assert narr({"key_themes": [{"theme": "ai"}]}) is True


# ── config gate refinement: crowd mention-history Reddit feed (2026-06-18) ────

def test_crowd_mention_gate_disabled_when_reddit_uncredentialed(tmp_path, monkeypatch):
    """crowd_radar.enabled=True but the Reddit feed reports no_credentials → the
    absent mention-history ledger is stale-by-design (disabled), not unwired."""
    import json
    import portfolio_automation.pipeline_wiring_probe as mod

    (tmp_path / "config.json").write_text(json.dumps({"crowd_radar": {"enabled": True}}))
    state_dir = tmp_path / "outputs" / "sandbox" / "discovery"
    state_dir.mkdir(parents=True)
    (state_dir / "crowd_knowledge_state.json").write_text(
        json.dumps({"source_status": "no_credentials"}))

    gates = mod._config_gates(tmp_path)
    assert gates["crowd_mention_history.json"] is False


def test_crowd_mention_gate_enabled_when_reddit_feed_ok(tmp_path):
    """When the Reddit feed is actually ingesting (source_status ok), the gate
    stays enabled so a genuinely-broken ledger would still surface."""
    import json
    import portfolio_automation.pipeline_wiring_probe as mod

    (tmp_path / "config.json").write_text(json.dumps({"crowd_radar": {"enabled": True}}))
    state_dir = tmp_path / "outputs" / "sandbox" / "discovery"
    state_dir.mkdir(parents=True)
    (state_dir / "crowd_knowledge_state.json").write_text(
        json.dumps({"source_status": "ok"}))

    gates = mod._config_gates(tmp_path)
    assert gates["crowd_mention_history.json"] is True


# ── orchestrator: real-repo smoke + contract ─────────────────────────────────

def test_run_probe_observe_only_and_contract(tmp_path):
    root = Path(__file__).resolve().parents[1]
    r = run_pipeline_wiring_probe(root=root, write_files=False)
    assert r["observe_only"] is True
    assert r["overall_status"] in {"green", "amber"}  # never red
    assert "summary" in r and "producers" in r
    assert isinstance(r["summary"].get("total_audited"), int)


def test_run_probe_degrades_on_error(monkeypatch):
    import portfolio_automation.pipeline_wiring_probe as mod

    def _boom(*a, **k):
        raise RuntimeError("forced")

    monkeypatch.setattr(mod, "_load_registry", _boom)
    r = run_pipeline_wiring_probe(root=".", write_files=False)
    assert r["observe_only"] is True
    assert r["overall_status"] == "amber"  # degraded, never crashes/red
    assert "error" in r


def test_cadence_window_map_present():
    for c in ("daily", "weekly", "monthly", "weekend", "yearly"):
        assert c in CADENCE_WINDOW_HOURS and CADENCE_WINDOW_HOURS[c] > 0
