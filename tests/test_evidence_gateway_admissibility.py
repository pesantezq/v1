"""EvidenceGateway PIT admissibility — Northstar 0C foundation.

The 0C exit gate is "lookahead-audited PIT reads over the research store". These
tests are the audit: they prove evidence that was not knowable at T cannot be
read as of T, and that every refusal states why.

Organised as:
  A. the core anti-lookahead rule (known_at <= as_of), including its boundary
  B. fail-closed behaviour on absent / incoherent / malformed timing
  C. decision-object integrity (a decision cannot lie about itself)
  D. purity — no clock, no I/O, fully reproducible
  E. boundary preservation — 0B contracts consumed, not redefined
"""
from __future__ import annotations

import ast
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from portfolio_automation.evidence_gateway import (
    AdmissibilityDecision, AdmissibilityReason, is_admissible, require_admissible)
from portfolio_automation.evidence_gateway.admissibility import AdmissibilityError
from portfolio_automation.northstar.pit import (
    KNOWN_AT_DERIVED_CONSERVATIVE, KNOWN_AT_SOURCE_REPORTED, KNOWN_AT_UNKNOWN,
    PointInTime)

MODULE = (Path(__file__).resolve().parents[1] / "portfolio_automation" /
          "evidence_gateway" / "admissibility.py")

AS_OF = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)


def _pit(known_at=None, basis=None, published_at=None, **kw) -> PointInTime:
    if known_at is not None and basis is None:
        basis = KNOWN_AT_SOURCE_REPORTED
    return PointInTime(known_at=known_at, known_at_basis=basis or KNOWN_AT_UNKNOWN,
                       published_at=published_at, **kw)


# ── A. the core anti-lookahead rule ────────────────────────────────────────
def test_evidence_known_before_as_of_is_admitted():
    d = is_admissible(_pit(known_at=AS_OF - timedelta(days=1)), AS_OF)
    assert d.admitted is True
    assert d.reason is AdmissibilityReason.ADMITTED


def test_evidence_known_after_as_of_is_refused():
    """The rule the 0B kernel names: future knowledge cannot enter a past read."""
    d = is_admissible(_pit(known_at=AS_OF + timedelta(days=1)), AS_OF)
    assert d.admitted is False
    assert d.reason is AdmissibilityReason.KNOWN_AT_AFTER_AS_OF


def test_one_microsecond_after_as_of_is_refused():
    """The boundary is enforced exactly, not approximately."""
    d = is_admissible(_pit(known_at=AS_OF + timedelta(microseconds=1)), AS_OF)
    assert d.admitted is False
    assert d.reason is AdmissibilityReason.KNOWN_AT_AFTER_AS_OF


def test_known_at_exactly_equal_to_as_of_is_admitted():
    """`known_at <= as_of` is inclusive: known_at is already the EARLIEST
    defensible moment, so equality means usable exactly then."""
    d = is_admissible(_pit(known_at=AS_OF), AS_OF)
    assert d.admitted is True


def test_the_kernel_docstring_scenario_is_enforced():
    """pit.py's own example: fiscal Q2 ended 2026-06-30 but was published
    2026-08-05, so a backtest as of July 15 must NOT see it — even though the
    business period ended before the read instant."""
    q2 = _pit(known_at=datetime(2026, 8, 5, 13, 0, tzinfo=timezone.utc),
              published_at=datetime(2026, 8, 5, 13, 0, tzinfo=timezone.utc),
              effective_period_start=date(2026, 4, 1),
              effective_period_end=date(2026, 6, 30),
              effective_period_label="2026-Q2")
    d = is_admissible(q2, AS_OF)
    assert d.admitted is False
    assert d.reason is AdmissibilityReason.KNOWN_AT_AFTER_AS_OF


