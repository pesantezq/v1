# Decision Engine

Status: implemented and tested, pending observe-only pipeline integration.

## Purpose

The Decision Engine is the central advisory unification layer for the Portfolio Automation System. It takes already-produced signals and recommendations from multiple subsystems and turns them into one ranked operator-facing action plan.

It is advisory only. It does not place trades, call a broker, or change portfolio state by itself.

## Why It Exists

Before this module, the system could produce several parallel recommendation streams:

- structural guardrail violations
- portfolio adjustments
- scored finance recommendations
- watchlist scanner signals
- market opportunity suggestions

Those streams were individually useful, but they were not normalized into one comparable decision list. `portfolio_automation/decision_engine.py` solves that by:

- translating heterogeneous source records into one decision record shape
- enforcing consistent decision labels such as `BUY`, `SELL`, `SCALE`, `HOLD`, `WAIT`, and `AVOID`
- preserving structural guardrails as the highest-priority advisory constraint
- applying portfolio-level overrides in one place for non-authoritative opportunity decisions
- producing a ranked plan plus a readable summary

## Current State

Current implementation status:

| Item | Status |
| --- | --- |
| `portfolio_automation/decision_engine.py` | Implemented |
| `portfolio_automation/__init__.py` | Present |
| `tests/test_decision_engine.py` | Present |
| Unit tests collected | 39 |
| Pipeline wiring | Not yet live |
| Output artifacts `decision_plan.json` / `decision_plan.md` | Approved next step, not yet emitted |

This means the module is ready for observe-only adoption, but should not be described as already live in daily pipeline outputs.

## Input Sources

The engine accepts five source families:

| Source | Input shape | Converter | Notes |
| --- | --- | --- | --- |
| Structural violations | guardrail breach dicts | `decision_from_structural_violation` | Highest authority; produces `SELL` |
| Portfolio adjustments | portfolio adjustment dicts | `decision_from_portfolio_adjustment` | Existing rules-based portfolio advice |
| Watchlist signals | watchlist scanner signal dicts | `decision_from_watchlist_signal` | Uses conviction, signal, confidence, and holding context |
| Market opportunities | broader market opportunity dicts | `decision_from_market_opportunity` | Used for underweight or contribution-style deployment ideas |
| Finance recommendations | scored finance recommendation dicts | `decision_from_finance_recommendation` | Non-trade financial guidance normalized into the same plan |

Portfolio context is passed separately into all relevant functions. Current context fields used include:

- `total_portfolio_value`
- `cash`
- `current_holdings`
- `degraded_mode`
- `data_mode`
- `drawdown_regime`
- `active_structural_violations`

## Core Functions

| Function | Responsibility |
| --- | --- |
| `decision_from_structural_violation` | Converts structural guardrail breaches into authoritative `SELL` decisions |
| `decision_from_portfolio_adjustment` | Converts portfolio adjustment records into normalized advisory decisions |
| `decision_from_watchlist_signal` | Converts watchlist scanner output into `BUY`, `SCALE`, `WAIT`, `HOLD`, or `AVOID` |
| `decision_from_market_opportunity` | Converts underweight or contribution opportunities into `BUY` or `SCALE` |
| `decision_from_finance_recommendation` | Converts finance recommendations into normalized advisory decisions |
| `apply_decision_overrides` | Applies degraded-data, drawdown, and guardrail-conflict caps to non-authoritative decisions |
| `build_decision_plan` | Orchestrates all inputs into one ranked list |
| `rank_decisions` | Applies final priority ordering and tiebreaks |
| `summarize_decision_plan` | Produces operator-readable summary text |

## Output Schema

The current module-level output is a ranked `list[dict]` of decision records.

### Decision Record Fields

| Field | Type | Meaning |
| --- | --- | --- |
| `symbol` | `str` | Symbol or logical identifier for the decision |
| `decision` | `str` | One of `BUY`, `SELL`, `SCALE`, `HOLD`, `WAIT`, `AVOID` |
| `priority` | `float` | Ranking score used for ordering |
| `urgency` | `str` | One of `critical`, `high`, `medium`, `low` |
| `source` | `str` | One of `structural`, `portfolio`, `watchlist`, `market`, `finance` |
| `recommended_action` | `str` | Operator-facing action text |
| `recommended_amount` | `float \| null` | Suggested dollar amount when available |
| `recommended_allocation_pct` | `float \| null` | Suggested allocation percentage when available |
| `reason` | `str` | Human-readable rationale |
| `risk_flags` | `list[str]` | Risk or downgrade flags such as `low_confidence` or `guardrail_conflict` |
| `confidence` | `float` | Advisory confidence for the decision record |
| `inputs_used` | `dict` | Source-specific normalized inputs used to make the decision |

### Summary Output

`summarize_decision_plan` returns a plain-text summary string. It is designed for operators, logs, memo inclusion, and future Markdown artifact generation.

## Decision Priority Model

The engine ranks decisions by priority descending, then by decision strength. `AVOID` decisions always trail actionable records even if their raw source score is non-trivial.

### Source Priority Ceilings

| Source | Ceiling |
| --- | ---: |
| Structural | `1.00` |
| Portfolio | `0.90` |
| Finance | `0.80` |
| Market | `0.65` |
| Watchlist | `0.65` |

### Structural Anchors

