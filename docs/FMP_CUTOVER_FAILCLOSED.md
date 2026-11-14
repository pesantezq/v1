# FMP-4 — Fail-closed cutover safety (issue #78)

Status: **candidate, not deployed.** Isolated branch `research/fmp-f4-failclosed-20261114`.
No cron / systemd / `config.json` / `.agent` change. No live activation.

## Why

The paid FMP Starter plan ends **2026-11-27**; the account becomes Free/Basic.
Two things are **unconfirmed**:

1. Free/Basic entitlement for Northstar symbols/endpoints.
2. The right to keep using cached or derived paid-tier data after the plan ends.

Until both are resolved, the only safe posture is a **universal HOLD**. It does not
assume Basic entitlements and has no opt-in auto-enable.

## What the HOLD does (from `2026-11-26T00:00:00Z`)

The HOLD starts 24h before 2026-11-27T00:00Z. "Nov 27" starts no earlier than
2026-11-26T10:00Z in any civil timezone (UTC+14), so the HOLD is already active
under every wall-clock or DST reading of the provider switch.

| Surface | Behaviour after the HOLD instant |
|---|---|
| `FMPClient._raw_get` (all cached/retrying methods, generic `get_json`, legacy v3/v4) | `FMPCutoverRefused` (an `FMPError`) raised **before** the URL or API key is assembled; re-checked before **every retry**; independent of `daily_budget` (0/uncapped does not help). |
| `FMPClient._raw_get_once` + VS-002 strict-live methods | Refused before `on_attempt`, before the budget counter is touched, and before any request. No `/full` substitution, no other vendor. |
| `_DiskCache.get` / `get_stale` | Return `None` (files are not opened, **not deleted**). No fresh or stale subscribed data is served silently. Callers then report data as *missing*. Prices are never zero-filled. |
| `scripts/run_daily_safe.sh` | Sources `scripts/fmp_cutover_guard.sh` **before** preflight, venv, `.env`, broker sync, and any outbound call. Exits **3** with a `FMP_CUTOVER_HOLD …` line and `DAILY RUN FAILED`. (FMP4-R1: the guard is now self-located, see below.) |
| `scripts/run_monthly_universe_refresh.sh` | Same guard **before** the flock, `.env` and the ~3,700-call rebuild. Exits **3**. The HOLD line goes to the log and stderr. |
| FMP4-R1 shell entrypoints: `run_daily.sh` (the live systemd `ExecStart`), `run_daily_sandbox_safe.sh`, `run_weekly_safe.sh`, `discovery_pulse.sh`, `run_weekly_etf_bundles.sh`, `historical_backfill.sh`, `pattern_loop_recheck.sh`, `pattern_loop_reconstruct.sh`, `server_run_pipeline.sh` | The guard runs as the **first action** after `set`. It runs before logs, flock, `.env`, venv, `main.py` and any broker/FMP call. For the ETF job it also runs before the kill-switch read. Exits **3**. A missing guard file also exits **3** (`reason=guard_missing`). |
| `main.py` | `main()` exits **3** (`FMP_CUTOVER_HOLD entrypoint=main.py …`) **before** the run lock, state store, broker overlay and cash-ledger sync. `run_portfolio_update` also fails closed at step 0: `data_mode=DATA_UNAVAILABLE`, `success=False`, and no summary, recommendations or decision plan. The `valid_holdings` predicate is re-gated, so a run that crosses the instant still stops. |
| `market_data.update_holdings_with_prices` | Under HOLD, makes **no** `get_prices` call. It clears every `current_price` and `market_value` to `None` and reports every symbol failed. A retained price (e.g. 100 × 10 shares = 1000) is no longer valued. |
| Direct `data/fmp_cache` readers: `flock_intelligence/data_sources.load_ticker_sector`, `historical_backfill._load_top100_symbols`, `universe_sanitation._load_fmp_top100` / `_load_sector`, `retune_impact_tracker._load_ticker_sector`, `cash_deployment_plan._sector_map_from_cache` | Each one checks the single shared accessor `fmp_cutover.fmp_cache_read_hold_reason(path)` first. This is a path-only check (any `fmp_cache` path segment); it is not a registry and has no allow-list. Under HOLD the reader returns `None` / `[]` / `"Unknown"` / `{}` and never opens the file. Files are **not deleted**. |
| `scanner/candidate_scanner.py`, `scanner/fallback_watchlist.py` | Under HOLD (explicit local gate on the same clock rules): `daily_refresh` returns `([], [])`, so there is no `q.price or row.price` re-stamp with a fresh `scanned_at`. `save_watchlist` and the fallback `save` write nothing. `load_watchlist` returns `[]` and `load_watchlist_screening` returns `None`. |
| `scripts/probe_fmp_crowd_endpoints.py` (manual, direct `urlopen`) | Exits **3** before `FMP_API_KEY` is read. Its HTTP function re-checks the HOLD before **every** request, before the URL or key is assembled. |

