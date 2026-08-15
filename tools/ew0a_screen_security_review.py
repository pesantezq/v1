"""Independent security review of the supervisor evidence screen change.

This change touches the credential/exfiltration boundary, so Claude cannot
self-certify it. One review, no verdict rerolling; only transport failure retries.

The packet is built through the NEW screen deliberately — if the change were
unsafe or self-defeating, the screen would refuse to transmit its own review.
"""
from __future__ import annotations

import datetime
import json
import subprocess
import sys
import time

REPO = "/home/pesan/stockbot-lab/repo/v1"
sys.path.insert(0, REPO)

from portfolio_automation.engineer_worker.gpt_supervisor import (  # noqa: E402
    SupervisorConfig, SupervisorVerdict, review)

KEY = "/home/pesan/.ew0a_openai_key"
RECORDS = f"{REPO}/docs/EW0A_0B_PHASE_CERTIFICATION.jsonl"
CFG = SupervisorConfig(key_file=KEY, model="gpt-4o", max_completion_tokens=1800)


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def read(rel: str, limit: int = 40000) -> str:
    with open(f"{REPO}/{rel}", encoding="utf-8") as fh:
        return fh.read()[:limit]


def git_diff() -> str:
    out = subprocess.run(["git", "diff", "df6c8af..HEAD", "--", "portfolio_automation/"],
                         cwd=REPO, capture_output=True, text=True)
    return out.stdout[:30000]


