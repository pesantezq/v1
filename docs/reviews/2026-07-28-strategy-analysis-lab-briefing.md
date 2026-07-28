# Strategy & Analysis Lab — External Review Briefing

**System:** advisory-only portfolio automation. Produces analysis, recommendations and
operator artifacts. **It does not execute trades and has no broker order path.**
**Date of snapshot:** 2026-07-28 19:15 UTC. All figures below were read from live
artifacts, not from documentation.

**What I want from you:** (1) validate that the system is genuinely working and
running, not merely producing files; (2) suggest improvements. Specific questions
are listed at the end. Please challenge the numbers — several of today's most
important findings were things that looked healthy and were not.

---

## 1. Two-lane architecture (the core governance idea)

| Lane | Status | Rule |
|---|---|---|
| **Simulation / sandbox** | ACTIVE | May freely change SANDBOX/SIMULATION outputs. Never touches production. |
| **Production** | PROTECTED, human-gated | Changes only via a human-approved promotion proposal. AI can *recommend* production readiness but can never approve it. |

Enforced invariant: `is_human_approver()` rejects every AI marker. An AI-authored
approval record is refused at write time and ignored at read time.

Decision source of truth is `outputs/latest/decision_plan.json`. GUI, memo and
explanation layers are **artifact consumers only** — they never recompute decisions.

---

## 2. Strategy Lab — current state (verified)

Deterministic health assessor `strategy_lab_health.assess_strategy_lab_health()`
returns:

```
status: GREEN
reason: "lab healthy: ran, populated, documented, no failing-OOS tactic surfaced"
tactic_count           : 26
coverage_complete      : true      (every tactic has a documented rationale)
age_hours              : 9.5
walk_forward_present   : true
failing_oos            : []        (no tactic surfaced as failing out-of-sample)
factor_data_available  : true
active_strategy_id     : defensive_capital_preservation
strategy_decisions_count: 4
top_tactic             : Volatility-Managed  (strategy_score 1.7474)
top_excess_vs_spy      : 0.5669
```

Leaderboard (26 entries, `outputs/sandbox/strategy_leaderboard.json`). Each row
carries `tactic_id, name, source, academic_basis, strategy_score,
mean_excess_vs_spy, prob_beat_spy, worst_max_drawdown, by_window, overfit,
still_works_oos, tax_note`. Top 5 by `strategy_score`:

| # | tactic | score | source | academic basis |
|---|---|---|---|---|
| 1 | Volatility-Managed | 1.7474 | strategy_profile | Moreira & Muir (2017) — reduce risk when vol is high |
| 2 | Black-Litterman Blend | 1.7132 | strategy_profile | Black-Litterman / Idzorek — market prior + confidence-weighted views |
| 3 | Boom Bucket | 1.7092 | shadow | — |
| 4 | Lower Risk | 1.6693 | shadow | — |
| 5 | Balanced Core-Satellite | 1.6498 | strategy_profile | — |

Artifacts: `strategy_leaderboard.json`, `strategy_catalog.json` (1.85 MB),
`portfolio_backtest.json` (1.60 MB), `portfolio_projection.json`,
`walk_forward_results.json`, `factor_attribution_summary.md`,
`strategy_comparison.json` (8 profiles).

**Governance:** the lab is sandbox-only. A human approving a Strategy-Lab result
re-anchors the **sandbox** active strategy only; it never writes
`decision_plan.json`. Every tactic must ship a catalog entry with a rationale for
each tunable parameter — a tactic with an empty rationale flips
`coverage_complete` false and must not surface in the Lab.

---

## 3. Analysis Lab — the four-lens review system

Every feature must be paired with a health check at its runtime cadence, or it is
considered incomplete. Four lenses: **developer** (cron health, silent zeros,
dependency drift), **quant** (hit-rate, Sharpe, regime performance, pattern
efficacy), **process analyst** (workflow health, audit activity, drift caps,
operator queue), **market expert** (sector rotation, regime calls, memo-vs-reality).

Cadence tiers, each a skill that reads artifacts, triages GREEN/AMBER/RED, and
threshold-dispatches specialist agents:

