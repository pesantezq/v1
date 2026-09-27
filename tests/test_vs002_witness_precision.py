"""VS-002 dividend-adjustment witness: precision-aware interval monotonicity.

The witness's monotonicity test is derived from the frozen evidence's own
observed decimal resolution, not a fixed 1e-6 scalar: a backwards move in the
factor is a defect ONLY when the measured precision cannot explain it. All
fixtures are synthetic/offline (no vendor corpus, no network, no credential).
"""
from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path

import pytest

from portfolio_automation.vs002_evidence import builder as B
from portfolio_automation.vs002_evidence import consumer as CON
from portfolio_automation.vs002_evidence import contracts as C

from tests.test_vs002_evidence import (
    BENCH, SyntheticAdjustedProvider, _db, _scan_dates, fixed_clock)

CENT = Decimal("0.01")


def _bars_and_companion(n, adj_fn, close_fn, start="2020-01-02"):
    d0 = dt.date.fromisoformat(start)
    dates = [(d0 + dt.timedelta(days=i)).isoformat() for i in range(n)]
    bars = [C.BarRow(symbol="SPY", session_date=dates[i],
                     adj_close=adj_fn(i), volume=1000 + i) for i in range(n)]
    comp = [{"date": dates[i], "close": close_fn(i), "volume": 1000 + i}
            for i in range(n)]
    return bars, comp


# ── 1. the real 2021-09-28 / 2021-09-29 example is NOT a material reversal ──

def test_observed_2021_pair_is_not_a_material_reversal():
    prev = B.factor_interval(Decimal("405.67"), Decimal("433.72"), CENT, CENT)
    curr = B.factor_interval(Decimal("406.35"), Decimal("434.45"), CENT, CENT)
    assert not B.is_material_reversal(prev, curr)
    # the current interval's high sits above the previous interval's low
    assert curr["high"] >= prev["low"]
    # intervals match the production diagnostic bounds
    assert float(prev["low"]) == pytest.approx(0.935304628509, abs=1e-9)
    assert float(prev["high"]) == pytest.approx(0.935349250084, abs=1e-9)
    assert float(curr["low"]) == pytest.approx(0.935298247229, abs=1e-9)
    assert float(curr["high"]) == pytest.approx(0.935342793679, abs=1e-9)


# ── 2. a reversal larger than the uncertainty envelope MUST fail ───────────

def test_reversal_beyond_envelope_is_material():
    prev = B.factor_interval(Decimal("100.00"), Decimal("100.00"), CENT, CENT)
    curr = B.factor_interval(Decimal("90.00"), Decimal("100.00"), CENT, CENT)
    assert B.is_material_reversal(prev, curr)


# ── 3. exact boundary: curr.high == prev.low -> NOT material (strict <) ─────

def test_boundary_touch_is_not_material():
    prev = {"low": Decimal("0.900000000")}
    curr_equal = {"high": Decimal("0.900000000")}
    curr_below = {"high": Decimal("0.899999999")}
    assert not B.is_material_reversal(prev, curr_equal)   # equal -> pass
    assert B.is_material_reversal(prev, curr_below)       # strictly below -> fail


# ── 4-6. precision derivation is series-wide and evidence-derived ──────────

def test_series_wide_precision_ignores_trailing_zero_collapse():
    # 405.70 collapses to 405.7 through JSON floats; series max places = 2
    assert B.series_decimal_places([405.7, 405.67, 406.35]) == 2


def test_three_decimal_precision_is_detected():
    assert B.series_decimal_places([1.234, 1.2, 1.23]) == 3
    assert B.series_decimal_places([100.5, 100.25]) == 2


