"""
Crowd Radar multi-source connectors (no-extra-cost, API-first, observe-only).

Each connector implements the :class:`CrowdSource` interface and returns a
structured :class:`SourceResult` from every method — it NEVER raises into the
pipeline. Sources are classified by cost/entitlement:

- ApeWisdom        — free, no-auth → active candidate
- FMP social       — existing key, entitlement-probed → active only if entitled
- Stocktwits       — official access unclear → probe / requires_manual_review
- Finnhub social   — premium → probe only if FINNHUB_API_KEY exists
- Quiver WSB       — paid → blocked_no_extra_cost unless an existing key opts in

No scraping, no browser/private endpoints, no paid plans. Sandbox-only; crowd
signals adjust research priority only and can never trigger a trade.
"""
