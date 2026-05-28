---
description: Yearly tool analysis — 365-day retrospective of the Portfolio Automation System across four lenses (developer / quant / process analyst / market expert). Surfaces lifetime tag efficacy, regime-conditional patterns, gauge era performance, discovery yield, cost vs portfolio value, and the operator decision queue retrospective. Designed to run via cron on January 1 at 10:00 UTC (or on demand for mid-year audits).
---

# Yearly Tool Analysis

System-wide 365-day retrospective at yearly cadence. Reads all 12 monthly
reports + lifetime pattern_efficacy_yearly (partitioned by gauge × regime)
+ lifetime audit log + lifetime signal_outcomes. Generates the annual
operator review document.

Runs annually at 10:00 UTC on January 1 (or invoked on demand for
mid-year audits, e.g. when a major refactor is being considered).
Working dir: `/opt/stockbot`.

---

## Step 1 — Read lifetime + 12-month history

**Read these artifacts** (degrade gracefully on any miss):

1. `outputs/latest/pattern_efficacy_yearly.json` → **partitioned_by_fingerprint_regime** is the prized input here; lifetime by_tag stats
2. `data/retune_audit_log.jsonl` → every entry; group by parameter; lifetime apply count, lifetime rollback count, monthly velocity trend
3. `data/gauge_versions.jsonl` → all eras + their durations
4. `outputs/performance/signal_outcomes.csv` → full lifetime universe-level stats
5. `outputs/regime/regime_performance.json` → full regime ledger
6. `docs/monthly_reports/<YYYY-MM>.md` (all 12 months) → roll-up of monthly verdicts
7. `data/monthly_check_state.json` → counts persisted across the year
8. `outputs/latest/discovery_pulse_status.json` history → annualized cost: actual_spend_year_usd
9. `outputs/portfolio/portfolio_snapshot.json` (current) + earliest archived snapshot → portfolio value change
10. `data/themes_catalog.json` + `outputs/history/*/theme_signals.json` → which themes actually drove discoveries vs noise

---

## Step 2 — Compute yearly metrics

### Developer lens
- **`lifetime_silent_failures_caught`** = count of content_liveness warns that recurred ≥3 days in a row (system observability score)
- **`feature_velocity`** = count of `outputs/latest/*.json` artifacts introduced this year (proxy for shipping cadence)
- **`test_coverage_delta`** = total tests passing now vs Jan 1 (or earliest archived)
- **`cron_uptime`** = ratio of dates with successful run_daily_safe completion / 365

### Quant lens
- **`lifetime_tag_efficacy`** — for each rationale_tag, lifetime hit_rate_1d × n_samples; rank by Sharpe-like ratio (mean_return / stdev)
- **`regime_x_tag_matrix`** — from pattern_efficacy_yearly.partitioned_by_fingerprint_regime: which (tag, regime) cells outperform; surface top 10 + bottom 10
- **`gauge_era_efficacy`** — each gauge fingerprint's lifetime hit-rate, mean-return, sample count, duration in days
- **`current_gauge_efficacy_vs_best`** — how does the live gauge compare to the best historical gauge?

### Process analyst lens
- **`audit_log_consistency`** — for each parameter, verify final config.json value equals (initial + sum of audit deltas)
- **`rollback_clusters`** — group rollbacks by week; flag weeks with ≥3 rollbacks as "loop instability events"
- **`operator_interventions`** — count of times apply_enabled was set to false; total downtime
- **`cost_per_resolved_signal`** — actual_spend_year_usd / total_resolved_signals_year (efficiency metric)

### Market expert lens
- **`memo_top_decision_lifetime_hit_rate`** — fraction of all memo-surfaced top-5 decisions that resolved positive
- **`discovery_yield_funnel`** — themes detected → candidates promoted → extended_watchlist active → resolved positive (5-stage funnel)
- **`sector_performance_table`** — per-sector annualized hit_rate from signal_outcomes (which sectors did the system actually pick well in?)
- **`regime_call_accuracy`** — when regime_performance flagged risk_on, did portfolio drawdown follow? When risk_off, was that vindicated?

---

## Step 3 — Yearly triage

Yearly is INSIGHT-FOCUSED, not RED-actionable. Goal: surface the year's
truth so the operator can make annual planning decisions.

