#!/usr/bin/env python3
"""
DIAGNOSTIC (read-only): prove WHY the weekly_refresh dropped 20 cached
candidates to 0 on 2026-05-28.

Hypothesis: the first weekly run fetched fundamentals AFTER the FMP daily
budget hit 500/500, so get_fundamentals_v3 returned rows lacking
'revenueGrowth' -> _passes_hard_filters appends "rev_growth=N/A" -> every
symbol fails -> 0 survivors.

This harness runs the EXACT production code path (CandidateScanner.weekly_refresh)
under two controlled conditions and prints per-symbol pass/fail reasons.

It does NOT call save_watchlist, does NOT touch portfolio.db, does NOT mutate
any artifact. Pure read + FMP fetch (condition B consumes a few dozen calls).
"""
import json
from pathlib import Path

from utils import load_env, load_config
from fmp_client import FMPClient
from scanner.candidate_scanner import CandidateScanner

load_env()
config = load_config("config.json")

scanner = CandidateScanner(
    min_mkt_cap=float(config.scanner.get("min_mkt_cap", 5e9)),
    min_rev_growth=float(config.scanner.get("min_rev_growth", 0.15)),
    trend_filter_200dma=bool(config.scanner.get("trend_filter_200dma", True)),
    top_k=int(config.scanner.get("top_k_watchlist", 100)),
)
print(f"Scanner thresholds: min_mkt_cap={scanner.min_mkt_cap/1e9:.0f}B  "
      f"min_rev_growth={scanner.min_rev_growth:.0%}  "
      f"trend_200dma={scanner.trend_filter_200dma}")

# Reconstruct the cached watchlist rows from the fmp_top100-sourced names in
# top100_weekly.json (the universe-sanitation union that survived tonight).
union = json.loads(Path("outputs/latest/top100_weekly.json").read_text())
rows = [
    {"symbol": c["symbol"], "mkt_cap": 50e9, "sector": c.get("sector", "")}
    for c in union.get("candidates", [])
    if "fmp_top100" in (c.get("sources") or [])
]
symbols = [r["symbol"] for r in rows]
print(f"\nReconstructed {len(rows)} fmp_top100 watchlist rows: {symbols}")

# ── Condition A: reproduce the failure (empty metrics, like budget-blocked) ──
print("\n" + "=" * 70)
print("CONDITION A — empty bulk_metrics (simulates budget-exhausted fetch)")
print("=" * 70)
cand_a, dbg_a = scanner.weekly_refresh(rows, bulk_metrics=[], batch_quotes={})
print(f"RESULT: {len(cand_a)} candidates survived (expected 0)")
reasons = {}
for d in dbg_a:
    reasons.setdefault(d["failed_filters"], []).append(d["symbol"])
for reason, syms in reasons.items():
    print(f"  [{len(syms):2d}] failed_filters='{reason}'  e.g. {syms[:5]}")

# ── Condition B: fresh budget, real fetch ────────────────────────────────────
print("\n" + "=" * 70)
print("CONDITION B — real metrics+quotes via fresh FMP budget")
print("=" * 70)
fmp = FMPClient(daily_budget=config.fmp_daily_calls_budget)
print(f"FMP calls_today BEFORE fetch: {fmp.calls_today}")
bulk_metrics = fmp.get_fundamentals_v3(symbols)
batch_quotes = fmp.get_batch_quotes(symbols)
print(f"FMP calls_today AFTER fetch:  {fmp.calls_today}")

# Show what revenueGrowth actually came back per symbol
mm = {r["symbol"]: r for r in bulk_metrics if r.get("symbol")}
have_rg = [s for s in symbols if mm.get(s, {}).get("revenueGrowth") is not None]
print(f"Symbols with non-null revenueGrowth: {len(have_rg)}/{len(symbols)}")

cand_b, dbg_b = scanner.weekly_refresh(rows, bulk_metrics, batch_quotes)
print(f"RESULT: {len(cand_b)} candidates survived")
for d in dbg_b:
    rg = mm.get(d["symbol"], {}).get("revenueGrowth")
    print(f"  {d['symbol']:6s} passed={str(d['passed']):5s} "
          f"score={d['score']:5.1f} revGrowth={rg}  "
          f"{('FAIL: '+d['failed_filters']) if d['failed_filters'] else ''}")

print("\n" + "=" * 70)
print("VERDICT")
print("=" * 70)
print(f"A (empty metrics):  {len(cand_a)} survivors")
print(f"B (real metrics):   {len(cand_b)} survivors")
a_all_revgrowth = all(d["failed_filters"] == "rev_growth=N/A" for d in dbg_a)
print(f"A failure reason uniformly 'rev_growth=N/A': {a_all_revgrowth}")
