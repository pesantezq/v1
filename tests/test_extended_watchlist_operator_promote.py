"""ExtendedWatchlist.promote_operator_approved — operator-gated promotion.

A human operator-approval is authoritative reinforcement: it bypasses the
multi-day-persistence / multi-theme gate that qualifies *automatic* discovery,
but still respects every other gate (static-skip, already-active reinforce,
capacity cap).
"""
from __future__ import annotations

from pathlib import Path

from watchlist_scanner.extended_watchlist import ExtendedWatchlist


def _ewl(tmp_path, **kw) -> ExtendedWatchlist:
    return ExtendedWatchlist(db_path=tmp_path / "p.db", **kw)


def test_operator_approval_promotes_without_persistence(tmp_path):
    # No persistence_7d, single theme — auto-discovery would skip this; the
    # operator click promotes it anyway.
    ewl = _ewl(tmp_path, max_symbols=5)
    res = ewl.promote_operator_approved("PANW", theme="cyber", confidence=0.9,
                                        static_watchlist=[])
    assert res["status"] == "promoted"
    assert "PANW" in {r["symbol"] for r in ewl.get_active_symbols()}


def test_operator_approval_respects_capacity(tmp_path):
    ewl = _ewl(tmp_path, max_symbols=2)
    ewl.promote_operator_approved("AAA", theme="t", confidence=0.9, static_watchlist=[])
    ewl.promote_operator_approved("BBB", theme="t", confidence=0.9, static_watchlist=[])
    res = ewl.promote_operator_approved("CCC", theme="t", confidence=0.9, static_watchlist=[])
    assert res["status"] == "skipped"
    assert res["reason"] == "extended_watchlist_full"
    assert "CCC" not in {r["symbol"] for r in ewl.get_active_symbols()}


def test_operator_approval_skips_static(tmp_path):
    ewl = _ewl(tmp_path, max_symbols=5)
    res = ewl.promote_operator_approved("AAPL", theme="t", confidence=0.9,
                                        static_watchlist=["AAPL"])
    assert res["status"] == "skipped"
    assert res["reason"] == "in_static_watchlist"


def test_operator_approval_reinforces_when_active(tmp_path):
    ewl = _ewl(tmp_path, max_symbols=5)
    ewl.promote_operator_approved("PANW", theme="cyber", confidence=0.9, static_watchlist=[])
    res = ewl.promote_operator_approved("PANW", theme="cyber", confidence=0.95, static_watchlist=[])
    assert res["status"] == "reinforced"
    # still a single active row
    assert [r["symbol"] for r in ewl.get_active_symbols()].count("PANW") == 1
