# FMP Compliance

Last verified against the documented stable-endpoint policy on 2026-04-30.

## Purpose

Keep all FMP usage explicit, approved, and regression-safe for both live analysis and planned offline replay work.

## Core Rule

Any FMP endpoint used by production code must be represented in `fmp_endpoint_registry.py` first.

## Live Scanner Rule

The daily scanner path must continue to use approved stable endpoints only.

## Historical Replay Rule

Planned historical replay should use the approved stable historical EOD endpoint only.

Expected endpoint family:

- stable historical EOD endpoint used by current FMP policy for end-of-day price history

Historical replay constraints:

- no premium endpoints unless explicitly enabled and documented
- no ad hoc endpoint additions
- no bypassing `fmp_endpoint_registry.py`
- no live-vs-replay endpoint drift without documentation

## Registry Rule

Before adding any new FMP endpoint for replay or calibration:

1. add it to `fmp_endpoint_registry.py`
2. classify it correctly
3. document the use case
4. keep compliance checks passing

## Rate Limits And Caching

Historical replay must respect:

- Starter-plan-safe assumptions where required
- documented rate limits
- existing caching discipline
- batch and replay workloads that do not starve the live daily pipeline

Recommended operational rule:

- replay should run offline or separately from the daily live schedule

## Safety Boundaries

- observe-only only
- no broker actions
- no hidden premium dependency
- no undocumented fallback endpoint
- no endpoint changes that silently change scanner or replay semantics

## Next Implementation Step

When `portfolio_automation/historical_decision_replay.py` is implemented, document its exact approved endpoint usage here and in `docs/DATA_AND_FMP_ENDPOINTS.md` in the same change.
