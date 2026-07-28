# E5 Follow-ups — Defect Report

Branch `fix/e5-followups`, off `main` @ `02ced4fc`. Advisory-only system; no
broker order path touched.

## Defect 1 — incomplete memo header fix + literal-list regression guard

### Root cause

`8686898d` fixed 3 of the 6 headers `portfolio_automation/capital_plan_view.py`
can emit via `h(...)` in `render_capital_plan_md`. Two headers sitting inside
conditional blocks were still unmapped in `gui_v2/data/dash_memo.py`
`_HEADER_MAP`:

- `"Funded Market Opportunities"` (line 868) — only emitted when a funded
  action is BUY-class (`high_conf_starter` / `lower_conf_or_extended`) *and*
  has entry-setup data available.
- `"Sell and Funding Dependencies"` (line 905) — only emitted when the
  decision plan has at least one SELL/TRIM row.

The regression guard added alongside the first fix
(`tests/test_gui_dashboard_memo.py::test_no_shipped_memo_header_is_orphaned`)
used a hand-maintained `shipped_headers` literal that only listed the 3
headers the author happened to observe — structurally identical to the bug
it was meant to catch.

### Fix

1. `gui_v2/data/dash_memo.py`: added both headers to `_HEADER_MAP`, mapped to
   **Portfolio Decisions** — same section as the other capital-plan blocks
   (Today's Capital Plan / What To Do Today / Deferred Recommendations),
   since both are capital-action content (funded-position entry guidance and
   sell/funding-dependency detail), not a separate concern. Updated the
   module docstring's section-mapping summary to match.
2. `tests/test_gui_dashboard_memo.py`: added `_capital_plan_view_headers()`,
   which parses `inspect.getsource(capital_plan_view.render_capital_plan_md)`
   with `ast` and extracts every string literal passed to `h(...)` —
   including ones inside `if` blocks that never execute for any single
   fixture. `SHIPPED_CAPITAL_PLAN_HEADERS` (used by the parametrized
   Portfolio-Decisions test) and the `shipped_headers` list inside
   `test_no_shipped_memo_header_is_orphaned` are now both derived from this
   function instead of hand-typed, for the capital_plan_view.py portion of
   the header set (the module implicated in both bugs). The remaining,
   non-capital-plan headers in the orphan test (Risk Delta, Advisor Stack,
   etc.) come from other producer modules without an equivalent extractor
   and are out of scope here.
3. Added `test_capital_plan_view_headers_nonempty` (extraction sanity check)
   and `test_orphan_guard_detects_a_removed_mapping` — a meta-test that
   temporarily strips all `"Portfolio Decisions"` entries from `_HEADER_MAP`
   and asserts the derived header list is then reported as orphaned. This
   proves the guard actually fails when the mapping regresses, rather than
   being a tautology that always passes.

### Producer header → section verification

Extracted via `_capital_plan_view_headers()` (AST over `render_capital_plan_md`):

| Header (as emitted by `h(...)`) | Maps to |
|---|---|
| `Today's Capital Plan` (×2 call sites: unavailable-state + main path) | Portfolio Decisions |
| `What To Do Today` | Portfolio Decisions |
| `Funded Market Opportunities` | Portfolio Decisions (**newly mapped**) |
| `Deferred Recommendations` | Portfolio Decisions |
| `Sell and Funding Dependencies` | Portfolio Decisions (**newly mapped**) |
| `Bottom Line` | Top Insight (closing verdict — intentionally not grouped with capital-plan content) |

Guard-failure demonstration (`test_orphan_guard_detects_a_removed_mapping`):
with all `"Portfolio Decisions"` entries removed from `_HEADER_MAP`, the
derived header list reports `Today's Capital Plan`, `What To Do Today`,
`Funded Market Opportunities`, `Deferred Recommendations`,
`Sell and Funding Dependencies` as orphaned — proving the guard is load-bearing.

## Defect 2 — weight proposer blind to expectancy

### Root cause

`portfolio_automation/retune_suggestions.py::_propose_weight_changes` gated
`auto_applicable` on hit-rate delta (`vs_baseline_pp`), sample size, delta
magnitude, and significance — never on `mean_return_1d`. A tag could win
more often than baseline while losing money on average and still receive
`auto_applicable: True` with a proposed weight **increase**. This path is
armed (`config.json backtesting.auto_apply.enabled=true`).

`mean_return_1d` (and `resolved_1d`, its backing sample count) is already
computed per-tag in `portfolio_automation/pattern_learning.py::_finalize`
and present in `pattern_efficacy_monthly.json`'s `by_tag` stats — it just was
never read by the proposer. No return-dispersion/variance statistic
(std/CI on `mean_return`) is computed upstream; only the mean and its
resolved-sample count are available, so only those are surfaced (no value is
invented).

### Fix

1. Every weight proposal now always carries `mean_return_1d`,
   `mean_return_resolved_n`, `expectancy_available`, `expectancy_contradiction`,
   and a human-readable `expectancy_note`, folded into `rationale`. This is
   visible for every proposal regardless of gate outcome, including ones that
   remain auto-applicable.
2. `auto_applicable` now additionally requires `expectancy_available` (i.e.
   `mean_return_1d is not None`) and `not expectancy_contradiction`, where
   `expectancy_contradiction = proposed_delta > 0 and mean_return_1d < 0`
   (a weight-increasing proposal whose tag actually loses money). Missing
   mean-return data is **never** imputed as `0.0` — it fails closed
   (`expectancy_available=False` blocks auto-apply outright, distinct from
   `expectancy_contradiction`).
3. This only removes existing `auto_applicable=True` rows; it can never
   create a new one, since it's added as an additional `and` clause on top
   of the pre-existing guardrails. Blocked proposals stay fully visible in
   `weight_proposals` with the contradiction/missing-data reason stated —
   nothing is dropped, only its auto-apply eligibility.
4. `render_retune_suggestions_md` table gained a "Mean return (1d)" column
   and a warning line under the table for any proposal flagged
   `expectancy_contradiction`.
5. `gate_proposal` (`_propose_promotion_gate`, the confidence-threshold
   proposal) was intentionally left untouched — the defect report and grep
   both scope this to `_propose_weight_changes`; it does not read
   `mean_return_1d` per-tag in the same way and was out of scope.

### Before / after on the REAL current artifact

Read `/opt/stockbot/outputs/latest/pattern_efficacy_monthly.json` as input
(same efficacy window the real `gate_retune_suggestions.json` was built
from) and re-ran `build_retune_suggestions`:

| Parameter | Δ (weight) | mean_return_1d | auto_applicable BEFORE | auto_applicable AFTER | Changed? |
|---|---|---|---|---|---|
| `sanitation_weight.theme` | −0.0037 | +0.4610 | True | True | no |
| `sanitation_weight.hit_rate` | +0.0011 | +0.4701 | True | True | no |
| `sanitation_weight.fmp` | +0.0258 | +0.7778 | False (n=103 < 200) | False | no |
| `sanitation_weight.sources` | +0.0106 | +0.6313 | True | True | no |
| `gate_proposal` (confidence_threshold, out of scope) | — | — | True | True | no |

**auto_applicable_count: 4 → 4 (unchanged).** All four tags currently backing
weight proposals in the live artifact carry a *positive* `mean_return_1d`, so
no existing proposal's status flips today — the specific failure mode
(positive hit-rate delta + negative expectancy) isn't present in this
window. The fix closes the gap for the next window in which it is (verified
by the new unit tests, which construct exactly that scenario and confirm the
gate blocks it while keeping the proposal visible).

## Tests

- `tests/test_gui_dashboard_memo.py`: 3 new tests
  (`test_capital_plan_view_headers_nonempty`,
  `test_orphan_guard_detects_a_removed_mapping`, plus the parametrized
  `test_capital_plan_headers_map_to_portfolio_decisions` now covers 2
  additional header cases via the derived list).
- `tests/test_retune_suggestions.py`: 5 new tests in `TestExpectancyGate`.

Test commands run (targeted, no full suite per instructions):
```
/opt/stockbot/.venv/bin/python -m py_compile portfolio_automation/retune_suggestions.py gui_v2/data/dash_memo.py tests/test_retune_suggestions.py tests/test_gui_dashboard_memo.py
/opt/stockbot/.venv/bin/python -m pytest -q tests/test_gui_dashboard_memo.py tests/test_retune_suggestions.py tests/test_retune_impact_tracker.py tests/test_pattern_learning.py tests/test_allocation_engine_tactical_retune.py tests/test_gui_retune_impact_card.py tests/test_scraped_intel_tuning.py tests/test_tuning_proposals.py tests/test_weight_tuning.py
```

Result: 254 passed, 4 failed. All 4 failures are **pre-existing** and
unrelated (verified via `git stash` against unmodified HEAD):
- `test_tuning_proposals.py::test_clear_edge_yields_bounded_nonzero_proposal`,
  `::test_delta_clamped_to_max_abs_delta` — fail identically on unmodified
  `main`/HEAD (unrelated arithmetic in a different module).
- `test_gui_dashboard_memo.py::test_memo_route_all_six_section_headings_present`,
  `::test_memo_route_has_stacked_sections` — fail because this worktree has
  no `outputs/latest/daily_memo.md` (route renders the empty state); fail
  identically on unmodified HEAD.

Zero new failures from either fix.
