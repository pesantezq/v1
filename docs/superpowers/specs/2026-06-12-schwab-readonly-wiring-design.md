# Schwab Read-Only Sync — Activation Wiring Design

**Date:** 2026-06-12
**Status:** Approved (owner, 2026-06-12)
**Builds on:** `docs/schwab_integration.md`, `docs/superpowers/specs/2026-06-08-schwab-readonly-sync-design.md`
**Roadmap:** post-activation step 3 (Schwab read-only); re-sequenced ahead of §14 dashboard polish per owner (2026-06-12).

---

## Goal

Activate the already-built, observe-only Schwab read-only broker sync: flip
`broker_sync_status` from `unconfigured` → `ok`, refreshed **daily** by the cron
**and** re-triggerable **on-demand** (CLI / GUI). All read-only invariants are
preserved unchanged — this is wiring + provisioning, not new capability.

## Non-Goals (YAGNI / hard boundaries)

- No trade path. The AST no-trade test must keep passing untouched.
- No auto-apply of reconciliation diffs to `config.json` — stays proposal-only,
  operator-applied via the existing reviewed safe-writer.
- No decision-core read/write. Schwab data is evidence only.
- `trading_enabled` / `read_only_mode` / `observe_only` stay hardcoded.

## Read-Only Safety Model (unchanged, restated)

Enforced + tested today; this design touches none of it:
`portfolio_automation/brokers/` contains no order/trade methods (AST-scanned by
`test_schwab_client.py::test_no_trading_capability_anywhere_in_brokers_package`);
`broker_sync_status.json` hardcodes `trading_enabled:false`, `read_only_mode:true`,
`observe_only:true`.

## Two Tracks

### Track A — Operator provisioning (owner inputs only)

The irreducible human step is the OAuth authorize click (Schwab requires the
account owner; cannot be automated). Everything else is automated by Claude.

1. Owner places 3 secrets in `/opt/stockbot/.env` (gitignored) via the `!`-prefix
   safe channel so `CLIENT_SECRET` is not echoed into the session transcript:
   `SCHWAB_CLIENT_ID`, `SCHWAB_CLIENT_SECRET`, `SCHWAB_REDIRECT_URI`
   (+ optional `SCHWAB_READ_ONLY_MODE=true`, `TRADING_ENABLED=false`).
2. Claude generates the authorize URL (`schwab_oauth.build_authorize_url()`).
3. Owner opens it, logs into Schwab, clicks Allow, pastes back the `?code=...`.
4. Claude runs `schwab_oauth.exchange_code(code)` → token persisted to
   `data/schwab_token.json` (mode 0600, gitignored); auto-refreshed thereafter.

### Track B — Code wiring (Claude, additive / observe-only / non-blocking)

Buildable and testable NOW without credentials (degrades gracefully while
unconfigured), so it runs in parallel with Track A.

1. **`portfolio_automation/env.py`** — register the 5 `SCHWAB_*` env vars in the
   env-var registry so `preflight.sh`'s env check recognizes them and absence
   degrades gracefully (today they are unknown to the registry). All optional at
   the registry level (the layer self-reports `unconfigured`); never required, so
   preflight stays green pre-provisioning.
2. **`scripts/run_daily_safe.sh`** — new **non-blocking** broker-sync stage placed
   *before* Stage 11 (daily_run_status) so Stages 11/12/13 count fresh broker data.
   Invokes `python -m portfolio_automation.brokers.schwab_sync --sync --reconcile`
   wrapped in `try/except`/`|| true` semantics — a Schwab API or token failure
   degrades `broker_sync_status` to `error`/`unconfigured` and NEVER aborts the
   pipeline. Mirrors the existing stage-wrapper style.
3. **`portfolio_automation/artifact_registry.yaml`** — change ONLY
   `broker_sync_status.json` cadence `on_demand` → `daily` (it is always-producible,
   so daily is honest). The 4 advisor artifacts (`schwab_portfolio_snapshot`,
   `schwab_positions`, `portfolio_reconciliation`, `portfolio_config_update_proposal`)
   STAY `on_demand` — they only populate post-auth, so daily cadence would
   manufacture stale-flag noise while unconfigured or on a failed sync. All stay
   `required:false`/`severity:info`.
4. **`.claude/commands/daily-tool-analysis.md`** — reclassify the broker-sync note:
   `broker_sync_status` is now a **daily** producer; `unconfigured`/`disabled` is
   still the inert pre-provisioning steady state (report, don't alert),
   `degraded`/`error` → AMBER (logic already present), never RED. This also gives
   the pipeline-wiring probe a daily caller for `broker_sync_status`, so it reads
   `healthy` rather than eventually `unwired`/stale.
5. **Tests** (`tests/`):
   - daily broker-sync stage is non-blocking + idempotent when uncredentialed
     (`run_sync`/`run_reconcile` return graceful `unconfigured`, no raise);
   - `artifact_registry.yaml` `broker_sync_status` cadence == `daily`, advisors
     still `on_demand`;
   - env registry contains the 5 `SCHWAB_*` entries and preflight env-check passes
     with them absent.

## Data Flow

```
.env (SCHWAB_*) + data/schwab_token.json
      │
      ▼
schwab_oauth (auth/refresh) → schwab_client (read-only: accounts, positions)
      │
      ├─ run_sync      → broker_sync_status.json (daily) + schwab_portfolio_snapshot.json + schwab_positions.json (on_demand)
      └─ run_reconcile → portfolio_reconciliation.json + portfolio_config_update_proposal.json (on_demand, proposal-only)
      │
      ▼
daily-tool-analysis (developer/risk lens) reads broker_sync_status → heartbeat line
GUI sync view (cockpit M6) renders snapshot + reconciliation proposal (operator applies manually)
```

## Failure Handling

- Unconfigured (no creds): `--sync` writes `broker_sync_status: unconfigured`;
  advisors absent (info-missing, not a fault). Inert steady state.
- Token expired/rotated: daily stage degrades to `error` (AMBER) until owner
  re-auths. **Daily is best-effort, not guaranteed** — accepted by owner.
- Schwab API/network error: non-blocking; pipeline continues; AMBER, never RED.

## Verification

- `python -m portfolio_automation.brokers.schwab_sync --status` →
  `configured:true, authenticated:true`, account/position counts > 0.
- Dry daily run shows the new broker-sync stage; `broker_sync_status` fresh + `ok`.
- `pipeline_wiring_status` lists `broker_sync_status` as `healthy` with a daily caller.
- `pytest` targeted suite green; full suite per VPS validation (note the
  signal_registry test-isolation caveat).

## Rollback

Pure config/env: remove `SCHWAB_*` from `.env` (or `SCHWAB_READ_ONLY_MODE=false`),
revert the registry cadence + the daily stage. Layer returns to `unconfigured`
inert state. No decision-core or schema impact.
