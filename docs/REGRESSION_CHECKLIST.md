# Regression Checklist

Use this before merging any change that touches scoring, ranking, allocation, state, FMP wiring, output artifacts, or production automation.

## 0. Production Preflight Gate

- `bash scripts/preflight.sh` is mandatory before production daily runs
- `python -m fmp_endpoint_compliance` must still emit `RESULT: COMPLIANT`
- `python -m pytest tests/ -k fmp -v` must pass before any production pipeline execution
- no FMP endpoint changes may bypass `fmp_endpoint_registry.py`
- production daily automation should use `bash scripts/run_daily_safe.sh`, not a direct `python main.py` cron entry

## 0b. Test Acceleration — Development Loop vs Final Gate (2026-09-27)

The official full-suite command is unchanged and remains the certification
universe:

- `python -m pytest -q --ignore=tests/test_gui_api_health.py --ignore=tests/test_gui_insight_cards.py`

Test-only tooling lives in `requirements-dev.txt` (`pytest`, `pytest-xdist`);
install it alongside `requirements.txt` for development and CI. It is never a
runtime dependency and is inert unless `-n` is passed.

**Development loop** (selective, fast):

- focused: `python -m pytest -q tests/<relevant_test>.py`
- affected domain: `python -m pytest -q -n 8 $(python scripts/ci_test_shards.py files <shard>)`
- whole suite locally: `python -m pytest -q -n 8 --ignore=tests/test_gui_api_health.py --ignore=tests/test_gui_insight_cards.py`
  (measured on the QPC, 8 CPUs: ~37 s wall vs ~338 s serial; 8 workers was the
  knee — 10 and 12 were slower)
- `-x` is fine here.

**Final exact-head certification** (complete, never inferred):

- the complete required universe, no `-x`, no `--lf`/`--ff`, no `.pytest_cache`
  reuse, no testmon, no verdict carried over from an earlier SHA;
- CI runs it as six domain shards (`governance`, `evidence_data`,
  `broker_portfolio`, `strategy_research`, `gui_readmodels`, `core`) with 4
  xdist workers each, plus a serial phase for tests marked `serial`
  (`pytest.ini`; nine tests that write shared checkout files, each justified
  at its marker);
- `python scripts/ci_test_shards.py verify` proves by exact node ID that the
  union of the shards equals the official collection, with no duplicates; the
  CI governance job runs it on every push and `tests/test_ci_test_shards.py`
  pins the static contract (every test file in exactly one shard, `core` is
  the catch-all, workflow matrix == shard names, deselect list shared);
- the ten CI-deselected node IDs (live-artifact / production-host tests) are
  declared once in `scripts/ci_test_shards.py` and still run in the VPS-side
  full suite.

Only dependencies are cached in CI (`pip`); correctness is never cached.

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

**FMP COMPLIANCE (MANDATORY)**
- [ ] `python -m fmp_endpoint_compliance` → `RESULT: COMPLIANT`
- [ ] `python -m pytest tests/ -k fmp -v` → 100% PASS
- [ ] No new endpoints bypass registry
- [ ] No v3 endpoints added without explicit approval

- Run `python -m unittest tests.test_fmp_endpoint_compliance -v`
- Run `python -m unittest tests.test_fmp_endpoint_registry_compliance -v`
- Run `python -m unittest tests.test_fmp_fallback -v`
- Run `python -m unittest tests.test_fmp_batch_quotes_stable -v`
- If FMP wiring changed, confirm stable endpoints were not regressed back to incompatible legacy paths.

## 4. Pipeline Dry Runs

- Run `python -m watchlist_scanner --dry-run`
- Run `python run_daily_pipeline.py --dry-run`
- If `main.py` behavior changed, run `python main.py --run-mode daily --dry-run`
- For production safety checks, run `DRY_RUN_MODE=1 bash scripts/run_daily_safe.sh`

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
- system-summary artifact health uses severity-aware wording
- `defaulting` and `optional_missing` must not inflate `missing_artifact_count`
- missing-artifact messages must name the exact file path and producer step
- `approved_ranking_config.json` and `approved_allocation_policy.json` absent state should read as `defaulting`, not critical missing
- `theme_opportunities.json` absent while `theme_signals.json` exists should read as `optional_missing`, not critical missing

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
