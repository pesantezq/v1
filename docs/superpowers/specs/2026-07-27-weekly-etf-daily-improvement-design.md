# Weekly ETF Bundles — Daily Continuous-Improvement (Split Cadence) — Design

**Date:** 2026-07-27
**Status:** Review changes requested (revised 2026-07-27 per operator amendments 1–12)
**Scope:** Additive, observe-only. No change to `decision_engine.py`, allocation logic, or
core score semantics. A human approval may change only the **active weekly ETF sandbox
variant** (`feeds_decision_engine=false` throughout) — a *human-approved sandbox champion
activation*, **not** a production mutation.

## 1. Context

The weekly ETF bundle subsystem (`portfolio_automation/weekly_etf_bundles/`) was merged
(`7a36dc37`) and **activated** 2026-07-27: 23 archives refreshed, first immutable
prediction frozen (`predictions/2026-07-27.json`, 23 rows), `WEEKLY_ETF_BUNDLES_ENABLED=1`,
Monday 08:30 UTC cron, email dry-run. Invariants: `observe_only`, `simulation_active`,
`production_gated`, `feeds_decision_engine=false`.

Architecturally **weekly**: predictions frozen once/week, immutable, keyed on
`market_data_date`, conflict-guarded. Outcomes mature at 1/4/12/26-week horizons. The
champion is currently a **hard-coded** constant (`strat_lab_adapter.CHAMPION_ID =
"weekly_etf_bundle_v1_baseline"`) — which cannot support an applied swap (amendment 3).

## 2. Goal

Improve continuously without breaking weekly immutability: run the non-freezing learning
steps daily, and route a champion change through a **durable, human-gated, anti-overfit,
idempotently-reconciled** lifecycle that affects **future weekly freezes only**.

## 3. Architecture — split cadence

| Cadence | Runner | Does | Freezes? | Writes |
|---|---|---|---|---|
| **Weekly** (Mon 08:30) | `run_weekly_etf_bundles.sh` (unchanged) | full run incl. freeze + digest; **reads active champion from `champion_state.json`** | ✅ once/week | weekly artifact set |
| **Daily** (new) | `run_daily_safe.sh` via `run_aux_stage` | mature → evaluate → Strat Lab OOS compare → health → **evidence → proposal** | ❌ never | daily subtree only |

## 4. Champion source of truth (amendment 3)

`outputs/weekly_etf_bundles/champion_state.json` — versioned, **CAS-protected**:

```
{ "schema":"weekly_etf_champion_state.v1", "version":<int>,
  "active_champion_id":"weekly_etf_bundle_v1_baseline",
  "champion_config_hash":"…", "effective_from_market_date":"<YYYY-MM-DD>",
  "updated_at":"…", "history":[ {from,to,proposal_id,approval_ref,applied_at,
     before_hash,after_hash,state_version_before,state_version_after,code_sha} ] }
```

- The **weekly freeze reads `active_champion_id` from here** (falls back to the hard-coded
  baseline if the file is absent — backward compatible).
- A swap bumps `version` via compare-and-swap and sets `effective_from_market_date` to the
  **next** weekly freeze date. **Never rewrites or reinterprets historical predictions**;
  frozen files stay immutable. Swaps affect future freezes only.

## 5. Proposal lifecycle (amendments 1, 2, 7)

`record_approval` **records a decision; it is not the proposal-emission mechanism.** The
lifecycle is a distinct chain:

```
Strat Lab evidence  ──gates──▶  informational-only (insufficient sample) [never queued]
        │ all gates pass
        ▼
  emit durable PromotionProposal (type: weekly_etf_champion_change, stable proposal_id)
        │ human decision via promotion_approvals.record_approval(proposal_id, …)
        ▼
  approved_unapplied ──▶ idempotent apply/rollback reconciler ──▶ applied / rolled_back
```

- **Informational tier:** "promising but insufficient sample" (or any failed gate) is
  written to the daily evidence/health artifacts only and **never enters the approval
  queue**.
- **New proposal type `weekly_etf_champion_change`** (amendment 2) with its **own routing +
  application contract** — it does **not** fall through to the sim-gov advisory or watchlist
  workflow. It is applied by the ETF reconciler (§8), not by the sim-gov production overlay.
