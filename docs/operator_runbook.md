# Operator Runbook

## Current State

The Decision Engine module is implemented and tested, but it is not yet wired into the production daily pipeline. Treat it as a completed foundation that still needs observe-only integration.

## How to Validate the Decision Engine

Run the focused Decision Engine test file:

```bash
python3 -m pytest tests/test_decision_engine.py -q
```

Run the broader test suite:

```bash
python3 -m pytest -q
```

What to confirm:

- the Decision Engine tests pass
- no regressions appear in the wider suite
- the module remains advisory only
- no pipeline or output contract changes are introduced unintentionally

## After Observe-Only Integration

Run the daily pipeline:

```bash
python3 main.py --run-mode daily
```

Check for the new additive artifacts:

```bash
ls outputs/latest/decision_plan.json outputs/latest/decision_plan.md
```

Additional validation points after integration:

- the daily run still completes successfully
- existing recommendation outputs are unchanged
- existing GUI consumers still load their expected artifacts
- top 3 decisions are logged during the run
- `decision_plan.json` and `decision_plan.md` reflect the same ranked plan

## Operational Notes

- `decision_plan.json` and `decision_plan.md` should be treated as additive outputs only
- structural `SELL` decisions should remain visible and undowngraded
- observe-only integration must not add execution behavior
- if artifact generation fails, the failure should be visible in logs rather than silently skipped

## Next Implementation Step

After the observe-only wiring lands, validate one full `daily` run and confirm the new decision plan artifacts exist without any change to the existing recommendation contract set.
