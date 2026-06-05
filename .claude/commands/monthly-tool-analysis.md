---
description: Monthly tool analysis — 30-day retrospective of the Portfolio Automation System across four lenses (developer / quant / process analyst / market expert). Surfaces pattern-efficacy trends, retune-apply audit summary, AI+FMP spend trajectory, memo-vs-outcome accuracy, and discovery yield. Designed to run via cron on the 1st of each month at 09:30 UTC.
---

# Monthly Tool Analysis

System-wide 30-day retrospective at monthly cadence. Reads rolling history,
surfaces what changed, recommends adjustments. Companion to
`daily-tool-analysis` (real-time triage) and `yearly-tool-analysis`
(long-term regime + lifetime view).

Runs at 09:30 UTC on the 1st of each month. Working dir: `/opt/stockbot`.

---

## Step 1 — Read 30-day history + monthly summaries

**Read these artifacts** (degrade gracefully on any miss):

1. `outputs/latest/pattern_efficacy_monthly.json` → by_tag winners/losers/insufficient_sample counts; match_rate trend
2. `outputs/latest/gate_retune_suggestions.json` → weight_proposals lifetime, auto_applicable_count
3. `data/retune_audit_log.jsonl` → all entries with `ts ≥ now - 30d` grouped by parameter; rollback count; magnitude distribution
4. `data/retune_auto_apply_state.json` → monthly_drift values + remaining headroom; pending_confirmations queue
5. `outputs/latest/discovery_pulse_status.json` → MTD: total_runs_month, skipped_runs_month, openai_cost_usd_month, fmp_calls_month
6. `outputs/latest/ai_budget_summary.json` → monthly_cost_total_usd actual vs $20 cap
7. `outputs/latest/top100_monthly.json` → source_breakdown trend, sector distribution, NET-NEW discoveries seen this month
8. `outputs/history/<dates>/decision_plan.json` (last 30 dates) → decision-type histogram, hit-rate of each decision-type (joined to signal_outcomes)
9. `outputs/history/<dates>/daily_memo.md` (last 30 dates) → memo top-decision retention rate (how often did decisions persist day-to-day)
10. `outputs/performance/signal_outcomes.csv` (last 30d) → universe-level hit-rate, mean return
11. `outputs/regime/regime_performance.json` → per-regime efficacy summary
12. `outputs/backtest/poc_simulation_results.json` → Pattern-Loop OOS sim: read `oos_window` (maturity countdown), `performance.evaluated`, `calibration.calibration_slope`, `added_metrics.per_regime`
13. `outputs/policy/signal_weight_proposals.json` → Step 4 weight proposals: read `summary.proposed_count`
14. `outputs/policy/calibration_correction_proposal.json` → D1 calibration proposer: read `inverted`, `apply_gate` (provisional until OOS matures)
15. `outputs/policy/signal_tagging_proposal.json` → D2 tagging proposer: read `untagged_pct`, `families_missing_registry_id`, `proposals`
16. `outputs/policy/auto_apply_audit.json` → E auto-apply audit: read the last entry's `status` (disabled/oos_immature/gpt_vetoed/applied/rolled_back) + provenance

---

## Step 2 — Compute monthly metrics

### Developer lens
- **`apply_rate_per_week`** = `count(audit_log entries last 30d with applied_by=auto) / 4.3` — auto-apply velocity
- **`rollback_ratio`** = `rollbacks_30d / applies_30d` (lower is better; ≥0.20 is concerning)
- **`pulse_skip_rate`** = `skipped_runs_month / (total_runs_month + skipped_runs_month)` — cap-pressure indicator
- **`cron_health`** = count of dates in last 30 with complete `outputs/history/<date>/` archives

### Quant lens
- **`tag_efficacy_drift`** = for each tag present in both pattern_efficacy_weekly (4 weeks ago) and pattern_efficacy_monthly (now), the delta in vs_baseline_pp
- **`winning_tag_count`** = tags with significance ∈ {winner, strong_winner} this month
- **`new_winners_this_month`** = winning tags that were neutral/insufficient last month
- **`fingerprint_stability`** = days since current gauge fingerprint first_seen_at
- **`oos_window_maturity`** = from `poc_simulation_results.json.oos_window`: `calendar_days_observed`/`full_window_days` (315), `folds_possible`, `full_window_eta`. The Pattern-Loop walk-forward cannot emit out-of-sample evidence until the window matures (first folds ~2027-01, full window ~2027-03). **While `folds_possible == false`, `signal_weight_proposals.json.summary.proposed_count == 0` is EXPECTED and healthy** — report it as "accruing", never as a failure.

