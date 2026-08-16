"""Independent review of the Northstar 0C activation.

This changes authoritative roadmap and controller state, so it is not
self-certifying. ONE review; the verdict is not rerolled; only transport failure
retries.

The reviewer is asked the question that actually matters for an ACTIVATION
mission: was permission recorded without any implementation or authority being
smuggled in alongside it?
"""
from __future__ import annotations

import datetime
import json
import subprocess
import sys
import time

REPO = "/home/pesan/stockbot-lab/repo/v1"
sys.path.insert(0, REPO)

from portfolio_automation.engineer_worker import supervisor_screen  # noqa: E402
from portfolio_automation.engineer_worker.gpt_supervisor import (  # noqa: E402
    SupervisorConfig, SupervisorVerdict, review)

KEY = "/home/pesan/.ew0a_openai_key"
RECORDS = f"{REPO}/docs/EW0A_0B_PHASE_CERTIFICATION.jsonl"
CFG = SupervisorConfig(key_file=KEY, model="gpt-4o", max_completion_tokens=1600)
MISSION = "northstar_0c_pit_evidence_gateway_research_store"


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def read(rel: str, limit: int = 30000) -> str:
    with open(f"{REPO}/{rel}", encoding="utf-8") as fh:
        return fh.read()[:limit]


def diff() -> str:
    out = subprocess.run(["git", "diff", "678c73e..HEAD"], cwd=REPO,
                         capture_output=True, text=True)
    return out.stdout[:45000]


PACKET = {
    "task": {"task_id": "Northstar-0C-Activation-Review",
             "mission_id": MISSION,
             "title": "Northstar 0C activation & controller pointer reconciliation",
             "risk_class": "E3", "executor": "CLAUDE",
             "base_sha": "678c73eff4a2e020e349c5a235694cb5111a61e6"},
    "operator_authorization": {
        "authorized_mission": MISSION,
        "granted": "start Phase 0C only",
        "explicitly_not_granted": [
            "Phase 0D or later phases", "capital-allocation changes",
            "portfolio/trading authority", "production deployment", "broker access",
            "vendor purchase or paid-data acquisition",
            "C1 activation or authority promotion"],
    },
    "requirements": [
        "the controller pointers (current_phase, current_step, "
        "next_official_step.primary) must name the AUTHORIZED mission, not a "
        "completed phase",
        "Phase 0B must remain complete with its certification history untouched",
        "Phase 0C must become the current authorized phase using EXISTING repository "
        "status vocabulary, inventing no new semantics",
        "if the repository distinguishes authorization from implementation start, that "
        "distinction must be PRESERVED rather than collapsed",
        "the runtime mission_id must equal the authorized mission and must continue to "
        "refuse tasks from other phases/missions",
        "no new authority may be granted: authority, engineering mode, concurrency and "
        "every disabled authority unchanged",
        "0D and later phases must remain not_started",
        "C1 must remain DISABLED and the Learning Kernel certification_candidate",
        "no vendor may be selected, integrated or purchased",
        "NO 0C implementation may be present — no EvidenceGateway, research store, PIT "
        "adapters or vendor integration",
    ],
    "acceptance_criteria": [
        "current_phase = northstar_phase_0c and current_step = the authorized mission",
        "next_official_step.primary = the authorized mission; prior_primary preserves "
        "the completed 0B step",
        "phase_status and project_state agree on 0C being active (no divergence between "
        "the two authoritative surfaces)",
        "0C status is `active` while implementation_started remains false",
        "config/ew0a_runtime.json mission_id equals the authorized mission with all "
        "disabled authorities still false",
        "an out-of-mission task is still refused by the real run_mission code path",
        "0B remains complete and its exit-gate evidence is unmodified",
        "the diff contains NO EvidenceGateway/research-store/PIT-adapter implementation",
    ],
    "verification_steps": [
        "deterministic: tests/test_northstar_authority.py, tests/test_ew0a_loop.py, "
        "tests/test_agent_context_check.py — 99 passed",
        "new guards: activation-is-not-implementation; pointers must not lag the phase "
        "map; runtime refuses an out-of-mission task through the real run_mission; "
        "scope excludes 0D/capital/portfolio/production/broker/C1; no vendor authority",
        "broad hermetic suite: 10,703 passed / 15 pre-existing failures; identical "
        "baseline node IDs -> NEW_RELEVANT_FAILURES=0",
        "scripts/agent_context_check.py now reports phase 0C and the 0C step",
    ],
    "allowed_paths": [".agent/", "config/", "docs/", "tests/"],
    "changed_files": [".agent/project_state.yaml", ".agent/phase_status.yaml",
                      "config/ew0a_runtime.json", "docs/NORTHSTAR_REDESIGN.md",
                      "docs/roadmap.md", "tests/test_northstar_authority.py"],
    "semantics_decision": (
        "0C is recorded as status `active` with implementation_started = FALSE. The "
        "repository already distinguishes authorization from construction, and this "
        "mission is explicitly forbidden from implementing 0C, so marking "
        "implementation_started true would assert work that was not done. `active` "
        "means authorized and current; the first implementation mission flips the flag. "
        "A test pins this so an activation can never quietly claim implementation."),
    "source_files": [
        {"path": "tests/test_northstar_authority.py",
         "content": read("tests/test_northstar_authority.py")},
    ],
    "diff": diff(),
    "tests_run": ["tests/test_northstar_authority.py", "tests/test_ew0a_loop.py",
                  "tests/test_agent_context_check.py"],
    "test_results": {"focused": "PASS (99 passed)",
                     "broad": "10,703 passed / 15 pre-existing failures, 0 new"},
    "py_compile_ok": True,
    "worker_claim": (
        "NORTHSTAR_0C_ACTIVATION_CANDIDATE — verify INDEPENDENTLY that this records "
        "permission to begin 0C and nothing more. Specifically: that no implementation "
        "was smuggled in, that no authority was widened, that the mission boundary still "
        "refuses foreign tasks, that 0B's completion and certification history are "
        "intact, and that marking 0C `active` while implementation_started stays false "
        "is an honest representation rather than a way to avoid or overstate work. If "
        "anything grants more than was authorized, return REPAIR or ESCALATE."),
}


