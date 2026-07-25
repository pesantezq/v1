---
description: Health + status review of the standalone Weekly ETF Bundle Watchlist. Runs the deterministic weekly_etf_bundles health assessor over the subsystem artifacts (analysis, scorecard, calibration, attribution, Strat Lab comparison, challenger registry, email receipt), triages GREEN/AMBER/RED, and emits a one-line heartbeat + structured body. Observe-only. Confirms the weekly ETF job is healthy and its governance invariants hold. Designed to run on demand and via the weekly cadence alongside the sim suite.
---

# Weekly ETF Bundle Analysis

Operational + health readout of the standalone Weekly ETF Bundle Watchlist
(`portfolio_automation/weekly_etf_bundles/run.py`, wrapper
`scripts/run_weekly_etf_bundles.sh`). Working dir `/opt/stockbot`. Observe-only —
never edits code/scoring/decision_plan; the weekly ETF job is fully isolated from
the daily pipeline and never feeds the production decision engine.

See `docs/WEEKLY_ETF_BUNDLES.md` for the full design.

## Step 1 — Read the health artifact + scorecard

```bash
.venv/bin/python -c "import json; from portfolio_automation.weekly_etf_bundles.health import load_health; print(json.dumps(load_health(root='.'), indent=2, default=str))"
```

Reads (all `outputs/weekly_etf_bundles/`): `health.json` (status + governance
invariants + signals), and — for the quant read — `scorecard.json`,
`calibration.json`, `attribution.json`, `strat_lab_comparison.json`,
`challenger_registry.json`, `email_receipt.json`.

```bash
.venv/bin/python -c "import json,pathlib; b=pathlib.Path('outputs/weekly_etf_bundles'); sc=json.loads((b/'scorecard.json').read_text()) if (b/'scorecard.json').exists() else {}; print('sample_status:', sc.get('sample_status'), '| matured:', sc.get('matured_prediction_count'), '| rel_hit:', sc.get('benchmark_relative_hit_rate'), '| IC:', sc.get('information_coefficient'))"
```

## Step 2 — Triage

- **RED** (governance breach / broken invariant — investigate immediately, do NOT
  act on outputs): any `reasons[]` entry prefixed `RED:` — e.g.
  `feeds_decision_engine_true`, `posture_invariant_broken`, `config_invalid`,
  `score_or_pct_out_of_bounds`, `enabled_bundle_missing`, `disabled_bundle_present`,
  `duplicate_prediction_ids`, `promotion_authority_breach`,
  `email_content_hash_mismatch`, `action_direction_emitted`,
  `forbidden_artifact_field`. RED here means an isolation/observe-only guarantee is
  in question — treat as a thing to VERIFY, and never approve a promotion off it.
- **AMBER** (degraded / inert / thin — report, don't alarm): `low_coverage`,
  `stale_symbols`, `failed_symbols`, `sample_insufficient|provisional`,
  `calibration_higher_buckets_underperform`, `analysis_status_*`,
  `health_absent` (pre-first-run inert state). A `promising_but_insufficient_sample`
  challenger is expected AMBER — it is NOT approvable.
- **GREEN**: `reasons` empty. The subsystem ran clean, all invariants hold.

## Step 3 — Governance verification (always)

Confirm the standing invariants (all must hold every run):
- `feeds_decision_engine == false`, `observe_only == true`,
  `production_gated == true`, `human_approval_required_for_production == true`.
- Every `pending_promotion_candidates[]` has `is_human_approved=false`,
  `production_mutation=false`, `feeds_decision_engine=false`,
  `target_lane="simulation"`. A candidate marked `ready_for_human_review` is a
  recommendation ONLY — champion change requires `schemas.is_human_approver`.
- No `outputs/latest/decision_plan.json` write and no approval record originate
  here. A challenger passing gates is the control WORKING — VERIFY it against the
  gate_result + registry; do not approve it here.

## Step 4 — Heartbeat + body

Emit one heartbeat line:
`weekly-etf: <GREEN|AMBER|RED> · mdd=<market_data_date> · bundles=<n>/<etf_count> · sample=<sample_status> · champion=<id>`

Then a structured body: status + reasons, bundle leaders/laggards from
`latest.json`, scorecard headline (sample-gated), calibration status, top Strat
Lab challenger + its gate status, and any data-quality warnings. On RED, name the
exact invariant and route to `portfolio-learning-loop-health` for a second read.

Observe-only. This review never mutates any artifact, never approves a promotion,
and never touches the daily pipeline.
