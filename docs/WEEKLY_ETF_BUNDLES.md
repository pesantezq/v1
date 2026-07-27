# Weekly ETF Bundle Watchlist

A **standalone, observe-only, weekly** subsystem that analyzes manually curated
ETF baskets, freezes every weekly ranking as an immutable prediction, matures
those predictions at multiple forward horizons, scores their quality, makes the
strategy available to Strat Lab for controlled improvement, and sends an
informational weekly email.

It is **fully isolated** from the daily recommendation/memo/watchlist/capital
packages and the production decision engine.

## Posture (hardcoded, non-conditional)

```
observe_only:                            true
simulation_active:                       true
production_gated:                        true
human_approval_required_for_production:  true
feeds_decision_engine:                   false
```

The subsystem never: creates trades/actions/allocations, modifies portfolio or
production-watchlist state, creates approval records, writes `decision_plan.json`,
feeds the production decision engine, auto-promotes a challenger, or auto-edits
curated bundle membership.

## Package layout

`portfolio_automation/weekly_etf_bundles/`
- `config.py` / `models.py` — load + validate `config/weekly_etf_bundles.yaml` (fail-closed; content-hashed).
- `analysis.py` — point-in-time ETF metrics + market/vol regime context (no future leak; raw-close price returns, same source as outcome eval).
- `scoring.py` — transparent, parameterized 0–100 watch score (renormalizes on missing components, never zero-coerces) + bundle breadth/dispersion/state.
- `predictions.py` — immutable, idempotent frozen prediction ledger (keyed on `market_data_date`; champion/challenger lanes).
- `outcomes.py` — forward maturation at 1/4/12/26w (4w primary); missing = pending/unresolvable, never a miss.
- `evaluation.py` — scorecard (hit rates, precision@K, Spearman ρ, information coefficient, top-bottom spread, by bundle/ETF/bucket/label/regime/horizon; sample-status gated).
- `calibration.py` — score-bucket calibration (Wilson intervals + status + higher-buckets-underperform warning).
- `attribution.py` — component/bundle/regime attribution → Strat Lab hypotheses (never auto-applies weights).
- `strat_lab_adapter.py` — `weekly_etf_bundles` family + 4 variants, walk-forward OOS comparison, promotion gates, challenger registry, PENDING-only human-gated promotion candidates.
- `engine_overlay.py` — simulation-only bounded (±0.05) context modifier A/B; imports nothing from `decision_engine`/`scoring`; writes only the SIMULATION namespace.
- `renderer.py` / `emailer.py` — MD+HTML digest (one shared section model) + standalone email (reuses memo transport; independent gates; dedup).
- `health.py` — GREEN/AMBER/RED health + governance-invariant assertions.
- `run.py` — standalone CLI orchestrator.

## Data flow

```
config/weekly_etf_bundles.yaml (human-owned)
  → load_config (validate + content hash)
  → governed_client("weekly_review") + load_price_panel (archive-first, FMP fallback)
  → build_weekly_analysis (point-in-time metrics + scores + regime)  → latest.{json,md,html}
  → freeze_predictions (champion; immutable, idempotent)             → predictions/<mdd>.json
  → mature_all_outcomes (1/4/12/26w)                                 → outcomes/<h>/<mdd>.json
  → build_scorecard / build_calibration / build_attribution         → scorecard/calibration/attribution.json
  → run_strat_lab_comparison (champion vs challengers, OOS)          → strat_lab_comparison.json, challenger_registry.json
  → build_health                                                     → health.json
  → send_weekly_etf_bundle_email (dry-run default)                   → email_receipt.json
```

## Scoring (v1 baseline)

Deterministic, absolute (no cross-sectional dependence) 0–100 components:

| component | weight |
|---|---|
| relative_strength_12w | 0.30 |
| momentum_4w | 0.20 |
| trend_structure | 0.20 |
| distance_from_52w_high | 0.10 |
| volatility_adjusted_return | 0.10 |
| drawdown_resilience | 0.10 |

Labels (informational only): 80–100 leading · 65–79 strengthening · 45–64 mixed
· 30–44 weakening · 0–29 lagging.

