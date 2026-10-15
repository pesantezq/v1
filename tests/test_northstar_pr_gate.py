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
    files = {".github/workflows/x.yml": "on:\n  pull_request_target:\n    types: [opened]\njobs:\n  a:\n    steps:\n      - uses: actions/checkout@v4\n",
             ".agent/mission_registry.yaml": "controller:\n  mode: shadow\n"}
    out = gate.check_control_plane_conformance(files, "shadow")
    assert any("pull_request_target" in v for v in out)
    assert any("unpinned action" in v for v in out)


def _reg(mode: str) -> dict:
    return {".agent/mission_registry.yaml": f"controller:\n  mode: {mode}\n"}


def test_conformance_mode_shadow_to_shadow_allowed():
    assert gate.check_control_plane_conformance(_reg("shadow"), "shadow") == []


def test_conformance_mode_shadow_to_enabled_rejected():
    out = gate.check_control_plane_conformance(_reg("enabled"), "shadow")
    assert any("cannot change controller rollout mode" in v for v in out)


def test_conformance_mode_enabled_to_enabled_allowed():
    assert gate.check_control_plane_conformance(_reg("enabled"), "enabled") == []


def test_conformance_mode_enabled_to_shadow_rejected():
    out = gate.check_control_plane_conformance(_reg("shadow"), "enabled")
    assert any("cannot change controller rollout mode" in v for v in out)


def test_conformance_missing_base_mode_rejected():
    out = gate.check_control_plane_conformance(_reg("shadow"), None)
    assert any("base-main controller.mode missing/unknown" in v for v in out)


def test_conformance_unknown_base_mode_rejected():
    out = gate.check_control_plane_conformance(_reg("shadow"), "banana")
    assert any("base-main controller.mode missing/unknown" in v for v in out)


def test_conformance_missing_candidate_registry_rejected():
    out = gate.check_control_plane_conformance({}, "shadow")
    assert any("candidate mission_registry.yaml missing" in v for v in out)


def test_conformance_unparseable_candidate_registry_rejected():
    out = gate.check_control_plane_conformance(
        {".agent/mission_registry.yaml": "controller:\n  mode: [a, b"}, "shadow")
    assert any("unparseable" in v for v in out)


def test_conformance_candidate_mode_absent_rejected():
    out = gate.check_control_plane_conformance({".agent/mission_registry.yaml": "other: 1\n"}, "shadow")
    assert any("controller.mode missing/unknown" in v for v in out)


def test_conformance_clean_when_pinned_and_mode_matches_base():
    files = {
        ".github/workflows/x.yml": "on:\n  pull_request:\n    branches: [main]\njobs:\n  a:\n    steps:\n      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683\n",
        ".agent/mission_registry.yaml": "controller:\n  mode: shadow\n",
    }
    assert gate.check_control_plane_conformance(files, "shadow") == []
    # Post-activation rotation: base enabled + candidate enabled is also clean.
    files_enabled = dict(files)
    files_enabled[".agent/mission_registry.yaml"] = "controller:\n  mode: enabled\n"
    assert gate.check_control_plane_conformance(files_enabled, "enabled") == []


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


# ------------------- real protected-state authority reconciliation ---------- #
def test_real_protected_state_reconciles_to_orchestration_mission():
    # Real-file proof: all protected sources + the roadmap guard agree, so the gate's
    # protected.current_mission resolves (NOT None) to the orchestration mission.
    import yaml
    import json as _json
    ps = yaml.safe_load((REPO_ROOT / ".agent/project_state.yaml").read_text(encoding="utf-8"))
    ph = yaml.safe_load((REPO_ROOT / ".agent/phase_status.yaml").read_text(encoding="utf-8"))
    ew = _json.loads((REPO_ROOT / "config/ew0a_runtime.json").read_text(encoding="utf-8"))
    assert asm.reconcile_authority(ps, ph, ew) == MISSION
    from portfolio_automation.engineer_worker.roadmap_guard import RoadmapAuthorization
    auth = RoadmapAuthorization.read(REPO_ROOT)
    assert auth.authoritative is True
    assert auth.authorized_mission_id == MISSION
    assert asm._reconciled_authoritative() == MISSION
    diag = asm._authority_diagnostics()
    assert diag["agree"] is True and diag["reason"] is None
    assert diag["reconciled"] == MISSION and diag["guard"] == MISSION


