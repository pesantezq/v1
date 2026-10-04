#!/usr/bin/env python3
"""Deterministic Northstar mission-transition proposer.

Usage:
    python scripts/northstar_transition.py evaluate --inputs <inputs.json>
    python scripts/northstar_transition.py -h

Given trusted inputs (the completed mission, base-main protected state, the
certified main SHA + post-merge evidence, the mission registry, and the exact
preserved paused authorization), `propose_transition` emits EITHER a deterministic
governance-PR proposal that edits only an explicit allowlist of protected-state
fields, OR a STOP (for `human_required` edges, E4, missing/ambiguous inputs).

It NEVER mutates protected state and NEVER pushes to `main`: a `preauthorized_auto`
edge is delivered as a governance branch/PR that goes through ordinary CI/review/
merge. No LLM writes transition state. Authority comes only from base-main
protected state; the paused VS-002 adapter authorization is restored BYTE/
STRUCTURE-EQUIVALENTLY from `phase_status.yaml` and is never reconstructed or
widened. Fail closed on any ambiguity.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def propose_transition(inputs: dict[str, Any]) -> dict[str, Any]:
    """Deterministic transition decision. Returns {decision: PROPOSE|STOP, ...}."""
    mode = inputs.get("mode", "shadow")
    checks: list[dict] = []

    def c(name: str, ok: bool, detail: str = "") -> bool:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        return bool(ok)

    def stop(reason: str) -> dict[str, Any]:
        return {"decision": "STOP", "reason": reason, "mode": mode,
                "controller_level": "C0.5_SHADOW" if mode == "shadow" else "enabled",
                "checks": checks, "proposal": None, "next_mission": None}

    completed = inputs.get("completed_mission")
    authoritative = inputs.get("authoritative_mission")
    registry = inputs.get("registry", {}) or {}
    missions = registry.get("missions", {}) or {}
    paused_auth = inputs.get("paused_authorization")
    certified_sha = inputs.get("certified_main_sha")
    protected_sha = inputs.get("protected_main_sha")
    post_merge = inputs.get("post_merge", {}) or {}

    # 1. the completed mission must be the authoritative base-main mission (no ambiguity)
    if not c("completed_is_authoritative",
             bool(completed) and completed == authoritative,
             f"{completed} vs {authoritative}"):
        return stop("completed_mission_not_authoritative")

    # 2. post-merge certification must be valid: success push on main at the exact sha
    pm_ok = (post_merge.get("conclusion") == "success"
             and post_merge.get("event") == "push"
             and post_merge.get("head_branch") == "main"
             and bool(certified_sha)
             and post_merge.get("head_sha") == certified_sha
             and certified_sha == protected_sha)
    if not c("post_merge_certified_exact_sha", pm_ok,
             f"pm_sha={post_merge.get('head_sha')} certified={certified_sha} protected={protected_sha} "
             f"event={post_merge.get('event')} concl={post_merge.get('conclusion')}"):
        return stop("post_merge_not_certified")

    # 3. registry entry for the completed mission
    entry = missions.get(completed)
    if not c("registry_entry_present", entry is not None, completed or ""):
        return stop("missing_registry_entry")

    policy = entry.get("transition_policy")
    # 4. human-required wall: no proposal, STOP
    if policy == "human_required":
        c("human_required_wall", True, "transition requires operator authorization")
        return {"decision": "STOP", "reason": "human_required", "mode": mode,
                "controller_level": "C0.5_SHADOW" if mode == "shadow" else "enabled",
                "checks": checks, "proposal": None,
                "next_mission": entry.get("on_success", {}).get("next_mission")}
    if not c("transition_policy_preauthorized", policy == "preauthorized_auto", f"policy={policy}"):
        return stop("unknown_or_unsupported_transition_policy")

    # 5. next mission must exist, be a registered claude mission, auto-dispatchable class
    on_success = entry.get("on_success", {}) or {}
    nxt = on_success.get("next_mission")
    if not c("next_mission_present", bool(nxt)):
        return stop("no_next_mission")
    nxt_entry = missions.get(nxt)
    if not c("next_mission_in_registry", nxt_entry is not None, nxt or ""):
        return stop("next_mission_not_in_registry")
    if not c("next_executor_claude", nxt_entry.get("executor") == "claude",
             str(nxt_entry.get("executor"))):
        return stop("next_mission_not_claude")
    allowed_rc = set(registry.get("auto_dispatch_allowed_risk_classes", []) or [])
    if not c("next_risk_class_allowed", nxt_entry.get("risk_class") in allowed_rc,
             f"rc={nxt_entry.get('risk_class')} allowed={sorted(allowed_rc)}"):
        return stop("next_mission_risk_class_requires_human")
    if nxt_entry.get("human_boundary_required") is True:
        c("next_mission_human_boundary", False, "next mission requires a human boundary")
        return stop("next_mission_requires_human_boundary")

    # 6. restore the EXACT preserved paused authorization (adapter resume case)
    restored_auth = None
    if on_success.get("restore_paused_authorization"):
        if not c("paused_authorization_present", paused_auth is not None):
            return stop("paused_authorization_missing")
        if not c("paused_authorization_matches_next",
                 isinstance(paused_auth, dict)
                 and paused_auth.get("authorized_mission") == nxt,
                 f"paused={paused_auth.get('authorized_mission') if isinstance(paused_auth, dict) else None} next={nxt}"):
            return stop("paused_authorization_mission_mismatch")
        # verbatim; never reconstructed, paraphrased, or widened
        restored_auth = paused_auth

    # 7. build the allowlisted governance-PR proposal (dispatch fields -> next mission)
    proposal = {
        "proposal_type": "governance_transition_pr",
        "delivery": "ordinary CI/review/human-merge governance; NOT a direct main push",
        "completed_mission": completed,
        "next_mission": nxt,
        "certified_main_sha": certified_sha,
        "allowlisted_field_edits": {
            ".agent/project_state.yaml#current_step": nxt,
            ".agent/project_state.yaml#next_official_step.primary": nxt,
            ".agent/project_state.yaml#next_official_step.prior_primary": completed,
            ".agent/phase_status.yaml#stockbot_northstar_redesign.engineer_runtime_state.mission_id": nxt,
            ".agent/phase_status.yaml#stockbot_northstar_redesign.phases.northstar_phase_0c.step": nxt,
            "config/ew0a_runtime.json#mission_id": nxt,
        },
        "restore_bounded_authorization": restored_auth,   # EXACT paused object or None
        "forbidden": [
            "no protected field outside allowlisted_field_edits may change",
            "no authority widening",
            "no direct push to main",
            "no mark-complete without operator review",
        ],
    }
    c("proposal_allowlist_only", True, "only dispatch fields + exact bounded-authorization restore")

    return {
        "decision": "PROPOSE",
        "reason": "preauthorized_auto edge to an already-authorized claude mission",
        "mode": mode,
        "controller_level": "C0.5_SHADOW" if mode == "shadow" else "enabled",
        "next_mission": nxt,
        "checks": checks,
        "proposal": proposal,
        "would_open_governance_pr": mode == "enabled",
        "bootstrap_note": "shadow mode emits NEXT_MISSION_READY only; opens no PR and mutates nothing",
    }


def _cli(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="northstar_transition.py", add_help=True,
                                 description="Propose a deterministic mission transition.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ev = sub.add_parser("evaluate", help="evaluate a transition from an inputs JSON file")
    ev.add_argument("--inputs", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "evaluate":
        try:
            inputs = json.loads(Path(args.inputs).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"ERROR: cannot read inputs: {e}", file=sys.stderr)
            return 1
        print(json.dumps(propose_transition(inputs), indent=2, sort_keys=True))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
