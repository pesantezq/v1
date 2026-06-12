# Schwab Read-Only Broker Sync — Integration Guide

**Status:** Shipped (2026-06-08), observe-only, proposal-only.
**Branch shipped on:** `feat/schwab-readonly-sync`
**Spec:** `docs/superpowers/specs/2026-06-08-schwab-readonly-sync-design.md`

---

## Overview

The Schwab broker-sync layer is a **read-only** integration that pulls account and position data from
the Schwab Trader API, compares it against the local StockBot `config.json` holdings, and emits a
set of observability artifacts. It never places orders, never executes trades, never writes to
decision-core artifacts, and never modifies `config.json` automatically — every reconciliation
difference surfaces as a **proposal only**, which an operator reviews and applies manually via a
separate safe-writer tool.

The layer is completely inert until Schwab credentials are configured. When unconfigured it still
emits a healthy `broker_sync_status.json` artifact (status: `unconfigured`) so the artifact-health
system always has a readable file.

---

## Read-Only Safety Model

This is not a trading integration. The following invariants are enforced in code and tested:

- **No trade methods exist anywhere** in `portfolio_automation/brokers/`. The module contains only
  `get_account_numbers()` and `get_accounts()` on `SchwabClient`. An AST test
  (`test_schwab_client.py::test_no_trading_capability_anywhere_in_brokers_package`) scans every
  `.py` file in the package and fails if any function or method whose name matches
  `place_order`, `submit_order`, `buy`, `sell`, `execute_trade`, `cancel_order`, or any name
  starting with `order` or `trade` is defined.
- **`trading_enabled` is hardcoded `false`** in every `broker_sync_status.json` artifact, regardless
  of environment variables.
- **`read_only_mode: true`** is hardcoded in the status artifact.
- **`observe_only: true`** is hardcoded in the status artifact.
- **Schwab data never modifies decision-core.** The artifacts `decision_plan.json`,
  `system_decision_summary.json`, `decision_explanations.json`, and `decision_triage.json` are not
  read or written by the broker layer. Schwab data is evidence only.
- **No config.json write in this slice.** The proposal artifact proposes changes; the operator
  applies them via a reviewed manual step (see "How Proposal/Apply Works" below).

---

## Schwab Developer App and OAuth Setup

### 1. Create a Schwab Developer App

