# Sub-project D — Feedback Proposers (calibration + tagging) — Design

- **Date:** 2026-06-05
- **Branch:** feature/pattern-improvement-loop (continues after Foundation merged at 0b8c0812)
- **Status:** Design; autonomous build authorized ("complete through E"). Stop at production-activation boundary.
- **Sequence:** Foundation (merged) → **D (this)** → E (auto-apply).

## Context

The first `real_signals_live` run surfaced two system-quality defects independent of the
OOS-maturity clock:

1. **Tagging gap.** 602 / ~858 history signals carry NO `alert_basis` → classified
   `UNKNOWN`; and `SIGNAL_SCORE` (46 signals) maps to a family with **no registry
   `signal_id`**, so those signals can never receive a weight. Confirmed by counting
   `outputs/history/*/watchlist_signals.json`: only `price_move` (203), `volume_spike`
   (71), `signal_score` (46) tags appear; everything else is empty. So this is a
   missing-DATA problem (untagged rows), not a missing-MAPPING problem.
2. **Calibration inversion.** `calibration_slope = -11.345`; the 40-60 confidence band
   (+8.49%) outperformed the 80-100 band (+3.88%). Confidence is currently
   anti-predictive on the in-sample window.

Both are real now, but **any APPLY is OOS-gated**: a calibration correction fit on the
38-day in-sample window would overfit (same trap as the weight proposals), and a tagging
backfill should be validated before it reshapes attribution. So D ships two **observe-only,
proposes-only, owner-gated** proposers — the same discipline as `tuning_proposals` (Step 4):
detect, quantify, propose a bounded change as a review artifact; never mutate live config.

## Non-goals

- No mutation of `confidence_score` / scoring / `signal_registry.yaml` / the watchlist
  scanner. D only *proposes* (writes review artifacts under `outputs/policy/`).
- No auto-apply (that is E). No change to observe-only / owner-gated invariants.
- No upstream change to the live signal producer in this sub-project (a proposed
  backfill rule is emitted for owner review; wiring it into the scanner is a separate,
  explicitly-approved change).

## Architecture

Two new pure, observe-only producer modules under `backtesting/` (the Pattern-Loop home),
each: pure functions → `{observe_only:true, proposed_only:true, ...}` dict → JSON+MD
artifact under `OutputNamespace.POLICY` (review/governance) → degraded dict on failure,
never raises. Both are invoked (non-blocking) by `run_loop` after the existing Step 4 so
they ride the same monthly recompute, and both are paired with `backtest_health` flags +
the monthly skill.

### D1 — calibration_correction_proposer
- **New** `backtesting/calibration_proposer.py`.
- Pure `propose_calibration_correction(results: dict, *, min_band_n=20) -> dict`:
  reads `results["calibration"]` + the confidence buckets in `results["added_metrics"]`
  (band → count, hit_rate). Detects inversion (`calibration_slope < 0`, or Spearman of
  band-midpoint vs hit_rate < 0). Proposes a **monotone recalibration map**: for each
  confidence band with `n >= min_band_n`, the empirical hit-rate, isotonically smoothed
  to be non-decreasing, as the *suggested* calibrated confidence. Output:
  `{observe_only, proposed_only, status, calibration_slope, inverted: bool,
    bands: [{band, n, empirical_hit_rate, suggested_calibrated_conf}],
    apply_gate: "oos_unconfirmed" | "ready", rationale}`.
  `apply_gate` is `"oos_unconfirmed"` whenever `results["oos_window"].folds_possible`
  is false (always, until ~2027) — making explicit that this is a provisional map, not
  an apply-ready one.
- **New** `write_calibration_proposal(payload, base_dir)` → `OutputNamespace.POLICY`
  `calibration_correction_proposal.{json,md}` via the governed safe writers.

