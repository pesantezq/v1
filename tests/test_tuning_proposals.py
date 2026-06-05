"""
Tests for backtesting/tuning_proposals.py — pattern efficacy → tuning *proposal*
(Pattern-Loop Step 4; observe-only, proposes-only).

Fully offline and deterministic. Covers HEALTHY cases (a clear OOS edge yields a
bounded, non-zero proposed delta with rationale; a negative edge proposes a
reduction; deltas clamp to max_abs_delta and weights to [0,1]) and DEGRADED cases
(sample below min_n → 'insufficient_evidence'; a CI straddling 50% → no
significant edge; unknown signal_id flagged; empty input safe). Critically, it
asserts the registry file is byte-identical before/after — this layer proposes,
never applies (Step 5 is the protected apply path).

Observe-only: reads config/signal_registry.yaml read-only and writes a review
artifact to the POLICY namespace; touches no protected scoring/decision logic.
"""

from __future__ import annotations

import json
from pathlib import Path

from backtesting.tuning_proposals import propose_weight_changes, write_proposals

_REGISTRY = "config/signal_registry.yaml"


def _proposal_for(payload: dict, signal_id: str) -> dict:
    for p in payload["proposals"]:
        if p["signal_id"] == signal_id:
            return p
    raise AssertionError(f"no proposal for {signal_id}")


# --------------------------------------------------------------------------
# Healthy proposals
# --------------------------------------------------------------------------

def test_clear_edge_yields_bounded_nonzero_proposal():
    oos = [{"signal_id": "STRONG_MOVE_UP", "n": 200, "hit_rate": 70.0,
            "hit_rate_ci95": [60.0, 78.0], "avg_return": 1.5}]
    payload = propose_weight_changes(oos, registry_path=_REGISTRY, min_n=50, max_abs_delta=0.05)
    assert payload["observe_only"] is True
    assert payload["proposed_only"] is True
    p = _proposal_for(payload, "STRONG_MOVE_UP")
    assert p["status"] == "proposed"
    assert p["current_weight"] == 0.45
    assert p["proposed_delta"] != 0.0
    assert abs(p["proposed_delta"]) <= 0.05
    assert 0.0 <= p["proposed_weight"] <= 1.0
    assert p["rationale"]  # non-empty rationale string


def test_delta_clamped_to_max_abs_delta():
    oos = [{"signal_id": "STRONG_MOVE_UP", "n": 500, "hit_rate": 90.0,
            "hit_rate_ci95": [85.0, 94.0]}]
    payload = propose_weight_changes(oos, registry_path=_REGISTRY, max_abs_delta=0.05)
    p = _proposal_for(payload, "STRONG_MOVE_UP")
    assert p["proposed_delta"] == 0.05          # clamped from a much larger raw edge
    assert p["proposed_weight"] == 0.50         # 0.45 + 0.05


def test_negative_edge_proposes_reduction():
    oos = [{"signal_id": "STRONG_MOVE_DOWN", "n": 300, "hit_rate": 30.0,
            "hit_rate_ci95": [22.0, 38.0]}]
    payload = propose_weight_changes(oos, registry_path=_REGISTRY, max_abs_delta=0.05)
    p = _proposal_for(payload, "STRONG_MOVE_DOWN")
    assert p["proposed_delta"] < 0.0
    assert p["proposed_weight"] == 0.40         # 0.45 - 0.05
    assert 0.0 <= p["proposed_weight"] <= 1.0


def test_all_proposed_weights_within_unit_range():
    oos = [{"signal_id": "VOLUME_SPIKE", "n": 200, "hit_rate": 5.0, "hit_rate_ci95": [2.0, 9.0]}]
    payload = propose_weight_changes(oos, registry_path=_REGISTRY)
    for p in payload["proposals"]:
        assert 0.0 <= p["proposed_weight"] <= 1.0


def test_accepts_harness_per_pattern_keys():
    # The harness per_pattern breakdown uses 'pattern'/'count', not 'signal_id'/'n'.
    oos = [{"pattern": "STRONG_MOVE_UP", "count": 120, "hit_rate": 68.0}]
    payload = propose_weight_changes(oos, registry_path=_REGISTRY, min_n=50)
    p = _proposal_for(payload, "STRONG_MOVE_UP")
    assert p["status"] == "proposed"


# --------------------------------------------------------------------------
# Degraded / guardrails
# --------------------------------------------------------------------------

def test_insufficient_sample_yields_no_proposal():
    oos = [{"signal_id": "STRONG_MOVE_UP", "n": 10, "hit_rate": 70.0}]
    payload = propose_weight_changes(oos, registry_path=_REGISTRY, min_n=50)
    p = _proposal_for(payload, "STRONG_MOVE_UP")
    assert p["status"] == "insufficient_evidence"
    assert p["proposed_delta"] == 0.0
    assert p["proposed_weight"] == p["current_weight"]


def test_ci_including_coinflip_yields_no_significant_edge():
    oos = [{"signal_id": "VOLUME_SPIKE", "n": 200, "hit_rate": 52.0,
            "hit_rate_ci95": [47.0, 57.0]}]
    payload = propose_weight_changes(oos, registry_path=_REGISTRY, min_n=50)
    p = _proposal_for(payload, "VOLUME_SPIKE")
    assert p["status"] == "no_significant_edge"
    assert p["proposed_delta"] == 0.0


def test_unknown_signal_id_is_flagged_not_proposed():
    oos = [{"signal_id": "NOT_A_REAL_SIGNAL", "n": 200, "hit_rate": 70.0,
            "hit_rate_ci95": [60.0, 80.0]}]
    payload = propose_weight_changes(oos, registry_path=_REGISTRY)
    p = _proposal_for(payload, "NOT_A_REAL_SIGNAL")
    assert p["status"] == "unknown_signal"
    assert p["proposed_delta"] == 0.0


def test_empty_input_is_safe():
    payload = propose_weight_changes([], registry_path=_REGISTRY)
    assert payload["proposals"] == []
    assert payload["observe_only"] is True
    assert payload["summary"]["proposed_count"] == 0


# --------------------------------------------------------------------------
# Registry safety + artifact write
# --------------------------------------------------------------------------

def test_registry_is_byte_identical_after_proposal_and_write(tmp_path):
    before = Path(_REGISTRY).read_bytes()
    oos = [{"signal_id": "STRONG_MOVE_UP", "n": 200, "hit_rate": 70.0,
            "hit_rate_ci95": [60.0, 78.0]}]
    payload = propose_weight_changes(oos, registry_path=_REGISTRY)
    write_proposals(payload, base_dir=str(tmp_path))
    after = Path(_REGISTRY).read_bytes()
    assert before == after, "Step 4 must propose only — the registry must never be mutated"


def test_artifact_written_to_policy_namespace(tmp_path):
    oos = [{"signal_id": "STRONG_MOVE_UP", "n": 200, "hit_rate": 70.0,
            "hit_rate_ci95": [60.0, 78.0]}]
    payload = propose_weight_changes(oos, registry_path=_REGISTRY)
    out = write_proposals(payload, base_dir=str(tmp_path))
    assert out.exists()
    assert "policy" in str(out)
    data = json.loads(out.read_text())
    assert data["observe_only"] is True
    assert data["proposed_only"] is True
