"""Tests for the deterministic transition proposer (scripts/northstar_transition.py).

Covers preauthorized_auto proposal emission (dispatch-field allowlist + EXACT
paused-authorization restore), and every STOP path (human_required, E4,
ambiguous/missing/uncertified state). Also parses the real mission registry and
the real preserved paused authorization. Routed to `governance` via prefix.
"""
from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
REGISTRY_FILE = REPO_ROOT / ".agent" / "mission_registry.yaml"
PHASE_FILE = REPO_ROOT / ".agent" / "phase_status.yaml"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


trans = _load("northstar_transition")
mat = _load("northstar_materialize_transition")

ORCH = "northstar_continuous_mission_orchestration_foundation"
ADAPTER = "northstar_vs002_execution_adapter_foundation"
FROZEN = "northstar_vs002_frozen_execution"
MAIN = "m" * 40

PAUSED_AUTH = {
    "authorized_by": "operator",
    "authorized_at": "2026-10-02",
    "authorized_mission": ADAPTER,
    "status": "PAUSED_NOT_EXECUTED",
    "scope": "Implement and certify the THIN VS-002 execution adapter ...",
    "resume_condition": "Resume only after orchestration is COMPLETE and durable ...",
    "not_yet_executed": True,
}


def registry() -> dict:
    return {
        "schema_version": "northstar.mission_registry.v1",
        "controller": {"mode": "shadow"},
        "auto_dispatch_allowed_risk_classes": ["E1", "E2", "E3"],
        "human_required_risk_classes": ["E4"],
        "missions": {
            ORCH: {"executor": "claude", "risk_class": "E3", "auto_dispatch": False,
                   "transition_policy": "preauthorized_auto",
                   "on_success": {"next_mission": ADAPTER, "restore_paused_authorization": True},
                   "human_boundary_required": False},
            ADAPTER: {"executor": "claude", "risk_class": "E3", "auto_dispatch": True,
                      "transition_policy": "human_required",
                      "on_success": {"next_mission": FROZEN, "restore_paused_authorization": False},
                      "human_boundary_required": False},
            FROZEN: {"executor": "claude", "risk_class": "E4", "auto_dispatch": False,
                     "transition_policy": "human_required",
                     "on_success": {"next_mission": None}, "human_boundary_required": True},
        },
    }


def valid_inputs() -> dict:
    return {
        "mode": "shadow",
        "completed_mission": ORCH,
        "authoritative_mission": ORCH,
        "completed_pr_mission": ORCH,   # the certified SHA merged THIS mission's PR
        "registry": registry(),
        "paused_authorization": copy.deepcopy(PAUSED_AUTH),
        "certified_main_sha": MAIN,
        "protected_main_sha": MAIN,
        "post_merge": {"conclusion": "success", "event": "push", "head_branch": "main", "head_sha": MAIN},
    }


def _ev(mut=None):
    i = valid_inputs()
    if mut:
        mut(i)
    return trans.propose_transition(i)


# ------------------------------- PROPOSE ------------------------------------ #
def test_preauthorized_edge_emits_proposal():
    res = _ev()
    assert res["decision"] == "PROPOSE", res
    assert res["next_mission"] == ADAPTER
    edits = res["proposal"]["allowlisted_field_edits"]
    assert edits[".agent/project_state.yaml#current_step"] == ADAPTER
    assert edits["config/ew0a_runtime.json#mission_id"] == ADAPTER


def test_proposal_is_governance_pr_not_direct_mutation():
    res = _ev()
    assert res["proposal"]["proposal_type"] == "governance_transition_pr"
    assert any("no direct push to main" in f for f in res["proposal"]["forbidden"])
    assert res["would_open_governance_pr"] is False  # shadow


def test_paused_authorization_restored_exactly():
    res = _ev()
    assert res["proposal"]["restore_bounded_authorization"] == PAUSED_AUTH  # verbatim, not widened


def test_enabled_mode_would_open_governance_pr():
    res = _ev(lambda i: i.update(mode="enabled"))
    assert res["decision"] == "PROPOSE"
    assert res["would_open_governance_pr"] is True


