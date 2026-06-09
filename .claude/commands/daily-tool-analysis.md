---
description: Daily tool analysis — health + status review of the whole Portfolio Automation System across four analytical lenses (developer / quant / process analyst / market expert). Reads today's run artifacts, triages GREEN/AMBER/RED, threshold-dispatches the resolver/attribution/render/discovery/learning-loop agents, emits a one-line heartbeat plus structured body. Designed to run via /schedule at 09:15 UTC daily, 14 minutes after the production cron.
---

# Daily Tool Analysis

System-wide health + status review at daily cadence, from four lenses:
- **Developer lens** — cron health, silent zeros, dependency drift (resolver-investigator, discovery-health)
- **Quant lens** — hit-rate, sector attribution, pattern efficacy (attribution-analyst, learning-loop-health)
- **Process analyst lens** — workflow health, audit log activity, drift caps (learning-loop-health)
- **Market expert lens** — memo accuracy, decision-vs-outcome alignment (memo-reviewer, attribution-analyst)

Runs at 09:15 UTC, 14 min after production cron at 09:00. Working dir: `/opt/stockbot`.

---

## Step 1 — Load state + read artifacts

**Read state** (creates with empty defaults if missing):

`data/daily_check_state.json`:
```json
{
  "last_run_at": "2026-MM-DDTHH:MM:SSZ",
  "last_fingerprint": "...",
  "last_current_fp_resolved_1d": 0,
  "last_pre_tracker_hit_rate_1d": null,
  "thresholds_crossed": [],
  "applied_fixes": []
}
```

`thresholds_crossed` is a subset of `["n_10", "n_30", "n_50", "n_100"]`.

`applied_fixes` is an append-only ledger of fixes this skill (or an operator)
shipped in response to a prior run's findings. Each batch carries
`{date, applied_at, commit, source_run, fixes:[...]}`; each fix carries
`{id, lens, finding, fix, expect_next_run, verify}`. The `verify` block is a
machine-checkable spec (see Compute below) that lets the *next* run confirm the
fix held, catch a regression, or know it's not yet observable. `applied_at` is
the ISO timestamp the fix went live — used to ignore artifacts generated before
the fix.

**Read artifacts** (degrade gracefully on any miss):

