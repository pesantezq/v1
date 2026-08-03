# tests/test_scanner_ranking_quality.py
"""Ranking-quality observability for scanner candidates — measurement only.

Proves the partial-screen degeneracy is gone (or visible if it returns). Under
`v3_max_symbols=100` full_scan emitted 100 candidates of which ~76 had no
fundamentals, scored identically, and formed an alphabetical tail.

Mechanism, verified in code: `full_scan` sorts by `score` alone with NO explicit
tiebreak. Python's sort is stable, so tied candidates keep their input order, and
`SP500Universe.get_symbols()` returns a `sorted()` list. The alphabetical tail is
therefore inherited input ordering, not a tiebreak rule.

This diagnostic MUST NOT change score or rank — asserted below.
"""
from __future__ import annotations

from degraded_mode import assess_ranking_quality


def _c(symbol, score):
    return {"symbol": symbol, "score": score}


def test_differentiated_ranking_is_not_flagged():
    cands = [_c(f"S{i}", 90 - i) for i in range(30)]
    r = assess_ranking_quality(cands)
    assert r["candidate_count"] == 30
    assert r["distinct_score_count"] == 30
    assert r["largest_tie_fraction"] < 0.5
    assert r["degenerate_ranking"] is False
    assert r["zero_variance"] is False


def test_large_tied_group_is_detected():
    """18 of 28 tied — the shape observed in the live watchlist digest."""
    cands = [_c(f"A{i}", 90 - i) for i in range(10)] + [_c(f"B{i}", 16.0) for i in range(18)]
    r = assess_ranking_quality(cands)
    assert r["largest_tie_group_size"] == 18
    assert r["largest_tie_fraction"] == round(18 / 28, 4)
    assert r["largest_tie_score"] == 16.0
    assert r["degenerate_ranking"] is True


def test_alphabetical_tie_tail_is_measured():
    """The exact partial-screen signature: a scored head, an alphabetical tail."""
    head = [_c("WDC", 70.6), _c("APH", 65.1), _c("NVDA", 64.0)]
    tail = [_c(s, 40.0) for s in ("AAPL", "ABNB", "ACGL", "ADM", "AEE")]
    r = assess_ranking_quality(head + tail)
    assert r["alphabetical_tie_tail_count"] == 5
    assert r["alphabetical_tiebreak_detected"] is True


def test_tail_sharing_a_score_but_not_alphabetical_is_not_counted():
    tail = [_c(s, 40.0) for s in ("ZZZ", "AAA", "MMM")]
    r = assess_ranking_quality([_c("WDC", 70.6)] + tail)
    assert r["alphabetical_tie_tail_count"] == 0
    assert r["alphabetical_tiebreak_detected"] is False


def test_single_element_tail_is_not_an_alphabetical_tail():
    r = assess_ranking_quality([_c("A", 9.0), _c("B", 8.0)])
    assert r["alphabetical_tie_tail_count"] == 0


def test_zero_variance_is_flagged_as_degenerate():
    r = assess_ranking_quality([_c(f"S{i}", 5.0) for i in range(12)])
    assert r["zero_variance"] is True
    assert r["degenerate_ranking"] is True
    assert r["largest_tie_fraction"] == 1.0


def test_empty_candidate_list_is_explicit_not_healthy():
    r = assess_ranking_quality([])
    assert r["candidate_count"] == 0
    assert r["distinct_score_count"] == 0
    assert r["degenerate_ranking"] is False      # nothing to be degenerate about
    assert r["insufficient_sample"] is True


def test_single_candidate_is_insufficient_sample():
    r = assess_ranking_quality([_c("NVDA", 70.0)])
    assert r["insufficient_sample"] is True
    assert r["degenerate_ranking"] is False


def test_missing_or_garbage_scores_do_not_crash_or_read_as_differentiated():
    cands = [{"symbol": "A"}, {"symbol": "B", "score": None},
             {"symbol": "C", "score": "x"}, "junk", None]
    r = assess_ranking_quality(cands)
    assert r["candidate_count"] == 3            # only the dict rows count
    assert r["scores_unparseable"] == 3
    assert r["distinct_score_count"] <= 1


def test_diagnostic_does_not_mutate_scores_or_order():
    cands = [_c("WDC", 70.6), _c("APH", 65.1), _c("AAPL", 40.0), _c("ABNB", 40.0)]
    before = [dict(c) for c in cands]
    assess_ranking_quality(cands)
    assert cands == before                       # no reordering, no score edits


def test_payload_is_json_serializable_and_deterministic():
    import json
    cands = [_c(f"S{i}", 90 - (i % 3)) for i in range(20)]
    a = assess_ranking_quality(cands)
    b = assess_ranking_quality(cands)
    assert a == b
    json.dumps(a)


def test_observed_post_fix_shape_is_healthy():
    """55 fully-screened candidates with genuine score spread — the target state."""
    cands = [_c(f"S{i}", 80 - i * 0.7) for i in range(55)]
    r = assess_ranking_quality(cands)
    assert r["degenerate_ranking"] is False
    assert r["distinct_score_count"] == 55
    assert r["alphabetical_tie_tail_count"] == 0
