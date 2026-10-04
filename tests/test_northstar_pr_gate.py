"""Tests for the NORTHSTAR_MERGE_GATE (scripts/northstar_pr_gate.py).

Fixture-based, hermetic, deterministic. Covers the full gate matrix: exact-head
CI binding, exact-head Codex binding (review commit_id + clean +1 reaction +
P1/P2 thread state), authority/forbidden/protected-path policy, and main-advance
invalidation. Routed to the `governance` shard via the `test_northstar` prefix.
"""
from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate = _load("northstar_pr_gate")
asm = _load("northstar_assemble_gate_inputs")

BOT = "chatgpt-codex-connector[bot]"
HEAD = "h" * 40
OLD = "o" * 40
MAIN = "m" * 40
MISSION = "northstar_continuous_mission_orchestration_foundation"


def valid_inputs() -> dict:
    return {
        "mode": "shadow",
        "pr": {"state": "OPEN", "is_draft": False, "base_ref": "main",
               "head_sha": HEAD, "mergeable": True},
        "ci": {"workflow_name": "northstar-ci", "head_sha": HEAD,
               "status": "completed", "conclusion": "success"},
        "codex": {
            "bot_login": BOT,
            "reviews": [{"author": BOT, "state": "COMMENTED", "commit_id": HEAD,
                         "submitted_at": "2026-10-04T10:00:00Z"}],
            "inline_comments": [],
            "clean_reaction": {"content": "+1", "actor": BOT,
                               "created_at": "2026-10-04T11:00:00Z"},
            "exact_head_review_request": {"sha": HEAD, "requested_at": "2026-10-04T09:00:00Z"},
        },
        "protected": {"current_mission": MISSION, "main_sha": MAIN},
        "candidate": {"authorized_mission": MISSION, "authority_mutated": False,
                      "forbidden_authority_introduced": [], "protected_path_violations": []},
        "now_main_sha": MAIN,
    }


def _ev(mut=None):
    i = valid_inputs()
    if mut:
        mut(i)
    return gate.evaluate_merge_gate(i)


def _blocking(res):
    return set(res["blocking_reasons"])


# --------------------------------- PASS ------------------------------------- #
def test_all_gates_pass():
    res = _ev()
    assert res["decision"] == "PASS", res["blocking_reasons"]
    assert res["merge_ready"] is True
    assert res["gate"] == "NORTHSTAR_MERGE_GATE"


def test_shadow_mode_never_merges_even_on_pass():
    res = _ev()
    assert res["decision"] == "PASS"
    assert res["would_merge"] is False  # shadow


def test_enabled_mode_would_merge_on_pass():
    res = _ev(lambda i: i.update(mode="enabled"))
    assert res["decision"] == "PASS"
    assert res["would_merge"] is True


# ------------------------------ CI binding ---------------------------------- #
def test_stale_ci_head_rejected():
    res = _ev(lambda i: i["ci"].update(head_sha=OLD))
    assert res["decision"] == "FAIL"
    assert "ci_bound_to_head" in _blocking(res)


def test_ci_not_success_blocks():
    res = _ev(lambda i: i["ci"].update(conclusion="failure"))
    assert "ci_success" in _blocking(res)


def test_ci_not_northstar_blocks():
    res = _ev(lambda i: i["ci"].update(workflow_name="other-ci"))
    assert "ci_is_northstar_ci" in _blocking(res)


# ---------------------------- Codex binding --------------------------------- #
def test_stale_codex_review_only_rejected():
    def mut(i):
        i["codex"]["reviews"] = [{"author": BOT, "state": "COMMENTED", "commit_id": OLD,
                                  "submitted_at": "2026-10-03T10:00:00Z"}]
        i["codex"]["exact_head_review_request"] = None  # no fresh exact-head anchor
    res = _ev(mut)
    assert res["decision"] == "FAIL"
    assert "codex_clean_at_head" in _blocking(res)


def test_old_reaction_after_head_change_rejected():
    # +1 predates the exact-head request/review anchor -> stale
    res = _ev(lambda i: i["codex"]["clean_reaction"].update(created_at="2026-10-04T08:00:00Z"))
    assert "codex_clean_at_head" in _blocking(res)


def test_exact_head_clean_reaction_accepted_without_review():
    # scenario B: no review object at all, just an exact-head request + later +1
    def mut(i):
        i["codex"]["reviews"] = []
        i["codex"]["exact_head_review_request"] = {"sha": HEAD, "requested_at": "2026-10-04T09:00:00Z"}
        i["codex"]["clean_reaction"] = {"content": "+1", "actor": BOT, "created_at": "2026-10-04T09:30:00Z"}
    res = _ev(mut)
    assert res["decision"] == "PASS", res["blocking_reasons"]


def test_clean_reaction_with_wrong_request_sha_rejected():
    def mut(i):
        i["codex"]["reviews"] = []
        i["codex"]["exact_head_review_request"] = {"sha": OLD, "requested_at": "2026-10-04T09:00:00Z"}
    res = _ev(mut)
    assert "codex_clean_at_head" in _blocking(res)


