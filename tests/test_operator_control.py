"""Tests for the operator-control plane (operator_control/).

Covers: registry validity, work-order validation rules, append-only storage,
append-only audit log, status transitions, the registry-derived requested
action (no command injection), worker-prompt safety content, and the CLI.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from operator_control import (
    work_orders_path,
    audit_log_path,
    prompt_path,
)
from operator_control import probe_registry as pr
from operator_control import skill_registry as sr
from operator_control import repair_policies as policy
from operator_control import audit_log
from operator_control import work_orders as wo
from operator_control import worker_prompts as wp


# ---------------------------------------------------------------------------
# Registries validate
# ---------------------------------------------------------------------------


def test_probe_registry_valid():
    assert pr.validate_registry() == []


def test_skill_registry_valid():
    assert sr.validate_registry() == []


def test_every_probe_resolves_to_a_skill_for_each_action():
    for probe in pr.list_probes():
        for action in probe.allowed_actions:
            skill = sr.skill_for_probe_action(probe.probe_id, action)
            assert skill is not None, (probe.probe_id, action)
            assert action in skill.allowed_modes


def test_all_spec_probes_present():
    expected = {
        "daily_run.failed_stages",
        "data_quality.warnings",
        "pipeline.run_status",
        "ai_budget.status",
        "fmp_budget.status",
        "memo.delivery_status",
        "schwab.broker_health",
        "artifact_registry.status",
        "quant.confidence_calibration",
        "quant.pattern_efficacy",
        "quant.retune_suggestions",
        "portfolio.risk_near_cap",
        "portfolio.advisory_decision_queue",
        "memo.generation_readability",
    }
    assert expected <= set(pr.probe_ids())


def test_all_spec_skills_present():
    expected = {
        "diagnose_daily_run_failure",
        "diagnose_data_quality_warnings",
        "propose_data_quality_fix",
        "diagnose_pipeline_status",
        "diagnose_quant_calibration",
        "propose_quant_retune_review",
        "diagnose_portfolio_risk",
        "regenerate_memo_from_artifacts",
        "diagnose_schwab_read_only_health",
        "inspect_artifact_registry",
    }
    assert expected <= set(sr.skill_ids())


def test_global_forbidden_actions_on_every_skill():
    for skill in sr.list_skills():
        eff = skill.effective_forbidden_actions()
        joined = " ".join(eff).lower()
        assert "trade" in joined
        assert "broker" in joined
        assert "scoring" in joined or "decision_engine" in joined
        assert "secret" in joined


# ---------------------------------------------------------------------------
# Validation rules
# ---------------------------------------------------------------------------


def test_unknown_probe_rejected(tmp_path):
    with pytest.raises(policy.WorkOrderValidationError, match="unknown probe"):
        wo.create_work_order(
            tmp_path, probe_id="nope", skill_id="diagnose_data_quality_warnings",
            mode="diagnose", created_by="t",
        )


def test_unknown_skill_rejected(tmp_path):
    with pytest.raises(policy.WorkOrderValidationError, match="unknown skill"):
        wo.create_work_order(
            tmp_path, probe_id="data_quality.warnings", skill_id="nope",
            mode="diagnose", created_by="t",
        )


def test_non_allowlisted_combination_rejected(tmp_path):
    with pytest.raises(policy.WorkOrderValidationError, match="not allowlisted"):
        wo.create_work_order(
            tmp_path, probe_id="data_quality.warnings",
            skill_id="inspect_artifact_registry", mode="diagnose", created_by="t",
        )


def test_disallowed_mode_rejected(tmp_path):
    # diagnose-only skill cannot run safe_repair
    with pytest.raises(policy.WorkOrderValidationError, match="not allowed by skill"):
        wo.create_work_order(
            tmp_path, probe_id="data_quality.warnings",
            skill_id="diagnose_data_quality_warnings", mode="safe_repair",
            created_by="t",
        )


def test_rejection_is_audited(tmp_path):
    with pytest.raises(policy.WorkOrderValidationError):
        wo.create_work_order(
            tmp_path, probe_id="nope", skill_id="x", mode="diagnose", created_by="t",
        )
    events = audit_log.read_events(tmp_path)
    assert any(e["event_type"] == "validation_rejected" for e in events)


# ---------------------------------------------------------------------------
# Approval policy
# ---------------------------------------------------------------------------


def test_diagnose_low_risk_is_queued_no_approval(tmp_path):
    rec = wo.create_work_order(
        tmp_path, probe_id="pipeline.run_status",
        skill_id="diagnose_pipeline_status", mode="diagnose", created_by="t",
    )
    assert rec["approval_required"] is False
    assert rec["status"] == "queued"


def test_safe_repair_requires_approval(tmp_path):
    rec = wo.create_work_order(
        tmp_path, probe_id="memo.generation_readability",
        skill_id="regenerate_memo_from_artifacts", mode="safe_repair",
        created_by="t",
    )
    assert rec["approval_required"] is True
    assert rec["status"] == "awaiting_approval"
    assert rec["risk_level"] in ("medium", "high")


def test_propose_fix_on_flagged_probe_requires_approval(tmp_path):
    rec = wo.create_work_order(
        tmp_path, probe_id="quant.retune_suggestions",
        skill_id="propose_quant_retune_review", mode="propose_fix", created_by="t",
    )
    assert rec["approval_required"] is True
    assert rec["status"] == "awaiting_approval"


# ---------------------------------------------------------------------------
# requested_action carries no caller-supplied / executable text
# ---------------------------------------------------------------------------


def test_requested_action_is_registry_derived(tmp_path):
    rec = wo.create_work_order(
        tmp_path, probe_id="data_quality.warnings",
        skill_id="diagnose_data_quality_warnings", mode="diagnose", created_by="t",
    )
    # No place for an executable command; only probe/skill names + mode.
    assert "Diagnose" in rec["requested_action"]
    assert "Data quality warnings" in rec["requested_action"]
    assert "diagnose_data_quality_warnings" not in rec["requested_action"]  # uses name


def test_work_order_has_no_executable_command_field(tmp_path):
    rec = wo.create_work_order(
        tmp_path, probe_id="data_quality.warnings",
        skill_id="diagnose_data_quality_warnings", mode="diagnose", created_by="t",
    )
    for forbidden_key in ("command", "cmd", "shell", "exec", "script"):
        assert forbidden_key not in rec
    assert rec["observe_only"] is True
    assert "no_trade_execution" in rec["safety_constraints"]


# ---------------------------------------------------------------------------
# Append-only storage
# ---------------------------------------------------------------------------


def test_work_orders_are_append_only(tmp_path):
    wo.create_work_order(
        tmp_path, probe_id="pipeline.run_status",
        skill_id="diagnose_pipeline_status", mode="diagnose", created_by="t",
    )
    path = work_orders_path(tmp_path)
    size_after_create = path.stat().st_size
    n_lines_1 = len(path.read_text().splitlines())

    rec2 = wo.create_work_order(
        tmp_path, probe_id="data_quality.warnings",
        skill_id="diagnose_data_quality_warnings", mode="diagnose", created_by="t",
    )
    # A transition appends, never rewrites — file only grows.
    wo.transition_work_order(tmp_path, rec2["work_order_id"],
                             new_status="cancelled", actor="t")
    n_lines_2 = len(path.read_text().splitlines())
    assert path.stat().st_size > size_after_create
    assert n_lines_2 > n_lines_1


def test_fold_returns_latest_state(tmp_path):
    rec = wo.create_work_order(
        tmp_path, probe_id="data_quality.warnings",
        skill_id="diagnose_data_quality_warnings", mode="diagnose", created_by="t",
    )
    wo.transition_work_order(tmp_path, rec["work_order_id"],
                             new_status="cancelled", actor="t")
    folded = wo.get_work_order(tmp_path, rec["work_order_id"])
    assert folded["status"] == "cancelled"
    assert len(folded["status_history"]) == 2


def test_audit_log_is_append_only(tmp_path):
    audit_log.record_event(tmp_path, event_type="work_order_created", actor="t")
    path = audit_log_path(tmp_path)
    n1 = len(path.read_text().splitlines())
    audit_log.record_event(tmp_path, event_type="prompt_generated", actor="t")
    n2 = len(path.read_text().splitlines())
    assert n2 == n1 + 1


# ---------------------------------------------------------------------------
# Status transitions
# ---------------------------------------------------------------------------


def test_illegal_transition_rejected(tmp_path):
    rec = wo.create_work_order(
        tmp_path, probe_id="pipeline.run_status",
        skill_id="diagnose_pipeline_status", mode="diagnose", created_by="t",
    )
    # queued → completed is not legal
    with pytest.raises(policy.WorkOrderValidationError, match="illegal transition"):
        wo.transition_work_order(tmp_path, rec["work_order_id"],
                                 new_status="completed", actor="t")


def test_approval_flow_transitions(tmp_path):
    rec = wo.create_work_order(
        tmp_path, probe_id="memo.generation_readability",
        skill_id="regenerate_memo_from_artifacts", mode="safe_repair",
        created_by="t",
    )
    wo.transition_work_order(tmp_path, rec["work_order_id"],
                             new_status="approved", actor="operator")
    assert wo.get_work_order(tmp_path, rec["work_order_id"])["status"] == "approved"
    events = audit_log.read_events(tmp_path)
    assert any(e["event_type"] == "approval_granted" for e in events)


# ---------------------------------------------------------------------------
# Worker prompt content
# ---------------------------------------------------------------------------


def test_generated_prompt_has_safety_content(tmp_path):
    rec = wo.create_work_order(
        tmp_path, probe_id="data_quality.warnings",
        skill_id="diagnose_data_quality_warnings", mode="diagnose", created_by="t",
    )
    out = wp.generate_prompt(tmp_path, rec["work_order_id"])
    text = out.read_text()
    # observe-only / no-trade constraints
    assert "Observe-only" in text
    assert "never** execute trades" in text or "never execute trades" in text
    # forbidden actions section
    assert "Forbidden actions" in text
    assert "broker" in text.lower()
    # required tests + report path
    assert "Required tests" in text
    assert "Expected output report" in text
    # references existing contracts/runbooks
    assert "CLAUDE.md" in text
    assert "PIPELINE_RUNBOOK.md" in text
    # source artifact referenced
    assert "data_quality_report.json" in text


def test_generate_prompt_records_path_without_status_change(tmp_path):
    rec = wo.create_work_order(
        tmp_path, probe_id="pipeline.run_status",
        skill_id="diagnose_pipeline_status", mode="diagnose", created_by="t",
    )
    wp.generate_prompt(tmp_path, rec["work_order_id"])
    folded = wo.get_work_order(tmp_path, rec["work_order_id"])
    assert folded["generated_prompt_path"] is not None
    assert folded["status"] == "queued"  # unchanged — generating a prompt != executing
    assert prompt_path(tmp_path, rec["work_order_id"]).exists()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_create_list_show(tmp_path, capsys):
    rc = wo.main([
        "--root", str(tmp_path), "create",
        "--probe-id", "data_quality.warnings",
        "--skill-id", "diagnose_data_quality_warnings",
        "--mode", "diagnose", "--created-by", "enrique_cli",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Created wo_" in out

    rc = wo.main(["--root", str(tmp_path), "list", "--json"])
    assert rc == 0
    listed = json.loads(capsys.readouterr().out)
    assert len(listed) == 1
    wid = listed[0]["work_order_id"]

    rc = wo.main(["--root", str(tmp_path), "show", "--id", wid])
    assert rc == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["work_order_id"] == wid


def test_cli_create_rejects_bad_combo(tmp_path, capsys):
    rc = wo.main([
        "--root", str(tmp_path), "create",
        "--probe-id", "data_quality.warnings",
        "--skill-id", "inspect_artifact_registry",
        "--mode", "diagnose", "--created-by", "t",
    ])
    assert rc == 2
    assert "REJECTED" in capsys.readouterr().err


def test_cli_generate_prompt(tmp_path, capsys):
    rec = wo.create_work_order(
        tmp_path, probe_id="pipeline.run_status",
        skill_id="diagnose_pipeline_status", mode="diagnose", created_by="t",
    )
    rc = wo.main(["--root", str(tmp_path), "generate-prompt", "--id", rec["work_order_id"]])
    assert rc == 0
    assert "Wrote prompt" in capsys.readouterr().out
