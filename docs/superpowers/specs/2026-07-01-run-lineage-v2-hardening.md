# Spec: Run Lineage v2 hardening (bounded)

**Status:** PLANNED — bounded follow-on to the SQG program (v1 shipped + merged
to `main` @ `7fdb7430`, 2026-07-01). Observe-only / additive; no scoring, no
`decision_engine.py` changes, no production-behavior change.
**Owner gate:** implement autonomously once scoped; stop at the production
(merge) boundary for explicit go, per the standing operating rule.

## Context — what v1 delivered

- `run_manifest.py`: `begin_run` (status=`running`) at Stage 00, `complete_run`
  (status=`complete`) at Stage 14; `is_complete` / `coherent_run_ids` guards;
  atomic `safe_write`. Manifest at `outputs/policy/run_manifest.json`.
- `contracts.lineage()` canonical block; `decision_plan.json` now stamped with
  `run_id` + `lineage` (2026-07-01, `main._write_decision_engine_outputs`).
- One manifest per run, overwritten in place; single `run_id` scheme
  (`<date>_<mode>_official`).

## v2 scope (exactly three items — do not expand)

### 1. Unique attempts
Today a re-run on the same date reuses/overwrites the same `run_id`
(`<date>_<mode>_official`), so two attempts on one day are indistinguishable in
lineage. Add a monotonic **attempt suffix** (e.g. `…_official.a2`) or an
`attempt` integer + `attempt_started_at` in the manifest, so each `begin_run`
after the first same-day run is uniquely identifiable. `coherent_run_ids` and
the `decision_plan` stamp must carry the attempt-qualified id. Deterministic:
attempt number derived from existing manifest history, not a clock/random.

### 2. Immutable manifest history
Today `write_manifest` overwrites `run_manifest.json` in place — prior runs'
identities are lost. Append each completed (or failed) manifest to an
**append-only** `outputs/policy/run_manifest_history.jsonl` (one line per
attempt), keeping `run_manifest.json` as the "current" pointer. Never rewrite a
prior line (retain-failed-experiments discipline). Add a reader + a
`daily_run_status`/wiring surface for the last N attempts.

### 3. `complete_with_warnings` semantics
Today status is binary `running` → `complete` (and an implicit failure path).
Add a third terminal state **`complete_with_warnings`**: the run finished and
produced a decision plan, but ≥1 non-fatal aux stage degraded (e.g. a
`run_aux_stage` caught exception, a content_liveness warn, a degraded input in
`daily_input_snapshot`). `complete_run` should accept an optional
`warnings: list[str]` and set the status accordingly; `is_complete` stays true
for both `complete` and `complete_with_warnings`, but consumers (daily-check,
lineage `quality`) can distinguish a clean run from a degraded-but-complete one.

## Non-goals / guardrails

- No change to `decision_engine.py`, scoring, or any `*_score` semantics.
- No change to what feeds `decision_plan.json` (lineage stays additive metadata).
- Deterministic + idempotent; no future-date leakage; no outcome overwrite.
- Ships with tests (healthy + degraded fixtures) per the Analysis+Health
  Coverage Requirement, and a `daily_run_status` / registry surface for the new
  fields so nothing is produced-but-unconsumed.

## Suggested sequence

1. Immutable history (append-only jsonl + reader + test) — lowest risk, no id-scheme change.
2. `complete_with_warnings` (extend `complete_run` + `is_complete` + lineage `quality` mapping + test).
3. Unique attempts (id-scheme change — touches `coherent_run_ids` + the `decision_plan` stamp; do last, most cross-cutting).