def test_no_reaction_means_not_clean_silence():
    res = _ev(lambda i: i["codex"].update(clean_reaction=None))
    assert "codex_clean_at_head" in _blocking(res)


def test_unresolved_p1_blocks():
    def mut(i):
        i["codex"]["inline_comments"] = [{"author": BOT, "severity": "P1", "commit_id": HEAD,
                                          "resolved": False, "created_at": "2026-10-04T10:30:00Z"}]
    res = _ev(mut)
    assert "codex_clean_at_head" in _blocking(res)


def test_unresolved_p2_blocks():
    def mut(i):
        i["codex"]["inline_comments"] = [{"author": BOT, "severity": "P2", "commit_id": HEAD,
                                          "resolved": False, "created_at": "2026-10-04T10:30:00Z"}]
    res = _ev(mut)
    assert "codex_clean_at_head" in _blocking(res)


def test_resolved_old_thread_does_not_block():
    def mut(i):
        i["codex"]["inline_comments"] = [{"author": BOT, "severity": "P1", "commit_id": OLD,
                                          "resolved": True, "created_at": "2026-10-03T10:30:00Z"}]
    res = _ev(mut)
    assert res["decision"] == "PASS", res["blocking_reasons"]


def test_newer_finding_after_reaction_blocks():
    def mut(i):
        i["codex"]["inline_comments"] = [{"author": BOT, "severity": "P1", "commit_id": HEAD,
                                          "resolved": False, "created_at": "2026-10-04T12:00:00Z"}]
    res = _ev(mut)
    assert "codex_clean_at_head" in _blocking(res)


# ------------------------------ PR state ------------------------------------ #
def test_draft_pr_blocks():
    assert "pr_not_draft" in _blocking(_ev(lambda i: i["pr"].update(is_draft=True)))


def test_non_main_base_blocks():
    assert "base_is_main" in _blocking(_ev(lambda i: i["pr"].update(base_ref="develop")))


def test_non_mergeable_pr_blocks():
    assert "pr_mergeable" in _blocking(_ev(lambda i: i["pr"].update(mergeable=False)))


def test_closed_pr_blocks():
    assert "pr_open" in _blocking(_ev(lambda i: i["pr"].update(state="CLOSED")))


# --------------------------- authority / policy ----------------------------- #
def test_protected_mission_mismatch_blocks():
    res = _ev(lambda i: i["candidate"].update(authorized_mission="northstar_vs002_frozen_execution"))
    assert "protected_mission_authorizes_pr" in _blocking(res)


def test_unauthorized_protected_state_modification_blocks():
    assert "no_authority_mutation" in _blocking(_ev(lambda i: i["candidate"].update(authority_mutated=True)))


def test_forbidden_authority_widening_blocks():
    res = _ev(lambda i: i["candidate"].update(forbidden_authority_introduced=["auto_merge"]))
    assert "no_forbidden_authority_introduced" in _blocking(res)


def test_protected_path_violation_blocks():
    res = _ev(lambda i: i["candidate"].update(protected_path_violations=["config/agent_policy.yaml"]))
    assert "protected_path_policy_ok" in _blocking(res)


def test_main_advancing_during_eval_blocks():
    assert "main_not_advanced" in _blocking(_ev(lambda i: i.update(now_main_sha="z" * 40)))


# --------------------------- assess_candidate ------------------------------- #
def _bh():
    base = {"dispatch": {"current_step": MISSION}, "ew0a": {f: False for f in gate.AUTO_FLAGS},
            "agent_policy_digest": "abc", "c1": "DISABLED", "phase_0d_status": "not_started"}
    head = copy.deepcopy(base)
    return base, head


def test_assess_candidate_clean():
    base, head = _bh()
    # ordinary, non-protected changes
    head["changed_paths"] = ["portfolio_automation/some_feature.py", "docs/NORTHSTAR_ORCHESTRATION.md"]
    out = gate.assess_candidate(base, head)
    assert out == {"authority_mutated": False, "forbidden_authority_introduced": [],
                   "protected_path_violations": []}


def test_assess_candidate_flags_controller_self_modification():
    base, head = _bh()
    head["changed_paths"] = ["scripts/northstar_pr_gate.py"]   # modifying the controller itself
    out = gate.assess_candidate(base, head)
    assert "scripts/northstar_pr_gate.py" in out["protected_path_violations"]


def test_assess_candidate_detects_authority_mutation():
    base, head = _bh()
    head["dispatch"]["current_step"] = "northstar_vs002_execution_adapter_foundation"
    assert gate.assess_candidate(base, head)["authority_mutated"] is True


def test_assess_candidate_detects_auto_flag_flip():
    base, head = _bh()
    head["ew0a"]["auto_merge"] = True
    assert "auto_merge" in gate.assess_candidate(base, head)["forbidden_authority_introduced"]


