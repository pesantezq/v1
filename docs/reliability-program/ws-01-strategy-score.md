# Workstream 1 — `strategy_score` decomposition and validation audit

Scope: `portfolio_automation/portfolio_sim/strategy_score.py`, `run_strategy_lab.py`,
`strategy_lab_health.py`, `walk_forward.py`, `tests/portfolio_sim/test_strategy_score.py`,
`tests/portfolio_sim/test_strategy_lab_health.py`, `tests/portfolio_sim/test_strategy_lab_e2e.py`,
and the live artifact `outputs/sandbox/strategy_leaderboard.json` (26 rows, `created_at`
2026-07-28T09:45:38Z, `status: ok`).

Audit is READ-ONLY. No files under `outputs/` or `data/` were modified. All numeric claims below
were produced by running short read-only Python snippets against the real, checked-in artifact and
are marked **[RUN]**; claims from source reading only are marked **[READ]**.

---

## 1. The exact formula — `portfolio_automation/portfolio_sim/strategy_score.py:12-68`

**[READ]** `DEFAULT_WEIGHTS` (lines 12-23):

```
excess_return_vs_spy:        1.0
probability_beat_spy_bonus:  0.5
drawdown_control_bonus:      0.5
consistency_bonus:           0.5
research_support_bonus:      0.25
turnover_penalty:            0.3
tax_drag_penalty:            0.3
concentration_penalty:       0.3
leverage_penalty:            0.3
overfit_penalty:             0.8   <- single largest weight after excess_return
```

The composite (lines 54-65):

```python
total = (
    w["excess_return_vs_spy"] * excess
    + w["probability_beat_spy_bonus"] * (pbeat - 0.5) * 2      # center at 0.5
    + w["drawdown_control_bonus"] * (1.0 + drawdown)            # less drawdown → higher
    + w["consistency_bonus"] * consistency
    + w["research_support_bonus"] * has_research
    - w["turnover_penalty"] * turnover
    - w["tax_drag_penalty"] * tax_drag
    - w["concentration_penalty"] * concentration
    - w["leverage_penalty"] * leverage
    - w["overfit_penalty"] * overfit_val
)
```

