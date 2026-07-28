# Audit WS2/WS3 — OOS Claims and Leaderboard Selection Bias

Scope: `/opt/stockbot`, read-only, `main` branch. Snapshot artifacts read on
2026-07-28 (`strategy_leaderboard.json` / `walk_forward_results.json` created
`2026-07-28T09:45:38Z`; `poc_simulation_results.json` / `signal_weight_proposals.json`
generated `2026-07-01T09:50:38Z`/`:39Z`). No files were modified.

---

## WS2 — What the OOS claims actually mean

### 1. What sets `still_works_oos = true`? Real OOS or relabelled in-sample?

`portfolio_automation/portfolio_sim/walk_forward.py:84`:
```python
"still_works_oos": bool(oos_mean > 0 and oos_hit >= 0.5),
```
This *is* a genuine train→test walk-forward: for each fold, params are chosen
by maximizing train-window `excess_vs_spy` (`walk_forward.py:54-58`, look-ahead
safe — grid search only sees `tr`), then the **same chosen params** are
re-evaluated on the next (unseen) `test_months`-long window (`walk_forward.py:64`).
So it is not a relabelling of in-sample data — it is a real, if narrow, OOS test.

But it is only ever computed for **one tactic** in the whole 26-row leaderboard.
`run_strategy_lab.py:173-188` (`_walk_forward_results`) hardcodes:
```python
out["research_momentum_rotation"] = walk_forward(build, grid, panel, ...)
```
No other tactic is passed through `walk_forward()`. Confirmed against the live
artifact — every row except `research_momentum_rotation` has
`still_works_oos: null, overfit: null`:
```
research_vol_managed            still_works_oos=None overfit=None score=1.7474
research_black_litterman        still_works_oos=None overfit=None score=1.7132
shadow_boom_bucket              still_works_oos=None overfit=None score=1.7092
...  (22 more rows, all None/None)
research_momentum_rotation      still_works_oos=True  overfit=2.009588 score=-1.1565
```
(full dump in scratch, 26/26 rows, only the last is non-null). The one tactic
that *is* tested is also the **worst-ranked** of the 26 (`strategy_score = -1.1565`,
last place) — the top 25 by score have simply never been OOS-tested.

**Fold count for the one tactic that is tested**: `train_months=24, test_months=3`
(`run_strategy_lab.py:185`), `walk_forward_results.json` → `"splits": 11`. No
minimum-fold requirement gates `still_works_oos` — with `splits=1` the same
boolean formula would apply.

### 2. Quantifying `walk_forward_results.json`

Full content (only tactic present, `research_momentum_rotation`):
```json
{
  "status": "ok", "train_months": 24, "test_months": 3, "splits": 11,
  "is_mean_excess": 2.120543, "oos_mean_excess": 0.110955,
  "oos_hit_rate": 0.6364, "is_oos_gap": 2.009588, "overfit": 2.009588,
  "still_works_oos": true
}
```
- 11 folds total, rolling forward `test_months=3` at a time
  (`walk_forward.py:47-69`, `i += test_months` each iteration).