| Tier | Skill | Cron |
|---|---|---|
| Daily | `daily-tool-analysis` (+ `daily-system-improvement`) | 09:15 weekdays |
| Weekly | `run-all-weekly` → doc-audit, strategy-lab, strategy-catalog, weekly-ETF | Mon 08:00 |
| Monthly | `monthly-tool-analysis`, `pattern-loop-analysis` | 1st 09:30 / 10:15 |
| Yearly | `yearly-tool-analysis` | Jan 1 10:00 |
| On demand | `quant-watch-analysis`, `strategy-lab-analysis`, `run-all-daily` | — |

Specialist agents dispatched on threshold: `portfolio-attribution-analyst`,
`portfolio-learning-loop-health`, `portfolio-discovery-health`,
`portfolio-resolver-investigator`, `portfolio-memo-reviewer`,
`portfolio-render-reviewer`, `portfolio-backtest-health`, `portfolio-test-reviewer`.

**Quant-watch probe ledger** — a self-managing tracker for *sub-RED* quant
concerns, so issues too small to alarm still persist across runs instead of being
forgotten. Currently AMBER, 2 active, 0 escalated:

- `manual:financial_services_sector_drag_5687885c` (13d) — the "sector drag" was
  localised to the crypto-miner subset (RIOT/MARA), not the sector; XLF and COIN
  are healthy. Non-gauge, static-universe hygiene issue.
- `manual:regime_coverage_gap_5687885c` (0d) — the current gauge's +9.0pp edge
  (p≈0.003) is demonstrated ~91% under the `neutral` regime; risk_off is unproven.

**Pattern-Improvement Loop** — `backtest_health` returns GREEN, no flags:
`evaluated 3318`, OOS window matured (`folds_possible: true`), 2 proposals,
`auto_apply last_status: gpt_vetoed`, look-ahead audit clean (389 dates, 0
mismatches), monthly recompute fresh (exit 0), 5 distinct regime labels.

---

## 4. What is actually running (crontab, verified)

```
09:00 daily    run_daily_safe.sh          main pipeline
09:15 weekdays daily_check.sh             daily analysis tier
09:45 daily    run_sims_daily.sh          simulation / strategy lab
11,15,19,23    discovery_pulse.sh         off-hours discovery (weekdays)
12,20          discovery_pulse.sh         weekends
Mon 07:00      run_doc_audit.sh
Mon 08:00      run_weekly_safe.sh
Mon 08:30      run_weekly_etf_bundles.sh
1st 09:30      monthly_check.sh
1st 09:50      pattern_loop_reconstruct.sh
1st 10:15      pattern_loop_check.sh
Jan1 10:00     yearly_check.sh
```

---

## 5. Gate flags — what is live right now

| Flag | Value | Meaning |
|---|---|---|
| `backtesting.auto_apply.enabled` | **true** | Registry `default_weight` auto-apply is ARMED post-OOS-maturity. Its one run was `gpt_vetoed` (nothing applied). |
| `sim_governance.enabled` | true | Two-lane governance lane active |
| `auto_approval.enabled` | true | GPT auto-approval, **simulation only** |
| `auto_approval.watchlist_enabled` | true | separate sim watchlist DB, cap 2/day |
| `auto_approval.strategy_enabled` | **true** | sandbox active strategy, cap 1/day |
| `auto_approval.live_watchlist_enabled` | **false** | production watchlist untouched |
| `auto_approval.evening_digest.enabled` | **true** | 18:00 America/New_York |
| `approval_packet.enabled` | true | two-tier operator packet |
| `production_application.apply_watchlist_overlay` | true | live |
| `production_application.apply_advisory_overlay` | true | live |
| `portfolio.broker_aware.enabled` | true | Schwab read-only holdings |

Auto-approval bounds: `min_confidence 0.85`, `veto_window_hours 48`,
`max_active_awaiting_veto 5`. Kill switches: `config/auto_approval.DISABLED` file
or `STOCKBOT_AUTO_APPROVAL_DISABLED=1`; equivalently
`config/auto_apply.DISABLED` / `STOCKBOT_AUTO_APPLY_DISABLED=1`.

---

## 6. Work completed today (context for review)

Three trust boundaries were repaired. Each failure was **invisible to the checks
designed to catch it**, because those checks confirmed the thing was *running*,
not that it was *right*.

