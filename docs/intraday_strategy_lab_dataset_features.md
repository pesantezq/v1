# Intraday Strategy Lab — Dataset & Features (Session 2)

**Status: `DATASET_FEATURE_FOUNDATION_READY`** · research-only · no production path.

The status is **computed, not asserted** — `foundation.session2_graduation()`
runs 25 checks against the live code and the persisted corpus, and READY means
all 25 passed. It was previously hardcoded to LIMITED with a hand-written
justification, which could not follow the evidence in either direction.

`strategy_validation_allowed` remains **`false`**. Graduating the DATA does not
graduate the strategy layer.

---

## 1. Calendar contract

Sessions come from **`exchange_calendars`** (Apache-2.0) using its `XNYS`
calendar. It was chosen over `pandas_market_calendars` because the latter
*depends on* it — a strict superset, so this is the smaller maintained
dependency that satisfies the contract.

| | |
|---|---|
| Exchange / timezone | `XNYS` / `America/New_York` → UTC |
| Certified window | **2017-01-01 … 2027-06-30** (2,636 sessions, 22 early closes) |
| Regular session | 09:30–16:00 ET → **78** 5-min bar starts, 09:30…15:55 |
| Early close | 09:30–13:00 ET → **42** bar starts, 09:30…12:55 |
| Holiday / weekend | `MARKET_CLOSED`, 0 expected bars |
| **Outside the window** | **`UNCERTIFIED` — refused** |

Grids are computed from the calendar's open/close, never hardcoded per date, so
DST is handled by the zone (summer 13:30Z open, winter 14:30Z) and **session
type is derived from the actual close time** — an early close is simply a
session closing before 16:00 ET, so a newly announced half-day arrives with the
calendar data rather than an edit to this repo.

The certified window's upper bound is a **fixed constant, not the library's own
`last_session`**, which advances with the wall clock — deriving from it would
mint a new calendar identity, and therefore new research meaning, every day.

### Calendar identity = schedule meaning, not package version

`calendar_identity()` hashes a digest of the actual certified schedule (every
session with its open and close) plus exchange, timezone, backend and semantics
version. The dependency's version string is disclosed separately and is
deliberately **not** in the identity:

* an upgrade that changes a historical session changes the digest → **new
  calendar era**, so research meaning can never be rewritten silently;
* an upgrade that changes nothing leaves the digest identical → no spurious era
  churn, and archived manifests stay remintable.

Certified against real NYSE history in
`tests/test_intraday_lab_calendar_certification.py` (69 cases): holidays across
years, the 2018 and 2025 national days of mourning, Juneteenth's 2022 start
year, early closes 2017–2025, both DST transitions in four separate years, and
exact timestamp grids.

### Fallback, and why it is not silent

If `exchange_calendars` is unavailable the module falls back to the repo-native
table and its narrow window. The **backend in force is part of the calendar
identity**, so a dataset built without the authoritative calendar is a different
research object, not a lookalike.

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

1. **Market-wide trading halts are not admissible — and this is a selection
   bias.** The calendar predicts a full grid; a halt removes bars that never
   printed, so the exact-grid rule rejects the session. Proven in the pilot:
   2020-03-09 and 2020-03-12 lost exactly the Level-1 circuit-breaker windows
   (09:35–09:40 and 09:40–09:45 ET) for **both** symbols, which is what
   identifies it as market-wide rather than a per-symbol data gap. Rejecting is
   the safe direction, but it removes the most volatile days in modern history
   from the research universe. Session 3 must account for this explicitly.
2. Volume-dependent features blocked (semantics unproven).
3. Absolute-price features blocked (split back-adjusted).
4. `SECTOR_CONTEXT_DEFERRED`.
5. Only **5min** is entitled; 1min returns HTTP 402 on this account.
6. Certified window ends **2027-06-30** by design (see §1).
7. No bulk backfill — the pilot is bounded by design.
8. **No CLI module.** The durable pipeline is library-level
   (`pipeline.build_historical_research_dataset`) with a `dry_run` mode; the
   operator CLI wrapper is still deferred.
9. Cross-session rolling features remain **DEFERRED**.
10. Manifests written before `calendar_identity` was persisted can only be
    re-migrated while their calendar remains reproducible; afterwards they
    resolve from migration lineage instead (see §8).

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

Session 3 **may assume**: exact calendar reconciliation certified to 2017,
immutable canonical datasets with deterministic PIT-complete fingerprints,
PIT-safe price features with full provenance, and fail-closed admission.

Session 3 **must not assume**: any volume-derived feature, absolute-price
features, sector context, admissibility of halted sessions, or that
`strategy_validation_allowed` has changed — it remains **`false`**, and no
Session 2 artifact implies otherwise.

The machine-readable contract is `foundation.session3_input_contract()`. It is
**gated**: while Session 2 is LIMITED it returns `SESSION_3_NO_GO` with the
exact blockers and **no contract body at all**, because a contract published
beside a LIMITED foundation reads as permission.

### The temporal invariant Session 3 inherits

> A 5-minute bar covering **10:00–10:05** has **`known_at` = 10:06**.
> A strategy consuming that completed bar may **not** claim a 10:05 fill.
> `decision_time >= known_at`, and `fill_time >= decision_time`.
> `known_at` is never derived from `retrieved_at`.

