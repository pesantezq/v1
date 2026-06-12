# Schwab Re-Auth Auto-Capture — Ephemeral On-Demand Design

Date: 2026-06-12
Status: Approved (brainstorm) — pending spec review → implementation plan
Owner: Enrique Pesantez
Depends on: shipped re-auth warning layer (`a0a1d869`) + email notifier (`d60e3e3b`)

## Goal

Make the mandatory weekly Schwab re-authorization a single tap. Today the
refresh-token expiry is surfaced (AMBER + optional email) but the actual re-auth
is a manual `build_authorize_url` → browser → copy `?code=` → `exchange_code`
relay. This feature captures the `?code=` server-side so the operator's only
action is approving the login in the Schwab app on their phone.

Target UX:

1. Re-auth email arrives (already built) when `reauth_status` is `due_soon`.
2. Operator triggers the self-contained task on the VPS (`schwab_reauth --begin`).
3. The task brings up an on-demand tunnel, surfaces the authorize URL (email +
   terminal print + optional QR), and waits.
4. Operator taps the URL on their phone → Schwab app approve (the only manual step).
5. Schwab redirects to `/schwab/callback` through the live tunnel → the task
   auto-captures the code, calls `exchange_code()`, tears the tunnel down, and
   reports the new 7-day expiry. No copy-paste, no VPS typing.

## Hard constraint (Schwab, not negotiable)

Schwab's OAuth authorize step is an interactive browser flow. There is no API to
initiate a device-push authorization headlessly, and no password/credential
grant. So a fully zero-touch scheduled re-auth is impossible — the one
browser/app tap that fires the phone approval is unavoidable (and is itself the
desired "I approve every login" security property). Everything *except* that tap
is automated.

## Non-Goals (YAGNI / hard boundaries)

- No fully zero-touch or blindly-scheduled re-auth (Schwab requires the tap).
- No always-on public trigger endpoint (operator initiates the task).
- No standing public inbound surface; `gui_v2` is not modified.
- No change to scoring, `decision_engine.py`, or any score/decision semantics.
- No trade/execution capability (the brokers package remains AST-enforced read-only).

## Exposure model (decided)