0. `outputs/latest/artifact_registry_status.json` → overall_status, counts, missing[], stale[], invalid_json[], unjustified_debt[], justified_no_consumer, by_consumer_status, classified, debt_target_met, severity, operator_message (added 2026-06-08; artifact-governance validator — READ FIRST, it gates confidence in everything below). If absent, fall back to daily_run_status as before and note the registry validator did not run.
1. `outputs/latest/daily_run_status.json` → overall_status, stage_summary, required_missing_count, **content_liveness, content_warn_count** (added 2026-05-28)
2. `outputs/latest/daily_memo.md` (first 50 lines)
3. `outputs/latest/retune_impact.json` → outcome_attribution.by_fingerprint, **sector_composition per fingerprint** (added 2026-05-28)
4. `outputs/latest/risk_delta.json` → overall_status, concentration.top_position, leverage.total_exposure
5. `outputs/latest/fmp_budget_status.json` → budget.status, news.article_count_raw
6. `outputs/latest/decisions_due_for_resolution.json` → stuck_count, by_ticker (top 3)
7. `data/gauge_versions.jsonl` tail line → current_fingerprint + first_seen_at
8. `outputs/latest/discovery_pulse_status.json` → usage.{openai_cost_usd_month, fmp_calls_month, total_runs_month, skipped_runs_month}, caps, last_run_at (added 2026-05-28)
9. `outputs/latest/ai_budget_summary.json` → monthly_cost_total_usd, daily_cost_total_usd, monthly_cost_limit_usd, warnings (added 2026-05-28; project-wide $20/mo cap)
10. `outputs/latest/pattern_efficacy_monthly.json` → snapshots_consumed, rows_matched_to_outcomes, match_rate, universe_baseline.n_samples, count of winner/strong_winner tags (added 2026-05-28)
11. `outputs/latest/gate_retune_suggestions.json` → available, auto_applicable_count, weight_proposals (lengths only), gate_proposal (added 2026-05-28)
12. `data/retune_audit_log.jsonl` (tail) → count of apply entries in last 7d, any rollback entries (added 2026-05-28)
13. `data/retune_auto_apply_state.json` → apply_enabled, max(monthly_drift.values()) (added 2026-05-28)
14. `outputs/latest/historical_backfill_status.json` → universe_size, fetched, errored, skipped_budget (added 2026-05-28; weekend-cadence producer)
15. `outputs/latest/doc_audit_status.json` → overall_status, len(coverage_gaps), count of unfixed `drift`/`consistency` findings (added 2026-06-01; weekly-cadence producer — may be absent until first /doc-audit run)
16. `outputs/policy/auto_apply_audit.json` → E auto-apply: last entry `status` (added 2026-06-05; default-inert mutator — absent or `disabled`/`oos_immature` is the expected steady state, NOT a finding)
17. `outputs/latest/correlation_risk_advisor.json` → risk lens: read `high_correlation_pairs`, `concentration_risk_score`, `recommendations` — flags when portfolio positions are highly correlated and concentration risk is elevated (added 2026-06-08; risk-lens consumer)
18. `outputs/latest/quant_watch_status.json` → overall_status, active_count, active[] (concern + age_days), registered_today, resolved_today, escalated_today, ledger_liveness (added 2026-06-08; quant-watch probe ledger — sub-RED quant concern tracker)
19. `outputs/latest/broker_sync_status.json` → overall_status (`disabled`|`unconfigured`|`error`|`ok`|`degraded`), configured, authenticated, account_count, position_count, last_error (added 2026-06-09; Schwab read-only broker sync — **on_demand / operator-run, NOT a daily-cron producer**; `unconfigured`/`disabled` is the expected inert steady state, NOT a finding. Always-producible even when uncredentialed.)
20. `outputs/operator_control/work_orders.jsonl` + `outputs/operator_control/audit_log.jsonl` (both append-only; fold work_orders by `work_order_id`, last line wins) → operator-control plane (Phases 1–3): counts by status + count of `worker_protected_path_violation` events in the audit log (added 2026-06-09; **observe-only, operator-driven** — absence / all-zero is the inert steady state, NOT a finding; never blocks the decision core)

**Compute**:

- `fingerprint_age_days` = days since current `first_seen_at`
- `fingerprint_changed` = state.last_fingerprint != current_fingerprint
- `current_fp_resolved_1d`, `pre_tracker_resolved_1d`
- `delta_hit_rate_pp` = (current_fp.hit_rate_1d − pre_tracker.hit_rate_1d) × 100
- `newly_crossed_thresholds` = thresholds in `{10, 30, 50, 100}` that `current_fp_resolved_1d` reached today but were not in `state.thresholds_crossed`
- `pulse_age_hours` = hours since `discovery_pulse_status.last_run_at` (or `null` if pulse never ran)
- `ai_budget_pct_of_cap` = `ai_budget.monthly_cost_total_usd / ai_budget.monthly_cost_limit_usd × 100`
- `pulse_cap_pct` = max over caps of `(usage_value / cap_value) × 100` from `discovery_pulse_status`
- `gauge_top_sector` = sector with highest `pct_of_signals` in current-fingerprint `sector_composition` (informational; explains lift drivers)
- `pattern_match_rate` = `pattern_efficacy_monthly.match_rate × 100` (null if artifact missing)
- `retune_auto_applicable_count` = `gate_retune_suggestions.auto_applicable_count` (or 0 if missing)
- `retune_applies_last_7d` = count of `retune_audit_log.jsonl` entries with `ts ≥ now-7d` AND `applied_by == "auto"`
- `retune_rollbacks_total` = count of audit entries with `applied_by == "rollback"`
- `retune_drift_max_pct` = `max(retune_auto_apply_state.monthly_drift.values()) / 0.25 × 100` (or 0)
- `retune_apply_enabled` = `retune_auto_apply_state.apply_enabled` (default `true`)
- `applied_fix_verdicts` = verify recorded fixes against today's artifacts:
  ```bash
  python -c "import json; from portfolio_automation.applied_fix_verifier import verify_applied_fixes, summarize; s=json.load(open('data/daily_check_state.json')); v=verify_applied_fixes(s, '.'); print(json.dumps({'verdicts': v, 'summary': summarize(v)}, indent=2))"
  ```
  Each verdict is `{id, status, detail}` where status ∈ `{confirmed, regressed, pending, manual}`. The module applies an `applied_at` staleness guard, so a fix reads `pending` (not `regressed`) until the pipeline has regenerated artifacts *after* the fix went live.
