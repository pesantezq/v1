# Crowd Intelligence — Artifact Writer

## Purpose

`portfolio_automation/crowd_intelligence/artifact_writer.py` is the run
entrypoint and artifact/persistence layer for the Crowd Intelligence subsystem
(Lane B — FMP crowd context). It resolves the symbol universe, invokes the crowd
signal builder, computes day-over-day trend, writes the three crowd artifacts,
and persists raw events + daily signals to SQLite.

---

## Two-Lane Governance

Observe-only context producer, **simulation-active / production-gated**. It
writes ONLY the three `crowd_intelligence*` artifacts (LATEST namespace) plus the
two `crowd_intelligence.db` tables. It never reads or mutates
`decision_plan.json`, allocations, or scoring. `run()` never raises — on any
failure it returns a degraded status dict. Any production use of crowd context
goes through the human-approved `sim_governance` promotion workflow.

---

## Inputs / Outputs

- **Inputs:** repo `root`, optional `symbols` override. Universe is otherwise
  built from advisory picks (`decision_plan.json`), holdings (`config.json`),
  and daily watchlist single-names (`watchlist_signals.json`), deduped,
  ticker-shape filtered, and capped (default 60) to bound governed FMP calls.
  Capability map is read from `outputs/latest/fmp_endpoint_capabilities.json`.
- **Artifacts written (OutputNamespace.LATEST → `outputs/latest/`):**

  | File | Path |
  |------|------|
  | JSON | `outputs/latest/crowd_intelligence.json` |
  | Markdown | `outputs/latest/crowd_intelligence.md` |
  | Status JSON | `outputs/latest/crowd_intelligence_status.json` |

- **Persistence:** `data/crowd_intelligence.db` (`crowd_raw_events` +
  `crowd_signal_daily` tables, via `CapabilityStore`).
- **JSON contract (`crowd_intelligence.json`):** `observe_only: true`,
  `source: "crowd_intelligence"`, `generated_at`, `weights`, and `symbols[]`
  (each an as-dict `CrowdSignal`: `composite_crowd_score`, `confidence`,
  `category_scores`, `enabled_sources`, `disabled_sources`, `top_reasons`,
  `warnings`, `data_freshness`, `source_records_count`, `composite_trend`,
  `trend_label`).

---

## Key Functions

- `run(root=".", *, symbols=None) -> dict` — non-blocking orchestrator: builds a
  governed `discovery` client, loads capabilities + universe, calls
  `crowd_signal_builder.build_signals`, applies trend vs the most-recent prior
  day, records events, upserts the daily row, and writes artifacts. Returns the
  status dict.
- `write_artifacts(signals, status, *, base_dir="outputs")` — writes the three
  LATEST artifacts via `safe_write_text`.
- `apply_trend(signals, prior_by_sym)` — sets `composite_trend` / `trend_label`
  (`building` with no history; `rising`/`falling`/`flat` at ±0.05).
- `_load_universe(root, *, max_symbols=60)` — free-artifact union with
  ticker-shape filter (rejects synthetic `decision_plan` rows like
  `EMERGENCY_FUND_2026-06-15`).

---

## Tests

Covered under `tests/` with the crowd-intelligence suite
(`python -m pytest -q tests -k crowd`).
