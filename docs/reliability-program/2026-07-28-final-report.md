# Reliability & Statistical-Validity Program — Final Report

Snapshot 2026-07-28. Advisory-only system; no broker order path exists or was added.

---

## 1. Current-state findings

Full detail with `file:line` in `FINDINGS-RUNNING.md` (271 lines) and six per-area
audits in this directory. The findings that mattered most, all confirmed by reading
code and — where marked — by running experiments against live artifacts.

### Confirmed by experiment

| # | Finding | Location |
|---|---|---|
| F11.1 | **A human approval can be silently lost.** Two concurrent approvals for *different* proposals collapse to one; both calls return `ok: True`; no error anywhere. | `promotion_approvals.py:143-168` |
| F10.1 | **The audit log lacked the fail-closed guard its two sibling logs have.** It is the sole source durable watchlist membership is reconstructed from. Corrupting it dropped `durably_live_count` 1→0 with `overlay_rebuild_skipped` still `False`. | `production_application.py:193-270` |
| F1.1 | **`strategy_score` was not reproducible from its own artifact.** `components` computed then discarded; reconstruction residuals 0.15–0.51 against a rank 1→5 gap of 0.0976. | `run_strategy_lab.py:226` |

### The false-GREEN family

| # | Finding | Location |
|---|---|---|
| F2.1 | `failing_oos` uses `is False`, which `None` never satisfies. 25/26 tactics are `null`. So "nothing was tested" and "everything passed" are the same value — and the assessor printed *"no failing-OOS tactic surfaced"* over GREEN. | `strategy_lab_health.py:121` |
| F4.1 | GREEN was a bare `else` fallback carrying no positive evidence; RED was gated on one hardcoded string, so any number of AMBER problems could never escalate. | `strategy_lab_health.py:77-151` |
| F4.2 | Tactics already carry an `overfit_unknown` flag. The assessor never read it. | — |
| F18.1 | `quant_watch_probes` fails to **GREEN** on exception; `artifact_registry` fails to **AMBER**. Two health producers disagreed on fail direction. | — |
| F8.1 | `coherent_run_ids()` — built and tested to catch stale-artifact mixing — is **never called by any production code**. The safeguard for this exact bug class existed, unwired. | `run_manifest.py` |

### Statistical validity

| # | Finding |
|---|---|
| F1.2 | **The lab rewarded absence of scrutiny.** `overfit=None → 0.0` (best case) on the largest penalty weight (0.8). Walk-forward runs for 1 of 26 tactics. The one validated tactic absorbed a real −1.6 penalty and ranks **last**; the #1 tactic outranks it *because* it was never tested. |
| F2.3 | In-sample mean excess **+212% collapses to +11.1% OOS** over 11 folds. No CI, no cost adjustment (`gross_until_cost_model`). Degradation never computed or surfaced. |
| F3.1 | **Zero** selection-bias control anywhere — no deflated Sharpe, SPA, PBO, rank stability, or any family-wise correction. |
| F3.2 | Effective independent trials ≈ **8–10, not 26** (8 `profile_*` are one parameterized family; 6 `shadow_*` are 2 baselines + 4 overlays). All 26 share one price panel. |
| F3.3 | Only 4 nested/overlapping window scalars persisted per tactic → a post-hoc correction is **not computable** without new instrumentation. |
| F14.1 | Regime concentration is **98.8% neutral** (2211/2238), 95.8% return-weighted. No assessor reads regime data into a validity verdict. The June collapse-guard **whitelists `neutral`** and structurally cannot catch this. |

### Operational

