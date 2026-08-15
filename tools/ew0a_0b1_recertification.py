"""ONE fresh independent certification of Northstar 0B.1 against the changed state.

The first attempt returned ABSTAIN because the trusted supervisor packet screen
could not transmit the two files that ARE the Evidence Kernel's security evidence:
``northstar/sources.py`` (the DataSourceDescriptor credential guard) and
``tests/test_northstar_evidence_kernel.py`` (the fixtures proving it rejects
credential material). The reviewer correctly refused to certify what it could not
read.

PR #17 (merged, main ``eddc6f2``, push CI run #29 SUCCESS) made the supervisor
screen precise enough to transmit that evidence WITHOUT weakening any credential
protection — independently security-reviewed PASS. So this packet differs from the
first materially: both files are now included verbatim as ``source_files`` rather
than withheld and described.

Protocol, fixed before any verdict is seen: ONE review. The verdict is NOT
rerolled. Only a transport failure (the reviewer never reached) is retried. The
verdict is persisted whatever it is.

The historical ABSTAIN remains permanently recorded in
``docs/EW0A_0B_PHASE_CERTIFICATION.jsonl``; this appends, it never rewrites.
"""
from __future__ import annotations

import datetime
import json
import sys
import time

REPO = "/home/pesan/stockbot-lab/repo/v1"
sys.path.insert(0, REPO)

from portfolio_automation.engineer_worker import supervisor_screen  # noqa: E402
from portfolio_automation.engineer_worker.gpt_supervisor import (  # noqa: E402
    SupervisorConfig, SupervisorVerdict, review)

KEY = "/home/pesan/.ew0a_openai_key"
RECORDS = f"{REPO}/docs/EW0A_0B_PHASE_CERTIFICATION.jsonl"
CFG = SupervisorConfig(key_file=KEY, model="gpt-4o", max_completion_tokens=1800)
BASE_SHA = "eddc6f2f92c880bcc131432d750600ce0be4503f"


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def read(rel: str, limit: int = 40000) -> str:
    with open(f"{REPO}/{rel}", encoding="utf-8") as fh:
        return fh.read()[:limit]


