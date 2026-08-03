# tests/test_screening_sufficiency.py
"""Screening coverage is a SEPARATE question from candidate count.

`universe_sufficiency` (shipped 2026-08-03) answers "did the scanner produce
enough rows?". It cannot answer "did the screen actually bind on the universe?".
Both were observed to diverge badly: with `v3_max_symbols=100`, full_scan
produced 100 candidates of which only ~24 had fundamentals — the other ~76 were
admitted UNSCREENED (missing revenueGrowth/peRatio/freeCashFlowYield are
non-fatal by deliberate design in `_passes_hard_filters`), tied on score, and
collapsed the ranking tail to alphabetical order.

Why coverage MUST be measured by field presence, not row count: `get_fundamentals_v3`
appends a row for EVERY requested symbol unconditionally — even a bare
``{'symbol': 'X'}`` when nothing resolved. So ``len(bulk_metrics)`` is
100%-by-construction and is useless as a coverage measure. Verified by reading
`fmp_client.py`.

This module measures the screen. It does not change it.
"""
from __future__ import annotations

import pytest

from degraded_mode import (
    SCREENING_HEALTHY_COVERAGE,
    SCREENING_MIN_COVERAGE,
    assess_screening_sufficiency,
)


def _row(symbol, rev=0.2, pe=20.0, fcf=0.05):
    """A metrics row as get_fundamentals_v3 assembles it."""
    row = {"symbol": symbol}
    if rev is not None:
        row["revenueGrowth"] = rev
    if pe is not None:
        row["peRatio"] = pe
    if fcf is not None:
        row["freeCashFlowYield"] = fcf
    return row


def _full(n, prefix="S"):
    return [_row(f"{prefix}{i}") for i in range(n)]


# --------------------------------------------------------------------------
# Denominators — each must be explicit
# --------------------------------------------------------------------------

def test_healthy_full_coverage():
    syms = [f"S{i}" for i in range(503)]
    r = assess_screening_sufficiency(eligible_symbols=syms, requested_symbols=syms,
                                     metrics_rows=_full(503))
    assert r["eligible_symbols"] == 503
    assert r["fundamentals_requested"] == 503
    assert r["fundamentals_resolved"] == 503
    assert r["fundamentals_missing"] == 0
    assert r["screening_coverage"] == 1.0
    assert r["unscreened_count"] == 0
    assert r["status"] == "healthy"
    assert r["sufficient"] is True
    assert r["reasons"] == []


def test_coverage_denominator_is_eligible_not_requested():
    """The v3_max_symbols cap binding is exactly the defect we must surface:
    500 eligible but only 100 requested is 20% coverage, not 100%."""
    eligible = [f"S{i}" for i in range(500)]
    requested = eligible[:100]
    r = assess_screening_sufficiency(eligible_symbols=eligible,
                                     requested_symbols=requested,
                                     metrics_rows=_full(100))
    assert r["fundamentals_requested"] == 100
    assert r["fundamentals_resolved"] == 100
    assert r["screening_coverage"] == pytest.approx(0.2)
    assert r["unscreened_count"] == 400
    assert r["status"] == "unsafe"


def test_small_number_missing_is_measured_accurately():
    eligible = [f"S{i}" for i in range(503)]
    rows = _full(498) + [{"symbol": f"S{i}"} for i in range(498, 503)]
    r = assess_screening_sufficiency(eligible_symbols=eligible, requested_symbols=eligible,
                                     metrics_rows=rows)
    assert r["fundamentals_resolved"] == 498
    assert r["fundamentals_missing"] == 5
    # Coverage is rounded to 4dp for artifact stability.
    assert r["screening_coverage"] == pytest.approx(498 / 503, abs=1e-4)
    assert r["status"] == "healthy"


# --------------------------------------------------------------------------
# "Returned something" is not "resolved"
# --------------------------------------------------------------------------

def test_bare_row_with_only_symbol_does_not_count_as_resolved():
    """get_fundamentals_v3 emits exactly this shape when nothing resolved."""
    eligible = ["A", "B"]
    r = assess_screening_sufficiency(eligible_symbols=eligible, requested_symbols=eligible,
                                     metrics_rows=[{"symbol": "A"}, {"symbol": "B"}])
    assert r["fundamentals_resolved"] == 0
    assert r["screening_coverage"] == 0.0
    assert r["status"] == "unsafe"