def test_witness_uses_independent_adjusted_and_companion_quanta():
    n = 260
    # companion close: 2 decimals; adjusted: 3 decimals -> q_close=0.01, q_adj=0.001
    close_fn = lambda i: round(400.0 + 0.05 * i, 2)
    adj_fn = lambda i: round(close_fn(i) * (0.95 + 0.05 * i / (n - 1)), 3)
    bars, comp = _bars_and_companion(n, adj_fn, close_fn)
    rep = B.dividend_adjustment_witness_report(bars, comp)
    assert rep["adjusted_precision"] == 3
    assert rep["companion_precision"] == 2
    assert rep["q_adj"] == "0.001" and rep["q_close"] == "0.01"
    assert rep["passed"], rep["findings"]


# ── a valid cent-quantized adjusted series passes despite rounding wiggle ───

def test_valid_cent_quantized_series_passes():
    n = 260
    close_fn = lambda i: round(400.0 + 0.05 * i, 2)
    adj_fn = lambda i: round(close_fn(i) * (0.95 + 0.05 * i / (n - 1)), 2)
    bars, comp = _bars_and_companion(n, adj_fn, close_fn)
    findings = B.verify_dividend_adjustment_witness(bars, comp)
    assert findings == [], findings


# ── 7. malformed / non-positive input fails closed ────────────────────────

def test_precision_none_on_nan():
    assert B.series_decimal_places([float("nan")]) is None
    assert B.series_decimal_places([1.0, float("inf")]) is None
    assert B.series_decimal_places([]) is None


def test_malformed_companion_fails_closed():
    n = 260
    close_fn = lambda i: round(400.0 + 0.05 * i, 2)
    adj_fn = lambda i: round(close_fn(i) * 0.97, 2)
    bars, comp = _bars_and_companion(n, adj_fn, close_fn)
    comp[10].pop("close")
    findings = B.verify_dividend_adjustment_witness(bars, comp)
    assert findings and "missing required field" in findings[0]


# ── 8. insufficient overlap still refused ──────────────────────────────────

def test_insufficient_overlap_refused():
    bars, comp = _bars_and_companion(50, lambda i: 100.0, lambda i: 100.0)
    findings = B.verify_dividend_adjustment_witness(bars, comp)
    assert findings and "overlapping benchmark" in findings[0]


# ── 9. final factor materially away from 1 still refused ───────────────────

def test_final_factor_away_from_one_refused():
    n = 260
    close_fn = lambda i: round(400.0 + 0.05 * i, 2)
    adj_fn = lambda i: round(close_fn(i) * 0.90, 2)   # factor stays ~0.90
    bars, comp = _bars_and_companion(n, adj_fn, close_fn)
    findings = B.verify_dividend_adjustment_witness(bars, comp)
    assert any("final dividend-adjustment factor" in f for f in findings)


# ── 10. masquerade (no adjustment) still refused ───────────────────────────

def test_masquerade_no_adjustment_refused():
    n = 260
    close_fn = lambda i: round(400.0 + 0.05 * i, 2)
    bars, comp = _bars_and_companion(n, close_fn, close_fn)   # adj == close
    findings = B.verify_dividend_adjustment_witness(bars, comp)
    assert any("NO dividend adjustment" in f for f in findings)


# ── an in-envelope material reversal integration case ──────────────────────

def test_midseries_material_reversal_integration():
    n = 260
    close_fn = lambda i: round(400.0 + 0.05 * i, 2)
    base = lambda i: round(close_fn(i) * (0.95 + 0.05 * i / (n - 1)), 2)
    def adj_fn(i):
        v = base(i)
        return round(v * 0.9, 2) if i == 150 else v   # a 10% dip, far > cent
    bars, comp = _bars_and_companion(n, adj_fn, close_fn)
    rep = B.dividend_adjustment_witness_report(bars, comp)
    assert not rep["passed"]
    assert rep["first_violation"] is not None
    assert rep["first_violation"]["date"] == bars[150].session_date


# ── 11. consumer replay == builder verdict, credential-free ────────────────

