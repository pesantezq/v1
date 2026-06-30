# Quant Feedback Attribution (Phase 5)

Status: **shipped** on `feat/complete-simulation-quant-governance-loop`.
Observe-only — produces evidence + proposals, never changes confidence,
weights, or any production state (Iron rule 3).

## What already existed

`pattern_learning` (efficacy by tag/source), `confidence_calibration` (5-bucket
calibration), `retune_impact_tracker` (by gauge fingerprint), and
`regime_performance` already compute slices of quant evidence. Phase 5 adds the
attribution that the Phase 4 context log makes possible.

## What Phase 5 added (`quant_feedback.py`)

`attribute_outcomes(context_records, outcome_map, dimension=…)` joins the Phase 4
decision-time context (regime / crowd-state / strategy / action **at decision**)
with matured outcomes and, per group, reports using the Phase 4 taxonomy:

- `n_samples`, `judgeable`, `hits`, `hit_rate` (only hit/miss in the
  denominator — neutral/unresolved/insufficient/invalidated excluded),
- `mean_return`, taxonomy counts, `sample_sufficient` (n ≥ 30),
- `mae` / `mfe` / `cost_adjusted_mean_return` — **declared by contract**,
  computed only where the source supports them (else `null` — no fabrication).

`build_quant_feedback()` emits `outputs/latest/quant_feedback.json` with
`by_regime` / `by_crowd_state` / `by_strategy` / `by_action`, a `fallback_rate`
(share of decisions captured under degraded data-quality), and an
`evidence_status` that distinguishes **insufficient evidence** (`n_resolved <
30`) from poor performance. Missing dimension values route to an `"unknown"`
bucket.

## Pipeline

`run_daily_safe.sh` **Stage 7i** runs after the context capture (7h). Smoke on
the first run: 47 context records, 0 resolved → `evidence_status:
insufficient` (honest — outcomes mature over 1/3/7 days).

## Boundary

No production mutation; no protected-score change. Deterministic; degrades to a
valid insufficient-evidence artifact when no outcomes have matured.

## Tests

`tests/test_quant_feedback.py` — attribution by regime/crowd/strategy with the
taxonomy + honest denominator, sample-sufficiency flag, unknown-bucket routing,
graceful degrade.
