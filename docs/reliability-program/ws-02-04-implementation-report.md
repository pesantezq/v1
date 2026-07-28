# WS2/WS4 Implementation Report — Explicit OOS States + Split Health Dimensions

Branch: `feat/ws2-oos-states-health` (off `main`). Scope: `portfolio_automation/portfolio_sim/`
(`oos_state.py` new, `walk_forward.py`, `run_strategy_lab.py`, `strategy_lab_health.py`) +
tests + two consumer-doc updates. `decision_engine.py`, protected score semantics,
`strategy_score` values/ranking, and the active-strategy/Strategy-Lab sandbox boundary
were not touched.

## What changed

### WS2 — explicit OOS states (`portfolio_automation/portfolio_sim/oos_state.py`, new)

- `OOSState` enum: `OOS_NOT_TESTED`, `OOS_INSUFFICIENT`, `OOS_MIXED`, `OOS_SUPPORTED`,
  `OOS_FAILED`, `OOS_DATA_BLOCKED`.
- `classify_oos_state(wf_entry)` — pure classifier over a `walk_forward()` result dict
  (or `None`). `None`/`no_params`/`insufficient_data` never fall through to a passing
  state. `MIN_FOLDS_FOR_SUFFICIENCY = 4` gates `OOS_INSUFFICIENT`. A single fold
  controlling the aggregate (`one_fold_controls_result`) downgrades an otherwise-passing
  result to `OOS_MIXED`.
- `legacy_still_works_oos(state)` — the ONLY place the legacy tri-state boolean is
  computed; it is derived FROM the state (`OOS_SUPPORTED`→True, `OOS_FAILED`→False,
  else→None), never the reverse.
- `build_oos_evidence(tactic_id, wf_entry)` — structured per-tactic record: folds, fold
  construction, train/test period months, embargo/purge rule (`"none"` — a genuine
  finding, not an omission), distinct test dates/weeks, distinct regimes (absent —
  walk_forward.py has no regime concept), benchmark comparison, OOS return/excess
  return/drawdown, IS→OOS degradation, confidence interval (absent — none is computed
  anywhere in the Lab), `survives_costs`/`tax_note: "gross_until_cost_model"`, and
  `one_fold_controls_result`. Absent fields are `None` with the state carrying the
  reason — nothing is fabricated.
- `walk_forward.py` gained purely additive fields to back the evidence record:
  `oos_mean_return`, `oos_mean_drawdown`, `one_fold_controls_result`,
  `distinct_test_dates`, `distinct_test_weeks`. **`is_mean_excess`, `oos_mean_excess`,
  `oos_hit_rate`, `is_oos_gap`, `overfit`, and `still_works_oos` are computed exactly as
  before** — same values, same formulas — so `strategy_score` is unaffected.
- `run_strategy_lab.py`'s `_score_tactic` now persists `oos_evidence` per leaderboard
  row and derives `still_works_oos` from it (previously read directly off the raw
  walk-forward dict — same practical values today, now routed through one classifier).

### WS4 — split health dimensions + fail-closed roll-up (`strategy_lab_health.py`)

Nine independent dimensions, each `{status, evidence[], reasons[]}`:
`runtime_health`, `artifact_completeness`, `documentation_coverage`,
`data_admissibility`, `statistical_sufficiency`, `oos_validity`, `ranking_credibility`,
`governance_compliance`, `presentation_consistency`.

Roll-up: **RED if any dimension RED; else AMBER if any dimension AMBER; else GREEN.**
A dimension can only report GREEN if it carries non-empty `evidence` — enforced by the
`_dim()` constructor, which silently downgrades an evidence-free GREEN to AMBER rather
than trust the caller (see `test_green_dimension_without_evidence_is_impossible`).

`oos_validity` is the fix for the headline defect: it requires **at least one tactic to
reach `OOS_SUPPORTED`** to be GREEN. `failing_oos == []` (nothing surfaced as failed) no
longer implies GREEN — if zero tactics are `OOS_SUPPORTED`, the dimension is AMBER with
reason `no_credible_oos_test`, regardless of how many are merely untested.