**Weighting is configurable in principle, hardcoded in practice.** `run_strategy_lab.py:64`
reads `cfg["scoring_weights"] = lab.get("scoring") or {}` from `config.json`
`portfolio_sim.strategy_lab.scoring`, merged over `DEFAULT_WEIGHTS` at `strategy_score.py:35`.
**[RUN]** `config.json`'s live `portfolio_sim.strategy_lab` block is `{"enabled": true, "windows":
[...]}` — **no `scoring` key exists**. Every score in the live artifact was produced with the
unmodified `DEFAULT_WEIGHTS`; the "configurable" path has never been exercised in production data.

**Finding A — double-counting of `prob_beat_spy`.** `consistency` is set to the *same* value as
`probability_beat_spy` at the call site (`run_strategy_lab.py:218`: `"consistency": prob_beat,`).
So one underlying quantity (fraction of windows beating SPY) is fed into the composite twice, once
centered `(pbeat-0.5)*2 * 0.5` and once raw `prob_beat * 0.5`, for a combined effective weight of
1.0 on a single 4-window binary-ish statistic. There is no independent measure of return-path
consistency (e.g., std-dev of excess return across windows) despite the parameter's name.

**Finding B — `tax_drag` is a hardcoded constant, not a computed penalty.**
`run_strategy_lab.py:219`: `"tax_drag": 0.0,` — literally always zero for all 26 tactics, every
run. Every row also carries `"tax_note": "gross_until_cost_model"` (line 231), which is an honest
admission that costs aren't modeled — but the score composite still allocates a `tax_drag_penalty`
weight of `0.3` that can never fire. The catalog markets the composite as "penalizing ...
tax ... " (module docstring, `strategy_score.py:3`); that penalty is inert.

**Finding C — `turnover` is a two-value categorical proxy, not a measured turnover rate.**
`run_strategy_lab.py:212`: `turnover = 0.7 if isinstance(tac, TimeVaryingTactic) else 0.3`. Every
static tactic gets exactly `0.3`, every time-varying tactic (`CrowdTactic` — `crowd_tactic.py:166`)
gets exactly `0.7`, regardless of actual rebalance frequency or trade size. This is a real
data-derived quantity turned into a binary class label.

**Finding D — `overfit`'s documented scale does not match its actual scale (see §4/§5 for the
real-artifact consequence).** The function docstring (`strategy_score.py:33`) states
`overfit (0..1 IS-OOS gap; None → unknown)`. The actual producer, `walk_forward.py:83`:
`"overfit": round(max(0.0, gap), 6)` where `gap = is_mean_excess - oos_mean_excess`
(`walk_forward.py:77`) — i.e., overfit is a **raw excess-return gap**, unbounded, in the same units
as `excess_return_vs_spy` (which itself ranges to ~0.7 in the live data). It is not clipped or
rescaled to `[0,1]` anywhere. **[RUN]** the one tactic with a real walk-forward result
(`research_momentum_rotation`) has `overfit = 2.009588` — double the documented max.

---

## 2. Reproducibility — **cannot be recomputed exactly from the persisted artifact; verified by running the reconstruction**

**[RUN]** Loaded the live `outputs/sandbox/strategy_leaderboard.json`
(row keys: `tactic_id, name, source, approximate, academic_basis, strategy_score, flags,
mean_excess_vs_spy, prob_beat_spy, worst_max_drawdown, by_window, overfit, still_works_oos,
tax_note` — confirmed via `sorted(rows[0].keys())`). Reconstructed each score using
`portfolio_automation.portfolio_sim.strategy_score.score()` with the only inputs available in the
artifact (`excess_return_vs_spy=mean_excess_vs_spy`, `probability_beat_spy=prob_beat_spy`,
`drawdown=worst_max_drawdown`, `consistency=prob_beat_spy`,
`has_research=bool(academic_basis)`, `overfit=overfit`), and the three components that are **never
persisted** (`turnover`, `concentration`, `leverage`) set to `0.0` as the closest available guess.

Residual (reconstructed − stored `strategy_score`), all 26 rows:

```
tactic_id                             stored   recon(missing=0)  residual
research_vol_managed                  1.7474   2.1439            0.3965
research_black_litterman              1.7132   2.0740            0.3608
shadow_boom_bucket                    1.7092   1.9577            0.2485
shadow_lower_risk                     1.6693   1.9300            0.2607
profile_balanced_core_satellite       1.6498   1.9211            0.2713
profile_long_term_compounding         1.6495   1.9169            0.2674
profile_tax_aware                     1.6495   1.9169            0.2674
shadow_actual_baseline                1.6405   1.9169            0.2764
shadow_engine_followed                1.6405   1.9169            0.2764
shadow_discovery_enhanced             1.6405   1.9169            0.2764
research_mean_variance                1.5883   1.8528            0.2645
profile_boom_bucket                   1.5841   1.8733            0.2892
profile_short_term_tactical           1.5716   1.8595            0.2879
profile_aggressive_growth             1.5528   1.8438            0.2910
shadow_target_allocation_baseline     1.4855   1.6955            0.2100
crowd_signal_plus_sentiment           1.4618   1.8583            0.3965
crowd_signal_only                     1.4597   1.8561            0.3964
research_dual_momentum                1.3940   1.9040            0.5100
benchmark_qqq                         1.0770   1.4670            0.3900
profile_income_dividend               0.8790   1.0393            0.1603
research_factor_tilt                  0.7221   0.9321            0.2100
profile_defensive_capital_preservation 0.4919   0.6472            0.1553
research_risk_parity_lite             0.2407   0.5052            0.2645
research_sixty_forty                  0.2215   0.4915            0.2700
benchmark_spy                        -0.4850  -0.0950            0.3900
research_momentum_rotation           -1.1565  -0.8715            0.2850
```

Residual magnitude ranges **0.15 to 0.51** — 1.5x to 5x larger than the entire rank 1→5 gap of
`0.0976` (see §5). **The artifact's persisted fields are structurally insufficient to reproduce
`strategy_score`; the gap is entirely attributable to `turnover`/`concentration`/`leverage`,
weighted 0.3 each, none of which are written anywhere in `outputs/sandbox/`.**

**This is not a fundamental limitation of the code — it's a persistence gap.**
`strategy_score.py:68` already returns `{"strategy_score": ..., "flags": ..., "components":
components}` — the full component dict, including `turnover`, `concentration`, `leverage`,
`tax_drag`, is computed and available in memory. But `run_strategy_lab.py:226` only destructures
`sc["strategy_score"]` and `sc["flags"]` when building the leaderboard row — `sc["components"]` is
computed and then discarded (`run_strategy_lab.py:191-231`, `_score_tactic`). Nothing prevents
persisting it; it simply isn't.

---

## 3. Raw vs normalized — no, and no normalization population is recorded

**[READ]** The `score()` docstring (`strategy_score.py:30`) asserts components are "all expected in
~[-1,1] or [0,1] normalized form," but this is aspirational, not enforced:
- `excess_return_vs_spy` is a raw cumulative-window excess return (`run_strategy_lab.py:207`,
  `mean_excess = sum(excesses)/len(excesses)` over `run_backtest(...).metrics["excess_vs_spy"]`),
  unbounded above 1.0 for multi-year windows. **[RUN]** live range is `-0.1388` to `0.7078`
  (`shadow_boom_bucket`), i.e., +70.8% mean excess return.
- `worst_max_drawdown` is a raw fraction (e.g. `-0.500161`), fits `[-1,0]` incidentally.
- `overfit` is a raw excess-return gap (§1 Finding D), observed at `2.009588` — 2x the documented
  ceiling.

There is **no cross-sectional normalization** at all: the composite is not z-scored, percentile
ranked, or rescaled against the other 25 tactics in the same run. Each tactic's score is an
absolute weighted sum of its own raw metrics; nothing records a "normalization population"
(which tactics / which date) because no population-relative normalization occurs — the phrase in
the docstring describes an intent that the code does not implement.

**Direction-of-goodness** is recorded only as inline code comments (`strategy_score.py:40`
`# ≤ 0` for drawdown, `line 56` `# center at 0.5` for pbeat, `line 57` `# less drawdown → higher`)
and by which arithmetic operator (`+`/`-`) each weight uses at the composite (lines 54-64) — it is
not captured as structured, machine-checkable metadata (e.g., no `direction: "higher_better"` field
per component in code or in the persisted artifact).

