# Regression Checklist

Run this after any change that touches `main.py`, `portfolio_automation/`, or `tests/`.

---

## 1. Compile Check

```bash
# Changed files only:
python -m py_compile portfolio_automation/<changed_module>.py

# All core modules (run if scope is broad):
python -m py_compile \
  portfolio_automation/data_governance.py \
  portfolio_automation/signal_registry.py \
  portfolio_automation/data_quality_monitor.py \
  portfolio_automation/ai_budget.py \
  portfolio_automation/decision_engine.py \
  portfolio_automation/decision_explainer.py \
  main.py
```

- [ ] Compile check passes for all changed files
- [ ] No new syntax errors

---

## 2. Targeted Tests

```bash
python -m pytest -q tests/test_<new_module>.py
```

- [ ] Targeted tests pass
- [ ] Test count matches expectations from task packet

---

## 3. Related Module Tests

If the change touches shared infrastructure (data_governance, main.py, decision_engine):

```bash
python -m pytest -q tests/test_data_governance.py
python -m pytest -q tests/test_signal_registry.py
python -m pytest -q tests/test_data_quality_monitor.py
python -m pytest -q tests/test_ai_budget.py
python -m pytest -q tests/test_decision_engine.py
python -m pytest -q tests/test_decision_engine_pipeline.py
python -m pytest -q tests/test_decision_explainer.py
```

- [ ] Related tests pass (identify which are relevant to this change)

---

## 4. Full Test Suite

```bash
python -m pytest -q \
  --ignore=tests/test_gui_api_health.py \
  --ignore=tests/test_gui_insight_cards.py
```

- [ ] Full suite passes
- [ ] No new failures introduced
- [ ] 1 known skip is acceptable; investigate any new skips

---

## 5. Dependency Check

```bash
pip install -r requirements.txt
python -c "import yaml; import portfolio_automation; print('imports OK')"
```

- [ ] All dependencies install without conflict
- [ ] Imports succeed
- [ ] No version conflicts reported

---

## 6. Import Check for New Module

```bash
python -c "from portfolio_automation.<new_module> import <MainClass>; print('import OK')"
```

- [ ] New module imports cleanly with no side effects
- [ ] No circular imports introduced

---

## 7. Agent Context Check (if .agent/ files changed)

```bash
python scripts/agent_context_check.py
```

- [ ] Script runs successfully
- [ ] Current phase, step, and next steps display correctly
- [ ] advisory_only: true
- [ ] no_auto_trading: true

---

## 8. Artifact Namespace Spot Check (if new file writes added)

After a live pipeline run (not dry run):

```bash
ls -la outputs/latest/
ls -la outputs/policy/
ls -la outputs/backtest/  # should NOT have new live artifacts
```

- [ ] New artifacts appear in the correct namespace
- [ ] No live artifacts in `outputs/backtest/`
- [ ] No replay artifacts in `outputs/latest/`

---

## Regression Pass Criteria

All boxes checked = regression pass.

Any failure = do not mark step complete. Investigate and fix before proceeding.