def _pkg(tmp, *, provider=None, scans=12):
    _db(tmp, scans=_scan_dates(scans))
    manifest = B.build(tmp, code_sha="t", generated_at="2026-09-06T00:00:00Z",
                       bar_provider=provider or SyntheticAdjustedProvider(),
                       bar_clock=fixed_clock())
    return tmp / B.DEFAULT_OUT_REL, manifest


def test_consumer_replay_matches_builder_verdict(tmp_path):
    root, m = _pkg(tmp_path)
    assert m["witness_result"] == "PASS"
    snap = CON.validate(root)              # credential-free replay must accept
    assert snap.has_bars
    # replay the witness directly from the frozen bytes -> same PASS verdict
    bench = B.full_panel_from_raw(snap.bars_raw, {BENCH})[BENCH]
    rep = B.dividend_adjustment_witness_report(bench, snap.bars_witness_raw["rows"])
    assert rep["passed"], rep["findings"]


# ── 12. tampering with frozen witness rows is still detected ───────────────

def test_witness_row_tamper_still_detected(tmp_path):
    root, _ = _pkg(tmp_path)
    wit = json.loads((root / "bars_witness_raw.json").read_text())
    wit["rows"][0]["close"] = wit["rows"][0]["close"] * 1.5
    (root / "bars_witness_raw.json").write_text(
        json.dumps(wit, indent=2, sort_keys=True))
    with pytest.raises(CON.SnapshotInvalid):
        CON.validate(root)


# ── 14-15. package contract unchanged: v1, seven artifacts, observe_only ───

def test_schema_and_witness_envelope_unchanged(tmp_path):
    root, m = _pkg(tmp_path)
    assert m["schema_version"] == "engineering.vs002_evidence.v1"
    names = {p.name for p in root.iterdir() if p.is_file()}
    assert names == {"signals.json", "returns.json", "bars.json", "bars_raw.json",
                     "bars_witness_raw.json", "bars_snapshots.json", "manifest.json"}
    wit = json.loads((root / "bars_witness_raw.json").read_text())
    assert wit["observe_only"] is True


def test_ratio_monotone_tolerance_is_removed():
    assert not hasattr(C, "RATIO_MONOTONE_TOLERANCE")
    assert C.RATIO_FINAL_TOLERANCE == 1e-4


# ── Codex P1: cumulative gradual decline (adjacent overlaps, total exceeds q) ─

def test_cumulative_gradual_decline_is_caught():
    """A gradual decline whose ADJACENT interval pairs each still overlap but
    whose CUMULATIVE drop far exceeds the uncertainty envelope must FAIL, via
    monotone-interval feasibility (running max of all prior lows). No adjacent
    pair alone is a material reversal, so an adjacent-only check would wrongly
    PASS this corrupt series."""
    n = 260
    close_fn = lambda i: 400.00
    lo_adj = round(400.00 - 0.01 * 130, 2)          # 398.70 after 130 cents
    def adj_fn(i):
        if i <= 130:
            return round(400.00 - 0.01 * i, 2)      # 400.00 -> 398.70
        frac = (i - 130) / (n - 1 - 130)
        return round(lo_adj + (400.00 - lo_adj) * frac, 2)   # ramp back to 400
    bars, comp = _bars_and_companion(n, adj_fn, close_fn)

    rep = B.dividend_adjustment_witness_report(bars, comp)
    assert not rep["passed"], "cumulative decline must be flagged"
    assert rep["first_violation"] is not None

    # ...yet NO adjacent pair on its own is a material reversal (the exact gap
    # the running-max feasibility test closes).
    q_adj = Decimal(1).scaleb(-B.series_decimal_places([b.adj_close for b in bars]))
    q_close = Decimal(1).scaleb(-B.series_decimal_places([c["close"] for c in comp]))
    ivs = [B.factor_interval(Decimal(str(b.adj_close)), Decimal(str(c["close"])),
                             q_adj, q_close) for b, c in zip(bars, comp)]
    assert not any(B.is_material_reversal(ivs[i - 1], ivs[i])
                   for i in range(1, len(ivs)))
