# Immutable Daily Input Snapshot (Phase 2)

Status: **shipped** on `feat/complete-simulation-quant-governance-loop`.
Observe-only. Mutates no decision/allocation/score/portfolio state.

Ensures the production decision and **every** daily simulation evaluate the
SAME point-in-time data, and that no daily simulation can read later
information (Iron rules 4 & 5). It is the input foundation Phases 3–5 build on.

## What it is

`portfolio_automation/daily_input_snapshot.py` freezes one bundle of all
decision-time inputs as **references + content hashes, not copies** (the
mission's allowed approach for large/immutable-for-the-run data). For each
declared `InputSource` it records:

| field | meaning |
|---|---|
| `key` / `kind` / `source` / `path` | identity + provenance (a reference) |
| `present` | did the artifact exist |
| `observation_timestamp` | payload `generated_at` (else file mtime) |
| `available_as_of` | the run's as-of (= `data_as_of`) |
| `freshness` / `quality` | `ok`/`fresh` · `stale` · `missing` · `invalid_future` |
| `age_hours` | observation age vs the run as-of |
| `content_hash` | sha256 of the artifact bytes |

Plus a single **`snapshot_hash`** over the valid inputs (sorted `key=hash`):
stable for identical inputs (idempotent retries — Iron rule 8) and different
when any meaningful input changes.

## Guarantees

- **Future-date rejection (no look-ahead):** an input observed *after* the run
  as-of is marked `invalid_future` and **excluded** from `snapshot_hash`, so it
  cannot leak into the run's coherent input identity.
- **Stale = degrade-but-usable:** age beyond the per-source `stale_after_hours`
  is flagged `stale` but still referenced + hashed (it is real data, just old).
- **Missing = honest:** absent artifact → `missing`, no crash.
- **Single frozen source:** `read_input_snapshot()` + `load_input(snap, key)`
  give production and every shadow sim the identical bundle.

## Lineage

Inherits `run_id`, `data_as_of`, `source_commit`, `config_hash` from the Phase 1
run manifest (degrades to `now` when absent). Carries the standard
`observe_only`/`no_trade` envelope + `lineage()` fields.

## Pipeline + operator visibility

- `run_daily_safe.sh` **Stage 7g** builds + writes
  `outputs/sandbox/daily_input_snapshot.json` after the decision pipeline +
  advisors (so the production baseline + holdings/risk/crowd inputs exist).
- `daily_run_status` surfaces an `input_snapshot` block
  (`present`, `snapshot_hash`, `valid_count`, `stale_count`, `missing_count`,
  `future_rejected_count`) — the snapshot validation visible in operator status.

## Declared inputs (best-effort references; absent → `missing`)

holdings (broker overlay + portfolio snapshot), decision baseline + holdings
source, news, unified crowd, regime, factors (Fama-French), source-health (data
budget), active production overlays, prod config, strategy config. Extend
`INPUT_SOURCES` as later phases add inputs.

## Consumed by

Phase 3's daily incremental simulation reads this snapshot (via
`read_input_snapshot` / `load_input`) so the production baseline and all 8
strategy profiles + shadows run on identical inputs.

## Tests

`tests/test_daily_input_snapshot.py` — provenance fields, missing/stale/future
handling, hash idempotency + change-sensitivity, write/read freeze, envelope +
summary, and the `daily_run_status` surfacing.