# --------------------------------- STOP ------------------------------------- #
def test_human_required_transition_stops():
    # completed == adapter (its transition_policy is human_required)
    def mut(i):
        i["completed_mission"] = ADAPTER
        i["authoritative_mission"] = ADAPTER
        i["completed_pr_mission"] = ADAPTER
    res = _ev(mut)
    assert res["decision"] == "STOP"
    assert res["reason"] == "human_required"
    assert res["proposal"] is None


def test_ambiguous_completed_vs_authoritative_stops():
    res = _ev(lambda i: i.update(authoritative_mission=ADAPTER))
    assert res["decision"] == "STOP"
    assert res["reason"] == "completed_mission_not_authoritative"


def test_certified_sha_must_have_merged_the_mission_pr():
    # an unrelated certified main push (no mission PR / different mission) must STOP
    res = _ev(lambda i: i.update(completed_pr_mission=None))
    assert res["decision"] == "STOP"
    assert res["reason"] == "certified_sha_did_not_merge_this_mission"
    res2 = _ev(lambda i: i.update(completed_pr_mission="northstar_unrelated"))
    assert res2["reason"] == "certified_sha_did_not_merge_this_mission"


def test_missing_registry_entry_stops():
    res = _ev(lambda i: i.update(completed_mission="northstar_unknown",
                                 authoritative_mission="northstar_unknown",
                                 completed_pr_mission="northstar_unknown"))
    assert res["reason"] == "missing_registry_entry"


def test_post_merge_not_certified_stops():
    res = _ev(lambda i: i["post_merge"].update(event="pull_request"))
    assert res["reason"] == "post_merge_not_certified"


def test_uncertified_sha_mismatch_stops():
    res = _ev(lambda i: i.update(protected_main_sha="z" * 40))
    assert res["reason"] == "post_merge_not_certified"


def test_post_merge_failed_ci_stops():
    res = _ev(lambda i: i["post_merge"].update(conclusion="failure"))
    assert res["reason"] == "post_merge_not_certified"


def test_post_merge_cancelled_ci_stops():
    res = _ev(lambda i: i["post_merge"].update(conclusion="cancelled"))
    assert res["reason"] == "post_merge_not_certified"


def test_post_merge_head_sha_mismatch_stops():
    # a green main run that no longer matches the current certified sha (main advanced)
    res = _ev(lambda i: i["post_merge"].update(head_sha="q" * 40))
    assert res["reason"] == "post_merge_not_certified"


def test_post_merge_non_main_branch_stops():
    res = _ev(lambda i: i["post_merge"].update(head_branch="feature/x"))
    assert res["reason"] == "post_merge_not_certified"


def test_next_mission_e4_stops():
    def mut(i):
        i["registry"]["missions"][ORCH]["on_success"]["next_mission"] = FROZEN
        i["registry"]["missions"][ORCH]["on_success"]["restore_paused_authorization"] = False
    res = _ev(mut)
    assert res["decision"] == "STOP"
    assert res["reason"] in ("next_mission_risk_class_requires_human", "next_mission_requires_human_boundary")


def test_next_mission_not_in_registry_stops():
    def mut(i):
        i["registry"]["missions"][ORCH]["on_success"]["next_mission"] = "northstar_ghost"
    res = _ev(mut)
    assert res["reason"] == "next_mission_not_in_registry"


def test_paused_authorization_missing_stops():
    res = _ev(lambda i: i.update(paused_authorization=None))
    assert res["reason"] == "paused_authorization_missing"


def test_paused_authorization_mission_mismatch_stops():
    def mut(i):
        i["paused_authorization"]["authorized_mission"] = "northstar_other"
    res = _ev(mut)
    assert res["reason"] == "paused_authorization_mission_mismatch"


def test_unknown_policy_stops():
    def mut(i):
        i["registry"]["missions"][ORCH]["transition_policy"] = "auto_merge_everything"
    res = _ev(mut)
    assert res["reason"] == "unknown_or_unsupported_transition_policy"


