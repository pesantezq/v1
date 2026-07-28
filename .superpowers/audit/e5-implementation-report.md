# Phase E5 — False-GREEN Adversarial Probe Suite — Implementation Report

Branch: `feat/e5-false-green-probes`, off `main` @ `ba73d62c`. Worktree:
`/opt/stockbot-worktrees/e5-false-green-probes`. Rollout class:
**validation-only** — no production code touched.

Note on report location: the task specified
`/opt/stockbot/.superpowers/audit/e5-implementation-report.md`. Per the
explicit worktree-isolation instruction ("do not `cd /opt/stockbot`"), this
report is committed at the equivalent path inside this worktree
(`.superpowers/audit/e5-implementation-report.md`) and will land at the
`/opt/stockbot` path once this branch merges, consistent with the two other
implementation reports already present in this directory
(`ws-01a-implementation-report.md`, `ws-02-04-implementation-report.md`).

## What was built

- `tests/probes/assertions.py` — 8 shared, reusable assertion helpers (raise
  `AssertionError` directly, generic over repo-specific types):
  `assert_meaningful_population`, `assert_nonzero_variance`,
  `assert_artifact_fresh_for_session`, `assert_decision_consumer_parity`
  (+ `extract_literal_header_calls` helper), `assert_oos_evidence_supported`,
  `assert_no_single_block_controls_result`,
  `assert_fail_closed_on_denial_state_corruption`,
  `assert_no_quality_screen_mislabeling`.
- `tests/probes/test_probe_population_and_variance.py` (9 tests) — scenarios 1, 4, 13.
- `tests/probes/test_probe_oos_evidence.py` (11 tests) — scenarios 5, 6, 17, 18.
- `tests/probes/test_probe_freshness_and_cron.py` (5 tests) — scenarios 7, 20.
- `tests/probes/test_probe_denial_state_integrity.py` (11 tests) — scenarios 8, 9, 10, 11, 12.
- `tests/probes/test_probe_consumer_parity.py` (8 tests) — scenarios 2, 3, 19.
- `tests/probes/test_probe_screening_and_regime.py` (8 tests) — scenarios 14, 15, 16.

**52 new tests, all passing.** No production code, `decision_engine.py`,
scoring logic, or `_TRACKED_KNOBS` touched.

## Scenario coverage table

