# Architecture

Last verified against code on 2026-04-28.

## Purpose

This repository is an advisory-only portfolio analysis system. It produces rankings, alerts, sizing suggestions, policy recommendations, evaluation artifacts, and GUI-ready outputs. It does not place trades or invoke any broker API.

Two analysis paths are active:

1. `watchlist_scanner/*`
   Produces watchlist alerts, conviction/sizing overlays, portfolio-construction previews, regime summaries, and performance feedback.
2. `main.py` + `scanner/candidate_scanner.py` + `digest_builder.py`
   Produces portfolio snapshots, scored finance recommendations, broader-market candidate outputs, policy evaluation inputs, and human/AI memo artifacts.

Decision Engine status:

- `portfolio_automation/decision_engine.py` is implemented and tested
- pipeline integration is not live yet
- approved next step is observe-only additive wiring

## End-To-End Flow

```text
Config + .env
    |
    +--> main.py
    |      |
    |      +--> market_data.py / FMPClient / retirement.py
    |      +--> portfolio.py
    |      +--> guardrails.py
    |      +--> scanner/candidate_scanner.py
    |      +--> market_coverage pipeline
    |      +--> theme_engine/__main__.py
    |      +--> watchlist_scanner/__main__.py
    |      +--> recommendations.py + finance_analyzer.py + scoring.py
    |      +--> policy_evaluator.history_writer
    |      +--> file_output.py / digest_builder.py / agent bundle outputs
    |      +--> state_store.py
    |
    +--> run_daily_pipeline.py
           |
           +--> theme_engine
           +--> watchlist_scanner
           +--> watchlist_scanner.weight_tuning
           +--> policy_evaluator.evaluator
           +--> watchlist_scanner.allocation_preview
           +--> watchlist_scanner.allocation_policy_simulation
           +--> watchlist_scanner.allocation_policy_activation
           +--> watchlist_scanner.system_summary
           +--> watchlist_scanner.daily_memo

GUI (gui/app.py)
    |
    +--> reads JSON/CSV/Markdown artifacts only
    +--> reads SQLite state for status/history views
```

Decision Engine target flow:

```text
Data sources
    -> scanner / market coverage / theme engine
    -> scoring / conviction / allocation
    -> Decision Engine
    -> recommendations / GUI / AI explanation / daily memo
```

## Watchlist Pipeline

Primary entry point: `watchlist_scanner/__main__.py:run`

```text
Alpha Vantage / FMP / cache inputs
    -> WatchlistScanner.run()
    -> signal_score
    -> confidence_score
    -> alert routing
    -> cooldown + action suppression
    -> effective_score
    -> portfolio priority overlay
    -> final_rank_score
    -> conviction_score / conviction_band
    -> observe-only allocation preview
    -> market regime detection
    -> persistence + outcome tracking
    -> JSON / CSV / Markdown artifacts
```

Key modules:

- `watchlist_scanner/scanner.py`
  Raw scan orchestration, signal scoring, confidence computation, alert decision routing, theme/portfolio enrichment, and ranking.
- `watchlist_scanner/confidence.py`
  Trustworthiness scoring from freshness, completeness, cache age, and budget provenance.
- `watchlist_scanner/alert_filter.py`
  Emission thresholds, evidence gates, confidence tiers, and cooldown bypass rules.
- `watchlist_scanner/postprocess.py`
  Cooldown persistence, `effective_score`, action suppression, portfolio-priority overlay, operator ordering.
- `watchlist_scanner/conviction.py`
  Converts effective signal quality into observe-only conviction and sizing bands.
- `watchlist_scanner/portfolio_construction.py`
  Builds capped, normalized allocation previews and concentration warnings.
- `watchlist_scanner/performance_feedback.py`
  Records signal feedback, resolves forward outcomes, and writes performance summaries.
- `watchlist_scanner/output_writers.py`
  Contract owner for `watchlist_signals.json`, `watchlist_alerts.csv`, `portfolio_snapshot.json`, and summaries.

## Portfolio Pipeline

Primary entry point: `main.py:run_portfolio_update`

