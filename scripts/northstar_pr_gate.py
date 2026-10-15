#!/usr/bin/env python3
"""NORTHSTAR deterministic assurance gate — the autonomous merge authority.

Usage:
    python scripts/northstar_pr_gate.py evaluate --inputs <inputs.json> [--require-pass]

FULLY AUTONOMOUS and DETERMINISTIC: PASS/FAIL is established entirely from trusted
base-main code and machine-verifiable evidence. NO AI review (Codex or any LLM) and
NO human participates in the decision. Codex comments, if present, are advisory
telemetry only and never alter the decision.

Merge authority =
    authorized mission (candidate mission == protected base-main mission)
  + exact PR head
  + exact-head northstar-ci success
  + base-main authority reconciliation
  + mission CHANGE ENVELOPE (DEFAULT-DENY) for the mission's change class
  + forbidden-authority checks
  + control-plane security conformance (for control_plane class)
  + remote-main freshness.

Fail closed: any missing/ambiguous input makes the relevant check fail. The gate
performs NO merge; acting on PASS is the caller's job and only in `enabled` mode.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

# auto_* capability flags that must never be flipped on by a candidate (ew0a_runtime.json)
AUTO_FLAGS = (
    "auto_merge",
    "auto_deploy",
    "auto_production_mutation",
    "auto_authority_promotion",
    "auto_capital_action",
)

# Paths NO ordinary (normal / control_plane) mission envelope may ever allow. Dispatch
# state and authority are mutated ONLY by an authenticated governance_transition (its
# own exact-match lane), never by an implementation PR. Secrets/broker/systemd never.
GLOBAL_ABSOLUTE_FORBIDDEN_PREFIXES = (
    ".agent/project_state.yaml",
    ".agent/phase_status.yaml",
    "config/ew0a_runtime.json",
    "config/agent_policy.yaml",
    ".env",
    "portfolio_automation/broker",
    "systemd/",
)
GLOBAL_ABSOLUTE_FORBIDDEN_SUBSTR = ("credentials", "secret", "id_rsa", ".pem", ".netrc")

CHANGE_CLASSES = ("normal", "control_plane", "governance_transition")


# --------------------------------------------------------------------------- #
# Default-deny change envelope (the primary deterministic replacement for      #
# open-ended AI review discovering an accidentally-unprotected file).          #
# --------------------------------------------------------------------------- #
def _absolutely_forbidden(path: str) -> bool:
    if any(path.startswith(p) for p in GLOBAL_ABSOLUTE_FORBIDDEN_PREFIXES):
        return True
    low = path.lower()
    return any(tok in low for tok in GLOBAL_ABSOLUTE_FORBIDDEN_SUBSTR)


def evaluate_change_envelope(changed: list[str], envelope: dict[str, Any]) -> list[str]:
    """Return the DEFAULT-DENY violations: every changed path that is not explicitly
    allowed by the protected mission envelope, or that is absolutely forbidden. An
    empty envelope denies everything (fail closed)."""
    allowed_paths = set(envelope.get("allowed_paths") or [])
    allowed_prefixes = tuple(envelope.get("allowed_path_prefixes") or [])
    forbidden_prefixes = tuple(envelope.get("forbidden_path_prefixes") or [])
    violations: list[str] = []
    for p in changed:
        if not p:
            continue
        if _absolutely_forbidden(p) or any(p.startswith(fp) for fp in forbidden_prefixes):
            violations.append(p)
            continue
        if p in allowed_paths or any(p.startswith(pre) for pre in allowed_prefixes):
            continue
        violations.append(p)   # DEFAULT DENY
    return violations


# --------------------------------------------------------------------------- #
# Control-plane security conformance (deterministic, evaluated on the          #
# candidate's fetched files by base-main code — the candidate never evaluates  #
# its own PR).                                                                 #
# --------------------------------------------------------------------------- #
def _yaml_on(text: str):
    try:
        import yaml
        data = yaml.safe_load(text)
        return data.get(True, data.get("on")) if isinstance(data, dict) else None
    except Exception:
        return None


def check_control_plane_conformance(files: dict[str, str], base_controller_mode: str | None) -> list[str]:
    """Deterministic constitutional checks on a control-plane candidate's own files.
    `files`: {path: text} for the candidate's workflows + mission registry (fetched
    at the PR head). Returns violations; empty == conformant."""
    v: list[str] = []
    for path, text in files.items():
        if "/.github/workflows/" in f"/{path}" or path.startswith(".github/workflows/"):
            on = _yaml_on(text)
            triggers = set(on.keys()) if isinstance(on, dict) else ({on} if on else set())
            if "pull_request_target" in triggers:
                v.append(f"{path}: pull_request_target trigger")
            for u in re.findall(r"uses:\s*([^\s#]+)", text):
                if u.startswith("./"):
                    continue
                ref = u.split("@", 1)[1] if "@" in u else ""
                if not re.fullmatch(r"[0-9a-f]{40}", ref):
                    v.append(f"{path}: unpinned action {u}")
            if re.search(r"permissions:\s*write-all", text):
                v.append(f"{path}: permissions write-all")
    # Base-relative controller-mode conformance. A candidate may NEVER change the
    # controller rollout mode: its mode must EQUAL the trusted base-main mode, which
    # the IO layer reads from the CHECKED-OUT protected base-main registry (never from
    # candidate-controlled data). shadow->shadow and enabled->enabled pass; shadow->
    # enabled (self-enable) and enabled->shadow (self-disable) both fail. Any missing/
    # unknown value on either side fails closed. Only an operator bootstrap changes mode.
    valid_modes = ("shadow", "enabled")
    if base_controller_mode not in valid_modes:
        v.append(f"base-main controller.mode missing/unknown: {base_controller_mode!r} (fail closed)")
    reg = files.get(".agent/mission_registry.yaml")
    if reg is None:
        v.append("candidate mission_registry.yaml missing at head (fail closed)")
    else:
        try:
            import yaml
            cand_mode = ((yaml.safe_load(reg).get("controller") or {}).get("mode"))
        except Exception:
            v.append("mission_registry unparseable")
        else:
            if cand_mode not in valid_modes:
                v.append(f"mission_registry controller.mode missing/unknown: {cand_mode!r} (fail closed)")
            elif base_controller_mode in valid_modes and cand_mode != base_controller_mode:
                v.append(
                    f"mission_registry controller.mode {cand_mode!r} != base-main "
                    f"{base_controller_mode!r} (candidate cannot change controller rollout mode)")
    return v


# --------------------------------------------------------------------------- #
# Registry authority NARROWING (control-plane). A candidate mission registry may
# be IDENTICAL or strictly NARROWER than trusted base main, but may NEVER widen
# authority autonomously. Evaluated by base-main gate code against the candidate
# registry; candidate TESTS are never an authority source. Fail closed on any
# unparseable / unknown / ambiguous input or unknown authority-bearing field.
# --------------------------------------------------------------------------- #
_REG_KNOWN_TOPLEVEL = frozenset({
    "schema_version", "controller",
    "auto_dispatch_allowed_risk_classes", "human_required_risk_classes", "missions",
})
_REG_KNOWN_CONTROLLER = frozenset({"mode", "level", "enabled_by", "enabled_at"})
_REG_KNOWN_MISSION = frozenset({
    "executor", "risk_class", "prompt_source", "auto_dispatch", "completion_gate",
    "allowed_authority", "forbidden_authority", "transition_policy", "on_success",
    "human_boundary_required", "change_policy",
})
_REG_KNOWN_ON_SUCCESS = frozenset({"next_mission", "restore_paused_authorization", "restore_source"})
_REG_KNOWN_CHANGE_POLICY = frozenset({
    "class", "allowed_path_prefixes", "allowed_paths",
    "forbidden_path_prefixes", "required_test_groups", "semantic_guards",
})
_REG_TRANSITION_POLICIES = frozenset({"preauthorized_auto", "human_required"})
_REG_MISSING = object()


def _reg_slot(d, key):
    """Exact-equality slot that distinguishes ABSENT from an explicit null."""
    return (key in d, d.get(key)) if isinstance(d, dict) else (False, _REG_MISSING)


def _reg_typed_eq(a, b) -> bool:
    """Equality that keeps bool distinct from int: Python treats 1 == True, so a
    YAML `1`/`0` could otherwise masquerade as `true`/`false` and slip past an
    authority check while downstream code (which uses `is True`) reads it the other
    way. bool is only equal to bool."""
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    return a == b


def _reg_slot_ne(base, cand, key) -> bool:
    """True iff the key's slot differs (present/absent, or value by typed equality)."""
    pb, vb = (key in base, base.get(key)) if isinstance(base, dict) else (False, _REG_MISSING)
    pc, vc = (key in cand, cand.get(key)) if isinstance(cand, dict) else (False, _REG_MISSING)
    return pb != pc or not _reg_typed_eq(vb, vc)


