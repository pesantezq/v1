# Regime Classifier — Neutral-Collapse Diagnosis & Fix

**Status:** simulation-lane fix validated; production promotion PENDING human approval.
**Work order:** `quant.regime_classifier_health` (operator-control plane).
**Quant-watch ledger:** `manual:regime_classifier_neutral_collapse` — kept ACTIVE / AMBER
(see "Ledger resolution gate" below).
**Branch:** `fix/regime-classifier-neutral-collapse` (not merged).

## Symptom

All 1286 rows of `outputs/performance/signal_outcomes.csv` carried the identical
triple:

```
regime_label = "neutral"   regime_confidence = 0.0   regime_data_quality = "limited"
```

The degenerate single bucket masked the volatility/trend regime that explains the
`f60e → d95e → e2b5` hit-rate / mean-return swing, feeding the recurring
favorable-baseline trap.

## Root cause — a producer-ordering bug, NOT a classifier defect

The classifier `market_regime.detect_market_regime` is healthy. For varied inputs
it emits all four implemented labels with confidence 0.77–0.88:

| inputs | label | confidence |
|---|---|---|
| up-trend + broad breadth | `risk_on` | 0.88 |
| down-trend + weak breadth | `risk_off` | 0.88 |
| elevated volatility proxy (≥3.0) | `high_volatility` | 0.83 |
| mixed trend + middling breadth | `neutral` | 0.77 |

The smoking gun was `regime_confidence = 0.0`. The classifier's arithmetic can
never emit 0.0 — its floor (base `0.45`, minus the `limited`/`degraded`
penalties) is ≈0.27. So the constant 0.0 could only have come from a
**record-time fallback literal**, never from a live classification.

Tracing the data flow backward through the producer:

`watchlist_scanner/__main__.py:run()` called the signal-feedback cycle (which
records each signal's regime tag) **before** it computed the regime:

```
run_signal_feedback_cycle(result, ...)        # records signals  ← ran FIRST
    └─ record_scan_signals(scan_result)
         └─ regime = scan_result.get("market_regime")  # {} — not set yet
            regime_label       = ... or "neutral"      # fallback
            regime_confidence  = ... or 0.0            # fallback
            regime_data_quality= ... or "limited"      # fallback
regime = detect_market_regime(...)            # computed AFTER
result["market_regime"] = regime              # attached AFTER — too late
```

Every recorded outcome row therefore got the constant `(neutral, 0.0, limited)`
fallback, regardless of the real market state.

There is **no `transition` label** in this system. The implemented regime
vocabulary is `{risk_on, risk_off, neutral, high_volatility}`; a transitional /
mixed condition resolves to `neutral` by design.

## Fix (simulation/test lane)

`watchlist_scanner/__main__.py`: moved the regime computation + attachment block
**ahead of** `run_signal_feedback_cycle`, so `result["market_regime"]` is
populated when `record_scan_signals` stamps each row. All dependencies
(`data_health`, `portfolio_construction`, prior regime, config) are already
available at the earlier point.

`watchlist_scanner/performance_feedback.py`: added a defense-in-depth WARNING log
when `record_scan_signals` records signals while `market_regime` is empty — so a
future producer-ordering re-regression surfaces instead of hiding behind the
silent fallback.

No threshold was changed. No scoring/decision/allocation semantics were touched.
`regime_label` is observe-only outcome metadata; it does not feed
`decision_engine.py`.

## Validation (simulation lane — `tools/validate_regime_collapse_fix.py`)

Writes `outputs/sandbox/regime_collapse_validation.{json,md}`. Before = production
CSV (read-only); after = corrected ordering replayed over a representative window
of varied market states into a throwaway sandbox DB.

| metric | before | after |
|---|---|---|
| distinct regime labels | 1 (`neutral`) | 4 |
| label transition frequency | 0 | 5 |
| collapse-triple rows | 1286 (100%) | 0 |
| by-regime avg confidence | 0.0 | 0.77–0.88 |
| by-regime buckets resolved | 1 | 4 |

Historical re-tagging of the 1286 production rows was **not** performed: the true
per-run inputs were never persisted (that is the bug) and `price_cache.json` holds
only current snapshots, so faithful re-tagging would require paid FMP historical
fetches. The fix corrects all future recordings; historical rows are left intact
as protected evidence.

## Tests

`tests/test_regime_classifier_degeneracy.py`:
- every valid label is individually reachable from deterministic synthetic inputs;
- the classifier confidence floor is never 0.0 (proving the constant came from the
  fallback);
- the live regime is persisted when attached before recording (the fix);
- the collapse is reproduced and locked when the regime is unset (failure mode);
- the defense-in-depth warning fires when the regime is unset;
- **degeneracy guard**: a varied input fixture must NOT collapse to one label,
  both at the classifier and at the producer (record→read) boundary.

## Ledger resolution gate

The `manual:regime_classifier_neutral_collapse` quant-watch entry stays ACTIVE /
AMBER. It is resolved by hand only when **either**:
1. a representative trailing window of **live** `signal_outcomes` rows contains
   more than one legitimate regime label (i.e. the fix is promoted to production
   and live runs accumulate diverse regimes), **or**
2. documented evidence shows neutral was genuinely correct for the period
   (it was not — see the classifier diversity above).

Production promotion (merge to `main`) remains human-gated; the simulation fix
passing does not auto-promote and does not auto-resolve the ledger.
