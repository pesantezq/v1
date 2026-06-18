# Crowd Signal Builder

## Purpose

`portfolio_automation/crowd_intelligence/crowd_signal_builder.py` is the
composition core of the Crowd Intelligence subsystem (Lane B). For each symbol it
runs the five category adapters (news, analyst, insider, congress, attention),
composes their bounded scores into a `composite_crowd_score`, computes a
`confidence`, and collects raw events. The `social_sentiment` category is always
neutral/disabled (`PLAN_LOCKED` on the FMP Starter tier).

---

## Two-Lane Governance

Observe-only context producer, **simulation-active / production-gated**. It
produces `CrowdSignal` records and raw events; it never reads or mutates
`decision_plan.json`, allocations, or scoring. One adapter failing never sinks a
symbol — the exception is caught and that category degrades to a neutral
`CategoryResult` with a warning. Production use only via the human-approved
`sim_governance` promotion workflow.

---

## Inputs / Outputs

- **Inputs:** `symbols`, a governed FMP `client`, optional Phase-1 `capabilities`
  map, optional `signal_date` / `now_iso`.
- **Output:** `(signals, all_events, status)` where `signals` is a list of
  `CrowdSignal`, `all_events` is a list of event dicts (for persistence), and
  `status` is the run summary dict (consumed by `artifact_writer`).
- The builder does not write files; `artifact_writer` persists the result.

---

## Key Functions

- `build_signals(symbols, *, client, capabilities=None, signal_date=None,
  now_iso=None) -> (list[CrowdSignal], list[dict], dict)` — the orchestrator.
  Splits each category's endpoints into usable/disabled via the capability map,
  pre-fetches shared (non per-symbol) endpoints once, then runs every adapter per
  symbol.
- `_is_per_symbol(eid)` — true when an endpoint's `params_template` interpolates
  `{symbol}` (so it must run per-symbol rather than once-shared).

Composition detail:

- `composite_crowd_score = norm.composite(category_scores)` (weighted by
  `norm.WEIGHTS`).
- `confidence = norm.confidence(coverage, freshness, agree, completeness)` where
  coverage/completeness are over the five non-social categories.
- `status.overall_status` is `ok` when any signal carries source records, else
  `degraded`; a warning is emitted when the capability map is absent (endpoints
  assumed usable optimistically).

---

## Tests

Covered under `tests/` with the crowd-intelligence suite
(`python -m pytest -q tests -k crowd`).
