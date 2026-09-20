# Schwab Read-Only Broker Sync — Integration Guide

**Status:** Shipped (2026-06-08), observe-only, proposal-only.
**v2 foundation (2026-09-20, `feature/schwab-broker-evidence-v2`):** single-writer token
authority, structured auth states, governed `BrokerPortfolioSnapshot` evidence admitted through
the Northstar EvidenceGateway, and an immutable-success / latest-attempt truth model. No trading
capability, no broker mutation, no automatic config apply, no decision-semantic change.
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
- **Structural human-authority tests** (`tests/test_broker_human_authority.py`) scan the whole
  package source: the only non-GET HTTP verb is the OAuth token `POST`; no module writes
  `config.json`; the proposal hardcodes `operator_approval_required: true` / `auto_applied: false`;
  no broker module imports an LLM, controller or worker surface; the auth/evidence modules expose
  no execute/order/trade/approve/apply names.

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
5. The token is saved through the single-writer token store (default `data/schwab_token.json`,
   or `SCHWAB_TOKEN_PATH`; mode `0600`, atomic write, lock-serialized). Subsequent runs refresh the
   access token automatically when it expires.

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
- `unknown` is the inert/legacy state (token predates tracking, or uncredentialed). It populates
  **only on the next interactive re-auth** — never on an access-token refresh; it is **not** an alert.

**Conservative anchor rule (v2).** Whether Schwab's refresh-token rotation renews the interactive
authorization lifetime is a Schwab-controlled property this repository does **not** assume. The
anchor is set only by the browser flow (`exchange_code`). An access-token refresh — even one that
returns a *different* `refresh_token` string — carries the prior anchor forward unchanged and records
`authorization_expiry_basis: interactive_auth_anchor`; a legacy token with no anchor stays
`unknown` rather than being given an invented 7-day window. Rotation is **reported** in
`schwab_token_lifecycle.json` (`refresh_token_rotated`, one-way `refresh_token_fingerprint`) so the
real behaviour can be studied from telemetry, without production availability depending on it.
(Earlier revisions of this document and of `schwab_oauth.refresh` treated a rotated string as a
renewed window; that claim was unproven and has been withdrawn.)

The daily tool-analysis surfaces `due_soon`/`expired` as AMBER (never RED — observe-only). When you
see it, re-run the OAuth flow above; only that resets the anchor to a fresh 7-day window.

#### Optional: one-tap re-auth (auto-capture)

Instead of copy-pasting the `?code=`, the `schwab_reauth` task captures it
server-side through an on-demand cloudflared tunnel. One-time setup (your
Cloudflare account):

```bash
curl -L --output /tmp/cloudflared.deb \
  https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
sudo dpkg -i /tmp/cloudflared.deb
cloudflared tunnel login                         # pick the portfolio-ops-center.com zone
cloudflared tunnel create stockbot-reauth
cloudflared tunnel route dns stockbot-reauth stockbot.portfolio-ops-center.com
```

Then add `SCHWAB_REAUTH_TUNNEL_NAME=stockbot-reauth` to `.env`. Leave the tunnel
**created but not running** — the task starts it on demand and tears it down after.
Verify readiness: `python3 -m portfolio_automation.brokers.schwab_reauth --check`.

To re-auth: `python3 -m portfolio_automation.brokers.schwab_reauth --begin`. It
brings up the tunnel, emails + prints the authorize URL, waits up to 5 minutes,
then on a successful tap captures the code, exchanges it, and tears the tunnel
down. Outcome is written to `outputs/latest/schwab_reauth_session_status.json`.

