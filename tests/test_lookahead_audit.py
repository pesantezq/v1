"""
Tests for the F look-ahead audit (assert_no_lookahead) — the load-bearing safety
gate for sub-project F. The critical test injects a reconstructor that peeks at a
FUTURE row to set a COMPARED field, and asserts the truncation-equality audit
catches it. A clean reconstructor must pass.
"""

from __future__ import annotations

from backtesting.historical_signal_recon import assert_no_lookahead, reconstruct_signals


def _series():
    # alternating moves so several dates emit STRONG_MOVE signals
    rows, price = [], 100.0
    for d in range(1, 25):
        price *= 1.04 if d % 2 == 0 else 0.97
        rows.append({"date": f"2026-02-{d:02d}", "close": round(price, 2), "volume": 1_000_000})
    return rows


def test_clean_reconstructor_passes_audit():
    rep = assert_no_lookahead({"AAA": _series()}, sample=8)
    assert rep["look_ahead_clean"] is True
    assert rep["mismatches"] == []
    assert rep["dates_checked"] > 0


def test_future_peek_is_caught():
    # Leaky reconstructor: forces `direction` (a COMPARED field) from the LAST
    # row's close — a future-dependent value. Truncating the series changes the
    # last row, so the date's direction flips → the audit must flag the mismatch.
    def leaky(ticker, rows, **kw):
        sigs = reconstruct_signals(ticker, rows, **kw)
        srt = sorted([r for r in rows if r.get("date")], key=lambda r: r["date"])
        if srt:
            future_close = float(srt[-1]["close"])
            for s in sigs:
                s["direction"] = "up" if future_close >= 100.0 else "down"
        return sigs

    rep = assert_no_lookahead({"AAA": _series()}, reconstructor=leaky, sample=8)
    assert rep["look_ahead_clean"] is False
    assert rep["mismatches"]