def test_assess_candidate_detects_c1_and_0d():
    base, head = _bh()
    head["c1"] = "ENABLED"
    head["phase_0d_status"] = "active"
    out = gate.assess_candidate(base, head)
    assert "c1_enabled" in out["forbidden_authority_introduced"]
    assert "phase_0d_advanced" in out["forbidden_authority_introduced"]


def test_assess_candidate_protected_path_violation_vs_allowlist():
    base, head = _bh()
    head["changed_paths"] = [".agent/project_state.yaml", ".agent/mission_registry.yaml"]
    # project_state.yaml is a protected edit; mission_registry.yaml is an allowlisted new file
    out = gate.assess_candidate(base, head, allowlisted_new_paths=[".agent/mission_registry.yaml"])
    assert ".agent/project_state.yaml" in out["protected_path_violations"]
    assert ".agent/mission_registry.yaml" not in out["protected_path_violations"]


# --------------- assembler pure helpers (Codex-hardening fixes) -------------- #
def test_candidate_mission_from_label():
    assert asm.candidate_mission_from_pr("body", ["mission:northstar_x"]) == "northstar_x"
    assert asm.candidate_mission_from_pr("body", ["mission/northstar_z"]) == "northstar_z"


def test_candidate_mission_from_body_line():
    assert asm.candidate_mission_from_pr("intro\nMISSION = northstar_y\ntail", []) == "northstar_y"


def test_candidate_mission_none_when_undeclared():
    # a PR that declares no mission cannot be tautologically authorized
    assert asm.candidate_mission_from_pr("no declaration here", []) is None
    assert asm.candidate_mission_from_pr(None, None) is None


def test_undeclared_candidate_mission_fails_gate():
    # when the assembler yields None (PR declares no mission), the gate must block
    res = _ev(lambda i: i["candidate"].update(authorized_mission=None))
    assert res["decision"] == "FAIL"
    assert "protected_mission_authorizes_pr" in _blocking(res)


def test_added_only_allowlist_permits_added_but_flags_modified():
    isp = lambda p: p.startswith((".agent/", "config/agent_policy"))
    changed = [{"path": ".agent/mission_registry.yaml", "status": "modified"},
               {"path": "scripts/northstar_pr_gate.py", "status": "added"},
               {"path": "config/agent_policy.yaml", "status": "modified"}]
    allow = {".agent/mission_registry.yaml", "scripts/northstar_pr_gate.py"}
    out = asm.added_only_protected_violations(changed, allow, isp)
    assert ".agent/mission_registry.yaml" in out   # MODIFYING an allowlisted file still violates
    assert "scripts/northstar_pr_gate.py" not in out  # scripts/ not protected by isp here
    assert "config/agent_policy.yaml" in out


def test_controller_self_protection_blocks_modifying_its_own_workflow():
    # a candidate that MODIFIES the controller's own workflow/script/registry must
    # be a protected-path violation (cannot self-grant write authority)
    isp = asm._is_protected
    changed = [{"path": ".github/workflows/northstar-pr-controller.yml", "status": "modified"},
               {"path": "scripts/northstar_pr_gate.py", "status": "modified"},
               {"path": ".agent/mission_registry.yaml", "status": "modified"}]
    allow = asm.ALLOWLISTED_ADDED_PATHS
    out = asm.added_only_protected_violations(changed, allow, isp)
    assert ".github/workflows/northstar-pr-controller.yml" in out
    assert "scripts/northstar_pr_gate.py" in out
    assert ".agent/mission_registry.yaml" in out


def test_controller_self_protection_permits_bootstrap_add():
    isp = asm._is_protected
    changed = [{"path": ".github/workflows/northstar-pr-controller.yml", "status": "added"},
               {"path": "scripts/northstar_pr_gate.py", "status": "added"}]
    out = asm.added_only_protected_violations(changed, asm.ALLOWLISTED_ADDED_PATHS, isp)
    assert out == []   # the bootstrap PR that ADDs the controller is allowed


def test_gate_is_protected_flags_controller_and_workflows():
    assert gate._is_protected(".github/workflows/northstar-orchestrator.yml")
    assert gate._is_protected("scripts/northstar_materialize_transition.py")
    assert gate._is_protected(".agent/missions/x.md")


def test_material_unresolved_from_threads_reads_real_resolution():
    head = "h" * 40
    threads = [
        {"isResolved": False, "comments": [{"author": BOT, "body": "![P1 Badge] x", "commit_id": head}]},
        {"isResolved": True, "comments": [{"author": BOT, "body": "![P1 Badge] y", "commit_id": head}]},   # resolved -> excluded
        {"isResolved": False, "comments": [{"author": BOT, "body": "nit, non-material", "commit_id": head}]},
        {"isResolved": False, "comments": [{"author": BOT, "body": "![P2 Badge] z", "commit_id": "o" * 40}]},  # wrong head
        {"isResolved": False, "comments": [{"author": "someone", "body": "![P1 Badge]", "commit_id": head}]},  # not the bot
    ]
    out = asm.material_unresolved_from_threads(threads, head, BOT)
    assert len(out) == 1 and out[0]["severity"] == "P1"
