---
description: Daily Portfolio Automation health check. Reads today's run artifacts, triages GREEN/AMBER/RED, threshold-dispatches the resolver/attribution/render agents, emits a one-line heartbeat plus structured body. Designed to run via /schedule at 09:15 UTC daily, 14 minutes after the production cron.
---

# Daily Portfolio Automation Health Check

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
  "thresholds_crossed": []
}
```

`thresholds_crossed` is a subset of `["n_10", "n_30", "n_50", "n_100"]`.

**Read artifacts** (degrade gracefully on any miss):

1. `outputs/latest/daily_run_status.json` → overall_status, stage_summary, required_missing_count
2. `outputs/latest/daily_memo.md` (first 50 lines)
3. `outputs/latest/retune_impact.json` → outcome_attribution.by_fingerprint
4. `outputs/latest/risk_delta.json` → overall_status, concentration.top_position, leverage.total_exposure
5. `outputs/latest/fmp_budget_status.json` → budget.status, news.article_count_raw
6. `outputs/latest/decisions_due_for_resolution.json` → stuck_count, by_ticker (top 3)
7. `data/gauge_versions.jsonl` tail line → current_fingerprint + first_seen_at

**Compute**:

- `fingerprint_age_days` = days since current `first_seen_at`
- `fingerprint_changed` = state.last_fingerprint != current_fingerprint
- `current_fp_resolved_1d`, `pre_tracker_resolved_1d`
- `delta_hit_rate_pp` = (current_fp.hit_rate_1d − pre_tracker.hit_rate_1d) × 100
- `newly_crossed_thresholds` = thresholds in `{10, 30, 50, 100}` that `current_fp_resolved_1d` reached today but were not in `state.thresholds_crossed`

---

## Step 2 — Triage

**GREEN** when all of:
- `overall_status == "ok"`
- `required_missing_count == 0`
- `stuck_count == 0`
- `budget.status ∈ {ok, near_cap}`
- `risk_delta.overall_status ∈ {ok, near_cap}`
- no unexpected fingerprint change
- attribution presence consistent with fingerprint age (n=0 only acceptable when age <2 days)

**AMBER** when GREEN fails on non-urgent advisory:
- `budget.status == "near_cap"`
- `risk_delta.overall_status == "near_cap"`
- attribution lag (age ≥2d AND n=0) — known cron-timing issue

**RED** when any of:
- `overall_status ∈ {"failed", "partial"}`
- `stuck_count > 0`
- `budget.status == "exhausted"`
- `risk_delta.overall_status == "breach"`
- `|delta_hit_rate_pp| ≥ 10` AND `current_fp_resolved_1d ≥ 30`

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

---

## Step 4 — Output (daily heartbeat — emit every run)

**Lead line, always**:

`[GREEN|AMBER|RED] daily check YYYY-MM-DD: <one-line headline>`

Headline grammar:
- GREEN: `"17 stages OK · retune n={N} at {H}% (Δ {sign}{pp}pp vs baseline) · FMP {used}/{cap}"`
- AMBER: `"WARN — {primary anomaly}; others nominal"`
- RED: `"ALERT — {primary fault}; action: {from RED template library}"`

**Body, under 200 words**:

1. Attribution snapshot (always): `"Attribution: current-fp n={N} at {H}% / pre-tracker n={N} at {H}% · Δ {sign}{pp}pp"`
2. Risk-delta state (always): `"Risk: {top_symbol} {weight}% (cap {cap}%, +{headroom}pp); leverage {L}%"`
3. Agent dispatch results (only if any fired) — one line per agent with its key finding quoted
4. For RED only: named action from the template library below
5. For GREEN: `"No action required."`

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

---

## Step 5 — Write state back

Update `data/daily_check_state.json`:
- `last_run_at` = today's iso timestamp
- `last_fingerprint` = current_fingerprint
- `last_current_fp_resolved_1d` = today's value
- `last_pre_tracker_hit_rate_1d` = today's value
- Append `newly_crossed_thresholds` to `thresholds_crossed`
- Reset `thresholds_crossed` to `[]` if `fingerprint_changed`

---

## Failure modes

- All `outputs/latest/` artifacts missing → RED, headline reason: "cron did not run today"
- Artifacts present but all mtime > 24h → AMBER, headline reason: "today's cron did not refresh artifacts"
- Agent dispatch raises → log error to summary body, continue with manual triage on remaining data, do not abort the summary
- `data/daily_check_state.json` corrupt → reset to defaults; one-time AMBER with body note `"State file reset; threshold crossing detection unreliable for one run"`
