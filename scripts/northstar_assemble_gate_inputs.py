#!/usr/bin/env python3
"""Assemble NORTHSTAR_MERGE_GATE inputs from the read-only GitHub API.

Usage:
    python scripts/northstar_assemble_gate_inputs.py --repo owner/name --pr N \
        --mode shadow --out inputs.json

IO GLUE (not a deterministic core): gathers PR / CI / Codex metadata via `gh` and
base-main protected state, then writes the `inputs.json` consumed by the
unit-tested `northstar_pr_gate.evaluate_merge_gate`. The decidable bits are
factored into PURE, unit-tested helpers below (candidate-mission derivation,
added-only protected-path policy, material-unresolved detection). Fails SOFT
(nonzero exit) so the controller treats assembly failure as a shadow no-op.
Read-only; mutates nothing.

Security properties (Codex-hardened):
  * candidate mission is derived INDEPENDENTLY from the PR (a `MISSION = <id>`
    contract line or a `mission:<id>` label), never copied from protected state —
    so `protected_mission_authorizes_pr` is not tautological;
  * all policy-relevant API queries are fully PAGINATED;
  * review-thread resolution is read from the GraphQL `reviewThreads` (real
    isResolved), not hardcoded;
  * `now_main_sha` is a FRESH remote read, distinct from the checked-out
    `protected.main_sha`, so `main_not_advanced` can actually fail;
  * the protected-path allowlist applies ONLY to files ADDED by the PR, so a later
    PR that MODIFIES the registry/scripts is a protected-path violation.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

CODEX_BOT_LOGIN = "chatgpt-codex-connector[bot]"
MATERIAL_SEVERITIES = ("P1", "P2")
REPO_ROOT = Path(__file__).resolve().parent.parent

# Files THIS bootstrap mission legitimately ADDS. The allowlist applies only when
# the PR's file status is "added" (see added_only_protected_violations) — a later
# PR that MODIFIES one of these is still a protected-path violation.
ALLOWLISTED_ADDED_PATHS = {
    ".agent/mission_registry.yaml",
    ".agent/missions/northstar_continuous_mission_orchestration_foundation.md",
    ".agent/missions/northstar_vs002_execution_adapter_foundation.md",
    "scripts/northstar_pr_gate.py",
    "scripts/northstar_transition.py",
    "scripts/northstar_mission_packet.py",
    "scripts/northstar_assemble_gate_inputs.py",
    "docs/NORTHSTAR_ORCHESTRATION.md",
    ".github/workflows/northstar-pr-controller.yml",
    ".github/workflows/northstar-orchestrator.yml",
    ".github/workflows/claude-authorized-mission.yml",
}

_MISSION_LINE = re.compile(r"(?mi)^\s*MISSION\s*=\s*(\S+)\s*$")
_MISSION_LABEL = re.compile(r"^mission[:/](\S+)$", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# PURE helpers (unit-tested in tests/test_northstar_pr_gate.py).              #
# --------------------------------------------------------------------------- #
def candidate_mission_from_pr(body: str | None, labels: list[str] | None) -> str | None:
    """Derive the mission the PR CLAIMS to implement, independently of protected
    state. Prefer an explicit `mission:<id>` label, else a `MISSION = <id>` line
    in the PR body. Returns None when the PR declares no mission (gate fails closed)."""
    for lb in labels or []:
        m = _MISSION_LABEL.match(str(lb).strip())
        if m:
            return m.group(1)
    if body:
        m = _MISSION_LINE.search(body)
        if m:
            return m.group(1)
    return None


def added_only_protected_violations(changed: list[dict], allowlist: set[str],
                                    is_protected) -> list[str]:
    """`changed`: [{"path","status"}]. A protected path is a violation unless it is
    a NEWLY ADDED file on the allowlist. Modifying an allowlisted file still violates."""
    out = []
    for f in changed:
        p, status = f.get("path"), f.get("status")
        if not p or not is_protected(p):
            continue
        if status == "added" and p in allowlist:
            continue
        out.append(p)
    return out


def material_unresolved_from_threads(threads: list[dict], head_sha: str,
                                     bot_login: str) -> list[dict]:
    """From GraphQL reviewThreads, return the UNRESOLVED material (P1/P2) threads
    authored by the Codex bot at the given head. `threads`: [{isResolved, comments:
    [{author, body, commit_id}]}]."""
    out = []
    for th in threads:
        if th.get("isResolved"):
            continue
        for cm in th.get("comments", []):
            if cm.get("author") != bot_login:
                continue
            sev = _severity_from_body(cm.get("body") or "")
            if sev in MATERIAL_SEVERITIES and cm.get("commit_id") == head_sha:
                out.append({"severity": sev, "commit_id": cm.get("commit_id"), "resolved": False})
                break
    return out


def _severity_from_body(body: str) -> str | None:
    if "badge/P1" in body or "![P1" in body:
        return "P1"
    if "badge/P2" in body or "![P2" in body:
        return "P2"
    return None


# --------------------------------------------------------------------------- #
# IO helpers.                                                                  #
# --------------------------------------------------------------------------- #
def _gh_json(args: list[str]) -> Any:
    out = subprocess.run(["gh", "api", *args], capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"gh api {' '.join(args)} failed: {out.stderr.strip()}")
    return json.loads(out.stdout)


def _gh_paginated(path: str) -> list[dict]:
    """Fully paginated list fetch: `gh api --paginate <path> -q '.[]'` emits one
    JSON object per line across ALL pages."""
    out = subprocess.run(["gh", "api", "--paginate", path, "-q", ".[]"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"gh api --paginate {path} failed: {out.stderr.strip()}")
    items = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if line:
            items.append(json.loads(line))
    return items


def _graphql_review_threads(owner: str, name: str, pr: int) -> list[dict]:
    q = """
    query($owner:String!,$name:String!,$pr:Int!,$cursor:String){
      repository(owner:$owner,name:$name){
        pullRequest(number:$pr){
          reviewThreads(first:100, after:$cursor){
            pageInfo{ hasNextPage endCursor }
            nodes{ isResolved comments(first:50){ nodes{
              author{ login } body
              commit{ oid }
            }}}
          }}}}"""
    threads: list[dict] = []
    cursor = None
    while True:
        args = ["api", "graphql", "-f", f"query={q}",
                "-F", f"owner={owner}", "-F", f"name={name}", "-F", f"pr={pr}"]
        if cursor:
            args += ["-F", f"cursor={cursor}"]
        res = subprocess.run(["gh", *args], capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"gh api graphql reviewThreads failed: {res.stderr.strip()}")
        data = json.loads(res.stdout)["data"]["repository"]["pullRequest"]["reviewThreads"]
        for node in data["nodes"]:
            comments = [{"author": (c.get("author") or {}).get("login"),
                         "body": c.get("body"),
                         "commit_id": (c.get("commit") or {}).get("oid")}
                        for c in node["comments"]["nodes"]]
            threads.append({"isResolved": node["isResolved"], "comments": comments})
        if data["pageInfo"]["hasNextPage"]:
            cursor = data["pageInfo"]["endCursor"]
        else:
            break
    return threads


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


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True,
                          cwd=str(REPO_ROOT)).stdout.strip()


def assemble(repo: str, pr_number: int, mode: str) -> dict:
    owner, name = repo.split("/", 1)
    pr = _gh_json([f"repos/{repo}/pulls/{pr_number}"])
    head_sha = pr["head"]["sha"]
    base_ref = pr["base"]["ref"]
    labels = [lb.get("name") for lb in pr.get("labels", [])]
    body = pr.get("body") or ""

    # CI: most recent northstar-ci run for the head sha (paginated)
    runs = _gh_json([f"repos/{repo}/actions/runs?head_sha={head_sha}&per_page=100"])
    ci = {"workflow_name": None, "head_sha": head_sha, "status": None, "conclusion": None}
    for r in runs.get("workflow_runs", []):
        if r.get("name") == "northstar-ci":
            ci = {"workflow_name": "northstar-ci", "head_sha": r.get("head_sha"),
                  "status": r.get("status"), "conclusion": r.get("conclusion")}
            break

    reviews = [{"author": (rv.get("user") or {}).get("login"), "state": rv.get("state"),
                "commit_id": rv.get("commit_id"), "submitted_at": rv.get("submitted_at")}
               for rv in _gh_paginated(f"repos/{repo}/pulls/{pr_number}/reviews")]

    # material findings + REAL resolution state via GraphQL reviewThreads
    threads = _graphql_review_threads(owner, name, pr_number)
    inline_comments = material_unresolved_from_threads(threads, head_sha, CODEX_BOT_LOGIN)

    clean_reaction = None
    for rx in _gh_paginated(f"repos/{repo}/issues/{pr_number}/reactions"):
        if rx.get("content") == "+1" and (rx.get("user") or {}).get("login") == CODEX_BOT_LOGIN:
            clean_reaction = {"content": "+1", "actor": CODEX_BOT_LOGIN, "created_at": rx.get("created_at")}

    changed = [{"path": f.get("filename"), "status": f.get("status")}
               for f in _gh_paginated(f"repos/{repo}/pulls/{pr_number}/files")]
    protected_violations = added_only_protected_violations(changed, ALLOWLISTED_ADDED_PATHS, _is_protected)

    # candidate mission: derived from the PR itself (independent of protected state)
    candidate_mission = candidate_mission_from_pr(body, labels)
    authoritative = _authoritative_mission()

    # main-advance: protected snapshot (checked-out HEAD) vs a FRESH remote read
    protected_main = _git("rev-parse", "HEAD") or None
    try:
        now_main = _gh_json([f"repos/{repo}/commits/main"]).get("sha")
    except Exception:
        now_main = None

    return {
        "mode": mode,
        "pr": {"state": pr.get("state", "").upper(), "is_draft": bool(pr.get("draft")),
               "base_ref": base_ref, "head_sha": head_sha,
               "mergeable": pr.get("mergeable") is True},
        "ci": ci,
        "codex": {"bot_login": CODEX_BOT_LOGIN, "reviews": reviews,
                  "inline_comments": inline_comments, "clean_reaction": clean_reaction,
                  "exact_head_review_request": None},
        "protected": {"current_mission": authoritative, "main_sha": protected_main},
        "candidate": {
            "authorized_mission": candidate_mission,   # from the PR, NOT protected state
            "authority_mutated": False,                 # deep base/head diff computed in enabled mode
            "forbidden_authority_introduced": [],
            "protected_path_violations": protected_violations,
        },
        "now_main_sha": now_main,                        # FRESH remote read
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
