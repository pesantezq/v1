# pe_challenger (research-only)

Last verified against `portfolio_automation/research/pe_challenger.py` (added
`74bf5d77`, 2026-08-03 — unchanged since). Last updated 2026-08-08.

## Purpose

A **research-only champion/challenger harness** that answers one question:
*what happens if the inert PE component of the candidate scanner is restored?*

- **Champion** = the current production scanner, exactly as deployed. No PE
  source.
- **Challenger** = identical configuration and identical frozen inputs, with the
  research PE field populated so the `pe > 50` bubble guard becomes evaluable
  and the 15-point PE attractiveness factor becomes evaluable.

No other change: no new thresholds, no weight retuning, no factor rebalancing.
The experiment isolates exactly one variable.

Background on *why* PE is inert in production is in `docs/pe_resolver.md` and
`docs/DATA_AND_FMP_ENDPOINTS.md` → "Known inert guard".

## Governance posture

**Research-only, and never wired to production.** Verified by grep on
2026-08-08: the only importer of `portfolio_automation.research.pe_challenger`
is `tests/test_pe_challenger.py`. There is **no** caller in `main.py`,
`scripts/run_daily_safe.sh`, any other wrapper script, or any cron entry.

- **Not wired to any scheduler.** No stage, no cadence. It runs only when a
  human or a test invokes it.
- It writes **no artifact** and declares **no `OutputNamespace`** — `run_pe_experiment`
  returns a dict. There is nothing for the artifact registry to govern, and no
  file for a health check to age.
- The returned payload hardcodes `research_only: true` and
  `feeds_production: false`.
- It never writes `decision_plan.json`, never mutates the watchlist, never
  changes scoring, and cannot alter the deployed scanner — it *instantiates* two
  throwaway scanners via a caller-supplied factory.

As with `pe_resolver`, it does **not** set `observe_only`: that field is the
convention for observe-only producers that write artifacts. This module writes
nothing and uses the `research_only` / `feeds_production` vocabulary instead.

## Same-input guarantee

Both arms consume the **same** universe, profiles, metrics and quotes objects,
captured once into a frozen snapshot by `build_snapshot`. The challenger cannot
fetch later data than the champion **because it fetches nothing** — PE values are
supplied up front and merged into a `copy.deepcopy` of the metrics rows by
`_challenger_metrics`, which never mutates the originals.

Two mechanisms make that auditable:

1. A **snapshot fingerprint** (`_fingerprint`, SHA-256 truncated to 16 hex chars
   over the sorted symbols, sorted metrics rows, sorted usable PEs, and `as_of`)
   is recorded on both the snapshot and the result, so an arm pairing is
   traceable.
2. A test asserts the champion arm's input rows are byte-identical to the
   originals after the run.

Two **independent** scanner instances are created from `scanner_factory()`, so
neither arm can carry state into the other.

## Injection policy: missing PE stays missing

`build_snapshot` takes `pe_by_symbol` — a mapping of symbol → a `pe_resolver`
result dict — and injects **only** results whose `quality` is `direct` or
`derived` *and* whose `pe_ratio` is not `None`. Everything else
(`negative_earnings`, `invalid`, `unavailable`) is recorded in `pe_skipped` and
injects **nothing**.

This is load-bearing. `_score` reads PE through an `or 100` default, so a fake
`0` injected for a missing PE would be silently banded rather than ignored. A
missing PE must stay missing in the challenger.

## Attribution: two effects, never merged

The two PE effects are reported separately because they are separate decisions:

| Effect | What it changes | How it is detected |
|---|---|---|
| **hard-filter effect** | **MEMBERSHIP** — a name the champion admitted that the challenger rejects on `pe > 50` (or vice versa) | Symbol dropped from the challenger's candidate set **and** present in `guard_rejected`, i.e. the challenger's debug row's `failed_filters` string contains `pe=` |
| **score effect** | **RANK** — a name present in both arms whose score moved purely by PE points | Non-zero `score_challenger - score_champion` for a symbol in both sets |

Conflating them would hide whether any future benefit comes from *excluding
expensive stocks* or from *ranking cheap ones higher*. Drops that are not
attributable to the PE guard are reported separately as
`dropped_other_reason` rather than being credited to PE.

`_pe_points` mirrors the scanner's band table (`PE_BANDS = (15, 12, 8, 3, 0)`):
`pe ≤ 15 → 15`, `≤ 25 → 12`, `≤ 35 → 8`, `≤ 50 → 3`, else `0`; missing or
non-positive PE → `0` (the `or 100` path). A test pins that the reported
`score_delta` reconciles exactly to `pe_points`.

## Module API

```python
build_snapshot(*, symbols, profiles, metrics, quotes, pe_by_symbol, as_of) -> dict
run_pe_experiment(scanner_factory, snapshot) -> dict
```

`scanner_factory` is a **zero-arg callable returning a fresh `CandidateScanner`
configured EXACTLY as production**. The module does not construct one itself —
that is the caller's responsibility, and it is what keeps "champion == production"
an assertion the caller makes rather than a duplicate the module maintains.

