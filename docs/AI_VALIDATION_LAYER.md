# AI Validation Layer

Last verified against `portfolio_automation/ai_decision_validator.py`, `main.py`, and `gui/app.py` on 2026-04-29.

## Purpose

The AI Validation Layer validates decisions after they are made. It does not score, rank, reallocate, or decide.

Architectural role:

- validate, not decide
- deterministic first
- observe-only only
- non-blocking on failure
- downstream consumer of `outputs/latest/decision_plan.json`

## Execution Order

```text
decision_plan.json
    -> portfolio_automation/ai_decision_validator.py
    -> deterministic validation
    -> optional LLM enhancement
    -> ai_decision_validation.json
    -> ai_decision_validation.md
```

The validator runs after the decision plan and explanation artifacts are written. It never feeds back into decision generation.

## Deterministic-First Design

The validator always applies deterministic rules first.

Validation statuses:

- `aligned`
  Decision and supporting narrative agree.
- `caution`
  Decision may still be valid, but confidence, degraded mode, fallback data, or other uncertainty requires caution.
- `contradiction`
  Decision and action language conflict.
- `insufficient_context`
  The row does not contain enough structured or textual context for a safe validation judgment.

The optional LLM path is advisory only:

- enabled only when `AI_VALIDATOR_USE_LLM=1`
- non-blocking on failure
- cannot change decisions, scores, or allocations
- only rewrites the plain-English validation summary when successful

## Contradiction Rules

The validator checks whether a decision and its action language agree.

True contradiction examples:

- `WAIT` plus `deploy capital now`
- `WAIT` plus `buy 500 shares`
- `HOLD` plus `open new position`
- `AVOID` plus `scale position now`

Non-contradiction rule:

- `WAIT` plus negated capital language is not a contradiction

## Negation Handling Fix

The validator explicitly treats phrases like these as non-deployment language:

- `do not deploy capital`
- `do not buy`
- `stand by`
- `hold off`
- `wait for a better entry point`
- `until conditions improve`

Important fixed case:

- `WAIT` + `Stand by - do not deploy capital until conditions improve.`
  Expected result: `caution`, not `contradiction`

Positive deployment language still counts as contradiction:

- `deploy`
- `buy`
- `open new position`
- `scale position`
- `invest proceeds`

## Output Artifacts

- `outputs/latest/ai_decision_validation.json`
- `outputs/latest/ai_decision_validation.md`

Top-level JSON fields:

- `generated_at`
- `observe_only`
- `available`
- `total_validated`
- `aligned_count`
- `caution_count`
- `contradiction_count`
- `insufficient_context_count`
- `ai_used`
- `summary_line`
- `validations`

Validation row fields:

- `symbol`
- `decision`
- `validation_status`
- `plain_english_summary`
- `rule_alignment`
- `narrative_context`
- `contradictions`
- `watch_next`
- `ai_used`
- `model`
- `generated_at`

Compact limits:

- validation runs only on the top decision set already exposed downstream
- `watch_next` is capped to the validator's compact output limit

## Examples

### `aligned`

```text
SELL QLD
Status: aligned
Reason: structural leverage breach and sell action match.
```

### `caution`

```text
BUY NVDA
Status: caution
Reason: decision is plausible, but degraded data or lower-confidence context requires caution.
```

### `contradiction`

```text
WAIT FANG
Capital action: deploy capital now
Status: contradiction
Reason: WAIT conflicts with immediate deployment language.
```

### False-Positive Fix Case

```text
WAIT FANG
Capital action: Stand by - do not deploy capital until conditions improve.
Status: caution
Reason: negated deployment language is not treated as contradiction.
```

## GUI Integration

The GUI consumes validation artifacts in the `AI Validation` section.

Boundaries:

- GUI is read-only
- GUI does not recompute validation
- GUI does not change decisions
- validation remains downstream of `decision_plan.json`

## Invariants

- no trade execution
- no broker integration
- no decision mutation
- no rank mutation
- no scoring changes
- no allocation changes
- validator failure must not fail the daily pipeline

## Next Implementation Step

Use validation-status history as a calibration input for future analytics, but keep validation strictly downstream and non-authoritative.
