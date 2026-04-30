# VPS Validation Prompt Template

Use this template to generate production validation commands after Claude returns a final report.
The user runs these commands manually on the VPS — Claude does not have VPS access.

Replace `{{CHANGED_FILES}}`, `{{TEST_FILE}}`, and `{{ARTIFACT_PATHS}}` with values from Claude's final report.

---

## VPS Validation Commands — {{STEP_NAME}}

Copy and run these commands on the VPS after pulling the latest changes.

### 0. Activate environment

```bash
cd /path/to/repo
source .venv/bin/activate
```

### 1. Check git status

```bash
git status
git log --oneline -5
```

Expected: working tree clean after pull, recent commits visible.

### 2. Pull latest changes

```bash
git pull
```

Expected: fast-forward merge or already up to date.

### 3. Install dependencies (if requirements.txt changed)

```bash
pip install -r requirements.txt
```

Expected: all packages satisfied or newly installed. If `pyyaml` is missing:
```bash
pip install pyyaml
```

### 4. Compile-check changed files

```bash
python -m py_compile {{CHANGED_FILES}}
```

Expected: no output (silent = pass).

### 5. Targeted tests

```bash
python -m pytest -q tests/{{TEST_FILE}}.py
```

Expected: all tests pass. Note count.

### 6. Agent context check (if agent orchestration files changed)

```bash
python scripts/agent_context_check.py
```

Expected: prints current phase, step, next steps, advisory-only: true.

### 7. Full test suite

```bash
python -m pytest -q \
  --ignore=tests/test_gui_api_health.py \
  --ignore=tests/test_gui_insight_cards.py
```

Expected: all tests pass (1 known skip is acceptable).

### 8. Optional: Dry-run pipeline (if main.py changed)

```bash
bash scripts/preflight.sh
bash scripts/run_daily_safe.sh --dry-run
```

Expected: preflight passes, dry run completes without errors.

### 9. Verify output artifacts (if new artifacts were added)

```bash
ls -la outputs/latest/
ls -la outputs/policy/

{{ARTIFACT_VERIFICATION_COMMANDS}}

# Example for a new artifact:
cat outputs/latest/ai_budget_summary.json | python -m json.tool | head -20
cat outputs/latest/data_quality_report.json | python -m json.tool | head -20
```

Expected:
- New artifact files exist.
- JSON parses cleanly.
- `observe_only` field is `true`.
- `available` field is `true` or `false` (not missing).

### 10. Check systemd timer status (if cron/timer changed)

```bash
systemctl --user status portfolio-daily.timer
systemctl --user list-timers
```

Expected: timer is active, next trigger is correct.

### 11. Check run_history (if pipeline ran)

```bash
python -c "
import sqlite3
conn = sqlite3.connect('data/portfolio.db')
rows = conn.execute('SELECT run_id, status, mode, created_at FROM run_history ORDER BY created_at DESC LIMIT 5').fetchall()
for r in rows:
    print(r)
conn.close()
"
```

Expected: recent rows with `status='success'` or expected status.
Note: use `mode` column, not `run_mode`. If the column does not exist, check migration status.

---

## Validation Result Reporting

After running the above, report back:

```
VPS Validation Results — {{STEP_NAME}}

Git pull: [success | already up to date | error]
pip install: [no changes | N packages installed | error]
py_compile: [pass | error — describe]
Targeted tests: [N passed, N failed]
Full suite: [N passed, N failed, N skipped]
Artifact check: [present | missing | json error]
observe_only in artifacts: [true | false | missing]
Timer status: [active | inactive | not checked]
Run history check: [N recent rows, status = X]
Overall: [PASS | FAIL — describe issue]
```

---

## Common Issues

| Issue | Fix |
|-------|-----|
| `ModuleNotFoundError: No module named 'yaml'` | `pip install pyyaml` |
| `ModuleNotFoundError: No module named 'portfolio_automation'` | `source .venv/bin/activate` |
| Artifact file missing after dry run | Expected — dry_run skips file writes |
| `mode` column missing from run_history | Run migration: `python portfolio_automation/migrations/001_add_user_id.py` |
| Tests import error | Check that changed files compile cleanly first |