def test_timezone_offsets_are_compared_correctly_not_textually():
    """Same instant, different offsets: admissibility is about instants."""
    eastern = timezone(timedelta(hours=-4))
    same_instant = AS_OF.astimezone(eastern)
    assert is_admissible(_pit(known_at=same_instant), AS_OF).admitted is True
    later = (AS_OF + timedelta(hours=1)).astimezone(eastern)
    assert is_admissible(_pit(known_at=later), AS_OF).admitted is False


# ── B. fail closed ─────────────────────────────────────────────────────────
def test_unknown_known_at_is_refused_not_admitted_by_default():
    """Absent timing is not permission. This is the load-bearing fail-closed
    case: the kernel deliberately represents unknown timing instead of
    backfilling it, and admitting on absence would undo that."""
    d = is_admissible(_pit(), AS_OF)
    assert d.admitted is False
    assert d.reason is AdmissibilityReason.KNOWN_AT_UNKNOWN
    assert d.known_at_basis == KNOWN_AT_UNKNOWN


def test_known_at_before_published_at_is_refused_as_incoherent():
    """Evidence claiming it was knowable before release cannot bound lookahead."""
    d = is_admissible(
        _pit(known_at=AS_OF - timedelta(days=10),
             published_at=AS_OF - timedelta(days=2)), AS_OF)
    assert d.admitted is False
    assert d.reason is AdmissibilityReason.KNOWN_AT_BEFORE_PUBLISHED_AT


def test_incoherent_timing_is_refused_even_when_known_at_precedes_as_of():
    """Coherence is checked BEFORE the comparison, so incoherent evidence cannot
    slip through merely by being old."""
    d = is_admissible(
        _pit(known_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
             published_at=datetime(2025, 1, 1, tzinfo=timezone.utc)), AS_OF)
    assert d.reason is AdmissibilityReason.KNOWN_AT_BEFORE_PUBLISHED_AT


def test_known_at_equal_to_published_at_is_coherent():
    at = AS_OF - timedelta(days=3)
    assert is_admissible(_pit(known_at=at, published_at=at), AS_OF).admitted is True


def test_naive_as_of_is_refused_never_coerced():
    """Assuming UTC for a naive boundary would silently shift it by hours."""
    d = is_admissible(_pit(known_at=AS_OF - timedelta(days=1)),
                      datetime(2026, 7, 15, 12, 0, 0))
    assert d.admitted is False
    assert d.reason is AdmissibilityReason.AS_OF_NOT_TIMEZONE_AWARE


def test_non_datetime_as_of_is_refused():
    d = is_admissible(_pit(known_at=AS_OF), "2026-07-15")
    assert d.admitted is False
    assert d.reason is AdmissibilityReason.AS_OF_NOT_A_DATETIME


def test_non_pit_input_is_refused():
    d = is_admissible({"known_at": AS_OF}, AS_OF)
    assert d.admitted is False
    assert d.reason is AdmissibilityReason.NOT_A_POINT_IN_TIME


def test_malformed_input_returns_a_decision_rather_than_raising():
    """Total, so a caller iterating a corpus cannot skip malformed evidence by
    catching an exception."""
    for bad in (None, 0, "x", object()):
        assert is_admissible(bad, AS_OF).admitted is False


def test_conservative_derived_known_at_is_honoured():
    """The kernel's one sanctioned derivation still participates normally."""
    pit = PointInTime(retrieved_at=AS_OF - timedelta(days=1)).with_conservative_known_at()
    d = is_admissible(pit, AS_OF)
    assert d.admitted is True
    assert d.known_at_basis == KNOWN_AT_DERIVED_CONSERVATIVE


# ── C. decision integrity ──────────────────────────────────────────────────
def test_a_decision_cannot_claim_admission_with_a_refusal_reason():
    with pytest.raises(ValueError):
        AdmissibilityDecision(admitted=True,
                              reason=AdmissibilityReason.KNOWN_AT_AFTER_AS_OF)


