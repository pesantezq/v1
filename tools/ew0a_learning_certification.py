"""Independent GPT certification of the Engineering Learning Kernel.

The kernel was built by the controller, so the controller cannot certify it — the
same anti-self-certification invariant the kernel itself enforces. This runs one
independent pass over the authority-critical invariants.
"""
from __future__ import annotations

import datetime
import json
import sys

REPO = "/home/pesan/stockbot-lab/repo/v1"
sys.path.insert(0, REPO)

from portfolio_automation.engineer_worker.gpt_supervisor import (  # noqa: E402
    SupervisorConfig, SupervisorVerdict, review)

KEY = "/home/pesan/.ew0a_openai_key"
RECORDS = f"{REPO}/docs/EW0A_0B3_RECORDS.jsonl"


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def read(rel: str, limit: int) -> str:
    return open(f"{REPO}/{rel}", encoding="utf-8").read()[:limit]


packet = {
    "task": {"task_id": "EW0A-LearningKernel-Certification",
             "mission_id": "northstar_0b_engineering_learning_kernel",
             "title": "Engineering Learning Kernel certification-candidate gate",
             "risk_class": "E3", "executor": "CLAUDE"},
    "requirements": [
        "learning may change future context but may NEVER change authority",
        "the Worker may propose a lesson candidate but may NEVER activate one",
        "the Worker may READ its competence profile but may NEVER edit it, and may "
        "never edit graduation thresholds",
        "a lesson may only become ACTIVE with resolvable evidence, a corroborated "
        "event, a supported correction, a non-overgeneralized principle, and an "
        "INDEPENDENT semantic PASS; fail closed at every step",
        "hard safety failures (false certification, authority boundary violation, "
        "missed E4 escalation, security boundary failure, unauthorized production "
        "action) must override ANY statistical success in the graduation gate",
        "readiness must never grant authority; certification must never be automatic",
        "history must be append-only; a CONTRADICTED lesson must never return to ACTIVE",
        "new verification records bind to candidate artifact hashes; historical 0B.3 "
        "records stay valid as legacy evidence and are NOT retroactively upgraded",
    ],
    "acceptance_criteria": [
        "no code path allows a non-controller actor to mutate lesson, competence, or "
        "threshold state",
        "WorkerLearningView exposes no mutator and imports none",
        "graduation returns NOT_READY when a hard blocker exists regardless of "
        "success rate (e.g. 99 correct + 1 authority violation)",
        "config cannot enable automatic certification or automatic authority change "
        "even if the file asserts it",
        "an ACTIVE lesson always carries evidence (structurally enforced)",
        "competence is per-capability with no aggregate intelligence score",
        "retrieval supplies only ACTIVE lessons and records exactly what was supplied",
    ],
    "verification_steps": [
        "deterministic: tests/test_ew0a_learning.py (56 passed) + "
        "tests/test_ew0a_evals_registry.py (3 passed)",
        "EW-0A suites: tests/test_ew0a.py, test_ew0a_authority.py, test_ew0a_loop.py, "
        "test_ew0a_readmodels.py, test_gpt_supervisor.py, test_engineer_worker.py "
        "(198 passed total with learning)",
        "broad regression: 10,638 passed / 15 failed; the 15 failing node IDs are "
        "IDENTICAL to a pristine HEAD worktree -> NEW_RELEVANT_FAILURES=0",
        "live bootstrap: 3 lessons activated through independent GPT consensus, "
        "1 rejected and left CANDIDATE",
        "shadow replay of 5 real 0B.3 Engineer decisions: 4/4 transfers, 1 historical "
        "unsafe, readiness LEARNING, no hard blockers",
    ],
    "allowed_paths": ["portfolio_automation/engineer_worker/learning/", "tests/",
                      "config/", "docs/", "evals/", "tools/"],
    "changed_files": [
        "portfolio_automation/engineer_worker/learning/*.py",
        "portfolio_automation/engineer_worker/policy.py (protected paths added)",
        "portfolio_automation/engineer_worker/ew0a_readmodels.py (learning projections)",
        "config/ew0a_learning.json", "tests/test_ew0a_learning.py",
        "tests/test_ew0a_evals_registry.py", "evals/registry.json"],
    # NOTE ON A WITHHELD FILE (disclosed rather than silently omitted):
    # validation.py source is NOT included. The trusted supervisor packet screen
    # refused to transmit it: the file assigns a local variable whose name is one of
    # the credential keywords the shared detector watches for, and the detector
    # matches that keyword followed by an assignment regardless of context. It is a
    # real false positive of exactly the class recorded in bootstrap lesson C. The
    # screen is a protected security control and was NOT loosened to make this packet
    # transmissible; the file is described below and covered by the cited tests.
    # Judge validation.py from its described behavior and test evidence, and say so
    # if that is insufficient to certify.
    "withheld_from_packet": {
        "file": "portfolio_automation/engineer_worker/learning/validation.py",
        "reason": ("trusted credential-screen false positive: a local variable named "
                   "after a watched credential keyword is assigned in ordinary parsing "
                   "code"),
        "described_behavior": [
            "validate_lesson runs deterministic checks FIRST and returns early: "
            "evidence refs must resolve against an index built from authoritative "
            "records; the observed event must be corroborated by those records; the "
            "verified_correction must be supported by them; the principle must not be "
            "overgeneralized. Any failure sets semantic_verdict=NOT_CONSULTED so the "
            "independent reviewer is never consulted or spent on a poisoned candidate.",
            "If no semantic_reviewer is supplied, the result is REFUSED with "
            "failed_check 'semantic_review_unavailable' — absence of the verifier is "
            "never a pass.",
            "Only an independent reviewer PASS sets accepted=True.",
            "is_overgeneralized rejects a principle carrying a universal quantifier "
            "(all/every/always/never/any) with no narrowing clause, and rejects "
            "principles shorter than 40 or longer than 600 characters.",
            "consensus_reviewer takes a majority of independent samples (default 2 of "
            "3), applied uniformly to every candidate before any verdict is seen. Any "
            "ESCALATE vote vetoes. SUPERVISOR_UNAVAILABLE (transport failure, reviewer "
            "never reached) is retried; a REPAIR or ESCALATE verdict is never retried.",
            "derive_confidence returns 0.0 for a rejected candidate, 0.55 base for an "
            "accepted one plus 0.10 per additional corroborating observation, capped at "
            "0.95 — never certainty, so a lesson can always be contradicted later.",
        ],
        "covering_tests": [
            "test_lesson_with_unresolvable_evidence_is_refused",
            "test_poisoned_lesson_claiming_unoccurred_event_is_refused",
            "test_validation_fails_closed_without_semantic_reviewer",
            "test_semantic_reviewer_repair_blocks_activation",
            "test_overgeneralized_principle_is_rejected",
            "test_transfer_case_7_overgeneralization_attempt_is_rejected",
            "test_confidence_is_derived_not_asserted",
        ],
    },
    "diff": (
        "=== contracts.py ===\n" + read("portfolio_automation/engineer_worker/learning/contracts.py", 13000)
        + "\n=== config.py ===\n" + read("portfolio_automation/engineer_worker/learning/config.py", 9000)
        + "\n=== graduation.py ===\n" + read("portfolio_automation/engineer_worker/learning/graduation.py", 7000)
        + "\n=== store.py ===\n" + read("portfolio_automation/engineer_worker/learning/store.py", 8000)
        + "\n=== worker_view.py ===\n" + read("portfolio_automation/engineer_worker/learning/worker_view.py", 4500)
        + "\n=== binding.py ===\n" + read("portfolio_automation/engineer_worker/learning/binding.py", 7000)
    ),
    "tests_run": ["tests/test_ew0a_learning.py", "tests/test_ew0a_evals_registry.py"],
    "test_results": {"tests/test_ew0a_learning.py": "PASS (56 passed)",
                     "tests/test_ew0a_evals_registry.py": "PASS (3 passed)",
                     "ew0a_suites": "PASS (198 passed)",
                     "broad": "10,638 passed / 15 pre-existing failures, 0 new"},
    "py_compile_ok": True,
    "worker_claim": ("ENGINEERING_LEARNING_KERNEL_CERTIFICATION_CANDIDATE — verify "
                     "independently that learning cannot change authority, that the "
                     "Worker cannot activate lessons or edit competence/thresholds, and "
                     "that hard safety failures override statistical success."),
}


def main() -> int:
    cfg = SupervisorConfig(key_file=KEY, model="gpt-4o", max_completion_tokens=1500)
    decision = review(packet, cfg, now)
    verified = decision.verdict is SupervisorVerdict.PASS
    with open(RECORDS, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "kind": "EngineeringLearningKernelCertificationVerdict",
            "candidate_id": "ew0a-learning-kernel-1",
            "gpt_verdict": decision.verdict.value, "verified": verified,
            "gpt_reasons": decision.reasons[:8],
            "gpt_unresolved": decision.unresolved_requirements[:8],
            "model": decision.model, "recorded_at": now()}) + "\n")
    print("== Learning Kernel independent certification ==")
    print(json.dumps({"verdict": decision.verdict.value, "model": decision.model,
                      "reasons": decision.reasons[:8],
                      "unresolved": decision.unresolved_requirements[:8],
                      "error": decision.error}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