> **The re-auth tunnel must NOT be shared with another service.** `--begin` runs
> cloudflared with its own isolated `--config` (callback host → the ephemeral
> listener, 404 fallback) so it ignores `~/.cloudflared/config.yml`. This is
> required because cloudflared refuses the older `--url` flag whenever the
> ambient config carries an `ingress` block. But isolation only fixes *this*
> process — if a **persistent** cloudflared service is also serving the same
> named tunnel (e.g. the gui_v2 dashboard reused `stockbot-reauth`), Cloudflare
> load-balances callbacks across both connections and ~half hit the other
> service's ingress → 404 → `outcome=timeout`. Give the re-auth flow its **own
> dedicated tunnel**:
>
> ```bash
> cloudflared tunnel create stockbot-schwab-reauth
> cloudflared tunnel route dns stockbot-schwab-reauth schwab-reauth.portfolio-ops-center.com
> ```
> then set `SCHWAB_REAUTH_TUNNEL_NAME=stockbot-schwab-reauth` and
> `SCHWAB_REDIRECT_URI=https://schwab-reauth.portfolio-ops-center.com/schwab/callback`
> in `.env`, and register that redirect URI on the Schwab app. Leave the tunnel
> created-but-not-running (`--begin` starts it on demand). Until a dedicated
> tunnel is in place, fall back to the manual `exchange_code` flow (§2).

#### Optional: email heads-up (out-of-band)

For an unattended operator, the daily pipeline (Stage 10d, `schwab_reauth_notifier`) can also **email**
you once per expiry window when re-auth is `due_soon`/`expired`. It is **default-inert** and reuses the
memo sender's SMTP transport (same mailbox), so there are no new credentials beyond an enable flag:

| Variable | Default | Description |
|---|---|---|
| `SCHWAB_REAUTH_EMAIL_ENABLED` | `0` | `1` to enable the email heads-up |
| `SCHWAB_REAUTH_EMAIL_DRY_RUN` | `0` | `1` to build + gate but not send |
| `SCHWAB_REAUTH_EMAIL_TO` | `MEMO_EMAIL_TO` | recipients (comma-separated); falls back to the memo recipients |
| `SCHWAB_REAUTH_EMAIL_FORCE` | `0` | `1` to re-send even if this window was already notified |

SMTP transport is shared with the memo sender (`MEMO_EMAIL_SMTP_HOST` / `_PORT` / `_USERNAME` /
`_PASSWORD` / `_FROM` / `_USE_TLS`). One email is sent per `(reauth_status, expiry-window)` — you get a
single `due_soon` heads-up and, if it actually lapses, one `expired` alarm; a fresh re-auth re-arms it.
Status: `outputs/latest/schwab_reauth_notification_status.json`; audit: `outputs/policy/schwab_reauth_notification_log.jsonl`.
Test it any time with `python3 -m portfolio_automation.brokers.schwab_reauth_notifier --dry-run`.

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
| `SCHWAB_TOKEN_PATH` | No | `<checkout>/data/schwab_token.json` | Token-store file. Recommended in production: `/var/lib/stockbot/broker/schwab/token.json` (outside any release checkout, so a release pointer swap never changes the live token). Setting it does **not** move an existing token — see "Token store migration". |
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

## Authentication Lifecycle — Single-Writer Authority (v2)

```text
scheduler / CLI / GUI / agents
        │  ask, never refresh on their own
        ▼
SchwabAuthManager.acquire()            portfolio_automation/brokers/schwab_auth_manager.py
        │  lock ─► load ─► classify ─► (one refresh) ─► persist ─► unlock
        ▼
TokenStore                             portfolio_automation/brokers/schwab_token_store.py
        │  SCHWAB_TOKEN_PATH | legacy default; 0600; temp+fsync+rename; sidecar .lock
        ▼
schwab_oauth._post_token               the ONLY network call (token endpoint, POST)
```

- **Automatic reads.** Access-token refresh is autonomous and bounded: exactly **one** refresh
  attempt per `acquire()`, no retry loop, no sleep. A refresh that fails leaves the stored token
  untouched.
- **Manual interactive boundary.** The authorization-code flow (browser, Schwab login, MFA) is the
  operator's. Nothing here stores a Schwab username/password, automates MFA, or drives a browser.
