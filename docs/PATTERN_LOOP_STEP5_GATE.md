# Step 5 Safety Gate — Protected-Score Value Regression (`backtesting/score_invariance_gate.py`)

**Status:** observe-only · pre-apply precondition · Step 5 (apply) remains inert/owner-gated
**Added:** 2026-06-05 · **Lens:** Quant + Developer (pairs with `backtest_health`)

This is **precondition #2** for ever executing a governed weight apply (Step 5):
a value-regression gate that proves applying a registry `default_weight` delta is
**semantically safe for the six protected scores**.

## What it does

Copies the registry to a temp file, applies a candidate delta to the **temp copy**
via `registry_apply.apply_approved_changes`, recomputes the protected scores over
a fixed offline fixture before and after, and asserts **no score value changed**.

| Verdict | Meaning |
|---|---|
| `GREEN` | The apply moved the registry weight, yet every protected score is bit-identical (the expected, currently-architected outcome). |
| `RED` | A protected score changed after the apply — a **coupling regression**. Hard block on any live Step 5 apply; route to re-review. |
| `inconclusive` | The apply was a no-op (delta capped, unknown signal, registry unreadable) — invariance can't be judged. |

## Why bit-identical, not "bounded"

Tracing on 2026-06-05 established that the registry `default_weight` is **not read
by any scoring function**. The six protected scores are computed by:

- `signal_score` — `scanner._compute_signal_score` (market data)
- `confidence_score` — `confidence.compute_confidence` (data-provenance)
- `effective_score` — `postprocess` (`= signal_score × confidence_score`)
- `conviction_score` — `conviction.apply_conviction_layer` (blend of effective + confidence)
- `final_rank_score` — `alert_ranking.apply_priority_score` (uses `config/base.json` "ranking" coefficients, **not** the registry)
- `recommendation_score` — `tools.policy_recommender` (separate allocation subsystem)

None read the registry. So a Step 5 apply **must** leave every score untouched.
The gate locks that invariant in: if a future change ever wires `default_weight`
into a score, the gate flips RED and forces re-review before any apply.

> **Material consequence:** because `default_weight` is decoupled, applying a
> proposed weight via Step 5 today changes the YAML value but **does not change
> any decision**. The Pattern-Loop's weight proposals have no scoring consumer
> yet. Wiring `default_weight` into scoring is a *protected-scope* change that
> needs explicit owner approval — it is intentionally **not** done here.

## Probe availability

`signal_score` is computed in `watchlist_scanner/scanner.py`, which imports
`pandas`; in a bare venv that probe is skipped (`unavailable_probes`) and the gate
runs on `confidence_score` + `final_rank_score`. On the operator's complete env
all probes run. The gate compares only scores computed on **both** sides.

## Usage

```python
from backtesting.score_invariance_gate import assert_scores_invariant_across_apply
v = assert_scores_invariant_across_apply(target_signal_id="STRONG_MOVE_UP", sample_delta=0.05)
assert v["status"] == "GREEN"   # required before approving a live apply
```

Wired into the yearly Quant-lens review via
`backtest_health.assess_backtest_health(run_score_gate=True)`, which adds the RED
flag `score_coupling_regression` if the gate is RED.

## Boundaries

- Observe-only: operates entirely on a temp copy; the live
  `config/signal_registry.yaml` is **byte-identical** before/after (asserted).
- No protected scoring/decision/allocation logic is touched or modified.
- Step 5 live apply remains owner-gated and inert.

## Tests

`tests/test_score_invariance_gate.py` (6): real-score computation + range/NaN;
GREEN when the weight moves but scores don't; RED via an injected registry-coupled
probe (proves the detection works); live registry byte-identical; `inconclusive`
on a capped delta and on an unknown signal. Plus 2 in `tests/test_backtest_health.py`
for the opt-in `run_score_gate` path.