Exit code **3** means HOLD on every guarded surface.

Clock rules: a naive (timezone-ambiguous), non-datetime or failing clock counts as
**HOLD**. No environment variable, config key, or flag can lift the HOLD.
`FMP_CUTOVER_GUARD_NOW_EPOCH` (bash, tests only) can only move time *forward*: the
guard uses `max(real, injected)`. An unreadable `date` output is also a HOLD.

Before the HOLD instant, all behaviour is byte-for-byte unchanged: same transport,
retries, cache, budget counter and v3 access.

## Basic provider-window model (inactive)

`portfolio_automation/data_budget/fmp_cutover.py::BasicProviderWindowBudget` is a
pure, in-memory, clock-injected model for **future** Basic-only research. It is
**not wired** into `fmp_client`, and it refuses everything unless
`basic_use_authorized=True`:

* a rolling 24h count of at most 250 *attempts*. It does not rely on the disputed
  fixed reset ("3 PM EST" in the FAQ, which may not be fixed across accounts);
* every HTTP attempt counts, recorded **before** the request, including failed
  attempts and each retry;
* 30-day raw bandwidth of 500 MB (decimal). It fails closed whenever upstream
  account usage or balance is unknown (`upstream_usage_bytes=None`);
* legacy `/api/v3` and `/api/v4` paths are rejected;
* cached or stale data is never admissible (`cached_data_rights_unresolved`).

## Reopening (not implemented, by design)

Any reopening needs **all** of the following:

1. A recorded, human-approved **scoped grant**: which endpoints/symbols on which
   plan, and whether pre-cutover cached/derived data may be used.
2. A reviewed **code change** to this guard. A config toggle is not enough; the
   guard has none.
3. An **independent deployment gate**: operator approval and VPS validation,
   separate from the code review.

If Basic access is granted, wire `BasicProviderWindowBudget` in at the transport
boundary under that same gate.

## Rollback

Rollback must preserve the post-cutoff HOLD. Reverting this guard AFTER the
HOLD instant would reopen FMP traffic without an entitlement/rights grant and
is NOT an authorized rollback. Before the HOLD, an approved revert is
stateless for the guard itself (no cache deletion). After the HOLD, restore
an independently accepted HOLD-capable release or explicitly pause/repoint
the affected jobs under separate operator change control. Existing pipeline
paths may emit failed-run status; no global no-write claim is made.

## Known limitations

The FMP4-R1 repair below resolves the earlier items on test clocks, the probe
and the unguarded weekly/discovery/ETF entrypoints. What remains is listed under
*FMP4-R1 remaining gaps*.

## FMP4-R1 cutover-safety repair (2026-11-14, candidate, not deployed)

Branch `repair/fmp4-r1-cutover-safety-20261114`, issue #78. Draft PR #80 is
unchanged. It addresses the independent FMP-5 findings:

* **Root/release mismatch (P0).** systemd runs
  `/opt/stockbot/current/scripts/run_daily.sh`, which previously had no guard.
  Every guarded wrapper now sources `fmp_cutover_guard.sh` from
  `dirname "${BASH_SOURCE[0]}"`: the guard that ships with the executed release,
  never one resolved through `REPO_ROOT`/`REPO_DIR`/`$PWD`. So the HOLD applies
  even when the Python it would launch comes from an older checkout.
* **Inherited stale prices (P0).** These are now cleared or refused in
  `market_data` and `main.py` (see the table). Prices are never zero-filled.
* **Direct cache readers and scanner re-stamping (P1).** These now use the shared
  accessor or the explicit gate (see the table).
* **Deterministic tests (P1).** `tests/conftest.py` pins
  `fmp_cutover._system_clock` (test-only, autouse) to 2026-11-14T12:00Z.
  Tests that simulate a later system time re-patch it (2026-11-26T00:00Z,
  Nov 27, Jan 2027). For bash, the `fmp_bash_date` fixture uses `BASH_ENV` to
  define a test-only `date` function for `date -u +%s`. Production still uses
  `max(real, injected)` in Python and `max(real, FMP_CUTOVER_GUARD_NOW_EPOCH)`
  in bash. No env or config bypass was added.

