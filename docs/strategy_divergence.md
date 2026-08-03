# strategy_divergence

Last verified against `portfolio_automation/strategy/strategy_divergence.py`
(added `86150e7b`, 2026-07-28) and `.claude/commands/strategy-lab-analysis.md`.
Last updated 2026-08-03.

## Purpose

Observe-only, sandbox-scoped **comparison of the human-approved active strategy
against the Strategy Lab leaderboard's #1-ranked tactic**.

Reliability-Program WS5 (`docs/reliability-program/ws-04-05-14-18-health.md`)
found that the persistent gap between `active_strategy_selection.json` and the
leaderboard's top row was recorded *nowhere*: no artifact compared them, no
health check gated on the gap, and the operator had no way to learn why the
active strategy had not moved, whether the top tactic even carried
out-of-sample support, or whether promoting it was currently possible at all.
This module makes the divergence — and the evidence behind it — explicit and
inspectable.

## Observe-Only Behavior

A pure, read-only **producer**. Verified against the source: it never writes
`decision_plan.json`, never touches `config.json` or `signal_registry.yaml`,
never calls `record_strategy_decision` / `record_auto_strategy_anchor`, and
never changes the active strategy. Its only write is one JSON artifact in
`OutputNamespace.SANDBOX`.

Every payload (including the degraded one) hardcodes
`observe_only: true`, `sandbox_only: true`, `no_trade: true`,
`artifact_only: true`, plus a `disclaimer` string stating that it never feeds
`decision_plan.json` or the production decision engine.

`compute_strategy_divergence()` never raises — missing or unparsable inputs
return a degraded dict instead. `write_strategy_divergence()` likewise returns
rather than raising; callers still wrap it in `try`/`except` per the repo's
non-blocking convention.

## Classification

Exactly one of five labels, **fail-closed toward the least flattering label
when evidence is ambiguous**. `classify_divergence(...)` is a pure decision
function; the first matching rule wins, in this precedence order:

| # | Rule | Label |
|---|---|---|
| 1 | `active_strategy_id` no longer appears in the current review queue | `STALE_ACTIVE_STRATEGY` |
| 2 | `rank_difference == 0` — active *is* the top tactic | `EXPECTED_POLICY_DIVERGENCE` |
| 3 | An explicit recorded policy reason exists | `EXPECTED_POLICY_DIVERGENCE` |
| 4 | Top tactic is `OOS_FAILED` — retaining the active strategy is the evidence-backed call | `EXPECTED_POLICY_DIVERGENCE` |
| 5 | Top tactic has not reached `OOS_SUPPORTED` (untested / data-blocked / insufficient folds / mixed) | `INSUFFICIENT_EVIDENCE` |
| 6 | Top tactic *is* `OOS_SUPPORTED` but is not in the review-queue candidate universe — structurally unpromotable, so nothing can be "pending" | `UNEXPLAINED_DIVERGENCE` |
| 7 | Top tactic is `OOS_SUPPORTED`, in the queue, and a human decision is outstanding | `PENDING_REVIEW` |
| 8 | Otherwise — validated evidence exists, promotion is possible, nothing explains the gap | `UNEXPLAINED_DIVERGENCE` |

Rule 1 comes first because a ranking comparison against a stale anchor is not
meaningful. Rule 5 is the fail-closed default: **"ranked #1" is not the same as
"validated"**, and the classifier must not be loosened to reach a more
comfortable label.

## Artifacts

| File | Path | Namespace |
|------|------|-----------|
| JSON | `outputs/sandbox/strategy_divergence.json` | `OutputNamespace.SANDBOX` |

