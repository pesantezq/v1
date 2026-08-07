# Memo Truth + Dark Controls — Design

**Date:** 2026-08-07
**Source:** `/run-all-daily` suite run, 2026-08-07 00:31–00:50 UTC
**Scope:** 9 confirmed defects (Group A: 5 memo-correctness · Group B: 4 dead/dark controls)
**Out of scope:** the 12 measurement-validity probes in quant-watch (Group C) — deliberately tracked, several need design decisions that do not exist yet.

---

## Problem

The 2026-08-07 daily suite returned RED on both `daily-tool-analysis` and
`quant-watch-analysis`. Neither RED was a new fault — the attribution delta is
the settled tape confound — but the run surfaced nine defects that **are**
fixable, verified individually against source rather than accepted from agent
reports.

All three RED quant-watch probes are memo-rendering defects. The underlying
JSON artifacts reconcile; roughly thirty figures were cross-checked and only
the rendered layer was wrong. Separately, three safety/observability controls
are dark while reading as present, and one acceptance artifact is invisible to
artifact governance.

Two failure classes recur across all nine:

1. **A verdict derived from a value on the wrong basis** (A1, A2, A4) — the
   arithmetic is right, the comparison is not.
2. **A control that reads as working while structurally unable to fire**
   (B6, B7, B8, B9) — the repo's documented recurring defect class.

---

## Constraints

- Advisory-only. No broker integration, no execution logic.
- **No protected semantics touched.** None of the nine goes near
  `decision_engine.py`, `signal_score`, `confidence_score`, `effective_score`,
  `conviction_score`, `final_rank_score`, or `recommendation_score`.
- B7 and B9 are observability-only: they must stay `observe_only` and must
  never gate the sleeve, the pipeline, or any score.
- Additive and backward compatible. `outputs/latest/decision_plan.json` remains
  the decision source of truth and is not read or written by any fix here.
- `pytest -q` mutates the real `config/signal_registry.yaml`
  (`default_weight` 0.4947). Verify restoration before every commit; stage
  explicit paths, never `git commit -am`.

---

## Approach: two branches, staged by blast radius

Group A touches only renderers. Worst case is a wrong memo line — the thing
being fixed. Group B touches `main.py`, which the 09:00 UTC cron runs; a
mistake there fails the daily run rather than producing a wrong line.

Group A is also **fully verifiable offline** against today's frozen artifacts.
Group B's B6/B7 only prove out on a real scanner run, so they carry a
"not fully verifiable until the next cron" tail that Group A does not.

Staging therefore lets the three RED clearances merge on proven evidence
without waiting on, or being blocked by, the riskier pipeline edits.

| Branch | Fixes | Blast radius | Verifiable today |
|---|---|---|---|
| `fix/memo-truth` | A1–A5 | renderers only | yes, fully |
| `fix/dark-controls` | B6–B9 | `main.py`, pulse, registry | partially |

---

## Branch 1 — `fix/memo-truth`

### A1 · Recovery clause asserts an arrival that did not happen — RED

`daily_memo.md:8` and `:72` read *"Recovery… to prior peak 68.9%"* while the
current gauge is **56.28%**, 12.66pp below that peak.

The predicate at `watchlist_scanner/daily_memo.py:791-796` is a *correct*
"not a new high" test:

```python
is_recovery = bool(... and (cur_hr - best_hr) * 100 <= margin_pp)   # margin_pp = 2.0
```

The defect is that this one boolean also emits the word "recovery", which
asserts proximity. It is one-sided, so it fires identically at +2pp above the
peak, at the peak, and 12.7pp below it.

**Fix.** Add a lower bound so `is_recovery` means "within ±2pp of the peak":

```python
and (cur_hr - best_hr) * 100 >= -margin_pp
```

Both call sites (`:894`, `:2103`) already hold `cur_hr` and `peak_hr`, so each
computes `gap_pp` locally. `_prior_peak`'s signature and 2-tuple return are
**unchanged**, so no existing test signature changes.

| Condition | Verdict clause (`:897`) | Advisor-stack clause (`:2106`) |
|---|---|---|
| `gap > +2pp` | *(suppressed — new high)* | *(suppressed)* |
| `-2pp <= gap <= +2pp` | ` Recovery to near prior peak {p}%, not a new high.` | ` — recovery to near prior peak {p}%, not a new high` |
| `gap < -2pp` | ` Still {abs(gap)}pp below prior peak {p}%.` | ` — still {abs(gap)}pp below prior peak {p}%` |

The `≈` glyph is dropped everywhere; the gap is always rendered numerically.

**Why all four existing `_prior_peak` tests still pass unchanged.** The fixture
in `tests/test_daily_memo_verdict.py:838-849` uses `CURRENT = 0.6955` against a
peak of `0.6894` — **+0.61pp above** it. The tests encode the correct intent;
the far-below case was simply never written. Verified:

| Case | gap | old | banded |
|---|---|---|---|
| `test_prior_peak_flags_recovery_to_older_peak` | +0.61pp | True | **True** |
| `test_prior_peak_no_recovery_when_genuine_new_high` | +6.06pp | False | **False** |
| live 2026-08-06 | −12.66pp | **True (bug)** | **False** |