- `applied_fix_regressions` = verdicts with `status == "regressed"` (a shipped fix's original symptom is back)
- `broker_status` = `broker_sync_status.overall_status` (default `unconfigured` if the artifact is missing — the layer ships always-producible, so a true absence means the broker module is not deployed)
- `broker_configured` = `broker_sync_status.configured` (default `false`); `broker_authenticated` = `broker_sync_status.authenticated` (default `false`)
- operator-control: from `.venv/bin/python -m operator_control.worker_runner status` (by_status counts + autonomous_enabled) and `outputs/operator_control/audit_log.jsonl`:
  - `operator_open_count` = orders in {queued, awaiting_approval, claimed, running, approved}; `operator_failed_count` = orders in `failed`
  - `operator_quarantined_today` = count of `worker_protected_path_violation` audit events dated today (absent files → 0)
  - `operator_stuck_running` = any order in `running`/`claimed` whose last `status_history.at` is > 24h ago (worker crashed mid-run, or a scaffolded order was never completed)
  - `operator_worker_mode` = `"autonomous ON"` if `autonomous_enabled` else `"scaffold-only"`

---

## Step 2 — Triage

**Artifact-governance gate (run before GREEN/AMBER/RED):**
- Read `artifact_registry_status.json` first. If a `role: source_of_truth` artifact is in `missing` or `stale` → **downgrade confidence and cap the run at AMBER at best** (the decision core is not trustworthy); never infer portfolio actions from a degraded decision core.
- If a `required` `role: probe` artifact is missing/stale → mark the analysis **partial** for that lens.
- `unjustified_debt` entries route to `portfolio-discovery-health` (advisory, not RED); `justified_no_consumer` is acknowledged, not debt.
- Only `source_of_truth` artifacts represent official actions; probe/advisor/telemetry/narrative artifacts inform confidence and explanation only.

**GREEN** when all of:
- `overall_status == "ok"` (NOT `ok_with_warnings`)
- `required_missing_count == 0`
- `content_warn_count == 0` (content-liveness clean across all 7 producers)
- `stuck_count == 0`
- `budget.status ∈ {ok, near_cap}`
- `risk_delta.overall_status ∈ {ok, near_cap}`
- `pulse_age_hours == null OR ≤ 8` (discovery pulse cron healthy)
- `ai_budget_pct_of_cap < 80` (AI spend well under $20/mo cap)
- `pulse_cap_pct < 80` (no pulse cap is near its trip-wire)
- `retune_drift_max_pct < 60` (no auto-apply param burning through monthly drift)
- `retune_apply_enabled == true` (learning loop not operator-paused)
- `applied_fix_regressions` is empty (no shipped fix has regressed)
- `broker_status ∈ {unconfigured, disabled, ok}` (Schwab layer inert or healthy — `degraded`/`error` is the only non-GREEN broker state)
- `operator_quarantined_today == 0` AND `operator_stuck_running` is false (operator-control plane inert or healthy — no work orders at all is GREEN)
- no unexpected fingerprint change
- attribution presence consistent with fingerprint age (n=0 only acceptable when age <2 days)

**AMBER** when GREEN fails on non-urgent advisory:
- `overall_status == "ok_with_warnings"` AND only content_liveness warns (no required missing, no stuck)
- `budget.status == "near_cap"`
- `risk_delta.overall_status == "near_cap"`
- `pulse_age_hours > 8 AND ≤ 24` (pulse stalled but not catastrophic)
- `ai_budget_pct_of_cap ∈ [80, 100)` (approaching $20/mo cap)
- `pulse_cap_pct ∈ [80, 100)` (approaching pulse trip-wire)
- `retune_drift_max_pct ∈ [60, 100)` (auto-apply approaching drift cap on some param)
- `retune_apply_enabled == false` for ≤ 14 days (operator paused; still acceptable short-term)
- `retune_auto_applicable_count ≥ 3` (unusually many proposals queued — worth a look)
- `applied_fix_regressions` is non-empty (a shipped fix regressed — its original symptom is back; advisory, investigate via discovery-health)
- `doc_audit_status.overall_status == "coverage_gap"` OR any unfixed `drift`/`consistency` finding present (docs lag code — advisory; resolved by the next `/doc-audit` run)
- `broker_status ∈ {degraded, error}` (Schwab configured but the OAuth token is unauthenticated, or the last broker API call failed — advisory only: the broker layer is observe-only evidence and **never** blocks the decision core; point the operator to `docs/schwab_integration.md` Troubleshooting. Never RED.)
- `operator_quarantined_today ≥ 1` (the autonomous worker hit a protected path today — contained/quarantined, never merged; review `/dashboard/operator/report/<id>` to confirm the guard fired correctly) OR `operator_stuck_running` (an order has sat in `running`/`claimed` > 24h — worker crashed or a scaffolded order was abandoned; inspect the worktree). Operator-control is observe-only and **never** RED (it never blocks the decision core).
- attribution lag (age ≥2d AND n=0) — known cron-timing issue

**RED** when any of:
- `overall_status ∈ {"failed", "partial"}`
- `stuck_count > 0`
- `budget.status == "exhausted"`
- `risk_delta.overall_status == "breach"`
- `|delta_hit_rate_pp| ≥ 10` AND `current_fp_resolved_1d ≥ 30`
- `pulse_age_hours > 24` (pulse cron silent for a day+)
- `ai_budget_pct_of_cap ≥ 100` (AI spend at/over $20/mo cap)
- `pulse_cap_pct ≥ 100` (pulse trip-wire active — runs being skipped)
- `retune_drift_max_pct ≥ 100` (auto-apply at monthly drift cap; further applies blocked)
- `retune_rollbacks_total ≥ 3` in last 7 days (loop is misfiring; immediate operator review)

---

## Step 3 — Threshold-driven agent dispatch

`portfolio-resolver-investigator` IF any of:
- `overall_status != "ok"`
- `required_missing_count > 0`
- `stuck_count > 0`
- `current_fp_resolved_1d == 0` AND `fingerprint_age_days ≥ 2`

`portfolio-attribution-analyst` IF any of:
- `newly_crossed_thresholds` is non-empty (sample-size milestone — n=10, 30, 50, or 100 first crossed)
- `fingerprint_changed` (new gauge era — analyst reads fresh baseline)
- `|delta_hit_rate_pp| ≥ 10` AND `current_fp_resolved_1d ≥ 30`

`portfolio-render-reviewer` IF any of (last 24h `git log`):
- `watchlist_scanner/daily_memo.py` modified
- `portfolio_automation/*_advisor.py` `render_*_md` function modified
- `gui_v2/templates/risk_impact.html` modified

`portfolio-memo-reviewer` ALWAYS (no threshold gate) — reviews the produced memo artifacts against source JSONs for accuracy, internal consistency, clarity, and compact-contract compliance.

`portfolio-discovery-health` IF any of:
- `daily_run_status.content_warn_count > 0` (any liveness check warned, e.g. empty `theme_signals.themes`)
- watchlist universe is all-`static` per `signal_outcomes.csv` (no dynamic-source signals lifetime)
- extended_watchlist DB has 0 active rows AND `fingerprint_age_days ≥ 2`
- `pulse_age_hours > 8` (discovery pulse cron not firing on schedule)
- `pulse_cap_pct ≥ 80` (pulse approaching or past trip-wire — investigate which cap)
- `ai_budget_pct_of_cap ≥ 80` (project-wide AI spend approaching $20/mo)
- `applied_fix_regressions` contains any discovery-layer fix id (e.g. `persistence_7d_daily_mode`, `pulse_last_run_age_sla`, `extended_watchlist_cross_day_gate`) — a previously-shipped discovery fix has regressed; pass the verdict `detail` so the agent can pinpoint which signal reverted

This agent audits the discovery layer: RSS feedparser availability, Ollama / LLM reachability, theme_signals emit-rate, extended_watchlist promotions, FMP profile-cache freshness, parallel FMP candidate-scanner wiring, **discovery_pulse cron health, AI budget cap utilization, and FMP monthly headroom**. Surfaces stacked silent-zero failures.

`portfolio-learning-loop-health` IF any of:
- `pattern_match_rate < 30` AND lookback has had ≥7 daily snapshots (join logic likely broken)
- `pattern_efficacy_monthly.json` missing OR `snapshots_consumed == 0`
- `retune_auto_applicable_count ≥ 3` (unusually many proposals; verify n_samples thresholds aren't drifting)
- `retune_drift_max_pct ≥ 60` (auto-apply approaching monthly cap on some parameter)
- `retune_rollbacks_total ≥ 1` in last 7 days (any rollback is a yellow flag worth investigating)
- `retune_apply_enabled == false` for ≥ 7 days (operator-paused; verify intent or remind to re-enable)
- pending_confirmations contains parameters that have been queued for > 14 days

This agent audits the learning loop: pattern_efficacy match-rate + tag count, retune_suggestions readiness, retune_auto_apply audit log activity, drift cap status, apply_enabled flag, pending confirmation queue. Surfaces stuck-confirmation loops and runaway auto-apply patterns.

`portfolio-attribution-analyst` ADDITIONALLY analyzes (when dispatched) the new `sector_composition` field in `retune_impact.json:outcome_attribution.by_fingerprint.<fp>` — comparing current-fingerprint hit-rates across sectors against the pre-tracker baseline by sector. If the gauge's lift comes disproportionately from one sector (e.g., ETF cluster carrying 78% hit-rate vs single-name techs at 55%), that's surfaced as a regime-correlation caveat in the analyst report.

`portfolio-backtest-health` IF the E auto-apply audit (`outputs/policy/auto_apply_audit.json`) last status is `rolled_back` (RED — a post-apply score-invariance regression auto-reverted; investigate the coupling immediately) OR `applied` (a registry weight was auto-changed today — verify the change and its outcome). This is oversight of the sanctioned auto-apply mutator: every weight change it makes must be surfaced and reviewed.

### Pattern-Loop operational sub-check (delegate to `/pattern-loop-analysis`)

Run the `/pattern-loop-analysis` skill's Step-1 backbone as the daily tripwire for the
Pattern-Improvement Loop tool (Foundation + D proposers + E auto-apply), and fold its one
-line heartbeat into the daily body (Step 4, item: "pattern-loop: …"). Do NOT re-derive
the loop's logic here — that skill owns it. Escalate the DAILY check to RED only on the
loop's RED conditions (per `/pattern-loop-analysis` Step 3): auto-apply `applied`/`rolled_back`,
a `*_offline` run mode when live was expected, `evaluated == 0`/degenerate regimes, or the
monthly recompute missing > 45 days. The pre-maturity steady state (live mode, 0 proposals,
inverted calibration, ~70% untagged, auto-apply inert) is GREEN/AMBER — report it, don't
alert on it. The recompute itself is monthly, so day-to-day the evidence is unchanged; the
daily value is catching auto-apply events same-day, a stalled recompute, or an offline
fallback.

### Quant-watch operational sub-check (delegate to `/quant-watch-analysis`)

Run the `/quant-watch-analysis` skill's Step 1 backbone as the daily driver of
the quant-watch probe ledger (auto-register sub-RED quant concerns, re-check
open probes, auto-archive resolved ones). Do NOT re-derive detector logic here
— that skill + `portfolio_automation/quant_watch_probes.py` own it. Fold its
one-line heartbeat into the daily body (Step 4, item: "quant-watch: …").

Escalate the DAILY check to RED only on the quant-watch RED condition
(`escalated_today` non-empty). By construction an escalated probe has crossed a
daily RED gate (e.g. `|delta_hit_rate_pp| >= 10 at n>=30`), so the existing
daily RED logic + `portfolio-attribution-analyst` dispatch already own the
response — quant-watch adds continuity + same-run visibility, not a second RED
authority. The steady state (≥1 active AMBER probe, e.g. the prior-gauge
underperformance trap) is AMBER — report it, don't alert on it.

---

## Step 4 — Output (daily heartbeat — emit every run)

**Lead line, always**:

`[GREEN|AMBER|RED] daily check YYYY-MM-DD: <one-line headline>`

Headline grammar:
- GREEN: `"17 stages OK · retune n={N} at {H}% (Δ {sign}{pp}pp vs baseline) · FMP {used}/{cap}"`
- AMBER: `"WARN — {primary anomaly}; others nominal"`
- RED: `"ALERT — {primary fault}; action: {from RED template library}"`

**Body, under 250 words**:

0. Artifact governance (always, first): `"Coverage: {present}/{total} present · {missing} missing ({missing_required} required) · {stale} stale · debt {unjustified_debt} (target 0) · classified {classified}/{total} · {overall_status}"` — from artifact_registry_status.json. RED here (critical/source-of-truth missing) forces the daily lead line to RED.
1. Attribution snapshot (always): `"Attribution: current-fp n={N} at {H}% / pre-tracker n={N} at {H}% · Δ {sign}{pp}pp · top sector {gauge_top_sector}"`
2. Risk-delta state (always): `"Risk: {top_symbol} {weight}% (cap {cap}%, +{headroom}pp); leverage {L}%"`
3. Discovery pulse + AI spend (always, since they're project-wide health signals): `"Pulse: last={pulse_age_hours}h ago, {total_runs_month} runs MTD ({skipped_runs_month} skipped) · AI: ${monthly_cost_total_usd:.2f}/${monthly_cost_limit_usd:.0f} cap ({ai_budget_pct_of_cap}%)"`
4. Learning loop snapshot (always when `pattern_efficacy_monthly.json` exists): `"Loop: match-rate {pattern_match_rate}% · {retune_applies_last_7d} applies/7d · drift max {retune_drift_max_pct}% of cap · apply_enabled={retune_apply_enabled}"`
5. Content liveness (only when `content_warn_count > 0`): `"Liveness warns ({content_warn_count}): {csv list of warn names}"`
5b. Applied-fix verification (only when `applied_fixes` is non-empty): `"Fixes: {confirmed} confirmed · {pending} pending · {manual} manual{, REGRESSED: <id(s)> if any}"`. List each regressed id explicitly with its `detail`. When a fix is `confirmed`, it is dropped from state in Step 5 (it held — stop re-checking).
6c. Docs (only when `doc_audit_status.json` exists): `"Docs: {overall_status} · {N} findings, {K} coverage gaps (last audit {last_audited_sha[:8]})"`
6d. Pattern-loop (always, from the sub-check above): `"Pattern-loop: {mode}, OOS {observed}/315 (~{eta}), proposals {N}, auto-apply {state}"` — folds in the `/pattern-loop-analysis` heartbeat.
6e. Quant-watch (always, from the sub-check above): `"Quant-watch: {overall_status} · {active_count} active ({top active probe concern}); {len(registered_today)}↑/{len(resolved_today)}↓/{len(escalated_today)} esc today"` — folds in the `/quant-watch-analysis` heartbeat. RED only when `escalated_today` is non-empty (which is already a daily RED key).
6f. Broker-sync (always): `"Broker-sync: {broker_status} (configured={broker_configured}, authenticated={broker_authenticated}) · {account_count} accts / {position_count} positions"` — Schwab read-only layer. `unconfigured`/`disabled` is the inert steady state (report, don't alert). `degraded`/`error` → AMBER; append `"— see docs/schwab_integration.md Troubleshooting"`. Never RED (observe-only evidence; never blocks decisions).
6g. Operator-control (always): `"Operator-control: {operator_open_count} open · {operator_failed_count} failed · {operator_quarantined_today} quarantined today · worker {operator_worker_mode}"` — observe-only work-order plane (Phases 1–3). No work orders / all-zero is the inert steady state (report, don't alert). `operator_quarantined_today ≥ 1` or a >24h stuck run → AMBER; append `"— review /dashboard/operator/report/<id>"`. Never RED (never blocks the decision core).
6. Agent dispatch results — one line per agent. memo-reviewer always fires, so its line always appears: `"memo-reviewer: clean"` or `"memo-reviewer: N issue(s) — <highest-severity summary>"`. Other agents appear only if they fired. The discovery-health and learning-loop-health agents report `"<name>: {verdict} — {root cause sentence}"`.
7. For RED only: named action from the template library below
8. For GREEN: `"No action required."`

---

## RED action template library

Choose the first match from this priority order:

| Trigger | Action line template |
|---|---|
| `stuck_count > 0` for ≥2 consecutive days | `"Resolver lag on {top_stuck_ticker} — run python -m portfolio_automation.resolution_due_probe manually; investigate FMP cache TTL."` |
| `budget.status == "exhausted"` | `"FMP daily budget exhausted at {count}/{cap} — news intel skipped today; consider raising fmp_daily_calls_budget or staggering producer calls."` |
| `risk_delta.overall_status == "breach"` | `"Concentration breach on {top_symbol} ({weight}% > cap {cap_pct}%); structural-cap trim signal active in decision_plan."` |
| `delta_hit_rate_pp ≤ -10` AND `n ≥ 30` | `"Current-fp underperforming pre-tracker by {delta}pp on n={n}; consider reverting most-aggressive knob first ({knob} {current}→{revert_to}) and re-check in 14 days."` |
| `delta_hit_rate_pp ≥ +10` AND `n ≥ 30` | `"Current-fp outperforming pre-tracker by {delta}pp on n={n}; retune validated. Consider whether to advance to next gauge candidate."` |
| `overall_status == "failed"` | `"Pipeline failed — check logs/daily_safe_{date}.log for stage that errored."` |
| Attribution lag (age ≥2d, n=0) | `"Resolver not picking up current-fp data — check FMP cache TTL (now 0 per outcome_evaluator); verify cron at 09:01 produced today's signal_outcomes.csv."` |
| `pulse_age_hours > 24` | `"Discovery pulse cron silent for >24h — check crontab, /var/lock/stockbot-discovery-pulse.lock, and logs/discovery_pulse_{date}.log."` |
| `ai_budget_pct_of_cap ≥ 100` | `"AI spend at/over \$20/mo cap — discovery_pulse trip-wire active. Either raise ai_budget.monthly_cost_limit_usd or wait until next month."` |
| `pulse_cap_pct ≥ 100` (FMP) | `"Discovery pulse FMP monthly cap exceeded ({fmp_calls_month}/{fmp_calls_max}); pulse skipping remaining runs this month. Check whether scraped_intel is consuming more than expected."` |
| `retune_drift_max_pct ≥ 100` | `"Retune auto-apply at monthly drift cap on {param} ({drift}/{cap}). Further auto-applies blocked until month rollover. Review whether tag taxonomy or thresholds need tightening."` |
| `retune_rollbacks_total ≥ 3` in 7d | `"Learning loop rollback streak ({N} in last 7d) — auto-apply is misfiring. Set apply_enabled=false in data/retune_auto_apply_state.json and review last 7d audit entries in data/retune_audit_log.jsonl."` |

---

## Step 5 — Write state back

Update `data/daily_check_state.json`:
- `last_run_at` = today's iso timestamp
- `last_fingerprint` = current_fingerprint
- `last_current_fp_resolved_1d` = today's value
- `last_pre_tracker_hit_rate_1d` = today's value
- Append `newly_crossed_thresholds` to `thresholds_crossed`
- Reset `thresholds_crossed` to `[]` if `fingerprint_changed`
- Prune `confirmed` fixes from `applied_fixes` (they held — stop re-checking); keep `pending` / `regressed` / `manual`. Use the module helper so empty batches are dropped:
  ```bash
  python -c "import json; from portfolio_automation.applied_fix_verifier import verify_applied_fixes, drop_resolved; p='data/daily_check_state.json'; s=json.load(open(p)); s2=drop_resolved(s, verify_applied_fixes(s, '.')); s.update(s2); json.dump(s, open(p,'w'), indent=2)"
  ```
  (Run this AFTER updating the telemetry fields above, or fold both writes together. Never drop `manual` fixes automatically — an operator clears those.)

---

## Failure modes

- All `outputs/latest/` artifacts missing → RED, headline reason: "cron did not run today"
- Artifacts present but all mtime > 24h → AMBER, headline reason: "today's cron did not refresh artifacts"
- Agent dispatch raises → log error to summary body, continue with manual triage on remaining data, do not abort the summary
- `data/daily_check_state.json` corrupt → reset to defaults; one-time AMBER with body note `"State file reset; threshold crossing detection unreliable for one run"`
