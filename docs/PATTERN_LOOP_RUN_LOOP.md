# Pattern-Improvement Loop — End-to-End Driver (`backtesting/run_loop.py`)

**Status:** observe-only · proposes-only · Step 5 (apply) inert/owner-gated
**Added:** 2026-06-05 · **Lens:** Quant (pairs with `backtesting/backtest_health.py`)

The connective tissue that makes the Pattern-Improvement Loop runnable
**end-to-end with one command**, instead of only as composable library pieces.
It chains Steps 1→4 and writes the loop's two review artifacts.

## What it does

| Step | Action | Module reused |
|---|---|---|
| 1 | Load the system's **real** emitted signals | `signal_sources` |
| 1b/3 | POC simulation (per-pattern, directional, per-regime) → `outputs/backtest/poc_simulation_results.json` | `poc_simulation_harness.run_poc` |
| 2 | Per-signal **out-of-sample** efficacy via walk-forward, grouped by registry `signal_id` | `walk_forward` |
| 4 | Convert OOS efficacy → small, guardrailed weight **proposals** → `outputs/policy/signal_weight_proposals.json` | `tuning_proposals` |

**Step 5 (governed apply) is never invoked here.** Applying approved proposals
is the protected, owner-gated path in `backtesting/registry_apply.py` and stays
inert. This driver only ever *proposes*.

## Signal → registry mapping (Step 1b)

`registry_signal_id()` maps each normalized signal to the registry `signal_id`
it scores against:

- `STRONG_MOVE` is direction-resolved to `STRONG_MOVE_UP` / `STRONG_MOVE_DOWN`
  (the registry keys are directional; the loaded family is not). Real signals
  default to `up` (long-only) until a price-series direction resolver lands.
- `VOLUME_SPIKE`, `BREAKOUT_PROXY` pass through (they match registry ids).
- `SIGNAL_SCORE`, `UNKNOWN` pass through too, so Step 4 flags them
  `unknown_signal` rather than silently dropping them.

## Usage

```bash
# Offline (default): deterministic synthetic prices, no FMP key. Validates wiring;
# synthetic outcomes are NOT a real-edge claim.
python -m backtesting.run_loop --signals-source outputs/latest/watchlist_signals.json
python -m backtesting.run_loop --history          # aggregate outputs/history snapshots

# REAL evidence: live FMP prices → real forward outcomes (needs FMP_API_KEY).
python -m backtesting.run_loop --history --live

# Tune folds to available history (defaults assume ~1y of daily snapshots):
python -m backtesting.run_loop --history --train-days 10 --test-days 5 \
    --step-days 5 --forward-days 3 --min-signals-per-fold 30

python -m backtesting.run_loop --no-write         # compute only, write nothing
```

**History requirement.** Meaningful OOS folds need enough dated snapshots:
the defaults (`train_days=252`) assume ~1 trading year. With only a few weeks of
`outputs/history`, shrink `--train-days/--test-days` or every fold reads
`insufficient` (honestly surfaced, never a crash).

## Output → health

Writing `signal_weight_proposals.json` flips `backtest_health` off
`proposals_missing`. With no real edge (offline, or thin history), the health
check reads `no_proposals` (AMBER) — the truthful state — not a false green. A
`--live` run with sufficient history is what produces real proposals.

## Boundaries

- Observe-only: reads signals + `config/signal_registry.yaml` read-only; writes
  only the two review artifacts via governed safe writers
  (`OutputNamespace.HISTORICAL` + `OutputNamespace.POLICY`).
- The registry is **byte-identical** before/after (asserted in tests).
- No protected scoring/decision/allocation logic is touched; no trades implied.

## Tests

`tests/test_run_loop.py` (11): registry-id mapping incl. direction resolution
and non-registry passthrough; the per-signal OOS bridge (grouping + proposal
input shape + insufficient-not-dropped); end-to-end writes of both artifacts
with `observe_only`/`proposed_only` asserted; the `proposals_missing` flag
clearing; the no-signals degraded path; the registry byte-identical invariant;
and a `main()` smoke test.
