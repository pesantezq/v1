# Operator Runbook

## Current State

The Decision Engine is implemented, tested, and integrated into `main.py` in observe-only mode. It writes additive decision-plan artifacts without changing existing recommendation behavior.

## How to Validate Decision Engine

Run the focused Decision Engine test suites:

```bash
python3 -m pytest tests/test_decision_engine.py tests/test_decision_engine_pipeline.py -q
```

Run the broader suite:

```bash
python3 -m pytest -q
```

Run the daily pipeline:

```bash
python3 main.py --run-mode daily
```

Check that the observe-only artifacts exist:

```bash
ls outputs/latest/decision_plan.json outputs/latest/decision_plan.md
```

Run a quick ranked-output sanity check:

```bash
python3 - <<'PY'
import json
data = json.load(open('outputs/latest/decision_plan.json'))
for d in data['decisions'][:8]:
    print(f"{d['decision']:6} {d.get('symbol','-'):6} pri={d['priority']:.3f} src={d['source']} urgency={d['urgency']}")
PY
```

## Expected Shape

Confirm the following:

- structural `SELL` decisions appear first
- no duplicate symbol-level decisions appear
- no `HOLD` remains for symbols with an active structural `SELL`
- underweight contribution targets appear as `SCALE` or `BUY`
- market opportunities remain `WAIT` unless conviction or confidence clears the rules

## Decision Plan Artifact Expectations

`outputs/latest/decision_plan.json` should contain:

- `generated_at`
- `run_mode`
- `observe_only`
- `total_decisions`
- `decisions`

Each decision record should contain:

- `symbol`
- `decision`
- `priority`
- `urgency`
- `source`
- `recommended_action`
- `recommended_amount`
- `recommended_allocation_pct`
- `reason`
- `risk_flags`
- `confidence`
- `inputs_used`

`outputs/latest/decision_plan.md` should contain a readable plan summary with top actions, urgency breakdown, and risk flags.

## Troubleshooting

### If duplicate symbols appear

- inspect `consolidate_decisions` in `portfolio_automation/decision_engine.py`
- verify source precedence and decision precedence behavior
- confirm generic placeholder symbols were not incorrectly treated as specific tickers

### If `PORTFOLIO` appears

- symbol inference for a structural or aggregate decision failed, or
- the decision intentionally remained generic because no specific holding could be resolved

For leverage issues, verify that the structural leverage violation could resolve to a leveraged holding such as `QLD`.

### If `QLD` or `QQQ` `HOLD` appears alongside a structural `SELL`

- conflict suppression regressed
- inspect `_suppress_structural_hold_conflicts`
- inspect symbol resolution for structural violations before consolidation

### If decision-plan artifacts are missing

- inspect the Decision Engine step in `main.py`
- inspect step 7 output writes in `main.py`
- confirm the run reached artifact-writing without a fatal earlier pipeline error
- check logs for `DECISION ENGINE:` warnings or output-write failures

## Operational Notes

- the Decision Engine is observe-only
- it is additive-only relative to existing recommendation behavior
- existing output schemas must remain intact
- top decisions should be visible in pipeline logs
- missing or malformed decision-plan artifacts should be treated as a visible regression

## Next Implementation Step

Use the integrated decision-plan artifacts as the validation target for the next GUI Decision Center and explanation-layer work, while keeping the existing recommendation stack unchanged.
