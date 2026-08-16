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
