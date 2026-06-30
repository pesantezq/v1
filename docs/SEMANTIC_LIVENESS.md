# Semantic-Liveness / Degeneracy Monitoring (Phase 6)

Status: **shipped** on `feat/complete-simulation-quant-governance-loop`.
Observe-only meta-monitor — never RED, never mutates decisions/scores/production.

Catches pipelines that are technically green but **semantically broken**: a
field collapsed to one value, excessive default usage, zero variance, or an
expected class that disappeared.

## What already existed
`quant_watch_probes.py` (3 detectors + a manual probe, e.g.
`manual:regime_classifier_neutral_collapse`) and `daily_run_status`
content-liveness. Phase 6 adds the **reusable generic detector framework** that
was missing.

## Detectors (`semantic_liveness.py`, pure)
- `detect_single_value_collapse(values, min_sample, allowed_single_values)` —
  one unique value over a varied window; guarded by `min_sample` and a
  documented-exception allow-set (a legitimately calm "neutral" regime is NOT a
  defect).
- `detect_excessive_default(values, default, max_default_frac)` — default value
  dominates (e.g. the 0.55 priority fallback plateau).
- `detect_zero_variance(values)` — numeric field with no spread.
- `detect_class_disappearance(current, expected)` — an expected class vanished.
- `detect_low_cardinality(values, min_distinct)`.

All return `None` (healthy) or an AMBER finding dict. **Guards** (`min_sample`,
documented exceptions) ensure legitimately single-state windows do not
false-positive.

## Runner + routing
`run_semantic_liveness()` applies the detectors to live fields (regime labels
with the `neutral` exception, decision-priority default plateau, …), writes
`outputs/latest/semantic_liveness_status.json` (AMBER-max), and routes sub-RED
findings to the quant-watch ledger for continuity. Pipeline: `run_daily_safe.sh`
**Stage 13b** (meta-monitor, before the manifest-complete stage). Smoke: green
(regime `neutral` is an allowed single state; priorities under threshold).

## Tests
`tests/test_semantic_liveness.py` (11) — each detector's positive + guarded
negative cases (incl. the documented single-state exception) + observe-only
runner degrade.