| # | Scenario | Coverage | Fixed / Open | Pre-fix-fail evidence |
|---|---|---|---|---|
| 1 | Artifact exists but zero meaningful records | Covered | generic (n/a) | `test_generic_population_probe_catches_zero_record_artifact` — direct demonstration of the helper |
| 2 | Dashboard renders while omitting a decision-critical section | Covered | **Fixed** (4/6 headers, commit `8686898d`) + **NEW finding**: 2/6 headers still orphaned | `test_dashboard_omits_two_decision_critical_sections_from_full_render` proves live content loss today; `test_pre_fix_header_map_would_have_orphaned_all_capital_plan_headers` reproduces the original a5387a27 map |
| 3 | Test fixture uses obsolete headers | Covered | **Open residual** (see #2) | `test_renderer_derived_headers_are_not_a_stale_hardcoded_list` proves the *existing* `tests/test_gui_dashboard_memo.py` hardcoded list is itself stale/incomplete |
| 4 | Leaderboard ranks while all scores equal | Covered | Fixed (`f30433b1`, WS9) | `test_diagnose_ranking_pre_fix_had_no_diagnostic_at_all` — naive pre-fix health check reads the exact 31-row all-tied fixture as GREEN |
| 5 | OOS field claims true without sufficient folds | Covered | Fixed (`92176881`, WS2/B1) | `test_pre_fix_is_false_check_would_have_certified_the_untested_tactic` — reproduces the literal `is False` predicate |
| 6 | Complete documentation coexists with invalid statistics | Covered | Fixed (`92176881`, WS4/B2) | `test_pre_fix_legacy_algorithm_called_this_exact_shape_green` — calls the REAL `_assess_legacy` (byte-for-byte pre-fix code, kept for rollback) on the same fixture; GREEN vs `_assess_strict`'s AMBER |
| 7 | Stale weekly artifact consumed by a daily gate | Covered (honest-state) | **Open** (F8.2 — no reader-specific freshness concept) | n/a (open item); demonstrates registry cadence-window tolerance vs a session-aware reader need on the real `gate_retune_suggestions.json` registry row |
| 8 | Unreadable approval log blocks approval application | Covered | Fixed (WS10/WS11 family) | `test_pre_fix_empty_approval_set_would_have_reversed_production_silently` |
| 9 | Unreadable revocation log blocks overlay reconstruction | Covered | Fixed (WS10) | `test_pre_fix_corrupt_revocation_log_would_have_resurrected_MARA` |
| 10 | Torn trailing line distinguished from total corruption | Covered | Fixed (WS10) | `assert_fail_closed_on_denial_state_corruption` applied against 2 independent sibling logs (revocations, audit) via the SAME shared helper |
| 11 | Durable op disappears from today's proposal set | Covered | Fixed (WS10/11) | `test_pre_fix_no_carry_forward_would_have_dropped_the_durable_op` |
| 12 | Revoked op attempts to resurrect | Covered | Fixed (WS10/11) | `test_pre_fix_naive_approved_only_check_would_have_resurrected_it` |
| 13 | Experiment runs with no admissible input for weeks | Covered | Fixed (`b013b24c`, WS13; ships gated OFF) | `test_enrich_baseline_key_mismatch_would_have_produced_zero_forever` — reproduces the literal `.get("candidates")` mismatch against a real "decisions"-keyed artifact |
| 14 | Quality-screen bypass mislabeled as screened | Covered (honest-state + adversarial construction) | **Open** (F15.1, zero code shipped) | `test_no_screened_label_field_exists_anywhere_today_STILL_OPEN` confirms no such field exists; `test_hypothetical_mislabel_is_caught` proves the helper would catch it once one does |
| 15 | Regime-concentrated signal described as generally validated | Covered (honest-state) | **Open** (F14.1) | `test_single_value_collapse_detector_is_blind_to_two_label_concentration` + structural grep confirming zero validity-assessor regime awareness |
| 16 | Strong raw hit rate + negative benchmark-relative expectancy | Covered | **Open** (new structural finding in `retune_suggestions.py`) | `test_retune_suggestion_proposes_weight_increase_despite_negative_expectancy` — real function, real-shaped input, `auto_applicable: True` despite negative `mean_return_1d` in the same record |
| 17 | One observation/week controls a removal/verdict decision | Covered | Fixed (`ONE_FOLD_DOMINANCE_SHARE`/`one_fold_controls_result`) + open sibling noted (F16.1 age-based auto-close) | `test_pre_fix_naive_classifier_would_have_passed_the_fold_dominated_case` |
| 18 | Many tested tactics create a false leaderboard winner | Covered (honest-state) | **Open** (F3.2/F3.3, C5 not built) | `test_no_selection_bias_correction_exists_over_clustered_families_STILL_OPEN` — 26-tactic/8-family fixture, all OOS_SUPPORTED, roll-up GREEN with zero mention of family/correction anywhere |
| 19 | Memo, GUI and decision artifact disagree | Covered | generic + real cross-check | `test_decision_consumer_parity_catches_gui_drift` (generic) + `test_real_coherence_view_funded_capital_agrees_with_capital_plan_view` (real modules) |
| 20 | Cron exits zero after producing stale/semantically invalid output | Covered (honest-state) | **Open** (F8.1 — `coherent_run_ids()` dead code) | `test_coherent_run_ids_is_still_unwired_in_production_STILL_OPEN` — repo-wide call-site scan confirms zero production callers |

**20/20 scenarios covered** (12 guard already-fixed defects with verified
pre-fix failure; 8 document still-open gaps per the task's "assert the
honest state" instruction — none are "not feasible").

## Pre-fix-fail verification count

**16 of the 52 tests are explicit verify-by-construction reproductions**
that assert the PRE-FIX/naive behavior first (proving it would have
passed/succeeded incorrectly), then assert the current fixed behavior
correctly rejects the same input:

1. `test_enrich_baseline_key_mismatch_would_have_produced_zero_forever`
2. `test_diagnose_ranking_pre_fix_had_no_diagnostic_at_all`
3. `test_pre_fix_is_false_check_would_have_certified_the_untested_tactic`
4. `test_pre_fix_legacy_algorithm_called_this_exact_shape_green`
5. `test_pre_fix_naive_classifier_would_have_passed_the_fold_dominated_case`
6. `test_pre_fix_naive_read_would_have_silently_degraded_to_empty`
7. `test_pre_fix_empty_approval_set_would_have_reversed_production_silently`
8. `test_pre_fix_corrupt_revocation_log_would_have_resurrected_MARA`
9. `test_pre_fix_no_carry_forward_would_have_dropped_the_durable_op`
10. `test_pre_fix_naive_approved_only_check_would_have_resurrected_it`
11. `test_pre_fix_header_map_would_have_orphaned_all_capital_plan_headers`
12. `test_coherent_run_ids_correctly_detects_a_mixed_run` (function itself correct — the bug is non-invocation, verified separately)
13–16. The 4 `assert_fail_closed_on_denial_state_corruption`/guard-comparison
   tests in `test_probe_denial_state_integrity.py` each independently
   reproduce a naive try/except-swallow read and show it silently degrades
   where the real guard refuses.

Two additional tests (`_assess_legacy` calls) exercise the REAL pre-fix
algorithm verbatim rather than a hand reproduction — the strongest form of
verification available, since `strategy_lab_health.py` deliberately kept the
original buggy algorithm byte-for-byte for exact-rollback comparison.

## NEW defects found by this probe suite (not fixed, per instructions)

1. **`gui_v2/data/dash_memo.py:_HEADER_MAP` is still missing 2 of the 6
   headers `capital_plan_view.render_capital_plan_md` emits** —
   `"Funded Market Opportunities"` and `"Sell and Funding Dependencies"`.
   Their content (entry-setup guidance for funded market opportunities;
   sell-proceeds detail for pending sells) is silently dropped from
   `/dashboard/memo`, exactly the a5387a27 defect class that commit
   `8686898d` fixed for the other 4 headers — this is a residual instance,
   confirmed live by executing the real renderer + real GUI parser
   end-to-end (`test_dashboard_omits_two_decision_critical_sections_from_full_render`).
   The existing regression guard for this bug class
   (`tests/test_gui_dashboard_memo.py::test_no_shipped_memo_header_is_orphaned`)
   does not catch it because its own header list is a hand-maintained
   literal that was never updated to include these two headers — confirmed
   by `test_renderer_derived_headers_are_not_a_stale_hardcoded_list`.
2. **`retune_suggestions._propose_weight_changes` never reads `mean_return`**
   — a source tag with a strong positive hit-rate delta vs baseline gets an
   `auto_applicable: True` weight-INCREASE proposal even when that same
   tag's `mean_return_1d` (computed and available in the same
   `pattern_efficacy` record) is negative. Confirmed structurally
   (`test_propose_weight_changes_source_never_reads_mean_return_STILL_OPEN`)
   and functionally against the real `build_retune_suggestions` entry point.

Neither defect was fixed here (test-and-helpers change only, per task
instructions) — both are pinned by honest-state tests with clear
`_STILL_OPEN` naming and docstrings explaining exactly what a future fix
should do to this probe.

## Test evidence

```
tests/probes/                                     52 passed
tests/portfolio_sim/test_strategy_lab_health.py
tests/portfolio_sim/test_oos_state.py
tests/portfolio_sim/test_strategy_score.py
tests/test_audit_log_fail_closed.py
tests/test_revocation_log_fail_closed.py
tests/test_promotion_approvals_unreadable_log_guard.py
tests/test_promotion_approvals_concurrency.py
tests/test_overlay_watchlist_durability.py
tests/test_overlay_durability_hardening.py
tests/test_universe_sanitation.py
tests/test_daily_run_status.py
tests/test_ws13_discovery_adds_gate.py
tests/test_gui_dashboard_memo.py
tests/test_quant_watch_probes.py
tests/test_artifact_registry.py
tests/test_run_manifest.py
tests/test_retune_suggestions.py
tests/test_capital_plan_view.py                   399 passed, 3 failed (pre-existing, confirmed
                                                   unrelated — same 3 fail with tests/probes/
                                                   excluded from the run entirely)
```

The 3 pre-existing failures (`test_memo_route_all_six_section_headings_present`,
`test_memo_route_has_stacked_sections`, `test_sqg_registration_keeps_registry_green_and_debt_free`)
reproduce identically whether or not `tests/probes/` is included in the run —
confirmed not caused by this change.

Full suite was NOT run per task constraints (~4 min; only new + touched-area
tests run here).