def _reg_coerce_set(value):
    """(set, ok): None -> empty; list -> set; anything else -> not ok (fail closed)."""
    if value is None:
        return set(), True
    if isinstance(value, list):
        try:
            return set(value), True
        except TypeError:
            return set(), False
    return set(), False


def _reg_subset(label, base_list, cand_list):
    """Candidate must be a SUBSET of base (additions widen -> FAIL)."""
    bs, okb = _reg_coerce_set(base_list); cs, okc = _reg_coerce_set(cand_list)
    if not okb or not okc:
        return [f"{label}: non-list value (fail closed)"]
    added = cs - bs
    return [f"{label}: added {sorted(map(str, added))} (authority widening; candidate must be a subset of base)"] if added else []


def _reg_superset(label, base_list, cand_list):
    """Candidate must be a SUPERSET of base (removals widen -> FAIL)."""
    bs, okb = _reg_coerce_set(base_list); cs, okc = _reg_coerce_set(cand_list)
    if not okb or not okc:
        return [f"{label}: non-list value (fail closed)"]
    removed = bs - cs
    return [f"{label}: removed {sorted(map(str, removed))} (authority widening; candidate must be a superset of base)"] if removed else []


def _reg_bool_narrow(label, base_val, cand_val, forbidden):
    """Booleans only; `forbidden` is the widening transition, e.g. (False, True)."""
    if not isinstance(base_val, bool) or not isinstance(cand_val, bool):
        return [f"{label}: non-boolean value base={base_val!r} cand={cand_val!r} (fail closed)"]
    if (base_val, cand_val) == forbidden:
        return [f"{label}: {base_val}->{cand_val} widens authority (must not)"]
    return []