**(a) Presentation trust.** The mobile memo view keyed its section map on header
names the memo stopped emitting after a redesign. 21 lines — the entire capital
plan, deferrals and bottom line — were silently dropped from `/dashboard/memo`.
Invisible because the panel still rendered, filled with unrelated price lines.
Every test passed against a stale fixture. Fixed, plus a guard test pinning every
shipped header to a section.

**(b) State trust.** Two pre-existing defects in the promotion pipeline:
- *Re-approval treadmill*: `candidate_id` is stable by design (salted by the
  underlying fact) but `proposal_id` is clock-salted, and approvals keyed only on
  the churning one — so an unchanged fact needed re-approval every run. Symptom:
  43 approvals recorded against an empty overlay.
- *Durable state rebuilt from an ephemeral flow*: both overlays were rebuilt from
  today's pending set, so an applied op vanished once its candidate stopped being
  proposed.

Fix: durability is now a property of the **proposal type**, not the workflow.
Membership decisions (watchlist add/remove/rank/tag) persist until explicitly
revoked; state-derived labels (flock candidate-logic, all advisory types) keep
refreshing, because persisting a stale label would mislead. Reversal is explicit,
human-gated, and appended to an append-only ledger.

**(c) Statistical trust.** A watchlist-removal feature was built, fully tested,
zero regressions — and **stopped before production** because its statistics were
invalid. See §7.

Adversarial probing of four trust-boundary scenarios found one further fail-open:
an unreadable *revocation* ledger silently resurrected a revoked production op.
The same `except: return set()` idiom fails **safe** on a permission list (no
approvals ⇒ nothing applies) and **dangerous** on a denial list (no revocations ⇒
nothing is blocked). Now fail-closed, distinguishing a torn trailing line
(tolerated, costs one record) from total corruption (refuse to rebuild).

Result: **9,048 tests passing, zero regressions**, three merges to `main`.

---

## 7. The removal feature that was stopped (please scrutinise this)

A gate was built to propose removing decayed watchlist members:

```
recent_resolved_1d >= 30
AND recent_hit_rate_1d < 0.40      (decayed accuracy)
AND recent_mean_return_1d < 0      (negative expectancy)
```

It passed every test with zero regressions. It is **not merged**, because the
statistics do not support a production decision:

**Outlier influence.** RIOT's expectancy leg flips *positive* (+0.192) if its
single worst observation (−15.01) is dropped. SMCI's positive mean (+0.139) — the
only reason the gate spared it — rests on one +28.24 winner; its median is −1.510.

**Effective sample size (the larger problem).** The 33 observations span 29
distinct dates and only **5 ISO weeks**. Week-clustered:

| symbol | weekly means | mean of weeks | raw daily mean |
|---|---|---|---|
| RIOT | −2.14 −2.24 −2.08 −1.87 +5.36 | −0.596 | −0.268 |
| TSLA | +4.15 +3.50 −0.57 −1.05 −4.76 | **+0.253** | −0.574 |
| NASA | +5.19 +3.20 −4.00 −2.38 −0.08 | **+0.386** | −0.633 |
| SMCI | −2.12 −2.18 +0.43 −3.34 +5.93 | **−0.257** | +0.139 |

No symbol has a stable sign across weeks. The gate was wrong on **both** live
candidates in opposite directions: it would have removed TSLA (net *positive* by
weeks) and spared SMCI (net *negative*). At n_eff ≈ 5 no confidence interval
excludes zero, so the correct verdict for every candidate is `DATA_INSUFFICIENT`.

**Agreed replacement rule** (not yet implemented). `REMOVE_CANDIDATE` only when
all hold:
1. **Data admissible** — `data_mode=="live"`, `degraded_mode==0`, acceptable
   `regime_data_quality`, artifact fresh vs the latest completed market session.
2. **Evidence sufficient** — minimum qualified outcomes AND minimum distinct
   prediction dates/weeks, with overlapping forward-return windows handled
   (non-overlapping observations, block bootstrap, week clustering, or an explicit
   effective-sample-size adjustment).
3. **Robust expectancy negative** — fixed 10% trimmed or winsorized mean, against
   a *meaningful negative threshold*, not merely `< 0`.
