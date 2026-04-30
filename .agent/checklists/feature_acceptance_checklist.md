# Feature Acceptance Checklist

Use before marking any step complete and updating `.agent/project_state.yaml`.

---

## Module and Code

- [ ] New module file created at the correct path in `portfolio_automation/`
- [ ] Module is importable: `python -m py_compile portfolio_automation/<module>.py`
- [ ] Module API matches what was specified in the task packet
- [ ] No `TODO: implement` stubs left in production paths
- [ ] No hardcoded paths outside the `outputs/` namespace hierarchy

## Tests

- [ ] `tests/test_<module>.py` created
- [ ] Targeted tests pass: `python -m pytest -q tests/test_<module>.py`
- [ ] Full suite passes: `python -m pytest -q --ignore=tests/test_gui_api_health.py --ignore=tests/test_gui_insight_cards.py`
- [ ] Test count is proportional to module complexity
- [ ] Edge cases covered: empty inputs, missing files, zero events, malformed data
- [ ] No tests import or depend on external services

## Documentation

- [ ] `docs/<MODULE_NAME>.md` created or updated
- [ ] `docs/roadmap.md` updated (step marked complete with brief summary)
- [ ] `docs/OUTPUT_ARTIFACT_CONTRACTS.md` updated if new artifacts added
- [ ] Module API documented in the docs file

## Advisory-Only Constraints

- [ ] No scoring behavior changed (signal_score, confidence_score, effective_score, conviction_score, final_rank_score, recommendation_score)
- [ ] No allocation behavior changed
- [ ] No recommendation outcome changed
- [ ] No broker API calls added
- [ ] No auto-trading code added
- [ ] `observe_only: true` hardcoded in all new artifact payloads

## Output Namespace

- [ ] All file writes use `safe_write_json` or `safe_write_text` from `data_governance.py`
- [ ] Artifacts written to the correct namespace (LATEST, POLICY, HISTORICAL, SANDBOX)
- [ ] Replay code does not write to LATEST or POLICY namespaces
- [ ] Live pipeline code does not write to HISTORICAL (backtest) namespace

## Pipeline Integration (if main.py was modified)

- [ ] Integration is non-blocking: wrapped in `try/except`
- [ ] Exception caught as a warning so the pipeline always continues
- [ ] `dry_run` guard present: artifacts not written during dry run
- [ ] Log message includes module name for traceability

## Output Schema

- [ ] No existing artifact keys removed or renamed
- [ ] New keys added only (additive changes)
- [ ] GUI consumers not broken
- [ ] Memo consumers not broken

## No Forbidden Changes

From `.agent/project_state.yaml:forbidden_changes` — none of the following occurred:
- [ ] No scoring behavior changed without explicit approval
- [ ] No allocation behavior changed without explicit approval
- [ ] No recommendation behavior changed without explicit approval
- [ ] No output artifact schema broken
- [ ] No live namespace contamination from replay
- [ ] No auto-execution or trading introduced