| # | Finding |
|---|---|
| F13.1 | The dead experiment is a **one-line key mismatch** — reads `.get("candidates")`, file says `"decisions"`. Git blame puts it in the function's original commit, written a month *after* the producer used `"decisions"`. It never worked; an aggregate `candidate_count` hid it. |
| F9.1 | The daily ranking's hit-rate term (20% weight) is **structurally zero-information** at a 1-day lookback: 0 of 27 rows resolve inside the window. 17/31 rows (55%) tie exactly, and the tiebreak collapses to **alphabetical** order. No health check detects zero-variance rankings. |
| F15.1 | **MARA fails two live filters** — market cap $4.49–4.62B vs a $5B floor, *and* price $11.77 below its $12.02 200-DMA. Static/fallback paths bypass every fundamental screen; the sim-governance overlay path has no fundamental screen at all. `watchlist_source` records *where* a symbol came from, never *which filters ran*. |
| F16.1 | Concerns **auto-close by age** (60 days) regardless of whether they were fixed. Only 1 of 3 detectors can ever escalate to RED. |
| F5.1 | A **ranking-triggered auto-anchor path for the active strategy exists and is config-enabled**, inert only because the daily collector hardcodes watchlist-only candidates. The top-ranked tactic is also structurally unpromotable — the approval queue's 8 fixed profiles don't include it. |
| F12.1 | Audit log conflates "first applied" with "still alive": ~365 rows/op/year at ~732 bytes/row. 2 event types exist vs 8 needed. |

### Corrections to my own earlier statements

| Claim | Reality |
|---|---|
| "~91% neutral regime" | **98.8%** |
| "MARA fails on market cap" | fails on **two** filters |
| "pattern loop: `evaluated: 3318`, OOS matured" | `3318` is a **non-OOS full-history count**; the Strategy Lab and pattern loop have **separate** walk-forward implementations. Real OOS is `oos_n: 3042/161` elsewhere. |

---

## 2. Decisions made

Twelve decisions with rejected alternatives are recorded in
`SPEC-reliability-program.md`. The load-bearing ones:

- **Sequence is forced by a persistence dependency.** No correction or sensitivity
  analysis is computable from stored data, so: persist → measure → correct.
  *Rejected:* computing corrections from the leaderboard as-is — residuals exceed
  the rank gaps they would resolve.
- **Fix the claim before fixing the numbers.** Correcting the health verdict changes
  no score and no ranking; correcting `overfit` re-ranks 25 tactics and needs a gate
  plus a comparison period.
- **Never impute a missing component.** A tactic missing `overfit` is excluded from
  overfit-penalised ranking, not handed the best-case value. Ranking one measured
  tactic against 25 unmeasured ones is not a ranking.
