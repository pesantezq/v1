---
name: portfolio-vps-validation
description: Generate a complete, copyable VPS validation command sequence after a feature is implemented or a fix is shipped. Used by the user to verify changes on the production VPS, since Claude cannot run those tests itself. Always emits commands; never claims a VPS test result.
---

# Skill: portfolio-vps-validation

## Purpose

Generate a complete, copyable VPS validation command sequence after a feature is implemented.
Claude does not run on the VPS in `read_only_ops` mode. These commands are for the user to run
manually. When the operating mode is `dev_on_vps` (current default), Claude may run them
directly — but still emits the block so the user can re-run independently.

## When to Use

- After any implementation that changes `main.py` pipeline integration
- After any new artifact write path is added (observability v2 producers, etc.)
- After any change to `requirements.txt`
- After any wrapper-stage change (`scripts/run_daily_safe.sh` or `scripts/preflight.sh`)
- After any resolver / data-flow fix (verify the downstream artifact actually populated)
- At the end of any feature final report

## When NOT to Use

- For docs-only changes (no VPS validation needed)
- For changes to `.agent/` YAML files only (use `agent_context_check.py` only)
- If the user explicitly says VPS validation is not needed for this step

## Step-by-Step Process

1. **Identify changed files** from the final report or `git diff`.
2. **Identify new artifacts** — path, namespace, expected key fields.
3. **Identify dependency changes** — did `requirements.txt` change?
4. **Identify wrapper changes** — did `run_daily_safe.sh` or `preflight.sh` change?
5. **Generate the validation sequence** using `.agent/task_templates/vps_validation_prompt.md`.

Fill in:
- `{{CHANGED_FILES}}` — space-separated list of `.py` files
- `{{TEST_FILE}}` — test module name (or `*` if multiple)
- `{{ARTIFACT_PATHS}}` — `stat` and `head` commands for new artifact files
- `{{ARTIFACT_KEY_FIELDS}}` — key fields to verify in the artifact JSON (e.g., `observe_only`, `overall_status`, `available`)

## Required Final Output

A copyable bash block with these sections, in order:

```bash
# 1. Activate environment
cd /opt/stockbot
source .venv/bin/activate

# 2. Git status + pull (only if changes pushed from another machine)
git status && git pull

# 3. Install dependencies (only if requirements.txt changed)
# pip install -r requirements.txt

# 4. Compile check (changed py files)
python -m py_compile <changed_files>

# 5. Wrapper syntax check (if run_daily_safe.sh or preflight.sh changed)
# bash -n scripts/run_daily_safe.sh && bash -n scripts/preflight.sh

# 6. Targeted tests
python -m pytest -q tests/<test_file>.py

# 7. Agent context check (if .agent/ files changed)
# python scripts/agent_context_check.py

# 8. Full suite (always)
python -m pytest -q \
  --ignore=tests/test_gui_api_health.py \
  --ignore=tests/test_gui_insight_cards.py

# 9. Dry-run validation of new module (if it has a __main__ entry)
# python -m portfolio_automation.<module>

# 10. Artifact verification (after live run)
stat -c '%y %n' outputs/latest/<artifact>.json outputs/latest/<artifact>.md
python -c "
import json
p = json.loads(open('outputs/latest/<artifact>.json').read())
print('observe_only:', p.get('observe_only'))
print('source:', p.get('source'))
print('overall_status:', p.get('overall_status'))
"

# 11. Optional: force re-run pipeline (only when validating end-to-end wiring)
# python -c "from state_store import PortfolioStateStore; from datetime import date; \
#   PortfolioStateStore().fail_run(f'{date.today().isoformat()}_daily')"
# bash scripts/run_daily_safe.sh
```

Always end with: "Report these results back. The step will be marked complete in `.agent/project_state.yaml` after VPS validation passes."

## Operating-Mode Note

The Operating Mode affects how this block is used:

- **`dev_on_vps`** (current default per `CLAUDE.md` operating-mode section): Claude *may* run the commands directly on `/opt/stockbot`. The block is still produced verbatim so the user has the auditable record.
- **`read_only_ops`** (target end state): Claude returns the block as text only. Never claims tests passed without operator confirmation.

## Cron Sanity Check

When validating a new wrapper stage, also verify the next cron run produces the expected artifact:

```bash
# Check this morning's cron log for the new stage
grep "<Stage Name>" logs/daily_safe_$(date '+%Y-%m-%d').log

# Check the artifact mtime matches today
stat -c '%y %n' outputs/latest/<new_artifact>.json
```

If the cron hasn't run yet (before 09:00 UTC), the operator can force a one-shot validation
of the new wrapper stages without waiting:

```bash
bash scripts/run_daily_safe.sh
```