# ------------------------- real protected artifacts ------------------------- #
def _yaml():
    import pytest
    try:
        import yaml
    except ImportError:
        pytest.skip("pyyaml not installed")
    return yaml


def test_real_mission_registry_parses_and_is_shadow():
    yaml = _yaml()
    reg = yaml.safe_load(REGISTRY_FILE.read_text(encoding="utf-8"))
    assert reg["controller"]["mode"] == "shadow"  # bootstrap ships shadow; cannot self-enable
    m = reg["missions"]
    assert m[ORCH]["transition_policy"] == "preauthorized_auto"
    assert m[ORCH]["on_success"]["next_mission"] == ADAPTER
    assert m[ORCH]["on_success"]["restore_paused_authorization"] is True
    assert m[ORCH]["auto_dispatch"] is False          # bootstrap mission never self-dispatches
    assert m[ADAPTER]["transition_policy"] == "human_required"
    assert m[FROZEN]["risk_class"] == "E4"
    assert m[FROZEN]["auto_dispatch"] is False
    assert "E4" not in reg["auto_dispatch_allowed_risk_classes"]


# ------------------------- transition MATERIALIZER -------------------------- #
import textwrap  # noqa: E402

PAUSED_FIXTURE = {
    "authorized_by": "operator",
    "authorized_mission": ADAPTER,
    "status": "PAUSED_NOT_EXECUTED",
    "not_yet_executed": True,
}

PROJECT_STATE_FIX = textwrap.dedent(f"""\
    current_step: {ORCH}  # authorized 2026-10-04
    next_official_step:
      primary: {ORCH}  # current bounded mission
      secondary: []
      prior_primary: {ADAPTER}  # paused, preserved
""")

PHASE_STATUS_FIX = textwrap.dedent(f"""\
    stockbot_northstar_redesign:
      engineer_runtime_state:
        mission_id: {ORCH}  # dispatch pointer
        c1: DISABLED
      phases:
        northstar_phase_0c:
          status: active
          step: {ORCH}  # current bounded step
          bounded_authorization:
            authorized_by: operator
            authorized_mission: {ORCH}  # active authorization
            scope: >-
              Build the orchestration foundation (do not reconstruct me).
            paused_bounded_authorization:
              authorized_by: operator
              authorized_mission: {ADAPTER}
              status: PAUSED_NOT_EXECUTED
              not_yet_executed: true
            prior_bounded_authorization:
              authorized_mission: northstar_vs002_result_runner
              status: COMPLETE
""")

EW0A_FIX = '{\n  "mission_id": "%s",\n  "c1": "DISABLED"\n}\n' % ORCH


def _proposal():
    return {
        "completed_mission": ORCH,
        "next_mission": ADAPTER,
        "certified_main_sha": "s" * 40,
        "allowlisted_field_edits": {
            ".agent/project_state.yaml#current_step": ADAPTER,
            ".agent/project_state.yaml#next_official_step.primary": ADAPTER,
            ".agent/project_state.yaml#next_official_step.prior_primary": ORCH,
            ".agent/phase_status.yaml#stockbot_northstar_redesign.engineer_runtime_state.mission_id": ADAPTER,
            ".agent/phase_status.yaml#stockbot_northstar_redesign.phases.northstar_phase_0c.step": ADAPTER,
            "config/ew0a_runtime.json#mission_id": ADAPTER,
        },
        "restore_bounded_authorization": dict(PAUSED_FIXTURE),
    }


def _files():
    return {".agent/project_state.yaml": PROJECT_STATE_FIX,
            ".agent/phase_status.yaml": PHASE_STATUS_FIX,
            "config/ew0a_runtime.json": EW0A_FIX}