def test_a_decision_cannot_claim_refusal_while_reason_says_admitted():
    with pytest.raises(ValueError):
        AdmissibilityDecision(admitted=False, reason=AdmissibilityReason.ADMITTED)


def test_every_refusal_carries_a_reason_from_the_closed_set():
    cases = [
        (_pit(), AS_OF), (_pit(known_at=AS_OF + timedelta(days=1)), AS_OF),
        (_pit(known_at=AS_OF, published_at=AS_OF + timedelta(days=1)), AS_OF),
        (_pit(known_at=AS_OF), datetime(2026, 7, 15)), (None, AS_OF),
    ]
    for pit, as_of in cases:
        d = is_admissible(pit, as_of)
        assert d.reason in set(AdmissibilityReason)
        assert d.detail, "a refusal must explain itself"


def test_decision_is_falsy_when_refused_and_truthy_when_admitted():
    assert not is_admissible(_pit(), AS_OF)
    assert is_admissible(_pit(known_at=AS_OF), AS_OF)


def test_require_admissible_raises_with_the_reason():
    with pytest.raises(AdmissibilityError) as exc:
        require_admissible(_pit(known_at=AS_OF + timedelta(days=1)), AS_OF)
    assert "KNOWN_AT_AFTER_AS_OF" in str(exc.value)


def test_require_admissible_returns_the_decision_when_admitted():
    assert require_admissible(_pit(known_at=AS_OF), AS_OF).admitted is True


# ── D. purity ──────────────────────────────────────────────────────────────
def test_identical_inputs_always_produce_identical_decisions():
    pit = _pit(known_at=AS_OF - timedelta(days=1))
    first, second = is_admissible(pit, AS_OF), is_admissible(pit, AS_OF)
    assert first.to_dict() == second.to_dict()