**Price basis:** returns are **price returns (raw close, ex-distributions)**,
consistent with the `portfolio_sim` suite and, critically, identical between
prediction time and outcome time so a frozen ranking never drifts against its own
evaluation. Adjusted-close total return is a documented future enhancement.

## Champion / challenger + promotion

- The approved config is the **champion**; only it appears in the operator email.
- 4 variants: `v1_baseline`, `v2_momentum_heavy`, `v3_breadth_adjusted`,
  `v4_regime_conditioned`. Challengers score the same weekly data and mature on
  the same horizons; results stay in simulation artifacts.
- Promotion is **never automatic**. Clearing every deterministic gate (see
  `DEFAULT_GATES`) yields a **pending** candidate stamped with the four
  sim-governance authority invariants (`target_lane="simulation"`,
  `production_mutation=False`, `feeds_decision_engine=False`,
  `is_human_approved=False`). Changing the champion requires
  `sim_governance.schemas.is_human_approver`. A challenger with good metrics but
  thin sample is labeled `promising_but_insufficient_sample` and is not approvable.

## Simulation-only decision-engine overlay

`engine_overlay.py` tests whether ETF-bundle context could improve selection
*inside simulation only*. It emits a bounded contextual signal (`|modifier| ≤
0.05`, clamped at apply time, mirroring `apply_sentiment_tilt`), applies it to a
copy of baseline scores (returns a number — never an action), and A/B-compares
top-k selection. It imports nothing from `decision_engine`/`scoring`, writes only
the SIMULATION namespace, and keeps `feeds_decision_engine=False`. Full portfolio
CAGR/Sharpe A/B via the `portfolio_sim` engine is a documented future extension.

## Outputs (namespace `OutputNamespace.WEEKLY_ETF_BUNDLES` → `outputs/weekly_etf_bundles/`)

`latest.{json,md,html}`, `health.json`, `scorecard.json`, `calibration.json`,
`attribution.json`, `strat_lab_comparison.json`, `challenger_registry.json`,
`email_receipt.json`, `predictions/<mdd>.json`,
`outcomes/{1w,4w,12w,26w}/<mdd>.json`. The engine overlay writes
`outputs/simulation/weekly_etf_bundle_engine_overlay.json`; the email dedup log is
`outputs/policy/weekly_etf_email_log.jsonl`.

## Orchestration & scheduling

Dedicated wrapper `scripts/run_weekly_etf_bundles.sh` with its **own flock lock +
log** — isolated from both `run_daily_safe.sh` and `run_weekly_safe.sh`. Suggested
operator crontab (VPS), after the weekly watchlist rebuild:

```
30 8 * * 1  /opt/stockbot/scripts/run_weekly_etf_bundles.sh
```

CLI:

```bash
python -m portfolio_automation.weekly_etf_bundles.run --as-of 2026-07-24 --email-dry-run
# modes: --analysis-only --mature-outcomes --evaluate --render-only
#        --email-dry-run --send-email --force-send
```

## Feature gates (default INERT)

```
WEEKLY_ETF_BUNDLES_ENABLED=0
WEEKLY_ETF_BUNDLES_EMAIL_ENABLED=0
WEEKLY_ETF_BUNDLES_EMAIL_DRY_RUN=1
```

Sending requires BOTH `--send-email` (dry_run=false) AND
`WEEKLY_ETF_BUNDLES_EMAIL_ENABLED=1`. Duplicate sends are suppressed by
`message_type + market_data_date + recipient_set + content_hash`
(`WEEKLY_ETF_BUNDLES_EMAIL_FORCE=1` to bypass).

## Health & analysis coverage

`weekly_etf_bundles/health.py` emits `health.json` (GREEN/AMBER/RED). The
`/weekly-etf-analysis` skill consumes it + the scorecard/calibration/strat-lab
artifacts, triages, and verifies the governance invariants; it is a member of
`/run-all-weekly`. RED = a broken invariant/governance breach (verify, never act
on); AMBER = degraded/thin/inert; GREEN = clean.
