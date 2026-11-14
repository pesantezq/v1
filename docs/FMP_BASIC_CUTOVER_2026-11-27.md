# Northstar — FMP Starter exit / Basic cutover research gate

**Owner deadline:** November 27, 2026. **Disposition:** NOT_READY; research-only.
**Workstream:** Northstar/StockBot; does not apply to SportsEdge.
**Parent source:** GitHub \`pesantezq/v1\` at \`2a6cb1649c4ea489f540c4f3d495d34a20deace2\`.
**Authority:** The owner plans to remove the paid FMP Starter plan (approximately $29/month) and downgrade to FMP Basic (Free). A downgrade request was previously sent; provider confirmation is not yet established. This is **not** evidence that FMP itself is closing on November 27.

## Why this gate is necessary

- **Only priced holdings source in current code:** \`market_data.py\` says FMP is the sole holdings price provider and the earlier Alpha Vantage market client was removed.
- **Large scheduled FMP dependency:** \`scripts/run_monthly_universe_refresh.sh\` documents approximately 3,700 calls per monthly full scan. Basic advertises **250 calls/day**, so the existing one-run refresh is not ready to fit that limit.
- **Legacy universe calls:** \`universe/fmp_universe.py\` invokes \`FMPClient.get_sp500_constituents\` and \`get_batch_profiles_v3\`. FMP says Basic customers cannot access discontinued Legacy v3/v4 APIs; legacy methods remain in \`fmp_endpoint_compliance.LEGACY_METHOD_MAP\`.
- **Daily key/entitlements:** \`scripts/preflight.sh\` requires \`FMP_API_KEY\`; a still-valid API key does not establish Basic rights to every Stable endpoint. \`fmp_endpoint_registry.py\` tracks **Starter-safe**, not **Basic-verified**, endpoints.
- **VS-002 scientific identity:** \`/stable/historical-price-eod/dividend-adjusted\` is the frozen evidence endpoint; generic \`/full\`, old caches or another vendor cannot silently replace it. VS-002 execution remains separately human-gated.
- **Historical rights:** the provider has published that post-downgrade rights to retained subscribed data are contract-specific; avoid treating the existing archive as automatically redistributable/reusable after cancellation.

## Offline implementation in this research branch

\`fmp_free_cutover_audit.py\` reuses the code-owned FMP endpoint registry and existing compliance method list, reporting required daily endpoints with **Basic access UNVERIFIED**, legacy method exposure, known universe legacy callers, estimated monthly/full-scan Basic quota shortfall, VS-002 endpoint identity, and explicit remaining rights/billing gates.

It uses **no network, keys, environment files or runtime mutations**. It never asserts that the system is ready. Running \`python -m fmp_free_cutover_audit --strict\` must exit **2** until later, independently reviewed and separately authorized evidence changes the contract. Ordinary display mode exits 0 only to allow the report to be consumed without claiming a pass. This branch does not add a startup gate or affect a scheduled job.

Deterministic tests: \`python -m pytest -q tests/test_fmp_free_cutover_audit.py\`. Negative tests cover unavailable Free entitlement, legacy method exposure, request-volume conflict, missing registry, VS-002 identity, network avoidance and key non-disclosure.

## Independent implementation sequence

1. **FMP-0 / existing-source inventory:** Finish read-only call-site, scheduled endpoint and true-call-ledger audit. Freeze a source/consumer/needed-quality/rights table and exact code SHA; no secret values and no live FMP probes without separately approved account/data authority.
2. **FMP-1 / Basic contract and rights:** Confirm actual Nov 27 plan/downgrade date with FMP account response; decide whether subscribed historical cache can be used, retained or needs removal/segregation. Verify Basic-only endpoint access under provider terms and explicit quotas; do not assume Starter-to-Free parity or reset semantics.
3. **FMP-2 / no-new-vendor parity:** For holdings quotes, universes, fundamentals, news and 5-year adjusted prices, identify existing lawful low-cost alternatives. Consider Alpha Vantage only with its independent 25/day free quota, SEC EDGAR only for filings/fundamentals (not live price quotes), and existing caches only where rights and as-of freshness are valid. Synthetic fixture tests must cover missing data, missing adjusted price, duplicate quote, splits, symbol identity and stale data.
4. **FMP-3 / isolated candidate repair:** Add a bounded data-provider abstraction/fallback only where actually missing. Guard required-data absence with clear \`DATA_UNAVAILABLE\`/HOLD, not zeroed financials or stale silently substituted prices. Preserve current broker/trading prohibition, run-mode identities and downstream contract equivalence.
5. **FMP-4 / independent review and cutover:** Separate non-author exact-SHA tests, metrics and operator decision for any future changes to the official pipeline, cron/systemd, subscriptions or shared configuration. Rehearse Basic limitations and verify new account tier before/after Nov 27. If no credible source exists, refuse unsupported workflows instead of fabricating performance or opening live broker access.

## Official provider references, checked November 14, 2026

- FMP pricing: https://site.financialmodelingprep.com/pricing-plans
- FMP FAQ (Basic 250 daily; Legacy not available on Free): https://site.financialmodelingprep.com/faqs
- FMP downgrade/retention limitations: https://site.financialmodelingprep.com/insights/platform/can-you-downgrade-or-return-to-the-free-fmp-plan-later
- Alpha Vantage 25/day for ordinary free API use: https://www.alphavantage.co/support/api-key/

**Not a runtime activation or financial profitability claim.** Keep Northstar separate from SportsEdge and any personal brokerage/retirement accounts.
