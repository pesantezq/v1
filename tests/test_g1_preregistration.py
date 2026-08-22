"""The G1 preregistration freeze must be provable, and the historical evidence
must stay separable from it.

WHY THESE TESTS MATTER MORE THAN THEY LOOK.

The previous candidate asserted a freeze point that was checkable and false: it
named the parent commit, which does not contain the criteria at all. Nothing
failed, because nothing checked. These tests check.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from portfolio_automation.engineer_worker.g1 import corpus as CORP
from portfolio_automation.engineer_worker.g1 import criteria as CRIT
from portfolio_automation.engineer_worker.g1 import preregistration as PRE
from portfolio_automation.engineer_worker.g1 import taxonomy as TAX

REPO = Path(__file__).resolve().parents[1]
EV = REPO / "evals" / "g1"
HIST = EV / "historical_exploratory"


# =========================================================================== #
# THE FALSE CLAIM IS GONE
# =========================================================================== #
def test_criteria_no_longer_asserts_a_freeze_commit():
    """The module must not carry a freeze SHA; that is the pointer's job."""
    assert not hasattr(CRIT, "CRITERIA_FROZEN_AT_CANDIDATE")
    assert "frozen_at_candidate" not in CRIT.criteria_manifest()


def test_the_previously_claimed_freeze_commit_really_lacked_the_criteria():
    """Documents the defect as a fact, not a recollection.

    If this ever starts failing it means history was rewritten, which is itself
    worth knowing."""
    done = subprocess.run(
        ["git", "cat-file", "-e",
         "3bdb329a5b0acf1b45937b0a972e31c5b6ca12a4:"
         "portfolio_automation/engineer_worker/g1/criteria.py"],
        cwd=str(REPO), capture_output=True, text=True)
    assert done.returncode != 0, (
        "the parent commit does contain criteria.py after all — the recorded "
        "rationale for this repair would then be wrong")


# =========================================================================== #
# THE FREEZE IS REAL
# =========================================================================== #
def test_the_freeze_digest_is_stable_and_content_derived():
    assert PRE.freeze_digest() == PRE.freeze_digest()
    assert PRE.freeze_digest().startswith("g1freeze_")


def test_the_digest_covers_everything_that_must_not_move():
    content = PRE.frozen_content()
    assert content["criteria"] == CRIT.criteria_manifest()
    assert content["taxonomy"] == TAX.taxonomy_manifest()
    cases = {c["case_id"]: c for c in content["corpus"]["cases"]}
    assert set(cases) == {c.case_id for c in CORP.ALL_CASES}
    for c in CORP.ALL_CASES:
        rec = cases[c.case_id]
        assert rec["expected_supervisor_verdict"] == \
            c.expected_supervisor_verdict.value
        assert rec["split"] == c.split.value
        assert rec["severity"] == c.severity.value
        assert rec["gold_basis"] == c.gold_basis.value
        assert rec["gold_provenance"] == c.gold_provenance
        assert rec["fingerprint"] == c.fingerprint()
    assert content["audit_policy"]["min_human_audit_fraction"] == \
        CRIT.MIN_HUMAN_AUDIT_FRACTION
    assert content["sample_size_policy"]["min_cell_n_for_rate"] == \
        CRIT.MIN_CELL_N_FOR_RATE
    assert "accuracy_population" in content["scoring_semantics"]


@pytest.mark.parametrize("mutate", [
    "expected_verdict", "split", "gold_basis", "gold_provenance", "severity",
    "alternates",
])
def test_changing_any_frozen_field_changes_the_digest(monkeypatch, mutate):
    """A freeze that survives a changed gold label is not a freeze."""
    from portfolio_automation.engineer_worker.g1 import contracts as C
    from portfolio_automation.engineer_worker.g1.taxonomy import OutcomeClass as V

    original = CORP.ALL_CASES
    before = PRE.freeze_digest()
    first = original[0]
    swaps = {
        "expected_verdict": dict(expected_supervisor_verdict=(
            V.ESCALATE if first.expected_supervisor_verdict is not V.ESCALATE
            else V.ABSTAIN), acceptable_alternate_verdicts=()),
        "split": dict(split=(C.Split.HELD_OUT if first.split is not C.Split.HELD_OUT
                             else C.Split.DEVELOPMENT)),
        "gold_basis": dict(gold_basis=C.GoldBasis.CONSENSUS_REVIEW),
        "gold_provenance": dict(gold_provenance="something else entirely"),
        "severity": dict(severity=(C.Severity.LOW
                                   if first.severity is not C.Severity.LOW
                                   else C.Severity.HIGH)),
        "alternates": dict(acceptable_alternate_verdicts=()),
    }
    kw = {**{f.name: getattr(first, f.name)
             for f in first.__dataclass_fields__.values()}, **swaps[mutate]}
    if mutate == "alternates" and not first.acceptable_alternate_verdicts:
        kw["acceptable_alternate_verdicts"] = (
            V.ABSTAIN if first.expected_supervisor_verdict is not V.ABSTAIN
            else V.ESCALATE,)
    mutated = C.EvaluationCaseV0(**kw)
    monkeypatch.setattr(CORP, "ALL_CASES", (mutated,) + original[1:])
    assert PRE.freeze_digest() != before, f"{mutate} did not affect the digest"


def test_the_preregistration_artifact_contains_no_commit_id():
    """A commit cannot contain its own SHA; the artifact must not pretend to."""
    art = PRE.preregistration_artifact()
    blob = json.dumps(art)
    assert "preregistration_commit" not in art
    assert art["freeze_digest"] == PRE.freeze_digest()
    # no 40-hex commit-shaped string smuggled in
    import re
    assert not re.search(r"\b[0-9a-f]{40}\b", blob)


