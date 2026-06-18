# Simulation Governance — Production Overlays

## Purpose

`portfolio_automation/sim_governance/production_overlays.py` is the **production**
side of the two-lane model. Each loader takes a baseline (what production would do
on its own) and applies ONLY the approved-proposal overlay artifacts written by
`production_application`. They are pure transforms at the INPUT boundary — exactly
like the broker-overlay pattern — and never call or modify `decision_engine` or
scoring logic.

---

## Two-Lane Governance

Each loader is gated by a config flag (default **OFF**), so turning on live
production effect is the final, explicit human step. When the flag is off the
loader is a no-op and returns the baseline unchanged. What the loaders ignore by
construction: raw simulation artifacts, pending proposals, rejected proposals,
and simulation-only items — they read only the approved overlay files (those
carrying `feeds_production: true`). Protected score semantics
(`signal_score` / `confidence` / scoring fields) are never modified; advisory ops
only touch context/annotation and ranking-hint fields.

---

## Inputs / Outputs

- **Inputs:** a baseline watchlist (list) or baseline advisory (list of picks),
  `base_dir`, and the `enabled` flag.
- **Reads:** `outputs/latest/approved_watchlist_proposals.json` and
  `outputs/latest/approved_advisory_proposals.json`.
- **Output:** the merged production view (does not itself write artifacts).

---

## Key Functions

- `load_production_watchlist(baseline_watchlist, *, base_dir, enabled) -> dict` —
  baseline + approved overlay when enabled; returns `{watchlist, ranks, tags,
  applied_proposal_ids, overlay_enabled}`.
- `apply_approved_watchlist(baseline_watchlist, overlay) -> dict` — pure
  add/remove/rank/tag application.
- `load_production_advisory(baseline_advisory, *, base_dir, enabled) -> dict` —
  baseline + approved overlay when enabled; returns `{advisory,
  applied_proposal_ids, overlay_enabled}`.
- `apply_approved_advisory(baseline_advisory, overlay) -> dict` — pure
  context/ranking-hint/strategy annotation (writes `overlay_context`,
  `overlay_rank_hint`, `overlay_strategy`, `overlay_proposal_id`).
- `_load_overlay(filename, base_dir) -> dict` — reads an overlay only when it
  declares `feeds_production`.

---

## Tests

Covered under `tests/` with the sim-governance suite
(`python -m pytest -q tests -k sim_governance`).