PACKET = {
    "task": {"task_id": "EW0A-SupervisorScreen-SecurityReview",
             "mission_id": "ew0a_supervisor_secret_screen_precision",
             "title": "Security review: supervisor evidence secret-screen precision",
             "risk_class": "E3", "executor": "CLAUDE"},
    "requirements": [
        "FALSE POSITIVES must decrease: legitimate security implementation and test "
        "evidence (a credential guard's own regex; synthetic fixtures proving it "
        "rejects credentials) must reach the independent reviewer",
        "FALSE NEGATIVES must NOT increase: real or plausible credential material must "
        "remain fail-closed in packet fields, nested structures, code, comments, test "
        "files, diffs, logs and prose",
        "the production evidence detector (prod_evidence._detect_secret) MUST remain "
        "unchanged — it guards a different boundary",
        "the DataSourceDescriptor structural credential rejection MUST remain unchanged",
        "no broad TEST_/fake_/example_/dummy_ bypass may be introduced",
        "any synthetic-fixture mechanism must be an EXACT sentinel, never a prefix, "
        "suffix or substring exemption",
        "a real secret must not be launderable by placing it in a conveniently named "
        "variable or inside a regex call",
    ],
    "acceptance_criteria": [
        "portfolio_automation/northstar/sources.py is transferable",
        "tests/test_northstar_evidence_kernel.py is transferable",
        "a packet containing a real provider-key shape is still refused",
        "regex-looking characters inside a value must NOT buy an exemption (the "
        "adversarial corpus below, each expected BLOCKED)",
        "sentinel prefix and suffix forms remain BLOCKED; only exact equality exempts",
        "a real provider key assigned to a conveniently named pattern variable remains "
        "BLOCKED (structural, not name-based classification)",
        "a real provider key wrapped in a regex call remains BLOCKED (layer 1 is "
        "context-free)",
        "unparseable Python and non-Python evidence get NO structural exemption",
        "the DataSourceDescriptor guard still rejects sentinel-bearing credential values",
    ],
    # The adversarial corpus is carried as STRUCTURED PAIRS rather than as literal
    # "keyword=value" strings. Writing them literally would embed real
    # credential-assignment patterns in this packet, and the screen would refuse to
    # transmit its own review — as it did on the first attempt, correctly. Splitting
    # the keyword from the value preserves every bit of information the reviewer
    # needs without creating the pattern. The screen was NOT relaxed to fit.
    "adversarial_corpus_must_remain_blocked": [
        {"keyword": "api_key", "value": "RealSecret|MoreSecret", "expected": "BLOCKED"},
        {"keyword": "token", "value": "abc[123]Secret", "expected": "BLOCKED"},
        {"keyword": "password", "value": "Strong(Password)Value", "expected": "BLOCKED"},
        {"keyword": "client_secret", "value": "abc?def*ghi", "expected": "BLOCKED"},
        {"keyword": "Authorization", "value": "Bearer Real|Secret", "expected": "BLOCKED"},
        {"keyword": "api_key", "value": "<sentinel>REALKEY1234",
         "expected": "BLOCKED (sentinel is exact-match only)"},
        {"keyword": "api_key", "value": "REALKEY1234<sentinel>",
         "expected": "BLOCKED (sentinel is exact-match only)"},
        {"keyword": "fake_api_key", "value": "plausible16charval",
         "expected": "BLOCKED (no fake_/TEST_/dummy_ exemption)"},
    ],
    "verification_steps": [
        "deterministic: tests/test_ew0a_supervisor_screen.py — 50 tests, all passing "
        "(sections: must-block, must-allow, bypass attempts, boundary preservation)",
        "tests/test_northstar_evidence_kernel.py — 45 tests passing, including a NEW "
        "test pinning that the descriptor still rejects sentinel-bearing values",
        "tests/test_gpt_supervisor.py — passing",
        "broad hermetic suite: 10,689 passed / 15 failed; the 15 failing node IDs are "
        "IDENTICAL to the pre-change baseline -> NEW_RELEVANT_FAILURES=0",
        "end-to-end: both previously-blocked files now screen clean; a negative control "
        "injecting a real provider-key shape into the same packet is still refused",
    ],
    "allowed_paths": ["portfolio_automation/engineer_worker/", "tests/", "docs/"],
    "changed_files": [
        "portfolio_automation/engineer_worker/supervisor_screen.py (new)",
        "portfolio_automation/engineer_worker/gpt_supervisor.py (screen wiring)",
        "tests/test_ew0a_supervisor_screen.py (new, 50 adversarial tests)",
        "tests/test_northstar_evidence_kernel.py (fixtures -> exact sentinel; new "
        "contract test)",
        "docs/EW0A_SUPERVISOR_SCREEN.md (new)",
    ],
    "old_behavior": (
        "gpt_supervisor._screen_packet applied prod_evidence._detect_secret to the "
        "serialized packet. Its credential-assignment branch matches a keyword, then "
        "':' or '=', then any 4+ non-space characters. On source code this fires on "
        "regex pattern definitions and on synthetic test fixtures, so the Evidence "
        "Kernel's own credential guard and the tests proving it works could not be "
        "transmitted. Northstar 0B.1 certification consequently returned ABSTAIN."),
    "demonstrated_false_positives": [
        "sources.py line 49 — the DataSourceDescriptor credential guard's own "
        "re.compile(...) pattern, which names credential keywords as regex "
        "alternatives. A pattern definition, not a value. Its full text is included "
        "verbatim in the source_files entry for sources.py below.",
        "test_northstar_evidence_kernel.py lines 351 and 355 — synthetic fixtures "
        "asserting the descriptor rejects credential-shaped notes.",
    ],
    "withheld_from_packet": {
        "file": "tests/test_ew0a_supervisor_screen.py",
        "reason": (
            "This file is INHERENTLY untransmittable BY DESIGN, and that is the "
            "correct outcome rather than a defect. It is the adversarial suite: it "
            "necessarily contains a literal private-key block and real "
            "credential-assignment strings, because its job is to prove those are "
            "blocked. Layer 1 refuses private-key blocks context-free and is never "
            "exemptible, so the screen refuses to ship it. The screen was NOT relaxed "
            "to transmit its own review. The first attempt at this very review was "
            "refused for exactly this reason."),
        "described_coverage": [
            "A. MUST BLOCK — private-key block, provider key, AWS key, GitHub token, "
            "JWT, Bearer value, six credential assignments, the regex-metacharacter "
            "corpus, secrets nested in packet structures, secrets inside source files, "
            "inside test files, and in prose",
            "B. MUST ALLOW — the real sources.py, the exact regex construct that caused "
            "the 0B.1 ABSTAIN, detector regex source text, redacted placeholders, exact "
            "sentinel fixtures, and documentation discussing credential terms",
            "C. BYPASS — sentinel prefix/suffix/doubled forms, fake_/TEST_/dummy_/"
            "example_ prefixes, a conveniently named pattern variable holding a real "
            "key, a real key laundered through re.compile, casing, whitespace, quoting, "
            "unparseable Python, malformed source_files fields, and a check that "
            "findings never echo the secret value",
            "D. BOUNDARY — the production detector is unchanged and still strict, the "
            "DataSourceDescriptor guard still rejects sentinel-bearing values, and a "
            "false-negative regression guard asserts the new screen blocks everything "
            "the production detector blocks on flat text",
        ],
        "result": "50 tests, all passing",
        "instruction_to_reviewer": (
            "Judge this file from the described coverage and the cited results. If that "
            "is insufficient to certify the change, return REPAIR or ESCALATE and say "
            "so — do not PASS on unseen material."),
    },
    "new_behavior": (
        "A separate supervisor_screen module with three layers. Layer 1: "
        "high-confidence shapes (private key, provider key, AWS, GitHub, JWT, Bearer) "
        "applied context-free to ALL evidence and never exemptible. Layer 2: the "
        "credential-assignment rule, exempt ONLY when the AST proves the match lies "
        "wholly inside a string literal that is a direct re.* argument — structural, "
        "never name-based; non-Python evidence and unparseable source get no exemption. "
        "Layer 3: an EXACT sentinel <synthetic-secret-fixture>, equality only. The new "
        "screen is stricter than the old one in two respects: it drops the leading word "
        "boundary (so fake_api_key/TEST_token/dummy_password are now blocked, which the "
        "production detector misses) and it catches config[\"token\"] = \"...\"."),
    "rejected_design": (
        "An earlier proposal exempted values containing regex metacharacters. It was "
        "rejected in security review as unsafe, correctly: a real credential may "
        "contain the characters | [ ] ( ) ? * — see the password entry in the "
        "adversarial corpus above, whose value is a plausible strong passphrase built "
        "from parentheses. The adversarial suite now pins that entire class as "
        "blocked."),
    "source_files": [
        {"path": "portfolio_automation/engineer_worker/supervisor_screen.py",
         "content": read("portfolio_automation/engineer_worker/supervisor_screen.py")},
        {"path": "portfolio_automation/northstar/sources.py",
         "content": read("portfolio_automation/northstar/sources.py")},
    ],
    "tests_run": ["tests/test_ew0a_supervisor_screen.py",
                  "tests/test_northstar_evidence_kernel.py",
                  "tests/test_gpt_supervisor.py"],
    "test_results": {
        "tests/test_ew0a_supervisor_screen.py": "PASS (50 passed)",
        "tests/test_northstar_evidence_kernel.py": "PASS (45 passed)",
        "tests/test_gpt_supervisor.py": "PASS",
        "broad": "10,689 passed / 15 pre-existing failures, 0 new",
    },
    "py_compile_ok": True,
    "worker_claim": (
        "EW0A_SUPERVISOR_SCREEN_PRECISION_CANDIDATE — verify INDEPENDENTLY that this "
        "made the detector more CORRECT rather than easier to pass. Specifically: that "
        "false negatives did not increase, that neither exemption (AST regex-literal "
        "context, exact sentinel) can be used as a general bypass, that the production "
        "detector and the DataSourceDescriptor guard are untouched, and that a real "
        "credential cannot be laundered through a regex call or a convenient variable "
        "name. If any exemption looks exploitable, return REPAIR or ESCALATE."),
}