def test_the_committed_preregistration_matches_the_current_material():
    """The registered file on disk still digests to the current content."""
    stored = json.loads((EV / "preregistration.json").read_text(encoding="utf-8"))
    assert stored["freeze_digest"] == PRE.freeze_digest(), (
        "the frozen material has changed since preregistration.json was written")


def test_the_freeze_is_not_refuted_anywhere():
    """Checks 1 and 2 hold in every checkout, shallow or not."""
    v = PRE.verify_freeze(REPO)
    assert v.ok, v.reasons
    assert v.recorded_commit != PRE.UNCOMMITTED
    assert v.recorded_digest == v.current_digest


def test_the_commit_level_proof_holds_where_the_commit_is_available():
    """The load-bearing check: read the registered file OUT OF the commit.

    A working-tree read would compare the freeze to itself. CI checks out with
    fetch-depth 1, so the freeze commit's object is absent there -- which is
    INDETERMINATE, not refuted, and must be reported as such rather than
    silently passing or noisily failing."""
    v = PRE.verify_freeze(REPO)
    if not v.commit_available:
        assert v.indeterminate_reasons, (
            "an unavailable commit must say why it could not be examined")
        assert not v.verified_against_commit
        assert v.ok, "absence of the object is not refutation"
        pytest.skip(f"freeze commit not in this checkout: "
                    f"{v.indeterminate_reasons[0]}")
    assert v.fully_verified, v.reasons
    assert v.verified_against_commit


def test_an_unavailable_commit_is_indeterminate_not_a_failure(tmp_path):
    """Negative control for the new distinction itself."""
    (tmp_path / "evals" / "g1").mkdir(parents=True)
    (tmp_path / PRE.FREEZE_POINTER_REL).write_text(json.dumps({
        "preregistration_commit": "0" * 40,
        "freeze_digest": PRE.freeze_digest()}), encoding="utf-8")
    v = PRE.verify_freeze(tmp_path)          # not a git repo at all
    assert not v.commit_available
    assert not v.verified_against_commit
    assert v.ok, "the digest matched; nothing was refuted"
    assert v.indeterminate_reasons


def test_verification_fails_when_a_present_commit_lacks_the_material(tmp_path):
    """Negative control: a commit that EXISTS but lacks the registered file is a
    genuine failure, distinct from an absent object."""
    import subprocess
    repo = tmp_path / "r"
    repo.mkdir()
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
    (repo / "unrelated.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "no preregistration here"],
                   cwd=repo, check=True, env=env)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                         capture_output=True, text=True, env=env).stdout.strip()
    (repo / "evals" / "g1").mkdir(parents=True)
    (repo / PRE.FREEZE_POINTER_REL).write_text(json.dumps({
        "preregistration_commit": sha,
        "freeze_digest": PRE.freeze_digest()}), encoding="utf-8")
    v = PRE.verify_freeze(repo)
    assert v.commit_available, "the commit exists in this repo"
    assert not v.ok
    assert any("does not contain" in r for r in v.reasons)


def test_verification_fails_when_the_recorded_digest_is_stale(tmp_path):
    (tmp_path / "evals" / "g1").mkdir(parents=True)
    (tmp_path / PRE.FREEZE_POINTER_REL).write_text(json.dumps({
        "preregistration_commit": "0" * 40,
        "freeze_digest": "g1freeze_somethingelse"}), encoding="utf-8")
    v = PRE.verify_freeze(tmp_path)
    assert not v.ok
    assert any("CHANGED since the freeze" in r for r in v.reasons)


# =========================================================================== #
# HISTORICAL EVIDENCE STAYS SEPARATE AND UNMODIFIED
# =========================================================================== #
def test_the_historical_evidence_is_preserved():
    assert (HIST / "g1_records.json").is_file()
    recs = json.loads((HIST / "g1_records.json").read_text(encoding="utf-8"))
    assert len(recs) == 34


def test_the_historical_manifest_describes_rather_than_relabels():
    man = json.loads((HIST / "MANIFEST.json").read_text(encoding="utf-8"))
    assert man["population"] == "EXPLORATORY_HISTORICAL"
    assert man["n_records"] == 34
    assert man["known_limitations"], "limitations must be stated, not implied"
    # the records themselves were not rewritten
    recs = json.loads((HIST / "g1_records.json").read_text(encoding="utf-8"))
    assert "run_id" not in recs[0], (
        "historical records must stay byte-identical; backfilling a run id "
        "would fabricate provenance")


def test_the_manifest_file_hashes_match_the_preserved_files():
    """Proves the relocation was a copy, not an edit."""
    import hashlib
    man = json.loads((HIST / "MANIFEST.json").read_text(encoding="utf-8"))
    for name, digest in man["file_sha256"].items():
        actual = hashlib.sha256((HIST / name).read_bytes()).hexdigest()
        assert actual == digest, name


def test_no_preregistered_claim_attaches_to_the_historical_report():
    """The old report predates the freeze and must not imply otherwise."""
    rep = json.loads((HIST / "g1_report.json").read_text(encoding="utf-8"))
    assert "freeze_digest" not in json.dumps(rep)
    man = json.loads((HIST / "MANIFEST.json").read_text(encoding="utf-8"))
    assert man["status"] == "PRESERVED_AS_EVIDENCE_NOT_A_PREREGISTERED_RESULT"
