# Deployment Validation Checklist

Use on the VPS after pulling changes. Run all steps in order.
Claude does not run on the VPS — this checklist is for the user.

---

## Environment Setup

- [ ] SSH into VPS as the correct user
- [ ] Repo directory is correct: `pwd` returns expected path
- [ ] Virtualenv is active: `which python` points to `.venv/bin/python`
  ```bash
  source .venv/bin/activate
  ```
- [ ] Python version correct: `python --version` → 3.11+

---

## Git State

- [ ] Working tree is clean: `git status` shows no uncommitted changes
- [ ] Latest commits are visible: `git log --oneline -5`
- [ ] Pull succeeds: `git pull` returns fast-forward or already up to date
  ```bash
  git status
  git pull
  ```

---

## Dependencies

- [ ] `requirements.txt` installed successfully
  ```bash
  pip install -r requirements.txt
  ```
- [ ] `pyyaml` is available: `python -c "import yaml; print(yaml.__version__)"`
- [ ] `portfolio_automation` is importable: `python -c "import portfolio_automation"`

---

## Compile Check

- [ ] All changed Python files compile cleanly
  ```bash
  python -m py_compile portfolio_automation/<changed_module>.py
  python -m py_compile main.py  # if main.py changed
  python -m py_compile scripts/agent_context_check.py  # if agent scripts changed
  ```

---

## Tests Passing

- [ ] Targeted tests pass for the new module
  ```bash
  python -m pytest -q tests/test_<new_module>.py
  ```
- [ ] Full suite passes
  ```bash
  python -m pytest -q \
    --ignore=tests/test_gui_api_health.py \
    --ignore=tests/test_gui_insight_cards.py
  ```
- [ ] Test count matches expected from Claude's final report

---

## Agent Context Check (if .agent/ files changed)

- [ ] Context check runs without error
  ```bash
  python scripts/agent_context_check.py
  ```
- [ ] Output shows correct current phase and step
- [ ] `advisory_only: true`
- [ ] `no_auto_trading: true`

---

## Artifact Verification (after a live run, not dry run)

- [ ] Expected artifacts exist in `outputs/latest/`:
  ```bash
  ls -la outputs/latest/
  ```
- [ ] New artifact file exists at declared path
- [ ] JSON artifact is valid:
  ```bash
  cat outputs/latest/<new_artifact>.json | python -m json.tool | head -30
  ```
- [ ] `observe_only` field is `true` in new artifact
- [ ] `available` field is present
- [ ] No artifacts accidentally written to `outputs/backtest/` by live pipeline

---

## Run History Check

Use the `mode` column (not `run_mode`):

```bash
python -c "
import sqlite3
conn = sqlite3.connect('data/portfolio.db')
rows = conn.execute(
    'SELECT run_id, status, mode, created_at FROM run_history ORDER BY created_at DESC LIMIT 5'
).fetchall()
for r in rows:
    print(r)
conn.close()
"
```

- [ ] Recent rows visible
- [ ] Status column shows expected values
- [ ] `mode` column exists (if migration ran)
- [ ] No unexpected failures in run history

---

## Systemd Timer Status

```bash
systemctl --user status portfolio-daily.timer
systemctl --user list-timers | grep portfolio
```

- [ ] Timer is active
- [ ] Next trigger time is correct
- [ ] No failed status

---

## Optional: Dry-Run Pipeline (if main.py changed)

```bash
bash scripts/preflight.sh
bash scripts/run_daily_safe.sh --dry-run
```

- [ ] Preflight passes
- [ ] Dry run completes without error
- [ ] Dry run log shows expected new module messages

---

## Deployment Pass Criteria

All boxes checked = deployment validated.

Any failure: investigate root cause before marking the step complete in `.agent/project_state.yaml`.
Report failures back so the roadmap state stays accurate.
