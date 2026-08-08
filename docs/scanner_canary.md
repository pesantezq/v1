# scanner_canary

Last verified against `portfolio_automation/scanner_canary.py` (added `74bf5d77`,
2026-08-03; daily-wiring + `assessed_at` fix `5361e2b6`, 2026-08-07). Last
updated 2026-08-08.

## Purpose

Turn the scanner-quality contract that already exists inside
`outputs/latest/scraped_intel_run_summary.json` into **one explicit
operator-readable verdict per dimension**, so the next run is judgeable at a
glance without log archaeology.

The scanner publishes five separate quality blocks under `scanner.*`
(constituent resolution, screening sufficiency, universe sufficiency, ranking
quality, factor liveness) plus the `safe_mode` consequence. Reading them means
opening a large JSON and knowing which field means what. This module renders
them as a fixed acceptance block with a single `overall` verdict, and — since
`5361e2b6` — persists it as a governed daily artifact.

## Design rules the module obeys

Three rules are stated in the module docstring and enforced by the code:

1. **Transport, never recompute.** Every verdict is derived from a published
   field. Freshness is never re-derived from a file mtime, coverage never from a
   row count, and no score or rank is touched.
2. **`n/a`, never inference.** A dimension whose input block is absent renders
   `n/a` and cannot contribute a PASS. The *previous* candidate count is shown
   only when an authoritative prior artifact exists under `outputs/history/`;
   `_previous_candidate_count` walks dated history dirs newest-first looking for
   a `scanner.symbol_count` integer and returns `n/a` rather than reconstructing
   a plausible number — a fabricated "previous" value would make a recovery look
   *proven* when it is merely assumed.
3. **Unavailable certification is not success.** `overall` can only be `PASS`
   when all four mandatory dimensions actually reported.

## Observe-Only Behavior

Observe-only, hardcoded: every payload sets `observe_only: true` (including the
`UNKNOWN` payload emitted when the run summary is missing), and
`tests/test_scanner_canary.py::TestCanaryIsAGovernedDailyArtifact::test_it_never_suppresses_or_gates`
pins it.

It reads artifacts, optionally writes one `OutputNamespace.POLICY` artifact, and
mutates no decision, allocation, score, watchlist, or approval state. It does
**not** feed `decision_plan.json`. It does **not** cause the safe-mode sleeve
suppression it reports — that decision is made upstream in the scanner and
merely transported here under `downstream`.

## Pipeline integration

| Caller | Cadence | Behavior |
|---|---|---|
| `scripts/run_daily_safe.sh` **Stage 11b** ("Scanner-quality acceptance canary") | daily, 09:00 UTC cron | `run_scanner_canary('.')` — writes the artifact and prints `overall` + `reasons`. Runs via `run_aux_stage`, so it is non-fatal. Deliberately ordered **after** Stage 11 (daily run status) and **before** Stage 12 (artifact-registry governance) so the freshly written artifact is inside the corpus that registry stage scans. |
| `scripts/run_monthly_universe_refresh.sh` | monthly, 1st @ 06:30 UTC | Prints `render_canary_text(run_scanner_canary('.'))` after the forced full universe rebuild, as the acceptance gate for that rebuild. Non-fatal (`|| printf 'scanner_canary non-fatal failure'`). |

**Why it is daily.** Until `5361e2b6` the monthly refresh was its *only* caller —
a monthly acceptance gate grading a subsystem that changes daily. The artifact
had no `artifact_registry.yaml` row either, so governance could not see it go
stale; it sat at a 2026-08-04 `run_timestamp` reading `overall: FAIL` while
nothing escalated. Both halves were fixed together: the daily stage writes it,
and the registry row governs it.

## Inputs

| Path | Read as |
|---|---|
| `outputs/latest/scraped_intel_run_summary.json` | The only mandatory input. Produced by `scraped_intel/run_summary.py`, whose `scanner` block is assembled from `universe/sp500_constituents.py` (constituent resolution) and `degraded_mode.py` (`assess_screening_sufficiency`, `assess_scanner_dataset_sufficiency`, `assess_ranking_quality`). |
| `outputs/history/<date>/scraped_intel_run_summary.json` | Optional. Scanned newest-first for `scanner.symbol_count` to populate `watchlist.previous_candidate_count`. |

