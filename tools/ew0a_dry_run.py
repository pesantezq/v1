"""EW-0A supervised-autonomous DRY RUN (harmless synthetic tasks).

Persists the conservative first-phase runtime policy and demonstrates the bounded
loop: mission -> auto next-task -> E1/E2 Engineer dispatch -> bounded repair ->
E3 Claude escalation -> independent verification -> mission-boundary STOP. No
Northstar 0B work is performed. The supervisor is injected deterministically
(the LIVE GPT verifier was certified live in EW-0A); this dry run validates the
loop orchestration, not GPT itself.
"""
from __future__ import annotations
import json
import sys

REPO = "/home/pesan/stockbot-lab/repo/v1"
sys.path.insert(0, REPO)
from portfolio_automation.engineer_worker.ew0a import (             # noqa: E402
    RiskClass, Executor, TaskStatus, EngineeringTaskV0, AttemptEvidence)
from portfolio_automation.engineer_worker.ew0a_authority import EngineerAuthorityLevel as Lvl  # noqa: E402
from portfolio_automation.engineer_worker.ew0a_loop import (        # noqa: E402
    RuntimePolicy, run_mission, write_runtime_policy, read_runtime_policy)
from portfolio_automation.engineer_worker.durable_certification import ReviewContext
from portfolio_automation.engineer_worker.gpt_supervisor import SupervisorDecision, SupervisorVerdict  # noqa: E402

# The AUTHORITATIVE next 0B milestone (see .agent/phase_status.yaml): the
# prediction/research/experiment contracts are 'ready'; the decision/outcome/
# passport contracts ("0B.3") are 'not_started'. The loop is mission-scoped to the
# next authorized milestone; STARTING it still requires explicit operator approval.
MISSION = "northstar_0b_prediction_research_experiment_contracts"

policy = RuntimePolicy(mission_id=MISSION)
write_runtime_policy(REPO, policy)
print("wrote config/ew0a_runtime.json:", json.dumps({"mission_id": MISSION,
      "authority": policy.authority, "gpt_supervisor_required": policy.gpt_supervisor_required,
      "engineer_attempts": policy.engineer_attempts_per_task,
      "disabled_ok": policy.disabled_authorities_ok()}))

_i = [0]
def now():
    _i[0] += 1
    return f"2026-08-11T13:00:{_i[0]:02d}Z"
_v = [0]
def vid():
    _v[0] += 1
    return f"dv{_v[0]}"

def _att(t, n, ok):
    return AttemptEvidence(attempt_id=f"a{n}", executor=Executor.ENGINEER, worker_claim="done",
                           changed_paths=["tests/tx.py"], tests_run=["tests/tx.py"],
                           test_results={"tests/tx.py": "PASS" if ok else "FAIL"},
                           py_compile_ok=True, canonical_repo_touched=False)

# synthetic engineer: task-2 fails first then passes; others pass first try
def engineer(task, n):
    if task.task_id == "dry-2":
        return _att(task, n, ok=(n >= 2))
    if task.task_id == "dry-3":
        return _att(task, n, ok=False)   # E3 shouldn't even reach here (routed to Claude)
    return _att(task, n, ok=True)

def claude(task, v):
    return _att(task, 9, ok=True)        # Claude produces a correct candidate

def supervisor(packet):                  # injected deterministic verifier
    return SupervisorDecision(SupervisorVerdict.PASS)

def T(tid, risk):
    return EngineeringTaskV0(task_id=tid, title=tid, goal="synthetic", risk_class=risk,
                             executor=Executor.ENGINEER, mission_id=MISSION,
                             allowed_paths=["tests/"], allowed_tests=["tests/tx.py"],
                             acceptance_criteria=["passes"], max_attempts=2)

queue = [T("dry-1", RiskClass.E1_ROUTINE),          # E1 -> pass
         T("dry-2", RiskClass.E2_MODERATE),          # E2 -> repair then pass
         T("dry-3", RiskClass.E3_HIGH),              # E3 -> Claude escalation
         EngineeringTaskV0(task_id="OUT-OF-MISSION", title="x", goal="x",
                           risk_class=RiskClass.E1_ROUTINE, executor=Executor.ENGINEER,
                           mission_id="northstar_0b_decision_outcome_passport_contracts",  # 0B.3 - refused
                           allowed_paths=["tests/"], allowed_tests=["tests/tx.py"], max_attempts=2)]

CERTIFICATION = ReviewContext.open(
    REPO, mission_id=policy.mission_id, session_id="ew0a_dry_run",
    reviewer_identity={"provider": "stub", "model": "dry-run"})
rep = run_mission(policy, queue, Lvl.A1_ASSISTED_ENGINEERING, engineer, claude,
                  supervisor, now, vid, certification=CERTIFICATION)

print("\n== DRY RUN MISSION REPORT ==")
for t in rep.tasks_run:
    print(f"  {t['task_id']:8} route={t['route']:9} status={t['final_status']:20} "
          f"eng_attempts={t['engineer_attempts']} escalated={t['escalated']} claude={t['claude_attempts']}")
print(f"  verified={rep.verified} escalated={rep.escalated} human_required={rep.human_required}")
print(f"  STOP: {rep.stop_reason}")

ran = [t["task_id"] for t in rep.tasks_run]
ok = (ran == ["dry-1", "dry-2", "dry-3"]                       # auto next-task, in-mission only
      and "OUT-OF-MISSION" not in ran                          # mission boundary held
      and rep.verified == 3                                    # E1 pass, E2 repair->pass, E3 Claude
      and rep.tasks_run[1]["engineer_attempts"] == 2           # bounded repair happened
      and rep.tasks_run[2]["route"] == "CLAUDE"                # E3 escalated to Claude
      and rep.stop_reason.startswith("STOP_FOR_MISSION_REVIEW"))
print("\nDRY_RUN_OK:", ok)
sys.exit(0 if ok else 1)
