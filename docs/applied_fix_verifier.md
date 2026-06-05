# Applied-Fix Verifier

Last verified against `portfolio_automation/applied_fix_verifier.py` and
`.claude/commands/daily-tool-analysis.md`. Last updated 2026-05-30.

## Purpose

Close the loop on fixes the `daily-tool-analysis` skill ships in response to a
run's findings. The skill records each fix into
`data/daily_check_state.json:applied_fixes`; on the next run this module
re-checks every fix against today's artifacts and classifies it, so a fix that
silently regresses is caught and a fix that held stops being re-flagged.

## Observe-Only Behavior

Pure and read-only. `verify_applied_fixes` READS artifacts and RETURNS verdicts;
it writes no artifact of its own. There is no `observe_only` output field because
the module emits no output artifact — its result is consumed in-process by the
skill. A bad `verify` spec or unreadable artifact degrades to `pending`, never
an exception.

## Verdict Classes

| Status | Meaning |
|---|---|
| `confirmed` | The fix's expected post-condition is observed. Skill prunes it from state. |
| `regressed` | The original symptom is back (fix did not hold). Blocks GREEN; dispatches discovery-health. |
| `pending` | Not yet observable (e.g. artifact predates the fix, or needs ≥1 prior day of data). |
| `manual` | No automated check; operator/agent must eyeball. |

## `verify` Spec Kinds

- **`liveness_row_not_warn`** `{row, regression_below_observed}` — reads
  `outputs/latest/daily_run_status.json:content_liveness`. `regressed` if the
  named row warns at `observed <= regression_below_observed` (old threshold
  back); `confirmed` if the row is `ok`; `pending` if the row warns above the
  new threshold (a genuine miss, not a regression) or is missing.
- **`artifact_max_field_gt`** `{artifact, list_path, field, threshold}` —
  `confirmed` if `max(field)` across the artifact's dotted `list_path` exceeds
  `threshold`; otherwise `pending`. **Never emits `regressed`** — a zero reading
  cannot distinguish "fix broke" from "first day of data".
- **`file_contains`** `{path, contains?, absent?}` — for a **text** artifact
  (e.g. `outputs/latest/daily_memo.md`) whose fix post-condition is a rendered
  string, not a JSON field. `contains`/`absent` may each be a string or a list.
  `regressed` if any `absent` (regression-marker) string is present; `confirmed`
  if all `contains` strings are present (and no `absent` marker is); `pending`
  if the file is missing, predates the fix, or a `contains` string isn't present
  yet. **A missing `contains` marker is never `regressed`** — absence can't be
  told apart from a legitimately not-applicable state (e.g. first-gauge era with
  no prior gauge to render). Staleness uses **file mtime** (the text-file
  equivalent of `generated_at`).
- Any other / absent `kind` → `manual`.

## Staleness Guard

A batch may carry `applied_at` (ISO timestamp the fix went live). JSON
artifact-reading checks compare it to the artifact's `generated_at`; the
`file_contains` check compares it to the file's **mtime**. Both return
`pending` ("predates fix") when the artifact is older. Without this, every fix
would false-read `regressed`/`confirmed` on its first run, before the pipeline
has regenerated artifacts under the new code. Backward compatible: a batch
without `applied_at` skips the guard.

## API

- `verify_applied_fixes(state: dict, artifacts_root) -> list[dict]` — one verdict
  `{id, date, commit, status, detail}` per recorded fix. `[]` if no `applied_fixes`.
- `summarize(verdicts) -> dict` — counts by status plus `has_regression`.
- `drop_resolved(state, verdicts) -> dict` — returns state with `confirmed`
  fixes removed and empty batches dropped (does not mutate the input).
- Constants: `CONFIRMED`, `REGRESSED`, `PENDING`, `MANUAL`.

## Pipeline Integration

Consumed by the `daily-tool-analysis` skill (not the production cron pipeline):

- **Step 1** computes `applied_fix_verdicts` and `applied_fix_regressions`.
- **Step 2** — `applied_fix_regressions` non-empty blocks GREEN and raises AMBER.
- **Step 3** — a discovery-layer regression dispatches `portfolio-discovery-health`.
- **Step 4** — body line `"Fixes: N confirmed · N pending · N manual[, REGRESSED: …]"`.
- **Step 5** — `drop_resolved` prunes confirmed fixes when writing state back.

## State Contract

See `docs/OUTPUT_ARTIFACT_CONTRACTS.md` → `data/daily_check_state.json`
(`applied_fixes` ledger). The state file is gitignored / host-local.

## Tests

`tests/test_applied_fix_verifier.py` (27): all three check kinds, the staleness
guard for both JSON (`generated_at`) and text (`mtime`) artifacts
(stale→pending, fresh→judged, no-`applied_at`→backward-compat), `file_contains`
`contains`/`absent`/list semantics and the never-false-regress rule, `manual`
fallback, `summarize`, and `drop_resolved`.