def _reg_unknown_keys(label, base, cand, known):
    """Any key outside `known` must be exactly equal, so a NEW authority-bearing
    field (or a change to an unrecognized one) fails closed."""
    out = []
    for k in sorted((set(base) | set(cand)) - set(known)):
        if _reg_slot_ne(base, cand, k):
            out.append(f"{label}.{k}: unknown authority-bearing field added/changed (fail closed)")
    return out


def _reg_change_policy(pfx, base, cand):
    if not isinstance(base, dict) or not isinstance(cand, dict):
        return [f"{pfx}.change_policy: not a mapping (fail closed)"]
    label = f"{pfx}.change_policy"; v = []
    if _reg_slot_ne(base, cand, "class"):
        v.append(f"{label}.class changed {base.get('class')!r}->{cand.get('class')!r} (must be exactly equal)")
    v += _reg_subset(f"{label}.allowed_path_prefixes", base.get("allowed_path_prefixes"), cand.get("allowed_path_prefixes"))
    v += _reg_subset(f"{label}.allowed_paths", base.get("allowed_paths"), cand.get("allowed_paths"))
    v += _reg_superset(f"{label}.forbidden_path_prefixes", base.get("forbidden_path_prefixes"), cand.get("forbidden_path_prefixes"))
    v += _reg_superset(f"{label}.required_test_groups", base.get("required_test_groups"), cand.get("required_test_groups"))
    v += _reg_superset(f"{label}.semantic_guards", base.get("semantic_guards"), cand.get("semantic_guards"))
    v += _reg_unknown_keys(label, base, cand, _REG_KNOWN_CHANGE_POLICY)
    return v