def main() -> int:
    result = supervisor_screen.screen_packet(PACKET)
    if result.blocked:
        print("PREFLIGHT: packet refused by the supervisor screen — NOT sending:")
        for f in result.findings:
            print("   ", json.dumps(f.to_dict()))
        return 2
    print(f"PREFLIGHT: packet clean ({len(result.exempted)} structural exemptions)")

    decision = review(PACKET, CFG, now)
    for attempt in range(3):
        if decision.verdict is not SupervisorVerdict.SUPERVISOR_UNAVAILABLE:
            break
        print(f"  transport failure ({decision.error}); retry {attempt + 1}")
        time.sleep(2 * (attempt + 1))
        decision = review(PACKET, CFG, now)

    verified = decision.verdict is SupervisorVerdict.PASS
    with open(RECORDS, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "kind": "Northstar0CActivationReviewVerdict",
            "mission": MISSION, "candidate_id": "northstar-0c-activation-1",
            "base_sha": "678c73eff4a2e020e349c5a235694cb5111a61e6",
            "gpt_verdict": decision.verdict.value, "verified": verified,
            "gpt_reasons": decision.reasons[:10],
            "gpt_unresolved": decision.unresolved_requirements[:10],
            "model": decision.model, "error": decision.error,
            "protocol": "one review; verdict not rerolled; transport-only retry",
            "recorded_at": now()}) + "\n")

    print("\n== Northstar 0C activation — independent review ==")
    print(json.dumps({"verdict": decision.verdict.value, "model": decision.model,
                      "reasons": decision.reasons[:10],
                      "unresolved": decision.unresolved_requirements[:10],
                      "error": decision.error}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
