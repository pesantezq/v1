# Simulation Governance — Daily AI/Product Review

## Purpose

`portfolio_automation/sim_governance/daily_ai_review.py` runs exactly ONE
consolidated AI/product review per day covering BOTH advisory and watchlist
candidates. The call's cost is estimated *before* it is made: within the daily
cap it runs the single review; over the cap it SKIPS and writes a deferred-review
artifact. A once-per-day guard prevents a second call on the same date.

---

## Two-Lane Governance

The review classifies each candidate as `reject` / `continue_testing` /
`ready_for_production_review`. It can only **recommend** readiness — the result
hardcodes `ai_can_approve_production: false`, and `ready_for_production_review`
only triggers a *pending* proposal downstream. Human approval is the production
gate. Writes to the PROMOTION_REVIEW namespace only; never touches production or
score semantics.

---

## Artifacts Written (OutputNamespace.PROMOTION_REVIEW → `outputs/promotion_review/`)

| File | Contents |
|------|----------|
| `daily_ai_review_result.json` | Verdicts + counts (when the review runs) |
| `daily_ai_review_deferred.json` | Written only when the cost estimate exceeds the cap |

The review also records an AI usage event (via `ai_budget`) so spend appears in
the AI budget summary + GUI. The heuristic fallback is free (recorded cost 0); an
injected LLM reviewer records the estimated spend.

---

## Key Functions

- `run_daily_ai_review(packet, now, *, base_dir, daily_cost_cap_usd=0.50,
  provider="openai", model="gpt-4o-mini", estimated_completion_tokens=600,
  reviewer=None, force=False, write_files=True) -> dict` — orchestrates the
  once-per-day guard, the cost gate, the review, the usage-event record, and the
  result write. `reviewer` is an injectable `packet -> list[verdict-dict]` seam
  (defaults to the deterministic `heuristic_reviewer`); `force` bypasses the
  daily guard for manual re-runs.
- `heuristic_reviewer(packet) -> list[dict]` — conservative deterministic
  classifier: only clean, high-confidence (`>=0.80`), low/medium-risk, `ok`
  data-quality candidates already flagged ready are recommended for production
  review; `<0.30` confidence is rejected; everything else keeps testing.

---

## Tests

Covered under `tests/` with the sim-governance suite
(`python -m pytest -q tests -k sim_governance`).