The two remaining tests short-circuit on `best_fp == prior_fp` regardless.

### A2 · Sector-cap false breach — RED

`daily_memo.md:59` reports `Financial Services 25.6% (soft target 25% — over)`.
Two different denominators:

- `portfolio_snapshot.top_sector.allocation_pct = 0.2563` is share of the
  **normalized allocation book** (`portfolio_construction.py:276`,
  `0.06 / 0.2341`).
- `allocation_engine.DEFAULT_CONFIG["sector_cap"] = 0.25` caps share of
  **portfolio value** (`allocation_engine.py:239`).

On the cap's own basis Financial Services is `allocation_by_sector = 0.06`
→ 6.0%, well inside a 25% cap. Nothing is over. The producer corroborates: the
threshold governing the share-of-book basis is
`top_sector_warning_threshold = 0.40`
(`DEFAULT_PORTFOLIO_CONSTRUCTION_CONFIG`), and it did not fire —
`portfolio_snapshot.warnings` holds only the two theme warnings.

**Fix.** Keep the 25.6% figure (it is the informative concentration read) and
swap the comparator to the threshold that governs that basis, imported the same
way the current code imports `allocation_engine`. Label the basis explicitly:

```
Top sector — Financial Services 25.6% of allocation book (warn at 40%)
```

### A3 · Advisor stack reads a different config than the run — RED

`daily_memo.py:1844` hardcodes `_CONFIG_BASE_REL = ("config", "base.json")`.
Verified divergence: `config/base.json` has `ml_advisor.enabled = True`;
`config.json` (what the run uses) has `False`. The operator is told an advisor
is ON that the run had OFF, and the same knob sits inside the gauge
fingerprint.

Secondary, same line: `daily_memo.py:1981` counts
`splitlines()` of `data/ml_history.json`, which is a pretty-printed JSON
**object**, not JSONL — rendering **13877** records for an actual **375**
(37× overstatement).

**Fix.**
- Resolve config through `config.loader.load_config()`, the same resolver the
  run uses (honors `CONFIG_PATH`, structured-vs-legacy). The memo then cannot
  diverge by construction rather than by keeping two paths in sync.
- Count via parsed length (`len()` of the loaded dict/list), guarded, falling
  back to `0` on unparseable input as today.

### A4 · Capital-plan glide double-count — AMBER

`daily_memo.md:16` shows `Incoming contributions: $1,145` one line below
`Cash on hand: $2,100`. `capital_plan_view.py:426-428` sources it from
`monthly_contribution_net_investable = 1144.57`, which is
`..._base 1000.0` **plus** `glide_slice 144.57`. The glide is by construction
`excess_cash_glide_fraction (0.25) × idle_excess (578.30)` — a slice of
**existing** idle cash already inside `cash_on_hand`. Corroborating:
`funding.deployable_from_incoming = 0.0`.

**Fix.** Source the line from `monthly_contribution_net_investable_base` and
render the glide as a labelled component so it stays visible without being
counted as new money:

```
Incoming contributions: $1,000 (+$145 glide from existing cash)
```

### A5 · Crowd divergent count saturates at the display cap — AMBER

`daily_memo.md:55` reports `divergent 8`; the true count is **11**.
`memo_coherence.py:798` truncates the ticker list with `[:8]`, and
`daily_memo.py:2804` renders `len()` of that capped list, so the figure can
never exceed 8. Ground truth is
`classified_state_counts.divergent_attention = 11` (verified present in
`memo_coherence.json`). The same memo line already does this correctly for
`insufficient_data_count`.

**Fix.** Read `classified_state_counts.*` for both `divergent_attention` and
`confirmed_attention` (the latter is right only by luck at 2 < 8 and carries
the identical latent defect). The `[:8]` cap stays — it is correct for a
*display list*; only the count stops reading through it.

---

## Branch 2 — `fix/dark-controls`

### B6 · Scanner safe-mode cannot reach the oversight surface — AMBER

`main.py:1437-1438` **does** populate `_scanner_meta["safe_mode"]` and
`["safe_mode_reasons"]`, and `:1465-1466` sets them on `result['scanner']`.
But `scraped_intel/run_summary.py:build_run_summary()` has no such parameters
and `main.py:1882` passes neither, so
`scraped_intel_run_summary.json:scanner.safe_mode` is absent.
`scanner_canary.py:189-191` then reads `None` and prints
*"speculative sleeve suppressed: None / suppression reasons: none"* on a run
where the sleeve **was** suppressed for two reasons. A safety control reads as
ABSENT rather than ENGAGED.

**Fix.** Add `safe_mode: Optional[bool] = None` and
`safe_mode_reasons: Optional[List[str]] = None` to `build_run_summary`, emit
them in the `"scanner"` dict alongside the existing quality dimensions, and
pass both from `main.py:1882` out of `_scanner_meta`. Defaults keep every
existing caller working unchanged.

### B7 · factor_liveness is null on every daily run — AMBER