4. **Uncertainty supports removal** — one-sided bootstrap CI upper bound `< 0`.
5. **No single observation controls it** — max leave-one-out robust expectancy `< 0`.

Raw mean, median, worst outcome and hit rate remain **visible diagnostics, never
gates**. The binary fire/don't-fire is replaced by
`KEEP / WATCH / REMOVE_CANDIDATE / DATA_BLOCKED`, so stale or poor-quality
evidence can never be silently read as support for KEEP.

Note deliberately recorded: mean-and-median sign agreement is an *interim
fail-closed guard only*, not the permanent rule — a negative median does not imply
negative expectancy, since positively skewed momentum/breakout strategies
legitimately show many small losses and a few large winners.

---

## 8. Known-open items (nothing hidden)

1. **`discovery_candidate_promotion` durability unresolved.** Shaped like a
   membership add but excluded from the durable set. Inert today (no producer),
   but needs an explicit yes/no before any producer ships.
2. **Durable producers are already live.** `watchlist_add` and
   `watchlist_rank_change` candidates are emitted today; the lane is
   producer-live and only application-inert. The first human approval of either
   creates the first durable production op.
3. **`record_approval` still does whole-document read-modify-write.** No lock/CAS.
   Contained (it now refuses to write while the log is unreadable) but the
   approvals log is not append-only while the revocations ledger is — the weaker
   link is load-bearing.
4. **Audit log re-appends per surviving op per run**, so row count measures
   *days survived*, not *applications*. The health check is explicitly documented
   not to count rows; unbounded growth remains.
5. **Two dead simulation-lane experiments.** `experiment_watchlist_discovery_adds`
   and `experiment_watchlist_rerank` return 0 candidates because their baseline
   inputs (`discovery_candidates`, `watchlist_ranked`) are never populated.
6. **`build_top100_daily(lookback_days=1)`** zeroes the recent-hit-rate weight, so
   all tickers tie and the daily artifact cannot discriminate. The removal gate
   therefore reads the *monthly* artifact — which refreshes weekly while the gate
   would run daily (up to 7-day-old evidence, no freshness guard).
7. **Regime coverage gap.** The current gauge's edge is ~91% neutral-regime;
   risk_off performance is unproven.
8. **Static universe bypasses every quality screen.** `min_mkt_cap $5B`,
   `min_rev_growth`, `trend_filter_200dma` govern only the FMP candidate path.
   MARA's market cap is $4.62B — below the system's own floor — and would be
   rejected today if it had to earn its seat.

---

## 9. Questions for you

**Validation**
1. Is `strategy_score` (top tactic 1.7474) a defensible composite? What would you
   want to see decomposed before trusting a leaderboard ranking of 26 tactics?
2. `still_works_oos` and `overfit` are per-tactic fields. What specific evidence
   would make you believe an OOS claim on a 26-tactic lab — and how would you
   detect leaderboard-level multiple-comparisons/selection bias across 26 tactics?
3. `coverage_complete: true` means every tactic has a rationale. Is
   documentation-coverage a meaningful health gate, or does it risk
   confusing prose completeness with statistical validity?
4. The `active_strategy` is `defensive_capital_preservation` while the top-ranked
   tactic is `Volatility-Managed`. Should a lab's top rank drive the active
   strategy, or is that divergence correct?

**Improvement**
5. Critique the §7 replacement rule. Is the 5-condition conjunction right? Is a
   10% trimmed mean the correct robust estimator here, or would you prefer a
   different one, and what "meaningful negative threshold" would you set?
6. For overlapping forward-return windows: block bootstrap vs week clustering vs
   an explicit n_eff adjustment — which, and why, at this sample size?
7. Item 6 in §8: the gate would read weekly-refreshed evidence on a daily
   cadence. What freshness contract would you require?
8. The quant-watch ledger tracks sub-RED concerns so they persist rather than
   being forgotten. Is this the right mechanism, and what concern classes are
   missing from a 4-lens review system?
9. Where is this system most likely to be *confidently wrong* — a metric that
   looks healthy while measuring the wrong thing? Today's three failures were all
   of that shape, so I expect more.
