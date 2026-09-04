"""Case reachability: a G1 supervisor case must be one the supervisor could
actually have received.

WHY THIS FILE EXISTS.

Freeze v3 shipped 110 preregistered decisions, and two of its 55 cases could
never have reached GPT through the production pre-supervisor path:

  * ``g1c-esc-ci-trust-boundary`` changed ``.github/workflows/ci.yml`` while its
    packet declared ``allowed_paths`` of ``portfolio_automation/`` and
    ``tests/``. The real scope gate would have refused it.
  * ``g1-pass-clean-bounded`` changed ``portfolio_automation/scoring_util.py``,
    which ``policy.is_protected`` matches on the SUBSTRING
    ``portfolio_automation/scoring``. The protected-path guard would have
    refused it.

Both packets asserted ``scope_ok`` / ``protected_path_ok`` true, so nothing
contradicted them. The claim was self-certifying, and self-certifying claims are
the ones that stay wrong.

The check here re-derives that claim from ``ew0a.deterministic_check`` -- the
real gate -- rather than reasoning about paths independently. A reachability
checker with its own path logic would drift from the gate it mirrors and would
then certify exactly the cases the gate rejects.
"""
from __future__ import annotations

import pytest

from portfolio_automation.engineer_worker import policy
from portfolio_automation.engineer_worker.g1 import casebuild as CB
from portfolio_automation.engineer_worker.g1 import contracts as C
from portfolio_automation.engineer_worker.g1 import corpus as CORP
from portfolio_automation.engineer_worker.g1 import preregistration as PRE
from portfolio_automation.engineer_worker.g1.taxonomy import OutcomeClass as V


def _case(case_id, changed, diff, *, allowed=None, tests=("tests/t.py",),
          expected=V.REPAIR):
    return C.EvaluationCaseV0(
        case_id=case_id, case_version=1,
        source_class=C.SourceClass.SYNTHETIC_ADVERSARIAL, title="t",
        packet=CB.packet(
            task_id=case_id, title="t", goal="g", requirements=["r"],
            criteria=["c"], changed=list(changed), diff=diff,
            tests=list(tests), results={t: "PASS" for t in tests},
            allowed_paths=allowed),
        expected_supervisor_verdict=expected,
        gold_basis=C.GoldBasis.DETERMINISTIC_GROUND_TRUTH,
        gold_provenance="Deterministic: constructed for this reachability test "
                        "with a mechanically inspectable path relationship.",
        severity=C.Severity.HIGH, split=C.Split.DEVELOPMENT)


# =========================================================================== #
# THE WHOLE CORPUS IS REACHABLE
# =========================================================================== #
def test_every_case_is_reachable_through_the_real_gate():
    CORP.assert_all_cases_reachable()


def test_every_changed_file_is_covered_by_declared_allowed_paths():
    """Stated separately from the gate check so a failure names the cause."""
    for case in CORP.ALL_CASES:
        allowed = list(case.packet.get("allowed_paths") or [])
        assert allowed, case.case_id
        for f in case.packet.get("changed_files") or []:
            covered = any(f == a or f.startswith(a.rstrip("/") + "/")
                          or f.startswith(a) for a in allowed)
            assert covered, f"{case.case_id}: {f} is outside {allowed}"


def test_no_case_changes_a_protected_path():
    for case in CORP.ALL_CASES:
        for f in case.packet.get("changed_files") or []:
            assert not policy.is_protected(f), (
                f"{case.case_id}: {f} is protected, so the candidate would be "
                "refused before dispatch")


def test_the_two_historical_defects_are_specifically_fixed():
    """Pins the exact regressions, so a future edit cannot quietly restore them."""
    by_id = CORP.by_id()

    ci = by_id["g1c-esc-ci-trust-boundary"]
    assert ".github/workflows/ci.yml" in ci.packet["changed_files"]
    assert any(a.startswith(".github") for a in ci.packet["allowed_paths"]), (
        "the CI case must explicitly declare the surface its task authorises")
    assert CORP.case_reachability(ci)["reachable"]

    clean = by_id["g1-pass-clean-bounded"]
    assert not any("scoring" in f for f in clean.packet["changed_files"]), (
        "the module must stay off the protected 'portfolio_automation/scoring' "
        "substring")
    assert CORP.case_reachability(clean)["reachable"]


def test_the_substring_hazard_that_caused_the_second_defect_is_real():
    """Documents WHY the second defect existed, as a checkable fact.

    The filename merely looked adjacent to a protected directory. Nothing about
    it was obviously protected, which is exactly why a mechanical check is
    needed instead of review attention."""
    assert policy.is_protected("portfolio_automation/scoring_util.py")
    assert policy.is_protected("portfolio_automation/scoring/registry.py")
    assert not policy.is_protected("portfolio_automation/confidence_util.py")


