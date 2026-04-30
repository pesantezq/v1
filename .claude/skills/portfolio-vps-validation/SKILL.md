# Skill: portfolio-vps-validation

## Purpose

Generate a complete, copyable VPS validation command sequence after a feature is implemented.
Claude does not run on the VPS. These commands are for the user to run manually.

## When to Use

- After any implementation that changes `main.py` pipeline integration
- After any new artifact write path is added
- After any change to `requirements.txt`
- At the end of any feature final report

## When NOT to Use

- For docs-only changes (no VPS validation needed)
- For changes to `.agent/` YAML files only (use `agent_context_check.py` only)
- If the user explicitly says VPS validation is not needed for this step

## Step-by-Step Process

1. **Identify changed files** from the final report
2. **Identify new artifacts** — path, namespace, expected key fields
3. **Identify dependency changes** — did `requirements.txt` change?
4. **Generate the validation sequence** using `.agent/task_templates/vps_validation_prompt.md`

Fill in:
- `{{CHANGED_FILES}}` — space-separated list of py files
- `{{TEST_FILE}}` — test module name
- `{{ARTIFACT_PATHS}}` — `ls` and `cat` commands for new artifact files
- `{{ARTIFACT_KEY_FIELDS}}` — key fields to verify in the artifact JSON

## Required Final Output

A copyable bash block with these sections, in order:

```bash
# 1. Activate environment
source .venv/bin/activate

# 2. Git status + pull
git status && git pull

# 3. Install dependencies (if requirements.txt changed)
pip install -r requirements.txt

# 4. Compile check
python -m py_compile <changed_files>

# 5. Targeted tests
python -m pytest -q tests/<test_file>.py

# 6. Agent context check (if .agent/ files changed)
python scripts/agent_context_check.py

# 7. Full suite
python -m pytest -q \
  --ignore=tests/test_gui_api_health.py \
  --ignore=tests/test_gui_insight_cards.py

# 8. Artifact verification (after live run)
ls -la outputs/latest/
ls -la outputs/policy/
cat outputs/latest/<artifact>.json | python -m json.tool | head -20

# 9. Optional: dry-run pipeline (if main.py changed)
bash scripts/preflight.sh
bash scripts/run_daily_safe.sh --dry-run
```

Always end with: "Report these results back. The step will be marked complete in `.agent/project_state.yaml` after VPS validation passes."
