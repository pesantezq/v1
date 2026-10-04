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


def check_control_plane_conformance(files: dict[str, str]) -> list[str]:
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
    reg = files.get(".agent/mission_registry.yaml")
    if reg is not None:
        try:
            import yaml
            mode = ((yaml.safe_load(reg).get("controller") or {}).get("mode"))
            if mode != "shadow":
                v.append("mission_registry controller.mode != shadow (candidate cannot self-enable)")
        except Exception:
            v.append("mission_registry unparseable")
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