def test_reconciled_authoritative_resolves_as_standalone_script(tmp_path):
    # Regression for the live PR#70 gate None: as a standalone script the repo root is
    # NOT on sys.path, so a top-level `portfolio_automation` import would fail and the
    # reconciler would (silently) return None. The fix self-heals the path. Runs the
    # assembler from a file with the repo root OFF sys.path[0] to reproduce the gate env.
    import subprocess
    import sys as _sys
    import textwrap
    code = textwrap.dedent(
        """
        import importlib.util
        from pathlib import Path
        root = Path(__ROOT__)
        spec = importlib.util.spec_from_file_location(
            "asm_standalone", root / "scripts" / "northstar_assemble_gate_inputs.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        print(m._reconciled_authoritative() or "NONE")
        """
    ).replace("__ROOT__", repr(str(REPO_ROOT)))
    script = tmp_path / "standalone_gate.py"
    script.write_text(code, encoding="utf-8")
    out = subprocess.run([_sys.executable, str(script)], cwd=str(REPO_ROOT),
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == MISSION, (out.stdout, out.stderr)


# -------------------- registry authority-narrowing (P1) --------------------- #
def _base_reg() -> dict:
    return {
        "schema_version": "northstar.mission_registry.v1",
        "controller": {"mode": "shadow", "level": "C0.5_SHADOW", "enabled_by": None, "enabled_at": None},
        "auto_dispatch_allowed_risk_classes": ["E1", "E2", "E3"],
        "human_required_risk_classes": ["E4"],
        "missions": {
            "m_orch": {
                "executor": "claude", "risk_class": "E3", "prompt_source": ".agent/missions/orch.md",
                "auto_dispatch": False, "completion_gate": "durable_on_main_with_post_merge_ci_success",
                "allowed_authority": ["impl", "shadow_emit"],
                "forbidden_authority": ["self_merge", "self_auth"],
                "transition_policy": "preauthorized_auto",
                "on_success": {"next_mission": "m_adapter", "restore_paused_authorization": True,
                               "restore_source": ".agent/phase_status.yaml#x"},
                "human_boundary_required": False,
                "change_policy": {"class": "control_plane",
                                  "allowed_path_prefixes": ["scripts/northstar_", "tests/test_northstar_"],
                                  "allowed_paths": [], "forbidden_path_prefixes": [],
                                  "required_test_groups": ["orchestration", "authority"],
                                  "semantic_guards": ["no_forbidden_authority", "control_plane_conformance"]},
            },
            "m_adapter": {
                "executor": "claude", "risk_class": "E3", "prompt_source": ".agent/missions/adapter.md",
                "auto_dispatch": True, "completion_gate": "durable_on_main_with_post_merge_ci_success",
                "allowed_authority": ["thin_adapter"], "forbidden_authority": ["real_pkg", "self_auth"],
                "transition_policy": "human_required",
                "on_success": {"next_mission": "m_frozen", "restore_paused_authorization": False},
                "human_boundary_required": False,
                "change_policy": {"class": "normal", "allowed_path_prefixes": ["portfolio_automation/vs002_evidence/"],
                                  "allowed_paths": ["portfolio_automation/data_governance.py"],
                                  "forbidden_path_prefixes": [], "required_test_groups": ["vs002"],
                                  "semantic_guards": ["no_forbidden_authority"]},
            },
            "m_frozen": {
                "executor": "claude", "risk_class": "E4", "prompt_source": None,
                "auto_dispatch": False, "completion_gate": "unauthorized",
                "allowed_authority": [], "forbidden_authority": ["real_pkg", "all_until_authorized"],
                "transition_policy": "human_required",
                "on_success": {"next_mission": None, "restore_paused_authorization": False},
                "human_boundary_required": True,
                "change_policy": {"class": "normal", "allowed_path_prefixes": [], "allowed_paths": [],
                                  "forbidden_path_prefixes": [], "required_test_groups": [],
                                  "semantic_guards": ["no_forbidden_authority"]},
            },
        },
    }


def _narrow(mutate):
    import copy
    base = _base_reg(); cand = copy.deepcopy(base); mutate(cand)
    return gate.check_registry_authority_narrowing(base, cand)


def test_registry_narrowing_identical_ok():
    assert _narrow(lambda c: None) == []


import pytest as _pytest


@_pytest.mark.parametrize("mutate, needle", [
    (lambda c: c["missions"]["m_orch"].__setitem__("auto_dispatch", True), "auto_dispatch"),
    (lambda c: c["missions"]["m_orch"]["allowed_authority"].append("new_power"), "allowed_authority"),
    (lambda c: c["missions"]["m_orch"]["forbidden_authority"].remove("self_merge"), "forbidden_authority"),
    (lambda c: c["missions"]["m_frozen"].__setitem__("human_boundary_required", False), "human_boundary_required"),
    (lambda c: c["missions"]["m_adapter"].__setitem__("transition_policy", "preauthorized_auto"), "transition_policy"),
    (lambda c: c["auto_dispatch_allowed_risk_classes"].append("E4"), "auto_dispatch_allowed_risk_classes"),
    (lambda c: c.__setitem__("human_required_risk_classes", []), "human_required_risk_classes"),
    (lambda c: c["missions"]["m_orch"]["change_policy"]["allowed_path_prefixes"].append("config/"), "allowed_path_prefixes"),
    (lambda c: c["missions"]["m_orch"]["change_policy"]["allowed_paths"].append("config/x.py"), "allowed_paths"),
    (lambda c: c["missions"]["m_orch"]["change_policy"]["required_test_groups"].remove("authority"), "required_test_groups"),
    (lambda c: c["missions"]["m_orch"]["change_policy"]["semantic_guards"].remove("no_forbidden_authority"), "semantic_guards"),
    (lambda c: c["missions"].__setitem__("m_new", dict(c["missions"]["m_orch"])), "missions added"),
    (lambda c: c["controller"].__setitem__("level", "C1"), "controller.level"),
    (lambda c: c["controller"].__setitem__("mode", "enabled"), "controller.mode"),
    (lambda c: c["missions"]["m_orch"]["on_success"].__setitem__("next_mission", "m_other"), "next_mission"),
])
def test_registry_narrowing_rejects_widening(mutate, needle):
    out = _narrow(mutate)
    assert any(needle in x for x in out), out


def test_registry_narrowing_malformed_candidate_fails_closed():
    assert any("candidate registry unparseable" in x for x in gate.check_registry_authority_narrowing(_base_reg(), None))
    assert any("candidate registry unparseable" in x for x in gate.check_registry_authority_narrowing(_base_reg(), "nope"))


def test_registry_narrowing_malformed_base_fails_closed():
    assert any("base-main registry unparseable" in x for x in gate.check_registry_authority_narrowing(None, _base_reg()))
    assert any("base-main registry unparseable" in x for x in gate.check_registry_authority_narrowing("nope", _base_reg()))


@_pytest.mark.parametrize("mutate", [
    lambda c: None,
    lambda c: c["missions"]["m_adapter"].__setitem__("auto_dispatch", False),            # true->false narrows
    lambda c: c["missions"]["m_orch"]["allowed_authority"].remove("shadow_emit"),         # allowed removal narrows
    lambda c: c["missions"]["m_orch"]["forbidden_authority"].append("extra_forbidden"),   # forbidden add narrows
    lambda c: c["missions"]["m_orch"].__setitem__("human_boundary_required", True),       # false->true narrows
    lambda c: c["missions"]["m_orch"].__setitem__("transition_policy", "human_required"),  # preauth->human narrows
    lambda c: c["missions"]["m_orch"]["change_policy"]["allowed_path_prefixes"].remove("tests/test_northstar_"),
    lambda c: c["missions"]["m_orch"]["change_policy"]["forbidden_path_prefixes"].append(".github/"),
    lambda c: c["missions"]["m_orch"]["change_policy"]["required_test_groups"].append("workflow_conformance"),
    lambda c: c["missions"]["m_orch"]["change_policy"]["semantic_guards"].append("extra_guard"),
    lambda c: c["auto_dispatch_allowed_risk_classes"].remove("E3"),                        # auto-risk removal narrows
    lambda c: c["human_required_risk_classes"].append("E5"),                               # human-risk add narrows
])
def test_registry_narrowing_allows_identical_or_narrower(mutate):
    assert _narrow(mutate) == []


def test_registry_narrowing_closes_p1_enabled_base_enabled_candidate_widening():
    # THE P1: after activation (base + candidate both enabled, mode unchanged so the
    # base-relative check passes), a candidate that WIDENS authority is still rejected.
    import copy
    base = _base_reg()
    base["controller"].update(mode="enabled", enabled_by="pesantezq", enabled_at="2026-10-15T00:00:00Z")
    cand = copy.deepcopy(base)
    cand["missions"]["m_orch"]["auto_dispatch"] = True   # widen while mode stays enabled==enabled
    out = gate.check_registry_authority_narrowing(base, cand)
    assert any("auto_dispatch" in x and "widen" in x for x in out), out
    assert not any("controller.mode" in x for x in out)   # mode identical -> no mode violation
    assert gate.check_registry_authority_narrowing(base, copy.deepcopy(base)) == []   # identical enabled -> clean


def test_registry_narrowing_rejects_unknown_new_field():
    assert any("unknown authority-bearing field" in x for x in _narrow(
        lambda c: c["missions"]["m_orch"].__setitem__("secret_backdoor", True)))
    assert any("unknown authority-bearing field" in x for x in _narrow(
        lambda c: c.__setitem__("new_toplevel_power", "x")))


def test_registry_narrowing_rejects_int_masquerading_as_bool():
    # YAML 1/0 must NOT masquerade as true/false: the guard requires real booleans,
    # because downstream dispatch code checks `is True`. (Codex P2 regression.)
    assert any("non-boolean" in x for x in _narrow(
        lambda c: c["missions"]["m_frozen"].__setitem__("human_boundary_required", 1)))   # true -> 1 bypass
    assert any("non-boolean" in x for x in _narrow(
        lambda c: c["missions"]["m_orch"].__setitem__("auto_dispatch", 1)))                # false -> 1 bypass
    # exact-equality bool fields are typed too: restore_paused_authorization true -> 1
    assert any("restore_paused_authorization" in x for x in _narrow(
        lambda c: c["missions"]["m_orch"]["on_success"].__setitem__("restore_paused_authorization", 1)))
    # a real boolean widening is still caught, and the identical registry stays clean
    assert any("auto_dispatch" in x and "widen" in x for x in _narrow(
        lambda c: c["missions"]["m_orch"].__setitem__("auto_dispatch", True)))
    assert _narrow(lambda c: None) == []


def test_real_base_registry_narrowing_rejects_widening():
    import copy
    base = asm._base_registry()
    assert isinstance(base, dict)
    controller = base.get("controller") or {}
    # Rollout-state-aware (was == "shadow"): the registry-widening security property
    # must hold in BOTH durable rollout states. The widening rejection below is
    # mode-independent, so the real base registry may be shadow OR (post-activation) enabled.
    assert controller.get("mode") in {"shadow", "enabled"}
    orch = base["missions"]["northstar_continuous_mission_orchestration_foundation"]
    assert orch["auto_dispatch"] is False
    assert gate.check_registry_authority_narrowing(base, copy.deepcopy(base)) == []
    cand = copy.deepcopy(base)
    cand["missions"]["northstar_continuous_mission_orchestration_foundation"]["auto_dispatch"] = True
    out = gate.check_registry_authority_narrowing(base, cand)
    assert any("auto_dispatch" in x and "widen" in x for x in out), out