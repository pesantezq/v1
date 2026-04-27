from __future__ import annotations

import json
from pathlib import Path

import pytest

from watchlist_scanner.alert_ranking import apply_priority_score
from watchlist_scanner.approved_config_loader import (
    _REQUIRED_WEIGHT_KEYS,
    _WEIGHT_SUM_TOLERANCE,
    load_approved_weights,
)
from watchlist_scanner.weight_tuning import CURRENT_WEIGHTS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_config(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "approved_ranking_config.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _valid_config(**overrides) -> dict:
    base = {
        "applied_to_live": False,
        "recommended_candidate": "portfolio_fit_heavy",
        "approved_at": "2026-04-27T12:00:00",
        "proposed_weights": {
            "augmented_signal_score": 0.30,
            "confidence_score": 0.20,
            "theme_alignment_score": 0.10,
            "portfolio_fit_score": 0.40,
        },
    }
    base.update(overrides)
    return base


def _signal(**overrides) -> dict:
    base = {
        "signal_score": 0.6,
        "augmented_signal_score": 0.65,
        "confidence_score": 0.7,
        "theme_alignment_score": 0.4,
        "portfolio_fit_score": 0.8,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# TestLoadApprovedWeights — file-level failures
# ---------------------------------------------------------------------------

class TestLoadApprovedWeights:
    def test_missing_file_returns_none(self, tmp_path):
        result = load_approved_weights(tmp_path / "no_file.json")
        assert result is None

    def test_malformed_json_returns_invalid(self, tmp_path):
        p = tmp_path / "approved_ranking_config.json"
        p.write_text("{bad json", encoding="utf-8")
        result = load_approved_weights(p)
        assert result is not None
        assert result["_valid"] is False

    def test_non_dict_json_returns_invalid(self, tmp_path):
        p = tmp_path / "approved_ranking_config.json"
        p.write_text("[1, 2, 3]", encoding="utf-8")
        result = load_approved_weights(p)
        assert result["_valid"] is False

    def test_applied_to_live_true_rejected(self, tmp_path):
        _write_config(tmp_path, _valid_config(applied_to_live=True))
        result = load_approved_weights(tmp_path / "approved_ranking_config.json")
        assert result["_valid"] is False
        assert "applied_to_live" in result["reason"]

    def test_applied_to_live_false_accepted(self, tmp_path):
        _write_config(tmp_path, _valid_config(applied_to_live=False))
        result = load_approved_weights(tmp_path / "approved_ranking_config.json")
        assert result["_valid"] is True

    def test_applied_to_live_absent_accepted(self, tmp_path):
        data = _valid_config()
        data.pop("applied_to_live", None)
        _write_config(tmp_path, data)
        result = load_approved_weights(tmp_path / "approved_ranking_config.json")
        assert result["_valid"] is True


# ---------------------------------------------------------------------------
# TestWeightValidation
# ---------------------------------------------------------------------------

class TestWeightValidation:
    def test_missing_proposed_weights_returns_invalid(self, tmp_path):
        data = _valid_config()
        del data["proposed_weights"]
        _write_config(tmp_path, data)
        result = load_approved_weights(tmp_path / "approved_ranking_config.json")
        assert result["_valid"] is False
        assert "proposed_weights" in result["reason"]

    def test_empty_proposed_weights_returns_invalid(self, tmp_path):
        _write_config(tmp_path, _valid_config(proposed_weights={}))
        result = load_approved_weights(tmp_path / "approved_ranking_config.json")
        assert result["_valid"] is False

    def test_missing_required_key_returns_invalid(self, tmp_path):
        w = dict(_valid_config()["proposed_weights"])
        del w["portfolio_fit_score"]
        _write_config(tmp_path, _valid_config(proposed_weights=w))
        result = load_approved_weights(tmp_path / "approved_ranking_config.json")
        assert result["_valid"] is False
        assert "portfolio_fit_score" in result["reason"]

    def test_non_numeric_weight_returns_invalid(self, tmp_path):
        w = dict(_valid_config()["proposed_weights"])
        w["augmented_signal_score"] = "not_a_number"
        _write_config(tmp_path, _valid_config(proposed_weights=w))
        result = load_approved_weights(tmp_path / "approved_ranking_config.json")
        assert result["_valid"] is False

    def test_weights_not_summing_to_one_returns_invalid(self, tmp_path):
        w = {
            "augmented_signal_score": 0.40,
            "confidence_score": 0.40,
            "theme_alignment_score": 0.40,
            "portfolio_fit_score": 0.40,
        }
        _write_config(tmp_path, _valid_config(proposed_weights=w))
        result = load_approved_weights(tmp_path / "approved_ranking_config.json")
        assert result["_valid"] is False
        assert "sum" in result["reason"].lower()

    def test_sum_within_tolerance_accepted(self, tmp_path):
        # sum = 1.0 + tolerance/2 — should pass
        margin = _WEIGHT_SUM_TOLERANCE / 2
        w = {
            "augmented_signal_score": 0.30 + margin,
            "confidence_score": 0.20,
            "theme_alignment_score": 0.10,
            "portfolio_fit_score": 0.40,
        }
        _write_config(tmp_path, _valid_config(proposed_weights=w))
        result = load_approved_weights(tmp_path / "approved_ranking_config.json")
        assert result["_valid"] is True

    def test_sum_outside_tolerance_rejected(self, tmp_path):
        margin = _WEIGHT_SUM_TOLERANCE + 0.01  # just over tolerance
        w = {
            "augmented_signal_score": 0.30 + margin,
            "confidence_score": 0.20,
            "theme_alignment_score": 0.10,
            "portfolio_fit_score": 0.40,
        }
        _write_config(tmp_path, _valid_config(proposed_weights=w))
        result = load_approved_weights(tmp_path / "approved_ranking_config.json")
        assert result["_valid"] is False


# ---------------------------------------------------------------------------
# TestValidConfigShape
# ---------------------------------------------------------------------------

class TestValidConfigShape:
    def test_valid_config_returns_valid_true(self, tmp_path):
        _write_config(tmp_path, _valid_config())
        result = load_approved_weights(tmp_path / "approved_ranking_config.json")
        assert result["_valid"] is True

    def test_valid_config_has_weights_dict(self, tmp_path):
        _write_config(tmp_path, _valid_config())
        result = load_approved_weights(tmp_path / "approved_ranking_config.json")
        assert isinstance(result["weights"], dict)

    def test_valid_config_weights_are_floats(self, tmp_path):
        _write_config(tmp_path, _valid_config())
        result = load_approved_weights(tmp_path / "approved_ranking_config.json")
        for k in _REQUIRED_WEIGHT_KEYS:
            assert isinstance(result["weights"][k], float)

    def test_valid_config_carries_candidate(self, tmp_path):
        _write_config(tmp_path, _valid_config())
        result = load_approved_weights(tmp_path / "approved_ranking_config.json")
        assert result["recommended_candidate"] == "portfolio_fit_heavy"

    def test_valid_config_carries_approved_at(self, tmp_path):
        _write_config(tmp_path, _valid_config())
        result = load_approved_weights(tmp_path / "approved_ranking_config.json")
        assert result["approved_at"] == "2026-04-27T12:00:00"

    def test_extra_weight_keys_allowed(self, tmp_path):
        w = dict(_valid_config()["proposed_weights"])
        w["some_extra_key"] = 0.0
        _write_config(tmp_path, _valid_config(proposed_weights=w))
        result = load_approved_weights(tmp_path / "approved_ranking_config.json")
        assert result["_valid"] is True

    def test_all_required_weight_keys_present(self, tmp_path):
        _write_config(tmp_path, _valid_config())
        result = load_approved_weights(tmp_path / "approved_ranking_config.json")
        assert _REQUIRED_WEIGHT_KEYS.issubset(result["weights"].keys())


# ---------------------------------------------------------------------------
# TestApplyPriorityScoreDefaults — no approved config
# ---------------------------------------------------------------------------

class TestApplyPriorityScoreDefaults:
    def test_no_config_source_is_default(self):
        row = _signal()
        apply_priority_score(row)
        assert row["final_rank_weights_source"] == "default"

    def test_no_config_candidate_is_default(self):
        row = _signal()
        apply_priority_score(row)
        assert row["final_rank_weights_candidate"] == "default"

    def test_no_config_approved_at_is_none(self):
        row = _signal()
        apply_priority_score(row)
        assert row["final_rank_weights_approved_at"] is None

    def test_no_config_config_valid_is_false(self):
        row = _signal()
        apply_priority_score(row)
        assert row["final_rank_weight_config_valid"] is False

    def test_none_config_uses_defaults(self):
        row = _signal()
        apply_priority_score(row, approved_weights_config=None)
        assert row["final_rank_weights_source"] == "default"

    def test_invalid_config_falls_back_to_default(self):
        row = _signal()
        apply_priority_score(row, approved_weights_config={"_valid": False, "reason": "bad"})
        assert row["final_rank_weights_source"] == "default"
        assert row["final_rank_weight_config_valid"] is False


# ---------------------------------------------------------------------------
# TestApplyPriorityScoreApproved — with valid approved config
# ---------------------------------------------------------------------------

class TestApplyPriorityScoreApproved:
    def _approved_cfg(self, **weight_overrides) -> dict:
        w = {
            "augmented_signal_score": 0.30,
            "confidence_score": 0.20,
            "theme_alignment_score": 0.10,
            "portfolio_fit_score": 0.40,
        }
        w.update(weight_overrides)
        return {
            "_valid": True,
            "weights": w,
            "recommended_candidate": "portfolio_fit_heavy",
            "approved_at": "2026-04-27T12:00:00",
        }

    def test_approved_source_field(self):
        row = _signal()
        apply_priority_score(row, approved_weights_config=self._approved_cfg())
        assert row["final_rank_weights_source"] == "approved"

    def test_approved_candidate_field(self):
        row = _signal()
        apply_priority_score(row, approved_weights_config=self._approved_cfg())
        assert row["final_rank_weights_candidate"] == "portfolio_fit_heavy"

    def test_approved_at_field(self):
        row = _signal()
        apply_priority_score(row, approved_weights_config=self._approved_cfg())
        assert row["final_rank_weights_approved_at"] == "2026-04-27T12:00:00"

    def test_config_valid_field_true(self):
        row = _signal()
        apply_priority_score(row, approved_weights_config=self._approved_cfg())
        assert row["final_rank_weight_config_valid"] is True

    def test_approved_weights_change_final_rank_score(self):
        # portfolio_fit_heavy shifts weight toward portfolio_fit_score
        row_default = _signal(portfolio_fit_score=1.0, augmented_signal_score=0.1)
        row_approved = _signal(portfolio_fit_score=1.0, augmented_signal_score=0.1)

        apply_priority_score(row_default)
        apply_priority_score(row_approved, approved_weights_config=self._approved_cfg())

        # With portfolio_fit_heavy, portfolio_fit_score gets 0.40 vs 0.20 default,
        # so approved score should be higher given fit=1.0 and aug=0.1
        assert row_approved["final_rank_score"] != row_default["final_rank_score"]

    def test_approved_weights_correct_formula(self):
        row = _signal(
            augmented_signal_score=0.5,
            confidence_score=0.6,
            theme_alignment_score=0.3,
            portfolio_fit_score=0.8,
        )
        cfg = self._approved_cfg()
        apply_priority_score(row, approved_weights_config=cfg)
        w = cfg["weights"]
        expected = round(
            0.5 * w["augmented_signal_score"]
            + 0.6 * w["confidence_score"]
            + 0.3 * w["theme_alignment_score"]
            + 0.8 * w["portfolio_fit_score"],
            4,
        )
        assert row["final_rank_score"] == pytest.approx(expected)


# ---------------------------------------------------------------------------
# TestPriorityScoreUnchanged — approved weights must NOT affect priority_score
# ---------------------------------------------------------------------------

class TestPriorityScoreUnchanged:
    def test_priority_score_same_with_and_without_approved_weights(self):
        row_default = _signal()
        row_approved = _signal()
        cfg = {
            "_valid": True,
            "weights": {"augmented_signal_score": 0.30, "confidence_score": 0.20,
                        "theme_alignment_score": 0.10, "portfolio_fit_score": 0.40},
            "recommended_candidate": "portfolio_fit_heavy",
            "approved_at": "2026-04-27T12:00:00",
        }
        apply_priority_score(row_default)
        apply_priority_score(row_approved, approved_weights_config=cfg)
        assert row_default["priority_score"] == row_approved["priority_score"]

    def test_augmented_priority_score_unchanged(self):
        row_default = _signal()
        row_approved = _signal()
        cfg = {
            "_valid": True,
            "weights": {"augmented_signal_score": 0.10, "confidence_score": 0.10,
                        "theme_alignment_score": 0.40, "portfolio_fit_score": 0.40},
            "recommended_candidate": "theme_heavy",
            "approved_at": "2026-04-27T12:00:00",
        }
        apply_priority_score(row_default)
        apply_priority_score(row_approved, approved_weights_config=cfg)
        assert row_default["augmented_priority_score"] == row_approved["augmented_priority_score"]

    def test_signal_score_not_mutated(self):
        row = _signal(signal_score=0.72)
        apply_priority_score(row, approved_weights_config={
            "_valid": True,
            "weights": {"augmented_signal_score": 0.30, "confidence_score": 0.20,
                        "theme_alignment_score": 0.10, "portfolio_fit_score": 0.40},
            "recommended_candidate": "portfolio_fit_heavy",
            "approved_at": "2026-04-27T12:00:00",
        })
        assert row["signal_score"] == pytest.approx(0.72)

    def test_augmented_signal_score_not_mutated(self):
        row = _signal(augmented_signal_score=0.68)
        apply_priority_score(row, approved_weights_config={
            "_valid": True,
            "weights": {"augmented_signal_score": 0.55, "confidence_score": 0.20,
                        "theme_alignment_score": 0.15, "portfolio_fit_score": 0.10},
            "recommended_candidate": "signal_heavy",
            "approved_at": "2026-04-27T12:00:00",
        })
        assert row["augmented_signal_score"] == pytest.approx(0.68)


# ---------------------------------------------------------------------------
# TestAlertEligibilityUnchanged — approved weights do NOT touch gating fields
# ---------------------------------------------------------------------------

class TestAlertEligibilityUnchanged:
    def _run_both(self, row_data: dict) -> tuple[dict, dict]:
        row_default = dict(row_data)
        row_approved = dict(row_data)
        cfg = {
            "_valid": True,
            "weights": {"augmented_signal_score": 0.10, "confidence_score": 0.10,
                        "theme_alignment_score": 0.40, "portfolio_fit_score": 0.40},
            "recommended_candidate": "portfolio_fit_heavy",
            "approved_at": "2026-04-27T12:00:00",
        }
        apply_priority_score(row_default)
        apply_priority_score(row_approved, approved_weights_config=cfg)
        return row_default, row_approved

    def test_priority_explanation_unchanged(self):
        d, a = self._run_both(_signal())
        assert d["priority_explanation"] == a["priority_explanation"]

    def test_confidence_score_field_unchanged(self):
        row_data = _signal(confidence_score=0.85)
        d, a = self._run_both(row_data)
        assert d["confidence_score"] == a["confidence_score"]

    def test_theme_alignment_score_field_unchanged(self):
        row_data = _signal(theme_alignment_score=0.55)
        d, a = self._run_both(row_data)
        assert d["theme_alignment_score"] == a["theme_alignment_score"]

    def test_portfolio_fit_score_field_unchanged(self):
        row_data = _signal(portfolio_fit_score=0.9)
        d, a = self._run_both(row_data)
        assert d["portfolio_fit_score"] == a["portfolio_fit_score"]