def _reg_mission_contract(mid, base, cand):
    pfx = f"missions.{mid}"
    if not isinstance(base, dict) or not isinstance(cand, dict):
        return [f"{pfx}: not a mapping (fail closed)"]
    v = []
    for key in ("executor", "risk_class", "prompt_source", "completion_gate"):
        if _reg_slot_ne(base, cand, key):
            v.append(f"{pfx}.{key} changed {base.get(key)!r}->{cand.get(key)!r} (must be exactly equal)")
    v += _reg_bool_narrow(f"{pfx}.auto_dispatch", base.get("auto_dispatch"), cand.get("auto_dispatch"), (False, True))
    v += _reg_bool_narrow(f"{pfx}.human_boundary_required", base.get("human_boundary_required"), cand.get("human_boundary_required"), (True, False))
    v += _reg_subset(f"{pfx}.allowed_authority", base.get("allowed_authority"), cand.get("allowed_authority"))
    v += _reg_superset(f"{pfx}.forbidden_authority", base.get("forbidden_authority"), cand.get("forbidden_authority"))
    bt, ct = base.get("transition_policy"), cand.get("transition_policy")
    if bt not in _REG_TRANSITION_POLICIES or ct not in _REG_TRANSITION_POLICIES:
        v.append(f"{pfx}.transition_policy: unknown value base={bt!r} cand={ct!r} (fail closed)")
    elif bt == "human_required" and ct == "preauthorized_auto":
        v.append(f"{pfx}.transition_policy: human_required->preauthorized_auto widens autonomy (must not)")
    bos, cos = base.get("on_success") or {}, cand.get("on_success") or {}
    if not isinstance(bos, dict) or not isinstance(cos, dict):
        v.append(f"{pfx}.on_success: not a mapping (fail closed)")
    else:
        for key in ("next_mission", "restore_paused_authorization", "restore_source"):
            if _reg_slot_ne(bos, cos, key):
                v.append(f"{pfx}.on_success.{key} changed (must be exactly equal, incl. absent/null)")
        v += _reg_unknown_keys(f"{pfx}.on_success", bos, cos, _REG_KNOWN_ON_SUCCESS)
    v += _reg_change_policy(pfx, base.get("change_policy") or {}, cand.get("change_policy") or {})
    v += _reg_unknown_keys(pfx, base, cand, _REG_KNOWN_MISSION)
    return v


def check_registry_authority_narrowing(base, candidate):
    """Pure: violations iff `candidate` mission registry WIDENS authority vs trusted
    `base`. [] == identical-or-narrower. Fail closed on unparseable/unknown input.
    `base` and `candidate` are parsed registry dicts (None == parse failure). The
    base registry MUST come from trusted base main; candidate tests are never an
    authority source. controller.mode exact-equality here complements (does not
    replace) the base-relative check in check_control_plane_conformance."""
    if not isinstance(base, dict):
        return ["registry: trusted base-main registry unparseable/missing (fail closed)"]
    if not isinstance(candidate, dict):
        return ["registry: candidate registry unparseable/missing (fail closed)"]
    v = []
    if _reg_slot_ne(base, candidate, "schema_version"):
        v.append(f"registry.schema_version changed {base.get('schema_version')!r}->{candidate.get('schema_version')!r} (must be exactly equal)")
    bc, cc = base.get("controller") or {}, candidate.get("controller") or {}
    if not isinstance(bc, dict) or not isinstance(cc, dict):
        v.append("registry.controller: not a mapping (fail closed)")
    else:
        for key in ("mode", "level", "enabled_by", "enabled_at"):
            if _reg_slot_ne(bc, cc, key):
                v.append(f"registry.controller.{key} changed {bc.get(key)!r}->{cc.get(key)!r} (must be exactly equal for autonomous control-plane PRs)")
        v += _reg_unknown_keys("registry.controller", bc, cc, _REG_KNOWN_CONTROLLER)
    v += _reg_subset("registry.auto_dispatch_allowed_risk_classes",
                     base.get("auto_dispatch_allowed_risk_classes"), candidate.get("auto_dispatch_allowed_risk_classes"))
    v += _reg_superset("registry.human_required_risk_classes",
                       base.get("human_required_risk_classes"), candidate.get("human_required_risk_classes"))
    bm, cm = base.get("missions") or {}, candidate.get("missions") or {}
    if not isinstance(bm, dict) or not isinstance(cm, dict):
        v.append("registry.missions: not a mapping (fail closed)")
    else:
        new, gone = sorted(set(cm) - set(bm)), sorted(set(bm) - set(cm))
        if new:
            v.append(f"registry.missions added {new} (new mission id; authority widening)")
        if gone:
            v.append(f"registry.missions removed {gone} (mission-id set must remain exactly equal; fail closed)")
        for mid in sorted(set(bm) & set(cm)):
            v += _reg_mission_contract(mid, bm[mid], cm[mid])
    v += _reg_unknown_keys("registry", base, candidate, _REG_KNOWN_TOPLEVEL)
    return v


