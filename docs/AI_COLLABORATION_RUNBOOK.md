# AI Collaboration Runbook

## Purpose

Step-by-step guide for working with Claude, Codex, and GPT on the Portfolio
Automation System. Use this when starting a new feature, reviewing a diff,
updating docs, or running VPS validation.

---

## Starting a New Feature

### 1. Check current project state

```bash
python scripts/agent_context_check.py
```

This prints: current phase, current step, next official steps, completed steps count,
forbidden changes count, advisory-only flag, no-auto-trading flag.

If the output does not match what you expect, update `.agent/project_state.yaml` before
creating a task packet.

### 2. Confirm the step is authorized

Check `.agent/project_state.yaml:next_official_step`. If the feature you want is not
listed, it is either deferred or not yet approved. Do not ask Claude to implement it.

If you want to add a new step, update `next_official_step` in `project_state.yaml` first.

### 3. Create a task packet (GPT role)

Use the template at `.agent/task_templates/claude_feature_prompt.md`.

Fill in all placeholders:
- `{{STEP_NAME}}` — must match an entry in `next_official_step`
- `{{GOAL}}` — one sentence
- `{{CONTEXT}}` — relevant architecture and project state
- `{{TARGET_FILES}}` — exact list of files to create or modify
- `{{REQUIREMENTS}}` — numbered requirements
- `{{OUT_OF_SCOPE}}` — what NOT to do
- `{{TEST_COMMANDS}}` — exact pytest commands
- `{{ACCEPTANCE_CRITERIA}}` — pass/fail checks

---

## Prompting Claude

### Do

- Provide the filled-in task packet from `.agent/task_templates/claude_feature_prompt.md`.
- Attach the current `.agent/project_state.yaml` content as context.
- State explicitly what is observe-only and what is optional.
- State explicitly what must NOT change.

### Do Not

- Ask Claude to "figure out the next step." GPT controls the roadmap.
- Ask Claude to add auto-trading or broker integration.
- Ask Claude to modify scoring or allocation unless you explicitly authorize it in the prompt.
- Ask Claude to start Discovery Engine work unless it is in `next_official_step`.

### Example opening

```
Read .agent/project_state.yaml.

Current step: {{STEP_NAME}}
Goal: {{GOAL}}

[Paste task packet content]
```

---

## Prompting Codex (Docs Update)

Use the template at `.agent/task_templates/codex_docs_prompt.md`.

When to use:
- After Claude completes a new module
- When `docs/<MODULE>.md` is missing or outdated
- When `docs/roadmap.md` needs updating
- When `docs/OUTPUT_ARTIFACT_CONTRACTS.md` needs a new artifact entry

When NOT to use:
- To generate new code (that is Claude's role)
- To change runtime behavior (no docs update should do this)

### Example opening

```
Read AGENTS.md.
Read .agent/project_state.yaml.
Read the diff for [MODULE] that Claude just implemented.

Your task: update docs only.

[Paste codex_docs_prompt.md content]
```

---

## Prompting Codex (Code Review)

Use the template at `.agent/task_templates/codex_review_prompt.md`.

When to use:
- After Claude completes a significant feature
- When you want a second opinion on output schema compatibility
- When namespace compliance is uncertain
- When a dependency was added and VPS impact is unclear

When NOT to use:
- For trivial single-line fixes
- For docs-only changes

### Example opening

```
Read AGENTS.md.
Read .agent/project_state.yaml.

Your task: review the following changes Claude made.

[Paste diff or file list]
[Paste codex_review_prompt.md content]
```

---

## When to Use Codex Review

Use Codex review when:
- A new module touches `main.py` pipeline integration
- Output artifact schemas might have changed
- A new namespace is used for the first time
- A new Python dependency was added
- Tests were modified or a test module was skipped

Skip Codex review for:
- Small docs-only changes
- Test-only changes that don't touch production code
- Roadmap status updates in YAML files

---

## When to Run VPS Validation

Always after:
- Any change to `main.py` pipeline integration
- Any new file write path in `outputs/`
- Any change to `requirements.txt`
- Any change to the systemd timer or cron scripts

The VPS validation commands are in the Claude final report and in
`.agent/task_templates/vps_validation_prompt.md`.

---

## VPS Validation Process

Claude does not run on the VPS. All production validation is manual.

1. Copy the VPS validation commands from Claude's final report.
2. SSH into the VPS.
3. Run the commands in order:

```bash
cd /path/to/repo
source .venv/bin/activate

git status
git pull

pip install -r requirements.txt

python -m py_compile <changed_files>

python -m pytest -q tests/<targeted_test>.py

python -m pytest -q \
  --ignore=tests/test_gui_api_health.py \
  --ignore=tests/test_gui_insight_cards.py

# If main.py changed:
bash scripts/preflight.sh
bash scripts/run_daily_safe.sh --dry-run

# Verify artifacts:
ls -la outputs/latest/
cat outputs/latest/ai_budget_summary.json | python -m json.tool
```

4. Report the results back so GPT can update `project_state.yaml`.

---

## Avoiding Context Drift

Context drift happens when agents work from outdated or inconsistent state.

Prevention:

| Action | When |
|--------|------|
| Run `python scripts/agent_context_check.py` | Before any task |
| Update `.agent/project_state.yaml` | After every completed step |
| Update `.agent/phase_status.yaml` | After every completed step |
| Update `docs/roadmap.md` | After every completed step |
| Attach project_state.yaml to task packets | Always |
| Confirm current step matches task | Before Claude starts |

---

## Handling Conflicting Next-Step Recommendations

Claude's final report includes a "Recommended next step." This is advisory only.

If Claude recommends something not in `project_state.yaml:next_official_step`:

1. Check whether it is a sensible addition or a premature skip.
2. If sensible: update `next_official_step` in `project_state.yaml` and then create the task packet.
3. If premature (e.g., Discovery Engine before Calibration): ignore it and proceed with the authorized next step.
4. Never let Claude's recommendation automatically become the next task.

---

## Capturing Final Reports

Claude returns a final report at the end of every implementation. Capture it as follows:

1. Copy the entire final report section from Claude's response.
2. Optionally save it to a file named `docs/CHANGELOG_<STEP_NAME>_<date>.md` if you want a permanent record.
3. Use the VPS commands section to run production validation.
4. After validation, update `.agent/project_state.yaml`:
   - Move current_step to `completed_steps`
   - Set `current_step` to the next step
   - Update `next_official_step` as needed

---

## Updating `.agent/project_state.yaml`

After a step is complete:

```yaml
# Move from next_official_step to completed_steps:
completed_steps:
  - ... (existing)
  - <newly_completed_step>

# Update current_step:
current_step: <next_step_name>

# Update next_official_step:
next_official_step:
  primary: <next_next_step>
```

Run the context check to verify:

```bash
python scripts/agent_context_check.py
```

---

## Quick Reference: Who Does What

| Task | Agent |
|------|-------|
| Design feature | GPT |
| Create task packet | GPT |
| Write Python module | Claude |
| Write tests | Claude |
| Pipeline integration | Claude |
| Final report | Claude |
| VPS validation | User |
| Docs update | Codex |
| Code review | Codex |
| Roadmap update | GPT (or User) |
| Scope decisions | User |