---

## 4. Missing data — confirmed zero-substitution on a lower-is-better component, and it is not hypothetical

`strategy_score.py:47-52`:

```python
overfit = components.get("overfit")
if overfit is None:
    overfit_val = 0.0
    flags.append("overfit_unknown")
else:
    overfit_val = max(0.0, float(overfit))
```

`overfit_penalty` weight is `0.8` — the largest penalty weight and second-largest weight overall.
Substituting `0.0` for "unknown" means an *unvalidated* tactic is scored as if it had **zero**
IS/OOS degradation — the best possible value for a lower-is-better component. This exactly matches
the failure mode named in the audit brief: **missing data is rewarded, not penalized or excluded.**

**[RUN]** confirmed against the live artifact — this is not a corner case, it's the norm:

```python
>>> set(r.get('overfit') for r in rows)
{None, 2.009588}
```

25 of 26 tactics have `overfit: null` (walk-forward is only run for one hardcoded tactic family —
`_walk_forward_results()`, `run_strategy_lab.py:173-188`, calls `walk_forward()` solely for
`MomentumRotation`/`research_momentum_rotation`; no other tactic is ever walk-forward validated).
Those 25 all receive the zero-penalty treatment. The **one** tactic that *was* actually validated —
`research_momentum_rotation` — is the only one penalized by `overfit_penalty`, and because
`overfit=2.009588` is far outside the intended `[0,1]` scale (§1 Finding D), the penalty
`-0.8 * 2.009588 = -1.608` drags it to **dead last** of all 26 (`strategy_score = -1.1565`, next
worst is `benchmark_spy` at `-0.4850`, a `0.67` gap — by far the largest gap in the leaderboard).

**Net effect, stated plainly:** the only tactic in the leaderboard that underwent genuine
out-of-sample validation ranks last, specifically *because* it was validated and found to have a
large IS/OOS gap — while every tactic that was never checked for overfitting sits above it,
credited with a clean bill of health it never earned. The composite currently rewards *absence of
scrutiny* over presence of (unfavorable) scrutiny. The GUI caption
(`gui_v2/templates/dashboard/strategy_lab.html:336`) reads "OOS ✗ = overfit in walk-forward
(ranked down)" — true only for the single validated tactic, and silent on the fact that the other
25 avoid any equivalent scrutiny entirely.

