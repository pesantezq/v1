"""Phase 4 — decision-time context capture + outcome taxonomy.

Observe-only complement to decision_outcome_tracker: stores each decision's
IMMUTABLE at-decision context (regime/crowd/factor/confidence/data-quality) and
provides the explicit outcome taxonomy + return neutral-band — WITHOUT mutating
the protected stored win-rate (mirrors how memo_coherence stayed additive).

TDD: written before portfolio_automation/decision_context_capture.py existed.
"""
from __future__ import annotations

import json
from pathlib import Path

import portfolio_automation.decision_context_capture as dc


_NOW = "2026-06-30T09:03:00+00:00"


# ---------------------------------------------------------------------------
# Horizon contracts (1/3/7 resolved; 21/63 declared, not forced)
# ---------------------------------------------------------------------------


def test_horizon_contracts():
    assert dc.RESOLVED_HORIZONS == [1, 3, 7]
    assert dc.CONTRACT_HORIZONS == [1, 3, 7, 21, 63]
    assert set(dc.RESOLVED_HORIZONS).issubset(set(dc.CONTRACT_HORIZONS))


# ---------------------------------------------------------------------------
# Taxonomy + neutral band
# ---------------------------------------------------------------------------


def test_classify_outcome_taxonomy():
    # unresolved: no return yet
    assert dc.classify_outcome("BUY", None, resolved=False) == "unresolved"
    # neutral: sub-band move is noise, not a hit (band default ±1%)
    assert dc.classify_outcome("BUY", 0.4, resolved=True) == "neutral"
    # hit: BUY rose beyond band
    assert dc.classify_outcome("BUY", 3.0, resolved=True) == "hit"
    # miss: BUY fell beyond band
    assert dc.classify_outcome("BUY", -3.0, resolved=True) == "miss"
    # SELL wants down -> down beyond band is a hit
    assert dc.classify_outcome("SELL", -3.0, resolved=True) == "hit"
    # invalidated by data quality
    assert dc.classify_outcome("BUY", 3.0, resolved=True, data_quality="invalid") == "invalidated"
    # insufficient data
    assert dc.classify_outcome("BUY", 3.0, resolved=True, data_quality="insufficient") == "insufficient_data"


def test_only_hit_and_miss_are_counted():
    assert dc.is_counted("hit") and dc.is_counted("miss")
    for label in ("neutral", "unresolved", "insufficient_data", "invalidated"):
        assert not dc.is_counted(label)


def test_counted_hit_rate_excludes_non_judgeable():
    labels = ["hit", "hit", "miss", "neutral", "unresolved", "insufficient_data", "invalidated"]
    hr = dc.counted_hit_rate(labels)
    assert hr["judgeable"] == 3 and hr["hits"] == 2
    assert abs(hr["hit_rate"] - (2 / 3)) < 1e-9


# ---------------------------------------------------------------------------
# Immutable at-decision context capture (pure)
# ---------------------------------------------------------------------------


def test_capture_records_immutable_decision_context():
    plan = {"decisions": [
        {"symbol": "AAPL", "decision": "BUY", "priority": 0.55, "confidence": 0.7,
         "suggested_amount": 500.0, "price": 200.0},
    ]}
    recs = dc.capture_decision_context(
        plan, run_id="2026-06-30_daily_official", now=_NOW,
        regime="bull", crowd={"AAPL": "confirmed_attention"},
        factor_state={"mkt": 0.01}, data_quality="ok", snapshot_hash="HASH",
    )
    r = recs[0]
    assert r["run_id"] == "2026-06-30_daily_official"
    assert r["strategy_id"] == "production"
    assert r["symbol"] == "AAPL" and r["action"] == "BUY"
    assert r["reference_price"] == 200.0
    assert r["confidence_at_decision"] == 0.7
    assert r["regime_at_decision"] == "bull"
    assert r["crowd_state_at_decision"] == "confirmed_attention"
    assert r["factor_state_at_decision"] == {"mkt": 0.01}
    assert r["data_quality_state"] == "ok"
    assert r["horizons"] == dc.CONTRACT_HORIZONS
    assert r["resolved_horizons"] == dc.RESOLVED_HORIZONS
    assert r["snapshot_hash"] == "HASH"
    assert r["timestamp"] == _NOW
    # unresolved at capture; outcome fields are None (filled later, never overwriting context)
    assert r["resolved"] is False


# ---------------------------------------------------------------------------
# Append-only persistence, idempotent per run_id (rule 6: never overwrite)
# ---------------------------------------------------------------------------


def test_write_is_append_only_and_idempotent(tmp_path):
    recs = [{"run_id": "r1", "symbol": "AAPL"}, {"run_id": "r1", "symbol": "MSFT"}]
    dc.write_decision_context(tmp_path, recs)
    dc.write_decision_context(tmp_path, recs)  # same run_id -> no duplicate
    path = tmp_path / "outputs" / "policy" / "decision_context_log.jsonl"
    lines = [l for l in path.read_text().splitlines() if l.strip()]
    assert len(lines) == 2  # not 4
    # a NEW run appends
    dc.write_decision_context(tmp_path, [{"run_id": "r2", "symbol": "AAPL"}])
    lines = [l for l in path.read_text().splitlines() if l.strip()]
    assert len(lines) == 3