```text
Prices + holdings + retirement data
    -> portfolio summary / allocations / holding analysis
    -> drawdown regime
    -> structural guardrails
    -> broader-market scanner and portfolio decision layer
    -> theme engine
    -> watchlist scanner
    -> legacy buy/sell/hold recommendations
    -> scored finance recommendations
    -> Decision Engine (implemented, pending observe-only integration)
    -> recommendation history + evaluation inputs
    -> outputs/latest + SQLite snapshot + outputs/history/YYYY-MM-DD
```

Key modules:

- `portfolio.py`
  Core holdings math and summary generation.
- `guardrails.py`
  Non-blocking structural checks before recommendation generation.
- `scanner/candidate_scanner.py`
  Broader-market ranking of FMP-backed candidates for the speculative sleeve.
- `portfolio_decision_engine.py`
  Maps promoted opportunities into advisory actions such as `PROMOTE_TO_PORTFOLIO`, `BUY`, `SELL`, `TRIM`, or `ADD_TO_WATCHLIST`.
- `allocation_engine.py`
  Advisory sizing for broader-market actions.
- `recommendations.py`
  Legacy rules-based buy/sell/hold recommendation layer.
- `finance_analyzer.py` + `scoring.py`
  0-100 finance recommendation scoring and scored output export.
- `portfolio_automation/decision_engine.py`
  Central advisory unification layer that converts structural violations, portfolio adjustments, finance recommendations, watchlist signals, and market opportunities into one ranked observe-only decision plan.
- `policy_evaluator/*`
  Recommendation history, evaluation, outcome attribution, and report writing.
- `agent/bundle_builder.py`
  Consolidates artifacts into an AI-oriented bundle for downstream agent use.

## Decision Engine Integration Direction

Approved observe-only integration plan:

- call `build_decision_plan(...)` only after portfolio adjustments, finance recommendations, watchlist signals, and market opportunities already exist
- emit new additive artifacts:
  - `outputs/latest/decision_plan.json`
  - `outputs/latest/decision_plan.md`
- log the top 3 ranked decisions
- leave existing recommendation behavior unchanged
- leave existing output schemas unchanged

This is intentionally additive. The Decision Engine is not yet the source of truth for downstream consumers.

## State And Memory

System memory is split across:

- `data/portfolio.db`
  Persistent run history, snapshots, cooldown state, theme signals, watchlist outcomes, signal feedback, subsystem health, structural violations, and cash ledger.
- `outputs/latest/*`
  Current contracts consumed by the GUI and agent bundle.
- `outputs/history/YYYY-MM-DD/*`
  Daily archival copy of the latest outputs after a successful `main.py` run.
- `data/*cache*`
  Price/API/RSS caches and call counters.

## Separation Of Concerns

These boundaries are intentional and should not be collapsed:

- `signal_score` is attractiveness.
  It is computed before conviction, allocation, or outcome overlays.
- `confidence_score` is trustworthiness.
  It is not a proxy for attractiveness.
- `effective_score` is a derived actionability metric.
  It is not a replacement for `signal_score` or `confidence_score`.
- `conviction_score` is a downstream advisory sizing input.
  It depends on effective score, confidence, and feedback history.
- `final_rank_score` is an ordering score.
  It should remain separate from emission thresholds and base signal semantics.
- `recommendation_score` in `outputs/policy/policy_recommendation.json` is a policy/profile selection score.
  It is unrelated to watchlist `signal_score`.
- GUI code must consume artifacts.
  It should not re-implement business logic.
- State tables record outcomes and suppress repeats.
  They must not mutate base ranking semantics.

## Architectural Invariants

- Analysis only. No broker integration, no execution authority.
- `observe_only` and `recommend_only` are hard behavioral constraints, not presentation flags.
- Output artifacts are contracts. GUI and agent code assume they remain backward compatible.
- SQLite tables are append/update state, not a replacement for output artifacts.
- Theme/news enrichment is additive evidence only.
- Derived metrics may be added, but base score names and meanings must remain intact.
- The Decision Engine must remain advisory only.
  It may unify and rank actions, but it must not execute trades or bypass structural guardrails.

## Next Implementation Step

Wire the Decision Engine into the daily portfolio path as an additive observe-only artifact generator, then validate that the new decision-plan outputs appear without changing existing recommendation or GUI contracts.