- Test windows are **contiguous, not overlapping** (fold *k*'s window ends where
  fold *k+1*'s begins — `_win(mdates[i], mdates[i+test_months])`, next
  `i += test_months`), so 11 × 3 months ≈ **33 distinct forward months**
  (~2.75 years) of test data. Train windows *do* overlap heavily (24-month
  rolling window, only advances 3 months/fold → ~21/24 months shared with the
  prior fold's train window) — normal for walk-forward, but means the 11
  "trials" are far from independent.
- **No embargo/purge gap** between train end and test start anywhere in
  `walk_forward.py` — `test_start = mdates[i]` directly follows
  `train_end = mdates[i-1]` with no skipped buffer.
- No regime tagging/labeling at all in this file — "distinct regimes" is not a
  concept the walk-forward module tracks.
- The IS→OOS collapse is stark: in-sample mean excess-vs-SPY per fold
  averages **+212.1%** (`is_mean_excess=2.120543`, cumulative over 24-month
  training windows, inflated by selecting the best of a 9-point grid on the
  train data itself) vs. **+11.1% OOS** (`oos_mean_excess`) — a ~95% degradation
  — yet `still_works_oos=True` because `oos_mean_excess > 0` and
  `oos_hit_rate=0.6364 ≥ 0.5`. The pass bar is binary sign + majority hit rate,
  not a magnitude or effect-size check.

### 3. Does `failing_oos: []` mean "all passed" or "none tested"?

Confirmed from code: **it means "none tested."** `strategy_lab_health.py:121`:
```python
failing_oos = [r["tactic_id"] for r in rows if r.get("still_works_oos") is False]
```
This is an `is False` identity check — `None` (untested) never matches, only an
explicit `False` (tested-and-failed) does. With today's real artifact, 25/26
rows are `None` and 0/26 are `False`, so `failing_oos == []` — and the health
assessor (`strategy_lab_health.py:136-144`) then emits `status: GREEN` with the
literal reason string `"lab healthy: ran, populated, documented, no
failing-OOS tactic surfaced"`. That GREEN reason is misleading as read: it
reads as "no tactic failed OOS" when the accurate statement is "1 of 26
tactics was OOS-tested at all, and it ranked last."
Separately, `walk_forward_present` (`strategy_lab_health.py:120`) is merely
`wf is not None` — a file-existence check, true as soon as
`walk_forward_results.json` exists with any content, regardless of how many
(or how few) tactics it actually covers.
**Test coverage gap confirmed**: `tests/portfolio_sim/test_strategy_lab_health.py::test_healthy_green`
(lines 39-49) fixtures a leaderboard with exactly **one row**, already
`still_works_oos: True` — it never exercises the real production shape (25
`None` rows + 1 tested row) as a GREEN case, so this gap is untested, not
merely undocumented.
The GUI layer is, to its credit, more honest: `gui_v2/templates/dashboard/strategy_lab.html:139-141`
renders `still_works_oos is sameas true` → "OOS ✓", `sameas false` → "OOS ✗",
else → **"OOS —"** (a genuine "unknown" glyph). The health assessor's roll-up
text is the layer that overstates confidence, not the per-row GUI display.

### 4. Confidence intervals / degradation measure / cost-adjusted OOS return?

- **Degradation measure**: present, but only for the one tested tactic —
  `is_oos_gap = is_mean - oos_mean` (`walk_forward.py:77`), clipped to
  `overfit = max(0, gap)` (`walk_forward.py:83`).
- **Confidence interval**: **absent** in the Strategy Lab (`portfolio_sim/`)
  entirely — no Wilson/bootstrap/t-interval anywhere in
  `walk_forward.py`, `strategy_score.py`, or `run_strategy_lab.py`. (A Wilson
  95% CI *does* exist, but only in the separate Pattern-Loop machinery —
  `backtesting/walk_forward.py:32-50` — see Q6.)
- **Cost-adjusted OOS return**: **absent**. `run_strategy_lab.py:212,219`
  hardcodes `turnover = 0.7 if TimeVaryingTactic else 0.3` and `"tax_drag": 0.0`
  as fixed constants (not measured), and every leaderboard row carries
  `"tax_note": "gross_until_cost_model"` (`run_strategy_lab.py:231`) — an
  explicit self-disclosure that returns are gross, not net-of-cost. This
  applies to in-sample and the one OOS tactic alike.

### 5. Is `overfit` a computed statistic or a flag?

Both, depending on which layer you read:
- In `walk_forward.py:83`, `overfit` **is** a computed statistic: the clipped
  IS−OOS excess-return gap, a real number (e.g. `2.009588`).
- But it is `None` for 25/26 tactics (never computed), and
  `strategy_score.py:47-52` converts that `None` into an **effective flag with
  a hardcoded value of 0**:
  ```python
  overfit = components.get("overfit")
  if overfit is None:
      overfit_val = 0.0
      flags.append("overfit_unknown")
  else:
      overfit_val = max(0.0, float(overfit))
  ```
  So untested tactics are scored as if they had **zero** overfit penalty
  (`overfit_penalty` weight = `0.8`, the single largest penalty weight in
  `DEFAULT_WEIGHTS`, `strategy_score.py:22`) while the one tactic that *was*
  tested absorbs a real `-0.8 × 2.0096 ≈ -1.61` penalty term. Confirmed in the
  live artifact: `research_momentum_rotation`'s `flags` list is `[]` (the
  penalty is baked into its score, not flagged) while all 25 untested rows
  carry `flags: ["overfit_unknown", ...]` yet keep `overfit_val=0`. Net effect:
  **the leaderboard's own scoring rewards not being OOS-tested** — testing is
  the only way to incur the overfit penalty at all.

