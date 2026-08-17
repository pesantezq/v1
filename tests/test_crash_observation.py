"""The crash observation format cannot express a guess as a conclusion."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from portfolio_automation.engineer_worker.crash_observation import (
    Citation, Confidence, CrashObservation, DerivedConclusion, ObservationError,
    ObservedFact, UnresolvedGap, ledger_digest,
)

REPO = Path(__file__).resolve().parents[1]
CRASHED_LEDGER = "docs/NORTHSTAR_0C_SESSION_ns0c-revision-supersession-002.jsonl"


def _fact(fid="f1"):
    return ObservedFact(
        fact_id=fid, statement="HEAD_UNCHANGED_AT_DISPATCH was PENDING",
        citation=Citation(ledger_rel=CRASHED_LEDGER, line=8,
                          json_path="$.candidate_binding.checks"),
        verbatim_value="PENDING")


def test_a_fact_without_a_citation_is_rejected():
    with pytest.raises(ObservationError):
        ObservedFact(fact_id="f1", statement="something happened",
                     citation=Citation(CRASHED_LEDGER, 0, "$"), verbatim_value="x")


def test_conclusions_may_not_reference_other_conclusions():
    c1 = DerivedConclusion("c1", "first", ("f1",), "because", Confidence.ENTAILED)
    c2 = DerivedConclusion("c2", "second", ("c1",), "because", Confidence.ENTAILED)
    with pytest.raises(ObservationError) as exc:
        CrashObservation("2026-08-16", "s", CRASHED_LEDGER, "sha",
                         facts=(_fact(),), conclusions=(c1, c2))
    assert "weakest link" in str(exc.value)


def test_a_conclusion_citing_an_unknown_fact_is_rejected():
    c = DerivedConclusion("c1", "x", ("f_missing",), "because", Confidence.ENTAILED)
    with pytest.raises(ObservationError):
        CrashObservation("2026-08-16", "s", CRASHED_LEDGER, "sha",
                         facts=(_fact(),), conclusions=(c,))


def test_a_conclusion_deriving_from_nothing_is_rejected():
    c = DerivedConclusion("c1", "x", (), "vibes", Confidence.ENTAILED)
    with pytest.raises(ObservationError):
        CrashObservation("2026-08-16", "s", CRASHED_LEDGER, "sha", conclusions=(c,))


def test_confidence_vocabulary_admits_no_speculation():
    assert {c.value for c in Confidence} == {"ENTAILED", "PROBABLE"}


def test_the_real_crashed_ledger_supports_the_pending_observation():
    """Not a fixture: the actual merged ledger, at the actual line."""
    path = REPO / CRASHED_LEDGER
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    binding_lines = [
        i + 1 for i, ln in enumerate(lines)
        if json.loads(ln).get("candidate_binding", {})
        .get("checks", {}).get("HEAD_UNCHANGED_AT_DISPATCH") == "PENDING"]
    assert binding_lines, "the defect this hardening fixes is recorded in history"

    obs = CrashObservation(
        observed_at_date="2026-08-16",
        observed_session_id="ns0c-revision-supersession-002",
        observed_ledger_rel=CRASHED_LEDGER,
        observed_ledger_sha256=ledger_digest(path),
        facts=(ObservedFact(
            fact_id="f_pending",
            statement="a dispatched review recorded an unresolved freshness check",
            citation=Citation(CRASHED_LEDGER, binding_lines[0],
                              "$.candidate_binding.checks.HEAD_UNCHANGED_AT_DISPATCH"),
            verbatim_value="PENDING"),),
        conclusions=(DerivedConclusion(
            conclusion_id="c_pending",
            statement="terminal HEAD evidence was never written on the success path",
            from_facts=("f_pending",),
            inference="recheck_head() returned self when HEAD was stationary, so the "
                      "binding-time PENDING was carried into the dispatch record",
            confidence=Confidence.ENTAILED),),
        gaps=(UnresolvedGap(
            gap_id="g_head_moved",
            question="did HEAD actually move during that dispatch?",
            why_unresolvable="only a hand-written git_head_at_dispatch was recorded; "
                             "no independent re-resolution exists in the ledger",
            what_would_resolve_it="the reflog for the candidate commit, if retained"),))

    d = obs.to_dict()
    assert d["observed_facts"][0]["verbatim_value"] == "PENDING"
    assert d["derived_conclusions"][0]["confidence"] == "ENTAILED"
    assert d["unresolved_gaps"][0]["gap_id"] == "g_head_moved"


def test_observing_a_ledger_does_not_modify_it():
    path = REPO / CRASHED_LEDGER
    before = ledger_digest(path)
    CrashObservation("2026-08-16", "s", CRASHED_LEDGER, before, facts=(_fact(),))
    assert ledger_digest(path) == before, "the past is annotated, never edited"
