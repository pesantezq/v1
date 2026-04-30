# Architecture

Last verified against code on 2026-04-29.

## Purpose

This repository is an advisory-only portfolio analysis system. It produces rankings, alerts, sizing suggestions, policy recommendations, evaluation artifacts, and GUI-ready outputs. It does not place trades or invoke any broker API.

Two analysis paths are active:

1. `watchlist_scanner/*`
   Produces watchlist alerts, conviction/sizing overlays, portfolio-construction previews, regime summaries, and performance feedback.
2. `main.py` + `scanner/candidate_scanner.py` + `digest_builder.py`
   Produces portfolio snapshots, scored finance recommendations, broader-market candidate outputs, policy evaluation inputs, and human/AI memo artifacts.

Decision Engine status:

- `portfolio_automation/decision_engine.py` is implemented and tested
- pipeline integration in `main.py` is live in observe-only mode
- `decision_plan.json` and `decision_plan.md` are additive artifacts
- `portfolio_automation/decision_explainer.py` is implemented and wired as an additive downstream consumer
- `portfolio_automation/ai_decision_validator.py` is implemented and wired as an additive downstream validation layer
- `portfolio_automation/decision_outcome_tracker.py` is implemented and wired as an additive downstream feedback layer

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

Decision Engine and learning flow:

```text
Data
    -> Scanner
    -> Decision Engine
    -> decision_plan.json
    -> decision_reason_structured
    -> Insight Cards
    -> AI Validation
    -> ai_decision_validation.json
    -> Decision Outcome Tracker
    -> decision_outcomes.jsonl
    -> decision_outcome_summary.json
```

System boundaries:

- observe-only only
- rules-first decision generation
- AI is limited to explanation and validation only
- feedback loop is a learning layer, not an execution layer

AI Explanation Layer flow:

```text
outputs/latest/decision_plan.json
    + outputs/latest/system_decision_summary.json (optional)
    -> portfolio_automation/decision_explainer.py
    -> outputs/latest/decision_explanations.json
    -> outputs/latest/decision_explanations.md
```

AI Validation Layer flow:

```text
outputs/latest/decision_plan.json
    -> portfolio_automation/ai_decision_validator.py
    -> deterministic validation rules
    -> optional LLM enhancement when AI_VALIDATOR_USE_LLM=1
    -> outputs/latest/ai_decision_validation.json
    -> outputs/latest/ai_decision_validation.md
    -> GUI "AI Validation" section
```

Decision Outcome Tracker flow:

```text
outputs/latest/decision_plan.json
    + outputs/latest/ai_decision_validation.json
    + outputs/latest/watchlist_signals.json
    -> portfolio_automation/decision_outcome_tracker.py
    -> outputs/policy/decision_outcomes.jsonl
    -> outputs/policy/decision_outcome_summary.json
    -> outputs/policy/decision_outcome_summary.md
    -> GUI "Decision Performance" section
```

Daily memo consumption flow:

```text
outputs/latest/system_decision_summary.json
    + outputs/latest/decision_plan.json (optional, additive)
    -> watchlist_scanner/daily_memo.py
    -> daily_memo.txt / daily_memo.md
    -> operator email / human review surfaces
```

GUI Decision Center consumption flow:

```text
outputs/latest/system_decision_summary.json
    + outputs/latest/decision_plan.json
    -> gui_operator_data.py
    -> gui/app.py Decision Center
    -> compact decision brief
    -> full queue / detailed tables / expanders
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
    -> Decision Engine (central observe-only action-plan layer)
    -> decision_plan.json / decision_plan.md
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
  Central observe-only advisory unification layer that converts structural violations, portfolio adjustments, finance recommendations, watchlist signals, and market opportunities into one ranked action plan with consolidation and conflict resolution.
- `policy_evaluator/*`
  Recommendation history, evaluation, outcome attribution, and report writing.
- `agent/bundle_builder.py`
  Consolidates artifacts into an AI-oriented bundle for downstream agent use.

## Decision Engine Observe-Only Layer

Current integrated behavior:

- call `build_decision_plan(...)` only after portfolio adjustments, finance recommendations, watchlist signals, and market opportunities already exist
- emit additive artifacts:
  - `outputs/latest/decision_plan.json`
  - `outputs/latest/decision_plan.md`
- include additive structured explainability fields on decision rows, including `decision_reason_structured`
- after the decision plan is written, call the additive explainer
- emit explanation artifacts:
  - `outputs/latest/decision_explanations.json`
  - `outputs/latest/decision_explanations.md`
- then run the additive AI validation layer
- emit validation artifacts:
  - `outputs/latest/ai_decision_validation.json`
  - `outputs/latest/ai_decision_validation.md`
- then run the additive decision outcome tracker
- emit feedback artifacts:
  - `outputs/policy/decision_outcomes.jsonl`
  - `outputs/policy/decision_outcome_summary.json`
  - `outputs/policy/decision_outcome_summary.md`
- log the top 3 ranked decisions
- leave existing recommendation behavior unchanged
- leave existing output schemas unchanged

The Decision Engine is now the central observe-only action-plan layer, but it is still intentionally additive. It does not replace current recommendation outputs or change execution behavior.

## AI Explanation Layer

Current integrated behavior:

- `portfolio_automation/decision_explainer.py` reads artifacts only
- it uses deterministic logic only in v1
- it does not call an LLM or any external API
- it does not mutate `decision_plan.json`
- failures are non-fatal and must not block the pipeline

Current explanation outputs:

- `outputs/latest/decision_explanations.json`
- `outputs/latest/decision_explanations.md`

Compact contract:

- maximum `5` explanations
- maximum `3` risks per explanation
- maximum `3` `what_to_watch_next` items
- concise explanation sentence only
- include `explanation_basis`
- include deterministic `ai_validation`:
  - `boost`
  - `neutral`
  - `caution`

Architectural role:

- explanation is a downstream reader of the decision plan
- it does not feed back into ranking, scoring, or recommendations
- it is safe for future GUI and memo consumers because it is additive and read-only

## AI Validation Layer

Current integrated behavior:

- `portfolio_automation/ai_decision_validator.py` runs after `decision_plan.json` is written
- deterministic rules run first
- optional LLM enhancement is enabled only when `AI_VALIDATOR_USE_LLM=1`
- validation failures are non-fatal
- validator never changes decisions, scores, ranks, or allocations

Current validation outputs:

- `outputs/latest/ai_decision_validation.json`
- `outputs/latest/ai_decision_validation.md`

Architectural role:

- validate, not decide
- rules-first
- AI is optional and non-blocking
- contradiction detection is QA and explainability only
- `WAIT` plus negated hold-off language is not a contradiction
- positive deploy/buy/open language against `WAIT`/`HOLD`/`AVOID` is a contradiction

## Feedback Loop

Current integrated behavior:

- `portfolio_automation/decision_outcome_tracker.py` runs after validation
- it snapshots decision rows into `outputs/policy/decision_outcomes.jsonl`
- it resolves outcomes on 1/3/7 day windows when prices are available
- it writes aggregated summaries for GUI and later calibration work
- tracker failures are non-fatal

Current feedback outputs:

- `outputs/policy/decision_outcomes.jsonl`
- `outputs/policy/decision_outcome_summary.json`
- `outputs/policy/decision_outcome_summary.md`

Resolution model:

- `SELL` and `AVOID` are directionally correct when price moves down
- `BUY` and `SCALE` are directionally correct when price moves up
- `WAIT` is correct when the move stays inside the wait threshold
- `HOLD` remains neutral and is excluded from hit-rate judgment

This is a learning system. It exists to support calibration and optimization later, not to alter current-run advisory decisions.

Architectural role:

- learning layer only
- supports hit rate, average return, and direction-correct measurement
- does not feed back into same-run decisions
- enables later calibration and optimization work

## Daily Memo Integration

The daily memo/reporting layer now consumes Decision Engine output as an additive downstream reader.

Current behavior:

- `watchlist_scanner/daily_memo.py` safely reads `outputs/latest/decision_plan.json`
- `generate_daily_memo(...)` attaches the decision plan only when the artifact is present
- missing `decision_plan.json` does not fail memo generation
- the memo renders a compact decision brief instead of a verbose data dump
- existing recommendation logic and output schemas remain unchanged

New memo sections sourced from the decision plan:

- `Top Insight`
  One or two short sentences only.
- `Top Decisions`
  Top 5 ranked decision records with action, symbol, priority, source, urgency, plain-English reason, and risk flags.
- `Capital Actions`
  SELL / SCALE / BUY summary plus total recommended capital amount when decision records include amounts.
- `Risk Focus`
  Structural decisions first, with concentration and leverage risks highlighted when present.
- `What Changed`
  Maximum 3 bullets.
- `System / Data Health`
  Only when degraded or fallback conditions are active.

## GUI Decision Center Integration

The GUI Decision Center v1 is now implemented and mirrors the compact daily-memo contract before showing detailed tables.

Current behavior:

- `gui_operator_data.py` reads:
  - `outputs/latest/decision_plan.json`
  - `outputs/latest/system_decision_summary.json`
- the page starts with an observe-only banner:
  - `Observe-only decision plan. No trades are executed.`
- the GUI remains artifact-driven and read-only
- no decision recomputation happens in the GUI
- the compact summary appears before detailed action tables
- the full decision queue remains available below the summary in a dedicated expander/table

Compact GUI summary contract:

- `Top Insight`
- `Top Decisions`
  Maximum 5.
- `Capital Actions`
  Grouped summary only.
- `Risk Focus`
  Maximum 3.
- `What Changed`
  Maximum 3.
- `System / Data Health`
  Only when degraded or fallback conditions are active.

Top decision rows render as:

- `ACTION SYMBOL | source | urgency | pri X.XXX`
- followed by one short human-readable reason

Reason formatting is intentionally compact:

- leverage breach -> `Leverage exceeds cap (current vs cap).`
- concentration breach -> `Concentration exceeds cap (current vs cap).`
- rebalance or drift -> `Drift exceeds rebalance threshold.`
- relative strength -> `Relative strength near highs.`
- momentum or breakout -> `Momentum breakout near highs.`

Long raw reasons are preserved in the full queue below the summary and are not dumped into the compact header block.

Architectural impact:

- the Decision Engine is now consumed by both operator-facing memo generation and GUI summary rendering
- both memo and GUI remain read-only and observe-only
- downstream consumers should reuse the same decision-plan helper layer where possible so memo, GUI, and explanation surfaces stay consistent

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
- Decision-plan artifacts are additive contracts.
  They must not silently break existing outputs that GUI or memo flows already consume.

## Next Implementation Step

Decision Engine, GUI Decision Center v1, and the AI Explanation Layer are now live. The next step is to decide which downstream surface should consume `decision_explanations.*` first while preserving read-only, artifact-driven behavior.