- **Correct for ~8–10 effective families, not 26.** *Rejected:* Bonferroni over 26 —
  both too harsh (variants aren't independent tests) and misleading.
- **Defer the statistic, ship the instrumentation.** *Rejected:* White's Reality Check
  against 4 nested overlapping scalars — a confident number with no validity.
- **Lock now, append-only later.** An append-only approvals migration must also fix
  `dash_governance.py`, which reimplements the approval fold; leaving those two
  disagreeing about production authority is worse than the race being closed.
- **Week clustering primary, block bootstrap as diagnostic.** At ~5 effective weeks a
  stationary bootstrap's own parameters can't be estimated reliably.
- **Every GREEN dimension must carry non-empty evidence.** This single rule kills
  F2.1, F4.1, F4.2 and F18.1 together.

---

## 3. Changes implemented

| Phase | Change | Rollout class | State |
|---|---|---|---|
| A1 | `record_approval` serialized with `fcntl.flock` on a sidecar lock | production-active | ✅ `826d15d8` |
| A2 | Audit-log fail-closed guard, matching its two siblings | production-active | ✅ `bb0e8349` |
| C1 | Persist `score_decomposition` (raw + normalized + weights + contributions + missing-data markers + direction-of-goodness) | artifact-only | ✅ `14d889a5` |
| B1 | Explicit OOS states (`OOS_NOT_TESTED` … `OOS_DATA_BLOCKED`) + structured evidence; legacy boolean derived from state | artifact-only | ✅ `92176881` |
| B2 | Nine independent health dimensions + evidence-required fail-closed roll-up | gated, default ON | ✅ `92176881` |
| A3 | WS13 key mismatch + per-experiment input accounting | gated-off | ✅ `b013b24c` |
| A4 | Degenerate-ranking diagnostic + health signal | artifact-only | ✅ `f30433b1` |

Not yet implemented, specced and sequenced: C2 sensitivity, C3 per-fold
persistence, C4 `overfit` no-imputation (gated-off, re-ranks), C5 selection-bias
correction, D1 wire `coherent_run_ids()`, D2 session helper, D3 week-clustered
effective-n, D4 five-state removal model (shadow-mode), D5 staleness enforcement,
E1 divergence artifact + close the latent auto-anchor path, E2 admission
provenance, E3 concern taxonomy, E4 audit event types, E5 false-GREEN probe suite.

---

## 4. Test evidence

**Controller-verified independently, not taken on report:**

- **Approval concurrency, 8 threads at a barrier** (harder than the 2-thread repro):
  8 returned `ok: True`, **8 survived on disk**, all 8 resolve via
  `approved_proposal_ids`. Pre-fix, the 2-thread repro lost one.
- **Audit-log guard**: total corruption → `durably_live_count` stays **1**,
  `overlay_rebuild_skipped: True`, reason `wholly_corrupt: 0 of 1 line(s) parsed`.
  Torn tail → tolerated, `durably_live_count` 1, no refusal. All three authority
  logs now behave identically.
- **Score reproducibility**: residual max **0.0**, mean **0.0** across all 26
  tactics (was 0.15–0.51). Ranking byte-identical to the pre-change artifact.
  25/26 tactics have exactly one missing component — `overfit`.
- **Health roll-up on real state**: overall **AMBER** (was GREEN), blocking on
  `statistical_sufficiency: only 1/26 tactics have sufficient-fold OOS evidence`
  and a newly-surfaced `data_admissibility: missing_price_history: ES, ISRG, JPM, OS`.

**Agent-reported suite runs:** 126/126 `tests/portfolio_sim/` (WS1a);
306/306 governance/promotion/overlay across 20 files including 14 new tests in 2
new files (WS10/11). **Full suite on the integrated branch: 9 failed / 9114 passed / 1 skipped — the 9 are the documented pre-existing baseline, zero regressions, +66 tests.** Merged to `main` as `b2d00eca`.

---

## 5. Health before and after

| Surface | Before | After | Why |
|---|---|---|---|
| Strategy Lab | **GREEN** "no failing-OOS tactic surfaced" | **AMBER** | 25/26 tactics never OOS-tested; `failing_oos == []` meant untested, not passing |
| Strategy Lab data admissibility | not evaluated | **AMBER** | missing price history for ES, ISRG, JPM, OS — newly surfaced |
| `strategy_score` auditability | unverifiable (residual 0.15–0.51) | reproducible (residual 0.0) | components now persisted |
| Approval durability | silent loss possible under concurrency | no loss (8/8 verified) | flock serialization |
| Audit-log corruption | silent drop of durable ops | fail-closed with reason | third guard added |

The GREEN→AMBER move is the program working as specified, not a regression.

---

## 6. Remaining risks

1. **The latent auto-anchor path (F5.1) is still open.** Config-enabled; inert only
   by accident of the collector. Highest-priority remaining governance item.
2. **`overfit` still imputed to best-case** — the ranking is still inverted with
   respect to scrutiny until C4 ships gated.
3. **No selection-bias correction** — the top tactic's credibility over ~8–10
   effective families is unquantified.
4. **No market-session concept.** Freshness is wall-clock only; `coherent_run_ids()`
   remains unwired.
5. **Concerns still auto-close by age.**
6. **`dash_governance.py` duplicate approval fold** — blocks append-only migration
   and could disagree with `effective_approvals`.
7. **Regime concentration (98.8%) still feeds no validity verdict.**
8. **Snapshots directory unbounded** (108 files / 448K, no retention).

---

## 7. Operator impact

- **Strategy Lab health flips GREEN → AMBER** with explicit blocking reasons and
  `known_limitations`. Expected; do not tune it away.
- The leaderboard gains a `score_decomposition` per tactic. **Scores and order are
  unchanged.**
- Per-tactic OOS state replaces a bare boolean; the legacy field still resolves.
- Approvals are serialized — no operator-visible change except that a concurrent
  double-submit can no longer lose one.
- Memo, GUI, approval packet, and `decision_plan.json`: **unchanged.**

---

## 8. Production-safety statement

- No trading or broker order path was added. ✅
- No human gate was weakened. `is_human_approver` untouched; AI markers still
  rejected at write time and ignored at read time. ✅
- No AI production-approval path was created. `auto_approval`'s eligible types
  untouched. ✅
- No presentation consumer became a decision producer. ✅
- No production strategy, watchlist membership, allocation, or capital
  recommendation was changed. `decision_plan.json` untouched. ✅
- Strategy Lab remains sandbox-only. ✅
- No fail-closed behaviour was weakened; three were added or strengthened. ✅

---

## 9. Process note — a mistake worth recording

I dispatched parallel implementers into a **single shared working tree**. They raced
on branch state: one commit landed on the wrong branch (byte-identical duplicate,
safe to drop) and two workstreams' commits ended up on a third workstream's branch.
No work was lost and `main` was never touched, but it was avoidable — each agent
should have had its own `git worktree` from the start. One agent independently
diagnosed this and moved itself to a worktree, which is how its work stayed clean.
I also stashed files while an agent was still writing them; that recovered intact,
but it was careless. Parallel dispatch stopped; remaining work is serialized.

---

# Addendum — Phase D/E batch (2026-07-28, later session)

## Delivered

| Phase | Change | Rollout | Commit |
|---|---|---|---|
| E1 | Ranking-triggered auto-anchor closed by a structural fail-closed gate | production-active (guard only refuses) | `86150e7b` |
| E1 | Strategy-divergence artifact | artifact-only | `86150e7b` |
| D1 | `coherent_run_ids()` wired into the daily consumer path | validation-only | `3287b37d` |
| D2 | Shared `market_session` helper + session provenance fields | artifact-only | `2de39107` |
| D2 | `market_session` date/datetime coercion fix | — | `388dd153` |
| E5 | False-GREEN adversarial probe suite, 20/20 scenarios, 52 tests | validation-only | `6adc0bd5` |
| B4 | Regime concentration wired into validity claims | artifact-only + health | `f7f70f63` |
| E3 | Concerns stop closing by age alone | production-active | `88281d6c` |
| — | Memo header map completed + guard derived from producer via AST | production-active | `c8ff5d95` |
| — | Weight proposals gated on expectancy, not just hit-rate | production-active (tightening) | `c0fc3c6c` |

Merged to `main`: `02ced4fc` (E1 + D1/D2), 9170 passing, zero regressions.

## Three defects found DURING this phase, not by the audit

**1. My own memo fix was incomplete, and its guard shared the bug.**
`capital_plan_view.py` emits six headers; two ("Funded Market Opportunities" `:868`,
"Sell and Funding Dependencies" `:905`) sit inside conditional blocks and were absent
from the memo sampled when `8686898d` was written, so they mapped to `None` and were
silently dropped from `/dashboard/memo` whenever those blocks fired. The regression
guard added with that fix hardcoded a hand-observed header list, so it structurally
could not catch a header its author had not seen — a stale-fixture bug fixed by
writing a new stale fixture. Now AST-derived from the producer's `h("...")` call
sites, with a test proving the guard fails when a mapping is stripped.

**2. The weight proposer was expectancy-blind in an armed mutating path.**
`retune_suggestions._propose_weight_changes` gated `auto_applicable` on hit-rate
delta, sample count and significance; `mean_return` appeared nowhere in the file. A
tag right more often while losing money earned `auto_applicable: True` and a weight
INCREASE — the same accuracy-vs-expectancy confusion that invalidated the
watchlist-removal gate, in the one path that can mutate registry weights
(`backtesting.auto_apply.enabled` is `true`, 3 auto-applicable proposals live).
Now expectancy is always recorded and contradiction blocks auto-applicability;
unavailable expectancy blocks rather than imputing zero. Count unchanged 4→4 today
because all four tags carry positive expectancy (0.46–0.78) — forward-looking
protection, honestly reported as such.

**3. A new safety helper crashed on the natural calling pattern.**
`market_session.is_past_coverage_horizon()` raised `TypeError` on a `datetime`.
Because `datetime` subclasses `date`, such a call satisfies the annotation and every
static check, then fails at runtime — in the one function whose job is to warn a
caller they are past the hardcoded holiday horizon. Fixed with subclass-ordered
coercion and an explicit tz policy (naive treated as UTC).

## Sent back for correction (RESOLVED 2026-07-29 — see Addendum 2; two of the claims below are themselves wrong)

**B4 regime coverage is inert on the input it actually reads.** The implementation
report quoted 2265 resolved signals across three regimes with effective counts and
return-weighted shares, computed during development from `data/portfolio.db`. The
shipped entry point reads `outputs/regime/regime_performance.json`
(`regime_coverage.py:240`), which:
- omits `risk_off` entirely — the CSV source of truth has three labels among 2292
  resolved rows (neutral 2211 / 96.47%, risk_off 54 / 2.36%, high_volatility 27 /
  1.18%); the artifact carries two
- carries no per-regime count field, so `n` and `share` resolve to `None` and
  share-of-evidence is uncomputable, which is why `REGIME_CONCENTRATED` never fires
- therefore emits `RISK_OFF_UNPROVEN` **because `risk_off` is absent from the
  artifact**, not because its evidence was measured — a correct-looking verdict
  derived from missing data, which would silently become WRONG the moment the
  producer starts emitting `risk_off`

This is a subtler false-signal than anything the audit found: earlier cases asserted
validity from absence of *failure*; this asserts a specific plausible verdict from
absence of *data*. Requested: fail-closed `REGIME_DATA_INSUFFICIENT` when counts are
missing; fix the producer or point the assessor at a longer-window source; re-derive
the reported numbers from the shipped path.

## Corrected claim

Regime concentration was reported earlier in this document as 98.8% neutral from
`regime_performance.json`. The CSV source of truth gives **96.47% neutral, 2.36%
risk_off, 1.18% high_volatility across 2292 resolved rows**. The artifact's figure
excludes `risk_off` entirely, which is the defect above.

> **Superseded 2026-07-29 — see "B4 correction resolved" below.** Both figures are
> correct at different resolution horizons (98.79% at the 3-day primary window,
> 96.47% at 1-day); the gap is not an artifact omission.

---

# Addendum 2 — B4 correction resolved (2026-07-29)

## What the previous addendum got wrong

**The producer was not broken.** `build_regime_performance_summary` does emit the
enrichment fields — verified against live rows: `neutral.share_of_evidence 0.9879`,
`return_weighted_share 0.9582`, `effective_signals 925`. The on-disk artifact
(`generated_at 2026-07-28T09:03:30`) lacked them because it was written by the
**2026-07-28 cron running pre-B4 `main` code**, while B4 sat on an unmerged branch.
The real fault was a *stale-artifact coupling*: an assessor reading an artifact one
schema generation behind its own producer.

**`risk_off` was not omitted — it was immature.** 108 `risk_off` rows existed
(2026-07-25→27, all `regime_data_quality=full`), 54 resolved at the 1-day window and
**zero at the 3-day primary window**. `by_regime` only covers rows resolved at the
primary window, so the label was two days too young to appear. The addendum read
that absence as the producer dropping a label.

**The two neutral-share figures were both right.** 2211/2238 = **98.79%** at the
3-day window; 2211/2292 = **96.47%** at 1-day. Different horizons, not a defect.

**The `n`/`share` diagnosis was imprecise.** The artifact *did* carry `total_signals`
(27 / 2211) and top-level `resolved_signals` (2238). What was absent was the derived
`share_of_evidence`, which `float(None or 0.0)` coerced to **0.0**.

## The defects that were real, and survive the staleness explanation

| # | Defect | Consequence |
|---|---|---|
| 1 | Missing `share_of_evidence` coerced to `0.0` (`regime_coverage.py:149`) | A 98.8%-concentrated window read as balanced; `REGIME_CONCENTRATED` structurally could not fire. **Best-case imputation from missing data** — the practice the program's own "never impute a missing component" decision forbids. |
| 2 | `max()` over an all-zeros share dict | Named `high_volatility` (n=**27**) as the concentration leader at 0.0%, while `neutral` held 98.8%. A misleading attribution, not merely a missing one. |
| 3 | `risk_off` absent → *"never observed in resolved evidence"* | **Factually false**, and the verdict would flip to "proven" purely on maturation. |
| 4 | Both absences collapse to `REGIME_DATA_INSUFFICIENT`, which by contract does **not** downgrade | An unreadable artifact bought a **GREEN** `strategy_lab_health` — verified before the fix. A stale producer silently earned credibility it never measured. |

Defect 4 was not in the sent-back list. It is the one that mattered most: the
fail-closed fix requested for defect 1 would, on its own, have *weakened* the
downgrade it was meant to strengthen.

## Changes

| Change | Rollout class |
|---|---|
| Producer emits additive `regime_census` — `{observed, resolved}` per label over ALL rows, resolved or not | artifact-only, backward compatible |
| Assessor fails closed on absent required fields → `REGIME_DATA_INSUFFICIENT` + `insufficiency_kind: missing_derived_fields`, naming the exact missing field(s); no concentration leader named | artifact-only |
| `insufficiency_kind` splits `too_few_resolved` (no evidence — no downgrade) from `missing_derived_fields` (unreadable evidence — **downgrades**) | artifact-only + health |
| `risk_off.absence_kind` ∈ `never_observed` / `immature` / `indeterminate` / `inconsistent`; no census → `indeterminate`, never a "never observed" claim | artifact-only |
| `strategy_lab_health` downgrades `ranking_credibility`/`oos_validity` on `regime_coverage_unreadable` | gated, default ON (health only) |
| `daily-tool-analysis` 26b / signals / 6m2 updated: unreadable ≠ thin, dispatches `portfolio-resolver-investigator` (developer lens) | analysis-only |
| Status artifact `schema_version` 1 → 2 | artifact-only |

## Verified on live state

Re-derived through the shipped path (`generate_regime_performance_reports` against
`data/portfolio.db`, 2026-07-29):

```
regime_census: neutral {observed 2238, resolved 2211}
               risk_off {observed 108, resolved 27}
               high_volatility {observed 27, resolved 27}
by_regime:     neutral share 0.9762 (return-wtd 1.0722), risk_off share 0.0119, high_vol 0.0119
assessor:      REGIME_CONCENTRATED + RISK_OFF_UNPROVEN, assessable: true
               concentration.max_share_regime: neutral (0.9762)  [was high_volatility @ 0.0]
               risk_off: effective_signals 27 < 30  [measured, not inferred from absence]
```

**`risk_off` matured 0 → 27 resolved overnight**, exactly as predicted. The verdict
label is unchanged (`RISK_OFF_UNPROVEN`) but it is now reached through the *measured*
sufficiency route instead of a false absence claim — yesterday's wrong reason would
have become a different wrong reason today.

`return_weighted_share 1.0722` exceeds 1.0 because contributions are signed and
`risk_off`'s is negative (−0.1189); that is the producer's documented caveat, not a
new defect.

## Tests

15 new tests, all watched failing first:

- `tests/test_regime_coverage.py` — 11 new: no-imputation, missing field named, no
  leader named, `too_few_resolved` vs `missing_derived_fields`, three `absence_kind`
  paths, plus two regression locks (the literal 2026-07-28 stale shape → fails
  closed; the regenerated shape → concentrated).
- `tests/test_regime_performance.py` — 2 new: census counts observed vs resolved;
  census includes labels absent from `by_regime`.
- `tests/portfolio_sim/test_strategy_lab_health.py` — 1 new: unreadable artifact
  downgrades and does not buy a free pass (failed with `GREEN != GREEN` pre-fix).

Suites run: 278 passed across `tests/portfolio_sim/` + the regime/quant-watch set;
54 passed across the performance-feedback set.

## Still open

Nothing from B4. The regime-concentration verdict now reaches the Strategy Lab's
validity dimensions from measured shares. The `98.8% neutral` concentration itself is
unchanged and remains a live caveat on every claimed edge — the fix makes it visible,
it does not resolve it.

---

# Addendum — 2026-07-29: B4 completion (the producer was never wired)

"Nothing from B4" above was wrong on one point, found by the next day's
`/run-all-daily`: B4 shipped `portfolio_automation/regime_coverage.py` with the pure
assessor, an in-process consumer, 15 tests, and the `/daily-tool-analysis` prose —
but **neither of the two registration steps** the `quant_watch_probes` precedent
establishes. `run_regime_coverage`, the function that writes
`outputs/latest/regime_coverage_status.json`, had **no caller** in
`scripts/run_daily_safe.sh`, `main.py`, or any crontab line. The artifact did not
exist on disk.

## Why nothing caught it

This is the program's own recurring defect class — a verdict derived from absent data
— but one level up: not a producer deriving a verdict from a missing input, **two
meta-monitors deriving GREEN from an absent registration.**

Both the artifact-registry validator and the pipeline-wiring probe enumerate
producers from the *same* source: the `artifacts:` map in
`portfolio_automation/artifact_registry.yaml` (`pipeline_wiring_probe._load_registry`
→ `classify_producers` iterates `registry.items()`). An artifact absent from that
YAML is outside the loop domain, so it **cannot** be classified `unwired` — hence
`unwired: 0` over 123 audited producers, and `overall_status: green`. The registry's
own header states the requirement it depends on: *"Hand-edited on every new
artifact."* That hand-edit was the miss.

Verified counterfactually: with the row present and the caller absent, the probe
correctly returns `status: unwired` / `overall: amber`. The row is what makes the gap
**visible**; the cron stage is what makes it **fixed**. Both were needed.

The intended path was skill-invoked, and it was doubly fragile: `scripts/daily_check.sh:80`
only runs the skill on a RED deterministic verdict, and even when it does run, the
sub-check is prose instructing an LLM to execute a bash block. On 2026-07-29 the skill
*did* run at 09:15 and the artifact was still absent.

## Shipped

| Change | File |
|---|---|
| Registry row (`role: probe`, `required: false`, `severity_if_missing: info`, `cadence: daily`, `producer: regime_coverage`) | `portfolio_automation/artifact_registry.yaml` |
| Deterministic cron caller, Stage 7f1, non-blocking `run_aux_stage`, after the decision run and before `run_daily_run_status` | `scripts/run_daily_safe.sh` |
| 6 wiring/contract tests | `tests/test_run_daily_safe_wiring.py` |

`required: false` / `severity_if_missing: info` is deliberate: every consumer already
degrades to `"Regime-coverage: not run"`, and `required: true` would move
`daily_run_status.required_missing_count` off zero on any degraded run. The row is
also kept out of the order-pinned `daily_run_status_tracked` list.

Post-fix live state: probe audits 124 producers (was 123) and reports
`regime_coverage_status.json` `healthy` with `caller_cadences: ["daily"]`; the registry
tracks 142 artifacts (was 141), 133 present, `regime_coverage_status.json` absent from
`missing[]`. Assessor output unchanged: `REGIME_CONCENTRATED` + `RISK_OFF_UNPROVEN`,
`resolved_signals: 2265`, `assessable: true`.

**Not changed:** the `/daily-tool-analysis` sub-check still runs `run_regime_coverage`
itself. It is idempotent and harmless now that cron writes the artifact, but note that
`write_files` defaults to `True`, so an out-of-band skill run stamps a fresh
`generated_at` that no pipeline produced. Removing the now-redundant sub-check touches
oversight config and needs explicit operator sign-off.

## Also completed from 2026-07-28: the expectancy gate was applied to one path only

`c0fc3c6c` ("gate weight auto-applicability on expectancy, not just hit-rate") fixed
`_propose_weight_changes` and left `_propose_promotion_gate` in the exact pre-fix
shape — `auto_applicable` read delta magnitude, `n`, and significance, and never
referenced `mean_return`. That was load-bearing, not cosmetic:
`retune_auto_apply.apply_suggestions` appends the gate proposal to the **same**
candidate list as the weight proposals (`retune_auto_apply.py:282-284`) and passes
each through `_apply_one`, so the accuracy-vs-expectancy confusion simply moved next
door on a path the armed auto-apply layer reads.

Symmetric fix in `portfolio_automation/retune_suggestions.py`:

- `mean_return_1d` + `mean_return_resolved_n` are always recorded on the gate
  proposal, including on the no-change default, so a consumer never has to
  distinguish "key absent" from "expectancy unverified".
- `auto_applicable` additionally requires `expectancy_available` and
  `not expectancy_contradiction`. Missing return data is never imputed as `0.0`.
- **Direction semantics differ from the weight path and are documented in the
  docstring:** a *positive* `high_theme_confidence` hit-rate delta *raises*
  `extended_watchlist.confidence_threshold`, i.e. leans **harder** on that tag. The
  contradiction is therefore "lean harder on a tag that loses money on average". A
  threshold *decrease* alongside a negative mean return is consistent and is **not**
  flagged.
- The Markdown renderer's gate section gained an explicit mean-return line and a
  `⚠` contradiction line, matching the column + warning `c0fc3c6c` added to the
  weight table (previously these facts existed only inside the rationale prose).

Fail-closed tightening only — it can turn an existing `True` into `False`, never the
reverse. On today's live `pattern_efficacy_monthly.json` the gate's expectancy is
`+0.8079` over 190 resolved samples, so `auto_applicable` stays `True` and
`auto_applicable_count` is unchanged at 4 — the same "no behaviour change today, gap
closed for the divergent window" outcome `c0fc3c6c` reported for the weight path. The
2-run confirmation token is `(proposed_value, delta)` only, not the artifact hash, so
the added schema keys do not reset `pending_confirmations`.

9 new tests (`tests/test_retune_suggestions.py::TestGateExpectancyGate`), all watched
failing first.

## Still open after this addendum

- The broader `auto_applicable` evidence bar is unchanged and remains loose: it
  accepts any significance not in `(None, "insufficient_sample")`, so `"neutral"`
  passes — 3 of today's 4 auto-applicable proposals are statistically neutral (one on
  a **+0.23pp** delta), while the only `winner` is blocked on `n < 200`. Tightening it
  is a policy change beyond completing `c0fc3c6c` and is queued for operator review,
  not shipped here.
- The 2-run confirmation token requires `(proposed, delta)` byte-identical across
  consecutive runs while the rolling-30d recompute changes it weekly, so it is
  structurally unsatisfiable — the loop has never actuated (`data/retune_audit_log.jsonl`
  does not exist; 0 applies lifetime). Also queued, not shipped.
