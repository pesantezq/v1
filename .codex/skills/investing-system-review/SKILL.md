---
name: investing-system-review
description: Review this repo's watchlist, alerting, scoring, and enrichment logic for small, high-impact improvements. Prioritize alert quality, explainability, score separation, and modular diff-friendly patches validated by tests or reproducible commands.
---

# Investing System Review

Use this skill when reviewing or improving watchlist behavior, signal ranking, confidence scoring, evidence aggregation, theme/news enrichment, alert suppression, promotion logic, cooldown-adjacent state handling, or output quality.

## Workflow

1. Identify the user-visible output being affected: `watchlist_signals.json`, `watchlist_alerts.csv`, `watchlist_summary.md`, digest output, or ranking state.
2. Trace the shortest function-level path from input data to that output.
3. Name exact files and functions before proposing or making edits.
4. Review adjacent suppression, promotion, ranking, confidence, derived-score, and evidence logic so the patch does not create noisy regressions.
5. Prefer the smallest modular change that improves alert quality, reduces fatigue, or preserves explainability.

## Repo Analysis Path

- Watchlist entry/output path: `watchlist_scanner/__main__.py` via `run()`, `_write_signals_json()`, `_write_alerts_csv()`, `_write_summary_md()`
- Watchlist signal path: `watchlist_scanner/scanner.py` via `WatchlistScanner.run()`, `_scan_symbol()`, `_compute_signal_score()`, `_evaluate_alert_decision()`
- Confidence/evidence path: `watchlist_scanner/confidence.py` via `compute_confidence()`, plus `watchlist_scanner/fundamentals_engine.py` and `watchlist_scanner/theme_engine.py`
- Theme/promotion path: `theme_engine/__main__.py` via `run()`, `theme_engine/theme_store.py` via `save_signals()`, and `watchlist_scanner/extended_watchlist.py` via `evaluate_candidates()`
- Secondary ranking path: `main.py` plus `scanner/candidate_scanner.py` via `full_scan()`, `weekly_refresh()`, `daily_refresh()`, and `apply_theme_boosts()`
- Digest/state path: `digest_builder.py` via `build_theme_highlights()`, and `state_store.py` via alert cooldown helpers when alert-fatigue behavior is relevant

## Checklist

- Does this improve alert quality or reduce alert fatigue?
- Are `signal_score` and `confidence_score` still separate, explicit, and correctly used?
- Does ranking preserve raw scores and label any derived metric clearly, including `trusted_signal_score` or theme boosts?
- Are suppression and promotion rules still understandable and evidence-based?
- Does stale or missing data reduce certainty instead of creating false confidence?
- Is the output still easy to audit from evidence to score to alert decision?
- Is there a focused test or reproducible command proving the change?

## Priority Order

1. Bugs that hurt alert quality, mis-rank symbols, or create false confidence
2. Alert suppression or promotion issues that increase fatigue or hide strong signals
3. Broken evidence aggregation or misleading theme/news enrichment
4. Output inconsistencies across JSON, CSV, markdown, and digest surfaces
5. Refactors only when they materially reduce risk in the touched path

## Output Format

- Scope: exact files and functions reviewed
- Findings: highest-risk issues first
- Proposed change: smallest high-impact patch
- Validation: tests or reproducible commands and expected outcome

Prefer validation such as:
- `python -m unittest tests.test_watchlist_scanner_alerts -v`
- `python -m unittest tests.test_theme_engine -v`
- `python -m unittest tests.test_scanner -v`
- `python -m watchlist_scanner --dry-run`
- `python -m theme_engine --mode daily --dry-run`

Always ground recommendations at the function level before editing. Prefer one or two diff-friendly fixes over rewrites.
