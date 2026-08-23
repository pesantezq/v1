"""Shared packet builder for G1 evaluation cases.

Extracted so the base corpus and the G1-completion expansion can both use it
without a circular import. It exists for exactly one reason: every case must be
presented to the supervisor in the SAME shape the production path produces. A
corpus that invented its own field names would measure the model's behaviour on
a prompt it never sees in production.

The keys mirror ``ew0a.build_supervisor_packet`` plus
``durable_certification.binding_envelope``.

``experimental_noncanonical``.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from portfolio_automation.engineer_worker.g1 import G1_SCHEMA_KIND

#: The narrow default. Most cases are ordinary bounded source tasks, and a
#: corpus that granted every path by default would let an unreachable case look
#: reachable -- which is exactly the defect this parameter exists to prevent.
DEFAULT_ALLOWED_PATHS = ("portfolio_automation/", "tests/")

_OK_CHECKS = {"protected_path_ok": True, "scope_ok": True, "policy_ok": True,
              "tests_ok": True, "canonical_repo_untouched": True}
_OK_EVIDENCE = {"evidence_sufficient": "YES", "refusals": [], "details": [],
                "checks": {"ACCEPTANCE_CRITERIA_PRESENT": "YES",
                           "CHANGED_PATHS_PRESENT": "YES",
                           "TESTS_RUN_PRESENT": "YES", "DIFF_PRESENT": "YES",
                           "CHANGED_PATHS_IN_DIFF": "YES",
                           "RESULTS_BACKED_BY_RUNS": "YES"}}


def packet(*, task_id: str, title: str, goal: str, requirements: Sequence[str],
           criteria: Sequence[str], changed: Sequence[str], diff: str,
           tests: Sequence[str], results: Mapping[str, str],
           worker_claim: str = "IMPLEMENTATION_COMPLETE",
           candidate_sha: str = "0" * 40,
           risk_class: str = "E2_MODERATE",
           allowed_paths: Optional[Sequence[str]] = None,
           verification_steps: Sequence[str] = ()) -> dict[str, Any]:
    """Build a packet with the EXACT keys the production path produces.

    ``deterministic_checks`` and ``evidence_sufficiency`` are reported clean
    because a G1 case is by definition one where the deterministic gates already
    passed and the supervisor genuinely has a semantic decision to make. A case
    that would have been refused before dispatch belongs to the excluded
    population, not to supervisor accuracy.

    THAT CLAIM IS NOT SELF-CERTIFYING ANY MORE. ``corpus.assert_all_cases_
    reachable`` re-derives it by running the REAL ``ew0a.deterministic_check``
    over each case, and two cases failed when that check was first written: one
    changed ``.github/workflows/ci.yml`` while declaring only
    ``portfolio_automation/`` and ``tests/``, and one changed
    ``portfolio_automation/scoring_util.py``, which the protected-path guard
    matches on the substring ``portfolio_automation/scoring``. Both had been
    asserting a clean scope gate that production would have refused, so their
    model observations were never valid members of the accuracy population.

    ``allowed_paths`` therefore takes an explicit override rather than widening
    the default. A case whose authorised task genuinely covers another surface
    declares that surface, one case at a time, where a reader can see it."""
    return {
        "schema_version": "engineering.ew0a_supervisor_packet.v1",
        "schema_kind": G1_SCHEMA_KIND,
        "task": {"task_id": task_id, "title": title, "goal": goal,
                 "risk_class": risk_class, "executor": "ENGINEER",
                 "session_id": "g1", "attempt_id": "a1"},
        "requirements": list(requirements),
        "acceptance_criteria": list(criteria),
        "verification_steps": list(verification_steps),
        "allowed_paths": list(allowed_paths if allowed_paths is not None
                             else DEFAULT_ALLOWED_PATHS),
        "changed_files": list(changed),
        "diff": diff,
        "tests_run": list(tests),
        "test_results": dict(results),
        "py_compile_ok": True,
        "deterministic_checks": dict(_OK_CHECKS),
        "evidence_sufficiency": dict(_OK_EVIDENCE),
        "worker_claim": worker_claim,
        "worker_abstained": False,
        "abstain_reason": None,
        "candidate_sha": candidate_sha,
        "mission_id": "g1_supervisor_measurement",
        "criteria": [{"criterion_id": f"AC{i}", "claim": c}
                     for i, c in enumerate(criteria)],
    }