- **Stable `proposal_id`** (amendment 7) = deterministic hash of
  `(active_champion_id, challenger_variant_id, evidence_fingerprint)`, where
  `evidence_fingerprint` = hash of the set of matured **cohort** identifiers used.
  **Never** derived from `generated_at` or the daily run date.
- **Dedup / one-active / cooldown / hysteresis:** at most **one** active
  `weekly_etf_champion_change` proposal at a time; re-emitting the same `proposal_id` is a
  no-op (idempotent); a decided (approved/rejected/vetoed) `evidence_fingerprint` is **not
  re-proposed until new matured evidence exists**; after any apply/reject a **cooldown** of
  `cooldown_cohorts` newly-matured cohorts applies; a **hysteresis** band requires the
  challenger to exceed the base threshold by an extra margin to avoid flip-flop.

## 6. Timezone guardrail — **Phase 1** (amendment 5)

Resolve the as-of trading session against the **latest *completed* trading session** using
canonical `America/New_York` (via `zoneinfo`), **not** a UTC-naive `now()` nor merely "the
Eastern calendar date". Explicitly handle: premarket (session not yet complete → use prior
session), post-close, exchange holidays, weekends, DST transitions, the UTC-midnight
boundary, and the **incomplete current bar** (never freeze/evaluate on a partial day). A
same-market-date daily run must be provably unable to mutate a frozen prediction
(content-hash stable — tested).

## 7. Statistical unit & anti-overfitting (amendments 6, 8)

**Unit = the weekly cohort** (all predictions frozen on one `market_data_date`), not the
individual ETF row. Record **both** `prediction_row_count` and `independent_cohort_count`.

- **Correlated same-week ETFs:** the ~23 ETFs in one cohort share market beta — treat the
  cohort as one clustered observation (block/cluster by cohort), never as 23 independent
  samples.
- **Overlapping horizons:** 1/4/12/26-week windows overlap in calendar time — use
  non-overlapping cohorts or a variance adjustment (block bootstrap / Newey-West) so serial
  correlation doesn't inflate significance.
- **Holm correction across challengers** after the per-challenger test.

A challenger is *proposed* only if **all** gates pass (else informational-only):
1. **Min matured sample** — ≥ `min_matured_cohorts` distinct matured cohorts per evaluated
   horizon (config; e.g. 8 cohorts), never 0/near-0.
2. **Out-of-sample / walk-forward** — measured on walk-forward folds, not the selection window.
3. **Sustained margin (K)** (amendment 6) — beats champion by ≥ `min_margin` over **K
   distinct newly-matured weekly cohorts / unique evaluation-data fingerprints**. Re-running
   the *same* dataset on consecutive days does **not** advance the streak.
4. **Multiple-comparison correction** — Holm/Bonferroni across the variant set (best-of-4 is
   selection-biased); record raw + corrected significance.
5. **Economic + statistical significance** — clears both a min effect size and the corrected
   bar; a Sharpe-style haircut/deflation is recorded.

All thresholds live in `config/weekly_etf_bundles.yaml` with recorded rationale (Strategy
Documentation Requirement).

## 8. Apply / rollback state machine (amendment 9)

Durable, **exactly-once** reconciler. Idempotency key = `proposal_id` + target
`champion_state.version`.

States: `pending` → `approved_unapplied` → `applied`; and `applied` → `rollback_pending` →
`rolled_back`; plus `rollback_conflict` (target moved under us — CAS fail, never overwrite
newer state) and `failure`. Re-running the reconciler in any state is safe (idempotent).

Append-only audit `outputs/weekly_etf_bundles/champion_swap_audit.jsonl`; every entry
carries: `before_hash`, `after_hash`, `state_version_before`, `state_version_after`,
`proposal_id`, `approval_ref`, `champion_config_hash`, `code_sha` (`git rev-parse HEAD`),
`effective_from_market_date`, `timestamp`, `actor`. No apply without a captured before-state.

## 9. Daily-lane write allowlist (amendment 4)

Daily lane writes **only** to a separate subtree `outputs/weekly_etf_bundles/daily/`
(evidence, daily-health, proposal artifacts) plus the two governance files
`champion_state.json` (CAS, apply-time only) and `champion_swap_audit.jsonl`. **Explicit
deny:** it must **never** write/overwrite weekly `latest.json`, the weekly digest
(MD/HTML), weekly `health.json`, any email artifact or email-dedup state, or any
`predictions/**` file. Enforced by a write-path allowlist + a test asserting weekly
artifacts' mtimes/hashes are unchanged by a daily run.