Hold timing is unchanged: **2026-11-26T00:00:00Z** (epoch 1795651200), 24h
before the 2026-11-27 Starter→Free switch.

### Owner release approval

This repair is **not** deployment permission. Running it on the VPS needs, in
order:

1. Owner/operator review of this diff.
2. An approved coherent release and scheduler plan covering the pinned
   release (/opt/stockbot/current) and legacy root-path callers
   (/opt/stockbot). Do NOT blindly copy into the dirty root or mutate a
   pinned release in place. Certify SHAs/imports; where root cannot be
   adopted safely, require an approved guarded pause/repoint of those jobs.
3. VPS validation using only synthetic tests (no real wrappers with a loaded
   `.env`).
4. An explicit owner release approval before
   2026-11-26T00:00Z.

Reopening FMP access still needs the scoped grant, code change and gate
described in *Reopening*.

### FMP4-R1 remaining gaps

* **Deployment.** None of this is live. Until it is deployed to both trees, the
  P0 findings still stand on the VPS.
* **Monthly root divergence.** `run_monthly_universe_refresh.sh` (and
  `run_daily.sh`, `pattern_loop_*.sh`) still run Python from `/opt/stockbot`
  while the shell may come from the release. The early self-located guard makes
  this safe for the HOLD. Making root and release coherent, for example by
  deriving the root from the script location, would change pre-hold behaviour
  and is left to the release-pointer work.
* **Health/other wrappers.** Active run_sims_daily.sh (cached historical
  research) and manual rerun_today_safe.sh (DB reset) now guard before side
  effects. daily_check.sh, monthly_check.sh, yearly_check.sh and other health
  helpers remain unguarded. They should report HOLD, not be disabled
  indiscriminately. FMP calls inside them depend on the FMPClient guard
  being installed in the actual runtime release. Existing market artifacts
  must not be mislabelled fresh.

* **Other direct readers.** Only the seven readers named by FMP-5 are gated.
  Other code that opens `data/fmp_cache/*` directly (outside `_DiskCache`) is
  not yet inventoried. Derived artifacts already copied out of `fmp_cache`
  (e.g. under `outputs/`) are not covered.
* **Health reporting.** No daily-tool-analysis check consumes the HOLD yet.
  The signals are exit code 3, the `FMP_CUTOVER_HOLD` log/stderr line, and (for
  direct `run_portfolio_update` callers) `pipeline_run_status` with
  `data_mode=DATA_UNAVAILABLE`.
* **Rights.** This repair does not resolve Free/Basic entitlement or
  cached-data rights. Archives are retained and nothing assumes Free
  entitlement.


## Independent QA hardening (2026-11-14)

ChatGPT independently reproduced 265 test passes, challenged the first candidate,
fixed the additional failure surfaces, and reran 270 targeted FMP/client/VS-002
and daily-wrapper tests: **270 PASS**, with py_compile and git diff --check PASS.
Five regression cases were added for: a test clock never rewinding the real
clock, a secret-bearing dynamic endpoint/cache key never appearing in a refusal
or warning, both HTTP paths being rechecked after a rate-limit sleep crosses
the cutover, and source-configured Basic limits never exceeding published caps.
Existing synthetic retry timing was adjusted to account for the extra check.

This is a local candidate pass, NOT GitHub protected CI, a formal non-author
review, production service acceptance, or FMP endpoint/source-rights admission.
Manual probe scripts outside FMPClient, early guard coverage of weekly/discovery
jobs, health-check consumption of the HOLD status, and existing tests on/after
2026-11-26 are explicit follow-up blockers for any production deployment.


## Independent FMP4-R1 QA addendum (2026-11-14)
ChatGPT independently ran 568/568 targeted tests spanning new FMP4-R1,
original FMP4, VS002/client, cache readers, scanner, market coverage and
wrapper modules, with py_compile and git diff --check PASS. Weekly ETF tests
could not collect in the shared QPC Python environment because markdown is
missing; this is UNVERIFIED, not PASS. The implementer saw a Windows SQLite
file-lock failure in the existing capability-store test. Both need controlled
dependency-complete Linux verification before formal acceptance. Additional
guarded scripts: active run_sims_daily.sh and manual rerun_today_safe.sh.
Rerun the expanded shell matrix on the final candidate. No production authority.