def evaluate_merge_gate(inputs: dict[str, Any]) -> dict[str, Any]:
    """Deterministic NORTHSTAR assurance gate. Fail closed. No AI/human input."""
    mode = inputs.get("mode", "shadow")
    checks: list[dict] = []

    def c(name: str, ok: bool, detail: str = "") -> bool:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        return bool(ok)

    pr = inputs.get("pr", {}) or {}
    ci = inputs.get("ci", {}) or {}
    protected = inputs.get("protected", {}) or {}
    cand = inputs.get("candidate", {}) or {}
    head = pr.get("head_sha")

    # ---- PR identity -------------------------------------------------------
    c("pr_open", pr.get("state") == "OPEN", str(pr.get("state")))
    c("pr_not_draft", pr.get("is_draft") is False, str(pr.get("is_draft")))
    c("base_is_main", pr.get("base_ref") == "main", str(pr.get("base_ref")))
    c("pr_mergeable", pr.get("mergeable") is True, str(pr.get("mergeable")))
    c("candidate_mission_declared", bool(cand.get("authorized_mission")),
      str(cand.get("authorized_mission")))
    c("protected_mission_authorizes_pr",
      bool(cand.get("authorized_mission")) and cand.get("authorized_mission") == protected.get("current_mission"),
      f"{cand.get('authorized_mission')} vs {protected.get('current_mission')}")

    # ---- change class + DEFAULT-DENY envelope (replaces AI semantic review) --
    klass = cand.get("change_class")
    c("change_class_known", klass in CHANGE_CLASSES, str(klass))
    if klass == "governance_transition":
        c("deterministic_transition_exact_match", cand.get("transition_exact_match") is True,
          "PR diff exactly equals the re-derived materialized transition")
    else:
        c("change_envelope_default_deny", not cand.get("change_envelope_violations"),
          str(cand.get("change_envelope_violations") or []))
        if klass == "control_plane":
            c("control_plane_authorized_by_base_main", cand.get("control_plane_authorized") is True,
              "base-main registry authorizes control_plane for the current mission")
            c("control_plane_security_conformance", not cand.get("conformance_violations"),
              str(cand.get("conformance_violations") or []))

    # ---- exact-head CI -----------------------------------------------------
    c("ci_is_northstar_ci", ci.get("workflow_name") == "northstar-ci", str(ci.get("workflow_name")))
    c("ci_success", ci.get("status") == "completed" and ci.get("conclusion") == "success",
      f"{ci.get('status')}/{ci.get('conclusion')}")
    c("ci_bound_to_head", bool(head) and ci.get("head_sha") == head,
      f"ci {ci.get('head_sha')} vs head {head}")

    # ---- required mission test groups (declared by the protected envelope) --
    missing_groups = cand.get("missing_required_test_groups")
    if missing_groups is not None:
        c("required_test_groups_present", not missing_groups, str(missing_groups or []))

    # ---- forbidden authority (always) --------------------------------------
    c("no_forbidden_authority_introduced", not cand.get("forbidden_authority_introduced"),
      str(cand.get("forbidden_authority_introduced") or []))

    # ---- remote-main freshness ---------------------------------------------
    c("main_not_advanced",
      bool(protected.get("main_sha")) and inputs.get("now_main_sha") == protected.get("main_sha"),
      f"{inputs.get('now_main_sha')} vs {protected.get('main_sha')}")

    blocking = [ck["name"] for ck in checks if not ck["ok"]]
    decision = "PASS" if not blocking else "FAIL"
    return {
        "gate": "NORTHSTAR_DETERMINISTIC_ASSURANCE_GATE",
        "decision": decision,
        "mode": mode,
        "controller_level": "C0.5_SHADOW" if mode == "shadow" else "enabled",
        "head_sha": head,
        "change_class": klass,
        "checks": checks,
        "blocking_reasons": blocking,
        "would_merge": decision == "PASS" and mode == "enabled",
        "merge_ready": decision == "PASS",
        # Codex / any AI review is ADVISORY ONLY and never affects the decision.
        "codex_role": "advisory_only",
        "advisory_codex": inputs.get("advisory_codex"),
        "bootstrap_note": "shadow mode performs no merge; enabled mode required to act on PASS",
    }


