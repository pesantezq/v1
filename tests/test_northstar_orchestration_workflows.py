"""Static architecture/security tests for the Northstar orchestration workflows.

These prove the ENABLED-mode EFFECTOR architecture EXISTS and is safely GATED —
not merely that the decision cores return `would_*` booleans. They assert, by
parsing the committed workflow YAML: privilege separation (write permissions only
on effect jobs), every effect job is unreachable unless mode==enabled + the exact
decision, no pull_request_target, immutable-SHA-pinned actions, WIF for Claude, and
no path granting Claude merge/approve. Routed to `governance` via the prefix.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

try:
    import yaml
except ImportError:  # pragma: no cover
    pytest.skip("pyyaml not installed", allow_module_level=True)

REPO_ROOT = Path(__file__).resolve().parent.parent
WF = REPO_ROOT / ".github" / "workflows"
PR_CONTROLLER = WF / "northstar-pr-controller.yml"
ORCHESTRATOR = WF / "northstar-orchestrator.yml"
CLAUDE = WF / "claude-authorized-mission.yml"
ALL = [PR_CONTROLLER, ORCHESTRATOR, CLAUDE]

CLAUDE_ACTION_SHA = "cab360f6565aa35a51d6ce9e43f1f4287c0a32ea"


def _load_mod(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


gate = _load_mod("northstar_pr_gate")


def _load(p: Path) -> dict:
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def _jobs(p: Path) -> dict:
    return _load(p).get("jobs", {})


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ------------------------------- existence ---------------------------------- #
def test_all_three_workflows_exist():
    for p in ALL:
        assert p.is_file(), p


# ------------------------- no pull_request_target --------------------------- #
@pytest.mark.parametrize("p", ALL)
def test_no_pull_request_target(p):
    data = _load(p)
    on = data.get(True, data.get("on"))   # PyYAML parses the `on:` key as boolean True
    triggers = set(on.keys()) if isinstance(on, dict) else {on}
    assert "pull_request_target" not in triggers


# ----------------------- immutable SHA-pinned actions ----------------------- #
@pytest.mark.parametrize("p", ALL)
def test_third_party_actions_are_sha_pinned(p):
    uses = re.findall(r"uses:\s*([^\s#]+)", _text(p))
    assert uses, f"no actions in {p.name}"
    for u in uses:
        if u.startswith("./"):
            continue
        assert "@" in u, u
        ref = u.split("@", 1)[1]
        assert re.fullmatch(r"[0-9a-f]{40}", ref), f"{u} is not immutable-SHA pinned"


# --------------------------- privilege separation --------------------------- #
def _has_write(perms: dict | None) -> bool:
    if not perms:
        return False
    return any(v == "write" for k, v in perms.items() if k in ("contents", "pull-requests", "id-token"))


def test_pr_controller_privilege_separation():
    jobs = _jobs(PR_CONTROLLER)
    gate = jobs["northstar-merge-gate"]
    effect = jobs["auto-merge-effect"]
    # gate is read-only
    assert gate["permissions"].get("contents") == "read"
    assert gate["permissions"].get("pull-requests") == "read"
    assert not _has_write(gate["permissions"])
    # effect has pull-requests:write ONLY, needs the gate, gated on enabled+PASS
    assert effect["permissions"].get("pull-requests") == "write"
    assert effect["permissions"].get("contents") == "read"
    assert "northstar-merge-gate" in effect["needs"]
    assert "enabled" in effect["if"] and "PASS" in effect["if"]


def test_orchestrator_privilege_separation():
    jobs = _jobs(ORCHESTRATOR)
    certify = jobs["post-merge-certify"]
    effect = jobs["governance-pr-effect"]
    assert certify["permissions"].get("contents") == "read"
    assert not _has_write(certify["permissions"])
    assert effect["permissions"].get("contents") == "write"
    assert effect["permissions"].get("pull-requests") == "write"
    assert "post-merge-certify" in effect["needs"]
    assert "enabled" in effect["if"] and "PROPOSE" in effect["if"]


def test_claude_dispatch_privilege_separation():
    jobs = _jobs(CLAUDE)
    build = jobs["build-packet"]
    effect = jobs["claude-dispatch"]
    assert build["permissions"].get("contents") == "read"
    assert not _has_write(build["permissions"])
    assert effect["permissions"].get("contents") == "write"
    assert effect["permissions"].get("pull-requests") == "write"
    assert effect["permissions"].get("id-token") == "write"   # WIF
    assert "build-packet" in effect["needs"]
    assert "enabled" in effect["if"] and "DISPATCH" in effect["if"]


# --------------- every effect job is unreachable in shadow ------------------ #
def test_every_effect_job_requires_enabled_mode():
    effects = [(_jobs(PR_CONTROLLER)["auto-merge-effect"]),
               (_jobs(ORCHESTRATOR)["governance-pr-effect"]),
               (_jobs(CLAUDE)["claude-dispatch"])]
    for e in effects:
        assert "enabled" in e["if"], e["if"]


def test_top_level_permissions_are_read_only():
    # default token is read-only at workflow scope; write is granted per-effect-job only
    for p in ALL:
        top = _load(p).get("permissions")
        assert top == {"contents": "read"}, f"{p.name}: {top}"


# ------------------------------ Claude safety ------------------------------- #
def test_claude_action_pinned_and_wif():
    text = _text(CLAUDE)
    assert f"anthropics/claude-code-action@{CLAUDE_ACTION_SHA}" in text
    for v in ("ANTHROPIC_FEDERATION_RULE_ID", "ANTHROPIC_ORGANIZATION_ID", "ANTHROPIC_SERVICE_ACCOUNT_ID"):
        assert v in text, v
    # no long-lived Anthropic API/OAuth secret
    assert "ANTHROPIC_API_KEY" not in text
    assert "secrets.ANTHROPIC" not in text


def test_claude_tools_constrained_no_merge_or_gh():
    text = _text(CLAUDE)
    assert "claude_args" in text
    # the allowed-tools whitelist must not grant gh/merge/approve/admin tools
    m = re.search(r"--allowedTools([^\n]*)", text)
    assert m, "no --allowedTools whitelist"
    allow = m.group(1)
    for forbidden in ("Bash(gh", "merge", "approve", "Bash(*)", '"Bash"'):
        assert forbidden not in allow, f"{forbidden!r} must not be an allowed tool"


def test_claude_dispatch_fails_closed_without_wif():
    # the verify step must fail closed when WIF identifiers are absent
    text = _text(CLAUDE)
    assert "WIF configuration absent" in text
    assert "fail closed" in text.lower()


def test_no_workflow_grants_claude_merge_authority():
    text = _text(CLAUDE)
    # no step performs a merge/approve
    assert "pr merge" not in text
    assert "--auto" not in text
    assert "pull-request-review" not in text.lower() or "approve" not in text.lower()


# ------------------------- trusted-main-only effects ------------------------ #
def test_effect_jobs_checkout_trusted_main():
    # effect jobs that check out code must check out ref: main (trusted), not the PR head
    orch_effect = _text(ORCHESTRATOR)
    assert "ref: main" in orch_effect
    claude_text = _text(CLAUDE)
    assert "ref: main" in claude_text


def test_registry_controller_mode_is_shadow():
    reg = yaml.safe_load((REPO_ROOT / ".agent" / "mission_registry.yaml").read_text(encoding="utf-8"))
    assert reg["controller"]["mode"] == "shadow"


# ------------------- round-2 hardening (re-triggers / fail-closed) ----------- #
def test_pr_controller_has_terminal_state_retriggers():
    on = _load(PR_CONTROLLER)
    on = on.get(True, on.get("on"))
    # Codex terminal-state re-evaluation (review / comment / thread resolution).
    for ev in ("pull_request_review", "pull_request_review_comment",
               "pull_request_review_thread"):
        assert ev in on, f"missing re-trigger {ev}"
    # CI-completion is handled by in-job polling (a workflow_run check would attach to
    # main, not the PR head), so the gate waits for northstar-ci on the PR head.
    t = _text(PR_CONTROLLER)
    assert "Wait for northstar-ci" in t and "DEADLINE" in t
    assert "workflow_run" not in (on if isinstance(on, dict) else {})


def test_generated_prs_use_triggering_credential():
    # governance-PR and Claude-dispatch effect jobs must use a triggering credential
    # (not the default GITHUB_TOKEN, which does not trigger downstream CI).
    assert "secrets.NORTHSTAR_BOT_TOKEN" in _text(ORCHESTRATOR)
    assert "secrets.NORTHSTAR_BOT_TOKEN" in _text(CLAUDE)


def test_pr_controller_fails_closed_on_assembly_failure_in_enabled():
    t = _text(PR_CONTROLLER)
    assert "assembly failed in enabled mode" in t
    assert "fail closed" in t.lower()
    # the controller-mode read must also fail closed
    assert "cannot read controller mode" in t


def test_orchestrator_requires_real_ci_push_evidence():
    t = _text(ORCHESTRATOR)
    assert 'event=push&status=success' in t
    assert 'northstar-ci' in t
    assert "UNCERTIFIED" in t


def test_all_effect_steps_fail_fast():
    for p in ALL:
        assert "set -euo pipefail" in _text(p), f"{p.name} missing fail-fast in an effect step"


def test_claude_dispatch_requires_real_certification():
    t = _text(CLAUDE)
    assert "not post-merge-certified" in t or "UNCERTIFIED" in t


def test_claude_dispatch_dedup_is_mission_level_not_per_sha():
    t = _text(CLAUDE)
    assert 'DEDUP="auto-dispatch:${MISSION}"' in t
    # must NOT dedup by the changing certified SHA (that would re-dispatch per commit)
    assert "${CERTIFIED_SHA:0:12}" not in t


def test_github_tree_fully_protected():
    # the whole .github/ tree is controller-protected (not just northstar-* workflows)
    assert gate._is_protected(".github/workflows/claude-authorized-mission.yml")
    assert gate._is_protected(".github/workflows/northstar-ci.yml")
    assert gate._is_protected(".github/workflows/anything-new.yml")


def test_forbidden_semantic_files_protected():
    for f in ("recommendations.py", "allocation_engine.py", "decision_engine.py"):
        assert gate._is_protected(f"portfolio_automation/{f}") or gate._is_protected(f)
