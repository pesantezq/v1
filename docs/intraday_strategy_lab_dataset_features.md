# Intraday Strategy Lab — Dataset & Features (Session 2)

**Status: `DATASET_FEATURE_FOUNDATION_LIMITED`** · research-only · no production path.

The full chain is proven end-to-end on real market data. The status is LIMITED —
not READY — for one reason: **calendar coverage, not data availability.**

---

## 1. Calendar contract

Built on repo-native `portfolio_automation.market_session` (no new dependency —
`exchange_calendars` / `pandas_market_calendars` are not installed and adding a
market-data dependency is an operator decision).

| | |
|---|---|
| Exchange / timezone | `XNYS` / `America/New_York` → UTC |
| Regular session | 09:30–16:00 ET → **78** 5-min bar starts, 09:30…15:55 |
| Early close | 09:30–13:00 ET → **42** bar starts, 09:30…12:55 |
| Holiday / weekend | `MARKET_CLOSED`, 0 expected bars |
| **Outside coverage** | **`UNCERTIFIED` — refused** |

Grids are computed from calendar open/close, never hardcoded per date, so DST is
handled by the zone (summer 13:30Z open, winter 14:30Z).

### Two gaps in the underlying data, both handled by failing closed

1. **`market_session` carries no early-close data** — its own docstring says
   *"no early-close half-days"*. Session 1 proved early closes are real in the
   feed, so this module adds an explicit early-close table.
2. **`NYSE_HOLIDAYS` spans 2025-01-01 … 2027-12-24 only.** Five-minute bars go
   back to 2017; every pre-2025 session is `UNCERTIFIED` and refused.

Both failure directions are safe. A *missed* early close → expected 78 vs
observed 42 → rejected. A *wrongly declared* early close → expected 42 vs
observed 78 → surplus → rejected. Neither can admit a corrupt session.

---

## 2. Admission contract

**Bar-count equality is not completeness.** The admission condition is exact set
equality after UTC normalization:

```
observed_bar_start_times == expected_bar_start_times
```

A session with 78 observed and 78 expected is still **rejected** if the 10:05
bar is absent and an off-grid bar took its place — the defect a count check
cannot see.

| Status | Meaning |
|---|---|
| `ADMITTED` | Exact grid match |
| `REJECTED_MIXED_ADJUSTMENT` | Checked **first**; outranks any grid defect |
| `REJECTED_CALENDAR_UNCERTIFIED` | Outside the holiday window |
| `REJECTED_CONFLICTING_DUPLICATE` | Two bars, one slot, different values |
| `REJECTED_CLOSED_SESSION_HAS_BARS` | Calendar and data disagree |
| `REJECTED_OFF_GRID` | Missing **and** unexpected timestamps |
| `REJECTED_MISSING_BARS` / `REJECTED_SURPLUS_BARS` | One-sided mismatch |

**Nothing is ever repaired.** No forward fill, interpolation, invented volume,
dropped timestamps, shifted bars, or padding. A rejected session contributes
zero bars, and the manifest discloses the exclusion — a dataset that silently
omitted it would look complete while covering a different window than requested.

---

## 3. Dataset identity

`canonical_fingerprint` (schema `intraday_canonical_v2`) covers symbol,
timeframe, bar start, OHLCV, **and `adjustment_state`** — v2's addition. Two
datasets with byte-identical OHLCV but different adjustment regimes mean
different things and must not share an identity.

`retrieved_at` remains excluded: re-fetching identical history must reproduce
the fingerprint, or every experiment is irreproducible by construction.

---

## 4. Feature PIT contract

```
feature.known_at >= max(input_bar.known_at)
```

A feature cannot become knowable before its newest required input. Every window
is backward-looking — no centred windows, no negative lag, no forward fill.
Insufficient history returns explicit absence (`FEATURE_NOT_AVAILABLE`), never a
padded or partial value, which is what stops a 20-bar feature from quietly
becoming a 3-bar feature near the open.

