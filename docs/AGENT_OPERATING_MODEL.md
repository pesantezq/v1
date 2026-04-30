# Agent Operating Model

## Purpose

This document defines how Claude, Codex, GPT, and the user collaborate on the
Portfolio Automation System. It exists to prevent context drift, role confusion,
and premature implementation of features that are not yet on the roadmap.

---

## Advisory-Only Constraint

This system is advisory-only. It produces analysis, scores, recommendations,
and operator artifacts. It does not place trades. AI agents (Claude, Codex, GPT)
must never:

- Override deterministic scoring, allocation, or recommendation logic
- Add broker API calls or trade execution
- Make autonomous investment decisions
- Remove or bypass `observe_only: true` flags
- Silently change output artifact schemas

---

## Role Definitions

### GPT — Architecture Lead and Scope Gatekeeper

GPT is the planning and orchestration role. It does not write production code.

Responsibilities:
- Design the overall architecture and roadmap
- Create structured task packets for Claude using `.agent/task_templates/claude_feature_prompt.md`
- Control which roadmap step is next
- Decide when to promote a feature from observe-only to operational
- Review Claude's final reports and update `.agent/project_state.yaml`
- Reject scope creep before it reaches Claude
- Determine when Codex review or docs update is needed

GPT does NOT:
- Write Python modules
- Run test suites
- Deploy to VPS
- Make autonomous investment decisions

---

### Claude — Implementation Agent

Claude implements scoped features and returns structured delivery reports.

Responsibilities:
- Write Python modules for the step specified in the task packet
- Write tests for every new module
- Write or update module documentation
- Integrate new modules non-blockingly into `main.py`
- Compile-check and test locally
- Return a final report with VPS validation commands

Claude does NOT:
- Run on the VPS — all VPS validation is performed manually by the user
- Choose which feature to implement next (that is GPT's role)
- Approve scope expansion beyond the task packet
- Change scoring, allocation, or recommendation behavior without explicit user approval

---

### Codex — Documentation Agent and Code Reviewer

Codex operates after Claude completes a feature.

Responsibilities:
- Update `docs/<MODULE>.md` after a Claude-built feature
- Update `docs/roadmap.md` to reflect step completion
- Review Claude's diff for hidden behavior changes, namespace violations, and schema breaks
- Audit test coverage and flag gaps
- Check for dependency changes and note VPS install impact
- Maintain changelog

Codex does NOT:
- Implement features (that is Claude's role)
- Run test suites
- Deploy to VPS
- Make roadmap decisions (that is GPT's role)
- Change runtime behavior

---

### User — Production Validator and Decision-Maker

The user controls production deployment and all scope decisions.

Responsibilities:
- Run VPS validation commands returned by Claude
- Approve or reject scope proposals from GPT
- Deploy changes to VPS via git pull + pip install
- Verify production artifacts after deployment
- Decide when to promote features from observe-only to operational
- Update `.agent/project_state.yaml` after step completion (or instruct GPT to do so)

The user does NOT:
- Write Python modules (Claude does this)
- Update docs (Codex does this)
- Design roadmap steps (GPT does this)

---

## Collaboration Workflow

```
A. GPT creates task packet
     - Uses .agent/task_templates/claude_feature_prompt.md
     - Fills in: STEP_NAME, GOAL, CONTEXT, REQUIREMENTS, OUT_OF_SCOPE
     - Attaches .agent/project_state.yaml context

B. Claude implements
     - Reads .agent/project_state.yaml to confirm step is authorized
     - Implements the minimal scoped feature
     - Adds tests, docs, non-blocking pipeline integration
     - Returns final report using .agent/task_templates/final_report_template.md

C. Claude returns final report
     - Files created/modified
     - Behavior implemented
     - Artifacts written
     - Test results
     - Assumptions and risks
     - VPS validation commands (for user to run manually)
     - Recommended next step (from project_state.yaml only)

D. User runs VPS validation
     - Copies VPS validation commands from Claude's final report
     - Runs on VPS: git pull, pip install, py_compile, pytest, artifact check
     - Reports results

E. Codex updates docs/reviews diff (optional)
     - Reviews Claude's diff for issues
     - Updates docs/<MODULE>.md
     - Updates docs/roadmap.md
     - Flags any hidden behavior changes or contract breaks

F. GPT updates .agent/project_state.yaml
     - Moves completed step to completed_steps
     - Updates current_step
     - Confirms or adjusts next_official_step

G. Next task selected
     - GPT creates next task packet
     - Cycle repeats
```

---

## Roadmap Control Rules

1. The authoritative next step is always `next_official_step` in `.agent/project_state.yaml`.

2. Claude's recommended next step in a final report is advisory only — it is not authoritative.

3. GPT controls the roadmap. If Claude recommends a next step that is not in `next_official_step`, GPT evaluates it before adopting it.

4. Discovery Engine must not be started until `confidence_calibration_feedback_loop` is complete or explicitly prioritized by the user.

5. Deferred items in `deferred_steps` must not be implemented without explicit user approval and a roadmap update.

---

## Context Drift Prevention

Context drift happens when an agent implements something out of sequence or outside
the agreed scope, or when a later agent works from outdated project state.

Prevention rules:

- Always read `.agent/project_state.yaml` before starting work.
- Run `python scripts/agent_context_check.py` to get a quick context summary.
- Do not rely on conversation history alone for project state — use the YAML files.
- After each step, update `.agent/project_state.yaml` before starting the next one.
- If in doubt about scope, ask the user rather than implementing.

---

## Task Packet Format

See `.agent/task_templates/claude_feature_prompt.md` for the standard template.

Key fields:
- `STEP_NAME` — matches an entry in `next_official_step`
- `GOAL` — one sentence
- `CONTEXT` — relevant background from project_state.yaml and existing architecture
- `TARGET_FILES` — exact files to create or modify
- `REQUIREMENTS` — numbered, testable requirements
- `OUT_OF_SCOPE` — explicit list of what not to do
- `ACCEPTANCE_CRITERIA` — test commands and artifact checks

---

## Final Report Requirements

Claude must return a final report using `.agent/task_templates/final_report_template.md`.

The report must include:
1. Files created
2. Files modified
3. Behavior implemented
4. Artifacts written (path + namespace)
5. Tests added (file + count)
6. Test commands run
7. Test results
8. Assumptions
9. Risks
10. VPS validation commands (copyable block)
11. Recommended next step (from `project_state.yaml` only)

---

## Artifact Sources of Truth

| Artifact | Source |
|----------|--------|
| Project state | `.agent/project_state.yaml` |
| Roadmap status | `.agent/phase_status.yaml`, `docs/roadmap.md` |
| Output contracts | `docs/OUTPUT_ARTIFACT_CONTRACTS.md` |
| Pipeline runbook | `docs/PIPELINE_RUNBOOK.md` |
| Regression checklist | `docs/REGRESSION_CHECKLIST.md` |
| Agent rules | `docs/CLAUDE_AGENT_RULES.md` |
| Architecture | `docs/ARCHITECTURE.md` |
| Collaboration guide | `docs/AI_COLLABORATION_RUNBOOK.md` |
