# Strategy-Lab Approval → Active-Strategy Re-anchor — Design

**Date:** 2026-06-24
**Status:** Approved (operator, 2026-06-24)
**Scope:** Additive, sandbox-only, human-gated. Never touches `decision_plan.json`, `config.json`, or `signal_registry.yaml`.

## Problem

The Strategy Lab tab already loads `strategy_review_queue.json` (8 ranked strategy
profiles, each carrying `allowed_actions: [approve_strategy_for_review,
reject_strategy, defer_strategy]`), but the queue is **display/copy-only**. There is
no POST endpoint that records a decision, and the contract sink
`strategy_decisions.jsonl` has never been written (`event_log_idle`). The governance
tab's `POST /dashboard/governance/decide` → `record_approval()` is the pattern to
mirror.

## Decision (operator-selected)

Approving a strategy **selects/activates** it: the sandbox projection + comparison
**re-anchor** on the approved strategy's tactic weights. Sandbox only — never feeds
`decision_plan.json`.

- **Timing:** re-anchor **immediately** on approval — the POST handler persists the
  selection AND re-runs the sandbox projection/comparison synchronously, behind a
  recompute guard.
- **Cardinality:** **single active strategy**; a new approve **supersedes** the prior.
- **Fans:** keep **both** the `shadow_actual_baseline` fan AND the selected strategy's
  fan (baseline retained as reference, not dropped).

## Architecture

### 1. Artifacts (2)

- **`outputs/policy/active_strategy_selection.json`** — NEW, `OutputNamespace.POLICY`,
  `replace`. Shape:
  ```json
  {
    "observe_only": true, "no_trade": true,
    "active_strategy_id": "risk_parity_lite", "name": "Risk Parity (lite)",
    "approved_by": "operator", "approved_at": "<iso>",
    "status": "approved", "supersedes": "<prev id or null>"
  }
  ```
  Registered in `artifact_registry.yaml` (`required:false`, `severity:info`,
  `role:advisor`). When no strategy is active, the file is absent or
  `active_strategy_id:null`.
- **`outputs/policy/strategy_decisions.jsonl`** — EXISTING contract (`OutputNamespace.POLICY`,
  `append`, `role:telemetry`). One line per decision:
  ```json
  {"ts":"<iso>","strategy_id":"...","decision":"approve|reject|defer","approver":"operator","prev_active":"<id or null>"}
  ```

### 2. Re-anchor read-points

Both producers read `active_strategy_selection.json` (fallback to baseline when
absent/null):

- **`run_portfolio_projection`** (`portfolio_automation/portfolio_sim/run_portfolio_projection.py`):
  if `active_strategy_id` maps to a tactic in `all_static_tactics`, add that tactic's
  fan to `anchor_fan` and set `anchor_strategy_id` in the payload. The
  `shadow_actual_baseline` fan is still emitted. With no/invalid selection, behaviour
  is unchanged (baseline-only anchor). This makes the re-anchor **durable** — the next
  weekly run reads the same selection and will not revert it.
- **`strategy_comparator`** (`portfolio_automation/strategy/strategy_comparator.py`):
  marks the matching row `operator_selected: true` in `strategy_comparison.json` and
  `strategy_review_queue.json`.

### 3. GUI (mirror governance)

- **`POST /dashboard/strategy-lab/decide`** in `gui_v2/app.py`. Form fields:
  `strategy_id`, `decision ∈ {approve, reject, defer}`. Guards:
  - `is_human_approver(approver)` (reuse `sim_governance/schemas.py`) — AI/non-human
    rejected with 400.
  - `strategy_id` must exist in the current `strategy_review_queue.json` — else 400.
  - Always append the decision to `strategy_decisions.jsonl`.
  - **approve:** write `active_strategy_selection.json` (supersede prior) → synchronous
    recompute (projection + comparison) behind the guard.
  - **reject:** if `strategy_id` is the active one, clear the selection
    (`active_strategy_id:null`); always logged.
  - **defer:** log only.
  - Redirect `303 → /dashboard/strategy-lab`.
- **Template** (`gui_v2/templates/dashboard/strategy_lab.html` + loader
  `gui_v2/data/dash_next_stage.py:collect_strategy_lab_view`): per-row
  Approve/Reject/Defer buttons gated on `allowed_actions`; an "Active: `<id>`" banner;
  an `operator_selected` badge on the active row.

### 4. Recompute guard

The synchronous recompute (projection ≈ 4s @ n_paths=5000, + comparison) is wrapped in
`try/except` with a soft wall-clock cap (~20s). On raise/timeout, **the selection is
still persisted** and the tab shows "Approved — effective next sim run." A failed
recompute never loses the decision (non-blocking by construction).

### 5. Safety invariants

- `observe_only:true` + `no_trade:true` on both artifacts.
- Runtime no-trade-verb assertion on the selection writer.
- Never writes `decision_plan.json`, `config.json`, or `signal_registry.yaml`.
- AI cannot self-approve (human-approver guard).
- Reuses `OutputNamespace.POLICY` (no new namespace — avoids the `get_policies()` gap).

### 6. Analysis + health pairing (CLAUDE.md requirement)

Strategy Lab is weekly-cadence → extend **`/strategy-lab-analysis`**: surface the
active selection + recent `strategy_decisions.jsonl` activity; content-liveness check —
if `active_strategy_id` no longer exists in the review queue, flag AMBER (stale
selection).

## Tests

1. approve → persists selection + logs decision + projection re-anchors (anchor fan
   includes selected tactic, `anchor_strategy_id` set).
2. approve supersedes a prior active selection (`supersedes` recorded).
3. reject/defer → log only, active selection unchanged.
4. reject of the currently-active strategy → clears the selection.
5. AI / non-human approver → 400, no write.
6. invalid `strategy_id` (not in queue) → 400, no write.
7. projection falls back to `shadow_actual_baseline` when no/invalid selection
   (unchanged behaviour).
8. recompute failure is non-fatal — selection still persisted.
9. comparator marks `operator_selected:true` on the active row.

## Out of scope (YAGNI)

- Multiple simultaneous active strategies.
- Production overlay / allocation effect (this is the sandbox-activate variant, not the
  promotion-lane variant).
- Background/async recompute (immediate synchronous recompute chosen).
