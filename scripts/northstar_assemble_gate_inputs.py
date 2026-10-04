#!/usr/bin/env python3
"""Assemble NORTHSTAR deterministic-assurance-gate inputs from the read-only API.

Usage:
    python scripts/northstar_assemble_gate_inputs.py --repo owner/name --pr N \
        --mode shadow --out inputs.json

IO GLUE (not the deterministic core): gathers PR / CI metadata, base-main protected
state, and the candidate's changed files, then writes the inputs consumed by the
unit-tested `northstar_pr_gate.evaluate_merge_gate`. NO AI review (Codex) is fetched
or required — Codex is advisory-only and never gated on. The decidable logic lives in
pure, tested helpers (reconcile_authority, the gate's evaluate_change_envelope /
check_control_plane_conformance). Fails SOFT (nonzero exit) so the controller treats
assembly failure as fail-closed in enabled mode.

Authority precedence (fail closed): protected base main > registry routing > the
candidate's declaration. The candidate's claimed mission is derived INDEPENDENTLY from
the PR; its change envelope comes ONLY from the protected base-main registry.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

_MISSION_LINE = re.compile(r"(?mi)^\s*MISSION\s*=\s*(\S+)\s*$")
_MISSION_LABEL = re.compile(r"^mission[:/](\S+)$", re.IGNORECASE)

# required_test_group -> a repo-relative glob that must match at least one collected
# test file (the assurance suite must EXIST; exact-head northstar-ci success proves it ran).
TEST_GROUP_GLOBS = {
    "orchestration": "tests/test_northstar_*.py",
    "authority": "tests/test_northstar_authority.py",
    "workflow_conformance": "tests/test_northstar_orchestration_workflows.py",
    "vs002": "tests/test_vs002_*.py",
}


# --------------------------------------------------------------------------- #
# PURE helpers (unit-tested in tests/test_northstar_pr_gate.py).              #
# --------------------------------------------------------------------------- #
def candidate_mission_from_pr(body: str | None, labels: list[str] | None) -> str | None:
    """Derive the mission the PR CLAIMS to implement, independently of protected
    state (a `mission:<id>` label or a `MISSION = <id>` body line). None = undeclared."""
    for lb in labels or []:
        m = _MISSION_LABEL.match(str(lb).strip())
        if m:
            return m.group(1)
    if body:
        m = _MISSION_LINE.search(body)
        if m:
            return m.group(1)
    return None


def reconcile_authority(project_state: dict, phase_status: dict, ew0a: dict) -> str | None:
    """Return the single mission all protected sources agree on, else None (fail
    closed). Sources: project_state.current_step, phase_status
    engineer_runtime_state.mission_id, phases.northstar_phase_0c.step, ew0a mission_id."""
    try:
        rd = (phase_status or {}).get("stockbot_northstar_redesign", {}) or {}
        vals = [
            (project_state or {}).get("current_step"),
            (rd.get("engineer_runtime_state", {}) or {}).get("mission_id"),
            ((rd.get("phases", {}) or {}).get("northstar_phase_0c", {}) or {}).get("step"),
            (ew0a or {}).get("mission_id"),
        ]
    except Exception:
        return None
    if any(not v for v in vals):
        return None
    return vals[0] if len(set(vals)) == 1 else None


# --------------------------------------------------------------------------- #
# IO helpers.                                                                  #
# --------------------------------------------------------------------------- #
def _gh_json(args: list[str]) -> Any:
    out = subprocess.run(["gh", "api", *args], capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"gh api {' '.join(args)} failed: {out.stderr.strip()}")
    return json.loads(out.stdout)


def _gh_paginated(path: str) -> list[dict]:
    out = subprocess.run(["gh", "api", "--paginate", path, "-q", ".[]"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"gh api --paginate {path} failed: {out.stderr.strip()}")
    return [json.loads(l) for l in out.stdout.splitlines() if l.strip()]


def _head_file(repo: str, path: str, ref: str) -> str | None:
    import base64
    try:
        d = _gh_json([f"repos/{repo}/contents/{path}?ref={ref}"])
        if d.get("encoding") == "base64":
            return base64.b64decode(d["content"]).decode("utf-8")
    except Exception:
        return None
    return None


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True,
                          cwd=str(REPO_ROOT)).stdout.strip()


def _load_sibling(mod_name: str):
    spec = importlib.util.spec_from_file_location(mod_name, REPO_ROOT / "scripts" / f"{mod_name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _reconciled_authoritative() -> str | None:
    try:
        import yaml
        ps = yaml.safe_load((REPO_ROOT / ".agent/project_state.yaml").read_text(encoding="utf-8"))
        ph = yaml.safe_load((REPO_ROOT / ".agent/phase_status.yaml").read_text(encoding="utf-8"))
        ew = json.loads((REPO_ROOT / "config/ew0a_runtime.json").read_text(encoding="utf-8"))
    except Exception:
        return None
    reconciled = reconcile_authority(ps, ph, ew)
    try:
        from portfolio_automation.engineer_worker.roadmap_guard import RoadmapAuthorization
        auth = RoadmapAuthorization.read(REPO_ROOT)
        guard = auth.authorized_mission_id if auth.authoritative else None
    except Exception:
        guard = None
    return reconciled if (reconciled and guard and reconciled == guard) else None


def _mission_envelope(mission: str) -> dict:
    """The DEFAULT-DENY change envelope from the PROTECTED base-main registry."""
    try:
        import yaml
        reg = yaml.safe_load((REPO_ROOT / ".agent/mission_registry.yaml").read_text(encoding="utf-8"))
        return ((reg.get("missions") or {}).get(mission or "", {}) or {}).get("change_policy") or {}
    except Exception:
        return {}


def _forbidden_authority(repo: str, head_sha: str) -> list[str]:
    """auto_* flag flips / C1 enabled / Phase 0D advanced, base main vs PR head."""
    gate = _load_sibling("northstar_pr_gate")
    out: list[str] = []
    try:
        base_ew = json.loads((REPO_ROOT / "config/ew0a_runtime.json").read_text(encoding="utf-8"))
        ht = _head_file(repo, "config/ew0a_runtime.json", head_sha)
        head_ew = json.loads(ht) if ht else base_ew
        for f in gate.AUTO_FLAGS:
            if head_ew.get(f) and not base_ew.get(f):
                out.append(f)
        if head_ew.get("c1") == "ENABLED" and base_ew.get("c1") != "ENABLED":
            out.append("c1_enabled")
        import yaml
        def _p0d(ph):
            try:
                return ph["stockbot_northstar_redesign"]["phases"]["northstar_phase_0d"]["status"]
            except Exception:
                return None
        base_ph = yaml.safe_load((REPO_ROOT / ".agent/phase_status.yaml").read_text(encoding="utf-8"))
        pt = _head_file(repo, ".agent/phase_status.yaml", head_sha)
        head_ph = yaml.safe_load(pt) if pt else base_ph
        if _p0d(head_ph) == "active" and _p0d(base_ph) != "active":
            out.append("phase_0d_advanced")
    except Exception:
        return out
    return out


def _missing_test_groups(required: list[str]) -> list[str]:
    missing = []
    for g in required or []:
        glob = TEST_GROUP_GLOBS.get(g)
        if not glob or not list(REPO_ROOT.glob(glob)):
            missing.append(g)
    return missing


def _transition_exact_match(repo: str, pr_number: int, head_sha: str) -> bool:
    """True iff the PR diff EXACTLY equals a re-derived deterministic transition
    materialized from current (base) main. Fail closed on any error."""
    try:
        import yaml
        trans = _load_sibling("northstar_transition")
        mat = _load_sibling("northstar_materialize_transition")
        reg = yaml.safe_load((REPO_ROOT / ".agent/mission_registry.yaml").read_text(encoding="utf-8"))
        ph = yaml.safe_load((REPO_ROOT / ".agent/phase_status.yaml").read_text(encoding="utf-8"))
        paused = (ph["stockbot_northstar_redesign"]["phases"]["northstar_phase_0c"]
                  ["bounded_authorization"].get("paused_bounded_authorization"))
        mission = _reconciled_authoritative()
        cur = _git("rev-parse", "HEAD")
        res = trans.propose_transition({
            "mode": "enabled", "completed_mission": mission, "authoritative_mission": mission,
            "completed_pr_mission": mission, "registry": reg, "paused_authorization": paused,
            "certified_main_sha": cur, "protected_main_sha": cur,
            "post_merge": {"conclusion": "success", "event": "push", "head_branch": "main", "head_sha": cur},
        })
        if res.get("decision") != "PROPOSE":
            return False
        proposal = res["proposal"]
        rels = {k.split("#", 1)[0] for k in proposal["allowlisted_field_edits"]}
        rels.add(".agent/phase_status.yaml")
        base_files = {rel: (REPO_ROOT / rel).read_text(encoding="utf-8") for rel in rels}
        result = mat.materialize_edits(base_files, proposal)
        expected, expected_changed = result["files"], set(result["changed"])
        changed = {f.get("filename") for f in _gh_paginated(f"repos/{repo}/pulls/{pr_number}/files")}
        if not expected_changed or changed != expected_changed:
            return False
        for rel in changed:
            ht = _head_file(repo, rel, head_sha)
            if ht is None or ht != expected[rel]:
                return False
        return True
    except Exception:
        return False


def assemble(repo: str, pr_number: int, mode: str) -> dict:
    gate = _load_sibling("northstar_pr_gate")
    pr = _gh_json([f"repos/{repo}/pulls/{pr_number}"])
    head_sha = pr["head"]["sha"]
    base_ref = pr["base"]["ref"]
    head_ref = (pr.get("head") or {}).get("ref") or ""
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

    changed = [f.get("filename") for f in _gh_paginated(f"repos/{repo}/pulls/{pr_number}/files")]

    candidate_mission = candidate_mission_from_pr(body, labels)
    authoritative = _reconciled_authoritative()

    # deterministic-transition lane (the controller's own governance PR)
    is_transition = head_ref.startswith("governance/transition-")
    envelope = _mission_envelope(authoritative or "")
    declared_class = (envelope.get("class") or "normal")
    change_class = "governance_transition" if is_transition else declared_class

    change_envelope_violations: list[str] = []
    control_plane_authorized = False
    conformance_violations: list[str] = []
    transition_exact = False
    if change_class == "governance_transition":
        transition_exact = _transition_exact_match(repo, pr_number, head_sha)
    else:
        change_envelope_violations = gate.evaluate_change_envelope(changed, envelope)
        if change_class == "control_plane":
            control_plane_authorized = (declared_class == "control_plane")
            # fetch the candidate's own controller files at head and check conformance
            cand_files = {}
            for p in (".github/workflows/northstar-pr-controller.yml",
                      ".github/workflows/northstar-orchestrator.yml",
                      ".github/workflows/claude-authorized-mission.yml",
                      ".agent/mission_registry.yaml"):
                t = _head_file(repo, p, head_sha)
                if t is not None:
                    cand_files[p] = t
            conformance_violations = gate.check_control_plane_conformance(cand_files)

    missing_groups = _missing_test_groups(envelope.get("required_test_groups") or [])

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
        "protected": {"current_mission": authoritative, "main_sha": protected_main},
        "candidate": {
            "authorized_mission": candidate_mission,
            "change_class": change_class,
            "change_envelope_violations": change_envelope_violations,
            "control_plane_authorized": control_plane_authorized,
            "conformance_violations": conformance_violations,
            "is_deterministic_transition": is_transition,
            "transition_exact_match": transition_exact,
            "forbidden_authority_introduced": _forbidden_authority(repo, head_sha),
            "missing_required_test_groups": missing_groups,
        },
        "now_main_sha": now_main,
        "advisory_codex": None,   # Codex is advisory-only and is NOT fetched or gated on
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
    except Exception as e:
        print(f"ERROR: gate-input assembly failed: {e}", file=sys.stderr)
        return 1
    Path(args.out).write_text(json.dumps(inputs, indent=2, sort_keys=True), encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