### Process analyst lens
- **`drift_cap_utilization`** = for each parameter in monthly_drift, current_drift / 0.25 expressed as %
- **`pending_confirmation_age_max`** = oldest pending confirmation in days
- **`pulse_cost_pace`** = monthly_cost_usd / (days_into_month / 30) — projected monthly burn

### Market expert lens
- **`memo_top_decision_hit_rate`** = fraction of memo's top-5 decisions over last 30d that had positive 1d outcome
- **`net_new_discovery_yield`** = count of tickers that entered the universe via theme_candidate AND later resolved with hit
- **`sector_rotation_score`** = entropy of sector distribution across the month's top100_daily snapshots (higher = more rotation)
- **`regime_consistency`** = % of days in dominant regime; flags whipsawing months

---

## Step 3 — Triage at monthly cadence

Different thresholds than daily — monthly drift is normal; what matters is direction.

**GREEN** (system is learning and improving):
- `rollback_ratio < 0.10`
- `new_winners_this_month ≥ 1`
- `pulse_cost_pace ≤ $15/month` (well under $20 cap)
- `drift_cap_utilization < 60%` on all parameters
- `memo_top_decision_hit_rate ≥ 0.55`
- `apply_rate_per_week ≥ 0.5` (learning loop active)

**AMBER** (worth investigating, no urgent action):
- `rollback_ratio ∈ [0.10, 0.20]`
- `pulse_skip_rate > 5%` (caps starting to bite)
- `pending_confirmation_age_max > 14d` (suggestions stuck)
- `drift_cap_utilization ∈ [60%, 80%]` on any parameter
- `memo_top_decision_hit_rate ∈ [0.45, 0.55]` (coin-flip range)
- `apply_rate_per_week == 0` (loop dormant — may be expected on quiet months)

**RED** (operator must act):
- `rollback_ratio ≥ 0.20`
- `pulse_cost_pace > $20/month` (will exceed cap)
- `drift_cap_utilization ≥ 80%` on any parameter (next month risks blocked applies)
- `memo_top_decision_hit_rate < 0.45` (system underperforming)
- `apply_enabled == false` for ≥ 21 days without operator note

---

## Step 4 — Agent dispatch (monthly cadence)

`portfolio-learning-loop-health` IF any of:
- `tag_efficacy_drift` has any tag swinging ≥ 10pp month-over-month
- `rollback_ratio ≥ 0.10`
- any `drift_cap_utilization ≥ 60%`
- `pending_confirmation_age_max > 14d`

`portfolio-attribution-analyst` IF any of:
- `fingerprint_stability` indicates fingerprint changed within last 30d (new gauge era)
- `memo_top_decision_hit_rate < 0.50` (request regime analysis of underperformance)
- `regime_consistency < 0.50` (whipsaw regime → request regime-conditional attribution)

