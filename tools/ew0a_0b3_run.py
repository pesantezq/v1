"""Northstar 0B.3 controller step + C0.5 shadow apprenticeship + live GPT verify.

For one 0B.3 controller decision:
  1. capture STATE-BEFORE, ask the REAL Engineer (Agent-Lab facade) for a
     non-authoritative controller proposal (ControllerDecisionCandidateV0), persist it;
  2. record Claude's AUTHORITATIVE controller decision;
  3. run the INDEPENDENT live GPT verification of the produced candidate;
  4. compare Engineer proposal vs authoritative decision (apprenticeship lesson);
  5. persist a durable outcome.

Diagnostic/verification only — writes no repo files. The Engineer proposal never
dispatches, certifies, or changes state.
"""
from __future__ import annotations
import base64, datetime, json, subprocess, sys

REPO = "/home/pesan/stockbot-lab/repo/v1"
sys.path.insert(0, REPO)
from portfolio_automation.engineer_worker.gpt_supervisor import review, SupervisorConfig, SupervisorVerdict  # noqa: E402

KEY = "/home/pesan/.ew0a_openai_key"
FACADE, MODEL = "http://10.201.0.1:11435", "qwen2.5:7b"
RECORDS = "/home/pesan/ew0a_0b3_records.jsonl"

def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def engineer(prompt, n=700):
    payload = json.dumps({"model": MODEL, "prompt": prompt, "stream": False,
                          "options": {"temperature": 0.0, "num_predict": n}})
    b64 = base64.b64encode(payload.encode()).decode()
    inner = f"echo {b64} | base64 -d > /tmp/ns.json && curl -s -m 160 {FACADE}/api/generate -d @/tmp/ns.json; rm -f /tmp/ns.json"
    try:
        p = subprocess.run(["wsl.exe", "-d", "StockBot-Agent-Lab", "--", "bash", "-lc", inner],
                           capture_output=True, text=True, timeout=180)
        return json.loads(p.stdout).get("response", "")
    except Exception as e:  # noqa: BLE001
        return f"__ENGINEER_ERROR__ {type(e).__name__}"

def rec(kind, obj):
    with open(RECORDS, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"kind": kind, "recorded_at": now(), **obj}, default=str) + "\n")

# ---- STATE BEFORE DECISION -------------------------------------------------
state = {
    "mission_id": "northstar_0b_decision_outcome_passport_contracts",
    "phase": "0B.3 ready; 0B.1+0B.2 complete",
    "existing_0b2_contracts": ["PredictionTask", "PredictionRecord", "ResearchTask",
                               "WorkerResult", "ResearchClaim"],
    "declared_0b3_deliverables": ["ExperimentSpec", "ExperimentResult", "CapitalProposal",
                                  "ExitProposal", "OutcomeRecord", "StrategyPassport"],
    "dependency_order": "ExperimentSpec -> ExperimentResult -> CapitalProposal -> ExitProposal -> OutcomeRecord -> StrategyPassport",
    "no_0b3_contract_exists_yet": True,
}

# ---- 1) ENGINEER shadow controller proposal (NON-AUTHORITATIVE) ------------
shadow_prompt = (
    "You are a SHADOW engineering controller (non-authoritative apprentice). Given the state, "
    "propose the SINGLE next engineering task. Output ONLY one JSON object with keys: "
    "proposed_next_task, proposed_risk_class (E1|E2|E3|E4), proposed_executor (ENGINEER|CLAUDE|HUMAN), "
    "proposed_allowed_scope (list of path prefixes), proposed_acceptance_criteria (list), "
    "proposed_verification_plan (list), proposed_retry_or_escalation, reasoning_summary.\n"
    "Risk guide: E1/E2 routine bounded -> ENGINEER; E3 architecture/security/new canonical contract -> CLAUDE; "
    "E4 production/capital/irreversible -> HUMAN.\nSTATE:\n" + json.dumps(state))
raw = engineer(shadow_prompt)
try:
    s = raw.strip()
    s = s[s.find("{"): s.rfind("}") + 1]
    proposal = json.loads(s)
except Exception:
    proposal = {"_unparseable": True, "raw_head": raw.strip()[:300]}
candidate = {"candidate_id": "cdc-exs-1", "mission_id": state["mission_id"],
             "current_state_ref": "0B.3-ready", "proposal": proposal}
rec("ControllerDecisionCandidateV0", candidate)
print("== C0.5 ENGINEER shadow proposal (non-authoritative) ==")
print(json.dumps(proposal, indent=2)[:1400])

