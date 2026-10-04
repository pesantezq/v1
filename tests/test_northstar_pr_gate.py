"""Tests for the NORTHSTAR deterministic assurance gate (scripts/northstar_pr_gate.py).

Fixture-based, hermetic, deterministic. The gate's PASS/FAIL is established WITHOUT any
AI review (Codex) or human input: exact-head CI, base-main authority, a DEFAULT-DENY
change envelope, forbidden-authority checks, control-plane conformance, and remote-main
freshness. Routed to the `governance` shard via the `test_northstar` prefix.
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
        "protected": {"current_mission": MISSION, "main_sha": MAIN},
        "candidate": {
            "authorized_mission": MISSION,
            "change_class": "normal",
            "change_envelope_violations": [],
            "control_plane_authorized": False,
            "conformance_violations": [],
            "is_deterministic_transition": False,
            "transition_exact_match": False,
            "forbidden_authority_introduced": [],
            "missing_required_test_groups": [],
        },
        "now_main_sha": MAIN,
        "advisory_codex": None,
    }


def _ev(mut=None):
    i = valid_inputs()
    if mut:
        mut(i)
    return gate.evaluate_merge_gate(i)


def _blk(res):
    return set(res["blocking_reasons"])


# --------------------------------- PASS ------------------------------------- #
def test_all_gates_pass():
    res = _ev()
    assert res["decision"] == "PASS", res["blocking_reasons"]
    assert res["merge_ready"] is True
    assert res["gate"] == "NORTHSTAR_DETERMINISTIC_ASSURANCE_GATE"


def test_shadow_never_merges_even_on_pass():
    assert _ev()["would_merge"] is False


def test_enabled_would_merge_on_pass():
    assert _ev(lambda i: i.update(mode="enabled"))["would_merge"] is True


# ----------------------- Codex independence (core) -------------------------- #
def test_passes_with_no_codex_data_at_all():
    # valid_inputs contains NO Codex field whatsoever
    res = _ev()
    assert res["decision"] == "PASS"
    assert res["codex_role"] == "advisory_only"


def test_advisory_codex_findings_do_not_affect_decision():
    def mut(i):
        i["advisory_codex"] = {"unresolved_p1": 5, "note": "codex thinks this is bad"}
    res = _ev(mut)
    assert res["decision"] == "PASS"           # advisory telemetry never blocks
    assert res["advisory_codex"]["unresolved_p1"] == 5


def test_no_codex_check_name_in_gate():
    names = {ck["name"] for ck in _ev()["checks"]}
    assert not any("codex" in n.lower() for n in names)


# ------------------------------ CI binding ---------------------------------- #
def test_stale_ci_head_rejected():
    assert "ci_bound_to_head" in _blk(_ev(lambda i: i["ci"].update(head_sha=OLD)))


def test_ci_not_success_blocks():
    assert "ci_success" in _blk(_ev(lambda i: i["ci"].update(conclusion="failure")))


def test_ci_not_northstar_blocks():
    assert "ci_is_northstar_ci" in _blk(_ev(lambda i: i["ci"].update(workflow_name="x")))


# ------------------------------- PR state ----------------------------------- #
def test_draft_blocks():
    assert "pr_not_draft" in _blk(_ev(lambda i: i["pr"].update(is_draft=True)))


def test_non_main_base_blocks():
    assert "base_is_main" in _blk(_ev(lambda i: i["pr"].update(base_ref="dev")))


def test_non_mergeable_blocks():
    assert "pr_mergeable" in _blk(_ev(lambda i: i["pr"].update(mergeable=False)))


def test_closed_blocks():
    assert "pr_open" in _blk(_ev(lambda i: i["pr"].update(state="CLOSED")))


def test_mission_mismatch_blocks():
    assert "protected_mission_authorizes_pr" in _blk(
        _ev(lambda i: i["candidate"].update(authorized_mission="northstar_other")))


def test_undeclared_mission_blocks():
    res = _ev(lambda i: i["candidate"].update(authorized_mission=None))
    assert "candidate_mission_declared" in _blk(res)


# -------------------------- change envelope / class ------------------------- #
def test_change_envelope_violation_blocks():
    res = _ev(lambda i: i["candidate"].update(change_envelope_violations=["config.json"]))
    assert "change_envelope_default_deny" in _blk(res)


def test_unknown_change_class_blocks():
    assert "change_class_known" in _blk(_ev(lambda i: i["candidate"].update(change_class="weird")))


def test_missing_required_test_group_blocks():
    res = _ev(lambda i: i["candidate"].update(missing_required_test_groups=["authority"]))
    assert "required_test_groups_present" in _blk(res)


def test_forbidden_authority_blocks():
    res = _ev(lambda i: i["candidate"].update(forbidden_authority_introduced=["auto_merge"]))
    assert "no_forbidden_authority_introduced" in _blk(res)


def test_main_advanced_blocks():
    assert "main_not_advanced" in _blk(_ev(lambda i: i.update(now_main_sha="z" * 40)))


# ------------------------------ control plane ------------------------------- #
def test_control_plane_pass():
    def mut(i):
        i["candidate"].update(change_class="control_plane", control_plane_authorized=True,
                              conformance_violations=[], change_envelope_violations=[])
    assert _ev(mut)["decision"] == "PASS"


def test_control_plane_unauthorized_blocks():
    def mut(i):
        i["candidate"].update(change_class="control_plane", control_plane_authorized=False)
    assert "control_plane_authorized_by_base_main" in _blk(_ev(mut))


def test_control_plane_conformance_violation_blocks():
    def mut(i):
        i["candidate"].update(change_class="control_plane", control_plane_authorized=True,
                              conformance_violations=["wf: pull_request_target"])
    assert "control_plane_security_conformance" in _blk(_ev(mut))


# --------------------------- governance transition -------------------------- #
def test_governance_transition_exact_match_passes():
    def mut(i):
        i["candidate"].update(change_class="governance_transition", transition_exact_match=True,
                              change_envelope_violations=["ignored for this class"])
    assert _ev(mut)["decision"] == "PASS"


def test_governance_transition_without_exact_match_blocks():
    def mut(i):
        i["candidate"].update(change_class="governance_transition", transition_exact_match=False)
    assert "deterministic_transition_exact_match" in _blk(_ev(mut))


# --------------------------- evaluate_change_envelope ----------------------- #
def test_envelope_allows_listed_paths_and_prefixes():
    env = {"allowed_paths": ["portfolio_automation/data_governance.py"],
           "allowed_path_prefixes": ["portfolio_automation/vs002_evidence/", "tests/test_vs002_"]}
    changed = ["portfolio_automation/vs002_evidence/execution.py",
               "tests/test_vs002_execution.py", "portfolio_automation/data_governance.py"]
    assert gate.evaluate_change_envelope(changed, env) == []


def test_envelope_default_denies_unknown_path():
    env = {"allowed_path_prefixes": ["portfolio_automation/vs002_evidence/"]}
    out = gate.evaluate_change_envelope(["portfolio_automation/scoring.py", "config.json"], env)
    assert "portfolio_automation/scoring.py" in out and "config.json" in out


def test_envelope_absolute_forbidden_even_if_allowed():
    env = {"allowed_path_prefixes": [".agent/"]}   # tries to allow dispatch state
    out = gate.evaluate_change_envelope([".agent/project_state.yaml", "config/ew0a_runtime.json"], env)
    assert ".agent/project_state.yaml" in out and "config/ew0a_runtime.json" in out


def test_envelope_secrets_always_denied():
    env = {"allowed_path_prefixes": [""]}   # allow everything
    out = gate.evaluate_change_envelope(["deploy/credentials.json", ".env"], env)
    assert set(out) == {"deploy/credentials.json", ".env"}


def test_empty_envelope_denies_everything():
    assert gate.evaluate_change_envelope(["a.py"], {}) == ["a.py"]


# ----------------------- check_control_plane_conformance -------------------- #
def test_conformance_flags_pull_request_target_and_unpinned():
    files = {".github/workflows/x.yml": "on:\n  pull_request_target:\n    types: [opened]\njobs:\n  a:\n    steps:\n      - uses: actions/checkout@v4\n"}
    out = gate.check_control_plane_conformance(files)
    assert any("pull_request_target" in v for v in out)
    assert any("unpinned action" in v for v in out)


def test_conformance_flags_registry_not_shadow():
    files = {".agent/mission_registry.yaml": "controller:\n  mode: enabled\n"}
    out = gate.check_control_plane_conformance(files)
    assert any("controller.mode" in v for v in out)


def test_conformance_clean_when_pinned_and_shadow():
    files = {
        ".github/workflows/x.yml": "on:\n  pull_request:\n    branches: [main]\njobs:\n  a:\n    steps:\n      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683\n",
        ".agent/mission_registry.yaml": "controller:\n  mode: shadow\n",
    }
    assert gate.check_control_plane_conformance(files) == []


# ----------------------------- reconcile / assess --------------------------- #
def test_reconcile_authority_requires_agreement():
    m = "northstar_x"
    ps = {"current_step": m}
    ph = {"stockbot_northstar_redesign": {"engineer_runtime_state": {"mission_id": m},
          "phases": {"northstar_phase_0c": {"step": m}}}}
    assert asm.reconcile_authority(ps, ph, {"mission_id": m}) == m
    assert asm.reconcile_authority(ps, ph, {"mission_id": "y"}) is None
    assert asm.reconcile_authority({"current_step": None}, ph, {"mission_id": m}) is None


def test_candidate_mission_from_pr():
    assert asm.candidate_mission_from_pr("x", ["mission:northstar_a"]) == "northstar_a"
    assert asm.candidate_mission_from_pr("foo\nMISSION = northstar_b\n", []) == "northstar_b"
    assert asm.candidate_mission_from_pr("none", []) is None


def test_assess_candidate_detects_auto_flag_and_controller_edit():
    base = {"dispatch": {"s": MISSION}, "ew0a": {f: False for f in gate.AUTO_FLAGS},
            "agent_policy_digest": "a", "c1": "DISABLED", "phase_0d_status": "not_started"}
    head = copy.deepcopy(base)
    head["ew0a"]["auto_merge"] = True
    head["changed_paths"] = ["scripts/northstar_pr_gate.py"]
    out = gate.assess_candidate(base, head)
    assert "auto_merge" in out["forbidden_authority_introduced"]
    assert "scripts/northstar_pr_gate.py" in out["protected_path_violations"]