### 6. Is the Pattern-Loop `folds_possible`/`evaluated: 3318` the same machinery as the Strategy Lab?

**No — two entirely separate implementations**, confirmed by reading both files
end to end:

| | Strategy Lab (`portfolio_sim/walk_forward.py`) | Pattern-Loop (`backtesting/walk_forward.py`) |
|---|---|---|
| Unit of analysis | Portfolio tactic (weight vector) | Individual registry signal (`signal_id`, e.g. `STRONG_MOVE_UP`) |
| Split basis | Month-end dates, `train_months=24`/`test_months=3` | Calendar-day ordinals, `train_days=252`/`test_days=63`, rolling `step_days=63` |
| Param selection | Grid search on train window, evaluate chosen params on test window | No parameter selection — replays already-emitted signals through `bt.simulate_signal_performance` |
| Statistical rigor | None (no CI) | Wilson 95% CI on every hit rate (`wilson_interval`, `walk_forward.py:32-50`), `min_signals_per_fold=30` sufficiency gate |
| Wired into | `strategy_leaderboard.json` / `walk_forward_results.json` (1 of 26 tactics) | `backtesting/run_loop.py:125` (`per_signal_oos`) → `signal_weight_proposals.json` |

Critically, **`evaluated: 3318`** (the number named in the prompt) is **not**
an OOS number at all. It comes from `poc_simulation_results.json`'s
`performance.evaluated` field, produced by `poc_simulation_harness.py:232`:
```python
perf = bt.simulate_signal_performance(signals, forward_days=forward_days, ...)
```
— a **full-history replay of all loaded signals with no train/test split**
(confirmed live: `total_signals: 7727`, `evaluated: 3318`, no fold structure in
that payload at all). `backtest_health.py:92-93` reads exactly this field for
its `low_sample`/`degenerate_regimes` checks — i.e., the yearly health check's
`evaluated` figure is an in-sample-style count, not a walk-forward OOS count.

The genuinely OOS-gated numbers live in a *different* artifact,
`outputs/policy/signal_weight_proposals.json` (Step 2→4, `per_signal_oos` →
`propose_weight_changes`), confirmed live:
```json
{"signal_id": "STRONG_MOVE_UP", "oos_n": 3042, "oos_hit_rate": 54.47,
 "oos_hit_rate_ci95": [52.7, 56.23], ...}
{"signal_id": "VOLUME_SPIKE", "oos_n": 161, "oos_hit_rate": 65.22,
 "oos_hit_rate_ci95": [57.58, 72.14], ...}
```
`folds_possible` itself comes from a third function, `oos_window_status()`
(`backtesting/walk_forward.py:198-251`) — a pure calendar-maturity gate
(`observed >= train_days`, `walk_forward.py:245`) that says nothing about how
many folds *actually* ran or how much data any given signal_id has; it just
means "the observed history is long enough that folds are structurally
possible." Live value: `calendar_days_observed: 1848`, `folds_possible: true`.

**Net finding**: `folds_possible: true` + `evaluated: 3318`, read together as
in the prompt, invite the inference "3,318 OOS-evaluated observations" — that
inference is wrong. `folds_possible` is a maturity flag from a third
function; `evaluated: 3318` is a non-OOS full-history count from a fourth
function (`poc_simulation_harness`); the actual OOS sample sizes are the much
smaller, per-signal `oos_n` values in `signal_weight_proposals.json` (3042 and
161, for the only two signal_ids with any proposal at all).

---

## WS3 — Leaderboard-level selection bias

### 1. Multiple-comparison correction / deflated Sharpe / SPA / PBO / bootstrap rank stability

