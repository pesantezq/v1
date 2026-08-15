"""Independent GPT certification of Northstar 0B.1 and 0B.2.

Closes a real gap found while assembling the Phase 0B exit-gate proof
(`contracts reviewed + versioned + test-covered`):

* 0B.3 carries full independent-review evidence (per-contract PASS records plus a
  cross-contract certification PASS).
* 0B.1's own milestone note says "hardened PASS CANDIDATE pending GPT independent
  verification — 'complete' here means implemented+hardened, not finally
  certified", and no such verification record exists anywhere in the repository.
* 0B.2 carries no independent-review record either.

So the "reviewed" clause was proven for one of three milestones. This tool obtains
the missing evidence rather than reinterpreting the gate to fit what we have.

Protocol (fixed before any verdict is seen):
* ONE review per milestone. Verdicts are NOT rerolled — a REPAIR is answered by
  fixing the code and re-reviewing the fix, never by asking again.
* Only a transport failure (SUPERVISOR_UNAVAILABLE, meaning the reviewer was never
  reached) is retried, because that is asking for the first time, not re-rolling.
* Every verdict is persisted, including REPAIR and ESCALATE.

Usage:  python tools/ew0a_0b12_certification.py [0b1|0b2|both]
"""
from __future__ import annotations

import datetime
import json
import sys
import time

REPO = "/home/pesan/stockbot-lab/repo/v1"
sys.path.insert(0, REPO)

from portfolio_automation.engineer_worker.gpt_supervisor import (  # noqa: E402
    SupervisorConfig, SupervisorVerdict, review)

KEY = "/home/pesan/.ew0a_openai_key"
RECORDS = f"{REPO}/docs/EW0A_0B_PHASE_CERTIFICATION.jsonl"
CFG = SupervisorConfig(key_file=KEY, model="gpt-4o", max_completion_tokens=1600)

TRANSPORT_RETRIES = 3


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def read(rel: str, limit: int = 24000) -> str:
    with open(f"{REPO}/{rel}", encoding="utf-8") as fh:
        return fh.read()[:limit]


