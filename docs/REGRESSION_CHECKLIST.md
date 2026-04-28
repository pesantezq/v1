# Regression Checklist

Use this before merging any change that touches scoring, ranking, allocation, state, FMP wiring, or output artifacts.

## 1. Compile And Import Checks

- Run `python -m compileall .`
- Confirm there are no syntax errors in `watchlist_scanner`, `policy_evaluator`, `gui`, and top-level modules.

## 2. Targeted Unit Tests

- Run `python -m unittest tests.test_watchlist_scanner_alerts -v`
- Run `python -m unittest tests.test_watchlist_confidence_cooldown -v`
- Run `python -m unittest tests.test_watchlist_conviction -v`
- Run `python -m unittest tests.test_watchlist_portfolio_construction -v`
- Run `python -m unittest tests.test_state_store -v`
- Run `python -m unittest tests.test_policy_evaluator -v`
- Run `python -m unittest tests.test_gui_operator_dashboard -v`

## 3. Endpoint Validation

- Run `python -m unittest tests.test_fmp_endpoint_compliance -v`
- Run `python -m unittest tests.test_fmp_fallback -v`
- Run `python -m unittest tests.test_fmp_batch_quotes_stable -v`
- If FMP wiring changed, confirm stable endpoints were not regressed back to incompatible legacy paths.

## 4. Pipeline Dry Runs

- Run `python -m watchlist_scanner --dry-run`
- Run `python run_daily_pipeline.py --dry-run`
- If `main.py` behavior changed, run `python main.py --run-mode daily --dry-run`

## 5. Artifact Validation

Verify that these files still exist and load as valid JSON after a run:

- `outputs/latest/watchlist_signals.json`
- `outputs/latest/theme_signals.json`
- `outputs/latest/watch_candidates.json`
- `outputs/portfolio/portfolio_snapshot.json`
- `outputs/policy/policy_recommendation.json`
- `outputs/policy/recommendation_evaluation.json`
- `outputs/performance/performance_summary.json`
- `outputs/latest/system_decision_summary.json`

Check these invariants:

- `watchlist_signals.json` still has `results` and `alerts`
- `portfolio_snapshot.json` still has `rows`
- `policy_recommendation.json` still has `recommendation.recommended_policy`, `recommendation.recommended_profile`, `recommendation.recommendation_score`
- empty-data runs degrade to empty/null/default values rather than contract breakage

## 6. Scoring And Ranking Integrity

- Confirm `signal_score` meaning did not change unless explicitly intended
- Confirm `confidence_score` still measures trustworthiness, not attractiveness
- Confirm `effective_score`, `conviction_score`, and `final_rank_score` are still clearly derived fields
- Confirm any derived metric additions did not replace base score fields

## 7. Allocation Integrity

- Confirm watchlist portfolio construction still respects total, ticker, and sector caps
- Confirm broader allocation engine still respects reserve, position cap, sector cap, and degraded penalties
- Confirm observe-only behavior remains explicit in output fields

## 8. State Schema Integrity

- Confirm `data/portfolio.db` opens successfully
- Inspect `PRAGMA table_info(...)` for any changed table
- Verify migrations are additive and old rows remain readable
- Confirm no table or column used by GUI/tests was silently removed or renamed

## 9. GUI Validation

- Run `python -m unittest tests.test_gui_api_health -v`
- Run `python -m unittest tests.test_gui_insights -v`
- Run `python -m unittest tests.test_gui_operator_dashboard -v`
- Launch `streamlit run gui/app.py` and verify the dashboard still loads without missing-key crashes

## 10. Behavior Sanity Checks

- Cooldown-suppressed alerts still appear in result rows with suppression metadata
- Degraded mode lowers certainty or size; it does not increase conviction
- Missing history produces empty evaluation summaries rather than errors
- `outputs/latest` remains current and `outputs/history/YYYY-MM-DD` archival behavior still works after successful `main.py` runs

## 11. High-Risk Changes That Require Extra Care

- Any change to output field names
- Any change to `signal_score` or `confidence_score` semantics
- Any change to `final_rank_score` weights
- Any change to alert fingerprint/state-hash behavior
- Any change to SQLite table names or primary keys
