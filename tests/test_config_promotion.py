from __future__ import annotations

import json
from pathlib import Path

import pytest

from watchlist_scanner.config_promotion import (
    _APPROVAL_NOTE,
    ConfigPromotionError,
    build_approved_config,
    promote_proposal,
    validate_proposal,
)
from watchlist_scanner.weight_tuning import CURRENT_WEIGHTS


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _valid_proposal(**overrides) -> dict:
    base = {
        "generated_at": "2026-04-27T10:00:00",
        "observe_only": True,
        "applied": False,
        "proposal_status": "not_applied",
        "source": "policy_simulation",
        "recommended_candidate": "portfolio_fit_heavy",
        "recommendation_reason": "Best top-quartile hit rate",
        "proposed_weights": {
            "augmented_signal_score": 0.30,
            "confidence_score": 0.20,
            "theme_alignment_score": 0.10,
            "portfolio_fit_score": 0.40,
        },
        "current_weights": dict(CURRENT_WEIGHTS),
        "weight_deltas": {
            "augmented_signal_score": -0.10,
            "confidence_score": -0.05,
            "theme_alignment_score": -0.05,
            "portfolio_fit_score": 0.20,
        },
        "performance_delta": {
            "hit_rate_delta": 0.08,
            "avg_return_delta": 0.5,
            "direction_correct_rate_delta": 0.06,
        },
        "advisory_note": "Observe-only.",
    }
    base.update(overrides)
    return base


def _write_proposal(tmp_path: Path, proposal: dict | None = None) -> Path:
    path = tmp_path / "config_proposal.json"
    path.write_text(json.dumps(proposal or _valid_proposal()), encoding="utf-8")
    return path


