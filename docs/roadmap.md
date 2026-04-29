# Roadmap

## Completed: Decision Engine Foundation

What was built:

- `portfolio_automation/decision_engine.py`
- `portfolio_automation/__init__.py`
- `tests/test_decision_engine.py`

Why it matters:

- it creates one normalized advisory layer across structural violations, portfolio adjustments, finance recommendations, watchlist signals, and market opportunities
- it preserves structural guardrails as first-class ranked decisions
- it gives future GUI, memo, and AI explanation layers one unified decision surface instead of multiple disconnected outputs

Current status:

- module complete
- tests complete
- implemented and tested, pending observe-only pipeline integration

## Next Phase: Observe-Only Pipeline Integration

Approved direction:

- call `build_decision_plan` after the existing recommendation inputs already exist
- emit additive-only artifacts:
  - `outputs/latest/decision_plan.json`
  - `outputs/latest/decision_plan.md`
- log the top 3 decisions
- do not change current recommendation behavior
- do not change current output schemas

Success criteria for this phase:

- the daily run completes without regression
- the decision plan is present as a new additive artifact
- existing consumers continue to work unchanged

## Later Phases

### GUI Decision Center

- surface ranked decisions in one operator view
- show source, urgency, risk flags, and evidence trail
- keep the display observe-only

### AI Explanation Layer

- generate plain-language explanations from the normalized decision records
- preserve source attribution and guardrail authority
- avoid replacing the underlying rule outputs

### Policy Feedback Tuning

- compare decision-plan outcomes against later recommendation history
- evaluate downgrade rules and priority ordering
- tune only after observe-only behavior is stable and measured

## Next Implementation Step

Integrate the Decision Engine into the daily run as an additive observe-only layer and validate the new artifacts before expanding GUI or AI-facing surfaces.
