# Flock Intelligence — Producer

## Purpose

`portfolio_automation/flock_intelligence/producer.py` orchestrates the
simulation-only Flock Intelligence pipeline: load metrics → classify group flock
state → emit simulation artifacts. Flock Intelligence detects crowd
flocking/dispersion across themes, sectors, and tickers (reusing existing crowd +
price artifacts, no new paid data). It is research context, never a
recommendation.

---

## Two-Lane Governance

**Simulation-only.** The producer writes ONLY to the SIMULATION namespace
(`outputs/simulation/`). It may change simulation context / watchlist candidates
/ advisory context that the `sim_governance` lane consumes and the GUI displays,
but it never touches production. Every artifact carries the observe fields
(`observe_only`, `no_trade`, `not_recommendation`, `sandbox_only`,
`simulation_only`) and the disclaimer. Production behavior changes only via a
human-approved promotion proposal. The classifier never reads the cross-source
enrichment aggregates — those are explainability context only.

---

## Artifacts Written (OutputNamespace.SIMULATION → `outputs/simulation/`)

| File | Contents |
|------|----------|
| `flock_intelligence.json` | Full report: groups + tickers + state summary |
| `flock_watchlist_candidates.json` | Simulation watchlist adds / tags / rank deltas |
| `flock_advisory_context.json` | Per-symbol advisory flock context |
| `flock_state_history.json` | Prior-state ledger for the next run |

`flock_intelligence.json` carries `source`, `schema_version`, `generated_at`,
the observe fields, `data_quality_status` (`ok` | `insufficient_data`),
`group_count`, `ticker_count`, `groups[]`, `tickers[]`, and a `summary`
bucketing groups by state (forming / confirmed / exhaustion / dispersing /
broken / insufficient).

---

## Key Functions

- `run_flock_intelligence(root, now, *, base_dir=None, write_files=True,
  watchlist=None, groups_override=None, crowd_override=None,
  returns_override=None, th=None) -> dict` — builds and (optionally) writes all
  four artifacts. All inputs are injectable for tests; never raises; returns a
  degraded report (`data_quality_status="insufficient_data"`) when there is
  nothing to classify. Write failures are logged and recorded in
  `report["write_errors"]` without breaking the pipeline.
- `build_group_metrics(group, kind, tickers, crowd, returns, prior) ->
  GroupMetrics` — pure assembly of velocity/breadth/correlation/spread/momentum/
  volatility + flock/dispersion/exhaustion composite scores, plus the additive
  unified cross-source aggregates (`crowd_source` is `"unified"` or `"legacy"`).
- `_ticker_flocks(...)`, `_watchlist_candidates(...)`, `_advisory_context(...)` —
  per-ticker context, sim watchlist candidate derivation (by `_TAG_BY_STATE`),
  and per-symbol advisory context (best/most-confident group per ticker).

The six flock states are produced by `flock_intelligence/states.py`
(`classify_group`); per-state meanings drive the advisory `_MEANING` labels.

---

## Pipeline Integration

Invoked as Step 1 of the daily governance orchestrator
(`portfolio_automation/sim_governance/daily_governance_run.py`), which writes the
simulation artifacts the active simulation lane then consumes as baseline.

---

## Tests

Covered under `tests/` with the flock-intelligence suite
(`python -m pytest -q tests -k flock`).