# --- 0B.1 — Contract Architecture & Evidence Kernel --------------------------
# sources.py and tests/test_northstar_evidence_kernel.py are WITHHELD from the
# packet and disclosed below: both legitimately contain credential-shaped material
# (sources.py implements the Evidence Plane's own secret screen; the kernel tests
# carry deliberate fake-credential fixtures proving that screen works), so the
# trusted supervisor packet screen refuses to transmit them. The screen is a
# protected security control and is NOT loosened to make this packet transmissible.
B1_PACKET = {
    "task": {"task_id": "Northstar-0B1-Certification",
             "mission_id": "northstar_0b_contract_architecture_and_evidence_kernel",
             "title": "Northstar 0B.1 Contract Architecture & Evidence Kernel certification",
             "risk_class": "E3", "executor": "CLAUDE"},
    "requirements": [
        "canonical strict serializer with deterministic sha256 identity",
        "PointInTime with a no-fabricated-time discipline (observed/published/known/"
        "retrieved), supporting lookahead-free reads",
        "Provenance and DataSourceDescriptor: external sources are Evidence Plane "
        "inputs with source/dataset/record identity, provenance, PIT timestamps, "
        "snapshot/hash, schema version and rights metadata — vendor schemas are NEVER "
        "embedded in engines or workers",
        "EvidenceSnapshot with revision chains; EvidenceRef; FeatureRecord",
        "fail-closed EXACT schema-version gate (serde.require_schema_version) plus "
        "constructor pinning; there is no migration framework, so missing/empty/"
        "unknown/future versions must all be rejected",
        "schema era (major version) participates in every deterministic identity",
        "centralized id/hash format validators; EvidenceRef carries schema_version",
        "provenance consistency: source_adapter requires source_id; snapshot and "
        "provenance must agree on source; feature transformation_id must equal "
        "derivation_id@derivation_version",
        "feature inputs are an UNORDERED dependency set: identity is order-free and "
        "duplicate inputs are rejected",
        "contracts only — nothing writes files, calls networks, or is wired into a pipeline",
    ],
    "acceptance_criteria": [
        "identity round-trips reproduce exactly; deserialization recomputes hashes and "
        "rejects mismatches",
        "provenance is never identity-bearing",
        "an unknown contract_type is rejected; schema_version must match exactly",
        "no lookahead is possible through the PIT surface",
        "construction validates or raises — no partially-valid contract object exists",
        "credential-shaped material in a DataSourceDescriptor is refused",
    ],
    "verification_steps": [
        "deterministic: tests/test_northstar_evidence_kernel.py — 44 tests, all passing",
        "full 0B contract suite: 194 tests across 8 files, all passing",
        "broad hermetic suite on merged main: 10,638 passed, 15 pre-existing failures, "
        "NEW_RELEVANT_FAILURES=0",
    ],
    "allowed_paths": ["portfolio_automation/northstar/", "tests/", "docs/"],
    "changed_files": ["portfolio_automation/northstar/{canonical,pit,provenance,serde,"
                      "sources,evidence,features,_collections}.py",
                      "tests/test_northstar_evidence_kernel.py",
                      "docs/NORTHSTAR_CONTRACTS.md"],
    "withheld_from_packet": {
        "files": ["portfolio_automation/northstar/sources.py",
                  "tests/test_northstar_evidence_kernel.py"],
        "reason": ("the trusted supervisor packet screen refuses to transmit them: "
                   "sources.py implements the Evidence Plane's own credential screen "
                   "(so it necessarily contains credential-shaped patterns), and the "
                   "kernel test file contains deliberate fake-credential fixtures that "
                   "prove that screen rejects them. Neither contains a real secret. The "
                   "screen was NOT loosened to make this packet transmissible"),
        "described_behavior": [
            "sources.py defines DataSourceDescriptor: source/dataset identity, "
            "provenance, PIT capability, schema version and rights metadata, and it "
            "REFUSES descriptors carrying credential-shaped material (authorization "
            "headers, token assignments, provider key shapes) so vendor credentials "
            "can never enter the Evidence Plane.",
            "Historical incompatibility is expressed as pit_capability='none'|'unknown' "
            "and known_at_basis='unknown' at the adapter, never papered over in the contract.",
            "tests/test_northstar_evidence_kernel.py holds all 44 deterministic tests "
            "for the kernel, including the credential-refusal cases whose fake fixtures "
            "are what trips the screen.",
        ],
        "instruction_to_reviewer": ("Judge these two files from the described behavior "
                                    "and the cited test evidence. If that is insufficient "
                                    "to certify, return ABSTAIN or ESCALATE and say so — "
                                    "do not PASS on unseen material."),
    },
    "diff": ("=== canonical.py ===\n" + read("portfolio_automation/northstar/canonical.py", 8000)
             + "\n=== pit.py ===\n" + read("portfolio_automation/northstar/pit.py", 7000)
             + "\n=== provenance.py ===\n" + read("portfolio_automation/northstar/provenance.py", 5000)
             + "\n=== serde.py ===\n" + read("portfolio_automation/northstar/serde.py", 3000)
             + "\n=== evidence.py ===\n" + read("portfolio_automation/northstar/evidence.py", 13000)
             + "\n=== features.py ===\n" + read("portfolio_automation/northstar/features.py", 11000)
             + "\n=== _collections.py ===\n" + read("portfolio_automation/northstar/_collections.py", 4000)),
    "tests_run": ["tests/test_northstar_evidence_kernel.py"],
    "test_results": {"tests/test_northstar_evidence_kernel.py": "PASS (44 passed)",
                     "full_0b_contract_suite": "PASS (194 passed across 8 files)"},
    "py_compile_ok": True,
    "worker_claim": ("NORTHSTAR_0B1_CERTIFICATION_CANDIDATE — this milestone was "
                     "previously recorded as 'hardened PASS CANDIDATE pending GPT "
                     "independent verification' and was never independently verified. "
                     "Verify the evidence kernel's identity, PIT, provenance and "
                     "fail-closed versioning invariants from the evidence."),
}

