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

0. `outputs/latest/artifact_registry_status.json` → overall_status, counts, missing[], stale[], idle[], invalid_json[], unjustified_debt[], justified_no_consumer, by_consumer_status, classified, debt_target_met, severity, operator_message (added 2026-06-08; artifact-governance validator — READ FIRST, it gates confidence in everything below). If absent, fall back to daily_run_status as before and note the registry validator did not run. `idle[]` (added 2026-07-10) holds append-only event-log rows (`idle_ok: true`, e.g. `system_improvement_history.jsonl`, `user_action_log.jsonl`) whose staleness is legitimate quiet-day idleness, not a broken producer — informational, never escalates. `source_of_truth` rows can never be idle_ok, so a stale decision-core artifact still lands in `stale[]`.
1. `outputs/latest/daily_run_status.json` → overall_status, stage_summary, required_missing_count, **content_liveness, content_warn_count** (added 2026-05-28)
2. `outputs/latest/daily_memo.md` (first 50 lines)
2b. `outputs/latest/memo_coherence.json` → coherence_status, reconciliation.unresolved_count, funding.{funded_count,blocked_count,below_safety_floor,deployable_from_incoming}, ranking.{default_fallback_count,distinct_priorities}, hit_rate.{directional_accuracy_pct,neutral}, crowd.{insufficient_data_count}, freshness.stale_sources (added 2026-06-30; memo decision-coherence reconciliation layer — **observe-only, advisory, never feeds `decision_plan.json`**; market-expert + developer lens). Absent → memo_coherence did not run yet (inert; report don't alert).
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
16b. `outputs/policy/auto_approval_audit.json` (summary) + `outputs/policy/auto_approval_events.jsonl` (append-only ledger) → GPT simulation auto-approval: summary `counters` (applied/gpt_veto/deterministic_reject/human_veto/rollback/rollback_conflict/failure), `active_item_count`, `circuit_breaker.{engaged,reason}`; ledger tail for today's kinds (added 2026-07-14; SIMULATION-only sanctioned mutator — absent or all-zero counters + breaker disengaged is the expected inert steady state, NOT a finding). Auto-approval NEVER touches production/decision_engine; a successful `human_veto`+`rollback` pair is the control WORKING — VERIFY, don't revert.
17. `outputs/latest/correlation_risk_advisor.json` → risk lens: read `high_correlation_pairs`, `concentration_risk_score`, `recommendations` — flags when portfolio positions are highly correlated and concentration risk is elevated (added 2026-06-08; risk-lens consumer)
18. `outputs/latest/quant_watch_status.json` → overall_status, active_count, active[] (concern + age_days), registered_today, resolved_today, escalated_today, ledger_liveness (added 2026-06-08; quant-watch probe ledger — sub-RED quant concern tracker)
19. `outputs/latest/broker_sync_status.json` → overall_status (`disabled`|`unconfigured`|`error`|`ok`|`degraded`), configured, authenticated, account_count, position_count, last_error (added 2026-06-09; Schwab read-only broker sync. **Daily-cron producer as of 2026-06-12** — refreshed every run by `run_daily_safe.sh` **Stage 0b** (`schwab_sync --sync --reconcile`, non-blocking; **moved ahead of the decision run on 2026-06-16** to kill the 24h holdings flap — the sync now writes the broker snapshot *before* `main.py` reads it) and re-triggerable on-demand via CLI/GUI. `unconfigured`/`disabled` is the expected inert steady state **before provisioning** (report, don't alert); once credentialed, `degraded`/`error` → AMBER, never RED. Always-producible even when uncredentialed. The 4 advisor artifacts (`schwab_portfolio_snapshot`, `schwab_positions`, `portfolio_reconciliation`, `portfolio_config_update_proposal`) stay on_demand — they only populate post-auth.)
20. `outputs/operator_control/work_orders.jsonl` + `outputs/operator_control/audit_log.jsonl` (both append-only; fold work_orders by `work_order_id`, last line wins) → operator-control plane (Phases 1–3): counts by status + count of `worker_protected_path_violation` events in the audit log (added 2026-06-09; **observe-only, operator-driven** — absence / all-zero is the inert steady state, NOT a finding; never blocks the decision core) Also fold the cost-cap audit events: `worker_cost_cap_deferred` (a run was deferred because today's spend hit `usd_per_day` — informational, the order stays eligible) and `worker_cost_cap_exceeded` (a run breached the per-run cap — AMBER, verify the rails fired). Also fold the Phase 3 events: `work_order_cancelled` (dead order cleared, not a failure), `worker_quarantine_discarded` (contained worktree rolled back), `worker_quarantine_salvaged` (branch reported for manual integration).
21. Next-stage research/strategy lane (Phases 1–15, activated 2026-06-10 as `run_daily_safe.sh` Stage 10b; **observe-only, advisory side-panels — never feeds `decision_plan.json`**). Read the producers' artifacts (degrade gracefully on any miss — the lane is non-fatal per-step):
    - `outputs/sandbox/opportunity_radar.json` → `opportunity_count` (universe scanner / Phases 5–6)
    - `outputs/sandbox/opportunity_approval_queue.json` → `queue_count` (market-opportunity prompts / Phases 4+8)
    - `outputs/sandbox/strategy_comparison.json` → `comparison` (len = profile count), `context_source` (Phases 11A+12–13 Multi-Strategy / Strategy Lab)
    - `outputs/sandbox/shadow_opportunity_tracking.json` → `record_count` (Phase 7 shadow tracking)
    - `outputs/portfolio/broker_aware_portfolio.json` → `holdings_source`, `degraded_mode` (Phase 10 broker-aware side-panel — `degraded_mode:true` + `holdings_source:config` is the expected inert state while Schwab is `unconfigured`, NOT a finding)
    - `outputs/latest/system_improvement_ideas.json` → `idea_count` (Phase 3; deeper triage owned by the dedicated `/daily-system-improvement` skill — daily check only surfaces liveness here)
22. `outputs/latest/pipeline_wiring_status.json` → overall_status, summary.{total_audited, healthy, unwired, cadence_mismatch, silently_skipped, fresh_but_empty, event_log_idle, disabled, not_audited}, producers[] (added 2026-06-11; **pipeline wiring probe** — the root-cause layer over the registry. For every declared producer it crosses artifact freshness with static caller-grep to explain WHY a producer is stale: `unwired` (no cron caller), `cadence_mismatch` (called by a different-cadence script than declared), `silently_skipped` (wired but config-gated/no-op), `fresh_but_empty` (produced but degenerate content). Observe-only, AMBER-max — never blocks the decision core. Runs as `run_daily_safe.sh` Stage 13, after registry governance.)
23. Crowd Radar / Public Knowledge Velocity Layer (added 2026-06-12 as `run_daily_safe.sh` Stage 9c; **observe-only, sandbox-only, DEFAULT-DISABLED — never feeds `decision_plan.json`**; market-discovery lens). Read (degrade gracefully on any miss):
    - `outputs/sandbox/discovery/crowd_knowledge_state.json` → `source_status` (`ok`|`disabled`|`degraded`|`no_credentials`|`rate_limited`|`source_terms_blocked`|`insufficient_data`|`error`), `data_quality_status`, `state_count`, `records[].crowd_state` — the per-ticker crowd-knowledge state classifier. `disabled`/`no_credentials` is the expected inert steady state **before provisioning** (report, don't alert).
    - `outputs/sandbox/discovery/social_signal_backtest.json` → `states_matured` (efficacy-gated; empty until ≥ min_sample resolved observations — expected for a new layer), `total_observations`
    - `outputs/sandbox/discovery/social_source_compliance.json` → `review_needed_count` (source-governance: >0 means a source's ToS review lapsed → AMBER)
    - `outputs/sandbox/discovery/crowd_radar_activation_check.json` (Stage 9c2 readiness probe) → `ready_to_collect`, `source_status`, `source_terms_status`, `credentials_present`, `last_smoke_test_status`, `warnings[]` — the pre-flight activation gate. `ready_to_collect=false` while disabled/un-provisioned is the expected inert state (report, don't alert). AMBER only on a *regression*: `source_terms_status ∈ {review_needed, blocked}`, or `enabled=true` with `ready_to_collect=false` for a reason other than missing creds (e.g. `rate_limit_not_configured`). Invariants `sandbox_only_assertion`/`decision_engine_blocked` must stay true — if either is ever false, that is a RED contract breach.
    - **content_liveness**: when `source_status == "ok"` but `state_count == 0`, that is a looks-fresh-but-empty failure (the layer ran and fetched but classified nothing — ticker-extraction or feed upstream likely broke). Treat as a content warning and dispatch `portfolio-discovery-health`.
23b. Social Sentiment Pipeline — free text-sentiment lane (added 2026-06-21; Bluesky/Mastodon/Lemmy + FinBERT; simulation-active / production-gated / never feeds `decision_plan.json`; sandbox-only). Read (degrade gracefully on any miss — default-disabled until text connectors are enabled in config):
    - `outputs/sandbox/discovery/social_sentiment_status.json` → `status`, `tickers_scored`, `simulation_active`, `production_gated`, `feeds_decision_engine`, `sandbox_only`, `run_id` — pipeline run summary. `not_run` / absent is the expected inert steady state. Invariants `feeds_decision_engine=false` and `sandbox_only=true` must always be true — if either flips, that is a RED contract breach.
    - `outputs/sandbox/discovery/social_sentiment_simulation_adjustment.json` → `adjustments` (per-ticker; bounded max ±0.05 score adjustment derived from cross-source sentiment) — the simulation-lane output. Absent when pipeline hasn't run. **content_liveness**: `status == "ok"` AND `tickers_scored == 0` after all text connectors are enabled → looks-fresh-but-empty failure → dispatch `portfolio-discovery-health`.
23a. Crowd Intelligence (FMP lane) + Unified Crowd Bus (added 2026-06-16 as `run_daily_safe.sh` Stage 7d3 + Stage 9c3; **simulation-active / production-gated — never feeds `decision_plan.json`**; the active simulation lane may consume the unified bus; market-discovery lens). Read (degrade gracefully on any miss):
    - `outputs/latest/crowd_intelligence_status.json` → `overall_status` (`ok`|`degraded`|`failed`), `symbols_count`, `enabled_categories`, `disabled_categories` — Lane B per-ticker crowd context from AVAILABLE FMP endpoints. `social_sentiment` PLAN_LOCKED is expected (not a fault). **content_liveness**: `overall_status == "ok"` but `symbols_count == 0` is a looks-fresh-but-empty failure → dispatch `portfolio-discovery-health`.
    - `outputs/latest/unified_crowd_intelligence_status.json` → `overall_status` (`ok`|`degraded`|`failed`), `total_tickers`, `lane_a_tickers`, `lane_b_tickers`, `overlap_tickers`, `state_counts`, `social_sentiment_status`, `warnings[]` — joins Lane A (ApeWisdom retail) + Lane B (FMP context). `degraded` (single-lane) is expected/report-only. **content_liveness**: both lanes available but `total_tickers == 0` is a looks-fresh-but-empty failure → dispatch `portfolio-discovery-health`.
24. Tax/Strategy broker-aware layer (added 2026-06-12; strategy/tax-aware hardening — all observe-only; absent/degraded is the inert state pre-broker-aware-flip, report don't alert):
    - `outputs/sandbox/strategy_tax_scorecard.json` → `degraded_mode`, `degraded_fields`, `portfolio_unrealized_gain`
    - `outputs/latest/tax_harvest_advisor.json` → `basis_source`, `harvestable_count`
    - `outputs/sandbox/strategy_comparison.json` → `context_source`
    - `outputs/latest/schwab_tax_lots.json` → `has_lots`
25. FMP Data Budget Governor (added 2026-06-15 as `run_daily_safe.sh` Stage 7d2; **observe-only** — the single guarded FMP access layer wrapping `fmp_client`; never feeds `decision_plan.json`; developer lens). Read (degrade gracefully on any miss — the layer ships enabled with a kill-switch, so absence means it has not run yet, which is inert):
    - `outputs/latest/data_budget_status.json` → `overall_status` (`ok`|`near_cap`|`constrained`), `monthly_bandwidth_pct`, `discovery_skipped_due_to_budget`, `backtest_skipped_due_to_budget`
    - `outputs/latest/fmp_usage_status.json` → `calls_by_run_mode`, `calls_by_endpoint`
    - `outputs/latest/fmp_cache_status.json` → `cache_hit_rate`, `portfolio_fresh`
26. `outputs/regime/regime_performance.json` → `resolved_signals`, `by_regime` (keyed by distinct regime_label), `observability.degraded_mode_vs_regime_reliability.<label>.avg_regime_confidence` (added 2026-06-23; **regime-degeneracy liveness** — quant lens). The producer-ordering fix (077c8a1a, `docs/REGIME_CLASSIFIER_NEUTRAL_COLLAPSE.md`) restored live regime tags on signal_outcomes rows; this check guards against a re-regression. `len(by_regime)` is the distinct-label count over the resolved window. **content_liveness**: `len(by_regime) == 1` AND `resolved_signals ≥ 30` is a looks-fresh-but-empty degeneracy (the classifier or its producer-ordering collapsed to a constant bucket) — the smoking-gun confirmation is `avg_regime_confidence == 0.0` for the single label (the hardcoded `("neutral", 0.0, "limited")` fallback triple, distinct from a *legitimately* calm single-regime window which carries real confidence ~0.77–0.88). Cross-checks the ledger entry `manual:regime_classifier_neutral_collapse`.
27. `outputs/promotion_review/operator_approval_packet.json` → `tier_sim`, `tier_production`, `counts.{tier_sim_within_veto,tier_production_pending}` (added 2026-07-15; **operator approval packet** — one-shot two-tier packet the evening email + GUI approval page both read; read-only, never mutates governance state; process-analyst + developer lens). Ships GATED (`sim_governance.approval_packet.enabled=false`); absent is the expected inert steady state pre-activation (report, don't alert). Compute health via:
    ```bash
    python -c "import json; from portfolio_automation.sim_governance.approval_packet import assess_packet_health; print(json.dumps(assess_packet_health('outputs', __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()), indent=1))"
    ```
    `assess_packet_health` never raises. **AMBER**: a tier-b candidate pending longer than `stale_pending_days` (default 3; operator decision-queue aging), or the packet is missing/unreadable while activated. **RED**: the packet marks an item decided (`approved`/`rejected` in its `status` string) but no matching valid record exists in `promotion_approvals.approved_proposal_ids`/`rejected_proposal_ids` (`packet_gate_drift` — a contract breach, escalate). **content_liveness**: packet fresh (generated today) but BOTH `tier_sim` and `tier_production` are empty while `pending_proposals.json` is non-empty — the packet builder silently dropped candidates it should have surfaced.

28. `outputs/latest/memo_datasets.json` → `domains.<name>.status` for each of `portfolio`, `crowd_watchlist`, `institutional`, `risk`, `system` (added 2026-07-20 as `run_daily_safe.sh` Stage 10c; **observe-only per-domain reassembly of the memo-producer artifacts — read-only, no dispatch/mutation, never feeds `decision_plan.json`**; decision_core lens). Purely a read-only consumer line — this check never triggers a dispatch. Count `ok` vs `unavailable` domains. **content_liveness**: artifact fresh (generated today) but ALL domains read `unavailable` while their underlying source artifacts (e.g. `system_decision_summary.json`, `unified_crowd_intelligence_status.json`, `daily_run_status.json`) are themselves present/fresh → warn (the reassembly step silently dropped inputs it should have picked up); absent artifact entirely is the expected pre-activation/not-yet-run inert state (report, don't alert).

**Compute**:

- `data_budget_health` = `portfolio_automation.data_budget.health.data_budget_health(data_budget_status)` → `{status: green|amber, reason}` (pure helper; absent artifact ⇒ green/inert; AMBER on bandwidth ≥80% of cap or any discovery/backtest skip — never RED, observe-only)
- `fingerprint_age_days` = days since current `first_seen_at`
- `fingerprint_changed` = state.last_fingerprint != current_fingerprint
- `current_fp_resolved_1d`, `pre_tracker_resolved_1d`
- `delta_hit_rate_pp` = (current_fp.hit_rate_1d − pre_tracker.hit_rate_1d) × 100
- `newly_crossed_thresholds` = thresholds in `{10, 30, 50, 100}` that `current_fp_resolved_1d` reached today but were not in `state.thresholds_crossed`
- `regime_label_distinct_count` = `len(regime_performance.by_regime)` (0 if artifact absent); `regime_degenerate` = `regime_label_distinct_count == 1 AND resolved_signals ≥ 30`; `regime_fallback_confirmed` = `regime_degenerate AND the single label's avg_regime_confidence == 0.0` (the fallback-triple signature, i.e. a producer-ordering re-regression rather than a genuinely calm window)
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
- `broker_reauth_status` = `broker_sync_status.reauth_status` ∈ `{ok, due_soon, expired, unknown}` (default `unknown`); `broker_reauth_days_remaining` = `broker_sync_status.reauth_days_remaining` (float or null). Tracks the Schwab **7-day refresh-token clock** — Schwab issues no rolling replacement, so a browser re-auth (`exchange_code`) is mandatory when it lapses. `unknown` is the inert/legacy state (no anchor yet, or uncredentialed — report, don't alert; it populates after the next refresh or re-auth). `due_soon` (≤2 days left) and `expired` are the actionable states.
- `outputs/latest/schwab_reauth_notification_status.json` → `enabled`, `sent`, `reason`, `error_class` (Stage 10d optional out-of-band email heads-up; default-inert `enabled:false` is the steady state — report, don't alert). `broker_reauth_notify_failed` = `enabled == true` AND `broker_reauth_status ∈ {due_soon, expired}` AND `sent == false` AND `reason ∈ {send_failed, smtp_error, missing_smtp_config, invalid_or_missing_recipients}` (the operator opted into the email alert but it didn't go out — the in-system AMBER below still fired, so this is advisory).
- `outputs/latest/schwab_reauth_session_status.json` → `outcome` (`success|timeout|error|cloudflared_missing`), `started_at`, `new_expires_at` (auto-capture re-auth session result; absent until the first `schwab_reauth --begin` run — absence is inert, report don't alert). `broker_reauth_capture_failed` = the most recent session `outcome ∈ {timeout, error, cloudflared_missing}` AND `broker_reauth_status ∈ {due_soon, expired}` (an attempted auto-capture re-auth did not complete while re-auth is actually due — advisory; fall back to the manual `exchange_code` flow).
- `outputs/latest/decision_holdings_source.json` → `holdings_source` (`broker|config`), `confidence_modifier`, `reason` (added 2026-06-12; Part B — which holdings source drove the decision run after the broker overlay; absent until the first overlaid pipeline run). `reason` vocabulary (from `holdings_resolver`): `fresh_broker_snapshot` (broker, conf 1.0 — **healthy**), `broker_aware_disabled` (flag off — expected inert when unprovisioned), `broker_data_missing` / `broker_data_stale` / `broker_no_positions` (config fallback, conf 0.8–0.85). **Post-2026-06-16 reorder semantics:** Stage 0b now refreshes the Schwab snapshot *before* the decision run, so when `portfolio.broker_aware.enabled` AND Schwab is credentialed+`ok`, the expected steady state is `holdings_source == "broker"` / `reason == "fresh_broker_snapshot"`. The old benign 24h-boundary `config` flap is fixed — therefore `decision_on_config_while_broker_ok` = `portfolio.broker_aware.enabled` AND `broker_sync_status.overall_status == ok` AND `decision_holdings_source.holdings_source == "config"` now signals a **real freshness regression** (the sync ran fresh first but the overlay still fell back — e.g. `reason == broker_data_stale/broker_data_missing` despite a same-run sync, or a silent Stage 0b failure). Dispatch `portfolio-resolver-investigator` with the `reason`; advisory/AMBER, never RED (holdings are a decision *input*; the decision core is untouched).
- operator-control: from `.venv/bin/python -m operator_control.worker_runner status` (by_status counts + autonomous_enabled) and `outputs/operator_control/audit_log.jsonl`:
  - `operator_open_count` = orders in {queued, awaiting_approval, claimed, running, approved}; `operator_failed_count` = orders in `failed`
  - `operator_quarantined_today` = count of `worker_protected_path_violation` audit events dated today (absent files → 0)
  - `operator_stuck_running` = any order in `running`/`claimed` whose last `status_history.at` is > 24h ago (worker crashed mid-run, or a scaffolded order was never completed)
  - `operator_worker_mode` = `"autonomous ON"` if `autonomous_enabled` else `"scaffold-only"`
- next-stage lane (Phases 1–15; all default to `null`/`absent` if the artifact is missing — the lane ships non-fatal and is observe-only):
  - `next_stage_lane_ran` = any next-stage artifact's `generated_at` is within the last 26h (lane fired in today's cron)
  - `next_stage_radar_candidates` = `opportunity_radar.opportunity_count`
  - `next_stage_opp_queue_open` = `opportunity_approval_queue.queue_count`; `next_stage_improvement_open` = `system_improvement_ideas.idea_count`
  - `next_stage_strategy_profiles` = `len(strategy_comparison.comparison)`; `next_stage_strategy_top` = `strategy_id` of the entry with `final_strategy_rank == 1`; `next_stage_strategy_context` = `strategy_comparison.context_source`
  - `next_stage_shadow_tracked` = `shadow_opportunity_tracking.record_count`
  - `next_stage_broker_aware_source` = `broker_aware_portfolio.holdings_source`; `next_stage_lane_degraded_steps` = count of next-stage producers reporting a degraded/error state (e.g. `broker_aware_portfolio.degraded_mode == true`)
  - `operator_market_decisions` = count of lines in `outputs/policy/user_decisions.jsonl` (human approve/reject/etc. on market-opportunity candidates via `POST /dashboard/strategy-lab/opportunity-decide`); `operator_market_promotions` = count of those with `action == "approve_to_watchlist_review"` AND `promote_result.status == "promoted"`. Both default 0 (absent = inert — the queue is operator-driven, not cron-driven; report, don't alert). An `approve_to_watchlist_review` writes to the live `extended_watchlist` DB (capacity-gated) — `portfolio-discovery-health` audits that DB, so an operator promotion surfaces there as a thing to VERIFY, not a violation.
- pipeline-wiring probe (from `pipeline_wiring_status.json`; default all-`0`/`green` if the artifact is missing — the probe is observe-only and non-fatal):
  - `wiring_overall` = `overall_status` (green|amber; never red)
  - `wiring_unwired` = `summary.unwired`; `wiring_cadence_mismatch` = `summary.cadence_mismatch`; `wiring_silently_skipped` = `summary.silently_skipped`; `wiring_fresh_but_empty` = `summary.fresh_but_empty`
  - `wiring_problems` = `wiring_unwired + wiring_cadence_mismatch + wiring_silently_skipped + wiring_fresh_but_empty`
  - `wiring_flagged` = the `producers[]` entries whose `status` is in {unwired, cadence_mismatch, silently_skipped, fresh_but_empty} (artifact + status + caller_cadences — the root-cause detail to pass to the agent)
- simulation-governance lane (Stage 10e; `portfolio_automation/sim_governance/`; added 2026-06-16). This lane is the two-lane model: simulation is ACTIVE (may change SANDBOX/SIMULATION outputs), production promotion is human-gated. It is **not observe-only** — but the only mutating path is human-approved proposals → production overlays (default-OFF). Treat an applied/approved event as a thing to VERIFY, not an observe-only violation. Read `outputs/promotion_review/daily_governance_status.json` + `daily_ai_review_result.json`/`daily_ai_review_deferred.json` + `pending_proposals.json` + `outputs/promotion_approvals/production_application_state.json` (all default `null`/absent → inert, report don't alert):
  - `sim_gov_ran` = `daily_governance_status.generated_at` within last 26h; `sim_gov_candidates` = `bundle` stage candidate_count
  - `sim_gov_ai_status` = review `status` (`reviewed|deferred|already_reviewed_today|disabled`); `sim_gov_ai_cost` = `actual_cost_usd`; `sim_gov_ai_cap` = `daily_cost_cap_usd` (0.50)
  - `sim_gov_ai_deferred` = `daily_ai_review_deferred.json` present for today AND review not `reviewed` (estimated cost exceeded the $0.50 cap — the consolidated review was skipped; advisory)
  - `sim_gov_pending` = `pending_proposals.pending_count`; `sim_gov_applied` = `production_application_state.applied_count`
  - **backlog review** (the per-proposal review + human-approval routing; module of record `portfolio_automation/sim_governance/backlog_review.py`, read-only, writes nothing): run
    ```bash
    python3 -c "import json; from portfolio_automation.sim_governance.backlog_review import review_pending_backlog; print(json.dumps(review_pending_backlog('.'), indent=1))"
    ```
    It joins `pending_proposals.json` ↔ `daily_ai_review_result.json` verdicts by `candidate_id`, classifies each proposal (`ready`/`hold`/`reject`/`unknown`), computes `age_days`, and derives a recommended **human** action per proposal. From its return: `simgov_ready_count` = `ready_count` (proposals the AI marked `ready_for_production_review` → routed to human approval), `simgov_hold_count`/`simgov_reject_count`, `simgov_oldest_ready_age_days` = `oldest_ready_age_days`, and `simgov_ai_can_approve` = `ai_can_approve_production` (a structural invariant that must stay `false` — AI can never approve production). Degrades to `available:false` when `pending_proposals.json` is absent.
  - `sim_gov_overlay_live` = `production_overlay_live.watchlist OR .advisory` (the live production overlays were flipped on — VERIFY any applied proposal against its approval record + audit trail)
  - **content_liveness**: `sim_gov_ran` true AND `sim_gov_candidates == 0` for the day is a looks-fresh-but-empty signal only if discovery/crowd inputs were expected (the lane runs after baseline; usually benign — report, don't alert)
  - Flock Intelligence (Step 1 of the sim-gov lane; `portfolio_automation/flock_intelligence/`; added 2026-06-16; simulation-only/observe-only — never feeds `decision_plan.json`). Read `daily_governance_status.json:stages.flock_intelligence` (+ the artifacts `outputs/simulation/flock_intelligence.json` / `flock_watchlist_candidates.json` / `flock_advisory_context.json`; all default absent → inert, report don't alert):
    - `flock_ran` = `stages.flock_intelligence.ok`; `flock_groups` = `stages.flock_intelligence.group_count`; `flock_dq` = `stages.flock_intelligence.data_quality` (`ok|insufficient_data|unknown`); `flock_wl_candidates` = `stages.flock_intelligence.watchlist_candidate_count`
    - from `flock_intelligence.json:summary`: `flock_forming/confirmed/exhaustion/dispersing/broken` group lists
    - **content_liveness**: `flock_ran` true AND `flock_groups > 0` AND `flock_dq == "insufficient_data"` for a sustained period is a looks-fresh-but-empty signal (groups built but none classifiable — crowd/price history too sparse; dispatch `portfolio-discovery-health`). `flock_groups == 0` while themes/sectors exist is a grouping/upstream gap. `insufficient_data` on a fresh layer is the expected early state (report, don't alert).

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
- next-stage lane healthy: `next_stage_lane_ran` is false (not yet run — neutral) OR (`next_stage_radar_candidates > 0` AND `next_stage_lane_degraded_steps ≤ 1`) — the lane is observe-only/advisory; `broker_aware` degraded-to-config while Schwab is unconfigured is the expected single degraded step and does NOT break GREEN
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
- `broker_reauth_status ∈ {due_soon, expired}` (the Schwab 7-day refresh token is about to lapse or has lapsed — a browser re-auth is required to keep the daily sync alive. `due_soon` is a heads-up so re-auth stays a planned ~30s task; `expired` means the sync will go `degraded`/unauthenticated on the next run until re-auth. Advisory only — point the operator to `docs/schwab_integration.md` OAuth bootstrap. Never RED.)
- `broker_reauth_notify_failed` (the operator enabled the Stage 10d email heads-up but the send failed/was misconfigured — the in-system re-auth AMBER above already fired, so the re-auth is still surfaced; fix the SMTP env per `docs/schwab_integration.md`. Advisory; never RED.)
- `broker_reauth_capture_failed` (an auto-capture re-auth session failed/timed out while re-auth is due — the manual `exchange_code` flow in `docs/schwab_integration.md` still works. Advisory; never RED.)
- `operator_quarantined_today ≥ 1` (the autonomous worker hit a protected path today — contained/quarantined, never merged; review `/dashboard/operator/report/<id>` to confirm the guard fired correctly) OR `operator_stuck_running` (an order has sat in `running`/`claimed` > 24h — worker crashed or a scaffolded order was abandoned; inspect the worktree). Operator-control is observe-only and **never** RED (it never blocks the decision core). OR `operator_cost_cap_pct ≥ 80` (daily operator-worker cost cap ≥80% exhausted — runs will defer once it reaches 100%) OR a `worker_cost_cap_exceeded` event today (a run breached the per-run rail; confirm the `--max-turns`/timeout rails are configured). Cost-cap AMBER is observe-only and never RED. OR `quarantine_pending ≥ 1` (a contained candidate fix is awaiting human salvage/discard — review `/dashboard/operator/report/<id>` then `quarantine-salvage` or `quarantine-discard`). Still observe-only, never RED.
- next-stage lane silent-zero: `next_stage_lane_ran` is true AND `next_stage_radar_candidates == 0` (the universe scanner ran but emitted no opportunities — discovery upstream likely broke; dispatch `portfolio-discovery-health`) OR `next_stage_lane_degraded_steps ≥ 2` (more than the expected broker-aware-only degradation — a second next-stage producer degraded). The next-stage lane is observe-only/advisory and **never** RED (it never feeds `decision_plan.json`).
- pipeline-wiring problem: `wiring_problems ≥ 1` (the wiring probe found a producer that is `unwired`, `cadence_mismatch`, `silently_skipped`, or `fresh_but_empty` — a declared producer that is not actually producing on its cadence). Advisory: dispatch `portfolio-discovery-health` with `wiring_flagged` so it can confirm the root cause and propose the wire-up. The probe is observe-only/AMBER-max and **never** RED (it is a meta-monitor, not the decision core).
- `tax_scorecard_unexpectedly_degraded` = broker_aware enabled AND `broker_sync_status.overall_status == ok` AND `strategy_tax_scorecard.degraded_mode == true` (broker is live but the cost-basis plumbing didn't flow to the scorecard — advisory; quant lens; never RED).
- `sim_gov_ai_deferred` (process-analyst lens; AMBER) — the daily consolidated AI/product review was skipped because its estimated cost exceeded the $0.50/day cap; a `daily_ai_review_deferred.json` was written. Advisory: confirm the packet didn't balloon unexpectedly; never RED.
- `sim_gov_pending_backlog` = `sim_gov_pending ≥ 5` (AMBER; process-analyst lens) — promotion proposals are accumulating without human approve/reject decisions. Advisory: surface the queue for operator review; never RED.
- `simgov_ready_awaiting_approval` = `simgov_ready_count ≥ 1` (AMBER; process-analyst + market-expert lens) — one or more pending proposals have been reviewed testing-ready (`ready_for_production_review`) and are routed to human approval. This is an *actionable* AMBER, not a fault: the operator should review each ready proposal (evidence / risk / rollback in the 6n block) and approve/reject via the human-gated path (GUI `/governance` or `promotion_approvals.record_approval`). Never RED. If `simgov_ai_can_approve` is ever `true`, that is a RED contract breach (AI must never be able to approve production) → escalate.
- `sim_gov_overlay_applied` (developer + process lens; **VERIFY, not a violation**) = `sim_gov_overlay_live` true AND `sim_gov_applied ≥ 1` — approved proposals are live in production. Per the sanctioned-mutator rule, treat as a thing to verify against the approval record + `production_application_audit.jsonl`, not an observe-only breach to revert. AMBER only if an applied proposal has no matching valid human approval (a contract breach → escalate).
- `strategy_context_not_broker` = broker_aware enabled AND Schwab `ok` AND `strategy_comparison.context_source == config` (resolver not flowing through to the strategy comparison — advisory; quant/market-expert lens; never RED).
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
- `decision_on_config_while_broker_ok` (the decision run fell back to config holdings despite Schwab being credentialed+`ok` AND the sync now running ahead of the decision run at Stage 0b — a freshness regression; pass `decision_holdings_source.reason` so the agent can trace the broker-snapshot age / Stage 0b outcome)

`portfolio-attribution-analyst` IF any of:
- `newly_crossed_thresholds` is non-empty (sample-size milestone — n=10, 30, 50, or 100 first crossed)
- `fingerprint_changed` (new gauge era — analyst reads fresh baseline)
- `|delta_hit_rate_pp| ≥ 10` AND `current_fp_resolved_1d ≥ 30`
- `regime_degenerate` (regime_performance shows a single regime_label over ≥30 resolved signals — the classifier/outcome-row tagging has collapsed to a constant bucket, masking the volatility regime that explains hit-rate swings; pass `regime_label_distinct_count`, the dominant label, `resolved_signals`, and `regime_fallback_confirmed`). When `regime_fallback_confirmed` (avg_regime_confidence == 0.0), this is the producer-ordering re-regression that 077c8a1a fixed — flag it as a returned-defect, not a market read. Always also surfaces to the quant-watch sub-check ledger entry `manual:regime_classifier_neutral_collapse`.

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
- `next_stage_lane_ran` is true AND `next_stage_radar_candidates == 0` (the next-stage universe scanner / opportunity-radar ran but emitted zero candidates — a silent-zero in the broad-market discovery layer; pass that the radar is empty so the agent can trace whether the scanner's price/universe inputs went stale)
- `wiring_problems ≥ 1` (the pipeline-wiring probe flagged a producer as `unwired` / `cadence_mismatch` / `silently_skipped` / `fresh_but_empty` — a declared producer not actually producing on its cadence). Pass `wiring_flagged` (artifact + status + caller_cadences) so the agent can confirm the root cause via the producer→caller chain and propose the wire-up (the static caller-grep is best-effort; the agent verifies). This is the generalization of the 2026-06-11 stale-producer audit — it catches the NEXT unwired producer, not just the ones already fixed.
- Crowd Radar looks-fresh-but-empty: `crowd_knowledge_state.source_status == "ok"` AND `crowd_state_count == 0` (the Public Knowledge Velocity Layer fetched but classified nothing — ticker-extraction or feed upstream likely broke), OR `source_status ∈ {rate_limited, source_terms_blocked, error}` once provisioned, OR `social_source_compliance.review_needed_count > 0` (a source's ToS review lapsed). Pass the `source_status` + `data_quality_status` so the agent can trace the connector → extractor → classifier chain. `disabled`/`no_credentials` is the inert pre-provisioning state — NOT a dispatch trigger.

This agent audits the discovery layer: RSS feedparser availability, LLM reachability (OpenAI primary / Anthropic fallback), theme_signals emit-rate, extended_watchlist promotions, FMP profile-cache freshness, parallel FMP candidate-scanner wiring, the **next-stage opportunity-radar / universe-scanner emit-rate**, **discovery_pulse cron health, AI budget cap utilization, FMP monthly headroom, the pipeline-wiring probe's flagged producers (unwired / cadence_mismatch / silently_skipped / fresh_but_empty), and the Crowd Radar / Public Knowledge Velocity Layer connector→extractor→classifier chain**. Surfaces stacked silent-zero failures.

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

`portfolio-learning-loop-health` (auto-approval oversight lens) IF the GPT simulation auto-approval audit (`outputs/policy/auto_approval_audit.json`) shows `circuit_breaker.engaged == true` OR the ledger has any `failure`/`rollback_conflict` today (RED/AMBER per §Triage) OR any `applied`/`human_veto`/`rollback` event today (VERIFY — confirm each applied event carries `approval_channel=="auto_approval"`, `is_human_approved==false`, `target_lane=="simulation"`, `production_mutation==false`, `feeds_decision_engine==false`, and that any veto rolled back cleanly). This is oversight of the sanctioned SIMULATION auto-approval mutator: surface + verify, never revert a legitimate simulation event.

`portfolio-learning-loop-health` (operator approval queue lens) IF `assess_packet_health` (over `outputs/promotion_review/operator_approval_packet.json`) returns AMBER (`packet_missing_or_unreadable` while activated, or `stale_pending:*`) or RED (`packet_gate_drift:*` — a decided item in the packet with no matching `promotion_approvals` record). Pass the full `reasons` list + `counts` so the agent can VERIFY each `stale_pending` candidate against the live `pending_proposals.json` queue and each `packet_gate_drift` candidate against `promotion_approvals.approved_proposal_ids`/`rejected_proposal_ids` — never revert a legitimate approval/veto the packet correctly reflects.

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

0. Artifact governance (always, first): `"Coverage: {present}/{total} present · {missing} missing ({missing_required} required) · {stale} stale · {idle} idle · debt {unjustified_debt} (target 0) · classified {classified}/{total} · {overall_status}"` — from artifact_registry_status.json. `idle` (counts.idle) = append-only event logs with no recent event (idle_ok rows) — informational, never a problem, never escalates. RED here (critical/source-of-truth missing) forces the daily lead line to RED.
1. Attribution snapshot (always): `"Attribution: current-fp n={N} at {H}% / pre-tracker n={N} at {H}% · Δ {sign}{pp}pp · top sector {gauge_top_sector}"`
2. Risk-delta state (always): `"Risk: {top_symbol} {weight}% (cap {cap}%, +{headroom}pp); leverage {L}%"`
3. Discovery pulse + AI spend (always, since they're project-wide health signals): `"Pulse: last={pulse_age_hours}h ago, {total_runs_month} runs MTD ({skipped_runs_month} skipped) · AI: ${monthly_cost_total_usd:.2f}/${monthly_cost_limit_usd:.0f} cap ({ai_budget_pct_of_cap}%)"`
4. Learning loop snapshot (always when `pattern_efficacy_monthly.json` exists): `"Loop: match-rate {pattern_match_rate}% · {retune_applies_last_7d} applies/7d · drift max {retune_drift_max_pct}% of cap · apply_enabled={retune_apply_enabled}"`
5. Content liveness (only when `content_warn_count > 0`): `"Liveness warns ({content_warn_count}): {csv list of warn names}"`
5b. Applied-fix verification (only when `applied_fixes` is non-empty): `"Fixes: {confirmed} confirmed · {pending} pending · {manual} manual{, REGRESSED: <id(s)> if any}"`. List each regressed id explicitly with its `detail`. When a fix is `confirmed`, it is dropped from state in Step 5 (it held — stop re-checking).
6c. Docs (only when `doc_audit_status.json` exists): `"Docs: {overall_status} · {N} findings, {K} coverage gaps (last audit {last_audited_sha[:8]})"`
6d. Pattern-loop (always, from the sub-check above): `"Pattern-loop: {mode}, OOS {observed}/315 (~{eta}), proposals {N}, auto-apply {state}"` — folds in the `/pattern-loop-analysis` heartbeat.
6e. Quant-watch (always, from the sub-check above): `"Quant-watch: {overall_status} · {active_count} active ({top active probe concern}); {len(registered_today)}↑/{len(resolved_today)}↓/{len(escalated_today)} esc today"` — folds in the `/quant-watch-analysis` heartbeat. RED only when `escalated_today` is non-empty (which is already a daily RED key).
6f. Broker-sync (always): `"Broker-sync: {broker_status} (configured={broker_configured}, authenticated={broker_authenticated}) · {account_count} accts / {position_count} positions · re-auth {broker_reauth_status}{ in {broker_reauth_days_remaining}d if due_soon}"` — Schwab read-only layer. `unconfigured`/`disabled` is the inert steady state (report, don't alert). `degraded`/`error` → AMBER; append `"— see docs/schwab_integration.md Troubleshooting"`. `broker_reauth_status ∈ {due_soon, expired}` → AMBER; append `"— browser re-auth due, see docs/schwab_integration.md OAuth bootstrap"`. `re-auth unknown` is inert (legacy/uncredentialed — report, don't alert). If `broker_reauth_notify_failed`, also append `"(email heads-up failed to send — check SMTP env)"`. If a `schwab_reauth_session_status.json` exists, append `"· last capture {outcome}"`; if `broker_reauth_capture_failed`, append `"(auto-capture failed — use manual exchange_code)"`. Never RED (observe-only evidence; never blocks decisions).
6g. Operator-control (always): `"Operator-control: {open} open · {awaiting_approval} awaiting · {failed} failed · {quarantined} quarantined · {stale} stale · quarantine-pending {quarantine_pending} · worker {mode} · readiness {overall_ready} · cost today ${today_usd}{/{cap_usd} ({cap_pct}% of daily) if cap_configured else ' (uncapped)'} · lifetime ${lifetime_usd}{ · deferred {n} · over-cap {m} if any cost-cap events today}"` — observe-only; computed live from `operator_worker_view` (NO new persisted artifact). AMBER on `quarantined ≥ 1`, `quarantine_pending ≥ 1` (a contained candidate fix is awaiting human salvage/discard), stale-run, `cap_pct ≥ 80` (daily cost cap nearly exhausted), or any `worker_cost_cap_exceeded` event today; readiness `<5/5` is reported, not alerted (it's the hardening-milestone tracker). Never RED.
6h. Next-stage lane (always when `next_stage_lane_ran`; else `"Next-stage lane: not run today"`): `"Next-stage lane: radar {next_stage_radar_candidates} candidates · queues {next_stage_opp_queue_open} opp / {next_stage_improvement_open} improvement · strategy top {next_stage_strategy_top} of {next_stage_strategy_profiles} ({next_stage_strategy_context}) · shadow {next_stage_shadow_tracked} tracked · broker-aware {next_stage_broker_aware_source}{, +N degraded if next_stage_lane_degraded_steps>1} · operator-decisions {operator_market_decisions} ({operator_market_promotions} promoted to watchlist)"` — the activated Phases 1–15 research/strategy lane (observe-only, advisory side-panels, never feeds `decision_plan.json`). This one line is the per-phase heartbeat: radar = universe scan (5–6), queues = opportunity prompts + system-improvement (3/4/8), strategy = Multi-Strategy / Strategy Lab (11A/12–13), shadow = sandbox tracking (7), broker-aware = holdings resolver (10). A `config` broker-aware source while Schwab is unconfigured is expected (report, don't alert). AMBER only on the silent-zero / ≥2-degraded conditions in Step 2; **never RED** (the lane never blocks the decision core).
6i. Pipeline-wiring (always): `"Pipeline-wiring: {wiring_overall} · {healthy}/{total_audited} healthy · {wiring_unwired} unwired · {wiring_cadence_mismatch} cadence-mismatch · {wiring_silently_skipped} silently-skipped · {wiring_fresh_but_empty} fresh-but-empty"` — the root-cause layer over the registry (Stage 13). When `wiring_problems ≥ 1`, append the flagged producers, e.g. `"— flagged: doc_audit_status (unwired)"`. `event_log_idle` (append-only telemetry logs) and `not_audited` (on_demand) are the expected non-problem states (don't alert). AMBER on any problem; **never RED** (meta-monitor, never blocks the decision core).
6j. Memo-coherence (always when `memo_coherence.json` present; else `"Memo-coherence: not run"`): `"Memo-coherence: {coherence_status} · {funding.funded_count} funded / {funding.blocked_count} deferred (${funding.funded_capital}{ from-incoming if deployable_from_cash==0 }) · weekly ${funding.monthly_envelope.weekly_pacing.weekly_remaining}/${funding.monthly_envelope.weekly_pacing.weekly_tranche} paced (glide ${funding.monthly_envelope.glide_slice}); {count of deferred_actions with status/blocking_reason == DEFERRED_BY_WEEKLY_PACING} week-paced · default-priority {ranking.default_fallback_count}/{ranking.total_actions} · hit-rate {hit_rate.directional_accuracy_pct}% neutral-band (neutral {hit_rate.neutral}) · {reconciliation.unresolved_count} unresolved"` — the memo decision-coherence reconciliation layer (added 2026-06-30; **observe-only, advisory, never feeds `decision_plan.json`**; market-expert lens). It reconciles verdict/theme/opportunity/fit/cash/actions and splits funded vs unfunded. The **glide-in + weekly-pacing** capital layer (added 2026-07-07) deploys the monthly contribution PLUS a capped slice of idle excess cash (`glide_slice`), paced weekly (`weekly_pacing`). `below_safety_floor` with funding from incoming contributions is expected (report, don't alert). AMBER on `coherence_status == "degraded"` (a source artifact missing → funding/hit-rate degraded) or `reconciliation.unresolved_count ≥ 1` (an unresolved contradiction — dispatch `portfolio-memo-reviewer` to confirm against source JSONs). Report-don't-alert (advisory) but note when: `glide_slice == 0` while `monthly_capital_envelope.idle_excess` is large (glide effectively stuck — verify config `excess_cash_glide_fraction`), or ALL capital names are `DEFERRED_BY_WEEKLY_PACING` with `weekly_remaining == 0` (week fully paced — normal early-week, but a persistent zero across a full week suggests a ledger/pacing bug → dispatch `portfolio-memo-reviewer`). The weekly figure is a *live residual* ("remaining this week"), not a fixed tranche — it drifts down intra-week by design. Resolved contradictions are explanatory context, not faults. **Never RED** (the layer never blocks the decision core and never executes trades). Source: `outputs/latest/memo_coherence.json`.
6k. Crowd Radar (always): `"Crowd-Radar: {crowd_source_status} · {crowd_state_count} states classified · backtest {len(crowd_states_matured)} matured · compliance {crowd_review_needed} review-needed · ready_to_collect {crowd_ready}"` — the Public Knowledge Velocity Layer (Stage 9c, observe-only/sandbox-only/default-disabled). `disabled`/`no_credentials` is the expected inert steady state before provisioning (report, don't alert). AMBER on: `source_status == "ok"` AND `state_count == 0` (looks-fresh-but-empty → dispatch `portfolio-discovery-health`), `source_status ∈ {rate_limited, source_terms_blocked, error}`, `crowd_review_needed > 0` (a source ToS review lapsed), or the activation check's `source_terms_status ∈ {review_needed, blocked}`. **Never RED** unless the activation check shows `sandbox_only_assertion` or `decision_engine_blocked` flipped false (a contract breach). `crowd_ready` comes from `crowd_radar_activation_check.json:ready_to_collect`.
6k2. Crowd Intelligence (FMP lane) (always): `"Crowd-Intel (FMP): {overall_status} · {symbols_count} symbols · enabled {len(enabled_categories)}/disabled {len(disabled_categories)} cat."` — Lane B per-ticker crowd context from AVAILABLE FMP endpoints (Stage 7d3, context lane / production-gated; never feeds `decision_plan.json`). `social_sentiment` PLAN_LOCKED is expected (not a fault). AMBER on `overall_status ∈ {degraded, failed}`, or content_liveness (`overall_status == "ok"` AND `symbols_count == 0`) → dispatch `portfolio-discovery-health`. **Never RED**. Source: `crowd_intelligence_status.json`.
6k3. Unified Crowd Bus (always): `"Unified-Crowd: {overall_status} · {total_tickers} tickers (A {lane_a_tickers}/B {lane_b_tickers}/overlap {overlap_tickers}) · {social_sentiment_status} · states {state_counts}"` — joins Lane A (ApeWisdom retail) + Lane B (FMP context) into one per-ticker view (Stage 9c3, simulation-active / production-gated; the active simulation lane may consume it; never feeds `decision_plan.json`). `degraded` (single-lane) is expected/report-only. AMBER on `overall_status == "failed"`, or content_liveness (both lanes available but `total_tickers == 0`) → dispatch `portfolio-discovery-health`. **Never RED** (production-gated context; never feeds `decision_plan.json`). Source: `unified_crowd_intelligence_status.json`.
6k4. Social Sentiment Pipeline (always when `social_sentiment_status.json` present; else `"Social-Sentiment: not run"`): `"Social-Sentiment: {status} · {tickers_scored} tickers scored · finbert {finbert_status} · sim-active=true · prod-gated=true"` — the free text-sentiment lane (Bluesky/Mastodon/Lemmy + FinBERT; Phases 3-11; simulation-active / production-gated; never feeds `decision_plan.json`; default-disabled until `crowd_radar.bluesky/mastodon/lemmy.enabled=true`). `finbert_status` is the FinBERT scorer status from the pipeline run (`ok` | `scorer_unavailable:…` | `disabled`). `not_run` / `insufficient_data` is the expected inert steady state before provisioning (report, don't alert). AMBER on: `status == "error"`, or content_liveness (`status == "ok"` AND `tickers_scored == 0` after all sources enabled) → dispatch `portfolio-discovery-health`. `feeds_decision_engine=false` and `sandbox_only=true` must always be true — if either flips, that is a RED contract breach. **Never RED** in the normal course (simulation-only / sandbox-only / human-gated for production). Source: `outputs/sandbox/discovery/social_sentiment_status.json` + `outputs/sandbox/discovery/social_sentiment_simulation_adjustment.json`.
6l. Tax/Strategy (always when broker_aware on; else `"Tax/Strategy: inert (broker-aware off)"`): `"Tax/Strategy: {portfolio_unrealized_gain} unrealized G/L · {harvestable_count} harvest cand. (basis {basis_source}) · strategy context {context_source}{ · lots present if has_lots}"` — observe-only/advisory; AMBER on `tax_scorecard_unexpectedly_degraded` and `strategy_context_not_broker` (see AMBER section); never RED. Source: `strategy_tax_scorecard.json` + `tax_harvest_advisor.json` + `strategy_comparison.json` + `schwab_tax_lots.json`.
6m. Regime-liveness (always when `regime_performance.json` exists; else `"Regime-liveness: not run"`): `"Regime-liveness: {regime_label_distinct_count} distinct label(s) over {resolved_signals} resolved · dominant {label} {pct}%{ · DEGENERATE (avg_conf 0.0 — fallback triple) if regime_fallback_confirmed}"` — guards the 2026-06-23 producer-ordering fix (077c8a1a). AMBER on `regime_degenerate` → dispatch `portfolio-attribution-analyst` and cross-check the quant-watch ledger entry `manual:regime_classifier_neutral_collapse`. A single label with real confidence (~0.77–0.88) over a short window is a legitimately calm tape (report, don't alert); a single label at `avg_regime_confidence == 0.0` is the fallback-triple re-regression (AMBER). **Never RED** (observe-only health signal; never blocks the decision core). Source: `outputs/regime/regime_performance.json`.
6m. Data-budget (always): `"Data-budget: {overall_status} · {monthly_bandwidth_pct}% of 20GB cap · {calls_this_run} calls/run · cache {cache_hit_rate}% · discovery-skipped {bool}"` — the FMP Budget Governor (Stage 7d2, observe-only; wraps fmp_client). Source: `data_budget_status.json` + `fmp_usage_status.json` + `fmp_cache_status.json`. AMBER when `data_budget_health.status == "amber"` (bandwidth ≥80% of cap, or discovery/backtest skipped due to budget) → dispatch `portfolio-discovery-health` (developer lens — it owns FMP headroom) with the bandwidth pct + skip flags. **Never RED** (observe-only; the governor never blocks the decision core — the kill-switch reverts to direct fmp_client). Absent artifacts = inert (not yet run), report don't alert.
6n. Sim-governance (always): `"Sim-gov: lane {active|disabled} · {sim_gov_candidates} cand. · AI {sim_gov_ai_status} ${sim_gov_ai_cost}/{sim_gov_ai_cap} cap · {sim_gov_pending} pending · {sim_gov_applied} applied · overlays {live|off}"` — the two-lane simulation-governance lane (Stage 10e). Simulation lane ACTIVE; production promotion human-gated; overlays default-OFF. AMBER on `sim_gov_ai_deferred`, `sim_gov_pending_backlog`, `simgov_ready_awaiting_approval`, or an applied proposal lacking a valid human approval. **Never RED** (the lane never feeds `decision_plan.json`; production overlays apply only human-approved, audited changes). Absent artifacts = inert (not yet run), report don't alert. Source: `daily_governance_status.json` + `daily_ai_review_result.json` + `pending_proposals.json` + `production_application_state.json`.
6n2. Sim-gov backlog review (always when `sim_gov_pending > 0`; else `"Sim-gov backlog: 0 pending"`) — the per-proposal review + human-approval routing (from `backlog_review.review_pending_backlog`). Lead with the summary line:
   `"Sim-gov backlog ({sim_gov_pending} pending: {simgov_ready_count} ready / {simgov_hold_count} hold / {simgov_reject_count} drop · overlays {live|off}):"`
   then one line per proposal (cap 8; if more, append `"+{N} more"`), most-ready first:
   `"- {proposal_id[:12]} {proposal_type} {symbol} · risk {risk_level} · ev {evidence_strength} · rollback {rollback_readiness} · AI {ai_decision} · {age_days}d → {recommendation}"`
   then the routing hand-off:
   - when `simgov_ready_count ≥ 1`: `"→ {simgov_ready_count} AWAITING HUMAN APPROVAL (oldest {simgov_oldest_ready_age_days}d) — review evidence/risk/rollback above, then approve via GUI /governance or promotion_approvals.record_approval(<proposal_id>, 'approve', <human_approver>, …). AI cannot approve (ai_can_approve_production={simgov_ai_can_approve})."`
   - else: `"→ 0 ready for human approval today ({simgov_hold_count} still testing)."`
   This realizes the "route once testing is good, receive human approval" flow: Claude classifies + routes the ready proposals and prints the exact human-only approval path, but **never approves**. `recommendation` ∈ {`AWAITING_HUMAN_APPROVAL`, `HOLD`, `DROP_CANDIDATE`, `SURFACE_FOR_REVIEW`}. AMBER on `simgov_ready_awaiting_approval` or `sim_gov_pending_backlog`. **RED only** if `simgov_ai_can_approve == true` (structural human-gate breach). Source: `backlog_review.review_pending_backlog('.')` over `pending_proposals.json` + `daily_ai_review_result.json`.
6n3. Auto-approval (SIMULATION) (always when `outputs/policy/auto_approval_audit.json` exists; else `"Auto-approval: inert (not run)"`): `"Auto-approval (sim): {applied} applied · {gpt_veto} gpt-vetoed · {human_veto} vetoed · {rollback} rolled-back · {rollback_conflict} conflict · {active_item_count} awaiting veto · breaker {engaged:reason|off}"` — the bounded GPT simulation auto-approval channel (Step 5b, sanctioned mutator; ships inert). SIMULATION-only: never feeds `decision_plan.json`, never mutates production. **RED** if: any `failure` event with `rollback_status==rollback_failed`; an `applied` event whose record is NOT `target_lane=="simulation"` / `production_mutation==false` / `feeds_decision_engine==false` / `is_human_approved==false` (authority breach); a duplicate application for one idempotency_key; or `circuit_breaker.failed_to_engage`. **AMBER** if: `active_item_count > 0` (items in veto window), a `human_veto`/`rollback` occurred today (control worked — VERIFY), a `rollback_conflict` awaits operator, the digest is enabled but delivery failed, a pending-human fallback fired on unavailable GPT, the breaker is engaged without a current production violation, or counts near a configured cap. A successful veto+rollback is **not** RED. Dispatch `portfolio-learning-loop-health` on any RED/AMBER. Absent artifacts / all-zero + breaker off = inert steady state (report, don't alert). Source: `auto_approval_audit.json` + `auto_approval_events.jsonl`.
6o. Flock Intelligence (always when `flock_ran`; else `"Flock: not run today"`): `"Flock: {flock_groups} groups (dq {flock_dq}) · forming {N}/confirmed {N}/exhaustion {N}/dispersing {N}/broken {N} · {flock_wl_candidates} wl candidates"` — simulation-only crowd flocking/dispersion (Step 1 of the sim-gov lane). Flock candidates ride the ONE consolidated AI review (no extra call) and only human-approved proposals reach production. AMBER only on the content_liveness condition above (groups built but all `insufficient_data` for a sustained period → dispatch `portfolio-discovery-health`). **Never RED** (simulation-only; never feeds `decision_plan.json`). Source: `daily_governance_status.json:stages.flock_intelligence` + `flock_intelligence.json:summary`.
6p. Decision-holdings source (always when `decision_holdings_source.json` present; else omit): `"Decision-holdings: {holdings_source} ({reason}){, conf ×{confidence_modifier} if <1.0}"` — which holdings fed the decision run after the §1a broker overlay. Healthy steady state once Schwab is live is `broker (fresh_broker_snapshot)`. Post-2026-06-16 reorder (Stage 0b runs the sync *before* the decision run), AMBER when `decision_on_config_while_broker_ok` (broker_aware on AND `broker_sync_status==ok` AND `holdings_source==config`) — a real freshness regression now that the flap is fixed → dispatch `portfolio-resolver-investigator` with the `reason`. `broker_aware_disabled` / Schwab unprovisioned is the expected inert state (report, don't alert). **Never RED** (holdings are a decision input; the decision core is untouched).
6q. Operator approval packet (always when `operator_approval_packet.json` present; else `"Approval-packet: inert (not run)"`): `"Approval-packet: {counts.tier_sim_within_veto} sim awaiting veto · {counts.tier_production_pending} production pending{ · stale: <csv of stale_pending reasons> if any}{ · GATE DRIFT: <csv of packet_gate_drift reasons> if any}"` — the one-shot two-tier packet (added 2026-07-15; ships GATED `sim_governance.approval_packet.enabled=false`) consumed by the evening email + GUI approval page. `tier_production_pending` is the count an operator still needs to act on via the human-gated `promotion_approvals.record_approval` path. AMBER on `assess_packet_health` AMBER (`stale_pending:*` or `packet_missing_or_unreadable` while activated) → dispatch `portfolio-learning-loop-health`. **RED** on `assess_packet_health` RED (`packet_gate_drift:*` — a decided item with no matching approval record; contract breach) → dispatch `portfolio-learning-loop-health` immediately. Absent/disabled is the expected inert steady state pre-activation (report, don't alert). Source: `outputs/promotion_review/operator_approval_packet.json` via `assess_packet_health`.
6r. Memo-datasets (always when `memo_datasets.json` present; else `"Memo-datasets: not run"`): `"Memo-datasets: {len(domains)} domains · {ok_count} ok/{unavailable_count} unavailable"` — pure read-only heartbeat over the per-domain memo reassembly (Stage 10c, added 2026-07-20; **observe-only, no dispatch/mutation, never feeds `decision_plan.json`**). This line is diagnostic-only — it never triggers an agent dispatch on its own. **content_liveness note (warn, not RED)**: artifact fresh (generated today) but ALL domains read `unavailable` while their underlying source artifacts are themselves present/fresh is a looks-fresh-but-empty reassembly failure — report it as a liveness warning (fold into item 5's `content_warn_count` line) rather than escalating. Absent artifact is the expected inert pre-run state (report, don't alert). Source: `outputs/latest/memo_datasets.json`.
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
