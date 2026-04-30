# AGENTS.md — Codex Operating Instructions

This file is the primary Codex entry point for the Portfolio Automation System.
Read this before any code change, documentation update, or review task.

---

## Project Overview

This is an **advisory-only** portfolio intelligence system.

- It produces analysis, recommendations, and operator artifacts.
- It does **not** place trades, call broker APIs, or execute autonomous financial decisions.
- AI plays a support role only: context, explanation, validation, documentation, orchestration.
- All scoring, allocation, risk, and recommendation logic is deterministic Python.

---

## Advisory-Only Rule

**AI — including Codex — must never:**

- Generate, modify, or override scoring logic
- Generate, modify, or override allocation decisions
- Generate, modify, or override recommendation outcomes
- Call broker APIs
- Add auto-trading behavior
- Make autonomous investment decisions

**Codex's role is:**

- Documentation updates after Claude-built features
- Code review after Claude commits
- Regression review: tests, artifact schemas, namespace compliance
- Artifact contract audit: does output JSON match the declared contract?
- Cleanup recommendations (not implementation)
- Changelog maintenance

---

## Before Making Any Change

1. **Read `.agent/project_state.yaml`** — machine-readable source of truth for:
   - Current phase and step
   - Completed steps
   - Next official steps
   - Forbidden changes
   - Output namespace policy
   - Role split

2. **Read `.agent/phase_status.yaml`** — roadmap status with per-step details.

3. **Identify the current roadmap step.** Do not invent next steps. Do not reorder phases.

4. **Check forbidden_changes.** If the task touches a forbidden area, stop and flag it.

5. **For docs tasks:** Confirm you are only updating documentation, not runtime behavior.

6. **For review tasks:** Inspect diffs, tests, artifact JSON contracts, namespace usage, and test coverage.

---

## Required Behavior Before Code Changes

- Identify the exact files that need to change.
- Confirm the change does not violate `forbidden_changes` in `project_state.yaml`.
- Confirm output artifacts continue to match declared contracts in `docs/OUTPUT_ARTIFACT_CONTRACTS.md`.
- Confirm `observe_only: true` is preserved in all new artifact payloads.

---

## Required Behavior After Code Changes

- List all files created or modified.
- State which tests were added or updated.
- Confirm `python -m py_compile <files>` passes.
- Confirm `python -m pytest -q` passes or list failures with root cause.
- Confirm no output artifact schemas were changed without authorization.
- Confirm no forbidden changes were made.

---

## Test Expectations

- Every new module must have a corresponding test file in `tests/`.
- Compile check: `python -m py_compile <changed_files>`
- Targeted test: `python -m pytest -q tests/<relevant_test>.py`
- Full suite: `python -m pytest -q --ignore=tests/test_gui_api_health.py --ignore=tests/test_gui_insight_cards.py`

---

## Artifact Contract Rules

- Output JSON schemas must not change silently.
- If a schema changes, update `docs/OUTPUT_ARTIFACT_CONTRACTS.md`.
- GUI consumers and memo consumers must not break.
- Replay artifacts must never appear in `outputs/latest/`, `outputs/policy/`, or `outputs/portfolio/`.
- Live pipeline artifacts must never appear in `outputs/backtest/`.
- All writes must use `safe_write_json` or `safe_write_text` from `portfolio_automation/data_governance.py`.

---

## Documentation Update Requirements

When updating docs after a Claude feature:

- Update the relevant `docs/<MODULE>.md` file.
- Update `docs/roadmap.md` to reflect step completion.
- Do not update `docs/ARCHITECTURE.md` without a factual change in the code.
- Do not invent architectural decisions not present in the code.
- Do not remove or contradict existing content without verifying the code changed.

---

## Forbidden Changes

From `.agent/project_state.yaml:forbidden_changes`:

- Changing scoring behavior without explicit user approval
- Changing allocation behavior without explicit user approval
- Changing recommendation behavior without explicit user approval
- Breaking output artifact schemas
- Writing live namespace outputs from replay context
- Introducing auto-execution or trading
- Calling broker APIs
- Silently removing `observe_only` flags
- Recomputing decisions outside the decision engine
- Bypassing FMP registry compliance rules

---

## Handling Conflicting Next-Step Recommendations

If Claude suggests a next step not in `next_official_step` in `project_state.yaml`:

- Do not treat it as authoritative.
- Flag it in your response.
- Check `phase_status.yaml` for the correct next step.
- Do not start Discovery Engine work unless it is listed as `next_official_step`.

---

## Dependency Changes

If a new package is added:

- Confirm it is in `requirements.txt`.
- Note that VPS install requires `pip install -r requirements.txt` in the virtualenv.
- Include this note in your review response.

---

## How to Use Agent Context

```bash
# Quick context summary
python scripts/agent_context_check.py

# Read state directly
cat .agent/project_state.yaml
cat .agent/phase_status.yaml
```

---

## Final Response Format

```
## Codex Response

Task type: [docs update | code review | regression review | artifact audit | cleanup]
Files inspected: [list]
Files updated: [list or none]
Issues found: [list or none]
Artifact contract: [preserved | changed — describe change]
Namespace compliance: [pass | violations — describe]
Test coverage: [adequate | gaps — describe]
Forbidden change check: [clean | flag — describe]
Dependency impact: [none | describe VPS install step]
Recommended next step: [from project_state.yaml next_official_step only]
```

---

## Quick Reference

| Question | Where to look |
|----------|--------------|
| Current step? | `.agent/project_state.yaml:current_step` |
| What comes next? | `.agent/project_state.yaml:next_official_step` |
| Forbidden changes? | `.agent/project_state.yaml:forbidden_changes` |
| Output namespaces? | `.agent/project_state.yaml:output_namespace_policy` |
| Artifact schemas? | `docs/OUTPUT_ARTIFACT_CONTRACTS.md` |
| Roadmap detail? | `docs/roadmap.md`, `.agent/phase_status.yaml` |
| Test commands? | `docs/REGRESSION_CHECKLIST.md` |
| VPS deployment? | `docs/PIPELINE_RUNBOOK.md`, `scripts/` |
| Agent operating model? | `docs/AGENT_OPERATING_MODEL.md` |
| Collaboration runbook? | `docs/AI_COLLABORATION_RUNBOOK.md` |