# --- 0B.2 — Prediction / Research / Experiment contracts ---------------------
B2_PACKET = {
    "task": {"task_id": "Northstar-0B2-Certification",
             "mission_id": "northstar_0b_prediction_research_experiment_contracts",
             "title": "Northstar 0B.2 Prediction / Research contracts certification",
             "risk_class": "E3", "executor": "CLAUDE"},
    "requirements": [
        "PredictionTask (ptk_) and PredictionRecord (prd_): uncertainty is MANDATORY, "
        "evidence refs are mandatory, model-provenance consistency is enforced",
        "a PredictionRecord's action surface is STRUCTURALLY banned — a prediction can "
        "never express or imply a trade, order, or capital action",
        "prediction resolution happens BY REFERENCE only; a record cannot rewrite itself",
        "ResearchTask (rtk_) and WorkerResult (wkr_): a worker result is NEVER production "
        "truth, authority-bearing keys are rejected, abstention is first-class, findings "
        "are frozen once recorded",
        "ResearchClaim (rcl_): structural falsifiability is required; a claim is NEVER "
        "certified alpha",
        "contracts only — no engines, runtimes, or data-source integrations",
    ],
    "acceptance_criteria": [
        "a PredictionRecord cannot carry an action/order/trade surface, including via "
        "arbitrary payload keys",
        "a WorkerResult cannot be promoted to production truth and cannot carry authority keys",
        "a ResearchClaim without a falsifiable structure is rejected",
        "uncertainty and evidence refs cannot be omitted from a PredictionRecord",
        "identity round-trips reproduce; provenance is never identity-bearing",
        "abstention is representable as a first-class outcome, not an error",
    ],
    "verification_steps": [
        "deterministic: tests/test_northstar_prediction_research.py — 36 tests, all passing",
        "full 0B contract suite: 194 tests across 8 files, all passing",
        "these contracts are consumed by the 0B.3 graph, which passed independent "
        "cross-contract certification",
    ],
    "allowed_paths": ["portfolio_automation/northstar/", "tests/", "docs/"],
    "changed_files": ["portfolio_automation/northstar/predictions.py",
                      "portfolio_automation/northstar/research.py",
                      "tests/test_northstar_prediction_research.py"],
    "diff": ("=== predictions.py ===\n" + read("portfolio_automation/northstar/predictions.py", 21000)
             + "\n=== research.py ===\n" + read("portfolio_automation/northstar/research.py", 22000)
             + "\n=== tests ===\n" + read("tests/test_northstar_prediction_research.py", 20000)),
    "tests_run": ["tests/test_northstar_prediction_research.py"],
    "test_results": {"tests/test_northstar_prediction_research.py": "PASS (36 passed)",
                     "full_0b_contract_suite": "PASS (194 passed across 8 files)"},
    "py_compile_ok": True,
    "worker_claim": ("NORTHSTAR_0B2_CERTIFICATION_CANDIDATE — this milestone has no "
                     "independent verification record. Verify the separation invariants: "
                     "predictions carry no action surface, worker results are never "
                     "production truth, research claims are never certified alpha."),
}


def certify(name: str, packet: dict) -> str:
    """One review. Retries ONLY transport failure; never rerolls a real verdict."""
    decision = review(packet, CFG, now)
    for attempt in range(TRANSPORT_RETRIES):
        if decision.verdict is not SupervisorVerdict.SUPERVISOR_UNAVAILABLE:
            break
        print(f"  transport failure ({decision.error}); retrying in {2 * (attempt + 1)}s")
        time.sleep(2 * (attempt + 1))
        decision = review(packet, CFG, now)

    verified = decision.verdict is SupervisorVerdict.PASS
    with open(RECORDS, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "kind": f"Northstar{name}CertificationVerdict",
            "milestone": packet["task"]["mission_id"],
            "candidate_id": packet["task"]["task_id"],
            "gpt_verdict": decision.verdict.value, "verified": verified,
            "gpt_reasons": decision.reasons[:8],
            "gpt_unresolved": decision.unresolved_requirements[:8],
            "evidence_checked": decision.evidence_checked[:8],
            "model": decision.model, "error": decision.error,
            "protocol": "single review; verdicts are not rerolled; only transport "
                        "failure is retried",
            "recorded_at": now()}) + "\n")

    print(f"== {name} independent certification ==")
    print(json.dumps({"verdict": decision.verdict.value, "model": decision.model,
                      "reasons": decision.reasons[:8],
                      "unresolved": decision.unresolved_requirements[:8],
                      "error": decision.error}, indent=2))
    return decision.verdict.value


def main() -> int:
    which = (sys.argv[1] if len(sys.argv) > 1 else "both").lower()
    results = {}
    if which in ("0b1", "both"):
        results["0B1"] = certify("0B1", B1_PACKET)
    if which in ("0b2", "both"):
        results["0B2"] = certify("0B2", B2_PACKET)
    print("\n=== SUMMARY ===")
    print(json.dumps(results, indent=2))
    print(f"records: {RECORDS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
