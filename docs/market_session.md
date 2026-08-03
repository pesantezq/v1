# market_session

Last verified against `portfolio_automation/market_session.py` (added
`2de39107`, 2026-07-28; date/datetime coercion fix `388dd153`). Last updated
2026-08-03.

## Purpose

Single source of truth for **calendar-aware NYSE trading-session logic**
(Reliability Program WS8 / Phase D2 — see
`docs/reliability-program/ws-08-09-freshness-silentzero.md`).

Before this module existed the only calendar-aware code in the repo was a
*private* `_NYSE_HOLIDAYS` set inside `resolution_due_probe.py`, used nowhere
else, while every other freshness check (`daily_input_snapshot.py`,
`artifact_registry.py`, `daily_run_status.py`) reimplemented a flat wall-clock
window with zero calendar awareness. This module promotes that holiday set —
**same data, unaltered** — to a shared importable helper so freshness and
provenance work has one place to ask calendar questions.

It answers three questions:

| Question | Function |
|---|---|
| Is this date a NYSE trading day? | `is_trading_day(d)` |
| Which NYSE session had already *closed* as of this timestamp? | `latest_completed_session(ts)` |
| How many sessions of new data lie between two timestamps? | `sessions_between(start, end)` |

The second one is the substantive fix (WS8-F2): a 09:00 UTC cron run is
~04:00–05:00 ET, well before the 09:30 ET open, so "today" has **no** completed
session yet and the honest answer is still the prior trading day. Flat
wall-clock freshness checks conflated the two.

## Observe-Only Behavior

