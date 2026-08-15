"""Northstar 0B.3 — final graph-level independent GPT certification.

All six contracts already passed per-contract deterministic + GPT verification.
This runs ONE final independent GPT pass over the cross-contract separation graph
(the certification-level 'live independent GPT verification PASS' gate), verifying
the architecture-law invariants hold across the whole milestone-3 family.
"""
from __future__ import annotations
import datetime, json, sys

REPO = "/home/pesan/stockbot-lab/repo/v1"
sys.path.insert(0, REPO)
from portfolio_automation.engineer_worker.gpt_supervisor import review, SupervisorConfig, SupervisorVerdict  # noqa: E402

KEY = "/home/pesan/.ew0a_openai_key"
RECORDS = f"{REPO}/docs/EW0A_0B3_RECORDS.jsonl"


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


cross = open(f"{REPO}/tests/test_northstar_0b3_cross_contract.py", encoding="utf-8").read()
packet = {
    "task": {"task_id": "0B3-Certification", "mission_id": "northstar_0b_decision_outcome_passport_contracts",
             "title": "Northstar 0B.3 cross-contract certification", "risk_class": "E3", "executor": "CLAUDE"},
    "requirements": [
        "six canonical milestone-3 contracts exist: ExperimentSpec(exs_), ExperimentResult(exr_), "
        "CapitalProposal(cap_), ExitProposal(xit_), OutcomeRecord(out_), StrategyPassport(spp_)",
        "the cross-contract test builds the full graph from one evidence root and proves separation invariants"],
    "acceptance_criteria": [
        "PredictionRecord cannot become CapitalProposal (reference, not inheritance)",
        "CapitalProposal cannot mutate PredictionRecord and cannot execute a capital action",
        "ExitProposal cannot execute an exit (order keys rejected)",
        "ExperimentResult cannot rewrite ExperimentSpec (holds no spec fields)",
        "OutcomeRecord cannot rewrite earlier predictions/proposals and never collapses attribution into one score",
        "StrategyPassport cannot grant itself production/capital authority",
        "no contract smuggles authority through arbitrary payload keys",
        "identity round-trips reproduce for all six; provenance is never identity-bearing; PIT bounds present"],
    "verification_steps": ["deterministic: tests/test_northstar_0b3_cross_contract.py (12 passed) + 6 per-contract suites",
                           "broad regression: 15 pre-existing failures only, NEW_RELEVANT_FAILURES=0"],
    "allowed_paths": ["portfolio_automation/northstar/", "tests/"],
    "changed_files": ["tests/test_northstar_0b3_cross_contract.py"],
    "diff": "CROSS-CONTRACT CERTIFICATION TEST:\n" + cross[:56000],
    "tests_run": ["tests/test_northstar_0b3_cross_contract.py"],
    "test_results": {"tests/test_northstar_0b3_cross_contract.py": "PASS (12 passed)",
                     "six_per_contract_suites": "PASS", "broad": "15 pre-existing failures, 0 new"},
    "py_compile_ok": True,
    "worker_claim": "NORTHSTAR_0B3_CERTIFICATION_CANDIDATE — independently verify the separation invariants hold across the whole graph.",
}
cfg = SupervisorConfig(key_file=KEY, model="gpt-4o", max_completion_tokens=1400)
decision = review(packet, cfg, now)
verified = (decision.verdict is SupervisorVerdict.PASS)
with open(RECORDS, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"kind": "Northstar0B3CertificationVerdict", "gpt_verdict": decision.verdict.value,
                         "verified": verified, "gpt_reasons": decision.reasons[:6],
                         "gpt_unresolved": decision.unresolved_requirements[:6], "recorded_at": now()}) + "\n")
print("== FINAL cross-contract GPT certification ==")
print(json.dumps({"verdict": decision.verdict.value, "model": decision.model, "reasons": decision.reasons[:6],
                  "unresolved": decision.unresolved_requirements[:6], "error": decision.error}, indent=2))
print(f"\nNORTHSTAR_0B3_GRAPH_GPT_VERIFIED = {verified}")
sys.exit(0 if verified else 1)
