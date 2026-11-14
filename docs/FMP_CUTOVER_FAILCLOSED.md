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
| `scripts/run_daily_safe.sh` | Sources `scripts/fmp_cutover_guard.sh` **before** preflight, venv, `.env`, broker sync, and any outbound call. Exits **3** with a `FMP_CUTOVER_HOLD …` line and `DAILY RUN FAILED`. |
| `scripts/run_monthly_universe_refresh.sh` | Same guard **before** the flock, `.env` and the ~3,700-call rebuild. Exits **3**. The HOLD line goes to the log and stderr. |

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

Revert the commit. The guard adds no state, writes no artifacts, and deletes no
cache files. No migration is needed.

## Known limitations

* After 2026-11-26 the **existing** FMP client test suites use the real clock and
  will see the HOLD. They will need an injected pre-cutover `clock=`, or will
  need to be quarantined, at that point.
* FMP traffic outside `fmp_client.FMPClient` (for example the operator probe
  `scripts/probe_fmp_crowd_endpoints.py`) is not covered by the transport gate.
  Scheduled paths are covered by the entrypoint guard.
* Other scheduled entrypoints (weekly, discovery pulses, ETF) have no early bash
  guard. Any FMP use they make is still refused at the client transport.
* No daily-tool-analysis health check consumes the HOLD yet. The signal is the
  `FMP_CUTOVER_HOLD` log line and exit code 3.


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