`overfit_unknown` is appended to `flags` (`strategy_score.py:50`) but **[RUN]** confirmed via grep
that this flag is never read by `strategy_lab_health.py`, never surfaced in the GUI template
(`gui_v2/templates/dashboard/strategy_lab.html` renders `strategy_score` and `academic_basis` only
— no `flags` field appears), and never asserted on in any test outside
`test_strategy_score.py::test_overfit_unknown_flagged` (which only checks the flag is *present*,
not that it changes downstream trust/health/ranking treatment).

Also confirmed no other component silently coerces to zero in a way that inflates score:
`has_research` defaults `False→0.0` (line 42, correctly the "no bonus" direction), `turnover`,
`tax_drag`, `concentration`, `leverage` all default to `0.0` via `.get(key, 0.0)`
(lines 43-46) — all of these are *penalty* terms where `0.0` is also the best-case value, but
because they are always computed (never `None`, §1 Findings B/C) rather than optionally missing,
this is a separate "cheap/constant proxy" problem (§1), not a missing-data substitution bug. The
`overfit` field is the only component with an explicit `None`-sentinel path, and it is the single
largest-weighted term.

---

## 5. Scale — the 1→5 gap is a small fraction of total spread, and is dwarfed by the reconstruction residual

**[RUN]** across all 26 scores:

```
min score:  -1.1565  (research_momentum_rotation — the one overfit-validated tactic, §4)
max score:   1.7474  (research_vol_managed)
full spread: 2.9039
```

Rank 1→5: `1.7474, 1.7132, 1.7092, 1.6693, 1.6498` → gaps `0.0342, 0.0040, 0.0399, 0.0195`,
**total span rank1→rank5 = 0.0976**, i.e. **3.4% of the full 26-tactic spread**.

Consecutive-rank gap distribution (26 sorted scores, all 25 gaps):

```
0.0342 0.0040 0.0399 0.0195 0.0003 0.0000 0.0090 0.0000 0.0000 0.0522
0.0042 0.0125 0.0188 0.0673 0.0237 0.0021 0.0657 0.3170 0.1980 0.1569
0.2302 0.2512 0.0192 0.7065 0.6715
```

Three exact 0.0000 ties exist (ranks 6-7, 8-9, 9-10 — `profile_long_term_compounding` /
`profile_tax_aware` at `1.6495`, and `shadow_actual_baseline` / `shadow_engine_followed` /
`shadow_discovery_enhanced` all at `1.6405`), consistent with those tactics sharing identical
target-weight construction rather than a coincidence — but it also means the composite has zero
discriminating power among them; any of the three could be presented as "the" #8 tactic.

The gap structure is **bimodal**: ranks 1-18 are tightly clustered (gaps mostly `<0.07`, several
`≈0`), then a discontinuity opens from rank ~18 onward (gaps `0.32, 0.20, 0.16, 0.23, 0.25, 0.02,
0.71, 0.67`) as scores fall into `benchmark_spy` and `research_momentum_rotation` territory — i.e.,
the wide spread (`2.90`) is driven almost entirely by the bottom of the table, not by meaningful
separation at the top.

**How much of the rank 1→5 gap (0.0976) is plausible noise:** the audit found **no error bars, no
confidence interval, and no noise/uncertainty estimate anywhere in the pipeline** for
`strategy_score` (see §6) — so this cannot be answered quantitatively from the artifact as it
stands. What can be stated: the §2 reconstruction residual (missing `turnover`/`concentration`/
`leverage` contribution) is `0.15–0.51` per tactic — i.e., **the unmodeled/unpersisted component of
each individual score is 1.5x–5x larger than the entire gap that separates rank 1 from rank 5**.
Whatever the "true" noise band is, the known-and-quantifiable reconstruction gap alone is already
large enough to plausibly re-order the top 5.

---

## 6. Rank stability — no sensitivity or perturbation analysis exists

