# Repository Purpose

This repo is an analysis-only investing automation system. It has two active analysis paths:
- the Alpha Vantage watchlist pipeline (`watchlist_scanner/*`) that produces `watchlist_signals.json`, `watchlist_alerts.csv`, `watchlist_summary.md`, `theme_signals.json`, and `watch_candidates.json`
- the broader portfolio and monthly candidate pipeline (`main.py`, `scanner/candidate_scanner.py`, `digest_builder.py`) that produces portfolio review, recommendation, and scanner ranking outputs

The core goal is better decision support, not trade execution.

# AI Agent Roles

## GPT (Planner)

- decides what to build next
- generates prompts
- merges outputs
- does NOT modify code

## Codex (Builder)

- implements features
- writes tests
- builds GUI
- handles file structure

## Claude (Analyst)

- evaluates performance
- builds attribution logic
- analyzes calibration
- generates reports

# System Rules

- No agent modifies ranking, conviction, alerts, allocation, or portfolio construction unless explicitly approved by the user for the specific task.
- All new systems must be advisory-only.
- All new systems must be read-only unless explicitly approved.
- Prefer small, modular changes.
- Prefer testable outputs.
- Prefer backward compatibility.
- If work is explicitly approved in a protected area, preserve the existing `signal_score` and `confidence_score` rules below and keep the change narrowly scoped and explainable.

# Operating Rules

- Optimize for alert quality first: better signal-to-noise, fewer noisy alerts, clearer evidence.
- Preserve explainability. A user should be able to see why a symbol was ranked, promoted, suppressed, or omitted.
- Keep `signal_score` and `confidence_score` explicitly separate.
- `signal_score` = attractiveness or opportunity strength.
- `confidence_score` = trustworthiness of today's evidence and data quality.
- Do not merge them, rename them, or let one silently stand in for the other.
- If a combined or adjusted rank is needed, keep the base scores intact and label the derived metric clearly, for example `trusted_signal_score` or a theme-boosted candidate rank.
- Prefer modular upgrades over rewrites. Change the narrowest function or module that improves behavior.
- Keep instructions and edits diff-friendly: small patches, stable field names, additive output changes where possible.
- Degrade gracefully when data is stale, missing, or budget-limited. Lower certainty or suppress alerts; do not invent conviction.

# Preferred Workflow

1. Trace the exact path from source data to output file.
2. Name exact files and functions before proposing or making edits.
3. Check nearby alert thresholds, suppression rules, promotion logic, derived ranking, and output formatting before changing behavior.
4. Prefer a focused patch in `watchlist_scanner/*`, `theme_engine/*`, `scanner/candidate_scanner.py`, `digest_builder.py`, or `state_store.py` over broad restructuring.
5. Validate with tests or reproducible commands, starting with the smallest relevant scope.

# Repository Priorities

- Alert quality over alert volume
- Signal-to-noise over novelty
- Explainable ranking over opaque scoring
- Suppression and promotion discipline over headline chasing
- Reproducible validation over intuition-only changes

# Scoring Guidance

- `signal_score` should reflect the quality of the opportunity or alert signal.
- `confidence_score` should reflect freshness, completeness, provenance, and evidence reliability.
- High signal plus low confidence is a watch candidate, not strong confirmation.
- High confidence plus weak signal is credible but not compelling.
- When changing thresholds or rank order, state exactly which score changed, where it changed, and how downstream alerting, suppression, or promotion is affected.
- If ordering uses a derived metric such as `trusted_signal_score`, explain how it relates to the base scores without replacing them.

# News, Themes, And Evidence

- Theme/news enrichment is supporting evidence, not a replacement for technical, fundamental, or guardrail logic.
- Favor durable, repeated, or reinforced themes over one-off headlines.
- Preserve evidence trails: headline counts, sentiment, theme names, direct mentions, persistence, suppression reasons, promotion reasons, boost metadata, and watchlist source should stay inspectable.
- Alert suppression and promotion logic should reduce fatigue without hiding materially strong signals.

# Validation

- Prefer targeted validation first, such as `python -m unittest tests.test_watchlist_scanner_alerts -v`, `python -m unittest tests.test_theme_engine -v`, or `python -m unittest tests.test_scanner -v`.
- Use reproducible dry-run commands when useful, such as `python -m watchlist_scanner --dry-run` or `python -m theme_engine --mode daily --dry-run`.
- Run broader validation like `python -m unittest discover tests/ -v` when the change crosses subsystem boundaries.

# Output Expectations

- Start with scope: exact files and functions reviewed.
- Then list findings or proposed edits in priority order.
- Tie each proposed change to expected impact on alert quality, fatigue, explainability, or ranking integrity.
- Include tests or reproducible commands for validation.
- Avoid vague references; name the exact function before suggesting an edit.