### D2 — signal_tagging_proposer
- **New** `backtesting/tagging_proposer.py`.
- Pure `propose_tagging_fixes(signals, *, registry_path) -> dict`: computes
  `untagged_count` / `untagged_pct` (rows whose `alert_basis` is empty/missing),
  the distribution of mapped families, and which mapped families have **no registry
  `signal_id`** (e.g. `SIGNAL_SCORE`). Proposes, as review items:
  (a) a **registry-entry proposal** for each mapped family missing a `signal_id`
      (id, suggested default_weight = neutral 0.0, rationale), and
  (b) a **backfill-inference proposal**: a deterministic rule to infer `alert_basis`
      for untagged rows from fields present on the row (e.g. `signal_score` present →
      add `signal_score`; large `volume_ratio` → `volume_spike`), reported as a rule
      spec + the count it would newly tag. Never applied here.
  Output: `{observe_only, proposed_only, status, total, untagged_count, untagged_pct,
    family_distribution, families_missing_registry_id, proposals: [...], rationale}`.
- **New** `write_tagging_proposal(payload, base_dir)` → `OutputNamespace.POLICY`
  `signal_tagging_proposal.{json,md}`.

### Integration (run_loop)
After Step 4 in `run_loop.run_loop`, wrapped in `try/except` (non-blocking), compute both
proposers from the already-loaded `signals` + the freshly-written `poc` payload and write
their artifacts. Add both to the returned summary (`calibration_proposal`,
`tagging_proposal` keys). No change to the existing Steps 1–4 flow or proposals.

### Health pairing (cadence-matched: monthly)
- `backtest_health.assess_backtest_health` reads the two new artifacts and adds AMBER
  flags: `calibration_correction_available` (inverted + a proposal exists) and
  `high_untagged_rate` (`untagged_pct >= 0.50`). No new RED tier.
- `.claude/commands/monthly-tool-analysis.md`: add the two artifacts to artifacts-read,
  a Quant/Developer-lens body line (untagged %, calibration inversion + provisional map
  availability), and a dispatch note to `portfolio-backtest-health` when either AMBER
  fires. Treat `apply_gate=="oos_unconfirmed"` as expected (accruing), not a fault.

## Error handling
Every producer: pure/total, returns a `{status:"degraded", error:...}` dict on any
exception, never raises. `run_loop` integration is `try/except` non-blocking. Missing
inputs (no calibration block, no signals) → `status:"insufficient"`, empty proposals.

## Testing
- `tests/test_calibration_proposer.py`: inverted-slope fixture → `inverted:true`,
  monotone (non-decreasing) `suggested_calibrated_conf`, `apply_gate:"oos_unconfirmed"`
  when window immature; well-calibrated fixture → `inverted:false`, no proposal; thin
  bands (`n<min_band_n`) excluded; empty/degraded input → no raise.
- `tests/test_tagging_proposer.py`: fixture with 60% empty `alert_basis` →
  `untagged_pct≈0.6`, backfill proposal present; `SIGNAL_SCORE` present + absent from
  registry → `families_missing_registry_id` includes it + a registry-entry proposal;
  fully-tagged fixture → no proposals; empty input → no raise.
- `tests/test_run_loop.py`: extend the oos_window test to assert the summary now also
  carries `calibration_proposal` + `tagging_proposal` keys (non-blocking presence).
- `tests/test_backtest_health.py`: assert the two new AMBER flags fire on degraded
  fixtures and are absent on healthy ones; tiers otherwise unchanged.
- Full suite green.

## Files
**New:** `backtesting/calibration_proposer.py`, `backtesting/tagging_proposer.py`,
`tests/test_calibration_proposer.py`, `tests/test_tagging_proposer.py`,
`docs/superpowers/specs/2026-06-05-pattern-loop-D-feedback-proposers-design.md`,
`docs/superpowers/plans/2026-06-05-pattern-loop-D-feedback-proposers.md`.
**Modified:** `backtesting/run_loop.py` (non-blocking integration + summary keys),
`backtesting/backtest_health.py` (read 2 artifacts + 2 AMBER flags),
`.claude/commands/monthly-tool-analysis.md`, `docs/CHANGELOG_DECISIONS.md`,
`.agent/project_state.yaml`.

## Risks
- Touches the same backtesting modules as Foundation, but all additive + observe-only.
- Calibration touches a PROTECTED concept (confidence) — mitigated: D only PROPOSES a
  review artifact and apply is OOS-gated; no scoring code changes. Flagged for the
  owner.
- Namespace: proposals → `OutputNamespace.POLICY` (governance/review), per the rules.