Every `FeatureValue` carries `source_dataset_id` + `source_dataset_fingerprint`,
so no feature can exist without tracing to the exact canonical dataset.

### Enabled

| Feature | Lookback | `known_at` rule | Adjustment-safe | Volume |
|---|---|---|---|---|
| `return_1bar` | 2 bars | `max(input.known_at)` | ✅ ratio | no |
| `return_nbar` | N+1 bars | `max(input.known_at)` | ✅ ratio | no |
| `realized_vol` | N+1 bars | `max(input.known_at)` | ✅ from returns | no |
| `normalized_range` | 1 bar | `bar.known_at` | ✅ ratio | no |
| `range_position` | N bars | `max(input.known_at)` | ✅ normalized | no |

**No enabled feature requires absolute price or volume** — asserted by test.

### Blocked

| Feature | Status | Why |
|---|---|---|
| `vwap` | `BLOCKED_VOLUME_SEMANTICS` | Needs price **and** volume adjusted consistently; neither established |
| `rvol` | `BLOCKED_VOLUME_SEMANTICS` | A split changes share counts; cross-split comparison unproven |
| `dollar_volume` | `BLOCKED_VOLUME_SEMANTICS` | Must **not** be used as a liquidity admission threshold |
| `absolute_atr` | `BLOCKED_ADJUSTMENT_SEMANTICS` | Back-adjusted dollar levels; use `normalized_range` |
| `sector_relative_return` | `DEFERRED` | `SECTOR_CONTEXT_DEFERRED` — no PIT-safe symbol→sector mapping |

Policy: a feature whose semantics cannot be proven point-in-time safe is
**blocked**, not enabled with a warning. A test asserts no `compute_*` function
exists for any blocked feature.

---

## 5. Pilot (real data, production VPS)

| | |
|---|---|
| Provider | FMP `/stable/historical-chart/5min` (registered) |
| Symbols | SPY (broad market), AAPL (liquid equity) |
| Dates | 2026-08-03…07 + 2025-11-28 (early close) |
| Sessions requested / admitted / rejected | **11 / 11 / 0** |
| Bars admitted | **822** |
| `dataset_fingerprint` | `d3d74e6b187b0bea32ba193cacb8dbaf` |
| Feature observations | 819 |
| `feature_fingerprint` | `38ed9d02ebad46d5a758cafc581806cd` |

The early close reconciled exactly against the calendar-derived 42-bar grid —
the strongest available evidence that calendar and provider agree.

**No strategy metrics. Session 2 produces no strategy evidence.**

---

## 5b. Hardening pass — five confirmed fail-open defects

All reproduced on `b781dd28` before being fixed:

| Defect | Observed | Now |
|---|---|---|
| Requested session could vanish | 3 days requested, Aug 4 absent from the mapping → **2** reconciliations | `DatasetRequest` drives the loop; 3 reconciliations, Aug 4 `REJECTED_MISSING_BARS` |
| Exact duplicate inflated the dataset | 78 expected, 78 unique observed, **79 canonical rows** | `REJECTED_EXACT_DUPLICATE` — research input is never silently deduplicated |
| Cross-session mixed adjustment admitted | dataset labelled `split_adjusted` while holding both regimes | Whole dataset admits nothing; state **derived** from bars, caller label ignored |
| Dict key trusted over the bar | AAPL bars admitted under a SPY key | `REJECTED_IDENTITY_MISMATCH` |
| Feature window crossed symbols | 3-bar window over SPY+AAPL produced a value **labelled AAPL** | `SeriesIntegrityError`; `group_series()` required |

Also fixed: `session_progress` was `ENABLED` with no implementation (now
`NOT_IMPLEMENTED`, with an `IMPLEMENTATIONS` map and an invariant test);
`feature_fingerprint` did not bind the source dataset (two datasets producing
identical values shared an identity); and `session2_status` reported
`canonical_dataset_ready: true` on `pilot=None` — readiness is now evidence-driven.

### Two identities