def _write_simulation(tmp_path: Path, sample_size: int = 22, warning: bool = False) -> Path:
    sim = {
        "recommended_policy": {
            "name": "portfolio_fit_heavy",
            "sample_size": sample_size,
            "low_sample_warning": warning,
        }
    }
    path = tmp_path / "policy_simulation.json"
    path.write_text(json.dumps(sim), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# TestValidateProposal
# ---------------------------------------------------------------------------

class TestValidateProposal:
    def test_valid_proposal_passes(self):
        validate_proposal(_valid_proposal())

    def test_rejects_non_dict(self):
        with pytest.raises(ConfigPromotionError, match="not a valid dict"):
            validate_proposal("not a dict")  # type: ignore[arg-type]

    def test_rejects_wrong_status(self):
        with pytest.raises(ConfigPromotionError, match="not_applied"):
            validate_proposal(_valid_proposal(proposal_status="applied"))

    def test_rejects_missing_status(self):
        with pytest.raises(ConfigPromotionError, match="not_applied"):
            validate_proposal(_valid_proposal(proposal_status=None))

    def test_rejects_applied_true(self):
        with pytest.raises(ConfigPromotionError, match="applied"):
            validate_proposal(_valid_proposal(applied=True))

    def test_rejects_applied_none(self):
        with pytest.raises(ConfigPromotionError, match="applied"):
            validate_proposal(_valid_proposal(applied=None))

    def test_rejects_missing_candidate(self):
        with pytest.raises(ConfigPromotionError, match="recommended_candidate"):
            validate_proposal(_valid_proposal(recommended_candidate=""))

    def test_rejects_none_candidate(self):
        with pytest.raises(ConfigPromotionError, match="recommended_candidate"):
            validate_proposal(_valid_proposal(recommended_candidate=None))

    def test_rejects_missing_proposed_weights(self):
        with pytest.raises(ConfigPromotionError, match="proposed_weights"):
            validate_proposal(_valid_proposal(proposed_weights=None))

    def test_rejects_empty_proposed_weights(self):
        with pytest.raises(ConfigPromotionError, match="proposed_weights"):
            validate_proposal(_valid_proposal(proposed_weights={}))


# ---------------------------------------------------------------------------
# TestNoProposalFile
# ---------------------------------------------------------------------------

class TestNoProposalFile:
    def test_raises_when_proposal_absent(self, tmp_path):
        missing = tmp_path / "no_proposal.json"
        with pytest.raises(ConfigPromotionError, match="No config proposal found"):
            promote_proposal(proposal_path=missing, output_dir=tmp_path)

    def test_error_message_includes_path(self, tmp_path):
        missing = tmp_path / "config_proposal.json"
        with pytest.raises(ConfigPromotionError) as exc_info:
            promote_proposal(proposal_path=missing, output_dir=tmp_path)
        assert str(missing) in str(exc_info.value)

    def test_raises_on_malformed_json(self, tmp_path):
        bad = tmp_path / "config_proposal.json"
        bad.write_text("{invalid json", encoding="utf-8")
        with pytest.raises(ConfigPromotionError, match="Could not read proposal"):
            promote_proposal(proposal_path=bad, output_dir=tmp_path)


# ---------------------------------------------------------------------------
# TestDryRun
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_dry_run_writes_no_files(self, tmp_path):
        _write_proposal(tmp_path)
        promote_proposal(proposal_path=tmp_path / "config_proposal.json",
                         output_dir=tmp_path, dry_run=True)
        assert not (tmp_path / "approved_ranking_config.json").exists()
        assert not (tmp_path / "config_promotion_audit.jsonl").exists()

    def test_dry_run_status_field(self, tmp_path):
        _write_proposal(tmp_path)
        result = promote_proposal(proposal_path=tmp_path / "config_proposal.json",
                                  output_dir=tmp_path, dry_run=True)
        assert result["dry_run"] is True
        assert result["status"] == "dry_run"

    def test_dry_run_message_mentions_approve(self, tmp_path):
        _write_proposal(tmp_path)
        result = promote_proposal(proposal_path=tmp_path / "config_proposal.json",
                                  output_dir=tmp_path, dry_run=True)
        assert "--approve" in result["message"]

    def test_dry_run_contains_approved_config_preview(self, tmp_path):
        _write_proposal(tmp_path)
        result = promote_proposal(proposal_path=tmp_path / "config_proposal.json",
                                  output_dir=tmp_path, dry_run=True)
        assert "approved_config" in result
        assert result["approved_config"]["applied_to_live"] is False

    def test_dry_run_still_validates_proposal(self, tmp_path):
        _write_proposal(tmp_path, _valid_proposal(proposal_status="already_applied"))
        with pytest.raises(ConfigPromotionError):
            promote_proposal(proposal_path=tmp_path / "config_proposal.json",
                             output_dir=tmp_path, dry_run=True)


# ---------------------------------------------------------------------------
# TestApproveWritesArtifact
# ---------------------------------------------------------------------------

class TestApproveWritesArtifact:
    def test_approve_writes_approved_config(self, tmp_path):
        _write_proposal(tmp_path)
        promote_proposal(proposal_path=tmp_path / "config_proposal.json",
                         output_dir=tmp_path, dry_run=False)
        assert (tmp_path / "approved_ranking_config.json").exists()

    def test_approved_config_content(self, tmp_path):
        _write_proposal(tmp_path)
        promote_proposal(proposal_path=tmp_path / "config_proposal.json",
                         output_dir=tmp_path, dry_run=False)
        artifact = json.loads((tmp_path / "approved_ranking_config.json").read_text())
        assert artifact["recommended_candidate"] == "portfolio_fit_heavy"
        assert artifact["applied_to_live"] is False

    def test_approve_status_field(self, tmp_path):
        _write_proposal(tmp_path)
        result = promote_proposal(proposal_path=tmp_path / "config_proposal.json",
                                  output_dir=tmp_path, dry_run=False)
        assert result["status"] == "approved"
        assert result["dry_run"] is False

    def test_approve_copies_proposed_weights(self, tmp_path):
        _write_proposal(tmp_path)
        promote_proposal(proposal_path=tmp_path / "config_proposal.json",
                         output_dir=tmp_path, dry_run=False)
        artifact = json.loads((tmp_path / "approved_ranking_config.json").read_text())
        assert artifact["proposed_weights"]["portfolio_fit_score"] == pytest.approx(0.40)

    def test_approve_includes_source_generated_at(self, tmp_path):
        _write_proposal(tmp_path)
        promote_proposal(proposal_path=tmp_path / "config_proposal.json",
                         output_dir=tmp_path, dry_run=False)
        artifact = json.loads((tmp_path / "approved_ranking_config.json").read_text())
        assert artifact["source_proposal_generated_at"] == "2026-04-27T10:00:00"

    def test_approve_with_simulation_pulls_sample_size(self, tmp_path):
        _write_proposal(tmp_path)
        _write_simulation(tmp_path, sample_size=25, warning=False)
        promote_proposal(proposal_path=tmp_path / "config_proposal.json",
                         output_dir=tmp_path, dry_run=False)
        artifact = json.loads((tmp_path / "approved_ranking_config.json").read_text())
        assert artifact["sample_size"] == 25
        assert artifact["low_sample_warning"] is False

    def test_approve_without_simulation_defaults_sample(self, tmp_path):
        _write_proposal(tmp_path)
        promote_proposal(proposal_path=tmp_path / "config_proposal.json",
                         output_dir=tmp_path, dry_run=False)
        artifact = json.loads((tmp_path / "approved_ranking_config.json").read_text())
        assert artifact["sample_size"] is None
        assert artifact["low_sample_warning"] is True


# ---------------------------------------------------------------------------
# TestAuditLog
# ---------------------------------------------------------------------------

class TestAuditLog:
    def test_audit_row_appended_on_approve(self, tmp_path):
        _write_proposal(tmp_path)
        promote_proposal(proposal_path=tmp_path / "config_proposal.json",
                         output_dir=tmp_path, dry_run=False)
        lines = (tmp_path / "config_promotion_audit.jsonl").read_text().strip().splitlines()
        assert len(lines) == 1

    def test_audit_row_is_valid_json(self, tmp_path):
        _write_proposal(tmp_path)
        promote_proposal(proposal_path=tmp_path / "config_proposal.json",
                         output_dir=tmp_path, dry_run=False)
        line = (tmp_path / "config_promotion_audit.jsonl").read_text().strip()
        row = json.loads(line)
        assert row["event"] == "approved"

    def test_audit_row_fields(self, tmp_path):
        _write_proposal(tmp_path)
        promote_proposal(proposal_path=tmp_path / "config_proposal.json",
                         output_dir=tmp_path, dry_run=False)
        row = json.loads((tmp_path / "config_promotion_audit.jsonl").read_text().strip())
        assert row["recommended_candidate"] == "portfolio_fit_heavy"
        assert row["applied_to_live"] is False
        assert row["dry_run"] is False

    def test_audit_accumulates_across_calls(self, tmp_path):
        _write_proposal(tmp_path)
        promote_proposal(proposal_path=tmp_path / "config_proposal.json",
                         output_dir=tmp_path, dry_run=False)
        promote_proposal(proposal_path=tmp_path / "config_proposal.json",
                         output_dir=tmp_path, dry_run=False)
        lines = (tmp_path / "config_promotion_audit.jsonl").read_text().strip().splitlines()
        assert len(lines) == 2

    def test_dry_run_does_not_write_audit(self, tmp_path):
        _write_proposal(tmp_path)
        promote_proposal(proposal_path=tmp_path / "config_proposal.json",
                         output_dir=tmp_path, dry_run=True)
        assert not (tmp_path / "config_promotion_audit.jsonl").exists()


# ---------------------------------------------------------------------------
# TestApprovedArtifactShape
# ---------------------------------------------------------------------------

class TestApprovedArtifactShape:
    def _artifact(self, tmp_path: Path) -> dict:
        _write_proposal(tmp_path)
        promote_proposal(proposal_path=tmp_path / "config_proposal.json",
                         output_dir=tmp_path, dry_run=False)
        return json.loads((tmp_path / "approved_ranking_config.json").read_text())

    def test_applied_to_live_is_false(self, tmp_path):
        assert self._artifact(tmp_path)["applied_to_live"] is False

    def test_required_keys_present(self, tmp_path):
        artifact = self._artifact(tmp_path)
        required = {
            "approved_at", "source_proposal_generated_at", "recommended_candidate",
            "proposed_weights", "current_weights", "weight_deltas", "performance_delta",
            "sample_size", "low_sample_warning", "applied_to_live", "approval_note",
        }
        assert required.issubset(artifact.keys())

    def test_approval_note_present(self, tmp_path):
        artifact = self._artifact(tmp_path)
        assert _APPROVAL_NOTE in artifact["approval_note"]

    def test_approved_at_is_iso_string(self, tmp_path):
        artifact = self._artifact(tmp_path)
        from datetime import datetime
        dt = datetime.fromisoformat(artifact["approved_at"])
        assert dt is not None

    def test_proposed_weights_copied_not_reference(self, tmp_path):
        artifact = self._artifact(tmp_path)
        artifact["proposed_weights"]["augmented_signal_score"] = 99.9
        reload = json.loads((tmp_path / "approved_ranking_config.json").read_text())
        assert reload["proposed_weights"]["augmented_signal_score"] != 99.9

    def test_artifact_is_json_serializable(self, tmp_path):
        _write_proposal(tmp_path)
        result = promote_proposal(proposal_path=tmp_path / "config_proposal.json",
                                  output_dir=tmp_path, dry_run=False)
        parsed = json.loads(json.dumps(result["approved_config"]))
        assert parsed["applied_to_live"] is False


# ---------------------------------------------------------------------------
# TestBuildApprovedConfig
# ---------------------------------------------------------------------------

class TestBuildApprovedConfig:
    def test_applied_to_live_hardcoded_false(self):
        artifact = build_approved_config(_valid_proposal())
        assert artifact["applied_to_live"] is False

    def test_simulation_sample_extracted(self):
        sim = {"recommended_policy": {"sample_size": 30, "low_sample_warning": False}}
        artifact = build_approved_config(_valid_proposal(), simulation=sim)
        assert artifact["sample_size"] == 30
        assert artifact["low_sample_warning"] is False

    def test_no_simulation_sample_defaults(self):
        artifact = build_approved_config(_valid_proposal(), simulation=None)
        assert artifact["sample_size"] is None
        assert artifact["low_sample_warning"] is True

    def test_proposed_weights_are_independent_copy(self):
        proposal = _valid_proposal()
        artifact = build_approved_config(proposal)
        artifact["proposed_weights"]["augmented_signal_score"] = 99.9
        assert proposal["proposed_weights"]["augmented_signal_score"] != 99.9