There is no Markdown companion. The artifact is **not registered in
`portfolio_automation/artifact_registry.yaml`** and has no declared cadence,
because nothing writes it on a schedule (see
[Invocation](#invocation-not-cron-wired)).

### JSON contract (`status: "ok"`)

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | str | `"1"` |
| `generated_at` | ISO str | Caller-supplied `now` or UTC now |
| `observe_only`, `sandbox_only`, `no_trade`, `artifact_only` | bool | Always `true` |
| `source` | str | `"strategy_divergence"` |
| `status` | str | `"ok"` \| `"degraded"` |
| `disclaimer` | str | Fixed safety string |
| `active_strategy` | obj | `{strategy_id, tactic_id, name, rank, score}` — `tactic_id` resolved via `strategy_selection.resolve_anchor_tactic_id` |
| `top_ranked_tactic` | obj | `{tactic_id, name, rank: 1, score}` |
| `rank_difference` | int \| null | `active_rank - 1`; null when the active strategy is absent from the leaderboard |
| `score_difference` | float \| null | `top_score - active_score`, rounded to 6dp |
| `ranking_confidence` | obj | `{level: "low"\|"moderate", reasons[]}` — **qualitative by design**; there is no calibrated confidence model here, so no number is fabricated |
| `top_tactic_oos` | obj | Full OOS evidence block from `portfolio_sim/oos_state.build_oos_evidence` (`state`, folds, embargo rule, CI, degradation, `tax_note`, …) |
| `regime_suitability` | obj | `{status: "not_computed", reason}` — leaderboard rows carry no regime dimension anywhere in the Strategy Lab today (confirmed absent, not merely unread); `outputs/regime/regime_performance.json` is a separate, unjoined artifact (WS14) |
| `turnover_impact` | obj | `active_turnover` / `top_turnover` raw `score_decomposition` components + a note that no live rebalance-cost simulation is run |
| `tax_impact` | obj | `active_tax_drag` / `top_tax_drag` raw components + a note that both are pre-tax-cost-model (`tax_note='gross_until_cost_model'`, WS2) |
| `drawdown_comparison` | obj | `{active_worst_max_drawdown, top_worst_max_drawdown, delta}` |
| `reason_active_unchanged` | str \| null | First classification reason, in plain language |
| `promotion_consideration` | obj | `{should_consider: bool, rationale: str}`; `should_consider` is true only for `UNEXPLAINED_DIVERGENCE` / `PENDING_REVIEW` |
| `structural_unpromotability` | obj | `{blocked: bool, reason: str, review_queue_profiles: [str]}` |
| `last_human_decision` | obj \| null | Last line of `outputs/policy/strategy_decisions.jsonl`: `{ts, strategy_id, decision, approver, source}` |
| `classification` | str | One of the five labels |
| `classification_reasons` | [str] | Human-readable justification |

`structural_unpromotability` is the field that answers "why hasn't the top
tactic just been promoted": Strategy-Lab research/shadow tactics are not
members of the 8 fixed `SEED_PROFILES` in `strategy_review_queue.json`, so a
human **cannot** approve one as the active strategy via the existing decide
route (`POST /dashboard/strategy-lab/decide`) without first widening the review
queue's candidate set.

### JSON contract (`status: "degraded"`)

Emitted instead of a fabricated comparison when a required input is missing.
Shape: the same safety envelope plus `status: "degraded"` and a `reason`
string. Triggers:

| Trigger | `reason` |
|---|---|
| `outputs/sandbox/strategy_leaderboard.json` absent/unparsable | `"… absent/unparsable -- nothing to compare"` (also carries `active_strategy_id`) |
| Leaderboard present but `leaderboard: []` | `"… present but empty (looks_fresh_but_empty)"` |
| No active selection recorded | `"no active strategy selection recorded (active_strategy_id is null/absent)"` |

## Inputs (read-only)

| Path | Used for |
|---|---|
| `outputs/policy/active_strategy_selection.json` | `active_strategy_id`, `name`, `policy_reason` |
| `outputs/sandbox/strategy_leaderboard.json` | ranked rows, `strategy_score`, `flags`, `score_decomposition`, `worst_max_drawdown` |
| `outputs/sandbox/walk_forward_results.json` | per-tactic OOS evidence |
| `outputs/latest/strategy_review_queue.json` | the promotable candidate universe |
| `outputs/policy/strategy_decisions.jsonl` | decided strategy ids + the last human decision |

Note: `policy_reason` (rule 3's escape hatch) is **not populated by any writer
today** — it exists so a deliberate, documented divergence can be recorded, but
nothing writes it yet.

## Module API

| Function | Role |
|---|---|
| `classify_divergence(*, rank_difference, active_in_queue, top_tactic_oos_state, top_tactic_in_queue, has_pending_promotion_proposal, explicit_policy_reason) -> (str, list[str])` | Pure decision function; returns one of `CLASSIFICATIONS` plus reasons. |
| `compute_strategy_divergence(root=".", now=None) -> dict` | Reads the inputs above and returns the payload (or the degraded dict). No writes. |
| `write_strategy_divergence(root=".", now=None, base_dir=None) -> dict` | Computes, then persists via `safe_write_json(OutputNamespace.SANDBOX, …)`. `base_dir` defaults to `<root>/outputs` — the convention `get_output_path` expects — and can be redirected (tests). |

`CLASSIFICATIONS` is exported as the tuple of the five valid labels.

## Invocation (not cron-wired)

**Verified by grep, 2026-08-03: no cron script, `main.py` stage, or
`run_daily_safe.sh` / `run_weekly_safe.sh` stage calls
`write_strategy_divergence`.** Consistent with that, no
`outputs/sandbox/strategy_divergence.json` exists on the production VPS — the
artifact is computed **on demand, in memory**.

The single consumer is the `/strategy-lab-analysis` skill
(`.claude/commands/strategy-lab-analysis.md`), which invokes the pure computer
directly:

```bash
.venv/bin/python -c "import json; from portfolio_automation.strategy.strategy_divergence import compute_strategy_divergence; print(json.dumps(compute_strategy_divergence(root='.'), indent=2, default=str))"
```

The skill reads `classification`, `rank_difference`, `top_tactic_oos.state`,
and `structural_unpromotability`, and is instructed to surface
`structural_unpromotability.blocked` **verbatim regardless of classification**.
Per that skill: this artifact only ever reports — it never re-anchors anything,
and `UNEXPLAINED_DIVERGENCE` is flagged for operator attention without any
auto-dispatch.

If the artifact is ever wanted on disk, `write_strategy_divergence()` is the
entry point, and it should then be given an `artifact_registry.yaml` row plus a
cadence so the registry validator can audit it.

## Config flags

None. There is no enable/disable gate and no feature flag — the module is
inert-by-construction (read-only, sandbox namespace) rather than
inert-by-flag.

## Current live state

`compute_strategy_divergence(root='.')` on the production VPS, 2026-08-03:

| Field | Value |
|---|---|
| `classification` | `INSUFFICIENT_EVIDENCE` |
| `active_strategy` | `defensive_capital_preservation` (`profile_defensive_capital_preservation`), rank 26, score −0.6971 |
| `top_ranked_tactic` | `research_dual_momentum` ("Dual Momentum"), rank 1, score 1.2149 |
| `rank_difference` / `score_difference` | 25 / 1.912 |
| `top_tactic_oos.state` | `OOS_NOT_TESTED` |
| `ranking_confidence.level` | `low` — 1/26 leaderboard tactics reach `OOS_SUPPORTED`; top tactic carries `flags=['overfit_unknown']` |
| `structural_unpromotability.blocked` | `true` |

This is the honest, non-regression state: the top-ranked tactic has never been
walk-forward tested, so there is not yet enough evidence to say the divergence
*should* be resolved — only that it exists. It is **not** something to "fix" by
loosening the classifier. The path forward is extending walk-forward coverage
beyond the single hardcoded tactic key in `run_strategy_lab.py`'s
`_walk_forward_results`.

(The E1 implementation report captured `research_vol_managed` as #1 on
2026-07-28; the leaderboard's top row moves as the Strategy Lab re-runs. The
classification has stayed `INSUFFICIENT_EVIDENCE` because the OOS gap, not the
identity of the top tactic, is what drives it.)

## Health pairing

`/strategy-lab-analysis` (quant lens) owns the check: it reads the
classification and triages `INSUFFICIENT_EVIDENCE` as report-don't-alert,
`STALE_ACTIVE_STRATEGY` as "matches `strategy_lab_health`'s existing
`stale_active_strategy_selection` signal — resolve that first", and
`UNEXPLAINED_DIVERGENCE` as operator attention. The artifact is sandbox /
observe-only and **never RED** on its own.

## Tests

```
.venv/bin/python -m pytest -q tests/test_strategy_divergence.py
# 19 passed
```

Covers each precedence rule of `classify_divergence` (including that it always
returns one of the five labels), a real-shape regression fixture resolving to
`INSUFFICIENT_EVIDENCE`, the no-divergence and stale-active paths, the
`PENDING_REVIEW` path, both degraded triggers, and that
`write_strategy_divergence` persists an artifact **only** (no selection or
decision state is mutated).

## See also

- `docs/reliability-program/ws-04-05-14-18-health.md` — the WS5 finding.
- `docs/RESEARCH_STRATEGY_LAB.md` — the leaderboard and `strategy_score` this
  compares against.
- `docs/SIM_GOVERNANCE.md` — the human-gated promotion workflow the
  `structural_unpromotability` field describes the boundary of.