def test_module_reads_no_clock_and_performs_no_io():
    """A predicate that could read the wall clock would make historical audit
    non-deterministic; one that could open a socket would couple the gateway to
    a vendor. Asserted structurally rather than by convention."""
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    forbidden_calls = {"now", "today", "utcnow", "time", "open", "urlopen",
                       "connect", "request", "get", "post"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            assert name not in forbidden_calls, f"forbidden call: {name}"
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mod = getattr(node, "module", "") or ""
            names = {a.name for a in node.names}
            for banned in ("os", "socket", "requests", "urllib", "sqlite3", "time"):
                assert banned not in names and not mod.startswith(banned), (
                    f"gateway must not import {banned}")


def test_no_default_as_of_exists():
    """as_of must always be supplied, so a decision can be re-audited forever."""
    with pytest.raises(TypeError):
        is_admissible(_pit(known_at=AS_OF))


# ── E. boundary preservation ───────────────────────────────────────────────
def test_gateway_consumes_the_0b_contracts_without_redefining_them():
    """The gateway must not grow its own PIT type; that would fork the canonical
    semantics it exists to enforce."""
    source = MODULE.read_text(encoding="utf-8")
    assert "from portfolio_automation.northstar.pit import" in source
    tree = ast.parse(source)
    classes = {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
    assert "PointInTime" not in classes
    assert "EvidenceSnapshot" not in classes


def test_gateway_interface_carries_no_vendor_schema():
    """Replaceable sources: no provider name may appear in the admission contract."""
    source = MODULE.read_text(encoding="utf-8").lower()
    for vendor in ("fmp", "finra", "bloomberg", "refinitiv", "polygon", "quandl",
                   "alphavantage", "iex"):
        assert vendor not in source, f"vendor {vendor} leaked into the gateway"


def test_gateway_introduces_no_prediction_or_capital_authority():
    """Checked over CODE IDENTIFIERS, not raw file text.

    A substring scan of the whole file is the wrong instrument here: it matches
    the package path `portfolio_automation/...` in a docstring and ordinary
    English like "in order to". Those are prose, not authority. What must not
    exist is a trading/allocation concept in the gateway's actual code surface."""
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    identifiers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            identifiers.add(node.id.lower())
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr.lower())
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            identifiers.add(node.name.lower())
        elif isinstance(node, ast.arg):
            identifiers.add(node.arg.lower())

    forbidden = ("order", "trade", "broker", "allocation", "allocate", "portfolio",
                 "position", "capital", "prediction", "signal", "buy", "sell")
    leaked = {i for i in identifiers if any(f in i for f in forbidden)}
    assert not leaked, f"trading/allocation concepts in the gateway code: {leaked}"


# ══ TASK 2: whole-evidence admission (identity + provenance binding) ═══════
from portfolio_automation.evidence_gateway.admission import (
    AdmissionDecision, AdmissionReason, admit)
from portfolio_automation.northstar.evidence import EvidenceRef, EvidenceSnapshot
from portfolio_automation.northstar.provenance import Provenance

SRC = "src_" + "a" * 32


def _prov(source_id=SRC, producer_type="source_adapter", **kw):
    # The 0B contract refuses a source_adapter provenance without a source_id,
    # so the no-source case must use a producer type for which that is coherent.
    if source_id is None:
        producer_type = "system"
    return Provenance(producer_id="adapter.test", producer_type=producer_type,
                      recorded_at=AS_OF - timedelta(days=2), source_id=source_id, **kw)


def _snap(known_at=None, payload=None, source_id=SRC, provenance=None):
    return EvidenceSnapshot(
        source_id=source_id, entity_id="AAPL", entity_type="symbol",
        evidence_type="fundamental.revenue",
        pit=_pit(known_at=known_at if known_at is not None else AS_OF - timedelta(days=1)),
        provenance=provenance if provenance is not None else _prov(source_id=source_id),
        payload=payload or {"revenue": 123})


# --- admission happy path + timing delegation -----------------------------
def test_well_formed_evidence_is_admitted():
    d = admit(_snap(), AS_OF)
    assert d.admitted is True
    assert d.reason is AdmissionReason.ADMITTED
    assert d.snapshot_id.startswith("evs_")


def test_future_evidence_is_refused_and_stays_attributable_to_timing():
    """A lookahead refusal must be reported as lookahead, not as something else —
    otherwise an auditor counting lookahead refusals undercounts them."""
    d = admit(_snap(known_at=AS_OF + timedelta(days=1)), AS_OF)
    assert d.admitted is False
    assert d.reason is AdmissionReason.PIT_REFUSED
    assert d.pit_reason is AdmissibilityReason.KNOWN_AT_AFTER_AS_OF


def test_unknown_timing_is_refused_through_the_pit_layer():
    snap = EvidenceSnapshot(
        source_id=SRC, entity_id="AAPL", entity_type="symbol",
        evidence_type="fundamental.revenue", pit=_pit(),   # known_at unknown
        provenance=_prov(), payload={"revenue": 1})
    d = admit(snap, AS_OF)
    assert d.reason is AdmissionReason.PIT_REFUSED
    assert d.pit_reason is AdmissibilityReason.KNOWN_AT_UNKNOWN


def test_timing_is_checked_before_identity():
    """Ordering is part of the audit contract: evidence that is BOTH future-dated
    and ref-mismatched must report the timing failure."""
    snap = _snap(known_at=AS_OF + timedelta(days=5))
    wrong_ref = _snap(payload={"revenue": 999}).ref()
    d = admit(snap, AS_OF, ref=wrong_ref)
    assert d.reason is AdmissionReason.PIT_REFUSED


# --- identity is recomputed, never trusted --------------------------------
def test_identity_is_derived_from_content_so_tampering_changes_it():
    """Two snapshots differing only in payload cannot share an identity; that is
    what makes the recomputation check meaningful rather than decorative."""
    a, b = _snap(payload={"revenue": 1}), _snap(payload={"revenue": 2})
    assert a.snapshot_id != b.snapshot_id
    assert a.payload_hash != b.payload_hash


def test_admitted_decision_reports_the_recomputed_identity():
    snap = _snap()
    d = admit(snap, AS_OF)
    assert d.snapshot_id == snap.snapshot_id


# --- provenance must not contradict the evidence --------------------------
def test_the_0b_contract_refuses_contradicting_provenance_at_construction():
    """PRIMARY enforcement lives in the CONTRACT, not the gateway.

    EvidenceSnapshot already refuses a provenance naming a different source, so a
    contradicting snapshot cannot normally exist to be presented to the gateway."""
    other = "src_" + "b" * 32
    with pytest.raises(ValueError):
        EvidenceSnapshot(
            source_id=SRC, entity_id="AAPL", entity_type="symbol",
            evidence_type="fundamental.revenue",
            pit=_pit(known_at=AS_OF - timedelta(days=1)),
            provenance=_prov(source_id=other), payload={"revenue": 1})


def test_gateway_backstops_contradicting_provenance_on_a_bypassed_object():
    """The gateway check is DEFENCE IN DEPTH, not the primary guarantee.

    It only matters for evidence reconstructed WITHOUT the constructor — a future
    store loading rows directly, say. Simulated by deliberately bypassing the
    frozen dataclass, which is the only way such an object can arise."""
    snap = _snap()
    object.__setattr__(snap.provenance, "source_id", "src_" + "c" * 32)
    d = admit(snap, AS_OF)
    assert d.admitted is False
    assert d.reason is AdmissionReason.PROVENANCE_SOURCE_MISMATCH


def test_provenance_without_a_source_is_admitted():
    """A system-produced snapshot names no source; coherent, not a defect."""
    assert admit(_snap(provenance=_prov(source_id=None)), AS_OF).admitted is True


# --- reference binding ----------------------------------------------------
def test_matching_ref_is_accepted():
    snap = _snap()
    assert admit(snap, AS_OF, ref=snap.ref()).admitted is True


def test_ref_pointing_at_different_content_is_refused():
    snap = _snap(payload={"revenue": 1})
    other_ref = _snap(payload={"revenue": 2}).ref()
    d = admit(snap, AS_OF, ref=other_ref)
    assert d.admitted is False
    assert d.reason is AdmissionReason.REF_DOES_NOT_MATCH_SNAPSHOT


def test_non_ref_object_is_refused():
    d = admit(_snap(), AS_OF, ref={"snapshot_id": "evs_x"})
    assert d.reason is AdmissionReason.NOT_AN_EVIDENCE_REF


# --- malformed input ------------------------------------------------------
def test_non_snapshot_input_returns_a_decision():
    for bad in (None, {}, "evs_x", 7):
        d = admit(bad, AS_OF)
        assert d.admitted is False
        assert d.reason is AdmissionReason.NOT_AN_EVIDENCE_SNAPSHOT


def test_admission_decision_cannot_lie_about_itself():
    with pytest.raises(ValueError):
        AdmissionDecision(admitted=True, reason=AdmissionReason.PIT_REFUSED)
    with pytest.raises(ValueError):
        AdmissionDecision(admitted=False, reason=AdmissionReason.ADMITTED)


def test_admission_carries_the_pit_decision_for_audit():
    """Even on a non-timing refusal the timing verdict stays visible."""
    snap = _snap()
    d = admit(snap, AS_OF, ref=_snap(payload={"x": 9}).ref())
    assert d.reason is AdmissionReason.REF_DOES_NOT_MATCH_SNAPSHOT
    assert d.pit_decision is not None and d.pit_decision.admitted is True


def test_admission_module_introduces_no_storage_or_vendor():
    mod = (Path(__file__).resolve().parents[1] / "portfolio_automation" /
           "evidence_gateway" / "admission.py")
    tree = ast.parse(mod.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = {a.name for a in node.names}
            module = getattr(node, "module", "") or ""
            for banned in ("sqlite3", "socket", "requests", "urllib", "os", "psycopg2"):
                assert banned not in names and not module.startswith(banned)
    src = mod.read_text(encoding="utf-8").lower()
    for vendor in ("fmp", "finra", "bloomberg", "polygon", "iex"):
        assert vendor not in src


# ══ SENIOR-REVIEW REPAIR: real payload-integrity adversarial tests ═════════
# The original task-2 tests showed that two DIFFERENT valid snapshots produce
# different hashes — an adjacent property. They never constructed an actual
# mismatch, so the frozen criterion "a tampered payload is refused" was never
# exercised and the task passed while the criterion was unmet. These tests
# construct the mismatch directly.
#
# Bypassing the frozen dataclass is the ONLY way such an object can arise (a
# normally-constructed snapshot cannot disagree with itself), and it is exactly
# how evidence reconstructed outside the constructor would look.

def test_mutated_payload_content_is_refused():
    """payload_canonical altered, payload_hash left intact — content no longer
    hashes to its recorded hash."""
    snap = _snap(payload={"revenue": 1})
    object.__setattr__(snap, "payload_canonical", '{"revenue":999}')
    d = admit(snap, AS_OF)
    assert d.admitted is False
    assert d.reason is AdmissionReason.PAYLOAD_HASH_MISMATCH


def test_mutated_payload_hash_is_refused():
    """payload_hash altered, content left intact — the recorded hash no longer
    matches what the content actually hashes to."""
    snap = _snap(payload={"revenue": 1})
    object.__setattr__(snap, "payload_hash", "0" * 64)
    d = admit(snap, AS_OF)
    assert d.admitted is False
    assert d.reason is AdmissionReason.PAYLOAD_HASH_MISMATCH


def test_unparseable_payload_canonical_is_refused():
    snap = _snap()
    object.__setattr__(snap, "payload_canonical", "{not json")
    d = admit(snap, AS_OF)
    assert d.admitted is False
    assert d.reason is AdmissionReason.PAYLOAD_NOT_CANONICAL


def test_integrity_check_is_a_real_recomputation_not_a_readback():
    """Regression guard for the exact false-pass the senior review caught.

    If admit() were re-reading snapshot.payload_hash instead of hashing the
    stored content, both mutation tests above would PASS admission. This test
    states the property directly: the recomputed hash is derived from content."""
    from portfolio_automation.northstar.canonical import content_hash
    snap = _snap(payload={"revenue": 1})
    assert content_hash(snap.payload_copy()) == snap.payload_hash
    object.__setattr__(snap, "payload_canonical", '{"revenue":2}')
    assert content_hash(snap.payload_copy()) != snap.payload_hash


def test_no_unreachable_snapshot_id_mismatch_reason_exists():
    """snapshot_id is derived with no stored counterpart, so an id-mismatch
    branch could never fire. Keeping one would make the API look stronger than
    it is — which is the claim this repair removed."""
    assert not hasattr(AdmissionReason, "SNAPSHOT_ID_MISMATCH")


def test_evidence_ref_is_the_actual_identity_anchor():
    """Identity is anchored by an EXTERNAL ref, not by self-comparison."""
    snap = _snap(payload={"revenue": 1})
    assert admit(snap, AS_OF, ref=snap.ref()).admitted is True
    assert admit(snap, AS_OF, ref=_snap(payload={"revenue": 2}).ref()).reason is (
        AdmissionReason.REF_DOES_NOT_MATCH_SNAPSHOT)


def test_from_dict_rejects_a_non_reproducing_serialized_identity():
    """The other identity anchor, provided by the 0B contract itself."""
    snap = _snap(payload={"revenue": 1})
    data = snap.to_canonical_dict()
    data["snapshot_id"] = "evs_" + "0" * 32
    with pytest.raises(ValueError):
        EvidenceSnapshot.from_dict(data)
