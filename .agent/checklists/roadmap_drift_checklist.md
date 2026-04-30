# Roadmap Drift Checklist

Use before starting a task, when reviewing Claude's output, or when evaluating a next-step recommendation.
Roadmap drift happens when features are built out of sequence, prematurely, or outside scope.

---

## Before Starting Any Task

- [ ] Current step matches `.agent/project_state.yaml:current_step`
  ```bash
  python scripts/agent_context_check.py
  ```
- [ ] Task packet specifies a step that is in `next_official_step`
- [ ] Task packet does not specify a step from `deferred_steps`
- [ ] Task packet does not specify a step from `permanently_deferred`

---

## Premature Discovery Engine Check

Discovery Engine must NOT be started unless explicitly listed in `next_official_step`.

- [ ] Is `discovery_engine_foundation` in `next_official_step`? If no, do not start it.
- [ ] Does any proposed code create a discovery promotion engine? Flag it.
- [ ] Does any proposed code move `discovery_only` signals to actionable without a corroboration gate? Flag it.

Note from `.agent/phase_status.yaml`:
```
discovery_engine_foundation:
  warning: PREMATURE_DISCOVERY_IS_FORBIDDEN
```

---

## Premature Calibration Check

Confidence calibration requires 20 resolved decisions (the gating rule).

- [ ] Is the calibration module being modified to lower the gate? Flag it.
- [ ] Is calibration feedback being inserted before the required foundation is complete? Flag it.
- [ ] Does the task change `decision_outcome_tracker.py` calibration thresholds? Flag it.

---

## Next-Step Recommendation Review

When Claude returns a "Recommended next step" in a final report:

- [ ] Is the recommended step in `project_state.yaml:next_official_step`? If yes: acceptable.
- [ ] Is the recommended step NOT in `next_official_step`? Mark it as advisory, not authoritative.
- [ ] Does the recommendation skip a phase step that is still pending? Flag it.
- [ ] Does the recommendation suggest Discovery Engine before Calibration? Reject it.

---

## Scope Creep Check

During implementation review:

- [ ] Claude implemented only what was in the task packet
- [ ] No extra modules created beyond what was requested
- [ ] No extra pipeline integrations added beyond what was specified
- [ ] No scoring or allocation changes added beyond what was authorized
- [ ] No new GUI pages or tabs added beyond what was specified
- [ ] No new external service dependencies added beyond what was specified

---

## Optional vs Required Feature Label

Recommendations marked optional in a final report must be labeled clearly:

- [ ] Optional recommendations are labeled as "optional" or "deferred"
- [ ] Optional items are not in the final `next_official_step` unless approved
- [ ] Optional items do not appear in `current_step` in `project_state.yaml`

---

## Project State Accuracy

After marking a step complete:

- [ ] `.agent/project_state.yaml:completed_steps` includes the step
- [ ] `.agent/project_state.yaml:current_step` reflects the actual next step in progress
- [ ] `.agent/project_state.yaml:next_official_step` is updated
- [ ] `.agent/phase_status.yaml` reflects the correct status for the completed step
- [ ] `docs/roadmap.md` reflects step completion

---

## Drift Detection Pass Criteria

All checks pass = no roadmap drift.

Any drift detected: flag it explicitly in the review response. Do not silently proceed.
The user controls roadmap decisions — surface conflicts for human resolution.