`main.py:1454` reads `bulk_metrics`, which is bound only in the monthly branch
(941/977) and weekly branch (1029–1067). On the daily cadence it raises
`UnboundLocalError`, caught at `:1461` and swallowed to `None`, so the
2026-08-03 factor/filter liveness feature — including the known-inert PE
tracking — is silently dark daily and the canary reports
`factor_liveness_absent`.

**Fix (chosen: explicit not-assessable).** Initialize `bulk_metrics = None`
alongside the other scanner-block locals near `main.py:876`, then skip the
assessment when metrics are absent and emit an explicit status meaning
*this cadence has no fundamentals to assess*, rather than running the assessor
against an empty dict.

**Rationale.** Assessing with `{}` would report every factor inert on every
daily run, burying the real known-inert PE finding in a daily false alarm — and
it would repeat the exact defect class this batch exists to fix: a verdict
derived from absent data. "We didn't look" must not render as "we looked and
it's dead."

### B8 · The $20/mo OpenAI cap is a dead gate — AMBER

`discovery_pulse.py:179` calls
`load_recent_ai_usage_events(base_dir=str(root / "outputs"))`. The real
signature is `(path='outputs/policy/ai_usage_events.jsonl', max_events=500)` —
there is no `base_dir` parameter. Reproduced: `TypeError`. The bare `except` at
`:187-189` returns the `-1.0` sentinel, so
`state['openai_cost_usd_month']` never leaves its `0.0` initializer and
`evaluate_caps` gates on that field against the $20 cap. **The cap can never
bind.** The failure logs at `logger.debug`, below the pulse runner's level, so
no trace appears in the log.

**Fix.** Call with the correct parameter
(`path=str(root / "outputs" / "policy" / "ai_usage_events.jsonl")`) and raise
the failure log from `debug` to `warning` so a future breakage is visible.
Real MTD value with the corrected call is ~$0.0144 — not near-binding, so no
runaway occurred; the control was simply inert.

Note: `usage.fmp_calls_month = 0` is a **true** zero (the pulse makes no FMP
calls; its 5000-call cap is vestigial). Do not "fix" it.

### B9 · scanner_recovery_canary is unregistered and monthly-only

`outputs/policy/scanner_recovery_canary.json` has **zero rows** in the 134-row
`portfolio_automation/artifact_registry.yaml`, and its sole caller is
`scripts/run_monthly_universe_refresh.sh`. It is a *monthly* acceptance gate
over a *daily*-mutating scanner, currently reading `overall: FAIL` with
`assessed_at: null` and a `run_timestamp` of 2026-08-04. Artifact governance
structurally cannot see it go stale.

**Fix.** Add the registry row (lens `market_discovery`, role `probe`,
`required: false`, cadence `daily`, `severity_if_missing: info`) **and** invoke
`run_scanner_canary` from the daily pipeline, non-blocking in `try/except`, so
the acceptance view tracks the subsystem it grades.

---

## Testing

TDD per fix: a failing test reproducing the **exact observed value** first,
then the fix.

| Fix | Test asserts |
|---|---|
| A1 | `_prior_peak` banded: `(0.5628, 0.6894)` → `is_recovery False`; `(0.6955, 0.6894)` → `True`; `(0.75, 0.6894)` → `False`. Rendered clause contains `"still 12.7pp below prior peak 68.9%"` and **not** `"recovery to"`. All four pre-existing tests pass unmodified. |
| A2 | Share 0.2563 with warn threshold 0.40 → no `over` flag; share 0.45 → flagged. Basis label present. |
| A3 | Memo reads the same config the run resolves (`ml_advisor.enabled False` when `config.json` says False); record count 375, not 13877. |
| A4 | Incoming renders base 1000.0, glide 144.57 shown as a labelled component, not summed into the headline figure. |
| A5 | `divergent` renders 11 when `classified_state_counts.divergent_attention == 11` and the display list is capped at 8. |
| B6 | `build_run_summary(safe_mode=True, safe_mode_reasons=[...])` round-trips into `scanner.safe_mode*`; omitted → `None` (back-compat). `scanner_canary` renders ENGAGED, not `None`. |
| B7 | Daily-cadence path (no `bulk_metrics`) does not raise and emits the not-assessable status, **not** an all-inert verdict. |
| B8 | `_refresh_monthly_openai_cost` returns a real positive figure, not `-1.0`; regression test asserts no `TypeError` on the call signature. |
| B9 | Registry row present and validates; the daily caller writes a canary whose `run_timestamp` is same-day. |

Then targeted suites, then full `python -m pytest -q`, confirming
`config/signal_registry.yaml` `default_weight` is restored to 0.4947 before
each commit.

---

## Follow-through

Each fix that closes a quant-watch probe gets `record_closure(...)` with the
commit SHA and its regression-test reference. A probe closes only when its
detector also stops firing — closure evidence alone does not resolve it.

Per the Analysis + Health Coverage Requirement, B9 adds the missing
artifact-registry consumer; the remaining eight are corrections to existing
producers already covered by daily checks, so no new health check is required.
