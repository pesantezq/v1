# FMP Compliance

Last verified against the documented stable-endpoint policy on 2026-04-30.

## Purpose

Keep all FMP usage explicit, approved, and regression-safe for both live analysis and planned offline replay work.

## Core Rule

Any FMP endpoint used by production code must be represented in `fmp_endpoint_registry.py` first.

## Live Scanner Rule

The daily scanner path must continue to use approved stable endpoints only.

## Historical Replay Rule

Historical Replay v1 is implemented. It uses only the approved stable historical EOD endpoint.

Approved endpoint:

- `FMPClient.get_historical_prices(symbol, years=N)` →
  `stable/historical-price-eod/full?symbol=X&from=YYYY-MM-DD`
- Registered in `fmp_endpoint_registry.py` as `historical_prices` (P0, Starter-plan safe)

Historical replay constraints (enforced):

- no premium endpoints
- no ad hoc endpoint additions
- no bypassing `fmp_endpoint_registry.py`
- uses existing FMPClient caching and budget guardrails
- replay runs offline and separately from the daily live pipeline

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

## Implementation Status

Historical Replay v1 is implemented at `portfolio_automation/historical_replay/`.
Endpoint usage is documented above and consistent with the registry.