**Confirmed absent, repo-wide**, for the Strategy Lab. Broad search:
```
grep -rniE "deflated.sharpe|reality check|SPA test|probability of backtest
  overfitting|\bPBO\b|bootstrap.*rank|multiple.comparison|bonferroni|holm|
  false discovery|benjamini" --include=*.py .
```
returned **zero hits** anywhere in `portfolio_sim/`, `backtesting/`, or
`tests/`. The only "significance"-adjacent code in the repo is the Wilson CI
in `backtesting/walk_forward.py` (Pattern-Loop, per-signal, not per-tactic)
and the phrase `"significance gate"` in `backtesting/tuning_proposals.py:13`
(same Pattern-Loop lane, a CI-straddles-50% check, not a leaderboard-selection
correction).

One place in the repo already *names* this exact defect class and proposes
the fix — but for a different, not-yet-built subsystem:
`docs/superpowers/specs/2026-07-27-weekly-etf-daily-improvement-design.md:116-117`:
> "Multiple-comparison correction — Holm/Bonferroni across the variant set
> (best-of-4 is selection-biased); record raw + corrected significance."

That is the Weekly-ETF-Bundle champion/challenger design (per
`docs/superpowers/specs/...`, Phase 1 implementation plan only, per repo
history — not merged/active code), and it covers a 4-variant selection, not
the 26-tactic Strategy Lab leaderboard. **The Strategy Lab itself has no such
correction, planned or implemented.**

### 2. How many of the 26 are materially distinct families?

Grouped by `source` + materialization code path (`tactics.py`,
`research_library.py`):

| Family | Count | tactic_ids | Distinctness |
|---|---|---|---|
| `research_*` (research_library.py) | 8 | vol_managed, black_litterman, mean_variance, dual_momentum, factor_tilt, risk_parity_lite, sixty_forty, momentum_rotation | Genuinely distinct academic methodologies, but 2 of 8 (`momentum_rotation`, `dual_momentum`) are both momentum-family; `risk_parity_lite`/`vol_managed` are both risk-based-sizing family |
| `profile_*` (SEED_PROFILES) | 8 | balanced_core_satellite, long_term_compounding, tax_aware, boom_bucket, short_term_tactical, aggressive_growth, income_dividend, defensive_capital_preservation | **One parameterized family, not 8 ideas** — all 8 run through the identical `_apply_tilts()` function (`tactics.py:157-194`) on the **same anchor base**, differing only in a hardcoded set of category multipliers/floors (`mul("equity", 1.5)` vs `mul("equity", 1.2)`, etc.) |
| `shadow_*`/`baseline` (shadow_tracker) | 6 | actual_baseline, target_allocation_baseline, engine_followed, lower_risk, discovery_enhanced, boom_bucket | 2 are literal baselines (not strategies); the other 4 are overlay variants of the same real-portfolio derivation (`build_shadow_portfolios`) |
| `crowd` | 2 | crowd_signal_only, crowd_signal_plus_sentiment | One idea (crowd signal), with/without a sentiment-tilt add-on — not independent |
| `benchmark` | 2 | benchmark_spy, benchmark_qqq | Single-asset reference points, not strategies |

**Effective independent-trial count is far below 26.** A defensible read:
8 research methodologies collapse to ~5-6 independent ideas (momentum-family
and risk-family internally correlated); the 8 `profile_*` rows are 1 family ×
8 preset variants (akin to an un-corrected grid search); the 6 `shadow_*` rows
are 2 baselines + 1 family × 4 overlay variants; crowd is 1 family × 2
variants. Order of magnitude: **~8-10 materially distinct ideas**, not 26 — a
multiple-comparisons correction over "26 trials" would already understate the
effective search space in the *other* direction (profile/shadow variants are
so correlated that Bonferroni-style independence assumptions would also be
wrong); a proper PBO/deflated-Sharpe treatment would need to model the
within-family correlation, which nothing in the repo currently captures.

### 3. What raw per-tactic stats are persisted — is post-hoc correction possible without re-running?

