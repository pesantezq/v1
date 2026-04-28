# Changelog Decisions

Use this file to record high-impact changes that affect meaning, not just implementation.

## How To Use This File

Add an entry whenever a change affects:

- scoring semantics
- ranking weights
- conviction or sizing behavior
- allocation caps
- output contracts
- SQLite schema
- architecture boundaries

## Required Entry Format

### Date

`YYYY-MM-DD`

### Area

One of:

- scoring
- allocation
- alerts
- state
- output_contract
- architecture
- evaluation

### Files / Functions

Name the exact files and functions changed.

### Decision

Describe what changed in plain language.

### Why

State the problem being solved.

### Invariants Preserved

Explicitly state what did not change.

### Downstream Impact

List affected artifacts, tests, and GUI surfaces.

---

## FMP Stable Baseline (v1.0)

### Date

2026-04-28

### Area

architecture

### Files / Functions

- `fmp_client.py` — all `_EP_*` constants, `get_batch_quotes`, `get_batch_profiles`, `get_historical_prices`, `get_ratios`, `get_stock_news`, `get_key_metrics`, `get_income_statement`
- `fmp_endpoint_registry.py` — new; machine-readable endpoint source of truth
- `fmp_endpoint_compliance.py` — new; runnable compliance checker (`python -m fmp_endpoint_compliance`)
- `watchlist_scanner/scanner.py` — FMP primary for all technical + fundamentals + news data
- `watchlist_scanner/fundamentals_engine.py` — `parse_fmp_profile`, `parse_fmp_fundamentals_bundle`
- `tests/test_fmp_endpoint_registry_compliance.py` — new; 23 compliance contract tests
- `docs/REGRESSION_CHECKLIST.md` — FMP compliance block added to section 3
- `docs/CLAUDE_AGENT_RULES.md` — FMP Data Rules hard constraint added

### Decision

Migrated all daily scanner and fundamentals paths to FMP stable endpoints (`https://financialmodelingprep.com/stable/`). Implemented an endpoint registry as the single source of truth and a runnable compliance checker that gates any future endpoint changes. Achieved 257/257 passing tests on VPS with zero violations.

- All core endpoints (`quote`, `profile`, `historical-price-eod/full`, `ratios`, `news/stock`, `key-metrics`, `income-statement`) use `FMP_STABLE_BASE_URL`.
- Legacy v3/v4 methods (`get_sp500_constituents`, `get_batch_profiles_v3`, `get_bulk_profiles`, etc.) retained for universe pipeline only — explicitly classified as `legacy_optional` or `premium_optional` and excluded from the daily scanner.
- Alpha Vantage demoted to true fallback; AV OHLCV skipped when FMP historical data is present.
- `technical_data_completeness` field added to scan output: `full` | `partial` | `price_only` | `missing`.

### Why

FMP v3 endpoints were returning HTTP 403 for historical prices and news on the Starter plan. The system was silently degrading with 0/22 profiles loaded. Stable endpoints resolve the auth issue and are guaranteed available at 300 calls/min on the Starter plan.

### Invariants Preserved

- Advisory-only operation — no execution logic changed
- `signal_score`, `confidence_score`, `conviction_score` semantics unchanged
- Output file paths and top-level contract shapes unchanged
- SQLite schema unchanged
- Alpha Vantage fallback path preserved for symbols where FMP data is unavailable

### Downstream Impact

- Daily scanner now reliably loads profiles, historical prices, and news for all watchlist symbols
- `fmp_endpoint_compliance` must pass (`RESULT: COMPLIANT`) before any FMP wiring change is merged
- `pytest tests/ -k fmp` (257 tests) is the regression gate for all FMP-related changes
- Enables safe high-frequency scanning at FMP Starter plan limits (300 calls/min)

---

## Baseline

### Date

2026-04-28

### Area

architecture

### Files / Functions

- `main.py:run_portfolio_update`
- `watchlist_scanner/__main__.py:run`
- `watchlist_scanner/scanner.py`
- `watchlist_scanner/confidence.py:compute_confidence`
- `watchlist_scanner/conviction.py:apply_conviction_layer`
- `watchlist_scanner/portfolio_construction.py:apply_portfolio_construction_layer`
- `allocation_engine.py:suggest_allocation`
- `policy_evaluator/*`
- `state_store.py`

### Decision

Documented the current baseline architecture, output contracts, scoring meanings, and state schema without changing application behavior.

### Why

AI agents and maintainers need a stable source of truth before making behavior changes.

### Invariants Preserved

- advisory-only operation
- separate `signal_score` and `confidence_score`
- output file paths and top-level contract shapes
- existing SQLite schema

### Downstream Impact

- documentation consumers
- AI coding agents
- future regression review