Also emitted: `known_limitations[]` (static, e.g. "walk-forward runs 1 hardcoded
tactic", "no multiple-comparison correction", "no CI", "gross returns", "no
embargo/purge gap") and `blocking_reasons[]` (every reason from every non-GREEN
dimension, when overall isn't GREEN).

Spec corollaries verified:
- documentation complete + insufficient OOS evidence ⇒ not fully GREEN — see
  `test_documentation_complete_plus_oos_insufficient_is_not_fully_green`.
- no failing-OOS + no credible OOS test ⇒ AMBER not GREEN — see
  `test_zero_tested_tactics_with_failing_oos_empty_yields_amber_not_green` (the direct
  regression test for `strategy_lab_health.py:121`'s old `is False` bug).
- fresh artifacts + invalid timestamp ⇒ RED — `_dim_runtime_health` returns RED on an
  unparsable `created_at`.
- cron succeeded + empty output ⇒ RED/AMBER — `looks_fresh_but_empty` stays RED
  (unchanged from the pre-existing behavior), `insufficient_data`/stale stay AMBER.

### Gate (default ON)

`strict_rollup_gate(root)` resolves, in order: kill-switch file
`config/strategy_lab_strict_health.DISABLED` → env
`STOCKBOT_STRATEGY_LAB_STRICT_HEALTH_DISABLED=1` → config
`portfolio_sim.strategy_lab.health.strict_oos_rollup_enabled` (now set `true` explicitly
in `config.json`) → default `True`. When disabled, `assess_strategy_lab_health` returns
`_assess_legacy(...)` — the pre-WS4 algorithm, byte-for-byte, including the known bug —
so disabling is an exact-reproduction rollback path only, not a partial relaxation.
Every returned dict (strict or legacy) carries a `gate: {strict_oos_rollup_enabled,
source}` block. Legacy top-level keys (`status`, `reasons`, `signals`, and all prior
`signals` sub-keys: `lab_status`, `tactic_count`, `age_hours`, `coverage_complete`,
`walk_forward_present`, `failing_oos`, `factor_data_available`, `top_tactic`,
`top_score`, `top_excess_vs_spy`, active-strategy fields) resolve unchanged under both
gate states.

### Docs touched (consumers)

- `.claude/commands/strategy-lab-analysis.md` — Step 2/3 rewritten for the 9-dimension
  roll-up, the gate, and the "AMBER is intended, don't re-flag" note.
- `.claude/commands/monthly-tool-analysis.md` — one line updated to the new AMBER/GREEN
  criteria and the "as of 2026-07-28 real leaderboard is AMBER, this is intended" note.

## Files created
- `portfolio_automation/portfolio_sim/oos_state.py`
- `tests/portfolio_sim/test_oos_state.py`
- `.superpowers/audit/ws-02-04-implementation-report.md` (this file)

## Files modified
- `portfolio_automation/portfolio_sim/walk_forward.py`
- `portfolio_automation/portfolio_sim/run_strategy_lab.py`
- `portfolio_automation/portfolio_sim/strategy_lab_health.py`
- `tests/portfolio_sim/test_strategy_lab_health.py`
- `config.json` (added `portfolio_sim.strategy_lab.health.strict_oos_rollup_enabled: true`)
- `.claude/commands/strategy-lab-analysis.md`
- `.claude/commands/monthly-tool-analysis.md`

No changes to `decision_engine.py`, `_TRACKED_KNOBS`, any protected score field, or any
`strategy_score` value/ranking. `run_portfolio_backtest.py`, `strategy_score.py`,
`research_library.py`, `tactics.py` untouched.

## Tests added

`tests/portfolio_sim/test_oos_state.py` (12 tests): classification for
not-tested/data-blocked/insufficient-folds/missing-aggregate-fields/supported/failed/
one-fold-dominance-downgrade/straddling-sign, plus evidence-record shape for the
not-tested and supported cases (no fabricated fields; genuine fields populated).

`tests/portfolio_sim/test_strategy_lab_health.py` (14 tests, up from 4): all four
original tests retained (`test_absent_is_amber`, `test_disabled_is_amber`,
`test_fresh_but_empty_is_red` unchanged; the old `test_healthy_green` /
`test_failing_oos_is_amber` fixtures now live on as
`test_gate_disabled_reproduces_legacy_green_verdict` /
`test_gate_disabled_via_config_reproduces_legacy_amber_for_failing_oos`, proving the
gate's rollback path reproduces the exact old verdict), plus 10 new tests covering every
item in the task's required list:
- `test_null_still_works_oos_maps_to_oos_not_tested_and_does_not_pass`
- `test_zero_tested_tactics_with_failing_oos_empty_yields_amber_not_green` (headline
  regression test)
- `test_documentation_complete_plus_oos_insufficient_is_not_fully_green`
- `test_green_dimension_without_evidence_is_impossible` +
  `test_every_green_dimension_in_a_real_verdict_has_evidence`
- `test_legacy_consumer_keys_still_resolve`
- `test_gate_disabled_reproduces_legacy_green_verdict` +
  `test_gate_disabled_via_config_reproduces_legacy_amber_for_failing_oos` +
  `test_gate_enabled_by_default`
- `test_failing_oos_tactic_is_amber_with_oos_failed_state`,
  `test_at_least_one_supported_tactic_can_reach_green` (positive control)

## Test commands run

```
.venv/bin/python -m py_compile portfolio_automation/portfolio_sim/oos_state.py \
    portfolio_automation/portfolio_sim/walk_forward.py \
    portfolio_automation/portfolio_sim/run_strategy_lab.py \
    portfolio_automation/portfolio_sim/strategy_lab_health.py
.venv/bin/python -m pytest -q tests/portfolio_sim/ tests/test_walk_forward.py tests/test_gui_strategy_lab_research.py
```

## Test results

`163 passed, 2 warnings` (warnings are pre-existing FastAPI `on_event` deprecation
notices, unrelated to this change). Full suite was NOT run per task constraints.

## Verification against the REAL repo state (2026-07-28)

Ran both the new strict assessor and the gate-disabled legacy path against the live
`outputs/sandbox/*.json` artifacts (26 tactics, 1 walk-forward-tested).

**OLD status (gate disabled / pre-WS4 algorithm): `GREEN`**
```json
{
  "status": "GREEN",
  "reasons": [
    "lab healthy: ran, populated, documented, no failing-OOS tactic surfaced"
  ],
  "signals": {
    "lab_status": "ok", "tactic_count": 26, "age_hours": 0.1,
    "coverage_complete": true, "walk_forward_present": true, "failing_oos": [],
    "factor_data_available": true, "active_strategy_id": "defensive_capital_preservation",
    "strategy_decisions_count": 4, "top_tactic": "Volatility-Managed",
    "top_score": 1.7474, "top_excess_vs_spy": 0.566893
  },
  "gate": {"strict_oos_rollup_enabled": false, "source": "env_kill_switch"}
}
```

**NEW status (strict rollup, default ON): `AMBER`**
```json
{
  "status": "AMBER",
  "reasons": [
    "data_admissibility: missing_price_history_warning:['missing_price_history:ES,ISRG,JPM,OS']",
    "statistical_sufficiency: only 1/26 tactics have sufficient-fold OOS evidence (walk-forward is wired to 1 hardcoded tactic today)",
    "ranking_credibility: top-ranked tactic 'Volatility-Managed' (tactic_id=research_vol_managed) has OOS state OOS_NOT_TESTED — ranking is not yet corrected for OOS evidence or selection bias (see .superpowers/audit/ws-02-03-oos-selection.md WS3)"
  ],
  "legacy_status": "GREEN",
  "gate": {"strict_oos_rollup_enabled": true, "source": "config"}
}
```

**Every dimension's status:**

| Dimension | Status | Evidence / Reason |
|---|---|---|
| `runtime_health` | GREEN | "leaderboard status=ok, 26 tactics scored at 2026-07-28T19:48:03.061196+00:00" |
| `artifact_completeness` | GREEN | "all 4 lab artifacts present: ['catalog', 'factor', 'leaderboard', 'walk_forward']" |
| `documentation_coverage` | GREEN | "coverage_complete=true, 0 undocumented tactics" |
| `data_admissibility` | AMBER | "missing_price_history_warning:['missing_price_history:ES,ISRG,JPM,OS']" |
| `statistical_sufficiency` | AMBER | "only 1/26 tactics have sufficient-fold OOS evidence (walk-forward is wired to 1 hardcoded tactic today)" |
| `oos_validity` | GREEN | "research_momentum_rotation: OOS_SUPPORTED" |
| `ranking_credibility` | AMBER | "top-ranked tactic 'Volatility-Managed' (tactic_id=research_vol_managed) has OOS state OOS_NOT_TESTED — ranking is not yet corrected for OOS evidence or selection bias (see .superpowers/audit/ws-02-03-oos-selection.md WS3)" |
| `governance_compliance` | GREEN | "observe_only=sandbox_only=no_trade=true; no stale active-strategy selection" |
| `presentation_consistency` | GREEN | "still_works_oos agrees with derived OOS state for all 26 tactics" |

**`blocking_reasons` (verbatim):**
```json
[
  "data_admissibility: missing_price_history_warning:['missing_price_history:ES,ISRG,JPM,OS']",
  "statistical_sufficiency: only 1/26 tactics have sufficient-fold OOS evidence (walk-forward is wired to 1 hardcoded tactic today)",
  "ranking_credibility: top-ranked tactic 'Volatility-Managed' (tactic_id=research_vol_managed) has OOS state OOS_NOT_TESTED — ranking is not yet corrected for OOS evidence or selection bias (see .superpowers/audit/ws-02-03-oos-selection.md WS3)"
]
```

**`oos_state_counts` (per-tactic classification across the real leaderboard):**
```json
{"OOS_NOT_TESTED": 25, "OOS_SUPPORTED": 1}
```

Note that `oos_validity` itself is GREEN — the one genuinely walk-forward-tested
tactic (`research_momentum_rotation`) really is `OOS_SUPPORTED` (11 folds,
`oos_mean_excess=0.1110>0`, `oos_hit_rate=0.6364≥0.5`, no single fold dominating), which
matches the audit's confirmation that the underlying walk-forward test is genuine, not
fabricated. The overall verdict is AMBER because `statistical_sufficiency` (only 1/26
tactics tested) and `ranking_credibility` (the top-ranked tactic by `strategy_score` is
`research_vol_managed`, which is untested) independently fail — i.e., the roll-up
correctly distinguishes "the one test we ran is trustworthy" from "the leaderboard as a
whole is OOS-validated," which the old collapsed verdict conflated.

## Assumptions

- `MIN_FOLDS_FOR_SUFFICIENCY = 4` and `ONE_FOLD_DOMINANCE_SHARE = 0.5` are conservative,
  documented floors chosen to be easy to clear once walk-forward is extended to more
  tactics — not a claim of statistical power. Revisit alongside any future
  multiple-comparison-correction work (WS3).
- `statistical_sufficiency`'s ≥50%-of-tactics-tested bar and `data_admissibility`'s
  treatment of `missing_price_history` warnings as AMBER (not RED) are new judgment
  calls not explicitly specified by the task; both are documented in the module
  docstring and covered by tests.
- `governance_compliance` treats an artifact missing `observe_only`/`sandbox_only`/
  `no_trade` keys as AMBER ("unconfirmed"), and only an explicit `False` as RED
  ("breached") — absence is not fabricated into either a pass or a failure.

## Risks

- None identified that affect the decision core: this change touches only
  `portfolio_sim/` (sandbox-only Strategy Lab) health reporting and OOS evidence: no
  write path to `outputs/latest/decision_plan.json`, no change to `decision_engine.py`,
  and `strategy_score`/ranking values are numerically identical to before (verified via
  the unchanged `walk_forward.py` core computation and the full `tests/portfolio_sim/`
  suite passing, 146→163 tests all green).
- Downstream consumers that print the health `status` as a single headline (e.g. dashboards,
  cron summaries) will now show AMBER for the Strategy Lab where they previously showed
  GREEN — this is the intended, required consequence of the fix, not a regression, and is
  called out explicitly in the updated skill docs so it isn't mistaken for new breakage.

## VPS validation commands

This work was implemented and verified directly on the VPS (`/opt/stockbot`, this is the
production VPS per `CLAUDE.md`'s Operating Mode), so the test results above are real,
not a laptop-side prediction. For an operator to re-verify independently:

```bash
cd /opt/stockbot
.venv/bin/python -m py_compile portfolio_automation/portfolio_sim/oos_state.py \
    portfolio_automation/portfolio_sim/walk_forward.py \
    portfolio_automation/portfolio_sim/run_strategy_lab.py \
    portfolio_automation/portfolio_sim/strategy_lab_health.py

.venv/bin/python -m pytest -q tests/portfolio_sim/ tests/test_walk_forward.py tests/test_gui_strategy_lab_research.py

# Real-repo verdict, strict (default) vs legacy (gate disabled):
.venv/bin/python -c "
import json
from portfolio_automation.portfolio_sim.strategy_lab_health import assess_strategy_lab_health
print(json.dumps(assess_strategy_lab_health(root='.'), indent=2, default=str))
"
STOCKBOT_STRATEGY_LAB_STRICT_HEALTH_DISABLED=1 .venv/bin/python -c "
import json
from portfolio_automation.portfolio_sim.strategy_lab_health import assess_strategy_lab_health
print(json.dumps(assess_strategy_lab_health(root='.'), indent=2, default=str))
"
```

## Recommended next step

Per `.agent/project_state.yaml:next_official_step` — check
`python scripts/agent_context_check.py` before starting further roadmap work; this task
was scoped to the WS2/WS4 reliability items from
`.superpowers/audit/ws-02-03-oos-selection.md` and does not itself advance the main
roadmap sequence. Natural following work (not implemented here, out of scope): WS3's
multiple-comparison correction across the leaderboard, and extending
`_walk_forward_results` beyond the single hardcoded `research_momentum_rotation` tactic
so `statistical_sufficiency`/`ranking_credibility` have more than 1/26 tactics to draw
evidence from.