def test_explicit_null_fields_do_not_count_as_resolved():
    eligible = ["A"]
    rows = [{"symbol": "A", "revenueGrowth": None, "peRatio": None,
             "freeCashFlowYield": None}]
    r = assess_screening_sufficiency(eligible_symbols=eligible, requested_symbols=eligible,
                                     metrics_rows=rows)
    assert r["fundamentals_resolved"] == 0


def test_non_numeric_field_does_not_count_as_resolved():
    eligible = ["A"]
    rows = [{"symbol": "A", "revenueGrowth": "n/a"}]
    r = assess_screening_sufficiency(eligible_symbols=eligible, requested_symbols=eligible,
                                     metrics_rows=rows)
    assert r["fundamentals_resolved"] == 0


def test_row_count_alone_never_implies_coverage():
    """The whole point: 503 rows returned, zero usable."""
    eligible = [f"S{i}" for i in range(503)]
    rows = [{"symbol": s} for s in eligible]
    r = assess_screening_sufficiency(eligible_symbols=eligible, requested_symbols=eligible,
                                     metrics_rows=rows)
    assert len(rows) == 503
    assert r["screening_coverage"] == 0.0


# --------------------------------------------------------------------------
# Partial rows are reported honestly, never rounded up to complete
# --------------------------------------------------------------------------

def test_partial_rows_are_counted_separately_from_complete():
    eligible = ["A", "B", "C", "D"]
    rows = [
        _row("A"),                                  # complete
        _row("B", pe=None, fcf=None),               # partial: primary only
        {"symbol": "C", "peRatio": 15.0},           # partial: no primary field
        {"symbol": "D"},                            # missing
    ]
    r = assess_screening_sufficiency(eligible_symbols=eligible, requested_symbols=eligible,
                                     metrics_rows=rows)
    assert r["rows_complete"] == 1
    assert r["rows_partial"] == 2
    assert r["rows_missing"] == 1
    # Only the PRIMARY screen field makes a name meaningfully screened.
    assert r["fundamentals_resolved"] == 2          # A and B carry revenueGrowth
    assert r["primary_field"] == "revenueGrowth"


def test_a_row_without_the_primary_field_is_not_resolved_even_if_other_fields_exist():
    """peRatio/fcf are guards; revenueGrowth is what min_rev_growth binds on.
    Without it the name is admitted unscreened — the observed defect."""
    eligible = ["C"]
    r = assess_screening_sufficiency(
        eligible_symbols=eligible, requested_symbols=eligible,
        metrics_rows=[{"symbol": "C", "peRatio": 15.0, "freeCashFlowYield": 0.03}])
    assert r["fundamentals_resolved"] == 0
    assert r["rows_partial"] == 1


# --------------------------------------------------------------------------
# Status bands
# --------------------------------------------------------------------------

def test_degraded_band_between_min_and_healthy():
    n = 100
    eligible = [f"S{i}" for i in range(n)]
    resolved = int(SCREENING_MIN_COVERAGE * n) + 2
    rows = _full(resolved) + [{"symbol": f"S{i}"} for i in range(resolved, n)]
    r = assess_screening_sufficiency(eligible_symbols=eligible, requested_symbols=eligible,
                                     metrics_rows=rows)
    assert r["status"] == "degraded"
    assert r["sufficient"] is True          # usable, but flagged
    assert any("degraded_screening_coverage" in x for x in r["reasons"])


def test_unsafe_below_minimum():
    n = 100
    eligible = [f"S{i}" for i in range(n)]
    rows = _full(10) + [{"symbol": f"S{i}"} for i in range(10, n)]
    r = assess_screening_sufficiency(eligible_symbols=eligible, requested_symbols=eligible,
                                     metrics_rows=rows)
    assert r["status"] == "unsafe"
    assert r["sufficient"] is False
    assert any("insufficient_screening_coverage" in x for x in r["reasons"])


