# Claude Feature Implementation Prompt Template

Copy this template and fill in all `{{PLACEHOLDERS}}` before sending to Claude.
Attach `.agent/project_state.yaml` content as additional context.

---

## Task: {{STEP_NAME}}

Read `.agent/project_state.yaml` before starting.
Confirm `{{STEP_NAME}}` is in `next_official_step`. If it is not, stop and ask.

## Continuous Mission Orchestration — always applies

Read `.agent/mission_registry.yaml` before implementation.

- The requested mission must equal the protected current mission across project_state, phase_status, and config/ew0a_runtime.json.
- Do not wait or poll for GitHub CI/Codex after pushing a candidate.
- Do not merge your own PR.
- Do not advance protected state or authorize the next mission.
- The trusted GitHub controller owns exact-head CI, exact-head Codex review, SHA-guarded merge, post-merge main certification, and any separately preauthorized transition.
- Automatic transitions are allowed only when the registry says `preauthorized_auto`.
- A `human_required` transition is a hard stop. This always includes E4/real-evidence/production/capital/C1 boundaries unless separately authorized.
- Ordinary missions must not modify the controller workflows/scripts or mission registry.
- If running under the GitHub Claude handoff workflow, push the implementation branch and return the final report; the controller opens/certifies the PR.
- If running interactively and opening the PR yourself, include this body marker:

```
<!-- northstar-orchestration
kind: implementation
mission_id: {{STEP_NAME}}
starting_main_sha: <exact starting main SHA>
-->
```

---

## Goal

{{GOAL}}

---

## Context

{{CONTEXT}}

Current project state:
- Phase: (from project_state.yaml:current_phase)
- Step: {{STEP_NAME}}
- Advisory-only: true — no scoring/allocation/recommendation changes
- Observe-only default: true — new features must set observe_only: true in artifacts
- Output namespaces: LATEST for per-run artifacts, POLICY for budget/governance, HISTORICAL for replay only

---

## Target Files

Files to create:
{{TARGET_FILES_CREATE}}

Files to modify:
{{TARGET_FILES_MODIFY}}

---

## Requirements

{{REQUIREMENTS}}

General requirements (always apply):
- Keep behavior observe-only unless explicitly stated otherwise
- New modules must be additive and backward-compatible
- All file writes must use safe_write_json or safe_write_text from data_governance.py
- All output artifacts must include observe_only: true
- Pipeline integration in main.py must be wrapped in try/except (non-blocking)
- All new modules must have tests in tests/

---

## Out of Scope

{{OUT_OF_SCOPE}}

Always out of scope (regardless of task):
- Changing scoring, allocation, or recommendation behavior
- Adding broker API calls or auto-trading
- Removing or bypassing observe_only flags
- Changing output artifact schemas without authorization
- Starting Discovery Engine implementation
- Changing protected semantics: signal_score, confidence_score, effective_score,
  conviction_score, final_rank_score, recommendation_score

---

## Test Commands

```bash
{{TEST_COMMANDS}}

# Always run:
python -m py_compile {{CHANGED_FILES}}
python -m pytest -q tests/{{TEST_FILE}}.py
python -m pytest -q \
  --ignore=tests/test_gui_api_health.py \
  --ignore=tests/test_gui_insight_cards.py
```

---

## Acceptance Criteria

{{ACCEPTANCE_CRITERIA}}

Standard acceptance criteria (always apply):
- [ ] py_compile passes on all changed files
- [ ] Targeted tests pass
- [ ] Full suite passes (ignoring known GUI health tests)
- [ ] No forbidden changes made
- [ ] Artifacts written to correct namespaces
- [ ] observe_only: true present in all new artifact payloads
- [ ] Pipeline integration is non-blocking (wrapped in try/except)
- [ ] Final report returned with VPS validation commands

---

## Final Report Required

Return the final report using `.agent/task_templates/final_report_template.md`.

Do not claim VPS tests passed — Claude runs locally. Return VPS commands for the user to run.