Pure functions: no I/O, no network, no clock calls (callers supply every
timestamp), no new third-party dependency. The module writes nothing and reads
nothing — it cannot change a decision, score, or allocation. It contributes
*fields* to other producers' artifacts (see
[Provenance fields](#provenance-fields)); it owns no artifact of its own.

## Public API

| Symbol | Role |
|---|---|
| `NYSE_HOLIDAYS: frozenset[date]` | NYSE **full-day** closures, 2025-01-01 → 2027-12-24. No early-close half-days are modeled (scoped exactly to what `resolution_due_probe` already modeled, so moving the data here changed no behavior). |
| `HOLIDAY_COVERAGE_THROUGH: date` | `2027-12-24` — the last date the hardcoded holiday data accounts for. |
| `is_past_coverage_horizon(d)` | True if `d` is beyond `HOLIDAY_COVERAGE_THROUGH`. |
| `is_trading_day(d)` | Mon–Fri and not in `NYSE_HOLIDAYS`. |
| `previous_trading_day(d)` | Most recent trading day *strictly before* `d`. Always returns a plain `date`. |
| `latest_completed_session(ts)` | `ts`'s own date if it is a trading day **and** `ts` is at/after the close boundary; otherwise walks back to `previous_trading_day`. |
| `sessions_between(start, end)` | Reduces both timestamps via `latest_completed_session`, then counts trading days strictly after the start session up to and including the end session. `0` when the end session is not strictly after the start session. |
| `session_provenance(as_of)` | Convenience wrapper producers call to stamp provenance (below). |

Every date-taking function accepts a `date` **or** a `datetime`. That matters:
`datetime` subclasses `date`, so passing a `datetime` into a `d: date`
parameter satisfies every static check and then crashes at runtime the moment
the function compares `d` against a `date` constant. That is exactly the defect
`388dd153` fixed in `is_past_coverage_horizon` — the one function whose job is
to warn a caller about the coverage horizon. All public entry points now route
through a subclass-ordered coercion (`isinstance(datetime)` tested *before*
`date`, or the `datetime` branch is unreachable).

**TZ policy, explicit not implicit:** a timezone-aware `datetime` is converted
to UTC before its calendar date is taken; a **naive** `datetime` is treated *as
UTC*, not as local time. So a naive and a UTC-aware datetime for the same
instant always agree on which calendar day they fall on.

## Provenance fields

`session_provenance(as_of)` returns:

| Key | Meaning |
|---|---|
| `latest_session_represented` | ISO date (`YYYY-MM-DD`) of the most recent NYSE session completed as of `as_of` — "which close does this data reflect". |
| `source_data_through` | ISO datetime upper bound of data currency: the (conservative, UTC-approximated) close instant of `latest_session_represented`. |
| `coverage_exceeded` | True if the query fell past `HOLIDAY_COVERAGE_THROUGH`, i.e. holiday awareness degraded to weekday-only for this answer. |

Consumers rename the third key to `session_coverage_exceeded` when stamping it
onto an artifact (see below).

## Callers (verified by grep, 2026-08-03)

| Caller | Uses | Effect |
|---|---|---|
| `portfolio_automation/resolution_due_probe.py` | `is_trading_day` | Trading-day age arithmetic for the stuck-resolution scan. Replaced the module's own private holiday set; behavior unchanged (all 24 pre-existing tests pass unmodified). A Friday signal cannot false-fire on Tuesday when the only weekday between was Memorial Day. |
| `portfolio_automation/run_manifest.py` (`_session_fields`) | `session_provenance` | Stamps `source_data_through`, `latest_session_represented`, `session_coverage_exceeded` onto `outputs/policy/run_manifest.json`, derived from `data_as_of`. |
| `portfolio_automation/next_stage/contracts.py` (`_session_fields`, inside `lineage()`) | `session_provenance` | Adds the same three keys to the canonical lineage block, so any artifact stamped via `lineage(...)` carries a calendar-aware companion to its wall-clock `data_as_of`. |

Live confirmation of the wiring (`outputs/policy/run_manifest.json`,
2026-08-03 09:01 UTC — a Monday pre-market run):

```json
{
  "data_as_of": "2026-08-03T09:01:29.055152+00:00",
  "source_data_through": "2026-07-31T21:00:00+00:00",
  "latest_session_represented": "2026-07-31",
  "session_coverage_exceeded": false
}
```

The run happened Monday but honestly reports Friday's close as the latest
completed session — the WS8-F2 behavior, in production.

**Not currently called in production:** `sessions_between`,
`previous_trading_day` (used internally only), `is_past_coverage_horizon`,
`NYSE_HOLIDAYS`, and `HOLIDAY_COVERAGE_THROUGH` are exported and tested but have
no production caller yet.

**The stamped provenance fields are write-only today** — no consumer reads
`latest_session_represented` / `source_data_through` /
`session_coverage_exceeded`. They are additive artifact enrichment; a freshness
check that *acts* on them is future work.

## Degraded states

There is no artifact and therefore no `status: degraded` payload. Two graceful
degradations exist instead:

1. **Past the coverage horizon** — after `2027-12-24` the answer silently loses
   holiday awareness (weekday-only). The functions still return an answer
   rather than raising, but every public path pairs the answer with a checkable
   flag (`is_past_coverage_horizon`, or `coverage_exceeded` on
   `session_provenance`), so a caller can surface degraded calendar precision
   instead of trusting a wrong answer. Making the horizon an explicit constant
   was WS8-F3's "landmine" fix.
2. **DST approximation** — no timezone-aware DST conversion is performed (no
   `zoneinfo`/`tzdata` dependency is assumed present, per the Windows-laptop +
   Linux-VPS dual environment in `CLAUDE.md`). NYSE closes 16:00 ET = 21:00 UTC
   under EST, 20:00 UTC under EDT; `_SESSION_CLOSE_UTC_HOUR = 21` uses the
   later (EST-equivalent) boundary **deliberately** — the conservative
   direction, so a session is never reported completed before it truly closed.
   During EDT this can lag the true close by up to one hour. The one production
   caller of timestamp-level logic is the 09:00 UTC daily cron, hours from any
   plausible close boundary, so the simplification does not affect it. **Do not
   reuse this module for intraday session-boundary precision** without
   revisiting that assumption.

Both degradations are documented in the module docstring; neither is silent.

## Naming collision (not a caller)

`portfolio_automation/institutional_intelligence/institutional_backtest.py`
defines its own local `next_market_session(avail, sessions)` over an explicit
session list. Despite the similar name it does **not** import this module and
shares no code with it.

## Tests

```
.venv/bin/python -m pytest -q tests/test_market_session.py
# 26 passed
```

Covers `is_trading_day` (weekday/weekend/holiday), `latest_completed_session`
(Saturday, market holiday, pre-market weekday, post-close, naive datetime),
`sessions_between`, `previous_trading_day`, the coverage-horizon flag, the
`session_provenance` field shape, and the `date`-vs-`datetime` coercion
regression from `388dd153`.

## See also

- `docs/reliability-program/ws-08-09-freshness-silentzero.md` — the WS8 audit
  findings (F1 wall-clock freshness, F2 pre-market conflation, F3 coverage
  landmine) this module answers.
- `docs/RUN_MANIFEST.md` — the run-manifest producer that stamps the provenance
  fields.
- `docs/EVALUATION_AND_LEARNING_LOOP.md` — covers `resolution_due_probe`, the
  trading-day-age consumer.
