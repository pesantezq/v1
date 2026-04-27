from __future__ import annotations

import json
from pathlib import Path

import pytest

from watchlist_scanner.allocation_policy_activation import (
    ALL_RULES,
    RULE_EFFICIENCY_POSITIVE,
    RULE_NOT_APPLIED,
    RULE_OBSERVE_ONLY,
    RULE_RANK_AWARE_BEATS_BASELINE,
    RULE_SAMPLE_SIZE,
    RULE_SIMULATION_EXISTS,
    _DEFAULT_MIN_SAMPLE_SIZE,
    _append_audit_row,
    _load_simulation,
    build_approved_allocation_policy,
    evaluate_activation_rules,
    run_activation_check,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sim(
    *,
    observe_only: bool = True,
    not_applied: bool = True,
    sample_size: int = 35,
    efficiency_delta: float = 0.10,
    b_eff: float = 0.50,
    ra_eff: float = 0.60,
) -> dict:
    return {
        "generated_at": "2026-04-27T12:00:00",
        "observe_only": observe_only,
        "not_applied": not_applied,
        "primary_window_days": 3,
        "sample_size": sample_size,
        "baseline": {
            "total_return": 1.0,
            "avg_return_per_trade": 0.03,
            "capital_efficiency": b_eff,
            "total_allocated_pct": 0.6,
            "win_capital_pct": 0.55,
            "loss_capital_pct": 0.45,
        },
        "rank_aware": {
            "total_return": 1.4,
            "avg_return_per_trade": 0.04,
            "capital_efficiency": ra_eff,
            "total_allocated_pct": 0.7,
            "win_capital_pct": 0.65,
            "loss_capital_pct": 0.35,
        },
        "delta": {
            "total_return_delta": 0.40,
            "efficiency_delta": efficiency_delta,
            "win_capital_delta": 0.10,
        },
        "details": [],
    }


def _valid_sim() -> dict:
    return _sim()


def _write_sim(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "outputs" / "performance" / "allocation_policy_simulation.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# TestEvaluateActivationRulesNone
# ---------------------------------------------------------------------------

class TestEvaluateActivationRulesNone:
    def test_none_simulation_all_passed_false(self):
        result = evaluate_activation_rules(None)
        assert result["all_passed"] is False

    def test_none_simulation_all_rules_fail(self):
        result = evaluate_activation_rules(None)
        for rule in ALL_RULES:
            assert result["rules"][rule]["passed"] is False

    def test_none_simulation_exists_rule_has_useful_reason(self):
        result = evaluate_activation_rules(None)
        reason = result["rules"][RULE_SIMULATION_EXISTS]["reason"].lower()
        assert "missing" in reason or "unreadable" in reason

    def test_none_simulation_dependent_rules_say_unavailable(self):
        result = evaluate_activation_rules(None)
        for rule in [RULE_OBSERVE_ONLY, RULE_NOT_APPLIED, RULE_SAMPLE_SIZE]:
            assert "unavailable" in result["rules"][rule]["reason"].lower()


# ---------------------------------------------------------------------------
# TestEvaluateActivationRulesValid
# ---------------------------------------------------------------------------

class TestEvaluateActivationRulesValid:
    def test_valid_simulation_all_passed_true(self):
        result = evaluate_activation_rules(_valid_sim())
        assert result["all_passed"] is True

    def test_valid_simulation_all_rules_pass(self):
        result = evaluate_activation_rules(_valid_sim())
        for rule in ALL_RULES:
            assert result["rules"][rule]["passed"] is True, f"Expected {rule} to pass"

    def test_simulation_exists_rule_passes(self):
        result = evaluate_activation_rules(_valid_sim())
        assert result["rules"][RULE_SIMULATION_EXISTS]["passed"] is True

    def test_rules_dict_has_all_rule_keys(self):
        result = evaluate_activation_rules(_valid_sim())
        assert set(result["rules"].keys()) == set(ALL_RULES)

    def test_each_rule_has_passed_and_reason(self):
        result = evaluate_activation_rules(_valid_sim())
        for rule in ALL_RULES:
            r = result["rules"][rule]
            assert "passed" in r
            assert "reason" in r
            assert isinstance(r["reason"], str)


# ---------------------------------------------------------------------------
# TestRuleObserveOnly
# ---------------------------------------------------------------------------

class TestRuleObserveOnly:
    def test_observe_only_false_fails(self):
        result = evaluate_activation_rules(_sim(observe_only=False))
        assert result["rules"][RULE_OBSERVE_ONLY]["passed"] is False
        assert result["all_passed"] is False

    def test_observe_only_true_passes(self):
        result = evaluate_activation_rules(_sim(observe_only=True))
        assert result["rules"][RULE_OBSERVE_ONLY]["passed"] is True

    def test_observe_only_none_fails(self):
        sim = _valid_sim()
        del sim["observe_only"]
        result = evaluate_activation_rules(sim)
        assert result["rules"][RULE_OBSERVE_ONLY]["passed"] is False


# ---------------------------------------------------------------------------
# TestRuleNotApplied
# ---------------------------------------------------------------------------

class TestRuleNotApplied:
    def test_not_applied_false_fails(self):
        result = evaluate_activation_rules(_sim(not_applied=False))
        assert result["rules"][RULE_NOT_APPLIED]["passed"] is False
        assert result["all_passed"] is False

    def test_not_applied_true_passes(self):
        result = evaluate_activation_rules(_sim(not_applied=True))
        assert result["rules"][RULE_NOT_APPLIED]["passed"] is True

    def test_not_applied_absent_fails(self):
        sim = _valid_sim()
        del sim["not_applied"]
        result = evaluate_activation_rules(sim)
        assert result["rules"][RULE_NOT_APPLIED]["passed"] is False


# ---------------------------------------------------------------------------
# TestRuleSampleSize
# ---------------------------------------------------------------------------

class TestRuleSampleSize:
    def test_below_default_minimum_fails(self):
        result = evaluate_activation_rules(_sim(sample_size=5))
        assert result["rules"][RULE_SAMPLE_SIZE]["passed"] is False
        assert result["all_passed"] is False

    def test_at_default_minimum_passes(self):
        result = evaluate_activation_rules(_sim(sample_size=_DEFAULT_MIN_SAMPLE_SIZE))
        assert result["rules"][RULE_SAMPLE_SIZE]["passed"] is True

    def test_above_default_minimum_passes(self):
        result = evaluate_activation_rules(_sim(sample_size=_DEFAULT_MIN_SAMPLE_SIZE + 1))
        assert result["rules"][RULE_SAMPLE_SIZE]["passed"] is True

    def test_custom_minimum_respected(self):
        result = evaluate_activation_rules(_sim(sample_size=10), min_sample_size=15)
        assert result["rules"][RULE_SAMPLE_SIZE]["passed"] is False

    def test_custom_minimum_met_passes(self):
        result = evaluate_activation_rules(_sim(sample_size=15), min_sample_size=15)
        assert result["rules"][RULE_SAMPLE_SIZE]["passed"] is True

    def test_sample_size_reason_contains_values(self):
        result = evaluate_activation_rules(_sim(sample_size=5), min_sample_size=30)
        reason = result["rules"][RULE_SAMPLE_SIZE]["reason"]
        assert "5" in reason and "30" in reason


# ---------------------------------------------------------------------------
# TestRuleEfficiencyDelta
# ---------------------------------------------------------------------------

class TestRuleEfficiencyDelta:
    def test_zero_efficiency_delta_fails(self):
        result = evaluate_activation_rules(_sim(efficiency_delta=0.0))
        assert result["rules"][RULE_EFFICIENCY_POSITIVE]["passed"] is False

    def test_negative_efficiency_delta_fails(self):
        result = evaluate_activation_rules(_sim(efficiency_delta=-0.05))
        assert result["rules"][RULE_EFFICIENCY_POSITIVE]["passed"] is False
        assert result["all_passed"] is False

    def test_positive_efficiency_delta_passes(self):
        result = evaluate_activation_rules(_sim(efficiency_delta=0.01))
        assert result["rules"][RULE_EFFICIENCY_POSITIVE]["passed"] is True

    def test_efficiency_delta_reason_contains_value(self):
        result = evaluate_activation_rules(_sim(efficiency_delta=-0.03))
        reason = result["rules"][RULE_EFFICIENCY_POSITIVE]["reason"]
        assert "-0.03" in reason or "-0.0300" in reason


# ---------------------------------------------------------------------------
# TestRuleRankAwareBeatsBaseline
# ---------------------------------------------------------------------------

class TestRuleRankAwareBeatsBaseline:
    def test_rank_aware_below_baseline_fails(self):
        result = evaluate_activation_rules(_sim(b_eff=0.70, ra_eff=0.60))
        assert result["rules"][RULE_RANK_AWARE_BEATS_BASELINE]["passed"] is False
        assert result["all_passed"] is False

    def test_rank_aware_equal_to_baseline_passes(self):
        result = evaluate_activation_rules(_sim(b_eff=0.60, ra_eff=0.60))
        assert result["rules"][RULE_RANK_AWARE_BEATS_BASELINE]["passed"] is True

    def test_rank_aware_above_baseline_passes(self):
        result = evaluate_activation_rules(_sim(b_eff=0.50, ra_eff=0.70))
        assert result["rules"][RULE_RANK_AWARE_BEATS_BASELINE]["passed"] is True

    def test_rank_aware_reason_contains_both_values(self):
        result = evaluate_activation_rules(_sim(b_eff=0.70, ra_eff=0.60))
        reason = result["rules"][RULE_RANK_AWARE_BEATS_BASELINE]["reason"]
        assert "0.70" in reason or "0.7000" in reason
        assert "0.60" in reason or "0.6000" in reason


# ---------------------------------------------------------------------------
# TestBuildApprovedAllocationPolicy
# ---------------------------------------------------------------------------

class TestBuildApprovedAllocationPolicy:
    def _rule_results(self, sim: dict) -> dict:
        return evaluate_activation_rules(sim)

    def test_applied_to_live_always_false(self):
        sim = _valid_sim()
        artifact = build_approved_allocation_policy(sim, self._rule_results(sim))
        assert artifact["applied_to_live"] is False

    def test_activation_status_approved_not_live(self):
        sim = _valid_sim()
        artifact = build_approved_allocation_policy(sim, self._rule_results(sim))
        assert artifact["activation_status"] == "approved_not_live"

    def test_artifact_has_approved_at(self):
        sim = _valid_sim()
        artifact = build_approved_allocation_policy(sim, self._rule_results(sim))
        assert "approved_at" in artifact
        assert isinstance(artifact["approved_at"], str)

    def test_artifact_carries_simulation_metrics(self):
        sim = _valid_sim()
        artifact = build_approved_allocation_policy(sim, self._rule_results(sim))
        assert artifact["sample_size"] == sim["sample_size"]
        assert "baseline" in artifact
        assert "rank_aware" in artifact
        assert "delta" in artifact

    def test_artifact_carries_rules_passed_list(self):
        sim = _valid_sim()
        rr = self._rule_results(sim)
        artifact = build_approved_allocation_policy(sim, rr)
        assert isinstance(artifact["rules_passed"], list)
        assert len(artifact["rules_passed"]) == len(ALL_RULES)

    def test_artifact_carries_rules_failed_list(self):
        sim = _valid_sim()
        artifact = build_approved_allocation_policy(sim, self._rule_results(sim))
        assert artifact["rules_failed"] == []

    def test_approval_note_embedded(self):
        sim = _valid_sim()
        note = "custom approval note"
        artifact = build_approved_allocation_policy(sim, self._rule_results(sim), approval_note=note)
        assert artifact["approval_note"] == note

    def test_low_sample_warning_false_when_above_minimum(self):
        sim = _sim(sample_size=35)
        artifact = build_approved_allocation_policy(sim, evaluate_activation_rules(sim))
        assert artifact["low_sample_warning"] is False

    def test_no_mutation_of_simulation(self):
        sim = _valid_sim()
        original_sample = sim["sample_size"]
        rr = evaluate_activation_rules(sim)
        build_approved_allocation_policy(sim, rr)
        assert sim["sample_size"] == original_sample

    def test_failed_rules_appear_in_rules_failed(self):
        sim = _sim(sample_size=5)
        rr = evaluate_activation_rules(sim)
        artifact = build_approved_allocation_policy(sim, rr)
        assert RULE_SAMPLE_SIZE in artifact["rules_failed"]


# ---------------------------------------------------------------------------
# TestLoadSimulation
# ---------------------------------------------------------------------------

class TestLoadSimulation:
    def test_missing_file_returns_none(self, tmp_path):
        result = _load_simulation(tmp_path / "no_file.json")
        assert result is None

    def test_malformed_json_returns_none(self, tmp_path):
        p = tmp_path / "sim.json"
        p.write_text("{bad json", encoding="utf-8")
        result = _load_simulation(p)
        assert result is None

    def test_non_dict_returns_none(self, tmp_path):
        p = tmp_path / "sim.json"
        p.write_text("[1, 2]", encoding="utf-8")
        result = _load_simulation(p)
        assert result is None

    def test_valid_simulation_returns_dict(self, tmp_path):
        p = tmp_path / "sim.json"
        p.write_text(json.dumps(_valid_sim()), encoding="utf-8")
        result = _load_simulation(p)
        assert isinstance(result, dict)
        assert result["sample_size"] == _valid_sim()["sample_size"]


# ---------------------------------------------------------------------------
# TestAppendAuditRow
# ---------------------------------------------------------------------------

class TestAppendAuditRow:
    def test_audit_row_written(self, tmp_path):
        audit_path = tmp_path / "audit.jsonl"
        _append_audit_row(
            audit_path,
            event="approved",
            approved_at="2026-04-27T12:00:00",
            dry_run=False,
            rule_results=evaluate_activation_rules(_valid_sim()),
            simulation=_valid_sim(),
        )
        assert audit_path.exists()

    def test_audit_row_applied_to_live_false(self, tmp_path):
        audit_path = tmp_path / "audit.jsonl"
        _append_audit_row(
            audit_path,
            event="approved",
            approved_at="2026-04-27T12:00:00",
            dry_run=False,
            rule_results=evaluate_activation_rules(_valid_sim()),
            simulation=_valid_sim(),
        )
        row = json.loads(audit_path.read_text(encoding="utf-8").strip())
        assert row["applied_to_live"] is False

    def test_audit_rows_append_across_calls(self, tmp_path):
        audit_path = tmp_path / "audit.jsonl"
        rr = evaluate_activation_rules(_valid_sim())
        for _ in range(3):
            _append_audit_row(
                audit_path,
                event="approved",
                approved_at="2026-04-27T12:00:00",
                dry_run=False,
                rule_results=rr,
                simulation=_valid_sim(),
            )
        lines = [l for l in audit_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines) == 3

    def test_audit_row_has_event_field(self, tmp_path):
        audit_path = tmp_path / "audit.jsonl"
        _append_audit_row(
            audit_path,
            event="rejected",
            approved_at="2026-04-27T12:00:00",
            dry_run=False,
            rule_results=evaluate_activation_rules(_sim(sample_size=5)),
            simulation=_sim(sample_size=5),
        )
        row = json.loads(audit_path.read_text(encoding="utf-8").strip())
        assert row["event"] == "rejected"

    def test_audit_row_parent_dir_created(self, tmp_path):
        audit_path = tmp_path / "deep" / "nested" / "audit.jsonl"
        _append_audit_row(
            audit_path,
            event="approved",
            approved_at="2026-04-27T12:00:00",
            dry_run=False,
            rule_results=evaluate_activation_rules(_valid_sim()),
            simulation=_valid_sim(),
        )
        assert audit_path.exists()


# ---------------------------------------------------------------------------
# TestRunActivationCheckDryRun
# ---------------------------------------------------------------------------

class TestRunActivationCheckDryRun:
    def test_dry_run_writes_no_artifact(self, tmp_path):
        _write_sim(tmp_path, _valid_sim())
        run_activation_check(root=tmp_path, output_dir=tmp_path / "out", approve=False)
        assert not (tmp_path / "out" / "approved_allocation_policy.json").exists()

    def test_dry_run_writes_no_audit(self, tmp_path):
        _write_sim(tmp_path, _valid_sim())
        run_activation_check(root=tmp_path, output_dir=tmp_path / "out", approve=False)
        assert not (tmp_path / "out" / "allocation_policy_activation_audit.jsonl").exists()

    def test_dry_run_report_dry_run_true(self, tmp_path):
        _write_sim(tmp_path, _valid_sim())
        report = run_activation_check(root=tmp_path, output_dir=tmp_path / "out", approve=False)
        assert report["dry_run"] is True

    def test_dry_run_report_approved_false(self, tmp_path):
        _write_sim(tmp_path, _valid_sim())
        report = run_activation_check(root=tmp_path, output_dir=tmp_path / "out", approve=False)
        assert report["approved"] is False

    def test_dry_run_still_evaluates_rules(self, tmp_path):
        _write_sim(tmp_path, _valid_sim())
        report = run_activation_check(root=tmp_path, output_dir=tmp_path / "out", approve=False)
        assert "rules" in report
        assert set(report["rules"].keys()) == set(ALL_RULES)

    def test_dry_run_missing_simulation_all_rules_fail(self, tmp_path):
        report = run_activation_check(root=tmp_path, output_dir=tmp_path / "out", approve=False)
        assert report["all_rules_passed"] is False


# ---------------------------------------------------------------------------
# TestRunActivationCheckApprove
# ---------------------------------------------------------------------------

class TestRunActivationCheckApprove:
    def test_approve_with_passing_rules_writes_artifact(self, tmp_path):
        _write_sim(tmp_path, _valid_sim())
        run_activation_check(root=tmp_path, output_dir=tmp_path / "out", approve=True)
        assert (tmp_path / "out" / "approved_allocation_policy.json").exists()

    def test_approve_with_passing_rules_artifact_valid_json(self, tmp_path):
        _write_sim(tmp_path, _valid_sim())
        run_activation_check(root=tmp_path, output_dir=tmp_path / "out", approve=True)
        content = (tmp_path / "out" / "approved_allocation_policy.json").read_text(encoding="utf-8")
        data = json.loads(content)
        assert data["applied_to_live"] is False
        assert data["activation_status"] == "approved_not_live"

    def test_approve_with_passing_rules_writes_audit(self, tmp_path):
        _write_sim(tmp_path, _valid_sim())
        run_activation_check(root=tmp_path, output_dir=tmp_path / "out", approve=True)
        assert (tmp_path / "out" / "allocation_policy_activation_audit.jsonl").exists()

    def test_approve_with_failing_rules_no_artifact(self, tmp_path):
        _write_sim(tmp_path, _sim(sample_size=5))
        run_activation_check(root=tmp_path, output_dir=tmp_path / "out", approve=True)
        assert not (tmp_path / "out" / "approved_allocation_policy.json").exists()

    def test_approve_with_failing_rules_still_writes_audit(self, tmp_path):
        _write_sim(tmp_path, _sim(sample_size=5))
        run_activation_check(root=tmp_path, output_dir=tmp_path / "out", approve=True)
        assert (tmp_path / "out" / "allocation_policy_activation_audit.jsonl").exists()

    def test_approve_rejected_audit_event_is_rejected(self, tmp_path):
        _write_sim(tmp_path, _sim(sample_size=5))
        run_activation_check(root=tmp_path, output_dir=tmp_path / "out", approve=True)
        audit_path = tmp_path / "out" / "allocation_policy_activation_audit.jsonl"
        row = json.loads(audit_path.read_text(encoding="utf-8").strip())
        assert row["event"] == "rejected"

    def test_approve_report_approved_true_when_passed(self, tmp_path):
        _write_sim(tmp_path, _valid_sim())
        report = run_activation_check(root=tmp_path, output_dir=tmp_path / "out", approve=True)
        assert report["approved"] is True
        assert report["artifact_written"] is True
        assert report["audit_written"] is True

    def test_approve_report_approved_false_when_failed(self, tmp_path):
        _write_sim(tmp_path, _sim(efficiency_delta=-0.05))
        report = run_activation_check(root=tmp_path, output_dir=tmp_path / "out", approve=True)
        assert report["approved"] is False
        assert report["artifact_written"] is False

    def test_artifact_does_not_claim_live_application(self, tmp_path):
        _write_sim(tmp_path, _valid_sim())
        run_activation_check(root=tmp_path, output_dir=tmp_path / "out", approve=True)
        data = json.loads(
            (tmp_path / "out" / "approved_allocation_policy.json").read_text(encoding="utf-8")
        )
        assert data["applied_to_live"] is False
        assert "live" not in data.get("activation_status", "").replace("not_live", "")

    def test_missing_simulation_approve_no_artifact(self, tmp_path):
        report = run_activation_check(root=tmp_path, output_dir=tmp_path / "out", approve=True)
        assert report["approved"] is False
        assert not (tmp_path / "out" / "approved_allocation_policy.json").exists()