**HEALTHY** (system is working as designed):
- `lifetime_tag_efficacy` shows positive ranking gradient (some tags clearly outperform others)
- `cron_uptime ≥ 0.95`
- `audit_log_consistency: pass` (no drift between log and current config)
- `current_gauge_efficacy_vs_best` shows current ≥ 90th percentile of historical

**DEGRADED-RECOVERING** (warts but trending right):
- `rollback_clusters` shows clusters early in the year but none in last 3 months
- `cron_uptime ∈ [0.85, 0.95]`
- `current_gauge_efficacy_vs_best` shows current within top 50th percentile
- `operator_interventions > 0` but each had clear cause + recovery

**STALE** (system hasn't evolved meaningfully):
- `feature_velocity == 0` (nothing shipped this year)
- `lifetime_tag_efficacy` shows no meaningful spread (all tags clustered around baseline)
- `discovery_yield_funnel` top 4 stages all show 0 conversion
- `apply_rate_per_week` averaged 0 for the year (learning loop unused)

**STRUCTURAL_ISSUE** (operator must redesign):
- `audit_log_consistency: FAIL` (config drift; audit log doesn't match current values)
- `current_gauge_efficacy_vs_best` shows current < 25th percentile (regression vs history)
- `cost_per_resolved_signal` > 10× last year's value
- `cron_uptime < 0.85`

---

## Step 4 — Agent dispatch (yearly cadence)

`portfolio-attribution-analyst` ALWAYS (year-end deep-dive):
- Lifetime tag × regime cross-tab analysis
- Identify the top 5 (tag, regime) cells worth amplifying in next year's parameters
- Identify the bottom 5 cells worth pruning from the taxonomy

`portfolio-learning-loop-health` ALWAYS:
- Audit log consistency check (does current config match log?)
- Rollback cluster analysis (root-cause any cluster)
- Recommend tag taxonomy adjustments (add high-signal new tags, retire low-signal ones)

`portfolio-architect` ALWAYS:
- Roadmap review: which planned features shipped, which didn't, what should drop off
- Identify next year's biggest architectural debt items
- Update `.agent/project_state.yaml:next_official_step` based on the year's learnings

`portfolio-discovery-health`: IF discovery_yield_funnel shows < 5% top-to-bottom conversion → run a deep review of the discovery pipeline

`portfolio-memo-reviewer`: IF `memo_top_decision_lifetime_hit_rate < 0.55` → review whether memo decision selection has been pulling from the right candidate pool

`portfolio-doc-writer` ALWAYS:
- Generates `docs/yearly_reports/<YYYY>.md` — the canonical year-end document
- Updates `docs/ARCHITECTURE.md` with major shifts
- Updates `docs/decision_engine.md` if scoring weights have drifted from defaults

---

## Step 5 — Output

**Lead line** (one of):
```
[HEALTHY]            yearly tool analysis YYYY: <one-line headline>
[DEGRADED-RECOVERING] yearly tool analysis YYYY: <main wart>; trend OK
[STALE]              yearly tool analysis YYYY: <stagnation summary>; consider <next-step>
[STRUCTURAL_ISSUE]   yearly tool analysis YYYY: <structural problem>; redesign needed
```

**Body** (up to 800 words for yearly — it's the longest report):

Sections (each 50-150 words):

1. **Year in numbers** — cron uptime, total runs, total cost, portfolio value change, feature velocity
2. **Developer lens** — silent failures caught + recovered, dependency drift events, test coverage trajectory
3. **Quant lens** — top 5 winning tags lifetime, top 3 winning (tag × regime) cells, best gauge era, current gauge percentile
4. **Process analyst lens** — audit log consistency, rollback clusters with root cause, operator interventions
5. **Market expert lens** — memo accuracy, discovery yield funnel, sector performance table, regime call accuracy
6. **Tag taxonomy proposals** — which tags to add (high evidence of unmodelled signals), which to retire (insufficient_sample for >12 months), which to merge
7. **Roadmap deltas** — which planned features shipped, which dropped, top 3 priorities for next year
8. **Operator action queue** (5 items max) — concrete annual planning decisions

---

## Step 6 — Persist yearly report

Write to `docs/yearly_reports/YYYY.md`. This becomes the input for the
next year's mid-year audit and the long-term system memory.

Update `data/yearly_check_state.json`:
- `last_run_at`, `last_verdict`, `lifetime_baseline_metrics`
- Snapshot of current config for next year's audit_log_consistency check
