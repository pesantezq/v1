"""
Tests for backtesting/signal_sources.py — real-signal ingestion (Pattern-Loop Step 1).

Fully offline and deterministic (no network, no API keys). Covers a HEALTHY
state (a fixture watchlist_signals.json normalizes to harness signal rows, with
alert_basis mapped to registry-family patterns, multi-tag) and DEGRADED states
(missing / empty / malformed artifact → [], no crash), per the repo's
analysis+health coverage rule.

Observe-only: these read an artifact and normalize rows; nothing is written and
no protected scoring/decision logic is touched.
"""

from __future__ import annotations

import json
from pathlib import Path

from backtesting.signal_sources import (
    load_historical_signal_snapshots,
    load_signals_from_artifact,
)


def _write_artifact(path: Path, results: list[dict]) -> None:
    path.write_text(json.dumps({"results": results, "alerts": []}), encoding="utf-8")


# --------------------------------------------------------------------------
# Healthy state
# --------------------------------------------------------------------------

def test_normalizes_core_fields(tmp_path):
    art = tmp_path / "watchlist_signals.json"
    _write_artifact(art, [
        {"ticker": "PLTR", "scan_time": "2026-04-01T09:00:00",
         "signal_score": 0.4978, "confidence_score": 0.92,
         "alert_basis": ["price_move", "volume_spike"]},
    ])
    rows = load_signals_from_artifact(str(art))
    assert len(rows) == 1
    r = rows[0]
    assert r["ticker"] == "PLTR"
    assert r["scan_time"] == "2026-04-01T09:00:00"
    assert r["signal_score"] == 0.4978
    assert r["confidence_score"] == 0.92
    assert "pattern" in r and "patterns" in r


def test_alert_basis_maps_to_registry_patterns_multitag(tmp_path):
    """A composite alert_basis credits EVERY mapped registry bucket (multi-tag);
    the representative `pattern` prefers price action."""
    art = tmp_path / "watchlist_signals.json"
    _write_artifact(art, [
        {"ticker": "AAA", "scan_time": "2026-04-01", "signal_score": 0.5,
         "confidence_score": 0.8, "alert_basis": ["price_move", "volume_spike"]},
    ])
    r = load_signals_from_artifact(str(art))[0]
    assert set(r["patterns"]) == {"STRONG_MOVE", "VOLUME_SPIKE"}
    assert r["pattern"] == "STRONG_MOVE"


def test_volume_only_and_signal_score_basis(tmp_path):
    art = tmp_path / "watchlist_signals.json"
    _write_artifact(art, [
        {"ticker": "V", "scan_time": "2026-04-01", "signal_score": 0.5,
         "confidence_score": 0.7, "alert_basis": ["volume_spike"]},
        {"ticker": "S", "scan_time": "2026-04-01", "signal_score": 0.6,
         "confidence_score": 0.7, "alert_basis": ["signal_score"]},
    ])
    rows = load_signals_from_artifact(str(art))
    assert rows[0]["pattern"] == "VOLUME_SPIKE"
    assert rows[0]["patterns"] == ["VOLUME_SPIKE"]
    assert rows[1]["pattern"] == "SIGNAL_SCORE"


def test_empty_basis_is_unknown(tmp_path):
    """Below-threshold watch rows (empty alert_basis) still carry valid scores
    but classify as UNKNOWN — they must not be silently dropped."""
    art = tmp_path / "watchlist_signals.json"
    _write_artifact(art, [
        {"ticker": "X", "scan_time": "2026-04-01", "signal_score": 0.3,
         "confidence_score": 0.4, "alert_basis": []},
    ])
    r = load_signals_from_artifact(str(art))[0]
    assert r["pattern"] == "UNKNOWN"
    assert r["patterns"] == ["UNKNOWN"]


def test_unknown_basis_tag_is_uppercased_passthrough(tmp_path):
    art = tmp_path / "watchlist_signals.json"
    _write_artifact(art, [
        {"ticker": "Z", "scan_time": "2026-04-01", "signal_score": 0.5,
         "confidence_score": 0.7, "alert_basis": ["some_new_basis"]},
    ])
    r = load_signals_from_artifact(str(art))[0]
    assert r["pattern"] == "SOME_NEW_BASIS"


# --------------------------------------------------------------------------
# Degraded states
# --------------------------------------------------------------------------

def test_missing_file_returns_empty(tmp_path):
    assert load_signals_from_artifact(str(tmp_path / "does_not_exist.json")) == []


def test_empty_results_returns_empty(tmp_path):
    art = tmp_path / "watchlist_signals.json"
    art.write_text(json.dumps({"results": []}), encoding="utf-8")
    assert load_signals_from_artifact(str(art)) == []


def test_malformed_json_returns_empty(tmp_path):
    art = tmp_path / "watchlist_signals.json"
    art.write_text("{ not valid json", encoding="utf-8")
    assert load_signals_from_artifact(str(art)) == []


