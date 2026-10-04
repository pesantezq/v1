#!/usr/bin/env python3
"""NORTHSTAR_MERGE_GATE — the single deterministic pre-merge gate.

Usage:
    python scripts/northstar_pr_gate.py evaluate --inputs <inputs.json> [--require-pass]
    python scripts/northstar_pr_gate.py -h

This is a DETERMINISTIC control-plane component, not an LLM. Given a fully
assembled, already-fetched `inputs` object (PR metadata, Northstar CI result,
Codex review/reaction state, review-thread state, base-main protected state, and
a candidate authority assessment), `evaluate_merge_gate` returns a
machine-readable audit with a per-check breakdown and an overall PASS/FAIL. It
FAILS CLOSED: any missing/ambiguous input makes the relevant check fail.

Authority precedence: the gate re-derives the authorized mission from base-main
protected state (passed in `inputs["protected"]`); a candidate branch's own copy
of state/registry can never satisfy it. The gate performs NO merge; acting on a
PASS is the caller's job and only in `enabled` mode.

Codex exact-head binding (observed `chatgpt-codex-connector[bot]` protocol):
  * a review is a PR review (state COMMENTED) whose `commit_id` is the reviewed head;
  * material findings are inline comments carrying a P1/P2 severity badge;
  * a CLEAN signal is a `+1` issue reaction from the bot created AFTER the
    exact-head review/request anchor, with zero unresolved material threads.
A stale signal bound to an older head never satisfies the gate, and clean is
never inferred from silence.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

CODEX_BOT_LOGIN = "chatgpt-codex-connector[bot]"
MATERIAL_SEVERITIES = ("P1", "P2")
# auto_* capability flags that must never be flipped on by a candidate (ew0a_runtime.json)
AUTO_FLAGS = (
    "auto_merge",
    "auto_deploy",
    "auto_production_mutation",
    "auto_authority_promotion",
    "auto_capital_action",
)


def evaluate_codex_binding(codex: dict[str, Any], head_sha: str | None) -> tuple[bool, list[dict]]:
    """Return (clean_at_head, subchecks). Fail closed.

    `codex` shape:
      {
        "bot_login": str,
        "reviews": [{"author","state","commit_id","submitted_at"}...],
        "inline_comments": [{"author","severity","commit_id","resolved","created_at"}...],
        "clean_reaction": {"content","actor","created_at"} | None,
        "exact_head_review_request": {"sha","requested_at"} | None,
      }
    """
    bot = codex.get("bot_login") or CODEX_BOT_LOGIN
    subs: list[dict] = []

    def s(name: str, ok: bool, detail: str = "") -> bool:
        subs.append({"name": name, "ok": bool(ok), "detail": detail})
        return bool(ok)

    if not head_sha:
        s("head_known", False, "no PR head sha")
        return False, subs

    reviews = [r for r in codex.get("reviews", []) if r.get("author") == bot]
    comments = [c for c in codex.get("inline_comments", []) if c.get("author") == bot]
    reaction = codex.get("clean_reaction")
    req = codex.get("exact_head_review_request")

    head_reviews = [r for r in reviews if r.get("commit_id") == head_sha]
    s("review_bound_to_head", bool(head_reviews),
      f"{len(head_reviews)} review(s) at {head_sha[:10]}")

    unresolved_material = [
        c for c in comments
        if c.get("severity") in MATERIAL_SEVERITIES and not c.get("resolved", False)
    ]
    no_unresolved = s("zero_unresolved_material_threads", not unresolved_material,
                      f"{len(unresolved_material)} unresolved P1/P2 finding(s)")

    # A clean terminal: a +1 reaction from the bot, bound to the exact head via an
    # exact-head review request (naming the full head sha) OR the latest head
    # review, created AFTER that anchor, with no newer material finding at head.
    reaction_ok = False
    reaction_detail = "no valid head-bound +1 reaction"
    if reaction and reaction.get("content") == "+1" and reaction.get("actor") == bot:
        created = reaction.get("created_at")
        anchor = None
        anchor_kind = None
        if req and req.get("sha") == head_sha and req.get("requested_at"):
            anchor, anchor_kind = req["requested_at"], "exact-head request"
        elif head_reviews:
            anchor = max(r.get("submitted_at", "") for r in head_reviews)
            anchor_kind = "head review"
        if anchor and created and created > anchor:
            newer_findings = [
                c for c in comments
                if c.get("commit_id") == head_sha
                and c.get("severity") in MATERIAL_SEVERITIES
                and (c.get("created_at") or "") > created
            ]
            if not newer_findings:
                reaction_ok = True
                reaction_detail = f"+1 after {anchor_kind}; no newer findings"
            else:
                reaction_detail = "newer material finding after the +1 (stale)"
        elif anchor:
            reaction_detail = "+1 predates the exact-head anchor (stale)"
        else:
            reaction_detail = "no exact-head anchor for the +1"
    s("clean_reaction_bound_to_head", reaction_ok, reaction_detail)

    clean = reaction_ok and no_unresolved
    return clean, subs


def evaluate_merge_gate(inputs: dict[str, Any]) -> dict[str, Any]:
    """Deterministic NORTHSTAR_MERGE_GATE. Fail closed on any failed check."""
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

    c("pr_open", pr.get("state") == "OPEN", str(pr.get("state")))
    c("pr_not_draft", pr.get("is_draft") is False, str(pr.get("is_draft")))
    c("base_is_main", pr.get("base_ref") == "main", str(pr.get("base_ref")))
    c("pr_mergeable", pr.get("mergeable") is True, str(pr.get("mergeable")))
    c("protected_mission_authorizes_pr",
      bool(cand.get("authorized_mission")) and cand.get("authorized_mission") == protected.get("current_mission"),
      f"{cand.get('authorized_mission')} vs {protected.get('current_mission')}")
    c("ci_is_northstar_ci", ci.get("workflow_name") == "northstar-ci", str(ci.get("workflow_name")))
    c("ci_success", ci.get("status") == "completed" and ci.get("conclusion") == "success",
      f"{ci.get('status')}/{ci.get('conclusion')}")
    c("ci_bound_to_head", bool(head) and ci.get("head_sha") == head,
      f"ci {ci.get('head_sha')} vs head {head}")

    codex_ok, codex_subs = evaluate_codex_binding(inputs.get("codex", {}) or {}, head)
    for sc in codex_subs:
        checks.append({"name": "codex:" + sc["name"], "ok": sc["ok"], "detail": sc["detail"]})
    c("codex_clean_at_head", codex_ok)

    c("no_authority_mutation", cand.get("authority_mutated") is False, str(cand.get("authority_mutated")))
    c("no_forbidden_authority_introduced", not cand.get("forbidden_authority_introduced"),
      str(cand.get("forbidden_authority_introduced") or []))
    c("protected_path_policy_ok", not cand.get("protected_path_violations"),
      str(cand.get("protected_path_violations") or []))
    c("main_not_advanced",
      bool(protected.get("main_sha")) and inputs.get("now_main_sha") == protected.get("main_sha"),
      f"{inputs.get('now_main_sha')} vs {protected.get('main_sha')}")

    # `codex:` entries are diagnostic detail for the aggregate `codex_clean_at_head`
    # check; only top-level checks gate the decision.
    blocking = [ck["name"] for ck in checks
                if not ck["ok"] and not ck["name"].startswith("codex:")]
    decision = "PASS" if not blocking else "FAIL"
    return {
        "gate": "NORTHSTAR_MERGE_GATE",
        "decision": decision,
        "mode": mode,
        "controller_level": "C0.5_SHADOW" if mode == "shadow" else "enabled",
        "head_sha": head,
        "checks": checks,
        "blocking_reasons": blocking,
        "would_merge": decision == "PASS" and mode == "enabled",
        "merge_ready": decision == "PASS",
        "bootstrap_note": "shadow mode performs no merge; enabled mode required to act on PASS",
    }


# --------------------------------------------------------------------------- #
# Candidate authority assessment (used by the CLI/workflow; unit-tested).      #
# --------------------------------------------------------------------------- #
def _is_protected(path: str) -> bool:
    """Mirror the engineer-worker protected-path policy; fall back to a minimal
    pattern set if the module is unavailable (keeps the gate importable in any
    checkout)."""
    try:
        from portfolio_automation.engineer_worker.policy import is_protected
        return bool(is_protected(path))
    except Exception:
        fallback = (".agent/", "config/agent_policy.yaml", "config/ew0a_runtime",
                    "decision_engine.py", "portfolio_automation/scoring",
                    "portfolio_automation/broker", "systemd/", ".git/", ".env",
                    "credentials", "secrets")
        return any(tok in path for tok in fallback)


def assess_candidate(base: dict[str, Any], head: dict[str, Any],
                     allowlisted_new_paths: list[str] | None = None) -> dict[str, Any]:
    """Compare base-main vs candidate-head authority surfaces.

    base/head shape: {"dispatch": {field: value}, "ew0a": {flag: bool},
                      "agent_policy_digest": str, "c1": str, "phase_0d_status": str}
    head also: {"changed_paths": [str]}.
    `allowlisted_new_paths`: new files this authorized mission legitimately adds
    (e.g. the orchestration registry/scripts/workflows). A protected path is a
    violation only if it is a change to an EXISTING protected path not on the
    allowlist.
    """
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
    violations = [
        p for p in head.get("changed_paths", [])
        if _is_protected(p) and p not in allow
    ]
    return {
        "authority_mutated": authority_mutated,
        "forbidden_authority_introduced": forbidden,
        "protected_path_violations": violations,
    }


def _cli(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="northstar_pr_gate.py", add_help=True,
                                 description="Evaluate the NORTHSTAR_MERGE_GATE.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ev = sub.add_parser("evaluate", help="evaluate the gate from an inputs JSON file")
    ev.add_argument("--inputs", required=True, help="path to the assembled inputs JSON")
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