def preflight() -> bool:
    """Screen the review packet with the NEW screen before sending.

    If this refuses, the packet — not the screen — is what must change. Reporting
    the findings here keeps that distinction visible instead of surfacing as an
    opaque transport failure."""
    from portfolio_automation.engineer_worker import supervisor_screen
    result = supervisor_screen.screen_packet(PACKET)
    if result.blocked:
        print("PREFLIGHT: packet refused by the new screen (screen is NOT at fault):")
        for f in result.findings:
            print("   ", json.dumps(f.to_dict()))
        return False
    print(f"PREFLIGHT: packet clean ({len(result.exempted)} structural exemptions)")
    return True


def main() -> int:
    if not preflight():
        return 2
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
            "kind": "SupervisorScreenPrecisionSecurityReviewVerdict",
            "mission": "ew0a_supervisor_secret_screen_precision",
            "candidate_id": "ew0a-screen-precision-1",
            "gpt_verdict": decision.verdict.value, "verified": verified,
            "gpt_reasons": decision.reasons[:10],
            "gpt_unresolved": decision.unresolved_requirements[:10],
            "model": decision.model, "error": decision.error,
            "protocol": "single review; verdicts not rerolled; transport-only retry",
            "recorded_at": now()}) + "\n")

    print("== Independent security review ==")
    print(json.dumps({"verdict": decision.verdict.value, "model": decision.model,
                      "reasons": decision.reasons[:10],
                      "unresolved": decision.unresolved_requirements[:10],
                      "error": decision.error}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