def test_missing_scores_row_is_skipped_not_crashed(tmp_path):
    """A row lacking the score fields degrades to None scores but does not crash;
    rows with no ticker are skipped."""
    art = tmp_path / "watchlist_signals.json"
    _write_artifact(art, [
        {"scan_time": "2026-04-01", "alert_basis": ["price_move"]},  # no ticker → skip
        {"ticker": "OK", "scan_time": "2026-04-01", "alert_basis": ["price_move"]},  # no scores
    ])
    rows = load_signals_from_artifact(str(art))
    assert [r["ticker"] for r in rows] == ["OK"]
    assert rows[0]["signal_score"] is None
    assert rows[0]["confidence_score"] is None


# --------------------------------------------------------------------------
# Historical snapshot aggregation
# --------------------------------------------------------------------------

def test_historical_snapshots_aggregate_dated_dirs(tmp_path):
    """Dated outputs/history/<date>/watchlist_signals.json snapshots aggregate
    into one signal list, with each row normalized (alert_basis → patterns)."""
    for date, ticker in (("2026-04-01", "AAA"), ("2026-04-02", "BBB")):
        day = tmp_path / date
        day.mkdir()
        _write_artifact(day / "watchlist_signals.json", [
            {"ticker": ticker, "scan_time": f"{date}T09:00:00", "signal_score": 0.5,
             "confidence_score": 0.7, "alert_basis": ["price_move", "volume_spike"]},
        ])
    rows = load_historical_signal_snapshots(str(tmp_path))
    # Both snapshots aggregated, sorted by glob (date-ordered).
    assert [r["ticker"] for r in rows] == ["AAA", "BBB"]
    assert all(set(r["patterns"]) == {"STRONG_MOVE", "VOLUME_SPIKE"} for r in rows)
    assert all(r["pattern"] == "STRONG_MOVE" for r in rows)


def test_historical_snapshots_flat_fallback_layout(tmp_path):
    """The flat <date>_watchlist_signals.json fallback layout is also picked up."""
    _write_artifact(tmp_path / "2026-04-01_watchlist_signals.json", [
        {"ticker": "FLAT", "scan_time": "2026-04-01", "signal_score": 0.5,
         "confidence_score": 0.7, "alert_basis": ["signal_score"]},
    ])
    rows = load_historical_signal_snapshots(str(tmp_path))
    assert [r["ticker"] for r in rows] == ["FLAT"]
    assert rows[0]["pattern"] == "SIGNAL_SCORE"


def test_historical_snapshots_missing_dir_returns_empty(tmp_path):
    assert load_historical_signal_snapshots(str(tmp_path / "no_such_dir")) == []


def test_historical_snapshots_empty_dir_returns_empty(tmp_path):
    """A directory with no readable snapshots degrades to [] (never raises)."""
    (tmp_path / "empty_history").mkdir()
    assert load_historical_signal_snapshots(str(tmp_path / "empty_history")) == []


# --------------------------------------------------------------------------
# run_poc integration (Step 1 acceptance)
# --------------------------------------------------------------------------

def test_run_poc_accepts_real_signals_source(tmp_path):
    """run_poc(signals_source=...) replays real signals through the existing
    metric path; synthetic remains the default."""
    from backtesting.poc_simulation_harness import run_poc
    art = tmp_path / "watchlist_signals.json"
    rows = [{"ticker": f"SYM{i:02d}", "scan_time": "2026-03-01",
             "signal_score": 0.5, "confidence_score": 0.7,
             "alert_basis": ["price_move"]} for i in range(40)]
    art.write_text(json.dumps({"results": rows}), encoding="utf-8")

    p = run_poc(signals_source=str(art), seed=42, write=False)
    assert p["mode"] == "real_signals_offline"
    assert p["params"]["signals_source"] == str(art)
    assert p["performance"]["total_signals"] == 40
    assert p["added_metrics"]["per_pattern"], "expected a per-pattern breakdown"


def test_run_poc_synthetic_is_still_default():
    from backtesting.poc_simulation_harness import run_poc
    p = run_poc(n_signals=30, seed=42, write=False)
    assert p["mode"] == "synthetic_offline"
    assert p["params"]["signals_source"] is None
    assert p["performance"]["total_signals"] == 30


def test_run_poc_degraded_source_evaluates_zero_without_raising(tmp_path):
    """A missing/degraded signals_source resolves to [] inside the harness, so
    run_poc evaluates zero signals without raising (degrade-gracefully contract)."""
    from backtesting.poc_simulation_harness import run_poc
    missing = tmp_path / "does_not_exist.json"
    p = run_poc(signals_source=str(missing), seed=42, write=False)
    assert p["mode"] == "real_signals_offline"
    assert p["performance"]["evaluated"] == 0
    assert p["performance"]["total_signals"] == 0
    assert p["added_metrics"]["per_pattern"] == []
