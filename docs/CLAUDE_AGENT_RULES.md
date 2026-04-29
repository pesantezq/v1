# Claude Agent Rules

This repository is designed for analysis and decision support. AI agents must preserve that boundary.

## Hard Constraints

- No auto trading.
- No broker integration.
- No execution authority.
- Preserve `observe_only` behavior in watchlist and allocation outputs.
- Preserve `recommend_only` behavior in structured config semantics.
- Do not bypass guardrails because of AI confidence or narrative quality.
- Do not merge `signal_score` and `confidence_score`.
- Do not rename base score fields without an explicit migration plan.
- Do not break GUI or artifact contracts.
- Prefer additive, diff-friendly changes.

## FMP Data Rules (Hard Constraint)

- Only use endpoints defined in `fmp_endpoint_registry.py` because it is the source of truth.
- All new endpoints must be added to the registry before being used in code.
- Never use `/v3/` or `/v4/` endpoints in the daily scanner path without explicit user approval.
- All endpoint changes must pass `python -m fmp_endpoint_compliance` → `RESULT: COMPLIANT`.
- Preflight is mandatory before production daily runs: `bash scripts/preflight.sh`.
- FMP-focused tests must pass before pipeline execution: `python -m pytest tests/ -k fmp -v`.
- No endpoint changes may bypass the registry, even temporarily.
- Starter plan compatibility is mandatory. `starter_safe: True` is required for any daily scanner endpoint.
- The stable base URL (`FMP_STABLE_BASE_URL`) must be used for all core endpoints. `FMP_BASE_URL` is legacy or universe-only.

## Protected Semantics

- `signal_score`
  Opportunity attractiveness.
- `confidence_score`
  Evidence quality and trustworthiness.
- `effective_score`
  Derived actionability metric.
- `conviction_score`
  Advisory sizing confidence.
- `final_rank_score`
  Ordering score.
- `recommendation_score`
  Policy or profile recommendation score.

These scores may be improved, but their meanings must remain explicit and separate.

## Allowed AI Behaviors

- Improve documentation
- Add tests
- Add explainability fields
- Add read-only analytics
- Add evaluation reports
- Add additive output metadata
- Improve degraded-mode handling
- Improve alert fatigue controls without hiding materially strong signals

## Disallowed AI Behaviors Without Explicit User Approval

- Changing ranking semantics in a protected area
- Changing conviction semantics in a protected area
- Changing allocation policy logic in a protected area
- Changing portfolio construction semantics in a protected area
- Introducing execution or auto-order behavior
- Silently removing output fields or state columns

## Change Style

- Trace the exact source-to-output path first.
- Name exact files and functions before changing behavior.
- Prefer the narrowest module that fixes the issue.
- Preserve backward compatibility in artifacts and state.
- Lower certainty when data is stale or incomplete. Do not fabricate conviction.

## Required Validation Before Finishing

- Run targeted tests for touched modules.
- Validate the relevant artifact contracts.
- Check SQLite migrations if state changed.
- Confirm the docs describe actual behavior, not intended future behavior.
- For production-run changes, validate `bash scripts/preflight.sh`.
- Keep `scripts/run_daily_safe.sh` as the required cron wrapper for daily production runs.

## Communication Rules For AI Agents

- State assumptions clearly.
- Explain derived metrics as derived.
- Prefer concise, inspectable reasoning over opaque summaries.
- When behavior changes, name the exact function and downstream artifact affected.
