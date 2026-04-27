"""
Tests for watchlist_scanner/approved_allocation_policy_loader.py.

Covers:
  - File-level failures (missing, malformed, non-dict)
  - Validation rule failures (activation_status, applied_to_live, sample_size,
    rank_aware metrics, efficiency_delta)
  - Valid policy happy path
  - Return-type contracts
  - No mutation of input data
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from watchlist_scanner.approved_allocation_policy_loader import (
    load_approved_allocation_policy,
    default_policy_path,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_policy(tmp_path: Path, data: Any) -> Path:
    p = tmp_path / "approved_allocation_policy.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _valid_policy(**overrides) -> dict:
    base = {
        "activation_status": "approved_not_live",
        "applied_to_live": False,
        "approved_at": "2026-04-27T12:00:00",
        "sample_size": 42,
        "primary_window_days": 3,
        "baseline": {"capital_efficiency": 0.12, "total_allocated_pct": 0.60},
        "rank_aware": {"capital_efficiency": 0.15, "total_allocated_pct": 0.65},
        "delta": {
            "efficiency_delta": 0.03,
            "total_return_delta": 0.05,
            "win_capital_delta": 0.01,
        },
        "approval_note": "Test approval note.",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# TestFileLevelFailures
# ---------------------------------------------------------------------------

class TestFileLevelFailures:
    def test_missing_file_returns_none(self, tmp_path):
        result = load_approved_allocation_policy(tmp_path / "nonexistent.json")
        assert result is None

    def test_malformed_json_returns_invalid(self, tmp_path):
        p = tmp_path / "approved_allocation_policy.json"
        p.write_text("{ not valid json }", encoding="utf-8")
        result = load_approved_allocation_policy(p)
        assert result is not None
        assert result["_valid"] is False
        assert "read error" in result["reason"]

    def test_json_array_returns_invalid(self, tmp_path):
        p = _write_policy(tmp_path, [1, 2, 3])
        result = load_approved_allocation_policy(p)
        assert result["_valid"] is False
        assert "not a JSON object" in result["reason"]

    def test_json_string_returns_invalid(self, tmp_path):
        p = _write_policy(tmp_path, "just a string")
        result = load_approved_allocation_policy(p)
        assert result["_valid"] is False

    def test_json_null_returns_invalid(self, tmp_path):
        p = _write_policy(tmp_path, None)
        result = load_approved_allocation_policy(p)
        assert result["_valid"] is False


# ---------------------------------------------------------------------------
# TestActivationStatusRule
# ---------------------------------------------------------------------------

class TestActivationStatusRule:
    def test_wrong_activation_status_rejected(self, tmp_path):
        p = _write_policy(tmp_path, _valid_policy(activation_status="pending"))
        result = load_approved_allocation_policy(p)
        assert result["_valid"] is False
        assert "activation_status" in result["reason"]
        assert "approved_not_live" in result["reason"]

    def test_missing_activation_status_rejected(self, tmp_path):
        data = _valid_policy()
        del data["activation_status"]
        p = _write_policy(tmp_path, data)
        result = load_approved_allocation_policy(p)
        assert result["_valid"] is False

    def test_live_status_rejected(self, tmp_path):
        p = _write_policy(tmp_path, _valid_policy(activation_status="live"))
        result = load_approved_allocation_policy(p)
        assert result["_valid"] is False

    def test_correct_status_passes(self, tmp_path):
        p = _write_policy(tmp_path, _valid_policy())
        result = load_approved_allocation_policy(p)
        assert result["_valid"] is True


# ---------------------------------------------------------------------------
# TestAppliedToLiveRule
# ---------------------------------------------------------------------------

class TestAppliedToLiveRule:
    def test_applied_to_live_true_rejected(self, tmp_path):
        p = _write_policy(tmp_path, _valid_policy(applied_to_live=True))
        result = load_approved_allocation_policy(p)
        assert result["_valid"] is False
        assert "applied_to_live is True" in result["reason"]

    def test_applied_to_live_false_passes(self, tmp_path):
        p = _write_policy(tmp_path, _valid_policy(applied_to_live=False))
        result = load_approved_allocation_policy(p)
        assert result["_valid"] is True

    def test_applied_to_live_missing_passes(self, tmp_path):
        data = _valid_policy()
        del data["applied_to_live"]
        p = _write_policy(tmp_path, data)
        result = load_approved_allocation_policy(p)
        assert result["_valid"] is True

    def test_applied_to_live_none_passes(self, tmp_path):
        p = _write_policy(tmp_path, _valid_policy(applied_to_live=None))
        result = load_approved_allocation_policy(p)
        assert result["_valid"] is True


# ---------------------------------------------------------------------------
# TestSampleSizeRule
# ---------------------------------------------------------------------------

class TestSampleSizeRule:
    def test_sample_size_missing_rejected(self, tmp_path):
        data = _valid_policy()
        del data["sample_size"]
        p = _write_policy(tmp_path, data)
        result = load_approved_allocation_policy(p)
        assert result["_valid"] is False
        assert "sample_size" in result["reason"]

    def test_sample_size_none_rejected(self, tmp_path):
        p = _write_policy(tmp_path, _valid_policy(sample_size=None))
        result = load_approved_allocation_policy(p)
        assert result["_valid"] is False

    def test_sample_size_string_nonnumeric_rejected(self, tmp_path):
        p = _write_policy(tmp_path, _valid_policy(sample_size="lots"))
        result = load_approved_allocation_policy(p)
        assert result["_valid"] is False
        assert "not numeric" in result["reason"]

    def test_sample_size_zero_passes(self, tmp_path):
        p = _write_policy(tmp_path, _valid_policy(sample_size=0))
        result = load_approved_allocation_policy(p)
        assert result["_valid"] is True

    def test_sample_size_string_numeric_passes(self, tmp_path):
        p = _write_policy(tmp_path, _valid_policy(sample_size="50"))
        result = load_approved_allocation_policy(p)
        assert result["_valid"] is True
        assert result["sample_size"] == 50

    def test_sample_size_float_coerced(self, tmp_path):
        p = _write_policy(tmp_path, _valid_policy(sample_size=42.9))
        result = load_approved_allocation_policy(p)
        assert result["_valid"] is True
        assert result["sample_size"] == 42


# ---------------------------------------------------------------------------
# TestRankAwareMetricsRule
# ---------------------------------------------------------------------------

class TestRankAwareMetricsRule:
    def test_rank_aware_missing_rejected(self, tmp_path):
        data = _valid_policy()
        del data["rank_aware"]
        p = _write_policy(tmp_path, data)
        result = load_approved_allocation_policy(p)
        assert result["_valid"] is False
        assert "rank_aware" in result["reason"]

    def test_rank_aware_none_rejected(self, tmp_path):
        p = _write_policy(tmp_path, _valid_policy(rank_aware=None))
        result = load_approved_allocation_policy(p)
        assert result["_valid"] is False

    def test_rank_aware_empty_dict_rejected(self, tmp_path):
        p = _write_policy(tmp_path, _valid_policy(rank_aware={}))
        result = load_approved_allocation_policy(p)
        assert result["_valid"] is False
        assert "capital_efficiency" in result["reason"]

    def test_rank_aware_missing_capital_efficiency_rejected(self, tmp_path):
        p = _write_policy(tmp_path, _valid_policy(rank_aware={"total_allocated_pct": 0.5}))
        result = load_approved_allocation_policy(p)
        assert result["_valid"] is False

    def test_rank_aware_with_capital_efficiency_passes(self, tmp_path):
        p = _write_policy(tmp_path, _valid_policy(
            rank_aware={"capital_efficiency": 0.15, "total_allocated_pct": 0.60}
        ))
        result = load_approved_allocation_policy(p)
        assert result["_valid"] is True


# ---------------------------------------------------------------------------
# TestEfficiencyDeltaRule
# ---------------------------------------------------------------------------

class TestEfficiencyDeltaRule:
    def test_efficiency_delta_missing_rejected(self, tmp_path):
        data = _valid_policy()
        data["delta"] = {"total_return_delta": 0.05}
        p = _write_policy(tmp_path, data)
        result = load_approved_allocation_policy(p)
        assert result["_valid"] is False
        assert "efficiency_delta" in result["reason"]

    def test_efficiency_delta_zero_rejected(self, tmp_path):
        data = _valid_policy()
        data["delta"]["efficiency_delta"] = 0.0
        p = _write_policy(tmp_path, data)
        result = load_approved_allocation_policy(p)
        assert result["_valid"] is False
        assert "not positive" in result["reason"]

    def test_efficiency_delta_negative_rejected(self, tmp_path):
        data = _valid_policy()
        data["delta"]["efficiency_delta"] = -0.01
        p = _write_policy(tmp_path, data)
        result = load_approved_allocation_policy(p)
        assert result["_valid"] is False

    def test_efficiency_delta_nonnumeric_rejected(self, tmp_path):
        data = _valid_policy()
        data["delta"]["efficiency_delta"] = "bad"
        p = _write_policy(tmp_path, data)
        result = load_approved_allocation_policy(p)
        assert result["_valid"] is False
        assert "not numeric" in result["reason"]

    def test_efficiency_delta_positive_passes(self, tmp_path):
        p = _write_policy(tmp_path, _valid_policy())
        result = load_approved_allocation_policy(p)
        assert result["_valid"] is True

    def test_efficiency_delta_very_small_positive_passes(self, tmp_path):
        data = _valid_policy()
        data["delta"]["efficiency_delta"] = 0.0001
        p = _write_policy(tmp_path, data)
        result = load_approved_allocation_policy(p)
        assert result["_valid"] is True

    def test_delta_missing_entirely_rejected(self, tmp_path):
        data = _valid_policy()
        del data["delta"]
        p = _write_policy(tmp_path, data)
        result = load_approved_allocation_policy(p)
        assert result["_valid"] is False


# ---------------------------------------------------------------------------
# TestValidPolicy
# ---------------------------------------------------------------------------

class TestValidPolicy:
    def test_returns_valid_true(self, tmp_path):
        p = _write_policy(tmp_path, _valid_policy())
        result = load_approved_allocation_policy(p)
        assert result is not None
        assert result["_valid"] is True

    def test_activation_status_present(self, tmp_path):
        p = _write_policy(tmp_path, _valid_policy())
        result = load_approved_allocation_policy(p)
        assert result["activation_status"] == "approved_not_live"

    def test_approved_at_present(self, tmp_path):
        p = _write_policy(tmp_path, _valid_policy())
        result = load_approved_allocation_policy(p)
        assert result["approved_at"] == "2026-04-27T12:00:00"

    def test_sample_size_integer(self, tmp_path):
        p = _write_policy(tmp_path, _valid_policy())
        result = load_approved_allocation_policy(p)
        assert result["sample_size"] == 42
        assert isinstance(result["sample_size"], int)

    def test_rank_aware_dict_returned(self, tmp_path):
        p = _write_policy(tmp_path, _valid_policy())
        result = load_approved_allocation_policy(p)
        assert isinstance(result["rank_aware"], dict)
        assert "capital_efficiency" in result["rank_aware"]

    def test_baseline_dict_returned(self, tmp_path):
        p = _write_policy(tmp_path, _valid_policy())
        result = load_approved_allocation_policy(p)
        assert isinstance(result["baseline"], dict)

    def test_delta_dict_returned(self, tmp_path):
        p = _write_policy(tmp_path, _valid_policy())
        result = load_approved_allocation_policy(p)
        assert isinstance(result["delta"], dict)
        assert result["delta"]["efficiency_delta"] == pytest.approx(0.03)

    def test_primary_window_days_returned(self, tmp_path):
        p = _write_policy(tmp_path, _valid_policy())
        result = load_approved_allocation_policy(p)
        assert result["primary_window_days"] == 3

    def test_approval_note_returned(self, tmp_path):
        p = _write_policy(tmp_path, _valid_policy())
        result = load_approved_allocation_policy(p)
        assert result["approval_note"] == "Test approval note."

    def test_original_data_not_mutated(self, tmp_path):
        policy_data = _valid_policy()
        original_rank_aware = dict(policy_data["rank_aware"])
        p = _write_policy(tmp_path, policy_data)
        load_approved_allocation_policy(p)
        assert policy_data["rank_aware"] == original_rank_aware


# ---------------------------------------------------------------------------
# TestInvalidResultShape
# ---------------------------------------------------------------------------

class TestInvalidResultShape:
    def test_invalid_result_has_reason(self, tmp_path):
        p = _write_policy(tmp_path, _valid_policy(activation_status="wrong"))
        result = load_approved_allocation_policy(p)
        assert "_valid" in result
        assert "reason" in result
        assert "activation_status" in result
        assert "approved_at" in result

    def test_invalid_approved_at_preserved(self, tmp_path):
        p = _write_policy(tmp_path, _valid_policy(activation_status="wrong"))
        result = load_approved_allocation_policy(p)
        assert result["approved_at"] == "2026-04-27T12:00:00"

    def test_string_path_accepted(self, tmp_path):
        p = _write_policy(tmp_path, _valid_policy())
        result = load_approved_allocation_policy(str(p))
        assert result["_valid"] is True


# ---------------------------------------------------------------------------
# TestDefaultPolicyPath
# ---------------------------------------------------------------------------

class TestDefaultPolicyPath:
    def test_returns_path_instance(self):
        result = default_policy_path()
        assert isinstance(result, Path)

    def test_path_ends_with_artifact_name(self):
        result = default_policy_path()
        assert result.name == "approved_allocation_policy.json"

    def test_custom_root_respected(self, tmp_path):
        result = default_policy_path(root=tmp_path)
        assert str(tmp_path) in str(result)