1. Go to [developer.schwab.com](https://developer.schwab.com) and sign in with a Schwab account.
2. Navigate to **My Apps → Add a new app**.
3. Fill in the app name and description. Choose "Individual Trader API" (not "Aggregator API").
4. For **Callback URL (Redirect URI)**, enter a localhost redirect such as
   `https://127.0.0.1/callback`. This must match the value you set in `SCHWAB_REDIRECT_URI`.
5. Under **API Products / Scopes**, configure the read-only scopes in the Schwab Developer portal app
   settings (e.g. `openid`, `profile`, `offline_access`, `readonly`). Scopes are bound to the app in
   the portal — the authorize URL does **not** pass a `scope` parameter; whether one is required by
   Schwab should be confirmed on the first live call (see "Confirm Field Names on First Live Call"
   below). Do **not** request order-placement or trading scopes — they are not needed and would widen
   the security footprint.
6. Submit the app. Schwab reviews new apps; approved credentials (Client ID + Client Secret) appear
   in the app dashboard once approved.

### 2. One-Time OAuth Authorization

Schwab uses an authorization-code flow. After credentials are approved:

1. Set the three required environment variables (see below).
2. Run:
   ```bash
   python3 -m portfolio_automation.brokers.schwab_sync --status
   ```
   This confirms the layer is configured and prints the authorize URL printed by:
   ```bash
   python3 -c "from portfolio_automation.brokers import schwab_oauth as oa; print(oa.build_authorize_url())"
   ```
3. Open the printed URL in a browser. Log in to Schwab. After authorization, the browser redirects
   to your callback URL with a `?code=...` query parameter in the address bar.
4. Paste the code value into:
   ```bash
   python3 -c "
   from portfolio_automation.brokers import schwab_oauth as oa
   oa.exchange_code('PASTE_CODE_HERE')
   print('Token saved to', oa.TOKEN_PATH)
   "
   ```
5. The token is saved to `data/schwab_token.json` (mode `0600`, gitignored). Subsequent runs
   refresh it automatically when it expires.

> There is **no `schwab_auto_auth` module and no `--bootstrap` flag**. The bootstrap is exactly the
> manual `build_authorize_url` → browser → `exchange_code` flow above (it requires Schwab's MFA, so it
> cannot be scripted with stored credentials).

### Re-authentication: the 7-day refresh-token clock

Schwab issues two tokens with very different lifetimes:

| Token | Lifetime | Renews without a browser? |
|---|---|---|
| `access_token` | ~30 min | ✅ auto-refreshed every sync via the stored refresh token |
| `refresh_token` | **7 days** | ❌ **no** — a browser re-auth (`exchange_code`) is mandatory |

Within any 7-day window the daily cron sync is fully hands-free. But Schwab issues **no rolling
replacement** for the refresh token, so when the 7-day clock lapses the sync goes `degraded`
(unauthenticated) until you repeat the OAuth flow above. "Never re-auth" is **not achievable** with
Schwab — a ~30-second weekly browser re-auth (which clears Schwab's MFA, so you're notified of every
login) is the floor.

To turn that from a silent outage into a planned task, `exchange_code()` anchors the 7-day clock and
`broker_sync_status.json` surfaces it:

- `reauth_status` ∈ `{ok, due_soon, expired, unknown}` — `due_soon` fires ≤2 days before expiry.
- `reauth_expires_at` (ISO) / `reauth_days_remaining` (float).
- `unknown` is the inert/legacy state (token predates tracking, or uncredentialed) — it populates
  on the next token refresh or re-auth; it is **not** an alert.

The daily tool-analysis surfaces `due_soon`/`expired` as AMBER (never RED — observe-only). When you
see it, just re-run the OAuth flow above; the anchor resets to a fresh 7-day window.

---

## Environment Variables

Three environment variables are required; two are optional. Set them in `.env` (gitignored) or as
system environment variables. Never hardcode credentials.

| Variable | Required | Default | Description |
|---|---|---|---|
| `SCHWAB_CLIENT_ID` | Yes | — | OAuth Client ID from the Schwab Developer portal |
| `SCHWAB_CLIENT_SECRET` | Yes | — | OAuth Client Secret from the Schwab Developer portal |
| `SCHWAB_REDIRECT_URI` | Yes | — | Exact redirect URI registered with the app, e.g. `https://127.0.0.1/callback` |
| `SCHWAB_READ_ONLY_MODE` | No | `true` | Controls whether the layer is active. Defaults true; set `false` only to explicitly disable. Trading is NOT implemented regardless of this value. |
| `TRADING_ENABLED` | No | `false` | Must remain `false`. The codebase has no trading implementation; this variable exists as an explicit documentation signal only. |

Example `.env` block (no real values — fill in your own):

```dotenv
SCHWAB_CLIENT_ID=your_client_id_here
SCHWAB_CLIENT_SECRET=your_client_secret_here
SCHWAB_REDIRECT_URI=https://127.0.0.1/callback
SCHWAB_READ_ONLY_MODE=true
TRADING_ENABLED=false
```

---

## Token and Security Notes

- **Token file location:** `data/schwab_token.json` at the repo root. The `/data/` directory is
  gitignored at repo root, so the token file is automatically excluded from commits.
- **File permissions:** the token file is written with mode `0600` (owner read/write only).
  Confirm this after first save with `ls -la data/schwab_token.json`.
- **Tokens are never logged.** The `redact()` helper in `broker_models.py` scrubs both
  snake_case (`access_token`, `refresh_token`, `client_secret`, `code`, `id_token`, `Authorization`)
  and camelCase (`accessToken`, `refreshToken`, `clientSecret`, `idToken`) key patterns from any
  string before it reaches a log statement or an artifact. Tests assert that no raw token value
  appears in any written artifact.
- **Account numbers are masked.** Every artifact and log line uses `mask_account()` which renders
  account numbers as `…NNNN` (last 4 characters). Full account numbers never appear in artifacts.
- **Never commit** `data/schwab_token.json`, `.env`, or any file containing the client secret.

---

## How to Run

The CLI entry point is:

```bash
python3 -m portfolio_automation.brokers.schwab_sync [--status | --sync | --reconcile]
```

Every invocation prints:

```
READ-ONLY MODE ACTIVE — no trading endpoints are called.
```

### `--status` (always safe)

Writes `outputs/latest/broker_sync_status.json` and prints a human-readable summary.
Works whether configured or not; no network calls when unconfigured.

```bash
python3 -m portfolio_automation.brokers.schwab_sync --status
# schwab: configured=False authenticated=False status=unconfigured accounts=0 positions=0
```

### `--sync` (requires credentials + live token)

Calls `GET /trader/v1/accounts/accountNumbers` and `GET /trader/v1/accounts?fields=positions`,
normalizes the response, and writes:

- `outputs/latest/broker_sync_status.json`
- `outputs/latest/schwab_portfolio_snapshot.json`
- `outputs/latest/schwab_positions.json`
- Archive copies under `outputs/archive/broker_sync/<YYYY-MM-DD>/`

Fails closed (writes an `error` status artifact) if unconfigured, unauthenticated, or if the API
call fails. Never raises an exception to the caller.

```bash
python3 -m portfolio_automation.brokers.schwab_sync --sync
```

### `--reconcile` (reconciles from the latest cached snapshot)

Loads the latest snapshot and positions artifacts from `outputs/latest/`, compares them against
`config.json`, and writes:

- `outputs/latest/portfolio_reconciliation.json`
- `outputs/latest/portfolio_config_update_proposal.json`

**`--reconcile` alone does NOT trigger a live sync.** Run `--sync` first (or use
`--sync --reconcile` together) so the cached snapshot is fresh.

```bash
# reconcile from the cached snapshot (no network):
python3 -m portfolio_automation.brokers.schwab_sync --reconcile

# sync then reconcile in one invocation:
python3 -m portfolio_automation.brokers.schwab_sync --sync --reconcile
```

---

## How to Read the Reconciliation Artifact

`outputs/latest/portfolio_reconciliation.json` has these key fields:

| Field | Meaning |
|---|---|
| `summary_status` | `ok` — all positions match; `mismatch` — at least one difference; `no_broker_data` — sync hasn't run or returned empty; `no_local_config` — `config.json` has no holdings |
| `matched` | Symbols where Schwab qty and local shares agree |
| `quantity_mismatches` | Symbols with different quantities; includes `delta` (Schwab − local) |
| `missing_in_local` | Symbols held at Schwab but not in `config.json` |
| `missing_in_schwab` | Symbols in `config.json` but not found at Schwab |
| `cash.delta` | Schwab cash balance minus `config.portfolio.cash_available` |
| `operator_review_message` | Plain-language summary; never issues buy/sell instructions |

Example `summary_status: "mismatch"` message:

```
Review 2 holding difference(s) and a $12.50 cash difference. Generate a config-update proposal
to align local config to Schwab reality.
```

---

## How Proposal/Apply Works

**This slice is proposal-only.** The system never writes to `config.json` automatically.

### The Proposal Artifact

`outputs/latest/portfolio_config_update_proposal.json` contains:

- `before` — current local `config.json` holdings and cash
- `proposed_after` — holdings/cash aligned toward Schwab reality
- `validation` — `{ok: true|false, errors: [...]}` — flags negative shares, missing symbols, bad
  weight sums, etc.
- `operator_approval_required: true` (hardcoded)
- `auto_applied: false` (hardcoded)
- `apply_instructions` — directs the operator to the safe-writer path

### Applying a Proposal (Reviewed Manual Step)

After reviewing the proposal artifact:

```bash
python3 -m tools.manual_portfolio_update
```

That tool performs backup + audit + validation before writing. It is entirely separate from the
broker layer; the broker layer has no access to it.

**Do not** apply a proposal if `validation.ok` is `false` — resolve the flagged errors first.

---

## Confirm Field Names on First Live Call

The Schwab Trader API detail pages are behind a developer-portal login. The fixture shapes in
`tests/fixtures/schwab/` mirror the documented response structure, and `broker_models.normalize_accounts()`
is **deliberately defensive** (multiple candidate key names, `.get()` chains, no `KeyError` on
missing fields). On the first live call, verify that:

- `securitiesAccount.positions[].instrument.symbol` is present (or detect the alternate key)
- `securitiesAccount.currentBalances.liquidationValue` is the correct market-value field
- `securitiesAccount.currentBalances.cashBalance` is correct for cash

If the live response uses different key names, update `broker_models.normalize_accounts()` to add
the new candidate key to the `.get()` chain (no behavioral change to any existing test).

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| `status=unconfigured` | `SCHWAB_CLIENT_ID` not set | Set all three required env vars |
| `status=error` with "unauthenticated" | Token file missing or expired | Re-run the OAuth one-time flow (exchange_code) |
| `status=error` with HTTP 401 | Expired token that couldn't refresh | Delete `data/schwab_token.json` and re-run the OAuth flow |
| `status=error` with HTTP 403 | Wrong scopes on the app | Check app scopes in the Schwab Developer portal |
| Positions empty / snapshot empty | API returned empty response | Run `--sync` first; confirm account has positions |
| `validation.ok: false` in proposal | Negative shares or missing symbol | Review `validation.errors` in the proposal artifact; fix the mismatch before applying |
| `data/schwab_token.json` permissions not 0600 | OS filesystem quirk | `chmod 0600 data/schwab_token.json` |

---

## Deferred Follow-Ups (Not Built in This Slice)

The following items are intentionally not implemented here. They are documented for the next
operator iteration.

### GUI Portfolio-Sync View

A `/dashboard/portfolio-sync` page showing the reconciliation table (matched, mismatches, missing)
and the proposal side-by-side. Lands once the GUI cockpit design (`feat/gui-cockpit`) exists or as
a bolt-on to `gui_v2`. Must include a prominent "read-only — updates local config only, no trades"
banner and must use no forbidden trade-action labels.

### Artifact-Registry Registration

The 5 new artifacts should be registered in the artifact registry (once `feat/artifact-registry-governance`
merges to `main`) with the following suggested roles:

| Artifact | Role | Consumer |
|---|---|---|
| `broker_sync_status.json` | developer / telemetry | daily-tool-analysis health check |
| `schwab_portfolio_snapshot.json` | portfolio-manager evidence / broker-snapshot | reconciliation, GUI |
| `schwab_positions.json` | portfolio-manager evidence / broker-snapshot | reconciliation, GUI |
| `portfolio_reconciliation.json` | portfolio-manager evidence / mismatch report | GUI, operator |
| `portfolio_config_update_proposal.json` | operator-approval artifact | manual_portfolio_update |

Until registration, the `broker_sync_status.json` artifact is always-producible (even when
unconfigured) so debt checks that scan `outputs/latest/` will see a valid file. The other four
artifacts are only present after a `--sync` / `--reconcile` run.

### Gated Config Apply

Once the GUI cockpit exists, a "Review and Apply" flow can wire the proposal artifact into the
existing safe-writer (`tools/manual_portfolio_update.py`) with a dry-run preview + operator
confirm step. This is the intended final step; it is NOT part of the current slice.

---

## Module Reference

| Module | Responsibility |
|---|---|
| `portfolio_automation/brokers/__init__.py` | Package marker |
| `portfolio_automation/brokers/broker_models.py` | Dataclasses, `mask_account`, `redact`, `normalize_accounts`, `snapshot_dict`, `positions_dict` — pure, no network |
| `portfolio_automation/brokers/broker_status.py` | `build_status()` — builds `broker_sync_status` shape, hardcodes `read_only_mode:true` + `trading_enabled:false` |
| `portfolio_automation/brokers/broker_reconciliation.py` | `reconcile`, `validate_proposed_holdings`, `build_proposal` — pure, no network, proposal-only |
| `portfolio_automation/brokers/schwab_oauth.py` | OAuth2 auth-url / exchange / refresh; token load/save (gitignored `data/schwab_token.json`, 0600); redaction |
| `portfolio_automation/brokers/schwab_client.py` | Read-only `SchwabClient` with `get_account_numbers()` / `get_accounts()` only — no trade methods |
| `portfolio_automation/brokers/schwab_sync.py` | Orchestrator + CLI; writes all 5 artifacts; never raises; archive |

---

*No real secrets, credentials, or personal account data appear in this document.*