## 10. Shell integration (amendment 10)

Daily lane is added to `run_daily_safe.sh` via the existing **`run_aux_stage`**
non-blocking helper (never affects the daily exit code). Gated by
`WEEKLY_ETF_BUNDLES_ENABLED` **and** `WEEKLY_ETF_BUNDLES_DAILY_ENABLED` (default 0 — ships
inert). **Shared-lock precedence:** the weekly and daily ETF runners share a lock; the
**weekly freeze takes precedence** — the daily lane must skip (not block/queue) if the
weekly runner holds the lock, and must never run concurrently with a freeze.

## 11. Proposal evidence schema (amendment 12)

Each `weekly_etf_champion_change` proposal embeds: `current_champion` (id + config hash),
`challenger` (variant id + config hash), `evaluation_window` {start,end}, `matured_prediction_count`,
`independent_cohort_count`, `metric_deltas` (hit-rate, mean-return, per horizon),
`risk_drawdown_comparison`, `significance` {raw_p, corrected_p, method}, `gate_results` (all
5 gates: pass/fail + values), `artifact_references`, `rationale`, `risks`, `config_hashes`
{champion, challenger}, and `rollback_target` {champion_id, champion_state.version}.

## 12. Analysis + Health coverage (mandatory pairing)

Daily cadence → extend `.claude/commands/daily-tool-analysis.md`: read the daily-subtree
artifacts (daily-health, evidence, proposal); signals `weekly_etf_daily_ran`,
`champion_swap_pending` (a proposal awaiting human approval → **actionable AMBER**),
content_liveness (`status==ok` but 0 tickers scored); **RED only** on an invariant breach
(`feeds_decision_engine` flips true) or an `applied`-without-approval / `rollback_conflict`.
`/weekly-etf-analysis` remains the deep readout. Tests assert healthy vs degraded states.

## 13. Error handling
Non-blocking daily stage; **fail-closed** proposer (no proposal on missing/short data,
invariant doubt, CAS/lock contention, or failed rollback-precondition); circuit-breaker on
repeated apply/rollback failure.

## 14. Testing
- Daily-observe: matures + evaluates, does **not** freeze; weekly artifacts unchanged
  (mtime + hash) — write-allowlist enforced.
- Timezone: latest-*completed*-session resolution across premarket / post-close / holiday /
  weekend / DST / UTC-boundary / partial-bar.
- Anti-overfit: each of the 5 gates blocks independently; cohort (not row) counting; K
  streak does not advance on a re-run of the same fingerprint; Holm across challengers.
- Proposal: stable id (independent of run date); dedup/one-active/cooldown/hysteresis;
  insufficient-sample stays informational (never queued).
- State machine: exactly-once apply; rollback restores before-state; `rollback_conflict` on
  a moved target; audit entry carries all required fields.
- champion_state CAS: concurrent bump → one wins, other yields conflict; weekly freeze reads
  active champion; historical predictions never reinterpreted.
- Health/analysis: `champion_swap_pending` AMBER; invariant-breach/applied-without-approval RED.

## 15. Phasing
1. **Phase 1** — timezone guardrail (§6); daily-observe mode (no freeze); daily
   `run_aux_stage` stage + lock precedence (inert sub-flag); daily write-allowlist +
   subtree; health/analysis coverage; tests.
2. **Phase 2** — `champion_state.json` (CAS) + weekly freeze reads it; evidence →
   `weekly_etf_champion_change` proposal with the §7 statistical unit + 5 anti-overfit gates
   + §5 dedup/cooldown/hysteresis + §11 evidence schema (proposal only; inert until data matures).
3. **Phase 3** — human-gated apply/rollback reconciler (§8 state machine, exactly-once,
   audit, CAS/veto).
Each phase is independently shippable and observe-only until its gate is flipped.

## 16. Boundaries (amendment 11)
No changes to the daily decision engine, allocation logic, or core score semantics. A human
approval changes **only** the active weekly ETF **sandbox** variant, which remains
`feeds_decision_engine=false` — a *human-approved sandbox champion activation*, not a
production mutation. All writes stay in the `WEEKLY_ETF_BUNDLES` namespace.