def test_thresholds_are_ordered_constants():
    assert 0 < SCREENING_MIN_COVERAGE < SCREENING_HEALTHY_COVERAGE <= 1.0


# --------------------------------------------------------------------------
# Fail-closed on missing inputs — never read as 100%
# --------------------------------------------------------------------------

def test_no_eligible_symbols_is_not_coverage_of_one():
    r = assess_screening_sufficiency(eligible_symbols=[], requested_symbols=[],
                                     metrics_rows=[])
    assert r["screening_coverage"] is None
    assert r["sufficient"] is False
    assert r["status"] == "unknown"
    assert any("no_eligible_universe" in x for x in r["reasons"])


def test_missing_metrics_input_fails_closed():
    eligible = ["A", "B"]
    r = assess_screening_sufficiency(eligible_symbols=eligible, requested_symbols=eligible,
                                     metrics_rows=None)
    assert r["sufficient"] is False
    assert r["status"] in ("unsafe", "unknown")
    assert r["screening_coverage"] != 1.0


def test_garbage_metrics_rows_are_ignored_not_credited():
    eligible = ["A"]
    r = assess_screening_sufficiency(eligible_symbols=eligible, requested_symbols=eligible,
                                     metrics_rows=["nonsense", 42, None])
    assert r["fundamentals_resolved"] == 0
    assert r["sufficient"] is False


def test_rows_for_symbols_outside_the_eligible_set_do_not_inflate_coverage():
    """A stale cache could return rows for names no longer eligible."""
    eligible = ["A", "B"]
    rows = _full(0) + [_row("A"), _row("ZZZ"), _row("QQQ")]
    r = assess_screening_sufficiency(eligible_symbols=eligible, requested_symbols=eligible,
                                     metrics_rows=rows)
    assert r["fundamentals_resolved"] == 1
    assert r["screening_coverage"] == pytest.approx(0.5)


def test_payload_is_json_serializable_and_declares_its_fields():
    import json
    eligible = [f"S{i}" for i in range(10)]
    r = assess_screening_sufficiency(eligible_symbols=eligible, requested_symbols=eligible,
                                     metrics_rows=_full(10))
    json.dumps(r)
    assert r["screening_fields"] == ["revenueGrowth", "peRatio", "freeCashFlowYield"]
    assert r["healthy_threshold"] == SCREENING_HEALTHY_COVERAGE
    assert r["minimum_threshold"] == SCREENING_MIN_COVERAGE


def test_deterministic():
    eligible = [f"S{i}" for i in range(50)]
    a = assess_screening_sufficiency(eligible_symbols=eligible, requested_symbols=eligible,
                                     metrics_rows=_full(40))
    b = assess_screening_sufficiency(eligible_symbols=eligible, requested_symbols=eligible,
                                     metrics_rows=_full(40))
    assert a == b


def test_per_field_resolution_counts_expose_a_silently_inert_guard():
    """Live 2026-08-03: peRatio resolved 0/503 (stable/key-metrics has no
    peRatio; the v3 fallback only fires when key-metrics returns nothing), so the
    PE>50 bubble guard could never bind. A field at 0 must be nameable."""
    eligible = [f"S{i}" for i in range(10)]
    rows = [_row(f"S{i}", pe=None) for i in range(10)]
    r = assess_screening_sufficiency(eligible_symbols=eligible, requested_symbols=eligible,
                                     metrics_rows=rows)
    assert r["field_resolution"]["revenueGrowth"] == 10
    assert r["field_resolution"]["peRatio"] == 0
    assert r["field_resolution"]["freeCashFlowYield"] == 10
    assert r["inert_fields"] == ["peRatio"]
    # An inert secondary guard does not by itself make coverage unsafe: the
    # primary screen still bound on every name.
    assert r["status"] == "healthy"
    assert r["rows_complete"] == 0


def test_no_inert_fields_when_all_resolve():
    eligible = [f"S{i}" for i in range(5)]
    r = assess_screening_sufficiency(eligible_symbols=eligible, requested_symbols=eligible,
                                     metrics_rows=_full(5))
    assert r["inert_fields"] == []
    assert r["rows_complete"] == 5
