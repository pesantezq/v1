# Codex Skill: portfolio-task-packet

## Purpose

Create a structured Claude task packet from the current roadmap state.
Used by GPT or the user to prepare the next implementation step for Claude.

## When to Use

- When starting a new feature step
- After a step completes and the next step needs a task packet
- When `.agent/project_state.yaml:next_official_step` has been updated and Claude needs a prompt

## When NOT to Use

- To implement the feature (Claude's role)
- To review the feature after implementation (use portfolio-review)
- To update docs (use portfolio-docs)
- When the next step is not yet decided

## Instructions

1. **Read `.agent/project_state.yaml`** — get `next_official_step`, `completed_steps`, `forbidden_changes`, `output_namespace_policy`.
2. **Read `.agent/phase_status.yaml`** — get the step description, prerequisites, and notes.
3. **Read `.agent/task_templates/claude_feature_prompt.md`** — this is the template to fill in.
4. Fill in all placeholders:
   - `{{STEP_NAME}}` — exact name from `next_official_step`
   - `{{GOAL}}` — one sentence from phase_status.yaml description
   - `{{CONTEXT}}` — relevant architecture, prerequisites, current project state
   - `{{TARGET_FILES_CREATE}}` — new files Claude should create
   - `{{TARGET_FILES_MODIFY}}` — existing files Claude should modify
   - `{{REQUIREMENTS}}` — numbered requirements
   - `{{OUT_OF_SCOPE}}` — explicit list of what not to do
   - `{{TEST_COMMANDS}}` — exact test commands
   - `{{CHANGED_FILES}}` — for compile check
   - `{{TEST_FILE}}` — test module name
   - `{{ACCEPTANCE_CRITERIA}}` — specific pass/fail checks
5. Attach the current content of `.agent/project_state.yaml` to the packet.
6. Return the complete filled-in task packet.

## Rules

- Do not add scope beyond what is in `next_official_step`
- Do not include Discovery Engine scope unless it is in `next_official_step`
- Do not include auto-trading or broker integration
- Do not suggest skipping tests
- Always include the observe-only requirement
- Always include the VPS warning

## Final Output Format

Return the complete filled-in task packet, ready to paste into a Claude conversation.
Followed by:

```
## Task Packet Summary

Step: [step_name]
Phase: [current_phase]
Prerequisites met: [yes/no — list any unmet prerequisites]
Forbidden changes called out: [yes]
Test commands included: [yes]
Out-of-scope clearly stated: [yes]
VPS warning included: [yes]
```