| Violation type | Typical priority | Urgency |
| --- | ---: | --- |
| `leverage` | `0.95` | `critical` |
| `concentration` | `0.88` | `high` |
| `drift` | `0.76` | `high` |

## Structural Violation Authority

Structural `SELL` decisions are authoritative. This is a core invariant of the module.

Actual current behavior:

- `decision_from_structural_violation` always emits `SELL`
- `apply_decision_overrides` never downgrades `SELL`
- `build_decision_plan` does not route structural violations through override capping

This means degraded data mode, bear drawdown regime, and similar safety caps do not weaken a structural `SELL`.

## Override Behavior

`apply_decision_overrides` applies safety caps in this order:

1. `degraded_mode=True` or `data_mode="fallback"` caps `BUY` and `SCALE` to `WAIT`
2. `drawdown_regime in {"bear", "severe"}` caps non-structural `BUY` and `SCALE` to `HOLD`
3. symbol conflict with `active_structural_violations` caps `BUY` and `SCALE` to `HOLD`

Important invariants:

- `SELL` is never downgraded
- overrides return a modified copy and do not mutate the original record
- `risk_flags` capture why the downgrade happened

## Source-Specific Decision Mapping

### Structural Violations

- always normalize to `SELL`
- severity determines urgency and base priority
- intended to surface guardrail-first action

### Portfolio Adjustments

Current mapping:

- `sell` or `trim` -> `SELL`
- `buy` or `add` -> `BUY`
- otherwise -> `HOLD`

Current orchestration detail:

- portfolio adjustments are treated as pre-evaluated advisory outputs inside `build_decision_plan`
- they are not currently passed through `apply_decision_overrides`

### Watchlist Signals

Current watchlist mapping uses:

- `conviction_band`
- `conviction_score`
- `signal_score`
- `confidence_score`
- `effective_score`
- existing-holding context
- cooldown state

Typical outcomes:

- sub-starter conviction -> `AVOID`
- low confidence -> `WAIT` for new names, `HOLD` for existing holdings
- cooldown active -> `WAIT` or `HOLD`
- high conviction existing holding -> `SCALE`
- high conviction new opportunity -> `BUY`

### Market Opportunities

Current market mapping:

- new symbol opportunity -> `BUY`
- existing holding opportunity -> `SCALE`
- priority depends on `opportunity_type`

### Finance Recommendations

Current finance mapping is text-guided:

- action contains `sell` or `reduce` -> `SELL`
- action contains `buy` or `add` -> `BUY`
- otherwise -> `HOLD`

This keeps finance recommendations visible in the unified plan without pretending they are trade execution instructions.

## Testing Coverage

`tests/test_decision_engine.py` currently collects 39 tests.

Covered areas include:

- structural violations outrank watchlist buys
- leverage breaches become `critical` `SELL`
- underweight market opportunities become `BUY`
- low-confidence watchlist signals cap to `WAIT` or `HOLD`
- degraded or fallback data downgrades non-authoritative opportunity decisions
- structural `SELL` is not downgraded
- existing holding plus strong conviction becomes `SCALE`
- weak conviction becomes `AVOID`
- ranking sorts by priority and keeps `AVOID` trailing
- missing optional fields do not crash converters
- summary rendering surfaces symbols, decisions, urgency, degraded state, and risk flags
- override layer does not mutate original inputs
- source-level priority ordering stays intact

Test command:

```bash
python3 -m pytest tests/test_decision_engine.py -q
```

## Example Output

Example decision record:

```json
{
  "symbol": "NVDA",
  "decision": "BUY",
  "priority": 0.5506,
  "urgency": "high",
  "source": "watchlist",
  "recommended_action": "Open NVDA position.",
  "recommended_amount": 2000.0,
  "recommended_allocation_pct": 0.04,
  "reason": "Band=high conviction, conviction=0.88, signal=0.82, confidence=0.91.",
  "risk_flags": [],
  "confidence": 0.88,
  "inputs_used": {
    "conviction_band": "high_conviction",
    "conviction_score": 0.88,
    "signal_score": 0.82,
    "confidence_score": 0.91,
    "effective_score": 0.85,
    "sizing_multiplier": 1.0,
    "cooldown_active": false,
    "data_mode": "live",
    "is_existing_holding": false
  }
}
```

Example summary usage:

```python
plan = build_decision_plan(...)
summary_text = summarize_decision_plan(plan, portfolio_context)
```

## Future Integration Path

Approved next integration direction:

- add `outputs/latest/decision_plan.json`
- add `outputs/latest/decision_plan.md`
- do not change existing recommendation behavior yet
- do not change existing output schemas
- call `build_decision_plan` only after portfolio adjustments, finance recommendations, watchlist signals, and market opportunities already exist
- log the top 3 decisions during the run
- treat the decision plan as additive only until validated

Recommended observe-only wiring sequence:

1. gather existing source outputs in `main.py` after portfolio adjustments, finance recommendations, watchlist signals, and market opportunities are available
2. call `build_decision_plan(...)`
3. serialize the ranked records to `decision_plan.json`
4. serialize `summarize_decision_plan(...)` to `decision_plan.md`
5. log the top three ranked decisions
6. leave all current recommendation artifacts untouched

## Next Implementation Step

Wire `build_decision_plan` into the daily pipeline as an additive observe-only artifact producer, then validate that `decision_plan.json` and `decision_plan.md` appear without changing any existing recommendation or GUI contracts.