`portfolio-memo-reviewer` IF any of:
- `memo_top_decision_hit_rate < 0.50`
- decision-type histogram shows imbalance (e.g., 80% WAIT for the month — what's the explanation?)

`portfolio-discovery-health` IF any of:
- `net_new_discovery_yield == 0` (theme engine isn't finding profitable new names)
- `sector_rotation_score < 0.5` (universe locked in one sector)
- `pulse_skip_rate > 5%`

`portfolio-backtest-health` IF the Pattern-Loop artifact is RED:
- `outputs/backtest/poc_simulation_results.json` missing
- `performance.evaluated == 0` (looks-fresh-but-empty / content_liveness)
- every `added_metrics.per_regime[].regime == "unknown"` (degenerate output)
- `calibration.calibration_slope < 0` (flipped calibration)
- Do **NOT** dispatch merely because `proposed_count == 0` while `oos_window.folds_possible == false` — that is the expected pre-maturity state (accruing toward ~2027).
- AMBER (no dispatch, surface in body) when the D feedback proposers flag `calibration_correction_available` (calibration inverted; the proposed map is PROVISIONAL until `apply_gate==ready`) or `high_untagged_rate` (`untagged_pct >= 0.50`; route the tagging proposal to the owner — backfill/registry-entry items improve attribution now).
- `portfolio-backtest-health` + `portfolio-attribution-analyst` IF the E auto-apply audit last status is `rolled_back` (RED `auto_apply_rolled_back` — a coupling regression slipped the pre-gate; investigate immediately) OR `applied` (a registry weight was auto-changed — verify the applied change's outcome). NOTE: auto-apply is a SANCTIONED gated mutator (CLAUDE.md 2026-06-05 exception); `disabled`/`oos_immature` are the expected steady state and are NOT findings.

- **Documentation lens** — invoke the `/doc-audit-monthly` skill (or read the latest
  `outputs/latest/doc_audit_status.json`) and fold its verdict into the monthly heartbeat:
  report standing coverage gaps + the doc-auditor's top decomposition recommendation.

`portfolio-doc-writer` ALWAYS (no threshold) — updates roadmap + project_state.yaml with the month's shipped features + observed metrics. Persists the monthly findings to `docs/monthly_reports/<YYYY-MM>.md`.

---

## Step 5 — Output

**Lead line** (one of):
```
[GREEN] monthly tool analysis YYYY-MM: <one-line summary>
[AMBER] monthly tool analysis YYYY-MM: <primary concern>
[RED]   monthly tool analysis YYYY-MM: <primary fault>; action: <named action>
```

**Body** (under 400 words):

1. **Developer lens** (always):
   `"Cron health: {N}/30 days archived. Apply rate {X}/wk ({rollbacks}/30 rollbacks). Pulse skip rate {Y}%."`
2. **Quant lens** (always):
   `"Tags this month: {N} winners, {M} losers, {K} insufficient. {P} new winners promoted. Fingerprint age {D} days."`
   `"Pattern-Loop OOS window: {calendar_days_observed}/315 cal days, folds_possible={bool}, first full window ~{full_window_eta}. Proposals: {proposed_count} (0 expected until window matures)."`
   `"Feedback proposers — calibration: inverted={bool} (apply_gate={apply_gate}); tagging: {untagged_pct} untagged, families missing registry id: {families_missing_registry_id}."`
3. **Process analyst lens** (always):
   `"Drift cap max {Z}% on {param}. Pending confirmations: {count} oldest {age}d. Burn pace ${P}/mo vs $20 cap."`
4. **Market expert lens** (always):
   `"Memo hit-rate {X}%. Net-new discoveries: {N} surfaced, {M} resolved positive. Sector rotation {S}. Dominant regime: {R} ({pct}%)."`
5. **Notable trend(s)** (1-3 bullets — what changed vs last month)
6. **Agent dispatch results** (one line per fired agent)
7. For RED: named action from template library
8. **Operator action queue** (3 items max — concrete things to do this month)

---

## RED action templates (monthly)

| Trigger | Action |
|---|---|
| `rollback_ratio ≥ 0.20` | "Learning loop misfiring at {ratio} rollback rate. Set apply_enabled=false; audit last 30d log entries; consider raising n_samples floor from 200 to 300." |
| `pulse_cost_pace > $20/month` | "AI spend pace ${P}/mo will exceed $20 cap. Reduce discovery_pulse cadence (4/day → 2/day weekday) OR raise ai_budget.monthly_cost_limit_usd." |
| `drift_cap_utilization ≥ 80%` | "Auto-apply approaching monthly drift cap on {param} ({drift}/0.25). Cap will block further applies; review whether the parameter has stabilized." |
| `memo_top_decision_hit_rate < 0.45` | "Memo decisions underperforming this month ({X}%). Dispatch attribution-analyst for regime-conditional breakdown; consider gauge era review." |

---

## Step 6 — Persist monthly report

Write the full report to `docs/monthly_reports/YYYY-MM.md`. This becomes
the longitudinal record that `yearly-tool-analysis` reads.

Update `data/monthly_check_state.json`:
- `last_run_at`, `last_verdict`, `last_top_concern`
- counts persisted for the year-end retrospective