PACKET = {
    "task": {"task_id": "Northstar-0B1-Recertification",
             "mission_id": "northstar_0b_contract_architecture_and_evidence_kernel",
             "title": "Northstar 0B.1 Contract Architecture & Evidence Kernel — fresh "
                      "certification against changed repository state",
             "risk_class": "E3", "executor": "CLAUDE",
             "base_sha": BASE_SHA},
    "prior_attempt": {
        "verdict": "ABSTAIN",
        "reason": ("the packet could not carry sources.py or the evidence-kernel tests, "
                   "so the reviewer could not validate the acceptance criterion that "
                   "credential material is refused in a DataSourceDescriptor"),
        "what_changed": ("PR #17 separated the supervisor evidence-transfer screen from "
                         "the production evidence detector. The production detector and "
                         "the DataSourceDescriptor guard are UNCHANGED. Both previously "
                         "unreadable files are included verbatim below."),
        "note": "The prior ABSTAIN remains permanently recorded and is not rewritten.",
    },
    "requirements": [
        "canonical strict serializer with deterministic sha256 identity",
        "PointInTime with a no-fabricated-time discipline (observed / published / known "
        "/ retrieved), supporting lookahead-free reads",
        "Provenance and DataSourceDescriptor: external sources are Evidence Plane inputs "
        "carrying source/dataset/record identity, provenance, PIT timestamps, "
        "snapshot/hash, schema version and rights metadata — vendor schemas are NEVER "
        "embedded in engines or workers",
        "EvidenceSnapshot with revision chains; EvidenceRef; FeatureRecord",
        "fail-closed EXACT schema-version gate (serde.require_schema_version) plus "
        "constructor pinning; there is no migration framework, so missing, empty, "
        "unknown and future versions must all be rejected",
        "schema era (major version) participates in every deterministic identity",
        "centralized id/hash format validators; EvidenceRef carries schema_version",
        "provenance consistency: source_adapter requires source_id; snapshot and "
        "provenance must agree on source; feature transformation_id must equal "
        "derivation_id@derivation_version",
        "feature inputs form an UNORDERED dependency set: identity is order-free and "
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
        "credential-shaped material in a DataSourceDescriptor is REFUSED (this is the "
        "criterion the prior attempt could not evaluate; sources.py and its tests are "
        "both included below)",
    ],
    "verification_steps": [
        "deterministic: tests/test_northstar_evidence_kernel.py — 45 tests, all passing "
        "on merged main eddc6f2 (44 original plus a new test pinning that the descriptor "
        "still rejects sentinel-bearing credential values)",
        "full 0B contract suite: 194 tests across 8 files, all passing",
        "broad hermetic suite: 10,689 passed / 15 pre-existing failures; identical "
        "baseline node IDs -> NEW_RELEVANT_FAILURES=0",
        "post-merge main CI: northstar-ci run #29 SUCCESS at eddc6f2",
    ],
    "allowed_paths": ["portfolio_automation/northstar/", "tests/", "docs/"],
    "changed_files": ["portfolio_automation/northstar/{canonical,pit,provenance,serde,"
                      "sources,evidence,features,_collections}.py",
                      "tests/test_northstar_evidence_kernel.py"],
    "source_files": [
        {"path": "portfolio_automation/northstar/sources.py",
         "content": read("portfolio_automation/northstar/sources.py")},
        {"path": "tests/test_northstar_evidence_kernel.py",
         "content": read("tests/test_northstar_evidence_kernel.py")},
        {"path": "portfolio_automation/northstar/canonical.py",
         "content": read("portfolio_automation/northstar/canonical.py")},
        {"path": "portfolio_automation/northstar/pit.py",
         "content": read("portfolio_automation/northstar/pit.py")},
        {"path": "portfolio_automation/northstar/provenance.py",
         "content": read("portfolio_automation/northstar/provenance.py")},
        {"path": "portfolio_automation/northstar/serde.py",
         "content": read("portfolio_automation/northstar/serde.py")},
        {"path": "portfolio_automation/northstar/evidence.py",
         "content": read("portfolio_automation/northstar/evidence.py")},
        {"path": "portfolio_automation/northstar/features.py",
         "content": read("portfolio_automation/northstar/features.py")},
    ],
    "note_on_test_fixtures": (
        "The credential-rejection test uses the exact sentinel <synthetic-secret-fixture> "
        "as its VALUE rather than a credential-shaped literal. What is under test is "
        "unchanged: the descriptor guard matches the credential KEYWORD, so each case is "
        "still rejected. A dedicated test pins that the guard rejects sentinel-bearing "
        "values too, so the sentinel is a transfer mechanism and never a contract "
        "exemption."),
    "tests_run": ["tests/test_northstar_evidence_kernel.py"],
    "test_results": {"tests/test_northstar_evidence_kernel.py": "PASS (45 passed)",
                     "full_0b_contract_suite": "PASS (194 passed across 8 files)",
                     "broad": "10,689 passed / 15 pre-existing failures, 0 new"},
    "py_compile_ok": True,
    "worker_claim": (
        "NORTHSTAR_0B1_RECERTIFICATION_CANDIDATE — the evidence that was unreadable at "
        "the prior attempt is now included in full. Verify the evidence kernel's "
        "identity, point-in-time, provenance and fail-closed versioning invariants, and "
        "in particular whether a DataSourceDescriptor genuinely refuses credential "
        "material. Judge only from the evidence; if anything remains unverifiable, "
        "return ABSTAIN or REPAIR rather than PASS."),
}


def main() -> int:
    result = supervisor_screen.screen_packet(PACKET)
    if result.blocked:
        print("PREFLIGHT: packet refused by the supervisor screen — NOT sending:")
        for f in result.findings:
            print("   ", json.dumps(f.to_dict()))
        return 2
    print(f"PREFLIGHT: packet clean ({len(result.exempted)} structural exemptions)")
    for e in result.exempted:
        print("   exempt:", e)

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
            "kind": "Northstar0B1RecertificationVerdict",
            "milestone": "northstar_0b_contract_architecture_and_evidence_kernel",
            "candidate_id": "northstar-0b1-recert-1",
            "base_sha": BASE_SHA,
            "supersedes_attempt": "Northstar0B1CertificationVerdict (ABSTAIN, retained)",
            "gpt_verdict": decision.verdict.value, "verified": verified,
            "gpt_reasons": decision.reasons[:10],
            "gpt_unresolved": decision.unresolved_requirements[:10],
            "evidence_checked": decision.evidence_checked[:10],
            "model": decision.model, "error": decision.error,
            "protocol": "one review; verdict not rerolled; transport-only retry",
            "recorded_at": now()}) + "\n")

    print("\n== Northstar 0B.1 fresh independent certification ==")
    print(json.dumps({"verdict": decision.verdict.value, "model": decision.model,
                      "reasons": decision.reasons[:10],
                      "unresolved": decision.unresolved_requirements[:10],
                      "error": decision.error}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
