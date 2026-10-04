#!/usr/bin/env python3
"""Assemble NORTHSTAR_MERGE_GATE inputs from the read-only GitHub API.

Usage:
    python scripts/northstar_assemble_gate_inputs.py --repo owner/name --pr N \
        --mode shadow --out inputs.json

IO GLUE, not a deterministic core: it gathers PR / CI / Codex metadata via `gh`
and base-main protected state, then writes the `inputs.json` consumed by the
unit-tested `northstar_pr_gate.evaluate_merge_gate`. It fails SOFT (nonzero exit,
message on stderr) so the controller can treat an assembly failure as a shadow
no-op. It performs only read-only API calls and never mutates anything.

The authoritative mission is read from base-main protected state via the
fail-closed `roadmap_guard`; protected-path detection reuses the engineer-worker
`policy`. A candidate branch's own copy of state can never be the authority here.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

CODEX_BOT_LOGIN = "chatgpt-codex-connector[bot]"
REPO_ROOT = Path(__file__).resolve().parent.parent

# New declarative artifacts this authorized mission legitimately adds; a change to
# one of these is not a protected-path violation.
ALLOWLISTED_NEW_PATHS = {
    ".agent/mission_registry.yaml",
    "scripts/northstar_pr_gate.py",
    "scripts/northstar_transition.py",
    "scripts/northstar_mission_packet.py",
    "scripts/northstar_assemble_gate_inputs.py",
    "docs/NORTHSTAR_ORCHESTRATION.md",
    ".github/workflows/northstar-pr-controller.yml",
    ".github/workflows/northstar-orchestrator.yml",
    ".github/workflows/claude-authorized-mission.yml",
}


def _gh_json(args: list[str]) -> Any:
    out = subprocess.run(["gh", "api", *args], capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"gh api {' '.join(args)} failed: {out.stderr.strip()}")
    return json.loads(out.stdout)


def _severity_from_body(body: str) -> str | None:
    if "badge/P1" in body or "![P1" in body:
        return "P1"
    if "badge/P2" in body or "![P2" in body:
        return "P2"
    return None


def _authoritative_mission() -> str | None:
    try:
        from portfolio_automation.engineer_worker.roadmap_guard import RoadmapAuthorization
        auth = RoadmapAuthorization.read(REPO_ROOT)
        return auth.authorized_mission_id if auth.authoritative else None
    except Exception:
        return None


def _is_protected(path: str) -> bool:
    try:
        from portfolio_automation.engineer_worker.policy import is_protected
        return bool(is_protected(path))
    except Exception:
        return path.startswith((".agent/", "config/agent_policy", "config/ew0a_runtime"))


def assemble(repo: str, pr_number: int, mode: str) -> dict:
    owner_repo = repo
    pr = _gh_json([f"repos/{owner_repo}/pulls/{pr_number}"])
    head_sha = pr["head"]["sha"]
    base_ref = pr["base"]["ref"]

    # CI: most recent northstar-ci run for the head sha
    runs = _gh_json([f"repos/{owner_repo}/actions/runs?head_sha={head_sha}&per_page=50"])
    ci = {"workflow_name": None, "head_sha": head_sha, "status": None, "conclusion": None}
    for r in runs.get("workflow_runs", []):
        if r.get("name") == "northstar-ci":
            ci = {"workflow_name": "northstar-ci", "head_sha": r.get("head_sha"),
                  "status": r.get("status"), "conclusion": r.get("conclusion")}
            break

    reviews_raw = _gh_json([f"repos/{owner_repo}/pulls/{pr_number}/reviews?per_page=100"])
    reviews = [{"author": (rv.get("user") or {}).get("login"),
                "state": rv.get("state"),
                "commit_id": rv.get("commit_id"),
                "submitted_at": rv.get("submitted_at")}
               for rv in reviews_raw]

    comments_raw = _gh_json([f"repos/{owner_repo}/pulls/{pr_number}/comments?per_page=100"])
    inline_comments = []
    for cm in comments_raw:
        sev = _severity_from_body(cm.get("body") or "")
        if sev:
            inline_comments.append({"author": (cm.get("user") or {}).get("login"),
                                    "severity": sev, "commit_id": cm.get("commit_id"),
                                    "resolved": False,  # REST can't see thread resolution; enabled mode uses GraphQL
                                    "created_at": cm.get("created_at")})

    reactions_raw = _gh_json([f"repos/{owner_repo}/issues/{pr_number}/reactions?per_page=100"])
    clean_reaction = None
    for rx in reactions_raw:
        if rx.get("content") == "+1" and (rx.get("user") or {}).get("login") == CODEX_BOT_LOGIN:
            clean_reaction = {"content": "+1", "actor": CODEX_BOT_LOGIN, "created_at": rx.get("created_at")}

    # changed paths → best-effort protected-path assessment (shadow)
    files_raw = _gh_json([f"repos/{owner_repo}/pulls/{pr_number}/files?per_page=100"])
    changed = [f.get("filename") for f in files_raw]
    protected_violations = [p for p in changed if _is_protected(p) and p not in ALLOWLISTED_NEW_PATHS]

    authoritative = _authoritative_mission()
    main_sha = subprocess.run(["git", "rev-parse", "origin/main"],
                              capture_output=True, text=True, cwd=str(REPO_ROOT)).stdout.strip() or None

    return {
        "mode": mode,
        "pr": {"state": pr.get("state", "").upper(), "is_draft": bool(pr.get("draft")),
               "base_ref": base_ref, "head_sha": head_sha,
               "mergeable": pr.get("mergeable") is True},
        "ci": ci,
        "codex": {
            "bot_login": CODEX_BOT_LOGIN,
            "reviews": reviews,
            "inline_comments": inline_comments,
            "clean_reaction": clean_reaction,
            "exact_head_review_request": None,  # enabled mode derives this from an @codex review at head
        },
        "protected": {"current_mission": authoritative, "main_sha": main_sha},
        "candidate": {
            "authorized_mission": authoritative,  # from base-main protected state (roadmap_guard)
            # Full base-vs-head authority diff is computed in enabled mode; shadow
            # reports protected-path touches and leaves the deeper diff to the gate's
            # dedicated assess_candidate step on two checkouts.
            "authority_mutated": False,
            "forbidden_authority_introduced": [],
            "protected_path_violations": protected_violations,
        },
        "now_main_sha": main_sha,
    }


def _cli(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="northstar_assemble_gate_inputs.py")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--pr", required=True, type=int)
    ap.add_argument("--mode", default="shadow")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    try:
        inputs = assemble(args.repo, args.pr, args.mode)
    except Exception as e:  # fail soft
        print(f"ERROR: gate-input assembly failed: {e}", file=sys.stderr)
        return 1
    Path(args.out).write_text(json.dumps(inputs, indent=2, sort_keys=True), encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