Both arms are driven through `CandidateScanner.full_scan(symbols, profiles,
metrics, quotes)`, whose `(candidates, debug_rows)` return shape is what the
attribution logic reads.

`SCHEMA_VERSION = "1"`.

### `build_snapshot` output

| Key | Meaning |
|---|---|
| `as_of`, `symbols`, `profiles`, `metrics`, `quotes` | The frozen inputs, verbatim |
| `pe_usable` | symbol → float, only `direct`/`derived` |
| `pe_skipped` | symbol → the quality string that disqualified it |
| `fingerprint` | 16-hex-char lineage hash |
| `research_only` | Always `true` |

### `run_pe_experiment` output

| Key | Contents |
|---|---|
| `schema_version`, `research_only`, `feeds_production` | `"1"`, `true`, `false` |
| `as_of`, `snapshot_fingerprint` | Lineage |
| `counts` | `champion_candidates`, `challenger_candidates`, `pe_usable`, `pe_skipped`, `pe_guard_rejections` |
| `overlap` | `top_10`, `top_20`, `top_50` set-overlap counts |
| `rank` | `spearman` (`None` when fewer than 3 shared symbols), `median_abs_displacement`, `max_displacement` |
| `scores` | `_stats` (mean/median/stdev/n) for champion, challenger, and the per-symbol delta |
| `pe_band_distribution` | Challenger candidate counts per PE band, keyed `"15pts"`…`"0pts"` |
| `membership` | `dropped_by_challenger`, `dropped_via_pe_guard`, `dropped_other_reason`, `added_by_challenger` |
| `attribution` | `hard_filter_effect` (full list), `score_effect_top` (top 20 by absolute delta), `score_effect_count` |
| `guard_rejected_symbols` | Every symbol the challenger's debug rows failed on PE |

## Failure / degraded behavior

There is no artifact and therefore no `status: degraded` payload. The module has
**no** internal `try`/`except`: it is a pure computation over inputs the caller
supplies, and a broken `scanner_factory` or a malformed snapshot will propagate.
Any pipeline integration would have to wrap it per the repo's non-blocking
convention — but there is no such integration today.

Graceful behaviors that *are* built in:

- Non-dict rows in `metrics` and non-dict values in `pe_by_symbol` are skipped
  rather than raising.
- `_spearman` returns `None` rather than dividing by zero when fewer than 3
  symbols appear in both arms (pinned by a test).
- `_stats` returns an all-`None`, `n: 0` shape for an empty list.
- Displacement metrics are `None` when the shared set is empty.
- The whole result is JSON-serializable (pinned by a test), so a caller can
  persist it if it ever gains one.

## Known limitations

- **No production caller, so no results are published anywhere.** The experiment
  exists as a capability, not as a running measurement. Nothing under
  `outputs/` carries its output, and no health check consumes it.
- **Champion fidelity is the caller's responsibility.** "Champion == production"
  holds only if `scanner_factory` really returns a production-configured
  scanner; the module cannot verify that.
- **`guard_rejected` is parsed from a debug string.** It matches the substring
  `pe=` in the challenger debug row's `failed_filters`. If the scanner changes
  how it formats failed-filter reasons, hard-filter attribution silently
  degrades to "dropped for another reason" rather than failing loudly.
- **`_pe_points` is a mirror, not the source.** It duplicates the scanner's band
  table for attribution purposes. It reconciles today (a test pins it), but it
  is a second copy that could drift if the scanner's bands change.
- **Membership `added_by_challenger` is reported but not attributed.** Only
  *drops* are split into guard-vs-other; additions are listed without a cause.
- **One point in time.** A snapshot is a single `as_of`; the harness has no
  multi-period or walk-forward mode, so it measures structural difference, not
  realised performance. It cannot say whether the challenger would have been
  *better* — only what it would have changed.

## Tests

```
.venv/bin/python -m pytest -q tests/test_pe_challenger.py
# 11 passed
```

Covers: champion input rows are never mutated; the champion arm matches the
production scanner exactly; the snapshot fingerprint is recorded and stable;
only usable PE qualities are injected; an unusable PE does not become a fake
zero; the PE guard rejects above 50 and is attributed to the hard filter; the
score effect reconciles exactly to `pe_points`; hard-filter and score effects
are not conflated; the structural metrics are reported; the payload declares
`research_only` / `feeds_production: false` and is JSON-serializable; and a
no-usable-PE run yields a `None` rank correlation safely.

## See also

- `docs/pe_resolver.md` — the resolver that produces the `pe_by_symbol` inputs
  and the quality vocabulary this module filters on.
- `docs/DATA_AND_FMP_ENDPOINTS.md` — the measured inertness of the production PE
  guard and the `score_breakdown()` read-only mirror of `_score`.
- `docs/scanner_canary.md` — `factor_liveness`, where inert components surface
  operationally as a WARN.