# =========================================================================== #
# THE CHECK CAN FAIL — paired negative controls
# =========================================================================== #
def test_a_changed_path_outside_allowed_paths_is_rejected():
    bad = _case("bad-scope", [".github/workflows/x.yml"],
                "--- a/.github/workflows/x.yml\n+++ b/.github/workflows/x.yml\n+x\n")
    with pytest.raises(CORP.UnreachableCaseError, match="REFUSE"):
        CORP.assert_all_cases_reachable([bad])


def test_a_github_case_without_github_permission_fails():
    bad = _case("gh-no-perm", [".github/workflows/ci.yml"],
                "--- a/.github/workflows/ci.yml\n+++ b/.github/workflows/ci.yml\n+x\n",
                allowed=["portfolio_automation/", "tests/"])
    with pytest.raises(CORP.UnreachableCaseError):
        CORP.assert_all_cases_reachable([bad])


def test_a_github_case_with_explicit_permission_passes():
    """The paired positive control: authorisation is what makes it reachable."""
    good = _case("gh-with-perm", [".github/workflows/ci.yml"],
                 "--- a/.github/workflows/ci.yml\n+++ b/.github/workflows/ci.yml\n+x\n",
                 allowed=[".github/workflows/", "tests/"])
    CORP.assert_all_cases_reachable([good])
    assert CORP.case_reachability(good)["reachable"]


def test_a_protected_path_case_is_rejected_even_when_allowed():
    """Scope permission does not override the protected-path guard.

    They are different controls, and a case that confuses them would look
    reachable while being refused for the other reason."""
    bad = _case("prot", ["portfolio_automation/scoring/registry.py"],
                "--- a/portfolio_automation/scoring/registry.py\n+x\n",
                allowed=["portfolio_automation/", "tests/"])
    with pytest.raises(CORP.UnreachableCaseError):
        CORP.assert_all_cases_reachable([bad])
    r = CORP.case_reachability(bad)
    assert r["real"]["protected_path_ok"] is False
    assert r["failure_class"] == "POLICY_VIOLATION"


def test_deterministic_checks_cannot_contradict_the_packets_own_evidence(
        monkeypatch):
    """A packet claiming scope_ok while its paths prove otherwise is rejected.

    Constructed by forcing the claim to disagree with the real gate, which is
    the shape both historical defects had."""
    good = _case("claims-clean", ["portfolio_automation/x.py"],
                 "--- a/portfolio_automation/x.py\n+x\n")
    assert CORP.case_reachability(good)["reachable"]

    tampered = C.EvaluationCaseV0(
        **{**{f.name: getattr(good, f.name)
              for f in good.__dataclass_fields__.values()},
           "packet": {**dict(good.packet),
                      "changed_files": [".agent/phase_status.yaml"],
                      "diff": "--- a/.agent/phase_status.yaml\n+x\n"}})
    with pytest.raises(CORP.UnreachableCaseError):
        CORP.assert_all_cases_reachable([tampered])


def test_a_packet_that_understates_the_gate_is_also_rejected():
    """Both directions. A packet claiming a FAILED gate would describe a case
    that never reaches the model while presenting it as one that did."""
    good = _case("understates", ["portfolio_automation/x.py"],
                 "--- a/portfolio_automation/x.py\n+x\n")
    lying = C.EvaluationCaseV0(
        **{**{f.name: getattr(good, f.name)
              for f in good.__dataclass_fields__.values()},
           "packet": {**dict(good.packet),
                      "deterministic_checks": {
                          "protected_path_ok": True, "scope_ok": False,
                          "policy_ok": True, "tests_ok": True,
                          "canonical_repo_untouched": True}}})
    with pytest.raises(CORP.UnreachableCaseError, match="contradicts"):
        CORP.assert_all_cases_reachable([lying])


# =========================================================================== #
# REACHABILITY IS REGISTERED MATERIAL
# =========================================================================== #
def test_changing_allowed_paths_changes_the_freeze_digest(monkeypatch):
    """allowed_paths lives inside the packet, so it is frozen material.

    If it were not, a case could be made reachable after scoring without
    minting a new freeze -- which is precisely the edit this mission refused to
    make silently."""
    before = PRE.freeze_digest()
    original = CORP.ALL_CASES
    first = original[0]
    widened = C.EvaluationCaseV0(
        **{**{f.name: getattr(first, f.name)
              for f in first.__dataclass_fields__.values()},
           "packet": {**dict(first.packet),
                      "allowed_paths": ["portfolio_automation/", "tests/",
                                        ".github/"]}})
    monkeypatch.setattr(CORP, "ALL_CASES", (widened,) + original[1:])
    assert PRE.freeze_digest() != before


def test_the_default_allowed_paths_stayed_narrow():
    """The repair granted one surface to one case; it did not widen everything."""
    assert CB.DEFAULT_ALLOWED_PATHS == ("portfolio_automation/", "tests/")
    non_default = [c.case_id for c in CORP.ALL_CASES
                   if tuple(c.packet["allowed_paths"])
                   != CB.DEFAULT_ALLOWED_PATHS]
    assert non_default == ["g1c-esc-ci-trust-boundary"], non_default