**[RUN]** grep across `portfolio_automation/`, `tests/`, and `docs/` for
`sensitivit|perturb` in strategy-lab context: **zero hits**. There is no code path that:
- re-scores tactics under alternate weight sets and reports rank churn,
- bootstraps/resamples the 4 windows (`trailing_1y/3y/5y/ytd`) to get a score confidence interval,
- jitters the raw metrics by a plausible estimation-error band and reports how often the top-5
  ordering changes.

`cfg["scoring_weights"]` (§1) is technically pluggable, which means a sensitivity sweep *could* be
built by calling `run_strategy_lab.run_strategy_lab(..., )` (weights currently come only from
`config.json`, not as a `run_strategy_lab()` parameter — an operator wanting to test alternate
weights today would have to edit `config.json` and rerun the whole lab each time) or by calling
`strategy_score.score()` directly per-row with the same components dict and swept weights (this
*is* reproducible today for the weighting axis alone, since `_score_tactic` computes and discards
`components` per §2 — a sweep would need that dict restored/persisted first).

**What would be needed to run one:** (a) persist the full `components` dict per tactic (currently
discarded, §2) so weight-sensitivity can be replayed without rerunning backtests; (b) a
resampling/error-bar source for `mean_excess_vs_spy`/`worst_max_drawdown` (currently point
estimates over 4 fixed windows, `run_strategy_lab.py:194-204`) to test metric-noise sensitivity;
(c) an explicit sweep harness over `DEFAULT_WEIGHTS` (none exists; `rank()` at
`strategy_score.py:71-73` is a pure sort with no variant-comparison mode).

The absence itself is the finding required by the brief.

---

## 7. Health coupling — `strategy_lab_health.py` treats "calculated" as "trustworthy," with narrow exceptions

`assess_strategy_lab_health()` (`strategy_lab_health.py:77-151`) computes `status` from:
- **RED** only if `status=="ok"` and `leaderboard` is empty (`looks_fresh_but_empty`, line 106) —
  i.e., RED fires on *absence* of scores, never on *quality* of scores.
- **AMBER** on: `insufficient_data` (line 108-109), staleness `>8` days (line 110-111),
  `coverage_complete is False` from the catalog i.e. missing `academic_basis` (lines 114-117),
  any row with `still_works_oos is False` explicitly surfaced (lines 120-124), missing factor data
  (lines 127-129), and a stale `active_strategy_selection` (lines 131-134,
  `check_active_strategy_selection`, lines 46-74).
- **GREEN** (lines 142-144) otherwise, with the literal reason string:
  `"lab healthy: ran, populated, documented, no failing-OOS tactic surfaced"`.

**Confirmed gap:** GREEN's criteria never reference `flags` (so `overfit_unknown` on the #1-ranked
tactic — true for all top ranks in the live data, §2 output — does not affect status), never
reference the reconstruction/component-persistence gap (§2), never reference score clustering/tie
density (§5), and never reference rank stability (§6, because no such signal exists to reference).
A leaderboard exactly like the current live one — top tactic `research_vol_managed`, `overfit_unknown`
flag, un-persisted turnover/concentration/leverage inputs, three-way score ties — reports **GREEN**
today under this logic (all rows have `academic_basis` or a documented `source`, so
`coverage_complete=True`; `research_momentum_rotation` is present with `still_works_oos=True` so it
doesn't trip the `failing_oos` AMBER path even though its raw `overfit=2.009588` is what sank its
own score). So: **yes — a successfully-calculated score currently implies a trustworthy ranking in
the health verdict.** The health check verifies the *pipeline ran and wrote data*, not that the
*composite's internal assumptions hold* (component scale, missing-data treatment, or
rank-separation-vs-noise).

---

## 8. Tests — assert internal math and pipeline plumbing, not cross-artifact ranking validity

`tests/portfolio_sim/test_strategy_score.py` (5 relevant tests, all pure unit tests on
`score()`/`rank()` with hand-built component dicts, no artifact involved):
- `test_higher_excess_scores_higher` — monotonicity in `excess_return_vs_spy` only.
- `test_overfit_penalizes` — `overfit=0.8` scores lower than `overfit=0.0`, **within the documented
  `[0,1]` range** — does not test the actual observed value of `2.009588`, so the scale-mismatch
  finding (§1 Finding D, §4) is untested.