# --------------------------------------------------------------------------- #
# Candidate authority assessment helpers (used by the CLI/assembler; tested).  #
# --------------------------------------------------------------------------- #
CONTROLLER_PROTECTED_PREFIXES = (
    ".github/",
    "scripts/northstar_",
    ".agent/mission_registry.yaml",
    ".agent/missions/",
)


def _is_protected(path: str) -> bool:
    """Belt-and-suspenders: controller files + known forbidden-semantic surfaces are
    protected regardless of envelope (used by assess_candidate and as a sanity layer).
    The DEFAULT-DENY envelope is the primary mechanism; this is defense in depth."""
    if any(path.startswith(p) for p in CONTROLLER_PROTECTED_PREFIXES):
        return True
    if path.rsplit("/", 1)[-1] in ("recommendations.py", "recommendation_engine.py",
                                    "allocation_engine.py", "decision_engine.py",
                                    "scoring.py", "config.json"):
        return True
    try:
        from portfolio_automation.engineer_worker.policy import is_protected
        return bool(is_protected(path))
    except Exception:
        fallback = (".agent/", "config/agent_policy.yaml", "config/ew0a_runtime",
                    "portfolio_automation/scoring", "portfolio_automation/broker",
                    "systemd/", ".git/", ".env", "credentials", "secrets")
        return any(tok in path for tok in fallback)


def assess_candidate(base: dict[str, Any], head: dict[str, Any],
                     allowlisted_new_paths: list[str] | None = None) -> dict[str, Any]:
    """Compare base-main vs candidate-head authority surfaces (forbidden-authority +
    a belt-and-suspenders protected-path list). The default-deny envelope is the
    primary path gate; this remains for the enabled two-checkout diff."""
    allow = set(allowlisted_new_paths or [])
    authority_mutated = (
        base.get("dispatch") != head.get("dispatch")
        or base.get("agent_policy_digest") != head.get("agent_policy_digest")
    )
    forbidden: list[str] = []
    for f in AUTO_FLAGS:
        if head.get("ew0a", {}).get(f) and not base.get("ew0a", {}).get(f):
            forbidden.append(f)
    if head.get("c1") == "ENABLED" and base.get("c1") != "ENABLED":
        forbidden.append("c1_enabled")
    if head.get("phase_0d_status") == "active" and base.get("phase_0d_status") != "active":
        forbidden.append("phase_0d_advanced")
    violations = [p for p in head.get("changed_paths", []) if _is_protected(p) and p not in allow]
    return {
        "authority_mutated": authority_mutated,
        "forbidden_authority_introduced": forbidden,
        "protected_path_violations": violations,
    }


def _cli(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="northstar_pr_gate.py", add_help=True,
                                 description="Evaluate the NORTHSTAR deterministic assurance gate.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ev = sub.add_parser("evaluate", help="evaluate the gate from an inputs JSON file")
    ev.add_argument("--inputs", required=True)
    ev.add_argument("--require-pass", action="store_true",
                    help="exit 1 unless decision==PASS (for an enabled required check)")
    args = ap.parse_args(argv)
    if args.cmd == "evaluate":
        try:
            inputs = json.loads(Path(args.inputs).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"ERROR: cannot read inputs: {e}", file=sys.stderr)
            return 1
        result = evaluate_merge_gate(inputs)
        print(json.dumps(result, indent=2, sort_keys=True))
        if args.require_pass and result["decision"] != "PASS":
            return 1
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
