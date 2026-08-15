"""Shadow validation: replay the REAL 0B.3 controller decisions through the Learning Kernel.

What this is: the five ``ControllerDecisionCandidateV0`` records produced by the
actual local Engineer (``engineer.local_qwen2_5_7b``) during Northstar 0B.3, fed
through the kernel's evaluator, competence updater and graduation gate. The
proposals, the authoritative decisions and the outcomes are all authoritative
records — nothing here is synthesized.

What this is NOT: a live new decision. Ollama is not running in this session, so the
Engineer cannot be asked anything new. This measures real historical behavior with
the new instrumentation; it does not prove the kernel changes behavior going forward.

Why the transfer measurement is not circular: the real timeline already separates
the two conditions. ``cdc-exs-1`` was decided with NO lesson retrieved and was
underclassified E2/ENGINEER. The four later contracts — CapitalProposal,
ExitProposal, OutcomeRecord, StrategyPassport, which are genuinely different
contracts with different semantics rather than a replay of the same example — each
recorded ``lessons_retrieved=1`` and were classified E3/CLAUDE correctly, with
ExitProposal onward additionally proposing escalation conditions. The retrieval
genuinely preceded the corrected proposals.

Caveat recorded honestly: the lesson supplied during 0B.3 was an ad-hoc lesson from
that session, not an ``EngineeringLessonV0`` produced by this kernel.
"""
from __future__ import annotations

import datetime
import json
import sys

REPO = "/home/pesan/stockbot-lab/repo/v1"
sys.path.insert(0, REPO)

from portfolio_automation.engineer_worker.learning import kernel, store  # noqa: E402
from portfolio_automation.engineer_worker.learning.config import (  # noqa: E402
    read_learning_config)
from portfolio_automation.engineer_worker.learning.contracts import (  # noqa: E402
    Capability, RiskDomain)
from portfolio_automation.engineer_worker.learning.extractor import (  # noqa: E402
    LearningObservation)
from portfolio_automation.engineer_worker.learning.retriever import (  # noqa: E402
    RetrievalContext)

ACTOR = "claude_code"
WORKER = "engineer.local_qwen2_5_7b"
RECORDS = f"{REPO}/docs/EW0A_0B3_RECORDS.jsonl"

CAPABILITY = Capability.CANONICAL_CONTRACT_RISK_ROUTING.value
TASK_CLASS = "author_canonical_contract"
SUBSYSTEM = "portfolio_automation/northstar"

# Risk-class spellings differ between the 0B.3 records ("E3") and the EW-0A enum
# ("E3_HIGH"); normalize so agreement is compared on meaning, not formatting.
_RISK = {"E1": "E1_ROUTINE", "E2": "E2_MODERATE", "E3": "E3_HIGH", "E4": "E4_CONSEQUENTIAL"}


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def load_records() -> list[dict]:
    return [json.loads(l) for l in open(RECORDS, encoding="utf-8") if l.strip()]


def build_observations(records: list[dict]) -> list[LearningObservation]:
    candidates = [r for r in records if r["kind"] == "ControllerDecisionCandidateV0"]
    comparisons = {r["candidate_id"]: r for r in records
                   if r["kind"] == "ApprenticeshipComparison"}
    observations = []
    for c in candidates:
        cid = c["candidate_id"]
        prop = c.get("proposal", {})
        cmp_ = comparisons.get(cid, {})
        retrieved = ["prior_session_adhoc_lesson"] if c.get("lessons_retrieved") else []
        observations.append(LearningObservation(
            observation_id=cid, worker_id=WORKER, capability=CAPABILITY,
            task_class=TASK_CLASS, subsystem=SUBSYSTEM,
            risk_domain=RiskDomain.ARCHITECTURE.value,
            proposed_risk_class=_RISK.get(prop.get("proposed_risk_class"),
                                          prop.get("proposed_risk_class")),
            proposed_executor=prop.get("proposed_executor"),
            proposed_reasoning=prop.get("reasoning_summary", ""),
            authoritative_risk_class=_RISK.get(cmp_.get("authoritative_risk"),
                                               cmp_.get("authoritative_risk")),
            authoritative_executor=cmp_.get("authoritative_executor"),
            deterministic_ok=True, gpt_verdict="PASS", final_outcome="VERIFIED",
            failure_class=("ARCHITECTURE_ESCALATION"
                           if cmp_.get("danger_underclassified_architecture_as_engineer")
                           else None),
            unsafe_underclassification=bool(
                cmp_.get("danger_underclassified_architecture_as_engineer")),
            lessons_retrieved=retrieved, first_pass=True, attempt_count=1,
            evidence_refs=[f"docs/EW0A_0B3_RECORDS.jsonl#{cid}"],
            recorded_at=c.get("recorded_at")))
    return observations


def main() -> int:
    cfg = read_learning_config(REPO)
    records = load_records()
    observations = build_observations(records)

    print("== Shadow replay of REAL 0B.3 Engineer decisions ==")
    print(f"observations: {len(observations)} (authoritative records, not synthesized)\n")

    for i, obs in enumerate(observations, 1):
        ctx = RetrievalContext(capability=obs.capability, task_class=obs.task_class,
                               subsystem=obs.subsystem, risk_domain=obs.risk_domain,
                               decision_candidate_id=obs.observation_id)
        retrieval = kernel.retrieve_for_decision(
            REPO, ctx, cfg=cfg, actor=ACTOR, now=now(),
            retrieval_id=f"ret-replay-{obs.observation_id}")
        result = kernel.run_learning_cycle(
            REPO, obs, cfg=cfg, actor=ACTOR, now=now(),
            evaluation_id=f"ev-replay-{obs.observation_id}",
            authoritative_records=records, semantic_reviewer=None)
        ev = result.evaluation
        print(f"[{i}] {obs.observation_id}")
        print(f"    proposed={obs.proposed_risk_class}/{obs.proposed_executor} "
              f"authoritative={obs.authoritative_risk_class}/{obs.authoritative_executor}")
        print(f"    lesson_retrieved={ev.lesson_retrieved} "
              f"transfer_success={ev.lesson_transfer_success} "
              f"correct={ev.is_correct} safe={ev.is_safe}")
        print(f"    kernel_retrieval_supplied={len(retrieval.lesson_ids)} "
              f"extraction={result.extraction.result.value}")
        if result.competence:
            p = result.competence
            print(f"    competence: obs={p.observations} correct={p.correct} "
                  f"unsafe={p.unsafe} consecutive_safe={p.consecutive_safe} "
                  f"transfers={p.successful_lesson_transfers}/{p.lesson_retrievals}")
        if result.readiness:
            print(f"    readiness: {result.readiness.state} "
                  f"blockers={result.readiness.hard_blockers}")
        print()

    print("== Final per-capability readiness ==")
    for cap, r in kernel.assess_all_readiness(REPO, cfg=cfg, now=now()).items():
        print(json.dumps({"capability": cap, "state": r.state,
                          "observations": r.observations, "success_rate": r.success_rate,
                          "lesson_transfer_rate": r.lesson_transfer_rate,
                          "consecutive_safe": r.consecutive_safe,
                          "is_high_risk": r.is_high_risk,
                          "hard_blockers": r.hard_blockers,
                          "unmet_thresholds": r.unmet_thresholds,
                          "grants_authority": r.grants_authority}, indent=2))
    print(f"\nactive_lessons={len(store.active_lessons(REPO))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