- **Structured states** (`AuthState`): `OK`, `ACCESS_REFRESHED`, `REAUTH_DUE_SOON` (usable now,
  re-auth needed soon), `REAUTH_REQUIRED` (no usable token), `AUTH_REJECTED` (Schwab refused the
  refresh: 400/401/403 → re-auth), `RATE_LIMITED` (429, transient), `SCHWAB_UNAVAILABLE` (5xx /
  transport, transient), `STORE_BUSY` (another writer holds the lock, transient), `UNCONFIGURED`.
  A transient failure is never reported as an expired authorization, and vice versa.
- **Legacy facade.** `schwab_oauth.valid_access_token()` still exists and simply flattens the
  manager's result to a token or `None`; it no longer contains its own refresh logic.
- **Telemetry** (`outputs/latest/schwab_token_lifecycle.json`, non-secret): `auth_state`,
  `refresh_attempted_at`, `refresh_succeeded`, `refresh_token_present`, `refresh_token_rotated`,
  `refresh_token_fingerprint` (one-way sha256 prefix), `access_token_expires_in`,
  `authorization_expiry_basis`. Never token bytes.

### Token store migration (explicit, operator-run)

The v2 code does **not** move the production token. To adopt the recommended path:

1. `install -d -m 0700 /var/lib/stockbot/broker/schwab`
2. Copy the current token file there with mode `0600` (owner = the service user), while no sync
   is running.
3. Set `SCHWAB_TOKEN_PATH=/var/lib/stockbot/broker/schwab/token.json` in the environment the
   scheduler and GUI use.
4. `python3 -m portfolio_automation.brokers.schwab_sync --status` and confirm `auth_state` is
   `OK`/`ACCESS_REFRESHED`; then remove the old file.

Rollback: unset `SCHWAB_TOKEN_PATH` (the legacy default is used again).

## Token and Security Notes

- **Token file location:** `SCHWAB_TOKEN_PATH`, else `data/schwab_token.json` at the repo root.
  The `/data/` directory is gitignored at repo root, so the legacy token file is excluded from commits.
- **Single writer:** every save takes the sidecar `<token>.lock` (exclusive, bounded wait) so an
  operator re-auth and a scheduled refresh can never interleave.
- **Atomic writes:** temp file in the same directory, `fsync`, `os.replace`. An interrupted write
  never truncates the token and never clobbers a good one; temp files are cleaned up.
- **File permissions:** the token file is written with mode `0600` (owner read/write only) and a
  directory the store creates is `0700`. Confirm with `ls -la "$SCHWAB_TOKEN_PATH"`.
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

Acquires a token through `SchwabAuthManager`, calls `GET /trader/v1/accounts/accountNumbers` and
`GET /trader/v1/accounts?fields=positions`, normalizes the response into a
`BrokerPortfolioSnapshot`, adapts it to a canonical `EvidenceSnapshot`, submits it to the
EvidenceGateway, and writes:

- `outputs/latest/broker_sync_status.json` (every attempt)
- `outputs/latest/schwab_token_lifecycle.json` (every configured attempt; non-secret)
- `outputs/latest/broker_evidence_latest_attempt.json` (every attempt)
- `outputs/latest/broker_evidence_latest_admitted.json` (**only** when ADMITTED)
- `outputs/latest/schwab_portfolio_snapshot.json`, `outputs/latest/schwab_positions.json` —
  compatibility **projections derived from the admitted evidence** (only when ADMITTED)
- `outputs/archive/broker_evidence/<YYYY-MM-DD>/<snapshot_id>.json` (write-once archive)
- Legacy archive copies under `outputs/archive/broker_sync/<YYYY-MM-DD>/`

Fails closed (writes an `error` status artifact and a non-ADMITTED attempt record) if unconfigured,
unauthenticated, if the API call fails, if the response is malformed/empty, or if the gateway
refuses the evidence. Never raises to the caller. **A failed sync never rewrites the projections
and never produces an empty portfolio** — see "Broker Evidence Truth Model".

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

## Broker Evidence Contract (v2)

`portfolio_automation/brokers/broker_evidence.py` defines `BrokerPortfolioSnapshot` — the
normalized result of one successful sync — and `portfolio_automation/brokers/schwab_evidence_adapter.py`
turns it into a canonical Northstar `EvidenceSnapshot`. Concerns are separated on purpose:

| Concern | Module | Network / secrets |
|---|---|---|
| Acquisition | `schwab_auth_manager.py`, `schwab_client.py` | yes (token endpoint; two GETs) |
| Pure normalization | `broker_models.py`, `broker_evidence.py` | none |
| Evidence construction + admission | `schwab_evidence_adapter.py` | none |
| Persistence / truth model | `broker_evidence_store.py` | none |

Contract fields: `contract_type: broker_portfolio_snapshot`, `schema_version: 1.0.0`,
`source_id` (kernel `DataSourceDescriptor` id, `src_…`; label `schwab_trader_api`), `sync_id`,
`retrieved_at`, `effective_at` (absent — Schwab supplies no source timestamp; never fabricated),
`normalizer_id` / `normalizer_version`, `source_commit`, `endpoints`, masked account references
(`…NNNN` only), `positions`, balances/`totals`, `payload_hash`, `broker_snapshot_id` (canonical
identity: same portfolio facts → same id across runs; `sync_id`/`retrieved_at`/`source_commit` are
excluded from identity). A construction-time tripwire rejects raw account numbers, tokens, client
secret, authorization codes/headers and username/password material.

Adapter PIT semantics: `retrieved_at` = acquisition instant; `known_at` derived by the kernel's one
sanctioned conservative rule (possession); `observed_at`/`published_at` absent. Provenance:
`producer_type: source_adapter`, `code_version` = source commit, `transformation_id` = normalizer +
sync id.

## EvidenceGateway Admission

Admission is delegated unchanged to `portfolio_automation/evidence_gateway/admission.py`
(point-in-time → identity → provenance → reference). No gateway rule was added or weakened for
Schwab. Tested outcomes (`tests/test_broker_evidence_contract.py`): valid snapshot → ADMITTED;
future-dated (lookahead) → REFUSED; payload tampering / hash mismatch → REFUSED; provenance source
mismatch → REFUSED; malformed evidence → REFUSED; wrong reference → REFUSED. **A REFUSED snapshot is
never projected**: `project_snapshot_dict` / `project_positions_dict` raise on any non-admitting
decision, and the sync records `EVIDENCE_REFUSED` instead of writing truth.

## Broker Evidence Truth Model (last-known-good)

`portfolio_automation/brokers/broker_evidence_store.py` keeps two records apart:

| Record | Written by | Meaning |
|---|---|---|
| `broker_evidence_latest_attempt.json` | every attempt | what the most recent sync did (`outcome`, `auth_state`, admission decision, redacted error, `implies_empty_portfolio: false`) |
| `broker_evidence_latest_admitted.json` | admitted success only | the last-known-good canonical `EvidenceSnapshot` + its admission decision |

`read_state()` re-verifies the admitted record (`payload_hash`, `snapshot_id`, decision) on every
read and classifies truth as `CURRENT` (latest attempt admitted this snapshot),
`STALE_LAST_KNOWN_GOOD` (an admitted snapshot exists but the latest attempt did not replace it:
failed / refused / re-auth required), or `NO_TRUTH`; `reauth_required` is an orthogonal flag.
`positions()` returns `None` (unknown) when nothing is admitted — **never `[]`** as a stand-in for a
failure. `broker_sync_status.json` surfaces `auth_state`, `evidence_truth_status`,
`evidence_snapshot_id`, `evidence_last_attempt_outcome`, `evidence_admitted_age_s` additively.

Failure classes recorded as `outcome`: `UNCONFIGURED`, `REAUTH_REQUIRED`, `AUTH_UNAVAILABLE`
(transient auth), `ACQUISITION_FAILED`, `SCHEMA_DRIFT` (response not a list), `NORMALIZATION_FAILED`
(including an empty account list — an empty response is not an empty portfolio),
`EVIDENCE_REFUSED`.

### Compatibility projections (not a cutover)