## Artifacts

| File | Path | Namespace |
|------|------|-----------|
| JSON | `outputs/policy/scanner_recovery_canary.json` | `OutputNamespace.POLICY` |
| Markdown | `outputs/policy/scanner_recovery_canary.md` | `OutputNamespace.POLICY` |

The `.md` is the `render_canary_text` output wrapped in a fenced code block.

Registry row (`portfolio_automation/artifact_registry.yaml`):
`lens: market_discovery`, `role: probe`, `required: false`, `cadence: daily`,
`producer: scanner_canary`, `consumers: [daily-tool-analysis]`,
`severity_if_missing: info`, `consumer_status: consumed`.

### JSON contract

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | str | `"1"` |
| `observe_only` | bool | Always `true`. |
| `assessed_at` | ISO str | **When the verdict was formed.** Always populated — defaults to `datetime.now(timezone.utc)` since `5361e2b6`; callers may pin it for tests. |
| `run_mode` / `run_timestamp` | str | Copied from the run summary — **when the scanner ran**, which is not the same as `assessed_at`. |
| `overall` | str | `PASS` · `WARN` · `FAIL` · `UNKNOWN` |
| `reasons` | list[str] | Machine-readable reason codes accumulated across all dimensions. |
| `constituent` | dict | `source`, `resolved`, `plausibility` (PASS/FAIL vs `plausibility_floor`), `freshness`, `age_days`, `degraded`, `cache_write`. |
| `screening` | dict | `eligible`, `fundamentals_requested`, `fundamentals_resolved`, `coverage`, `unscreened`, `status`, `verdict`, `minimum_threshold`. |
| `watchlist` | dict | `previous_candidate_count`, `current_candidate_count`, `trust_floor`, `universe_sufficiency`, `small_dataset` (`PRESENT`/`CLEARED`). |
| `ranking` | dict | `candidate_count`, `unique_score_count`, `largest_tie_fraction`, `alphabetical_tie_tail_count`, `degeneracy` (`WARN`/`PASS`). |
| `factors` | dict | `status`, `inert` (list or `"none"`), `inert_count`, `suppresses_sleeve`, `detail`. |
| `downstream` | dict | `speculative_sleeve_suppressed`, `suppression_reasons`, `suppression_cleared_because`. |

Any absent input block collapses its dimension to `n/a` values plus a
`*_absent` reason code.

## Verdict algorithm

`overall` is decided in one pass over `reasons`, in this precedence order:

| # | Condition | Verdict |
|---|---|---|
| 0 | Run summary missing/unparsable | `UNKNOWN` (payload carries only `reasons: ["run_summary_missing"]`) |
| 1 | Any hard-fail reason: `constituent_implausible`, `constituent_cache_expired`, `insufficient_screening_coverage`, `insufficient_dataset` | `FAIL` |
| 2 | Any absent-input reason (`constituent_resolution_absent`, `screening_sufficiency_absent`, `universe_sufficiency_absent`, `ranking_quality_absent`, `factor_liveness_absent`, `constituent_age_unknown`) | `WARN` |
| 3 | `inert_factors:*`, `degenerate_ranking`, or constituent `freshness == STALE` | `WARN` |
| 4 | Otherwise | `PASS` |

Two deliberate asymmetries:

- **Absent ≠ FAIL.** A missing dimension may simply not apply to the run mode —
  a daily quote refresh resolves no constituents — so the chain is
  *uncertifiable*, not broken.
- **Inert factors and degenerate ranking are WARN, never FAIL.** A documented
  scoring component being inert is a real finding, but making it a hard fail
  would change production authority semantics: PE has been inert all along and
  the speculative sleeve was permitted throughout. So factor liveness degrades
  the canary to `WARN` at most. See `docs/DATA_AND_FMP_ENDPOINTS.md` → "Known
  inert guard".

## Module API