Confirmed by reading a live leaderboard row in full (`research_vol_managed`):
only a `by_window` array of **4 scalar summaries** is persisted per tactic —
`trailing_1y`, `trailing_3y`, `trailing_5y`, `ytd` — each carrying
`excess_vs_spy, cagr, max_drawdown, sharpe, final_balance_dca`. No daily or
monthly return series, no per-fold results (for the 25 non-walk-forward
tactics; even the 1 walk-forward tactic only persists 11 fold-level scalars,
not the underlying return series). Also worth flagging: these 4 "windows" are
**nested, not independent** — `trailing_1y ⊂ trailing_3y ⊂ trailing_5y`, and
`ytd ⊂ trailing_1y` — all over the *same* single historical price history.
`prob_beat_spy` (e.g. `1.0` = "beats SPY in 100% of windows" for
`research_vol_managed`) is therefore a statistic over 4 heavily-overlapping
slices of one realized path, not 4 independent observations — a materially
weaker "consistency" claim than the field name implies.

**Conclusion**: a post-hoc multiple-comparison correction (deflated Sharpe,
SPA, PBO, bootstrap rank stability) is **not computable from what's
persisted** — those techniques need either a full return series per
tactic/fold or the ability to resample paths, and only 4 nested scalar
summaries per tactic are written to `outputs/sandbox/strategy_leaderboard.json`.
Any such correction would require re-running `run_strategy_lab.py` with
additional instrumentation to persist return series, not just post-processing
the existing JSON.

### 4. Is the same historical data reused across all 26 tactics?

**Confirmed yes.** `run_strategy_lab.py:262-271`:
```python
tickers = sorted({t for tac in tactics for t in tac.target_weights} | extra_tickers | {bench_t})
panel = load_price_panel(tickers, root)
...
windows = resolve_windows(cfg["windows"], panel.dates)
bench = {w.key: benchmark_total_return(panel, bench_t, w) for w in windows}
```
One `panel` (single price load) and one `windows`/`bench` set are computed
once, then every tactic is scored against that identical panel/windows in the
`_score_tactic(t, panel, windows, bench, cfg, wf_results)` loop
(`run_strategy_lab.py:275`). All 26 tactics compete on the exact same
historical draw — there is no per-tactic resampling, no held-out data split
across tactics, nothing that would break the shared-history dependence that a
selection-bias correction needs to account for.

---

## Summary of confirmed-vs-inferred

| Claim | Status |
|---|---|
| `still_works_oos=true` is a real train/test OOS test | Confirmed real, but computed for only 1/26 tactics |
| `failing_oos: []` means "all passed" | **False** — confirmed it means "none tested" (`is False` check, `None` never matches) |
| `walk_forward_present: true` implies broad OOS coverage | **False** — confirmed it is a file-existence check only |
| GREEN "no failing-OOS tactic surfaced" is trustworthy language | Misleading as read; confirmed by code that 25/26 tactics were simply never tested, not tested-and-passed |
| `overfit` is a computed statistic | True only for the 1 tested tactic; `None`→`0.0` for the other 25, which the scorer treats as "no overfit," the opposite of "unknown" |
| Untested tactics are penalized less than the tested one | Confirmed: `overfit_penalty` weight 0.8 hits only the 1 tested (and worst-ranked) tactic |
| Pattern-Loop `evaluated: 3318` is an OOS figure | **False** — confirmed it's a full-history, non-split count from a different function (`poc_simulation_harness.simulate_signal_performance`); real per-signal OOS n is 3042 / 161 in `signal_weight_proposals.json` |
| Strategy Lab and Pattern-Loop walk-forward are the same code | **False** — confirmed two separate implementations, different domains, no shared functions |
| Any multiple-comparison/deflated-Sharpe/SPA/PBO/bootstrap correction exists | **Confirmed absent** repo-wide for the Strategy Lab; planned (Holm/Bonferroni) only for the unrelated, not-yet-active Weekly-ETF-Bundle 4-variant selector |
| Post-hoc correction is computable from persisted data | **No** — only 4 nested/overlapping window scalars per tactic are stored; no return series, no fold-level data for 25/26 tactics |
| Same historical data used for all 26 tactics | Confirmed yes — one shared price panel/windows for the whole leaderboard |