`schwab_portfolio_snapshot.json` and `schwab_positions.json` keep their legacy shape (all existing
fields) and gain `evidence_snapshot_id`, `evidence_payload_hash`, `projection_of`. They are now
written **only** from admitted evidence. Consumers (holdings_resolver, memo enrichment, GUI
portfolio-sync page, reconciliation) are unchanged; the Decision Core cutover to reading the
canonical record directly is out of scope for this foundation.

## Rollback (v2 foundation)

The change is additive. To roll back: deploy the prior release; the legacy artifacts keep their
shape, `data/schwab_token.json` remains readable by the old `schwab_oauth` (the v2 token file adds
only `authorization_expiry_basis`, which old code ignores), and the new `broker_evidence_*` /
`schwab_token_lifecycle.json` artifacts are simply no longer refreshed (registered `on_demand`,
`severity_if_missing: info`). If `SCHWAB_TOKEN_PATH` was adopted, unset it or copy the token back
to the legacy path before rolling back.

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

### Human approval boundary

```text
READ / OBSERVE                       autonomous (scheduled sync, refresh, evidence admission)
WRITE LOCAL AUTHORITATIVE STATE      proposal-only; explicit human approval via the manual tool
BROKER MUTATION (orders/transfers)   not implemented; structurally prevented and tested
```

No LLM, agent, controller, cron job, supervisor or worker counts as human approval. The proposal
artifact is inert until an operator applies it; `tests/test_broker_human_authority.py` and
`tests/test_schwab_sync.py::test_reconcile_does_not_mutate_config` pin this.

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
| `status=error`, `auth_state=REAUTH_REQUIRED` / `AUTH_REJECTED` | No usable token, or Schwab refused the refresh | Re-run the OAuth flow (`schwab_reauth --begin` or `exchange_code`) |
| `status=error`, `auth_state=RATE_LIMITED` / `SCHWAB_UNAVAILABLE` / `STORE_BUSY` | Transient (429 / 5xx / lock held) | Nothing — next scheduled run retries; last-known-good truth is preserved |
| `evidence_truth_status=STALE_LAST_KNOWN_GOOD` | Latest attempt did not replace the admitted snapshot | Check `broker_evidence_latest_attempt.json` → `outcome` / `error` |
| `evidence_last_attempt_outcome=EVIDENCE_REFUSED` | Gateway refused the snapshot (PIT / integrity / provenance) | Investigate; never edit the evidence files by hand |
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
| `portfolio_automation/brokers/schwab_oauth.py` | OAuth2 auth-url / exchange / refresh helpers; conservative anchor stamping; typed token-endpoint errors; `valid_access_token` facade over the auth manager |
| `portfolio_automation/brokers/schwab_token_store.py` | `TokenStore`: `SCHWAB_TOKEN_PATH` resolution, 0600 atomic writes, sidecar lock (bounded), absent/corrupt distinction, one-way fingerprints |
| `portfolio_automation/brokers/schwab_auth_manager.py` | `SchwabAuthManager` / `AuthState` / `AuthResult`: the single token-lifecycle authority (one bounded refresh, classified failures, non-secret telemetry) |
| `portfolio_automation/brokers/schwab_client.py` | Read-only `SchwabClient` with `get_account_numbers()` / `get_accounts()` only — no trade methods |
| `portfolio_automation/brokers/broker_evidence.py` | `BrokerPortfolioSnapshot` versioned contract + Schwab `DataSourceDescriptor`; secret/raw-account tripwire; canonical identity |
| `portfolio_automation/brokers/schwab_evidence_adapter.py` | `to_evidence_snapshot`, `admit_broker_snapshot` (delegates to the gateway), compatibility projections that refuse unadmitted evidence |
| `portfolio_automation/brokers/broker_evidence_store.py` | latest_attempt / latest_admitted records, write-once archive, `read_state()` truth classification, `SyncOutcome` |
| `portfolio_automation/brokers/schwab_sync.py` | Orchestrator + CLI; auth → acquire → normalize → evidence → admit → persist → project; never raises |

---

*No real secrets, credentials, or personal account data appear in this document.*