| Symbol | Contract |
|---|---|
| `build_scanner_canary(root=".", *, now=None) -> dict` | Pure-ish over the filesystem: reads, never writes. Never raises on missing/unparsable input (`_load` swallows `OSError`/`ValueError` and returns `None`). `now` defaults to UTC now. |
| `render_canary_text(canary) -> str` | Pure formatter over the dict. No I/O. |
| `run_scanner_canary(root=".", now=None, *, write=True) -> dict` | Entry point: build, optionally persist both files, return the dict. |

`SCHEMA_VERSION = "1"`, `NA = "n/a"`.

## Failure / degraded behavior

- **Run summary missing or unparsable** → `overall: UNKNOWN`, `reasons:
  ["run_summary_missing"]`, `observe_only: true`. No dimension blocks are
  emitted.
- **Any individual `scanner.*` block missing or not a dict** → that dimension is
  all-`n/a` plus a `*_absent` reason; `overall` cannot be `PASS`.
- **`outputs/history/` missing or unreadable** → `previous_candidate_count` is
  `n/a`.
- **Write failure** → caught inside `run_scanner_canary`, logged as
  `scanner_canary: write failed: …` at WARNING, and the built dict is still
  returned. The import of `data_governance` is inside the same `try`, so even a
  broken governance import cannot break the caller.
- **Wrapper failure** → both callers invoke it non-fatally, so a raise cannot
  fail the daily run or the monthly refresh.

## Known limitations

- **It grades a contract, not the scanner.** Every verdict is only as good as
  the upstream block. A dimension the scanner stops publishing degrades to
  `n/a`/`WARN` — the canary cannot detect that the *producer* regressed, only
  that the field is gone. `scraped_intel/run_summary.py` carries a note about
  exactly this class of bug: a block that was omitted entirely made the canary
  read `None` and print a misleading suppression line.
- **`n/a` fields are heterogeneous.** Absent inputs render the string `"n/a"`
  where a number is otherwise expected, so consumers must type-check rather than
  assume numerics.
- **A `WARN` on a daily quote-refresh run is normal.** On the live 2026-08-08
  daily run the canary reads `overall: FAIL` with
  `constituent_resolution_absent`, `screening_sufficiency_absent`,
  `insufficient_dataset`, and `factors.status: NOT_ASSESSABLE`
  (`metrics_not_fetched_this_cadence`) — the daily cadence does not fetch the
  inputs three of the dimensions need. The dataset finding is the substantive
  one; the two `*_absent` codes are cadence artifacts.
- **No trend view.** Each run overwrites the artifact; there is no history file,
  so "did the verdict improve?" is only answerable through
  `outputs/history/`-dated run summaries, not through the canary itself.

## Health pairing

`.claude/commands/daily-tool-analysis.md` (developer + quant lens) reads
`outputs/policy/scanner_recovery_canary.json` (`overall`, `reasons`) as the
deterministic acceptance view over
`outputs/latest/scraped_intel_run_summary.json:scanner`. The registry row lists
`daily-tool-analysis` as its only consumer.

## Tests

```
.venv/bin/python -m pytest -q tests/test_scanner_canary.py
# 23 passed
```

Covers each dimension's PASS/FAIL mapping, the absent-input `n/a` paths, the
`UNKNOWN` path, determinism across repeated builds, the render block, and the
`TestCanaryIsAGovernedDailyArtifact` group: `assessed_at` is never null, an
explicit `now` still wins, a valid `artifact_registry.yaml` row exists with
`cadence == daily`, `run_daily_safe.sh` actually contains `run_scanner_canary`,
and the payload never suppresses or gates.

`tests/test_run_summary_scanner_quality.py` additionally pins the upstream
contract this module transports.

## See also

- `docs/DATA_AND_FMP_ENDPOINTS.md` — the scanner-quality contract, the safe-mode
  recovery model, and the inert-PE finding surfaced under `factor_liveness`.
- `docs/OUTPUT_ARTIFACT_CONTRACTS.md` — `outputs/latest/scraped_intel_run_summary.json`.
- `docs/CRON_AND_PREFLIGHT_RUNBOOK.md` — the daily and monthly-universe cron rows.
- `docs/pe_challenger.md` — the research experiment that quantifies what the
  inert PE component would change.
