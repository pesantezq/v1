"""
Tests for backtesting/tagging_proposer.py — observe-only / proposes-only signal-tagging
proposer (sub-project D2).

Fully offline and deterministic. Asserts: a signal set with ~60% empty alert_basis yields
untagged_pct≈0.6 and a backfill-inference proposal; a mapped family with no registry
signal_id (SIGNAL_SCORE) appears in families_missing_registry_id with a registry-entry
proposal; a fully-tagged, fully-covered set proposes nothing; empty input never raises.

Observe-only: reads signals + the registry read-only and proposes a review artifact;
mutates nothing.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from backtesting.tagging_proposer import propose_tagging_fixes

_REGISTRY = "config/signal_registry.yaml"


def _tmp_registry(signal_ids: list[str]) -> str:
    body = "signals:\n" + "".join(
        f"  - signal_id: {sid}\n    default_weight: 0.3\n" for sid in signal_ids)
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(body)
    f.close()
    return f.name


def test_high_untagged_rate_yields_backfill_proposal():
    signals = ([{"ticker": "A", "alert_basis": [], "signal_score": 0.7}] * 6
               + [{"ticker": "B", "alert_basis": ["price_move"]}] * 4)
    out = propose_tagging_fixes(signals, registry_path=_REGISTRY)
    assert out["observe_only"] is True
    assert out["proposed_only"] is True
    assert out["status"] == "ok"
    assert out["total"] == 10
    assert out["untagged_count"] == 6
    assert abs(out["untagged_pct"] - 0.6) < 1e-9
    kinds = {p["kind"] for p in out["proposals"]}
    assert "backfill_inference" in kinds


def test_family_missing_registry_id_yields_registry_entry_proposal():
    reg = _tmp_registry(["STRONG_MOVE_UP", "VOLUME_SPIKE"])  # no SIGNAL_SCORE
    signals = [{"ticker": "A", "alert_basis": ["signal_score"]},
               {"ticker": "B", "alert_basis": ["price_move"]}]
    out = propose_tagging_fixes(signals, registry_path=reg)
    assert "SIGNAL_SCORE" in out["families_missing_registry_id"]
    entry_props = [p for p in out["proposals"] if p["kind"] == "registry_entry"]
    assert any(p["signal_id"] == "SIGNAL_SCORE" for p in entry_props)


def test_fully_tagged_and_covered_proposes_nothing():
    reg = _tmp_registry(["STRONG_MOVE_UP", "STRONG_MOVE_DOWN", "VOLUME_SPIKE"])
    signals = [{"ticker": "A", "alert_basis": ["price_move"]},
               {"ticker": "B", "alert_basis": ["volume_spike"]}]
    out = propose_tagging_fixes(signals, registry_path=reg)
    assert out["untagged_count"] == 0
    assert out["families_missing_registry_id"] == []
    assert out["proposals"] == []


def test_empty_input_never_raises():
    out = propose_tagging_fixes([], registry_path=_REGISTRY)
    assert out["status"] in ("ok", "insufficient")
    assert out["total"] == 0
    assert out["untagged_pct"] == 0.0
    assert out["proposals"] == []
