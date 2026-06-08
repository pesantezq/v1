# Schwab Read-Only Broker Sync — Design Spec

**Date:** 2026-06-08
**Status:** Approved (design); pending implementation plan
**Author:** Claude Code (brainstormed with operator)
**Lens:** Portfolio-manager evidence + developer/system observability
**Branch:** `feat/schwab-readonly-sync` off `main` (standalone; independent of the 5 pending branches and the parked GUI-cockpit design).

---

## 1. Objective

Add a **read-only** Charles Schwab broker-sync layer so StockBot can compare *actual* Schwab holdings against the *local* StockBot portfolio configuration and surface mismatches — **observe-only**. It syncs reality; it never executes strategy and never places orders.

## 2. Goals / Non-Goals

**Goals**
- Authenticate via Schwab OAuth 2.0 (authorization-code + refresh), per the official Trader API (Individual).
- Pull accounts, positions, balances/cash, market value, cost basis (where the API provides them).
- Normalize into local broker-snapshot artifacts.
- Reconcile Schwab actual vs local `config.json` holdings → a reconciliation artifact.
- Generate a **proposal-only** local-config update artifact when they differ.
- Graceful **disabled/unconfigured** behavior that still emits a status artifact.
- Fixture-based tests; no live credentials required to test.

**Non-Goals (hard)**
- **No order placement.** No trading endpoint may be implemented, imported, or called.
- **No broker trading capability**, no `Execute/Trade/Buy Now/Sell Now/Place Order/Auto-Trade/Auto-Approve` anywhere.
- **No direct config apply in this slice** — stop at the proposal artifact; the apply path is documented as the next safe step (reusing `tools/manual_portfolio_update.py`'s backup+audit+validate writer). (Operator chose "proposal-only now".)
- **No GUI view + no registry registration in this slice** — deferred follow-ups (need the parked cockpit + the pending `feat/artifact-registry-governance`). Documented in §15.
- Schwab data may update broker snapshot + reconciliation + proposal artifacts. It may **NOT** modify decision-core artifacts (`decision_plan`, `system_decision_summary`, `decision_explanations`, `decision_triage`) — those remain the only official portfolio-action sources.

## 3. Safety invariants (enforced + tested)
- `read_only_mode: true` / `trading_enabled: false` hardcoded in `broker_sync_status.json`; the client has **no order/trade methods at all** (a test asserts no trading symbol exists).
- Fail-closed when unconfigured/disabled (no creds → no network calls → status artifact says `unconfigured`).
- Secrets/tokens never committed, never logged; redaction helper scrubs client secret / tokens / auth codes from all error/log strings.
- Account numbers masked (last-4 or opaque hash) in every artifact and log.
- No secrets in any artifact.
- Schwab artifacts are evidence/observability/proposal only — never decision-core.

## 4. Architecture

```
portfolio_automation/brokers/
  __init__.py
  broker_models.py        # dataclasses: BrokerAccount, BrokerPosition, BrokerSnapshot, normalization
  schwab_oauth.py         # OAuth2 auth-URL build, token exchange, refresh; token load/save (gitignored); redaction
  schwab_client.py        # read-only HTTP client: get_account_numbers(), get_accounts(positions=True), get_balances() — NO trade methods
  schwab_sync.py          # orchestrator + CLI (--status/--sync/--reconcile); writes snapshot/positions/status artifacts; archive
  broker_reconciliation.py# compare snapshot vs config.json → reconciliation + proposal artifacts (pure functions)
```

**Data flow:** `schwab_sync` (CLI or callable) → `schwab_oauth` (token) → `schwab_client` (read-only GET) → `broker_models` (normalize) → write snapshot/positions/status → `broker_reconciliation` (vs `config.json`) → write reconciliation + proposal. All artifact writes go through `data_governance.safe_write_json(OutputNamespace.LATEST, ...)`; archive copies under `OutputNamespace.HISTORICAL` or a dedicated `outputs/archive/broker_sync/<date>/`.

**Pure vs I/O:** `broker_models` + `broker_reconciliation` are pure (fixture-testable, no network). `schwab_oauth`/`schwab_client` isolate all network + secrets. `schwab_sync` orchestrates and never raises (degraded status on any failure).

## 5. OAuth, secrets, token storage
- **Env vars (never hardcoded):** `SCHWAB_CLIENT_ID`, `SCHWAB_CLIENT_SECRET`, `SCHWAB_REDIRECT_URI`, `SCHWAB_READ_ONLY_MODE=true` (default true), `SCHWAB_TRADING_ENABLED=false` (default false; if ever set true, the layer logs a refusal and stays read-only — trading is not implemented regardless).
- **OAuth2 auth-code flow** (`api.schwabapi.com/v1/oauth/{authorize,token}`): `build_authorize_url()`, `exchange_code(code)`, `refresh(token)`. Auth-code/manual-paste flow documented (Schwab uses a redirect-capture step).
- **Token storage:** conservative local file `data/schwab_token.json` (the repo's `/data/` is already gitignored → auto-protected), written `0600`, containing only the token payload. Loaded on demand; refreshed when expired. **Never logged.** A `_redact()` helper scrubs `access_token`/`refresh_token`/`client_secret`/`code` from any string before logging or putting in an artifact.
- If creds absent → `configured: false`; never attempts network.

## 6. Schwab API assumptions (verified 2026-06-08; confirm-at-connect)
The Trader API detail pages are behind developer-portal login. Public-confirmable facts drive the design; **exact response field names are confirmed against the live API at connect-time**, so `broker_models` normalizes **defensively** (multiple candidate key names, `.get` chains, never KeyErrors):
- Host `api.schwabapi.com`; OAuth `/v1/oauth/authorize` + `/v1/oauth/token`.
- `GET /trader/v1/accounts/accountNumbers` → plain↔encrypted account-number mapping (use the encrypted hash; mask the plain).
- `GET /trader/v1/accounts?fields=positions` → accounts with `securitiesAccount.positions[]`, `currentBalances` (cash, liquidationValue/market value), positions carry `instrument.symbol`, `longQuantity`/`shortQuantity`, `marketValue`, `averagePrice`/cost basis, `instrument.assetType`.
- Fixtures in `tests/fixtures/schwab/` mirror this shape and are the single source the normalizer is tested against; a doc note flags "confirm field names on first live call."

## 7. Artifacts (exact shapes)

All under `outputs/latest/`, `observe_only: true`, `source: "schwab"`, via `safe_write_json`. Archive copy under `outputs/archive/broker_sync/<YYYY-MM-DD>/`.

**`broker_sync_status.json`** (always producible, even disabled): `{generated_at, observe_only:true, source:"schwab", enabled, configured, authenticated, read_only_mode:true, trading_enabled:false, last_success_at, last_error (redacted), account_count, position_count, overall_status (ok|degraded|unconfigured|disabled|error)}`.

**`schwab_portfolio_snapshot.json`**: `{generated_at, source:"schwab", snapshot_timestamp, accounts:[{account_id_masked, account_type, total_market_value, cash, positions_count}], totals:{market_value, cash}}`.

**`schwab_positions.json`**: `{generated_at, source:"schwab", positions:[{symbol, quantity, market_value, average_cost, asset_type, account_ref_masked, source_timestamp}]}`.

**`portfolio_reconciliation.json`**: `{generated_at, source:"schwab", summary_status (ok|mismatch|no_local_config|no_broker_data), matched:[{symbol, schwab_qty, local_shares}], quantity_mismatches:[{symbol, schwab_qty, local_shares, delta}], missing_in_local:[...], missing_in_schwab:[...], cash:{schwab, local, delta}, target_allocation_comparison:[...]|null, operator_review_message}`.

**`portfolio_config_update_proposal.json`** (proposal-only): `{generated_at, source:"schwab", source_snapshot_timestamp, before:{holdings, cash}, proposed_after:{holdings, cash}, reason, validation:{ok, errors:[...]}, operator_approval_required:true, auto_applied:false, apply_instructions:"reviewed manual step via tools/manual_portfolio_update.py"}`.

## 8. Reconciliation algorithm (`broker_reconciliation`, pure)
Key by uppercased symbol. For each symbol in (schwab ∪ local):
- in both → `matched` if `abs(schwab_qty − local_shares) < EPS` else `quantity_mismatches` (with signed delta).
- schwab-only → `missing_in_local`. local-only → `missing_in_schwab`.
- Cash: `schwab cash` vs `config.portfolio.cash_available` → `cash.delta`.
- Target-allocation comparison only if local `target_weight`s exist; compares schwab market-value % vs local target_weight (informational).
- `summary_status`: `no_broker_data` if snapshot empty; `no_local_config` if config holdings empty; else `mismatch` if any mismatch/missing/cash-delta>threshold; else `ok`.
- `operator_review_message`: plain-language summary; **never** a buy/sell/hold instruction — phrased as "review N differences; generate a config-update proposal to align local config."

## 9. Proposal generation (proposal-only)
From a reconciliation, build `proposed_after` = local holdings adjusted toward Schwab reality (quantities/cash), run the **validation rules (§11)**, set `operator_approval_required:true`, `auto_applied:false`. **No write to config.json.** The artifact + docs name the exact next safe step: the operator (or a future gated GUI confirm) applies via `tools/manual_portfolio_update.py` (which already does backup+audit+validate). This slice does NOT call it.

## 10. CLI (`python -m portfolio_automation.brokers.schwab_sync`)
- `--status`: emit `broker_sync_status.json` + print human status (configured? authenticated? read-only active?). Works disabled/unconfigured.
- `--sync`: pull → normalize → write snapshot/positions/status + archive. Fail-closed if unconfigured.
- `--reconcile`: load latest snapshot (or sync first if `--sync` combined) → write reconciliation + proposal.
- Always prints "READ-ONLY MODE ACTIVE — no trading endpoints are called." Prints **no secrets**. Never calls trading endpoints (none exist).

## 11. Validation rules (proposal)
The proposal validator guards **data sanity only**: no negative shares; no negative cash; symbol field required/non-empty; duplicate symbols rejected; target-weight sum check (if any `target_weight` values are present, they must sum to ~1.0 within ±0.02). Violations populate `validation.errors` and set `validation.ok=false` (proposal still emitted, flagged not-applyable).

**Concentration and leverage caps are NOT enforced here.** Those guardrails are enforced at allocation/decision time by the decision engine and allocation-policy layer — not on a shares-to-broker-reality sync proposal. The sync proposal reflects broker reality; it does not constrain it.

## 12. Masking & redaction
`mask_account(num)` → `"…1234"` (last 4) or opaque short hash; applied in every artifact + log. `_redact(text)` scrubs token/secret/code substrings. Tests assert no full account number and no token/secret appears in any artifact or log line.

## 13. Disabled / unconfigured behavior
- `SCHWAB_*` unset → `configured:false`, `overall_status:"unconfigured"`, no network, status artifact still written.
- `SCHWAB_READ_ONLY_MODE` defaults true; the layer is inert until creds present. CLI `--status` always succeeds.

## 14. Testing (fixtures; no live creds)
`tests/test_schwab_*.py` + `tests/fixtures/schwab/*.json`:
1. disabled/unconfigured → status artifact shape, `unconfigured`, no network attempted.
2. OAuth/redaction: `_redact` scrubs token/secret/code; token never in logs/artifacts.
3. `broker_models` parses fixture accounts/positions → normalized rows (defensive against missing fields).
4. reconciliation: matched / quantity_mismatch / missing_in_local / missing_in_schwab / cash_delta.
5. proposal generation: before/after, reason, validation pass + fail cases.
6. validation failures: negative shares/cash, missing/duplicate symbol, target-weight sum out of range. (Concentration/leverage cap breaches are NOT tested here — they are not a validator concern; see §11.)
7. **no trading capability**: assert the brokers package exposes no function/method whose name matches `place_order`, `submit_order`, `buy`, `sell`, `execute_trade`, `cancel_order`, or any name starting with `order`/`trade` (AST/attribute scan).
8. account masking: no full account id in any artifact.
9. artifact shapes match §7.
10. `schwab_sync` never raises on degraded inputs (returns/-writes degraded status).

## 15. Deferred follow-ups (documented, not built here)
- **GUI `/dashboard/portfolio-sync`** (desktop table + mobile stacked cards, "updates local config only — no trades" banner, forbidden-label-free) — lands once the GUI cockpit exists (or as a bolt-on to existing gui_v2).
- **Artifact-registry registration** of the 5 artifacts with `consumer_status`/`role` (`broker_sync_status`→developer/telemetry; snapshot/positions→portfolio-manager evidence/broker-snapshot; reconciliation→portfolio-manager evidence; proposal→operator approval) — lands once `feat/artifact-registry-governance` merges; must keep debt checks passing.
- **Gated config apply** via the existing safe writer (dry-run + confirm + backup + audit) — the cockpit's portfolio-config flow.

## 16. Docs
`docs/schwab_integration.md`: overview, Schwab Developer app/OAuth setup, env vars, read-only safety model, token/security notes, how to run sync/reconcile, how to read reconciliation, how proposal/apply works (proposal now; apply = next step via manual_portfolio_update), troubleshooting, "confirm field names on first live call." No secrets/personal data. CHANGELOG entry (architecture/output_contract).

## 17. Milestones (sequential)
1. `broker_models` + fixtures + parse tests (pure).
2. `schwab_oauth` (auth-url/exchange/refresh + token storage + `_redact` + masking) + redaction/masking tests.
3. `schwab_client` (read-only GETs; NO trade methods) + the no-trading-capability test.
4. `schwab_sync` orchestrator + CLI + status/snapshot/positions/archive + disabled-graceful + never-raise tests.
5. `broker_reconciliation` (reconciliation + proposal + validation) + reconciliation/proposal/validation tests.
6. Docs + CHANGELOG + full validation. (GUI + registry deferred per §15.)

## 18. Risks & mitigations
| Risk | Mitigation |
|---|---|
| Exact Schwab field names differ from fixtures | Defensive normalization (candidate keys, `.get`); doc note to confirm on first live call; fixtures are the test contract |
| Token leakage | gitignored `data/` storage, 0600, `_redact` everywhere, tests assert no leak |
| Scope creep into trading | No trade methods exist; AST test forbids order/trade/buy/sell symbols; trading_enabled hardcoded false |
| Accidental config mutation | Proposal-only; no write to config.json in this slice |
| Account-id exposure | `mask_account` in all artifacts/logs; test asserts masking |