def test_materialize_changes_only_allowlisted_fields():
    import yaml
    out = mat.materialize_edits(_files(), _proposal())
    ps = yaml.safe_load(out["files"][".agent/project_state.yaml"])
    ph = yaml.safe_load(out["files"][".agent/phase_status.yaml"])
    ew = __import__("json").loads(out["files"]["config/ew0a_runtime.json"])
    assert ps["current_step"] == ADAPTER
    assert ps["next_official_step"]["primary"] == ADAPTER
    assert ps["next_official_step"]["prior_primary"] == ORCH
    assert ps["next_official_step"]["secondary"] == []        # untouched
    rd = ph["stockbot_northstar_redesign"]
    assert rd["engineer_runtime_state"]["mission_id"] == ADAPTER
    assert rd["engineer_runtime_state"]["c1"] == "DISABLED"   # untouched
    ba = rd["phases"]["northstar_phase_0c"]["bounded_authorization"]
    assert rd["phases"]["northstar_phase_0c"]["step"] == ADAPTER
    assert ba["authorized_mission"] == ADAPTER                # active repointed
    assert ew["mission_id"] == ADAPTER and ew["c1"] == "DISABLED"


def test_materialize_restores_paused_authorization_exactly():
    out = mat.materialize_edits(_files(), _proposal())
    assert mat._paused_object(out["files"][".agent/phase_status.yaml"]) == PAUSED_FIXTURE


def test_materialize_preserves_comments_and_scope():
    out = mat.materialize_edits(_files(), _proposal())
    ph_text = out["files"][".agent/phase_status.yaml"]
    assert "# dispatch pointer" in ph_text          # inline comment preserved
    assert "do not reconstruct me" in ph_text       # active scope text untouched
    ps_text = out["files"][".agent/project_state.yaml"]
    assert "# authorized 2026-10-04" in ps_text


def test_materialize_refuses_mismatched_paused_object():
    p = _proposal()
    p["restore_bounded_authorization"] = {"authorized_mission": "northstar_other"}
    with pytest.raises(mat.MaterializeError):
        mat.materialize_edits(_files(), p)


def test_materialize_refuses_missing_anchor():
    p = _proposal()
    p["allowlisted_field_edits"][".agent/project_state.yaml#does_not_exist"] = "x"
    with pytest.raises(mat.MaterializeError):
        mat.materialize_edits(_files(), p)


def test_materialize_is_idempotent():
    out1 = mat.materialize_edits(_files(), _proposal())
    # feed the already-materialized files back in; nothing should change
    out2 = mat.materialize_edits(out1["files"], _proposal())
    assert out2["changed"] == []


def test_materialize_does_not_touch_prior_or_paused_authorized_mission():
    import yaml
    out = mat.materialize_edits(_files(), _proposal())
    ph = yaml.safe_load(out["files"][".agent/phase_status.yaml"])
    ba = ph["stockbot_northstar_redesign"]["phases"]["northstar_phase_0c"]["bounded_authorization"]
    assert ba["paused_bounded_authorization"]["authorized_mission"] == ADAPTER  # unchanged (already adapter)
    assert ba["prior_bounded_authorization"]["authorized_mission"] == "northstar_vs002_result_runner"


def test_branch_name_is_stable_and_deterministic():
    b1 = mat.branch_name(ORCH, "s" * 40)
    b2 = mat.branch_name(ORCH, "s" * 40)
    assert b1 == b2
    assert b1.startswith("governance/transition-") and ("s" * 12) in b1


def test_materialize_emits_no_git_side_effects():
    # materialize_edits returns text only; it performs no git operations / no main push
    out = mat.materialize_edits(_files(), _proposal())
    assert set(out.keys()) == {"files", "changed", "verified"}
    assert out["verified"] is True


def test_real_paused_authorization_preserved_unchanged():
    # PR #63 / the adapter remain PAUSED, not executed, during this mission.
    yaml = _yaml()
    phase = yaml.safe_load(PHASE_FILE.read_text(encoding="utf-8"))
    ba = phase["stockbot_northstar_redesign"]["phases"]["northstar_phase_0c"]["bounded_authorization"]
    pba = ba["paused_bounded_authorization"]
    assert pba["authorized_mission"] == ADAPTER
    assert pba["status"] == "PAUSED_NOT_EXECUTED"
    assert pba["not_yet_executed"] is True
    # and the active mission is still the orchestration foundation (not the adapter)
    assert ba["authorized_mission"] == ORCH
