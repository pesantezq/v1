# GUI Improvements — Phase 2: Close Data-Surfacing Gaps

**Date:** 2026-07-09
**Status:** implemented
**Owner change scope:** `gui_v2/` presentation layer only. Observe-only consumer.
No decision/scoring/allocation/broker code. No artifact schema changes. No
recompute of any producer's analytical verdict.

---

## Program context

Phase 2 of the four-phase GUI program (see
`2026-07-08-gui-phase1-design-system-foundation-design.md`). Phase 1 built the
primitive layer; Phase 2 surfaces shipped-but-unrendered backend data using
those Phase-1 primitives (`status_card`, `section_header`, `badge`, tables).

## Gap audit (2026-07-09, full `outputs/latest/*.json` sweep)

An artifact→GUI-consumer audit found the three named gaps plus several more.
Ranked by operator value:

1. `memo_coherence.json` — fully unrendered (funding math, reconciliation,
   funded/deferred actions, coherence verdict).
2. `quant_watch_status.json active[]` — rendered as a count only.
3. `quant_feedback.json by_regime/by_crowd_state/by_strategy` — unrendered.
4. `decision_triage.json` — fully unrendered (45 decisions bucketed + top_actions).
5. `retune_impact.json` current-gauge attribution — not on the card.
6. `news_intelligence.json` — unrendered.
7. Discovery funnel cluster (`discovery_pulse_status`, `top100_*`,
   `theme_signals`, `watch_candidates`) — unrendered.
8. `pipeline_wiring_status.json` — unrendered (dev/ops value).

Already-closed (verified): weekly-deployment detail (portfolio Weekly Deployment
section, PR1) and the memo peak/recovery caveat (flows through `daily_memo.md` →
memo tab).

## Scope — this PR (quant-lens cluster)

Cohesive, single-tab (quant), high operator value, clean primitive fit:

- **A. Quant Watch active concerns** — one `status_card` per `active[]` probe
  (severity rail = probe severity) in a new "Active Quant Concerns" section,
  above the evidence grid. Shows the concern narrative, detector, and age.
- **B. quant_feedback breakdown** — `by_regime` / `by_crowd_state` /
  `by_strategy` as three compact tables ("Regime / Crowd / Strategy Breakdown").
  Rows sort by sample count; `hit_rate`/`mean_return` render "—" until outcomes
  resolve; an "Nu" suffix marks unresolved samples.
- **C. Retune Impact card** — append the current gauge's actual 1d hit-rate /
  mean-return / resolved-sample evidence from `outcome_attribution`. The
  vs-prior-gauge comparison verdict is **deliberately NOT recomputed** (it is
  owned by the memo producer's baseline-comparison logic — see
  `project_positive_red_favorable_baseline`); the card links to the memo instead.

### Scale invariant (C)

`hit_rate_1d` is a fraction (0.66 → 66%); `mean_return_1d` is already a percent
(0.998 → +1.00%). They are formatted differently and a test pins both.

## Deferred (with rationale)

- `decision_triage`, `memo_coherence` full surface, `news_intelligence` →
  **Phase 3 per-tab deep-dives** (Today → Portfolio → Memo). These are
  operator-workflow surfaces that belong inside those tabs' redesigns, not bolted
  onto quant.
- `pipeline_wiring_status`, discovery funnel → **System-tab follow-up** (dev/ops
  lens, lower daily-trading value).

## Invariants preserved

- Consumer, never author: reads existing artifacts; writes nothing; recomputes
  no verdict.
- Observe-only: no trade verbs; quant cards are evidence-only.
- Every figure sourced: every new section names its artifact via
  `section_header`'s tag / `source_artifacts`.
- Honest degradation: absent artifacts → empty list / "—"; unresolved samples
  are labeled, never shown as a fabricated zero hit-rate.

## Health-check pairing

Features are GUI consumers of existing producers (`quant_watch_status`,
`quant_feedback`, `retune_impact`) — those already have quant-lens agents
(`portfolio-attribution-analyst`, `portfolio-learning-loop-health`) and daily
coverage. The render is covered by `portfolio-render-reviewer`. No new producer,
so no new health check required; the corollary (every artifact has a consumer) is
advanced, not regressed.

## Tests

- `tests/test_gui_quant_watch_detail.py` (3)
- `tests/test_gui_quant_feedback_breakdown.py` (4)
- `tests/test_gui_retune_impact_card.py` (3)
