# Decision-Time Context Capture & Outcome Maturation (Phase 4)

Status: **shipped** on `feat/complete-simulation-quant-governance-loop`.
Observe-only. Does NOT mutate the protected stored win-rate.

## What already existed

`portfolio_automation/decision_outcome_tracker.py` already snapshots decisions
to `decision_outcomes.jsonl` (run_id, symbol, decision, price_at_decision,
confidence, validation_status), matures them at **1/3/7d**, computes
`direction_correct` (HOLD → `None`, excluded from the denominator), and
aggregates a hit-rate. That stored win-rate is **protected** (it feeds
`performance_feedback`), so Phase 4 leaves it untouched.

## What Phase 4 added (`decision_context_capture.py`)

A complementary, additive layer — exactly the pattern `memo_coherence` used to
add a neutral band without changing stored history:

### 1. Immutable at-decision context
`capture_decision_context()` records, per production decision: `run_id`,
`strategy_id`, `symbol`, `action`, `amount_or_weight`, `reference_price`,
`timestamp`, `horizons` (contract) + `resolved_horizons`, and the **decision-time
conditions** — `regime_at_decision`, `crowd_state_at_decision`,
`factor_state_at_decision`, `confidence_at_decision`, `data_quality_state`, plus
the frozen Phase 2 `snapshot_hash` and `source_refs`.

Written **append-only** to `outputs/policy/decision_context_log.jsonl`,
idempotent per `run_id` — decision-time evidence is **never overwritten** by
later outcomes (Iron rule 6). Historical rows that never persisted point-in-time
inputs are **not** retro-reconstructed.

### 2. Explicit outcome taxonomy + return neutral band
`classify_outcome()` maps an outcome to one of:
`hit · miss · neutral · unresolved · insufficient_data · invalidated`.
Precedence: data-quality (`invalid`→`invalidated`, `insufficient`→
`insufficient_data`) → unresolved → **±1% neutral band** (sub-band move is
noise) → direction. `is_counted()` / `counted_hit_rate()` ensure **only hit/miss
enter the denominator** — neutral, unresolved, insufficient, invalidated are
excluded (honest denominators). This is applied here only, never to the stored
win-rate.

### 3. Horizon contracts
`RESOLVED_HORIZONS = [1,3,7]` (matured today); `CONTRACT_HORIZONS =
[1,3,7,21,63]` — 21/63 are declared by contract but **not forced** (the source
data / current design do not yet support them safely).

## Pipeline

`run_daily_safe.sh` **Stage 7h** runs `run_decision_context_capture()` after the
Phase 2 snapshot (7g), reading the live decision plan + regime + unified crowd
and binding to the frozen `snapshot_hash`. Smoke: 47 decisions captured.

## Boundary

No change to `decision_engine.py`, `decision_outcome_tracker` math,
`performance_feedback` win-rate, or any protected score semantics. Append-only;
observe-only; deterministic (injected `now`).

## Tests

`tests/test_decision_context_capture.py` — horizon contracts, full taxonomy +
neutral band, counted-hit-rate exclusion, immutable context capture,
append-only idempotency.

## Consumed by

Phase 5 (quant feedback) reads the context log to attribute performance by
regime / crowd-state / strategy / horizon using the standardized taxonomy.