Ephemeral on-demand. A **named** cloudflared tunnel (required — the registered
redirect URI is the fixed `stockbot.portfolio-ops-center.com/schwab/callback`, so
a random quick-tunnel hostname would not match Schwab) is started only for the
~2-minute re-auth window and torn down afterward. When down, the hostname returns
521 (today's steady state). The box has no standing public endpoint 99.9% of the
time.

## Listener architecture (decided: Approach A)

A dedicated ephemeral in-process listener owned by the `--begin` command — NOT a
route in the always-on dashboard. The endpoint exists only while the command
runs, in its own process, and dies with it. `gui_v2` is untouched.

## Components

1. **`portfolio_automation/brokers/schwab_reauth.py`** *(new)* — orchestrator +
   CLI (`--begin`, plus `--check` to print cloudflared/config readiness). Owns the
   state machine and guarantees tunnel teardown via try/finally.
2. **Ephemeral callback listener** — a stdlib `http.server.BaseHTTPRequestHandler`
   bound to `127.0.0.1:<port>`. Handles a single `GET /schwab/callback`: validates
   the `state` nonce, extracts `code` (or surfaces a Schwab `error=`), hands the
   code to the orchestrator, returns a minimal "you can close this tab" HTML page,
   and signals completion. Accepts exactly one valid callback, then stops.
3. **Tunnel manager** — thin wrapper that runs `cloudflared tunnel run <name>` as
   a subprocess and kills it on success or timeout (context manager). Detects a
   missing/misconfigured cloudflared and returns a readiness error with setup steps.
4. **OAuth state nonce** — extend `schwab_oauth.py`:
   `build_authorize_url(state=...)` already accepts a `state`; add
   `generate_state()` (random 32-byte urlsafe) and `verify_state()` (constant-time)
   plus 0600 persistence with a TTL. Additive; no score/decision semantics changed.
5. **Reuse (unchanged):** `exchange_code()`, the email transport
   (`memo_email_sender` / `schwab_reauth_notifier`), `SCHWAB_REDIRECT_URI` from env,
   `redact()`.

## Data flow

```
operator runs `schwab_reauth --begin`
  → generate_state() nonce persisted (data/schwab_reauth_state.json, 0600, ~10-min TTL)
  → tunnel manager: cloudflared tunnel run <name>  (subprocess up)
  → build_authorize_url(state=nonce)
  → surface URL: email (reuse transport) + terminal print + optional QR
  → ephemeral listener waits on 127.0.0.1:<port>  (timeout 300s)
operator taps URL on phone → Schwab login + app approve
  → Schwab 302 → https://stockbot.portfolio-ops-center.com/schwab/callback?code=&state=
  → tunnel routes hostname → 127.0.0.1:<port>
  → listener: verify_state(state) [constant-time, TTL] → extract code
       → hands (code) to the orchestrator via a thread-safe handoff (queue/Event)
       → returns "you can close this tab" page, then stops accepting
  → orchestrator (main thread): exchange_code(code) → data/schwab_token.json saved (0600)
       → refresh-token 7-day clock re-anchored; reauth_status → ok
  → orchestrator tears down tunnel (try/finally)
  → write schwab_reauth_session_status.json (observe_only)
  → print success + new expiry
```

## Security model

- **Nonce:** random 32-byte urlsafe, single-use, TTL ~10 min, persisted 0600,
  compared constant-time (`hmac.compare_digest`). Callback rejected on
  mismatch/expiry/absence.
- **Binding:** listener bound to `127.0.0.1` only; reachable solely via the
  authenticated named tunnel. Accepts exactly one valid callback, then stops.
- **Window:** tunnel up only during the window; hard timeout → teardown even with
  no callback. Teardown is guaranteed (try/finally), including on exception/SIGINT.
- **Secrets:** `code`/token never logged; callback query string redacted in any
  log line; token written 0600 by existing `save_token`.
- **Rejection cases:** missing/old nonce, missing `code`, Schwab `error=` param,
  second callback after one already handled.
- **No new always-on surface:** `gui_v2` unchanged; nothing listens publicly
  outside the window.

## Error handling / degradation

| Condition | Behavior |
|---|---|
| cloudflared missing / not logged in / no named tunnel | No tunnel started; print setup steps; exit non-zero. `outcome=cloudflared_missing`. |
| Timeout, no callback | Teardown; token unchanged; `outcome=timeout`. |
| Nonce/state invalid | Listener 400; no exchange; keep waiting until timeout. |
| `exchange_code` fails | Teardown; sanitized error; old token preserved; `outcome=error`. |
| Success | Token re-anchored; `outcome=success`; new expiry printed. |

Never touches the decision core; the only mutation is `data/schwab_token.json`
through the existing `exchange_code` path.

## Observe-only artifact + health coverage

- Writes `outputs/latest/schwab_reauth_session_status.json` with hardcoded
  `observe_only: true`, no secrets: `generated_at`, `started_at`, `outcome`
  (`success|timeout|error|cloudflared_missing`), `new_expires_at` (on success).
- Health: extend `.claude/commands/daily-tool-analysis.md` to read the session
  status for last-outcome visibility. The primary signal stays the existing
  `reauth_status` (→ `ok` after a successful capture). Per the repo's
  analysis+health-coverage rule, the new producer is paired with this daily check.

## Testing

- `generate_state` / `verify_state`: match, mismatch, expired, missing file.
- Listener handler: valid callback → `exchange_code` called once; bad/missing
  `state` → 400, no exchange; Schwab `error=` → handled, no exchange; second
  callback ignored. (Drive the handler via direct method calls or a localhost
  request with `exchange_code` and the tunnel mocked.)
- Orchestrator state machine with tunnel manager + listener mocked: success,
  timeout, cloudflared-missing, exchange-failure paths; assert teardown always runs.
- No-secret-in-artifact test for `schwab_reauth_session_status.json`.
- Targeted first, then full `pytest -q`.

## Infra prerequisite (operator, one-time, their Cloudflare account)

1. Install cloudflared on the VPS.
2. `cloudflared tunnel login` (browser, Cloudflare account).
3. `cloudflared tunnel create stockbot-reauth`.
4. DNS-route `stockbot.portfolio-ops-center.com` → that tunnel
   (`cloudflared tunnel route dns ...`).
5. Store tunnel name/creds where the manager reads them (config or env, e.g.
   `SCHWAB_REAUTH_TUNNEL_NAME`).

`schwab_reauth --check` validates these and prints any missing step. Documented in
`docs/schwab_integration.md`.

## Optional enhancement (low priority)

Terminal **QR code** of the authorize URL so the operator can scan it from the VPS
terminal with their phone instead of relaying the link. Implement only if a
no-new-heavy-dependency option is available (e.g. a tiny pure-Python QR or ASCII
renderer); otherwise skip. Not on the critical path.

## Rollback

Feature is a new standalone module + an additive nonce helper + one daily-skill
read. Rollback = revert the commit; the manual `exchange_code` relay (documented)
remains the fallback at all times. No data migration.