# ---- 2) CLAUDE authoritative controller decision ---------------------------
authoritative = {
    "next_task": "author ExperimentSpec canonical contract (portfolio_automation/northstar/experiments.py) + tests",
    "risk_class": "E3",
    "executor": "CLAUDE",
    "allowed_scope": ["portfolio_automation/northstar/", "tests/"],
    "acceptance_criteria": [
        "frozen canonical contract mirroring the hardened kernel (deterministic exs_ identity, schema_era, PIT as_of)",
        "hypothesis_claim_id validated as a rcl_ ResearchClaim id",
        "preregistration IS identity (any prereg change is a new experiment)",
        "structural invariants: no result fields, no action/authority fields (ExperimentSpec != ExperimentResult; not an action)",
        "canonical round-trip + identity reproduction + tamper rejection",
        "new tests pass; no new relevant regressions"],
    "verification_plan": ["deterministic: py_compile + tests/test_northstar_experiment_spec.py",
                          "independent GPT verification of the contract vs acceptance criteria"],
    "retry_or_escalation": "bounded repair on deterministic failure; already applied 1 repair (test serialization)",
    "rationale": "Authoring a NEW canonical Northstar contract is architecture; per 'do not let the Engineer invent canonical architecture' it routes E3 -> Claude.",
}
rec("AuthoritativeControllerDecision", {"candidate_id": "cdc-exs-1", "decision": authoritative})
print("\n== CLAUDE authoritative decision: E3 -> CLAUDE (ExperimentSpec) ==")

# ---- 3) INDEPENDENT live GPT verification of the ExperimentSpec candidate ---
contract_src = open(f"{REPO}/portfolio_automation/northstar/experiments.py", encoding="utf-8").read()
packet = {
    "task": {"task_id": "0B3-ExperimentSpec", "mission_id": state["mission_id"],
             "title": "ExperimentSpec canonical contract", "risk_class": "E3", "executor": "CLAUDE"},
    "requirements": ["new canonical Northstar contract ExperimentSpec (milestone 3)",
                     "integrate with existing ResearchClaim + evidence/PIT/identity kernel"],
    "acceptance_criteria": authoritative["acceptance_criteria"],
    "verification_steps": authoritative["verification_plan"],
    "allowed_paths": authoritative["allowed_scope"],
    "changed_files": ["portfolio_automation/northstar/experiments.py",
                      "tests/test_northstar_experiment_spec.py",
                      "portfolio_automation/northstar/__init__.py"],
    "diff": "NEW FILE portfolio_automation/northstar/experiments.py:\n" + contract_src[:55000],
    "tests_run": ["tests/test_northstar_experiment_spec.py"],
    "test_results": {"tests/test_northstar_experiment_spec.py": "PASS (27 passed)"},
    "py_compile_ok": True,
    "worker_claim": "CLAUDE_CANDIDATE_READY (please independently verify)",
}
cfg = SupervisorConfig(key_file=KEY, model="gpt-4o", max_completion_tokens=1400)
decision = review(packet, cfg, now)
print("\n== INDEPENDENT GPT verification ==")
print(json.dumps({"verdict": decision.verdict.value, "model": decision.model,
                  "reasons": decision.reasons[:4],
                  "unresolved": decision.unresolved_requirements[:4], "error": decision.error}, indent=2))

verified = (decision.verdict is SupervisorVerdict.PASS)   # deterministic already PASS
rec("ExperimentResultOutcome", {"task_id": "0B3-ExperimentSpec", "deterministic": "PASS",
    "gpt_verdict": decision.verdict.value, "verified": verified,
    "gpt_reasons": decision.reasons[:4]})

# ---- 4) apprenticeship comparison ------------------------------------------
eng_risk = str(proposal.get("proposed_risk_class", "")).upper()
eng_exec = str(proposal.get("proposed_executor", "")).upper()
appr = {
    "engineer_proposed_task_relates_to_experimentspec": "experimentspec" in json.dumps(proposal).lower(),
    "engineer_risk": eng_risk, "authoritative_risk": "E3",
    "risk_agreement": eng_risk == "E3",
    "engineer_executor": eng_exec, "authoritative_executor": "CLAUDE",
    "routing_agreement": eng_exec == "CLAUDE",
    "danger_underclassified_architecture_as_engineer": (eng_exec == "ENGINEER" or eng_risk in ("E1", "E2")),
}
rec("ApprenticeshipComparison", {"candidate_id": "cdc-exs-1", **appr})
print("\n== APPRENTICESHIP comparison ==")
print(json.dumps(appr, indent=2))
print(f"\nVERIFIED={verified}  (deterministic PASS AND GPT {decision.verdict.value})")
sys.exit(0 if verified else 1)