- `test_overfit_unknown_flagged` — flag present when `overfit` key absent; does **not** assert
  anything about the resulting score being favorable/best-case relative to a known-bad case (the
  §4 finding is entirely unguarded by this test).
- `test_penalties_reduce_score` — sanity check that turnover/tax_drag/concentration/leverage=1.0
  each reduce score vs a baseline; does not test the live binary-proxy values (`0.3`/`0.7`) or the
  constant `tax_drag=0.0`.
- `test_rank_orders_desc` — `rank()` sorts descending on 3 synthetic floats; no connection to
  reconstructed/component-derived scores.

`tests/portfolio_sim/test_strategy_lab_e2e.py`:
- `test_e2e_leaderboard_ranked_by_score` — asserts the *persisted* `strategy_score` list is sorted
  descending (internal self-consistency of `_write`/`rank`), **not** that those scores match a
  recomputation from persisted components. No test in the repo performs the §2 reconstruction.
- `test_walk_forward_artifact_and_overfit` — asserts `research_momentum_rotation` is *present* in
  the leaderboard; does not assert anything about its relative rank or the asymmetry with
  un-validated tactics.

`tests/portfolio_sim/test_strategy_lab_health.py` (read in full, 60 lines): 5 tests
(`test_absent_is_amber`, `test_disabled_is_amber`, `test_fresh_but_empty_is_red`,
`test_healthy_green`, `test_failing_oos_is_amber`) — all fixture-based, all cover the status-machine
branches in §7 exactly as coded. None constructs a fixture with `overfit_unknown` flags present on
the top tactic, none constructs a fixture with score ties, and none asserts that GREEN requires any
form of ranking-integrity check beyond the fields already enumerated in §7. **No test anywhere in
the repo verifies that leaderboard ordering matches a reconstruction from stored raw fields** — this
was the central question of the workstream and it is unguarded.

---

## Summary of confirmed findings (all verified by reading code and/or running snippets against the live artifact; none inferred without evidence cited above)

1. `strategy_score.py:12-68` — 10-term weighted sum, `overfit_penalty=0.8` is the largest penalty
   weight; weights come from `config.json portfolio_sim.strategy_lab.scoring`, which is currently
   **absent** — every live score used unmodified `DEFAULT_WEIGHTS`.
2. **Not reproducible from the persisted artifact.** `turnover`, `concentration`, `leverage` are
   computed (`run_strategy_lab.py:210-212`) and fed into `score()`, but never written to
   `strategy_leaderboard.json`, even though `score()` itself already returns them in a
   `components` dict (`strategy_score.py:68`) that `_score_tactic` discards
   (`run_strategy_lab.py:191-231`). Reconstruction residual: `0.15–0.51` per tactic.
3. No cross-sectional normalization occurs; "normalized ~[-1,1]/[0,1]" in the docstring is
   aspirational. `overfit`'s real range (`0` to `2.0096` observed) is 2x its documented ceiling.
4. **Confirmed zero-substitution reward for missing data.** `overfit=None → 0.0` penalty
   (`strategy_score.py:47-52`); 25/26 live tactics have `overfit=null` and pay zero penalty; the
   1/26 tactic that was actually walk-forward-validated is penalized into last place, partly
   because of the scale mismatch in Finding D.
5. Rank 1→5 span (`0.0976`) is `3.4%` of the full 26-tactic spread (`2.9039`) and is smaller than
   the per-tactic reconstruction residual (`0.15–0.51`, item 2). Three exact score ties exist.
6. No sensitivity/perturbation analysis exists anywhere in the repo (`grep` confirmed zero hits);
   would require persisting `components` (item 2) plus a weight-sweep harness and a metric-noise
   model, none of which currently exist.
7. `strategy_lab_health.py` GREEN status is silent on score-composition quality (component
   persistence, flags, ties, overfit scale) — it verifies the pipeline ran and populated fields,
   not that the ranking is internally defensible. The live artifact would report GREEN today.
8. No test reconstructs leaderboard order from persisted fields; `test_overfit_penalizes` only
   exercises `overfit` within its documented `[0,1]` range, never the observed `2.0096`; no health
   fixture includes `overfit_unknown` on a top-ranked row.

No files were modified during this audit.
