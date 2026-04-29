# Roadmap

## Completed: Decision Engine Foundation + Observe-Only Integration

What was built:

- `portfolio_automation/decision_engine.py`
- observe-only integration in `main.py`
- `outputs/latest/decision_plan.json`
- `outputs/latest/decision_plan.md`
- `tests/test_decision_engine.py`
- `tests/test_decision_engine_pipeline.py`

What was completed inside this phase:

- module implemented
- pipeline tests added
- additive pipeline artifacts added
- consolidation and symbol-level dedup completed
- validated final output shape established

Why it matters:

- the system now has one central observe-only action-plan layer
- structural guardrails, portfolio actions, finance guidance, watchlist signals, and market opportunities can be compared in one ranked list
- conflict resolution is explicit instead of being left to downstream readers
- existing recommendation behavior and existing schemas remain unchanged

Current status:

- implemented
- tested
- wired into the daily pipeline in observe-only mode
- additive only, not a replacement for the current recommendation stack

## What "Observe-Only" Means Here

- Decision Engine artifacts are written in parallel with existing outputs
- current recommendation logic is still the operational source of advice
- no trade execution behavior is introduced
- existing consumers are not forced to adopt the decision plan yet

## Next

### GUI Decision Center

- surface the ranked decision plan in one operator-facing panel
- show source, priority, urgency, risk flags, and merged reasons
- preserve the observe-only boundary

### AI Explanation Layer

- generate concise explanations from consolidated decision records
- preserve source attribution and structural authority
- use decision-plan artifacts as an additive explanation source

### Policy Feedback Loop Using Decision Outcomes

- measure how consolidated decisions perform over time
- compare decision-plan outcomes with later recommendation history
- tune precedence, suppression, and downgrade rules only after outcome evidence exists

## Next Implementation Step

Use the now-live observe-only decision-plan artifacts as the input contract for a GUI Decision Center and for explanation-layer prototypes, without changing the current recommendation engine behavior.