- **Content fingerprint** — admitted bars + timeframe + adjustment. For storage dedupe.
- **Manifest fingerprint** — request + calendar + admitted/rejected sessions + content.
  "Aug 1–10 with 3 rejections" and "only the 7 admitted days" can be byte-identical
  yet answer different research questions. Experiments bind here.

### Feature continuity

Windows are validated for single symbol, single timeframe, and **exact temporal
adjacency**. A window bridging a rejected session returns `FEATURE_NOT_AVAILABLE`
rather than a silently shortened lookback. Cross-session rolling features are
**DEFERRED**.

## 6. Limitations

1. **Calendar coverage is the binding constraint** — 2025-01-01…2027-12-24.
   Bars exist back to 2017; every earlier session is refused. Extending the
   holiday table from a verified source is the highest-value unblock.
2. Early-close table is hand-maintained; an error causes a rejection, never a
   silent bad admit.
3. Volume-dependent features blocked (semantics unproven).
4. Absolute-price features blocked (split back-adjusted).
5. `SECTOR_CONTEXT_DEFERRED`.
6. No bulk backfill — the pilot is deliberately small.
7. **No CLI module.** The durable pipeline is library-level
   (`pipeline.build_historical_research_dataset`) with a `dry_run` mode; the
   operator CLI wrapper is still deferred.
8. Cross-session rolling features remain **DEFERRED**.

## 5c. Durability & provenance (completion pass)

The chain is now end-to-end and durable:

```
DatasetRequest → acquire() → immutable RAW snapshot → normalize → calendar
  → exact reconciliation → immutable CANONICAL snapshot → build_features()
  → immutable FEATURE snapshot
```

**Request accounting is total.** `resolved_items()` returns every requested
symbol×date with a calendar status — `EXPECTED_TRADING_SESSION`,
`MARKET_CLOSED`, or `CALENDAR_UNCERTIFIED`. A requested 2023 weekday and a
requested Saturday both previously vanished from the record entirely; both now
persist. A requested closed date is `NOT_A_TRADING_SESSION` — accounted for, but
not counted as a rejection. Provider results outside the authorized matrix are
`REJECTED_UNEXPECTED_PROVIDER_RESULT` rather than silently ignored.

**Immutable snapshots** (`storage.py`) are content-addressed under
`outputs/backtest/intraday/{raw,datasets,features}/<identity>/`. Identical
content is verified and reused; the same identity with different bytes is a
`SnapshotCollisionError`, never an overwrite — a silently replaced dataset would
invalidate every experiment bound to it with no trace. `retrieved_at` is outside
the raw content hash, so refetching the same observations reuses one identity.

**Calendar semantics are in the manifest identity.** `calendar_fingerprint()`
hashes exchange, timezone, source, coverage bounds, the holiday table, the
early-close table and the grid times. Same bars under a changed calendar → same
content fingerprint, **different** manifest fingerprint.

**Adjustment state is never caller-supplied** — derived from admitted bars, or
`NOT_APPLICABLE` when none were admitted.

**Features bind to both identities** (`source_dataset_fingerprint` and
`source_dataset_manifest_fingerprint`), and `pipeline.build_features(dataset)`
takes the dataset object, so no caller can pair bars from A with the identity of
B. A test asserts the signature exposes no `dataset_id`/`fingerprint` argument.

**Readiness is recomputed from persisted bytes.** `_canonical_ready` requires
the snapshot to exist, a request manifest to be present, and the stored bars to
re-hash to their own directory name. Fabricated metadata reads FALSE, and a
tampered snapshot fails verification — both under test.

Snapshots are gitignored: content-addressed and reproducible from the committed
request + pipeline.

---

## 7. Session 3 preconditions

Session 3 **may assume**: exact calendar reconciliation, immutable canonical
datasets with deterministic fingerprints, PIT-safe price features with full
provenance, and fail-closed admission.

Session 3 **must not assume**: pre-2025 history, any volume-derived feature,
absolute-price features, sector context, or that `strategy_validation_allowed`
has changed — it remains **`false`**, and no Session 2 artifact implies
otherwise.