`known_at` is part of canonical identity, so a dataset that moves knowability
earlier is a **different research object**, not the same one tuned.

---

## 8. Identity eras and migration

An immutable object is content-addressed: its directory name *is* a hash of its
content. So **changing the hash function retroactively re-labels every existing
object as corrupt.** That happened on 2026-08-09 — raw identity gained
`provider`/`endpoint` and canonical identity gained `bar_end_at`/`known_at`, and
the verifier reported five byte-perfect objects with the tampering reason.

The dangerous consequence is not the false alarm; it is **desensitisation**.
Once half the corpus permanently reports tampering, that message stops meaning
anything and a genuine tampering event hides in the noise.

Identity changes are therefore **eras**, and two questions are kept apart:

| Question | Answered by |
|---|---|
| **Integrity** — does it verify under the schema that *minted* it? | `verified` |
| **Research eligibility** — does it satisfy today's contract? | `current_era` |

Neither is allowed to imply the other. `_canonical_ready` requires **both**.

### Verification states (`identity.py`)

| State | Meaning |
|---|---|
| `VERIFIED_CURRENT` | integrity OK under the era in force today — the only state eligible for research |
| `VERIFIED_LEGACY_MIGRATABLE` | OK under an older era; current identity computable from the same bytes |
| `VERIFIED_LEGACY_ARCHIVAL` | OK under an older era, but a field today's identity protects was never stored — permanently archival |
| `UNSUPPORTED_IDENTITY_SCHEMA` | declares an era this build does not implement → fail closed |
| `AMBIGUOUS_IDENTITY_SCHEMA` | more than one era reproduces the identity → fail closed |
| `INTEGRITY_FAILURE` | no supported era reproduces it — **the only state that means tampering** |

The registry is **closed**, not "try every hash forever". An object that
*declares* its era is verified under that era **alone** — probing past a
declaration would let a forger pick whichever historical function validates
their bytes. Probing is reserved for objects written before declarations
existed, and demands a *unique* match.

### Migration (`migration.py`)

```
legacy object → verify under its OWN era → confirm current-required fields exist
→ compute current identity FROM THE PERSISTED BYTES → write NEW current-era
object → write immutable lineage
```

Four rules, enforced rather than documented:

1. **Never refetch to migrate.** Vendors restate and re-adjust; a refetch would
   substitute today's data for archived evidence and call it the same dataset.
2. **Never rewrite, rename or delete the legacy object.** Verified in test by
   byte-comparing every legacy file before and after.
3. **Content equivalence is proved**, not assumed.
4. **Calendar meaning is held constant** — migration replays the legacy manifest
   identity first and refuses if it cannot, so migrating after a calendar change
   fails closed instead of silently reinterpreting archived research.

Features are **reminted, never relabelled**: values stay numerically identical
while the fingerprint *changes*, because feature identity binds to the source
dataset. Both halves are asserted.

`migration.active_corpus()` computes — from evidence, not a curated list —
which manifest graphs Session 3 may consume. **Archival manifests are retained
and verifiable, and are never silently reused.**

---

## 9. Governed provider identity

`acquire()` previously took a bare callable and stamped the resulting evidence
with a hardcoded `provider="fmp"`. Since provider and endpoint are now part of
raw identity, that assumption would **mis-address the immutable object**.

A provider is now an object that knows what it is (`providers.py`):

* `GovernedFMPIntradayProvider` — opens no sockets of its own; delegates to
  `FMPClient.get_json`, inheriting cache-first reads, the daily budget guard,
  the rate limiter and the call ledger. The endpoint comes from
  `fmp_endpoint_registry`, so an unregistered timeframe **cannot be fetched**
  (interpolating `/stable/historical-chart/{tf}` would happily produce the 1min
  path this account is not entitled to).
* `FakeIntradayProvider` — declares its own identity, so fixture-produced
  evidence is addressed as a fake.
* `CallableIntradayProvider` — a bare callable is adapted only as
  `callable:unspecified`, never assumed to be FMP.

A budget refusal is `PROVIDER_BUDGET_REFUSED`, distinct from `NO_DATA`: **our**
refusal to call must never be recorded as the market having no data.

### Failure causality

| Condition | Acquisition | Reconciliation |
|---|---|---|
| provider raised | `PROVIDER_ERROR` | `REJECTED_PROVIDER_ERROR` |
| HTTP success, empty rows | `NO_DATA` | `REJECTED_MISSING_BARS` |
| rows returned, parse failed | `OK` + `normalization_status=FAILED` | `REJECTED_NORMALIZATION_ERROR` |

Four causes, four distinct states, pinned as a table so a refactor cannot
quietly merge two of them. Raw evidence is preserved even when normalization
fails — otherwise a provider schema change would be invisible.

---

## 10. Graduation gate

`foundation.session2_graduation()` splits its checks deliberately:

* **measured (25)** — proven by running the real function over the real corpus.
* **test_enforced (8)** — invariants a runtime status function *cannot honestly
  self-certify* (tamper cascades, adversarial legacy handling). They are named
  with their enforcing tests so the claim is traceable, and are **not** counted
  as measured evidence.

Asserting "tamper detection works" without running a tamper would be exactly the
verdict-from-absent-data failure this lab exists to prevent. A gate that raises
is also a failure mode, so every probe degrades to a named blocker rather than
an exception.
